"""SQLite 持久化层（标准库 sqlite3）。"""
from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fonts (
    sha256        TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    family        TEXT,
    subfamily     TEXT,
    postscript    TEXT,
    version       TEXT,
    upem          INTEGER,
    num_glyphs    INTEGER,
    sfnt_version  TEXT,
    cmap_count    INTEGER,
    scripts       TEXT,
    license_evidence TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    request_json  TEXT NOT NULL,           -- gzip 压缩的完整请求参数
    font_summaries_json TEXT NOT NULL,     -- gzip：分析时固定的字体摘要快照
    result_json   TEXT NOT NULL            -- gzip：完整整形结果
);

-- 不可变许可策略版本（按使用场景各自线性版本号）
CREATE TABLE IF NOT EXISTS license_policies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario      TEXT NOT NULL,           -- web / desktop / print
    version       INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    policy_json   TEXT NOT NULL,
    UNIQUE(scenario, version)
);

-- 各场景当前生效版本指针（仅一行/scenario），CAS 更新
CREATE TABLE IF NOT EXISTS license_policy_active (
    scenario      TEXT PRIMARY KEY,
    version       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS license_exemptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    revoked_at    TEXT,
    scenario      TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    font_digest   TEXT NOT NULL,
    rule_codes    TEXT NOT NULL,           -- JSON 数组：豁免的检查代码
    justification TEXT NOT NULL,
    requested_by  TEXT NOT NULL,
    expires_at    TEXT,                    -- NULL = 长期（仍受摘要绑定约束）
    applies_to_stack_conflict INTEGER NOT NULL DEFAULT 0,
    revoked       INTEGER NOT NULL DEFAULT 0
);

-- 发布冻结的门禁决定（幂等：request_id / fingerprint 唯一）
CREATE TABLE IF NOT EXISTS license_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    request_id      TEXT,
    fingerprint     TEXT NOT NULL UNIQUE,
    scenario        TEXT NOT NULL,
    policy_version  INTEGER NOT NULL,
    font_stack_json TEXT NOT NULL,
    manifest_json   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_request_id
    ON license_decisions(request_id) WHERE request_id IS NOT NULL;
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧库补齐增量列。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fonts)")}
    if "license_evidence" not in cols:
        conn.execute("ALTER TABLE fonts ADD COLUMN license_evidence TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _gz(obj: Any) -> str:
    return gzip.compress(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        mtime=0,
    ).hex()


def _ungz(hexstr: str) -> Any:
    return json.loads(gzip.decompress(bytes.fromhex(hexstr)).decode("utf-8"))


# ---------------------------------------------------------------- fonts

def insert_font(meta: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO fonts
               (sha256, filename, family, subfamily, postscript, version,
                upem, num_glyphs, sfnt_version, cmap_count, scripts,
                license_evidence, created_at)
               VALUES (:sha256, :filename, :family, :subfamily, :postscript,
                       :version, :upem, :num_glyphs, :sfnt_version,
                       :cmap_count, :scripts, :license_evidence, :created_at)""",
            {**meta, "created_at": _now(),
             "scripts": json.dumps(meta.get("scripts", []), ensure_ascii=False),
             "license_evidence": json.dumps(meta.get("license_evidence") or {},
                                            ensure_ascii=False)},
        )


def _font_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["scripts"] = json.loads(d.pop("scripts") or "[]")
    d["license_evidence"] = json.loads(d.pop("license_evidence") or "{}")
    return d


def get_font(sha256: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM fonts WHERE sha256=?", (sha256,)).fetchone()
    if row is None:
        return None
    return _font_row_to_dict(row)


def list_fonts() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM fonts ORDER BY created_at, family").fetchall()
    return [_font_row_to_dict(r) for r in rows]


def delete_font(sha256: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM fonts WHERE sha256=?", (sha256,))
        return cur.rowcount > 0


# --------------------------------------------------------------- reviews

def insert_review(title: str, request: dict[str, Any],
                  font_summaries: list[dict[str, Any]], result: dict[str, Any]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reviews
               (title, created_at, request_json, font_summaries_json, result_json)
               VALUES (?,?,?,?,?)""",
            (title, _now(), _gz(request), _gz(font_summaries), _gz(result)),
        )
        return int(cur.lastrowid)


def list_reviews() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at FROM reviews ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_review(review_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "request": _ungz(row["request_json"]),
        "font_summaries": _ungz(row["font_summaries_json"]),
        "result": _ungz(row["result_json"]),
    }


def delete_review(review_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM reviews WHERE id=?", (review_id,))
        return cur.rowcount > 0


# ----------------------------------------------------- license gate: policies

class PolicyConflict(Exception):
    """策略 CAS 冲突（版本已被他人推进）。"""


class PolicyExists(Exception):
    """同一 (场景, 版本号) 已存在不可变记录。"""


def register_license_policy(scenario: str, policy: dict[str, Any],
                            registered_by: str, activate: bool,
                            expected_active_version: Optional[int]
                            ) -> dict[str, Any]:
    """登记一个不可变策略版本；activate=True 时用 CAS 切换生效指针。

    expected_active_version 为当前生效版本（无生效版本时必须传 0）。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM license_policies WHERE scenario=?",
            (scenario,)).fetchone()
        next_version = int(row["v"]) + 1
        try:
            conn.execute(
                """INSERT INTO license_policies
                   (scenario, version, created_at, registered_by, policy_json)
                   VALUES (?,?,?,?,?)""",
                (scenario, next_version, _now(), registered_by,
                 json.dumps(policy, ensure_ascii=False)),
            )
        except sqlite3.IntegrityError as exc:
            raise PolicyExists(str(exc)) from exc
    if activate:
        # 独立事务：登记是不可变事实，CAS 失败不应回滚登记
        activate_license_policy(scenario, next_version, expected_active_version)
    return {"scenario": scenario, "version": next_version}


def _activate_policy(conn: sqlite3.Connection, scenario: str,
                     new_version: int, expected: Optional[int]) -> None:
    row = conn.execute(
        "SELECT version FROM license_policy_active WHERE scenario=?",
        (scenario,)).fetchone()
    current = None if row is None else int(row["version"])
    expected_norm = 0 if expected is None else int(expected)
    if (current or 0) != expected_norm:
        raise PolicyConflict(
            f"场景 {scenario} 当前生效版本为 {current or 0}，"
            f"期望基准 {expected_norm}，请基于最新版本重试")
    if current is None:
        conn.execute(
            "INSERT INTO license_policy_active(scenario, version) VALUES(?,?)",
            (scenario, new_version))
    else:
        conn.execute(
            "UPDATE license_policy_active SET version=? WHERE scenario=?",
            (new_version, scenario))


def activate_license_policy(scenario: str, version: int,
                            expected_active_version: Optional[int]) -> None:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM license_policies WHERE scenario=? AND version=?",
            (scenario, version)).fetchone()
        if exists is None:
            raise KeyError(f"策略 {scenario} v{version} 不存在")
        _activate_policy(conn, scenario, version, expected_active_version)


def _policy_from_row(row: sqlite3.Row) -> dict[str, Any]:
    d = {
        "scenario": row["scenario"],
        "version": row["version"],
        "created_at": row["created_at"],
        "registered_by": row["registered_by"],
        "policy": json.loads(row["policy_json"]),
    }
    return d


def list_license_policies(scenario: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.*, a.version AS active_version FROM license_policies p
               LEFT JOIN license_policy_active a ON a.scenario = p.scenario
               WHERE p.scenario=? ORDER BY p.version""",
            (scenario,)).fetchall()
    out = []
    for r in rows:
        d = _policy_from_row(r)
        d["active"] = (r["active_version"] is not None
                       and int(r["active_version"]) == d["version"])
        out.append(d)
    return out


def get_license_policy(scenario: str, version: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM license_policies WHERE scenario=? AND version=?",
            (scenario, version)).fetchone()
    return _policy_from_row(row) if row else None


def get_active_license_policy(scenario: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT p.* FROM license_policy_active a
               JOIN license_policies p
                 ON p.scenario=a.scenario AND p.version=a.version
               WHERE a.scenario=?""",
            (scenario,)).fetchone()
    return _policy_from_row(row) if row else None


def list_scenarios() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scenario FROM license_policies ORDER BY scenario"
        ).fetchall()
    return [r["scenario"] for r in rows]


# -------------------------------------------------- license gate: exemptions

def insert_license_exemption(data: dict[str, Any]) -> int:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM license_policies WHERE scenario=? AND version=?",
            (data["scenario"], data["policy_version"])).fetchone()
        if exists is None:
            raise KeyError(
                f"策略 {data['scenario']} v{data['policy_version']} 不存在，"
                "豁免只能绑定已登记的不可变版本")
        cur = conn.execute(
            """INSERT INTO license_exemptions
               (created_at, scenario, policy_version, font_digest, rule_codes,
                justification, requested_by, expires_at,
                applies_to_stack_conflict)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_now(), data["scenario"], data["policy_version"],
             data["font_digest"],
             json.dumps(data["rule_codes"], ensure_ascii=False),
             data["justification"], data["requested_by"],
             data.get("expires_at"),
             1 if data.get("applies_to_stack_conflict") else 0),
        )
        return int(cur.lastrowid)


def _exemption_from_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["rule_codes"] = json.loads(d["rule_codes"])
    d["applies_to_stack_conflict"] = bool(d["applies_to_stack_conflict"])
    d["revoked"] = bool(d["revoked"])
    return d


def list_license_exemptions(scenario: Optional[str] = None,
                            policy_version: Optional[int] = None,
                            font_digest: Optional[str] = None,
                            include_revoked: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM license_exemptions WHERE 1=1"
    args: list[Any] = []
    if scenario is not None:
        sql += " AND scenario=?"; args.append(scenario)
    if policy_version is not None:
        sql += " AND policy_version=?"; args.append(policy_version)
    if font_digest is not None:
        sql += " AND font_digest=?"; args.append(font_digest)
    if not include_revoked:
        sql += " AND revoked=0"
    sql += " ORDER BY id DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_exemption_from_row(r) for r in rows]


def get_license_exemption(exemption_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM license_exemptions WHERE id=?",
            (exemption_id,)).fetchone()
    return _exemption_from_row(row) if row else None


def revoke_license_exemption(exemption_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE license_exemptions SET revoked=1, revoked_at=?
               WHERE id=? AND revoked=0""",
            (_now(), exemption_id))
        return cur.rowcount > 0


# ------------------------------------------------- license gate: decisions

def insert_license_decision(request_id: Optional[str], fingerprint: str,
                            scenario: str, policy_version: int,
                            font_stack: list[str], manifest: dict[str, Any]
                            ) -> tuple[int, bool]:
    """幂等写入冻结决定。返回 (id, created)。

    fingerprint 唯一；request_id（若非空）也唯一。重复调用返回既有记录。
    """
    with get_conn() as conn:
        if request_id:
            row = conn.execute(
                "SELECT id FROM license_decisions WHERE request_id=?",
                (request_id,)).fetchone()
            if row is not None:
                return int(row["id"]), False
        row = conn.execute(
            "SELECT id FROM license_decisions WHERE fingerprint=?",
            (fingerprint,)).fetchone()
        if row is not None:
            return int(row["id"]), False
        cur = conn.execute(
            """INSERT INTO license_decisions
               (created_at, request_id, fingerprint, scenario, policy_version,
                font_stack_json, manifest_json)
               VALUES (?,?,?,?,?,?,?)""",
            (_now(), request_id, fingerprint, scenario, policy_version,
             json.dumps(font_stack), _gz(manifest)),
        )
        return int(cur.lastrowid), True


def _decision_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "request_id": row["request_id"],
        "fingerprint": row["fingerprint"],
        "scenario": row["scenario"],
        "policy_version": row["policy_version"],
        "font_stack": json.loads(row["font_stack_json"]),
        "manifest": _ungz(row["manifest_json"]),
    }


def get_license_decision(decision_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM license_decisions WHERE id=?",
            (decision_id,)).fetchone()
    return _decision_from_row(row) if row else None


def list_license_decisions(scenario: Optional[str] = None) -> list[dict[str, Any]]:
    sql = ("SELECT id, created_at, request_id, fingerprint, scenario, "
           "policy_version, font_stack_json FROM license_decisions")
    args: list[Any] = []
    if scenario is not None:
        sql += " WHERE scenario=?"; args.append(scenario)
    sql += " ORDER BY id DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["font_stack"] = json.loads(d.pop("font_stack_json"))
        out.append(d)
    return out
