"""BODY-DEMO-MODO-MARCA-1: every page served from a demo home says so, with a small chip in the
layout every page shares (base.html's wrap) -- the same marker file body/demo.py writes and refuses
to overwrite a real home with (demo.MARKER_NAME), read fresh on every request, never cached and
never a second flag. A real home (no marker) renders byte-identical HTML for that region."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401,E402  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, web
from tests._rutas import contar_sql

ES_CHIP = "Demo · datos inventados"
EN_CHIP = "Demo · invented data"


def _fresh_client(tc: unittest.TestCase) -> tuple:
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)  # made ready BEFORE any route is measured (tests/test_rendimiento.py's own pattern)
    return TestClient(web.app, follow_redirects=False), conn


def _write_marker() -> None:
    (config.DB_PATH.parent / demo.MARKER_NAME).write_text("{}", encoding="utf-8")


class MarkerPresentTest(unittest.TestCase):
    def setUp(self):
        self.client, self.conn = _fresh_client(self)
        _write_marker()

    def test_chip_on_today_trends_settings_in_spanish(self):
        for path in ("/", "/trends", "/settings"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(ES_CHIP, r.text, f"{path} missing the demo chip in Spanish")

    def test_chip_on_today_trends_settings_in_english(self):
        self.client.post("/settings/language", data={"lang": "en"})
        for path in ("/", "/trends", "/settings"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(EN_CHIP, r.text, f"{path} missing the demo chip in English")

    def test_chip_carries_role_status(self):
        r = self.client.get("/")
        self.assertIn(f'role="status">{ES_CHIP}', r.text)


class MarkerAbsentTest(unittest.TestCase):
    def setUp(self):
        self.client, self.conn = _fresh_client(self)

    def test_chip_absent_on_today_trends_settings(self):
        for path in ("/", "/trends", "/settings"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn(ES_CHIP, r.text, f"{path} shows the demo chip on a real (unmarked) home")


class MarkerAddedAfterStartTest(unittest.TestCase):
    """The marker is read fresh per request (ruling 1): a home seeded while the server already runs
    carries no chip on the request before the marker exists, and the chip on the very next one."""

    def test_absent_then_present_on_next_request(self):
        client, conn = _fresh_client(self)
        before = client.get("/")
        self.assertNotIn(ES_CHIP, before.text)
        _write_marker()
        after = client.get("/")
        self.assertIn(ES_CHIP, after.text)


class GetWritesNothingTest(unittest.TestCase):
    """The chip's own check (a filesystem stat) never turns a GET into a write, with or without
    the marker present."""

    def test_no_write_with_marker(self):
        client, conn = _fresh_client(self)
        _write_marker()
        with contar_sql() as stats:
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(stats["writes"], 0)

    def test_no_write_without_marker(self):
        client, conn = _fresh_client(self)
        with contar_sql() as stats:
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(stats["writes"], 0)


class StatementCountUnchangedTest(unittest.TestCase):
    """Ruling 1: es_demo() reads the filesystem, never the database, so "/"'s own statement count
    (tests/test_rendimiento.py's own ceiling) is identical whether the marker exists or not."""

    def test_same_statement_count_with_and_without_marker(self):
        client, conn = _fresh_client(self)
        with contar_sql() as without_marker:
            r1 = client.get("/")
        _write_marker()
        with contar_sql() as with_marker:
            r2 = client.get("/")
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(without_marker["statements"], with_marker["statements"],
                          f"the demo marker changed /'s own statement count: "
                          f"{without_marker['statements']} vs {with_marker['statements']}")


if __name__ == "__main__":
    unittest.main()
