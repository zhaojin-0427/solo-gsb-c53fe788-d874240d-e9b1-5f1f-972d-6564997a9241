"""font_loader 的 OS/2 fsType 位定义与取证标签测试（不依赖字体文件）。

覆盖真实 fsType embedding types（旧版 TrueType/OS/2 规范，也是本系统对受限
嵌入的判定依据）：
  0x0000 installable / 0x0002 restricted / 0x0004 preview&print /
  0x0008 editable / bit8 0x0100 no-subsetting / bit9 0x0200 bitmap-only；
  bit0、bit4–7、bit10–15 为保留位。
回归保护：修复前代码把类型值错按位位置解读（0x0002 误标为预览印刷、
0x0001 误标为 restricted、0x0008 误标为 no-subsetting、0x0100 误标为
bitmap-only）。
"""
from __future__ import annotations

import unittest

from app import font_loader as fl


class FsTypeConstantTests(unittest.TestCase):
    def test_true_bit_values(self):
        self.assertEqual(fl.FSTYPE_INSTALLABLE, 0x0000)
        self.assertEqual(fl.FSTYPE_RESTRICTED, 0x0002)
        self.assertEqual(fl.FSTYPE_PREVIEW, 0x0004)
        self.assertEqual(fl.FSTYPE_EDITABLE, 0x0008)
        self.assertEqual(fl.FSTYPE_NO_SUBSETTING, 0x0100)
        self.assertEqual(fl.FSTYPE_BITMAP_ONLY, 0x0200)

    def test_labels_for_true_values(self):
        self.assertEqual(
            fl.FSTYPE_FLAG_LABELS[fl.FSTYPE_RESTRICTED], "restricted")
        self.assertEqual(
            fl.FSTYPE_FLAG_LABELS[fl.FSTYPE_PREVIEW], "preview-print-only")
        self.assertEqual(
            fl.FSTYPE_FLAG_LABELS[fl.FSTYPE_EDITABLE], "editable")
        self.assertEqual(
            fl.FSTYPE_FLAG_LABELS[fl.FSTYPE_NO_SUBSETTING], "no-subsetting")
        self.assertEqual(
            fl.FSTYPE_FLAG_LABELS[fl.FSTYPE_BITMAP_ONLY], "bitmap-only")

    def test_reserved_mask_covers_bit0_bit4to7_bit10to15(self):
        # bit0（0x0001 保留类型值）必须纳入保留掩码
        self.assertTrue(fl.FSTYPE_RESERVED_MASK & 0x0001)
        # bit4–7
        for bit in (0x0010, 0x0020, 0x0040, 0x0080):
            self.assertTrue(fl.FSTYPE_RESERVED_MASK & bit, hex(bit))
        # bit10–15
        for bit in (0x0400, 0x0800, 0x1000, 0x2000, 0x4000, 0x8000):
            self.assertTrue(fl.FSTYPE_RESERVED_MASK & bit, hex(bit))
        # 已定义的有效位均不在保留掩码中
        for bit in (fl.FSTYPE_RESTRICTED, fl.FSTYPE_PREVIEW,
                    fl.FSTYPE_EDITABLE, fl.FSTYPE_NO_SUBSETTING,
                    fl.FSTYPE_BITMAP_ONLY):
            self.assertFalse(fl.FSTYPE_RESERVED_MASK & bit, hex(bit))
        self.assertEqual(fl.FSTYPE_RESERVED_MASK, 0xFCF1)

    def test_restricted_0x0002_is_not_confused_with_preview(self):
        # 修复前的核心错误回归：0x0002 必须标为 restricted
        flags = fl.fstype_flags(0x0002)
        self.assertEqual(flags, [0x0002])
        labels = [fl.FSTYPE_FLAG_LABELS[b] for b in flags]
        self.assertEqual(labels, ["restricted"])
        self.assertNotIn("preview-print-only", labels)
        # 0x0004 才是 preview & print
        self.assertEqual(
            [fl.FSTYPE_FLAG_LABELS[b] for b in fl.fstype_flags(0x0004)],
            ["preview-print-only"])

    def test_flags_combinations(self):
        # editable(0x0008) + no-subsetting(bit8)
        self.assertEqual(fl.fstype_flags(0x0108),
                         [0x0008, 0x0100])
        # installable 仅叠加 no-subsetting（常见真实字体值 0x0100）
        self.assertEqual(fl.fstype_flags(0x0100), [0x0100])
        self.assertEqual(
            [fl.FSTYPE_FLAG_LABELS[b] for b in fl.fstype_flags(0x0100)],
            ["no-subsetting"])
        # bitmap-only 叠加位是 0x0200，而非旧代码的 0x0100
        self.assertEqual(
            [fl.FSTYPE_FLAG_LABELS[b] for b in fl.fstype_flags(0x0200)],
            ["bitmap-only"])

    def test_installable_has_no_flags(self):
        self.assertEqual(fl.fstype_flags(0x0000), [])

    def test_reserved_value_0x0001_not_labeled_restricted(self):
        # 0x0001 是保留值，不得再产生 restricted 标签
        self.assertEqual(fl.fstype_flags(0x0001), [])
        self.assertEqual(fl.fstype_reserved_bits(0x0001), 0x0001)

    def test_reserved_bits_isolation(self):
        self.assertEqual(fl.fstype_reserved_bits(0x0002), 0)
        self.assertEqual(fl.fstype_reserved_bits(0x0100), 0)
        self.assertEqual(fl.fstype_reserved_bits(0x1000), 0x1000)


class NormalizeEvidenceFstypeTests(unittest.TestCase):
    """读取存证时以数值 fsType 为准重算派生字段（副本，不改原对象）。

    模拟升级前（v1 旧位序）登记的存证：0x0002 被误标 preview-print-only、
    0x0100 被误标 bitmap-only、保留位按旧掩码 0xFEF1 计算。
    """

    def _legacy_evidence(self):
        return {
            "container": "ttc",
            "members_complete": True,
            "unpack_summary": {"container": "ttc", "member_count": 2},
            "members": [
                {"member_index": 0, "sha256": "a" * 64, "fstype": 0x0002,
                 "fstype_flags": [0x0002],
                 "fstype_flag_labels": ["preview-print-only"],
                 "fstype_reserved_bits": 0},
                {"member_index": 1, "sha256": "b" * 64, "fstype": 0x0100,
                 "fstype_flags": [0x0100],
                 "fstype_flag_labels": ["bitmap-only"],
                 "fstype_reserved_bits": 0},
            ],
        }

    def test_recompute_covers_all_ttc_members(self):
        ev = fl.normalize_evidence_fstype(self._legacy_evidence())
        m0, m1 = ev["members"]
        self.assertEqual(m0["fstype_flags"], [0x0002])
        self.assertEqual(m0["fstype_flag_labels"], ["restricted"])
        self.assertEqual(m0["fstype_reserved_bits"], 0)
        self.assertEqual(m1["fstype_flags"], [0x0100])
        self.assertEqual(m1["fstype_flag_labels"], ["no-subsetting"])
        self.assertEqual(m1["fstype_reserved_bits"], 0)

    def test_original_evidence_not_mutated(self):
        legacy = self._legacy_evidence()
        fl.normalize_evidence_fstype(legacy)
        self.assertEqual(legacy["members"][0]["fstype_flag_labels"],
                         ["preview-print-only"])
        self.assertEqual(legacy["members"][1]["fstype_flag_labels"],
                         ["bitmap-only"])

    def test_reserved_bits_recomputed_with_corrected_mask(self):
        # 旧掩码 0xFEF1 把 bit9(0x0200) 当保留位；修正版 0x0200=bitmap-only
        ev = {"members": [{"member_index": 0, "fstype": 0x0200,
                           "fstype_flags": [], "fstype_flag_labels": [],
                           "fstype_reserved_bits": 0x0200}]}
        out = fl.normalize_evidence_fstype(ev)["members"][0]
        self.assertEqual(out["fstype_flags"], [0x0200])
        self.assertEqual(out["fstype_flag_labels"], ["bitmap-only"])
        self.assertEqual(out["fstype_reserved_bits"], 0)

    def test_missing_fstype_yields_empty_derived(self):
        ev = {"members": [{"member_index": 0, "fstype": None,
                           "fstype_flags": [0x0002],
                           "fstype_flag_labels": ["preview-print-only"],
                           "fstype_reserved_bits": 7}]}
        out = fl.normalize_evidence_fstype(ev)["members"][0]
        self.assertEqual(out["fstype_flags"], [])
        self.assertEqual(out["fstype_flag_labels"], [])
        self.assertEqual(out["fstype_reserved_bits"], 0)

    def test_empty_or_missing_members_passthrough(self):
        self.assertEqual(fl.normalize_evidence_fstype({}), {})
        ev = {"container": "sfnt", "members": []}
        self.assertEqual(fl.normalize_evidence_fstype(ev), ev)

    def test_other_fields_preserved(self):
        ev = self._legacy_evidence()
        out = fl.normalize_evidence_fstype(ev)
        self.assertEqual(out["container"], "ttc")
        self.assertEqual(out["unpack_summary"], ev["unpack_summary"])
        self.assertEqual(out["members"][0]["sha256"], "a" * 64)
        self.assertEqual(out["members"][0]["member_index"], 0)


if __name__ == "__main__":
    unittest.main()
