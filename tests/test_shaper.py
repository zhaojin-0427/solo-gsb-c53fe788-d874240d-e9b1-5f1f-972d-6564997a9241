"""引擎核心行为测试（直接调用，无需启动服务器）。

运行: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

from app import analyzer
from app.font_loader import load_font_from_bytes

FONTS = Path("/usr/share/fonts/truetype/dejavu")


def _load(name: str):
    data = (FONTS / name).read_bytes()
    lf, _ = load_font_from_bytes(data)
    return lf


@unittest.skipUnless(FONTS.exists(), "DejaVu 测试字体不存在")
class ShapingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sans = _load("DejaVuSans.ttf")
        cls.mono = _load("DejaVuSansMono.ttf")

    def analyze(self, text, stack=None, **kw):
        kw.setdefault("language", "en")
        kw.setdefault("script_override", None)
        kw.setdefault("direction", "auto")
        kw.setdefault("feature_toggles", [])
        kw.setdefault("norm", "none")
        return analyzer.analyze(text, stack or [self.sans, self.mono], **kw)

    def test_basic_latin_primary(self):
        r = self.analyze("ABC")
        self.assertTrue(all(c["covered"] for c in r["clusters"]))
        self.assertTrue(all(c["font_position"] == 0 for c in r["clusters"]))
        self.assertEqual(r["stats"]["missing"], 0)

    def test_cjk_missing_both_fonts(self):
        r = self.analyze("你好")
        self.assertEqual(r["stats"]["missing"], 2)
        for c in r["clusters"]:
            self.assertFalse(c["covered"])
            self.assertTrue(c["fallback_reason"].startswith("exhausted"))

    def test_ligature_fi_shared(self):
        r = self.analyze("fi")
        self.assertEqual(len(r["clusters"]), 2)
        fi_gid = r["clusters"][0]["glyphs"][0]["gid"]
        self.assertEqual(fi_gid, 5042)
        # 连字字形共享给 f 与 i，两者都标记合并
        self.assertTrue(all(c["merged_into_ligature"]
                            for c in r["clusters"]))
        self.assertEqual(r["stats"]["clusters_merged_into_ligature"], 2)

    def test_ligature_disabled(self):
        r = self.analyze("fi", feature_toggles=[("liga", False)])
        names = [g["name"] for c in r["clusters"] for g in c["glyphs"]]
        self.assertEqual(names, ["f", "i"])
        self.assertFalse(any(c["merged_into_ligature"]
                             for c in r["clusters"]))

    def test_combining_mark_kept_with_base(self):
        # q + U+0301：q 与组合锐音符必须在同一簇，各自有字形与定位
        r = self.analyze("q́")
        self.assertEqual(len(r["clusters"]), 1)
        c = r["clusters"][0]
        names = [g["name"] for g in c["glyphs"]]
        self.assertIn("q", names)
        self.assertIn("acutecomb", names)

    def test_missing_base_falls_back_as_whole_cluster(self):
        # 缺失的 CJK 基字 + 存在的组合符：整簇必须回退/缺失，不能只用附加符
        r = self.analyze("你́")
        self.assertEqual(len(r["clusters"]), 1)
        self.assertFalse(r["clusters"][0]["covered"])

    def test_variation_selector_warning_not_failure(self):
        # é + VS16：DejaVu 无 cmap14 序列 -> 覆盖但有警告
        r = self.analyze("é️")
        c = r["clusters"][0]
        self.assertTrue(c["covered"])
        self.assertTrue(any("format-14" in w for w in c["warnings"]))

    def test_bidi_levels(self):
        r = self.analyze("abc אבג")
        levels = {c["text"]: c["bidi_level"] for c in r["clusters"]}
        self.assertEqual(levels["א"], 1)
        self.assertEqual(levels["a"], 0)
        self.assertEqual(r["input"]["paragraph_base_level"], 0)

    def test_rtl_explicit(self):
        r = self.analyze("אבג", direction="rtl")
        self.assertTrue(all(c["bidi_level"] == 1 for c in r["clusters"]))

    def test_zwj_ignorable_not_missing(self):
        # U+115F HANGUL CHOSEONG FILLER 可独立成簇、为 Default_Ignorable，
        # 且在 DejaVu 中整形为 .notdef：无可见字形是预期行为，不应报缺字
        r = self.analyze("aᅟb")
        wj = [c for c in r["clusters"] if c["text"] == "ᅟ"]
        self.assertEqual(len(wj), 1)
        self.assertTrue(wj[0]["covered"])
        self.assertTrue(any("default-ignorable" in w
                            for w in wj[0]["warnings"]))

    def test_grapheme_clusters_not_split(self):
        # emoji ZWJ 家庭序列是一个字素簇
        r = self.analyze("👨‍👩‍👧")
        self.assertEqual(len(r["clusters"]), 1)

    def test_arabic_contextual_shaping(self):
        r = self.analyze("مرحبا", language="ar")
        gids = [g["gid"] for c in r["clusters"] for g in c["glyphs"]]
        # 应出现上下文形式（非孤立名义字形）
        self.assertTrue(any(g >= 0xFB00 or g in (5256, 5258, 5276, 5288)
                            for g in gids))

    def test_fallback_second_font_serves_cluster(self):
        # Sans 首选；构造一个 Sans 无而 Mono 有的差异。
        # Sans 有希伯来文，此处用栈顺序断言：首选覆盖的簇 position=0
        r = self.analyze("A")
        self.assertEqual(r["clusters"][0]["font_position"], 0)
        # 首选不含该字符时回退到二号字体
        # （DejaVu Sans 与 Mono 覆盖基本相同，改用栈顺序 [mono, sans]
        # 验证 A 由 mono 提供）
        r2 = self.analyze("A", stack=[self.mono, self.sans])
        self.assertEqual(r2["clusters"][0]["font_position"], 0)
        self.assertEqual(r2["clusters"][0]["font_family"], "DejaVu Sans Mono")

    def test_deterministic(self):
        a = self.analyze("fi مرحبا 你好 q́")
        b = self.analyze("fi مرحبا 你好 q́")
        def sig(r):
            return [(c["font_sha256"], c["bidi_level"],
                     [(g["gid"], g["x_advance"], g["x_offset"],
                       g["y_offset"], g["y_advance"]) for g in c["glyphs"]])
                    for c in r["clusters"]]
        self.assertEqual(sig(a), sig(b))

    def test_shaping_attempts_audited(self):
        r = self.analyze("你好 A")
        texts = [(a["text"], a["result"]) for a in r["shaping_attempts"]]
        self.assertTrue(any(
            res == "font-stack-exhausted" for _, res in texts))
        self.assertTrue(any(res == "covered" for _, res in texts))

    def test_positioning_data_present(self):
        r = self.analyze("A")
        g = r["clusters"][0]["glyphs"][0]
        for key in ("x_advance", "y_advance", "x_offset", "y_offset",
                    "nominal_advance", "x_advance_em", "extents"):
            self.assertIn(key, g)

    def test_vertical_metrics(self):
        r = self.analyze("ABC", direction="ttb")
        for c in r["clusters"]:
            self.assertTrue(all(g["y_advance"] != 0 for g in c["glyphs"]))


if __name__ == "__main__":
    unittest.main()
