"""BODY-CUENTA-SCHEMA-1: the schema half of Cuenta Duna sign-in -- person.cuenta_sub, server-side
sessions (sesion), the short-lived sign-in attempt (inicio_sesion) and consent records
(consentimiento). Additive only; no sign-in code here (BODY-CUENTA-RP-1 follows). Invented demo
data only, no real health figures.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db


def _db(case: unittest.TestCase, name: str) -> sqlite3.Connection:
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


def _old_db(case: unittest.TestCase, name: str) -> sqlite3.Connection:
    """A DB built from the schema as it stood before this slice. db.SCHEMA's CREATE TABLE person
    never defined cuenta_sub (that column is only ever added by _migrate, like batch_id before it),
    so executing today's SCHEMA and dropping the three new tables reproduces the pre-slice shape
    exactly -- the person/goal/metric_def DDL text is unchanged by this slice."""
    conn = _db(case, name)
    conn.executescript(db.SCHEMA)
    conn.execute("DROP TABLE sesion")
    conn.execute("DROP TABLE inicio_sesion")
    conn.execute("DROP TABLE consentimiento")
    conn.commit()
    return conn


class FreshDbTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "fresh.db")
        db.init(self.conn)

    def test_person_has_cuenta_sub_column(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(person)")}
        self.assertIn("cuenta_sub", cols)

    def test_person_cuenta_sub_unique_index_exists(self):
        names = {r[1] for r in self.conn.execute("PRAGMA index_list(person)")}
        self.assertIn("person_cuenta_sub", names)

    def test_new_tables_exist(self):
        for name in ("sesion", "inicio_sesion", "consentimiento"):
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            self.assertIsNotNone(row, f"{name} missing")

    def test_init_twice_is_idempotent(self):
        before_person = self.conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        before_metric = self.conn.execute("SELECT COUNT(*) FROM metric_def").fetchone()[0]
        db.init(self.conn)  # must not raise
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM person").fetchone()[0], before_person)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM metric_def").fetchone()[0], before_metric)


class MigrationPreservesRowsTest(unittest.TestCase):
    def test_init_on_old_schema_adds_pieces_without_changing_existing_rows(self):
        conn = _old_db(self, "migrate.db")
        conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Other')")
        conn.execute(
            "INSERT INTO metric_def (key, label, unit, better) "
            "VALUES ('custom_metric_test', 'Custom demo metric', 'u', 'lower')"
        )
        conn.execute(
            "INSERT INTO goal (id, person_id, title, kind, metric_key, direction, target_value, "
            "baseline_value, start_day, deadline, status) VALUES "
            "(1, 1, 'Lower demo metric', 'metric', 'custom_metric_test', 'lower', 50, 60, "
            "'2026-10-01', NULL, 'active')"
        )
        conn.commit()

        before_person = [dict(r) for r in conn.execute("SELECT * FROM person ORDER BY id")]
        before_metric = [dict(r) for r in conn.execute(
            "SELECT key, label, unit, better, kind, origen FROM metric_def WHERE key='custom_metric_test'")]
        before_goal = [dict(r) for r in conn.execute("SELECT * FROM goal ORDER BY id")]

        db.init(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(person)")}
        self.assertIn("cuenta_sub", cols)
        for name in ("sesion", "inicio_sesion", "consentimiento"):
            row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            self.assertIsNotNone(row, f"{name} missing")

        after_person = conn.execute(
            "SELECT id, display_name, timezone, language, cuenta_sub FROM person ORDER BY id"
        ).fetchall()
        self.assertEqual(len(after_person), len(before_person))
        for before, after in zip(before_person, after_person):
            self.assertEqual(before["id"], after["id"])
            self.assertEqual(before["display_name"], after["display_name"])
            self.assertEqual(before["timezone"], after["timezone"])
            self.assertEqual(before["language"], after["language"])
            self.assertIsNone(after["cuenta_sub"])

        after_metric = [dict(r) for r in conn.execute(
            "SELECT key, label, unit, better, kind, origen FROM metric_def WHERE key='custom_metric_test'")]
        self.assertEqual(before_metric, after_metric)
        after_agregado = conn.execute(
            "SELECT agregado FROM metric_def WHERE key='custom_metric_test'").fetchone()["agregado"]
        self.assertIsNone(after_agregado)
        after_goal = [dict(r) for r in conn.execute("SELECT * FROM goal ORDER BY id")]
        self.assertEqual(before_goal, after_goal)


class CuentaSubUniquenessTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "sub.db")
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Other')")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'Third')")
        self.conn.commit()

    def test_many_persons_may_have_null_sub(self):
        rows = self.conn.execute("SELECT COUNT(*) FROM person WHERE cuenta_sub IS NULL").fetchone()[0]
        self.assertEqual(rows, 3)  # local person + Other + Third, all unlinked

    def test_two_persons_cannot_share_a_non_null_sub(self):
        self.conn.execute("UPDATE person SET cuenta_sub = 'sub-demo-aaa' WHERE id = 2")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE person SET cuenta_sub = 'sub-demo-aaa' WHERE id = 3")


class ConsentimientoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "consent.db")
        db.init(self.conn)

    def test_duplicate_person_version_refused(self):
        self.conn.execute(
            "INSERT INTO consentimiento (person_id, version, accepted_at) VALUES (1, 'v1', '2026-11-06T00:00:00Z')"
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO consentimiento (person_id, version, accepted_at) "
                "VALUES (1, 'v1', '2026-11-06T01:00:00Z')"
            )


class SesionForeignKeyTest(unittest.TestCase):
    def test_session_for_a_missing_person_is_refused(self):
        conn = _db(self, "fk.db")
        db.init(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sesion (id_hash, person_id, created_at, last_seen_at, expires_at) "
                "VALUES ('demo-hash', 999, '2026-11-06T00:00:00Z', '2026-11-06T00:00:00Z', "
                "'2026-11-06T01:00:00Z')"
            )


if __name__ == "__main__":
    unittest.main()
