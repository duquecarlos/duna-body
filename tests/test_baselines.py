"""body/baselines.py's `baseline()` with a `body/lector.py` Lector must give EXACTLY the same
answer as its original per-call query path (BODY-RENDIMIENTO-2)."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import baselines, db
from body.lector import Lector


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


class BaselineLectorEqualityTest(unittest.TestCase):
    def test_matches_with_and_without_a_lector(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(baselines.WINDOW_DAYS)
        d, wobble = start, 0
        while d < hoy:
            wobble = (wobble + 1) % 4
            if wobble != 3:  # a few gaps, same honest-missing-day shape as the real metric
                db.set_metric(conn, d.isoformat(), "hrv_last_night", 50.0 + wobble, "garmin", 1)
            d += timedelta(1)
        conn.commit()
        lector = Lector(conn, 1, start.isoformat(), hoy.isoformat(), ["hrv_last_night"])
        without = baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), person_id=1)
        with_lector = baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), person_id=1, lector=lector)
        self.assertIsNotNone(without)
        self.assertEqual(without, with_lector)

    def test_not_enough_data_matches_too(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(baselines.WINDOW_DAYS)
        lector = Lector(conn, 1, start.isoformat(), hoy.isoformat(), ["hrv_last_night"])
        without = baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), person_id=1)
        with_lector = baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), person_id=1, lector=lector)
        self.assertIsNone(without)
        self.assertIsNone(with_lector)


class PeriodKindBaselineExclusionTest(unittest.TestCase):
    """BODY-PLAN-FASES-4 ruling 3/6: a kind's own excluye_del_rango decides whether its days leave
    baseline()'s window -- an open 'molestia' period never empties it, the exact opposite of an
    open 'lesion' one covering the same days."""

    def test_open_molestia_period_leaves_the_baseline_intact(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(baselines.WINDOW_DAYS)
        d = start
        while d < hoy:
            db.set_metric(conn, d.isoformat(), "hrv_last_night", 50.0, "garmin", 1)
            d += timedelta(1)
        db.crear_periodo(conn, 1, "molestia", start.isoformat(), None)
        conn.commit()
        b = baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), person_id=1)
        self.assertIsNotNone(b)
        self.assertEqual(b.n, baselines.WINDOW_DAYS)

    def test_open_lesion_period_covering_the_same_days_empties_it(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(baselines.WINDOW_DAYS)
        d = start
        while d < hoy:
            db.set_metric(conn, d.isoformat(), "hrv_last_night", 50.0, "garmin", 1)
            d += timedelta(1)
        db.crear_periodo(conn, 1, "lesion", start.isoformat(), None)
        conn.commit()
        self.assertIsNone(baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), person_id=1))


if __name__ == "__main__":
    unittest.main()
