"""BODY-STATIC-VERSION-1: static URLs carry a content hash, and the service worker's cache name
follows it, so a CSS/JS change reaches a phone that already cached the old one."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, web
from body.rutas import comun

client = TestClient(web.app, follow_redirects=False)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "body" / "templates"


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "g.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


class StaticUrlTest(unittest.TestCase):
    def test_rendered_page_links_hashed_stylesheet(self):
        _reset_db(self)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/static/body.css?v={comun.STATIC_HASH}", r.text)

    def test_static_url_helper(self):
        self.assertEqual(comun.static_url("body.css"), f"/static/body.css?v={comun.STATIC_HASH}")


class NoLiteralVersionInTemplatesTest(unittest.TestCase):
    def test_no_template_has_a_hand_written_query_version(self):
        for path in TEMPLATES_DIR.glob("*.html"):
            self.assertNotIn("?v=", path.read_text(encoding="utf-8"), f"{path.name} has a hand-written ?v=")


class ServiceWorkerTest(unittest.TestCase):
    def test_cache_name_carries_hash_and_response_is_not_cached(self):
        r = client.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"body-static-{comun.STATIC_HASH}", r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-cache")


class StaticHashChangesWithContentTest(unittest.TestCase):
    def test_changing_a_file_changes_the_hash(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.txt").write_bytes(b"one")
            h1 = comun._static_hash(d)
            (d / "a.txt").write_bytes(b"two")
            h2 = comun._static_hash(d)
            self.assertNotEqual(h1, h2)

    def test_same_content_gives_the_same_hash(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            d1, d2 = Path(d1), Path(d2)
            (d1 / "a.txt").write_bytes(b"same")
            (d2 / "a.txt").write_bytes(b"same")
            self.assertEqual(comun._static_hash(d1), comun._static_hash(d2))


if __name__ == "__main__":
    unittest.main()
