"""BODY-PERSON-SCOPE-1: every request resolves WHO it serves through body/identity.py, every web
read and write filters by that resolved person, and a row that belongs to another person answers
404 -- never 403, never the row. Two persons (A, B), invented demo data, no real health figures.
"""
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import baselines, config, cuenta, db, goals, history, profile, readiness, shift_import, vida_export, web
from body import identity
from body.agente import ajustes

PERSON_A = 1   # db.local_person_id's answer in local mode
PERSON_B = 2

BODY_DIR = Path(__file__).resolve().parent.parent / "body"

client = TestClient(web.app, follow_redirects=False)


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _reset_db(tc: unittest.TestCase):
    """A fresh db for this test, nested under TMP, with person B added alongside the local person
    A. dependency_overrides is cleared on teardown so later test modules see the real resolver."""
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "scope.db"
    conn = db.connect()
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
    conn.commit()
    return conn


def _seed_person(conn, person_id: int, day: str, title: str, batch_name: str, received: str):
    """One day of data, one active goal with a daily habit (always due) and a check, one proposed
    shift batch -- enough for "/", "/goals", "/goals/<id>" and "/shifts" to render real content."""
    db.set_metric(conn, day, "resting_hr", 50.0 + person_id, "garmin", person_id)
    gid = goals.propose(conn, person_id, title, day, metric_key="resting_hr", direction="lower",
                        target_value=45.0, baseline_value=55.0, deadline="2027-01-01")
    goals.activate(conn, gid)
    hid = goals.add_habit(conn, gid, f"{title} habit", {"per": "day", "times": 1})
    goals.check_habit(conn, hid, person_id, day, True)
    conn.execute(
        "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, day_count, created_at) "
        "VALUES (?, ?, ?, ?, 'proposed', 1, ?)",
        (person_id, batch_name, received, received, received),
    )
    batch_id = conn.execute("SELECT id FROM shift_batch WHERE person_id = ? AND file_name = ?",
                            (person_id, batch_name)).fetchone()["id"]
    conn.execute(
        "INSERT INTO shift_batch_item (batch_id, day, kind, start_local, end_local) "
        "VALUES (?, ?, 'night', ?, ?)", (batch_id, day, f"{day}T22:00:00", f"{day}T22:00:00"),
    )
    conn.commit()
    return gid, hid, batch_id


class WebScopeTest(unittest.TestCase):
    """Every GET route renders only the resolved person's data; every id-taking route 404s on the
    other person's id (GET and POST), and that other person's rows are left untouched."""

    def setUp(self):
        conn = _reset_db(self)
        self.gid_a, self.hid_a, self.batch_a = _seed_person(
            conn, PERSON_A, "2026-11-01", "Goal for person A", "turnos-a.json", "2026-11-01T12:00:00-05:00")
        self.gid_b, self.hid_b, self.batch_b = _seed_person(
            conn, PERSON_B, "2026-11-10", "Goal for person B", "turnos-b.json", "2026-11-10T12:00:00-05:00")
        self.conn = conn

    def _goal_status(self, goal_id: int) -> str:
        return self.conn.execute("SELECT status FROM goal WHERE id = ?", (goal_id,)).fetchone()["status"]

    def _batch_status(self, batch_id: int) -> str:
        return self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (batch_id,)).fetchone()["status"]

    def _habit_check_count(self, habit_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM habit_check WHERE habit_id = ?", (habit_id,)).fetchone()[0]

    # ---- GET routes: only the resolved person's data ----

    def test_today_shows_only_the_resolved_persons_due_habit(self):
        _as(PERSON_A)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Goal for person A habit", r.text)
        self.assertNotIn("Goal for person B", r.text)

        _as(PERSON_B)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Goal for person B habit", r.text)
        self.assertNotIn("Goal for person A", r.text)

    def test_goals_list_shows_only_the_resolved_persons_goals(self):
        _as(PERSON_A)
        r = client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Goal for person A", r.text)
        self.assertNotIn("Goal for person B", r.text)

        _as(PERSON_B)
        r = client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Goal for person B", r.text)
        self.assertNotIn("Goal for person A", r.text)

    def test_shifts_page_shows_only_the_resolved_persons_batch_date(self):
        from body.i18n import T
        a_date = web._batch_date(T("es"), "2026-11-01T12:00:00-05:00")
        b_date = web._batch_date(T("es"), "2026-11-10T12:00:00-05:00")

        _as(PERSON_A)
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn(a_date, r.text)
        self.assertNotIn(b_date, r.text)

        _as(PERSON_B)
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b_date, r.text)
        self.assertNotIn(a_date, r.text)

    # ---- id-taking GET: 404 on the other person's id ----

    def test_goal_detail_404s_on_the_other_persons_goal(self):
        _as(PERSON_A)
        self.assertEqual(client.get(f"/goals/{self.gid_a}").status_code, 200)
        self.assertEqual(client.get(f"/goals/{self.gid_b}").status_code, 404)

        _as(PERSON_B)
        self.assertEqual(client.get(f"/goals/{self.gid_b}").status_code, 200)
        self.assertEqual(client.get(f"/goals/{self.gid_a}").status_code, 404)

    # ---- id-taking POST: 404 on the other person's id, and that row is left untouched ----

    def test_goal_lifecycle_posts_404_on_the_other_persons_goal(self):
        _as(PERSON_A)
        r = client.post(f"/goals/{self.gid_b}/habits", data={"title": "Sneaky", "per": "week", "times": "1"})
        self.assertEqual(r.status_code, 404)
        for action in ("activate", "pause", "finish", "drop"):
            r = client.post(f"/goals/{self.gid_b}/{action}")
            self.assertEqual(r.status_code, 404)
        self.assertEqual(self._goal_status(self.gid_b), "active")  # B's goal: untouched
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM habit WHERE goal_id = ?",
                                           (self.gid_b,)).fetchone()[0], 1)  # no "Sneaky" habit added

    def test_habit_check_post_404s_on_the_other_persons_habit(self):
        _as(PERSON_A)
        before = self._habit_check_count(self.hid_b)
        r = client.post(f"/habits/{self.hid_b}/check", data={"day": "2026-11-10", "done": "1", "back": "/"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._habit_check_count(self.hid_b), before)  # B's habit_check rows: untouched

    def test_shifts_confirm_and_dismiss_404_on_the_other_persons_batch(self):
        _as(PERSON_A)
        r = client.post("/shifts/confirm", data={"batch_id": str(self.batch_b)})
        self.assertEqual(r.status_code, 404)
        r = client.post("/shifts/dismiss", data={"batch_id": str(self.batch_b)})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._batch_status(self.batch_b), "proposed")  # B's batch: untouched

        _as(PERSON_B)
        r = client.post("/shifts/confirm", data={"batch_id": str(self.batch_a)})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._batch_status(self.batch_a), "proposed")  # A's batch: untouched


class HostedPersonScopeTest(unittest.TestCase):
    """BODY-CUENTA-RP-1: with real Cuenta sessions (not dependency_overrides), two persons signed
    in with two sessions see only their own rows and get 404 on the other's ids."""

    def setUp(self):
        conn = _reset_db(self)
        self.gid_a, self.hid_a, self.batch_a = _seed_person(
            conn, PERSON_A, "2026-11-01", "Goal for person A", "turnos-a.json", "2026-11-01T12:00:00-05:00")
        self.gid_b, self.hid_b, self.batch_b = _seed_person(
            conn, PERSON_B, "2026-11-10", "Goal for person B", "turnos-b.json", "2026-11-10T12:00:00-05:00")

        env = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.token_a = cuenta.create_session(conn, PERSON_A, "sid-scope-a")
        self.token_b = cuenta.create_session(conn, PERSON_B, "sid-scope-b")

    def _client_as(self, token: str) -> TestClient:
        c = TestClient(web.app, follow_redirects=False)
        c.cookies.set(cuenta.SESSION_COOKIE, token)
        return c

    def test_two_sessions_see_only_their_own_goals_and_404_on_the_others_id(self):
        ca, cb = self._client_as(self.token_a), self._client_as(self.token_b)

        ra = ca.get("/goals")
        self.assertEqual(ra.status_code, 200)
        self.assertIn("Goal for person A", ra.text)
        self.assertNotIn("Goal for person B", ra.text)

        rb = cb.get("/goals")
        self.assertEqual(rb.status_code, 200)
        self.assertIn("Goal for person B", rb.text)
        self.assertNotIn("Goal for person A", rb.text)

        self.assertEqual(ca.get(f"/goals/{self.gid_a}").status_code, 200)
        self.assertEqual(ca.get(f"/goals/{self.gid_b}").status_code, 404)
        self.assertEqual(cb.get(f"/goals/{self.gid_b}").status_code, 200)
        self.assertEqual(cb.get(f"/goals/{self.gid_a}").status_code, 404)

    def test_no_session_cookie_redirects_to_login(self):
        c = TestClient(web.app, follow_redirects=False)
        r = c.get("/goals")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/login"))


# ---- static guards: the resolver is the only seam, and nothing defaults person_id away ----

class NoDefaultPersonIdOutsideDbTest(unittest.TestCase):
    def test_default_person_id_appears_only_in_db_py(self):
        offenders = []
        for p in BODY_DIR.rglob("*.py"):
            if p.name == "db.py":
                continue
            text = p.read_text(encoding="utf-8")
            if "DEFAULT_PERSON_ID" in text:
                offenders.append(str(p.relative_to(BODY_DIR)))
        self.assertEqual(offenders, [])


class PersonIdHasNoDefaultTest(unittest.TestCase):
    def test_listed_data_functions_require_person_id(self):
        targets = [
            (db, "save_day"), (db, "load_day"), (db, "set_metric"), (db, "series"), (db, "person"),
            (db, "set_language"), (db, "day_values"), (db, "last_sync"), (db, "days_with_data"),
            (baselines, "baseline"), (profile, "metric_profile"), (profile, "window"),
            (readiness, "compute"), (readiness, "update"), (readiness, "recompute_all"),
            (history, "coverage"), (history, "training"), (history, "patterns"), (history, "by_shift"),
            (shift_import, "propose"), (shift_import, "confirm_all"), (shift_import, "summary_proposed"),
            (shift_import, "latest_proposed"), (shift_import, "latest_confirmed"),
            (vida_export, "week_section"), (ajustes, "nivel_datos"),
        ]
        for module, name in targets:
            func = getattr(module, name)
            sig = inspect.signature(func)
            param = sig.parameters.get("person_id")
            self.assertIsNotNone(param, f"{module.__name__}.{name} has no person_id parameter")
            self.assertIs(param.default, inspect.Parameter.empty,
                          f"{module.__name__}.{name}'s person_id still has a default: {param.default!r}")

    def test_household_of_requires_person_id(self):
        from body import household
        sig = inspect.signature(household.household_of)
        param = sig.parameters["person_id"]
        self.assertIs(param.default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
