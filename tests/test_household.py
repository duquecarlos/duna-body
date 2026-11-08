"""Household source: local mirror of Cuenta's duna_hogar claim (hogar-v1)."""
import tempfile
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from pathlib import Path

from tests._db import open_conn

from body import db, household


class HouseholdTest(unittest.TestCase):
    def test_none_then_shared(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "h.db")
        db.init(conn)
        self.assertIsNone(household.household_of(conn, 1))
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        h = household.household_of(conn, 1)
        self.assertEqual(h.id, "h-1")
        self.assertEqual(h.member_ids, (1, 2))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(goal)")}
        self.assertIn("hogar_id", cols)


if __name__ == "__main__":
    unittest.main()
