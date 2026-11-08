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
"""
from __future__ import annotations

import contextvars
import hashlib
from datetime import date
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import cuenta, db, fechas, tutoriales
from ..agente import cliente as agente_cliente
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
        db.init(conn)
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


def _turnos_on(conn, person_id: int) -> bool:
    """Whether every shift surface (BODY-TURNOS-OPCIONAL-3) shows for this person -- the nav tab, Hoy's
    card, Día's block line, Historial's por-tipo card, Tendencias' night shading, /shifts, Ayuda's
    chapter, and the assistant's shift tools. NULL (not answered) and False both hide it; only True
    shows it, computed once per request from the one predicate db.trabaja_turnos_si."""
    return db.trabaja_turnos_si(conn, person_id)


async def _form(request: Request) -> dict:
    return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}


def _today_local(person: dict) -> date:
    """BODY-WEB-ESTRUCTURA-1: kept as a one-line wrapper over fechas.hoy_local rather than deleted --
    tests/test_turnos_opcional_3.py patches this clock for a whole TestCase whose other test methods
    call body/agente/herramientas.py's tools directly, which read fechas.hoy_local on their own
    (body/fechas.py's own docstring: web.py and herramientas.py each grew the same clock and both
    now delegate here). Patching fechas.hoy_local itself would mock that unrelated path too."""
    return fechas.hoy_local(person)
