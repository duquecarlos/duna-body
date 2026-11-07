"""vida-v1 weekly section (what Personal's Sunday review reads)."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from datetime import date, timedelta
from pathlib import Path
import tempfile

from body import db, goals, household, vida_export


class VidaExportTest(unittest.TestCase):
    def test_week_section_shape(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "v.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        for i in range(5):
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "resting_hr", 55 + i, "garmin", 1)
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "readiness", 70, "body-v1", 1)
        conn.commit()
        s = vida_export.week_section(conn, monday, 1)
        self.assertEqual(s["area"], "body")
        self.assertEqual(s["week_start"], "2026-11-02")
        by_key = {m["key"]: m for m in s["metrics"]}
        self.assertEqual(by_key["body.resting_hr"]["value"], 57.0)
        self.assertEqual(by_key["body.readiness"]["kind"], "estimated")
        self.assertTrue(all(m["visibility"] == "owner" for m in s["metrics"]))
        self.assertIn("5 of 7", s["summary"])
        self.assertEqual(s["goals"], [])
        self.assertEqual(s["goal_checks"], [])

    def test_goals_only_active_with_their_own_check(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "v2.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        proposed_id = goals.propose(conn, 1, "Proposed goal", "2026-11-02", metric_key="steps", direction="raise",
                                     target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        active_id = goals.propose(conn, 1, "Active goal", "2026-11-02", metric_key="resting_hr", direction="lower",
                                   target_value=50.0, baseline_value=60.0, deadline="2026-12-14")
        goals.activate(conn, active_id)
        for i in range(7):
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "resting_hr", 55, "garmin", 1)
        goals.weekly_check(conn, active_id, 1, "2026-11-02")

        s = vida_export.week_section(conn, monday, 1)
        self.assertEqual(s["goals"], [{"goal_id": active_id, "on_track": True}])
        self.assertNotIn(proposed_id, [g["goal_id"] for g in s["goals"]])

    def test_goal_checks_for_shared_household_goals(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "v3.db")
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executescript(household.SCHEMA)
        conn.executemany("INSERT INTO hogar_local (person_id, hogar_id) VALUES (?, ?)", [(1, "h-1"), (2, "h-1")])
        conn.commit()
        monday = date(2026, 11, 2)
        gid = goals.propose(conn, 1, "Shared steps goal", "2026-11-02", metric_key="steps", direction="raise",
                             target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14", hogar_id="h-1",
                             visibility="household")
        goals.activate(conn, gid)
        for i in range(7):
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "steps", 9500, "garmin", 1)
        goals.weekly_check(conn, gid, 1, "2026-11-02")

        s = vida_export.week_section(conn, monday, 1)
        self.assertEqual(len(s["goal_checks"]), 1)
        gc = s["goal_checks"][0]
        self.assertEqual(gc["goal_id"], gid)
        self.assertEqual(gc["person"], vida_export.PERSON_KEY)
        self.assertEqual(gc["week_start"], "2026-11-02")
        self.assertTrue(gc["on_track"])


if __name__ == "__main__":
    unittest.main()
