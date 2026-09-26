"""分析编排：从请求参数到完整的可复核 JSON 结果。"""
from __future__ import annotations

import unicodedata
from typing import Any

import uharfbuzz as hb

from .font_loader import LoadedFont, is_variation_selector
from .shaper import (
    DEFAULT_FEATURES,
    Cluster,
    _feature_dict,
    build_clusters,
    shape_run,
    split_runs,
    normalize_text,
)

HARFBUZZ_VERSION = hb.version_string()


def _cp_meta(cp: int) -> dict[str, Any]:
    ch = chr(cp)
    try:
        name = unicodedata.name(ch)
    except ValueError:
        if is_variation_selector(cp):
            name = f"VARIATION SELECTOR-{cp - 0xFE00 + 1 if cp < 0xE0100 else cp - 0xE0100 + 17}"
        elif 0xE000 <= cp <= 0xF8FF:
            name = "<private use>"
        else:
            name = "<no Unicode name>"
    cat = unicodedata.category(ch)
    return {
        "codepoint": f"U+{cp:04X}",
        "name": name,
        "category": cat,
        "combining_class": unicodedata.combining(ch),
    }


def _cluster_text_kind(c: Cluster) -> str:
    cps = [ord(ch) for ch in c.text]
    if any(is_variation_selector(cp) for cp in cps):
        return "variation-sequence"
    if 0x200D in cps:
        return "zwj-sequence"
    if any(unicodedata.category(ch) in ("Mn", "Me") for ch in c.text):
        return "base+marks"
    if any(0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF for cp in cps):
        return "symbol/emoji"
    if len(cps) == 1:
        return "single"
    return "multi-codepoint"


def analyze(text: str, stack: list[LoadedFont], language: str,
            script_override: str | None, direction: str,
            feature_toggles: list[tuple[str, bool]], norm: str
            ) -> dict[str, Any]:
    text = normalize_text(text, norm)
    vertical = direction == "ttb"

    clusters, base_level, levels = build_clusters(
        text, "ltr" if vertical else direction)
    runs = split_runs(clusters)

    features = _feature_dict(feature_toggles)
    if vertical:
        # 竖排时启用垂直替换（用户显式开关仍优先）
        features = {**features, "vrt2": features.get("vrt2", True)}
    attempts_all: list[dict[str, Any]] = []
    run_summaries: list[dict[str, Any]] = []

    for ri, run in enumerate(runs):
        run_script = (script_override or run[0].script)
        before = len(attempts_all)
        shape_run(run, text, stack, language, features, vertical, attempts_all)
        run_summaries.append({
            "run_index": ri,
            "cluster_start": run[0].index,
            "cluster_end": run[-1].index + 1,
            "text": text[run[0].start:run[-1].end],
            "script": run_script,
            "bidi_level": run[0].level,
            "direction": ("ttb" if vertical
                          else ("rtl" if run[0].level % 2 else "ltr")),
            "attempts": list(range(before, len(attempts_all))),
        })

    cluster_out: list[dict[str, Any]] = []
    missing = 0
    fallback_count = 0
    for c in clusters:
        if c.resolved_font is None:
            missing += 1
        if c.font_position > 0:
            fallback_count += 1
        glyphs = c.glyphs or []
        merged = any(g.get("merges_grapheme_clusters") for g in glyphs)
        rec: dict[str, Any] = {
            "cluster_index": c.index,
            "text": c.text,
            "codepoints": [_cp_meta(ord(ch)) for ch in c.text],
            "char_start": c.start,
            "char_end": c.end,
            "bidi_level": c.level,
            "script": (script_override or c.script),
            "kind": _cluster_text_kind(c),
            "covered": c.resolved_font is not None,
            "font_position": c.font_position,
            "font_sha256": c.resolved_font.digest if c.resolved_font else None,
            "font_family": c.resolved_font.family if c.resolved_font else None,
            "fallback_reason": c.reason or ("primary" if c.font_position == 0
                                            else "exhausted"),
            "merged_into_ligature": merged,
            "warnings": c.warnings,
            "glyph_count": len(glyphs),
            "glyphs": glyphs,
        }
        cluster_out.append(rec)

    ligature_glyphs = sum(
        1 for c in cluster_out
        for g in c["glyphs"]
        if g.get("merges_grapheme_clusters"))
    return {
        "engine": {
            "shaper": "HarfBuzz (uharfbuzz)",
            "harfbuzz_version": HARFBUZZ_VERSION,
            "cluster_policy": "UAX #29 grapheme clusters; "
                              "hb buffer cluster_level=monotone-characters",
        },
        "input": {
            "text": text,
            "text_length_chars": len(text),
            "num_clusters": len(clusters),
            "language": language,
            "script": script_override or "auto",
            "direction_requested": direction,
            "paragraph_base_level": base_level,
            "vertical": vertical,
            "normalization": norm,
            "bidi_levels_per_char": levels,
        },
        "font_stack": [
            {"position": i, "sha256": f.digest, "family": f.family,
             "subfamily": f.subfamily, "postscript": f.postscript}
            for i, f in enumerate(stack)
        ],
        "features": {
            "defaults": DEFAULT_FEATURES,
            "effective": features,
            "user_overrides": [
                {"tag": t, "value": "on" if v else "off"}
                for t, v in feature_toggles
            ],
        },
        "runs": run_summaries,
        "shaping_attempts": attempts_all,
        "clusters": cluster_out,
        "stats": {
            "num_clusters": len(clusters),
            "covered": len(clusters) - missing,
            "missing": missing,
            "served_by_primary": sum(1 for c in clusters
                                    if c.font_position == 0),
            "served_by_fallback": fallback_count,
            "clusters_merged_into_ligature": sum(
                1 for c in cluster_out if c["merged_into_ligature"]),
            "ligature_glyph_instances": ligature_glyphs,
        },
    }


def font_summary(lf: LoadedFont, filename: str) -> dict[str, Any]:
    from .font_loader import font_metadata
    meta = font_metadata(lf, filename)
    meta["num_glyphs_verified"] = lf.num_glyphs
    return meta
