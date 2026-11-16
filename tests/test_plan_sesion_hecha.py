"""BODY-PLAN-SESION-HECHA-1: a `plan` row the person marked done, with exercises
(`ejercicios_json`), counts as one strength session on its day -- ruling 1 -- but only when no
measured session is already counted that day -- ruling 2 -- and never for a cardio measure, since
a plan row with no exercises cannot say what was done. The union lives in
`entreno_racha.con_plan_hecho`, folded into `horizonte._recopilar_datos`'s own `fuerza_con_plan`
(ruling 3), so `horizonte.semana` (and every caller of it -- `plan_vs_real`, `resumen`, Today's own
plan card) sees it without any change of its own. Invented demo data only."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db, entreno_racha, goals, horizonte

HOY = date(2026, 11, 12)  # Thursday inside week 2026-11-09..15
MONDAY = "2026-11-09"

_EJERCICIOS = [{"ejercicio": "sentadilla", "series": 3, "repeticiones": 8}]


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    case.addCleanup(conn.close)
    db.init(conn)  # seeds person id 1 ('Me', DEFAULT_PERSON_ID)
    return conn


def _horizonte(conn, person_id: int = 1) -> int:
    return goals.propose(conn, person_id, "Plan de prueba", "2026-10-05", kind="milestone",
                         deadline="2027-01-04")


def _plan(conn, person_id, day, title, status, *, ejercicios=None, goal_id=None):
    conn.execute(
        "INSERT INTO plan (person_id, goal_id, day, title, status, ejercicios_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (person_id, goal_id, day, title, status,
         json.dumps(ejercicios, ensure_ascii=False) if ejercicios is not None else None),
    )
    conn.commit()


def _activity(conn, person_id, source_id, day, *, type="strength_training"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, type, source) VALUES (?, ?, ?, ?, 'garmin')",
        (person_id, source_id, day, type),
    )
    conn.commit()


def _real_fuerza(conn, horizon, person_id=1, week_start=MONDAY, hoy=HOY) -> dict:
    db.crear_meta_semanal(conn, person_id, horizon, week_start, "sesiones_fuerza", "al_menos", 1)
    s = horizonte.semana(conn, horizon, person_id, week_start, hoy=hoy)
    return s["lineas"][0]


class DonePlanRowCountsAsAStrengthSessionTest(unittest.TestCase):
    """Ruling 1: a done plan row with exercises, alone, counts once."""

    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_done_with_exercises_alone_counts_one(self):
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza", "done", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 1)
        self.assertEqual(linea["estado"], "cumple")

    def test_done_without_exercises_counts_zero(self):
        _plan(self.conn, 1, "2026-11-10", "Caminata", "done")
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 0)

    def test_skipped_with_exercises_counts_zero(self):
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza", "skipped", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 0)

    def test_proposed_with_exercises_counts_zero(self):
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza", "proposed", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 0)

    def test_accepted_with_exercises_counts_zero(self):
        # Not yet done -- the plan's word is only "done", never "accepted" or "proposed".
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza", "accepted", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 0)

    def test_the_demo_horizons_hecho_row_now_counted(self):
        # The exact shape BODY-PLAN-PANTALLA-1's demo horizon seeds for its Tuesday session (a
        # done plan row titled "Entrenamiento de fuerza", BODY-ENTRENO-SCHEMA-1's own
        # ejercicios_json column) -- the gap this slice closes.
        _plan(self.conn, 1, "2026-11-10", "Entrenamiento de fuerza", "done", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 1)


class MeasuredSessionWinsTest(unittest.TestCase):
    """Ruling 1: never twice -- a day with a measured session plus a done plan row still counts
    exactly the measured count."""

    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_measured_plus_done_plan_same_day_counts_once_not_twice(self):
        _activity(self.conn, 1, "a1", "2026-11-10")
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza", "done", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 1)

    def test_two_measured_plus_done_plan_same_day_counts_the_measured_two(self):
        _activity(self.conn, 1, "a1", "2026-11-10")
        _activity(self.conn, 1, "a2", "2026-11-10")
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza", "done", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 2)

    def test_two_done_plan_rows_same_day_no_measured_session_counts_two(self):
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza A", "done", ejercicios=_EJERCICIOS)
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza B", "done", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 2)

    def test_done_plan_on_a_different_day_adds_its_own_count(self):
        _activity(self.conn, 1, "a1", "2026-11-10")
        _plan(self.conn, 1, "2026-11-11", "Sesión de fuerza", "done", ejercicios=_EJERCICIOS)
        linea = _real_fuerza(self.conn, self.horizon)
        self.assertEqual(linea["real"], 2)


class CardioStaysMeasuredOnlyTest(unittest.TestCase):
    """Ruling 2: a done plan row never counts toward a cardio measure, exercises or not."""

    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_done_plan_with_exercises_does_not_count_as_cardio(self):
        _plan(self.conn, 1, "2026-11-10", "Sesión de fuerza", "done", ejercicios=_EJERCICIOS)
        db.crear_meta_semanal(self.conn, 1, self.horizon, MONDAY, "sesiones_cardio", "al_menos", 1)
        s = horizonte.semana(self.conn, self.horizon, 1, MONDAY, hoy=HOY)
        self.assertEqual(s["lineas"][0]["real"], 0)


class ConPlanHechoUnitTest(unittest.TestCase):
    """Ruling 1/2, directly on `entreno_racha.con_plan_hecho`: pure union, no database."""

    def test_empty_plan_days_returns_sessions_unchanged(self):
        sesiones = [{"day": date(2026, 11, 10), "start_local": None}]
        self.assertEqual(entreno_racha.con_plan_hecho(sesiones, []), sesiones)

    def test_plan_day_with_no_measured_session_is_added(self):
        out = entreno_racha.con_plan_hecho([], [date(2026, 11, 10)])
        self.assertEqual([o["day"] for o in out], [date(2026, 11, 10)])

    def test_plan_day_already_measured_adds_nothing(self):
        sesiones = [{"day": date(2026, 11, 10), "start_local": "2026-11-10T07:00:00"}]
        out = entreno_racha.con_plan_hecho(sesiones, [date(2026, 11, 10)])
        self.assertEqual(out, sesiones)

    def test_two_plan_days_same_day_both_added_when_unmeasured(self):
        out = entreno_racha.con_plan_hecho([], [date(2026, 11, 10), date(2026, 11, 10)])
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
