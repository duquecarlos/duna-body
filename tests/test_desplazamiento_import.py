"""ORCH-CARLOS-DELEGATES-TIER-1-1: Personal's daily commute summary (body/desplazamiento_import.py)
-- the metric_def.origen 'vida' widening, the four vehicle/walking/cycling metric rows, the generic
fuente_importacion table, the importer itself, and the two ways in (local vivo task, hosted device-
key route). Invented demo data only, no real health or location figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


# ---- ruling 1: metric_def.origen gains 'vida' ------------------------------------------------------

_OLD_METRIC_DEF_SQL = """
CREATE TABLE metric_def (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    unit    TEXT NOT NULL,
    better  TEXT NOT NULL CHECK (better IN ('higher', 'lower', 'neutral')),
    kind    TEXT NOT NULL DEFAULT 'measured' CHECK (kind IN ('measured', 'estimated')),
    origen  TEXT NOT NULL DEFAULT 'garmin' CHECK (origen IN ('garmin', 'self'))
);
"""


def _old_db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """A DB built from the current SCHEMA (so every other table is already right), with
    metric_def dropped and recreated in its pre-slice shape -- the same technique
    tests/test_entreno_schema.py uses for metric_def/plan/propuesta_agente."""
    conn = _db(case, name)
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE metric_def")
    conn.execute(_OLD_METRIC_DEF_SQL)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class FreshDbOrigenVidaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "fresh.db")
        db.init(self.conn)

    def test_the_four_vida_rows_are_seeded(self):
        rows = {
            r["key"]: (r["origen"], r["kind"], r["better"], r["agregado"]) for r in self.conn.execute(
                "SELECT key, origen, kind, better, agregado FROM metric_def WHERE key IN "
                "('vehicle_minutes', 'vehicle_trips', 'walking_minutes', 'cycling_minutes')"
            )
        }
        self.assertEqual(rows, {
            "vehicle_minutes": ("vida", "measured", "neutral", None),
            "vehicle_trips": ("vida", "measured", "neutral", None),
            "walking_minutes": ("vida", "measured", "neutral", None),
            "cycling_minutes": ("vida", "measured", "neutral", None),
        })

    def test_vida_origen_is_accepted_by_the_check(self):
        self.conn.execute(
            "INSERT INTO metric_def (key, label, unit, better, kind, origen) VALUES "
            "('zz_prueba_vida', 'Prueba', 'u', 'neutral', 'measured', 'vida')"
        )
        self.conn.commit()

    def test_fuente_importacion_table_exists(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(fuente_importacion)")}
        self.assertEqual(cols, {
            "person_id", "fuente", "primer_dia", "ultimo_dia", "computed_at", "dias",
            "importado_at", "ultimo_error", "ultimo_error_at",
        })

    def test_init_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM metric_def WHERE origen = 'vida'"
        ).fetchone()["n"]
        self.assertEqual(rows, 4)


class OldShapeMigrationTest(unittest.TestCase):
    """An old-shape DB (origen CHECK IN ('garmin', 'self') only) with an invented row migrates
    with every existing row preserved and the new origen value accepted, and the migration is
    idempotent."""

    def setUp(self):
        self.conn = _old_db(self, "vieja.db")
        self.conn.execute(
            "INSERT INTO metric_def (key, label, unit, better, kind, origen) VALUES "
            "('mi_metrica_vieja', 'Mi metrica vieja', 'u', 'higher', 'measured', 'self')"
        )
        self.conn.commit()

    def _assert_migrated_state(self):
        old_row = self.conn.execute(
            "SELECT label, origen FROM metric_def WHERE key = 'mi_metrica_vieja'"
        ).fetchone()
        self.assertEqual(old_row["label"], "Mi metrica vieja")
        self.assertEqual(old_row["origen"], "self")

        new_rows = {
            r["key"]: r["origen"] for r in self.conn.execute(
                "SELECT key, origen FROM metric_def WHERE key IN "
                "('vehicle_minutes', 'vehicle_trips', 'walking_minutes', 'cycling_minutes')"
            )
        }
        self.assertEqual(new_rows, {
            "vehicle_minutes": "vida", "vehicle_trips": "vida",
            "walking_minutes": "vida", "cycling_minutes": "vida",
        })

        self.conn.execute(
            "INSERT INTO metric_def (key, label, unit, better, kind, origen) VALUES "
            "('zz_otra_vida', 'Otra', 'u', 'neutral', 'measured', 'vida')"
        )
        self.conn.commit()

    def test_migration_preserves_the_old_row_and_accepts_vida(self):
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


class OrigenBranchTreatsVidaAsMeasuredTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: every place code branches on origen already
    compares explicitly against 'self' (never a bare "not garmin"), so 'vida' falls through to the
    same MEASURED/garmin-sized treatment without any code change -- this pins that down for the
    three branches the ruling names."""

    def setUp(self):
        self.conn = _db(self, "branches.db")
        db.init(self.conn)

    def test_tendencias_floor_treats_vida_like_garmin(self):
        from body.rutas import tendencias
        defs = {r["key"]: dict(r) for r in self.conn.execute("SELECT * FROM metric_def")}
        for key in ("vehicle_minutes", "vehicle_trips", "walking_minutes", "cycling_minutes"):
            min_days = 1 if defs[key]["origen"] == "self" else tendencias.TREND_MIN_DAYS
            min_month = (tendencias.SELF_MIN_MONTH_POINTS if defs[key]["origen"] == "self"
                         else tendencias.GARMIN_MIN_MONTH_POINTS)
            self.assertEqual(min_days, tendencias.TREND_MIN_DAYS, key)
            self.assertEqual(min_month, tendencias.GARMIN_MIN_MONTH_POINTS, key)

    def test_catalogo_tendencias_self_group_excludes_vida(self):
        from body import catalogo_tendencias
        self_group = dict(catalogo_tendencias.TREND_GROUPS)["self"]
        for key in ("vehicle_minutes", "vehicle_trips", "walking_minutes", "cycling_minutes"):
            self.assertNotIn(key, self_group)

    def test_registrar_medida_refuses_a_vida_key(self):
        fila = self.conn.execute(
            "SELECT agregado FROM metric_def WHERE key = 'vehicle_minutes' AND origen = 'self'"
        ).fetchone()
        self.assertIsNone(fila)


if __name__ == "__main__":
    unittest.main()
