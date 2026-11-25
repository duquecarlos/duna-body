"""BODY-PLAN-HOY-1: Today's own "Tu plan esta semana" card -- ruling 1's shared plan definition,
ruling 2's card content (week position, estado counts, the honest gap, the next checkpoint within
7 days, PLANES_HOY_MAX and "more"), ruling 3's bounded cost. Invented demo data only, built through
goals.propose/activate and db.py's own horizonte_* writers."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, web
from body.rutas import comun, hoy

client = TestClient(web.app, follow_redirects=False)

HOY = date(2026, 11, 12)  # a Thursday: 4 days remain (Sunday inclusive) in the current week
MONDAY_CURRENT = date(2026, 11, 9)
START_DAY = date(2026, 10, 19).isoformat()  # 3 weeks before the current Monday
DEADLINE = date(2026, 12, 6).isoformat()    # a few weeks after


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "planhoy.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    # One daily_metric row so "/" passes comun._latest_day's early "no data at all" return.
    db.set_metric(conn, HOY.isoformat(), "sleep_minutes", 400, "garmin", 1)
    conn.commit()
    return conn


def _get(path: str = "/", hoy: date = HOY):
    with mock.patch.object(comun, "_today_local", return_value=hoy):
        return client.get(path)


def _horizonte(conn, person_id: int = 1, titulo: str = "ZZ Horizonte", start_day: str = START_DAY,
               deadline: str = DEADLINE) -> int:
    return goals.propose(conn, person_id, titulo, start_day, kind="milestone", deadline=deadline)


def _activity(conn, day: str, type_: str, source_id: str, duration_s: float = 1800.0, person_id: int = 1):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, duration_s, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'garmin')",
        (person_id, source_id, day, f"{day}T07:00:00", type_, duration_s))
    conn.commit()


class CardAbsentTest(unittest.TestCase):
    def test_no_plan_at_all(self):
        _reset_db(self)
        self.assertNotIn("Tu plan esta semana", _get().text)

    def test_plain_milestone_with_no_plan_row_has_no_card(self):
        conn = _reset_db(self)
        gid = _horizonte(conn)
        goals.activate(conn, gid)
        conn.commit()
        self.assertNotIn("Tu plan esta semana", _get().text)

    def test_a_proposed_but_not_activated_plan_is_absent(self):
        conn = _reset_db(self)
        gid = _horizonte(conn)
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        conn.commit()
        self.assertNotIn("Tu plan esta semana", _get().text)

    def test_a_plan_that_has_not_started_yet_is_absent(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, start_day="2026-12-01", deadline="2027-02-01")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        goals.activate(conn, gid)
        conn.commit()
        self.assertNotIn("Tu plan esta semana", _get().text)

    def test_a_plan_that_already_ended_is_absent(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, start_day="2026-01-05", deadline="2026-02-01")
        db.crear_meta_semanal(conn, 1, gid, "2026-01-05", "sesiones_fuerza", "al_menos", 1.0)
        goals.activate(conn, gid)
        conn.commit()
        self.assertNotIn("Tu plan esta semana", _get().text)

    def test_another_persons_plan_never_shows(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        conn.commit()
        gid = _horizonte(conn, person_id=2)
        db.crear_meta_semanal(conn, 2, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        goals.activate(conn, gid)
        conn.commit()
        self.assertNotIn("Tu plan esta semana", _get().text)


class CardPresentTest(unittest.TestCase):
    def test_active_plan_shows_title_and_week_position_and_days_left(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Plan activo")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("Tu plan esta semana", page)
        self.assertIn("ZZ Plan activo", page)
        self.assertIn(f"/goals/{gid}", page)
        self.assertIn("Semana 4 de 7", page)  # 2026-10-19 Mon .. 2026-12-06 (Monday 2026-11-30), hoy in the 4th
        self.assertIn("faltan 24 días", page)  # (2026-12-06 - 2026-11-12).days

    def test_ends_today_text(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, start_day="2026-11-02", deadline=HOY.isoformat())
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        goals.activate(conn, gid)
        conn.commit()
        self.assertIn("termina hoy", _get().text)

    def test_english_coverage(self):
        conn = _reset_db(self)
        conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        gid = _horizonte(conn, titulo="ZZ Active plan")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        _activity(conn, MONDAY_CURRENT.isoformat(), "strength_training", "en1")
        goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("Your plan this week", page)
        self.assertIn("ZZ Active plan", page)
        self.assertIn("Week 4 of 7", page)
        self.assertIn("24 days left", page)
        self.assertIn("1 met", page)


class FaseLineTest(unittest.TestCase):
    """BODY-PLAN-FASES-2 ruling 4: Hoy's plan line names the current phase, only when one covers
    today -- "Fase: <titulo> · semana <n> de <m>" (es) / "Phase: <titulo> · week <n> of <m>" (en)."""

    def test_phase_covering_today_shows_its_own_week_count(self):
        conn = _reset_db(self)
        gid = _horizonte(conn)
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        db.crear_fase_horizonte(conn, 1, gid, "ZZ Fase pico", "2026-11-09", "2026-11-22")
        goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("Fase: ZZ Fase pico · semana 1 de 2", page)

    def test_no_phase_covering_today_shows_no_fase_line(self):
        conn = _reset_db(self)
        gid = _horizonte(conn)
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        db.crear_fase_horizonte(conn, 1, gid, "ZZ Fase pasada", "2026-10-19", "2026-10-25")
        goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("Tu plan esta semana", page)
        self.assertNotIn("Fase:", page)

    def test_plan_with_no_phases_at_all_shows_no_fase_line(self):
        conn = _reset_db(self)
        gid = _horizonte(conn)
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        goals.activate(conn, gid)
        conn.commit()
        self.assertNotIn("Fase:", _get().text)

    def test_english_coverage(self):
        conn = _reset_db(self)
        conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        gid = _horizonte(conn)
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        db.crear_fase_horizonte(conn, 1, gid, "ZZ Peak phase", "2026-11-09", "2026-11-22")
        goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("Phase: ZZ Peak phase · week 1 of 2", page)


class EstadoCountsTest(unittest.TestCase):
    """Ruling 2: this week's weekly lines as counts by estado, singular via the repo's `_one`
    convention -- one of each estado, deterministically: a count-type line already reached (cumple),
    one still reachable with days left (en_curso), one unreachable in the days left (no_cumple), and
    a mean-type line with no readings at all this week (sin_dato)."""

    def test_one_of_each_estado(self):
        conn = _reset_db(self)
        gid = _horizonte(conn)
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        _activity(conn, MONDAY_CURRENT.isoformat(), "strength_training", "a1")  # cumple
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_cardio", "al_menos", 3.0)
        # en_curso: 0 done, gap 3 <= dias_restantes (4)
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "hora_acostarse", "antes_de", 660.0)
        # sin_dato: no sleep_session rows at all this week
        goals.activate(conn, gid)
        conn.commit()
        # A second horizon carries the no_cumple line (one weekly target line per medida per
        # horizon per week is the schema's own rule -- a second horizon keeps this fixture simple).
        gid2 = _horizonte(conn, titulo="ZZ Horizonte dos")
        db.crear_meta_semanal(conn, 1, gid2, MONDAY_CURRENT.isoformat(), "sesiones_cardio", "al_menos", 10.0)
        goals.activate(conn, gid2)
        conn.commit()

        page = _get().text
        self.assertIn("1 cumplida", page)
        self.assertIn("1 en curso", page)
        self.assertIn("1 sin dato", page)
        self.assertIn("1 no cumplida", page)

    def test_plural_forms(self):
        conn = _reset_db(self)
        gid = _horizonte(conn)
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        _activity(conn, MONDAY_CURRENT.isoformat(), "strength_training", "a1")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_cardio", "al_menos", 1.0)
        _activity(conn, MONDAY_CURRENT.isoformat(), "running", "a2", duration_s=1800.0)
        goals.activate(conn, gid)
        conn.commit()
        self.assertIn("2 cumplidas", _get().text)


class HonestGapTest(unittest.TestCase):
    def test_a_week_with_no_weekly_lines_shows_the_gap(self):
        conn = _reset_db(self)
        gid = _horizonte(conn)
        # ruling: a plain milestone with no plan row at all is not a horizon -- this one needs
        # SOME plan row to qualify, but not necessarily a weekly line for the CURRENT week.
        db.crear_regla_horizonte(conn, 1, gid, "siempre", None, "Texto de la regla")
        goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("Tu plan esta semana", page)
        self.assertIn("Sin metas del plan esta semana", page)


class CheckpointTest(unittest.TestCase):
    def test_checkpoint_within_seven_days_is_shown(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Con chequeo")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        db.crear_checkpoint_horizonte(
            conn, 1, gid, "2026-11-19", "ZZ Chequeo cercano",
            [{"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 3.0, "ventana": "semana"}])
        goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("Chequeo el 19 nov: ZZ Chequeo cercano", page)

    def test_checkpoint_beyond_seven_days_is_not_shown(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Sin chequeo cercano")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
        db.crear_checkpoint_horizonte(
            conn, 1, gid, "2026-11-20", "ZZ Chequeo lejano",
            [{"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 3.0, "ventana": "semana"}])
        goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("Tu plan esta semana", page)
        self.assertNotIn("ZZ Chequeo lejano", page)


class PlanesHoyMaxTest(unittest.TestCase):
    def test_more_than_the_max_collapses_into_one_link(self):
        conn = _reset_db(self)
        for i in range(3):
            gid = _horizonte(conn, titulo=f"ZZ Plan {i}")
            db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
            goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("ZZ Plan 2", page)  # newest first
        self.assertIn("ZZ Plan 1", page)
        self.assertNotIn("ZZ Plan 0", page)  # beyond PLANES_HOY_MAX (2)
        self.assertIn("y 1 plan más", page)
        self.assertIn('href="/goals"', page)

    def test_at_most_the_max_has_no_more_link(self):
        conn = _reset_db(self)
        for i in range(2):
            gid = _horizonte(conn, titulo=f"ZZ Plan {i}")
            db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 1.0)
            goals.activate(conn, gid)
        conn.commit()
        page = _get().text
        self.assertIn("ZZ Plan 0", page)
        self.assertIn("ZZ Plan 1", page)
        self.assertNotIn("planes más", page)
        self.assertNotIn("plan más", page)


def _plan_hoy_row(conn, day: str = "2026-11-12") -> None:
    conn.execute(
        "INSERT INTO plan (person_id, day, time, title, status, ejercicios_json) VALUES "
        "(1, ?, '15:30', 'Pecho', 'accepted', '[{\"ejercicio\":\"press banca\",\"series\":4,"
        "\"categoria\":\"pecho\"}]')", (day,))
    conn.commit()


class DiaPorDelanteSenalesTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6) ruling 3: Hoy's own planned-session line
    carries the same one-line reason as the proposal card, only when a session is planned AND a
    signal applies."""

    def test_reentrada_shows_on_the_planned_session_line(self):
        conn = _reset_db(self)
        _plan_hoy_row(conn)
        _activity(conn, "2026-11-05", "strength_training", "a0")  # 7 days before HOY
        page = _get().text
        self.assertIn("Vuelta tras 7 días sin fuerza: sesión 1, volumen al 90 %.", page)

    def test_tras_carga_shows_on_the_planned_session_line(self):
        conn = _reset_db(self)
        _plan_hoy_row(conn)
        for i, (day, dur) in enumerate([("2026-11-01", 600.0), ("2026-11-02", 700.0), ("2026-11-03", 800.0)]):
            conn.execute(
                "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
                "VALUES (1, ?, ?, ?, 'other', 'Yoga', ?, 'garmin')", (f"bg{i}", day, f"{day}T07:00:00", dur))
        conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
            "VALUES (1, 'basket', '2026-11-11', '2026-11-11T18:00:00', 'other', 'Baloncesto', 3000.0, 'garmin')")
        conn.commit()
        page = _get().text
        self.assertIn("Ayer: Baloncesto; hoy sin carga pesada de pierna.", page)

    def test_no_line_when_neither_signal_applies(self):
        conn = _reset_db(self)
        _plan_hoy_row(conn)
        page = _get().text
        self.assertNotIn("Vuelta tras", page)
        self.assertNotIn("hoy sin carga pesada", page)

    def test_no_line_when_no_session_is_planned(self):
        conn = _reset_db(self)
        _activity(conn, "2026-11-05", "strength_training", "a0")
        page = _get().text
        self.assertNotIn("Vuelta tras", page)


class PlanHoyDoneDropsOffRenderTest(unittest.TestCase):
    """BODY-REVISION-DIA-PERIODO-1 ruling 1, rendered end-to-end through GET / -- the same three
    states tests/test_hoy.py's own PlanHoyDoneDropsOffTest proves on the helper directly."""

    def test_done_and_past_its_time_is_gone_from_the_page(self):
        conn = _reset_db(self)
        conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status, ejercicios_json) VALUES "
            "(1, ?, '15:30', 'Pecho', 'done', '[{\"ejercicio\":\"press banca\",\"series\":4}]')", (HOY.isoformat(),))
        conn.commit()
        with mock.patch.object(hoy, "_hhmm_ahora", return_value="16:00"):
            page = _get().text
        self.assertNotIn("Pecho", page)

    def test_done_but_not_past_its_time_shows_checked_and_muted(self):
        conn = _reset_db(self)
        conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status, ejercicios_json) VALUES "
            "(1, ?, '15:30', 'Pecho', 'done', '[{\"ejercicio\":\"press banca\",\"series\":4}]')", (HOY.isoformat(),))
        conn.commit()
        with mock.patch.object(hoy, "_hhmm_ahora", return_value="10:00"):
            page = _get().text
        self.assertIn("Pecho", page)
        self.assertIn('class="block c-muted"', page)
        self.assertIn("Hecho", page)

    def test_not_done_and_past_its_time_is_marked_earlier_today(self):
        conn = _reset_db(self)
        conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status, ejercicios_json) VALUES "
            "(1, ?, '15:30', 'Pecho', 'accepted', '[{\"ejercicio\":\"press banca\",\"series\":4}]')",
            (HOY.isoformat(),))
        conn.commit()
        with mock.patch.object(hoy, "_hhmm_ahora", return_value="16:00"):
            page = _get().text
        self.assertIn("Pecho", page)
        self.assertIn("Antes de hoy", page)
        self.assertNotIn("Hecho", page)


if __name__ == "__main__":
    unittest.main()
