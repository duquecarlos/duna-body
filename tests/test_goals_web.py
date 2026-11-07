"""The Metas screens: /goals, /goals/<id>, /goals/new, habit check-off, and the Hoy plan card."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, household, web

_STATIC_DIR = Path(__file__).resolve().parent.parent / "body" / "static"

client = TestClient(web.app, follow_redirects=False)


def _conn():
    return db.connect()


def _reset_db(tc: unittest.TestCase):
    """A fresh db for this test, nested under TMP (other tests guard that config.DB_PATH stays there),
    restored to its prior value on teardown so later test modules see it unchanged."""
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "g.db"
    conn = _conn()
    db.init(conn)
    return conn


class EmptyStateTest(unittest.TestCase):
    def test_empty_spanish_and_english(self):
        conn = _reset_db(self)
        r = client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Empieza con la primera.", r.text)

        db.set_language(conn, "en")
        r = client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Start with your first one.", r.text)


class ListSectionsTest(unittest.TestCase):
    def test_active_proposed_archived(self):
        conn = _reset_db(self)
        today = "2026-11-02"
        gid_active = goals.propose(conn, 1, "Lower resting HR", today, metric_key="resting_hr",
                                   direction="lower", target_value=50.0, baseline_value=60.0,
                                   deadline="2026-12-14")
        goals.activate(conn, gid_active)
        goals.propose(conn, 1, "Walk more", today, metric_key="steps", direction="raise",
                      target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        gid_archived = goals.propose(conn, 1, "Old goal", today, metric_key="steps", direction="raise",
                                     target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid_archived)
        goals.pause(conn, gid_archived)

        r = client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Lower resting HR", r.text)
        self.assertIn("Walk more", r.text)
        self.assertIn("Old goal", r.text)
        self.assertIn("Propuestas", r.text)
        self.assertIn("Archivo", r.text)


class CreateGoalTest(unittest.TestCase):
    def test_create_metric_proposed(self):
        _reset_db(self)
        resp = client.post("/goals/new", data={
            "kind": "metric", "title": "Bajar el pulso", "area": "body",
            "metric_key": "resting_hr", "direction": "lower", "target_value": "52",
            "baseline_value": "60", "deadline": "2027-03-31",
        })
        self.assertEqual(resp.status_code, 303)
        location = resp.headers["location"]
        self.assertRegex(location, r"^/goals/\d+$")
        detail = client.get(location)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Bajar el pulso", detail.text)
        self.assertIn("Activar", detail.text)

    def test_create_milestone_proposed(self):
        _reset_db(self)
        resp = client.post("/goals/new", data={
            "kind": "milestone", "title": "Subir el Cocuy", "area": "adventure",
            "deadline": "2027-06-01",
        })
        self.assertEqual(resp.status_code, 303)
        location = resp.headers["location"]
        self.assertRegex(location, r"^/goals/\d+$")
        detail = client.get(location)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Subir el Cocuy", detail.text)
        self.assertIn("Activar", detail.text)


class ActivateAndHabitTest(unittest.TestCase):
    def test_activate_add_habit_and_check(self):
        conn = _reset_db(self)
        gid = goals.propose(conn, 1, "Dormir mejor", "2026-11-02", metric_key="sleep_minutes",
                            direction="raise", target_value=420.0, baseline_value=350.0,
                            deadline="2026-12-31")
        r = client.post(f"/goals/{gid}/activate")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/goals/{gid}")

        r = client.post(f"/goals/{gid}/habits", data={"title": "Dormir temprano", "per": "week", "times": "3"})
        self.assertEqual(r.status_code, 303)
        habit_id = conn.execute("SELECT id FROM habit WHERE goal_id = ?", (gid,)).fetchone()[0]

        today_iso = date.today().isoformat()
        r = client.post(f"/habits/{habit_id}/check", data={"day": today_iso, "done": "1", "back": "/"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        checked = conn.execute("SELECT done FROM habit_check WHERE habit_id = ? AND day = ?",
                               (habit_id, today_iso)).fetchone()
        self.assertEqual(checked["done"], 1)

        detail = client.get(f"/goals/{gid}")
        self.assertIn("Dormir temprano", detail.text)


class ContextHabitOnHoyTest(unittest.TestCase):
    def test_before_night_shown_and_hidden(self):
        conn = _reset_db(self)
        db.set_metric(conn, "2026-11-01", "resting_hr", 55, "garmin")
        gid = goals.propose(conn, 1, "Dormir antes de la noche", "2026-11-02", metric_key="sleep_minutes",
                            direction="raise", target_value=420.0, baseline_value=350.0,
                            deadline="2026-12-31")
        goals.activate(conn, gid)
        goals.add_habit(conn, gid, "Siesta antes de turno", {"per": "day", "times": 1, "context": "before_night"})

        # 2026-11-13 is a night shift -> its before_night due day is 2026-11-12
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, start_local, end_local) VALUES "
            "(1, '2026-11-13', 'night', '2026-11-13T22:00:00', '2026-11-14T06:00:00')")
        conn.commit()

        with mock.patch.object(web, "_today_local", return_value=date(2026, 11, 12)):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Siesta antes de turno", r.text)

        with mock.patch.object(web, "_today_local", return_value=date(2026, 11, 9)):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Siesta antes de turno", r.text)


class GoalDetailKindTest(unittest.TestCase):
    def test_metric_and_milestone_detail(self):
        conn = _reset_db(self)
        gid_m = goals.propose(conn, 1, "Bajar pulso", "2026-11-02", metric_key="resting_hr", direction="lower",
                              target_value=52.0, baseline_value=60.0, deadline="2027-03-31")
        goals.activate(conn, gid_m)
        r = client.get(f"/goals/{gid_m}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Bajar pulso", r.text)

        gid_h = goals.propose(conn, 1, "Subir el Cocuy", "2026-11-02", kind="milestone", deadline="2027-06-01")
        goals.activate(conn, gid_h)
        r = client.get(f"/goals/{gid_h}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Subir el Cocuy", r.text)
        self.assertIn("Fecha: 1 jun 2027", r.text)


class NewGoalFormKindTest(unittest.TestCase):
    def test_metric_only_group_and_both_kind_radios(self):
        _reset_db(self)
        r = client.get("/goals/new")
        self.assertEqual(r.status_code, 200)
        self.assertIn("metric-only", r.text)
        self.assertIn('name="kind" value="metric"', r.text)
        self.assertIn('name="kind" value="milestone"', r.text)


class FormTapTargetCssTest(unittest.TestCase):
    def test_buttons_and_inputs_are_44px(self):
        css = (_STATIC_DIR / "body.css").read_text(encoding="utf-8")
        self.assertIn(".btn{display:inline-flex;align-items:center;justify-content:center;"
                      "white-space:nowrap;min-width:44px;min-height:44px", css)
        self.assertIn(".field{display:block;width:100%;min-height:44px", css)


class HouseholdGoalDetailTest(unittest.TestCase):
    def test_member_check_shown(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executescript(household.SCHEMA)
        conn.executemany("INSERT INTO hogar_local (person_id, hogar_id) VALUES (?, ?)", [(1, "h-1"), (2, "h-1")])
        conn.commit()
        gid = goals.propose(conn, 1, "Caminar juntos", "2026-11-02", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14",
                            hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        goals.weekly_check(conn, gid, 2, "2026-11-02")

        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Partner", r.text)


if __name__ == "__main__":
    unittest.main()
