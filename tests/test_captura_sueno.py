"""body/captura_sueno.py: BODY-NOCHE-SIN-REM-1 -- zero REM reads as a capture gap only on a night
long enough for at least two full sleep cycles; a short night, a missing REM figure, or REM above
zero all read as None alike. Invented values only, no real figures."""
from tests._isolate import TMP  # noqa: F401  (must come before any body import)

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import captura_sueno, config, db, identity, web

_STAGE_BLOCK = re.compile(r'<svg class="stage".*?</div>', re.DOTALL)


class TestEvaluar(unittest.TestCase):
    def test_zero_rem_six_hour_night_is_marked(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 240, "sleep_rem_min": 0, "sleep_awake_min": 30}
        out = captura_sueno.evaluar(vals)
        self.assertEqual(out, {"rem_no_capturado": True, "version": captura_sueno.VERSION})

    def test_zero_rem_two_hour_night_is_none(self):
        vals = {"sleep_deep_min": 60, "sleep_light_min": 60, "sleep_rem_min": 0, "sleep_awake_min": 10}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_rem_missing_key_is_none(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 240, "sleep_awake_min": 30}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_rem_none_value_is_none(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 240, "sleep_rem_min": None, "sleep_awake_min": 30}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_rem_one_minute_is_none(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 239, "sleep_rem_min": 1, "sleep_awake_min": 30}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_awake_minutes_do_not_count_toward_minimum(self):
        vals = {"sleep_deep_min": 50, "sleep_light_min": 50, "sleep_rem_min": 0, "sleep_awake_min": 400}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_rem_above_zero_is_none_even_on_a_long_night(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 240, "sleep_rem_min": 5, "sleep_awake_min": 30}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_exact_minimum_is_marked(self):
        vals = {"sleep_deep_min": 100, "sleep_light_min": 80, "sleep_rem_min": 0, "sleep_awake_min": 0}
        out = captura_sueno.evaluar(vals)
        self.assertEqual(out, {"rem_no_capturado": True, "version": captura_sueno.VERSION})

    def test_one_minute_under_minimum_is_none(self):
        vals = {"sleep_deep_min": 100, "sleep_light_min": 79, "sleep_rem_min": 0, "sleep_awake_min": 0}
        self.assertIsNone(captura_sueno.evaluar(vals))


class TestModelo(unittest.TestCase):
    def test_modelo_is_versioned_and_keys_are_named(self):
        self.assertEqual(captura_sueno.MODELO["version"], captura_sueno.VERSION)
        self.assertEqual(set(captura_sueno.MODELO), {"version", "rem_minimo_noche_min"})
        self.assertEqual(captura_sueno.MODELO["rem_minimo_noche_min"], 180)


def _web_conn(case: unittest.TestCase, name: str):
    """A connection the web app's own routes read -- same shape as tests/test_sueno_fc.py's own
    `_web_conn`, repeated here rather than imported (that module is not one this slice touches)."""
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    case.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    web.app.dependency_overrides[identity.current_person_id] = lambda: 1
    return conn


def _seed_noche(conn, day: str, deep: float, light: float, rem: float, awake: float) -> None:
    db.set_metric(conn, day, "sleep_minutes", deep + light + rem + awake, "garmin", 1)
    db.set_metric(conn, day, "sleep_deep_min", deep, "garmin", 1)
    db.set_metric(conn, day, "sleep_light_min", light, "garmin", 1)
    db.set_metric(conn, day, "sleep_rem_min", rem, "garmin", 1)
    db.set_metric(conn, day, "sleep_awake_min", awake, "garmin", 1)


class DiaMarkedNightTest(unittest.TestCase):
    """BODY-NOCHE-SIN-REM-1: a marked night's own Día page keeps the other stages, draws no
    zero-width REM segment, and carries the one muted line, in es and en."""

    def test_marked_night_shows_the_line_in_spanish(self):
        conn = _web_conn(self, "dia_marcada_es.db")
        _seed_noche(conn, "2026-11-10", deep=120.0, light=240.0, rem=0.0, awake=30.0)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        page = client.get("/day/2026-11-10").text
        self.assertIn("Tu reloj no registró fase REM esta noche.", page)
        bloque = _STAGE_BLOCK.search(page)
        self.assertIsNotNone(bloque)
        self.assertNotIn('class="c-body"', bloque.group())  # REM's own fixed class (hoy.py STAGES)

    def test_marked_night_shows_the_line_in_english(self):
        conn = _web_conn(self, "dia_marcada_en.db")
        db.set_language(conn, "en", 1)
        _seed_noche(conn, "2026-11-10", deep=120.0, light=240.0, rem=0.0, awake=30.0)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        page = client.get("/day/2026-11-10").text
        self.assertIn("Your watch recorded no REM sleep this night.", page)
        bloque = _STAGE_BLOCK.search(page)
        self.assertIsNotNone(bloque)
        self.assertNotIn('class="c-body"', bloque.group())

    def test_unmarked_night_stage_block_is_byte_identical_to_captura_forced_none(self):
        conn = _web_conn(self, "dia_normal.db")
        _seed_noche(conn, "2026-11-10", deep=120.0, light=240.0, rem=60.0, awake=30.0)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        page_real = client.get("/day/2026-11-10").text
        with mock.patch.object(captura_sueno, "evaluar", return_value=None):
            page_forced = client.get("/day/2026-11-10").text
        bloque_real = _STAGE_BLOCK.search(page_real)
        bloque_forzado = _STAGE_BLOCK.search(page_forced)
        self.assertIsNotNone(bloque_real)
        self.assertIsNotNone(bloque_forzado)
        self.assertEqual(bloque_real.group(), bloque_forzado.group())
        self.assertNotIn("rem_no_capturado", page_real)
        self.assertNotIn("Tu reloj no registró fase REM esta noche.", page_real)


if __name__ == "__main__":
    unittest.main()
