"""BODY-SERIES-ORDEN-1: Garmin's exerciseSets alternate active/rest slots (set_order 1=active,
2=rest, 3=active, ...). `db.exercise_sets` reads a corrected slot as 'active' whatever the watch
said (ruling 1), `_registrar_entreno` maps spoken sets onto the watch's ACTIVE slots only, in order
(ruling 2), a watch active slot the spoken series never reaches is proposed for exclusion (ruling
3, applied/undone alongside the correction, ruling 4's own last sentence), `descartar_series` lets
the person mark an already-logged day's leftover watch slot as not done (ruling 4), and a rest slot
is never read as a set (ruling 5). Invented demo data only, no real health figures.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _insert_activity(conn, person_id, source_id, day, source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', NULL, ?)",
        (person_id, source_id, day, f"{day}T07:00:00", source),
    )


def _set(set_type, exercise=None, reps=8.0, weight_value=40.0, weight_unit="kg"):
    return garmin_adapter.ExerciseSet(
        set_type=set_type, category="pecho", exercise=exercise, reps=reps,
        weight_value=weight_value if set_type == "active" else None,
        weight_unit=weight_unit if set_type == "active" else None,
        duration_s=30.0, start_local="2026-11-09T07:00:00",
    )


def _alternating(n_active, exercise="press banca"):
    """n_active active slots, one rest slot inserted after each -- Garmin's own alternation.
    Returns the list to pass to db.save_exercise_sets; set_order 1 is the first active slot."""
    sets = []
    for i in range(n_active):
        sets.append(_set("active", exercise=f"{exercise} {i + 1}"))
        sets.append(_set("rest"))
    return sets


class ExerciseSetsEfectivoTest(unittest.TestCase):
    """Ruling 1: a corrected slot reads as 'active' through db.exercise_sets, whatever the
    watch's own set_type said. Ruling 3's own column round-trips through db.set_series_excluida."""

    def setUp(self):
        self.conn = _conn(self)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()

    def test_uncorrected_rest_slot_stays_rest(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", _alternating(1))
        self.conn.commit()
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual(sets[0]["set_type"], "active")
        self.assertEqual(sets[1]["set_type"], "rest")

    def test_a_corrected_rest_slot_reads_as_active(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", _alternating(1))
        self.conn.commit()
        # set_order 2 is the watch's own rest slot -- the person dictated a set onto it anyway.
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 2, exercise="remo", reps=10)
        self.conn.commit()
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        corregido = next(s for s in sets if s["set_order"] == 2)
        self.assertEqual(corregido["set_type"], "active")
        self.assertTrue(corregido["corrected_by_person"])

    def test_excluded_by_person_defaults_to_false(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", _alternating(1))
        self.conn.commit()
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertFalse(sets[0]["excluded_by_person"])

    def test_set_series_excluida_round_trips(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", _alternating(1))
        self.conn.commit()
        db.set_series_excluida(self.conn, 1, "garmin", "a1", 1, True)
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertTrue(sets[0]["excluded_by_person"])
        db.set_series_excluida(self.conn, 1, "garmin", "a1", 1, False)
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertFalse(sets[0]["excluded_by_person"])


class LeerEntrenoRestYExcluidasOcultasTest(unittest.TestCase):
    """Rulings 1, 5: a rest slot never reaches leer_entreno's own `series`; a slot the person
    marked excluded is hidden too."""

    def setUp(self):
        self.conn = _conn(self)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", _alternating(3))
        self.conn.commit()

    def test_rest_slots_never_appear_as_series(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-09"}, "completo")
        ordenes = [s["orden"] for s in out["sesiones"][0]["series"]]
        self.assertEqual(ordenes, [1, 3, 5])

    def test_a_corrected_rest_slot_shows_up_as_active(self):
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 2, exercise="remo", reps=10)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-09"}, "completo")
        ordenes = [s["orden"] for s in out["sesiones"][0]["series"]]
        self.assertIn(2, ordenes)

    def test_an_excluded_active_slot_is_hidden(self):
        db.set_series_excluida(self.conn, 1, "garmin", "a1", 1, True)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-09"}, "completo")
        ordenes = [s["orden"] for s in out["sesiones"][0]["series"]]
        self.assertNotIn(1, ordenes)
        self.assertEqual(ordenes, [3, 5])


class RegistrarEntrenoSoloActivasTest(unittest.TestCase):
    """Ruling 2: spoken set k maps onto the watch's k-th ACTIVE slot, never a rest slot in
    between."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", _alternating(6))
        self.conn.commit()
        self.series = [{"ejercicio": f"press {i}", "reps": 10, "peso_kg": 50.0} for i in range(1, 7)]

    def _despues(self, out):
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        return json.loads(registro["despues_json"])

    def test_six_spoken_sets_land_on_the_six_active_slots(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-09", "series": self.series}, "ninguno", self.cid)
        despues = self._despues(out)
        ordenes = [m["set_order"] for m in despues["mapeo"]]
        self.assertEqual(ordenes, [1, 3, 5, 7, 9, 11])
        self.assertEqual(despues["no_emparejadas"], [])

    def test_apply_corrects_only_the_active_slots_rest_slots_are_untouched(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-09", "series": self.series}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        rest = self.conn.execute(
            "SELECT corrected_by_person, set_type FROM workout_set WHERE person_id = 1 AND set_order = 2"
        ).fetchone()
        self.assertEqual(rest["set_type"], "rest")
        self.assertEqual(rest["corrected_by_person"], 0)
        activo = self.conn.execute(
            "SELECT person_exercise, corrected_by_person FROM workout_set WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(activo["person_exercise"], "press 1")
        self.assertEqual(activo["corrected_by_person"], 1)

    def test_more_spoken_sets_than_active_slots_get_new_orders_after_the_last_one(self):
        series = self.series + [{"ejercicio": "press 7", "reps": 8}]
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                    {"day": "2026-11-09", "series": series}, "ninguno", self.cid)
        despues = self._despues(out)
        # 6 active/6 rest slots already stored (set_order 1..12); the 7th spoken set is new.
        self.assertEqual(despues["mapeo"][-1]["set_order"], 13)
        self.assertIsNone(despues["mapeo"][-1]["reloj"])


class RegistrarEntrenoExcluyeNoEmparejadasTest(unittest.TestCase):
    """Rulings 3, 4 (own last sentence): fewer spoken sets than the watch's active slots proposes
    excluding the leftover active slots, applied and undone alongside the correction."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", _alternating(6))
        self.conn.commit()
        self.series = [{"ejercicio": f"press {i}", "reps": 10} for i in range(1, 5)]  # only 4 of 6

    def _registrar(self):
        return herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                     {"day": "2026-11-09", "series": self.series}, "ninguno", self.cid)

    def test_propose_lists_the_two_leftover_active_slots(self):
        out = self._registrar()
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        despues = json.loads(registro["despues_json"])
        self.assertEqual([n["set_order"] for n in despues["no_emparejadas"]], [9, 11])

    def test_apply_excludes_them_and_leer_entreno_hides_them(self):
        out = self._registrar()
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        sets = {s["set_order"]: s for s in db.exercise_sets(self.conn, 1, "garmin", "a1")}
        self.assertTrue(sets[9]["excluded_by_person"])
        self.assertTrue(sets[11]["excluded_by_person"])
        leido = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-09"}, "completo")
        ordenes = [s["orden"] for s in leido["sesiones"][0]["series"]]
        self.assertNotIn(9, ordenes)
        self.assertNotIn(11, ordenes)

    def test_undo_restores_them(self):
        out = self._registrar()
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        sets = {s["set_order"]: s for s in db.exercise_sets(self.conn, 1, "garmin", "a1")}
        self.assertFalse(sets[9]["excluded_by_person"])
        self.assertFalse(sets[11]["excluded_by_person"])

    def test_drift_refuses_when_a_leftover_slot_changed_before_apply(self):
        out = self._registrar()
        # Something else corrected set_order 9 before Aplicar was tapped.
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 9, exercise="otra cosa", reps=5)
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        fila = self.conn.execute(
            "SELECT excluded_by_person FROM workout_set WHERE person_id = 1 AND set_order = 11"
        ).fetchone()
        self.assertEqual(fila["excluded_by_person"], 0)


class DescartarSeriesTest(unittest.TestCase):
    """Ruling 4: a tool for an already-logged day -- refuses a slot the person filled, a rest
    slot or an unknown order; apply excludes, undo restores."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", _alternating(6))
        self.conn.commit()

    def test_refuses_when_no_strength_activity_that_day(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "descartar_series",
                                  {"day": "2026-11-08", "series": [1]}, "ninguno", self.cid)

    def test_refuses_an_unknown_order(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "descartar_series",
                                  {"day": "2026-11-09", "series": [99]}, "ninguno", self.cid)

    def test_refuses_a_rest_slot(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "descartar_series",
                                  {"day": "2026-11-09", "series": [2]}, "ninguno", self.cid)

    def test_refuses_a_slot_the_person_already_filled(self):
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, exercise="press", reps=10)
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "descartar_series",
                                  {"day": "2026-11-09", "series": [1]}, "ninguno", self.cid)

    def test_propose_card_carries_the_watchs_own_exercise_and_reps(self):
        out = herramientas.ejecutar(self.conn, 1, "descartar_series",
                                    {"day": "2026-11-09", "series": [11]}, "ninguno", self.cid)
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        despues = json.loads(registro["despues_json"])
        self.assertEqual(despues["series"][0]["set_order"], 11)
        self.assertEqual(despues["series"][0]["ejercicio"], "press banca 6")
        self.assertEqual(despues["series"][0]["reps"], 8.0)

    def test_apply_excludes_and_undo_restores(self):
        out = herramientas.ejecutar(self.conn, 1, "descartar_series",
                                    {"day": "2026-11-09", "series": [11]}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        fila = self.conn.execute(
            "SELECT excluded_by_person FROM workout_set WHERE person_id = 1 AND set_order = 11"
        ).fetchone()
        self.assertEqual(fila["excluded_by_person"], 1)
        leido = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-09"}, "completo")
        self.assertNotIn(11, [s["orden"] for s in leido["sesiones"][0]["series"]])

        deshecho = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(deshecho["estado"], "deshecha")
        fila = self.conn.execute(
            "SELECT excluded_by_person FROM workout_set WHERE person_id = 1 AND set_order = 11"
        ).fetchone()
        self.assertEqual(fila["excluded_by_person"], 0)

    def test_duplicate_orders_in_one_call_are_deduped(self):
        out = herramientas.ejecutar(self.conn, 1, "descartar_series",
                                    {"day": "2026-11-09", "series": [11, 11]}, "ninguno", self.cid)
        registro = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        despues = json.loads(registro["despues_json"])
        self.assertEqual(len(despues["series"]), 1)

    def test_drift_refuses_when_the_slot_changed_before_apply(self):
        out = herramientas.ejecutar(self.conn, 1, "descartar_series",
                                    {"day": "2026-11-09", "series": [11]}, "ninguno", self.cid)
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 11, exercise="otra cosa", reps=5)
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_person_b_cannot_apply_person_as_proposal(self):
        out = herramientas.ejecutar(self.conn, 1, "descartar_series",
                                    {"day": "2026-11-09", "series": [11]}, "ninguno", self.cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])


_OLD_WORKOUT_SET_SQL = """
CREATE TABLE workout_set (
    id                  INTEGER PRIMARY KEY,
    person_id           INTEGER NOT NULL REFERENCES person(id),
    activity_source     TEXT NOT NULL,
    activity_source_id  TEXT NOT NULL,
    set_order           INTEGER NOT NULL,
    set_type            TEXT NOT NULL CHECK (set_type IN ('active', 'rest')),
    garmin_category     TEXT,
    garmin_exercise     TEXT,
    garmin_reps         INTEGER,
    garmin_weight_value REAL,
    garmin_weight_unit  TEXT,
    garmin_duration_s   REAL,
    garmin_start_local  TEXT,
    person_category     TEXT,
    person_exercise     TEXT,
    person_reps         INTEGER,
    person_weight_kg    REAL,
    person_note         TEXT,
    corrected_by_person INTEGER NOT NULL DEFAULT 0 CHECK (corrected_by_person IN (0, 1)),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CHECK ((garmin_weight_value IS NULL) = (garmin_weight_unit IS NULL)),
    FOREIGN KEY (person_id, activity_source, activity_source_id)
        REFERENCES activity(person_id, source, source_id),
    UNIQUE (person_id, activity_source, activity_source_id, set_order)
);
"""

_OLD_PROPUESTA_AGENTE_SQL_SIN_DESCARTAR_SERIES = """
CREATE TABLE propuesta_agente (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito',
                         'proponer_experimento', 'marcar_experimento',
                         'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                         'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                         'registrar_comida', 'registrar_checkin')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp'))
);
"""


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


class WorkoutSetMigrationTest(unittest.TestCase):
    """BODY-SERIES-ORDEN-1 ruling 3: the additive `excluded_by_person` column reaches an old
    `workout_set` table through the existing ALTER pattern, defaulting every stored row to 0."""

    def setUp(self):
        self.conn = _db(self, "vieja_workout_set.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE workout_set")
        self.conn.execute(_OLD_WORKOUT_SET_SQL)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.execute(
            "INSERT INTO workout_set (person_id, activity_source, activity_source_id, set_order, "
            "set_type, corrected_by_person, created_at, updated_at) VALUES "
            "(1, 'garmin', 'a1', 1, 'active', 0, '2026-11-09T00:00:00+00:00', '2026-11-09T00:00:00+00:00')"
        )
        self.conn.commit()

    def test_migration_adds_the_column_defaulted_to_zero_and_preserves_the_row(self):
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT set_type, excluded_by_person FROM workout_set WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(row["set_type"], "active")
        self.assertEqual(row["excluded_by_person"], 0)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT excluded_by_person FROM workout_set WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(row["excluded_by_person"], 0)


class PropuestaAgenteSeriesOrdenMigrationTest(unittest.TestCase):
    """BODY-SERIES-ORDEN-1: an eighth widening of propuesta_agente.accion (descartar_series) on
    top of the CHECKIN-era shape -- the existing row survives, the new action is accepted."""

    def setUp(self):
        self.conn = _db(self, "vieja_propuesta.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(_OLD_PROPUESTA_AGENTE_SQL_SIN_DESCARTAR_SERIES)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.cid = self.conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en) VALUES (1, '2026-11-09T00:00:00+00:00')"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'registrar_checkin', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.commit()

    def _assert_migrated_state(self):
        old = self.conn.execute(
            "SELECT accion FROM propuesta_agente WHERE conversacion_id = ?", (self.cid,)
        ).fetchone()
        self.assertEqual(old["accion"], "registrar_checkin")
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'descartar_series', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.commit()

    def test_migration_preserves_the_old_row_and_accepts_descartar_series(self):
        db.init(self.conn)
        self._assert_migrated_state()

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self._assert_migrated_state()
        count = self.conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente").fetchone()["n"]
        self.conn.commit()
        db.init(self.conn)
        after = self.conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente").fetchone()["n"]
        self.assertEqual(count, after)


if __name__ == "__main__":
    unittest.main()
