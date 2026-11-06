"""Long-term profile: monthly means, normal range, typical week."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
import tempfile
from datetime import date, timedelta
from pathlib import Path

from body import db, profile


class ProfileTest(unittest.TestCase):
    def test_profile(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "p.db")
        db.init(conn)
        start = date(2026, 1, 1)
        for i in range(90):
            d = start + timedelta(i)
            db.set_metric(conn, d.isoformat(), "sleep_minutes", 400 if d.weekday() == 2 else 420, "garmin")
        conn.commit()
        p = profile.metric_profile(conn, "sleep_minutes")
        self.assertEqual(len(p.months), 3)
        self.assertEqual(p.weekday[2], 400)  # Wednesdays are the short nights
        self.assertIn("<svg", profile.month_svg(p))
        self.assertIsNone(profile.metric_profile(conn, "steps"))  # no data -> honest None


if __name__ == "__main__":
    unittest.main()
