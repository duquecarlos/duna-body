"""BODY-ENTRENO-TOOLS-1: the three training tools on the propose/apply/undo pattern --
`planear_entreno`, `registrar_entreno` and `leer_entreno` -- over the storage merged in
BODY-ENTRENO-SCHEMA-1/BODY-ENTRENO-PULL-1. No network: every Garmin call goes through
`body.pull.garmin_adapter.fetch_exercise_sets`, stubbed. Invented demo data only.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, goals
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _insert_activity(conn, person_id, source_id, day, type_="strength_training", source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?)",
        (person_id, source_id, day, f"{day}T07:00:00", type_, source),
    )


def _watch_set(exercise="press banca", reps=8.0, weight_value=50.0):
    return garmin_adapter.ExerciseSet(
        set_type="active", category="pecho", exercise=exercise, reps=reps,
        weight_value=weight_value, weight_unit="kg", duration_s=30.0, start_local="2026-11-10T07:00:00",
    )


class PlanearEntrenoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_propose_writes_nothing_to_plan(self):
        before = self.conn.execute("SELECT COUNT(*) FROM plan").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-11-10", "titulo": "Espalda y bíceps",
            "ejercicios": [{"ejercicio": "remo con barra", "series": 4, "reps": "8-10"},
                           {"ejercicio": "curl de bíceps", "series": 3}],
        }, "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plan").fetchone()[0], before)

    def test_apply_creates_the_plan_row_only_for_that_person(self):
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-11-10", "titulo": "Espalda y bíceps",
            "ejercicios": [{"ejercicio": "remo con barra", "series": 4, "reps": "8-10"}],
        }, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        rows = self.conn.execute("SELECT * FROM plan WHERE person_id = 1").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Espalda y bíceps")
        self.assertEqual(json.loads(rows[0]["ejercicios_json"])[0]["ejercicio"], "remo con barra")

    def test_undo_removes_the_plan_row(self):
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-11-10", "titulo": "Pierna",
            "ejercicios": [{"ejercicio": "sentadilla", "series": 4}],
        }, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plan WHERE person_id = 1").fetchone()[0], 0)

    def test_undo_refuses_when_the_plan_row_was_already_changed(self):
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-11-10", "titulo": "Pierna",
            "ejercicios": [{"ejercicio": "sentadilla", "series": 4}],
        }, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        plan_id = self.conn.execute("SELECT id FROM plan WHERE person_id = 1").fetchone()["id"]
        self.conn.execute("DELETE FROM plan WHERE id = ?", (plan_id,))
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_unknown_or_foreign_goal_id_is_refused(self):
        gid = goals.propose(self.conn, 2, "Meta de B", "2026-10-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-01")
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "planear_entreno", {
                "day": "2026-11-10", "titulo": "Pierna", "goal_id": gid,
                "ejercicios": [{"ejercicio": "sentadilla", "series": 4}],
            }, "ninguno", self.cid)

    def test_person_b_cannot_apply_person_as_proposal(self):
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-11-10", "titulo": "Pierna",
            "ejercicios": [{"ejercicio": "sentadilla", "series": 4}],
        }, "ninguno", self.cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])


class RegistrarEntrenoGarminTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-10")
        self.conn.commit()
        self.series = [{"ejercicio": "press banca", "reps": 10, "peso_kg": 55.0},
                       {"ejercicio": "remo", "reps": 12}]

    def test_build_fetches_series_when_not_stored_yet(self):
        data = garmin_adapter.ExerciseSetsData(activity_id="a1", sets=(_watch_set(), _watch_set(exercise="remo")))
        with mock.patch("body.pull.garmin_adapter.fetch_exercise_sets", return_value=data) as m:
            out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                        {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        m.assert_called_once_with("a1")
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        despues = json.loads(registro["despues_json"])
        self.assertFalse(despues["manual"])
        self.assertEqual(len(despues["mapeo"]), 2)
        self.assertEqual(despues["mapeo"][0]["reloj"]["ejercicio"], "press banca")
        self.assertEqual(despues["mapeo"][0]["tu"]["ejercicio"], "press banca")
        # the watch's own reading was cached, but nothing of the person's was written yet
        row = self.conn.execute(
            "SELECT garmin_exercise, person_exercise, corrected_by_person FROM workout_set "
            "WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(row["garmin_exercise"], "press banca")
        self.assertIsNone(row["person_exercise"])
        self.assertEqual(row["corrected_by_person"], 0)

    def test_build_skips_the_fetch_when_sets_are_already_stored(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(), _watch_set(exercise="remo")])
        with mock.patch("body.pull.garmin_adapter.fetch_exercise_sets") as m:
            herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                  {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        m.assert_not_called()

    def test_apply_writes_through_correct_exercise_set(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(), _watch_set(exercise="remo")])
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual(sets[0]["exercise"], "press banca")
        self.assertEqual(sets[0]["reps"], 10)
        self.assertEqual(sets[0]["weight_value"], 55.0)
        self.assertTrue(sets[0]["corrected_by_person"])
        self.assertEqual(sets[1]["exercise"], "remo")

    def test_drift_refuses_when_the_watchs_reading_changed_after_propose(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(), _watch_set(exercise="remo")])
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        db.save_exercise_sets(self.conn, 1, "garmin", "a1",
                               [_watch_set(exercise="press distinto"), _watch_set(exercise="remo")])
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        row = self.conn.execute(
            "SELECT person_exercise FROM workout_set WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertIsNone(row["person_exercise"])

    def test_undo_restores_the_set_to_uncorrected(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(), _watch_set(exercise="remo")])
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT garmin_exercise, person_exercise, corrected_by_person FROM workout_set "
            "WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(row["garmin_exercise"], "press banca")  # the watch's own value, untouched
        self.assertIsNone(row["person_exercise"])
        self.assertEqual(row["corrected_by_person"], 0)

    def test_undo_restores_a_prior_correction_exactly(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(), _watch_set(exercise="remo")])
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, exercise="press banca (antes)", reps=9)
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        row = self.conn.execute(
            "SELECT person_exercise, person_reps, corrected_by_person FROM workout_set "
            "WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(row["person_exercise"], "press banca (antes)")
        self.assertEqual(row["person_reps"], 9)
        self.assertEqual(row["corrected_by_person"], 1)

    def test_person_b_cannot_apply_person_as_proposal(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(), _watch_set(exercise="remo")])
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])


class RegistrarEntrenoManualTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.series = [{"ejercicio": "sentadilla", "reps": 10, "peso_kg": 40.0},
                       {"ejercicio": "zancada", "reps": 12}]

    def test_propose_creates_no_activity_yet(self):
        before = self.conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                              {"day": "2026-11-10", "titulo": "Pierna en casa", "series": self.series},
                              "ninguno", self.cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0], before)

    def test_apply_creates_the_manual_session_with_its_sets(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "titulo": "Pierna en casa", "series": self.series},
                                    "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        actividad = self.conn.execute(
            "SELECT source, name FROM activity WHERE person_id = 1 AND day = '2026-11-10'"
        ).fetchone()
        self.assertEqual(actividad["source"], "manual")
        self.assertEqual(actividad["name"], "Pierna en casa")
        sets = db.exercise_sets(self.conn, 1, "manual",
                                self.conn.execute(
                                    "SELECT source_id FROM activity WHERE person_id = 1"
                                ).fetchone()["source_id"])
        self.assertEqual([s["exercise"] for s in sets], ["sentadilla", "zancada"])
        self.assertTrue(all(s["corrected_by_person"] for s in sets))

    def test_undo_removes_the_manual_session_entirely(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "titulo": "Pierna en casa", "series": self.series},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity WHERE person_id = 1").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM workout_set WHERE person_id = 1").fetchone()[0], 0)

    def test_undo_refuses_when_the_session_was_corrected_again(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "titulo": "Pierna en casa", "series": self.series},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        source_id = self.conn.execute("SELECT source_id FROM activity WHERE person_id = 1").fetchone()["source_id"]
        db.correct_exercise_set(self.conn, 1, "manual", source_id, 1, exercise="sentadilla distinta")
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")


class LeerEntrenoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _insert_activity(self.conn, 1, "a1", "2026-11-10")
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(exercise="press")])
        self.conn.execute(
            "INSERT INTO plan (person_id, day, title, status, ejercicios_json) VALUES "
            "(1, '2026-11-10', 'Espalda', 'proposed', ?)",
            (json.dumps([{"ejercicio": "remo", "series": 4}]),),
        )
        self.conn.commit()

    def test_reads_the_days_session_and_plan(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-10"}, "completo")
        self.assertEqual(len(out["sesiones"]), 1)
        self.assertEqual(out["sesiones"][0]["series"][0]["ejercicio"], "press")
        self.assertEqual(len(out["planeado"]), 1)
        self.assertEqual(out["planeado"][0]["ejercicios"][0]["ejercicio"], "remo")

    def test_range_query(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno",
                                    {"desde": "2026-11-09", "hasta": "2026-11-11"}, "completo")
        self.assertEqual(len(out["sesiones"]), 1)

    def test_requires_day_or_a_range(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "leer_entreno", {}, "completo")

    def test_person_b_cannot_read_person_as_sessions(self):
        out = herramientas.ejecutar(self.conn, 2, "leer_entreno", {"day": "2026-11-10"}, "completo")
        self.assertEqual(out["sesiones"], [])
        self.assertEqual(out["planeado"], [])


if __name__ == "__main__":
    unittest.main()
