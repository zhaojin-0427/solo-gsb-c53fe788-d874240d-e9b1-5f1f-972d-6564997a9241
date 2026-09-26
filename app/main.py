"""字体回退与字形审查台 — FastAPI 入口。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analyzer, database, license_gate
from . import font_loader
from .font_loader import (
    get_loaded_font,
    register_font,
    font_metadata,
    sha256_bytes,
)
from .models import (
    AnalyzeRequest,
    ExemptionCreateRequest,
    GateEvaluateRequest,
    GatePublishRequest,
    PolicyActivateRequest,
    PolicyCreateRequest,
)

app = FastAPI(title="字体回退与字形审查台", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"

ALLOWED_EXT = {".otf", ".ttf", ".woff", ".woff2", ".ttc"}
MAX_UPLOAD = 50 * 1024 * 1024


@app.on_event("startup")
def _startup() -> None:
    database.init_db()
    _seed_default_policies()


def _seed_default_policies() -> None:
    """保留 v1 历史基线的不可变登记事实，并追加/激活 fsType 修正版策略（幂等）。

    已登记策略不可原地改写：启动时若场景尚无任何版本，则先登记 v1 历史基线
    （保持平台初始档案可复核），再以 CAS 基准 1 追加并激活修正版 v2；
    若场景已有 v1 但尚无修正版，则按当前生效版本为 CAS 基准追加 v2 并切换。
    逐条证据、冻结清单与幂等发布此后统一引用同一修正版；历史冻结决定不重算。
    """
    legacy = license_gate.legacy_default_policies_v1()
    corrected = license_gate.default_policies()
    for scenario, v1_policy in legacy.items():
        versions = database.list_license_policies(scenario)
        if not versions:
            # 全新部署：先留 v1 登记事实，再追加激活修正版 v2
            database.register_license_policy(
                scenario=scenario, policy=v1_policy,
                registered_by="system-bootstrap",
                activate=True, expected_active_version=0)
            database.register_license_policy(
                scenario=scenario, policy=corrected[scenario],
                registered_by=license_gate.CORRECTED_POLICY_REGISTERED_BY,
                activate=True, expected_active_version=1)
            continue
        already_fixed = any(
            v["policy"].get(license_gate.POLICY_REVISION_KEY)
            == license_gate.POLICY_REVISION_CORRECTED
            for v in versions)
        if already_fixed:
            continue
        # 既有部署（仅含修正前旧策略）：追加修正版并按当前生效版本 CAS 切换
        active = database.get_active_license_policy(scenario)
        expected = active["version"] if active else None
        database.register_license_policy(
            scenario=scenario, policy=corrected[scenario],
            registered_by=license_gate.CORRECTED_POLICY_REGISTERED_BY,
            activate=active is not None,
            expected_active_version=expected or 0)


# ---------------------------------------------------------------- 字体管理

@app.post("/api/fonts")
async def upload_font(file: UploadFile) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的扩展名 {suffix or '(无)'}，"
                                 f"允许: {sorted(ALLOWED_EXT)}")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "字体文件过大（上限 50MB）")
    if not raw:
        raise HTTPException(400, "空文件")
    try:
        lf = register_font(raw)
    except Exception as exc:  # fontTools 对损坏文件会抛各种异常
        raise HTTPException(400, f"字体解析失败: {exc}") from exc
    original = f"{Path(file.filename).stem}{suffix}"
    meta = font_metadata(lf, original)
    database.insert_font(meta)
    return meta


@app.get("/api/fonts")
def api_list_fonts() -> list[dict[str, Any]]:
    return database.list_fonts()


@app.get("/api/fonts/{digest}")
def api_get_font(digest: str) -> dict[str, Any]:
    meta = database.get_font(digest)
    if meta is None:
        raise HTTPException(404, "字体不存在")
    return meta


@app.get("/api/fonts/{digest}/file")
def api_font_file(digest: str):
    lf = get_loaded_font(digest)
    if lf is None:
        raise HTTPException(404, "字体文件不存在")
    meta = database.get_font(digest)
    name = (meta or {}).get("filename") or f"{digest}.otf"
    return Response(content=lf.raw, media_type="font/otf",
                    headers={"Content-Disposition":
                             f'inline; filename="{Path(name).stem}.otf"'})


@app.get("/api/fonts/{digest}/glyph/{gid}.svg")
def api_glyph_svg(digest: str, gid: int):
    """渲染单个字形轮廓为 SVG（viewBox 为 font 坐标系，Y 向上）。"""
    lf = get_loaded_font(digest)
    if lf is None:
        raise HTTPException(404, "字体文件不存在")
    if not 0 <= gid < lf.num_glyphs:
        raise HTTPException(404, "字形编号超出范围")
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.svgPathPen import SVGPathPen

    glyph_set = lf.ft.getGlyphSet()
    name = lf.glyph_name(gid)
    svg_pen = SVGPathPen(glyph_set)
    glyph_set[name].draw(svg_pen)
    d = svg_pen.getCommands()

    bp = BoundsPen(glyph_set)
    glyph_set[name].draw(bp)
    if bp.bounds is not None:
        xmin, ymin, xmax, ymax = bp.bounds
    else:  # 空格等无轮廓字形
        adv = lf.hb_font.get_glyph_h_advance(gid)
        xmin, ymin, xmax, ymax = 0, 0, adv, 0

    pad = max((xmax - xmin), (ymax - ymin)) * 0.08 + 10
    vx, vy = xmin - pad, ymin - pad
    vw, vh = (xmax - xmin) + 2 * pad, (ymax - ymin) + 2 * pad
    upem = lf.upem
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx:.1f} {vy:.1f} '
        f'{vw:.1f} {vh:.1f}" width="100%" height="100%">'
        f'<rect x="{-upem}" y="0" width="{3*upem}" height="0.04" fill="#c0392b"/>'
        f'<path d="{d}" fill="currentColor"/></svg>'
    )
    return Response(content=svg.encode("utf-8"), media_type="image/svg+xml")


@app.delete("/api/fonts/{digest}")
def api_delete_font(digest: str) -> dict[str, str]:
    # 磁盘文件保留：审查记录依赖固定字体文件才可复核
    if database.delete_font(digest):
        return {"status": "deleted"}
    raise HTTPException(404, "字体不存在")


# ---------------------------------------------------------------- 分析

def _load_stack(digests: list[str]):
    stack = []
    for d in digests:
        lf = get_loaded_font(d)
        if lf is None:
            raise HTTPException(404, f"字体 {d} 不存在或文件已丢失，请先上传")
        stack.append(lf)
    return stack


def _summaries(stack, digests: list[str]) -> list[dict[str, Any]]:
    out = []
    for i, lf in enumerate(stack):
        meta = database.get_font(lf.digest) or {}
        out.append({
            "position": i,
            "sha256": lf.digest,
            "filename": meta.get("filename", f"{lf.digest}.otf"),
            "family": lf.family,
            "subfamily": lf.subfamily,
            "postscript": lf.postscript,
            "version": lf.version,
            "upem": lf.upem,
            "num_glyphs": lf.num_glyphs,
            "sfnt_version": lf.sfnt_version,
            "cmap_count": len(lf.cmap),
            "uvs_entry_count": len(lf.uvs),
            "container": lf.container,
        })
    return out


@app.post("/api/analyze")
def api_analyze(req: AnalyzeRequest, save: bool = False,
                title: str = "") -> dict[str, Any]:
    if len(req.font_stack) != len(set(req.font_stack)):
        raise HTTPException(400, "字体栈中存在重复字体")
    stack = _load_stack(req.font_stack)
    toggles = [(f.tag.strip(), f.value == "on") for f in req.features]
    result = analyzer.analyze(
        text=req.text,
        stack=stack,
        language=req.language,
        script_override=req.script,
        direction=req.direction,
        feature_toggles=toggles,
        norm=req.normalize,
    )
    payload = {
        "request": req.model_dump(),
        "font_summaries": _summaries(stack, req.font_stack),
        "result": result,
    }
    if save:
        rid = database.insert_review(
            title or req.text[:40] or "审查记录",
            req.model_dump(),
            payload["font_summaries"],
            result,
        )
        payload["review_id"] = rid
    return payload


# ---------------------------------------------------------------- 审查记录

@app.get("/api/reviews")
def api_list_reviews() -> list[dict[str, Any]]:
    return database.list_reviews()


@app.get("/api/reviews/{review_id}")
def api_get_review(review_id: int) -> dict[str, Any]:
    rec = database.get_review(review_id)
    if rec is None:
        raise HTTPException(404, "审查记录不存在")
    return rec


@app.post("/api/reviews/{review_id}/rerun")
def api_rerun_review(review_id: int) -> dict[str, Any]:
    """用记录中固定的参数与字体摘要重新整形，并与存档结果逐簇比对。"""
    rec = database.get_review(review_id)
    if rec is None:
        raise HTTPException(404, "审查记录不存在")
    saved_req = rec["request"]
    req = AnalyzeRequest(**saved_req)
    stack = _load_stack(req.font_stack)

    # 校验字体摘要仍然一致
    current = _summaries(stack, req.font_stack)
    pinned = {f["sha256"]: f for f in rec["font_summaries"]}
    drift = []
    for f in current:
        p = pinned.get(f["sha256"])
        if p is None:
            drift.append({"sha256": f["sha256"], "issue": "font-missing"})
            continue
        for key in ("num_glyphs", "upem", "cmap_count", "version",
                    "sfnt_version"):
            if f.get(key) != p.get(key):
                drift.append({"sha256": f["sha256"], "issue": f"{key}-changed",
                              "stored": p.get(key), "current": f.get(key)})

    toggles = [(x.tag, x.value == "on") for x in req.features]
    fresh = analyzer.analyze(
        text=req.text, stack=stack, language=req.language,
        script_override=req.script, direction=req.direction,
        feature_toggles=toggles, norm=req.normalize)

    stored = rec["result"]
    consistent, diff = _diff_results(stored, fresh)
    return {
        "review_id": review_id,
        "font_drift": drift,
        "consistent": consistent and not drift,
        "differences": diff,
        "fresh_result": fresh,
    }


def _diff_results(stored: dict, fresh: dict) -> tuple[bool, list[dict]]:
    diffs: list[dict] = []
    sc, fc = stored.get("clusters", []), fresh.get("clusters", [])
    if len(sc) != len(fc):
        diffs.append({"type": "cluster-count",
                      "stored": len(sc), "current": len(fc)})
    for i, (a, b) in enumerate(zip(sc, fc)):
        for key in ("font_sha256", "font_position", "covered",
                    "merged_into_ligature", "glyph_count", "fallback_reason",
                    "bidi_level"):
            if a.get(key) != b.get(key):
                diffs.append({"cluster_index": i, "field": key,
                              "stored": a.get(key), "current": b.get(key)})
        ag = [(g.get("gid"), g.get("x_advance"), g.get("x_offset"),
               g.get("y_offset"), g.get("y_advance")) for g in a.get("glyphs", [])]
        bg = [(g.get("gid"), g.get("x_advance"), g.get("x_offset"),
               g.get("y_offset"), g.get("y_advance")) for g in b.get("glyphs", [])]
        if ag != bg:
            diffs.append({"cluster_index": i, "field": "glyphs",
                          "stored": ag, "current": bg})
    return (len(diffs) == 0, diffs)


@app.delete("/api/reviews/{review_id}")
def api_delete_review(review_id: int) -> dict[str, str]:
    if database.delete_review(review_id):
        return {"status": "deleted"}
    raise HTTPException(404, "审查记录不存在")


@app.get("/api/reviews/{review_id}/export")
def api_export_review(review_id: int) -> JSONResponse:
    rec = database.get_review(review_id)
    if rec is None:
        raise HTTPException(404, "审查记录不存在")
    report = {
        "report": "font-fallback-glyph-review",
        "schema_version": "1.0",
        "review": {
            "id": rec["id"],
            "title": rec["title"],
            "created_at": rec["created_at"],
        },
        "request_parameters": rec["request"],
        "font_summaries": rec["font_summaries"],
        "result": rec["result"],
    }
    return JSONResponse(
        report,
        headers={"Content-Disposition":
                 f'attachment; filename="review-{review_id}.json"'},
    )


# ------------------------------------------------- 许可与嵌入合规门禁

def _resolve_policy(scenario: str, version: int | None) -> dict[str, Any]:
    if version is None:
        rec = database.get_active_license_policy(scenario)
        if rec is None:
            raise HTTPException(
                409, f"场景 {scenario} 尚无生效策略版本，请管理员先登记")
        return rec
    rec = database.get_license_policy(scenario, version)
    if rec is None:
        raise HTTPException(404, f"策略 {scenario} v{version} 不存在")
    return rec


def _build_gate_stack(digests: list[str]) -> list[dict[str, Any]]:
    """加载栈中每个字体的许可证据，并核验磁盘字体摘要是否漂移。"""
    stack = []
    for i, digest in enumerate(digests):
        meta = database.get_font(digest)
        path = font_loader.FONT_DIR / f"{digest}.otf"
        if not path.exists():
            raise HTTPException(404, f"字体 {digest} 不存在或文件已丢失")
        disk_bytes = path.read_bytes()
        actual = sha256_bytes(disk_bytes)
        if meta and meta.get("license_evidence"):
            evidence = meta["license_evidence"]
        else:
            # 历史字体或元数据缺失：从解包 sfnt 重建（无法枚举 TTC 全成员）
            lf = get_loaded_font(digest)
            evidence = lf.license_evidence if lf else {}
            if not evidence:
                from .font_loader import license_evidence_from_sfnt
                evidence = license_evidence_from_sfnt(disk_bytes, digest)
        if actual != digest:
            evidence = json.loads(json.dumps(evidence, ensure_ascii=False))
            for m in evidence.get("members", []):
                if m.get("member_index") == 0:
                    m["actual_sha256"] = actual
        stack.append({
            "position": i,
            "sha256": digest,
            "filename": (meta or {}).get("filename", f"{digest[:12]}….otf"),
            "family": (meta or {}).get("family", "(unknown)"),
            "evidence": evidence,
        })
    return stack


def _run_gate(scenario: str, version: int | None,
              digests: list[str]) -> dict[str, Any]:
    if len(digests) != len(set(digests)):
        raise HTTPException(400, "字体栈中存在重复字体")
    policy_rec = _resolve_policy(scenario, version)
    stack = _build_gate_stack(digests)
    exemptions = database.list_license_exemptions(
        scenario=scenario, policy_version=policy_rec["version"])
    manifest = license_gate.evaluate(policy_rec, stack, exemptions)
    return {"policy_version": policy_rec["version"], "manifest": manifest}


@app.post("/api/license/policies")
def api_create_policy(req: PolicyCreateRequest) -> dict[str, Any]:
    try:
        license_gate.validate_policy(req.policy)
    except ValueError as exc:
        raise HTTPException(422, f"策略校验失败: {exc}") from exc
    try:
        result = database.register_license_policy(
            scenario=req.scenario, policy=req.policy,
            registered_by=req.registered_by, activate=req.activate,
            expected_active_version=req.expected_active_version)
    except database.PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    rec = database.get_license_policy(req.scenario, result["version"])
    return {**rec, "active": req.activate}


@app.get("/api/license/policies")
def api_list_policies(scenario: str = Query(...)) -> list[dict[str, Any]]:
    if scenario not in license_gate.SCENARIOS:
        raise HTTPException(400, f"场景必须是 {license_gate.SCENARIOS}")
    return database.list_license_policies(scenario)


@app.get("/api/license/policies/{scenario}/active")
def api_active_policy(scenario: str) -> dict[str, Any]:
    if scenario not in license_gate.SCENARIOS:
        raise HTTPException(400, f"场景必须是 {license_gate.SCENARIOS}")
    rec = database.get_active_license_policy(scenario)
    if rec is None:
        raise HTTPException(404, f"场景 {scenario} 尚无生效策略")
    return rec


@app.get("/api/license/policies/{scenario}/versions/{version}")
def api_get_policy(scenario: str, version: int) -> dict[str, Any]:
    rec = database.get_license_policy(scenario, version)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    return rec


@app.post("/api/license/policies/{scenario}/versions/{version}/activate")
def api_activate_policy(scenario: str, version: int,
                        req: PolicyActivateRequest) -> dict[str, str]:
    try:
        database.activate_license_policy(
            scenario, version, req.expected_active_version)
    except database.PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "activated", "scenario": scenario, "version": str(version)}


@app.post("/api/license/exemptions")
def api_create_exemption(req: ExemptionCreateRequest) -> dict[str, Any]:
    if not database.get_font(req.font_digest) and not (
            font_loader.FONT_DIR / f"{req.font_digest}.otf").exists():
        raise HTTPException(404, "豁免绑定的字体不存在")
    data = req.model_dump()
    if data.get("expires_at") is not None:
        data["expires_at"] = data["expires_at"].isoformat()
    try:
        eid = database.insert_license_exemption(data)
    except KeyError as exc:
        raise HTTPException(422, str(exc)) from exc
    return database.get_license_exemption(eid)


@app.get("/api/license/exemptions")
def api_list_exemptions(scenario: str | None = None,
                        policy_version: int | None = None,
                        font_digest: str | None = None,
                        include_revoked: bool = True) -> list[dict[str, Any]]:
    return database.list_license_exemptions(
        scenario=scenario, policy_version=policy_version,
        font_digest=font_digest, include_revoked=include_revoked)


@app.get("/api/license/exemptions/{exemption_id}")
def api_get_exemption(exemption_id: int) -> dict[str, Any]:
    rec = database.get_license_exemption(exemption_id)
    if rec is None:
        raise HTTPException(404, "豁免不存在")
    return rec


@app.post("/api/license/exemptions/{exemption_id}/revoke")
def api_revoke_exemption(exemption_id: int) -> dict[str, Any]:
    if not database.revoke_license_exemption(exemption_id):
        raise HTTPException(404, "豁免不存在或已撤销")
    return database.get_license_exemption(exemption_id)


@app.post("/api/license/evaluate")
def api_gate_evaluate(req: GateEvaluateRequest) -> dict[str, Any]:
    """即时评估（dry-run）：返回逐条证据与结论，不冻结、不幂等入库。"""
    out = _run_gate(req.scenario, req.policy_version, req.font_stack)
    return {"frozen": False, **out}


@app.post("/api/license/publish")
def api_gate_publish(req: GatePublishRequest) -> JSONResponse:
    """评估并原子冻结（字体摘要 + 策略版本 + 决定）；重复请求幂等。"""
    out = _run_gate(req.scenario, req.policy_version, req.font_stack)
    manifest = out["manifest"]
    version = out["policy_version"]
    decision_id, created = database.insert_license_decision(
        request_id=req.request_id or None,
        fingerprint=manifest["fingerprint"],
        scenario=req.scenario, policy_version=version,
        font_stack=req.font_stack, manifest=manifest)
    if not created:
        frozen = database.get_license_decision(decision_id)
        manifest = frozen["manifest"]
    return JSONResponse({
        "frozen": True,
        "created": created,
        "decision_id": decision_id,
        "policy_version": manifest["policy"]["version"],
        "manifest": manifest,
    }, status_code=201 if created else 200)


@app.get("/api/license/decisions")
def api_list_decisions(scenario: str | None = None) -> list[dict[str, Any]]:
    return database.list_license_decisions(scenario=scenario)


@app.get("/api/license/decisions/{decision_id}")
def api_get_decision(decision_id: int) -> dict[str, Any]:
    rec = database.get_license_decision(decision_id)
    if rec is None:
        raise HTTPException(404, "冻结决定不存在")
    return rec


@app.get("/api/license/decisions/{decision_id}/export")
def api_export_decision(decision_id: int) -> JSONResponse:
    rec = database.get_license_decision(decision_id)
    if rec is None:
        raise HTTPException(404, "冻结决定不存在")
    return JSONResponse(
        rec["manifest"],
        headers={"Content-Disposition":
                 f'attachment; filename="license-gate-decision-{decision_id}.json"'},
    )


@app.get("/api/fonts/{digest}/license-evidence")
def api_font_license_evidence(digest: str) -> dict[str, Any]:
    path = font_loader.FONT_DIR / f"{digest}.otf"
    if not path.exists():
        raise HTTPException(404, "字体文件不存在")
    meta = database.get_font(digest)
    evidence = (meta or {}).get("license_evidence") or {}
    if not evidence:
        from .font_loader import license_evidence_from_sfnt
        evidence = license_evidence_from_sfnt(path.read_bytes(), digest)
    # 以数值 fsType 为准，在副本中重算派生标签（升级前登记的存证可能
    # 仍按旧位序标记）；数据库中的原始存证保持原貌，不改写
    evidence = font_loader.normalize_evidence_fstype(evidence)
    return {"sha256": digest,
            "summary": license_gate.summarize_evidence(evidence),
            "evidence": evidence}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- 静态页面

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
