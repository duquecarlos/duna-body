"""BODY-FECHA-FUTURA-1: a habit check is an ACTUAL record -- something that already happened -- so
it refuses a `day` beyond the checking person's local today plus one day of tolerance (hub
hit_01m6w6cn8x, LOTES-API-FECHA-FUTURA-1). A goal deadline and a shift-pattern start day are PLANS
and keep accepting future dates.
"""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from fastapi.testclient import TestClient

from body import config, db, fechas, goals, identity, patron_turnos, web


def _conn(case: unittest.TestCase):
    return open_conn(case, Path(tempfile.mkdtemp()) / "ff.db")


class FechaRealValidaTest(unittest.TestCase):
    """The pure predicate: day <= hoy + 1."""

    def test_today_accepted(self):
        hoy = date(2026, 11, 8)
        self.assertTrue(fechas.fecha_real_valida(hoy, hoy))

    def test_tomorrow_accepted(self):
        hoy = date(2026, 11, 8)
        self.assertTrue(fechas.fecha_real_valida(hoy + timedelta(1), hoy))

    def test_day_after_tomorrow_refused(self):
        hoy = date(2026, 11, 8)
        self.assertFalse(fechas.fecha_real_valida(hoy + timedelta(2), hoy))

    def test_a_past_day_of_the_current_week_accepted(self):
        hoy = date(2026, 11, 8)  # a Sunday
        monday = hoy - timedelta(hoy.weekday())
        self.assertTrue(fechas.fecha_real_valida(monday, hoy))


class HoyLocalTimezoneTest(unittest.TestCase):
    """The person's own `timezone` decides today, not the server's -- one real instant lands on
    different local dates for two people in different zones."""

    def _patched(self, instant: datetime):
        return mock.patch("body.fechas.datetime", **{
            "now.side_effect": lambda tz=None: instant.astimezone(tz) if tz else instant,
        })

    def test_timezones_either_side_of_midnight_utc_see_different_dates(self):
        instant = datetime(2026, 11, 10, 4, 30, tzinfo=timezone.utc)
        with self._patched(instant):
            bogota = fechas.hoy_local({"timezone": "America/Bogota"})  # UTC-5: still Nov 9
            tokyo = fechas.hoy_local({"timezone": "Asia/Tokyo"})       # UTC+9: already Nov 10
        self.assertEqual(bogota, date(2026, 11, 9))
        self.assertEqual(tokyo, date(2026, 11, 10))

    def test_missing_timezone_defaults_to_bogota(self):
        instant = datetime(2026, 11, 10, 4, 30, tzinfo=timezone.utc)
        with self._patched(instant):
            self.assertEqual(fechas.hoy_local({}), date(2026, 11, 9))


class CheckHabitRefusesTheFutureTest(unittest.TestCase):
    """goals.check_habit, given the checking person's real local today via `hoy`, refuses a day
    beyond tolerance and writes nothing; a plain call with no `hoy` (every pre-existing caller --
    demo seeding, the rest of the test suite) stays the pure function it always was."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        gid = goals.propose(self.conn, 1, "Meta", "2026-11-01", kind="milestone", deadline="2027-01-01")
        goals.activate(self.conn, gid)
        self.hid = goals.add_habit(self.conn, gid, "Habito", {"per": "day", "times": 1})
        self.hoy = date(2026, 11, 8)

    def _count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM habit_check WHERE habit_id = ?", (self.hid,)
        ).fetchone()["n"]

    def test_today_accepted(self):
        goals.check_habit(self.conn, self.hid, 1, self.hoy.isoformat(), True, hoy=self.hoy)
        self.assertEqual(self._count(), 1)

    def test_tomorrow_accepted(self):
        goals.check_habit(self.conn, self.hid, 1, (self.hoy + timedelta(1)).isoformat(), True, hoy=self.hoy)
        self.assertEqual(self._count(), 1)

    def test_day_after_tomorrow_refused_and_nothing_stored(self):
        with self.assertRaises(ValueError):
            goals.check_habit(self.conn, self.hid, 1, (self.hoy + timedelta(2)).isoformat(), True, hoy=self.hoy)
        self.assertEqual(self._count(), 0)

    def test_a_past_day_of_the_current_week_accepted(self):
        monday = self.hoy - timedelta(self.hoy.weekday())
        goals.check_habit(self.conn, self.hid, 1, monday.isoformat(), True, hoy=self.hoy)
        self.assertEqual(self._count(), 1)

    def test_no_hoy_passed_keeps_the_old_behaviour(self):
        future = (self.hoy + timedelta(30)).isoformat()
        goals.check_habit(self.conn, self.hid, 1, future, True)
        self.assertEqual(self._count(), 1)


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _fresh_conn(case: unittest.TestCase, name: str = "a.db"):
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


class HabitCheckRouteTest(unittest.TestCase):
    """POST /habits/{id}/check through the real route: the hub rule as the screens meet it."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.conn = _fresh_conn(self)
        self.addCleanup(web.app.dependency_overrides.clear)
        _as(1)
        gid = goals.propose(self.conn, 1, "Meta", "2026-11-01", kind="milestone", deadline="2027-01-01")
        goals.activate(self.conn, gid)
        self.hid = goals.add_habit(self.conn, gid, "Habito", {"per": "day", "times": 1})
        self.hoy = date(2026, 11, 8)

    def _check(self, day: str):
        with mock.patch.object(web, "_today_local", return_value=self.hoy):
            return self.client.post(f"/habits/{self.hid}/check", data={"day": day, "done": "1", "back": "/"})

    def test_today_is_accepted(self):
        self.assertEqual(self._check(self.hoy.isoformat()).status_code, 303)

    def test_tomorrow_is_accepted(self):
        self.assertEqual(self._check((self.hoy + timedelta(1)).isoformat()).status_code, 303)

    def test_day_after_tomorrow_is_refused_404_and_nothing_stored(self):
        resp = self._check((self.hoy + timedelta(2)).isoformat())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM habit_check WHERE habit_id = ?", (self.hid,)
            ).fetchone()["n"],
            0,
        )

    def test_a_past_day_of_the_current_week_is_accepted(self):
        monday = self.hoy - timedelta(self.hoy.weekday())
        self.assertEqual(self._check(monday.isoformat()).status_code, 303)


class PlansStillAcceptFutureDatesTest(unittest.TestCase):
    """A goal deadline and a shift-pattern start day are PLANS, untouched by BODY-FECHA-FUTURA-1."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.conn = _fresh_conn(self)
        self.addCleanup(web.app.dependency_overrides.clear)
        _as(1)

    def test_goal_deadline_far_in_the_future_is_accepted(self):
        future_deadline = (date.today() + timedelta(days=365)).isoformat()
        gid = goals.propose(self.conn, 1, "Meta futura", date.today().isoformat(),
                             kind="milestone", deadline=future_deadline)
        row = self.conn.execute("SELECT deadline FROM goal WHERE id = ?", (gid,)).fetchone()
        self.assertEqual(row["deadline"], future_deadline)

    def test_shift_pattern_start_day_far_in_the_future_is_accepted(self):
        db.set_trabaja_turnos(self.conn, True, 1)
        self.conn.commit()
        future_start = (date.today() + timedelta(days=60)).isoformat()
        data = {"dias": "1", "start_day": future_start, "horizon_weeks": "1",
                "kind_0": "off", "start_0": "", "end_0": ""}
        resp = self.client.post("/shifts/patron", data=data)
        self.assertEqual(resp.status_code, 303)
        saved = patron_turnos.leer(self.conn, 1)
        self.assertEqual(saved["start_day"], future_start)


if __name__ == "__main__":
    unittest.main()
