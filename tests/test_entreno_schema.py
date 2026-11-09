"""BODY-ENTRENO-SCHEMA-1: the pure schema for ROADMAP 9b -- `workout_set`, `metric_def.origen`,
`plan.ejercicios_json`, the two generic self-measured metric_def rows, and `propuesta_agente.
accion`'s widened CHECK. Nothing reads or writes these shapes yet; tests/test_db.py does not
exist, so everything for this slice lives here. Invented demo data only, no real health figures.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, privacidad


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


def _insert_activity(conn, person_id, source_id, day, source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', NULL, ?)",
        (person_id, source_id, day, f"{day}T07:00:00", source),
    )


def _insert_workout_set(conn, person_id, source, source_id, set_order, set_type="active", **cols):
    base = {
        "person_id": person_id, "activity_source": source, "activity_source_id": source_id,
        "set_order": set_order, "set_type": set_type,
        "garmin_category": None, "garmin_exercise": None, "garmin_reps": None,
        "garmin_weight_value": None, "garmin_weight_unit": None, "garmin_duration_s": None,
        "garmin_start_local": None, "person_category": None, "person_exercise": None,
        "person_reps": None, "person_weight_kg": None, "person_note": None,
        "corrected_by_person": 0, "created_at": "2026-11-09T00:00:00+00:00",
        "updated_at": "2026-11-09T00:00:00+00:00",
    }
    base.update(cols)
    conn.execute(
        "INSERT INTO workout_set (person_id, activity_source, activity_source_id, set_order, set_type, "
        "garmin_category, garmin_exercise, garmin_reps, garmin_weight_value, garmin_weight_unit, "
        "garmin_duration_s, garmin_start_local, person_category, person_exercise, person_reps, "
        "person_weight_kg, person_note, corrected_by_person, created_at, updated_at) "
        "VALUES (:person_id, :activity_source, :activity_source_id, :set_order, :set_type, "
        ":garmin_category, :garmin_exercise, :garmin_reps, :garmin_weight_value, :garmin_weight_unit, "
        ":garmin_duration_s, :garmin_start_local, :person_category, :person_exercise, :person_reps, "
        ":person_weight_kg, :person_note, :corrected_by_person, :created_at, :updated_at)",
        base,
    )


class FreshDbTest(unittest.TestCase):
    """A fresh install gets every new shape directly from SCHEMA."""

    def setUp(self):
        self.conn = _db(self, "fresh.db")
        db.init(self.conn)

    def test_workout_set_table_exists_with_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(workout_set)")}
        expected = {
            "id", "person_id", "activity_source", "activity_source_id", "set_order", "set_type",
            "garmin_category", "garmin_exercise", "garmin_reps", "garmin_weight_value",
            "garmin_weight_unit", "garmin_duration_s", "garmin_start_local", "person_category",
            "person_exercise", "person_reps", "person_weight_kg", "person_note",
            "corrected_by_person", "created_at", "updated_at",
        }
        self.assertEqual(cols, expected)

    def test_metric_def_has_origen_defaulting_to_garmin(self):
        row = self.conn.execute("SELECT origen FROM metric_def WHERE key = 'resting_hr'").fetchone()
        self.assertEqual(row["origen"], "garmin")

    def test_two_generic_self_measured_metric_defs_are_seeded(self):
        rows = {
            r["key"]: r["origen"] for r in self.conn.execute(
                "SELECT key, origen FROM metric_def WHERE key IN ('flex_alcance_cm', 'snoring_min')"
            )
        }
        self.assertEqual(rows, {"flex_alcance_cm": "self", "snoring_min": "self"})

    def test_plan_has_ejercicios_json_column(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(plan)")}
        self.assertIn("ejercicios_json", cols)

    def test_propuesta_agente_accion_accepts_the_new_tool_actions(self):
        cid = self.conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en) VALUES (1, '2026-11-09T00:00:00+00:00')"
        ).lastrowid
        acciones = ("planear_entreno", "registrar_entreno", "registrar_medida", "marcar_habito",
                    "proponer_experimento", "marcar_experimento")
        for accion in acciones:
            self.conn.execute(
                "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
                "creada_en, expira_en) VALUES (1, ?, ?, '{}', '2026-11-09T00:00:00+00:00', "
                "'2026-11-09T00:10:00+00:00')",
                (cid, accion),
            )
        self.conn.commit()
        count = self.conn.execute(
            f"SELECT COUNT(*) FROM propuesta_agente WHERE accion IN "
            f"({','.join('?' * len(acciones))})", acciones
        ).fetchone()[0]
        self.assertEqual(count, len(acciones))

    def test_weight_value_and_unit_are_null_together(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_workout_set(
                self.conn, 1, "garmin", "a1", 1, garmin_weight_value=50.0, garmin_weight_unit=None
            )

    def test_weight_value_and_unit_together_is_allowed(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        _insert_workout_set(
            self.conn, 1, "garmin", "a1", 1, garmin_weight_value=50.0, garmin_weight_unit="kg"
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT garmin_weight_value, garmin_weight_unit FROM workout_set "
            "WHERE activity_source_id = 'a1'"
        ).fetchone()
        self.assertEqual((row["garmin_weight_value"], row["garmin_weight_unit"]), (50.0, "kg"))

    def test_both_weight_columns_null_is_allowed(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        _insert_workout_set(self.conn, 1, "garmin", "a1", 1)
        self.conn.commit()

    def test_set_order_is_unique_per_activity(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        _insert_workout_set(self.conn, 1, "garmin", "a1", 1)
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_workout_set(self.conn, 1, "garmin", "a1", 1)

    def test_fk_to_activity_holds_for_a_garmin_source(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-09", source="garmin")
        _insert_workout_set(self.conn, 1, "garmin", "a1", 1)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM workout_set WHERE activity_source = 'garmin' AND activity_source_id = 'a1'"
        ).fetchone()
        self.assertEqual(row["n"], 1)

    def test_fk_to_activity_holds_for_a_manual_source(self):
        _insert_activity(self.conn, 1, "m1", "2026-11-09", source="manual")
        _insert_workout_set(self.conn, 1, "manual", "m1", 1)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM workout_set WHERE activity_source = 'manual' AND activity_source_id = 'm1'"
        ).fetchone()
        self.assertEqual(row["n"], 1)

    def test_fk_to_activity_rejects_an_unknown_activity(self):
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_workout_set(self.conn, 1, "garmin", "does-not-exist", 1)

    def test_init_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        rows = self.conn.execute("SELECT COUNT(*) AS n FROM metric_def").fetchone()["n"]
        self.assertGreater(rows, 0)


_OLD_METRIC_DEF_SQL = """
CREATE TABLE metric_def (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    unit    TEXT NOT NULL,
    better  TEXT NOT NULL CHECK (better IN ('higher', 'lower', 'neutral')),
    kind    TEXT NOT NULL DEFAULT 'measured' CHECK (kind IN ('measured', 'estimated'))
);
"""

_OLD_PLAN_SQL = """
CREATE TABLE plan (
    id        INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    hogar_id  TEXT,
    goal_id   INTEGER REFERENCES goal(id),
    day       TEXT NOT NULL,
    time      TEXT,
    title     TEXT NOT NULL,
    booking   TEXT CHECK (booking IS NULL OR json_valid(booking)),
    status    TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'accepted', 'done', 'skipped'))
);
"""

_OLD_PROPUESTA_AGENTE_SQL = """
CREATE TABLE propuesta_agente (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT
);
"""


_OLD_PROPUESTA_AGENTE_SQL_ENTRENO = """
CREATE TABLE propuesta_agente (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT
);
"""


def _old_db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """A DB built from the current SCHEMA (so every other table is already right), with metric_def,
    plan and propuesta_agente dropped and recreated in their pre-slice shape -- the same technique
    tests/test_turnos_esquema_1.py uses for person/shift_batch."""
    conn = _db(case, name)
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE metric_def")
    conn.execute(_OLD_METRIC_DEF_SQL)
    conn.execute("DROP TABLE plan")
    conn.execute(_OLD_PLAN_SQL)
    conn.execute("DROP TABLE propuesta_agente")
    conn.execute(_OLD_PROPUESTA_AGENTE_SQL)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class MigrationPopulatedTest(unittest.TestCase):
    """An old-shape DB with invented rows in every touched table migrates with every row preserved
    and the new defaults applied, and the migration is idempotent."""

    def setUp(self):
        self.conn = _old_db(self, "vieja.db")
        self.conn.execute(
            "INSERT INTO metric_def (key, label, unit, better, kind) VALUES "
            "('mi_metrica_vieja', 'Mi metrica vieja', 'u', 'higher', 'measured')"
        )
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.conn.execute(
            "INSERT INTO plan (person_id, day, title, status) VALUES (1, '2026-11-09', 'Caminar', 'proposed')"
        )
        cid = self.conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en) VALUES (1, '2026-11-09T00:00:00+00:00')"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'proponer_idioma', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')",
            (cid,),
        )
        self.conn.commit()
        self._cid = cid

    def _assert_migrated_state(self):
        old_row = self.conn.execute(
            "SELECT label, origen FROM metric_def WHERE key = 'mi_metrica_vieja'"
        ).fetchone()
        self.assertEqual(old_row["label"], "Mi metrica vieja")
        self.assertEqual(old_row["origen"], "garmin")

        new_rows = {
            r["key"]: r["origen"] for r in self.conn.execute(
                "SELECT key, origen FROM metric_def WHERE key IN ('flex_alcance_cm', 'snoring_min')"
            )
        }
        self.assertEqual(new_rows, {"flex_alcance_cm": "self", "snoring_min": "self"})

        plan_row = self.conn.execute(
            "SELECT title, ejercicios_json FROM plan WHERE title = 'Caminar'"
        ).fetchone()
        self.assertEqual(plan_row["title"], "Caminar")
        self.assertIsNone(plan_row["ejercicios_json"])

        old_proposal = self.conn.execute(
            "SELECT accion FROM propuesta_agente WHERE conversacion_id = ?", (self._cid,)
        ).fetchone()
        self.assertEqual(old_proposal["accion"], "proponer_idioma")

        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'planear_entreno', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')",
            (self._cid,),
        )
        self.conn.commit()

    def test_migration_preserves_every_row_and_applies_the_new_defaults(self):
        db.init(self.conn)
        self._assert_migrated_state()

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self._assert_migrated_state()
        count = self.conn.execute("SELECT COUNT(*) AS n FROM metric_def").fetchone()["n"]
        self.conn.commit()
        db.init(self.conn)
        after = self.conn.execute("SELECT COUNT(*) AS n FROM metric_def").fetchone()["n"]
        self.assertEqual(count, after)


class ExperimentosAccionMigrationTest(unittest.TestCase):
    """BODY-EXPERIMENTOS-REGISTRO-1: a second, later widening of the same CHECK (proponer_experimento,
    marcar_experimento) on top of a DB that already has the ENTRENO-era eight-action shape -- every
    existing proposal row survives byte-for-byte, and the migration is idempotent."""

    def setUp(self):
        self.conn = _db(self, "experimentos_migra.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(_OLD_PROPUESTA_AGENTE_SQL_ENTRENO)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.cid = self.conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en) VALUES (1, '2026-11-09T00:00:00+00:00')"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'marcar_habito', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.commit()

    def _assert_migrated_state(self):
        old_proposal = self.conn.execute(
            "SELECT accion FROM propuesta_agente WHERE conversacion_id = ?", (self.cid,)
        ).fetchone()
        self.assertEqual(old_proposal["accion"], "marcar_habito")
        for accion in ("proponer_experimento", "marcar_experimento"):
            self.conn.execute(
                "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
                "creada_en, expira_en) VALUES (1, ?, ?, '{}', '2026-11-09T00:00:00+00:00', "
                "'2026-11-09T00:10:00+00:00')",
                (self.cid, accion),
            )
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE accion IN ('proponer_experimento', 'marcar_experimento')"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_migration_preserves_the_old_row_and_accepts_the_new_actions(self):
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


class PrivacyCensusTest(unittest.TestCase):
    """workout_set is the person's own health data: BODY-ENTRENO-SCHEMA-1-M's privacidad.py census
    puts it in FULLY_HANDLED_TABLES, so an invented row is in the person's export and gone after
    delete-all, same as every other person-owned table."""

    def setUp(self):
        self.conn = _db(self, "privacidad.db")
        db.init(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        _insert_workout_set(self.conn, 1, "garmin", "a1", 1, person_exercise="Sentadilla", person_reps=8)
        self.conn.commit()

    def test_workout_set_row_is_in_the_export(self):
        data = privacidad.export_person(self.conn, 1)
        rows = data["sets_entreno"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["person_exercise"], "Sentadilla")

    def test_delete_all_removes_the_workout_set_row(self):
        privacidad.delete_person(self.conn, 1)
        count = self.conn.execute("SELECT COUNT(*) FROM workout_set").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
