"""The M T W T F S S week strip on a weekly habit's own progress line, goal detail
(BODY-METAS-HABITO-SEMANA-TIRA-1). Rulings:

1. Monday..Sunday day cells for every WEEKLY habit of the person's own (not another member's,
   not a day/month-cadence habit): a letter from the app's own t('date.days') initials, and a
   state -- hecho (a done=1 check that day, wins over any other condition), hoy (today, not
   done), futuro (after today), pasado (before today, not done) -- each with an accessible text
   naming the day and the state in words.
2. One SELECT over habit_check for every weekly habit of the goal, reused for `checked_today`
   too, instead of one comun._habit_checked call per habit.
3. The row sits under the habit's own progress line; no JS, the check form stays as is.
"""
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, web
from body.rutas import comun, metas
from tests._rutas import contar_sql

client = TestClient(web.app, follow_redirects=False)

# 2026-11-02 is a Monday; today patched to 2026-11-04 (Wednesday) gives one past day, today, and
# four future days to exercise every state in one week.
_MONDAY = date(2026, 11, 2)
_SUNDAY = _MONDAY + timedelta(6)
_TODAY = date(2026, 11, 4)

_CELL_RE = re.compile(r'habit-week-cell (\w+)" aria-label="([^"]*)">([^<]+)</span>')


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "g.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _make_goal_with_weekly_habit(conn, times: int = 3) -> tuple[int, int]:
    gid = goals.propose(conn, 1, "Dormir mejor", "2026-11-02", metric_key="sleep_minutes",
                        direction="raise", target_value=420.0, baseline_value=350.0, deadline="2026-12-31")
    goals.activate(conn, gid)
    habit_id = goals.add_habit(conn, gid, "Dormir temprano", {"per": "week", "times": times})
    return gid, habit_id


def _cells(html: str) -> list[tuple[str, str, str]]:
    """[(state, aria, letter), ...] in document order."""
    return _CELL_RE.findall(html)


class WeekStripStatesAndOrderTest(unittest.TestCase):
    def test_monday_first_every_state_and_accessible_text_es(self):
        conn = _reset_db(self)
        gid, habit_id = _make_goal_with_weekly_habit(conn)
        goals.check_habit(conn, habit_id, 1, "2026-11-03", True, hoy=_TODAY)  # Tuesday: done

        with mock.patch.object(comun, "_today_local", return_value=_TODAY):
            r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)

        cells = _cells(r.text)
        self.assertEqual(len(cells), 7)
        self.assertEqual([c[2] for c in cells], ["L", "M", "M", "J", "V", "S", "D"])
        self.assertEqual([c[0] for c in cells],
                         ["pasado", "hecho", "hoy", "futuro", "futuro", "futuro", "futuro"])
        self.assertEqual([c[1] for c in cells], [
            "lunes: sin marcar", "martes: hecho", "miércoles: hoy", "jueves: por venir",
            "viernes: por venir", "sábado: por venir", "domingo: por venir",
        ])

    def test_monday_first_every_state_and_accessible_text_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        gid, habit_id = _make_goal_with_weekly_habit(conn)
        goals.check_habit(conn, habit_id, 1, "2026-11-03", True, hoy=_TODAY)  # Tuesday: done

        with mock.patch.object(comun, "_today_local", return_value=_TODAY):
            r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)

        cells = _cells(r.text)
        self.assertEqual(len(cells), 7)
        self.assertEqual([c[2] for c in cells], ["M", "T", "W", "T", "F", "S", "S"])
        self.assertEqual([c[0] for c in cells],
                         ["pasado", "hecho", "hoy", "futuro", "futuro", "futuro", "futuro"])
        self.assertEqual([c[1] for c in cells], [
            "Monday: not checked", "Tuesday: done", "Wednesday: today", "Thursday: coming up",
            "Friday: coming up", "Saturday: coming up", "Sunday: coming up",
        ])

    def test_today_done_shows_hecho_not_hoy(self):
        conn = _reset_db(self)
        gid, habit_id = _make_goal_with_weekly_habit(conn)
        goals.check_habit(conn, habit_id, 1, _TODAY.isoformat(), True, hoy=_TODAY)

        with mock.patch.object(comun, "_today_local", return_value=_TODAY):
            r = client.get(f"/goals/{gid}")
        cells = _cells(r.text)
        self.assertEqual(cells[2][0], "hecho")
        self.assertEqual(cells[2][1], "miércoles: hecho")


class OnlyWeeklyOwnHabitsGetARowTest(unittest.TestCase):
    def test_day_cadence_and_other_members_habit_have_no_row(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Ana')")
        conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        conn.commit()
        gid = goals.propose(conn, 1, "Aventura compartida", "2026-11-02", kind="milestone",
                            deadline="2027-06-01", hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        goals.add_habit(conn, gid, "Semanal propio", {"per": "week", "times": 1})
        goals.add_habit(conn, gid, "Diario propio", {"per": "day", "times": 1})
        goals.add_habit(conn, gid, "Semanal de Ana", {"per": "week", "times": 1}, assignee_person_id=2)

        with mock.patch.object(comun, "_today_local", return_value=_TODAY):
            r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Semanal propio", r.text)
        self.assertIn("Diario propio", r.text)
        self.assertIn("Semanal de Ana", r.text)
        # Exactly one week strip (7 cells) -- the own weekly habit's -- and no more.
        self.assertEqual(r.text.count("habit-week-cell"), 7)


class WeekChecksOneStatementTest(unittest.TestCase):
    """Ruling 2: the week's checks come from ONE SELECT regardless of how many weekly habits the
    goal has, and zero habit ids costs no statement at all. A connection opened BEFORE
    contar_sql() patches sqlite3.connect is never traced (BODY-RENDIMIENTO-1's own tests/_rutas.py
    contract), so each measurement opens its own plain connection inside the `with` block --
    db.connect()'s two PRAGMAs would otherwise inflate every count by the same fixed amount and
    hide the real one-statement result."""

    def _traced(self, tc: unittest.TestCase, habit_ids: list[int]) -> dict:
        tc_conn_path = config.DB_PATH
        with contar_sql() as stats:
            conn = sqlite3.connect(tc_conn_path)
            conn.row_factory = sqlite3.Row
            try:
                metas._week_checks(conn, habit_ids, 1, _MONDAY, _SUNDAY)
            finally:
                conn.close()
        return stats

    def test_zero_habit_ids_costs_no_statement(self):
        _reset_db(self)
        stats = self._traced(self, [])
        self.assertEqual(stats["statements"], 0)

    def test_one_statement_whether_one_or_many_habit_ids(self):
        conn = _reset_db(self)
        gid, h1 = _make_goal_with_weekly_habit(conn)
        h2 = goals.add_habit(conn, gid, "H2", {"per": "week", "times": 1})
        h3 = goals.add_habit(conn, gid, "H3", {"per": "week", "times": 1})
        conn.commit()

        stats_one = self._traced(self, [h1])
        stats_three = self._traced(self, [h1, h2, h3])
        self.assertEqual(stats_one["statements"], 1)
        self.assertEqual(stats_three["statements"], 1)


if __name__ == "__main__":
    unittest.main()
