"""Body's morning wake check for Personal's desk (BODY-DESPERTAR-1): every ~20 minutes between
04:00 and 13:00, pull ONLY last night's Garmin sleep and, as soon as its end is known, write
BODY_HOME/vida/despertar-<day>.json so Personal's morning brief can read when Carlos woke up,
instead of falling back to his shift slot.

    python -m body.despertar [--day YYYY-MM-DD]

One Garmin sleep-only call per check (garmin_adapter.fetch_sleep, not the heavier fetch_day), and
NONE once the day's file already exists or the daily pull already stored sleep_end for that day.
Local person only (no hosted path yet). Always exits 0 so the scheduled task stays calm -- a
problem is a printed line, never a traceback, and never a payload.

File: exactly {"woke_at": "<local ISO with offset>", "source": "garmin", "written_at": "<ISO now,
with offset>"} -- no other figure -- written atomically (temp file + os.replace, same pattern as
body/traer_semana.py) so the desk's watcher never reads a half-written file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, cuenta, db, fechas, garmin_adapter, pull

VIDA_DIR_NAME = "vida"


def _path_for(day: str) -> Path:
    return config.BODY_HOME / VIDA_DIR_NAME / f"despertar-{day}.json"


def _with_offset(local_iso: str, tz_name: str, timespec: str) -> str:
    """Attaches the person's own zone to a naive local-wall-time ISO string -- the convention
    garmin_adapter._local_iso and sleep_session.end_local both already use -- so the file carries
    an offset the desk can read unambiguously, never a bare wall time."""
    dt = datetime.fromisoformat(local_iso).replace(tzinfo=ZoneInfo(tz_name))
    return dt.isoformat(timespec=timespec)


def _write(day: str, woke_at_local_iso: str, tz_name: str) -> Path:
    out_dir = config.BODY_HOME / VIDA_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _path_for(day)
    payload = {
        "woke_at": _with_offset(woke_at_local_iso, tz_name, "minutes"),
        "source": "garmin",
        "written_at": datetime.now(ZoneInfo(tz_name)).isoformat(timespec="seconds"),
    }
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp_path.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    return path


def _sleep_end_stored(conn, person_id: int, day: str) -> str | None:
    row = conn.execute(
        "SELECT end_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, day),
    ).fetchone()
    return row["end_local"] if row else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m body.despertar")
    ap.add_argument("--day", help="YYYY-MM-DD (default: today in the person's own zone)")
    a = ap.parse_args(argv)

    if cuenta.hosted():
        print("despertar: hosted no soportado todavia")
        return 0

    conn = db.connect()
    try:
        db.init(conn)
        person_id = db.local_person_id(conn)
        person = db.person(conn, person_id)
        tz_name = person["timezone"]
        day = a.day or fechas.hoy_local(person).isoformat()

        if _path_for(day).exists():
            print("despertar: ya escrito")
            return 0

        stored = _sleep_end_stored(conn, person_id, day)
        if stored is not None:
            _write(day, stored, tz_name)
            print("despertar: escrito (ya en la base)")
            return 0

        if not garmin_adapter.has_token():
            print("despertar: sin sesion de Garmin")
            return 0

        lock = pull._try_lock()
        if lock is None:
            print("despertar: pull en curso, se omite este chequeo")
            return 0
        try:
            try:
                wake = garmin_adapter.fetch_sleep(day)
            except Exception as e:  # keep the scheduler calm -- a kind, never a payload
                print(f"despertar: error {type(e).__name__}")
                return 0
        finally:
            pull._unlock(lock)

        if wake is None:
            print("despertar: sin dato todavia")
            return 0

        _write(day, wake, tz_name)
        print("despertar: escrito")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
