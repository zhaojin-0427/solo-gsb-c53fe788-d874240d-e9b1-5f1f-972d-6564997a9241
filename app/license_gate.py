"""字体资产许可与嵌入合规门禁。

管理员按使用场景（web/desktop/print）登记不可变策略版本；引擎读取字体的
OS/2 fsType、name 表许可描述（nameID 13）与许可 URL（nameID 14）、集合字体
成员及解包摘要，按字体栈组合逐条给出证据与确定性 allow / deny 结论。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from .font_loader import (
    FSTYPE_EDITABLE,
    FSTYPE_EMBEDDING_MASK,
    FSTYPE_FLAG_LABELS,
    FSTYPE_INSTALLABLE,
    FSTYPE_NO_SUBSETTING,
    FSTYPE_PREVIEW,
    FSTYPE_RESERVED_MASK,
    FSTYPE_RESTRICTED,
    normalize_evidence_fstype,
)

MANIFEST_KIND = "font-asset-license-embedding-gate"
MANIFEST_SCHEMA = "1.0"

SCENARIOS = ("web", "desktop", "print")

# 策略体修订标记：v1 基线曾把 fsType 类型值错按位位置解读（0x0002 误判为
# 预览印刷）。已登记版本不可原地改写，正确位定义以追加的修正版（revision=2）
# 发布并经 CAS 切换生效；逐条证据、冻结清单与幂等发布都引用同一修正版。
POLICY_REVISION_KEY = "fstype_bits_revision"
POLICY_REVISION_CORRECTED = 2
CORRECTED_POLICY_REGISTERED_BY = "system-bootstrap-fstype-fix"

# 必须的 name 许可字段 -> 证据键
NAME_FIELDS = {
    "license_description": "name_license_description",
    "license_url": "name_license_url",
}

# ---------------------------------------------------------------- 检查代码
FONT_DIGEST_MISMATCH = "font-digest-mismatch"
EVIDENCE_INCOMPLETE_COLLECTION = "evidence-incomplete-collection"
FSTYPE_MISSING = "fstype-missing"
FSTYPE_RESERVED_BITS = "fstype-reserved-bits"
FSTYPE_EMBEDDING_FLAGS_CONFLICT = "fstype-embedding-flags-conflict"
FSTYPE_MASK_NOT_ALLOWED = "fstype-mask-not-allowed"
FSTYPE_BIT_FORBIDDEN = "fstype-bit-forbidden"
NAME_FIELD_MISSING = "name-field-missing"
NAME_FIELD_BAD_URL = "name-field-bad-url"
LICENSE_TEXT_FORBIDDEN = "license-text-forbidden"
FSTYPE_TEXT_CONFLICT = "fstype-text-conflict"
TTC_MEMBER_LICENSE_MISMATCH = "ttc-member-license-mismatch"
STACK_LICENSE_CONFLICT = "stack-license-conflict"

SEVERITY_DENY = "deny"
SEVERITY_WARN = "warn"

_VALID_FLAG_LABELS = set(FSTYPE_FLAG_LABELS.values())
_VALID_RULE_TYPES = {"fstype-flag-text", "fstype-flag-pair", "license-text-pair"}


# ---------------------------------------------------------------- 策略校验

def validate_policy(policy: dict[str, Any]) -> None:
    """登记前对策略体做严格校验，拒绝不可解析的策略。"""
    if not isinstance(policy, dict):
        raise ValueError("策略必须是 JSON 对象")

    allowed = policy.get("allowed_fstype_masks", [])
    if not isinstance(allowed, list) or not all(
            isinstance(m, int) and 0 <= m <= 0xFFFF for m in allowed):
        raise ValueError("allowed_fstype_masks 必须是 0..65535 的整数数组")

    forbidden = policy.get("forbidden_fstype_bits", [])
    if not isinstance(forbidden, list) or not all(
            isinstance(b, int) and 0 <= b <= 0xFFFF for b in forbidden):
        raise ValueError("forbidden_fstype_bits 必须是整数数组")

    required = policy.get("required_name_fields", [])
    if not isinstance(required, list) or any(r not in NAME_FIELDS for r in required):
        raise ValueError(
            f"required_name_fields 只能包含 {sorted(NAME_FIELDS)}")

    patterns = policy.get("forbidden_license_patterns", [])
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ValueError("forbidden_license_patterns 必须是字符串数组")
    for p in patterns:
        try:
            re.compile(p)
        except re.error as exc:
            raise ValueError(f"非法正则 {p!r}: {exc}") from exc

    rules = policy.get("conflict_rules", [])
    if not isinstance(rules, list):
        raise ValueError("conflict_rules 必须是数组")
    for i, rule in enumerate(rules):
        _validate_rule(rule, i)

    for flag_key in ("deny_on_fstype_reserved_bits", "deny_on_embedding_flags_conflict",
                     "deny_on_ttc_member_license_mismatch",
                     "deny_on_incomplete_collection_evidence",
                     "require_https_license_url"):
        if flag_key in policy and not isinstance(policy[flag_key], bool):
            raise ValueError(f"{flag_key} 必须是布尔值")


def _validate_rule(rule: Any, i: int) -> None:
    if not isinstance(rule, dict):
        raise ValueError(f"conflict_rules[{i}] 必须是对象")
    rtype = rule.get("type")
    if rtype not in _VALID_RULE_TYPES:
        raise ValueError(
            f"conflict_rules[{i}].type 必须是 {sorted(_VALID_RULE_TYPES)} 之一")
    if not isinstance(rule.get("message", ""), str):
        raise ValueError(f"conflict_rules[{i}].message 必须是字符串")
    if rtype == "fstype-flag-text":
        _validate_flag(rule.get("flag"), f"conflict_rules[{i}].flag")
        for key in ("pattern",):
            if not isinstance(rule.get(key), str):
                raise ValueError(f"conflict_rules[{i}].{key} 必须是字符串")
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            raise ValueError(f"conflict_rules[{i}].pattern 非法正则: {exc}") from exc
        if rule.get("field", "license_description") not in NAME_FIELDS:
            raise ValueError(f"conflict_rules[{i}].field 非法")
    elif rtype == "fstype-flag-pair":
        _validate_flag(rule.get("flag_a"), f"conflict_rules[{i}].flag_a")
        _validate_flag(rule.get("flag_b"), f"conflict_rules[{i}].flag_b")
    else:  # license-text-pair
        for key in ("pattern_a", "pattern_b"):
            if not isinstance(rule.get(key), str):
                raise ValueError(f"conflict_rules[{i}].{key} 必须是字符串")
            try:
                re.compile(rule[key])
            except re.error as exc:
                raise ValueError(f"conflict_rules[{i}].{key} 非法正则: {exc}") from exc


def _validate_flag(value: Any, where: str) -> None:
    if value not in _VALID_FLAG_LABELS:
        raise ValueError(f"{where} 必须是 {sorted(_VALID_FLAG_LABELS)} 之一")


# ---------------------------------------------------------------- 默认策略

def legacy_default_policies_v1() -> dict[str, dict[str, Any]]:
    """三个场景的初始基线策略（v1，已冻结保留，不得原地改写）。

    v1 把 fsType 的 4 位嵌入类型值错当成独立位位置解读：0x0002（Restricted
    License embedding，受限禁止嵌入）被误标为“预览印刷”，并被印刷策略放行；
    同时 0x0008 被误当作 no-subsetting、0x0100 被误当作 bitmap-only。
    此函数仅为保留历史登记事实与让旧冻结决定可按原版本复核而存在，
    新部署/新评估一律使用 default_policies() 的修正版（经 CAS 追加生效）。
    """
    return {
        "web": {
            "name": "Web 字体嵌入基线策略",
            # 仅允许可安装嵌入（含同时声明 no-subsetting 的变体）
            "allowed_fstype_masks": [0x0000, 0x0008],
            "forbidden_fstype_bits": [0x0001],
            "required_name_fields": ["license_description", "license_url"],
            "require_https_license_url": True,
            "forbidden_license_patterns": [],
            "deny_on_fstype_reserved_bits": True,
            "deny_on_embedding_flags_conflict": True,
            "deny_on_ttc_member_license_mismatch": True,
            "deny_on_incomplete_collection_evidence": True,
            "conflict_rules": [
                {"type": "fstype-flag-pair",
                 "flag_a": "preview-print-only", "flag_b": "editable",
                 "message": "同一 Web 字体栈不得混用预览/印刷与可编辑嵌入字体"},
            ],
        },
        "desktop": {
            "name": "桌面应用捆绑字体基线策略",
            # 可安装或可编辑嵌入（含 no-subsetting 变体）
            "allowed_fstype_masks": [0x0000, 0x0004, 0x0008, 0x000C],
            "forbidden_fstype_bits": [0x0001, 0x0002],
            "required_name_fields": ["license_description"],
            "require_https_license_url": False,
            "forbidden_license_patterns": [],
            "deny_on_fstype_reserved_bits": True,
            "deny_on_embedding_flags_conflict": True,
            "deny_on_ttc_member_license_mismatch": True,
            "deny_on_incomplete_collection_evidence": True,
            "conflict_rules": [],
        },
        "print": {
            "name": "印刷出版字体基线策略",
            # 可安装 / 预览印刷 / 可编辑均可（含 no-subsetting 变体）
            "allowed_fstype_masks": [0x0000, 0x0002, 0x0004,
                                     0x0008, 0x000A, 0x000C],
            "forbidden_fstype_bits": [0x0001],
            "required_name_fields": ["license_description"],
            "require_https_license_url": False,
            "forbidden_license_patterns": [],
            "deny_on_fstype_reserved_bits": True,
            "deny_on_embedding_flags_conflict": True,
            "deny_on_ttc_member_license_mismatch": True,
            "deny_on_incomplete_collection_evidence": True,
            "conflict_rules": [],
        },
    }


def default_policies() -> dict[str, dict[str, Any]]:
    """三个场景的修正版不可变策略（revision=2）。

    按真实 fsType embedding types 统一解读：
      0x0000 installable；0x0002 restricted（受限，禁止嵌入）；
      0x0004 preview & print（临时安装、不可编辑）；0x0008 editable；
      bit8 0x0100 no-subsetting、bit9 0x0200 bitmap-only 为可叠加附加位；
      bit0、bit4–7、bit10–15 为保留位。
    因此 0x0002 在三种场景下一律拒绝（证据标为 restricted），仅可凭绑定字体、
    策略版本与检查代码的有效豁免降级；preview&print(0x0004) 不适用 Web/桌面
    捆绑发行，印刷发布默认也不接受无授权嵌入。
    """
    common = {
        "forbidden_fstype_bits": [FSTYPE_RESTRICTED],
        "require_https_license_url": False,
        "forbidden_license_patterns": [],
        "deny_on_fstype_reserved_bits": True,
        "deny_on_embedding_flags_conflict": True,
        "deny_on_ttc_member_license_mismatch": True,
        "deny_on_incomplete_collection_evidence": True,
        POLICY_REVISION_KEY: POLICY_REVISION_CORRECTED,
    }
    return {
        "web": {
            "name": "Web 字体嵌入基线策略（fsType 位定义修正版）",
            # 仅 installable，可叠加 bit8 no-subsetting；
            # restricted/preview-print/editable 一律不允许
            "allowed_fstype_masks": [0x0000, FSTYPE_NO_SUBSETTING],
            "required_name_fields": ["license_description", "license_url"],
            **common,
            "require_https_license_url": True,
            "conflict_rules": [
                {"type": "fstype-flag-pair",
                 "flag_a": "preview-print-only", "flag_b": "editable",
                 "message": "同一 Web 字体栈不得混用预览/印刷与可编辑嵌入字体"},
            ],
        },
        "desktop": {
            "name": "桌面应用捆绑字体基线策略（fsType 位定义修正版）",
            # installable 或 editable，可叠加 bit8 no-subsetting；
            # restricted/preview-print 不允许
            "allowed_fstype_masks": [0x0000, FSTYPE_EDITABLE,
                                     FSTYPE_NO_SUBSETTING,
                                     FSTYPE_EDITABLE | FSTYPE_NO_SUBSETTING],
            "required_name_fields": ["license_description"],
            **common,
            "conflict_rules": [],
        },
        "print": {
            "name": "印刷出版字体基线策略（fsType 位定义修正版）",
            # installable / preview & print / editable 可叠加 bit8
            # no-subsetting；restricted(0x0002) 一律拒绝，需有效豁免
            "allowed_fstype_masks": [
                0x0000, FSTYPE_PREVIEW, FSTYPE_EDITABLE,
                FSTYPE_NO_SUBSETTING, FSTYPE_PREVIEW | FSTYPE_NO_SUBSETTING,
                FSTYPE_EDITABLE | FSTYPE_NO_SUBSETTING],
            "required_name_fields": ["license_description"],
            **common,
            "conflict_rules": [],
        },
    }


# ---------------------------------------------------------------- 评估引擎

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _exemption_effective(ex: dict[str, Any], now: datetime) -> tuple[bool, str]:
    if ex.get("revoked"):
        return False, "exemption-revoked"
    expires = ex.get("expires_at")
    if expires:
        dt = _parse_iso(expires)
        if dt is None:
            return False, "exemption-bad-expiry"
        if dt <= now:
            return False, "exemption-expired"
    return True, ""


def _exemption_summary(ex: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ex["id"],
        "policy_version": ex["policy_version"],
        "rule_codes": ex["rule_codes"],
        "justification": ex["justification"],
        "requested_by": ex.get("requested_by"),
        "expires_at": ex.get("expires_at"),
        "applies_to_stack_conflict": ex.get("applies_to_stack_conflict", False),
        "created_at": ex.get("created_at"),
    }


def _finding(code: str, message: str, *, font_position: Optional[int] = None,
             font_digest: Optional[str] = None,
             font_digests: Optional[list[str]] = None,
             member_index: Optional[int] = None,
             evidence: Optional[dict[str, Any]] = None,
             rule: Optional[dict[str, Any]] = None,
             field: Optional[str] = None) -> dict[str, Any]:
    f: dict[str, Any] = {
        "code": code,
        "severity": SEVERITY_DENY,
        "message": message,
        "font_position": font_position,
        "font_digest": font_digest,
        "member_index": member_index,
        "field": field,
        "rule": rule,
        "evidence": evidence or {},
    }
    if font_digests is not None:
        f["font_digests"] = font_digests
    return f


def _member_text(member: dict[str, Any], field_key: str) -> str:
    return member.get(NAME_FIELDS[field_key], "") or ""


def evaluate(policy_record: dict[str, Any],
             stack: list[dict[str, Any]],
             exemptions: list[dict[str, Any]],
             now: Optional[datetime] = None) -> dict[str, Any]:
    """执行确定性合规评估。

    policy_record: database.get_active_license_policy 返回的完整不可变记录
    stack: [{"position", "sha256", "filename", "family", "evidence"}]
           证据中的 fsType 派生标签在副本中按数值 fstype 重算（修正版位
           定义），调用方对象不被改写
    exemptions: 该 scenario + policy_version 下的豁免（含已撤销/过期，引擎判定）
    返回自包含清单（manifest，不含 fingerprint 之外的外层包装）。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    scenario = policy_record["scenario"]
    version = policy_record["version"]
    policy = policy_record["policy"]

    # 以数值 fsType 为准，在副本中重算每个成员的派生标签：升级前登记的
    # 存证可能仍按旧位序标记（如 0x0002 被记为 preview-print-only）。
    # 数值检查、标签冲突规则、逐条证据与冻结清单统一使用重算后的证据；
    # 调用方对象与数据库原始存证不被改写。
    stack = [{**font,
              "evidence": normalize_evidence_fstype(font.get("evidence") or {})}
             for font in stack]

    findings: list[dict[str, Any]] = []

    for font in stack:
        position = font["position"]
        claimed = font["sha256"]
        ev = font.get("evidence") or {}
        members = ev.get("members") or []

        if not ev.get("members_complete", False):
            findings.append(_finding(
                EVIDENCE_INCOMPLETE_COLLECTION,
                "缺少完整的集合字体成员/解包证据（证据由解包 sfnt 重建，"
                "无法确认 TTC 全体成员或原始容器摘要）",
                font_position=position, font_digest=claimed,
                evidence={"container": ev.get("container"),
                          "reconstructed": ev.get("reconstructed", False)}))

        actual = None
        if members:
            actual = members[0].get("actual_sha256") or members[0].get("sha256")
            if members[0].get("actual_sha256") or actual != claimed:
                findings.append(_finding(
                    FONT_DIGEST_MISMATCH,
                    f"字体摘要不匹配：登记 {claimed[:16]}…，"
                    f"当前磁盘内容 {str(actual)[:16]}…",
                    font_position=position, font_digest=claimed,
                    evidence={"claimed_sha256": claimed,
                              "actual_sha256": members[0].get("actual_sha256")
                                              or members[0].get("sha256")}))

        for member in members:
            findings.extend(
                _evaluate_member(member, policy, position, claimed))

        if len(members) > 1 and policy.get(
                "deny_on_ttc_member_license_mismatch", True):
            findings.extend(_ttc_member_mismatch(members, position, claimed))

    findings.extend(_stack_conflicts(stack, policy))

    # 应用豁免：过期 / 已撤销 / 摘要不匹配均不改变拒绝结论
    applied: list[dict[str, Any]] = []
    for finding in findings:
        ex = _match_exemption(finding, exemptions, scenario, version, now)
        if ex is not None:
            finding["severity"] = SEVERITY_WARN
            finding["exempted"] = True
            finding["exemption"] = _exemption_summary(ex)
            applied.append({"finding_code": finding["code"],
                            "exemption_id": ex["id"],
                            "font_digest": finding.get("font_digest"),
                            "member_index": finding.get("member_index")})

    deny_codes = [f["code"] for f in findings if f["severity"] == SEVERITY_DENY]
    decision = "deny" if deny_codes else "allow"

    manifest = {
        "manifest": MANIFEST_KIND,
        "schema_version": MANIFEST_SCHEMA,
        "scenario": scenario,
        "evaluated_at": now.isoformat(timespec="seconds"),
        "decision": decision,
        "deny_codes": deny_codes,
        "policy": {
            "scenario": scenario,
            "version": version,
            "created_at": policy_record.get("created_at"),
            "registered_by": policy_record.get("registered_by"),
            "rules": policy,
        },
        "font_stack": [_freeze_font(font) for font in stack],
        "exemptions_considered": [_exemption_summary(e) for e in exemptions],
        "exemptions_applied": applied,
        "findings": findings,
    }
    manifest["fingerprint"] = fingerprint(manifest)
    return manifest


def _freeze_font(font: dict[str, Any]) -> dict[str, Any]:
    return {
        "position": font["position"],
        "sha256": font["sha256"],
        "filename": font.get("filename"),
        "family": font.get("family"),
        "license_evidence": font.get("evidence") or {},
    }


def _evaluate_member(member: dict[str, Any], policy: dict[str, Any],
                     position: int, claimed: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    idx = member.get("member_index", 0)
    fs_type = member.get("fstype")
    ev = {k: member.get(k) for k in (
        "fstype", "fstype_flags", "fstype_flag_labels",
        "fstype_reserved_bits", "name_license_description",
        "name_license_url", "family", "sha256")}

    if fs_type is None:
        out.append(_finding(FSTYPE_MISSING,
                            "字体缺少 OS/2 表，无法读取 fsType 嵌入限制位",
                            font_position=position, font_digest=claimed,
                            member_index=idx, evidence=ev))
        return out  # 其余 fsType 检查无意义；name 字段仍可继续

    if policy.get("deny_on_fstype_reserved_bits", True) and (
            fs_type & FSTYPE_RESERVED_MASK):
        out.append(_finding(
            FSTYPE_RESERVED_BITS,
            f"fsType={fs_type:#06x} 含有保留位置位 "
            f"({fs_type & FSTYPE_RESERVED_MASK:#06x}，bit0/bit4–7/bit10–15 "
            f"必须为零），违反 fsType 规范",
            font_position=position, font_digest=claimed,
            member_index=idx, evidence=ev))

    # fsType 低 4 位是互斥的嵌入权限类型枚举（bit0 保留必须为 0）：
    # 合法值仅 0（installable）、2（restricted）、4（preview&print）、
    # 8（editable）；0x0001 为保留值，3/6/10/12/14 等组合自相矛盾。
    # bit8(0x0100 no-subsetting)/bit9(0x0200 bitmap-only) 是可叠加附加位，
    # 因此 0x0100 仍是合法的 installable 嵌入。
    embedding_bits = fs_type & FSTYPE_EMBEDDING_MASK
    if policy.get("deny_on_embedding_flags_conflict", True) \
            and embedding_bits not in (
                FSTYPE_INSTALLABLE, FSTYPE_RESTRICTED,
                FSTYPE_PREVIEW, FSTYPE_EDITABLE):
        out.append(_finding(
            FSTYPE_EMBEDDING_FLAGS_CONFLICT,
            f"fsType={fs_type:#06x} 的低 4 位嵌入权限类型非法"
            f"（合法类型值仅 0/2/4/8，0x0001 为保留值，实际 "
            f"{embedding_bits:#06x}）",
            font_position=position, font_digest=claimed,
            member_index=idx, evidence=ev))

    allowed = policy.get("allowed_fstype_masks", [])
    if allowed and fs_type not in allowed:
        out.append(_finding(
            FSTYPE_MASK_NOT_ALLOWED,
            f"fsType={fs_type:#06x} 不在策略允许的位掩码集合 "
            f"{[f'0x{m:04x}' for m in allowed]} 中",
            font_position=position, font_digest=claimed,
            member_index=idx, evidence=ev,
            rule={"allowed_fstype_masks": allowed}))

    for bit in policy.get("forbidden_fstype_bits", []):
        if fs_type & bit:
            label = FSTYPE_FLAG_LABELS.get(bit, f"bit {bit:#06x}")
            out.append(_finding(
                FSTYPE_BIT_FORBIDDEN,
                f"fsType={fs_type:#06x} 含策略禁止位 {label} ({bit:#06x})",
                font_position=position, font_digest=claimed,
                member_index=idx, evidence=ev,
                rule={"forbidden_bit": bit, "label": label}))

    for field_key in policy.get("required_name_fields", []):
        text = _member_text(member, field_key)
        if not text:
            out.append(_finding(
                NAME_FIELD_MISSING,
                f"name 表缺少必需许可字段 {field_key}（nameID "
                f"{13 if field_key == 'license_description' else 14}）",
                font_position=position, font_digest=claimed,
                member_index=idx, field=field_key, evidence=ev))
        elif field_key == "license_url" and policy.get(
                "require_https_license_url", False):
            parsed = urlparse(text)
            if parsed.scheme.lower() != "https" or not parsed.netloc:
                out.append(_finding(
                    NAME_FIELD_BAD_URL,
                    f"许可 URL 必须为 https 绝对地址，实际为 {text!r}",
                    font_position=position, font_digest=claimed,
                    member_index=idx, field=field_key,
                    evidence={**ev, "parsed_scheme": parsed.scheme}))

    blob = " ".join([
        member.get("name_license_description", "") or "",
        member.get("name_license_url", "") or ""])
    for pattern in policy.get("forbidden_license_patterns", []):
        m = re.search(pattern, blob, re.IGNORECASE)
        if m:
            out.append(_finding(
                LICENSE_TEXT_FORBIDDEN,
                f"许可文本命中策略禁止模式 {pattern!r}：{m.group(0)!r}",
                font_position=position, font_digest=claimed,
                member_index=idx, evidence={**ev, "match": m.group(0)},
                rule={"pattern": pattern}))

    for rule in policy.get("conflict_rules", []):
        if rule.get("type") != "fstype-flag-text":
            continue
        label = rule["flag"]
        field_key = rule.get("field", "license_description")
        text = _member_text(member, field_key)
        if label in (member.get("fstype_flag_labels") or []) and (
                not text or re.search(rule["pattern"], text, re.IGNORECASE)):
            out.append(_finding(
                FSTYPE_TEXT_CONFLICT,
                rule.get("message") or
                f"fsType 位 {label} 与许可文本组合违反策略",
                font_position=position, font_digest=claimed,
                member_index=idx, field=field_key,
                evidence={**ev, "text_excerpt": text[:200]}, rule=rule))
    return out


def _ttc_member_mismatch(members: list[dict[str, Any]], position: int,
                         claimed: str) -> list[dict[str, Any]]:
    diff_fields = []
    base = members[0]
    for key in ("fstype", "name_license_description", "name_license_url"):
        values = {m.get(key) or "" for m in members}
        if len(values) > 1:
            diff_fields.append(key)
    if not diff_fields:
        return []
    return [_finding(
        TTC_MEMBER_LICENSE_MISMATCH,
        f"集合字体 {len(members)} 个成员的许可字段不一致：{', '.join(diff_fields)}",
        font_position=position, font_digest=claimed,
        evidence={"members": [{
            "member_index": m.get("member_index"),
            "family": m.get("family"),
            "sha256": m.get("sha256"),
            "fstype": m.get("fstype"),
            "name_license_description": m.get("name_license_description"),
            "name_license_url": m.get("name_license_url"),
        } for m in members], "diff_fields": diff_fields})]


def _stack_conflicts(stack: list[dict[str, Any]],
                     policy: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rules = [r for r in policy.get("conflict_rules", [])
             if r["type"] in ("fstype-flag-pair", "license-text-pair")]
    if not rules:
        return out

    # 每个字体（任一成员命中即算）的标签与文本集合
    font_flags: list[set[str]] = []
    font_texts: list[str] = []
    for font in stack:
        flags: set[str] = set()
        texts = []
        for m in (font.get("evidence") or {}).get("members") or []:
            flags.update(m.get("fstype_flag_labels") or [])
            texts.append(m.get("name_license_description") or "")
            texts.append(m.get("name_license_url") or "")
        font_flags.append(flags)
        font_texts.append("\n".join(texts))

    for ai, font_a in enumerate(stack):
        for bi in range(ai + 1, len(stack)):
            font_b = stack[bi]
            for rule in rules:
                hit = False
                detail: dict[str, Any] = {}
                if rule["type"] == "fstype-flag-pair":
                    hit = (rule["flag_a"] in font_flags[ai]
                           and rule["flag_b"] in font_flags[bi]) or (
                          rule["flag_b"] in font_flags[ai]
                          and rule["flag_a"] in font_flags[bi])
                    detail = {"flag_a": rule["flag_a"], "flag_b": rule["flag_b"]}
                else:
                    ha = bool(re.search(rule["pattern_a"], font_texts[ai],
                                        re.IGNORECASE))
                    hb = bool(re.search(rule["pattern_b"], font_texts[bi],
                                        re.IGNORECASE))
                    ha2 = bool(re.search(rule["pattern_a"], font_texts[bi],
                                         re.IGNORECASE))
                    hb2 = bool(re.search(rule["pattern_b"], font_texts[ai],
                                         re.IGNORECASE))
                    hit = (ha and hb) or (ha2 and hb2)
                    detail = {"pattern_a": rule["pattern_a"],
                              "pattern_b": rule["pattern_b"]}
                if hit:
                    out.append(_finding(
                        STACK_LICENSE_CONFLICT,
                        rule.get("message") or "字体栈许可组合冲突",
                        font_position=font_a["position"],
                        font_digests=[font_a["sha256"], font_b["sha256"]],
                        evidence={
                            "positions": [font_a["position"],
                                          font_b["position"]],
                            "families": [font_a.get("family"),
                                         font_b.get("family")],
                            **detail,
                        },
                        rule=rule))
    return out


def _match_exemption(finding: dict[str, Any], exemptions: list[dict[str, Any]],
                     scenario: str, version: int,
                     now: datetime) -> Optional[dict[str, Any]]:
    code = finding["code"]
    stack_conflict = code == STACK_LICENSE_CONFLICT
    for ex in exemptions:
        if ex.get("scenario") != scenario or \
                ex.get("policy_version") != version:
            continue
        ok, _reason = _exemption_effective(ex, now)
        if not ok:
            continue
        if code not in ex.get("rule_codes", []) and "*" not in ex.get(
                "rule_codes", []):
            continue
        if stack_conflict:
            if not ex.get("applies_to_stack_conflict"):
                continue
            if ex.get("font_digest") not in (finding.get("font_digests") or []):
                continue
        else:
            if ex.get("font_digest") != finding.get("font_digest"):
                continue
        return ex
    return None


def fingerprint(manifest: dict[str, Any]) -> str:
    """决定指纹：场景 + 策略版本 + 栈内全部成员摘要（字体内容漂移即变）。"""
    fonts = []
    for f in manifest["font_stack"]:
        ev = f.get("license_evidence") or {}
        fonts.append({
            "d": f["sha256"],
            "m": [m.get("sha256") for m in ev.get("members") or []],
            "c": ev.get("container"),
            "u": ev.get("unpack_summary"),
        })
    basis = {
        "kind": MANIFEST_KIND,
        "scenario": manifest["scenario"],
        "policy_version": manifest["policy"]["version"],
        "fonts": fonts,
    }
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def summarize_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """字体卡片/前端用的精简许可摘要。"""
    members = evidence.get("members") or []
    first = members[0] if members else {}
    return {
        "container": evidence.get("container"),
        "members_complete": evidence.get("members_complete", False),
        "member_count": len(members),
        "unpack_summary": evidence.get("unpack_summary"),
        "fstype": first.get("fstype"),
        "fstype_flag_labels": first.get("fstype_flag_labels"),
        "fstype_reserved_bits": first.get("fstype_reserved_bits"),
        "name_license_description": first.get("name_license_description"),
        "name_license_url": first.get("name_license_url"),
        "members": [{
            "member_index": m.get("member_index"),
            "family": m.get("family"),
            "sha256": m.get("sha256"),
            "fstype": m.get("fstype"),
            "fstype_flag_labels": m.get("fstype_flag_labels"),
            "name_license_url": m.get("name_license_url"),
        } for m in members],
    }
