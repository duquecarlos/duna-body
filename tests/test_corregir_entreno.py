"""BODY-CORREGIR-MEDIDA-ENTRENO-1-W ruling B: `corregir_entreno`, a propuesta tool addressed by
position (`dia`, `orden`) that fixes ONE set of a training session already logged that day --
"the third one was 8 reps" -- without resending every set. Resolves the day's own session the same
way ruling 2's `_resolver_sesion_entreno_dia` does (a Garmin strength day or an existing manual
one), applies through `db.correct_exercise_set`, and undoes through the same direct UPDATE on
`workout_set` `registrar_entreno`'s own undo already uses for a Garmin-matched slot. Invented demo
data only, no real health figures, no network.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, mcp
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

DAY = "2026-11-05"


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


def _watch_set(exercise="press banca", reps=8.0, weight_value=40.0):
    return garmin_adapter.ExerciseSet(
        set_type="active", category="pecho", exercise=exercise, reps=reps,
        weight_value=weight_value, weight_unit="kg", duration_s=30.0, start_local=f"{DAY}T07:00:00",
    )


class GarminFixture:
    """One watch strength day with a single set at order 1, unset up fresh per test."""

    def _seed_garmin(self, conn, person_id=1, day=DAY, source_id="g1"):
        _insert_activity(conn, person_id, source_id, day)
        conn.commit()
        db.save_exercise_sets(conn, person_id, "garmin", source_id, [_watch_set()])
        conn.commit()
        return source_id


class ManualFixture:
    def _seed_manual(self, conn, person_id=1, day=DAY):
        source_id = db.create_manual_session(conn, person_id, day, None, "Pierna en casa")
        db.correct_exercise_set(conn, person_id, "manual", source_id, 1, exercise="sentadilla",
                                 reps=10, weight_kg=40.0)
        conn.commit()
        return source_id


# ---- arguments: each alone, nothing to change, type/shape validation --------------------------

class ArgumentValidationTest(unittest.TestCase, GarminFixture):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self._seed_garmin(self.conn)

    def test_ejercicio_alone_is_enough(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "ejercicio": "press inclinado"}, "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")

    def test_reps_alone_is_enough(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "reps": 9}, "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")

    def test_peso_kg_alone_is_enough(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "peso_kg": 42.5}, "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")

    def test_nothing_to_change_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_entreno", {"dia": DAY, "orden": 1},
                                  "ninguno", self.cid)

    def test_orden_must_be_an_integer(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                  {"dia": DAY, "orden": "primera", "reps": 9}, "ninguno", self.cid)

    def test_orden_below_one_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                  {"dia": DAY, "orden": 0, "reps": 9}, "ninguno", self.cid)

    def test_a_blank_ejercicio_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                  {"dia": DAY, "orden": 1, "ejercicio": "   "}, "ninguno", self.cid)


# ---- session/set resolution: no session, no set, future day, person scoping -------------------

class SessionResolutionTest(unittest.TestCase, GarminFixture, ManualFixture):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_no_session_that_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                  {"dia": DAY, "orden": 1, "reps": 9}, "ninguno", self.cid)
        self.assertEqual(ctx.exception.args[0], T("es")("agente.error.entreno_sin_sesion"))

    def test_no_set_at_that_order_is_refused(self):
        self._seed_garmin(self.conn)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                  {"dia": DAY, "orden": 5, "reps": 9}, "ninguno", self.cid)
        self.assertEqual(ctx.exception.args[0], T("es")("agente.error.entreno_sin_serie", orden=5))

    def test_a_future_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                  {"dia": "2099-01-01", "orden": 1, "reps": 9}, "ninguno", self.cid)

    def test_another_persons_day_is_never_touched(self):
        """Person 2 has a session that same day; person 1 has none -- person 1's call must still
        see "no session", never person 2's."""
        self._seed_garmin(self.conn, person_id=2)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                  {"dia": DAY, "orden": 1, "reps": 9}, "ninguno", self.cid)
        self.assertEqual(ctx.exception.args[0], T("es")("agente.error.entreno_sin_sesion"))
        # person 2's own slot is untouched
        fila = self.conn.execute(
            "SELECT corrected_by_person FROM workout_set WHERE person_id = 2 AND set_order = 1").fetchone()
        self.assertEqual(fila["corrected_by_person"], 0)

    def test_a_watch_strength_day_is_preferred_over_a_manual_one(self):
        self._seed_garmin(self.conn)
        self._seed_manual(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "reps": 9}, "ninguno", self.cid)
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)).fetchone()
        self.assertEqual(json.loads(registro["despues_json"])["source"], "garmin")


# ---- apply/undo exact, on a Garmin slot ---------------------------------------------------------

class ApplyUndoGarminTest(unittest.TestCase, GarminFixture):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.source_id = self._seed_garmin(self.conn)

    def test_apply_writes_only_the_given_fields(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "reps": 9}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        fila = self.conn.execute(
            "SELECT person_exercise, person_reps, person_weight_kg, corrected_by_person, garmin_exercise "
            "FROM workout_set WHERE person_id = 1 AND activity_source = 'garmin' AND activity_source_id = ? "
            "AND set_order = 1", (self.source_id,)).fetchone()
        self.assertEqual(fila["person_reps"], 9)
        self.assertIsNone(fila["person_exercise"])  # never given, never written
        self.assertIsNone(fila["person_weight_kg"])
        self.assertEqual(fila["corrected_by_person"], 1)
        self.assertEqual(fila["garmin_exercise"], "press banca")  # the watch's own value, untouched

    def test_undo_restores_the_uncorrected_slot_exactly(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "ejercicio": "press inclinado", "reps": 9,
                                     "peso_kg": 45.0}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        fila = self.conn.execute(
            "SELECT person_exercise, person_reps, person_weight_kg, corrected_by_person "
            "FROM workout_set WHERE person_id = 1 AND activity_source = 'garmin' AND activity_source_id = ? "
            "AND set_order = 1", (self.source_id,)).fetchone()
        self.assertIsNone(fila["person_exercise"])
        self.assertIsNone(fila["person_reps"])
        self.assertIsNone(fila["person_weight_kg"])
        self.assertEqual(fila["corrected_by_person"], 0)  # back to uncorrected, not left at 1

    def test_undo_restores_a_prior_persons_own_correction(self):
        """A slot already corrected once (by the person) before corregir_entreno touched it --
        undo restores THAT prior correction, not the watch's raw reading."""
        db.correct_exercise_set(self.conn, 1, "garmin", self.source_id, 1, exercise="press banca",
                                 reps=8, weight_kg=40.0)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "reps": 9}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        fila = self.conn.execute(
            "SELECT person_reps, corrected_by_person FROM workout_set WHERE person_id = 1 AND "
            "activity_source = 'garmin' AND activity_source_id = ? AND set_order = 1",
            (self.source_id,)).fetchone()
        self.assertEqual(fila["person_reps"], 8)
        self.assertEqual(fila["corrected_by_person"], 1)


# ---- apply/undo exact, on a manual slot ---------------------------------------------------------

class ApplyUndoManualTest(unittest.TestCase, ManualFixture):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.source_id = self._seed_manual(self.conn)

    def test_apply_corrects_the_manual_slot(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "reps": 12}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        fila = self.conn.execute(
            "SELECT person_exercise, person_reps, person_weight_kg FROM workout_set WHERE person_id = 1 "
            "AND activity_source = 'manual' AND activity_source_id = ? AND set_order = 1",
            (self.source_id,)).fetchone()
        self.assertEqual(fila["person_reps"], 12)
        self.assertEqual(fila["person_exercise"], "sentadilla")  # untouched, not given
        self.assertEqual(fila["person_weight_kg"], 40.0)  # untouched, not given

    def test_undo_restores_the_prior_manual_values(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "reps": 12, "peso_kg": 45.0}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        fila = self.conn.execute(
            "SELECT person_exercise, person_reps, person_weight_kg FROM workout_set WHERE person_id = 1 "
            "AND activity_source = 'manual' AND activity_source_id = ? AND set_order = 1",
            (self.source_id,)).fetchone()
        self.assertEqual(fila["person_reps"], 10)
        self.assertEqual(fila["person_weight_kg"], 40.0)
        self.assertEqual(fila["person_exercise"], "sentadilla")


# ---- drift: a slot changed since propose, both on apply and on undo ---------------------------

class DriftTest(unittest.TestCase, GarminFixture):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.source_id = self._seed_garmin(self.conn)

    def test_drift_on_apply_refuses(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "reps": 9}, "ninguno", self.cid)
        db.correct_exercise_set(self.conn, 1, "garmin", self.source_id, 1, exercise="otra cosa", reps=5)
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_drift_on_undo_refuses(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno",
                                    {"dia": DAY, "orden": 1, "reps": 9}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        db.correct_exercise_set(self.conn, 1, "garmin", self.source_id, 1, exercise="otra cosa", reps=5)
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")


# ---- card: title and summary, es/en ------------------------------------------------------------

class CardTextTest(unittest.TestCase, GarminFixture):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self._seed_garmin(self.conn)
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, **campos):
        params = {"dia": DAY, "orden": 1}
        params.update(campos)
        out = herramientas.ejecutar(self.conn, 1, "corregir_entreno", params, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_title_es(self):
        self.assertEqual(T("es")("asistente.propuesta.titulo.corregir_entreno"), "Corregir serie")

    def test_title_en(self):
        self.assertEqual(T("en")("asistente.propuesta.titulo.corregir_entreno"), "Correct set")

    def test_summary_names_the_order_and_day_es(self):
        t = self._tarjeta("es", reps=9)
        self.assertIn("serie 1", t["despues"])
        self.assertIn("9 rep", t["despues"])

    def test_summary_names_the_order_and_day_en(self):
        t = self._tarjeta("en", reps=9)
        self.assertIn("set 1", t["despues"])
        self.assertIn("9 rep", t["despues"])

    def test_summary_names_the_exercise_when_given(self):
        t = self._tarjeta("es", ejercicio="press inclinado")
        self.assertIn("press inclinado", t["despues"])

    def test_card_render_never_raises_on_a_malformed_despues(self):
        tarjeta = asistente._tarjeta_propuesta_segura(T("es"), self.defs, {
            "id": 999, "accion": "corregir_entreno", "despues_json": {}, "antes_json": {},
            "estado": "propuesta", "expira_en": "2026-11-09T00:10:00+00:00", "aplicada_via": None,
        })
        self.assertIn("despues", tarjeta)


# ---- catalog parity: in-app registry, stdio MCP -------------------------------------------------

class CatalogParityTest(unittest.TestCase, GarminFixture):
    def setUp(self):
        self.conn = _conn(self)
        self._seed_garmin(self.conn)
        # mcp._lista_herramientas sets herramientas._TURNOS_ACTUAL as a side effect -- restore the
        # default so this test never leaks state into another test module (tests/test_turnos_
        # opcional_3.py's own precedent for this same ContextVar).
        self.addCleanup(herramientas._TURNOS_ACTUAL.set, True)

    def test_listed_in_the_in_app_registry(self):
        nombres = {h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta"}
        self.assertIn("corregir_entreno", nombres)

    def test_listed_over_stdio_mcp(self):
        sesion = mcp._Sesion(self.conn, 1)
        nombres = {t["name"] for t in mcp._lista_herramientas(sesion)}
        self.assertIn("corregir_entreno", nombres)

    def test_not_in_acciones_reemplazables(self):
        """Unlike registrar_medida (ruling 1), a second corregir_entreno is its own, separate
        pending proposal -- never replaces an earlier one of the same day/slot."""
        self.assertNotIn("corregir_entreno", propuestas.ACCIONES_REEMPLAZABLES)
