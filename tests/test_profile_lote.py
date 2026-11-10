"""body/profile.py's batched `metric_profiles()` and `windows()` must give EXACTLY the same answer,
per key, as calling `metric_profile()` / `window()` for each key alone (BODY-AGUJETAS-1-B)."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, profile


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "p.db")
    db.init(conn)
    return conn


class BatchedProfileTest(unittest.TestCase):
    def test_metric_profiles_matches_single_key_calls(self):
        conn = _mkconn(self)
        start = date(2026, 1, 1)
        for i in range(90):
            d = start + timedelta(i)
            db.set_metric(conn, d.isoformat(), "sleep_minutes", 400 if d.weekday() == 2 else 420, "garmin", 1)
        for i in range(3):  # fewer than min_days -> honest None, same as the single-key path
            db.set_metric(conn, (date(2026, 4, 1) + timedelta(i)).isoformat(), "body_weight_kg", 70.0, "self", 1)
        conn.commit()

        keys = ["sleep_minutes", "body_weight_kg", "steps"]  # steps: no data at all
        min_days = {"sleep_minutes": 14, "body_weight_kg": 1, "steps": 14}
        min_month_points = {"sleep_minutes": 7, "body_weight_kg": 2, "steps": 7}
        batch = profile.metric_profiles(conn, keys, person_id=1, min_days_por_clave=min_days,
                                        min_month_points_por_clave=min_month_points)
        for k in keys:
            single = profile.metric_profile(conn, k, person_id=1, min_days=min_days[k],
                                             min_month_points=min_month_points[k])
            self.assertEqual(batch[k], single)
        self.assertIsNotNone(batch["sleep_minutes"])
        self.assertIsNotNone(batch["body_weight_kg"])
        self.assertIsNone(batch["steps"])

    def test_metric_profiles_defaults_match_metric_profile_defaults(self):
        conn = _mkconn(self)
        start = date(2026, 1, 1)
        for i in range(20):
            db.set_metric(conn, (start + timedelta(i)).isoformat(), "resting_hr", 55.0, "garmin", 1)
        conn.commit()
        batch = profile.metric_profiles(conn, ["resting_hr"], person_id=1)
        self.assertEqual(batch["resting_hr"], profile.metric_profile(conn, "resting_hr", person_id=1))

    def test_metric_profiles_empty_keys(self):
        conn = _mkconn(self)
        self.assertEqual(profile.metric_profiles(conn, [], person_id=1), {})

    def test_windows_matches_single_key_calls(self):
        conn = _mkconn(self)
        for i in range(20):
            if i != 15:  # one night without the watch
                db.set_metric(conn, (date(2026, 3, 1) + timedelta(i)).isoformat(), "resting_hr", 50 + (i >= 10),
                             "garmin", 1)
                db.set_metric(conn, (date(2026, 3, 1) + timedelta(i)).isoformat(), "hrv_last_night", 60 + i,
                             "garmin", 1)
        conn.commit()
        keys = ["resting_hr", "hrv_last_night", "steps"]  # steps: no data at all
        batch = profile.windows(conn, keys, date(2026, 3, 11), date(2026, 3, 20), person_id=1)
        for k in keys:
            self.assertEqual(batch[k], profile.window(conn, k, date(2026, 3, 11), date(2026, 3, 20), 1))
        self.assertEqual(len(batch["resting_hr"].values), 9)
        self.assertEqual((batch["resting_hr"].mean, batch["resting_hr"].prev_mean), (51, 50))
        self.assertIsNone(batch["steps"].mean)

    def test_windows_empty_keys(self):
        conn = _mkconn(self)
        self.assertEqual(profile.windows(conn, [], date(2026, 3, 11), date(2026, 3, 20), person_id=1), {})


if __name__ == "__main__":
    unittest.main()
