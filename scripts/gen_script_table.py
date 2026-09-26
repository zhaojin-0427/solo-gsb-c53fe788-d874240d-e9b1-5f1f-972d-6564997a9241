#!/usr/bin/env python3
"""从 Unicode UCD 生成 app/script_data.py。

生成内容：
* 码点 -> ISO 15924 四字母脚本标签（Scripts.txt + PropertyValueAliases.txt）
* Default_Ignorable_Code_Point 码位集合（DerivedCoreProperties.txt）

用法:
    python3 scripts/gen_script_table.py

数据文件可从 https://www.unicode.org/Public/UCD/latest/ucd/ 下载，
默认与本脚本放在同一目录。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent


def parse_aliases(path: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split("#", 1)[0].split(";")]
        if len(parts) >= 3 and parts[0] == "sc":
            aliases[parts[2]] = parts[1]
    return aliases


def parse_ranges(path: Path) -> list[tuple[int, int, str]]:
    """解析 UCD 分号格式: '<码位或区间> ; <值>'。"""
    out: list[tuple[int, int, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = [p.strip() for p in line.split(";")]
        field = fields[0]
        value = fields[1] if len(fields) > 1 else ""
        if ".." in field:
            s, e = field.split("..")
        else:
            s = e = field
        out.append((int(s, 16), int(e, 16), value))
    return out


def merge_same_value(ranges):
    ranges = sorted(ranges)
    merged = []
    for s, e, v in ranges:
        if merged and s == merged[-1][1] + 1 and v == merged[-1][2]:
            merged[-1] = (merged[-1][0], e, v)
        else:
            merged.append((s, e, v))
    return merged


def merge_plain(ranges):
    ranges = sorted(ranges)
    merged = []
    for s, e in ranges:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def main() -> None:
    alias = parse_aliases(HERE / "PropertyValueAliases.txt")
    script_ranges = []
    for s, e, name in parse_ranges(HERE / "Scripts.txt"):
        tag = alias.get(name)
        if tag is None:
            raise SystemExit(f"缺少脚本别名: {name}")
        script_ranges.append((s, e, tag))
    script_ranges = merge_same_value(script_ranges)

    di_ranges = merge_plain([
        (s, e) for s, e, v in parse_ranges(HERE / "DerivedCoreProperties.txt")
        if v == "Default_Ignorable_Code_Point"
    ])

    scripts = sorted({t for _, _, t in script_ranges})
    idx = {t: i for i, t in enumerate(scripts)}

    parts = [
        '"""由 Unicode UCD 自动生成的码点属性表。',
        "数据来源: https://www.unicode.org/Public/UCD/latest/ucd/",
        "  - Scripts.txt / PropertyValueAliases.txt（Script 属性）",
        "  - DerivedCoreProperties.txt（Default_Ignorable_Code_Point）",
        "Unicode, Inc.，依据 Unicode 使用条款发布。",
        '请勿手工编辑；运行 scripts/gen_script_table.py 重新生成。"""',
        "",
        f"# {len(scripts)} 个脚本，{len(script_ranges)} 个码位区间",
        "SCRIPTS = " + repr(tuple(scripts)),
        "",
        "SCRIPT_RANGES = (",
    ]
    for s, e, tag in script_ranges:
        parts.append(f"    (0x{s:04X}, 0x{e:04X}, {idx[tag]}),")
    parts.append(")")
    parts.append("")
    parts.append(f"# Default_Ignorable_Code_Point：{len(di_ranges)} 个区间")
    parts.append("DEFAULT_IGNORABLE_RANGES = (")
    for s, e in di_ranges:
        parts.append(f"    (0x{s:04X}, 0x{e:04X}),")
    parts.append(")")
    parts.append("")
    parts.append('''
def script_of(cp: int) -> str:
    """返回 ISO 15924 四字母脚本标签；未列出的码点返回 Zzzz。"""
    lo, hi = 0, len(SCRIPT_RANGES) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        s, e, i = SCRIPT_RANGES[mid]
        if cp < s:
            hi = mid - 1
        elif cp > e:
            lo = mid + 1
        else:
            return SCRIPTS[i]
    return "Zzzz"


def is_default_ignorable(cp: int) -> bool:
    """UCD DerivedCoreProperties 中的 Default_Ignorable_Code_Point。

    这些码点按 UAX #29/UBA 不应产生可见字形，字体没有显式字形时不算缺字。
    """
    lo, hi = 0, len(DEFAULT_IGNORABLE_RANGES) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        s, e = DEFAULT_IGNORABLE_RANGES[mid]
        if cp < s:
            hi = mid - 1
        elif cp > e:
            lo = mid + 1
        else:
            return True
    return False
''')

    out = ROOT / "app" / "script_data.py"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {out} ({len(scripts)} scripts, {len(script_ranges)} ranges, "
          f"{len(di_ranges)} default-ignorable ranges, "
          f"{out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
