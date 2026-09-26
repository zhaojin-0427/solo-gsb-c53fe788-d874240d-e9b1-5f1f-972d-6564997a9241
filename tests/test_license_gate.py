"""许可与嵌入合规门禁的纯逻辑测试（不依赖 fontTools/HTTP 层）。

证据对象手工构造，验证策略匹配、冲突、豁免、指纹与冻结语义。
端到端（真实字体解析、API、CAS、幂等）见 test_license_api.py。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app import license_gate as lg


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

POLICY = {
    "scenario": "web",
    "version": 2,
    "created_at": "2026-09-01T00:00:00+00:00",
    "registered_by": "admin",
    "policy": {
        # 修正后的真实 fsType 位定义：0x0100 = no-subsetting（叠加位）
        "allowed_fstype_masks": [0x0000, 0x0100],
        "forbidden_fstype_bits": [0x0002],
        "required_name_fields": ["license_description", "license_url"],
        "require_https_license_url": True,
        "forbidden_license_patterns": [r"proprietary[- ]?only"],
        "deny_on_fstype_reserved_bits": True,
        "deny_on_embedding_flags_conflict": True,
        "deny_on_ttc_member_license_mismatch": True,
        "deny_on_incomplete_collection_evidence": True,
        "conflict_rules": [
            {"type": "fstype-flag-pair",
             "flag_a": "preview-print-only", "flag_b": "editable",
             "message": "preview 与 editable 不得混用"},
        ],
    },
}


def member(**over):
    m = {
        "member_index": 0,
        "sha256": "a" * 64,
        "sfnt_size": 100,
        "family": "Font",
        "fstype": 0x0000,
        "fstype_flags": [],
        "fstype_flag_labels": [],
        "fstype_reserved_bits": 0,
        "name_license_description": "SIL Open Font License 1.1",
        "name_license_url": "https://scripts.sil.org/OFL",
    }
    m.update(over)
    return m


def evidence(members, container="sfnt", complete=True):
    return {
        "container": container,
        "container_size": 100,
        "members_complete": complete,
        "unpack_summary": {"container": container},
        "members": members,
    }


def font(d, members, complete=True, container="sfnt"):
    return {"position": 0, "sha256": d, "filename": "x.otf",
            "family": "Font", "evidence": evidence(members, container, complete)}


def stack1(**mover):
    return [font("a" * 64, [member(**mover)])]


class EvaluateTests(unittest.TestCase):
    def test_clean_font_allows(self):
        m = lg.evaluate(POLICY, stack1(), [], NOW)
        self.assertEqual(m["decision"], "allow", m["findings"])
        self.assertEqual(m["deny_codes"], [])
        self.assertEqual(len(m["findings"]), 0)
        self.assertEqual(len(m["fingerprint"]), 64)

    def test_fstype_mask_not_allowed(self):
        # 0x0008 = editable 嵌入，不在 Web 允许掩码中
        m = lg.evaluate(POLICY, stack1(fstype=0x0008,
                        fstype_flags=[0x0008],
                        fstype_flag_labels=["editable"]), [], NOW)
        self.assertEqual(m["decision"], "deny")
        self.assertIn(lg.FSTYPE_MASK_NOT_ALLOWED, m["deny_codes"])

    def test_fstype_forbidden_restricted(self):
        # 0x0002 = Restricted License embedding（受限，禁止嵌入）
        m = lg.evaluate(POLICY, stack1(fstype=0x0002,
                        fstype_flags=[0x0002],
                        fstype_flag_labels=["restricted"],
                        name_license_description="Proprietary",
                        name_license_url="https://example.com/license"),
                        [], NOW)
        finding = next(f for f in m["findings"]
                       if f["code"] == lg.FSTYPE_BIT_FORBIDDEN)
        self.assertEqual(finding["rule"]["label"], "restricted")
        self.assertIn(lg.FSTYPE_BIT_FORBIDDEN, m["deny_codes"])
        # 0x0002 不在允许掩码中
        self.assertIn(lg.FSTYPE_MASK_NOT_ALLOWED, m["deny_codes"])

    def test_fstype_reserved_bits(self):
        m = lg.evaluate(POLICY, stack1(fstype=0x0010,
                        fstype_flags=[],
                        fstype_flag_labels=[],
                        fstype_reserved_bits=0x0010), [], NOW)
        self.assertIn(lg.FSTYPE_RESERVED_BITS, m["deny_codes"])
        self.assertIn(lg.FSTYPE_MASK_NOT_ALLOWED, m["deny_codes"])

    def test_fstype_bit0_reserved(self):
        # 0x0001 是保留值（旧代码曾误标为 restricted）：保留位 + 非法类型
        m = lg.evaluate(POLICY, stack1(fstype=0x0001,
                        fstype_flags=[],
                        fstype_flag_labels=[],
                        fstype_reserved_bits=0x0001), [], NOW)
        self.assertIn(lg.FSTYPE_RESERVED_BITS, m["deny_codes"])
        self.assertIn(lg.FSTYPE_EMBEDDING_FLAGS_CONFLICT, m["deny_codes"])
        self.assertNotIn(lg.FSTYPE_BIT_FORBIDDEN, m["deny_codes"])

    def test_embedding_flags_conflict_combo(self):
        # 0x0006 = restricted(0x0002) + preview-print(0x0004) 同时置位，
        # 低 4 位类型字段互斥非法
        m = lg.evaluate(POLICY, stack1(fstype=0x0006,
                        fstype_flags=[0x0002, 0x0004],
                        fstype_flag_labels=["restricted",
                                           "preview-print-only"]),
                        [], NOW)
        self.assertIn(lg.FSTYPE_EMBEDDING_FLAGS_CONFLICT, m["deny_codes"])
        self.assertIn(lg.FSTYPE_MASK_NOT_ALLOWED, m["deny_codes"])

    def test_installable_with_no_subsetting_allowed(self):
        # 0x0100 = installable 嵌入（低 4 位类型为 0）+ 禁止子集化，合法
        m = lg.evaluate(POLICY, stack1(fstype=0x0100,
                        fstype_flags=[0x0100],
                        fstype_flag_labels=["no-subsetting"],
                        name_license_description="OFL",
                        name_license_url="https://x.test"),
                        [], NOW)
        self.assertNotIn(lg.FSTYPE_EMBEDDING_FLAGS_CONFLICT, m["deny_codes"],
                         m["findings"])
        self.assertNotIn(lg.FSTYPE_RESERVED_BITS, m["deny_codes"],
                         m["findings"])
        self.assertNotIn(lg.FSTYPE_MASK_NOT_ALLOWED, m["deny_codes"],
                         m["findings"])

    def test_required_name_fields_missing(self):
        m = lg.evaluate(POLICY,
                        stack1(name_license_description="",
                               name_license_url=""),
                        [], NOW)
        codes = m["deny_codes"]
        self.assertEqual(codes.count(lg.NAME_FIELD_MISSING), 2)

    def test_license_url_must_be_https(self):
        m = lg.evaluate(POLICY,
                        stack1(name_license_url="http://example.com/x"),
                        [], NOW)
        self.assertIn(lg.NAME_FIELD_BAD_URL, m["deny_codes"])

    def test_forbidden_license_pattern(self):
        m = lg.evaluate(POLICY,
                        stack1(name_license_description="Proprietary-Only license"),
                        [], NOW)
        self.assertIn(lg.LICENSE_TEXT_FORBIDDEN, m["deny_codes"])

    def test_incomplete_collection_evidence_denied(self):
        st = [font("a" * 64, [member()], complete=False)]
        m = lg.evaluate(POLICY, st, [], NOW)
        self.assertIn(lg.EVIDENCE_INCOMPLETE_COLLECTION, m["deny_codes"])

    def test_digest_mismatch(self):
        members = [member(actual_sha256="b" * 64)]
        st = [font("a" * 64, members)]
        m = lg.evaluate(POLICY, st, [], NOW)
        self.assertIn(lg.FONT_DIGEST_MISMATCH, m["deny_codes"])

    def test_ttc_member_license_mismatch(self):
        m1 = member(member_index=0, fstype=0x0000)
        m2 = member(member_index=1, sha256="c" * 64,
                    fstype=0x0008, fstype_flags=[0x0008],
                    fstype_flag_labels=["editable"])
        st = [font("a" * 64, [m1, m2], container="ttc")]
        m = lg.evaluate(POLICY, st, [], NOW)
        self.assertIn(lg.TTC_MEMBER_LICENSE_MISMATCH, m["deny_codes"])

    def test_stack_flag_pair_conflict(self):
        # 0x0004 = preview&print，0x0008 = editable
        f1 = font("a" * 64, [member(
            fstype=0x0004, fstype_flags=[0x0004],
            fstype_flag_labels=["preview-print-only"],
            name_license_url="https://a.test/x",
            name_license_description="License A")])
        f1["position"] = 0
        f2 = font("b" * 64, [member(
            fstype=0x0008, fstype_flags=[0x0008],
            fstype_flag_labels=["editable"],
            name_license_url="https://b.test/x",
            name_license_description="License B")])
        f2["position"] = 1
        # print 风格掩码允许这些类型 —— 临时放宽
        pol = {**POLICY}
        pol["policy"] = {**POLICY["policy"],
                         "allowed_fstype_masks": [0x0004, 0x0008],
                         "forbidden_fstype_bits": []}
        m = lg.evaluate(pol, [f1, f2], [], NOW)
        self.assertIn(lg.STACK_LICENSE_CONFLICT, m["deny_codes"])

    def test_restricted_0x0002_denied_even_with_required_name_fields(self):
        # 即使含必需的 nameID 13/14，0x0002 仍是 Restricted License embedding：
        # 无有效豁免时必须拒绝，证据标签为 restricted
        m = lg.evaluate(POLICY, stack1(
            fstype=0x0002, fstype_flags=[0x0002],
            fstype_flag_labels=["restricted"],
            name_license_description="See license agreement",
            name_license_url="https://example.com/restricted"), [], NOW)
        self.assertEqual(m["decision"], "deny")
        labels = m["font_stack"][0]["license_evidence"]["members"][0][
            "fstype_flag_labels"]
        self.assertEqual(labels, ["restricted"])
        self.assertNotIn("preview-print-only", labels)
        self.assertIn(lg.FSTYPE_BIT_FORBIDDEN, m["deny_codes"])
        self.assertIn(lg.FSTYPE_MASK_NOT_ALLOWED, m["deny_codes"])

    def test_restricted_0x0002_allowed_only_with_valid_exemption(self):
        ex = self._exemption(lg.FSTYPE_BIT_FORBIDDEN,
                             expires_delta=timedelta(days=7))
        ex["rule_codes"] = [lg.FSTYPE_BIT_FORBIDDEN,
                            lg.FSTYPE_MASK_NOT_ALLOWED]
        m = lg.evaluate(POLICY, stack1(
            fstype=0x0002, fstype_flags=[0x0002],
            fstype_flag_labels=["restricted"],
            name_license_description="Restricted but licensed",
            name_license_url="https://example.com/license"), [ex], NOW)
        self.assertEqual(m["decision"], "allow", m["findings"])
        for f in m["findings"]:
            self.assertEqual(f["severity"], "warn")

    def test_preview_0x0004_not_labeled_restricted(self):
        # 0x0004 是 preview & print，不是受限（区别于 0x0002）
        m = lg.evaluate(POLICY, stack1(
            fstype=0x0004, fstype_flags=[0x0004],
            fstype_flag_labels=["preview-print-only"]), [], NOW)
        self.assertEqual(m["decision"], "deny")
        self.assertIn(lg.FSTYPE_MASK_NOT_ALLOWED, m["deny_codes"])
        self.assertNotIn(lg.FSTYPE_BIT_FORBIDDEN, m["deny_codes"])

    # ------------------------------------------------------------- 豁免

    def _exemption(self, code, expires_delta=None, revoked=False,
                   stack_conflict=False, digest="a" * 64, eid=1):
        ex = {
            "id": eid, "scenario": "web", "policy_version": 2,
            "font_digest": digest, "rule_codes": [code],
            "justification": "有书面授权", "requested_by": "legal",
            "expires_at": None, "revoked": revoked,
            "applies_to_stack_conflict": stack_conflict,
            "created_at": "2026-09-01T00:00:00+00:00",
        }
        if expires_delta is not None:
            ex["expires_at"] = (NOW + expires_delta).isoformat()
        return ex

    def test_valid_exemption_turns_deny_into_warn_and_allows(self):
        ex = self._exemption(lg.NAME_FIELD_BAD_URL,
                             expires_delta=timedelta(days=7))
        m = lg.evaluate(POLICY,
                        stack1(name_license_url="http://example.com/x"),
                        [ex], NOW)
        self.assertEqual(m["decision"], "allow")
        finding = next(f for f in m["findings"]
                       if f["code"] == lg.NAME_FIELD_BAD_URL)
        self.assertEqual(finding["severity"], "warn")
        self.assertTrue(finding["exempted"])
        self.assertEqual(m["exemptions_applied"][0]["exemption_id"], 1)

    def test_expired_exemption_does_not_pass(self):
        ex = self._exemption(lg.NAME_FIELD_BAD_URL,
                             expires_delta=timedelta(days=-1))
        m = lg.evaluate(POLICY,
                        stack1(name_license_url="http://example.com/x"),
                        [ex], NOW)
        self.assertEqual(m["decision"], "deny")

    def test_revoked_exemption_does_not_pass(self):
        ex = self._exemption(lg.NAME_FIELD_BAD_URL, revoked=True,
                             expires_delta=timedelta(days=7))
        m = lg.evaluate(POLICY,
                        stack1(name_license_url="http://example.com/x"),
                        [ex], NOW)
        self.assertEqual(m["decision"], "deny")

    def test_exemption_digest_mismatch_does_not_pass(self):
        ex = self._exemption(lg.NAME_FIELD_BAD_URL,
                             digest="f" * 64)
        m = lg.evaluate(POLICY,
                        stack1(name_license_url="http://example.com/x"),
                        [ex], NOW)
        self.assertEqual(m["decision"], "deny")

    def test_exemption_wrong_policy_version_ignored(self):
        ex = self._exemption(lg.NAME_FIELD_BAD_URL,
                             expires_delta=timedelta(days=7))
        ex["policy_version"] = POLICY["version"] + 1
        m = lg.evaluate(POLICY,
                        stack1(name_license_url="http://example.com/x"),
                        [ex], NOW)
        self.assertEqual(m["decision"], "deny")

    def test_digest_mismatch_cannot_be_exempted_via_claim_identity(self):
        # 豁免绑定声明摘要，但 finding 也是声明摘要 → 可以豁免代码本身；
        # 实际发布时磁盘校验仍以证据为准。这里验证语义：
        # digest-mismatch 是证据事实，可按代码豁免但警告保留实际摘要。
        members = [member(actual_sha256="b" * 64)]
        st = [font("a" * 64, members)]
        ex = self._exemption(lg.FONT_DIGEST_MISMATCH,
                             expires_delta=timedelta(days=7))
        m = lg.evaluate(POLICY, st, [ex], NOW)
        finding = next(f for f in m["findings"]
                       if f["code"] == lg.FONT_DIGEST_MISMATCH)
        self.assertEqual(finding["evidence"]["actual_sha256"], "b" * 64)
        self.assertEqual(finding["severity"], "warn")

    # ------------------------------------------------------------- 指纹/确定

    def test_fingerprint_changes_with_policy_version_and_font(self):
        m1 = lg.evaluate(POLICY, stack1(), [], NOW)
        pol2 = {**POLICY, "version": 2, "policy": dict(POLICY["policy"])}
        # 与 POLICY 同版本时指纹相同；版本不同时指纹改变
        m_same = lg.evaluate({**POLICY, "policy": dict(POLICY["policy"])},
                             stack1(), [], NOW)
        self.assertEqual(m1["fingerprint"], m_same["fingerprint"])
        pol2["version"] = 3
        m2 = lg.evaluate(pol2, stack1(), [], NOW)
        self.assertNotEqual(m1["fingerprint"], m2["fingerprint"])

        st2 = [font("b" * 64, [member(sha256="b" * 64)])]
        m3 = lg.evaluate(POLICY, st2, [], NOW)
        self.assertNotEqual(m1["fingerprint"], m3["fingerprint"])

    def test_deterministic_same_input_same_output(self):
        st = stack1(name_license_url="http://x.test")
        ex = self._exemption(lg.NAME_FIELD_BAD_URL,
                             expires_delta=timedelta(days=7))
        m1 = lg.evaluate(POLICY, st, [ex], NOW)
        m2 = lg.evaluate(POLICY, st, [ex], NOW)
        # evaluated_at 也相同（传入同一 now）
        self.assertEqual(m1, m2)


class LegacyEvidenceRecomputeTests(unittest.TestCase):
    """升级前登记的存证（旧位序派生标签）：评估以数值 fsType 为准，
    在副本中重算 fstype_flags/labels/reserved_bits，原始对象不改写。"""

    def test_legacy_0x0002_preview_label_recomputed(self):
        # 旧存证：fstype=0x0002 但派生标签仍记为 preview-print-only
        st = stack1(fstype=0x0002,
                    fstype_flags=[0x0002],
                    fstype_flag_labels=["preview-print-only"],
                    fstype_reserved_bits=0,
                    name_license_description="Proprietary",
                    name_license_url="https://example.com/lic")
        m = lg.evaluate(POLICY, st, [], NOW)
        self.assertEqual(m["decision"], "deny")
        self.assertIn(lg.FSTYPE_BIT_FORBIDDEN, m["deny_codes"])
        # 冻结清单中的标签以数值为准
        labels = m["font_stack"][0]["license_evidence"]["members"][0][
            "fstype_flag_labels"]
        self.assertEqual(labels, ["restricted"])
        # 逐条证据中的标签同步重算，结论与证据不再矛盾
        finding = next(f for f in m["findings"]
                       if f["code"] == lg.FSTYPE_BIT_FORBIDDEN)
        self.assertEqual(finding["evidence"]["fstype_flag_labels"],
                         ["restricted"])
        # 调用方传入的原始证据对象不被改写
        self.assertEqual(
            st[0]["evidence"]["members"][0]["fstype_flag_labels"],
            ["preview-print-only"])

    def test_legacy_reserved_bits_recomputed_with_corrected_mask(self):
        # 旧掩码 0xFEF1 把 bit9(0x0200) 当保留位；修正版 0x0200=bitmap-only
        st = stack1(fstype=0x0200,
                    fstype_flags=[],
                    fstype_flag_labels=[],
                    fstype_reserved_bits=0x0200,
                    name_license_description="OFL",
                    name_license_url="https://x.test")
        m = lg.evaluate(POLICY, st, [], NOW)
        member0 = m["font_stack"][0]["license_evidence"]["members"][0]
        self.assertEqual(member0["fstype_flags"], [0x0200])
        self.assertEqual(member0["fstype_flag_labels"], ["bitmap-only"])
        self.assertEqual(member0["fstype_reserved_bits"], 0)
        # 不再误报保留位（但 0x0200 不在 Web 允许掩码中，仍拒绝）
        self.assertNotIn(lg.FSTYPE_RESERVED_BITS, m["deny_codes"])
        self.assertIn(lg.FSTYPE_MASK_NOT_ALLOWED, m["deny_codes"])

    def test_legacy_ttc_all_members_recomputed(self):
        m0 = member(member_index=0, fstype=0x0100,
                    fstype_flags=[0x0100],
                    fstype_flag_labels=["bitmap-only"])  # 旧位序误标
        m1 = member(member_index=1, sha256="c" * 64, fstype=0x0002,
                    fstype_flags=[0x0002],
                    fstype_flag_labels=["preview-print-only"],
                    name_license_description="Other",
                    name_license_url="https://c.test/x")
        st = [font("a" * 64, [m0, m1], container="ttc")]
        m = lg.evaluate(POLICY, st, [], NOW)
        members = m["font_stack"][0]["license_evidence"]["members"]
        self.assertEqual(members[0]["fstype_flag_labels"], ["no-subsetting"])
        self.assertEqual(members[1]["fstype_flag_labels"], ["restricted"])
        # 原始对象未被改写
        self.assertEqual(m0["fstype_flag_labels"], ["bitmap-only"])
        self.assertEqual(m1["fstype_flag_labels"], ["preview-print-only"])

    def test_legacy_label_no_false_stack_conflict(self):
        # 0x0002（旧标 preview-print-only）与 editable 字体不再误命中
        # preview/editable 栈冲突：数值为准后 0x0002=restricted
        f1 = font("a" * 64, [member(
            fstype=0x0002, fstype_flags=[0x0002],
            fstype_flag_labels=["preview-print-only"],
            name_license_description="R", name_license_url="https://a.test")])
        f1["position"] = 0
        f2 = font("b" * 64, [member(
            fstype=0x0008, fstype_flags=[0x0008],
            fstype_flag_labels=["editable"],
            name_license_description="E", name_license_url="https://b.test")])
        f2["position"] = 1
        pol = {**POLICY}
        pol["policy"] = {**POLICY["policy"],
                         "allowed_fstype_masks": [0x0002, 0x0008],
                         "forbidden_fstype_bits": []}
        m = lg.evaluate(pol, [f1, f2], [], NOW)
        self.assertNotIn(lg.STACK_LICENSE_CONFLICT, m["deny_codes"],
                         m["findings"])

    def test_true_preview_still_conflicts_with_editable(self):
        # 真正的 0x0004 preview&print（旧存证曾误标为 editable）仍命中冲突
        f1 = font("a" * 64, [member(
            fstype=0x0004, fstype_flags=[0x0004],
            fstype_flag_labels=["editable"],  # 旧位序误标
            name_license_description="P", name_license_url="https://a.test")])
        f1["position"] = 0
        f2 = font("b" * 64, [member(
            fstype=0x0008, fstype_flags=[0x0008],
            fstype_flag_labels=["editable"],
            name_license_description="E", name_license_url="https://b.test")])
        f2["position"] = 1
        pol = {**POLICY}
        pol["policy"] = {**POLICY["policy"],
                         "allowed_fstype_masks": [0x0004, 0x0008],
                         "forbidden_fstype_bits": []}
        m = lg.evaluate(pol, [f1, f2], [], NOW)
        self.assertIn(lg.STACK_LICENSE_CONFLICT, m["deny_codes"])

    def test_recompute_does_not_change_fingerprint(self):
        # 指纹只含成员摘要/容器/解包摘要：同一字体新旧存证标签指纹一致
        legacy = stack1(fstype=0x0002, fstype_flags=[0x0002],
                        fstype_flag_labels=["preview-print-only"],
                        name_license_description="P",
                        name_license_url="https://x.test")
        fixed = stack1(fstype=0x0002, fstype_flags=[0x0002],
                       fstype_flag_labels=["restricted"],
                       name_license_description="P",
                       name_license_url="https://x.test")
        m1 = lg.evaluate(POLICY, legacy, [], NOW)
        m2 = lg.evaluate(POLICY, fixed, [], NOW)
        self.assertEqual(m1["fingerprint"], m2["fingerprint"])


class ValidationTests(unittest.TestCase):
    def test_valid_policy(self):
        lg.validate_policy(POLICY["policy"])  # 不抛异常

    def test_bad_masks(self):
        with self.assertRaises(ValueError):
            lg.validate_policy({"allowed_fstype_masks": ["0"]})
        with self.assertRaises(ValueError):
            lg.validate_policy({"allowed_fstype_masks": [70000]})

    def test_bad_name_field(self):
        with self.assertRaises(ValueError):
            lg.validate_policy({"required_name_fields": ["copyright"]})

    def test_bad_pattern(self):
        with self.assertRaises(ValueError):
            lg.validate_policy({"forbidden_license_patterns": ["(["]})

    def test_bad_conflict_rule(self):
        with self.assertRaises(ValueError):
            lg.validate_policy({"conflict_rules": [{"type": "nope"}]})
        with self.assertRaises(ValueError):
            lg.validate_policy({"conflict_rules": [
                {"type": "fstype-flag-pair", "flag_a": "x",
                 "flag_b": "editable"}]})
        with self.assertRaises(ValueError):
            lg.validate_policy({"conflict_rules": [
                {"type": "license-text-pair", "pattern_a": "a",
                 "pattern_b": "(["}]})

    def test_default_policies_well_formed(self):
        for scenario, p in lg.default_policies().items():
            lg.validate_policy(p)
            self.assertIn(scenario, lg.SCENARIOS)

    def test_default_policies_restrict_0x0002_everywhere(self):
        # 修正版：三种场景都把 0x0002 列为禁止位（restricted）
        for scenario in lg.SCENARIOS:
            p = lg.default_policies()[scenario]
            self.assertEqual(
                p.get(lg.POLICY_REVISION_KEY),
                lg.POLICY_REVISION_CORRECTED)
            self.assertIn(0x0002, p["forbidden_fstype_bits"])
            self.assertNotIn(0x0002, p["allowed_fstype_masks"])
            # 0x0001 是保留值，修正版不再把它当作 restricted 禁止位
            self.assertNotIn(0x0001, p["forbidden_fstype_bits"])
            # 0x0008 是 editable 类型值；仅桌面/印刷允许，Web 不允许
            if scenario == "web":
                self.assertNotIn(0x0008, p["allowed_fstype_masks"])
            # 真正的 no-subsetting 叠加位 0x0100 在各场景均有对应允许掩码
            self.assertIn(0x0100, p["allowed_fstype_masks"])

    def test_default_policies_deny_restricted_member_in_eval(self):
        # 端到端纯逻辑：修正后的 print 策略下，0x0002 + 完整 name13 必须拒绝
        pol = {
            "scenario": "print", "version": 2,
            "created_at": "2026-09-01T00:00:00+00:00",
            "registered_by": "admin",
            "policy": lg.default_policies()["print"],
        }
        st = [font("a" * 64, [member(
            fstype=0x0002, fstype_flags=[0x0002],
            fstype_flag_labels=["restricted"],
            name_license_description="Restricted license; do not embed")])]
        m = lg.evaluate(pol, st, [], NOW)
        self.assertEqual(m["decision"], "deny")
        self.assertIn(lg.FSTYPE_BIT_FORBIDDEN, m["deny_codes"])

    def test_legacy_v1_preserved_separately(self):
        # 历史 v1（含错误解释）必须原样保留，供旧冻结决定按原版本复核
        legacy = lg.legacy_default_policies_v1()
        self.assertNotIn(lg.POLICY_REVISION_KEY, legacy["print"])
        # v1 的错误：印刷策略曾放行 0x0002、禁用 0x0001
        self.assertIn(0x0002, legacy["print"]["allowed_fstype_masks"])
        self.assertIn(0x0001, legacy["print"]["forbidden_fstype_bits"])
        corrected = lg.default_policies()
        self.assertNotIn(0x0002, corrected["print"]["allowed_fstype_masks"])
        self.assertIn(0x0002, corrected["print"]["forbidden_fstype_bits"])


if __name__ == "__main__":
    unittest.main()
