"""BODY-TENDENCIAS-KPI-MINI-1: /trends' compact KPI strip -- rulings 1-2 (the four metric tiles
against their own card's band, and the strength study's "this week" tile). Invented demo data
only, no real health figures.
"""
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, goals, identity, web
from body.rutas import comun
from body.rutas import tendencias
from tests._rutas import contar_sql

PERSON_A = 1


def _conn(case: unittest.TestCase):
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _fill_metric(conn, person_id: int, key: str, hoy: date, value: float, n: int = 20) -> None:
    for i in range(n):
        d = hoy - timedelta(n - 1 - i)
        db.set_metric(conn, d.isoformat(), key, value, "garmin", person_id)


def _habit(conn, person_id: int, title: str, *, times: int = 3) -> int:
    gid = goals.propose(conn, person_id, f"Goal for {title}", "2026-09-01", kind="metric",
                        metric_key="resting_hr", direction="lower", target_value=50.0, baseline_value=60.0)
    return goals.add_habit(conn, gid, title, {"per": "week", "times": times}, active=True)


def _sesion_fuerza(conn, person_id: int, day: date, idx: int = 1) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, 'strength_training', "
        "'Fuerza', 2400, NULL, 115, 140, 240, 'garmin')",
        (person_id, f"kpi-test-{day.isoformat()}-{idx}", day.isoformat(), f"{day.isoformat()}T18:00:00"),
    )


# ---------------------------------------------------------------- pure functions, no DB needed

class KpiBandaTest(unittest.TestCase):
    def test_below_the_band(self):
        self.assertEqual(tendencias._kpi_banda(10.0, 20.0, 30.0), "abajo")

    def test_above_the_band(self):
        self.assertEqual(tendencias._kpi_banda(40.0, 20.0, 30.0), "arriba")

    def test_inside_the_band(self):
        self.assertEqual(tendencias._kpi_banda(25.0, 20.0, 30.0), "dentro")

    def test_boundary_values_count_as_inside(self):
        self.assertEqual(tendencias._kpi_banda(20.0, 20.0, 30.0), "dentro")
        self.assertEqual(tendencias._kpi_banda(30.0, 20.0, 30.0), "dentro")

    def test_no_figure_means_no_banda(self):
        self.assertIsNone(tendencias._kpi_banda(None, 20.0, 30.0))


class KpiEstadoTest(unittest.TestCase):
    def test_dentro_is_the_positive_word_in_both_languages(self):
        self.assertEqual(tendencias._kpi_estado(comun.T("es"), "dentro"), ("en tu rango", "good"))
        self.assertEqual(tendencias._kpi_estado(comun.T("en"), "dentro"), ("in your range", "good"))

    def test_arriba_is_neutral_text_in_both_languages(self):
        self.assertEqual(tendencias._kpi_estado(comun.T("es"), "arriba"), ("por encima de tu rango", "flat"))
        self.assertEqual(tendencias._kpi_estado(comun.T("en"), "arriba"), ("above your range", "flat"))

    def test_abajo_is_neutral_text_in_both_languages(self):
        self.assertEqual(tendencias._kpi_estado(comun.T("es"), "abajo"), ("por debajo de tu rango", "flat"))
        self.assertEqual(tendencias._kpi_estado(comun.T("en"), "abajo"), ("below your range", "flat"))

    def test_no_banda_means_no_status_text(self):
        self.assertEqual(tendencias._kpi_estado(comun.T("es"), None), (None, ""))


class KpiFuerzaFaltanTxtTest(unittest.TestCase):
    def test_singular_es(self):
        self.assertEqual(tendencias._kpi_fuerza_faltan_txt(comun.T("es"), 1), "falta 1")

    def test_singular_en(self):
        self.assertEqual(tendencias._kpi_fuerza_faltan_txt(comun.T("en"), 1), "1 to go")

    def test_plural_es(self):
        self.assertEqual(tendencias._kpi_fuerza_faltan_txt(comun.T("es"), 3), "faltan 3")

    def test_plural_en(self):
        self.assertEqual(tendencias._kpi_fuerza_faltan_txt(comun.T("en"), 3), "3 to go")


class FuerzaMiniTest(unittest.TestCase):
    """_fuerza_mini's own status rule (ruling 2), driven directly -- a Monday gives the most days
    left in the week (7, today included), a Sunday the fewest (1)."""

    def test_no_floor_means_no_tile(self):
        self.assertIsNone(tendencias._fuerza_mini(comun.T("es"), None, 2, date(2026, 11, 9)))

    def test_reached_when_this_weeks_count_already_meets_the_floor(self):
        tile = tendencias._fuerza_mini(comun.T("es"), 3, 3, date(2026, 11, 9))
        self.assertEqual(tile["estado"], "cumplida")
        self.assertEqual(tile["cls"], "good")
        self.assertEqual(tile["valor"], "3 de 3")

    def test_reached_when_this_weeks_count_exceeds_the_floor(self):
        tile = tendencias._fuerza_mini(comun.T("en"), 3, 4, date(2026, 11, 9))
        self.assertEqual(tile["estado"], "reached")

    def test_on_track_with_enough_days_left_es(self):
        # Monday: 7 days left including today, deficit of 2 -- plenty of room.
        tile = tendencias._fuerza_mini(comun.T("es"), 4, 2, date(2026, 11, 9))
        self.assertEqual(tile["estado"], "vas bien")
        self.assertEqual(tile["cls"], "good")
        self.assertEqual(tile["valor"], "2 de 4")

    def test_on_track_in_english(self):
        tile = tendencias._fuerza_mini(comun.T("en"), 4, 2, date(2026, 11, 9))
        self.assertEqual(tile["estado"], "on track")
        self.assertEqual(tile["valor"], "2 of 4")

    def test_boundary_days_left_equal_to_the_deficit_is_still_on_track(self):
        # Sunday: 1 day left including today; deficit of 1 -- still achievable today.
        tile = tendencias._fuerza_mini(comun.T("es"), 3, 2, date(2026, 11, 15))
        self.assertEqual(tile["estado"], "vas bien")

    def test_to_go_when_too_few_days_remain_es(self):
        # Sunday: 1 day left including today; deficit of 3 -- not achievable this week.
        tile = tendencias._fuerza_mini(comun.T("es"), 3, 0, date(2026, 11, 15))
        self.assertEqual(tile["estado"], "faltan 3")
        self.assertEqual(tile["cls"], "flat")

    def test_to_go_when_too_few_days_remain_en(self):
        tile = tendencias._fuerza_mini(comun.T("en"), 3, 0, date(2026, 11, 15))
        self.assertEqual(tile["estado"], "3 to go")

    def test_anchor_and_absent_unit(self):
        tile = tendencias._fuerza_mini(comun.T("es"), 4, 2, date(2026, 11, 9))
        self.assertEqual(tile["anchor"], "racha")
        self.assertEqual(tile["unidad"], "")
        self.assertFalse(tile["est"])


# ---------------------------------------------------------------------------- the rendered page

class _PageTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.hoy = date(2026, 11, 9)  # a Monday
        self.addCleanup(web.app.dependency_overrides.clear)
        _as(PERSON_A)
        self.client = TestClient(web.app, follow_redirects=False)
        patcher = mock.patch.object(comun, "_today_local", return_value=self.hoy)
        patcher.start()
        self.addCleanup(patcher.stop)


class FullStripTest(_PageTestCase):
    """The approved mock's own shape: four metrics squarely inside their own band, plus a
    strength tile that is "on track" -- exactly BODY-MOCK-R2-CENSO-1-INV's censused numbers
    (58 ms, 53 bpm, 7 h 05, 24, 2 of 4)."""

    def setUp(self):
        super().setUp()
        _fill_metric(self.conn, PERSON_A, "hrv_last_night", self.hoy, 58.0)
        _fill_metric(self.conn, PERSON_A, "resting_hr", self.hoy, 53.0)
        _fill_metric(self.conn, PERSON_A, "sleep_minutes", self.hoy, 425.0)  # 7 h 05
        _fill_metric(self.conn, PERSON_A, "stress_avg", self.hoy, 24.0)
        self.habit_id = _habit(self.conn, PERSON_A, "Entrenar fuerza", times=4)
        db.set_racha_piso(self.conn, PERSON_A, self.habit_id)
        _sesion_fuerza(self.conn, PERSON_A, self.hoy, idx=1)
        _sesion_fuerza(self.conn, PERSON_A, self.hoy, idx=2)
        self.conn.commit()

    def test_all_five_anchors_present_in_order(self):
        r = self.client.get("/trends")
        self.assertEqual(r.status_code, 200)
        text = r.text
        anchors = ["hrv_last_night", "resting_hr", "sleep_minutes", "stress_avg", "racha"]
        positions = []
        for a in anchors:
            self.assertIn(f'href="#{a}"', text)
            positions.append(text.index(f'href="#{a}"'))
        self.assertEqual(positions, sorted(positions))

    def test_anchors_resolve_to_an_id_on_the_page(self):
        r = self.client.get("/trends")
        text = r.text
        for a in ("hrv_last_night", "resting_hr", "sleep_minutes", "stress_avg", "racha"):
            self.assertIn(f'id="{a}"', text)

    def test_labels(self):
        r = self.client.get("/trends")
        text = r.text
        self.assertIn("Variabilidad", text)
        self.assertIn("Pulso en reposo", text)
        self.assertIn("Fuerza esta semana", text)

    def test_figures_match_the_cards_own_figure(self):
        r = self.client.get("/trends")
        text = r.text
        # The tile and the full card each carry one "fig" span with the same number.
        self.assertEqual(len(re.findall(r'class="fig">58<', text)), 2)
        self.assertEqual(len(re.findall(r'class="fig">53<', text)), 2)
        self.assertEqual(len(re.findall(r'class="fig">24<', text)), 2)
        self.assertEqual(len(re.findall(r'class="fig">7 h 05<', text)), 2)

    def test_all_four_metrics_read_in_your_range(self):
        r = self.client.get("/trends")
        self.assertEqual(r.text.count("en tu rango"), 4)

    def test_strength_tile_reads_on_track_with_the_weeks_count(self):
        r = self.client.get("/trends")
        text = r.text
        self.assertIn("2 de 4", text)
        self.assertIn("vas bien", text)

    def test_english(self):
        db.set_language(self.conn, "en", PERSON_A)
        self.conn.commit()
        r = self.client.get("/trends")
        text = r.text
        self.assertIn("Heart-rate variability", text)
        self.assertIn("Resting pulse", text)
        self.assertIn("Strength this week", text)
        self.assertIn("2 of 4", text)
        self.assertIn("on track", text)
        self.assertEqual(text.count("in your range"), 4)


class TileAbsentWithoutItsOwnCardTest(_PageTestCase):
    """Ruling 1: a tile shows only when its own metric's card exists on the page."""

    def setUp(self):
        super().setUp()
        _fill_metric(self.conn, PERSON_A, "hrv_last_night", self.hoy, 58.0)
        _fill_metric(self.conn, PERSON_A, "resting_hr", self.hoy, 53.0)
        _fill_metric(self.conn, PERSON_A, "sleep_minutes", self.hoy, 425.0)
        # stress_avg never seeded -- its card (and tile) must be absent.
        self.conn.commit()

    def test_present_metrics_show_absent_one_does_not(self):
        r = self.client.get("/trends")
        text = r.text
        self.assertIn('href="#hrv_last_night"', text)
        self.assertIn('href="#resting_hr"', text)
        self.assertIn('href="#sleep_minutes"', text)
        self.assertNotIn('href="#stress_avg"', text)


class StrengthTileAbsentWithoutAFloorTest(_PageTestCase):
    """Ruling 2: a strength study with no floor chosen gets no strength tile."""

    def setUp(self):
        super().setUp()
        _fill_metric(self.conn, PERSON_A, "hrv_last_night", self.hoy, 58.0)
        _sesion_fuerza(self.conn, PERSON_A, self.hoy, idx=1)
        self.conn.commit()

    def test_no_racha_anchor_in_the_strip(self):
        r = self.client.get("/trends")
        text = r.text
        self.assertIn('href="#hrv_last_night"', text)
        self.assertNotIn('href="#racha"', text)


class StrengthTileAbsentWithoutAnyStudyTest(_PageTestCase):
    """No strength session at all -- the study card itself is absent, so the strip's fifth tile
    cannot be either."""

    def setUp(self):
        super().setUp()
        _fill_metric(self.conn, PERSON_A, "hrv_last_night", self.hoy, 58.0)
        self.habit_id = _habit(self.conn, PERSON_A, "Entrenar fuerza", times=4)
        db.set_racha_piso(self.conn, PERSON_A, self.habit_id)
        self.conn.commit()

    def test_no_racha_anchor(self):
        r = self.client.get("/trends")
        self.assertNotIn('href="#racha"', r.text)


class StrengthTileToGoWordingTest(_PageTestCase):
    """A floor too far out of reach this week reads "faltan k" / "k to go", never an alarm colour
    class -- same seeding style as tests/test_tendencias_racha.py's own CardWithFloorTest."""

    def setUp(self):
        super().setUp()
        self.hoy = date(2026, 11, 15)  # a Sunday: 1 day left including today
        patcher = mock.patch.object(comun, "_today_local", return_value=self.hoy)
        patcher.start()
        self.addCleanup(patcher.stop)
        _fill_metric(self.conn, PERSON_A, "hrv_last_night", self.hoy, 58.0)
        self.habit_id = _habit(self.conn, PERSON_A, "Entrenar fuerza", times=3)
        db.set_racha_piso(self.conn, PERSON_A, self.habit_id)
        # One strength session, ten weeks ago -- some history to exist, none this week.
        _sesion_fuerza(self.conn, PERSON_A, self.hoy - timedelta(70), idx=1)
        self.conn.commit()

    def test_to_go_wording_present(self):
        r = self.client.get("/trends")
        self.assertIn("faltan 3", r.text)


# --------------------------------------------------------------------- statement count (BODY-WEB)

class StripAddsNoSqlStatementTest(unittest.TestCase):
    """ZERO new SQL on /trends (the spec's own ceiling rule): /trends' statement count must be the
    same whether the strip's fifth tile shows or not -- this slice adds no `conn.execute` call of
    its own, only Python over data the route already read."""

    def test_statement_count_identical_with_and_without_the_strength_tile(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "rendimiento.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.ensure_ready(conn)  # made ready BEFORE either request is measured
        # BODY-DEMO-HOY-FIJO-1: pinned -- /trends reads the real clock (comun._today_local), and
        # this test's own statement-count comparison must not depend on which real day it runs on.
        anchor = date(2026, 11, 20)
        patcher = mock.patch.object(comun, "_today_local", return_value=anchor)
        patcher.start()
        self.addCleanup(patcher.stop)
        demo._seed(conn, dias=30, idioma="es", today=anchor)  # seeds hrv/resting_hr/sleep/stress and a floor
        conn.commit()
        self.addCleanup(web.app.dependency_overrides.clear)
        _as(PERSON_A)
        client = TestClient(web.app, follow_redirects=False)

        with contar_sql() as con_piso:
            r1 = client.get("/trends")
        self.assertEqual(r1.status_code, 200)
        self.assertIn('href="#racha"', r1.text)  # the strip's fifth tile is showing

        db.set_racha_piso(conn, PERSON_A, None)
        conn.commit()
        with contar_sql() as sin_piso:
            r2 = client.get("/trends")
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn('href="#racha"', r2.text)  # now it is not

        self.assertEqual(con_piso["statements"], sin_piso["statements"],
                         f"statement count changed with the strip's own fifth tile: "
                         f"{con_piso['statements']} -> {sin_piso['statements']}")


if __name__ == "__main__":
    unittest.main()
