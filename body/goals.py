"""Goal lifecycle, habit checks and the weekly on-track rule (vida-v1 shapes, body/db.py).

Pure functions over a connection. No schema of its own and nothing sent anywhere, so this stays
Tier 2 even though goal/habit/goal_check are Tier 1 tables.

`evaluar_semana` computes a goal_check body without writing; every GET route uses it for the
current week, live (BODY-SEGURIDAD-3). `weekly_check` adds the upsert; its only caller is
`cerrar_semanas`, run once a day by body/pull.py -- the one writer of goal_check. No GET route may
call `weekly_check` or `cerrar_semanas` (guarded by tests/test_get_sin_escritura.py).
"""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta

from . import baselines, db, fechas, household

RULE_VERSION = "metas-v2"
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


def check_habit(conn, habit_id, person_id, day, done: bool, note: str | None = None, *,
                 hoy: date) -> None:
    """A habit check is an ACTUAL record -- something that already happened. Every caller states
    today (keyword-only, no default -- the web route the person's real local today, the demo its
    own scenario day, each test its fixture's today): a `day` beyond `hoy` plus one day of
    tolerance is refused (`ValueError`, nothing written -- BODY-FECHA-FUTURA-1, hub hit_01m6w6cn8x).
    This function never reads the clock itself, so a caller cannot forget to enforce the rule by
    omission (BODY-FECHA-FUTURA-1-M)."""
    if not fechas.fecha_real_valida(date.fromisoformat(day), hoy):
        raise ValueError(f"check_habit: day {day!r} is past {person_id}'s local today {hoy.isoformat()}")
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


def habit_progress(conn, habit, person_id: int, monday: date, today: date | None = None) -> tuple[int, int, str]:
    """(done, due, period) for habit H, this person, in H's own cadence period -- THE RULE
    (BODY-HABITO-VECES-1, orchestrator ledger), the one place `times` is read:

    days = the week starting `monday` that H can be done on (its shift `context` days via
    `_context_due_days`, or all seven via `_week_days`); checked = P's done=1 checks of H on
    those days.

      per='day'   -> due = len(days); done = min(checked, due). `times` > 1 a day cannot be told
                     apart by one check a day, so the count stays per day.
      per='week'  -> due = min(times, len(days)); done = min(checked, due).
      per='month' -> period='month' tells `_habits_progress` to add nothing for H to the WEEKLY
                     tally (due 0 for the week). done/due returned here are instead H's own
                     calendar-month figure: done = P's checks so far this month (the month of
                     `today`, default the end of `monday`'s week), due = `times`.
    """
    cadence = json.loads(habit["cadence"])
    per = cadence.get("per", "week")
    times = max(1, cadence.get("times", 1))
    if per == "month":
        day = today or (monday + timedelta(6))
        start = day.replace(day=1)
        checked = conn.execute(
            "SELECT COUNT(*) FROM habit_check WHERE habit_id = ? AND person_id = ? AND done = 1 "
            "AND day >= ? AND day <= ?",
            (habit["id"], person_id, start.isoformat(), day.isoformat()),
        ).fetchone()[0]
        return min(checked, times), times, "month"
    context = cadence.get("context")
    due_days = _context_due_days(conn, person_id, monday, context) if context else _week_days(monday)
    if not due_days:
        return 0, 0, per
    placeholders = ",".join("?" * len(due_days))
    checked = conn.execute(
        f"SELECT COUNT(*) FROM habit_check WHERE habit_id = ? AND person_id = ? AND done = 1 "
        f"AND day IN ({placeholders})",
        (habit["id"], person_id, *due_days),
    ).fetchone()[0]
    due = len(due_days) if per == "day" else min(times, len(due_days))
    return min(checked, due), due, per


def habit_due_today(conn, habit, person_id: int, today: date) -> bool:
    """True when H is due today (THE RULE, BODY-HABITO-VECES-1): today is in H's due days for
    this week, and -- for per='week'/'month' only -- H is not already satisfied for its own
    period (this week's `done < due`, or this month's checks < `times`), UNLESS today itself
    carries the done=1 check that satisfied it: a habit already checked today stays listed,
    ticked, as today, even on the day its quota is met, so there is still something to untick
    (BODY-HABITO-VECES-1-M -- the worker's first pass applied this only to per='day'). per='day'
    has no extra gate: each day is its own due instance, so a habit already checked today stays
    listed, ticked, as today."""
    cadence = json.loads(habit["cadence"])
    context = cadence.get("context")
    per = cadence.get("per", "week")
    monday = fechas.lunes_de(today)
    due_days = _context_due_days(conn, person_id, monday, context) if context else _week_days(monday)
    if today.isoformat() not in due_days:
        return False
    if per == "day":
        return True
    done, due, _ = habit_progress(conn, habit, person_id, monday, today)
    if done < due:
        return True
    checked_today = conn.execute(
        "SELECT 1 FROM habit_check WHERE habit_id = ? AND person_id = ? AND day = ? AND done = 1",
        (habit["id"], person_id, today.isoformat()),
    ).fetchone()
    return checked_today is not None


def _habits_progress(conn, goal_id: int, person_id: int, monday: date) -> tuple[int, int]:
    """(habits_done, habits_due) for this goal, this person, this week -- sums `habit_progress`,
    skipping a per='month' habit's own figure (period='month' means due 0 for the week, by THE
    RULE)."""
    habits = conn.execute(
        "SELECT id, cadence FROM habit WHERE goal_id = ? AND active = 1 "
        "AND (assignee_person_id IS NULL OR assignee_person_id = ?)",
        (goal_id, person_id),
    ).fetchall()
    done = due = 0
    for h in habits:
        h_done, h_due, period = habit_progress(conn, h, person_id, monday)
        if period == "month":
            continue
        done += h_done
        due += h_due
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


def evaluar_semana(conn, goal_id: int, person_id: int, week_start: str) -> dict | None:
    """Computes this person's goal_check body for this goal and week -- WITHOUT writing anything
    (BODY-SEGURIDAD-3): the read-only half of what used to be weekly_check's only shape. Same
    fields, same rule_version. Returns None for a goal that is not active: a proposed, paused,
    done or dropped goal is never checked, computed or stored."""
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

    return {"goal_id": goal_id, "person_id": person_id, "week_start": week_start, "metric_avg": metric_avg,
            "on_track": on_track, "habits_done": habits_done, "habits_due": habits_due, "rule_version": RULE_VERSION}


def weekly_check(conn, goal_id: int, person_id: int, week_start: str) -> dict | None:
    """Compute (evaluar_semana) and upsert this person's goal_check row for this goal and week.

    Returns None without writing anything for a goal that is not active: a proposed, paused,
    done or dropped goal is never checked. The only writer is body/pull.py's cerrar_semanas
    (BODY-SEGURIDAD-3): nothing on a GET route may call this.
    """
    result = evaluar_semana(conn, goal_id, person_id, week_start)
    if result is None:
        return None
    with conn:
        conn.execute(
            "INSERT INTO goal_check (goal_id, person_id, week_start, metric_avg, on_track, habits_done, "
            "habits_due, rule_version, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(goal_id, person_id, week_start) DO UPDATE SET "
            "metric_avg = excluded.metric_avg, on_track = excluded.on_track, "
            "habits_done = excluded.habits_done, habits_due = excluded.habits_due, "
            "rule_version = excluded.rule_version",
            (goal_id, person_id, week_start, result["metric_avg"], result["on_track"], result["habits_done"],
             result["habits_due"], RULE_VERSION),
        )
    return result


def cerrar_semanas(conn, person_id: int, *, hoy: date, desde: date | None = None) -> int:
    """Writes this week's and last week's goal_check row for every ACTIVE goal `person_id` can see
    (own goals plus shared household milestones, `goal_scope_sql` -- the same scope /goals uses).
    The one writer of goal_check (BODY-SEGURIDAD-3), run by the daily pull: last week is included
    too so late data (a Sunday pulled on Monday) still lands in the week it belongs to. `hoy` is
    required and keyword-only (the clock rule, BODY-TIER1-LIMPIEZA-1) -- this function never reads
    the clock itself.

    `desde` (BODY-SEMANAS-TARDIAS-1): cerrar_semanas on its own only ever re-closes the current and
    previous week, so data landing further back than that -- a backfill restoring older days, or a
    late habit-check correction -- used to leave the correspondingly older goal_check row stale.
    When given, every week from `lunes_de(desde)` up to (but not including) last week is re-closed
    too. Returns the number of goal_check rows written."""
    hh = household.household_of(conn, person_id)
    scope_sql, scope_params = goal_scope_sql(person_id, hh)
    goal_ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM goal WHERE status = 'active' AND {scope_sql}", scope_params)]
    this_monday = fechas.lunes_de(hoy)
    last_monday = this_monday - timedelta(7)
    weeks = [this_monday, last_monday]
    if desde is not None:
        monday = fechas.lunes_de(desde)
        while monday < last_monday:
            weeks.append(monday)
            monday += timedelta(7)
    written = 0
    for goal_id in goal_ids:
        for week in weeks:
            if weekly_check(conn, goal_id, person_id, week.isoformat()) is not None:
                written += 1
    return written


def personas_con_meta_activa(conn) -> list[int]:
    """Every person who can see at least one ACTIVE goal: a goal's own creator, plus -- for a
    visibility='household' kind='milestone' goal (the only kind a household can share,
    BODY-HOGAR-MIEMBRO-1-M) -- every member of its household (`hogar_miembro`). The daily pull
    (body/pull.py, BODY-SEGURIDAD-3-M) closes weeks for each of these once, whether or not they
    have a connected Garmin account: a habit goal's verdict never depends on Garmin data, so a
    household member with no Garmin connection, or whose pull failed today, still gets theirs."""
    ids = {r["person_id"] for r in conn.execute("SELECT DISTINCT person_id FROM goal WHERE status = 'active'")}
    hogares = [r["hogar_id"] for r in conn.execute(
        "SELECT DISTINCT hogar_id FROM goal WHERE status = 'active' AND visibility = 'household' "
        "AND kind = 'milestone' AND hogar_id IS NOT NULL")]
    if hogares:
        placeholders = ",".join("?" * len(hogares))
        ids.update(r["person_id"] for r in conn.execute(
            f"SELECT person_id FROM hogar_miembro WHERE hogar_id IN ({placeholders})", hogares))
    return sorted(ids)
