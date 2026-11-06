"""body/history.py: your whole Garmin history read as patterns, on invented demo data (temp database)."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from datetime import date, timedelta

from body import db, history, web

START = date(2025, 9, 1)
END = date(2025, 12, 31)
GAP_START = date(2025, 11, 5)
GAP_END = date(2025, 11, 7)  # 3 days with no watch data at all, inside the range

# type, [days with an activity], duration_s per session, distance_m per session (None = never tracked)
ACTIVITIES = [
    ("running", ["2025-09-05", "2025-09-12", "2025-09-19", "2025-09-26", "2025-10-03", "2025-10-10",
                 "2025-10-17", "2025-10-24", "2025-10-31", "2025-12-01"], 1800, 5000),
    ("cycling", ["2025-09-07", "2025-09-14", "2025-09-21", "2025-09-28", "2025-10-05", "2025-10-12",
                 "2025-10-19", "2025-10-26"], 1800, None),
    ("paddle_boarding", ["2025-09-10", "2025-10-08", "2025-11-12", "2025-11-19", "2025-12-03",
                         "2025-12-10"], 1800, None),         # not a known Garmin type: tests the fallback
    ("swimming", ["2025-09-15", "2025-10-15", "2025-11-15", "2025-12-15"], 1800, 1000),
    ("hiking", ["2025-09-20", "2025-10-20", "2025-12-20"], 1800, None),
    ("elliptical", ["2025-12-25"], 600, None),                # smallest total time: merged into "other"
]

NIGHT_SHIFT_DAYS = ["2025-09-05", "2025-09-12", "2025-09-19", "2025-09-26"]
DAY_SHIFT_DAYS = ["2025-09-08", "2025-09-15", "2025-09-22"]


def _mkconn():
    conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
    db.init(conn)
    return conn


def _seed(conn):
    d = START
    while d <= END:
        if GAP_START <= d <= GAP_END:
            d += timedelta(1)
            continue
        rhr = 55.0 + (6.0 if d.weekday() == 5 else 0.0)        # Saturdays clearly worse (higher resting HR)
        hrv = 50.0 + (0.2 if d.weekday() == 5 else 0.0)        # near-flat: under the noise floor
        sleep = 380.0 if d.month == 12 else 450.0              # December clearly worse (less sleep)
        for key, v in (("resting_hr", rhr), ("hrv_last_night", hrv), ("sleep_minutes", sleep)):
            db.set_metric(conn, d.isoformat(), key, v, "garmin")
        d += timedelta(1)
    for i, (kind, days, duration_s, distance_m) in enumerate(ACTIVITIES):
        for j, day in enumerate(days):
            conn.execute(
                "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
                "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (db.DEFAULT_PERSON_ID, f"act-{i}-{j}", day, f"{day}T06:00:00", kind, kind, duration_s,
                 distance_m, 120, 150, 300, "garmin"),
            )
    for day in NIGHT_SHIFT_DAYS:
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source) "
            "VALUES (?, ?, 'night', 'N', ?, ?, 'manual')",
            (db.DEFAULT_PERSON_ID, day, f"{day}T22:00:00", f"{day}T06:00:00"),
        )
    for day in DAY_SHIFT_DAYS:
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source) "
            "VALUES (?, ?, 'day', 'D', ?, ?, 'manual')",
            (db.DEFAULT_PERSON_ID, day, f"{day}T06:00:00", f"{day}T14:00:00"),
        )
    conn.commit()


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn()
        _seed(self.conn)

    def test_no_data_is_none(self):
        self.assertIsNone(history.coverage(_mkconn()))

    def test_dates_and_gap(self):
        cov = history.coverage(self.conn)
        self.assertEqual(cov["first_day"], START.isoformat())
        self.assertEqual(cov["last_day"], END.isoformat())
        self.assertEqual(cov["days_total"], (END - START).days + 1)
        self.assertEqual(cov["days_missing"], 3)
        self.assertEqual(cov["days_present"], cov["days_total"] - 3)
        self.assertEqual(cov["longest_gap_days"], 3)
        self.assertEqual(cov["longest_gap_start"], GAP_START.isoformat())
        self.assertEqual(cov["longest_gap_end"], GAP_END.isoformat())
        self.assertEqual(cov["activity_count"], sum(len(d) for _, d, _, _ in ACTIVITIES))


class TrainingTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn()
        _seed(self.conn)
        self.train = history.training(self.conn)

    def test_no_activities_is_none(self):
        conn = _mkconn()
        db.set_metric(conn, START.isoformat(), "resting_hr", 55.0, "garmin")
        self.assertIsNone(history.training(conn))

    def test_grouping_and_unknown_type_fallback(self):
        by_key = {t["key"]: t for t in self.train["types"]}
        self.assertIn("paddle_boarding", by_key)
        pb = by_key["paddle_boarding"]
        self.assertFalse(pb["known"])
        self.assertEqual(pb["fallback_label"], "Paddle boarding")
        self.assertEqual(pb["sessions"], 6)
        running = by_key["running"]
        self.assertTrue(running["known"])
        self.assertEqual(running["sessions"], 10)
        self.assertAlmostEqual(running["km"], 10 * 5000 / 1000)
        self.assertIsNone(by_key["cycling"]["km"])  # cycling never had a distance_m in this history

    def test_smallest_type_merges_into_other(self):
        keys = [t["key"] for t in self.train["types"]]
        self.assertEqual(len(keys), 6)  # 5 top types + "other"
        self.assertIn("other", keys)
        self.assertNotIn("elliptical", keys)
        other = next(t for t in self.train["types"] if t["key"] == "other")
        self.assertEqual(other["sessions"], 1)

    def test_months_cover_the_full_range(self):
        months = {m["month"] for m in self.train["months"]}
        self.assertEqual(months, {"2025-09", "2025-10", "2025-11", "2025-12"})

    def test_usual_week_is_set(self):
        self.assertIsNotNone(self.train["usual_week_sessions"])
        self.assertGreater(self.train["usual_week_sessions"], 0)

    def test_year_over_year_is_null_without_a_year_ago(self):
        self.assertIsNone(self.train["yoy"])  # no activities in 2024: an honest gap, never a 0


class PatternsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn()
        _seed(self.conn)
        self.pat = history.patterns(self.conn)

    def test_weekday_finding_above_the_noise_floor(self):
        findings = {(f["metric"], f["axis"]): f for f in self.pat["findings"]}
        self.assertIn(("resting_hr", "weekday"), findings)
        f = findings[("resting_hr", "weekday")]
        self.assertEqual(f["index"], 5)  # Saturday: the "worse" (highest) resting HR day
        self.assertAlmostEqual(f["value"], 61.0)

    def test_weekday_finding_below_the_noise_floor_is_excluded(self):
        axes = {(f["metric"], f["axis"]) for f in self.pat["findings"]}
        self.assertNotIn(("hrv_last_night", "weekday"), axes)  # 0.2 ms spread, under NOISE_FLOOR_DEFAULT

    def test_month_finding_for_sleep(self):
        findings = {(f["metric"], f["axis"]): f for f in self.pat["findings"]}
        self.assertIn(("sleep_minutes", "month"), findings)
        f = findings[("sleep_minutes", "month")]
        self.assertEqual(f["index"], 11)  # December, 0-indexed

    def test_at_most_three_findings(self):
        self.assertLessEqual(len(self.pat["findings"]), history.MAX_FINDINGS)

    def test_metrics_carry_a_band(self):
        rhr = self.pat["metrics"]["resting_hr"]
        sat = rhr["weekday"][5]
        self.assertIsNotNone(sat)
        self.assertIn("low", sat)
        self.assertIn("high", sat)


class ByShiftTest(unittest.TestCase):
    def test_empty_state_without_shifts(self):
        conn = _mkconn()
        db.set_metric(conn, START.isoformat(), "resting_hr", 55.0, "garmin")
        conn.commit()
        self.assertEqual(history.by_shift(conn), {"has_shifts": False, "kinds": []})

    def test_populated_state(self):
        conn = _mkconn()
        _seed(conn)
        result = history.by_shift(conn)
        self.assertTrue(result["has_shifts"])
        kinds = {k["kind"]: k for k in result["kinds"]}
        self.assertEqual(kinds["night"]["days"], len(NIGHT_SHIFT_DAYS))
        self.assertEqual(kinds["day"]["days"], len(DAY_SHIFT_DAYS))
        self.assertIsNotNone(kinds["night"]["metrics"]["resting_hr"])


class HistoryRouteTest(unittest.TestCase):
    """The route rendered end to end, in both languages, the same client pattern test_pipeline.py's
    `test_end_to_end_page` already uses (FastAPI's own TestClient; /settings/language to switch)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        conn = db.connect()  # the isolated BODY_HOME db that web.py's _ctx() also opens
        db.init(conn)
        _seed(conn)
        cls.client = TestClient(web.app)

    def test_returns_200_in_spanish(self):
        self.client.post("/settings/language", data={"lang": "es"})
        r = self.client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tu historia", r.text)

    def test_returns_200_in_english(self):
        self.client.post("/settings/language", data={"lang": "en"})
        r = self.client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your story", r.text)


if __name__ == "__main__":
    unittest.main()
