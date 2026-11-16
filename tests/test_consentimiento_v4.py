"""BODY-CONSENTIMIENTO-V4: consent widens to name each activity's full detail (heart-rate curve,
laps, zones, strength sets) and minute-by-minute nights and days. CONSENT_VERSION moves from
consentimiento-v3 to consentimiento-v4, the consent page shows a one-line "what changed" notice
to anyone who had accepted an earlier version, and body/privacidad.py gains guarda_detalle(),
the one question a per-activity-detail fetcher must ask before it stores anything. Invented demo
data only, no real health figures.
"""
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, privacidad, web
from tests.test_cuenta_rp import CuentaRpTestCase, SUB


class ConsentPageChangeNoticeTest(CuentaRpTestCase):
    def test_v3_only_hosted_person_sees_the_cambio_line(self):
        self._sign_in(return_to="/")
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id, "consentimiento-v3")
        conn.close()

        location = self.client.get("/").headers["location"]
        r = self.client.get(location)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Actualizamos lo que guardamos", r.text)

    def test_brand_new_person_sees_no_cambio_line(self):
        self._sign_in(return_to="/")
        location = self.client.get("/").headers["location"]
        r = self.client.get(location)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Actualizamos lo que guardamos", r.text)

    def test_accepting_v4_records_the_version_and_passes_the_gate(self):
        self._sign_in(return_to="/")
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id, "consentimiento-v3")
        conn.close()

        location = self.client.get("/").headers["location"]
        return_to = parse_qs(urlparse(location).query)["return_to"][0]
        r = self.client.post("/privacidad/consentimiento", data={"decision": "acepto", "return_to": return_to})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(
            self._db_count("SELECT COUNT(*) FROM consentimiento WHERE version = ?", "consentimiento-v4"), 1
        )

        r2 = self.client.get("/")
        self.assertEqual(r2.status_code, 200)


class GuardaDetalleTest(CuentaRpTestCase):
    def test_false_for_a_v3_only_hosted_person(self):
        self._sign_in()
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id, "consentimiento-v3")
        self.assertFalse(privacidad.guarda_detalle(conn, person_id))
        conn.close()

    def test_true_once_v4_is_accepted(self):
        self._sign_in()
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id, "consentimiento-v4")
        self.assertTrue(privacidad.guarda_detalle(conn, person_id))
        conn.close()


class GuardaDetalleLocalModeTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        self.client = TestClient(web.app, follow_redirects=False)

    def test_true_in_local_mode_with_no_consent_at_all(self):
        conn = db.connect()
        self.assertTrue(privacidad.guarda_detalle(conn, db.DEFAULT_PERSON_ID))
        conn.close()


if __name__ == "__main__":
    unittest.main()
