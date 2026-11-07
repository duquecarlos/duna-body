"""Baselines and readiness v1 on synthetic days (no Garmin, temp database)."""
import tempfile
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from datetime import date, timedelta
from pathlib import Path

from tests._db import open_conn

from body import baselines, db, readiness


def make_db(case: unittest.TestCase, days=31, hrv=50.0, rhr=55.0, stress=30.0):
    conn = open_conn(case, Path(tempfile.mkdtemp()) / "t.db")
    db.init(conn)
    start = date(2026, 10, 1)
    for i in range(days):
        d = (start + timedelta(i)).isoformat()
        wobble = (-1) ** i  # alternating +/-1 so the spread is not zero
        for k, v in (("hrv_last_night", hrv + 2 * wobble), ("resting_hr", rhr + wobble),
                     ("stress_avg", stress + 3 * wobble), ("sleep_minutes", 450), ("sleep_need_min", 480),
                     ("body_battery_wake", 75)):
            db.set_metric(conn, d, k, v, "garmin", 1)
    conn.commit()
    return conn, (start + timedelta(days - 1)).isoformat()


class ReadinessTest(unittest.TestCase):
    def test_baseline_excludes_the_day_and_needs_seven_days(self):
        conn, last = make_db(self, days=5)
        self.assertIsNone(baselines.baseline(conn, "hrv_last_night", last, person_id=1))
        conn, last = make_db(self)
        b = baselines.baseline(conn, "hrv_last_night", last, person_id=1)
        self.assertEqual(b.n, 30)
        self.assertAlmostEqual(b.mean, 50, delta=0.2)

    def test_normal_day_scores_near_seventy_plus(self):
        conn, last = make_db(self)
        r = readiness.compute(conn, last, 1)
        self.assertIsNotNone(r.score)
        self.assertTrue(60 <= r.score <= 85, r.score)
        self.assertEqual(len(r.parts), 5)

    def test_bad_night_scores_lower(self):
        conn, last = make_db(self)
        normal = readiness.compute(conn, last, 1).score
        for k, v in (("hrv_last_night", 38), ("sleep_minutes", 300), ("body_battery_wake", 40), ("resting_hr", 60)):
            db.set_metric(conn, last, k, v, "garmin", 1)
        bad = readiness.compute(conn, last, 1)
        self.assertLess(bad.score, normal - 20)
        self.assertEqual(bad.band, "low")

    def test_too_little_data_gives_no_score(self):
        conn, last = make_db(self, days=3)
        r = readiness.compute(conn, last, 1)
        self.assertIsNone(r.score)  # only sleep + battery available; honest "not yet"

    def test_update_stores_estimate_with_version(self):
        conn, last = make_db(self)
        readiness.update(conn, last, 1)
        row = conn.execute("SELECT source FROM daily_metric WHERE metric_key='readiness' AND day=?", (last,)).fetchone()
        self.assertEqual(row["source"], readiness.VERSION)
        kind = conn.execute("SELECT kind FROM metric_def WHERE key='readiness'").fetchone()["kind"]
        self.assertEqual(kind, "estimated")


if __name__ == "__main__":
    unittest.main()
