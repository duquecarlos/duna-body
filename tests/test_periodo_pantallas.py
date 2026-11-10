"""BODY-PERIODO-PANTALLAS-1: the screens that read a marked period (BODY-PERIODO-1) say so in
words and shade it -- Hoy's readiness card, Día, Tendencias' daily charts, and Tu historia's
patterns/by_shift, which also leave marked days out the same rule baselines.baseline() already
applies. Invented data only, no real health figures.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, history, profile, web
from body.i18n import T
from body.rutas import comun

PERSON_A = 1
NOTE_SECRETO = "nota-secreta-no-debe-verse-123"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


class HoyPeriodoTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_open_period_covering_today_shows_the_line(self):
        conn = self._reset_db("hoy_abierto.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.crear_periodo(conn, PERSON_A, "malestar", (hoy - timedelta(5)).isoformat(), None, note=NOTE_SECRETO)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "periodo.kind.malestar"), r.text)
        self.assertIn(_t("es", "today.periodo_enlace"), r.text)
        self.assertIn('href="#asis-btn"', r.text)
        self.assertNotIn(NOTE_SECRETO, r.text)

    def test_period_closed_before_today_shows_no_line(self):
        conn = self._reset_db("hoy_cerrado.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.crear_periodo(conn, PERSON_A, "viaje", (hoy - timedelta(10)).isoformat(), (hoy - timedelta(2)).isoformat())
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(_t("es", "today.periodo_enlace"), r.text)

    def test_no_period_shows_no_line(self):
        conn = self._reset_db("hoy_ninguno.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(_t("es", "today.periodo_enlace"), r.text)

    def test_renders_in_english(self):
        conn = self._reset_db("hoy_en.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.crear_periodo(conn, PERSON_A, "lesion", (hoy - timedelta(1)).isoformat(), None)
        db.set_language(conn, "en", PERSON_A)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("en", "periodo.kind.lesion"), r.text)
        self.assertIn(_t("en", "today.periodo_enlace"), r.text)


class DiaPeriodoTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_marked_day_says_so_with_the_kind(self):
        conn = self._reset_db("dia_marcado.db")
        dia = date(2026, 6, 10)
        db.crear_periodo(conn, PERSON_A, "viaje", dia.isoformat(), dia.isoformat(), note=NOTE_SECRETO)
        conn.commit()
        r = self.client.get(f"/day/{dia.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "day.periodo", kind=_t("es", "periodo.kind.viaje")), r.text)
        self.assertNotIn(NOTE_SECRETO, r.text)

    def test_unmarked_day_says_nothing(self):
        conn = self._reset_db("dia_sin_marca.db")
        dia = date(2026, 6, 10)
        db.crear_periodo(conn, PERSON_A, "viaje", (dia - timedelta(5)).isoformat(), (dia - timedelta(4)).isoformat())
        conn.commit()
        r = self.client.get(f"/day/{dia.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(_t("es", "day.periodo", kind=_t("es", "periodo.kind.viaje")), r.text)


class WindowSvgMarcasTest(unittest.TestCase):
    """profile.window_svg's own shading: a marked day draws a rect distinct from a night's."""

    def test_marcas_rect_distinct_from_nights(self):
        start, end = date(2026, 1, 1), date(2026, 1, 5)
        values = {(start + timedelta(i)).isoformat(): 10.0 + i for i in range(5)}
        w = profile.Window(start, end, values, mean=12.0, prev_mean=None)
        svg = profile.window_svg(w, low=8.0, high=16.0, nights={"2026-01-02"}, marcas={"2026-01-03"})
        self.assertIn('class="c-night op-18"', svg)
        self.assertIn('class="c-amber op-35"', svg)

    def test_no_marcas_no_amber_rect(self):
        start, end = date(2026, 1, 1), date(2026, 1, 5)
        values = {(start + timedelta(i)).isoformat(): 10.0 + i for i in range(5)}
        w = profile.Window(start, end, values, mean=12.0, prev_mean=None)
        svg = profile.window_svg(w, low=8.0, high=16.0, nights={"2026-01-02"})
        self.assertNotIn("c-amber", svg)


class TrendsPeriodoRouteTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def _seed_window(self, conn, end: date, days: int = 30):
        for i in range(days):
            d = end - timedelta(days - 1 - i)
            db.set_metric(conn, d.isoformat(), "resting_hr", 55.0 + (i % 3), "garmin", PERSON_A)

    def test_legend_shown_only_when_window_has_a_marked_day(self):
        conn = self._reset_db("trends_marcado.db")
        hoy = date(2026, 6, 20)
        self._seed_window(conn, hoy)
        db.crear_periodo(conn, PERSON_A, "malestar", (hoy - timedelta(2)).isoformat(), (hoy - timedelta(1)).isoformat())
        conn.commit()
        r = self.client.get("/trends?r=7")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "trends.periodos"), r.text)
        self.assertIn('class="c-amber op-35"', r.text)

    def test_legend_absent_without_a_marked_day_in_the_window(self):
        conn = self._reset_db("trends_sin_marca.db")
        hoy = date(2026, 6, 20)
        self._seed_window(conn, hoy)
        conn.commit()
        r = self.client.get("/trends?r=7")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(_t("es", "trends.periodos"), r.text)


class PatternsExclusionTest(unittest.TestCase):
    """history.patterns() leaves a marked day out of its own weekday/month buckets, the same rule
    baselines.baseline() already applies (BODY-PERIODO-1 ruling 3)."""

    def _conn(self):
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "patrones.db")
        db.init(conn)
        return conn

    def test_marked_day_does_not_pull_its_weekday_bucket(self):
        conn = self._conn()
        saturday = date(2026, 1, 3)  # a Saturday
        for i in range(8):
            d = saturday - timedelta(7 * i)
            db.set_metric(conn, d.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        # one extreme outlier, on a day marked as unwell -- it must not move the Saturday bucket
        extremo = saturday
        db.set_metric(conn, extremo.isoformat(), "resting_hr", 999.0, "garmin", PERSON_A)
        db.crear_periodo(conn, PERSON_A, "malestar", extremo.isoformat(), extremo.isoformat())
        for key in ("hrv_last_night", "sleep_minutes", "stress_avg", "body_battery_wake"):
            for i in range(8):
                d = saturday - timedelta(7 * i)
                db.set_metric(conn, d.isoformat(), key, 50.0, "garmin", PERSON_A)
        conn.commit()
        pat = history.patterns(conn, PERSON_A)
        bucket = pat["metrics"]["resting_hr"]["weekday"][saturday.weekday()]
        self.assertIsNotNone(bucket)
        self.assertLess(bucket["center"], 100.0, "the marked outlier leaked into the weekday bucket")


class ByShiftExclusionTest(unittest.TestCase):
    """history.by_shift() leaves a marked day out of its own kind's figures and day count."""

    def _conn(self):
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "por_turno.db")
        db.init(conn)
        return conn

    def test_marked_shift_day_excluded_from_count_and_metrics(self):
        conn = self._conn()
        dias = [date(2026, 2, 1) + timedelta(i) for i in range(5)]
        for d in dias:
            conn.execute(
                "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source) "
                "VALUES (?, ?, 'night', 'N', ?, ?, 'manual')",
                (PERSON_A, d.isoformat(), f"{d.isoformat()}T22:00:00", f"{d.isoformat()}T06:00:00"),
            )
            db.set_metric(conn, d.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        marcado = dias[0]
        db.crear_periodo(conn, PERSON_A, "lesion", marcado.isoformat(), marcado.isoformat())
        conn.commit()
        result = history.by_shift(conn, PERSON_A)
        kinds = {k["kind"]: k for k in result["kinds"]}
        self.assertEqual(kinds["night"]["days"], len(dias) - 1)


class HistoryPeriodoLineTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def _seed_days(self, conn, start: date, n: int):
        for i in range(n):
            d = start + timedelta(i)
            db.set_metric(conn, d.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)

    def test_count_line_names_how_many_days_and_why(self):
        conn = self._reset_db("historia_excluida.db")
        start = date(2026, 1, 1)
        self._seed_days(conn, start, 20)
        db.crear_periodo(conn, PERSON_A, "malestar", (start + timedelta(3)).isoformat(),
                          (start + timedelta(5)).isoformat(), note=NOTE_SECRETO)
        conn.commit()
        r = self.client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "history.periodo_excluido", n="3", kinds=_t("es", "periodo.kind.malestar")), r.text)
        self.assertNotIn(NOTE_SECRETO, r.text)

    def test_no_line_without_a_marked_day(self):
        conn = self._reset_db("historia_sin_marca.db")
        start = date(2026, 1, 1)
        self._seed_days(conn, start, 20)
        conn.commit()
        r = self.client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(_t("es", "history.periodo_excluido", n="0", kinds=""), r.text)


if __name__ == "__main__":
    unittest.main()
