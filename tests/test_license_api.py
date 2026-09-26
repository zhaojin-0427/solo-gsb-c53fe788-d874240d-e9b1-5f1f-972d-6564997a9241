"""许可门禁端到端测试：真实字体解析、API、CAS、豁免、幂等冻结、摘要漂移。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="license-gate-test-")
os.environ.setdefault("TMPDIR", _tmp)

import app.database as database
import app.font_loader as font_loader
from app.main import app, _seed_default_policies
from fastapi.testclient import TestClient
from fontTools.ttLib import TTCollection, TTFont

database.DB_PATH = Path(_tmp) / "app.db"
font_loader.FONT_DIR = Path(_tmp) / "fonts"
font_loader.FONT_DIR.mkdir(parents=True, exist_ok=True)

SYS = Path("/usr/share/fonts/truetype/dejavu")


@unittest.skipUnless(SYS.exists(), "DejaVu 测试字体不存在")
class LicenseGateApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        _seed_default_policies()
        cls.client = TestClient(app)
        cls.sans = cls._upload("DejaVuSans.ttf")
        cls.mono = cls._upload("DejaVuSansMono.ttf")
        cls.woff2_digest = cls._upload_woff2()
        cls.ttc_digest = cls._upload_ttc()
        cls.restricted_digest = cls._upload_restricted()

    @classmethod
    def _upload(cls, name):
        data = (SYS / name).read_bytes()
        r = cls.client.post(
            "/api/fonts",
            files={"file": (name, io.BytesIO(data), "font/sfnt")})
        assert r.status_code == 200, r.text
        return r.json()["sha256"]

    @classmethod
    def _upload_woff2(cls):
        ft = TTFont(SYS / "DejaVuSans.ttf")
        ft.flavor = "woff2"
        buf = io.BytesIO()
        ft.save(buf)
        r = cls.client.post(
            "/api/fonts",
            files={"file": ("DejaVuSans.woff2", io.BytesIO(buf.getvalue()),
                            "font/woff2")})
        assert r.status_code == 200, r.text
        return r.json()["sha256"]

    @classmethod
    def _upload_ttc(cls):
        ft0 = TTFont(SYS / "DejaVuSans.ttf")
        ft1 = TTFont(SYS / "DejaVuSans.ttf")
        # 0x0008 = Editable embedding（修正后的真实类型值）
        ft1["OS/2"].fsType = 0x0008
        ft1["name"].setName("Completely different proprietary license",
                            13, 3, 1, 0x409)
        col = TTCollection()
        col.fonts = [ft0, ft1]
        buf = io.BytesIO()
        col.save(buf)
        r = cls.client.post(
            "/api/fonts",
            files={"file": ("coll.ttc", io.BytesIO(buf.getvalue()),
                            "font/collection")})
        assert r.status_code == 200, r.text
        return r.json()["sha256"]

    @classmethod
    def _upload_restricted(cls):
        # 0x0002 = Restricted License embedding；同时具备必需的 nameID 13/14。
        # 旧逻辑曾把 0x0002 误标为 preview-print 并在印刷策略下错误放行。
        ft = TTFont(SYS / "DejaVuSans.ttf")
        ft["OS/2"].fsType = 0x0002
        ft["name"].setName(
            "Restricted license embedding is not permitted without a "
            "signed vendor agreement", 13, 3, 1, 0x409)
        ft["name"].setName("https://example.com/restricted-font-license",
                           14, 3, 1, 0x409)
        buf = io.BytesIO()
        ft.save(buf)
        r = cls.client.post(
            "/api/fonts",
            files={"file": ("Restricted.ttf", io.BytesIO(buf.getvalue()),
                            "font/sfnt")})
        assert r.status_code == 200, r.text
        return r.json()["sha256"]

    # ------------------------------------------------------------- 证据/策略

    def test_01_upload_records_license_evidence(self):
        meta = self.client.get("/api/fonts").json()
        sans = next(f for f in meta if f["sha256"] == self.sans)
        ev = sans["license_evidence"]
        self.assertEqual(ev["container"], "sfnt")
        m0 = ev["members"][0]
        self.assertEqual(m0["fstype"], 0)
        self.assertIn("Bitstream", m0["name_license_description"])
        self.assertTrue(m0["name_license_url"].startswith("http://"))
        # 独立证据端点
        r = self.client.get(f"/api/fonts/{self.sans}/license-evidence")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["summary"]["fstype"], 0)

    def test_02_ttc_members_enumerated(self):
        meta = self.client.get("/api/fonts").json()
        ttc = next(f for f in meta if f["sha256"] == self.ttc_digest)
        ev = ttc["license_evidence"]
        self.assertEqual(ev["container"], "ttc")
        self.assertTrue(ev["members_complete"])
        self.assertEqual(len(ev["members"]), 2)
        self.assertEqual(ev["members"][0]["fstype"], 0)
        self.assertEqual(ev["members"][1]["fstype"], 8)
        self.assertEqual(ev["unpack_summary"]["member_count"], 2)

    def test_02c_restricted_font_evidence_labels_0x0002(self):
        meta = self.client.get("/api/fonts").json()
        rf = next(f for f in meta if f["sha256"] == self.restricted_digest)
        m0 = rf["license_evidence"]["members"][0]
        self.assertEqual(m0["fstype"], 0x0002)
        self.assertEqual(m0["fstype_flags"], [0x0002])
        self.assertEqual(m0["fstype_flag_labels"], ["restricted"])
        self.assertNotIn("preview-print-only", m0["fstype_flag_labels"])
        self.assertEqual(m0["fstype_reserved_bits"], 0)
        self.assertIn("Restricted", m0["name_license_description"])

    def test_02b_woff2_single_decode_identity_consistent(self):
        # woff2 重新解包非逐字节稳定：证据成员摘要必须等于字体身份摘要，
        # 且解包摘要记录原始/解包大小
        meta = self.client.get("/api/fonts").json()
        w2 = next(f for f in meta if f["sha256"] == self.woff2_digest)
        ev = w2["license_evidence"]
        self.assertEqual(ev["container"], "woff2")
        self.assertEqual(ev["members"][0]["sha256"], self.woff2_digest)
        self.assertEqual(ev["unpack_summary"]["sha256"], self.woff2_digest)
        self.assertLess(ev["unpack_summary"]["original_size"],
                        ev["unpack_summary"]["unpacked_size"])
        # 桌面策略评估不得误报摘要不匹配
        r = self._eval("desktop", [self.woff2_digest])
        m = r.json()["manifest"]
        self.assertNotIn("font-digest-mismatch", m["deny_codes"])
        self.assertEqual(m["decision"], "allow", m["findings"])

    def test_03_default_policies_seeded(self):
        # v1 历史基线保留为不可变登记事实；生效版本为修正版 v2
        for sc in ("web", "desktop", "print"):
            r = self.client.get(f"/api/license/policies/{sc}/active")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["version"], 2)
            versions = self.client.get(
                f"/api/license/policies?scenario={sc}").json()
            self.assertEqual([v["version"] for v in versions], [1, 2])
            v1, v2 = versions
            self.assertFalse(v1["active"])
            self.assertTrue(v2["active"])
            self.assertEqual(
                v2["policy"].get("fstype_bits_revision"), 2)
            self.assertIn(0x0002, v2["policy"]["forbidden_fstype_bits"])

    # ------------------------------------------------------------- 评估

    def _eval(self, scenario, stack, version=None):
        return self.client.post("/api/license/evaluate", json={
            "scenario": scenario, "font_stack": stack,
            "policy_version": version})

    def test_10_web_denies_http_license_url(self):
        r = self._eval("web", [self.sans])
        self.assertEqual(r.status_code, 200, r.text)
        m = r.json()["manifest"]
        self.assertEqual(m["decision"], "deny")
        self.assertIn("name-field-bad-url", m["deny_codes"])
        self.assertFalse(r.json()["frozen"])

    def test_11_desktop_allows_dejavu(self):
        r = self._eval("desktop", [self.sans])
        m = r.json()["manifest"]
        self.assertEqual(m["decision"], "allow", m["findings"])

    def test_11b_restricted_0x0002_denied_in_all_scenarios(self):
        # 含必需 nameID 13（Web 还要求 14 https，本字体两者齐备），
        # 但 fsType=0x0002 是 Restricted License embedding：
        # 无有效豁免时三种场景（含印刷默认策略）必须拒绝。
        for sc in ("web", "desktop", "print"):
            r = self._eval(sc, [self.restricted_digest])
            m = r.json()["manifest"]
            self.assertEqual(m["decision"], "deny", (sc, m["findings"]))
            self.assertIn("fstype-bit-forbidden", m["deny_codes"], sc)
            self.assertIn("fstype-mask-not-allowed", m["deny_codes"], sc)
            finding = next(f for f in m["findings"]
                           if f["code"] == "fstype-bit-forbidden")
            self.assertEqual(finding["rule"]["label"], "restricted")
            self.assertEqual(finding["rule"]["forbidden_bit"], 0x0002)
            labels = m["font_stack"][0]["license_evidence"]["members"][0][
                "fstype_flag_labels"]
            self.assertEqual(labels, ["restricted"])
            # 逐条证据引用的是同一修正版
            self.assertEqual(m["policy"]["version"], 2)

    def test_11c_restricted_print_publish_freezes_deny_on_v2(self):
        r = self.client.post("/api/license/publish", json={
            "scenario": "print", "font_stack": [self.restricted_digest],
            "request_id": "release-restricted-must-freeze-deny"})
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertTrue(body["created"])
        m = body["manifest"]
        self.assertEqual(m["decision"], "deny")
        self.assertEqual(m["policy"]["version"], 2)
        # 幂等：再次发布命中同一冻结清单（不重算）
        r2 = self.client.post("/api/license/publish", json={
            "scenario": "print", "font_stack": [self.restricted_digest],
            "request_id": "release-restricted-must-freeze-deny"})
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["created"])
        self.assertEqual(r2.json()["decision_id"], body["decision_id"])
        self.assertEqual(r2.json()["manifest"]["fingerprint"],
                         m["fingerprint"])

    def test_11d_legacy_v1_decision_not_recomputed(self):
        # 历史冻结决定不得重算：显式按旧 v1（曾错误放行 0x0002）冻结，
        # 其指纹与修正版 v2 不同；旧记录原样可取、不被新评估改写。
        r1 = self.client.post("/api/license/publish", json={
            "scenario": "print", "policy_version": 1,
            "font_stack": [self.restricted_digest],
            "request_id": "legacy-v1-frozen-2026-09-01"})
        self.assertEqual(r1.status_code, 201, r1.text)
        legacy = r1.json()
        self.assertEqual(legacy["policy_version"], 1)
        self.assertEqual(legacy["manifest"]["decision"], "allow")
        legacy_id = legacy["decision_id"]
        legacy_fp = legacy["manifest"]["fingerprint"]

        # 同一栈在修正版 v2 下的指纹（test_11c 已冻结，幂等命中同一记录）
        r2 = self.client.post("/api/license/publish", json={
            "scenario": "print",
            "font_stack": [self.restricted_digest],
            "request_id": "release-restricted-must-freeze-deny"})
        self.assertEqual(r2.status_code, 200, r2.text)
        corrected = r2.json()
        self.assertEqual(corrected["policy_version"], 2)
        self.assertEqual(corrected["manifest"]["decision"], "deny")
        self.assertNotEqual(corrected["manifest"]["fingerprint"], legacy_fp)
        self.assertNotEqual(corrected["decision_id"], legacy_id)

        # 旧冻结记录仍按原 v1 清单返回，未被重算改写
        rec = self.client.get(
            f"/api/license/decisions/{legacy_id}").json()
        self.assertEqual(rec["manifest"]["decision"], "allow")
        self.assertEqual(rec["manifest"]["policy"]["version"], 1)
        self.assertEqual(rec["fingerprint"], legacy_fp)

    def test_12_ttc_member_mismatch_denied(self):
        r = self._eval("desktop", [self.ttc_digest])
        m = r.json()["manifest"]
        self.assertEqual(m["decision"], "deny")
        self.assertIn("ttc-member-license-mismatch", m["deny_codes"])

    def test_13_restricted_fstype_denied(self):
        # TTC 成员 1 为 editable(0x0008)：print 允许但 web 掩码不允许
        rw = self._eval("web", [self.ttc_digest])
        self.assertIn("fstype-mask-not-allowed",
                      rw.json()["manifest"]["deny_codes"])

    def test_14_policy_version_pin(self):
        r = self._eval("desktop", [self.sans], version=2)
        self.assertEqual(r.status_code, 200)
        r2 = self._eval("desktop", [self.sans], version=99)
        self.assertEqual(r2.status_code, 404)

    def test_15_missing_font_404(self):
        r = self._eval("web", ["0" * 64])
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------- CAS

    def test_20_policy_cas_success_and_conflict(self):
        # 以基准 2（修正版）登记 v3 并激活
        r = self.client.post("/api/license/policies", json={
            "scenario": "web", "expected_active_version": 2,
            "activate": True, "registered_by": "tester",
            "policy": self._web_policy_body()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 3)

        # 旧基准再次发布 → 409 CAS 冲突（登记仍成功为 v4，不回滚不可变事实）
        r2 = self.client.post("/api/license/policies", json={
            "scenario": "web", "expected_active_version": 2,
            "activate": True, "registered_by": "tester",
            "policy": self._web_policy_body()})
        self.assertEqual(r2.status_code, 409)
        # v4 已登记但未激活；生效仍为 v3
        active = self.client.get("/api/license/policies/web/active").json()
        self.assertEqual(active["version"], 3)
        versions = self.client.get(
            "/api/license/policies?scenario=web").json()
        self.assertEqual([v["version"] for v in versions], [1, 2, 3, 4])
        self.assertTrue(versions[2]["active"])
        self.assertFalse(versions[3]["active"])

        # 显式激活 v4 需要正确基准
        bad = self.client.post(
            "/api/license/policies/web/versions/4/activate",
            json={"expected_active_version": 2})
        self.assertEqual(bad.status_code, 409)
        ok = self.client.post(
            "/api/license/policies/web/versions/4/activate",
            json={"expected_active_version": 3})
        self.assertEqual(ok.status_code, 200)

    def test_21_invalid_policy_rejected(self):
        r = self.client.post("/api/license/policies", json={
            "scenario": "print", "expected_active_version": 1,
            "activate": False,
            "policy": {"allowed_fstype_masks": ["nope"]}})
        self.assertEqual(r.status_code, 422)
        r2 = self.client.post("/api/license/policies", json={
            "scenario": "print", "expected_active_version": 1,
            "activate": False,
            "policy": {"forbidden_license_patterns": ["(["]}})
        self.assertEqual(r2.status_code, 422)

    @staticmethod
    def _web_policy_body():
        return {
            "allowed_fstype_masks": [0, 0x0100],
            "forbidden_fstype_bits": [0x0002],
            "required_name_fields": ["license_description", "license_url"],
            "require_https_license_url": True,
            "forbidden_license_patterns": [],
            "deny_on_fstype_reserved_bits": True,
            "deny_on_embedding_flags_conflict": True,
            "deny_on_ttc_member_license_mismatch": True,
            "deny_on_incomplete_collection_evidence": True,
            "conflict_rules": [],
        }

    # ------------------------------------------------------------- 豁免

    def _active_web_version(self):
        return self.client.get(
            "/api/license/policies/web/active").json()["version"]

    def test_30_expired_exemption_still_denies(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        r = self.client.post("/api/license/exemptions", json={
            "scenario": "web",
            "policy_version": self._active_web_version(),
            "font_digest": self.sans,
            "rule_codes": ["name-field-bad-url"],
            "justification": "过期授权", "expires_at": past})
        self.assertEqual(r.status_code, 200, r.text)
        m = self._eval("web", [self.sans]).json()["manifest"]
        self.assertEqual(m["decision"], "deny")
        self.assertIn("name-field-bad-url", m["deny_codes"])

    def test_31_valid_exemption_allows_then_revoke_denies(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        r = self.client.post("/api/license/exemptions", json={
            "scenario": "web",
            "policy_version": self._active_web_version(),
            "font_digest": self.sans,
            "rule_codes": ["name-field-bad-url"],
            "justification": "法务批准 #LIC-2026-001",
            "expires_at": future})
        self.assertEqual(r.status_code, 200, r.text)
        eid = r.json()["id"]

        m = self._eval("web", [self.sans]).json()["manifest"]
        self.assertEqual(m["decision"], "allow", m["findings"])
        finding = next(f for f in m["findings"]
                       if f["code"] == "name-field-bad-url")
        self.assertEqual(finding["severity"], "warn")
        self.assertTrue(finding["exempted"])
        self.assertEqual(m["exemptions_applied"][0]["exemption_id"], eid)

        # 豁免必须绑定具体字体：mono 字体仍被拒绝
        m2 = self._eval("web", [self.mono]).json()["manifest"]
        self.assertEqual(m2["decision"], "deny")

        # 撤销后再次拒绝
        self.assertEqual(
            self.client.post(f"/api/license/exemptions/{eid}/revoke").status_code,
            200)
        m3 = self._eval("web", [self.sans]).json()["manifest"]
        self.assertEqual(m3["decision"], "deny")

    def test_32_exemption_requires_existing_policy(self):
        r = self.client.post("/api/license/exemptions", json={
            "scenario": "web", "policy_version": 999,
            "font_digest": self.sans, "rule_codes": ["*"],
            "justification": "x"})
        self.assertEqual(r.status_code, 422)

    def test_33_restricted_font_passes_only_with_valid_exemption(self):
        version = self._active_web_version()
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        # 无豁免：拒绝
        m = self._eval("web", [self.restricted_digest]).json()["manifest"]
        self.assertEqual(m["decision"], "deny")

        r = self.client.post("/api/license/exemptions", json={
            "scenario": "web",
            "policy_version": version,
            "font_digest": self.restricted_digest,
            "rule_codes": ["fstype-bit-forbidden", "fstype-mask-not-allowed"],
            "justification": "供应商书面授权 #LIC-2026-RESTRICTED",
            "expires_at": future})
        self.assertEqual(r.status_code, 200, r.text)
        eid = r.json()["id"]

        m2 = self._eval("web", [self.restricted_digest]).json()["manifest"]
        self.assertEqual(m2["decision"], "allow", m2["findings"])
        codes = {f["code"] for f in m2["findings"]}
        self.assertEqual(
            codes, {"fstype-bit-forbidden", "fstype-mask-not-allowed"})
        self.assertTrue(all(f["severity"] == "warn" for f in m2["findings"]))
        self.assertEqual(m2["policy"]["version"], version)

        # 豁免撤销后再次拒绝
        self.client.post(f"/api/license/exemptions/{eid}/revoke")
        m3 = self._eval("web", [self.restricted_digest]).json()["manifest"]
        self.assertEqual(m3["decision"], "deny")

    # ------------------------------------------------------------- 发布/幂等

    def test_40_publish_freezes_and_is_idempotent(self):
        # 桌面策略下 DejaVu 直接放行
        r1 = self.client.post("/api/license/publish", json={
            "scenario": "desktop", "font_stack": [self.sans],
            "request_id": "release-2026-09-22-001"})
        self.assertEqual(r1.status_code, 201, r1.text)
        did = r1.json()["decision_id"]
        self.assertTrue(r1.json()["created"])
        fp = r1.json()["manifest"]["fingerprint"]

        # 同 request_id 重复 → 同一决定，created=false
        r2 = self.client.post("/api/license/publish", json={
            "scenario": "desktop", "font_stack": [self.sans],
            "request_id": "release-2026-09-22-001"})
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["created"])
        self.assertEqual(r2.json()["decision_id"], did)
        self.assertEqual(r2.json()["manifest"]["fingerprint"], fp)

        # 无 request_id 但相同指纹（同栈+同策略版本）→ 仍命中同一冻结记录
        r3 = self.client.post("/api/license/publish", json={
            "scenario": "desktop", "font_stack": [self.sans]})
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["decision_id"], did)

        # 不同栈 → 新决定
        r4 = self.client.post("/api/license/publish", json={
            "scenario": "desktop", "font_stack": [self.sans, self.mono]})
        self.assertEqual(r4.status_code, 201)
        self.assertNotEqual(r4.json()["decision_id"], did)

        # 列表/详情/导出
        lst = self.client.get(
            "/api/license/decisions?scenario=desktop").json()
        self.assertTrue(any(d["id"] == did for d in lst))
        detail = self.client.get(f"/api/license/decisions/{did}").json()
        self.assertEqual(detail["manifest"]["fingerprint"], fp)
        self.assertEqual(detail["manifest"]["decision"], "allow")
        self.assertEqual(detail["font_stack"], [self.sans])

        exp = self.client.get(f"/api/license/decisions/{did}/export")
        self.assertEqual(exp.status_code, 200)
        self.assertIn("attachment", exp.headers["content-disposition"])
        exported = exp.json()
        self.assertEqual(exported["manifest"],
                         "font-asset-license-embedding-gate")
        self.assertEqual(exported["fingerprint"], fp)
        # 冻结清单内包含完整证据与策略快照（桌面生效为修正版 v2）
        self.assertEqual(exported["policy"]["version"], 2)
        self.assertEqual(
            exported["font_stack"][0]["license_evidence"]["members"][0]["fstype"],
            0)

    # ------------------------------------------------------------- 摘要漂移

    def test_50_disk_digest_drift_denies_and_blocks_publish(self):
        target = font_loader.FONT_DIR / f"{self.mono}.otf"
        original = target.read_bytes()
        try:
            # 用 sans 的字节覆盖 mono 寻址文件
            target.write_bytes(
                (font_loader.FONT_DIR / f"{self.sans}.otf").read_bytes())
            font_loader._cache.pop(self.mono, None)
            r = self._eval("desktop", [self.mono])
            m = r.json()["manifest"]
            self.assertEqual(m["decision"], "deny")
            self.assertIn("font-digest-mismatch", m["deny_codes"])
            finding = next(f for f in m["findings"]
                           if f["code"] == "font-digest-mismatch")
            self.assertEqual(finding["evidence"]["claimed_sha256"], self.mono)
            self.assertEqual(finding["evidence"]["actual_sha256"], self.sans)

            # 过期豁免也不能放行摘要不匹配（此处根本不给豁免）
            pr = self.client.post("/api/license/publish", json={
                "scenario": "desktop", "font_stack": [self.mono]})
            self.assertEqual(pr.json()["manifest"]["decision"], "deny")
        finally:
            target.write_bytes(original)
            font_loader._cache.pop(self.mono, None)

        # 恢复后立即放行
        m = self._eval("desktop", [self.mono]).json()["manifest"]
        self.assertEqual(m["decision"], "allow", m["findings"])

    # ------------------------------------------- 存量存证派生标签重算

    def _rewrite_stored_evidence(self, digest, evidence):
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE fonts SET license_evidence=? WHERE sha256=?",
                (json.dumps(evidence, ensure_ascii=False), digest))

    def test_60_legacy_stored_evidence_recomputed_on_read(self):
        """升级前登记的存证（旧位序派生标签）：评估/发布/许可摘要读取时
        以数值 fsType 为准重算，数据库原始存证不被改写。"""
        digest = self.restricted_digest  # fstype=0x0002（restricted）
        original = database.get_font(digest)["license_evidence"]
        # 模拟 v1 旧位序存证：0x0002 被误标为 preview-print-only
        legacy = json.loads(json.dumps(original))
        legacy["members"][0]["fstype_flags"] = [0x0002]
        legacy["members"][0]["fstype_flag_labels"] = ["preview-print-only"]
        legacy["members"][0]["fstype_reserved_bits"] = 0
        self._rewrite_stored_evidence(digest, legacy)
        try:
            # 原始存证保持旧标签（读取路径不改写数据库）
            stored = database.get_font(digest)["license_evidence"]
            self.assertEqual(stored["members"][0]["fstype_flag_labels"],
                             ["preview-print-only"])

            # 许可摘要端点以数值 fsType 为准重算
            r = self.client.get(f"/api/fonts/{digest}/license-evidence")
            self.assertEqual(r.status_code, 200)
            summary = r.json()["summary"]
            self.assertEqual(summary["fstype"], 0x0002)
            self.assertEqual(summary["fstype_flag_labels"], ["restricted"])
            ev0 = r.json()["evidence"]["members"][0]
            self.assertEqual(ev0["fstype_flag_labels"], ["restricted"])
            self.assertEqual(ev0["fstype_flags"], [0x0002])

            # 即时评估：结论不变，逐条证据与清单标签同步重算
            m = self._eval("print", [digest]).json()["manifest"]
            self.assertEqual(m["decision"], "deny")
            self.assertIn("fstype-bit-forbidden", m["deny_codes"])
            labels = m["font_stack"][0]["license_evidence"]["members"][0][
                "fstype_flag_labels"]
            self.assertEqual(labels, ["restricted"])
            finding = next(f for f in m["findings"]
                           if f["code"] == "fstype-bit-forbidden")
            self.assertEqual(finding["evidence"]["fstype_flag_labels"],
                             ["restricted"])

            # 发布冻结：新清单同样是重算后的标签
            pr = self.client.post("/api/license/publish", json={
                "scenario": "desktop", "font_stack": [digest],
                "request_id": "legacy-evidence-recompute-freeze"})
            self.assertEqual(pr.status_code, 201, pr.text)
            self.assertTrue(pr.json()["created"])
            frozen_labels = pr.json()["manifest"]["font_stack"][0][
                "license_evidence"]["members"][0]["fstype_flag_labels"]
            self.assertEqual(frozen_labels, ["restricted"])

            # 评估与发布后，数据库原始存证仍未被改写
            stored2 = database.get_font(digest)["license_evidence"]
            self.assertEqual(stored2["members"][0]["fstype_flag_labels"],
                             ["preview-print-only"])
        finally:
            self._rewrite_stored_evidence(digest, original)

    def test_61_historical_frozen_decision_keeps_legacy_labels(self):
        """修复前冻结的决定（清单内仍含旧位序标签）原样返回，绝不重算。"""
        digest = self.restricted_digest
        legacy_manifest = {
            "manifest": "font-asset-license-embedding-gate",
            "schema_version": "1.0",
            "scenario": "print",
            "evaluated_at": "2026-09-01T00:00:00+00:00",
            "decision": "allow",
            "deny_codes": [],
            "policy": {"scenario": "print", "version": 1,
                       "created_at": "2026-09-01T00:00:00+00:00",
                       "registered_by": "system-bootstrap",
                       "rules": {}},
            "font_stack": [{
                "position": 0, "sha256": digest,
                "filename": "Restricted.ttf", "family": "DejaVu Sans",
                "license_evidence": {
                    "container": "sfnt", "members_complete": True,
                    "members": [{
                        "member_index": 0, "sha256": digest,
                        "fstype": 0x0002,
                        "fstype_flags": [0x0002],
                        "fstype_flag_labels": ["preview-print-only"],
                        "fstype_reserved_bits": 0}]}}],
            "exemptions_considered": [], "exemptions_applied": [],
            "findings": [],
            "fingerprint": "legacy-frozen-fingerprint-for-test",
        }
        did, created = database.insert_license_decision(
            request_id="legacy-frozen-keep-as-is",
            fingerprint=legacy_manifest["fingerprint"],
            scenario="print", policy_version=1,
            font_stack=[digest], manifest=legacy_manifest)
        self.assertTrue(created)

        # 详情与导出都按冻结原貌返回旧标签，不重算
        rec = self.client.get(f"/api/license/decisions/{did}")
        self.assertEqual(rec.status_code, 200)
        labels = rec.json()["manifest"]["font_stack"][0][
            "license_evidence"]["members"][0]["fstype_flag_labels"]
        self.assertEqual(labels, ["preview-print-only"])
        exp = self.client.get(f"/api/license/decisions/{did}/export")
        self.assertEqual(exp.status_code, 200)
        self.assertEqual(exp.json()["font_stack"][0]["license_evidence"]
                         ["members"][0]["fstype_flag_labels"],
                         ["preview-print-only"])


if __name__ == "__main__":
    unittest.main()
