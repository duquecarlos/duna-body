"""Long-term profile: monthly means, normal range, typical week."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
import tempfile
from datetime import date, timedelta
from pathlib import Path

from tests._db import open_conn

from body import db, profile


class ProfileTest(unittest.TestCase):
    def test_profile(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "p.db")
        db.init(conn)
        start = date(2026, 1, 1)
        for i in range(90):
            d = start + timedelta(i)
            db.set_metric(conn, d.isoformat(), "sleep_minutes", 400 if d.weekday() == 2 else 420, "garmin", 1)
        conn.commit()
        p = profile.metric_profile(conn, "sleep_minutes", person_id=1)
        self.assertEqual(len(p.months), 3)
        self.assertEqual(p.weekday[2], 400)  # Wednesdays are the short nights
        self.assertIn("<svg", profile.month_svg(p))
        self.assertIsNone(profile.metric_profile(conn, "steps", person_id=1))  # no data -> honest None
        self.assertIn('href="/trends?m=2026-01#sleep_minutes"', profile.month_svg(p))  # month drills to its days

    def test_window(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "w.db")
        db.init(conn)
        for i in range(20):
            if i != 15:  # one night without the watch
                db.set_metric(conn, (date(2026, 3, 1) + timedelta(i)).isoformat(), "resting_hr", 50 + (i >= 10),
                             "garmin", 1)
        conn.commit()
        w = profile.window(conn, "resting_hr", date(2026, 3, 11), date(2026, 3, 20), 1)
        self.assertEqual(len(w.values), 9)
        self.assertEqual((w.mean, w.prev_mean), (51, 50))
        svg = profile.window_svg(w, 49, 52, nights={"2026-03-12"})
        self.assertEqual(svg.count("<polyline"), 2)        # the missing day breaks the line, no zero drawn
        self.assertIn('href="/day/2026-03-16"', svg)       # every day, even the gap, opens its day
        self.assertIn("var(--night)", svg)                 # night shift shaded


if __name__ == "__main__":
    unittest.main()
