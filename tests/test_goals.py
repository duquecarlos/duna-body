"""Goal lifecycle, habit checks and the weekly on-track rule."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, goals, household, vida_export


def _conn(case: unittest.TestCase):
    return open_conn(case, Path(tempfile.mkdtemp()) / "g.db")


def _seed_metric(conn, metric_key, person_id, day, value):
    db.set_metric(conn, day, metric_key, value, "garmin", person_id)


class LifecycleTest(unittest.TestCase):
    def test_proposed_active_paused_done(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Lower resting HR", "2026-11-02", metric_key="resting_hr",
                             direction="lower", target_value=50.0, baseline_value=60.0, deadline="2026-12-14")
        self.assertEqual(conn.execute("SELECT status FROM goal WHERE id=?", (gid,)).fetchone()[0], "proposed")
        goals.activate(conn, gid)
        self.assertEqual(conn.execute("SELECT status FROM goal WHERE id=?", (gid,)).fetchone()[0], "active")
        goals.pause(conn, gid)
        self.assertEqual(conn.execute("SELECT status FROM goal WHERE id=?", (gid,)).fetchone()[0], "paused")
        goals.activate(conn, gid)
        goals.finish(conn, gid)
        self.assertEqual(conn.execute("SELECT status FROM goal WHERE id=?", (gid,)).fetchone()[0], "done")

    def test_drop(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Walk more", "2026-11-02", metric_key="steps", direction="raise",
                             target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.drop(conn, gid)
        self.assertEqual(conn.execute("SELECT status FROM goal WHERE id=?", (gid,)).fetchone()[0], "dropped")


class ProposedNeverCheckedTest(unittest.TestCase):
    def test_proposed_goal_not_checked_and_not_exported(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Lower resting HR", "2026-11-02", metric_key="resting_hr",
                             direction="lower", target_value=50.0, baseline_value=60.0, deadline="2026-12-14")
        for i in range(7):
            _seed_metric(conn, "resting_hr", 1, f"2026-11-{2 + i:02d}", 55)
        result = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertIsNone(result)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0], 0)

        section = vida_export.week_section(conn, date(2026, 11, 2), 1)
        self.assertEqual(section["goals"], [])


class MetricOnTrackTest(unittest.TestCase):
    def test_raise_on_track(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Steps", "2026-11-02", metric_key="steps", direction="raise",
                             target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        for i in range(7):
            _seed_metric(conn, "steps", 1, f"2026-11-{2 + i:02d}", 9500)
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertTrue(r["on_track"])

    def test_raise_off_track(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Steps", "2026-11-02", metric_key="steps", direction="raise",
                             target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        for i in range(7):
            _seed_metric(conn, "steps", 1, f"2026-11-{2 + i:02d}", 6100)
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertFalse(r["on_track"])

    def test_lower_on_track(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Resting HR", "2026-11-02", metric_key="resting_hr", direction="lower",
                             target_value=50.0, baseline_value=60.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        for i in range(7):
            _seed_metric(conn, "resting_hr", 1, f"2026-11-{2 + i:02d}", 51)
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertTrue(r["on_track"])

    def test_lower_off_track(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Resting HR", "2026-11-02", metric_key="resting_hr", direction="lower",
                             target_value=50.0, baseline_value=60.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        for i in range(7):
            _seed_metric(conn, "resting_hr", 1, f"2026-11-{2 + i:02d}", 59)
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertFalse(r["on_track"])


class KeepBandTest(unittest.TestCase):
    def test_keep_inside_band(self):
        conn = _conn(self)
        db.init(conn)
        for i in range(20):
            _seed_metric(conn, "stress_avg", 1, f"2026-10-{1 + i:02d}", 30 + (i % 2))
        gid = goals.propose(conn, 1, "Keep stress", "2026-11-02", metric_key="stress_avg", direction="keep",
                             target_value=None, baseline_value=None, deadline=None)
        goals.activate(conn, gid)
        for i in range(7):
            _seed_metric(conn, "stress_avg", 1, f"2026-11-{2 + i:02d}", 30)
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertIsNotNone(r["metric_avg"])
        self.assertTrue(r["on_track"])

    def test_keep_outside_band(self):
        conn = _conn(self)
        db.init(conn)
        for i in range(20):
            _seed_metric(conn, "stress_avg", 1, f"2026-10-{1 + i:02d}", 30 + (i % 2))
        gid = goals.propose(conn, 1, "Keep stress", "2026-11-02", metric_key="stress_avg", direction="keep",
                             target_value=None, baseline_value=None, deadline=None)
        goals.activate(conn, gid)
        for i in range(7):
            _seed_metric(conn, "stress_avg", 1, f"2026-11-{2 + i:02d}", 70)
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertIsNotNone(r["metric_avg"])
        self.assertFalse(r["on_track"])


class FewerThanFourDaysTest(unittest.TestCase):
    def test_not_enough_data_is_null(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Resting HR", "2026-11-02", metric_key="resting_hr", direction="lower",
                             target_value=50.0, baseline_value=60.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        for i in range(3):
            _seed_metric(conn, "resting_hr", 1, f"2026-11-{2 + i:02d}", 51)
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertIsNone(r["on_track"])
        self.assertIsNone(r["metric_avg"])


class MilestoneHabitsTest(unittest.TestCase):
    def test_milestone_from_habits_on_track(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Finish 10k training plan", "2026-11-02", kind="milestone",
                             deadline="2026-12-14")
        goals.activate(conn, gid)
        hid = goals.add_habit(conn, gid, "Run", {"per": "day", "times": 1})
        for i in range(5):  # 5 of 7 days done = 71% >= 70%
            goals.check_habit(conn, hid, 1, f"2026-11-{2 + i:02d}", True, hoy=date(2026, 11, 6))
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertEqual(r["habits_due"], 7)
        self.assertEqual(r["habits_done"], 5)
        self.assertTrue(r["on_track"])

    def test_milestone_from_habits_off_track(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Finish 10k training plan", "2026-11-02", kind="milestone",
                             deadline="2026-12-14")
        goals.activate(conn, gid)
        hid = goals.add_habit(conn, gid, "Run", {"per": "day", "times": 1})
        for i in range(3):  # 3 of 7 = 43%
            goals.check_habit(conn, hid, 1, f"2026-11-{2 + i:02d}", True, hoy=date(2026, 11, 4))
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertFalse(r["on_track"])


class HabitContextTest(unittest.TestCase):
    def test_before_night_counts_only_matching_days(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Finish 10k training plan", "2026-11-02", kind="milestone",
                             deadline="2026-12-14")
        goals.activate(conn, gid)
        hid = goals.add_habit(conn, gid, "Sleep prep", {"per": "day", "times": 1, "context": "before_night"})
        # a single night shift this week, on Thursday 2026-11-05
        conn.execute("INSERT INTO shift (person_id, day, kind, start_local) VALUES (1, '2026-11-05', 'night', "
                     "'2026-11-05T20:00:00')")
        conn.commit()
        goals.check_habit(conn, hid, 1, "2026-11-04", True, hoy=date(2026, 11, 4))  # the day before the night shift
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertEqual(r["habits_due"], 1)
        self.assertEqual(r["habits_done"], 1)

    def test_no_shifts_means_context_habit_not_due(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Finish 10k training plan", "2026-11-02", kind="milestone",
                             deadline="2026-12-14")
        goals.activate(conn, gid)
        goals.add_habit(conn, gid, "Sleep prep", {"per": "day", "times": 1, "context": "before_night"})
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertEqual(r["habits_due"], 0)
        self.assertIsNone(r["on_track"])


class EvaluarSemanaNoWriteTest(unittest.TestCase):
    def test_writes_nothing(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Steps", "2026-11-02", metric_key="steps", direction="raise",
                             target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        for i in range(7):
            _seed_metric(conn, "steps", 1, f"2026-11-{2 + i:02d}", 9500)
        r = goals.evaluar_semana(conn, gid, 1, "2026-11-02")
        self.assertTrue(r["on_track"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0], 0)

    def test_non_active_goal_returns_none_and_writes_nothing(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Steps", "2026-11-02", metric_key="steps", direction="raise",
                             target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        self.assertIsNone(goals.evaluar_semana(conn, gid, 1, "2026-11-02"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0], 0)

    def test_same_fields_as_weekly_check(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Steps", "2026-11-02", metric_key="steps", direction="raise",
                             target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        for i in range(7):
            _seed_metric(conn, "steps", 1, f"2026-11-{2 + i:02d}", 9500)
        computed = goals.evaluar_semana(conn, gid, 1, "2026-11-02")
        written = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertEqual(computed, written)


class CerrarSemanasTest(unittest.TestCase):
    def test_hoy_is_required_and_keyword_only(self):
        conn = _conn(self)
        db.init(conn)
        with self.assertRaises(TypeError):
            goals.cerrar_semanas(conn, 1)

    def test_writes_this_and_last_week_for_active_goals_only(self):
        conn = _conn(self)
        db.init(conn)
        gid_active = goals.propose(conn, 1, "Steps", "2026-10-01", metric_key="steps", direction="raise",
                                    target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid_active)
        gid_proposed = goals.propose(conn, 1, "Other", "2026-10-01", metric_key="steps", direction="raise",
                                      target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        n = goals.cerrar_semanas(conn, 1, hoy=date(2026, 11, 5))  # a Thursday; Monday is 2026-11-02
        self.assertEqual(n, 2)
        rows = conn.execute("SELECT week_start FROM goal_check WHERE goal_id = ? ORDER BY week_start",
                            (gid_active,)).fetchall()
        self.assertEqual([r["week_start"] for r in rows], ["2026-10-26", "2026-11-02"])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM goal_check WHERE goal_id = ?", (gid_proposed,)).fetchone()[0], 0)

    def test_household_milestone_included_for_a_member(self):
        conn = _conn(self)
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        conn.commit()
        gid = goals.propose(conn, 1, "Shared steps goal", "2026-10-01", kind="milestone", deadline="2026-12-14",
                            hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        n = goals.cerrar_semanas(conn, 2, hoy=date(2026, 11, 5))  # person 2: a member, not the creator
        self.assertEqual(n, 2)
        rows = conn.execute(
            "SELECT week_start FROM goal_check WHERE goal_id = ? AND person_id = ? ORDER BY week_start",
            (gid, 2)).fetchall()
        self.assertEqual([r["week_start"] for r in rows], ["2026-10-26", "2026-11-02"])

    def test_desde_recloses_the_older_weeks_and_no_others(self):
        """BODY-SEMANAS-TARDIAS-1: `desde` re-closes every week from its own Monday up to (not
        including) last week -- which cerrar_semanas already re-closes on its own -- and nothing
        further back than that."""
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Steps", "2026-09-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        # hoy a Thursday (Monday 2026-11-02); desde's own Monday is 2026-10-19, one week before
        # last week (2026-10-26) -- exactly one extra older week, no further back.
        n = goals.cerrar_semanas(conn, 1, hoy=date(2026, 11, 5), desde=date(2026, 10, 20))
        self.assertEqual(n, 3)
        rows = conn.execute("SELECT week_start FROM goal_check WHERE goal_id = ? ORDER BY week_start",
                            (gid,)).fetchall()
        self.assertEqual([r["week_start"] for r in rows], ["2026-10-19", "2026-10-26", "2026-11-02"])

    def test_desde_on_or_after_last_monday_adds_nothing(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Steps", "2026-09-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        n = goals.cerrar_semanas(conn, 1, hoy=date(2026, 11, 5), desde=date(2026, 10, 29))  # last week itself
        self.assertEqual(n, 2)
        rows = conn.execute("SELECT week_start FROM goal_check WHERE goal_id = ? ORDER BY week_start",
                            (gid,)).fetchall()
        self.assertEqual([r["week_start"] for r in rows], ["2026-10-26", "2026-11-02"])


class PersonasConMetaActivaTest(unittest.TestCase):
    """BODY-SEGURIDAD-3-M: the person list the daily pull sweeps to close weeks comes from the
    goal table (own goals plus shared household milestones), never from garmin_cuenta."""

    def test_own_active_goal_only(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Steps", "2026-10-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        self.assertEqual(goals.personas_con_meta_activa(conn), [1])

    def test_proposed_or_archived_goal_excluded(self):
        conn = _conn(self)
        db.init(conn)
        goals.propose(conn, 1, "Steps", "2026-10-01", metric_key="steps", direction="raise",
                      target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        gid_done = goals.propose(conn, 1, "Old", "2026-10-01", metric_key="steps", direction="raise",
                                 target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        goals.activate(conn, gid_done)
        goals.finish(conn, gid_done)
        self.assertEqual(goals.personas_con_meta_activa(conn), [])

    def test_household_milestone_includes_every_member_not_only_the_creator(self):
        conn = _conn(self)
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        conn.commit()
        gid = goals.propose(conn, 1, "Shared steps goal", "2026-10-01", kind="milestone", deadline="2026-12-14",
                            hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        self.assertEqual(goals.personas_con_meta_activa(conn), [1, 2])

    def test_legacy_household_metric_goal_does_not_add_the_member(self):
        """A pre-BODY-HOGAR-MIEMBRO-1-M row (visibility='household', kind='metric') is never
        shared -- the same exclusion goal_access and goal_scope_sql apply."""
        conn = _conn(self)
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        gid = goals.propose(conn, 1, "Legacy", "2026-10-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        conn.execute("UPDATE goal SET visibility = 'household', hogar_id = 'h-1' WHERE id = ?", (gid,))
        goals.activate(conn, gid)
        conn.commit()
        self.assertEqual(goals.personas_con_meta_activa(conn), [1])


class HouseholdGoalCheckTest(unittest.TestCase):
    def test_two_members_each_get_their_own_goal_check(self):
        conn = _conn(self)
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        conn.commit()
        gid = goals.propose(conn, 1, "Shared steps goal", "2026-11-02", kind="milestone",
                             deadline="2026-12-14", hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        hid = goals.add_habit(conn, gid, "Walk", {"per": "day", "times": 1})
        for i in range(5):  # person 1: 5 of 7 = 71% >= 70%
            goals.check_habit(conn, hid, 1, f"2026-11-{2 + i:02d}", True, hoy=date(2026, 11, 6))
        for i in range(3):  # person 2: 3 of 7 = 43%
            goals.check_habit(conn, hid, 2, f"2026-11-{2 + i:02d}", True, hoy=date(2026, 11, 6))
        r1 = goals.weekly_check(conn, gid, 1, "2026-11-02")
        r2 = goals.weekly_check(conn, gid, 2, "2026-11-02")
        self.assertTrue(r1["on_track"])
        self.assertFalse(r2["on_track"])
        rows = conn.execute("SELECT person_id FROM goal_check WHERE goal_id=? ORDER BY person_id", (gid,)).fetchall()
        self.assertEqual([r["person_id"] for r in rows], [1, 2])


class HouseholdMetricRefusedTest(unittest.TestCase):
    """BODY-HOGAR-MIEMBRO-1-M fix 1: a household goal is a milestone -- a metric goal can't be shared."""

    def test_propose_refuses_metric_household_and_writes_nothing(self):
        conn = _conn(self)
        db.init(conn)
        with self.assertRaises(ValueError):
            goals.propose(conn, 1, "Shared steps goal", "2026-11-02", metric_key="steps", direction="raise",
                          target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14", hogar_id="h-1",
                          visibility="household")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0], 0)

    def test_milestone_household_still_allowed(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Shared milestone", "2026-11-02", kind="milestone",
                            deadline="2026-12-14", hogar_id="h-1", visibility="household")
        self.assertEqual(conn.execute("SELECT visibility FROM goal WHERE id = ?", (gid,)).fetchone()[0],
                         "household")


class LegacyMetricHouseholdRowTest(unittest.TestCase):
    """A metric goal marked household from before this slice (the only way one can still exist,
    since goals.propose now refuses to create one) stays creator-only, forever."""

    def test_legacy_metric_household_row_is_creator_only(self):
        conn = _conn(self)
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        gid = goals.propose(conn, 1, "Old shared metric goal", "2026-11-02", metric_key="steps",
                            direction="raise", target_value=9000.0, baseline_value=6000.0,
                            deadline="2026-12-14")
        conn.execute("UPDATE goal SET hogar_id = 'h-1', visibility = 'household' WHERE id = ?", (gid,))
        conn.commit()

        self.assertEqual(goals.goal_access(conn, gid, 1), "creator")
        self.assertIsNone(goals.goal_access(conn, gid, 2))

        hh = household.household_of(conn, 2)
        scope_sql, scope_params = goals.goal_scope_sql(2, hh)
        rows = conn.execute(f"SELECT id FROM goal WHERE {scope_sql}", scope_params).fetchall()
        self.assertEqual([r["id"] for r in rows], [])


OLD_GOAL_SCHEMA = """
CREATE TABLE person (
    id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'America/Bogota',
    language TEXT NOT NULL DEFAULT 'es'
);
CREATE TABLE metric_def (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    unit TEXT NOT NULL,
    better TEXT NOT NULL CHECK (better IN ('higher', 'lower', 'neutral')),
    kind TEXT NOT NULL DEFAULT 'measured' CHECK (kind IN ('measured', 'estimated'))
);
CREATE TABLE shift (
    person_id   INTEGER NOT NULL REFERENCES person(id),
    day         TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('morning', 'afternoon', 'night', 'day', 'off', 'other')),
    code        TEXT,
    start_local TEXT,
    end_local   TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (person_id, day, start_local)
);
CREATE TABLE goal (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES person(id),
    hogar_id       TEXT,
    title          TEXT NOT NULL,
    metric_key     TEXT REFERENCES metric_def(key),
    target_value   REAL,
    baseline_value REAL,
    start_day      TEXT NOT NULL,
    deadline       TEXT,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done', 'dropped'))
);
CREATE TABLE plan_item (
    id            INTEGER PRIMARY KEY,
    goal_id       INTEGER NOT NULL REFERENCES goal(id),
    title         TEXT NOT NULL,
    cadence       TEXT,
    shift_context TEXT,
    assignee_person_id INTEGER REFERENCES person(id),
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE weekly_check (
    id          INTEGER PRIMARY KEY,
    goal_id     INTEGER NOT NULL REFERENCES goal(id),
    week_start  TEXT NOT NULL,
    metric_avg  REAL,
    on_track    INTEGER,
    note        TEXT,
    UNIQUE (goal_id, week_start)
);
"""


def _build_old_db(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp()) / "old.db")
    conn.executescript(OLD_GOAL_SCHEMA)
    conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
    conn.execute("INSERT INTO metric_def (key, label, unit, better) VALUES ('resting_hr', 'Resting HR', 'bpm', "
                 "'lower')")
    conn.execute("INSERT INTO goal (id, person_id, hogar_id, title, metric_key, target_value, baseline_value, "
                 "start_day, deadline, status) VALUES (1, 1, NULL, 'Lower resting HR', 'resting_hr', 50, 60, "
                 "'2026-10-01', '2026-12-01', 'active')")
    conn.execute("INSERT INTO plan_item (id, goal_id, title, cadence, shift_context, assignee_person_id, active) "
                 "VALUES (1, 1, 'Sleep early', 'daily', NULL, NULL, 1)")
    conn.execute("INSERT INTO plan_item (id, goal_id, title, cadence, shift_context, assignee_person_id, active) "
                 "VALUES (2, 1, 'Wind down', '3x/week', 'before_night', NULL, 1)")
    conn.execute("INSERT INTO weekly_check (id, goal_id, week_start, metric_avg, on_track, note) "
                 "VALUES (1, 1, '2026-10-05', 55.0, 1, NULL)")
    conn.commit()
    return conn


class MigrationTest(unittest.TestCase):
    def test_old_shape_migrates_to_new_shape(self):
        conn = _build_old_db(self)
        db.init(conn)

        goal = conn.execute("SELECT * FROM goal WHERE id=1").fetchone()
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["kind"], "metric")
        self.assertEqual(goal["direction"], "lower")
        self.assertEqual(goal["area"], "body")
        self.assertEqual(goal["visibility"], "private")

        habits = {r["id"]: r for r in conn.execute("SELECT * FROM habit ORDER BY id")}
        self.assertEqual(len(habits), 2)
        self.assertEqual(json.loads(habits[1]["cadence"]), {"per": "day", "times": 1})
        self.assertEqual(habits[1]["cadence_text_legacy"], "daily")
        self.assertEqual(json.loads(habits[2]["cadence"]), {"per": "week", "times": 3, "context": "before_night"})
        self.assertEqual(habits[2]["cadence_text_legacy"], "3x/week")

        gc = conn.execute("SELECT * FROM goal_check WHERE id=1").fetchone()
        self.assertEqual(gc["person_id"], 1)
        self.assertEqual(gc["rule_version"], "legacy")
        self.assertEqual(gc["metric_avg"], 55.0)
        self.assertEqual(gc["on_track"], 1)

        for name in ("plan_item", "weekly_check"):
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    def test_second_init_is_a_no_op(self):
        conn = _build_old_db(self)
        db.init(conn)
        before_goal = dict(conn.execute("SELECT * FROM goal WHERE id=1").fetchone())
        before_habits = [dict(r) for r in conn.execute("SELECT * FROM habit ORDER BY id")]
        before_gc = [dict(r) for r in conn.execute("SELECT * FROM goal_check ORDER BY id")]

        db.init(conn)

        after_goal = dict(conn.execute("SELECT * FROM goal WHERE id=1").fetchone())
        after_habits = [dict(r) for r in conn.execute("SELECT * FROM habit ORDER BY id")]
        after_gc = [dict(r) for r in conn.execute("SELECT * FROM goal_check ORDER BY id")]
        self.assertEqual(before_goal, after_goal)
        self.assertEqual(before_habits, after_habits)
        self.assertEqual(before_gc, after_gc)

    def test_forced_count_mismatch_rolls_back(self):
        conn = _build_old_db(self)
        calls = {"n": 0}
        real = db._copy_habit_row

        def flaky(conn, r, old_cols):
            calls["n"] += 1
            if calls["n"] == 2:
                return  # silently drop this row, forcing a count mismatch
            real(conn, r, old_cols)

        with mock.patch.object(db, "_copy_habit_row", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                db.init(conn)

        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='habit'").fetchone())
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_item'").fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM plan_item").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
