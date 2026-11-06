"""Household source: local mirror of Cuenta's duna_hogar claim (hogar-v1)."""
import tempfile
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from pathlib import Path

from body import db, household


class HouseholdTest(unittest.TestCase):
    def test_none_then_shared(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "h.db")
        db.init(conn)
        self.assertIsNone(household.household_of(conn, 1))
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executescript(household.SCHEMA)
        conn.executemany("INSERT INTO hogar_local (person_id, hogar_id) VALUES (?, ?)", [(1, "h-1"), (2, "h-1")])
        h = household.household_of(conn, 1)
        self.assertEqual(h.id, "h-1")
        self.assertEqual(h.member_ids, (1, 2))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(goal)")}
        self.assertIn("hogar_id", cols)


if __name__ == "__main__":
    unittest.main()
