"""BODY-ENTRENO-SUENO-STORE-1: the four night-breathing DayData fields BODY-ENTRENO-SUENO-PARSE-1
parses (sleep_spo2_avg, sleep_respiration_avg, sleep_respiration_low, sleep_respiration_high)
reach daily_metric through save_day, absent staying absent (no row, never 0)."""
import unittest
from dataclasses import dataclass, field

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db  # noqa: E402

KEYS = ("sleep_spo2_avg", "sleep_respiration_avg", "sleep_respiration_low", "sleep_respiration_high")


@dataclass
class _DayData:
    day: str
    source: str = "garmin"
    raw: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    sleep_start: str | None = None
    sleep_end: str | None = None
    activities: list = field(default_factory=list)
    sleep_spo2_avg: float | None = None
    sleep_respiration_avg: float | None = None
    sleep_respiration_low: float | None = None
    sleep_respiration_high: float | None = None


class NightBreathStoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)

    def _rows(self, day: str) -> dict:
        rows = self.conn.execute(
            "SELECT metric_key, value FROM daily_metric WHERE person_id = ? AND day = ? AND metric_key IN "
            "(?, ?, ?, ?)", (self.person_id, day, *KEYS),
        ).fetchall()
        return {r["metric_key"]: r["value"] for r in rows}

    def test_metric_def_rows_exist(self):
        defs = {r["key"]: (r["unit"], r["better"], r["kind"], r["origen"])
                for r in self.conn.execute("SELECT key, unit, better, kind, origen FROM metric_def")}
        self.assertEqual(defs["sleep_spo2_avg"], ("%", "higher", "measured", "garmin"))
        self.assertEqual(defs["sleep_respiration_avg"], ("brpm", "neutral", "measured", "garmin"))
        self.assertEqual(defs["sleep_respiration_low"], ("brpm", "neutral", "measured", "garmin"))
        self.assertEqual(defs["sleep_respiration_high"], ("brpm", "neutral", "measured", "garmin"))

    def test_full_night_stores_four_rows(self):
        day_data = _DayData(day="2026-11-01", sleep_spo2_avg=96.0, sleep_respiration_avg=14.5,
                             sleep_respiration_low=11.0, sleep_respiration_high=19.0)
        db.save_day(self.conn, day_data, self.person_id)
        rows = self._rows("2026-11-01")
        self.assertEqual(rows, {"sleep_spo2_avg": 96.0, "sleep_respiration_avg": 14.5,
                                 "sleep_respiration_low": 11.0, "sleep_respiration_high": 19.0})

    def test_absent_field_stores_no_row(self):
        day_data = _DayData(day="2026-11-02", sleep_spo2_avg=95.0, sleep_respiration_avg=None,
                             sleep_respiration_low=None, sleep_respiration_high=None)
        db.save_day(self.conn, day_data, self.person_id)
        rows = self._rows("2026-11-02")
        self.assertEqual(rows, {"sleep_spo2_avg": 95.0})

    def test_all_absent_stores_nothing(self):
        day_data = _DayData(day="2026-11-03")
        db.save_day(self.conn, day_data, self.person_id)
        self.assertEqual(self._rows("2026-11-03"), {})

    def test_resave_is_idempotent(self):
        day_data = _DayData(day="2026-11-04", sleep_spo2_avg=97.0, sleep_respiration_avg=13.0,
                             sleep_respiration_low=10.0, sleep_respiration_high=17.0)
        db.save_day(self.conn, day_data, self.person_id)
        db.save_day(self.conn, day_data, self.person_id)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM daily_metric WHERE person_id = ? AND day = ? AND metric_key IN (?, ?, ?, ?)",
            (self.person_id, "2026-11-04", *KEYS),
        ).fetchone()[0]
        self.assertEqual(count, 4)
        self.assertEqual(self._rows("2026-11-04"), {"sleep_spo2_avg": 97.0, "sleep_respiration_avg": 13.0,
                                                      "sleep_respiration_low": 10.0, "sleep_respiration_high": 17.0})

    def test_zero_is_stored_as_a_real_value_not_dropped(self):
        # 0 is physiologically impossible and garmin_adapter's own range check keeps it out before
        # it ever reaches DayData -- save_day itself only treats None as absent, never 0.
        day_data = _DayData(day="2026-11-05", sleep_respiration_low=0.0)
        db.save_day(self.conn, day_data, self.person_id)
        self.assertEqual(self._rows("2026-11-05"), {"sleep_respiration_low": 0.0})
