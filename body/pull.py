"""Pull Garmin data into the local database, then update Body's own estimates.

    python -m body.pull                 # yesterday and today
    python -m body.pull --date 2026-11-05
    python -m body.pull --days 90       # backfill the last N days
    python -m body.pull --all           # whole history: walk back from the oldest stored day until
                                        # 60 empty days in a row (resumable; skips days already stored)
    python -m body.pull --recompute     # only recompute readiness for every stored day
"""
import argparse
import sys
import time
from datetime import date, timedelta

from . import cuenta, db, garmin_adapter, garmin_cuenta, readiness

PAUSE_S = 1.0  # be gentle with Garmin's servers during a backfill
EMPTY_STOP = 60  # --all stops after this many consecutive days with no watch data


def _stored(conn, person_id: int) -> set[str]:
    return set(db.days_with_data(conn, person_id))


def pull_all(conn, person_id: int) -> int:
    """Walk back day by day until EMPTY_STOP empty days in a row. Returns days saved."""
    stored = _stored(conn, person_id)
    d = date.fromisoformat(min(stored)) if stored else date.today()
    empty = saved = 0
    while empty < EMPTY_STOP:
        d -= timedelta(1)
        key = d.isoformat()
        if key in stored:
            empty = 0
            continue
        time.sleep(PAUSE_S)
        data = garmin_adapter.fetch_day(key)
        if len(data.raw.get("_errors", {})) >= 4:  # Garmin not answering (rate limit?), not an empty day
            print(f"Garmin is not answering at {key}; waiting 2 minutes and retrying once...", flush=True)
            time.sleep(120)
            data = garmin_adapter.fetch_day(key)
            if len(data.raw.get("_errors", {})) >= 4:
                print(f"Still no answer. Stopped at {key}; run `python -m body.pull --all` later to resume.")
                return saved
        if data.metrics or data.activities:
            db.save_day(conn, data, person_id)
            saved += 1
            empty = 0
        else:
            empty += 1
        if saved and saved % 30 == 0 and empty == 0:
            print(f"... back to {key}, {saved} days saved", flush=True)
    print(f"History start reached around {(d + timedelta(EMPTY_STOP)).isoformat()} ({saved} days saved).")
    return saved


def _pull_one_person(conn, person_id: int, days: list[str]) -> None:
    """Pulls `days` for one hosted person, re-encrypting the stored token only if Garmin refreshed
    it. Raises garmin_adapter.NotSignedIn if the token is rejected -- the caller records that and
    moves on to the next person."""
    token = garmin_cuenta.leer(conn, person_id)
    if token is None:
        return
    token_before = token
    for i, d in enumerate(sorted(days)):
        if i:
            time.sleep(PAUSE_S)
        data, token = garmin_adapter.fetch_day(d, token_str=token)
        db.save_day(conn, data, person_id)
    if token != token_before:
        garmin_cuenta.guardar(conn, person_id, token)


def pull_hosted(conn, days: list[str]) -> tuple[int, int, int]:
    """Loops over every person with a connected Garmin account. Returns (persons ok, persons
    failed, days stored). Never prints a health figure -- counts only."""
    ok = failed = saved = 0
    for person_id in garmin_cuenta.every_person_id(conn):
        try:
            _pull_one_person(conn, person_id, days)
        except garmin_adapter.NotSignedIn as e:
            garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
            failed += 1
            continue
        saved += len(days)
        ok += 1
    return ok, failed, saved


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.pull")
    p.add_argument("--date", help="one day, YYYY-MM-DD")
    p.add_argument("--days", type=int, default=2, help="how many days back from today (default 2)")
    p.add_argument("--all", action="store_true", help="whole Garmin history (resumable; local mode only)")
    p.add_argument("--recompute", action="store_true",
                    help="recompute readiness only, no Garmin calls (local mode only)")
    a = p.parse_args(argv)

    conn = db.connect()
    db.init(conn)

    if cuenta.hosted():
        days = [a.date] if a.date else [(date.today() - timedelta(n)).isoformat() for n in range(a.days)]
        ok, failed, saved = pull_hosted(conn, days)
        print(f"{ok} ok, {failed} failed, {saved} days stored", flush=True)
        return 0

    person_id = db.local_person_id(conn)
    if a.all:
        try:
            pull_all(conn, person_id)
        except garmin_adapter.NotSignedIn as e:
            print(e)
            return 2
    elif not a.recompute:
        days = [a.date] if a.date else [(date.today() - timedelta(n)).isoformat() for n in range(a.days)]
        for i, d in enumerate(sorted(days)):
            if i:
                time.sleep(PAUSE_S)
            try:
                data = garmin_adapter.fetch_day(d)
            except garmin_adapter.NotSignedIn as e:
                print(e)
                return 2
            db.save_day(conn, data, person_id)
            missing = data.raw.get("_errors")
            # Counts only: health values never go to logs or stdout.
            print(f"{d}: {len(data.metrics)} metrics, {len(data.activities)} activities"
                  + (f", missing feeds: {', '.join(missing)}" if missing else ""), flush=True)
    # Baselines depend on earlier days, so recompute every stored day in order (cheap, local).
    n = readiness.recompute_all(conn, person_id)
    print(f"Readiness updated for {n} days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
