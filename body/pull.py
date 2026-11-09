"""Pull Garmin data into the local database, then update Body's own estimates.

    python -m body.pull                 # yesterday and today
    python -m body.pull --date 2026-11-05
    python -m body.pull --days 90       # pull the last N days
    python -m body.pull --all           # whole history: walk back from the oldest stored day until
                                        # 60 empty days in a row (resumable; skips days already stored)
    python -m body.pull --backfill      # hosted mode only: up to 15 more days per connected person,
                                        # newest first, toward the 90-day window (BODY-PRIMER-PULL-1)
    python -m body.pull --recompute     # only recompute readiness for every stored day
    python -m body.pull --intradia --dias N  # intraday heart-rate/stress, last N days (local mode
                                        # only; default and max N: INTRADIA_ALCANCE_DIAS)

The daily hosted pull and --backfill share a file lock under BODY_HOME (`pull.lock`) so only one
runs at a time. --backfill takes it non-blocking and exits 0 quietly if the daily pull holds it --
the backfill timer runs every 10 minutes, so skipping one run is harmless. The daily hosted pull
runs once a day, so it WAITS for the lock (up to LOCK_ESPERA_S) if a backfill holds it, and only
after that bound exits 0 with the same skip line.
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from . import config, cuenta, db, errores, fechas, garmin_adapter, garmin_cuenta, goals, readiness

PAUSE_S = 1.0  # be gentle with Garmin's servers during a backfill
EMPTY_STOP = 60  # --all stops after this many consecutive days with no watch data
BACKFILL_DIAS = 90  # --backfill's window: the 90 days before today
BACKFILL_PASO = 15  # at most this many days fetched per person per --backfill run
LOCK_ESPERA_S = 1800  # the daily hosted pull's bound while it waits for a backfill holding the lock
LOCK_SONDEO_S = 5  # how often it retries the non-blocking lock while waiting

# Garmin's intraday retention, measured 2026-11-09 (BODY-INTRADIA-SPOTCHECK-1): about 130-140
# days -- a day not pulled before it crosses this line is lost for good. Kept conservative (the
# low end of the measured range) so the range helper's refusal below never lets a day that is
# actually already gone look like it was simply never asked for.
INTRADIA_ALCANCE_DIAS = 130


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


def _esperar_lock(max_s: float):
    """Polls _try_lock() every LOCK_SONDEO_S seconds until it succeeds or max_s has elapsed.
    Used by the daily hosted pull, which runs once a day (OnCalendar 09:30) and must not lose
    that day's run to a backfill holding the lock; --backfill keeps the plain non-blocking
    _try_lock(), since it runs every 10 minutes and skipping one run is harmless."""
    deadline = time.monotonic() + max_s
    while True:
        lock = _try_lock()
        if lock is not None:
            return lock
        if time.monotonic() >= deadline:
            return None
        time.sleep(LOCK_SONDEO_S)


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


def pull_all(conn, person_id: int) -> tuple[int, date | None]:
    """Walk back day by day until EMPTY_STOP empty days in a row. Returns (days saved, oldest day
    saved this run or None) -- the oldest feeds `_cerrar_semanas_seguro`'s `desde`
    (BODY-SEMANAS-TARDIAS-1), so a day restored from further back than last week still re-closes
    its own goal_check row. `date.today()` here is the PC's own date -- local mode only, this is
    the person's own machine, not a business-boundary clock (BODY-PULL-RELOJ-1 leaves it)."""
    stored = _stored(conn, person_id)
    d = date.fromisoformat(min(stored)) if stored else date.today()
    empty = saved = 0
    oldest = None
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
                return saved, oldest
        if data.metrics or data.activities:
            db.save_day(conn, data, person_id)
            saved += 1
            oldest = d
            empty = 0
        else:
            empty += 1
        if saved and saved % 30 == 0 and empty == 0:
            print(f"... back to {key}, {saved} days saved", flush=True)
    print(f"History start reached around {(d + timedelta(EMPTY_STOP)).isoformat()} ({saved} days saved).")
    return saved, oldest


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


def _cerrar_semanas_seguro(conn, person_id: int, *, desde: date | None = None) -> None:
    """The one writer of goal_check (BODY-SEGURIDAD-3): closes this person's current and previous
    week, and -- when `desde` is given (BODY-SEMANAS-TARDIAS-1) -- every older week back to
    `desde` too. A failure here is never a Garmin failure and never stops the next person --
    caught, printed as a counts-only line with the exception TYPE (body/errores.py's
    texto_seguro), nothing else happens."""
    try:
        hoy = fechas.hoy_local(db.person(conn, person_id))
        n = goals.cerrar_semanas(conn, person_id, hoy=hoy, desde=desde)
        print(f"weekly checks: {n} rows", flush=True)
    except Exception as e:
        print(f"weekly checks failed: {errores.texto_seguro(e)}", flush=True)


def _cerrar_semanas_todas(conn, desde_por_persona: dict[int, date] | None = None) -> None:
    """Closes weeks, once each, for EVERY person who can see an active goal (own or shared
    household milestone, `goals.personas_con_meta_activa`) -- connected to Garmin or not, pulled
    OK or failed today (BODY-SEGURIDAD-3-M). Run once after the Garmin loop in both the daily
    hosted pull and --backfill, replacing the per-connected-person call that used to sit inside
    that loop: the person list comes from the goal table, never from
    `garmin_cuenta.every_person_id`, so a habit goal's own member who has no Garmin connection at
    all still gets their weekly checks stored. `desde_por_persona` (BODY-SEMANAS-TARDIAS-1): the
    oldest day stored this run for a person who has one, so that person's older weeks re-close
    too; a person absent from the mapping gets the plain current/previous-week close."""
    desde_por_persona = desde_por_persona or {}
    for person_id in goals.personas_con_meta_activa(conn):
        _cerrar_semanas_seguro(conn, person_id, desde=desde_por_persona.get(person_id))


def pull_hosted(conn, *, fecha: str | None = None, n_days: int = 2) -> tuple[int, int, int]:
    """Loops over every person with a connected Garmin account, pulling `fecha` alone when given
    (the same one explicit date for everyone, `--date`), or else today and the `n_days - 1` days
    before it IN THAT PERSON'S OWN ZONE (`fechas.hoy_local`, BODY-PULL-RELOJ-1) -- the hosted timer
    fires at a fixed server instant, so a server-date list would ask a person far from the
    server's zone for the wrong "today". Then closes weeks for every person who can see an active
    goal (`_cerrar_semanas_todas`) -- connected or not, pulled OK or failed today
    (BODY-SEGURIDAD-3-M). Returns (persons ok, persons failed, days stored). Never prints a health
    figure -- counts only."""
    ok = failed = saved = 0
    for person_id in garmin_cuenta.every_person_id(conn):
        if fecha is not None:
            days = [fecha]
        else:
            hoy = fechas.hoy_local(db.person(conn, person_id))
            days = [(hoy - timedelta(n)).isoformat() for n in range(n_days)]
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
    _cerrar_semanas_todas(conn)
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


def _backfill_one_person(conn, person_id: int, hoy: date) -> tuple[int, date | None] | None:
    """Fetches this person's next backfill batch (at most BACKFILL_PASO days, newest first),
    advancing their backfill marker after each day so a day already tried -- with or without
    data -- is never asked again. Returns (days fetched, oldest day fetched this run or None), or
    None when the person has no token to pull with. Raises whatever the adapter or Garmin raise,
    with the days-fetched-so-far attached as `.saved` and the oldest of those as `.oldest`
    (BODY-SEMANAS-TARDIAS-1), the same `.saved` contract `_pull_one_person` uses."""
    token = garmin_cuenta.leer(conn, person_id)
    if token is None:
        return None
    dias = _dias_pendientes_backfill(conn, person_id, hoy)
    if not dias:
        return 0, None
    token_before = token
    saved = 0
    oldest = None
    try:
        for i, d in enumerate(dias):
            if i:
                time.sleep(PAUSE_S)
            data, token = garmin_adapter.fetch_day(d, token_str=token)
            db.save_day(conn, data, person_id)
            saved += 1
            oldest = d
            garmin_cuenta.avanzar_backfill(conn, person_id, d)
    except Exception as e:
        e.saved = saved
        e.oldest = oldest
        raise
    finally:
        if token != token_before:
            garmin_cuenta.guardar(conn, person_id, token)
    return saved, oldest


def pull_backfill_hosted(conn, *, hoy: date | None = None) -> tuple[int, int, int]:
    """Loops over every person with a connected Garmin account whose backfill marker has not yet
    reached the 90-day window's start, fetching up to BACKFILL_PASO more days each (newest first).
    `hoy`, when given, is used for every person alike (tests only); left out, each person's own
    local today is used (`fechas.hoy_local`, BODY-PULL-RELOJ-1) -- no `date.today()` left here. A
    person already complete, or not connected, costs no Garmin call. A person whose last pull
    recorded an error (a rejected or expired token, Garmin down, anything) is also skipped, with
    no Garmin call and not counted as failed -- otherwise the backfill timer would retry a
    rejected token every 10 minutes and risk Garmin rate-limiting the account. The daily pull
    still tries them once a day and clears the error on success, which lets the backfill resume;
    a reconnect (garmin_cuenta.guardar) also clears it. Also closes weeks for every person who can
    see an active goal (`_cerrar_semanas_todas`), same as the daily pull -- cheap next to the
    Garmin calls this loop already makes, and a day backfilled into the current or previous week
    can change that week's stored verdict (BODY-SEGURIDAD-3-M); a day backfilled further back than
    that re-closes that older week too (BODY-SEMANAS-TARDIAS-1), via the oldest day fetched this
    run for that person (`desde_por_persona`), on success or on a mid-run failure alike. Returns
    (persons ok, persons failed, days stored). Never prints a health figure -- counts only."""
    ok = failed = saved = 0
    desde_por_persona: dict[int, date] = {}
    for person_id in garmin_cuenta.every_person_id(conn):
        if garmin_cuenta.ultimo_error(conn, person_id) is not None:
            continue
        hoy_persona = hoy or fechas.hoy_local(db.person(conn, person_id))
        try:
            result = _backfill_one_person(conn, person_id, hoy_persona)
        except Exception as e:
            garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
            failed += 1
            saved += getattr(e, "saved", 0)
            oldest = getattr(e, "oldest", None)
            if oldest is not None:
                desde_por_persona[person_id] = date.fromisoformat(oldest)
            continue
        if result is None:
            continue
        n, oldest = result
        garmin_cuenta.limpiar_error(conn, person_id)
        saved += n
        ok += 1
        if oldest is not None:
            desde_por_persona[person_id] = date.fromisoformat(oldest)
    _cerrar_semanas_todas(conn, desde_por_persona)
    return ok, failed, saved


def traer_series(conn, person_id: int, activity_source_id: str, token_str: str | None = None):
    """Pulls one activity's per-set detail through the adapter (`garmin_adapter.fetch_exercise_
    sets` -- the only Garmin access here) and stores it (`db.save_exercise_sets`). Returns the
    number of sets saved, or `(sets saved, token_after)` when `token_str` is given (hosted,
    per-person token path -- the same dual-return shape `garmin_adapter.fetch_exercise_sets`
    itself uses). NOT called by `pull_all`, `pull_hosted`, `pull_backfill_hosted` or `main` --
    this runs only on demand, when a person logs or corrects a session (the assistant tools,
    BODY-ENTRENO-1-INV slice 4)."""
    if token_str is not None:
        data, token_after = garmin_adapter.fetch_exercise_sets(activity_source_id, token_str=token_str)
    else:
        data = garmin_adapter.fetch_exercise_sets(activity_source_id)
    db.save_exercise_sets(conn, person_id, garmin_adapter.SOURCE, str(activity_source_id), data.sets)
    if token_str is not None:
        return len(data.sets), token_after
    return len(data.sets)


def traer_intradia(conn, person_id: int, day: str, token_str: str | None = None, *, forzar: bool = False):
    """Pulls one day's intraday heart-rate/stress series through the adapter only, and stores
    both kinds (db.guardar_intradia). Skips the Garmin call entirely when the day is already
    stored (db.intradia_almacenado), unless `forzar=True`. Returns "already_stored", "empty"
    (fetched, but out of Garmin's retention window -- both an honest gap and the empty marker
    that keeps it from being re-fetched), or "fetched"; with `token_str` given, returns
    (that status, token_after), the same dual-return shape fetch_intraday/traer_series use.
    On demand only -- not called by pull_all, pull_hosted or pull_backfill_hosted."""
    if not forzar and db.intradia_almacenado(conn, person_id, day):
        return ("already_stored", token_str) if token_str is not None else "already_stored"
    zona = db.person(conn, person_id)["timezone"]
    if token_str is not None:
        data, token_after = garmin_adapter.fetch_intraday(day, zona, token_str=token_str)
    else:
        data = garmin_adapter.fetch_intraday(day, zona)
    db.guardar_intradia(conn, person_id, day, "intradia_fc", data.raw.get("heart_rate"))
    db.guardar_intradia(conn, person_id, day, "intradia_estres", data.raw.get("stress"))
    estado = "fetched" if (data.heart_rate or data.stress) else "empty"
    return (estado, token_after) if token_str is not None else estado


def traer_intradia_rango(conn, person_id: int, desde: date, hasta: date, pausa_s: float = 1.0) -> tuple[int, int, int]:
    """Pulls traer_intradia for every day from `desde` to `hasta` (inclusive), oldest first, with a
    polite pause between Garmin calls. Stops cleanly -- returning the counts gathered so far -- on
    a sign-in error; a single day's own empty/error result is counted and the loop continues.
    Local person only (no token_str): the on-demand / explicit-backfill path this slice adds, not
    the hosted per-person loop. Prints counts and dates only. Returns (days fetched, days already
    stored, days empty)."""
    limite = date.today() - timedelta(days=INTRADIA_ALCANCE_DIAS)
    if desde < limite:
        raise ValueError(
            f"desde {desde.isoformat()} is older than Garmin's intraday reach "
            f"(~{INTRADIA_ALCANCE_DIAS} days back, to {limite.isoformat()}); that data is already gone."
        )
    fetched = stored = empty = 0
    d = desde
    primero = True
    while d <= hasta:
        if not primero:
            time.sleep(pausa_s)
        primero = False
        try:
            estado = traer_intradia(conn, person_id, d.isoformat())
        except garmin_adapter.NotSignedIn as e:
            print(f"intraday stopped at {d.isoformat()}: {type(e).__name__}", flush=True)
            break
        if estado == "already_stored":
            stored += 1
        elif estado == "empty":
            empty += 1
        else:
            fetched += 1
        d += timedelta(1)
    print(f"intraday {desde.isoformat()}..{hasta.isoformat()}: "
          f"{fetched} fetched, {stored} already stored, {empty} empty", flush=True)
    return fetched, stored, empty


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.pull")
    p.add_argument("--date", help="one day, YYYY-MM-DD")
    p.add_argument("--days", type=int, default=2, help="how many days back from today (default 2)")
    p.add_argument("--all", action="store_true", help="whole Garmin history (resumable; local mode only)")
    p.add_argument("--backfill", action="store_true",
                    help="hosted mode only: up to 15 more days per person, toward the 90-day window")
    p.add_argument("--recompute", action="store_true",
                    help="recompute readiness only, no Garmin calls (local mode only)")
    p.add_argument("--intradia", action="store_true",
                    help="pull intraday heart-rate/stress for the last --dias days (local mode only)")
    p.add_argument("--dias", type=int, default=INTRADIA_ALCANCE_DIAS,
                    help=f"days back for --intradia (default and max {INTRADIA_ALCANCE_DIAS})")
    a = p.parse_args(argv)

    conn = db.connect()
    try:
        db.init(conn)
        return _run(conn, a)
    finally:
        conn.close()


def _run(conn, a) -> int:
    if a.intradia:
        if cuenta.hosted():
            print("hosted: intraday pull is local-only for now.")
            return 1
        dias = min(max(a.dias, 1), INTRADIA_ALCANCE_DIAS)
        person_id = db.local_person_id(conn)
        hoy = date.today()
        traer_intradia_rango(conn, person_id, hoy - timedelta(dias - 1), hoy)
        return 0

    if cuenta.hosted():
        lock = _try_lock() if a.backfill else _esperar_lock(LOCK_ESPERA_S)
        if lock is None:
            print("another pull is already running; skipping this run.", flush=True)
            return 0
        try:
            if a.backfill:
                ok, failed, saved = pull_backfill_hosted(conn)
                print(f"backfill: {ok} ok, {failed} failed, {saved} days stored", flush=True)
                return 0
            ok, failed, saved = pull_hosted(conn, fecha=a.date, n_days=a.days)
            print(f"{ok} ok, {failed} failed, {saved} days stored", flush=True)
            return 0
        finally:
            _unlock(lock)

    if a.backfill:
        print("local mode has no --backfill; it has --all instead.", flush=True)
        return 2

    person_id = db.local_person_id(conn)
    desde = None  # BODY-SEMANAS-TARDIAS-1: set below only by --all, which can restore days older
                  # than last week; --days/--date/--recompute never reach further back than that.
    if a.all:
        try:
            _, desde = pull_all(conn, person_id)
        except garmin_adapter.NotSignedIn as e:
            print(e)
            return 2
    elif not a.recompute:
        # Local mode keeps the PC's own date here (it is the person's own machine, not a server
        # serving several zones) -- BODY-PULL-RELOJ-1 leaves this date.today() as is.
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
    # Through the same safe wrapper the hosted path uses (BODY-SEGURIDAD-3-M): a failure here
    # prints a type-only line and the sync still exits 0, instead of failing the whole run. Runs
    # after every local path that stores or recomputes data -- --all, --recompute, and the
    # default/--days/--date pull all reach this line.
    _cerrar_semanas_seguro(conn, person_id, desde=desde)
    return 0


if __name__ == "__main__":
    sys.exit(main())
