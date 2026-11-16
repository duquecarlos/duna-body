"""Pull Garmin data into the local database, then update Body's own estimates.

    python -m body.pull                 # local: yesterday and today; hosted: heals gaps (below)
    python -m body.pull --date 2026-11-05
    python -m body.pull --days 90       # pull the last N days (hosted: still heals gaps too; see below)
    python -m body.pull --all           # whole history: walk back from the oldest stored day until
                                        # 60 empty days in a row (resumable; skips days already stored)
    python -m body.pull --backfill      # hosted mode only: up to 15 more days per connected person,
                                        # newest first, toward the 90-day window (BODY-PRIMER-PULL-1)
    python -m body.pull --hoy           # hosted mode only: one sweep of today alone for every
                                        # connected person, inside their own 06:00-23:00 window
                                        # (BODY-SYNC-VIVO-TIMER-1); local mode has no --hoy
    python -m body.pull --recompute     # only recompute readiness for every stored day
    python -m body.pull --intradia --dias N  # intraday heart-rate/stress, last N days (local mode
                                        # only; default and max N: INTRADIA_ALCANCE_DIAS)

The daily hosted pull and --backfill share a file lock under BODY_HOME (`pull.lock`) so only one
runs at a time. --backfill takes it non-blocking and exits 0 quietly if the daily pull holds it --
the backfill timer runs every 10 minutes, so skipping one run is harmless. The daily hosted pull
runs once a day, so it WAITS for the lock (up to LOCK_ESPERA_S) if a backfill holds it, and only
after that bound exits 0 with the same skip line.

BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 1, amended by BODY-GARMIN-SYNC-CONSISTENTE-1-B: hosted
healing never depends on whether --days was given. The per-person day set in hosted mode is
ALWAYS the union of (a) today and the N-1 days before it (N = an explicit --days, else 2) and (b)
every day in the last garmin_cuenta.VENTANA_RELLENO days with no stored Garmin daily row
(pull_hosted's own `_dias_pendientes_relleno`), under the existing per-run cap
(MAX_DIAS_POR_CORRIDA). So `python -m body.pull --days 2` -- the hosted timer's own daily call,
deploy/host/body-pull.service -- heals exactly like the bare call; `--days 90` still pulls its 90
days. `main`'s own `--days` default is None so a bare `python -m body.pull` reaches this same path
with N=2. Local mode is unaffected (unchanged default of 2); --all and --backfill are unchanged.

On the implicit "today" sweep (no explicit --date), both the default/--days local pull and the
daily hosted pull also fetch any intraday days an active intraday experiment still needs
(experimentos.dias_intradia_necesarios), capped at INTRADIA_MAX_POR_CORRIDA per person per run --
BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1. A person with no active intraday experiment costs no
extra Garmin calls.

BODY-ENTRENO-DETALLE-1 ruling 5: once privacidad.guarda_detalle agrees for a person (local mode:
always; hosted: consentimiento-v4 only), the SAME implicit "today" sweep also widens its intraday
days to every day in reach, not only its experiments' own windows (_dias_intradia_necesarios_
todos), and fetches each still-missing activity's full detail (heart-rate curve, laps, zones)
through _pull_detalle_pendientes, capped at DETALLE_MAX_POR_CORRIDA activities per run. A person
who has not agreed, and has no active experiment, still costs no extra Garmin call.
"""
import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import (config, cuenta, db, errores, experimentos, fechas, garmin_adapter, garmin_cuenta, goals, privacidad,
               readiness, recuperacion, sueno_fc, turno_fc)

PAUSE_S = 1.0  # be gentle with Garmin's servers during a backfill
EMPTY_STOP = 60  # --all stops after this many consecutive days with no watch data
BACKFILL_DIAS = 90  # --backfill's window: the 90 days before today
BACKFILL_PASO = 15  # at most this many days fetched per person per --backfill run
LOCK_ESPERA_S = 1800  # the daily hosted pull's bound while it waits for a backfill holding the lock

# BODY-SYNC-VIVO-ESTADO-1 ruling 1's puede_intentar also takes a minimum-space-between-attempts
# guard, independent of garmin_cuenta's own backoff window. The daily pull (once a day) and
# --backfill (every 10 minutes) already never re-enter the same person fast enough for that to
# matter on their own -- what ruling 3 actually asks these two call sites to respect is the
# BACKOFF window alone, so both pass 0 here (puede_intentar's "ahora - last < 0 minutes" never
# trips): the parameter stays real for any future caller that does need it.
ESPACIO_MIN_ENTRE_INTENTOS = 0
LOCK_SONDEO_S = 5  # how often it retries the non-blocking lock while waiting

# BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 1: the daily hosted pull's own cap -- at most this many
# days fetched per person per run. Never trims in practice today: garmin_cuenta.VENTANA_RELLENO
# (14) plus today/yesterday already always fall inside it (today and yesterday are themselves
# inside that 14-day window), so the healing set never exceeds 14 candidates -- kept as a named,
# independent safety bound rather than derived from the window, so a future wider window still
# can't runaway a single run.
MAX_DIAS_POR_CORRIDA = 16

# Garmin's intraday retention, measured 2026-11-09 (BODY-INTRADIA-SPOTCHECK-1): about 130-140
# days -- a day not pulled before it crosses this line is lost for good. Kept conservative (the
# low end of the measured range) so the range helper's refusal below never lets a day that is
# actually already gone look like it was simply never asked for.
INTRADIA_ALCANCE_DIAS = 130

# BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1: at most this many intraday days fetched per
# person per scheduled-pull run (newest first, from experimentos.dias_intradia_necesarios) -- a
# new experiment's test mornings come first, and its baseline window fills in over a few runs
# rather than one big burst of Garmin calls.
INTRADIA_MAX_POR_CORRIDA = 14

# BODY-ENTRENO-DETALLE-1 ruling 4: at most this many activities' full detail (heart-rate curve,
# laps, zones) fetched per person per scheduled-pull run, newest day first -- a backlog of old
# activities fills in over several runs rather than one big burst of Garmin calls (three calls
# per activity, three times the per-call cost of the intraday fetch above).
DETALLE_MAX_POR_CORRIDA = 10

# BODY-ENTRENO-DETALLE-1 ruling 4: activities older than this many days are never fetched for
# full detail -- a conservative window well inside Garmin's own activity-detail retention (not
# measured the way intraday's ~130-140 days was, so kept narrower than INTRADIA_ALCANCE_DIAS).
DETALLE_ALCANCE_DIAS = 90

# BODY-SYNC-VIVO-2 ruling 1: the on-demand pull's own minimum space between attempts for the SAME
# person -- independent of garmin_cuenta's own backoff window (ESPACIO_MIN_ENTRE_INTENTOS above is
# 0, the scheduled pulls' own spacing; a live trigger is asked for far more often than once a day,
# so it needs a real floor of its own).
ESPACIO_MIN_HOY = 15

# BODY-SYNC-VIVO-2 ruling 4: no on-demand pull between 23:00 and 06:00 in the person's own zone --
# the watch's own overnight sync has very likely not landed yet and the person is very likely
# asleep. Checked against the person's own local clock, never the server's (BODY-PULL-RELOJ-1).
# BODY-SYNC-VIVO-TIMER-1 ruling 1 reuses the same window for the --hoy sweep's own per-person
# daytime check -- one "is it daytime for this person" rule, never two.
QUIETA_DESDE_HORA = 23
QUIETA_HASTA_HORA = 6

# BODY-SYNC-VIVO-2 ruling 1: person_ids with an on-demand (pull_hoy_ahora) or connect-triggered
# (pull_persona_ahora) pull in flight right now -- ONE shared in-process guard, enough for one web
# worker, so the two triggers never run concurrently for the same person. Lifted here from
# body/rutas/garmin.py's own BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 4b guard, which now reaches it
# as `pull._PULL_EN_CURSO` -- never a re-imported or re-bound copy (tests/test_guardia_parches.py's
# NoShimAliasGuardTest forbids a top-level `_PULL_EN_CURSO = pull._PULL_EN_CURSO` alias in a route
# module). Never persisted: a crash mid-pull leaves no stuck entry, the next trigger just tries
# again.
_PULL_EN_CURSO: set[int] = set()


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


def _registrar_reloj_si_hay(conn, person_id: int, token: str) -> None:
    """BODY-SYNC-VIVO-ESTADO-1 ruling 4: one read-only call for the watch's own last-contact
    instant, after the day fetches, for one person's pull -- a failure never raises (fetch_
    device_last_sync's own contract), so this never fails the caller's pull either. Re-encrypts
    the token only if Garmin refreshed it during this call; only writes reloj_sincronizo_en when
    a value actually parsed, leaving the last known value in place otherwise."""
    reloj, token_after = garmin_adapter.fetch_device_last_sync(token_str=token)
    if token_after != token:
        garmin_cuenta.guardar(conn, person_id, token_after)
    if reloj is not None:
        garmin_cuenta.registrar_reloj_sincronizo(conn, person_id, reloj)


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
        _registrar_reloj_si_hay(conn, person_id, token)
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


def _recalcular_recuperacion_seguro(conn, person_id: int) -> None:
    """BODY-RECUPERACION-1's post-pull step: fills `recuperacion_actividad` for this person's own
    activities not yet computed under the current model version -- idempotent, cheap on a re-run.
    A failure here is never a Garmin failure and never stops the pull for this or the next person
    (same safe-wrapper shape as `_cerrar_semanas_seguro`): caught, printed as a counts-only line
    with the exception TYPE, nothing else happens. Silent when there was nothing new, the same
    rule the weekly-check sweep follows (BODY-RECUPERACION-1-B)."""
    try:
        resultado = recuperacion.recalcular(conn, person_id)
        if resultado["nuevas"]:
            print(f"recuperacion: {resultado['nuevas']} filas nuevas", flush=True)
    except Exception as e:
        print(f"recuperacion failed: {errores.texto_seguro(e)}", flush=True)


def _recalcular_turno_fc_seguro(conn, person_id: int) -> None:
    """BODY-TURNO-FC-1's post-pull step: fills `turno_fc` for this person's own shifts not yet
    computed under the current model version -- idempotent, cheap on a re-run. A failure here is
    never a Garmin failure and never stops the pull for this or the next person (same safe-wrapper
    shape as `_recalcular_recuperacion_seguro`): caught, printed as a counts-only line with the
    exception TYPE, nothing else happens. Silent when there was nothing new, the same rule the
    recovery sweep follows."""
    try:
        resultado = turno_fc.recalcular(conn, person_id)
        if resultado["nuevas"]:
            print(f"turno_fc: {resultado['nuevas']} filas nuevas", flush=True)
    except Exception as e:
        print(f"turno_fc failed: {errores.texto_seguro(e)}", flush=True)


def _recalcular_sueno_fc_seguro(conn, person_id: int) -> None:
    """BODY-SUENO-FC-1's post-pull step: fills `sleep_hr_avg`/`sleep_hr_min` for this person's own
    nights not yet computed under the current model version -- idempotent, cheap on a re-run (same
    safe-wrapper shape as `_recalcular_turno_fc_seguro`): caught, printed as a counts-only line with
    the exception TYPE, nothing else happens. Silent when there was nothing new, the same rule the
    other two recalc sweeps follow."""
    try:
        resultado = sueno_fc.recalcular(conn, person_id)
        if resultado["nuevas"]:
            print(f"sueno_fc: {resultado['nuevas']} filas nuevas", flush=True)
    except Exception as e:
        print(f"sueno_fc failed: {errores.texto_seguro(e)}", flush=True)


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


def _dias_pendientes_relleno(conn, person_id: int, hoy: date, n: int = 2) -> list[str]:
    """BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 1, amended by -1-B: today and the `n - 1` days before
    it ALWAYS (n defaults to 2 -- today and yesterday, since Garmin finalises yesterday late;
    an explicit `--days N` passes n=N so that window still heals too), plus every other gap day in
    garmin_cuenta.dias_sin_datos' own VENTANA_RELLENO-day window (one query per person, never one
    per day), oldest first. A day already stored is not refetched unless it falls in the always-
    fetched window. Capped at MAX_DIAS_POR_CORRIDA: the always-fetched window never gives way (the
    cap trims the OLDEST gap days first, keeping the newest ones) -- never trims anything in
    practice for the default n=2, since VENTANA_RELLENO (14) already fits under the cap (16)."""
    obligatorios = {(hoy - timedelta(i)).isoformat() for i in range(n)}
    huecos = [d for d in garmin_cuenta.dias_sin_datos(conn, person_id, hoy) if d not in obligatorios]
    cupo = max(MAX_DIAS_POR_CORRIDA - len(obligatorios), 0)
    return sorted(obligatorios | set(huecos[-cupo:] if cupo else []))


def _registrar_intento_persona(conn, person_id: int, ahora: datetime, exc: Exception | None) -> None:
    """BODY-SYNC-VIVO-ESTADO-1 ruling 3: one garmin_cuenta.registrar_intento call per person per
    attempt -- `exc` is the exception the attempt raised, or None on success. Prints a counts/
    class-only line on failure (errores.texto_seguro_con_estado), never a message body, never a
    health figure."""
    estado = garmin_adapter.estado_http(exc) if exc is not None else None
    garmin_cuenta.registrar_intento(conn, person_id, ahora, ok=exc is None, estado_http=estado)
    if exc is not None:
        print(f"pull attempt failed: {errores.texto_seguro_con_estado(exc, estado)}", flush=True)


def _pull_persona(conn, person_id: int, days: list[str]) -> dict:
    """One person's pull-and-postprocess: `_pull_one_person` (which itself handles the token --
    read, refresh, re-encrypt), the error record/clear, and the three post-pull recalcs -- shared
    by `pull_hosted`'s own per-person loop and the "connecting starts a pull now" path (ruling 4b),
    the same per-person path, never a copy. Returns {"estado": "sin_token"|"error"|"ok", "saved":
    int} -- "sin_token" when the person has no token to pull with (never counted as a failure,
    same as before this refactor). Records exactly one attempt (BODY-SYNC-VIVO-ESTADO-1 ruling 3)
    -- never for "sin_token", which never reached Garmin at all."""
    ahora = datetime.now(timezone.utc)
    try:
        n = _pull_one_person(conn, person_id, days)
    except Exception as e:
        garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
        _registrar_intento_persona(conn, person_id, ahora, e)
        return {"estado": "error", "saved": getattr(e, "saved", 0)}
    if n is None:
        return {"estado": "sin_token", "saved": 0}
    garmin_cuenta.limpiar_error(conn, person_id)
    _registrar_intento_persona(conn, person_id, ahora, None)
    _recalcular_recuperacion_seguro(conn, person_id)
    _recalcular_turno_fc_seguro(conn, person_id)
    _recalcular_sueno_fc_seguro(conn, person_id)
    return {"estado": "ok", "saved": n}


def pull_persona_ahora(person_id: int) -> None:
    """BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 4b: pulls ONE person's healing set (ruling 1) right
    now -- the same per-person path `pull_hosted`'s own loop uses (`_pull_persona`), never a copy.
    Called from a FastAPI BackgroundTask after `/garmin/conectar` or `/garmin/codigo` stores a
    token, so the gap since the person's last data fills in the same run the connect itself
    triggers, instead of waiting for the next daily pull. Opens and closes its own connection (a
    background task runs after the request's own connection has already closed). Records the
    error exactly as the daily timer does; prints counts only, never a health figure."""
    conn = db.connect()
    try:
        db.ensure_ready(conn)
        hoy = fechas.hoy_local(db.person(conn, person_id))
        dias = _dias_pendientes_relleno(conn, person_id, hoy)
        resultado = _pull_persona(conn, person_id, dias)
        print(f"connect pull: {resultado['estado']}, {resultado['saved']} days stored", flush=True)
    finally:
        conn.close()


def _en_horas_quietas(ahora_local: datetime) -> bool:
    h = ahora_local.hour
    return h >= QUIETA_DESDE_HORA or h < QUIETA_HASTA_HORA


def _pull_hoy_persona(conn, person_id: int, ahora_local: datetime, ahora_utc: datetime) -> bool:
    """The fetch+record+recalc sequence for one person's "today" alone -- shared by
    `pull_hoy_ahora`'s own trigger and the --hoy sweep (`pull_hoy_todos`), never a copy: fetches
    exactly one day through `_pull_one_person` (the same fetch path, token re-encryption and watch-
    last-sync read the daily/connect pulls already use for one day), records the attempt
    (`garmin_cuenta.registrar_error`/`limpiar_error`/`_registrar_intento_persona`, the same shape
    every other pull uses), and on success recomputes only `sueno_fc` (BODY-SYNC-VIVO-2's own
    reasoning: the one post-pull derivation a live trigger could actually read right after this
    call returns). Never raises -- a Garmin failure is recorded, not propagated. Returns True on
    success, False on a Garmin failure."""
    dia = ahora_local.date().isoformat()
    try:
        _pull_one_person(conn, person_id, [dia])
    except Exception as e:
        garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
        _registrar_intento_persona(conn, person_id, ahora_utc, e)
        return False
    garmin_cuenta.limpiar_error(conn, person_id)
    _registrar_intento_persona(conn, person_id, ahora_utc, None)
    _recalcular_sueno_fc_seguro(conn, person_id)
    return True


def pull_hoy_todos(conn) -> tuple[int, int, int, int]:
    """BODY-SYNC-VIVO-TIMER-1 ruling 1: the --hoy sweep's own per-person loop, for every connected
    person (`garmin_cuenta.every_person_id`), sequential (PAUSE_S between people, the same
    politeness pause `_pull_one_person`'s own day loop uses) -- never concurrent, so a slow Garmin
    reply from one person never piles calls up for the next.

    Each person is skipped, with no Garmin call, when either is true:
      * it is NOT 06:00-23:00 in THEIR OWN zone right now (`_en_horas_quietas`, the exact window
        BODY-SYNC-VIVO-2 ruling 4 already uses for the on-demand trigger -- one daytime rule for
        both) -- counted as `fuera_horario`.
      * `garmin_cuenta.puede_intentar` is false -- either a recorded backoff has not expired, or
        fewer than ESPACIO_MIN_HOY minutes have passed since the last attempt OF EITHER KIND for
        this person (the SAME floor the on-demand trigger enforces on itself, and the SAME
        `ultimo_intento_en` column both write to -- so a person who was just pulled on demand is
        not immediately re-pulled by this sweep either, and vice versa; this is what keeps the
        combined daily ceiling at ESPACIO_MIN_HOY's own bound, the README's ruling 4 math) --
        counted as `en_espera`.

    Otherwise runs the shared `_pull_hoy_persona` fetch -- counted as `ok` or `fallidas`. Returns
    (ok, en_espera, fuera_horario, fallidas). Never prints a health figure -- counts only."""
    ok = en_espera = fuera_horario = fallidas = 0
    primero = True
    for person_id in garmin_cuenta.every_person_id(conn):
        if not primero:
            time.sleep(PAUSE_S)
        primero = False
        person = db.person(conn, person_id)
        tz = ZoneInfo(person.get("timezone") or "America/Bogota")
        ahora_local = datetime.now(tz)
        ahora_utc = datetime.now(timezone.utc)
        if _en_horas_quietas(ahora_local):
            fuera_horario += 1
            continue
        if not garmin_cuenta.puede_intentar(conn, person_id, ahora_utc, ESPACIO_MIN_HOY):
            en_espera += 1
            continue
        if _pull_hoy_persona(conn, person_id, ahora_local, ahora_utc):
            ok += 1
        else:
            fallidas += 1
    return ok, en_espera, fuera_horario, fallidas


def pull_hoy_ahora(person_id: int) -> str:
    """BODY-SYNC-VIVO-2 ruling 1: an on-demand pull of TODAY alone, in the person's own zone,
    triggered by Hoy's `GET /` or by a `lectura` tool whose answer is today's own figures (ruling
    2) -- hosted mode only; local mode keeps its own daily scheduled sync and this is a no-op here
    ("sin_cuenta": there is no hosted account to pull on demand for).

    Fetches exactly one day through `_pull_one_person` -- the SAME fetch path (the Garmin call,
    `db.save_day`, the token's own re-encryption if Garmin refreshed it) the daily/connect pulls
    already use for one day -- plus the watch's own last-sync read, which `_pull_one_person`'s own
    `finally` already does (`_registrar_reloj_si_hay`). NOT the healing set
    `_dias_pendientes_relleno` builds for the daily/connect pulls: a live trigger asks for today,
    once, never for the last VENTANA_RELLENO days of gaps.

    After a successful fetch, recomputes ONLY `sueno_fc` (the sleep-window heart rate): it is the
    one post-pull derivation that lands IN `daily_metric` for last night's own day row, so it is
    the one thing `leer_hoy`/`leer_estado_reloj`/Hoy's own card could actually read differently
    right after this call returns. `recuperacion` (per-activity recovery estimate) and `turno_fc`
    (shift heart rate) both write to their OWN tables, read only by the Día and Turnos screens --
    neither of which this trigger ever reaches (ruling 2 names the trigger sites) -- so recomputing
    them on every on-demand pull would cost real work for a card that never reads their result.

    Takes the shared per-person lock (`_PULL_EN_CURSO`) and `garmin_cuenta.puede_intentar`
    (ESPACIO_MIN_HOY minutes since the last attempt, and outside any recorded backoff) before ever
    calling Garmin, plus ruling 4's quiet hours (`_en_horas_quietas`, in the person's own zone).
    Opens and closes its own connection (a BackgroundTask, or the thread a `lectura` tool starts,
    runs after the request's own connection may already be closing). Never raises into the
    caller: a Garmin failure is recorded the same way the daily pull records one
    (`garmin_cuenta.registrar_error`/`registrar_intento`), and any OTHER unexpected failure is
    swallowed with a counts-only line -- this function always returns, never raises.

    Returns "iniciado" (a pull just ran, whatever its own outcome -- the caller only needs to know
    one was attempted), "en_curso" (already running for this person), "espera" (too soon, backed
    off, or inside quiet hours), or "sin_cuenta" (local mode, or no Garmin token for this person)."""
    if not cuenta.hosted():
        return "sin_cuenta"
    if person_id in _PULL_EN_CURSO:
        return "en_curso"
    conn = db.connect()
    try:
        db.ensure_ready(conn)
        if garmin_cuenta.leer(conn, person_id) is None:
            return "sin_cuenta"
        person = db.person(conn, person_id)
        tz = ZoneInfo(person.get("timezone") or "America/Bogota")
        ahora_local = datetime.now(tz)
        ahora_utc = datetime.now(timezone.utc)
        if _en_horas_quietas(ahora_local):
            return "espera"
        if not garmin_cuenta.puede_intentar(conn, person_id, ahora_utc, ESPACIO_MIN_HOY):
            return "espera"
        _PULL_EN_CURSO.add(person_id)
        try:
            _pull_hoy_persona(conn, person_id, ahora_local, ahora_utc)
            return "iniciado"
        finally:
            _PULL_EN_CURSO.discard(person_id)
    except Exception as e:
        print(f"pull_hoy_ahora: unexpected failure: {errores.texto_seguro(e)}", flush=True)
        return "iniciado"
    finally:
        conn.close()


def texto_estado_vivo(t, conn, person_id: int, tz: ZoneInfo, hoy_local: date) -> str | None:
    """BODY-SYNC-VIVO-2 ruling 3: the plain sentence Hoy's own «Tu reloj» live card and the
    assistant's `leer_estado_reloj` answer both show for the SAME state -- built here once, shared,
    so the two can never read differently for the same state. Three states, the first that applies
    wins:

      1. a pull for this person is running right now (`_PULL_EN_CURSO`, the one in-process guard
         `pull_hoy_ahora`/`pull_persona_ahora` both take) -> "syncing".
      2. the last recorded attempt failed with a backoff still ahead of it
         (`garmin_cuenta.estado_sync`'s own `reintentar_despues_de`, set only for a 429/5xx/
         connection-timeout failure -- `garmin_cuenta.registrar_intento`'s own ruling 2) -> names
         when it failed and when it tries again, both in the person's own zone.
      3. the watch's own last-contact instant is known (`reloj_sincronizo_en`) -> names when, with
         the day spelled out (the app's own abbreviated-month spelling, the same
         `comun._short_date`/`hoy.py`'s `_dia_mes` already use -- duplicated here rather than
         imported, since neither a route module nor this one may import the other for one helper)
         when it is not today.

    None of the three -> None (no card, no claim) -- a person never connected, or connected but
    never yet attempted and never yet watch-synced."""
    if person_id in _PULL_EN_CURSO:
        return t("reloj_vivo.sincronizando")
    estado = garmin_cuenta.estado_sync(conn, person_id)
    if estado["reintentar_despues_de"] is not None:
        hora = datetime.fromisoformat(estado["ultimo_intento_en"]).astimezone(tz).strftime("%H:%M")
        reintento = datetime.fromisoformat(estado["reintentar_despues_de"]).astimezone(tz).strftime("%H:%M")
        return t("reloj_vivo.fallo_reintento", hora=hora, reintento=reintento)
    if estado["reloj_sincronizo_en"] is not None:
        momento = datetime.fromisoformat(estado["reloj_sincronizo_en"]).astimezone(tz)
        if momento.date() == hoy_local:
            hora = momento.strftime("%H:%M")
        else:
            hora = f"{momento.day} {t('date.months')[momento.month - 1][:3]}, {momento.strftime('%H:%M')}"
        return t("reloj_vivo.ultimo_sync", hora=hora)
    return None


def pull_hosted(conn, *, fecha: str | None = None, n_days: int | None = None) -> tuple[int, int, int]:
    """Loops over every person with a connected Garmin account, pulling `fecha` alone when given
    (the same one explicit date for everyone, `--date`), or else -- `n_days` given or not -- the
    healing set `_dias_pendientes_relleno` builds (ruling 1, amended by -1-B): today and the
    `n_days - 1` days before it (n_days defaults to 2) ALWAYS, plus every gap day in the last
    VENTANA_RELLENO days, so an explicit `--days N` call heals exactly like the bare call instead
    of skipping it. Every day list is built IN THAT PERSON'S OWN ZONE (`fechas.hoy_local`,
    BODY-PULL-RELOJ-1) -- the hosted timer fires at a fixed
    server instant, so a server-date list would ask a person far from the server's zone for the
    wrong "today". Then closes weeks for every person who can see an active goal
    (`_cerrar_semanas_todas`) -- connected or not, pulled OK or failed today (BODY-SEGURIDAD-3-M).
    On the implicit "today" sweep (no explicit `fecha`), also fetches any intraday days an active
    intraday experiment still needs (`_pull_intradia_pendientes`,
    BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1) -- zero extra calls for a person with none.
    Returns (persons ok, persons failed, days stored). Never prints a health figure -- counts
    only."""
    ok = failed = saved = omitidos = 0
    for person_id in garmin_cuenta.every_person_id(conn):
        # BODY-SYNC-VIVO-ESTADO-1 ruling 3: a person still inside a backoff window is skipped
        # with no Garmin call at all -- never counted as failed, since nothing was attempted.
        if not garmin_cuenta.puede_intentar(conn, person_id, datetime.now(timezone.utc), ESPACIO_MIN_ENTRE_INTENTOS):
            omitidos += 1
            continue
        hoy = None
        if fecha is not None:
            days = [fecha]
        else:
            hoy = fechas.hoy_local(db.person(conn, person_id))
            days = _dias_pendientes_relleno(conn, person_id, hoy, n=n_days if n_days is not None else 2)
        resultado = _pull_persona(conn, person_id, days)
        saved += resultado["saved"]
        if resultado["estado"] == "sin_token":
            continue
        if resultado["estado"] == "error":
            failed += 1
            continue
        ok += 1
        # BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1: only on the implicit "today" sweep (no
        # explicit --date), the same scheduled-pull shape the daily hosted timer runs.
        if hoy is not None:
            token = garmin_cuenta.leer(conn, person_id)
            if token is not None:
                _pull_intradia_pendientes(conn, person_id, hoy, token_str=token)
                # Re-read: _pull_intradia_pendientes may just have refreshed and persisted this
                # same person's token (garmin_cuenta.guardar) -- this call must use that, not the
                # value read above.
                token = garmin_cuenta.leer(conn, person_id)
                _pull_detalle_pendientes(conn, person_id, hoy, token_str=token)
    if omitidos:
        print(f"omitido: {omitidos} personas en espera", flush=True)
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
        _registrar_reloj_si_hay(conn, person_id, token)
    return saved, oldest


def pull_backfill_hosted(conn, *, hoy: date | None = None) -> tuple[int, int, int]:
    """Loops over every person with a connected Garmin account whose backfill marker has not yet
    reached the 90-day window's start, fetching up to BACKFILL_PASO more days each (newest first).
    `hoy`, when given, is used for every person alike (tests only); left out, each person's own
    local today is used (`fechas.hoy_local`, BODY-PULL-RELOJ-1) -- no `date.today()` left here. A
    person already complete, or not connected, costs no Garmin call. A person whose last pull
    recorded an auth-type error (a rejected or expired token) is skipped, with no Garmin call and
    not counted as failed, for as long as that error has NO backoff of its own (BODY-SYNC-VIVO-
    ESTADO-1 ruling 3: a recorded error with no reintentar_despues_de is the "no backoff" bucket
    -- an auth failure, today's handling -- never auto-retried here, since reconnecting is the
    fix, not a timer) -- otherwise the backfill timer would retry a rejected token every 10
    minutes and risk Garmin rate-limiting the account. A person inside an EXPIRING backoff
    (429/5xx/connection-timeout) is skipped only until `garmin_cuenta.puede_intentar` says it has
    passed, then resumes automatically -- counted in `omitidos`, printed as 'omitido: N personas
    en espera' when any were. The daily pull still tries an auth-failed person once a day and
    clears the error on success, which lets the backfill resume; a reconnect (garmin_cuenta.
    guardar) also clears it. Also closes weeks for every person who can see an active goal
    (`_cerrar_semanas_todas`), same as the daily pull -- cheap next to the Garmin calls this loop
    already makes, and a day backfilled into the current or previous week can change that week's
    stored verdict (BODY-SEGURIDAD-3-M); a day backfilled further back than that re-closes that
    older week too (BODY-SEMANAS-TARDIAS-1), via the oldest day fetched this run for that person
    (`desde_por_persona`), on success or on a mid-run failure alike. Returns (persons ok, persons
    failed, days stored). Never prints a health figure -- counts only."""
    ok = failed = saved = omitidos = 0
    desde_por_persona: dict[int, date] = {}
    for person_id in garmin_cuenta.every_person_id(conn):
        estado = garmin_cuenta.estado_sync(conn, person_id)
        if estado["ultimo_error"] is not None and estado["reintentar_despues_de"] is None:
            continue
        if not garmin_cuenta.puede_intentar(conn, person_id, datetime.now(timezone.utc), ESPACIO_MIN_ENTRE_INTENTOS):
            omitidos += 1
            continue
        hoy_persona = hoy or fechas.hoy_local(db.person(conn, person_id))
        ahora = datetime.now(timezone.utc)
        try:
            result = _backfill_one_person(conn, person_id, hoy_persona)
        except Exception as e:
            garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
            _registrar_intento_persona(conn, person_id, ahora, e)
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
        if n > 0:  # n == 0 is "this person's backfill window is already complete" -- no Garmin
            # call was made (BACKFILL_PASO's loop always saves at least one day when it runs at
            # all), so there is no attempt to record.
            _registrar_intento_persona(conn, person_id, ahora, None)
        saved += n
        ok += 1
        _recalcular_recuperacion_seguro(conn, person_id)
        _recalcular_turno_fc_seguro(conn, person_id)
        _recalcular_sueno_fc_seguro(conn, person_id)
        if oldest is not None:
            desde_por_persona[person_id] = date.fromisoformat(oldest)
    if omitidos:
        print(f"omitido: {omitidos} personas en espera", flush=True)
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
    sets = tuple(s for s in data.sets if (s.weight_value is None) == (s.weight_unit is None))
    descartadas = len(data.sets) - len(sets)
    if descartadas:
        print(f"series descartadas: {descartadas}", file=sys.stderr)
    db.save_exercise_sets(conn, person_id, garmin_adapter.SOURCE, str(activity_source_id), sets)
    if token_str is not None:
        return len(sets), token_after
    return len(sets)


def traer_intradia(conn, person_id: int, day: str, token_str: str | None = None, *, forzar: bool = False):
    """Pulls one day's intraday heart-rate/stress series through the adapter only, and stores
    both kinds (db.guardar_intradia, INSERT OR REPLACE). Skips the Garmin call entirely when the
    day is already stored AND COMPLETE (db.intradia_almacenado -- fetched at least
    db.INTRADIA_MARGEN_CIERRE_H hours after the day's local close), unless `forzar=True`. A day
    stored before its close is re-fetched and its earlier, partial row is overwritten, exactly
    like a day never fetched at all -- BODY-MOMENTOS-DIA-COMPLETO-1: before this, the watch's own
    sync lag could leave a day's last hours missing forever. Returns "already_stored", "empty"
    (fetched, but out of Garmin's retention window -- both an honest gap and the empty marker
    that keeps it from being re-fetched once it is trusted), or "fetched" -- a re-fetch of a
    previously partial day counts as "fetched", never "already_stored"; with `token_str` given,
    returns (that status, token_after), the same dual-return shape fetch_intraday/traer_series
    use. On demand only -- not called by pull_all, pull_hosted or pull_backfill_hosted."""
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


def _dias_intradia_necesarios_todos(conn, person_id: int, hoy: date) -> list[str]:
    """BODY-ENTRENO-DETALLE-1 ruling 5: the full set of intraday days a scheduled pull should
    fetch this run, newest first -- experimentos.dias_intradia_necesarios' own experiment days
    UNION, only when privacidad.guarda_detalle agrees for this person, every day from
    `hoy - INTRADIA_ALCANCE_DIAS` to yesterday not yet stored complete (db.intradia_almacenado).
    A person who has not agreed (hosted, no consentimiento-v4) gets exactly the experiment days,
    unchanged -- Carlos's own local machine (guarda_detalle always true there, cuenta.hosted() is
    False) gets every day in reach, not only its experiments' own test/baseline windows. Lives in
    pull.py, not experimentos.py (ruling 5's "wherever reads more naturally"): it needs pull's
    own INTRADIA_ALCANCE_DIAS and privacidad, and its only caller, _pull_intradia_pendientes,
    already lives here. Capping (INTRADIA_MAX_POR_CORRIDA) stays in that caller -- this only
    decides WHICH days are candidates."""
    dias = set(experimentos.dias_intradia_necesarios(conn, person_id, hoy))
    if privacidad.guarda_detalle(conn, person_id):
        limite = hoy - timedelta(days=INTRADIA_ALCANCE_DIAS)
        d = hoy - timedelta(1)
        while d >= limite:
            ds = d.isoformat()
            if ds not in dias and not db.intradia_almacenado(conn, person_id, ds):
                dias.add(ds)
            d -= timedelta(1)
    return sorted(dias, reverse=True)


def _pull_intradia_pendientes(conn, person_id: int, hoy: date, token_str: str | None = None) -> int:
    """BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1: fetches up to INTRADIA_MAX_POR_CORRIDA of this
    person's missing intraday days (_dias_intradia_necesarios_todos, newest first -- the active
    experiments' own days, plus, once BODY-ENTRENO-DETALLE-1's guarda_detalle agrees, every day
    in reach) through the same on-demand traer_intradia this module already has -- a person with
    no active experiment AND no agreement makes no Garmin call. A failure on one day is logged
    (counts and dates only, errores.texto_seguro) and does not stop the others or fail the run.
    With token_str given (hosted), persists the refreshed token (garmin_cuenta.guardar) if
    traer_intradia changed it, the same pattern _pull_one_person uses. Returns the number of days
    actually fetched this run; prints 'intradia: N días traídos, M pendientes' (counts and dates
    only) when at least one day was needed, M counting every day still missing after this run
    (capped-out or errored alike)."""
    pendientes = _dias_intradia_necesarios_todos(conn, person_id, hoy)
    if not pendientes:
        return 0
    dias = pendientes[:INTRADIA_MAX_POR_CORRIDA]
    token = token_str
    token_before = token
    fetched = 0
    for d in dias:
        try:
            if token is not None:
                _, token = traer_intradia(conn, person_id, d, token_str=token)
            else:
                traer_intradia(conn, person_id, d)
            fetched += 1
        except Exception as e:
            print(f"intradia: fetch failed for {d}: {errores.texto_seguro(e)}", flush=True)
    if token_str is not None and token != token_before:
        garmin_cuenta.guardar(conn, person_id, token)
    print(f"intradia: {fetched} días traídos, {len(pendientes) - fetched} pendientes", flush=True)
    return fetched


def traer_detalle_actividad(conn, person_id: int, source_id: str, token_str: str | None = None):
    """Pulls one activity's full detail through the adapter only (garmin_adapter.fetch_activity_
    detail) and stores each kind that call actually returned (db.guardar_detalle_actividad) --
    'detalle'/'vueltas'/'zonas', matching ActivityDetailData.raw's own keys. A kind whose own
    Garmin call failed is simply absent from raw and is not stored this run (left for a later
    run's db.actividades_sin_detalle to pick up again), the same `_errors` pattern fetch_intraday
    already uses -- one failing call never loses the other two. Returns the number of kinds
    stored (0-3), or (that count, token_after) when token_str is given (hosted, per-person token
    path, the same dual-return shape fetch_activity_detail/traer_series use."""
    if token_str is not None:
        data, token_after = garmin_adapter.fetch_activity_detail(source_id, token_str=token_str)
    else:
        data = garmin_adapter.fetch_activity_detail(source_id)
    stored = 0
    for kind in db.DETALLE_KINDS:
        if kind in data.raw:
            db.guardar_detalle_actividad(conn, person_id, garmin_adapter.SOURCE, source_id, kind, data.raw[kind])
            stored += 1
    if token_str is not None:
        return stored, token_after
    return stored


def _pull_detalle_pendientes(conn, person_id: int, hoy: date, token_str: str | None = None) -> int:
    """BODY-ENTRENO-DETALLE-1 ruling 4: fetches up to DETALLE_MAX_POR_CORRIDA of this person's
    activities still missing full detail (db.actividades_sin_detalle, newest day first) through
    traer_detalle_actividad -- but only when privacidad.guarda_detalle agrees for this person
    (local mode: always; hosted: consentimiento-v4 only). A person who has not agreed makes NO
    Garmin call here at all, and this returns 0 immediately. A failure on one activity is logged
    (counts only, errores.texto_seguro) and does not stop the others or fail the run. With
    token_str given (hosted), persists the refreshed token (garmin_cuenta.guardar) if traer_
    detalle_actividad changed it, the same pattern _pull_intradia_pendientes uses. Returns the
    number of activities actually fetched this run; prints 'detalle: N actividades traídas, M
    pendientes' (counts only) when at least one activity was pending."""
    if not privacidad.guarda_detalle(conn, person_id):
        return 0
    desde = (hoy - timedelta(days=DETALLE_ALCANCE_DIAS)).isoformat()
    pendientes = db.actividades_sin_detalle(conn, person_id, desde)
    if not pendientes:
        return 0
    actividades = pendientes[:DETALLE_MAX_POR_CORRIDA]
    token = token_str
    token_before = token
    fetched = 0
    for source_id in actividades:
        try:
            if token is not None:
                _, token = traer_detalle_actividad(conn, person_id, source_id, token_str=token)
            else:
                traer_detalle_actividad(conn, person_id, source_id)
            fetched += 1
        except Exception as e:
            print(f"detalle: fetch failed for one activity: {errores.texto_seguro(e)}", flush=True)
    if token_str is not None and token != token_before:
        garmin_cuenta.guardar(conn, person_id, token)
    print(f"detalle: {fetched} actividades traídas, {len(pendientes) - fetched} pendientes", flush=True)
    return fetched


def traer_intradia_rango(conn, person_id: int, desde: date, hasta: date, pausa_s: float = 1.0) -> tuple[int, int, int]:
    """Pulls traer_intradia for every day from `desde` to `hasta` (inclusive), oldest first, with a
    polite pause between Garmin calls. Stops cleanly -- returning the counts gathered so far -- on
    a sign-in error; a single day's own empty/error result is counted and the loop continues.
    Local person only (no token_str): the on-demand / explicit-backfill path this slice adds, not
    the hosted per-person loop. A day re-fetched because it was stored but not yet complete
    (BODY-MOMENTOS-DIA-COMPLETO-1) counts as fetched, not already stored -- traer_intradia's own
    return already makes that distinction. Prints counts and dates only. Returns (days fetched,
    days already stored, days empty)."""
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
    p.add_argument("--days", type=int, default=None,
                    help="how many days back from today; hosted still heals gaps (ruling 1) on top "
                         "of this window either way -- local keeps its own default of 2 when left out")
    p.add_argument("--all", action="store_true", help="whole Garmin history (resumable; local mode only)")
    p.add_argument("--backfill", action="store_true",
                    help="hosted mode only: up to 15 more days per person, toward the 90-day window")
    p.add_argument("--hoy", action="store_true",
                    help="hosted mode only: one sweep of today alone for every connected person, "
                         "inside their own 06:00-23:00 window (BODY-SYNC-VIVO-TIMER-1)")
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
        lock = _try_lock() if (a.backfill or a.hoy) else _esperar_lock(LOCK_ESPERA_S)
        if lock is None:
            print("another pull is already running; skipping this run.", flush=True)
            return 0
        try:
            if a.backfill:
                ok, failed, saved = pull_backfill_hosted(conn)
                print(f"backfill: {ok} ok, {failed} failed, {saved} days stored", flush=True)
                return 0
            if a.hoy:
                ok, espera, fuera, fallidas = pull_hoy_todos(conn)
                print(f"hoy: {ok} ok, {espera} en espera, {fuera} fuera de horario, {fallidas} fallidas",
                      flush=True)
                return 0
            ok, failed, saved = pull_hosted(conn, fecha=a.date, n_days=a.days)
            print(f"{ok} ok, {failed} failed, {saved} days stored", flush=True)
            return 0
        finally:
            _unlock(lock)

    if a.backfill:
        print("local mode has no --backfill; it has --all instead.", flush=True)
        return 2
    if a.hoy:
        print("local mode has no --hoy; it keeps its own daily scheduled sync instead.", flush=True)
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
        # serving several zones) -- BODY-PULL-RELOJ-1 leaves this date.today() as is. Ruling 1's
        # healing default is hosted-only; local keeps its own plain default of 2 days when --days
        # is left out (a.days is None).
        dias_atras = a.days if a.days is not None else 2
        days = [a.date] if a.date else [(date.today() - timedelta(n)).isoformat() for n in range(dias_atras)]
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
        if not a.date:
            # BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1: only on the implicit "today" sweep (no
            # explicit --date), the same shape the morning `manana` run and the plain scheduled
            # daily pull both use.
            _pull_intradia_pendientes(conn, person_id, date.today())
            _pull_detalle_pendientes(conn, person_id, date.today())
    # Baselines depend on earlier days, so recompute every stored day in order (cheap, local).
    n = readiness.recompute_all(conn, person_id)
    print(f"Readiness updated for {n} days.")
    # Through the same safe wrapper the hosted path uses (BODY-SEGURIDAD-3-M): a failure here
    # prints a type-only line and the sync still exits 0, instead of failing the whole run. Runs
    # after every local path that stores or recomputes data -- --all, --recompute, and the
    # default/--days/--date pull all reach this line.
    _cerrar_semanas_seguro(conn, person_id, desde=desde)
    _recalcular_recuperacion_seguro(conn, person_id)
    _recalcular_turno_fc_seguro(conn, person_id)
    _recalcular_sueno_fc_seguro(conn, person_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
