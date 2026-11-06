"""vida-v1 weekly section (what Personal's Sunday review reads)."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from datetime import date, timedelta
from pathlib import Path
import tempfile

from body import db, vida_export


class VidaExportTest(unittest.TestCase):
    def test_week_section_shape(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "v.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        for i in range(5):
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "resting_hr", 55 + i, "garmin")
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "readiness", 70, "body-v1")
        conn.commit()
        s = vida_export.week_section(conn, monday)
        self.assertEqual(s["area"], "body")
        self.assertEqual(s["week_start"], "2026-11-02")
        by_key = {m["key"]: m for m in s["metrics"]}
        self.assertEqual(by_key["body.resting_hr"]["value"], 57.0)
        self.assertEqual(by_key["body.readiness"]["kind"], "estimated")
        self.assertTrue(all(m["visibility"] == "owner" for m in s["metrics"]))
        self.assertIn("5 of 7", s["summary"])


if __name__ == "__main__":
    unittest.main()
