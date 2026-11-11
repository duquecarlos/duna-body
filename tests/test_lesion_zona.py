"""BODY-LESION-ZONA-1: a period can name the groups it limits (ruling 1), training reads mark a
set in those zonas as deliberate -- never a drop (ruling 2), and leer_hoy carries the open
limitation as context for planear_entreno (ruling 3). Invented demo data only, no real health
figures.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import anatomia, db, garmin_adapter
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas

DAY = "2026-11-10"


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


def _set(category, exercise="press militar"):
    return garmin_adapter.ExerciseSet(
        set_type="active", category=category, exercise=exercise, reps=8.0, weight_value=20.0,
        weight_unit="kg", duration_s=30.0, start_local=f"{DAY}T07:00:00",
    )


# ---- schema / migration ------------------------------------------------------------------------

class MigrationOnAnExistingDbTest(unittest.TestCase):
    """A database built from the current SCHEMA with period's own `zonas` column dropped (purely
    additive, so dropping it is enough to simulate main's current schema, pre-this-slice)."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("ALTER TABLE period DROP COLUMN zonas")
        self.conn.execute("INSERT INTO period_kind (key, sort_order) VALUES ('lesion', 2)")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.conn.commit()

    def test_migration_adds_the_column(self):
        db.init(self.conn)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(period)")}
        self.assertIn("zonas", cols)

    def test_a_row_from_before_the_migration_reads_as_null(self):
        self.conn.execute(
            "INSERT INTO period (person_id, kind, start_day, created_at, updated_at) "
            "VALUES (1, 'lesion', '2026-11-01', '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00')"
        )
        self.conn.commit()
        db.init(self.conn)
        row = self.conn.execute("SELECT zonas FROM period").fetchone()
        self.assertIsNone(row["zonas"])

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        pid = db.crear_periodo(self.conn, 1, "lesion", "2026-11-01", zonas=["hombro"])
        db.init(self.conn)
        row = self.conn.execute("SELECT zonas FROM period WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(json.loads(row["zonas"]), ["hombro"])


# ---- db.zonas_en_recuperacion -------------------------------------------------------------------

class ZonasEnRecuperacionTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_open_lesion_with_zonas_covering_the_day(self):
        db.crear_periodo(self.conn, 1, "lesion", DAY, None, zonas=["hombro", "brazo"])
        self.assertEqual(db.zonas_en_recuperacion(self.conn, 1, DAY), frozenset({"hombro", "brazo"}))

    def test_closed_period_contributes_nothing(self):
        db.crear_periodo(self.conn, 1, "lesion", "2026-11-01", "2026-11-05", zonas=["hombro"])
        self.assertEqual(db.zonas_en_recuperacion(self.conn, 1, DAY), frozenset())

    def test_non_lesion_kind_contributes_nothing(self):
        db.crear_periodo(self.conn, 1, "malestar", DAY, None)
        self.assertEqual(db.zonas_en_recuperacion(self.conn, 1, DAY), frozenset())

    def test_period_with_no_zonas_contributes_nothing(self):
        db.crear_periodo(self.conn, 1, "lesion", DAY, None)
        self.assertEqual(db.zonas_en_recuperacion(self.conn, 1, DAY), frozenset())

    def test_period_not_covering_the_day_contributes_nothing(self):
        db.crear_periodo(self.conn, 1, "lesion", "2026-12-01", None, zonas=["hombro"])
        self.assertEqual(db.zonas_en_recuperacion(self.conn, 1, DAY), frozenset())

    def test_another_persons_period_contributes_nothing(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        db.crear_periodo(self.conn, 2, "lesion", DAY, None, zonas=["hombro"])
        self.assertEqual(db.zonas_en_recuperacion(self.conn, 1, DAY), frozenset())


# ---- leer_entreno marks a set as deliberate, never a drop ---------------------------------------

class LeerEntrenoMarcadoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _insert_activity(self.conn, 1, "a1", DAY)
        self.conn.commit()

    def _series(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": DAY}, "completo")
        return out["sesiones"][0]["series"]

    def test_a_set_in_the_zona_carries_the_line(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("hombro")])
        db.crear_periodo(self.conn, 1, "lesion", DAY, None, zonas=["hombro"])
        self.conn.commit()
        self.assertEqual(self._series()[0]["liviano_a_proposito"],
                         "Más liviano a propósito: hombro en recuperación")

    def test_a_set_outside_the_zona_carries_no_line(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("pecho")])
        db.crear_periodo(self.conn, 1, "lesion", DAY, None, zonas=["hombro"])
        self.conn.commit()
        self.assertIsNone(self._series()[0]["liviano_a_proposito"])

    def test_no_line_once_the_period_closes(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("hombro")])
        db.crear_periodo(self.conn, 1, "lesion", "2026-11-01", "2026-11-05", zonas=["hombro"])
        self.conn.commit()
        self.assertIsNone(self._series()[0]["liviano_a_proposito"])

    def test_the_periods_own_note_never_appears(self):
        secreto = "ZZNOTA-SECRETA-NO-DEBE-SALIR"
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("hombro")])
        db.crear_periodo(self.conn, 1, "lesion", DAY, None, note=secreto, zonas=["hombro"])
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": DAY}, "completo")
        self.assertNotIn(secreto, json.dumps(out, ensure_ascii=False))


# ---- leer_dia marks the same way, through db.load_day's own zonas_limitadas --------------------

class LeerDiaMarcadoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _insert_activity(self.conn, 1, "a1", DAY)
        self.conn.commit()

    def test_a_set_in_the_zona_carries_the_line(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("hombro")])
        db.crear_periodo(self.conn, 1, "lesion", DAY, None, zonas=["hombro"])
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(out["activities"][0]["liviano_a_proposito"],
                         "Más liviano a propósito: hombro en recuperación")
        self.assertNotIn("zonas_limitadas", out["activities"][0])

    def test_a_set_outside_the_zona_carries_no_line(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("pecho")])
        db.crear_periodo(self.conn, 1, "lesion", DAY, None, zonas=["hombro"])
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertIsNone(out["activities"][0]["liviano_a_proposito"])

    def test_with_no_open_lesion_the_field_is_none(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("hombro")])
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertIsNone(out["activities"][0]["liviano_a_proposito"])


# ---- leer_hoy lists the open limitation as context for planear_entreno -------------------------

class LeerHoyLimitacionesTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def _hoy(self):
        return herramientas.ejecutar(self.conn, 1, "leer_hoy", {}, "completo")

    def test_open_lesion_with_zonas_is_listed(self):
        db.set_metric(self.conn, DAY, "resting_hr", 55.0, "garmin", 1)
        self.conn.commit()
        db.crear_periodo(self.conn, 1, "lesion", DAY, None, zonas=["hombro"])
        self.conn.commit()
        out = self._hoy()
        self.assertEqual(out["limitaciones"], [{"tipo": "lesion", "desde": DAY, "zonas": ["hombro"]}])

    def test_the_notes_never_appears_in_limitaciones(self):
        secreto = "ZZNOTA-SECRETA-NO-DEBE-SALIR"
        db.set_metric(self.conn, DAY, "resting_hr", 55.0, "garmin", 1)
        db.crear_periodo(self.conn, 1, "lesion", DAY, None, note=secreto, zonas=["hombro"])
        self.conn.commit()
        out = self._hoy()
        self.assertNotIn(secreto, json.dumps(out, ensure_ascii=False))

    def test_a_lesion_period_with_no_zonas_lists_an_empty_list(self):
        db.set_metric(self.conn, DAY, "resting_hr", 55.0, "garmin", 1)
        db.crear_periodo(self.conn, 1, "lesion", DAY, None)
        self.conn.commit()
        out = self._hoy()
        self.assertEqual(out["limitaciones"], [{"tipo": "lesion", "desde": DAY, "zonas": []}])

    def test_a_closed_lesion_is_not_listed(self):
        db.set_metric(self.conn, DAY, "resting_hr", 55.0, "garmin", 1)
        db.crear_periodo(self.conn, 1, "lesion", "2026-11-01", "2026-11-05", zonas=["hombro"])
        self.conn.commit()
        out = self._hoy()
        self.assertEqual(out["limitaciones"], [])

    def test_a_non_lesion_period_is_not_listed(self):
        db.set_metric(self.conn, DAY, "resting_hr", 55.0, "garmin", 1)
        db.crear_periodo(self.conn, 1, "malestar", DAY, None)
        self.conn.commit()
        out = self._hoy()
        self.assertEqual(out["limitaciones"], [])


if __name__ == "__main__":
    unittest.main()
