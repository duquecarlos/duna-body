"""BODY-HABITO-VECES-1: a habit's cadence `times` governs what is due -- the single rule in
`goals.habit_progress` / `goals.habit_due_today`, used by the weekly check and every screen."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, goals

_MONDAY = date(2026, 11, 2)  # a Monday; the week is 2026-11-02 .. 2026-11-08
_WEEK = [f"2026-11-{2 + i:02d}" for i in range(7)]


def _conn(case: unittest.TestCase):
    return open_conn(case, Path(tempfile.mkdtemp()) / "hv.db")


def _goal(conn, kind="milestone", **kw):
    gid = goals.propose(conn, 1, "Habito veces", "2026-11-02", kind=kind, deadline="2026-12-14", **kw)
    goals.activate(conn, gid)
    return gid


class PerDayTest(unittest.TestCase):
    def test_due_is_every_day_regardless_of_times(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        hid = goals.add_habit(conn, gid, "Estirar", {"per": "day", "times": 1})
        habit = conn.execute("SELECT * FROM habit WHERE id = ?", (hid,)).fetchone()
        for day in _WEEK[:3]:
            goals.check_habit(conn, hid, 1, day, True)
        done, due, period = goals.habit_progress(conn, habit, 1, _MONDAY)
        self.assertEqual((done, due, period), (3, 7, "day"))


class PerWeekTimesTest(unittest.TestCase):
    def _habit(self, conn, gid, times):
        hid = goals.add_habit(conn, gid, "Entrenar", {"per": "week", "times": times})
        return conn.execute("SELECT * FROM habit WHERE id = ?", (hid,)).fetchone()

    def test_times_1_due_is_one(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        habit = self._habit(conn, gid, 1)
        done, due, period = goals.habit_progress(conn, habit, 1, _MONDAY)
        self.assertEqual((done, due, period), (0, 1, "week"))

    def test_times_2_due_is_two(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        habit = self._habit(conn, gid, 2)
        goals.check_habit(conn, habit["id"], 1, _WEEK[0], True)
        done, due, period = goals.habit_progress(conn, habit, 1, _MONDAY)
        self.assertEqual((done, due, period), (1, 2, "week"))

    def test_times_7_due_is_seven(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        habit = self._habit(conn, gid, 7)
        for day in _WEEK:
            goals.check_habit(conn, habit["id"], 1, day, True)
        done, due, period = goals.habit_progress(conn, habit, 1, _MONDAY)
        self.assertEqual((done, due, period), (7, 7, "week"))

    def test_done_capped_at_due(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        habit = self._habit(conn, gid, 2)
        for day in _WEEK[:5]:  # 5 checks, but due is only 2
            goals.check_habit(conn, habit["id"], 1, day, True)
        done, due, period = goals.habit_progress(conn, habit, 1, _MONDAY)
        self.assertEqual((done, due, period), (2, 2, "week"))


class ContextFewerDaysThanTimesTest(unittest.TestCase):
    def test_context_days_cap_due_below_times(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        hid = goals.add_habit(conn, gid, "Dormir antes", {"per": "week", "times": 3, "context": "before_night"})
        habit = conn.execute("SELECT * FROM habit WHERE id = ?", (hid,)).fetchone()
        # a single night shift this week, on Thursday 2026-11-05 -> before_night day is 2026-11-04
        conn.execute("INSERT INTO shift (person_id, day, kind, start_local) VALUES (1, '2026-11-05', 'night', "
                     "'2026-11-05T20:00:00')")
        conn.commit()
        goals.check_habit(conn, hid, 1, "2026-11-04", True)
        done, due, period = goals.habit_progress(conn, habit, 1, _MONDAY)
        self.assertEqual((done, due, period), (1, 1, "week"))  # due = min(3, 1), not 3


class PerMonthTest(unittest.TestCase):
    def test_month_progress_counts_this_calendar_month(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        hid = goals.add_habit(conn, gid, "Pesarse", {"per": "month", "times": 2})
        habit = conn.execute("SELECT * FROM habit WHERE id = ?", (hid,)).fetchone()
        goals.check_habit(conn, hid, 1, "2026-11-03", True)
        goals.check_habit(conn, hid, 1, "2026-11-10", True)
        today = date(2026, 11, 15)
        done, due, period = goals.habit_progress(conn, habit, 1, _MONDAY, today)
        self.assertEqual((done, due, period), (2, 2, "month"))

    def test_month_progress_capped_at_times(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        hid = goals.add_habit(conn, gid, "Pesarse", {"per": "month", "times": 2})
        habit = conn.execute("SELECT * FROM habit WHERE id = ?", (hid,)).fetchone()
        for day in ("2026-11-03", "2026-11-10", "2026-11-12"):  # 3 checks, times is 2
            goals.check_habit(conn, hid, 1, day, True)
        today = date(2026, 11, 15)
        done, due, period = goals.habit_progress(conn, habit, 1, _MONDAY, today)
        self.assertEqual((done, due, period), (2, 2, "month"))

    def test_month_habit_contributes_nothing_to_the_weekly_tally(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        hid = goals.add_habit(conn, gid, "Pesarse", {"per": "month", "times": 2})
        goals.check_habit(conn, hid, 1, "2026-11-03", True)
        r = goals.weekly_check(conn, gid, 1, _MONDAY.isoformat())
        self.assertEqual(r["habits_due"], 0)
        self.assertIsNone(r["on_track"])  # 0 due habits -> no verdict, not a false on-track


class DueTodayTest(unittest.TestCase):
    def test_due_today_false_once_a_twice_a_week_habit_has_two_checks(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        hid = goals.add_habit(conn, gid, "Entrenar", {"per": "week", "times": 2})
        habit = conn.execute("SELECT * FROM habit WHERE id = ?", (hid,)).fetchone()
        self.assertTrue(goals.habit_due_today(conn, habit, 1, date.fromisoformat(_WEEK[0])))
        goals.check_habit(conn, hid, 1, _WEEK[0], True)
        goals.check_habit(conn, hid, 1, _WEEK[1], True)
        self.assertFalse(goals.habit_due_today(conn, habit, 1, date.fromisoformat(_WEEK[2])))

    def test_day_cadence_stays_due_today_even_once_checked(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        hid = goals.add_habit(conn, gid, "Estirar", {"per": "day", "times": 1})
        habit = conn.execute("SELECT * FROM habit WHERE id = ?", (hid,)).fetchone()
        goals.check_habit(conn, hid, 1, _WEEK[0], True)
        self.assertTrue(goals.habit_due_today(conn, habit, 1, date.fromisoformat(_WEEK[0])))


class MilestoneOnTrackWithTimesTest(unittest.TestCase):
    def test_milestone_with_only_a_twice_a_week_habit_done_twice_is_on_track(self):
        conn = _conn(self)
        db.init(conn)
        gid = _goal(conn)
        hid = goals.add_habit(conn, gid, "Entrenar", {"per": "week", "times": 2})
        goals.check_habit(conn, hid, 1, _WEEK[0], True)
        goals.check_habit(conn, hid, 1, _WEEK[1], True)
        r = goals.weekly_check(conn, gid, 1, _MONDAY.isoformat())
        self.assertEqual(r["habits_done"], 2)
        self.assertEqual(r["habits_due"], 2)
        self.assertTrue(r["on_track"])


if __name__ == "__main__":
    unittest.main()
