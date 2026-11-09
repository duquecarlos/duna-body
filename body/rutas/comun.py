"""Shared request plumbing every route module needs (BODY-WEB-ESTRUCTURA-1): the Jinja
environment, the one-connection-per-request pattern, and the handful of module-level helpers two
or more route domains call. Never imports body.web -- web.py imports this module, and every
other route module reaches these names as comun.X, never through a from-import (a from-import
copies the binding, and a test's patch on this module would then miss it).

BODY-WEB-ESTRUCTURA-1-M: comun configures its own Jinja globals and the one contextvar they need,
once, when this module is imported -- never from body/web.py, which would re-run on every
throwaway re-import of body.web (tests/test_env_local_1.py's ImportarWebTest) and risk closing
over a second, separate copy of a name this module already owns. web.py's middleware sets and
resets `comun._RUTA_ACTUAL`; nothing else outside this module touches it.

BODY-WEB-ESTRUCTURA-7 gave this module its own `router` for the one route that belonged with its
own data rather than with any screen (`/sw.js`, next to the STATIC_HASH it serves) -- moved out
again in BODY-WEB-ESTRUCTURA-8 to body/rutas/pwa.py: plumbing should not itself hold a route.
_latest_day / _sync_status_text stay here, each read by two or more of Hoy, Día, Tendencias and
Turnos. TREND_KEYS / TREND_GROUPS move on from here too, re-exported under the same names from
body/catalogo_tendencias.py (BODY-AGENTE-TENDENCIAS-CATALOGO-1) -- that module holds no route, so
the assistant (body/agente/herramientas.py) can read the same catalogue without importing a route
module.
"""
from __future__ import annotations

import contextvars
import hashlib
import html
import json
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .. import cuenta, db, fechas, garmin_cuenta, tutoriales
from ..agente import cliente as agente_cliente
from ..catalogo_tendencias import TREND_GROUPS, TREND_KEYS
from ..i18n import T

HERE = Path(__file__).parent.parent

env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())

# ---- Current request path, for base.html's one in-context "¿Cómo se hace?" helper (tutoriales-v1
# section 5.2, decision 3) -- set from the raw ASGI path before routing, so no screen template
# needs to pass its own copy down to the shared base template.
_RUTA_ACTUAL: contextvars.ContextVar[str] = contextvars.ContextVar("_ruta_actual", default="")


def _static_hash(directory: Path) -> str:
    """sha256 over every file under `directory` (sorted relative path + bytes), first 10 hex chars --
    changes whenever any static file's content changes, so a cache keyed on it never goes stale."""
    h = hashlib.sha256()
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(directory)).replace("\\", "/").encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


STATIC_HASH = _static_hash(HERE / "static")


def static_url(path: str) -> str:
    return f"/static/{path}?v={STATIC_HASH}"


env.globals["static_url"] = static_url
env.globals["hosted"] = cuenta.hosted
env.globals["asistente_configurado"] = lambda: agente_cliente.proveedor_configurado() is not None
env.globals["tarea_en_contexto"] = lambda: tutoriales.tarea_en_ruta(_RUTA_ACTUAL.get())


# BODY-CONEXIONES-CERRADAS-1: _ctx() opens one connection per route call and stores it here so a
# route that calls _ctx() more than once (an early-return branch, then a fallback) still leaves
# exactly one connection to close. The close itself happens in _cierra_conexion[_async] below, which
# wraps the route function directly rather than a middleware or a separate FastAPI dependency --
# sqlite3 connections are thread-affine, and either of those could run its cleanup on a different
# worker thread than the one that opened the connection.
_REQUEST_CONN: contextvars.ContextVar = contextvars.ContextVar("_request_conn", default=None)


def _ctx(person_id: int) -> tuple:
    conn = _REQUEST_CONN.get()
    if conn is None:
        conn = db.connect()
        db.ensure_ready(conn)
        _REQUEST_CONN.set(conn)
    person = db.person(conn, person_id)
    return conn, person, T(person.get("language", "es"))


def _cierra_conexion(fn):
    """Decorates a sync route that calls _ctx(): closes whichever connection it opened, once the
    route function returns, in the same thread that opened it."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _REQUEST_CONN.set(None)
        try:
            return fn(*args, **kwargs)
        finally:
            conn = _REQUEST_CONN.get()
            if conn is not None:
                conn.close()
            _REQUEST_CONN.reset(token)
    return wrapper


def _cierra_conexion_async(fn):
    """Same as _cierra_conexion, for an `async def` route."""
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        token = _REQUEST_CONN.set(None)
        try:
            return await fn(*args, **kwargs)
        finally:
            conn = _REQUEST_CONN.get()
            if conn is not None:
                conn.close()
            _REQUEST_CONN.reset(token)
    return wrapper


def _render(name: str, **kw) -> HTMLResponse:
    return HTMLResponse(env.get_template(name).render(**kw))


def _short_date(t: T, d: date) -> str:
    """19 jun 2024 style: day, abbreviated month, year -- no weekday, unlike t.long_date. Shared by
    two or more route domains (BODY-WEB-ESTRUCTURA-2), so it lives here rather than with any one."""
    return f"{d.day} {t('date.months')[d.month - 1][:3]} {d.year}"


UNITS = {"bpm": "unit.bpm", "ms": "unit.ms", "pts": "unit.pts", "steps": "unit.steps", "kcal": "unit.kcal",
         "brpm": "unit.brpm", "ml/kg/min": "unit.ml_kg_min", "years": "unit.years", "%": "unit.percent"}
OF_100 = {"readiness", "stress_avg", "body_battery_high", "body_battery_low", "body_battery_wake", "sleep_score"}


def _is_minutes(key: str) -> bool:
    return key.endswith("_min") or key == "sleep_minutes"


def _value(t: T, key: str, v) -> str:
    if v is None:
        return "—"
    return t.minutes(v) if _is_minutes(key) else t.num(v)


def _unit(t: T, d: dict) -> str:
    if d["key"] in OF_100:
        return t("unit.of100")
    if _is_minutes(d["key"]):
        return ""
    return t(UNITS[d["unit"]]) if d["unit"] in UNITS else d["unit"]


def _fig_unit(t: T, key: str, v, defs: dict) -> str:
    """BODY-WEB-ESTRUCTURA-4: moved here with _value/_unit/_is_minutes/UNITS/OF_100 -- called by
    two or more domains still in web.py (goals, history/trends) as well as by the asistente domain
    moved out in this slice, so two-or-more-domains puts the whole cluster in comun.py."""
    val = _value(t, key, v)
    if val == "—":
        return val
    u = _unit(t, defs[key])
    return f"{val} {u}" if u else val


def _defs(conn) -> dict[str, dict]:
    """BODY-WEB-ESTRUCTURA-4: the canonical definition, reached everywhere as comun._defs(conn)."""
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM metric_def ORDER BY rowid")}


def _es_extra_metric(key: str) -> bool:
    """Whether `key` lives behind "Ver todas las medidas" on Tendencias -- called from Día
    (body/rutas/hoy.py's _tendencias_href) and from Turnos (body/rutas/turnos.py's
    _turnos_filas), so it lives here rather than with either (BODY-WEB-ESTRUCTURA-6)."""
    return any(key in keys for _, keys in TREND_GROUPS)


def _latest_day(conn, person_id: int) -> str | None:
    """BODY-WEB-ESTRUCTURA-7: called from Hoy's today() and from Tendencias' trends(), so it lives
    here rather than with either."""
    days = db.days_with_data(conn, person_id)
    return days[0] if days else None


def _sync_status_text(conn, person: dict, t: T) -> str:
    """An empty state never dead-ends (BODY-SIN-FRICCION-1): say the sync runs on its own and name the
    last successful sync's date, if one is recorded. Never invents a schedule time -- the actual time
    is a per-install Task Scheduler setting (scripts/install-daily-sync.ps1 -At) this app cannot read.
    In hosted mode, with no sync yet, WHEN data will arrive is knowable: a connected watch's first
    figures come from the --backfill timer within minutes, not the next day's pull (BODY-PRIMER-
    PULL-1), so a connected watch says so instead of just "it hasn't synced yet"; a watch not yet
    connected keeps that sentence and adds a link to connect one.

    BODY-WEB-ESTRUCTURA-7: called from Hoy's today(), Día's day_view() and Historial's
    history_view(), so it lives here rather than with any one of them."""
    ts = db.last_sync(conn, person["id"])
    if ts:
        tz = ZoneInfo(person.get("timezone") or "America/Bogota")
        last_date = datetime.fromisoformat(ts).astimezone(tz).date()
        return t("sync.runs") + " " + t("sync.last", date=_short_date(t, last_date))
    if cuenta.hosted():
        if garmin_cuenta.conectado(conn, person["id"]):
            return t("sync.hosted_connected")
        return Markup(
            html.escape(t("sync.runs") + " " + t("sync.none_yet")) +
            ' <a class="taplink" href="/garmin">' + html.escape(t("sync.connect_watch")) + "</a>"
        )
    return t("sync.runs") + " " + t("sync.none_yet")


async def _form(request: Request) -> dict:
    return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}


def _today_local(person: dict) -> date:
    """BODY-WEB-ESTRUCTURA-1: kept as a one-line wrapper over fechas.hoy_local rather than deleted --
    tests/test_turnos_opcional_3.py patches this clock for a whole TestCase whose other test methods
    call body/agente/herramientas.py's tools directly, which read fechas.hoy_local on their own
    (body/fechas.py's own docstring: web.py and herramientas.py each grew the same clock and both
    now delegate here). Patching fechas.hoy_local itself would mock that unrelated path too."""
    return fechas.hoy_local(person)


def _habit_cadence(h) -> dict:
    """BODY-WEB-ESTRUCTURA-5: called by two domains still apart -- Hoy's due-today card in web.py
    and Metas' goal detail in body/rutas/metas.py -- so it lives here rather than with either."""
    return json.loads(h["cadence"])


def _context_label(t: T, context: str | None) -> str | None:
    return t("habit.context." + context) if context else None


def _context_chip_cls(context: str | None) -> str:
    """DESIGN rule 7: a habit's context chip borrows a shift band's own family -- before/after a
    night shift keep the night tint (still about a night), but off_days is not a night and gets
    the off-day family the shift band and strip already use (`.shift.off`, `.strip-cell.off`)."""
    return "off" if context == "off_days" else "night"


def _habit_checked(conn, habit_id: int, person_id: int, day: str) -> bool:
    row = conn.execute("SELECT done FROM habit_check WHERE habit_id = ? AND person_id = ? AND day = ?",
                       (habit_id, person_id, day)).fetchone()
    return bool(row and row["done"])
