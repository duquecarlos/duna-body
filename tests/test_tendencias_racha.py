"""BODY-TENDENCIAS-RACHA-GRAFICO-1: /trends' strength-session study card -- rulings 2-3 (the
card, its chart and figures, the "Compare with" picker) and the /trends/racha-piso POST route.
Invented demo data only, no real health figures.
"""
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, entreno_racha, fechas, goals, identity, web
from body.rutas import comun

PERSON_A = 1
PERSON_B = 2


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


def _fill_metrics(conn, person_id: int, hoy: date, n: int = 20) -> None:
    for i in range(n):
        d = hoy - timedelta(n - 1 - i)
        db.set_metric(conn, d.isoformat(), "resting_hr", 55.0, "garmin", person_id)


def _habit(conn, person_id: int, title: str, *, per: str = "week", times: int = 3,
          active: bool = True, assignee_person_id: int | None = None) -> int:
    gid = goals.propose(conn, person_id, f"Goal for {title}", "2026-09-01", kind="metric",
                        metric_key="resting_hr", direction="lower", target_value=50.0, baseline_value=60.0)
    return goals.add_habit(conn, gid, title, {"per": per, "times": times}, active=active,
                           assignee_person_id=assignee_person_id)


def _sesion_fuerza(conn, person_id: int, day: date, idx: int = 1) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, 'strength_training', "
        "'Fuerza', 2400, NULL, 115, 140, 240, 'garmin')",
        (person_id, f"racha-test-{day.isoformat()}-{idx}", day.isoformat(), f"{day.isoformat()}T18:00:00"),
    )


def _racha_de_tres_semanas(conn, person_id: int, hasta_lunes: date) -> None:
    """3 consecutive weeks, each with 3 strength sessions, ending two weeks before hasta_lunes --
    a clean "longest run" with no current run: both the current week and the week right before it
    (racha_semanal's own "still open" window) are left untouched."""
    for semanas_atras in (2, 3, 4):
        lunes = hasta_lunes - timedelta(7 * semanas_atras)
        for offset in (0, 2, 4):
            _sesion_fuerza(conn, person_id, lunes + timedelta(offset))


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


class CardAbsentWithoutStrengthSessionsTest(_PageTestCase):
    def test_card_absent(self):
        _fill_metrics(self.conn, PERSON_A, self.hoy)
        self.conn.commit()
        r = self.client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('name="habit_id"', r.text)


class CardWithFloorTest(_PageTestCase):
    def setUp(self):
        super().setUp()
        _fill_metrics(self.conn, PERSON_A, self.hoy)
        self.habit_id = _habit(self.conn, PERSON_A, "Entrenar fuerza")
        db.set_racha_piso(self.conn, PERSON_A, self.habit_id)
        _racha_de_tres_semanas(self.conn, PERSON_A, fechas.lunes_de(self.hoy))
        self.conn.commit()
        self.resultado = entreno_racha.racha_semanal(self.conn, PERSON_A, piso=3, hoy=self.hoy)

    def test_card_present_with_the_month_year_title(self):
        r = self.client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn("cerrando la brecha", r.text)

    def test_days_from_watch_history_sub_line(self):
        r = self.client.get("/trends")
        self.assertIn(f"{self.resultado['dias_historia']} días de todo el historial", r.text)

    def test_floor_note_present_with_the_floor_number(self):
        r = self.client.get("/trends")
        self.assertIn("Verde cuando llegó a 3. La línea punteada es el piso de tu plan.", r.text)

    def test_night_shift_note_absent_when_the_person_does_not_track_shifts(self):
        r = self.client.get("/trends")
        self.assertNotIn("Marcas azules", r.text)

    def test_longest_run_figure_present(self):
        r = self.client.get("/trends")
        self.assertIn("3 semanas", r.text)
        self.assertIn("tu racha más larga en 3 o más", r.text)

    def test_no_current_run_figure_when_there_is_none(self):
        r = self.client.get("/trends")
        self.assertNotIn("tu racha actual", r.text)

    def test_dashed_floor_line_present(self):
        r = self.client.get("/trends")
        self.assertIn('stroke-dasharray="4 4"', r.text)

    def test_bar_count_matches_the_non_zero_week_count(self):
        r = self.client.get("/trends")
        bars = re.findall(r'<rect[^>]*class="c-(?:body|other)"', r.text)
        non_zero = sum(1 for s in self.resultado["semanas"] if s["sesiones"] > 0)
        self.assertEqual(len(bars), non_zero)

    def test_no_style_attribute_anywhere_on_the_page(self):
        r = self.client.get("/trends")
        self.assertNotRegex(r.text, r"\sstyle\s*=")

    def test_no_current_run_implies_actual_is_none(self):
        self.assertIsNone(self.resultado["actual"])
        self.assertIsNotNone(self.resultado["mas_larga"])


class CardWithoutFloorTest(_PageTestCase):
    def setUp(self):
        super().setUp()
        _fill_metrics(self.conn, PERSON_A, self.hoy)
        _racha_de_tres_semanas(self.conn, PERSON_A, fechas.lunes_de(self.hoy))
        self.conn.commit()

    def test_card_present_with_no_floor_note(self):
        r = self.client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn("cerrando la brecha", r.text)
        self.assertNotIn("La línea punteada", r.text)

    def test_no_dashed_line_without_a_floor(self):
        r = self.client.get("/trends")
        self.assertNotIn('stroke-dasharray="4 4"', r.text)

    def test_no_figures_without_a_floor(self):
        r = self.client.get("/trends")
        self.assertNotIn("tu racha más larga", r.text)
        self.assertNotIn("tu racha actual", r.text)


class CardEnglishTest(_PageTestCase):
    def setUp(self):
        super().setUp()
        db.set_language(self.conn, "en", PERSON_A)
        _fill_metrics(self.conn, PERSON_A, self.hoy)
        self.habit_id = _habit(self.conn, PERSON_A, "Strength training")
        db.set_racha_piso(self.conn, PERSON_A, self.habit_id)
        _racha_de_tres_semanas(self.conn, PERSON_A, fechas.lunes_de(self.hoy))
        self.conn.commit()

    def test_english_title_and_floor_note(self):
        r = self.client.get("/trends")
        self.assertIn("closing the gap", r.text)
        self.assertIn("Green when it reached 3. The dashed line is the floor of your plan.", r.text)

    def test_english_longest_run_figure(self):
        r = self.client.get("/trends")
        self.assertIn("3 weeks", r.text)
        self.assertIn("your longest run at 3 or more", r.text)


class CardNightShiftFigureTest(_PageTestCase):
    """Ruling 3: with shifts on and at least one FINISHED run, the "N of M runs ended in a
    night-shift week" figure appears; it never appears for the current (still-open) run."""

    def setUp(self):
        super().setUp()
        _fill_metrics(self.conn, PERSON_A, self.hoy)
        self.habit_id = _habit(self.conn, PERSON_A, "Entrenar fuerza")
        db.set_racha_piso(self.conn, PERSON_A, self.habit_id)
        hasta = fechas.lunes_de(self.hoy)
        _racha_de_tres_semanas(self.conn, PERSON_A, hasta)
        # A night shift the week right after the run ends (so it counts as "terminated in a
        # night-shift week" -- entreno_racha.racha_semanal's own own rule).
        semana_noche = hasta - timedelta(7)
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, 'night', 'manual')",
            (PERSON_A, semana_noche.isoformat()),
        )
        db.set_trabaja_turnos(self.conn, True, PERSON_A)
        self.conn.commit()

    def test_night_shift_note_present(self):
        r = self.client.get("/trends")
        self.assertIn("Marcas azules: semanas con turno de noche.", r.text)

    def test_terminated_in_night_figure_present(self):
        r = self.client.get("/trends")
        self.assertIn("1 de 1", r.text)
        self.assertIn("rachas terminaron en una semana de turno de noche", r.text)


class PickerOptionsTest(_PageTestCase):
    def setUp(self):
        super().setUp()
        _fill_metrics(self.conn, PERSON_A, self.hoy)
        _racha_de_tres_semanas(self.conn, PERSON_A, fechas.lunes_de(self.hoy))
        self.active_weekly = _habit(self.conn, PERSON_A, "Entrenar fuerza")
        self.inactive_weekly = _habit(self.conn, PERSON_A, "Habito inactivo", active=False)
        self.daily = _habit(self.conn, PERSON_A, "Habito diario", per="day", times=1)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        self.conn.commit()
        self.other_assignee = _habit(self.conn, PERSON_A, "Habito de otra persona",
                                     assignee_person_id=PERSON_B)
        self.other_persons = _habit(self.conn, PERSON_B, "Habito de B")

    def test_active_weekly_habit_is_offered(self):
        r = self.client.get("/trends")
        self.assertIn("Entrenar fuerza · 3 por semana", r.text)

    def test_inactive_habit_is_not_offered(self):
        r = self.client.get("/trends")
        self.assertNotIn("Habito inactivo", r.text)

    def test_daily_habit_is_not_offered(self):
        r = self.client.get("/trends")
        self.assertNotIn("Habito diario", r.text)

    def test_assigned_to_another_person_is_not_offered(self):
        r = self.client.get("/trends")
        self.assertNotIn("Habito de otra persona", r.text)

    def test_another_persons_habit_is_not_offered(self):
        r = self.client.get("/trends")
        self.assertNotIn("Habito de B", r.text)

    def test_no_floor_option_is_selected_by_default(self):
        r = self.client.get("/trends")
        self.assertRegex(r.text, r'<option value=""\s+selected>Sin piso</option>')


class PostRachaPisoTest(_PageTestCase):
    def setUp(self):
        super().setUp()
        _fill_metrics(self.conn, PERSON_A, self.hoy)
        _racha_de_tres_semanas(self.conn, PERSON_A, fechas.lunes_de(self.hoy))
        self.active_weekly = _habit(self.conn, PERSON_A, "Entrenar fuerza")
        self.inactive_weekly = _habit(self.conn, PERSON_A, "Inactivo", active=False)
        self.daily = _habit(self.conn, PERSON_A, "Diario", per="day", times=1)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        self.conn.commit()
        self.other_persons = _habit(self.conn, PERSON_B, "Habito de B")

    def test_sets_the_floor_and_keeps_the_range(self):
        r = self.client.post("/trends/racha-piso", data={"habit_id": str(self.active_weekly), "r": "30"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/trends?r=30")
        self.assertEqual(db.racha_piso_habit_id(self.conn, PERSON_A), self.active_weekly)

    def test_default_range_drops_the_query(self):
        r = self.client.post("/trends/racha-piso", data={"habit_id": str(self.active_weekly), "r": "all"})
        self.assertEqual(r.headers["location"], "/trends")

    def test_empty_habit_id_clears_the_floor(self):
        db.set_racha_piso(self.conn, PERSON_A, self.active_weekly)
        self.conn.commit()
        r = self.client.post("/trends/racha-piso", data={"habit_id": ""})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(db.racha_piso_habit_id(self.conn, PERSON_A))

    def test_refuses_an_inactive_habit(self):
        r = self.client.post("/trends/racha-piso", data={"habit_id": str(self.inactive_weekly)})
        self.assertEqual(r.status_code, 404)

    def test_refuses_a_daily_habit(self):
        r = self.client.post("/trends/racha-piso", data={"habit_id": str(self.daily)})
        self.assertEqual(r.status_code, 404)

    def test_refuses_another_persons_habit(self):
        r = self.client.post("/trends/racha-piso", data={"habit_id": str(self.other_persons)})
        self.assertEqual(r.status_code, 404)

    def test_refuses_a_non_numeric_id(self):
        r = self.client.post("/trends/racha-piso", data={"habit_id": "not-a-number"})
        self.assertEqual(r.status_code, 404)

    def test_a_refused_pick_leaves_the_floor_unchanged(self):
        db.set_racha_piso(self.conn, PERSON_A, self.active_weekly)
        self.conn.commit()
        self.client.post("/trends/racha-piso", data={"habit_id": str(self.inactive_weekly)})
        self.assertEqual(db.racha_piso_habit_id(self.conn, PERSON_A), self.active_weekly)


if __name__ == "__main__":
    unittest.main()
