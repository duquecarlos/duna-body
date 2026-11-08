"""BODY-PATRON-ERROR-CICLO-1: `patron_turnos.validar` can raise a whole-cycle error
(`{"index": None, "code": ...}`), which `web.shifts_patron_save` used to drop silently --
the form re-rendered with no message at all. Now a whole-cycle error renders as a
form-level message, in both languages, and a per-row error still renders on its row.
Invented demo data only, no real schedules.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, patron_turnos, web
from body.i18n import T


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _fresh_conn(case: unittest.TestCase, name: str = "a.db"):
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


_VALID_DATA = {"dias": "2", "start_day": "2026-01-01", "horizon_weeks": "4",
               "kind_0": "night", "start_0": "22:00", "end_0": "06:00",
               "kind_1": "off", "start_1": "", "end_1": ""}


class WholeCycleErrorRenderTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.conn = _fresh_conn(self)
        db.set_trabaja_turnos(self.conn, True, 1)
        self.conn.commit()
        self.addCleanup(web.app.dependency_overrides.clear)
        _as(1)

    def test_whole_cycle_error_renders_message_es(self):
        with patch("body.patron_turnos.validar",
                    side_effect=patron_turnos.PatronInvalido([{"index": None, "code": "largo_invalido"}])):
            resp = self.client.post("/shifts/patron", data=_VALID_DATA)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(T("es")("patron.error.largo_invalido"), resp.text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_pattern").fetchone()[0], 0)

    def test_whole_cycle_error_renders_message_en(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        with patch("body.patron_turnos.validar",
                    side_effect=patron_turnos.PatronInvalido([{"index": None, "code": "largo_invalido"}])):
            resp = self.client.post("/shifts/patron", data=_VALID_DATA)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(T("en")("patron.error.largo_invalido"), resp.text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_pattern").fetchone()[0], 0)

    def test_per_row_error_still_renders_on_its_row(self):
        data = {"dias": "2", "start_day": "2026-01-01", "horizon_weeks": "4",
                "kind_0": "morning", "start_0": "", "end_0": "",
                "kind_1": "off", "start_1": "", "end_1": ""}
        resp = self.client.post("/shifts/patron", data=data)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(T("es")("patron.error.hora_invalida"), resp.text)
        self.assertNotIn(T("es")("patron.error.largo_invalido"), resp.text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_pattern").fetchone()[0], 0)

    def test_valid_form_still_saves_and_redirects(self):
        resp = self.client.post("/shifts/patron", data=_VALID_DATA)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/shifts")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_pattern").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
