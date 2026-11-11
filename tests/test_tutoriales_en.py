"""BODY-TUTORIALES-EN-1 ruling 1: Today's "First steps" and Ayuda show tutorial titles in the
person's own language -- Spanish by default, English when the person set English. Invented demo
data only, no real health figures.
"""
import os
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, web


def _fresh_client(case: unittest.TestCase) -> tuple:
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return TestClient(web.app, follow_redirects=False), conn


class _LocalModeTestCase(unittest.TestCase):
    """Local mode (no Cuenta sign-in configured): the default person, no gates."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.client, self.conn = _fresh_client(self)
        self.assertNotIn(cuenta.ISSUER_ENV, os.environ)


class AyudaLanguageTest(_LocalModeTestCase):
    def test_spanish_titles_by_default(self):
        r = self.client.get("/ayuda")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Primeros pasos", r.text)
        self.assertIn("Iniciar sesión y aceptar", r.text)
        self.assertIn("Conectar tu Garmin", r.text)

    def test_english_titles_when_the_person_set_english(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        r = self.client.get("/ayuda")
        self.assertEqual(r.status_code, 200)
        self.assertIn("First steps", r.text)
        self.assertIn("Sign in and accept", r.text)
        self.assertIn("Connect your Garmin", r.text)
        self.assertNotIn("Iniciar sesión y aceptar", r.text)
        self.assertNotIn("Primeros pasos", r.text)


class TodayFirstStepsLanguageTest(_LocalModeTestCase):
    """A fresh install has no day of data yet (soon.html), which still renders the First steps card
    -- "conectar-garmin" is not done, so the card has an unticked item and shows."""

    def test_spanish_titles_by_default(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Iniciar sesión y aceptar", r.text)
        self.assertIn("Conectar tu Garmin", r.text)

    def test_english_titles_when_the_person_set_english(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sign in and accept", r.text)
        self.assertIn("Connect your Garmin", r.text)
        self.assertNotIn("Iniciar sesión y aceptar", r.text)


if __name__ == "__main__":
    unittest.main()
