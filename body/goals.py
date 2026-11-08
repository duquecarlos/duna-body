"""Goal lifecycle, habit checks and the weekly on-track rule (vida-v1 shapes, body/db.py).

Pure functions over a connection. No schema of its own and nothing sent anywhere, so this stays
Tier 2 even though goal/habit/goal_check are Tier 1 tables.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta

from . import baselines, db, household

RULE_VERSION = "metas-v1"
MIN_METRIC_DAYS = 4             # fewer days with data this week -> on_track = NULL, an honest gap
MILESTONE_ON_TRACK_PCT = 0.70   # share of due habits that must be done for a milestone week to be on track


def goal_access(conn, goal_id: int, person_id: int) -> str | None:
    """'creator' if this person created goal G, 'member' if G is visibility='household', kind=
    'milestone' and shared with their ACTIVE household (body/household.py's household_of, the one
    place membership is read), else None (treat as not found: every goal/habit route and the
    assistant's read tools check here instead of keeping their own `person_id = ?` clause --
    BODY-HOGAR-MIEMBRO-1). The kind='milestone' requirement (BODY-HOGAR-MIEMBRO-1-M) is a household
    goal is a milestone in v1: a metric goal keeps only the creator's baseline_value, so a member's
    verdict and chart would silently read the wrong person's plan (BODY-HOGAR-PLAN-1 (cont.)) --
    a legacy row still marked household with kind='metric' stays creator-only, forever."""
    g = conn.execute("SELECT person_id, hogar_id, visibility, kind FROM goal WHERE id = ?", (goal_id,)).fetchone()
    if g is None:
        return None
    if g["person_id"] == person_id:
        return "creator"
    if g["visibility"] == "household" and g["kind"] == "milestone" and g["hogar_id"]:
        hh = household.household_of(conn, person_id)
        if hh and hh.id == g["hogar_id"]:
            return "member"
    return None


def goal_scope_sql(person_id: int, hh) -> tuple[str, tuple]:
    """A WHERE fragment (and its params) selecting every goal `person_id` may see: their own, plus
    any visibility='household', kind='milestone' goal shared with `hh` (their `household.household_of`
    result, or None). The one scope every goal-listing site uses, instead of a bare `person_id = ?`.
    kind='milestone' is required for the same reason goal_access requires it (BODY-HOGAR-MIEMBRO-1-M,
    citing BODY-HOGAR-PLAN-1 (cont.)): a legacy metric household row stays out of every member's list."""
    if hh:
        return ("(person_id = ? OR (visibility = 'household' AND kind = 'milestone' AND hogar_id = ?))",
                (person_id, hh.id))
    return "person_id = ?", (person_id,)


def propose(conn, person_id, title, start_day, *, area="body", why=None, kind="metric", metric_key=None,
            direction=None, target_value=None, baseline_value=None, deadline=None, hogar_id=None,
            visibility="private") -> int:
    """Create a goal with status 'proposed'. Returns its id.

    Refuses (ValueError, nothing written) a metric goal proposed as visibility='household': a
    household goal is a milestone in v1 (BODY-HOGAR-MIEMBRO-1-M) -- a metric goal's verdict and
    chart are judged on its own baseline_value, which only its creator has, so sharing one would
    silently judge every member on the creator's numbers (BODY-HOGAR-PLAN-1 (cont.))."""
    if visibility == "household" and kind == "metric":
        raise ValueError("a household goal is a milestone in v1: a metric goal cannot be visibility='household'")
    with conn:
        cur = conn.execute(
            "INSERT INTO goal (person_id, hogar_id, area, title, why, kind, metric_key, direction, "
            "target_value, baseline_value, start_day, deadline, status, visibility) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
            (person_id, hogar_id, area, title, why, kind, metric_key, direction, target_value, baseline_value,
             start_day, deadline, visibility),
        )
    return cur.lastrowid


def _set_status(conn, goal_id, status) -> None:
    with conn:
        conn.execute("UPDATE goal SET status = ? WHERE id = ?", (status, goal_id))


def activate(conn, goal_id) -> None:
    _set_status(conn, goal_id, "active")


def pause(conn, goal_id) -> None:
    _set_status(conn, goal_id, "paused")


def finish(conn, goal_id) -> None:
    _set_status(conn, goal_id, "done")


def drop(conn, goal_id) -> None:
    _set_status(conn, goal_id, "dropped")


def add_habit(conn, goal_id, title, cadence: dict, *, assignee_person_id=None, active=True) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO habit (goal_id, title, cadence, assignee_person_id, active) VALUES (?, ?, ?, ?, ?)",
            (goal_id, title, json.dumps(cadence), assignee_person_id, 1 if active else 0),
        )
    return cur.lastrowid


def check_habit(conn, habit_id, person_id, day, done: bool, note: str | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO habit_check (habit_id, person_id, day, done, note) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(habit_id, person_id, day) DO UPDATE SET done = excluded.done, note = excluded.note",
            (habit_id, person_id, day, 1 if done else 0, note),
        )


def _week_days(monday: date) -> list[str]:
    return [(monday + timedelta(i)).isoformat() for i in range(7)]


def _context_due_days(conn, person_id: int, monday: date, context: str) -> list[str]:
    """Days this week where a habit's shift context applies.

    before_night = the day before a night shift's day; after_night = the day after;
    off_days = 'off' days. No shift rows for a day -> that day is not due.
    """
    sunday = monday + timedelta(6)
    shifts = {r["day"]: r["kind"] for r in conn.execute(
        "SELECT day, kind FROM shift WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, (monday - timedelta(1)).isoformat(), (sunday + timedelta(1)).isoformat()))}
    days = _week_days(monday)
    if context == "before_night":
        return [d for d in days if shifts.get((date.fromisoformat(d) + timedelta(1)).isoformat()) == "night"]
    if context == "after_night":
        return [d for d in days if shifts.get((date.fromisoformat(d) - timedelta(1)).isoformat()) == "night"]
    if context == "off_days":
        return [d for d in days if shifts.get(d) == "off"]
    return days


def _habits_progress(conn, goal_id: int, person_id: int, monday: date) -> tuple[int, int]:
    """(habits_done, habits_due) for this goal, this person, this week."""
    habits = conn.execute(
        "SELECT id, cadence FROM habit WHERE goal_id = ? AND active = 1 "
        "AND (assignee_person_id IS NULL OR assignee_person_id = ?)",
        (goal_id, person_id),
    ).fetchall()
    done = due = 0
    for h in habits:
        cadence = json.loads(h["cadence"])
        context = cadence.get("context")
        due_days = _context_due_days(conn, person_id, monday, context) if context else _week_days(monday)
        if not due_days:
            continue
        due += len(due_days)
        placeholders = ",".join("?" * len(due_days))
        done += conn.execute(
            f"SELECT COUNT(*) FROM habit_check WHERE habit_id = ? AND person_id = ? AND done = 1 "
            f"AND day IN ({placeholders})",
            (h["id"], person_id, *due_days),
        ).fetchone()[0]
    return done, due


def _expected_on_line(goal, monday: date) -> float | None:
    """The straight line from baseline_value at start_day to target_value at deadline, at week's end."""
    if not goal["deadline"] or not goal["start_day"]:
        return None
    if goal["baseline_value"] is None or goal["target_value"] is None:
        return None
    start = date.fromisoformat(goal["start_day"])
    end = date.fromisoformat(goal["deadline"])
    at = monday + timedelta(6)
    if end <= start:
        return goal["target_value"]
    frac = max(0.0, min(1.0, (at - start).days / (end - start).days))
    return goal["baseline_value"] + frac * (goal["target_value"] - goal["baseline_value"])


def weekly_check(conn, goal_id: int, person_id: int, week_start: str) -> dict | None:
    """Compute and upsert this person's goal_check row for this goal and week.

    Returns None without writing anything for a goal that is not active: a proposed, paused,
    done or dropped goal is never checked.
    """
    goal = conn.execute("SELECT * FROM goal WHERE id = ?", (goal_id,)).fetchone()
    if goal is None or goal["status"] != "active":
        return None
    monday = date.fromisoformat(week_start)
    sunday = monday + timedelta(6)

    habits_done, habits_due = _habits_progress(conn, goal_id, person_id, monday)

    metric_avg = None
    on_track = None
    if goal["kind"] == "metric":
        vals = list(db.series(conn, goal["metric_key"], monday.isoformat(), sunday.isoformat(), person_id).values())
        if len(vals) >= MIN_METRIC_DAYS:
            metric_avg = statistics.fmean(vals)
            if goal["direction"] == "keep":
                b = baselines.baseline(conn, goal["metric_key"], monday.isoformat(), person_id=person_id)
                on_track = (b.low <= metric_avg <= b.high) if b else None
            else:
                expected = _expected_on_line(goal, monday)
                if expected is not None:
                    on_track = metric_avg >= expected if goal["direction"] == "raise" else metric_avg <= expected
    elif habits_due > 0:
        on_track = (habits_done / habits_due) >= MILESTONE_ON_TRACK_PCT

    with conn:
        conn.execute(
            "INSERT INTO goal_check (goal_id, person_id, week_start, metric_avg, on_track, habits_done, "
            "habits_due, rule_version, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(goal_id, person_id, week_start) DO UPDATE SET "
            "metric_avg = excluded.metric_avg, on_track = excluded.on_track, "
            "habits_done = excluded.habits_done, habits_due = excluded.habits_due, "
            "rule_version = excluded.rule_version",
            (goal_id, person_id, week_start, metric_avg, on_track, habits_done, habits_due, RULE_VERSION),
        )
    return {"goal_id": goal_id, "person_id": person_id, "week_start": week_start, "metric_avg": metric_avg,
            "on_track": on_track, "habits_done": habits_done, "habits_due": habits_due, "rule_version": RULE_VERSION}
