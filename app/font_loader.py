"""字体加载、摘要计算与元数据提取（fontTools + uharfbuzz）。"""
from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import regex
import uharfbuzz as hb
from fontTools.ttLib import TTCollection, TTFont

FONT_DIR = Path(__file__).resolve().parent.parent / "data" / "fonts"

# 常见书写系统 -> 基本字符范围（用于字体支持脚本的粗略摘要）
_SCRIPT_SAMPLES: dict[str, range] = {
    "latn": range(0x0041, 0x005B),
    "cyrl": range(0x0410, 0x0430),
    "grek": range(0x0391, 0x03B0),
    "hebr": range(0x05D0, 0x05EB),
    "arab": range(0x0621, 0x063B),
    "deva": range(0x0904, 0x093A),
    "beng": range(0x0985, 0x09B8),
    "hani": range(0x4E00, 0x4E80),
    "hira": range(0x3042, 0x307E),
    "kana": range(0x30AB, 0x30ED),
    "hang": range(0xAC00, 0xAC80),
    "thai": range(0x0E01, 0x0E2E),
    "ethi": range(0x1200, 0x1248),
    "nkoo": range(0x07C0, 0x07EA),
    "emoji": range(0x1F600, 0x1F650),
}


def _decode_sfnt(raw: bytes) -> tuple[bytes, str, str]:
    """返回 (sfnt 字节, sfnt 标识, 容器格式)。支持 woff / woff2。"""
    sig = raw[:4]
    if sig in (b"OTTO", b"\x00\x01\x00\x00", b"true", b"typ1"):
        tag = {b"OTTO": "OTTO (CFF)", b"true": "true (TrueType)",
               b"typ1": "typ1 (TrueType)"}.get(sig, "1.0 (TrueType)")
        return raw, tag, "sfnt"
    if sig == b"wOF2":
        ft = TTFont(io.BytesIO(raw))  # fontTools + Brotli 自动解码
        ft.flavor = None              # 输出解包后的 sfnt
        buf = io.BytesIO()
        ft.save(buf)
        data = buf.getvalue()
        return data, _sfnt_tag(data), "woff2"
    if sig == b"wOFF":
        ft = TTFont(io.BytesIO(raw))
        ft.flavor = None
        buf = io.BytesIO()
        ft.save(buf)
        data = buf.getvalue()
        return data, _sfnt_tag(data), "woff"
    if sig == b"ttcf":
        ft = TTFont(io.BytesIO(raw), fontNumber=0)
        ft.flavor = None
        buf = io.BytesIO()
        ft.save(buf)
        data = buf.getvalue()
        return data, _sfnt_tag(data), "ttc"
    raise ValueError("无法识别的字体文件格式（需要 sfnt/woff/woff2/ttc）")


def _sfnt_tag(data: bytes) -> str:
    sig = data[:4]
    return {b"OTTO": "OTTO (CFF)", b"\x00\x01\x00\x00": "1.0 (TrueType)",
            b"true": "true (TrueType)", b"typ1": "typ1 (TrueType)"}.get(
        sig, sig.hex(" "))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 许可证据
# OS/2 fsType 位定义（TrueType / OS/2 规范的 fsType embedding types）。
# 注意：低 4 位（0x0001/0x0002/0x0004/0x0008）不是独立可叠加的“位”，而是一个
# 4 位枚举类型字段（bit0 保留，必须为 0），合法类型值仅 0/2/4/8，互斥；
# bit8(no-subsetting)/bit9(bitmap-only) 才是可叠加的附加位。
FSTYPE_INSTALLABLE = 0x0000
FSTYPE_RESTRICTED = 0x0002   # Restricted License embedding（受限许可，禁止嵌入）
FSTYPE_PREVIEW = 0x0004      # Preview & Print embedding（临时安装，不可编辑）
FSTYPE_EDITABLE = 0x0008     # Editable embedding（可永久安装、可编辑）
FSTYPE_NO_SUBSETTING = 0x0100  # bit8：禁止子集化/获取
FSTYPE_BITMAP_ONLY = 0x0200    # bit9：仅允许位图栅格嵌入
# bit0、bit4–7、bit10–15 保留，必须为零
FSTYPE_RESERVED_MASK = 0xFCF1
# 4 位嵌入权限类型字段（合法值 0/2/4/8，互斥；0x0001 保留）
FSTYPE_EMBEDDING_MASK = 0x000F

FSTYPE_FLAG_LABELS = {
    FSTYPE_RESTRICTED: "restricted",
    FSTYPE_PREVIEW: "preview-print-only",
    FSTYPE_EDITABLE: "editable",
    FSTYPE_NO_SUBSETTING: "no-subsetting",
    FSTYPE_BITMAP_ONLY: "bitmap-only",
}

# name 表许可字段 nameID
NAME_LICENSE_DESCRIPTION = 13
NAME_LICENSE_URL = 14


def fstype_flags(value: int) -> list[int]:
    """返回 fsType 中置位的已知位（升序）。"""
    return [bit for bit in FSTYPE_FLAG_LABELS if value & bit]


def fstype_reserved_bits(value: int) -> int:
    return value & FSTYPE_RESERVED_MASK


def recompute_member_fstype(member: dict[str, Any]) -> dict[str, Any]:
    """以数值 fstype 为准重算成员的派生字段，返回新 dict（不改原对象）。

    升级前登记的存证仍按旧位序记录派生字段：0x0002 被误标为
    preview-print-only、0x0100 被误标为 bitmap-only，保留位也按旧掩码
    0xFEF1 计算。读取存证时一律以数值 fsType 按修正版位定义重算
    fstype_flags / fstype_flag_labels / fstype_reserved_bits 三个派生字段。
    """
    m = dict(member)
    fs_type = m.get("fstype")
    if not isinstance(fs_type, int) or isinstance(fs_type, bool):
        # 无数值 fsType（如缺 OS/2 表）：派生字段无从谈起，置空
        m["fstype_flags"] = []
        m["fstype_flag_labels"] = []
        m["fstype_reserved_bits"] = 0
        return m
    flags = fstype_flags(fs_type)
    m["fstype_flags"] = flags
    m["fstype_flag_labels"] = [FSTYPE_FLAG_LABELS[b] for b in flags]
    m["fstype_reserved_bits"] = fstype_reserved_bits(fs_type)
    return m


def normalize_evidence_fstype(evidence: dict[str, Any]) -> dict[str, Any]:
    """在副本中重算全部成员（含 TTC 集合成员）的 fsType 派生字段。

    用于评估、发布与许可摘要的存证读取路径，使逐条证据、新冻结清单与
    页面呈现统一以数值 fsType 为准；数据库中的原始存证与历史冻结决定
    保持原貌，绝不改写。
    """
    if not isinstance(evidence, dict):
        return evidence
    members = evidence.get("members")
    if not members:
        return evidence
    out = dict(evidence)
    out["members"] = [recompute_member_fstype(m) for m in members]
    return out


def _name_record_unicode(ft: TTFont, name_id: int) -> str:
    """按 Windows English(0x409) → Mac Roman → 任意可解码的优先级取 name 字符串。"""
    if "name" not in ft:
        return ""
    candidates = []
    for record in ft["name"].names:
        if record.nameID != name_id:
            continue
        try:
            text = record.toUnicode()
        except Exception:
            continue
        if text is None:
            continue
        candidates.append(((record.platformID, record.langID), text.strip()))
    if not candidates:
        return ""
    for pref in ((3, 0x409), (3, 0x0C04), (1, 0)):
        for key, text in candidates:
            if key == pref and text:
                return text
    # platform 3 任意语言 → 任意非空
    for key, text in candidates:
        if key[0] == 3 and text:
            return text
    return candidates[0][1]


def _license_member_info(ft: TTFont, sfnt: bytes, index: int) -> dict[str, Any]:
    """单个 sfnt 成员的许可证据。"""
    fs_type = int(ft["OS/2"].fsType) if "OS/2" in ft else None
    flags = fstype_flags(fs_type) if fs_type is not None else []
    reserved = fstype_reserved_bits(fs_type) if fs_type is not None else 0
    license_desc = _name_record_unicode(ft, NAME_LICENSE_DESCRIPTION)
    license_url = _name_record_unicode(ft, NAME_LICENSE_URL)
    family = _name_record_unicode(ft, 1) or "(unknown family)"
    return {
        "member_index": index,
        "sha256": sha256_bytes(sfnt),
        "sfnt_size": len(sfnt),
        "family": family,
        "fstype": fs_type,
        "fstype_flags": flags,
        "fstype_flag_labels": [FSTYPE_FLAG_LABELS[b] for b in flags],
        "fstype_reserved_bits": reserved,
        "name_license_description": license_desc,
        "name_license_url": license_url,
    }


def _ft_to_sfnt(ft: TTFont) -> bytes:
    ft.flavor = None
    buf = io.BytesIO()
    ft.save(buf)
    return buf.getvalue()


_SFNT_SIGNATURES = (b"OTTO", b"\x00\x01\x00\x00", b"true", b"typ1")


def unpack_container(raw: bytes) -> tuple[bytes, str, str, dict[str, Any]]:
    """只解码一次容器，返回 (member0 sfnt, sfnt 标识, 容器, 许可证据)。

    woff2 经 fontTools+brotli 解包后再次编译并非逐字节稳定，因此字体身份
    摘要与许可证据必须共用同一次解包得到的 sfnt 字节，不能各自重新解码。
    """
    sig = raw[:4]

    if sig == b"ttcf":
        collection = TTCollection(io.BytesIO(raw))
        members: list[dict[str, Any]] = []
        sfnt0 = b""
        for i, ft in enumerate(collection.fonts):
            sfnt = _ft_to_sfnt(ft)
            if i == 0:
                sfnt0 = sfnt
            members.append(_license_member_info(ft, sfnt, i))
        evidence = {
            "container": "ttc",
            "container_size": len(raw),
            "members_complete": True,
            "unpack_summary": {
                "container": "ttc",
                "member_count": len(members),
                "selected_member": 0,
                "sha256": members[0]["sha256"] if members else sha256_bytes(sfnt0),
                "sfnt_size": members[0]["sfnt_size"] if members else len(sfnt0),
            },
            "members": members,
        }
        return sfnt0, _sfnt_tag(sfnt0), "ttc", evidence

    if sig in (b"wOF2", b"wOFF"):
        ft = TTFont(io.BytesIO(raw))
        sfnt = _ft_to_sfnt(ft)
        kind = "woff2" if sig == b"wOF2" else "woff"
        info = _license_member_info(ft, sfnt, 0)
        evidence = {
            "container": kind,
            "container_size": len(raw),
            "members_complete": True,
            "unpack_summary": {
                "container": kind,
                "original_size": len(raw),
                "unpacked_size": len(sfnt),
                "sha256": info["sha256"],
            },
            "members": [info],
        }
        return sfnt, _sfnt_tag(sfnt), kind, evidence

    if sig in _SFNT_SIGNATURES:
        ft = TTFont(io.BytesIO(raw))
        info = _license_member_info(ft, raw, 0)
        evidence = {
            "container": "sfnt",
            "container_size": len(raw),
            "members_complete": True,
            "unpack_summary": {
                "container": "sfnt",
                "sha256": info["sha256"],
                "sfnt_size": len(raw),
            },
            "members": [info],
        }
        return raw, _sfnt_tag(raw), "sfnt", evidence

    raise ValueError("无法识别的字体文件格式（需要 sfnt/woff/woff2/ttc）")


def extract_license_evidence(raw: bytes) -> dict[str, Any]:
    """完整解析上传容器，提取许可与嵌入证据（与字体身份绑定，逐字节确定）。"""
    return unpack_container(raw)[3]


def license_evidence_from_sfnt(sfnt: bytes, digest: str) -> dict[str, Any]:
    """从磁盘上的解包 sfnt 重建许可证据（历史字体 / 原始 TTC 已不可得时）。

    单 sfnt 的成员证据本身完整（fsType 与 name 字段均可读）；仅容器来源
    不可考时以 reconstructed 标注，不能伪造 TTC 全成员枚举结果。
    """
    ft = TTFont(io.BytesIO(sfnt))
    info = _license_member_info(ft, sfnt, 0)
    if info["sha256"] != digest:
        # 磁盘内容与寻址摘要不一致 —— 证据如实标注，由门禁拒绝
        info["actual_sha256"] = info["sha256"]
        info["sha256"] = digest
    return {
        "container": "sfnt",
        "container_size": len(sfnt),
        "members_complete": True,
        "reconstructed": True,
        "unpack_summary": {
            "container": "sfnt",
            "sha256": digest,
            "sfnt_size": len(sfnt),
            "reconstructed": True,
        },
        "members": [info],
    }


@dataclass
class LoadedFont:
    digest: str
    raw: bytes
    hb_font: hb.Font
    hb_face: hb.Face
    ft: TTFont
    cmap: dict[int, str]
    # format-14 UVS: {(base_cp, variation_selector): glyph_name_or_empty}
    uvs: dict[tuple[int, int], str]
    glyph_order: list[str]
    upem: int
    family: str
    subfamily: str
    postscript: str
    version: str
    num_glyphs: int
    sfnt_version: str
    container: str
    license_evidence: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def glyph_name(self, gid: int) -> str:
        if 0 <= gid < len(self.glyph_order):
            return self.glyph_order[gid]
        return f"<gid{gid}>"

    def variation_glyph_id(self, base_cp: int, vs: int) -> Optional[int]:
        """检查 format-14 cmap 是否定义了 (base, VS) 变体序列。"""
        if (base_cp, vs) not in self.uvs:
            return None
        name = self.uvs[(base_cp, vs)]
        if name:  # 非默认 UVS：映射到专用字形
            try:
                return self.glyph_order.index(name)
            except ValueError:
                return None
        # 默认 UVS：使用基字符字形
        gname = self.cmap.get(base_cp)
        if gname is None:
            return None
        try:
            return self.glyph_order.index(gname)
        except ValueError:
            return None


def _extract_uvs(ft: TTFont) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    for table in ft["cmap"].tables:
        if table.format == 14:
            for vs, entries in table.uvsDict.items():
                for base, gname in entries:
                    out[(base, vs)] = gname or ""
    return out


def load_font_from_bytes(raw: bytes) -> tuple[LoadedFont, str]:
    """返回 (LoadedFont, 原始文件名用容器扩展)。"""
    # 单次解码：身份 sfnt、容器类型与许可证据共用同一字节结果
    # （woff2 重新解包并非逐字节稳定，不可解码两次）
    try:
        sfnt, sfnt_tag, container, license_evidence = unpack_container(raw)
    except Exception:
        sfnt, sfnt_tag, container = _decode_sfnt(raw)
        license_evidence = {}
    digest = sha256_bytes(sfnt)
    path = FONT_DIR / f"{digest}.otf"
    if not path.exists():
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sfnt)

    ft = TTFont(io.BytesIO(sfnt))
    names = {}
    for record in ft["name"].names:
        if record.platformID == 3 and record.nameID in (1, 2, 6, 5):
            try:
                names[record.nameID] = record.toUnicode()
            except Exception:
                pass

    hb_face = hb.Face(sfnt)
    hb_font = hb.Font(hb_face)

    if not license_evidence:
        license_evidence = license_evidence_from_sfnt(sfnt, digest)

    lf = LoadedFont(
        digest=digest,
        raw=sfnt,
        hb_font=hb_font,
        hb_face=hb_face,
        ft=ft,
        cmap=ft.getBestCmap() or {},
        uvs=_extract_uvs(ft),
        glyph_order=ft.getGlyphOrder(),
        upem=ft["head"].unitsPerEm,
        family=names.get(1, "(unknown family)"),
        subfamily=names.get(2, ""),
        postscript=names.get(6, ""),
        version=(names.get(5, "") or "").strip(),
        num_glyphs=ft["maxp"].numGlyphs,
        sfnt_version=sfnt_tag,
        container=container,
        license_evidence=license_evidence,
    )
    ext = {"sfnt": ".otf", "woff": ".woff", "woff2": ".woff2",
           "ttc": ".ttc"}[container]
    return lf, ext


def summarize_scripts(lf: LoadedFont) -> list[str]:
    """返回字体 cmap 覆盖比例达到阈值的脚本标签。"""
    found = []
    cmap = lf.cmap
    for tag, sample_range in _SCRIPT_SAMPLES.items():
        cps = list(sample_range)
        hit = sum(1 for cp in cps if cp in cmap)
        if hit / len(cps) >= 0.6:
            found.append(tag)
    # 组合附加符 / 变体选择符覆盖提示
    if any(0x0300 <= cp <= 0x036F for cp in cmap):
        found.append("combining-marks")
    if lf.uvs:
        found.append("variation-sequences")
    return found


# 进程内字体缓存（hb.Font 不是线程安全可重入的，每个字体自带锁）
_cache: dict[str, LoadedFont] = {}
_cache_lock = threading.Lock()


def register_font(raw: bytes) -> LoadedFont:
    lf, _ext = load_font_from_bytes(raw)
    with _cache_lock:
        _cache[lf.digest] = lf
    return lf


def get_loaded_font(digest: str) -> Optional[LoadedFont]:
    with _cache_lock:
        if digest in _cache:
            return _cache[digest]
    path = FONT_DIR / f"{digest}.otf"
    if not path.exists():
        return None
    lf, _ext = load_font_from_bytes(path.read_bytes())
    with _cache_lock:
        _cache.setdefault(digest, lf)
    return lf


def font_metadata(lf: LoadedFont, original_filename: str) -> dict:
    return {
        "sha256": lf.digest,
        "filename": original_filename,
        "family": lf.family,
        "subfamily": lf.subfamily,
        "postscript": lf.postscript,
        "version": lf.version,
        "upem": lf.upem,
        "num_glyphs": lf.num_glyphs,
        "sfnt_version": lf.sfnt_version,
        "container": lf.container,
        "cmap_count": len(lf.cmap),
        "scripts": summarize_scripts(lf),
        "license_evidence": lf.license_evidence,
    }


def is_private_use(cp: int) -> bool:
    return (0xE000 <= cp <= 0xF8FF
            or 0xF0000 <= cp <= 0xFFFFD
            or 0x100000 <= cp <= 0x10FFFD)


_VS_RANGE = [(0xFE00, 0xFE0F), (0xE0100, 0xE01EF)]


def is_variation_selector(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _VS_RANGE)


def find_vs_in_cluster(cluster: str) -> Optional[tuple[int, int]]:
    """返回 (基字符索引, VS 字符索引)；簇中可能有 emoji 序列等多个字符，
    按 UAX #44，VS 作用于其前一个基字符。"""
    for i, ch in enumerate(cluster):
        if is_variation_selector(ord(ch)) and i > 0:
            return (i - 1, i)
    return None


_ZWJ = 0x200D
_ZWNJ = 0x200C


def cluster_codepoints(cluster: str) -> list[int]:
    return [ord(c) for c in cluster]


def cluster_has_join_control(cluster: str) -> bool:
    return _ZWJ in cluster_codepoints(cluster) or _ZWNJ in cluster_codepoints(cluster)


# 防止 regex 编译重复
_GRAPHEME_RE = regex.compile(r"\X")


def grapheme_clusters(text: str) -> list[str]:
    """按 Unicode 字素簇（grapheme cluster）切分。"""
    return _GRAPHEME_RE.findall(text)
