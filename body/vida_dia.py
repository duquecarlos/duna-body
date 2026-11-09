"""Body's side of vida-v1 (dev-protocol/contracts/vida-v1.md): the DAY section Personal's morning
brief reads -- approximately when Carlos woke up, how last night's sleep went, and the readiness
figure Body already computes.

    python -m body.vida_dia                    # today, in the person's own zone
    python -m body.vida_dia --day 2026-11-09

Writes BODY_HOME/vida/body-day-<day>.json:
{"person", "day", "generated_at", "metrics": [metric_value rows], "events": [Event rows], "missing"}
Built only from vida-v1 v1 blocks already defined (Metric, Event) -- no contract change needed.
Files stay on this PC (rule 1). Personal reads the file, never Body's database (rule 2).
Every row is visibility "owner": health figures are never shared by default.

A figure the watch has not synced yet is never invented and never zero: its namespaced key goes in
`missing` instead of a metric row, so the reader can say "your watch has not synced last night yet"
rather than show a false number.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

from . import config, cuenta, db, fechas, vida_export

# The figures this brief needs, as a module constant so the set is not scattered across the
# functions below. Namespaced `body.<key>` in the output, same as vida_export.
DAY_METRICS = ["sleep_minutes", "sleep_score", "readiness", "body_battery_wake"]


def _metric_source(key: str) -> str:
    """vida_export's metrics all come from Garmin; readiness is Body's own estimate."""
    return "body" if key == "readiness" else "garmin"


def _sleep_event_id(person: str, day: str) -> str:
    """A stable uuid derived from person+day -- the same day's file always carries the same id,
    so Personal can tell "the same night, rewritten" from "a different night" without comparing
    every field."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"body.vida_dia:{person}:{day}:sleep"))


def day_section(conn, day: str, person_id: int) -> dict:
    person = vida_export.vida_person(conn, person_id)
    tz = db.person(conn, person_id)["timezone"]
    defs = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM metric_def")}

    metrics = []
    missing = []
    for key in DAY_METRICS:
        value = db.series(conn, key, day, day, person_id).get(day)
        if value is None:
            missing.append(f"body.{key}")
            continue
        metrics.append({
            "person": person, "day": day, "key": f"body.{key}", "value": value,
            "source": _metric_source(key), "visibility": "owner", "kind": defs[key]["kind"],
        })

    events = []
    sleep = conn.execute(
        "SELECT start_local, end_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, day),
    ).fetchone()
    if sleep is not None:
        events.append({
            "id": _sleep_event_id(person, day), "person": person, "hogar_id": None,
            "kind": "body.sleep", "starts_at": sleep["start_local"], "ends_at": sleep["end_local"],
            "all_day": False, "timezone": tz, "source": "garmin", "ref": None, "data": None,
        })

    return {
        "person": person, "day": day,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": metrics, "events": events, "missing": missing,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.vida_dia")
    p.add_argument("--day", help="YYYY-MM-DD (default: today in the person's own zone)")
    a = p.parse_args(argv)
    if cuenta.hosted():
        print("hosted: the daily Body section is served per person (a later slice)")
        return 1
    conn = db.connect()
    try:
        db.init(conn)
        person_id = db.local_person_id(conn)
        day = a.day or fechas.hoy_local(db.person(conn, person_id)).isoformat()
        out_dir = config.BODY_HOME / "vida"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"body-day-{day}.json"
        path.write_text(json.dumps(day_section(conn, day, person_id), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"Wrote {path}")  # the path only; never the values
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
