"""Pull Garmin data into the local database, then update Body's own estimates.

    python -m body.pull                 # yesterday and today
    python -m body.pull --date 2026-11-05
    python -m body.pull --days 90       # pull the last N days
    python -m body.pull --all           # whole history: walk back from the oldest stored day until
                                        # 60 empty days in a row (resumable; skips days already stored)
    python -m body.pull --backfill      # hosted mode only: up to 15 more days per connected person,
                                        # newest first, toward the 90-day window (BODY-PRIMER-PULL-1)
    python -m body.pull --recompute     # only recompute readiness for every stored day

The daily hosted pull and --backfill share a non-blocking file lock under BODY_HOME (`pull.lock`)
so only one runs at a time; the other exits 0 quietly and the systemd timer tries again.
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from . import config, cuenta, db, garmin_adapter, garmin_cuenta, readiness

PAUSE_S = 1.0  # be gentle with Garmin's servers during a backfill
EMPTY_STOP = 60  # --all stops after this many consecutive days with no watch data
BACKFILL_DIAS = 90  # --backfill's window: the 90 days before today
BACKFILL_PASO = 15  # at most this many days fetched per person per --backfill run


def _lock_path() -> Path:
    return config.BODY_HOME / "pull.lock"


def _try_lock():
    """A non-blocking exclusive lock under BODY_HOME, taken by both the daily hosted pull and
    --backfill so only one Garmin pull runs at a time. Returns an open, locked file handle, or
    None if another pull already holds it. POSIX uses fcntl.flock; Windows (dev/test only --
    the host is Linux) uses msvcrt.locking as the equivalent."""
    config.ensure_home()
    f = open(_lock_path(), "a+b")
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _unlock(f) -> None:
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


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


def _pull_one_person(conn, person_id: int, days: list[str]) -> int | None:
    """Pulls `days` for one hosted person, re-encrypting the stored token only if Garmin refreshed
    it -- even when a later day raises, via the try/finally below. Returns the number of days
    actually stored, or None when the person has no token to pull with. Raises whatever the
    adapter or Garmin raise, with the days-saved-so-far attached as `.saved` on the exception so
    the caller can still count them; the caller records the error and moves on to the next person."""
    token = garmin_cuenta.leer(conn, person_id)
    if token is None:
        return None
    token_before = token
    saved = 0
    try:
        for i, d in enumerate(sorted(days)):
            if i:
                time.sleep(PAUSE_S)
            data, token = garmin_adapter.fetch_day(d, token_str=token)
            db.save_day(conn, data, person_id)
            saved += 1
    except Exception as e:
        e.saved = saved
        raise
    finally:
        if token != token_before:
            garmin_cuenta.guardar(conn, person_id, token)
    return saved


def pull_hosted(conn, days: list[str]) -> tuple[int, int, int]:
    """Loops over every person with a connected Garmin account. Returns (persons ok, persons
    failed, days stored). Never prints a health figure -- counts only."""
    ok = failed = saved = 0
    for person_id in garmin_cuenta.every_person_id(conn):
        try:
            n = _pull_one_person(conn, person_id, days)
        except Exception as e:
            garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
            failed += 1
            saved += getattr(e, "saved", 0)
            continue
        if n is None:
            continue
        garmin_cuenta.limpiar_error(conn, person_id)
        saved += n
        ok += 1
    return ok, failed, saved


def _dias_pendientes_backfill(conn, person_id: int, hoy: date) -> list[str]:
    """Up to BACKFILL_PASO days still inside the 90-day window and not yet attempted by this
    person's backfill, newest first. [] once the marker has reached the window's start."""
    marcador = garmin_cuenta.marcador_backfill(conn, person_id)
    mas_reciente = date.fromisoformat(marcador) - timedelta(1) if marcador else hoy - timedelta(1)
    limite = hoy - timedelta(BACKFILL_DIAS)
    if mas_reciente < limite:
        return []
    dias = []
    d = mas_reciente
    while d >= limite and len(dias) < BACKFILL_PASO:
        dias.append(d.isoformat())
        d -= timedelta(1)
    return dias


def _backfill_one_person(conn, person_id: int, hoy: date) -> int | None:
    """Fetches this person's next backfill batch (at most BACKFILL_PASO days, newest first),
    advancing their backfill marker after each day so a day already tried -- with or without
    data -- is never asked again. Returns the number of days fetched, or None when the person has
    no token to pull with. Raises whatever the adapter or Garmin raise, with the days-fetched-so-
    far attached as `.saved`, the same contract `_pull_one_person` uses."""
    token = garmin_cuenta.leer(conn, person_id)
    if token is None:
        return None
    dias = _dias_pendientes_backfill(conn, person_id, hoy)
    if not dias:
        return 0
    token_before = token
    saved = 0
    try:
        for i, d in enumerate(dias):
            if i:
                time.sleep(PAUSE_S)
            data, token = garmin_adapter.fetch_day(d, token_str=token)
            db.save_day(conn, data, person_id)
            saved += 1
            garmin_cuenta.avanzar_backfill(conn, person_id, d)
    except Exception as e:
        e.saved = saved
        raise
    finally:
        if token != token_before:
            garmin_cuenta.guardar(conn, person_id, token)
    return saved


def pull_backfill_hosted(conn, hoy: date | None = None) -> tuple[int, int, int]:
    """Loops over every person with a connected Garmin account whose backfill marker has not yet
    reached the 90-day window's start, fetching up to BACKFILL_PASO more days each (newest first).
    A person already complete, or not connected, costs no Garmin call. Returns (persons ok,
    persons failed, days stored). Never prints a health figure -- counts only."""
    hoy = hoy or date.today()
    ok = failed = saved = 0
    for person_id in garmin_cuenta.every_person_id(conn):
        try:
            n = _backfill_one_person(conn, person_id, hoy)
        except Exception as e:
            garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
            failed += 1
            saved += getattr(e, "saved", 0)
            continue
        if n is None:
            continue
        garmin_cuenta.limpiar_error(conn, person_id)
        saved += n
        ok += 1
    return ok, failed, saved


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.pull")
    p.add_argument("--date", help="one day, YYYY-MM-DD")
    p.add_argument("--days", type=int, default=2, help="how many days back from today (default 2)")
    p.add_argument("--all", action="store_true", help="whole Garmin history (resumable; local mode only)")
    p.add_argument("--backfill", action="store_true",
                    help="hosted mode only: up to 15 more days per person, toward the 90-day window")
    p.add_argument("--recompute", action="store_true",
                    help="recompute readiness only, no Garmin calls (local mode only)")
    a = p.parse_args(argv)

    conn = db.connect()
    try:
        db.init(conn)
        return _run(conn, a)
    finally:
        conn.close()


def _run(conn, a) -> int:
    if cuenta.hosted():
        lock = _try_lock()
        if lock is None:
            print("another pull is already running; skipping this run.", flush=True)
            return 0
        try:
            if a.backfill:
                ok, failed, saved = pull_backfill_hosted(conn)
                print(f"backfill: {ok} ok, {failed} failed, {saved} days stored", flush=True)
                return 0
            days = [a.date] if a.date else [(date.today() - timedelta(n)).isoformat() for n in range(a.days)]
            ok, failed, saved = pull_hosted(conn, days)
            print(f"{ok} ok, {failed} failed, {saved} days stored", flush=True)
            return 0
        finally:
            _unlock(lock)

    if a.backfill:
        print("local mode has no --backfill; it has --all instead.", flush=True)
        return 2

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
