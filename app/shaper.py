"""HarfBuzz 整形与按字素簇的字体回退引擎。

设计要点
--------
* 输入文本先按 Unicode 字素簇 (UAX #29) 切分，回退与报告均以“簇”为单位，
  因此组合附加符、ZWJ emoji 序列、变体选择符序列不会被拆散。
* 水平方向使用 Unicode 双向算法 (python-bidi) 得到每个字符的嵌入级别，
  按级别切分为若干 bidi run；再按书写系统 (hb Unicode funcs 的脚本值，
  按 UAX #24 继承 Common/Inherited 脚本) 切分为 shape run。
* 每个 shape run 用字体栈依次整体整形；HarfBuzz 输出中若出现 gid 0
  (.notdef)，则把对应簇区间切出，用栈中下一个字体继续整形，直到全部
  覆盖或字体栈耗尽（标记 missing）。连字使多个簇合并为一个字形时，
  该字形的字体/定位信息归属到它覆盖的每一个簇。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

import uharfbuzz as hb
from bidi.algorithm import (
    explicit_embed_and_overrides,
    get_base_level,
    get_embedding_levels,
    get_empty_storage,
    resolve_implicit_levels,
    resolve_neutral_types,
    resolve_weak_types,
)

from .font_loader import (
    LoadedFont,
    cluster_has_join_control,
    find_vs_in_cluster,
    grapheme_clusters,
    is_variation_selector,
)
from .script_data import is_default_ignorable, script_of

# 整形默认启用的特性（HarfBuzz 常规默认），用户开关在其上叠加。
# 注意：vrt2（竖排替代）不放在全局默认中——仅竖排方向时由编排层加入。
DEFAULT_FEATURES: dict[str, bool] = {
    "calt": True,
    "ccmp": True,
    "clig": True,
    "liga": True,
    "locl": True,
    "rlig": True,
    "rclt": True,
    "kern": True,
    "mark": True,
    "mkmk": True,
    "curs": True,
    "isol": True,
    "fina": True,
    "medi": True,
    "init": True,
    "med2": True,
    "fin2": True,
    "haln": True,
}


# ------------------------------------------------------------------ 数据结构

@dataclass
class Cluster:
    index: int                 # 逻辑文本中的簇序号
    text: str                  # 原始簇文本
    start: int                 # 起始字符下标
    end: int                   # 结束字符下标（不含）
    level: int = 0
    script: str = ""           # hb 4 字节脚本标签，如 Latn
    resolved_font: Optional[LoadedFont] = None
    font_position: int = -1    # 命中字体在栈中的位置（0 = 首选）
    reason: str = ""
    glyphs: list[dict[str, Any]] = None
    warnings: list[str] = None

    def __post_init__(self) -> None:
        if self.glyphs is None:
            self.glyphs = []
        if self.warnings is None:
            self.warnings = []


# ------------------------------------------------------------------ 文本预处理

def normalize_text(text: str, mode: str) -> str:
    if mode == "nfc":
        return unicodedata.normalize("NFC", text)
    if mode == "nfkc":
        return unicodedata.normalize("NFKC", text)
    return text


def _logical_bidi_levels(text: str, direction: str) -> tuple[int, list[int]]:
    """返回 (段落基级别, 每个逻辑字符的最终嵌入级别)。"""
    if direction in ("ltr", "ttb"):
        base_level = 0
    elif direction == "rtl":
        base_level = 1
    else:
        base_level = get_base_level(text)

    storage = get_empty_storage()
    storage["base_level"] = base_level
    storage["base_dir"] = ("R", "L")[base_level]
    get_embedding_levels(text, storage, False, False)
    explicit_embed_and_overrides(storage, False)
    resolve_weak_types(storage, False)
    resolve_neutral_types(storage, False)
    resolve_implicit_levels(storage, False)

    # python-bidi 的 X9 步骤会从存储中移除 BN 类字符（ZWJ/ZWNJ/WJ 等）。
    # 存活字符保持原序，按顺序对齐回原码位；被移除字符沿用相邻存活字符
    # 的嵌入级别（UBA 中 BN 字符最终获得其所属 run 的级别）。
    levels = [base_level] * len(text)
    si = 0
    last_level = base_level
    for i, ch in enumerate(text):
        if si < len(storage["chars"]) and ch == storage["chars"][si]["ch"]:
            last_level = storage["chars"][si]["level"]
            si += 1
        levels[i] = last_level
    return base_level, levels


def _script_tag(cp: int) -> str:
    return script_of(cp)


def _cluster_scripts(text: str) -> str:
    """用簇内第一个有确定脚本的字符决定脚本。"""
    for ch in text:
        tag = _script_tag(ord(ch))
        if tag not in ("Zyyy", "Zinh"):
            return tag
    return "Zyyy"


def build_clusters(text: str, direction: str) -> tuple[list[Cluster], int, list[int]]:
    """切分字素簇并附加 bidi 级别。"""
    base_level, levels = _logical_bidi_levels(text, direction)
    parts = grapheme_clusters(text)
    clusters: list[Cluster] = []
    pos = 0
    for i, part in enumerate(parts):
        end = pos + len(part)
        # 以簇首字符的 bidi 级别作为簇级别
        cl = levels[pos]
        clusters.append(Cluster(index=i, text=part, start=pos, end=end,
                                level=cl, script=_cluster_scripts(part)))
        pos = end
    # UAX #24 风格：Common/Inherited 簇继承邻近的确定脚本（向前，再向后）
    for i, c in enumerate(clusters):
        if c.script in ("Zyyy", "Zinh"):
            for j in range(i - 1, -1, -1):
                if clusters[j].script not in ("Zyyy", "Zinh"):
                    c.script = clusters[j].script
                    break
    for i in range(len(clusters) - 1, -1, -1):
        c = clusters[i]
        if c.script in ("Zyyy", "Zinh"):
            for j in range(i + 1, len(clusters)):
                if clusters[j].script not in ("Zyyy", "Zinh"):
                    c.script = clusters[j].script
                    break
    return clusters, base_level, levels


def split_runs(clusters: list[Cluster]) -> list[list[Cluster]]:
    """按 (bidi 级别, 脚本) 把簇切为 shape run，保持逻辑顺序。"""
    runs: list[list[Cluster]] = []
    cur: list[Cluster] = []
    key = None
    for c in clusters:
        k = (c.level, c.script)
        if key is not None and k != key:
            runs.append(cur)
            cur = []
        cur.append(c)
        key = k
    if cur:
        runs.append(cur)
    return runs


# ------------------------------------------------------------------ 整形

def _feature_dict(toggles: list[tuple[str, bool]]) -> dict[str, bool]:
    feats = dict(DEFAULT_FEATURES)
    for tag, val in toggles:
        feats[tag] = val
    return feats


def _hb_direction(level: int, vertical: bool) -> str:
    if vertical:
        return "ttb"
    return "rtl" if level % 2 else "ltr"


def _shape_substring(lf: LoadedFont, text: str, script: str, level: int,
                     vertical: bool, language: str,
                     features: dict[str, bool]
                     ) -> tuple[list, list, list, list]:
    """对一段逻辑文本整形，返回 (glyph_infos, positions, metrics, names)。
    RTL 时输出为视觉顺序（簇号递减），由调用方负责映射。"""
    buf = hb.Buffer()
    buf.add_str(text)
    buf.direction = _hb_direction(level, vertical)
    buf.script = script
    buf.language = (language or "dflt").replace("_", "-")
    buf.cluster_level = hb.BufferClusterLevel.MONOTONE_CHARACTERS
    with lf.lock:
        hb.shape(lf.hb_font, buf, features)
        infos = list(buf.glyph_infos)
        positions = list(buf.glyph_positions)
        # 字形度量需要在锁内取出（hb_font 访问非可重入）
        metrics = []
        for info in infos:
            gid = info.codepoint
            ext = lf.hb_font.get_glyph_extents(gid)
            if _hb_direction(level, vertical) == "ttb":
                adv = lf.hb_font.get_glyph_v_advance(gid)
            else:
                adv = lf.hb_font.get_glyph_h_advance(gid)
            metrics.append((adv, ext))
        names = [lf.glyph_name(info.codepoint) for info in infos]
    return infos, positions, metrics, names


def _glyph_record(gid: int, name: str, pos, metrics, cluster_offset: int,
                  upem: int, vertical: bool) -> dict[str, Any]:
    advance, ext = metrics
    rec = {
        "gid": gid,
        "name": name,
        "notdef": gid == 0,
        "cluster_offset": cluster_offset,
        "x_advance": pos.x_advance,
        "y_advance": pos.y_advance,
        "x_offset": pos.x_offset,
        "y_offset": pos.y_offset,
        "nominal_advance": advance,
    }
    if ext is not None:
        rec["extents"] = {
            "x_bearing": ext.x_bearing,
            "y_bearing": ext.y_bearing,
            "width": ext.width,
            "height": ext.height,
        }
    rec["x_advance_em"] = round(pos.x_advance / upem, 5)
    rec["y_advance_em"] = pos.y_advance / upem
    rec["x_offset_em"] = pos.x_offset / upem
    rec["y_offset_em"] = pos.y_offset / upem
    return rec


def _cluster_variation_ok(lf: LoadedFont, ctext: str) -> tuple[bool, str]:
    """检查簇中的变体序列是否被该字体的 format-14 cmap 支持。"""
    vs = find_vs_in_cluster(ctext)
    if vs is None:
        return True, ""
    base_i, vs_i = vs
    base_cp = ord(ctext[base_i])
    vs_cp = ord(ctext[vs_i])
    if lf.variation_glyph_id(base_cp, vs_cp) is None:
        return False, (f"variation-sequence U+{base_cp:04X} U+{vs_cp:04X} "
                       "not in cmap format-14")
    return True, ""


def shape_run(run: list[Cluster], full_text: str, stack: list[LoadedFont],
              language: str, features: dict[str, bool], vertical: bool,
              attempts: list[dict[str, Any]]) -> None:
    """对一个 shape run 执行带回退的整形，并把结果写入每个 Cluster。

    attempts 记录每次整形尝试（用于回退原因审计）。
    """
    level = run[0].level
    script = run[0].script

    # 待处理区间：(起始簇序号(含), 结束簇序号(不含), 起始字体栈位置)
    pending: list[tuple[int, int, int]] = [(0, len(run), 0)]

    while pending:
        ci0, ci1, font_pos = pending.pop(0)
        seg_clusters = run[ci0:ci1]
        seg_text = full_text[seg_clusters[0].start:seg_clusters[-1].end]
        offset = seg_clusters[0].start

        # 字体栈耗尽
        if font_pos >= len(stack):
            for c in seg_clusters:
                ignorable_only = all(is_default_ignorable(ord(ch))
                                     for ch in c.text)
                if ignorable_only:
                    # 不可见字符：归属首选字体（不参与可见覆盖），不算缺字
                    c.resolved_font = stack[0] if stack else None
                    c.font_position = 0
                    c.reason = "primary"
                    if not any("default-ignorable" in w
                               for w in c.warnings):
                        c.warnings.append(
                            "default-ignorable cluster produced no visible "
                            "glyph (expected per UCD)")
                    c.glyphs = []
                    continue
                c.resolved_font = None
                c.font_position = -1
                if not c.reason or not c.reason.startswith("exhausted"):
                    prior = c.reason or "notdef in every font"
                    c.reason = f"exhausted: {prior}"
                c.glyphs = []
            attempts.append({
                "text": seg_text, "script": script, "level": level,
                "font_position": None, "result": "font-stack-exhausted",
            })
            continue

        lf = stack[font_pos]
        infos, positions, metrics, names = _shape_substring(
            lf, seg_text, script, level, vertical, language, features)
        out_dir = _hb_direction(level, vertical)
        rtl = out_dir == "rtl"
        seg_len = len(seg_text)

        # ---- 输出字形的“输入覆盖区间”
        # HarfBuzz cluster 值是字形输入区间的起点；连字会让相邻字形共享同
        # 一簇值（区间扩展到下一不同簇值）。MONOTONE_CHARACTERS 下输出按
        # 簇值单调（LTR 非减 / RTL 非增），故按输出方向求下一个不同簇值
        # 即可得到该字形覆盖的输入字符区间 [start, end)。
        order = list(range(len(infos)))
        recs: list[dict[str, Any]] = []
        for idx in order:
            info = infos[idx]
            recs.append(_glyph_record(
                info.codepoint, names[idx], positions[idx], metrics[idx],
                info.cluster, lf.upem, vertical))

        # ---- 输出字形的输入覆盖区间与簇归属
        # 每个字形按 cluster 起始偏移唯一归属到“簇首偏移等于该值”的簇；
        # 连字时多个输入簇共享同一个簇值（这些簇的簇首偏移都等于该值），
        # 因此连字字形自然地共享给它合并的每一个簇。阿拉伯文等连写字体
        # 每个字符仍产生独立起始簇值，字形一一归属，不会跨簇重复。
        # 同时计算 [input_start, input_end) 仅供报告展示该字形实际吞并的
        # 输入范围。
        cluster_starts = frozenset(c.start - offset for c in seg_clusters)
        cvals = [info.cluster for info in infos]
        starts: dict[int, list[dict[str, Any]]] = {}
        starts_notdef: set[int] = set()
        valid_span_mask = [False] * seg_len
        for i, rec in enumerate(recs):
            cval = cvals[i]
            rec.pop("cluster_offset", None)
            # 簇值始终是输入区间的逻辑起点。逻辑上“下一个”字形在视觉序中
            # 的位置：LTR 为 i+1，RTL 为 i-1。沿该方向寻找下一个不同簇值，
            # 即本字形输入区间的末端。
            step = -1 if rtl else 1
            j = i + step
            hi = seg_len
            while 0 <= j < len(cvals):
                if cvals[j] != cval:
                    hi = cvals[j]
                    break
                j += step
            lo, hi = cval, (hi if not rtl else max(hi, cval + 1))
            rec["input_start"] = lo
            rec["input_end"] = hi
            # 字形吞并的“字素簇首”数量；阿拉伯连写只跨同一单字符簇内的
            # 字符，不会命中多个簇首，只有真正合并多簇的连字（fi、لا）才为真。
            consumed_clusters = sum(1 for o in cluster_starts
                                    if lo <= o < hi)
            rec["merges_grapheme_clusters"] = consumed_clusters > 1
            starts.setdefault(cval, []).append(rec)
            if rec["gid"] == 0:
                starts_notdef.add(cval)
            else:
                for o in range(lo, hi):
                    valid_span_mask[o] = True

        # ---- 判定每个簇是否覆盖（字符级，再聚合到簇）：
        # 每个输入字符偏移都必须满足：有非 notdef 字形起始于该偏移，或被
        # 某个非 notdef 字形的输入区间吞并（连字）。簇内任一字符不满足，
        # 整簇即未覆盖——这样基字符与组合附加符会作为一个整体回退。
        # 特殊情况：
        #  * 整簇仅由 Default_Ignorable 字符构成（ZWJ/方向控制符等）-> 不
        #    产生可见字形是预期行为，算覆盖并记录警告；
        #  * format-14 变体序列缺失只作警告（HarfBuzz 会回落到基字形）。
        bad: list[int] = []
        for k, c in enumerate(seg_clusters):
            c0, c1 = c.start - offset, c.end - offset
            ignorable_only = all(is_default_ignorable(ord(ch))
                                 for ch in c.text)
            uncovered_chars = [
                o for o in range(c0, c1)
                if o in starts_notdef
                or (o not in starts and not valid_span_mask[o])
            ]
            vs_ok, vs_note = _cluster_variation_ok(lf, c.text)
            if not vs_ok:
                c.warnings.append(vs_note)
            if ignorable_only and uncovered_chars:
                c.warnings.append(
                    "default-ignorable cluster produced no visible glyph "
                    "(expected per UCD)")
                continue
            if uncovered_chars:
                bad.append(k)

        attempt = {
            "text": seg_text,
            "font_sha256": lf.digest,
            "font_family": lf.family,
            "font_position": font_pos,
            "script": script,
            "level": level,
            "direction": out_dir,
            "num_glyphs": len(infos),
            "uncovered_clusters": [seg_clusters[k].index for k in bad],
            "result": "covered" if not bad else "partial",
        }
        attempts.append(attempt)

        if not bad:
            # 全部覆盖：连字字形可能覆盖多个簇，按簇归属复制记录
            note = ""
            if font_pos > 0:
                note = ("fallback: previous font(s) lacked coverage — "
                        + (seg_clusters[0].reason or "notdef in prior font"))
            _assign_glyphs(seg_clusters, starts, recs, offset, lf,
                           font_pos, note, rtl)
            continue

        # ---- 部分覆盖：未覆盖区间交给下一个字体；已覆盖簇保留本字体
        # 先把好簇标记为本字体
        bad_set = set(bad)
        good = [k for k in range(len(seg_clusters)) if k not in bad_set]
        if good:
            note = ""
            if font_pos > 0:
                prior = seg_clusters[good[0]].reason or "notdef in prior font"
                note = f"fallback: previous font(s) lacked coverage — {prior}"
            _assign_glyphs(
                [seg_clusters[k] for k in good],
                starts, recs, offset, lf, font_pos, note, rtl)

        # 连续的坏簇合并成一个区间，尝试下一个字体
        start = bad[0]
        prev = bad[0]
        sub: list[tuple[int, int]] = []
        for k in bad[1:]:
            if k == prev + 1:
                prev = k
                continue
            sub.append((start, prev + 1))
            start = prev = k
        sub.append((start, prev + 1))
        # 保持逻辑顺序入队（对最终结果无影响，但 attempts 更易读）
        for s0, s1 in reversed(sub):
            pending.insert(0, (ci0 + s0, ci0 + s1, font_pos + 1))

        # 坏簇的回退原因（在最终字体确定后仍可能改写；先记录本次发现）
        for k in bad:
            c = seg_clusters[k]
            c.reason = reason_for_cluster(c.text)


def _assign_glyphs(clusters: list[Cluster], glyphs_by_start: dict[int, list],
                   all_recs: list[dict[str, Any]], offset: int,
                   lf: LoadedFont, font_pos: int,
                   reason: str, rtl: bool) -> None:
    """把字形归属到簇：
    * 起始于簇内任一字符偏移的字形（基字形、组合附加符各自起始）；
    * 以及输入区间吞并了簇内字符的字形（如 fi 连字中被合并的 i）。
    保持 HarfBuzz 输出顺序（RTL 即视觉顺序）。"""
    for c in clusters:
        c0, c1 = c.start - offset, c.end - offset
        by_id: dict[int, dict[str, Any]] = {}
        for o in range(c0, c1):
            for rec in glyphs_by_start.get(o, []):
                if rec["gid"] != 0:
                    by_id.setdefault(id(rec), rec)
        for rec in all_recs:
            if rec["gid"] == 0:
                continue
            if any(rec["input_start"] <= o < rec["input_end"]
                   for o in range(c0, c1)):
                by_id.setdefault(id(rec), rec)
        ordered = [r for r in all_recs if id(r) in by_id]
        c.resolved_font = lf
        c.font_position = font_pos
        c.reason = reason
        c.glyphs = ordered
        if rtl:
            for r in c.glyphs:
                r["visual_order"] = True


def reason_for_cluster(text: str, prefix: str = "notdef") -> str:
    """给出簇未被某字体覆盖的人类可读原因。"""
    details = []
    cps = [ord(ch) for ch in text]
    has_vs = any(is_variation_selector(cp) for cp in cps)
    has_joiner = cluster_has_join_control(text)
    has_combining = any(unicodedata.category(ch) in ("Mn", "Me")
                        for ch in text)
    if all(is_default_ignorable(cp) for cp in cps):
        details.append("default-ignorable/joiner-only cluster")
    if len(cps) > 1 and has_joiner:
        details.append("ZWJ/ZWNJ ligature or join sequence")
    if has_vs:
        details.append("variation selector sequence")
    if has_combining:
        marks = " ".join(f"U+{cp:04X}" for cp in cps
                         if unicodedata.category(chr(cp)) in ("Mn", "Me"))
        base_cp = next((cp for cp in cps
                        if unicodedata.category(chr(cp)) not in ("Mn", "Me", "Cf")
                        and not is_variation_selector(cp)), None)
        base_name = ""
        if base_cp is not None:
            try:
                base_name = " on base U+%04X (%s)" % (
                    base_cp, unicodedata.name(chr(base_cp)))
            except ValueError:
                base_name = f" on base U+{base_cp:04X}"
        details.append(f"combining mark sequence ({marks}){base_name}")
    if not details:
        first = next(ch for ch in text
                     if unicodedata.category(ch) not in ("Cf",))
        cat = unicodedata.category(first)
        cat_desc = {
            "Lo": "letter (other)", "Ll": "lowercase letter",
            "Lu": "uppercase letter", "Lt": "titlecase letter",
            "Lm": "modifier letter", "N": "number",
            "Nd": "decimal number", "So": "symbol (other)",
            "Sk": "modifier symbol", "Co": "private-use codepoint",
        }.get(cat, f"category {cat}")
        try:
            name = unicodedata.name(first)
        except ValueError:
            name = "unnamed codepoint"
        details.append(f"{cat_desc} U+{ord(first):04X} ({name}) "
                       "missing in cmap / shaped to .notdef")
    return f"{prefix}: " + "; ".join(details)
