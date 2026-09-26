"""HTTP API 端到端测试（FastAPI TestClient，使用临时数据目录）。"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path

# 在导入应用前把数据目录重定向到临时位置
_tmp = tempfile.mkdtemp(prefix="glyph-review-test-")
os.environ.setdefault("TMPDIR", _tmp)

import app.database as database
import app.font_loader as font_loader
from app.main import app
from fastapi.testclient import TestClient

database.DB_PATH = Path(_tmp) / "app.db"
font_loader.FONT_DIR = Path(_tmp) / "fonts"
font_loader.FONT_DIR.mkdir(parents=True, exist_ok=True)

SYS = Path("/usr/share/fonts/truetype/dejavu")


@unittest.skipUnless(SYS.exists(), "DejaVu 测试字体不存在")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        cls.client = TestClient(app)
        cls.sans = cls._upload("DejaVuSans.ttf")
        cls.mono = cls._upload("DejaVuSansMono.ttf")

    @classmethod
    def _upload(cls, name):
        data = (SYS / name).read_bytes()
        resp = cls.client.post(
            "/api/fonts",
            files={"file": (name, io.BytesIO(data), "font/sfnt")})
        assert resp.status_code == 200, resp.text
        return resp.json()["sha256"]

    def test_health_and_font_list(self):
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")
        fonts = self.client.get("/api/fonts").json()
        digests = {f["sha256"] for f in fonts}
        self.assertIn(self.sans, digests)
        self.assertIn(self.mono, digests)

    def test_upload_rejects_bad_extension(self):
        r = self.client.post("/api/fonts",
                             files={"file": ("x.txt", b"xx", "text/plain")})
        self.assertEqual(r.status_code, 400)

    def test_upload_rejects_corrupt_font(self):
        r = self.client.post("/api/fonts",
                             files={"file": ("x.otf", b"GARBAGE", "font/otf")})
        self.assertEqual(r.status_code, 400)

    def _analyze_body(self, **over):
        body = {
            "text": "fi 你好",
            "font_stack": [self.sans, self.mono],
            "language": "auto", "script": None, "direction": "auto",
            "features": [], "normalize": "none",
        }
        body.update(over)
        return body

    def test_analyze_payload(self):
        r = self.client.post("/api/analyze", json=self._analyze_body())
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIn("result", d)
        self.assertIn("font_summaries", d)
        self.assertEqual(len(d["font_summaries"]), 2)
        stats = d["result"]["stats"]
        self.assertEqual(stats["num_clusters"], 5)   # f i 空格 你 好
        self.assertEqual(stats["missing"], 2)
        self.assertEqual(stats["clusters_merged_into_ligature"], 2)

    def test_missing_font_returns_404(self):
        r = self.client.post("/api/analyze", json=self._analyze_body(
            font_stack=["a" * 64]))
        self.assertEqual(r.status_code, 404)

    def test_save_rerun_export(self):
        body = self._analyze_body(text="ABC مرحبا")
        r = self.client.post("/api/analyze?save=true&title=t1", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["review_id"]

        rec = self.client.get(f"/api/reviews/{rid}").json()
        self.assertEqual(rec["title"], "t1")
        # 固定的字体摘要
        self.assertEqual(rec["font_summaries"][0]["sha256"], self.sans)
        self.assertIn("num_glyphs", rec["font_summaries"][0])

        rr = self.client.post(f"/api/reviews/{rid}/rerun").json()
        self.assertTrue(rr["consistent"], rr["differences"])
        self.assertEqual(rr["font_drift"], [])
        self.assertEqual(rr["differences"], [])

        exp = self.client.get(f"/api/reviews/{rid}/export")
        self.assertEqual(exp.status_code, 200)
        self.assertIn("attachment", exp.headers["content-disposition"])
        report = exp.json()
        self.assertEqual(report["schema_version"], "1.0")
        self.assertEqual(report["request_parameters"]["text"], "ABC مرحبا")
        self.assertEqual(report["font_summaries"][0]["sha256"], self.sans)
        self.assertIn("clusters", report["result"])

    def test_glyph_svg(self):
        r = self.client.get(f"/api/fonts/{self.sans}/glyph/5042.svg")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<svg", r.content)
        self.assertIn(b"<path", r.content)


if __name__ == "__main__":
    unittest.main()
