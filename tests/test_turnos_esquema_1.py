"""BODY-TURNOS-ESQUEMA-1: person.trabaja_turnos (NULL/0/1), its one-time backfill, shift_pattern,
shift_batch.kind, and the three accessors (get_trabaja_turnos, set_trabaja_turnos,
trabaja_turnos_si). Invented demo data only, no real health figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, privacidad


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


_OLD_PERSON_SQL = """
CREATE TABLE person (
    id          INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    timezone    TEXT NOT NULL DEFAULT 'America/Bogota',
    language    TEXT NOT NULL DEFAULT 'es'
);
"""

_OLD_SHIFT_BATCH_SQL = """
CREATE TABLE shift_batch (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    file_name       TEXT NOT NULL,
    received        TEXT NOT NULL,
    extracted_at    TEXT NOT NULL,
    source_person   TEXT,
    source_timezone TEXT,
    status          TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'dismissed', 'superseded')),
    day_count       INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    UNIQUE (person_id, file_name, received, extracted_at)
);
"""


def _old_db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """A DB built from the schema as it stood before this slice: person without trabaja_turnos,
    shift_batch without kind, no shift_pattern table at all. SQLite's ALTER TABLE DROP COLUMN
    refuses a column named by a CHECK constraint (trabaja_turnos, kind), so person and shift_batch
    are dropped and recreated in their pre-slice shape instead, the way tests/test_cuenta_schema.py
    drops whole tables for its own pre-slice shape."""
    conn = _db(case, name)
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE person")
    conn.execute(_OLD_PERSON_SQL)
    conn.execute("DROP TABLE shift_batch")
    conn.execute(_OLD_SHIFT_BATCH_SQL)
    conn.execute("DROP TABLE shift_pattern")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    return conn


class BackfillTest(unittest.TestCase):
    def test_migration_backfills_only_people_with_shift_data(self):
        conn = _old_db(self, "backfill.db")
        conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'A')")
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'C')")
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, start_local) VALUES (1, '2026-11-01', 'day', NULL)"
        )
        conn.execute(
            "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, day_count, created_at) "
            "VALUES (2, 'turnos.pdf', '2026-11-01', '2026-11-01T00:00:00+00:00', 1, '2026-11-01T00:00:00+00:00')"
        )
        conn.commit()

        db.init(conn)

        rows = {r["id"]: r["trabaja_turnos"] for r in conn.execute("SELECT id, trabaja_turnos FROM person")}
        self.assertEqual(rows[1], 1)
        self.assertEqual(rows[2], 1)
        self.assertIsNone(rows[3])

    def test_backfill_runs_once_and_a_later_answer_is_never_undone(self):
        conn = _old_db(self, "backfill_once.db")
        conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'A')")
        conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'C')")
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, start_local) VALUES (1, '2026-11-01', 'day', NULL)"
        )
        conn.commit()

        db.init(conn)  # first migration: A -> 1 (backfilled), C -> NULL

        db.set_trabaja_turnos(conn, False, 1)
        db.set_trabaja_turnos(conn, False, 3)
        db.init(conn)  # re-running must not re-backfill and flip A or C back to 1

        rows = {r["id"]: r["trabaja_turnos"] for r in conn.execute("SELECT id, trabaja_turnos FROM person")}
        self.assertEqual(rows[1], 0)
        self.assertEqual(rows[3], 0)


class AccessorTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "accessors.db")
        db.init(self.conn)

    def test_fresh_person_is_not_answered(self):
        self.assertIsNone(db.get_trabaja_turnos(self.conn, db.DEFAULT_PERSON_ID))
        self.assertFalse(db.trabaja_turnos_si(self.conn, db.DEFAULT_PERSON_ID))

    def test_trabaja_turnos_si_is_true_only_for_one(self):
        pid = db.DEFAULT_PERSON_ID
        db.set_trabaja_turnos(self.conn, False, pid)
        self.assertFalse(db.trabaja_turnos_si(self.conn, pid))
        self.assertEqual(db.get_trabaja_turnos(self.conn, pid), False)

        db.set_trabaja_turnos(self.conn, True, pid)
        self.assertTrue(db.trabaja_turnos_si(self.conn, pid))
        self.assertEqual(db.get_trabaja_turnos(self.conn, pid), True)

    def test_trabaja_turnos_si_infers_from_shift_data_when_null(self):
        """BODY-TURNOS-INFERIR-1: a fresh database never runs the migration's backfill, so NULL
        with shift data must be read-time-inferred as True; an explicit 0 is never overridden."""
        pid = db.DEFAULT_PERSON_ID
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, start_local) VALUES (?, '2026-11-01', 'day', NULL)",
            (pid,),
        )
        self.conn.commit()
        self.assertIsNone(db.get_trabaja_turnos(self.conn, pid))
        self.assertTrue(db.trabaja_turnos_si(self.conn, pid))

        other = "INSERT INTO person (id, display_name) VALUES (2, 'B')"
        self.conn.execute(other)
        self.conn.execute(
            "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, day_count, created_at) "
            "VALUES (2, 'turnos.pdf', '2026-11-01', '2026-11-01T00:00:00+00:00', 1, '2026-11-01T00:00:00+00:00')"
        )
        self.conn.commit()
        self.assertIsNone(db.get_trabaja_turnos(self.conn, 2))
        self.assertTrue(db.trabaja_turnos_si(self.conn, 2))

        self.conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'C')")
        self.conn.commit()
        self.assertIsNone(db.get_trabaja_turnos(self.conn, 3))
        self.assertFalse(db.trabaja_turnos_si(self.conn, 3))

        db.set_trabaja_turnos(self.conn, False, pid)
        self.assertFalse(db.trabaja_turnos_si(self.conn, pid))

    def test_set_trabaja_turnos_rejects_a_non_bool(self):
        with self.assertRaises(TypeError):
            db.set_trabaja_turnos(self.conn, 1, db.DEFAULT_PERSON_ID)
        with self.assertRaises(TypeError):
            db.set_trabaja_turnos(self.conn, "yes", db.DEFAULT_PERSON_ID)
        with self.assertRaises(TypeError):
            db.set_trabaja_turnos(self.conn, None, db.DEFAULT_PERSON_ID)


class FreshShapeTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "fresh.db")
        db.init(self.conn)

    def test_fresh_db_has_the_new_pieces(self):
        pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(person)")}
        self.assertIn("trabaja_turnos", pcols)
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'shift_pattern'"
        ).fetchone()
        self.assertIsNotNone(row)
        sbcols = {r[1] for r in self.conn.execute("PRAGMA table_info(shift_batch)")}
        self.assertIn("kind", sbcols)

    def test_existing_shift_batch_row_reads_kind_import_after_migration(self):
        conn = _old_db(self, "old_batch.db")
        conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'A')")
        conn.execute(
            "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, day_count, created_at) "
            "VALUES (1, 'turnos.pdf', '2026-11-01', '2026-11-01T00:00:00+00:00', 1, '2026-11-01T00:00:00+00:00')"
        )
        conn.commit()

        db.init(conn)

        kind = conn.execute("SELECT kind FROM shift_batch WHERE person_id = 1").fetchone()["kind"]
        self.assertEqual(kind, "import")


class PrivacyCoverageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "privacy.db")
        db.init(self.conn)

    def test_export_includes_shift_pattern_and_trabaja_turnos(self):
        self.conn.execute(
            "INSERT INTO shift_pattern (person_id, start_day, cycle, updated_at) VALUES "
            "(1, '2026-11-01', '[{\"kind\": \"day\", \"start_local\": null, \"end_local\": null}]', "
            "'2026-11-01T00:00:00+00:00')"
        )
        db.set_trabaja_turnos(self.conn, True, 1)
        self.conn.commit()

        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(data["persona"]["trabaja_turnos"], 1)
        self.assertIsNotNone(data["vida"]["shift_pattern"])
        self.assertEqual(data["vida"]["shift_pattern"]["start_day"], "2026-11-01")

    def test_export_with_no_shift_pattern_is_none(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertIsNone(data["vida"]["shift_pattern"])

    def test_delete_all_removes_the_shift_pattern_row(self):
        self.conn.execute(
            "INSERT INTO shift_pattern (person_id, start_day, cycle, updated_at) VALUES "
            "(1, '2026-11-01', '[]', '2026-11-01T00:00:00+00:00')"
        )
        self.conn.commit()

        privacidad.delete_person(self.conn, 1)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM shift_pattern WHERE person_id = 1").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
