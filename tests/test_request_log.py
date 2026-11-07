"""BODY-REQUEST-LOG-SCRUB-1: the `body.request` logger carries method, the matched route's path
TEMPLATE and status only -- never a concrete path segment, a query string, a form value or
exception text that could hold a health figure. Invented demo values only, no real health data.
"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, web

client = TestClient(web.app, follow_redirects=False)

LINE_RE = re.compile(r"^(GET|POST) \S+ \d{3} \d+ms$")
SECRET_DAY = "2026-01-02"
SECRET_FORM_VALUE = "73.123"
SECRET_QUERY_VALUE = "88.456"


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "reqlog.db"
    conn = db.connect()
    db.init(conn)
    return conn


def _message(record_line: str) -> str:
    """assertLogs gives "LEVEL:logger.name:message" -- strip the first two fields."""
    return record_line.split(":", 2)[2]


class GetRequestLogTest(unittest.TestCase):
    def test_day_route_logs_template_not_the_date_or_query_string(self):
        _reset_db(self)
        with self.assertLogs("body.request", level="INFO") as cm:
            r = client.get(f"/day/{SECRET_DAY}?x={SECRET_QUERY_VALUE}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(cm.output), 1)
        msg = _message(cm.output[0])
        self.assertRegex(msg, LINE_RE)
        self.assertIn("/day/{day}", msg)
        self.assertNotIn(SECRET_DAY, msg)
        self.assertNotIn(SECRET_QUERY_VALUE, msg)

    def test_goal_detail_route_logs_template_not_the_id_or_query_string(self):
        conn = _reset_db(self)
        from body import goals
        gid = goals.propose(conn, 1, "Lower resting HR", SECRET_DAY, metric_key="resting_hr",
                            direction="lower", target_value=50.0, baseline_value=60.0,
                            deadline="2027-01-01")
        with self.assertLogs("body.request", level="INFO") as cm:
            r = client.get(f"/goals/{gid}?x={SECRET_QUERY_VALUE}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(cm.output), 1)
        msg = _message(cm.output[0])
        self.assertRegex(msg, LINE_RE)
        self.assertIn("/goals/{goal_id}", msg)
        self.assertNotIn(str(gid), msg)
        self.assertNotIn(SECRET_QUERY_VALUE, msg)


class PostRequestLogTest(unittest.TestCase):
    def test_form_post_logs_template_not_the_form_value(self):
        _reset_db(self)
        with self.assertLogs("body.request", level="INFO") as cm:
            r = client.post("/goals/new", data={
                "kind": "metric", "title": "Lower resting HR", "metric_key": "resting_hr",
                "direction": "lower", "target_value": SECRET_FORM_VALUE, "baseline_value": "60",
                "deadline": "2027-01-01",
            })
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(cm.output), 1)
        msg = _message(cm.output[0])
        self.assertRegex(msg, LINE_RE)
        self.assertIn("/goals/new", msg)
        self.assertNotIn(SECRET_FORM_VALUE, msg)


class UnmatchedRouteLogsDashTest(unittest.TestCase):
    def test_unmatched_path_logs_dash_as_template(self):
        _reset_db(self)
        with self.assertLogs("body.request", level="INFO") as cm:
            r = client.get("/this-route-does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(len(cm.output), 1)
        msg = _message(cm.output[0])
        self.assertTrue(msg.startswith("GET - 404"))


class UnhandledExceptionLogTest(unittest.TestCase):
    def test_raising_route_logs_class_name_not_the_message_and_hides_it_from_the_response(self):
        _reset_db(self)
        with mock.patch.object(web, "_delta", side_effect=ValueError(SECRET_FORM_VALUE)):
            with self.assertLogs("body.request", level="INFO") as cm:
                r = client.get(f"/day/{SECRET_DAY}")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn(SECRET_FORM_VALUE, r.text)
        self.assertEqual(len(cm.output), 1)
        msg = _message(cm.output[0])
        self.assertIn("/day/{day}", msg)
        self.assertIn("500", msg)
        self.assertIn("ValueError", msg)
        self.assertNotIn(SECRET_FORM_VALUE, msg)


if __name__ == "__main__":
    unittest.main()
