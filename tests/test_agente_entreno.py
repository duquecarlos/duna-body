"""BODY-ENTRENO-TOOLS-1: the three training tools on the propose/apply/undo pattern --
`planear_entreno`, `registrar_entreno` and `leer_entreno` -- over the storage merged in
BODY-ENTRENO-SCHEMA-1/BODY-ENTRENO-PULL-1. No network: every Garmin call goes through
`body.pull.garmin_adapter.fetch_exercise_sets`, stubbed. Invented demo data only.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
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

    def test_a_far_future_day_is_accepted(self):
        # BODY-AGENTE-DIA-FUTURO-1: a plan is for later, on purpose -- unlike registrar_entreno.
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2027-06-01", "titulo": "Pierna",
            "ejercicios": [{"ejercicio": "sentadilla", "series": 4}],
        }, "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")

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


def _despues(conn, propuesta_id: int) -> dict:
    return json.loads(conn.execute(
        "SELECT despues_json FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()["despues_json"])


def _seed_dia_pesado(conn, person_id: int, day_ayer: str) -> None:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6): a heavy day before `day_ayer`'s own
    tomorrow -- three short 'other' activities to give the duration tercile something to compare
    against, then a much longer one (a ball game) on `day_ayer` itself. No intraday heart-rate
    data anywhere, on purpose: `recuperacion_actividad` then reads as 'sin_cobertura' for all of
    them, so this exercises reentrada.py's own duration-tercile fallback, not its recovery-minutes
    path (already covered by tests/test_reentrada.py)."""
    for i, (day, dur) in enumerate([("2026-10-01", 600.0), ("2026-10-03", 700.0), ("2026-10-05", 800.0)]):
        conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
            "VALUES (?, ?, ?, ?, 'other', 'Yoga', ?, 'garmin')",
            (person_id, f"bg{i}", day, f"{day}T07:00:00", dur))
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
        "VALUES (?, 'basket', ?, ?, 'other', 'Baloncesto', 3000.0, 'garmin')",
        (person_id, day_ayer, f"{day_ayer}T18:00:00"))
    conn.commit()


class PlanearEntrenoSenalesTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6) rulings 1-2: a qualifying gap scales
    every exercise's own series; a heavy day before `day` drops the exercises that share its own
    loaded groups -- either way, stored structured in `despues` for the card to word."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_reentrada_escala_las_series_y_se_guarda_en_despues(self):
        _insert_activity(self.conn, 1, "a0", "2026-10-01")  # 7 days before 2026-10-08
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-10-08", "titulo": "Pecho",
            "ejercicios": [{"ejercicio": "press banca", "series": 5, "categoria": "pecho"}],
        }, "ninguno", self.cid)
        despues = _despues(self.conn, out["propuesta_id"])
        self.assertEqual(despues["ejercicios"][0]["series"], 3)  # round(5 * 0.6)
        self.assertEqual(despues["reentrada"], {"dias_sin_fuerza": 7, "sesion_n": 1, "factor": 0.6})
        self.assertNotIn("tras_carga", despues)

    def test_tras_carga_quita_los_ejercicios_de_pierna_y_deja_los_demas(self):
        _seed_dia_pesado(self.conn, 1, "2026-10-09")  # the day before 2026-10-10
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-10-10", "titulo": "Full body",
            "ejercicios": [{"ejercicio": "sentadilla", "series": 4, "categoria": "pierna"},
                           {"ejercicio": "press banca", "series": 4, "categoria": "pecho"}],
        }, "ninguno", self.cid)
        despues = _despues(self.conn, out["propuesta_id"])
        self.assertEqual([e["ejercicio"] for e in despues["ejercicios"]], ["press banca"])
        self.assertEqual(despues["tras_carga"]["actividad"], {"tipo": "other", "nombre": "Baloncesto"})
        self.assertEqual(despues["tras_carga"]["grupos"], ["cuadriceps", "isquios"])
        self.assertNotIn("reentrada", despues)

    def test_tras_carga_nunca_vacia_la_propuesta(self):
        _seed_dia_pesado(self.conn, 1, "2026-10-09")
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-10-10", "titulo": "Pierna",
            "ejercicios": [{"ejercicio": "sentadilla", "series": 4, "categoria": "pierna"}],
        }, "ninguno", self.cid)
        despues = _despues(self.conn, out["propuesta_id"])
        self.assertEqual([e["ejercicio"] for e in despues["ejercicios"]], ["sentadilla"])

    def test_registrar_entreno_un_dia_ya_pasado_nunca_se_toca(self):
        """Ruling 2: 'nothing blocks; a logged session is never altered' -- `registrar_entreno` is
        for what already happened, so it has no notion of 'a lighter session' at all. Same qualifying
        gap as the reentrada test above, but through `registrar_entreno` instead: series/reps stay
        exactly as spoken and `despues` carries neither signal key."""
        _insert_activity(self.conn, 1, "a0", "2026-10-01")  # 7 days before 2026-10-08
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
            "day": "2026-10-08", "titulo": "Pecho",
            "series": [{"ejercicio": "press banca", "reps": 5, "peso_kg": 60.0}],
        }, "ninguno", self.cid)
        despues = _despues(self.conn, out["propuesta_id"])
        self.assertEqual(despues["series"][0]["reps"], 5)
        self.assertEqual(despues["series"][0]["peso_kg"], 60.0)
        self.assertNotIn("reentrada", despues)
        self.assertNotIn("tras_carga", despues)


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


class RegistrarEntrenoRelojFalloTest(unittest.TestCase):
    """BODY-SERIES-UNIDAD-1 ruling 3: the watch's sets are a help, not a precondition. When
    pull.traer_series raises -- Garmin unreachable, a bad payload, a DB error -- the proposal is
    still built from the person's own spoken sets, mapped onto no watch set."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-10")
        self.conn.commit()
        self.series = [{"ejercicio": "press banca", "reps": 10, "peso_kg": 55.0},
                       {"ejercicio": "remo", "reps": 12}]

    def _registrar(self, side_effect):
        with mock.patch("body.pull.traer_series", side_effect=side_effect):
            return herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                        {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)

    def _despues(self, out):
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        return json.loads(registro["despues_json"])

    def test_adapter_that_raises_still_proposes_from_the_spoken_sets(self):
        out = self._registrar(RuntimeError("Garmin unreachable"))
        despues = self._despues(out)
        self.assertFalse(despues["manual"])
        self.assertTrue(despues["reloj_fallo"])
        self.assertEqual(len(despues["mapeo"]), 2)
        for m in despues["mapeo"]:
            self.assertIsNone(m["reloj"])
        self.assertEqual(despues["mapeo"][0]["tu"]["ejercicio"], "press banca")

    def test_a_db_error_from_traer_series_is_also_caught(self):
        out = self._registrar(db.sqlite3.IntegrityError("CHECK constraint failed"))
        despues = self._despues(out)
        self.assertTrue(despues["reloj_fallo"])
        for m in despues["mapeo"]:
            self.assertIsNone(m["reloj"])

    def test_the_proposal_can_still_be_applied(self):
        out = self._registrar(RuntimeError("Garmin unreachable"))
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual(sets[0]["exercise"], "press banca")
        self.assertIsNone(sets[0]["garmin"]["exercise"])  # the watch never answered


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

    def test_propose_with_no_titulo_stores_none_not_a_literal(self):
        # BODY-ENTRENO-TITULO-1: absent titulo must stay absent (None) in the proposal, never a
        # stored Spanish word -- the screen, not the record, decides how to word "no title".
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        self.assertIsNone(json.loads(registro["despues_json"])["titulo"])

    def test_propose_with_a_blank_titulo_stores_none(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "titulo": "   ", "series": self.series},
                                    "ninguno", self.cid)
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        self.assertIsNone(json.loads(registro["despues_json"])["titulo"])

    def test_propose_with_the_spoken_titulo_entrenamiento_keeps_it_literally(self):
        # A person who actually says "Entrenamiento" must not be mistaken for one who said nothing.
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "titulo": "Entrenamiento", "series": self.series},
                                    "ninguno", self.cid)
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        self.assertEqual(json.loads(registro["despues_json"])["titulo"], "Entrenamiento")

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

    def test_apply_with_no_titulo_writes_the_default_session_name_in_spanish(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        actividad = self.conn.execute(
            "SELECT name FROM activity WHERE person_id = 1 AND day = '2026-11-10'"
        ).fetchone()
        self.assertEqual(actividad["name"], "Entrenamiento")

    def test_apply_with_no_titulo_writes_the_default_session_name_in_english(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        actividad = self.conn.execute(
            "SELECT name FROM activity WHERE person_id = 1 AND day = '2026-11-10'"
        ).fetchone()
        self.assertEqual(actividad["name"], "Training")

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


class RegistrarEntrenoSegundaVezManualTest(unittest.TestCase):
    """BODY-CORREGIR-MEDIDA-ENTRENO-1 ruling 2: a second `registrar_entreno` call for a day that
    already holds a MANUAL session (source='manual', type IS NULL -- the exact pair
    `db.create_manual_session` writes) corrects that same session in place, never a duplicate
    activity."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.series = [{"ejercicio": "sentadilla", "reps": 10, "peso_kg": 40.0},
                       {"ejercicio": "zancada", "reps": 12}]

    def _registrar_y_aplicar(self, series):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "titulo": "Pierna en casa", "series": series},
                                    "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        return out

    def test_second_call_corrects_the_same_session_no_duplicate_activity(self):
        self._registrar_y_aplicar(self.series)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity WHERE person_id = 1").fetchone()[0], 1)
        source_id = self.conn.execute("SELECT source_id FROM activity WHERE person_id = 1").fetchone()["source_id"]

        segunda = [{"ejercicio": "sentadilla", "reps": 11, "peso_kg": 42.0},
                   {"ejercicio": "zancada", "reps": 14}]
        self._registrar_y_aplicar(segunda)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity WHERE person_id = 1").fetchone()[0], 1)
        mismo_source_id = self.conn.execute(
            "SELECT source_id FROM activity WHERE person_id = 1").fetchone()["source_id"]
        self.assertEqual(mismo_source_id, source_id)
        sets = db.exercise_sets(self.conn, 1, "manual", source_id)
        self.assertEqual(len(sets), 2)
        self.assertEqual([s["reps"] for s in sets], [11, 14])

    def test_undo_of_the_second_call_restores_the_first_corrections(self):
        self._registrar_y_aplicar(self.series)
        source_id = self.conn.execute("SELECT source_id FROM activity WHERE person_id = 1").fetchone()["source_id"]
        out2 = self._registrar_y_aplicar([{"ejercicio": "sentadilla", "reps": 11, "peso_kg": 42.0},
                                           {"ejercicio": "zancada", "reps": 14}])
        resultado = propuestas.deshacer(self.conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        sets = db.exercise_sets(self.conn, 1, "manual", source_id)
        self.assertEqual([s["reps"] for s in sets], [10, 12])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity WHERE person_id = 1").fetchone()[0], 1)

    def test_a_day_with_only_a_run_is_not_matched_as_manual(self):
        _insert_activity(self.conn, 1, "r1", "2026-11-10", type_="running", source="garmin")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        self.assertTrue(json.loads(registro["despues_json"])["manual"])
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        # the run stays exactly as it was -- a second, separate manual session was created instead.
        tipos = {r["type"] for r in self.conn.execute("SELECT type FROM activity WHERE person_id = 1")}
        self.assertEqual(tipos, {"running", None})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity WHERE person_id = 1").fetchone()[0], 2)


class RegistrarEntrenoFutureDayTest(unittest.TestCase):
    """BODY-AGENTE-DIA-FUTURO-1: `registrar_entreno` is "what I did" -- an actual record -- so it
    refuses a day its own person has not reached yet, checked in THEIR zone, not the server's.
    `Pacific/Kiritimati` (UTC+14) and a UTC instant late in its day are picked so the person's local
    date and the UTC date disagree, proving the check reads the person's own today."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.conn.execute("UPDATE person SET timezone = 'Pacific/Kiritimati' WHERE id = 1")
        self.conn.commit()
        self.series = [{"ejercicio": "sentadilla", "reps": 8}]

    def _patched(self, instant: datetime):
        return mock.patch("body.fechas.datetime", **{
            "now.side_effect": lambda tz=None: instant.astimezone(tz) if tz else instant,
        })

    def test_today_in_the_persons_zone_is_accepted(self):
        instant = datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc)  # already Nov 10 in Kiritimati
        with self._patched(instant):
            out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                        {"day": "2026-11-10", "series": self.series}, "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")

    def test_a_day_beyond_tolerance_in_the_persons_zone_is_refused(self):
        instant = datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc)  # local today is Nov 10
        with self._patched(instant):
            with self.assertRaises(herramientas.ToolError):
                herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                      {"day": "2026-11-12", "series": self.series}, "ninguno", self.cid)


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


class LeerEntrenoUltimaVezTest(unittest.TestCase):
    """BODY-ENTRENO-ULTIMA-VEZ-1 ruling 3: leer_entreno's own `ultima_vez` key, additive -- the
    existing session/series/plan output is untouched."""

    def setUp(self):
        self.conn = _conn(self)
        _insert_activity(self.conn, 1, "a1", "2026-11-01")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(reps=8, weight_value=40)])
        _insert_activity(self.conn, 1, "a2", "2026-11-10")
        db.save_exercise_sets(self.conn, 1, "garmin", "a2", [_watch_set(reps=10, weight_value=45)])
        self.conn.commit()

    def test_each_session_carries_its_own_ultima_vez(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-10"}, "completo")
        sesion = out["sesiones"][0]
        self.assertEqual(
            sesion["ultima_vez"]["press banca"],
            {"source": "garmin", "source_id": "a1", "day": "2026-11-01",
             "sets": [{"reps": 8, "weight_kg": 40.0, "corrected": False}],
             "mejor": {"reps": 8, "weight_kg": 40.0, "corrected": False}},
        )

    def test_nothing_else_in_the_output_changes(self):
        # ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PARTIDO-1-ASIS) ruling 1 adds `tipo`/`duracion_min`;
        # ruling 2 adds `esfuerzo` -- both present on every session now, this strength one included.
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-10"}, "completo")
        sesion = out["sesiones"][0]
        self.assertEqual(set(sesion),
                         {"dia", "titulo", "fuente", "hora_inicio", "tipo", "duracion_min", "series",
                          "ultima_vez", "dolor", "esfuerzo"})
        self.assertEqual(sesion["dia"], "2026-11-10")
        self.assertEqual(sesion["series"][0]["ejercicio"], "press banca")
        self.assertEqual(sesion["series"][0]["repeticiones"], 10)
        self.assertEqual(sesion["series"][0]["peso_kg"], 45.0)

    def test_an_exercise_never_done_before_reads_none(self):
        _insert_activity(self.conn, 1, "a3", "2026-11-12")
        db.save_exercise_sets(self.conn, 1, "garmin", "a3", [_watch_set(exercise="curl de bíceps")])
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-12"}, "completo")
        self.assertIsNone(out["sesiones"][0]["ultima_vez"]["curl de bíceps"])


class LeerHoySenalesTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6) ruling 4: leer_hoy carries today's own
    reentrada/tras_carga signal, present only when it applies."""

    def setUp(self):
        self.conn = _conn(self)
        db.set_metric(self.conn, "2026-10-08", "resting_hr", 55.0, "garmin", 1)
        self.conn.commit()

    def test_incluye_reentrada_cuando_aplica(self):
        _insert_activity(self.conn, 1, "a0", "2026-10-01")  # 7 days before the latest day with data
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_hoy", {}, "completo")
        self.assertEqual(out["reentrada"], {"dias_sin_fuerza": 7, "sesion_n": 1, "factor": 0.6})
        self.assertNotIn("tras_carga", out)

    def test_ninguna_clave_cuando_no_aplican(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_hoy", {}, "completo")
        self.assertNotIn("reentrada", out)
        self.assertNotIn("tras_carga", out)

    def test_leer_entreno_nunca_lleva_estas_claves(self):
        """Census for the design choice behind ruling 4: `leer_hoy` is the tool with a notion of
        'today' as a role (it already answers 'limitaciones', the despertar card, the plan-week
        card) -- `leer_entreno` just reads one day's own logged/planned session by `day`, with no
        concept of today, so it is never where these signals belong. Same gap fixture as
        `test_incluye_reentrada_cuando_aplica` above, read through `leer_entreno` instead: the
        signal never appears there."""
        _insert_activity(self.conn, 1, "a0", "2026-10-01")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-10-08"}, "completo")
        self.assertNotIn("reentrada", out)
        self.assertNotIn("tras_carga", out)


class LeerDiasLibresSenalesTest(unittest.TestCase):
    """Ruling 4: each of leer_dias_libres' own day entries carries reentrada/tras_carga where it
    applies -- never every day in the range."""

    def test_solo_el_dia_que_aplica_lleva_tras_carga(self):
        conn = _conn(self)
        for i, (day, dur) in enumerate([("2026-10-01", 600.0), ("2026-10-03", 700.0), ("2026-10-05", 800.0)]):
            conn.execute(
                "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
                "VALUES (1, ?, ?, ?, 'other', 'Yoga', ?, 'garmin')", (f"bg{i}", day, f"{day}T07:00:00", dur))
        conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
            "VALUES (1, 'basket', '2026-10-09', '2026-10-09T18:00:00', 'other', 'Baloncesto', 3000.0, 'garmin')")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dias_libres", {"desde": "2026-10-09", "dias": 3}, "ninguno")
        por_dia = {d["dia"]: d for d in out["dias_libres"]}
        self.assertNotIn("tras_carga", por_dia["2026-10-09"])
        self.assertEqual(por_dia["2026-10-10"]["tras_carga"]["actividad"], {"tipo": "other", "nombre": "Baloncesto"})
        self.assertNotIn("tras_carga", por_dia["2026-10-11"])


if __name__ == "__main__":
    unittest.main()
