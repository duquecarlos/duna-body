"""Body's side of vida-v1 (dev-protocol/contracts/vida-v1.md): the weekly section Personal's Sunday review reads.

    python -m body.vida_export                 # last full week (Monday to Sunday)
    python -m body.vida_export --week 2026-11-02
    python -m body.vida_export --current       # the week that started this Monday (for the Sunday-evening review)

Writes BODY_HOME/vida/body-week-<monday>.json in the Review section shape:
{"area": "body", "summary", "metrics": [{key, value, kind}], "goals": [{goal_id, on_track}],
 "goal_checks": [{goal_id, person, week_start, metric_avg, on_track, note}], ...}
Files stay on this PC (rule 1). Personal reads the file, never Body's database (rule 2).
Every figure is visibility "owner": health numbers are never shared by default.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

from . import config, cuenta, db, household

PERSON_KEY = "carlos"  # vida-v1: local person key until Body has Cuenta sign-in
WEEK_METRICS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake", "stress_avg",
                "steps", "intensity_minutes"]


def current_week_monday(today: date | None = None) -> date:
    """The week `--current` picks: Monday of the week `today` falls in. body/web.py's
    GET /api/vida/semana reuses this for its own default, rather than re-deriving it."""
    today = today or date.today()
    return today - timedelta(today.weekday())


def week_section(conn, monday: date, person_id: int) -> dict:
    sunday = monday + timedelta(6)
    defs = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM metric_def")}
    metrics = []
    for key in WEEK_METRICS:
        vals = list(db.series(conn, key, monday.isoformat(), sunday.isoformat(), person_id).values())
        if not vals:
            continue
        avg = statistics.fmean(vals)
        metrics.append({"key": f"body.{key}", "value": round(avg, 1), "kind": defs[key]["kind"],
                        "days": len(vals), "visibility": "owner"})
    goals = [{"goal_id": r["id"], "on_track": None if r["on_track"] is None else bool(r["on_track"])}
             for r in conn.execute(
                 "SELECT g.id, gc.on_track FROM goal g "
                 "LEFT JOIN goal_check gc ON gc.goal_id = g.id AND gc.week_start = ? AND gc.person_id = g.person_id "
                 "WHERE g.person_id = ? AND g.status = 'active'", (monday.isoformat(), person_id))]

    goal_checks = []
    hh = household.household_of(conn, person_id)
    if hh:
        for r in conn.execute(
                "SELECT gc.goal_id, gc.metric_avg, gc.on_track, gc.note FROM goal g "
                "JOIN goal_check gc ON gc.goal_id = g.id AND gc.week_start = ? AND gc.person_id = ? "
                "WHERE g.hogar_id = ? AND g.status = 'active'", (monday.isoformat(), person_id, hh.id)):
            goal_checks.append({"goal_id": r["goal_id"], "person": PERSON_KEY, "week_start": monday.isoformat(),
                                "metric_avg": r["metric_avg"],
                                "on_track": None if r["on_track"] is None else bool(r["on_track"]),
                                "note": r["note"]})

    days = max((m["days"] for m in metrics), default=0)
    summary = (f"{days} of 7 days with watch data." if days else "No watch data this week.")
    return {
        "area": "body", "summary": summary, "metrics": metrics, "goals": goals, "goal_checks": goal_checks,
        "person": PERSON_KEY, "week_start": monday.isoformat(), "source": "body",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.vida_export")
    p.add_argument("--week", help="the Monday of the week, YYYY-MM-DD (default: last full week)")
    p.add_argument("--current", action="store_true", help="this week, Monday to today")
    a = p.parse_args(argv)
    if cuenta.hosted():
        print("hosted: the weekly Body section is served per person at /api/vida/semana")
        return 1
    today = date.today()
    if a.week:
        monday = date.fromisoformat(a.week)
    elif a.current:
        monday = current_week_monday(today)
    else:
        monday = today - timedelta(today.weekday() + 7)
    if monday.weekday() != 0:
        print("--week must be a Monday")
        return 1
    conn = db.connect()
    db.init(conn)
    person_id = db.local_person_id(conn)
    out_dir = config.BODY_HOME / "vida"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"body-week-{monday.isoformat()}.json"
    path.write_text(json.dumps(week_section(conn, monday, person_id), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {path}")  # the path only; never the values
    return 0


if __name__ == "__main__":
    sys.exit(main())
