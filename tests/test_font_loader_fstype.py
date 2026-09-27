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

import copy
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
    """读取存证时以数值 fsType 为准重算派生字段（升级前错标回归保护）。"""

    @staticmethod
    def _legacy_derivation(value):
        """模拟升级前旧位序：把 4 位类型枚举值错按独立位位置解读。"""
        legacy = {0x0001: "restricted", 0x0002: "preview-print-only",
                  0x0004: "editable", 0x0008: "no-subsetting",
                  0x0100: "bitmap-only"}
        flags = [b for b in legacy if value & b]
        return flags, [legacy[b] for b in flags]

    def _stale_member(self, index, fstype, sha="a" * 64):
        flags, labels = self._legacy_derivation(fstype)
        return {
            "member_index": index,
            "sha256": sha,
            "sfnt_size": 100,
            "family": "Font",
            "fstype": fstype,
            "fstype_flags": flags,
            "fstype_flag_labels": labels,
            # 旧保留位掩码 0xFEF0：bit0/bit8 被当作标志位，未计入保留位
            "fstype_reserved_bits": fstype & 0xFEF0,
            "name_license_description": "License",
            "name_license_url": "https://example.com/x",
        }

    def _evidence(self, members):
        return {"container": "ttc" if len(members) > 1 else "sfnt",
                "container_size": 100,
                "members_complete": True,
                "unpack_summary": {"container": "sfnt"},
                "members": members}

    def test_restricted_0x0002_stale_preview_label_corrected(self):
        # 核心回归：存量 0x0002 被旧位序记为 preview-print-only
        ev = self._evidence([self._stale_member(0, 0x0002)])
        self.assertEqual(ev["members"][0]["fstype_flag_labels"],
                         ["preview-print-only"])
        out = fl.normalize_evidence_fstype(ev)
        m = out["members"][0]
        self.assertEqual(m["fstype"], 0x0002)
        self.assertEqual(m["fstype_flags"], [0x0002])
        self.assertEqual(m["fstype_flag_labels"], ["restricted"])
        self.assertEqual(m["fstype_reserved_bits"], 0)

    def test_all_shifted_labels_corrected(self):
        cases = [
            (0x0001, [], 0x0001),                    # 保留值，旧标 restricted
            (0x0004, ["preview-print-only"], 0),     # 旧标 editable
            (0x0008, ["editable"], 0),               # 旧标 no-subsetting
            (0x0100, ["no-subsetting"], 0),          # 旧标 bitmap-only
            (0x0200, ["bitmap-only"], 0),            # 旧掩码不含此位
            (0x0108, ["editable", "no-subsetting"], 0),
        ]
        for fstype, want_labels, want_reserved in cases:
            ev = self._evidence([self._stale_member(0, fstype)])
            m = fl.normalize_evidence_fstype(ev)["members"][0]
            self.assertEqual(m["fstype_flag_labels"], want_labels,
                             hex(fstype))
            self.assertEqual(m["fstype_reserved_bits"], want_reserved,
                             hex(fstype))

    def test_ttc_all_members_covered(self):
        ev = self._evidence([
            self._stale_member(0, 0x0002, sha="a" * 64),
            self._stale_member(1, 0x0100, sha="b" * 64),
            self._stale_member(2, 0x0008, sha="c" * 64),
        ])
        out = fl.normalize_evidence_fstype(ev)
        labels = [m["fstype_flag_labels"] for m in out["members"]]
        self.assertEqual(labels, [["restricted"], ["no-subsetting"],
                                  ["editable"]])

    def test_original_evidence_not_mutated(self):
        ev = self._evidence([self._stale_member(0, 0x0002)])
        snapshot = copy.deepcopy(ev)
        fl.normalize_evidence_fstype(ev)
        self.assertEqual(ev, snapshot)  # 原始存证保持旧标签原貌

    def test_missing_os2_clears_derived_fields(self):
        m = self._stale_member(0, 0x0002)
        m["fstype"] = None  # 缺 OS/2 表却残留派生字段
        out = fl.normalize_evidence_fstype(self._evidence([m]))
        m0 = out["members"][0]
        self.assertEqual(m0["fstype_flags"], [])
        self.assertEqual(m0["fstype_flag_labels"], [])
        self.assertEqual(m0["fstype_reserved_bits"], 0)

    def test_fresh_evidence_idempotent(self):
        ev = self._evidence([self._stale_member(0, 0x0002)])
        once = fl.normalize_evidence_fstype(ev)
        twice = fl.normalize_evidence_fstype(once)
        self.assertEqual(once, twice)

    def test_empty_and_memberless_evidence(self):
        self.assertEqual(fl.normalize_evidence_fstype({}), {})
        ev = {"container": "sfnt", "members": []}
        self.assertEqual(fl.normalize_evidence_fstype(ev), ev)

    def test_other_fields_preserved(self):
        ev = self._evidence([self._stale_member(0, 0x0002)])
        ev["reconstructed"] = True
        out = fl.normalize_evidence_fstype(ev)
        m = out["members"][0]
        self.assertTrue(out["reconstructed"])
        self.assertEqual(m["sha256"], "a" * 64)
        self.assertEqual(m["name_license_url"], "https://example.com/x")
        self.assertEqual(out["unpack_summary"], {"container": "sfnt"})


if __name__ == "__main__":
    unittest.main()
