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

from . import (config, cuenta, db, entreno_sesion, errores, experimentos, fechas, garmin_adapter, garmin_cuenta,
               goals, lugares, privacidad, readiness, recuperacion, sueno_fc, turno_fc)

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

# BODY-GARMIN-DIA-VACIO-1 ruling 5: garmin_adapter.fetch_day (Tier 1, not edited from here) always
# attempts exactly this many feed calls (its own `calls` dict) and catches each one separately
# into raw['_errors'] -- a fetched day whose '_errors' names every one of these is a request
# failure (the watch not yet synced to Garmin Connect, most likely), never an honest empty day
# Garmin genuinely answered with nothing.
FEEDS_POR_DIA = 6


class FeedsFallidos(Exception):
    """Raised by _pull_one_person/_backfill_one_person when a fetched day's raw['_errors'] names
    every one of FEEDS_POR_DIA feeds -- the day is NOT saved (its metrics would be empty anyway)
    and the per-person caller records this exactly like any other failed attempt (counted in
    'failed', garmin_cuenta.registrar_error/registrar_intento). Carries `nombres`, {exception
    class name: count}, never a message body a library composed."""

    def __init__(self, nombres: dict[str, int]):
        self.nombres = dict(nombres)
        super().__init__(_detalle_fallos(self.nombres))


def _detalle_fallos(nombres: dict[str, int]) -> str:
    return ", ".join(f"{nombre} x{n}" for nombre, n in sorted(nombres.items()))


def _fallos_de_dia(data) -> dict[str, int]:
    """{exception class name: count} from one fetched day's raw['_errors'] (garmin_adapter.
    fetch_day's own per-feed catch) -- {} when every feed answered."""
    errores = data.raw.get("_errors") or {}
    conteo: dict[str, int] = {}
    for nombre in errores.values():
        conteo[nombre] = conteo.get(nombre, 0) + 1
    return conteo


def _dia_vacio(data) -> bool:
    """BODY-GARMIN-DIA-VACIO-1 rulings 1-2: True when this fetched day carries no non-None metric
    value at all -- an honest Garmin gap, judged from the fetched DayData itself (db.save_day
    stays as it is), independently of raw['_errors'] (rulings 5-6, a request failure)."""
    return not any(v is not None for v in data.metrics.values())


# BODY-GARMIN-DIAGNOSTICO-1 ruling 1: DayData.raw's six feed keys, in the fixed order the shape
# line always prints them -- never derived from raw's own key order, which a dict carries for
# whichever feeds happened to succeed or fail this call.
_FEEDS_ORDEN = ("user_summary", "sleep", "hrv", "body_battery", "activities", "max_metrics")

# Ruling 1: a numeric leaf past this depth (the feed's own top-level value is depth 0) is never
# counted -- bounds the cost of a pathological payload and keeps the shape line from ever growing
# with the data's own size.
_FORMA_PROFUNDIDAD_MAX = 4


def _contar_numeros(valor, profundidad: int = 0) -> int:
    """Ruling 1's 'k num': numeric leaves found recursively inside one feed's raw value -- a bool
    is never a number here (Python's bool is an int subclass), and nothing past
    _FORMA_PROFUNDIDAD_MAX deep is ever examined."""
    if profundidad > _FORMA_PROFUNDIDAD_MAX:
        return 0
    if isinstance(valor, bool):
        return 0
    if isinstance(valor, (int, float)):
        return 1
    if isinstance(valor, dict):
        return sum(_contar_numeros(v, profundidad + 1) for v in valor.values())
    if isinstance(valor, list):
        return sum(_contar_numeros(v, profundidad + 1) for v in valor)
    return 0


def _forma_valor(valor) -> str:
    """Ruling 1's shape for one feed's own raw value -- never a value, never a key name: 'None',
    'dict:<n> claves/<k> num', 'list:<n>/<k> num' (n = top-level length, k = _contar_numeros), or
    the bare type name for anything else (a string, a bool, a number on its own)."""
    if valor is None:
        return "None"
    if isinstance(valor, dict):
        return f"dict:{len(valor)} claves/{_contar_numeros(valor)} num"
    if isinstance(valor, list):
        return f"list:{len(valor)}/{_contar_numeros(valor)} num"
    return type(valor).__name__


def _forma_feeds(raw: dict) -> str:
    """BODY-GARMIN-DIAGNOSTICO-1 ruling 1: 'name=<shape>' for each of _FEEDS_ORDEN's six feeds, in
    that order, space-separated -- printed once for a fetched-but-empty day (every hosted path,
    via the two call sites below) so the operator can tell an honest 'Garmin sent nothing'
    (ausente/None) from 'Garmin sent a shape Body does not read' (an unrecognised dict/list) from
    'the feed call itself raised' (error:<Class>, from raw['_errors'] -- the class NAME only,
    never the exception's own message). A feed present in neither raw nor raw['_errors'] is
    'ausente' -- never produced by garmin_adapter.fetch_day itself today (every one of its six
    calls lands in exactly one of the two), kept for any raw a test or a future caller hands in
    with a feed simply missing."""
    errores = raw.get("_errors") or {}
    partes = []
    for nombre in _FEEDS_ORDEN:
        if nombre in errores:
            forma = f"error:{errores[nombre]}"
        elif nombre not in raw:
            forma = "ausente"
        else:
            forma = _forma_valor(raw[nombre])
        partes.append(f"{nombre}={forma}")
    return " ".join(partes)


def _sufijo_reloj(conn, person_id: int | None) -> str:
    """BODY-GARMIN-DIAGNOSTICO-1 ruling 2: ', reloj <YYYY-MM-DD HH:MM> UTC' from this person's own
    persisted reloj_sincronizo_en (garmin_cuenta.estado_sync -- the same field Hoy's live card and
    the assistant's leer_estado_reloj already read; always UTC, garmin_adapter.fetch_device_last_
    sync's own contract), or ', reloj desconocido' when `person_id` is None (no person was
    actually pulled this run) or nothing has ever been recorded for them -- a timestamp only,
    never a health figure."""
    if person_id is not None:
        reloj = garmin_cuenta.estado_sync(conn, person_id).get("reloj_sincronizo_en")
        if reloj is not None:
            momento = datetime.fromisoformat(reloj).astimezone(timezone.utc)
            return f", reloj {momento.strftime('%Y-%m-%d %H:%M')} UTC"
    return ", reloj desconocido"


def _sumar_fallos(total: dict[str, int], nuevos: dict[str, int]) -> None:
    for nombre, n in nuevos.items():
        total[nombre] = total.get(nombre, 0) + n


def _sufijo_fallos_parciales(fallos: dict[str, int]) -> str:
    """Ruling 6: ', {n} feeds failed ({Class} x{k}, ...)' appended to a job line that would
    otherwise read as a plain success -- '' when nothing partially failed this run."""
    if not fallos:
        return ""
    return f", {sum(fallos.values())} feeds failed ({_detalle_fallos(fallos)})"


def _estado_http_de_fallos(nombres: dict[str, int]) -> int | None:
    """Ruling 5's backoff choice for a FeedsFallidos attempt: None (today's auth handling -- no
    backoff, reconnecting is the fix) when ANY recorded class is one garmin_adapter.estado_http's
    own rules read that way; otherwise the first recognised numeric status. Only the class NAME
    survives past garmin_adapter.fetch_day's own per-feed catch -- never the instance -- so this
    mirrors estado_http's own three cases for a bare, argument-less instance of a class named here
    (which never carries a `.response` of its own, so that branch of estado_http's rule never
    applies either way) BY NAME alone, never importing garminconnect: only
    body/garmin_adapter.py may (Tier 1)."""
    estados = []
    for nombre in sorted(nombres):
        if nombre == "GarminConnectTooManyRequestsError":
            estados.append(429)
        elif nombre == "GarminConnectConnectionError":
            estados.append(0)
        else:
            estados.append(None)
    if any(e is None for e in estados):
        return None
    return estados[0]


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


def _pull_one_person(conn, person_id: int, days: list[str]) -> tuple[int, int, dict[str, int]] | None:
    """Pulls `days` for one hosted person, re-encrypting the stored token only if Garmin refreshed
    it -- even when a later day raises, via the try/finally below. Returns (days actually stored,
    days fetched but empty, {feed failure class: count} across every partially-failed day this
    run -- BODY-GARMIN-DIA-VACIO-1 rulings 1-2 and 6), or None when the person has no token to
    pull with. A day whose raw['_errors'] names every feed attempted (ruling 5) is NOT saved and
    raises FeedsFallidos instead of being counted at all. Raises whatever the adapter, Garmin or
    FeedsFallidos raise, with the counts-so-far attached as `.saved`/`.vacios`/`.fallos_parciales`
    on the exception so the caller can still record them; the caller records the error and moves
    on to the next person."""
    token = garmin_cuenta.leer(conn, person_id)
    if token is None:
        return None
    token_before = token
    saved = vacios = 0
    fallos_parciales: dict[str, int] = {}
    try:
        for i, d in enumerate(sorted(days)):
            if i:
                time.sleep(PAUSE_S)
            data, token = garmin_adapter.fetch_day(d, token_str=token)
            fallos = _fallos_de_dia(data)
            if sum(fallos.values()) >= FEEDS_POR_DIA:
                raise FeedsFallidos(fallos)
            if fallos:
                _sumar_fallos(fallos_parciales, fallos)
            db.save_day(conn, data, person_id)
            if _dia_vacio(data):
                vacios += 1
                print(f"dia vacio {d}: {_forma_feeds(data.raw)}", flush=True)
            else:
                saved += 1
    except Exception as e:
        e.saved = saved
        e.vacios = vacios
        e.fallos_parciales = fallos_parciales
        raise
    finally:
        if token != token_before:
            garmin_cuenta.guardar(conn, person_id, token)
        _registrar_reloj_si_hay(conn, person_id, token)
    return saved, vacios, fallos_parciales


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


def _recalcular_readiness_seguro(conn, person_id: int) -> None:
    """BODY-REVISION-SYNC-READINESS-1 ruling 1: the SAME recompute the local branch's own daily
    loop calls (`readiness.recompute_all`), never a second implementation -- so every hosted path
    that saves days also stores readiness for them, not only the local one. Readiness is derived
    from this person's own stored metrics, so recomputing their WHOLE history on every pull is
    idempotent (never wrong, only possibly redundant) -- the same reasoning that lets the nightly
    backfill timer fill in the stored history gradually with no one-off command. A failure here is
    never a Garmin failure and never stops the pull for this or the next person (same safe-wrapper
    shape as `_recalcular_sueno_fc_seguro`): caught, printed as a counts-only line with the
    exception TYPE, nothing else happens. Unlike the other two recalc sweeps, never silent-on-
    success-only: recompute_all's own count is "every day with data," not "every new row," so a
    no-op success would print a nonzero line on literally every call -- silent either way."""
    try:
        readiness.recompute_all(conn, person_id)
    except Exception as e:
        print(f"readiness failed: {errores.texto_seguro(e)}", flush=True)


def _sincronizar_lugares_seguro(conn, person_id: int) -> None:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: at most once per person per calendar day (skipped,
    silently, when `lugares_sync.intento_at` already names today -- never a second network
    attempt the same day); never for `--backfill`. A failure here is never a Garmin failure and
    never stops the pull for this or the next person (same safe-wrapper shape as
    `_recalcular_turno_fc_seguro`): caught, printed as a counts-only line, nothing else happens."""
    try:
        hoy = datetime.now(timezone.utc).date()  # same clock `sincronizar`'s own `ahora` uses
        estado = db.lugares_estado(conn, person_id)
        if estado["intento_at"] and estado["intento_at"][:10] == hoy.isoformat():
            return
        resultado = lugares.sincronizar(conn, person_id, hoy)
        if resultado["estado"] == "sin_conectar":
            print("lugares: sin conectar", flush=True)
        elif resultado["estado"] == "ok":
            print(f"lugares: {resultado['viajes']} viajes", flush=True)
        else:
            print(f"lugares: {resultado['motivo']}", flush=True)
    except Exception as e:
        print(f"lugares failed: {errores.texto_seguro(e)}", flush=True)


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


def _dias_pendientes_relleno(
    conn, person_id: int, hoy: date, n: int = 2, incluir_antiguos: bool = False,
) -> list[str] | tuple[list[str], set[str]]:
    """BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 1, amended by -1-B: today and the `n - 1` days before
    it ALWAYS (n defaults to 2 -- today and yesterday, since Garmin finalises yesterday late;
    an explicit `--days N` passes n=N so that window still heals too), plus every other gap day in
    garmin_cuenta.dias_sin_datos' own VENTANA_RELLENO-day window (one query per person, never one
    per day), oldest first. A day already stored is not refetched unless it falls in the always-
    fetched window. Capped at MAX_DIAS_POR_CORRIDA: the always-fetched window never gives way (the
    cap trims the OLDEST gap days first, keeping the newest ones) -- never trims anything in
    practice for the default n=2, since VENTANA_RELLENO (14) already fits under the cap (16).

    BODY-GARMIN-RELLENO-ANTIGUO-1 rulings 1-3: `incluir_antiguos=True` -- the daily hosted pull
    only (`pull_hosted`'s own implicit-today call), never the today-sweep, connect or backfill --
    also retries gap days OLDER than VENTANA_RELLENO and inside BACKFILL_DIAS
    (`garmin_cuenta.dias_sin_datos_antiguos`, newest first), AFTER the always-fetched window and
    the 14-day gaps above and only in the room they leave under MAX_DIAS_POR_CORRIDA, capped at
    garmin_cuenta.RELLENO_ANTIGUO_POR_CORRIDA. When `incluir_antiguos` is true, returns `(dias,
    antiguos)` instead of a plain list -- `antiguos` the subset of `dias` added for this reason
    (empty when there was no room left), so the caller can count them for its own job line without
    recomputing which days are "older." `incluir_antiguos=False` (the default, every other caller)
    returns the plain list exactly as before this ruling -- unchanged shape, unchanged days."""
    obligatorios = {(hoy - timedelta(i)).isoformat() for i in range(n)}
    huecos = [d for d in garmin_cuenta.dias_sin_datos(conn, person_id, hoy) if d not in obligatorios]
    cupo = max(MAX_DIAS_POR_CORRIDA - len(obligatorios), 0)
    dias = obligatorios | set(huecos[-cupo:] if cupo else [])
    if not incluir_antiguos:
        return sorted(dias)
    cupo_antiguos = max(MAX_DIAS_POR_CORRIDA - len(dias), 0)
    antiguos: set[str] = set()
    if cupo_antiguos:
        candidatos = garmin_cuenta.dias_sin_datos_antiguos(conn, person_id, hoy, BACKFILL_DIAS)
        antiguos = set(candidatos[:min(cupo_antiguos, garmin_cuenta.RELLENO_ANTIGUO_POR_CORRIDA)])
    return sorted(dias | antiguos), antiguos


def _registrar_intento_persona(conn, person_id: int, ahora: datetime, exc: Exception | None) -> None:
    """BODY-SYNC-VIVO-ESTADO-1 ruling 3: one garmin_cuenta.registrar_intento call per person per
    attempt -- `exc` is the exception the attempt raised, or None on success. Prints a counts/
    class-only line on failure (errores.texto_seguro_con_estado), never a message body, never a
    health figure.

    BODY-GARMIN-DIA-VACIO-1 ruling 5: a FeedsFallidos attempt (every feed a fetched day attempted
    raised) is special-cased -- the backoff comes from `_estado_http_de_fallos` (which class names
    the day carried, not FeedsFallidos' own type), and the printed line names those classes and
    their counts directly, never the generic class-plus-status line."""
    if isinstance(exc, FeedsFallidos):
        estado = _estado_http_de_fallos(exc.nombres)
        garmin_cuenta.registrar_intento(conn, person_id, ahora, ok=False, estado_http=estado)
        print(f"pull attempt failed: FeedsFallidos ({_detalle_fallos(exc.nombres)})", flush=True)
        return
    estado = garmin_adapter.estado_http(exc) if exc is not None else None
    garmin_cuenta.registrar_intento(conn, person_id, ahora, ok=exc is None, estado_http=estado)
    if exc is not None:
        print(f"pull attempt failed: {errores.texto_seguro_con_estado(exc, estado)}", flush=True)


def _pull_persona(conn, person_id: int, days: list[str]) -> dict:
    """One person's pull-and-postprocess: `_pull_one_person` (which itself handles the token --
    read, refresh, re-encrypt), the error record/clear, and the four post-pull recalcs (BODY-
    REVISION-SYNC-READINESS-1 ruling 1 adds readiness to the existing three) -- shared by
    `pull_hosted`'s own per-person loop and the "connecting starts a pull now" path (ruling 4b),
    the same per-person path, never a copy. Returns {"estado": "sin_token"|"error"|"ok", "saved":
    int, "vacios": int, "fallos_parciales": dict[str, int]} -- "sin_token" when the person has no
    token to pull with (never counted as a failure, same as before this refactor); "saved" counts
    only non-empty days (BODY-GARMIN-DIA-VACIO-1 ruling 2), "vacios" the empty ones, and
    "fallos_parciales" every partially-failed day's feed-failure classes (ruling 6) -- all three
    default to 0/{} on "sin_token"/"error" when `_pull_one_person` never got that far. Records
    exactly one attempt (BODY-SYNC-VIVO-ESTADO-1 ruling 3) -- never for "sin_token", which never
    reached Garmin at all."""
    ahora = datetime.now(timezone.utc)
    try:
        resultado = _pull_one_person(conn, person_id, days)
    except Exception as e:
        garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
        _registrar_intento_persona(conn, person_id, ahora, e)
        return {"estado": "error", "saved": getattr(e, "saved", 0), "vacios": getattr(e, "vacios", 0),
                "fallos_parciales": getattr(e, "fallos_parciales", {})}
    if resultado is None:
        return {"estado": "sin_token", "saved": 0, "vacios": 0, "fallos_parciales": {}}
    n, vacios, fallos_parciales = resultado
    garmin_cuenta.limpiar_error(conn, person_id)
    _registrar_intento_persona(conn, person_id, ahora, None)
    _recalcular_recuperacion_seguro(conn, person_id)
    _recalcular_turno_fc_seguro(conn, person_id)
    _recalcular_sueno_fc_seguro(conn, person_id)
    _recalcular_readiness_seguro(conn, person_id)  # ruling 1: hosted now stores it too
    return {"estado": "ok", "saved": n, "vacios": vacios, "fallos_parciales": fallos_parciales}


def pull_persona_ahora(person_id: int) -> None:
    """BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 4b: pulls ONE person's healing set (ruling 1) right
    now -- the same per-person path `pull_hosted`'s own loop uses (`_pull_persona`), never a copy.
    Called from a FastAPI BackgroundTask after `/garmin/conectar` or `/garmin/codigo` stores a
    token, so the gap since the person's last data fills in the same run the connect itself
    triggers, instead of waiting for the next daily pull. Opens and closes its own connection (a
    background task runs after the request's own connection has already closed). Records the
    error exactly as the daily timer does; prints counts only, never a health figure.

    BODY-GARMIN-DIA-VACIO-1 ruling 2: the printed line also names the empty-day count and, when
    any day this run partially failed, which feeds and how many times (ruling 6).
    BODY-GARMIN-DIAGNOSTICO-1 ruling 2: it also names this person's own last known watch-contact
    instant (a timestamp, or "desconocido"); a fetched-but-empty day also prints its own one-line
    shape (ruling 1), from the shared `_pull_one_person`."""
    conn = db.connect()
    try:
        db.ensure_ready(conn)
        hoy = fechas.hoy_local(db.person(conn, person_id))
        dias = _dias_pendientes_relleno(conn, person_id, hoy)
        resultado = _pull_persona(conn, person_id, dias)
        print(f"connect pull: {resultado['estado']}, {resultado['saved']} days stored, "
              f"{resultado['vacios']} empty{_sufijo_fallos_parciales(resultado['fallos_parciales'])}"
              f"{_sufijo_reloj(conn, person_id)}",
              flush=True)
    finally:
        conn.close()


def _en_horas_quietas(ahora_local: datetime) -> bool:
    h = ahora_local.hour
    return h >= QUIETA_DESDE_HORA or h < QUIETA_HASTA_HORA


def _pull_hoy_persona(conn, person_id: int, ahora_local: datetime, ahora_utc: datetime) -> tuple[bool, int]:
    """The fetch+record+recalc sequence for one person's "today" alone -- shared by
    `pull_hoy_ahora`'s own trigger and the --hoy sweep (`pull_hoy_todos`), never a copy: fetches
    exactly one day through `_pull_one_person` (the same fetch path, token re-encryption and watch-
    last-sync read the daily/connect pulls already use for one day), records the attempt
    (`garmin_cuenta.registrar_error`/`limpiar_error`/`_registrar_intento_persona`, the same shape
    every other pull uses), and on success recomputes `sueno_fc` (BODY-SYNC-VIVO-2's own
    reasoning: the one post-pull derivation a live trigger could actually read right after this
    call returns) and, since BODY-REVISION-SYNC-READINESS-1 ruling 1, readiness too -- not for
    Hoy/Día's own SCORE (both read it live, `readiness.compute`, never storage), but for the
    STORED history their own "vs your average" baseline line leans on, and for Tendencias' own
    readiness chart (ruling 4): this trigger saves a day same as any other pull path, so it stores
    readiness for it same as any other. `recuperacion`/`turno_fc` still stay out, unchanged
    (neither Día nor Hoy reaches either from this trigger). Never raises -- a Garmin failure is
    recorded, not propagated. Returns (True, days empty) on success, (False, days empty) on a
    Garmin failure -- BODY-GARMIN-DIAGNOSTICO-1 ruling 3: the empty count `_pull_one_person`
    already returns (or attaches to its own exception as `.vacios`, the same contract every other
    caller here reads), carried out so the --hoy sweep's own line can count it too."""
    dia = ahora_local.date().isoformat()
    try:
        resultado = _pull_one_person(conn, person_id, [dia])
    except Exception as e:
        garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
        _registrar_intento_persona(conn, person_id, ahora_utc, e)
        return False, getattr(e, "vacios", 0)
    garmin_cuenta.limpiar_error(conn, person_id)
    _registrar_intento_persona(conn, person_id, ahora_utc, None)
    _recalcular_sueno_fc_seguro(conn, person_id)
    _recalcular_readiness_seguro(conn, person_id)
    _, vacios, _ = resultado
    return True, vacios


def pull_hoy_todos(conn) -> tuple[int, int, int, int, int, int | None]:
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

    Otherwise runs the shared `_pull_hoy_persona` fetch -- counted as `ok` or `fallidas`, its own
    empty-day count carried into `vacios` (BODY-GARMIN-DIAGNOSTICO-1 ruling 3 -- VACIO-1 left this
    sweep's own line without an empty count, unlike every other hosted job line). Returns (ok,
    en_espera, fuera_horario, fallidas, vacios, last person actually pulled this run or None --
    ruling 2's own `_sufijo_reloj` input, read by `_run`'s own printed line). Never prints a
    health figure -- counts only, and never prints anything itself; the caller (`_run`) owns the
    one printed line for this whole sweep."""
    ok = en_espera = fuera_horario = fallidas = vacios = 0
    ultima_persona = None
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
        exito, vacios_persona = _pull_hoy_persona(conn, person_id, ahora_local, ahora_utc)
        vacios += vacios_persona
        ultima_persona = person_id
        if exito:
            ok += 1
        else:
            fallidas += 1
    return ok, en_espera, fuera_horario, fallidas, vacios, ultima_persona


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
    BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1) -- zero extra calls for a person with none. Each
    person this run actually pulls also gets their readiness recomputed (`_pull_persona`'s own
    ruling 1, BODY-REVISION-SYNC-READINESS-1) -- the SAME call the local branch's own daily loop
    makes, never a second implementation.
    Returns (persons ok, persons failed, days stored -- BODY-GARMIN-DIA-VACIO-1 ruling 2: an
    empty/all-NULL day never counts here, so this return's own arity and meaning for "saved" stay
    exactly as before for every existing caller). Prints its own job line before returning
    ("{ok} ok, {failed} failed, {saved} days stored, {vacios} empty, {antiguos} older retried",
    plus ruling 6's feed-failure suffix when any day this run partially failed, plus
    BODY-GARMIN-DIAGNOSTICO-1 ruling 2's own ", reloj <timestamp> UTC"/", reloj desconocido" suffix
    for the last person this run actually pulled) -- counts, class names and a timestamp only,
    never a health figure. A fetched-but-empty day also prints its own one-line shape ("dia vacio
    <day>: ...", BODY-GARMIN-DIAGNOSTICO-1 ruling 1), from the shared `_pull_one_person`.

    BODY-GARMIN-RELLENO-ANTIGUO-1 ruling 2: this is the ONLY caller of `_dias_pendientes_relleno`
    that passes `incluir_antiguos=True` -- the once-a-day hosted pull (the host's own
    deploy/host/body-pull.service timer, and any manual `--days N` run through this same CLI
    entry point). `pull_persona_ahora` (the "connecting starts a pull now" path, which can run
    more than once a day) and `_pull_hoy_persona`/`pull_hoy_todos` (the --hoy sweep) both call
    `_dias_pendientes_relleno` with the default `incluir_antiguos=False` and never retry an older
    day; `_backfill_one_person` does not call `_dias_pendientes_relleno` at all. Only on the
    implicit "today" sweep (no explicit `fecha`) -- an explicit `--date` never retries an older
    gap either, same as it never heals a recent one."""
    ok = failed = saved = omitidos = vacios = antiguos = 0
    fallos_parciales: dict[str, int] = {}
    ultima_persona = None  # BODY-GARMIN-DIAGNOSTICO-1 ruling 2: the reloj suffix below names
    # the last person this run actually pulled -- a single-scalar approximation that reads
    # exactly right for today's one hosted person, and names something real (that one person's
    # own last-known watch contact) rather than nothing for any future multi-person run.
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
            days, antiguos_persona = _dias_pendientes_relleno(
                conn, person_id, hoy, n=n_days if n_days is not None else 2, incluir_antiguos=True)
            antiguos += len(antiguos_persona)
        resultado = _pull_persona(conn, person_id, days)
        _sincronizar_lugares_seguro(conn, person_id)
        ultima_persona = person_id
        saved += resultado["saved"]
        vacios += resultado["vacios"]
        _sumar_fallos(fallos_parciales, resultado["fallos_parciales"])
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
    print(f"{ok} ok, {failed} failed, {saved} days stored, {vacios} empty, {antiguos} older retried"
          f"{_sufijo_fallos_parciales(fallos_parciales)}{_sufijo_reloj(conn, ultima_persona)}", flush=True)
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


def _backfill_one_person(
    conn, person_id: int, hoy: date,
) -> tuple[int, int, date | None, dict[str, int]] | None:
    """Fetches this person's next backfill batch (at most BACKFILL_PASO days, newest first),
    advancing their backfill marker after each day so a day already tried -- with or without
    data -- is never asked again, EVEN an empty one (BODY-GARMIN-DIA-VACIO-1 ruling 2: the daily
    pull's own 14-day window is what heals recent gaps; an older empty backfill day is named in
    open_followups, not solved here). A day whose raw['_errors'] names every feed attempted
    (ruling 5) is the one exception -- it is NOT saved and the marker does NOT advance past it,
    raising FeedsFallidos instead. Returns (days stored, days fetched but empty, oldest day
    fetched this run or None, {feed failure class: count} across every partially-failed day this
    run -- ruling 6), or None when the person has no token to pull with. Raises whatever the
    adapter, Garmin or FeedsFallidos raise, with the counts-so-far attached as
    `.saved`/`.vacios`/`.fallos_parciales` and the oldest of those as `.oldest`
    (BODY-SEMANAS-TARDIAS-1), the same `.saved` contract `_pull_one_person` uses."""
    token = garmin_cuenta.leer(conn, person_id)
    if token is None:
        return None
    dias = _dias_pendientes_backfill(conn, person_id, hoy)
    if not dias:
        return 0, 0, None, {}
    token_before = token
    saved = vacios = 0
    oldest = None
    fallos_parciales: dict[str, int] = {}
    try:
        for i, d in enumerate(dias):
            if i:
                time.sleep(PAUSE_S)
            data, token = garmin_adapter.fetch_day(d, token_str=token)
            fallos = _fallos_de_dia(data)
            if sum(fallos.values()) >= FEEDS_POR_DIA:
                raise FeedsFallidos(fallos)
            if fallos:
                _sumar_fallos(fallos_parciales, fallos)
            db.save_day(conn, data, person_id)
            oldest = d
            garmin_cuenta.avanzar_backfill(conn, person_id, d)
            if _dia_vacio(data):
                vacios += 1
                print(f"dia vacio {d}: {_forma_feeds(data.raw)}", flush=True)
            else:
                saved += 1
    except Exception as e:
        e.saved = saved
        e.vacios = vacios
        e.oldest = oldest
        e.fallos_parciales = fallos_parciales
        raise
    finally:
        if token != token_before:
            garmin_cuenta.guardar(conn, person_id, token)
        _registrar_reloj_si_hay(conn, person_id, token)
    return saved, vacios, oldest, fallos_parciales


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
    failed, days stored -- BODY-GARMIN-DIA-VACIO-1 ruling 2: an empty/all-NULL day never counts
    here, so this return's own arity and meaning for "saved" stay exactly as before for every
    existing caller). Prints its own job line before returning ("backfill: {ok} ok, {failed}
    failed, {saved} days stored, {vacios} empty", plus ruling 6's feed-failure suffix when any
    day this run partially failed, plus BODY-GARMIN-DIAGNOSTICO-1 ruling 2's own reloj suffix for
    the last person this run actually pulled) -- counts, class names and a timestamp only, never
    a health figure. A fetched-but-empty day also prints its own one-line shape (ruling 1), from
    the shared `_backfill_one_person`.

    BODY-REVISION-SYNC-READINESS-1 ruling 1: on a person this run actually fetched something for,
    also recomputes that person's readiness across their WHOLE stored history (the nightly timer
    is what fills the stored history in gradually, batch by batch, with no one-off command)."""
    ok = failed = saved = omitidos = vacios = 0
    desde_por_persona: dict[int, date] = {}
    fallos_parciales: dict[str, int] = {}
    ultima_persona = None  # BODY-GARMIN-DIAGNOSTICO-1 ruling 2: see pull_hosted's own comment.
    for person_id in garmin_cuenta.every_person_id(conn):
        estado = garmin_cuenta.estado_sync(conn, person_id)
        if estado["ultimo_error"] is not None and estado["reintentar_despues_de"] is None:
            continue
        if not garmin_cuenta.puede_intentar(conn, person_id, datetime.now(timezone.utc), ESPACIO_MIN_ENTRE_INTENTOS):
            omitidos += 1
            continue
        hoy_persona = hoy or fechas.hoy_local(db.person(conn, person_id))
        ahora = datetime.now(timezone.utc)
        ultima_persona = person_id
        try:
            result = _backfill_one_person(conn, person_id, hoy_persona)
        except Exception as e:
            garmin_cuenta.registrar_error(conn, person_id, type(e).__name__)
            _registrar_intento_persona(conn, person_id, ahora, e)
            failed += 1
            saved += getattr(e, "saved", 0)
            vacios += getattr(e, "vacios", 0)
            _sumar_fallos(fallos_parciales, getattr(e, "fallos_parciales", {}))
            oldest = getattr(e, "oldest", None)
            if oldest is not None:
                desde_por_persona[person_id] = date.fromisoformat(oldest)
            continue
        if result is None:
            continue
        n, vacios_persona, oldest, fallos_persona = result
        garmin_cuenta.limpiar_error(conn, person_id)
        if n + vacios_persona > 0:  # both zero is "this person's backfill window is already
            # complete" -- no Garmin call was made this run, so there is no attempt to record; a
            # day actually fetched (even one Garmin answered empty, ruling 1-2) always counts
            # toward one of the two.
            _registrar_intento_persona(conn, person_id, ahora, None)
        saved += n
        vacios += vacios_persona
        _sumar_fallos(fallos_parciales, fallos_persona)
        ok += 1
        _recalcular_recuperacion_seguro(conn, person_id)
        _recalcular_turno_fc_seguro(conn, person_id)
        _recalcular_sueno_fc_seguro(conn, person_id)
        _recalcular_readiness_seguro(conn, person_id)  # ruling 1: across this person's whole
        # stored history, not just this run's own batch -- the nightly timer's own reach widens a
        # little more each run, so the stored history fills in gradually with no one-off command.
        if oldest is not None:
            desde_por_persona[person_id] = date.fromisoformat(oldest)
    if omitidos:
        print(f"omitido: {omitidos} personas en espera", flush=True)
    _cerrar_semanas_todas(conn, desde_por_persona)
    print(f"backfill: {ok} ok, {failed} failed, {saved} days stored, {vacios} empty"
          f"{_sufijo_fallos_parciales(fallos_parciales)}{_sufijo_reloj(conn, ultima_persona)}", flush=True)
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


# BODY-DESPERTAR-REAL-2 ruling 2: an in-process guard only -- a worker restart loses it, which
# only costs one extra on-demand fetch, never a wrong answer. Deliberately NOT garmin_cuenta's own
# ultimo_intento_en/reintentar_despues_de (BODY-SYNC-VIVO-ESTADO-1): an intraday refresh failure
# must never show up as a daily-sync backoff on Hoy's own "Tu reloj" card (texto_estado_vivo reads
# exactly those two columns) or delay the next scheduled pull's own puede_intentar check.
_ULTIMO_INTENTO_FRESCURA_INTRADIA: dict[tuple[int, str], datetime] = {}
INTRADIA_FRESCURA_ESPACIO_MIN = 30


def actualizar_intradia_si_corresponde(person_id: int, day: str) -> bool:
    """BODY-DESPERTAR-REAL-2 ruling 2: called from body/agente/herramientas.py's `ejecutar()`,
    never from `_leer_intradia`'s own body -- a `lectura` tool must never write (BODY-ASISTENTE-
    OBS-1), the same place BODY-SYNC-VIVO-2's own `_lanzar_pull_hoy_en_hilo` already does this for
    leer_hoy/leer_estado_reloj. Opens its OWN connection: the caller's `conn` sits under PRAGMA
    query_only for the whole tool call, so a write through it would simply fail.

    Refreshes `day` through `traer_intradia(forzar=True)` -- the SAME per-person token path
    `_pull_intradia_pendientes` already uses: hosted (`garmin_cuenta.leer`/`.guardar`), else the
    local adapter's own stored token -- but only when `day` is today or yesterday in the person's
    own zone and not yet stored complete (`db.intradia_almacenado`), and at most once per
    (person_id, day) every `INTRADIA_FRESCURA_ESPACIO_MIN` minutes (the in-process dict above).

    Returns False whenever no on-demand fetch was attempted (an older day, a future day, an
    already-complete day, the 30-minute guard, or anything that keeps this from even deciding --
    an unknown person, say) OR one was attempted and it succeeded -- the stored data already
    answers either way. Returns True -- never raises -- when one was attempted and failed or was
    refused (no Garmin connection, a Garmin error, any other unexpected failure)."""
    conn = db.connect()
    try:
        db.ensure_ready(conn)
        try:
            fecha = date.fromisoformat(day)
            person = db.person(conn, person_id)
            hoy = fechas.hoy_local(person)
            necesita = (hoy - timedelta(days=1) <= fecha <= hoy
                        and not db.intradia_almacenado(conn, person_id, day))
        except Exception:
            return False
        if not necesita:
            return False
        clave = (person_id, day)
        ahora = datetime.now(timezone.utc)
        ultimo = _ULTIMO_INTENTO_FRESCURA_INTRADIA.get(clave)
        if ultimo is not None and (ahora - ultimo).total_seconds() < INTRADIA_FRESCURA_ESPACIO_MIN * 60:
            return False
        _ULTIMO_INTENTO_FRESCURA_INTRADIA[clave] = ahora
        try:
            if cuenta.hosted():
                token = garmin_cuenta.leer(conn, person_id)
                if token is None:
                    return True
                _, token_after = traer_intradia(conn, person_id, day, token_str=token, forzar=True)
                if token_after != token:
                    garmin_cuenta.guardar(conn, person_id, token_after)
            else:
                traer_intradia(conn, person_id, day, forzar=True)
            return False
        except Exception as e:
            print(f"actualizar_intradia_si_corresponde: fetch failed for {day}: {errores.texto_seguro(e)}",
                  flush=True)
            return True
    finally:
        conn.close()


def traer_detalle_actividad(conn, person_id: int, source_id: str, token_str: str | None = None):
    """Pulls one activity's full detail through the adapter only (garmin_adapter.fetch_activity_
    detail) and stores each kind that call actually returned (db.guardar_detalle_actividad) --
    'detalle'/'vueltas'/'zonas', matching ActivityDetailData.raw's own keys. A kind whose own
    Garmin call failed (present in raw['_errors'] instead) is told apart by its error class name
    (garmin_adapter.detalle_no_disponible, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1-2): a 404
    (Garmin genuinely has nothing) is stored as the existing empty marker (db.guardar_detalle_
    actividad(..., kind, {}), db.py:472-473's convention) so it counts as stored and is never
    re-asked for; any other class (429, a 5xx, a timeout, or an unrecognised class) is left
    unstored, same as before this ruling, and its class name is counted into `fallidas` --
    {class name: count} across the (at most 3) kinds that stayed unresolved this call, the
    `_fallos_de_dia` shape -- so the caller can tell a transient failure apart from a genuine
    404, the same `_errors` pattern fetch_intraday already uses: one failing call never loses the
    other two. Returns (stored, fallidas) -- stored the number of kinds now resolved (0-3,
    counting a 404 marker as resolved) -- or (stored, fallidas, token_after) when token_str is
    given (hosted, per-person token path, the same dual-return shape fetch_activity_detail/
    traer_series use, extended with `fallidas` in the middle so every existing caller's own
    `token_after`/last-position unpacking stays where it was)."""
    if token_str is not None:
        data, token_after = garmin_adapter.fetch_activity_detail(source_id, token_str=token_str)
    else:
        data = garmin_adapter.fetch_activity_detail(source_id)
    errores_por_kind = data.raw.get("_errors") or {}
    stored = 0
    fallidas: dict[str, int] = {}
    for kind in db.DETALLE_KINDS:
        if kind in data.raw:
            db.guardar_detalle_actividad(conn, person_id, garmin_adapter.SOURCE, source_id, kind, data.raw[kind])
            stored += 1
            continue
        nombre_error = errores_por_kind.get(kind)
        if nombre_error is None:
            continue
        if garmin_adapter.detalle_no_disponible(nombre_error):
            db.guardar_detalle_actividad(conn, person_id, garmin_adapter.SOURCE, source_id, kind, {})
            stored += 1
        else:
            fallidas[nombre_error] = fallidas.get(nombre_error, 0) + 1
    if token_str is not None:
        return stored, fallidas, token_after
    return stored, fallidas


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
                _, _, token = traer_detalle_actividad(conn, person_id, source_id, token_str=token)
            else:
                traer_detalle_actividad(conn, person_id, source_id)
            fetched += 1
        except Exception as e:
            print(f"detalle: fetch failed for one activity: {errores.texto_seguro(e)}", flush=True)
    if token_str is not None and token != token_before:
        garmin_cuenta.guardar(conn, person_id, token)
    print(f"detalle: {fetched} actividades traídas, {len(pendientes) - fetched} pendientes", flush=True)
    return fetched


# BODY-DETALLE-HISTORIA-1 ruling 3: the manual backfill's own priority order, kept as DATA in the
# checkin_sesion._CONJUNTOS shape -- a non-generic type matches by its CODE alone ('tipos'),
# Garmin's generic 'other' bucket matches by the folded name instead ('nombres_other',
# entreno_sesion._normaliza_nombre). Checked in order; an activity matching none of these three
# falls into the last, catch-all group ("otro"). GRUPOS_PRIORIDAD_HISTORIA_NOMBRES names each
# group (plus "otro") in the same order, for anything printing the group a source_id landed in
# without re-deriving it.
_PRIORIDAD_HISTORIA: tuple[dict, ...] = (
    {"tipos": frozenset({"basketball"}),
     "nombres_other": frozenset({"basketball", "baloncesto", "basquetbol"})},
    {"tipos": frozenset(), "nombres_other": frozenset({"futbol", "football", "soccer"})},
    {"tipos": frozenset({"strength_training"}), "nombres_other": frozenset()},
)

GRUPOS_PRIORIDAD_HISTORIA_NOMBRES = ("basketball", "soccer", "strength", "otro")


def _grupo_prioridad_historia(tipo: str | None, nombre: str | None) -> int:
    """The index into _PRIORIDAD_HISTORIA (or len(_PRIORIDAD_HISTORIA) for the catch-all "otro"
    group) this activity's own (type, name) matches -- the same two-step match set_para's own
    loop uses (checkin_sesion.py), never a copy of its logic re-derived here."""
    nombre_norm = entreno_sesion._normaliza_nombre(nombre)
    for i, c in enumerate(_PRIORIDAD_HISTORIA):
        if tipo in c["tipos"]:
            return i
        if tipo == entreno_sesion.TIPO_GENERICO and nombre_norm in c["nombres_other"]:
            return i
    return len(_PRIORIDAD_HISTORIA)


def _tipos_nombres_de(conn, person_id: int, source_ids: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """This person's own (type, name) for each of `source_ids` -- a direct read of `activity`
    (the same direct-SQL style body.cobertura._actividades_con_detalle already uses over this
    same table; db.actividades_sin_detalle itself returns source_ids only, with no column to add
    without a db.py change this slice does not make)."""
    if not source_ids:
        return {}
    marcadores = ",".join("?" for _ in source_ids)
    filas = conn.execute(
        f"SELECT source_id, type, name FROM activity WHERE person_id = ? AND source = ? "
        f"AND source_id IN ({marcadores})",
        (person_id, garmin_adapter.SOURCE, *source_ids),
    ).fetchall()
    return {f["source_id"]: (f["type"], f["name"]) for f in filas}


def _ordenar_prioridad_historia(conn, person_id: int, pendientes: list[str]) -> list[str]:
    """`pendientes` (db.actividades_sin_detalle's own newest-day-first order), regrouped by
    _PRIORIDAD_HISTORIA while keeping each group's own relative order -- which, since the input
    was already newest first overall, is already newest first within each group too, with no
    second sort needed."""
    tipos_nombres = _tipos_nombres_de(conn, person_id, pendientes)
    grupos: list[list[str]] = [[] for _ in range(len(_PRIORIDAD_HISTORIA) + 1)]
    for source_id in pendientes:
        tipo, nombre = tipos_nombres.get(source_id, (None, None))
        grupos[_grupo_prioridad_historia(tipo, nombre)].append(source_id)
    return [source_id for grupo in grupos for source_id in grupo]


# ORCH-CARLOS-DELEGATES-TIER-1-1: the earliest possible ISO date, passed to db.actividades_sin_
# detalle so it selects with no day floor at all -- the whole history, never only DETALLE_ALCANCE_
# DIAS back (the scheduled path's own, narrower window). No db.py change: actividades_sin_detalle
# already takes `desde_dia` as a plain string compared with SQL >=, so any date before every real
# activity day serves as "no floor".
FECHA_MINIMA_HISTORIA = "0001-01-01"


def traer_detalle_historia(
    conn, person_id: int, *, token_str: str, max_actividades: int, pausa_s: float = PAUSE_S,
) -> dict:
    """BODY-DETALLE-HISTORIA-1 ruling 3: the manual, paced, resumable backfill over this person's
    WHOLE activity history (db.actividades_sin_detalle with FECHA_MINIMA_HISTORIA, no day floor),
    newest first within _PRIORIDAD_HISTORIA's own priority groups (_ordenar_prioridad_historia).
    Always the hosted per-person token path -- garmin_cuenta.leer/guardar for the token, never the
    local TOKEN_DIR shortcut -- and only through traer_detalle_actividad, so a 404 still lands the
    SAME empty marker a scheduled pull would store (ruling 2) and this run is resumable with no
    marker of its own: a re-run simply sees fewer db.actividades_sin_detalle candidates.

    Gated the same way the scheduled path is (privacidad.guarda_detalle): not consented makes NO
    Garmin call at all and returns with detenida_por='sin_consentimiento'. Holds the same pull
    lock the daily/backfill timers use, waiting for it exactly as the daily pull does
    (_esperar_lock(LOCK_ESPERA_S)) so this never runs beside a scheduled pull; still held after
    that wait refuses outright (detenida_por='bloqueo'), no Garmin call either.

    Sleeps `pausa_s` between activities (none before the first) and stops once `max_actividades`
    have been resolved. Stops at the FIRST transient failure (an exception raised straight out of
    `_client()` such as a 429/5xx/connection error -- never caught inside traer_detalle_actividad
    itself, since that only catches the three per-kind Garmin calls -- or a non-empty `fallidas`
    from traer_detalle_actividad, i.e. a kind whose own error was not a 404) or `garmin_adapter.
    NotSignedIn`, recording it through the EXISTING `_registrar_intento_persona` (reusing
    `FeedsFallidos` purely as `_estado_http_de_fallos`'s own {class name: count} carrier, so the
    backoff this sets is the exact SAME one a scheduled pull's own feed failures would set --
    one account, one backoff) before returning.

    Returns {'resueltas': activities fully resolved this run, 'restantes': still-pending
    activities after this run (across the WHOLE history, not just this run's own selection),
    'fallidas_transitorias': {class name: count} for the one activity that stopped this run (empty
    when it did not stop early), 'detenida_por': the stop reason, or None when every selected
    activity (up to `max_actividades`, or fewer when fewer were pending) resolved cleanly} --
    counts and class names only, never a source_id or an activity name."""
    if not privacidad.guarda_detalle(conn, person_id):
        print("detalle historia: sin consentimiento; no se hizo ninguna llamada a Garmin", flush=True)
        return {"resueltas": 0, "restantes": 0, "fallidas_transitorias": {}, "detenida_por": "sin_consentimiento"}

    pendientes_todas = db.actividades_sin_detalle(conn, person_id, FECHA_MINIMA_HISTORIA)
    if not pendientes_todas:
        return {"resueltas": 0, "restantes": 0, "fallidas_transitorias": {}, "detenida_por": None}

    lock = _esperar_lock(LOCK_ESPERA_S)
    if lock is None:
        print("detalle historia: otro pull está en curso; nada se hizo esta corrida", flush=True)
        return {"resueltas": 0, "restantes": len(pendientes_todas), "fallidas_transitorias": {},
                "detenida_por": "bloqueo"}

    token = token_before = token_str
    try:
        seleccion = _ordenar_prioridad_historia(conn, person_id, pendientes_todas)[:max_actividades]
        resueltas = 0
        fallidas_transitorias: dict[str, int] = {}
        detenida_por = None
        ahora = datetime.now(timezone.utc)
        for i, source_id in enumerate(seleccion):
            if i:
                time.sleep(pausa_s)
            try:
                _, fallidas, token = traer_detalle_actividad(conn, person_id, source_id, token_str=token)
            except garmin_adapter.NotSignedIn as e:
                _registrar_intento_persona(conn, person_id, ahora, e)
                detenida_por = "sin_firma"
                break
            except Exception as e:
                _registrar_intento_persona(conn, person_id, ahora, e)
                fallidas_transitorias = {type(e).__name__: 1}
                detenida_por = "transitorio"
                break
            if fallidas:
                _registrar_intento_persona(conn, person_id, ahora, FeedsFallidos(fallidas))
                fallidas_transitorias = fallidas
                detenida_por = "transitorio"
                break
            resueltas += 1
        return {
            "resueltas": resueltas,
            "restantes": len(pendientes_todas) - resueltas,
            "fallidas_transitorias": fallidas_transitorias,
            "detenida_por": detenida_por,
        }
    finally:
        if token != token_before:
            garmin_cuenta.guardar(conn, person_id, token)
        _unlock(lock)


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
                pull_backfill_hosted(conn)  # prints its own job line (ruling 2)
                return 0
            if a.hoy:
                ok, espera, fuera, fallidas, vacios, ultima_persona = pull_hoy_todos(conn)
                print(f"hoy: {ok} ok, {espera} en espera, {fuera} fuera de horario, {fallidas} fallidas, "
                      f"{vacios} vacios{_sufijo_reloj(conn, ultima_persona)}",
                      flush=True)
                return 0
            pull_hosted(conn, fecha=a.date, n_days=a.days)  # prints its own job line (ruling 2)
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
    _sincronizar_lugares_seguro(conn, person_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
