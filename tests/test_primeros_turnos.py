"""BODY-PRIMEROS-TURNOS-1: Primeros pasos' `trabajas-por-turnos` item is ticked when the answer is
known at all -- stored OR inferred from the person's own shift / shift_batch rows
(`db.trabaja_turnos_si`, BODY-TURNOS-INFERIR-1) -- never only from the stored column
(`db.get_trabaja_turnos`). Read-time only: the stored value never changes just from rendering "/".
Invented demo data only, no real health figures.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, identity, tutoriales, web


def _fresh_client(case: unittest.TestCase) -> tuple[TestClient, "db.sqlite3.Connection"]:
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return TestClient(web.app, follow_redirects=False), conn


def _insert_shift(conn, person_id: int, day: str) -> None:
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, 'night', 'manual')",
        (person_id, day),
    )
    conn.commit()


def _insert_shift_batch(conn, person_id: int) -> None:
    conn.execute(
        "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, day_count, "
        "created_at) VALUES (?, 'turnos.pdf', '2026-11-01T00:00:00', '2026-11-01T00:00:00', "
        "'proposed', 1, '2026-11-01T00:00:00')",
        (person_id,),
    )
    conn.commit()


class _HostedModeTestCase(unittest.TestCase):
    """Person resolved by overriding the dependency directly (BODY-DISPOSITIVO-1's own test
    convention), so consent is not in the way of a route that only cares about the person id."""

    def setUp(self):
        self.addCleanup(tutoriales._manifest_cache.clear)
        self.client, self.conn = _fresh_client(self)
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1


# ---- tutoriales.primeros_pasos: ticked when stored OR inferred -------------------------------------

class HechoFunctionTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def _hecho(self) -> bool:
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        return items["trabajas-por-turnos"]

    def test_null_with_shift_rows_is_hecho(self):
        _insert_shift(self.conn, 1, "2026-11-01")
        self.assertTrue(self._hecho())

    def test_null_with_shift_batch_row_only_is_hecho(self):
        _insert_shift_batch(self.conn, 1)
        self.assertTrue(self._hecho())

    def test_null_with_nothing_is_not_hecho(self):
        self.assertFalse(self._hecho())

    def test_stored_no_is_hecho(self):
        db.set_trabaja_turnos(self.conn, False, 1)
        self.assertTrue(self._hecho())

    def test_stored_si_is_hecho(self):
        db.set_trabaja_turnos(self.conn, True, 1)
        self.assertTrue(self._hecho())


# ---- "/" rendering: Sí/No only on the open item, never a write -------------------------------------

class CardRenderTest(_HostedModeTestCase):
    def test_null_with_shift_rows_renders_ticked_link_not_si_no(self):
        _insert_shift(self.conn, 1, "2026-11-01")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/ayuda#trabajas-por-turnos"', r.text)
        self.assertNotIn('action="/settings/trabaja_turnos"', r.text)

    def test_null_with_shift_batch_row_renders_ticked_link_not_si_no(self):
        _insert_shift_batch(self.conn, 1)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/ayuda#trabajas-por-turnos"', r.text)
        self.assertNotIn('action="/settings/trabaja_turnos"', r.text)

    def test_null_with_nothing_renders_si_no(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('action="/settings/trabaja_turnos"', r.text)
        self.assertIn('name="valor" value="si"', r.text)
        self.assertIn('name="valor" value="no"', r.text)

    def test_rendering_with_shift_rows_never_writes_the_stored_value(self):
        _insert_shift(self.conn, 1, "2026-11-01")
        self.assertIsNone(db.get_trabaja_turnos(self.conn, 1))
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(db.get_trabaja_turnos(self.conn, 1))


if __name__ == "__main__":
    unittest.main()
