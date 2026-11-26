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
from body.rutas import tendencias
from tests._rutas import contar_sql

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


def _semanas_atras(conn, person_id: int, hasta_lunes: date, desde: int, hasta: int) -> None:
    """One strength session a week, for every week from `desde` to `hasta` weeks before
    hasta_lunes (exclusive of `hasta`) -- a night block every 8th week, so the history this
    builds carries night-shift weeks too (BODY-TENDENCIAS-RACHA-GRAFICO-1-M2 step 2's own guard)."""
    for i in range(desde, hasta):
        lunes = hasta_lunes - timedelta(7 * i)
        _sesion_fuerza(conn, person_id, lunes)
        if i % 8 == 0:
            conn.execute(
                "INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, 'night', 'manual')",
                (person_id, (lunes + timedelta(2)).isoformat()))


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

    def test_days_since_first_activity_sub_line(self):
        """BODY-REVISION-GRAFICOS-FIGURA-1 finding 11b: the wording names exactly what
        dias_historia counts (a calendar span since the first recorded activity), never the
        "all of your watch's history" claim that implied the SAME count as Trends' top "days
        with data" -- the two are honestly different measures (one's a span that can include
        gaps, the other a count of days actually synced), so they say so instead of matching by
        coincidence or never matching at all."""
        r = self.client.get("/trends")
        self.assertIn(f"{self.resultado['dias_historia']} días desde tu primera actividad registrada", r.text)

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


class TrendsStatementCountIndependentOfHistoryLengthTest(_PageTestCase):
    """BODY-TENDENCIAS-RACHA-GRAFICO-1-M2 step 2: the whole /trends route, including the card's
    reads (entreno_racha.racha_semanal and _piso_y_opciones), must cost the same number of
    statements whether the watch history behind it is a few weeks or two years -- the regression
    the route's own ceiling (tests/test_rendimiento.py) exists to catch."""

    def test_trends_statements_equal_for_a_short_and_a_long_history(self):
        db.ensure_ready(self.conn)  # made ready BEFORE either request is measured, as
                                    # tests/test_rendimiento.py's own _reset_db does
        _fill_metrics(self.conn, PERSON_A, self.hoy)
        habit_id = _habit(self.conn, PERSON_A, "Entrenar fuerza")
        db.set_racha_piso(self.conn, PERSON_A, habit_id)
        # An explicit "no" (db.trabaja_turnos_si's own NULL-infers-True rule otherwise flips on
        # once a shift row exists) keeps Turnos' own, unrelated extra reads out of this
        # comparison -- this guard is about racha_semanal's and _piso_y_opciones' own cost, not
        # turnos_costo.perfiles_patron's.
        db.set_trabaja_turnos(self.conn, False, PERSON_A)
        hasta_lunes = fechas.lunes_de(self.hoy)
        _semanas_atras(self.conn, PERSON_A, hasta_lunes, 1, 4)
        self.conn.commit()
        with contar_sql() as corta:
            r1 = self.client.get("/trends")
        self.assertEqual(r1.status_code, 200)

        _semanas_atras(self.conn, PERSON_A, hasta_lunes, 4, 105)  # two more years of weekly history
        self.conn.commit()
        with contar_sql() as larga:
            r2 = self.client.get("/trends")
        self.assertEqual(r2.status_code, 200)

        self.assertEqual(corta["statements"], larga["statements"],
                         f"statement count grew with history length: {corta['statements']} -> "
                         f"{larga['statements']}")


class RachaRangoMesesTest(unittest.TestCase):
    """BODY-TENDENCIAS-RACHA-GRAFICO-1-M2 step 5: the three range shapes a figure's own month
    range can take, es and en."""

    def test_same_month_names_it_once_es(self):
        self.assertEqual(tendencias._racha_rango_meses(comun.T("es"), date(2026, 10, 5), date(2026, 10, 25)),
                         "oct 2026")

    def test_same_month_names_it_once_en(self):
        self.assertEqual(tendencias._racha_rango_meses(comun.T("en"), date(2026, 10, 5), date(2026, 10, 25)),
                         "Oct 2026")

    def test_same_year_different_months_year_once_at_the_end_es(self):
        self.assertEqual(tendencias._racha_rango_meses(comun.T("es"), date(2026, 10, 12), date(2026, 11, 1)),
                         "oct–nov 2026")

    def test_same_year_different_months_year_once_at_the_end_en(self):
        self.assertEqual(tendencias._racha_rango_meses(comun.T("en"), date(2026, 10, 12), date(2026, 11, 1)),
                         "Oct–Nov 2026")

    def test_different_years_year_on_both_es(self):
        self.assertEqual(tendencias._racha_rango_meses(comun.T("es"), date(2025, 12, 1), date(2026, 2, 5)),
                         "dic 2025–feb 2026")

    def test_different_years_year_on_both_en(self):
        self.assertEqual(tendencias._racha_rango_meses(comun.T("en"), date(2025, 12, 1), date(2026, 2, 5)),
                         "Dec 2025–Feb 2026")


class RachaEjeTest(unittest.TestCase):
    """BODY-TENDENCIAS-RACHA-GRAFICO-1-M2 step 5's own axis tests: at most 4 labels, every label
    inside the viewBox by the character-width approximation, none overlapping, and the year rule
    (the first label and every January carry it; every other label is the bare month name)."""

    def _semanas(self, desde: date, n: int) -> list[dict]:
        return [{"lunes": (desde + timedelta(7 * i)).isoformat(), "sesiones": 0, "noche": False,
                 "alcanza": None} for i in range(n)]

    def _revisar(self, etiquetas: list[dict]):
        self.assertLessEqual(len(etiquetas), 4)
        for e in etiquetas:
            mitad = tendencias._racha_label_px(e["texto"]) / 2
            izq = e["x"] - mitad if e["anchor"] == "middle" else (
                e["x"] if e["anchor"] == "start" else e["x"] - 2 * mitad)
            der = izq + 2 * mitad
            self.assertGreaterEqual(izq, -0.01, e)
            self.assertLessEqual(der, tendencias._RACHA_WIDTH + 0.01, e)
        for a, b in zip(etiquetas, etiquetas[1:]):
            mitad_a = tendencias._racha_label_px(a["texto"]) / 2
            mitad_b = tendencias._racha_label_px(b["texto"]) / 2
            self.assertLessEqual(a["x"] + mitad_a + tendencias._RACHA_LABEL_GAP_X, b["x"] - mitad_b + 0.01,
                                 f"{a['texto']!r} and {b['texto']!r} are closer than the minimum gap")

    def test_few_weeks_one_label_with_year(self):
        semanas = self._semanas(date(2026, 10, 5), 3)
        etiquetas = tendencias._racha_eje(comun.T("es"), semanas, tendencias._RACHA_WIDTH / len(semanas))
        self.assertEqual([e["texto"] for e in etiquetas], ["oct 2026"])
        self._revisar(etiquetas)

    def test_two_years_weekly_at_most_four_labels_no_overlap(self):
        semanas = self._semanas(date(2024, 11, 4), 104)
        etiquetas = tendencias._racha_eje(comun.T("es"), semanas, tendencias._RACHA_WIDTH / len(semanas))
        self.assertLessEqual(len(etiquetas), 4)
        self.assertGreaterEqual(len(etiquetas), 2)
        self._revisar(etiquetas)

    def test_first_label_and_every_january_carry_the_year(self):
        # Month starts: 2024-11-04 (Nov, the first label), 2024-12-02 (Dec, not first nor
        # January -- bare month name), 2025-01-06 (Jan -- carries the year even though it is
        # neither the first label). Three month-starts, all shown (<= 4).
        semanas = self._semanas(date(2024, 11, 4), 13)
        etiquetas = tendencias._racha_eje(comun.T("es"), semanas, tendencias._RACHA_WIDTH / len(semanas))
        textos = [e["texto"] for e in etiquetas]
        self.assertEqual(textos, ["nov 2024", "dic", "ene 2025"])
        self._revisar(etiquetas)

    def test_edge_labels_anchor_away_from_the_edge(self):
        # Many weeks packs the first week's own x close enough to the viewBox's left edge that a
        # middle-anchored label would run off it, so the first label anchors "start" instead.
        semanas = self._semanas(date(2024, 11, 4), 104)
        etiquetas = tendencias._racha_eje(comun.T("es"), semanas, tendencias._RACHA_WIDTH / len(semanas))
        self.assertEqual(etiquetas[0]["anchor"], "start")
        self._revisar(etiquetas)

    def test_last_label_survives_even_when_the_gap_cannot_be_met(self):
        # Only two month-starts, one week apart (BODY-TENDENCIAS-RACHA-GRAFICO-1-M3): the naive
        # greedy scan would drop the second as too close to the first, but the last label is never
        # the one dropped -- both are kept even though their boxes end up closer than the 8-unit
        # floor, because there is no middle label left to drop instead.
        semanas = self._semanas(date(2025, 12, 29), 5)
        etiquetas = tendencias._racha_eje(comun.T("es"), semanas, tendencias._RACHA_WIDTH / len(semanas))
        self.assertEqual([e["texto"] for e in etiquetas], ["dic 2025", "ene 2026"])

    def test_a_conflicting_last_label_pops_the_middle_one_instead(self):
        # Three month-starts: Nov (first, 25 weeks), a single Dec week (middle), then Jan (last,
        # 24 weeks) -- built directly rather than via _semanas so the middle month is just one
        # index wide. Jan's box lands too close to Dec's, so Dec -- the middle label -- is the one
        # dropped, and Nov and Jan (first and last) both survive with the full gap between them.
        semanas = (
            [{"lunes": date(2025, 11, 3).isoformat(), "sesiones": 0, "noche": False, "alcanza": None}] * 25
            + [{"lunes": date(2025, 12, 29).isoformat(), "sesiones": 0, "noche": False, "alcanza": None}]
            + [{"lunes": date(2026, 1, 5).isoformat(), "sesiones": 0, "noche": False, "alcanza": None}] * 24
        )
        etiquetas = tendencias._racha_eje(comun.T("es"), semanas, tendencias._RACHA_WIDTH / len(semanas))
        self.assertEqual([e["texto"] for e in etiquetas], ["nov 2025", "ene 2026"])
        self._revisar(etiquetas)


if __name__ == "__main__":
    unittest.main()
