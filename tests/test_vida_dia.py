"""vida-v1 day section (what Personal's morning brief reads)."""
import unittest
from pathlib import Path
import tempfile

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from datetime import date

from tests._db import open_conn

from body import db, vida_dia, vida_export

DAY = date(2026, 11, 9).isoformat()

_ALLOWED_METRIC_FIELDS = {"person", "day", "key", "value", "source", "visibility", "kind"}
_ALLOWED_EVENT_FIELDS = {"id", "person", "hogar_id", "kind", "starts_at", "ends_at", "all_day",
                          "timezone", "source", "ref", "data"}
_ALLOWED_WRAPPER_FIELDS = {"person", "day", "generated_at", "metrics", "events", "missing"}


class DaySectionShapeTest(unittest.TestCase):
    def test_full_day_writes_four_metrics_and_the_sleep_event(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "d1.db")
        db.init(conn)
        db.set_metric(conn, DAY, "sleep_minutes", 410.0, "garmin", 1)
        db.set_metric(conn, DAY, "sleep_score", 82.0, "garmin", 1)
        db.set_metric(conn, DAY, "readiness", 70.0, "body-v1", 1)
        db.set_metric(conn, DAY, "body_battery_wake", 88.0, "garmin", 1)
        conn.execute(
            "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
            (1, DAY, f"{DAY}T22:30", "2026-11-09T06:15"),
        )
        conn.commit()

        s = vida_dia.day_section(conn, DAY, 1)
        self.assertEqual(s["day"], DAY)
        self.assertEqual(s["person"], vida_export.PERSON_KEY)
        self.assertEqual(s["missing"], [])
        self.assertEqual(set(s.keys()), _ALLOWED_WRAPPER_FIELDS)

        by_key = {m["key"]: m for m in s["metrics"]}
        self.assertEqual(set(by_key), {"body.sleep_minutes", "body.sleep_score", "body.readiness",
                                        "body.body_battery_wake"})
        for m in s["metrics"]:
            self.assertEqual(set(m.keys()), _ALLOWED_METRIC_FIELDS)
            self.assertEqual(m["person"], vida_export.PERSON_KEY)
            self.assertEqual(m["day"], DAY)
            self.assertEqual(m["visibility"], "owner")
        self.assertEqual(by_key["body.sleep_minutes"]["value"], 410.0)
        self.assertEqual(by_key["body.sleep_minutes"]["source"], "garmin")
        self.assertEqual(by_key["body.sleep_minutes"]["kind"], "measured")
        self.assertEqual(by_key["body.readiness"]["source"], "body")
        self.assertEqual(by_key["body.readiness"]["kind"], "estimated")

        self.assertEqual(len(s["events"]), 1)
        event = s["events"][0]
        self.assertEqual(set(event.keys()), _ALLOWED_EVENT_FIELDS)
        self.assertEqual(event["kind"], "body.sleep")
        self.assertEqual(event["starts_at"], f"{DAY}T22:30")
        self.assertEqual(event["ends_at"], "2026-11-09T06:15")
        self.assertEqual(event["all_day"], False)
        self.assertEqual(event["source"], "garmin")
        self.assertEqual(event["timezone"], "America/Bogota")
        self.assertEqual(event["person"], vida_export.PERSON_KEY)
        self.assertIsInstance(event["id"], str)
        self.assertTrue(event["id"])

    def test_same_person_and_day_always_gets_the_same_event_id(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "d2.db")
        db.init(conn)
        conn.execute(
            "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
            (1, DAY, f"{DAY}T23:00", "2026-11-09T05:00"),
        )
        conn.commit()
        first = vida_dia.day_section(conn, DAY, 1)["events"][0]["id"]
        second = vida_dia.day_section(conn, DAY, 1)["events"][0]["id"]
        self.assertEqual(first, second)

    def test_unsynced_watch_reports_missing_never_zero(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "d3.db")
        db.init(conn)
        # Nothing stored for DAY at all: the watch has not synced yet.
        s = vida_dia.day_section(conn, DAY, 1)
        self.assertEqual(s["metrics"], [])
        self.assertEqual(sorted(s["missing"]), sorted(
            f"body.{k}" for k in vida_dia.DAY_METRICS
        ))
        self.assertEqual(s["events"], [])

    def test_partial_day_mixes_metrics_and_missing(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "d4.db")
        db.init(conn)
        db.set_metric(conn, DAY, "sleep_minutes", 400.0, "garmin", 1)
        conn.commit()
        s = vida_dia.day_section(conn, DAY, 1)
        keys = {m["key"] for m in s["metrics"]}
        self.assertEqual(keys, {"body.sleep_minutes"})
        self.assertIn("body.sleep_score", s["missing"])
        self.assertIn("body.readiness", s["missing"])
        self.assertIn("body.body_battery_wake", s["missing"])
        self.assertNotIn("body.sleep_minutes", s["missing"])

    def test_no_sleep_session_row_means_no_event(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "d5.db")
        db.init(conn)
        db.set_metric(conn, DAY, "sleep_minutes", 400.0, "garmin", 1)
        conn.commit()
        s = vida_dia.day_section(conn, DAY, 1)
        self.assertEqual(s["events"], [])


class DayMetricsConstantTest(unittest.TestCase):
    def test_module_constant_is_a_list_of_four_keys(self):
        self.assertEqual(vida_dia.DAY_METRICS,
                          ["sleep_minutes", "sleep_score", "readiness", "body_battery_wake"])


class MainHostedTest(unittest.TestCase):
    def test_hosted_refuses_and_writes_nothing(self):
        import io
        import contextlib
        from unittest.mock import patch

        with patch.object(vida_dia.cuenta, "hosted", return_value=True), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = vida_dia.main([])
        self.assertEqual(code, 1)
        self.assertIn("hosted", out.getvalue())


class MainWritesFileTest(unittest.TestCase):
    def test_printed_line_is_the_path_only(self):
        import io
        import contextlib

        conn = db.connect()
        try:
            db.init(conn)
            db.set_metric(conn, DAY, "sleep_minutes", 400.0, "garmin", 1)
            conn.commit()
        finally:
            conn.close()

        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = vida_dia.main(["--day", DAY])
        self.assertEqual(code, 0)
        printed = out.getvalue().strip()
        self.assertTrue(printed.startswith("Wrote "))
        self.assertIn(f"body-day-{DAY}.json", printed)
        self.assertNotIn("400", printed)

        from body import config
        path = config.BODY_HOME / "vida" / f"body-day-{DAY}.json"
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
