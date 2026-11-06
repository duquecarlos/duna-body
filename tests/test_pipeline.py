"""End-to-end without Garmin: a fake payload goes through parse -> SQLite -> page."""
import os
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ["BODY_HOME"] = TMP  # before importing body.*, so nothing touches the real home

from fastapi.testclient import TestClient  # noqa: E402

from body import config, db, garmin_adapter, web  # noqa: E402

FAKE_RAW = {
    "user_summary": {
        "restingHeartRate": 52, "totalSteps": 10432, "averageStressLevel": 31,
        "activeKilocalories": 610, "bodyBatteryHighestValue": 88, "bodyBatteryLowestValue": 21,
        "bodyBatteryAtWakeTime": 80, "moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 15,
    },
    "sleep": {"dailySleepDTO": {
        "sleepTimeSeconds": 26100, "sleepScores": {"overall": {"value": 78}},
        "sleepStartTimestampLocal": 1793833200000, "sleepEndTimestampLocal": 1793859300000,
    }},
    "hrv": {"hrvSummary": {"lastNightAvg": 48, "weeklyAvg": 51}},
    "body_battery": [],
    "activities": [{"activityId": 1, "startTimeLocal": "2026-11-05 18:00:00", "activityType": {"typeKey": "running"},
                    "activityName": "Evening run", "duration": 1800, "distance": 5200, "averageHR": 148,
                    "maxHR": 171, "calories": 380}],
}


class PipelineTest(unittest.TestCase):
    def test_parse(self):
        d = garmin_adapter.parse_day("2026-11-05", FAKE_RAW)
        self.assertEqual(d.metrics["resting_hr"], 52)
        self.assertEqual(d.metrics["sleep_minutes"], 435)
        self.assertEqual(d.metrics["intensity_minutes"], 50)
        self.assertEqual(d.sleep_start, "2026-11-04T23:00")
        self.assertEqual(len(d.activities), 1)

    def test_no_data_values_are_dropped(self):
        d = garmin_adapter.parse_day("2026-11-05", {"user_summary": {"averageStressLevel": -1}})
        self.assertNotIn("stress_avg", d.metrics)

    def test_end_to_end_page(self):
        conn = db.connect()
        db.init(conn)
        db.save_day(conn, garmin_adapter.parse_day("2026-11-05", FAKE_RAW))
        self.assertTrue(str(config.DB_PATH).startswith(TMP))
        client = TestClient(web.app)
        r = client.get("/", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/day/2026-11-05")
        page = client.get("/day/2026-11-05").text
        for text in ("Resting heart rate", "52", "7 h 15", "10 432", "Evening run", "No shift recorded"):
            self.assertIn(text, page)


if __name__ == "__main__":
    unittest.main()
