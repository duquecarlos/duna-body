"""Body's side of vida-v1 (dev-protocol/contracts/vida-v1.md): the weekly section Personal's Sunday review reads.

    python -m body.vida_export                 # last full week (Monday to Sunday)
    python -m body.vida_export --week 2026-11-02

Writes BODY_HOME/vida/body-week-<monday>.json in the Review section shape:
{"area": "body", "summary", "metrics": [{key, value, kind}], "goals": [{goal_id, on_track}], ...}
Files stay on this PC (rule 1). Personal reads the file, never Body's database (rule 2).
Every figure is visibility "owner": health numbers are never shared by default.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

from . import config, db

PERSON_KEY = "carlos"  # vida-v1: local person key until Body has Cuenta sign-in
WEEK_METRICS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake", "stress_avg",
                "steps", "intensity_minutes"]


def week_section(conn, monday: date, person_id: int = db.DEFAULT_PERSON_ID) -> dict:
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
                 "SELECT g.id, w.on_track FROM goal g LEFT JOIN weekly_check w ON w.goal_id = g.id AND w.week_start = ? "
                 "WHERE g.person_id = ? AND g.status = 'active'", (monday.isoformat(), person_id))]
    days = max((m["days"] for m in metrics), default=0)
    summary = (f"{days} of 7 days with watch data." if days else "No watch data this week.")
    return {
        "area": "body", "summary": summary, "metrics": metrics, "goals": goals,
        "person": PERSON_KEY, "week_start": monday.isoformat(), "source": "body",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.vida_export")
    p.add_argument("--week", help="the Monday of the week, YYYY-MM-DD (default: last full week)")
    a = p.parse_args(argv)
    today = date.today()
    monday = date.fromisoformat(a.week) if a.week else today - timedelta(today.weekday() + 7)
    if monday.weekday() != 0:
        print("--week must be a Monday")
        return 1
    conn = db.connect()
    db.init(conn)
    out_dir = config.BODY_HOME / "vida"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"body-week-{monday.isoformat()}.json"
    path.write_text(json.dumps(week_section(conn, monday), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {path}")  # the path only; never the values
    return 0


if __name__ == "__main__":
    sys.exit(main())
