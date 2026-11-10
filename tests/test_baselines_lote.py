"""body/baselines.py's batched `baselines()` must give EXACTLY the same answer, per key, as calling
`baseline()` for each key alone (BODY-AGUJETAS-1-B) -- including the marked-days exclusion
(BODY-PERIODO-1 ruling 3) and the honest-not-enough-data None.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import baselines, db


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


class BatchedBaselineTest(unittest.TestCase):
    def test_matches_single_key_calls(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(baselines.WINDOW_DAYS)
        keys = ["hrv_last_night", "resting_hr", "steps"]  # steps left with no data -> honest None
        d, wobble = start, 0
        while d < hoy:
            wobble = (wobble + 1) % 4
            if wobble != 3:  # a few gaps, same honest-missing-day shape as the real metric
                db.set_metric(conn, d.isoformat(), "hrv_last_night", 50.0 + wobble, "garmin", 1)
                db.set_metric(conn, d.isoformat(), "resting_hr", 60.0 - wobble, "garmin", 1)
            d += timedelta(1)
        conn.commit()

        batched = baselines.baselines(conn, keys, hoy.isoformat(), person_id=1)
        for k in keys:
            self.assertEqual(batched[k], baselines.baseline(conn, k, hoy.isoformat(), person_id=1))
        self.assertIsNotNone(batched["hrv_last_night"])
        self.assertIsNotNone(batched["resting_hr"])
        self.assertIsNone(batched["steps"])

    def test_marked_days_excluded_same_as_single_key(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(baselines.WINDOW_DAYS)
        keys = ["hrv_last_night", "resting_hr"]
        d = start
        while d < hoy:
            db.set_metric(conn, d.isoformat(), "hrv_last_night", 50.0, "garmin", 1)
            db.set_metric(conn, d.isoformat(), "resting_hr", 55.0, "garmin", 1)
            d += timedelta(1)
        db.crear_periodo(conn, 1, "malestar", (hoy - timedelta(10)).isoformat(), (hoy - timedelta(5)).isoformat())
        conn.commit()

        batched = baselines.baselines(conn, keys, hoy.isoformat(), person_id=1)
        for k in keys:
            single = baselines.baseline(conn, k, hoy.isoformat(), person_id=1)
            self.assertEqual(batched[k], single)
            self.assertEqual(single.n, baselines.WINDOW_DAYS - 6)  # 6 marked days left out

    def test_empty_keys(self):
        conn = _mkconn(self)
        self.assertEqual(baselines.baselines(conn, [], "2026-06-01", person_id=1), {})


if __name__ == "__main__":
    unittest.main()
