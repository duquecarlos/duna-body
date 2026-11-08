"""Body web app. Local only: http://127.0.0.1:8770 (add --lan to open it on the phone at home).

    python -m body.web
    python -m body.web --lan

Screens follow docs/DESIGN.md. Every word comes from body/i18n (es default, en full version).
Health values never go into URLs or logs (access log is off; URLs carry only dates).
"""
from __future__ import annotations

import argparse
import contextvars
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import (cuenta, db, dispositivo, entorno, garmin_adapter, garmin_cuenta, goals, history, household, identity,
               listen, patron_turnos, privacidad, profile, readiness, shift_import, turnos_costo, tutoriales,
               vida_export)
from .agente import ajustes as agente_ajustes
from .agente import bucle as agente_bucle
from .agente import cliente as agente_cliente
from .agente import conversacion as agente_conversacion
from .agente import herramientas as agente_herramientas
from .agente import propuestas as agente_propuestas
from .agente import uso as agente_uso
from .baselines import baseline
from .i18n import DEFAULT as DEFAULT_LANG
from .i18n import LANGS, T
from .identity import ConsentRequired, DispositivoNoAutorizado, DispositivoSinAlcance, LoginRequired
from .request_log import RequestLogMiddleware

HERE = Path(__file__).parent


@asynccontextmanager
async def _lifespan(app: FastAPI):
    cuenta.ensure_configured()
    garmin_cuenta.ensure_configured()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

# ---- Current request path, for base.html's one in-context "¿Cómo se hace?" helper (tutoriales-v1
# section 5.2, decision 3) -- set from the raw ASGI path before routing, so no screen template
# needs to pass its own copy down to the shared base template.
_RUTA_ACTUAL: contextvars.ContextVar[str] = contextvars.ContextVar("_ruta_actual", default="")


class _CaminoActualMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _RUTA_ACTUAL.set(scope.get("path", ""))
        try:
            await self.app(scope, receive, send)
        finally:
            _RUTA_ACTUAL.reset(token)


app.add_middleware(_CaminoActualMiddleware)
app.add_middleware(RequestLogMiddleware)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())


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


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired) -> RedirectResponse:
    qs = f"return_to={quote(exc.return_to, safe='')}"
    if exc.prompt:
        qs += f"&prompt={exc.prompt}"
    return RedirectResponse(f"/login?{qs}", status_code=303)


@app.exception_handler(ConsentRequired)
def _consent_required(request: Request, exc: ConsentRequired) -> RedirectResponse:
    return RedirectResponse(
        f"/privacidad/consentimiento?return_to={quote(exc.return_to, safe='')}", status_code=303
    )


@app.exception_handler(DispositivoNoAutorizado)
def _dispositivo_no_autorizado(request: Request, exc: DispositivoNoAutorizado) -> JSONResponse:
    return JSONResponse({"error": "no_autorizado"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


@app.exception_handler(DispositivoSinAlcance)
def _dispositivo_sin_alcance(request: Request, exc: DispositivoSinAlcance) -> JSONResponse:
    return JSONResponse({"error": "alcance"}, status_code=403)


_CUENTA_DOWN_TEMPLATE = env.from_string(
    '{% extends "base.html" %}{% block main %}'
    '<div class="top"><div><h1>{{ t("app.name") }}</h1></div></div>'
    '<div class="card"><p class="read">{{ t("cuenta.down") }}</p></div>'
    "{% endblock %}"
)


def _cuenta_down_page() -> HTMLResponse:
    return HTMLResponse(_CUENTA_DOWN_TEMPLATE.render(t=T(DEFAULT_LANG), tab=None))

UNITS = {"bpm": "unit.bpm", "ms": "unit.ms", "pts": "unit.pts", "steps": "unit.steps", "kcal": "unit.kcal",
         "brpm": "unit.brpm", "ml/kg/min": "unit.ml_kg_min", "years": "unit.years"}
OF_100 = {"readiness", "stress_avg", "body_battery_high", "body_battery_low", "body_battery_wake", "sleep_score"}
TODAY_CARDS = ["sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake"]
STAGES = [("sleep_deep_min", "metric.sleep_deep_min", "var(--night)"),
          ("sleep_light_min", "metric.sleep_light_min", "color-mix(in srgb, var(--night) 45%, var(--card))"),
          ("sleep_rem_min", "metric.sleep_rem_min", "var(--body)"),
          ("sleep_awake_min", "metric.sleep_awake_min", "var(--amber)")]


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


def _turnos_on(conn, person_id: int) -> bool:
    """Whether every shift surface (BODY-TURNOS-OPCIONAL-3) shows for this person -- the nav tab, Hoy's
    card, Día's block line, Historial's por-tipo card, Tendencias' night shading, /shifts, Ayuda's
    chapter, and the assistant's shift tools. NULL (not answered) and False both hide it; only True
    shows it, computed once per request from the one predicate db.trabaja_turnos_si."""
    return db.trabaja_turnos_si(conn, person_id)


async def _form(request: Request) -> dict:
    return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}


def _today_local(person: dict) -> date:
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    return datetime.now(tz).date()


def _week_monday(d: date) -> date:
    return d - timedelta(d.weekday())


def _defs(conn) -> dict[str, dict]:
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM metric_def ORDER BY rowid")}


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


def _delta(conn, t: T, d: dict, day: str, v, person_id: int) -> tuple[str, str]:
    """Delta vs your 30-day average: text and css class (good/bad/flat)."""
    if v is None:
        return "", "flat"
    b = baseline(conn, d["key"], day, person_id=person_id)
    if b is None:
        return "", "flat"
    diff = v - b.mean
    z = b.z(v)
    if z is None or abs(z) < 0.5:
        return t("delta.same"), "flat"
    arrow = "↑" if diff > 0 else "↓"
    amount = t.minutes(abs(diff)) if _is_minutes(d["key"]) else t.num(abs(diff))
    better = d["better"]
    cls = "flat" if better == "neutral" else ("good" if (diff > 0) == (better == "higher") else "bad")
    return f"{arrow} {amount} {t('delta.vs')}", cls


def _latest_day(conn, person_id: int) -> str | None:
    days = db.days_with_data(conn, person_id)
    return days[0] if days else None


def _reading(t: T, r: readiness.Readiness) -> str:
    if not r.parts:
        return ""
    weakest = min(r.parts, key=lambda p: p.subscore)
    strongest = max(r.parts, key=lambda p: p.subscore)
    out = t("today.all_normal") if weakest.subscore >= 60 else t("today.weakest", part=t("part." + weakest.key))
    if strongest.subscore >= 80 and strongest is not weakest:
        out += " " + t("today.strongest", part=t("part." + strongest.key))
    return out


def _synced(conn, person) -> str | None:
    ts = db.last_sync(conn, person["id"])
    if not ts:
        return None
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    local = datetime.fromisoformat(ts).astimezone(tz)
    return local.strftime("%H:%M") if local.date() == datetime.now(tz).date() else None


def _sync_status_text(conn, person: dict, t: T) -> str:
    """An empty state never dead-ends (BODY-SIN-FRICCION-1): say the sync runs on its own and name the
    last successful sync's date, if one is recorded. Never invents a schedule time -- the actual time
    is a per-install Task Scheduler setting (scripts/install-daily-sync.ps1 -At) this app cannot read.
    In hosted mode, with no sync yet, WHEN data will arrive is knowable: a connected watch's first
    figures come from the --backfill timer within minutes, not the next day's pull (BODY-PRIMER-
    PULL-1), so a connected watch says so instead of just "it hasn't synced yet"; a watch not yet
    connected keeps that sentence and adds a link to connect one."""
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


def _parse_day(day: str) -> date:
    try:
        return date.fromisoformat(day)
    except ValueError:
        raise HTTPException(404)


def _batch_date(t: T, received: str) -> str:
    return _short_date(t, datetime.fromisoformat(received).date())


@app.get("/healthz")
def healthz():
    return Response("ok", media_type="text/plain")


@app.get("/login")
@app.get("/login/cuenta/start")
def login_start(request: Request):
    if not cuenta.hosted():
        raise HTTPException(404)
    return_to = cuenta.safe_return_to(request.query_params.get("return_to"))
    prompt = request.query_params.get("prompt")
    prompt = prompt if prompt in ("none", "login") else None
    max_age = 600 if request.query_params.get("max_age") == "600" else None
    conn = db.connect()
    db.init(conn)
    try:
        url, flujo_id = cuenta.start_login(conn, return_to, prompt=prompt, max_age=max_age)
    finally:
        conn.close()
    resp = RedirectResponse(url, status_code=303)
    cuenta.set_flujo_cookie(resp, flujo_id)
    return resp


@app.get("/login/cuenta/callback")
def login_callback(request: Request):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn = db.connect()
    db.init(conn)
    try:
        result = cuenta.handle_callback(conn, request.cookies.get(cuenta.FLUJO_COOKIE),
                                        dict(request.query_params))
    except cuenta.FlowRefused:
        conn.close()
        return HTMLResponse(env.get_template("acceso_rechazado.html").render(t=T(DEFAULT_LANG)),
                            status_code=400)
    except cuenta.CuentaUnavailable:
        conn.close()
        return _cuenta_down_page()
    if result.kind == "reauth":
        conn.close()
        return RedirectResponse(f"/login?return_to={quote(result.return_to or '/', safe='')}",
                                status_code=303)
    if result.kind == "sin_acceso":
        conn.close()
        return _render("sin_acceso.html", t=T(DEFAULT_LANG),
                       switcher_url=cuenta.SWITCHER_URL, logout_url=cuenta.logout_url(None))
    conn.close()
    resp = RedirectResponse(result.return_to or "/", status_code=303)
    cuenta.set_session_cookie(resp, result.session_token)
    cuenta.clear_flujo_cookie(resp)
    return resp


@app.post("/logout")
def logout(request: Request):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn = db.connect()
    db.init(conn)
    sid = cuenta.end_session(conn, request.cookies.get(cuenta.SESSION_COOKIE))
    conn.close()
    resp = RedirectResponse(cuenta.logout_url(sid), status_code=303)
    cuenta.clear_session_cookie(resp)
    return resp


_BACKCHANNEL_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-forwarded-host", "x-real-ip")


@app.post("/login/cuenta/logout-token")
async def backchannel_logout(request: Request):
    if not cuenta.hosted():
        raise HTTPException(404)
    host = request.client.host if request.client else None
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(403)
    if any(h in request.headers for h in _BACKCHANNEL_PROXY_HEADERS):
        # Behind Caddy, a proxied request also arrives from 127.0.0.1 -- the client-address
        # check alone is not enough. Caddy's reverse_proxy adds X-Forwarded-For.
        raise HTTPException(404)
    f = await _form(request)
    conn = db.connect()
    db.init(conn)
    ok = cuenta.handle_backchannel_logout(conn, f.get("logout_token", ""))
    conn.close()
    return Response(status_code=200 if ok else 400)


def _hoy_turno_ctx(t: T, conn, person_id: int, today_local: date) -> dict:
    """Hoy's second promise (BODY-HOY-TURNO-1): today's shift, the next one, and a recovery line --
    one vocabulary with Turnos' strip (shift.kind.*, the same band classes)."""
    turno = turnos_costo.turno_de_hoy(conn, person_id, today_local)
    hoy, siguiente, recuperacion = turno["hoy"], turno["siguiente"], turno["recuperacion"]

    hoy_texto = None
    if hoy is not None:
        palabra = t("shift.kind." + hoy["kind"])
        if hoy["start_local"] and hoy["end_local"]:
            hoy_texto = t("today.shift_today_hours", turno=palabra,
                         inicio=hoy["start_local"][11:16], fin=hoy["end_local"][11:16])
        else:
            hoy_texto = t("today.shift_today", turno=palabra)

    siguiente_texto = None
    if siguiente is not None and siguiente["start_local"]:
        palabra = t("shift.kind." + siguiente["kind"])
        fecha = _short_weekday_date(t, date.fromisoformat(siguiente["start_local"][:10]))
        if siguiente["end_local"]:
            siguiente_texto = t("today.shift_next_hours", turno=palabra, fecha=fecha,
                                inicio=siguiente["start_local"][11:16], fin=siguiente["end_local"][11:16])
        else:
            siguiente_texto = t("today.shift_next", turno=palabra, fecha=fecha)

    recuperacion_texto = None
    if recuperacion is not None:
        recuperacion_texto = t("today.recovery", dia=recuperacion["dia"], estimado=recuperacion["estimado"])

    return {"clase": hoy["kind"] if hoy is not None else None, "hoy_texto": hoy_texto,
            "siguiente_texto": siguiente_texto, "recuperacion_texto": recuperacion_texto}


@app.get("/")
@_cierra_conexion
def today(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    turnos_on = _turnos_on(conn, person_id)
    day = _latest_day(conn, person_id)
    proposed_count = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch WHERE person_id = ? AND status = 'proposed'",
        (person_id,),
    ).fetchone()["n"]
    primeros = tutoriales.primeros_pasos(conn, person_id, cuenta.hosted())
    primeros_pasos = primeros if any(not p["hecho"] for p in primeros) else None
    today_local = _today_local(person)
    turno_ctx = _hoy_turno_ctx(t, conn, person_id, today_local)
    if not day:
        return _render("soon.html", t=t, tab="today", title=t("today.title"),
                       text=_sync_status_text(conn, person, t), primeros_pasos=primeros_pasos,
                       proposed_count=proposed_count, turno=turno_ctx, turnos=turnos_on)
    defs = _defs(conn)
    vals = db.day_values(conn, day, person_id)
    r = readiness.compute(conn, day, person_id)
    rb = baseline(conn, "readiness", day, person_id=person_id)
    ready_avg = ready_delta = None
    ready_cls = "flat"
    if r.score is not None and rb:
        ready_avg = round(rb.mean)
        ready_delta, ready_cls = _delta(conn, t, defs["readiness"], day, r.score, person_id)
    cards = []
    for k in TODAY_CARDS:
        v = vals.get(k)
        delta, cls = _delta(conn, t, defs[k], day, v, person_id)
        cards.append({"key": k, "label": t("metric." + k), "value": _value(t, k, v),
                      "unit": _unit(t, defs[k]) if v is not None else "", "delta": delta, "cls": cls})
    return _render("today.html", t=t, tab="today", day=day, date_long=t.long_date(date.fromisoformat(day)),
                   synced=_synced(conn, person), r=r, reading=_reading(t, r), ready_avg=ready_avg,
                   ready_delta=ready_delta, ready_cls=ready_cls, cards=cards,
                   missing_txt=", ".join(t("part." + m) for m in r.missing), proposed_count=proposed_count,
                   turno=turno_ctx, turnos=turnos_on,
                   plan_habits=_today_due_habits(conn, t, person_id, today_local),
                   today_iso=today_local.isoformat(),
                   primeros_pasos=primeros_pasos)


# readiness.Part.key -> the Tendencias metric it reads from (readiness.Part carries no metric key of its
# own). BODY-PULIDO-1: how()'s part rows link to their own measure through this.
PART_METRIC = {"hrv": "hrv_last_night", "sleep": "sleep_minutes", "battery": "body_battery_wake",
               "resting_hr": "resting_hr", "stress": "stress_avg"}


@app.get("/how/{day}")
@_cierra_conexion
def how(day: str, person_id: int = Depends(identity.current_person_id)):
    d = _parse_day(day)
    conn, person, t = _ctx(person_id)
    turnos_on = _turnos_on(conn, person_id)
    today_local = _today_local(person)
    r = readiness.compute(conn, d.isoformat(), person_id)
    parts = []
    for p in r.parts:
        ref_note = ""
        if p.key == "sleep":
            value, ref, ref_note = t.minutes(p.value), t.minutes(p.reference), t("how.need_short")
        else:
            value, ref = t.num(p.value), (t.num(p.reference) if p.reference is not None else "—")
        label = t("part." + p.key)
        metric_key = PART_METRIC.get(p.key)
        href = (_tendencias_href(metric_key, "como", d.isoformat(), today_local)
                if metric_key and p.value is not None else None)
        parts.append({"label": label[:1].upper() + label[1:], "value": value, "ref": ref, "ref_note": ref_note,
                      "score": t.num(p.subscore), "weight": f"{round(p.weight * 100)} %", "href": href})
    missing = [t("part." + m) for m in r.missing]
    return _render("how.html", t=t, tab="today", r=r, parts=parts, date_long=t.long_date(d),
                   missing=[m[:1].upper() + m[1:] for m in missing], turnos=turnos_on)


@app.get("/day/{day}")
@_cierra_conexion
def day_view(day: str, person_id: int = Depends(identity.current_person_id)):
    d = _parse_day(day)
    conn, person, t = _ctx(person_id)
    turnos_on = _turnos_on(conn, person_id)
    today_local = _today_local(person)
    key = d.isoformat()
    data = db.load_day(conn, key, person_id)
    vals = {m["key"]: m["value"] for m in data["metrics"]}
    defs = _defs(conn)
    cards = []
    for k, df in defs.items():
        v = vals.get(k)
        delta, cls = _delta(conn, t, df, key, v, person_id)
        href = _tendencias_href(k, "dia", key, today_local) if v is not None else None
        cards.append({"key": k, "label": t("metric." + k), "value": _value(t, k, v), "raw": v,
                      "unit": _unit(t, df), "delta": delta, "cls": cls, "est": df["kind"] == "estimated",
                      "href": href})
    sleep = None
    if vals.get("sleep_minutes") is not None:
        total_stage = sum(vals.get(s[0]) or 0 for s in STAGES)
        stages = [{"label": t(lbl), "value": t.minutes(vals[k]), "color": col,
                   "pct": round(100 * vals[k] / total_stage, 1)}
                  for k, lbl, col in STAGES if vals.get(k)] if total_stage else []
        win = data["sleep"] or {}
        window = " → ".join(x[11:16] for x in (win.get("start_local"), win.get("end_local")) if x)
        sleep = {"total": t.minutes(vals["sleep_minutes"]), "window": window, "stages": stages}
    acts = []
    for a in data["activities"]:
        bits = []
        if a["duration_s"]:
            bits.append(t.minutes(a["duration_s"] / 60))
        if a["distance_m"]:
            bits.append(f"{t.num(a['distance_m'] / 1000, 1)} km")
        if a["avg_hr"]:
            bits.append(f"{t.num(a['avg_hr'])} {t('unit.bpm')}")
        acts.append({"name": a["name"] or a["type"] or "—", "detail": " · ".join(bits)})
    shifts = []
    for s in data["shifts"]:
        kind = s["kind"]
        key = "shift.kind." + kind
        label = t(key)
        if label == key:
            label = kind
        shifts.append({"kind": kind, "text": f"{label}{' (' + s['code'] + ')' if s['code'] else ''} "
                       f"{(s['start_local'] or '')[11:16]} → {(s['end_local'] or '')[11:16]}"})
    has_data = any(v is not None for v in vals.values())
    ctx = turnos_costo.contexto_dia(conn, person_id, d)
    block_ctx = None
    if ctx is not None:
        if ctx["tipo"] == "noche":
            block_ctx = t("day.shift_ctx_noche", n=ctx["n"], de=ctx["de"])
        else:
            block_ctx = t("day.shift_ctx_despues", n=ctx["n"])
    return _render("day.html", t=t, tab="today", date_long=t.long_date(d), cards=cards, sleep=sleep,
                   activities=acts, shifts=shifts, has_data=has_data, block_ctx=block_ctx,
                   sync_text=None if has_data else _sync_status_text(conn, person, t),
                   prev=(d - timedelta(1)).isoformat(), next=(d + timedelta(1)).isoformat(),
                   turnos=turnos_on)


def _owned_batch_or_404(conn, batch_id: int, person_id: int) -> None:
    if not conn.execute("SELECT 1 FROM shift_batch WHERE id = ? AND person_id = ?",
                        (batch_id, person_id)).fetchone():
        raise HTTPException(404)


_STRIP_WEEKS = 8
_STRIP_DAYS = _STRIP_WEEKS * 7
_STRIP_KIND_ORDER = ["morning", "afternoon", "night", "day", "off", "other", "none"]


def _es_extra_metric(key: str) -> bool:
    """Whether `key` lives behind "Ver todas las medidas" on Tendencias (TREND_GROUPS, defined below --
    resolved at call time, so the order of definitions in this file doesn't matter)."""
    return any(key in keys for _, keys in TREND_GROUPS)


def _rango_que_contiene(dia: date, today_local: date) -> str:
    """The Tendencias range whose window actually contains `dia`, relative to the person's own today
    (BODY-PULIDO-1-M: r=30 opened for a day outside that 30-day window). Within the last 30 days -> r=30;
    within 90 -> r=90; older -> no r at all, the monthly/all-time view that reaches any day."""
    age = (today_local - dia).days
    if age <= 30:
        return "r=30"
    if age <= 90:
        return "r=90"
    return ""


def _tendencias_href(key: str, desde: str, dia: str, today_local: date) -> str | None:
    """A measure's own Tendencias card, with a way back to where the tap came from -- Dia's cards and
    Como lo calculo's part rows (BODY-PULIDO-1), same shape as Turnos' cost rows. None when `key` has
    no card there (TREND_KEYS or an extra group), so the caller leaves the figure unlinked."""
    if key not in TREND_KEYS and not _es_extra_metric(key):
        return None
    todas = "&todas=1" if _es_extra_metric(key) else ""
    rango = _rango_que_contiene(date.fromisoformat(dia), today_local)
    r_part = f"{rango}&" if rango else ""
    return f"/trends?{r_part}desde={desde}&dia={dia}{todas}#{key}"


def _turnos_filas(t: T, defs: dict, resumen: dict) -> list[dict]:
    filas = []
    for key in turnos_costo.MODELO["metricas"]:
        v = resumen["costo_promedio"].get(key)
        d = defs.get(key, {})
        if v is None:
            filas.append({"label": t("metric." + key), "sub": t("shifts.cost_sub." + key),
                         "value": None, "palabra": None, "href": None})
            continue
        diff, palabra = turnos_costo.diferencia_y_palabra(key, v)
        sign = "+" if diff >= 0 else "−"
        amount = t.minutes(abs(diff)) if _is_minutes(key) else t.num(abs(diff), 1)
        unit = _unit(t, d)
        href = f"/trends?r=90&desde=turnos{'&todas=1' if _es_extra_metric(key) else ''}#{key}"
        filas.append({"label": t("metric." + key), "sub": t("shifts.cost_sub." + key),
                     "value": f"{sign}{amount}" + (f" {unit}" if unit else ""),
                     "palabra": t("shifts.word." + palabra), "href": href})
    return filas


def _turnos_strip(conn, person_id: int, today_local: date, t: T) -> tuple[list[dict], list[dict]]:
    start = today_local - timedelta(_STRIP_DAYS - 1)
    rows = conn.execute(
        "SELECT day, kind FROM shift WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, start.isoformat(), today_local.isoformat()),
    ).fetchall()
    kind_by_day = {r["day"]: r["kind"] for r in rows}
    strip = []
    present: set[str] = set()
    for i in range(_STRIP_DAYS):
        d = (start + timedelta(i)).isoformat()
        kind = kind_by_day.get(d) or "none"
        present.add(kind)
        score = readiness.compute(conn, d, person_id).score
        # long_date (weekday, day, month -- no year) rather than _short_date: a cell's own day is
        # always within the last 8 weeks, so the year adds nothing and only risks reading the same
        # as an unrelated batch's received date elsewhere on the page.
        fecha = t.long_date(date.fromisoformat(d))
        turno = t("shift.kind." + kind)
        label = (t("shifts.strip_cell", fecha=fecha, turno=turno, n=score) if score is not None
                 else t("shifts.strip_cell_sin_dato", fecha=fecha, turno=turno))
        strip.append({"day": d, "band": kind, "score": score, "label": label})
    legend = [{"kind": k, "label": t("shift.kind." + k)} for k in _STRIP_KIND_ORDER if k in present]
    return strip, legend


@app.get("/shifts")
@_cierra_conexion
def shifts(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    turnos_on = _turnos_on(conn, person_id)
    if not turnos_on:
        raise HTTPException(404)
    rows = conn.execute(
        "SELECT * FROM shift_batch WHERE person_id = ? AND status = 'proposed' ORDER BY received, extracted_at",
        (person_id,),
    ).fetchall()
    result_msg = None
    qp = request.query_params
    if qp.get("done") == "1":
        kept = int(qp.get("kept_manual", 0)) + int(qp.get("kept_newer", 0))
        result_msg = t("shifts.confirm_all_result", batches=qp.get("batches", "0"), applied=qp.get("applied", "0"))
        if kept:
            result_msg += " " + t("shifts.confirm_all_kept", n=kept)
    today_local = _today_local(person)
    resumen = turnos_costo.resumen(conn, person_id, hoy=today_local)
    strip, strip_legend = _turnos_strip(conn, person_id, today_local, t)
    costo_ctx = {"hero_dias": resumen["dias_recuperacion"], "motivo": resumen["motivo"],
                 "filas": _turnos_filas(t, _defs(conn), resumen), "strip": strip, "strip_legend": strip_legend}
    tiene_patron = patron_turnos.leer(conn, person_id) is not None
    if len(rows) > 1:
        summary = shift_import.summary_proposed(conn, person_id)
        batches = [
            {"batch": dict(b), "received_date": _batch_date(t, b["received"]),
             "preview": _patron_preview_ctx(t, shift_import.preview(conn, b["id"]))}
            for b in rows
        ]
        many_mixed = any(b["kind"] == "pattern" for b in rows)
        return _render("shifts.html", t=t, tab="shifts", batch=None, many=True, batches=batches,
                       many_mixed=many_mixed,
                       summary_from=t.month_year(date.fromisoformat(summary["first_day"])),
                       summary_to=t.month_year(date.fromisoformat(summary["last_day"])),
                       summary_days=summary["day_count"], summary_nights=summary["night_count"],
                       result_msg=result_msg, turnos=turnos_on, tiene_patron=tiene_patron, **costo_ctx)
    if rows:
        proposed = rows[0]
        prev = _patron_preview_ctx(t, shift_import.preview(conn, proposed["id"]))
        return _render("shifts.html", t=t, tab="shifts", batch=dict(proposed), many=False,
                       received_date=_batch_date(t, proposed["received"]), preview=prev, result_msg=result_msg,
                       turnos=turnos_on, tiene_patron=tiene_patron, **costo_ctx)
    confirmed = shift_import.latest_confirmed(conn, person_id)
    return _render("shifts.html", t=t, tab="shifts", batch=None, many=False,
                   confirmed_date=_batch_date(t, confirmed["received"]) if confirmed else None,
                   result_msg=result_msg, turnos=turnos_on, tiene_patron=tiene_patron, **costo_ctx)


def _patron_preview_ctx(t: T, prev: dict) -> dict:
    """Adds the short from/to dates a pattern batch's card reads ("del X al Y") to shift_import.preview()'s
    own dict, without changing its existing keys."""
    return {**prev,
            "from_date": _short_date(t, date.fromisoformat(prev["first_day"])) if prev["first_day"] else None,
            "to_date": _short_date(t, date.fromisoformat(prev["last_day"])) if prev["last_day"] else None}


def _turnos_bloque_linea(t: T, b: dict, hoy: date) -> str:
    inicio = _short_date(t, date.fromisoformat(b["inicio"]))
    fin_dt = date.fromisoformat(b["fin"])
    fin = _short_date(t, fin_dt)
    if b["recuperacion_dias"] is not None:
        recuperacion = t("shifts.como_bloque_dias", n=b["recuperacion_dias"])
    else:
        ventana_post = turnos_costo.MODELO["ventana_post"]
        ultimo_dia = fin_dt + timedelta(ventana_post + turnos_costo.MODELO["dias_estables"] - 1)
        recuperacion = (t("shifts.sin_dato_corto") if ultimo_dia > hoy
                        else t("shifts.como_bloque_mas_de", n=ventana_post))
    return t("shifts.como_bloque", inicio=inicio, fin=fin, dias=b["dias"], recuperacion=recuperacion)


@app.get("/shifts/como")
@_cierra_conexion
def shifts_como(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    if not _turnos_on(conn, person_id):
        raise HTTPException(404)
    today_local = _today_local(person)
    resumen = turnos_costo.resumen(conn, person_id, hoy=today_local)
    bloques = [_turnos_bloque_linea(t, b, today_local) for b in resumen["bloques"]]
    return _render("how.html", t=t, tab="shifts", modo="turnos", version=resumen["version"],
                   n_bloques=len(resumen["bloques"]), n_con_recuperacion=resumen["bloques_con_recuperacion"],
                   n_estables=turnos_costo.MODELO["dias_estables"], n_referencia=turnos_costo.REFERENCIA_DIAS,
                   bloques=bloques, turnos=True)


@app.post("/shifts/confirm")
@_cierra_conexion_async
async def shifts_confirm(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    if not _turnos_on(conn, person_id):
        raise HTTPException(404)
    batch_id = parse_qs((await request.body()).decode()).get("batch_id", [""])[0]
    if batch_id.isdigit():
        _owned_batch_or_404(conn, int(batch_id), person_id)
        try:
            shift_import.confirm(conn, int(batch_id))
        except (shift_import.ImportRefused, shift_import.SlotIndexMissing):
            pass
    return RedirectResponse("/shifts", status_code=303)


@app.post("/shifts/confirm-all")
@_cierra_conexion_async
async def shifts_confirm_all(person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    if not _turnos_on(conn, person_id):
        raise HTTPException(404)
    try:
        result = shift_import.confirm_all(conn, person_id)
    except (shift_import.ImportRefused, shift_import.SlotIndexMissing):
        return RedirectResponse("/shifts", status_code=303)
    return RedirectResponse(
        f"/shifts?done=1&batches={result['batches']}&applied={result['days_applied']}"
        f"&kept_manual={result['days_kept_manual']}&kept_newer={result['days_kept_newer']}",
        status_code=303,
    )


@app.post("/shifts/dismiss")
@_cierra_conexion_async
async def shifts_dismiss(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    if not _turnos_on(conn, person_id):
        raise HTTPException(404)
    batch_id = parse_qs((await request.body()).decode()).get("batch_id", [""])[0]
    if batch_id.isdigit():
        _owned_batch_or_404(conn, int(batch_id), person_id)
        shift_import.dismiss(conn, int(batch_id))
    return RedirectResponse("/shifts", status_code=303)


_PATRON_DEFAULT_DIAS = 7
_PATRON_DEFAULT_HORIZONTE = 8


def _patron_dias(raw: str | None, guardado: dict | None) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = len(guardado["cycle"]) if guardado else _PATRON_DEFAULT_DIAS
    return max(1, min(56, n))


def _patron_filas(t: T, cycle: list[dict], start_day: date, errores: dict[int, str] | None = None) -> list[dict]:
    errores = errores or {}
    filas = []
    for i, entry in enumerate(cycle):
        codigo = errores.get(i)
        filas.append({
            "idx": i,
            "n": i + 1,
            "label": t("patron.dia_label", n=i + 1, fecha=t.long_date(start_day + timedelta(i))),
            "kind": entry.get("kind") or "off",
            "start": entry.get("start") or "",
            "end": entry.get("end") or "",
            "error": t("patron.error." + codigo) if codigo else None,
        })
    return filas


@app.get("/shifts/patron")
@_cierra_conexion
def shifts_patron(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    if not _turnos_on(conn, person_id):
        raise HTTPException(404)
    guardado = patron_turnos.leer(conn, person_id)
    dias = _patron_dias(request.query_params.get("dias"), guardado)
    start_day = date.fromisoformat(guardado["start_day"]) if guardado else _today_local(person)
    horizon_weeks = guardado["horizon_weeks"] if guardado else _PATRON_DEFAULT_HORIZONTE
    cycle = guardado["cycle"] if guardado and len(guardado["cycle"]) == dias else (
        [{"kind": "off", "start": None, "end": None}] * dias)
    return _render("shift_pattern.html", t=t, tab="shifts", turnos=True, dias=dias,
                   start_day=start_day.isoformat(), horizon_weeks=horizon_weeks,
                   filas=_patron_filas(t, cycle, start_day), tiene_patron=guardado is not None)


@app.post("/shifts/patron")
@_cierra_conexion_async
async def shifts_patron_save(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    if not _turnos_on(conn, person_id):
        raise HTTPException(404)
    f = await _form(request)
    dias = _patron_dias(f.get("dias"), None)
    try:
        start_day = date.fromisoformat(f.get("start_day") or "")
    except ValueError:
        start_day = _today_local(person)
    try:
        horizon_weeks = max(1, min(26, int(f.get("horizon_weeks", ""))))
    except ValueError:
        horizon_weeks = _PATRON_DEFAULT_HORIZONTE

    cycle = []
    for i in range(dias):
        kind = f.get(f"kind_{i}") or "off"
        if kind == "off":
            cycle.append({"kind": "off", "start": None, "end": None})
        else:
            cycle.append({"kind": kind, "start": f.get(f"start_{i}") or None, "end": f.get(f"end_{i}") or None})

    try:
        patron_turnos.validar(cycle)
    except patron_turnos.PatronInvalido as e:
        errores = {err["index"]: err["code"] for err in e.errors if err["index"] is not None}
        return _render("shift_pattern.html", t=t, tab="shifts", turnos=True, dias=dias,
                       start_day=start_day.isoformat(), horizon_weeks=horizon_weeks,
                       filas=_patron_filas(t, cycle, start_day, errores),
                       tiene_patron=patron_turnos.leer(conn, person_id) is not None)

    patron_turnos.guardar(conn, person_id, start_day.isoformat(), cycle, horizon_weeks)
    patron_turnos.proponer(conn, person_id, _today_local(person))
    return RedirectResponse("/shifts", status_code=303)


@app.post("/shifts/patron/quitar")
@_cierra_conexion_async
async def shifts_patron_quitar(person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    if not _turnos_on(conn, person_id):
        raise HTTPException(404)
    patron_turnos.quitar(conn, person_id)
    for r in conn.execute(
        "SELECT id FROM shift_batch WHERE person_id = ? AND status = 'proposed' AND kind = 'pattern'",
        (person_id,),
    ).fetchall():
        shift_import.dismiss(conn, r["id"])
    return RedirectResponse("/shifts", status_code=303)


TREND_KEYS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake", "steps",
              "vo2max", "vo2max_cycling", "fitness_age"]
WEEK_KEYS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr"]
TREND_MIN_DAYS = 14  # body/profile.py's metric_profile() needs this many rows before a metric gets a card
SPARSE_KEYS = {"vo2max", "vo2max_cycling", "fitness_age"}  # Garmin updates these only after qualifying activities

# "Ver todas las medidas" (DESIGN rule 8): the rest of the catalogue, grouped by plain-word topic. The single
# source both the page and tests/test_tendencias_todas.py read -- a key here that is not a METRIC_DEFS key
# fails the test, on purpose. sleep_need_min (Garmin's own target) is deliberately never shown here.
TREND_GROUPS = [
    ("sleep", ["sleep_score", "sleep_deep_min", "sleep_rem_min", "sleep_light_min", "sleep_awake_min"]),
    ("energy", ["body_battery_high", "body_battery_low", "body_battery_sleep_charge"]),
    ("stress", ["stress_avg", "high_stress_min"]),
    ("activity", ["intensity_minutes", "active_kcal"]),
    ("breathing", ["respiration_awake"]),
]


def _trend_days_have(conn, person_id: int) -> int:
    """How many days the best-covered trend metric already has, for the honest-gap message below
    TREND_MIN_DAYS -- a count, never a figure (BODY-SIN-FRICCION-1)."""
    placeholders = ",".join("?" * len(TREND_KEYS))
    rows = conn.execute(
        f"SELECT COUNT(*) AS n FROM daily_metric WHERE person_id = ? AND metric_key IN ({placeholders}) "
        "GROUP BY metric_key",
        (person_id, *TREND_KEYS),
    ).fetchall()
    return max((r["n"] for r in rows), default=0)


RANGES = ["7", "30", "90", "all"]


def _compare(t: T, k: str, d: dict, value, ref, spread: float, vs_key: str) -> tuple[str, str]:
    if value is None or ref is None:
        return "", "flat"
    diff = value - ref
    if abs(diff) < (spread or 1) * 0.15:
        return t("delta.same"), "flat"
    amount = t.minutes(abs(diff)) if _is_minutes(k) else t.num(abs(diff))
    cls = "flat" if d["better"] == "neutral" else ("good" if (diff > 0) == (d["better"] == "higher") else "bad")
    return f"{'↑' if diff > 0 else '↓'} {amount} {t(vs_key)}", cls


def _nights(conn, start: date, end: date, person_id: int) -> set[str]:
    return {r["day"] for r in conn.execute("SELECT day FROM shift WHERE person_id = ? AND kind = 'night' AND day "
                                           "BETWEEN ? AND ?", (person_id, start.isoformat(), end.isoformat()))}


def _trend_card(conn, t: T, defs: dict, k: str, start, end, month, nights, person_id: int):
    """One card -- own baseline, honest gaps, night shading, the month tap -- built the same way for the
    default 9 and every extra metric behind "Ver todas las medidas" (DESIGN rules 3, 6, 7)."""
    p = profile.metric_profile(conn, k, person_id=person_id)
    if not p:
        return None, None
    d = defs[k]
    spread = p.high - p.low
    metric_label = t("metric." + k)
    valor_fmt = lambda v: _fig_unit(t, k, v, defs)
    if start:
        w = profile.window(conn, k, start, end, person_id)
        if month:
            delta, cls = _compare(t, k, d, w.mean, p.all_mean, spread, "trends.vs_hist")
        else:
            delta, cls = _compare(t, k, d, w.mean, w.prev_mean, spread, "trends.vs_prev")
        etiqueta_dia = lambda day: _short_date(t, day)
        aria = _chart_aria(t, metric_label, etiqueta_dia(w.start), etiqueta_dia(w.end))
        fig, svg = w.mean, profile.window_svg(w, p.low, p.high, nights, etiqueta_dia=etiqueta_dia, aria_label=aria,
                                              valor_fmt=valor_fmt, link_label=t("trends.chart_open_day"))
    else:
        delta, cls = _compare(t, k, d, p.last30, p.all_mean, spread, "trends.vs_hist")
        etiqueta_mes = lambda m: t.month_year(date.fromisoformat(m + "-01"))
        aria = (_chart_aria(t, metric_label, etiqueta_mes(p.months[0][0]), etiqueta_mes(p.months[-1][0]), months=True)
                if len(p.months) >= 2 else None)
        fig, svg = p.last30, profile.month_svg(p, etiqueta_mes=etiqueta_mes, aria_label=aria,
                                               valor_fmt=valor_fmt, link_label=t("trends.chart_open_month"))
    card = {"key": k, "question": t("trends.q." + k), "est": d["kind"] == "estimated",
            "last30": _value(t, k, fig), "unit": _unit(t, d) if fig is not None else "", "delta": delta,
            "cls": cls, "svg": svg, "low": _value(t, k, p.low), "high": _value(t, k, p.high),
            "sparse": k in SPARSE_KEYS, "subtitle": None}
    return card, p


@app.get("/trends")
@_cierra_conexion
def trends(r: str = "all", m: str | None = None, todas: str | None = None, desde: str | None = None,
           dia: str | None = None, person_id: int = Depends(identity.current_person_id)):
    """r = 7 | 30 | 90 | all (the whole history, monthly). m = YYYY-MM opens one month day by day (from a tap on
    the monthly line). todas=1 keeps "Ver todas las medidas" open across a range switch. desde=turnos|dia|como
    arrives from a Turnos cost row, a Dia card or a Como lo calculo row (BODY-TURNOS-ENLACES-1, BODY-PULIDO-1)
    and shows the way back, kept on every range link; dia=<YYYY-MM-DD> names the day to go back to for
    desde=dia/como, and an unparseable dia drops both. The URL carries a range, a month or those flags, never
    a value."""
    conn, person, t = _ctx(person_id)
    turnos_on = _turnos_on(conn, person_id)
    defs = _defs(conn)
    last = _latest_day(conn, person_id)
    desde_dia = None
    if desde in ("dia", "como") and dia:
        try:
            desde_dia = date.fromisoformat(dia).isoformat()
        except ValueError:
            desde_dia = None
    if desde not in ("turnos", "dia", "como") or (desde in ("dia", "como") and not desde_dia):
        desde = None
    back_href = back_label = None
    if desde == "turnos":
        back_href, back_label = "/shifts", t("trends.back_turnos")
    elif desde == "dia":
        back_href, back_label = f"/day/{desde_dia}", t("trends.back_dia")
    elif desde == "como":
        back_href, back_label = f"/how/{desde_dia}", t("trends.back_como")
    month = None
    if m:
        try:
            month = date.fromisoformat(m + "-01")
        except ValueError:
            raise HTTPException(404)
        r = "month"
    elif r not in RANGES:
        r = "all"
    start = end = None
    if month:
        start = month
        end = (month.replace(day=28) + timedelta(4)).replace(day=1) - timedelta(1)
    elif r != "all" and last:
        end = date.fromisoformat(last)
        start = end - timedelta(int(r) - 1)
    nights = _nights(conn, start, end, person_id) if start and turnos_on else set()
    cards, since, days = [], None, 0
    for k in TREND_KEYS:
        card, p = _trend_card(conn, t, defs, k, start, end, month, nights, person_id)
        if not card:
            continue
        since = since or p.first_day
        days = max(days, p.days)
        cards.append(card)
    if not cards:
        have = _trend_days_have(conn, person_id)
        if have:
            return _render("soon.html", t=t, tab="trends", title=t("nav.trends"),
                           text=t("trends.too_little", days=TREND_MIN_DAYS - have),
                           back_href=back_href, back_label=back_label, turnos=turnos_on)
        return _render("soon.html", t=t, tab="trends", title=t("nav.trends"),
                       text=_sync_status_text(conn, person, t),
                       back_href=back_href, back_label=back_label, turnos=turnos_on)
    extra_groups, extra_count = [], 0
    for group_key, keys in TREND_GROUPS:
        group_cards = []
        for k in keys:
            card, p = _trend_card(conn, t, defs, k, start, end, month, nights, person_id)
            if not card:
                continue
            since = since or p.first_day
            days = max(days, p.days)
            card["subtitle"] = t("trends.plain." + k)
            group_cards.append(card)
            extra_count += 1
        if group_cards:
            extra_groups.append({"title": t("trends.group." + group_key), "cards": group_cards})
    week = None
    profs = [profile.metric_profile(conn, k, person_id=person_id) for k in WEEK_KEYS]
    if all(profs):
        cols = []
        for k, p in zip(WEEK_KEYS, profs):
            wd = p.weekday
            worse = max if defs[k]["better"] == "lower" else min
            present = [v for v in wd if v is not None]
            spread = max(present) - min(present)
            mark = wd.index(worse(present)) if spread >= (1 if not _is_minutes(k) else 5) else -1
            cols.append([{"text": _value(t, k, v), "mark": i == mark} for i, v in enumerate(wd)])
        week = {"heads": [t("trends.short." + k) for k in WEEK_KEYS],
                "rows": [{"day": t("date.days")[i][:3].capitalize(), "vals": [c[i] for c in cols]} for i in range(7)]}
    all_days = db.days_with_data(conn, person_id)
    since, days = (all_days[-1], len(all_days)) if all_days else (since, days)
    if month:
        fig_label = t("trends.month_avg", month=t.month_year(month))
    elif start:
        fig_label = t("trends.avg_n", n=r)
    else:
        fig_label = t("trends.last30")
    return _render("trends.html", t=t, tab="trends", cards=cards, week=week if r == "all" else None,
                   since=t.long_date(date.fromisoformat(since)) if since else "", days=t.num(days),
                   r=r, ranges=RANGES, fig_label=fig_label, nights=bool(nights),
                   month_title=t.month_year(month) if month else None,
                   extra_groups=extra_groups, extra_count=extra_count, todas=todas == "1",
                   desde=desde, dia=desde_dia, back_href=back_href, back_label=back_label, turnos=turnos_on)


def _short_date(t: T, d: date) -> str:
    """19 jun 2024 style: day, abbreviated month, year -- no weekday, unlike t.long_date."""
    return f"{d.day} {t('date.months')[d.month - 1][:3]} {d.year}"


def _chart_aria(t: T, metric: str, start_label: str, end_label: str, months: bool = False) -> str:
    """Accessible name for a trend chart: the metric and its span in words, no figure in it (BODY-EJES-FECHAS-1).
    The connector words come from trends.aria_days / trends.aria_months in i18n, not literals here. A month span
    reads mid-sentence in Spanish, so its month names go lowercase here -- t.month_year itself still capitalizes,
    for the axis ends where they stand alone (BODY-EJES-FECHAS-1-M)."""
    if months and t.lang == "es":
        start_label, end_label = start_label.lower(), end_label.lower()
    key = "trends.aria_months" if months else "trends.aria_days"
    return t(key, metric=metric, desde=start_label, hasta=end_label)


def _short_weekday_date(t: T, d: date) -> str:
    """jue 13 nov style: abbreviated weekday, day, abbreviated month -- no year, for a shift within
    the next 14 days (BODY-HOY-TURNO-1)."""
    return f"{t('date.days')[d.weekday()][:3]} {d.day} {t('date.months')[d.month - 1][:3]}"


def _kind_label(t: T, info: dict | None) -> str:
    if not info:
        return "—"
    return t("activity.type." + info["key"]) if info["known"] else info["fallback_label"]


def _finding_text(t: T, defs: dict, f: dict) -> str:
    """A plain sentence, not "Your toughest {label}": the metric in its sentence form (never title case
    mid-sentence) and the direction said from `better` -- a "lower is better" metric reads worst where it
    runs highest, a "higher is better" metric reads worst where it runs lowest."""
    metric = t("metric." + f["metric"] + ".sentence")
    value = _value(t, f["metric"], f["value"])
    unit = _unit(t, defs[f["metric"]])
    better = defs[f["metric"]]["better"]
    key = f"history.find.{f['axis']}_better_{better}"
    if f["axis"] == "weekday":
        return t(key, metric=metric, day=t("date.days")[f["index"]], value=value, unit=unit)
    return t(key, metric=metric, month=t("date.months")[f["index"]], value=value, unit=unit)


TRAINING_COLORS = ["var(--body)", "var(--night)", "var(--amber)", "var(--info)", "var(--est)"]
OTHER_COLOR = "var(--muted)"
TRAINING_RECENT_MONTHS = 12


def _color_map(types: list[dict]) -> dict[str, str]:
    colors, i = {}, 0
    for ty in types:
        if ty["key"] == "other":
            colors["other"] = OTHER_COLOR
        else:
            colors[ty["key"]] = TRAINING_COLORS[i % len(TRAINING_COLORS)]
            i += 1
    return colors


def _training_ctx(t: T, train: dict) -> dict:
    colors = _color_map(train["types"])
    types = [{"label": _kind_label(t, ty), "sessions": t.num(ty["sessions"]), "minutes": t.minutes(ty["minutes"]),
              "km": f"{t.num(ty['km'], 1)} km" if ty["km"] is not None else None, "color": colors[ty["key"]]}
             for ty in train["types"]]
    max_minutes = train["max_month_minutes"] or 0.0
    months = []
    for m in train["months"]:
        segs = [{"label": _kind_label(t, seg), "color": colors.get(seg["key"], OTHER_COLOR),
                 "pct": (seg["minutes"] / max_minutes * 100) if max_minutes else 0.0} for seg in m["by_type"]]
        months.append({"month": t.month_year(date.fromisoformat(m["month"] + "-01")),
                       "href": f"/trends?m={m['month']}", "sessions": t.num(m["sessions"]),
                       "minutes": t.minutes(m["minutes"]), "segs": segs})
    months.reverse()  # newest first (BODY-HISTORIA-POLISH-1): training() builds them oldest first
    yoy, yoy_ctx = train["yoy"], None
    if yoy:
        yoy_ctx = {"now": t("history.training_yoy_now", sessions=t.num(yoy["sessions_now"]),
                             minutes=t.minutes(yoy["minutes_now"])),
                   "prev": t("history.training_yoy_prev", sessions=t.num(yoy["sessions_year_ago"]),
                             minutes=t.minutes(yoy["minutes_year_ago"]))}
    return {"types": types, "months": months[:TRAINING_RECENT_MONTHS], "months_older": months[TRAINING_RECENT_MONTHS:],
            "usual_week": (t("history.training_usual_week", n=t.num(train["usual_week_sessions"]))
                           if train["usual_week_sessions"] is not None else None),
            "longest": _kind_label(t, train["longest_kind"]) if train["longest_kind"] else None,
            "frequent": _kind_label(t, train["most_frequent_kind"]) if train["most_frequent_kind"] else None,
            "yoy": yoy_ctx}


def _pattern_rows(t: T, defs: dict, pat: dict) -> dict | None:
    present = [k for k in history.PATTERN_KEYS if pat["metrics"].get(k)]
    if not present:
        return None
    marks = {(f["metric"], f["axis"], f["index"]) for f in pat["findings"]}

    def row(axis: str, idx: int, label: str) -> dict:
        vals = []
        for k in present:
            b = pat["metrics"][k][axis][idx]
            vals.append({"text": _value(t, k, b["center"]) if b else "—", "mark": (k, axis, idx) in marks})
        return {"label": label, "vals": vals}

    return {"heads": [t("trends.short." + k) for k in present],
            "weekday": [row("weekday", i, t("date.days")[i][:3].capitalize()) for i in range(7)],
            "month": [row("month", i, t("date.months")[i][:3].capitalize()) for i in range(12)]}


def _shift_ctx(t: T, defs: dict, shift_data: dict) -> dict:
    if not shift_data["has_shifts"]:
        return {"has_shifts": False}
    kinds = []
    for row in shift_data["kinds"]:
        metrics = [{"label": t("metric." + k), "value": _value(t, k, row["metrics"].get(k)),
                    "unit": _unit(t, defs[k]) if row["metrics"].get(k) is not None else ""}
                   for k in history.PATTERN_KEYS]
        kinds.append({"kind": row["kind"], "kind_label": t("shift.kind." + row["kind"]),
                      "days": t("history.days_count", n=t.num(row["days"])), "metrics": metrics})
    return {"has_shifts": True, "kinds": kinds}


@app.get("/history")
@_cierra_conexion
def history_view(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    turnos_on = _turnos_on(conn, person_id)
    cov = history.coverage(conn, person_id)
    if not cov:
        return _render("soon.html", t=t, tab="trends", title=t("history.title"),
                       text=_sync_status_text(conn, person, t), turnos=turnos_on)
    defs = _defs(conn)
    train = history.training(conn, person_id)
    pat = history.patterns(conn, person_id)
    shift_data = history.by_shift(conn, person_id)

    since = _short_date(t, date.fromisoformat(cov["first_day"]))
    coverage_line = t("history.coverage", since=since, days=t.num(cov["days_present"]))
    gap_line = (t("history.coverage_gap", days=t.num(cov["longest_gap_days"]),
                  start=_short_date(t, date.fromisoformat(cov["longest_gap_start"])),
                  end=_short_date(t, date.fromisoformat(cov["longest_gap_end"])))
                if cov["longest_gap_days"] > 0 else t("history.coverage_no_gap"))
    hero_finding = _finding_text(t, defs, pat["findings"][0]) if pat["findings"] else None

    return _render("history.html", t=t, tab="trends", coverage_line=coverage_line, gap_line=gap_line,
                   activity_count=t.num(cov["activity_count"]), hero_finding=hero_finding,
                   training=_training_ctx(t, train) if train else None,
                   patterns=_pattern_rows(t, defs, pat),
                   findings=[_finding_text(t, defs, f) for f in pat["findings"][1:]],
                   shift=_shift_ctx(t, defs, shift_data), turnos=turnos_on)


AREAS = ["body", "money", "adventure", "growth", "work", "home"]
DIRECTIONS = ["raise", "lower", "keep"]
CONTEXTS = ("before_night", "after_night", "off_days")
BETTER_TO_DIRECTION = {"higher": "raise", "lower": "lower", "neutral": "keep"}


def _fig_unit(t: T, key: str, v, defs: dict) -> str:
    val = _value(t, key, v)
    if val == "—":
        return val
    u = _unit(t, defs[key])
    return f"{val} {u}" if u else val


def _habit_cadence(h) -> dict:
    return json.loads(h["cadence"])


def _cadence_text(t: T, cadence: dict) -> str:
    per = t("goals.cadence.per." + cadence.get("per", "week"))
    return t("goals.cadence", n=cadence.get("times", 1), per=per)


def _context_label(t: T, context: str | None) -> str | None:
    return t("habit.context." + context) if context else None


def _parse_new_habit(f: dict, prefix: str = "") -> tuple[str | None, dict | None]:
    """Reads title/times/per/context fields the same way for the goal detail's 'add a habit' form
    and goal creation's optional first-habit block (BODY-SIN-FRICCION-1, prefix='habit_' there so
    its fields never collide with the goal's own 'title'). Empty title = no habit, (None, None)."""
    title = (f.get(prefix + "title") or "").strip()
    if not title:
        return None, None
    per = f.get(prefix + "per") if f.get(prefix + "per") in ("day", "week", "month") else "week"
    try:
        times = max(1, int(f.get(prefix + "times", "1")))
    except ValueError:
        times = 1
    context = f.get(prefix + "context") or None
    cadence = {"per": per, "times": times}
    if context in CONTEXTS:
        cadence["context"] = context
    return title, cadence


def _due_days_for(conn, person_id: int, monday: date, context: str | None) -> list[str]:
    """The days of this week a habit is due. Calls goals.py's own shift-context rule
    (_context_due_days / _week_days) rather than re-deriving it here."""
    return goals._context_due_days(conn, person_id, monday, context) if context else goals._week_days(monday)


def _habit_progress(conn, h, person_id: int, monday: date) -> tuple[int, int]:
    due_days = _due_days_for(conn, person_id, monday, _habit_cadence(h).get("context"))
    if not due_days:
        return 0, 0
    placeholders = ",".join("?" * len(due_days))
    done = conn.execute(
        f"SELECT COUNT(*) FROM habit_check WHERE habit_id = ? AND person_id = ? AND done = 1 "
        f"AND day IN ({placeholders})", (h["id"], person_id, *due_days)).fetchone()[0]
    return done, len(due_days)


def _habit_checked(conn, habit_id: int, person_id: int, day: str) -> bool:
    row = conn.execute("SELECT done FROM habit_check WHERE habit_id = ? AND person_id = ? AND day = ?",
                       (habit_id, person_id, day)).fetchone()
    return bool(row and row["done"])


def _is_due_today(conn, h, person_id: int, today: date) -> bool:
    context = _habit_cadence(h).get("context")
    if not context:
        return True
    return today.isoformat() in _due_days_for(conn, person_id, _week_monday(today), context)


def _habits_for_goal(conn, goal_id: int):
    """Every active habit of G, whoever it is assigned to -- goal_detail renders a habit assigned
    to another household member as theirs (name only), never this viewer's own checkbox."""
    return conn.execute("SELECT * FROM habit WHERE goal_id = ? AND active = 1 ORDER BY id", (goal_id,)).fetchall()


def _today_due_habits(conn, t: T, person_id: int, today: date) -> list[dict]:
    hh = household.household_of(conn, person_id)
    scope_sql, scope_params = goals.goal_scope_sql(person_id, hh)
    rows = conn.execute(
        f"SELECT h.*, g.title AS goal_title FROM habit h JOIN goal g ON g.id = h.goal_id "
        f"WHERE g.status = 'active' AND {scope_sql} AND h.active = 1 "
        f"AND (h.assignee_person_id IS NULL OR h.assignee_person_id = ?) ORDER BY h.id",
        (*scope_params, person_id)).fetchall()
    out = []
    for h in rows:
        if not _is_due_today(conn, h, person_id, today):
            continue
        out.append({"id": h["id"], "title": h["title"], "goal_title": h["goal_title"],
                    "context": _context_label(t, _habit_cadence(h).get("context")),
                    "checked": _habit_checked(conn, h["id"], person_id, today.isoformat())})
    return out


def _verdict(t: T, on_track) -> tuple[str, str]:
    if on_track is None:
        return "", t("goals.verdict.no_data")
    return ("ok", t("goals.verdict.on")) if on_track else ("warn", t("goals.verdict.off"))


def _metric_pct(g, current) -> float | None:
    if g["target_value"] is None or g["baseline_value"] is None:
        return None
    span = g["target_value"] - g["baseline_value"]
    if span == 0:
        return 100.0
    if current is None:
        return 0.0
    return max(0.0, min(100.0, (current - g["baseline_value"]) / span * 100))


def _time_pct(g, today: date) -> float | None:
    if not g["deadline"] or not g["start_day"]:
        return None
    s, e = date.fromisoformat(g["start_day"]), date.fromisoformat(g["deadline"])
    if e <= s:
        return 100.0
    return max(0.0, min(100.0, (today - s).days / (e - s).days * 100))


def _goal_target_text(t: T, defs: dict, g) -> str:
    date_txt = _short_date(t, date.fromisoformat(g["deadline"])) if g["deadline"] else "—"
    if g["kind"] == "metric":
        return t("goals.card.target", target=_fig_unit(t, g["metric_key"], g["target_value"], defs), date=date_txt)
    return t("goals.card.milestone_date", date=date_txt)


def _goal_check_row(conn, goal_id: int, person_id: int, week_start: str) -> dict:
    row = conn.execute("SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? AND week_start = ?",
                       (goal_id, person_id, week_start)).fetchone()
    if row:
        return dict(row)
    result = goals.weekly_check(conn, goal_id, person_id, week_start)
    return result or {"on_track": None, "metric_avg": None, "habits_done": 0, "habits_due": 0}


def _goal_line_svg(t: T, defs: dict, g, checks, today: date, width: int = 320, height: int = 120,
                    color: str = "var(--body)") -> str:
    """The straight target line from baseline at start_day to target at deadline, with the
    actual weekly averages plotted over it and today marked. Small and local to this screen,
    since body/profile.py's chart helpers are built for daily_metric history, not a goal's plan.

    Accessible name, legend and a per-week tap/drag readout (DESIGN rule 9, BODY-PULIDO-2): the aria-label and
    legend name the plan and its dates, never a raw baseline or target figure; weekly points in data-points
    carry a label only, no day -- a week is not a day, so charts.js shows no link for them."""
    start, end = date.fromisoformat(g["start_day"]), date.fromisoformat(g["deadline"])
    if end <= start:
        return ""
    baseline_v, target_v = g["baseline_value"], g["target_value"]
    lo, hi = min(baseline_v, target_v), max(baseline_v, target_v)
    pad = (hi - lo) * 0.15 or 1
    lo, hi = lo - pad, hi + pad
    top, h = 6, height - 26
    span_days = (end - start).days

    def x(d: date) -> float:
        return max(0.0, min(1.0, (d - start).days / span_days)) * width

    def y(v: float) -> float:
        return top + h - (v - lo) / (hi - lo) * h

    out = [f'<line x1="{x(start):.1f}" y1="{y(baseline_v):.1f}" x2="{x(end):.1f}" y2="{y(target_v):.1f}" '
           f'style="stroke:var(--muted)" stroke-width="1.6" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>']
    pts = sorted((date.fromisoformat(c["week_start"]), date.fromisoformat(c["week_start"]) + timedelta(6),
                  c["metric_avg"]) for c in checks)
    pts = [(ws, d, v) for ws, d, v in pts if start <= d <= end]
    if len(pts) >= 2:
        line_pts = " ".join(f"{x(d):.1f},{y(v):.1f}" for _, d, v in pts)
        out.append(f'<polyline fill="none" style="stroke:{color}" stroke-width="2.2" '
                   f'vector-effect="non-scaling-stroke" stroke-linejoin="round" points="{line_pts}"/>')
    elif len(pts) == 1:
        out.append(f'<circle cx="{x(pts[0][1]):.1f}" cy="{y(pts[0][2]):.1f}" r="4" style="fill:{color}"/>')
    today_x = x(min(max(today, start), end))
    out.append(f'<line x1="{today_x:.1f}" y1="{top}" x2="{today_x:.1f}" y2="{top + h}" '
               f'style="stroke:var(--line)" stroke-width="1"/>')
    aria = html.escape(f"{g['title']}: {t('goals.chart_aria', start=_short_date(t, start), end=_short_date(t, end))}")
    svg = (f'<svg viewBox="0 0 {width} {top + h + 6}" width="100%" height="110" role="img" aria-label="{aria}" '
           f'preserveAspectRatio="none" style="display:block;margin-top:8px">{"".join(out)}</svg>')
    legend = (
        '<div class="legend" style="margin-top:4px">'
        f'<span><i style="background:none;border-top:1.6px dashed var(--muted);height:0;width:12px"></i>{t("goals.legend_plan")}</span>'
        f'<span><i style="background:{color};height:3px;vertical-align:3px"></i>{t("goals.legend_avg")}</span>'
        f'<span><i style="background:var(--line);width:1px;height:12px"></i>{t("goals.legend_today")}</span>'
        '</div>')
    data_pts = [{"x": max(0.0, min(1.0, (d - start).days / span_days)),
                 "label": t("goals.chart_week_label", date=_short_date(t, ws), value=_fig_unit(t, g["metric_key"], v, defs))}
                for ws, d, v in pts]
    return profile.chart_figure(svg + legend, data_pts)


def _verdict_sentence(t: T, defs: dict, g, check: dict) -> str:
    _, verdict = _verdict(t, check["on_track"])
    if g["kind"] == "metric" and check["metric_avg"] is not None:
        return t("goals.history.metric", verdict=verdict, value=_fig_unit(t, g["metric_key"], check["metric_avg"], defs))
    if g["kind"] == "milestone" and check["habits_due"]:
        return t("goals.history.milestone", verdict=verdict, done=check["habits_done"], due=check["habits_due"])
    return verdict


@app.get("/goals")
@_cierra_conexion
def goals_list(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    today = _today_local(person)
    week_start = _week_monday(today).isoformat()
    defs = _defs(conn)

    hh = household.household_of(conn, person_id)
    scope_sql, scope_params = goals.goal_scope_sql(person_id, hh)
    active = conn.execute(f"SELECT * FROM goal WHERE status = 'active' AND {scope_sql} ORDER BY id",
                          scope_params).fetchall()
    proposed = conn.execute(f"SELECT * FROM goal WHERE status = 'proposed' AND {scope_sql} ORDER BY id",
                            scope_params).fetchall()
    archived = conn.execute(
        f"SELECT * FROM goal WHERE status IN ('paused', 'done', 'dropped') AND {scope_sql} ORDER BY id",
        scope_params).fetchall()

    cards = []
    for g in active:
        check = _goal_check_row(conn, g["id"], person_id, week_start)
        cls, verdict = _verdict(t, check["on_track"])
        if g["kind"] == "metric":
            fig = _fig_unit(t, g["metric_key"], check["metric_avg"], defs)
            started = (t("goals.card.started", value=_fig_unit(t, g["metric_key"], g["baseline_value"], defs))
                      if g["baseline_value"] is not None else None)
            pct = _metric_pct(g, check["metric_avg"])
        else:
            fig = t("goals.card.milestone_fig", done=check["habits_done"] or 0, due=check["habits_due"] or 0)
            started = None
            pct = _time_pct(g, today)
        cards.append({"id": g["id"], "title": g["title"], "cls": cls, "verdict": verdict, "fig": fig,
                      "period": t("goals.card.this_week"), "target_text": _goal_target_text(t, defs, g),
                      "started": started, "pct": round(pct) if pct is not None else None})

    proposed_cards = [{"id": g["id"], "title": g["title"], "sub": _goal_target_text(t, defs, g)} for g in proposed]
    archived_cards = [{"id": g["id"], "title": g["title"], "status_text": t("goals.status." + g["status"])}
                      for g in archived]

    return _render("goals.html", t=t, tab="goals", cards=cards, proposed=proposed_cards, archived=archived_cards,
                   empty=not (active or proposed or archived), turnos=_turnos_on(conn, person_id))


@app.get("/goals/new")
@_cierra_conexion
def goal_new_form(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    defs = _defs(conn)
    today = _today_local(person).isoformat()
    metrics = []
    for k, d in defs.items():
        b = baseline(conn, k, today, person_id=person_id)
        metrics.append({"key": k, "label": t("metric." + k), "unit": _unit(t, d),
                        "direction": BETTER_TO_DIRECTION[d["better"]], "baseline": b.mean if b else None})
    hh = household.household_of(conn, person_id)
    return _render("goal_new.html", t=t, tab="goals", metrics=metrics, areas=AREAS, directions=DIRECTIONS,
                   has_household=bool(hh and len(hh.member_ids) > 1), turnos=_turnos_on(conn, person_id))


@app.post("/goals/new")
@_cierra_conexion_async
async def goal_new_create(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    f = await _form(request)
    kind = f.get("kind")
    title = (f.get("title") or "").strip()
    if not title or kind not in ("metric", "milestone"):
        return RedirectResponse("/goals/new", status_code=303)
    today = _today_local(person).isoformat()
    why = (f.get("why") or "").strip() or None
    area = f.get("area") if f.get("area") in AREAS else "body"
    hh = household.household_of(conn, person_id)
    visibility = "household" if f.get("visibility") == "household" and hh else "private"
    hogar_id = hh.id if visibility == "household" else None
    if kind == "metric":
        defs = _defs(conn)
        metric_key, direction = f.get("metric_key"), f.get("direction")
        if metric_key not in defs or direction not in DIRECTIONS or not f.get("deadline"):
            return RedirectResponse("/goals/new", status_code=303)
        try:
            target_value = float(f.get("target_value", ""))
            baseline_value = float(f.get("baseline_value", ""))
        except ValueError:
            return RedirectResponse("/goals/new", status_code=303)
        gid = goals.propose(conn, person_id, title, today, area=area, why=why, kind="metric",
                            metric_key=metric_key, direction=direction, target_value=target_value,
                            baseline_value=baseline_value, deadline=f["deadline"], hogar_id=hogar_id,
                            visibility=visibility)
    else:
        if not f.get("deadline"):
            return RedirectResponse("/goals/new", status_code=303)
        gid = goals.propose(conn, person_id, title, today, area=area, why=why, kind="milestone",
                            deadline=f["deadline"], hogar_id=hogar_id, visibility=visibility)
    habit_title, habit_cadence = _parse_new_habit(f, "habit_")
    if habit_title:
        goals.add_habit(conn, gid, habit_title, habit_cadence)
    return RedirectResponse(f"/goals/{gid}", status_code=303)


@app.get("/goals/{goal_id}")
@_cierra_conexion
def goal_detail(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    access = goals.goal_access(conn, goal_id, person_id)
    if access is None:
        raise HTTPException(404)
    g = conn.execute("SELECT * FROM goal WHERE id = ?", (goal_id,)).fetchone()
    defs = _defs(conn)
    today = _today_local(person)
    monday = _week_monday(today)
    if g["status"] == "active":
        _goal_check_row(conn, goal_id, person_id, monday.isoformat())  # upserts THIS VIEWER's check if missing

    chart = None
    if g["kind"] == "metric" and g["deadline"] and g["baseline_value"] is not None and g["target_value"] is not None:
        checks_for_chart = conn.execute(
            "SELECT week_start, metric_avg FROM goal_check WHERE goal_id = ? AND person_id = ? "
            "AND metric_avg IS NOT NULL ORDER BY week_start", (goal_id, person_id)).fetchall()
        chart = _goal_line_svg(t, defs, g, checks_for_chart, today)

    history_rows = conn.execute(
        "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? ORDER BY week_start DESC LIMIT 8",
        (goal_id, person_id)).fetchall()
    history_list = [{"week": t("goals.week_of", date=_short_date(t, date.fromisoformat(r["week_start"]))),
                     "text": _verdict_sentence(t, defs, g, dict(r))} for r in history_rows]

    habit_rows = []
    for h in _habits_for_goal(conn, goal_id):
        cadence = _habit_cadence(h)
        is_mine = h["assignee_person_id"] is None or h["assignee_person_id"] == person_id
        row = {"id": h["id"], "title": h["title"], "cadence": _cadence_text(t, cadence),
               "context": _context_label(t, cadence.get("context")), "mine": is_mine}
        if is_mine:
            done, due = _habit_progress(conn, h, person_id, monday)
            row.update({
                "progress": t("goals.habit.progress", done=done, due=due) if due else t("goals.habit.not_due"),
                "due_today": _is_due_today(conn, h, person_id, today),
                "checked_today": _habit_checked(conn, h["id"], person_id, today.isoformat()),
            })
        else:
            other = conn.execute("SELECT display_name FROM person WHERE id = ?",
                                 (h["assignee_person_id"],)).fetchone()
            row["assignee_name"] = t("goals.habit_assignee", name=other["display_name"] if other else "?")
        habit_rows.append(row)

    members = None
    shared_line = None
    if g["visibility"] == "household":
        hh = household.household_of(conn, person_id)
        other_ids = [m for m in (hh.member_ids if hh else ()) if m != person_id]
        if other_ids:
            members = []
            for mid in other_ids:
                mrow = conn.execute("SELECT display_name FROM person WHERE id = ?", (mid,)).fetchone()
                crow = conn.execute(
                    "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? ORDER BY week_start DESC LIMIT 1",
                    (goal_id, mid)).fetchone()
                _, verdict = _verdict(t, crow["on_track"] if crow else None)
                members.append({"name": mrow["display_name"] if mrow else f"#{mid}", "verdict": verdict})
        if access == "creator":
            shared_line = t("goals.shared_by_you")
        else:
            creator = conn.execute("SELECT display_name FROM person WHERE id = ?", (g["person_id"],)).fetchone()
            shared_line = t("goals.shared_by", name=creator["display_name"] if creator else "?")

    latest_on_track = history_rows[0]["on_track"] if history_rows else None
    cls, verdict = _verdict(t, latest_on_track)

    return _render("goal.html", t=t, tab="goals", goal=g, cls=cls, verdict=verdict, chart=chart,
                   history=history_list, habits=habit_rows, members=members, shared_line=shared_line,
                   is_creator=(access == "creator"),
                   target_text=_goal_target_text(t, defs, g), area_label=t("goals.area." + g["area"]),
                   today=today.isoformat(), turnos=_turnos_on(conn, person_id))


def _require_creator(conn, goal_id: int, person_id: int) -> None:
    """Editing G and activate/pause/finish/drop stay with the creator in v1 (BODY-HOGAR-MIEMBRO-1
    rule 5) -- a member, or a stranger, gets 404 exactly like an unknown goal."""
    if goals.goal_access(conn, goal_id, person_id) != "creator":
        raise HTTPException(404)


@app.post("/goals/{goal_id}/activate")
@_cierra_conexion
def goal_activate(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    goals.activate(conn, goal_id)
    return RedirectResponse(f"/goals/{goal_id}", status_code=303)


@app.post("/goals/{goal_id}/pause")
@_cierra_conexion
def goal_pause(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    goals.pause(conn, goal_id)
    return RedirectResponse(f"/goals/{goal_id}", status_code=303)


@app.post("/goals/{goal_id}/finish")
@_cierra_conexion
def goal_finish(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    goals.finish(conn, goal_id)
    return RedirectResponse("/goals", status_code=303)


@app.post("/goals/{goal_id}/drop")
@_cierra_conexion
def goal_drop(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    goals.drop(conn, goal_id)
    return RedirectResponse("/goals", status_code=303)


@app.post("/goals/{goal_id}/habits")
@_cierra_conexion_async
async def goal_add_habit(goal_id: int, request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    if goals.goal_access(conn, goal_id, person_id) is None:
        raise HTTPException(404)
    g = conn.execute("SELECT visibility FROM goal WHERE id = ?", (goal_id,)).fetchone()
    f = await _form(request)
    title, cadence = _parse_new_habit(f)
    if title:
        # BODY-HOGAR-MIEMBRO-1 rule 4: on a household goal, the adder (creator or member) picks
        # "for me" (assignee = themselves) or "for both" (assignee = NULL, the default).
        assignee = person_id if g["visibility"] == "household" and f.get("for") == "me" else None
        goals.add_habit(conn, goal_id, title, cadence, assignee_person_id=assignee)
    return RedirectResponse(f"/goals/{goal_id}", status_code=303)


@app.post("/habits/{habit_id}/check")
@_cierra_conexion_async
async def habit_check(habit_id: int, request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    f = await _form(request)
    day = f.get("day")
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise HTTPException(404)
    row = conn.execute(
        "SELECT h.assignee_person_id, h.goal_id FROM habit h WHERE h.id = ?", (habit_id,)).fetchone()
    if row is None or goals.goal_access(conn, row["goal_id"], person_id) is None:
        raise HTTPException(404)
    # BODY-HOGAR-MIEMBRO-1 rule 2: a member checks off a habit only when it is for both (NULL) or
    # for themselves; a habit assigned to someone else cannot be checked by this viewer.
    if row["assignee_person_id"] is not None and row["assignee_person_id"] != person_id:
        raise HTTPException(404)
    goals.check_habit(conn, habit_id, person_id, day, f.get("done") == "1")
    back = f.get("back") or "/"
    return RedirectResponse(back, status_code=303)


@app.get("/settings")
@_cierra_conexion
def settings(person_id: int = Depends(identity.current_person_id)):
    conn, _, t = _ctx(person_id)
    nivel_datos = agente_ajustes.nivel_datos(conn, person_id)
    trabaja_turnos = db.get_trabaja_turnos(conn, person_id)
    return _render("settings.html", t=t, tab="settings", nivel_datos=nivel_datos,
                   trabaja_turnos=trabaja_turnos, turnos=_turnos_on(conn, person_id))


# ---- Ayuda (tutoriales-v1 section 5.1): chapters, their tasks, and the recorrido completo ---------

def _tarea_ayuda_ctx(tarea: dict, hosted: bool) -> dict:
    objetivo = tutoriales.ir_a_pantalla(tarea)
    solo_en_linea = objetivo in tutoriales.SOLO_EN_LINEA and not hosted
    return {
        "id": tarea["id"], "titulo": tarea["titulo"], "video": tutoriales.video(tarea["id"]),
        "pantalla_link": None if solo_en_linea else objetivo, "solo_en_linea": solo_en_linea,
        "con_asistente": tarea["agente"] == "si",
    }


@app.get("/ayuda")
@_cierra_conexion
def ayuda(person_id: int = Depends(identity.current_person_id)):
    conn, _, t = _ctx(person_id)
    turnos_on = _turnos_on(conn, person_id)
    hosted = cuenta.hosted()
    capitulos = []
    recorrido = []
    for c in tutoriales.capitulos_con_tareas(incluir_turnos=turnos_on):
        tareas = [_tarea_ayuda_ctx(tarea, hosted) for tarea in c["tareas"]]
        capitulos.append({"id": c["id"], "titulo": c["titulo"], "tareas": tareas})
        recorrido.extend(tarea for tarea in tareas if tarea["video"])
    return _render("ayuda.html", t=t, tab="settings", capitulos=capitulos, recorrido=recorrido, turnos=turnos_on)


@app.post("/settings/language")
@_cierra_conexion_async
async def set_language(request: Request, person_id: int = Depends(identity.current_person_id)):
    lang = parse_qs((await request.body()).decode()).get("lang", [""])[0]
    if lang in LANGS:
        conn, _, _ = _ctx(person_id)
        db.set_language(conn, lang, person_id)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/trabaja_turnos")
@_cierra_conexion_async
async def set_trabaja_turnos(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = parse_qs((await request.body()).decode())
    valor = f.get("valor", [""])[0]
    if valor in ("si", "no"):
        conn, _, _ = _ctx(person_id)
        db.set_trabaja_turnos(conn, valor == "si", person_id)
    volver = f.get("volver", ["/settings"])[0]
    if volver not in ("/", "/settings"):
        volver = "/settings"
    return RedirectResponse(volver, status_code=303)


# ---- «Lo que ve el asistente» (BODY-NIVEL-DATOS-UI-1): the screen the consent text promises -------

PROVEEDOR_LABEL_KEYS = {
    "anthropic": "ajustes.asistente.proveedor.anthropic",
    "openai": "ajustes.asistente.proveedor.openai",
    "gemini": "ajustes.asistente.proveedor.gemini",
}


@app.get("/ajustes/asistente")
@_cierra_conexion
def nivel_datos_view(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, t = _ctx(person_id)
    nivel = agente_ajustes.nivel_datos(conn, person_id)
    configurados = agente_cliente.proveedores_configurados()
    proveedores = None
    proveedor_actual = None
    if len(configurados) >= 2:
        proveedores = [{"nombre": n, "etiqueta": t(PROVEEDOR_LABEL_KEYS[n])} for n in configurados]
        proveedor_actual = agente_cliente.nombre_elegido(configurados, conn, person_id)
    guardado = request.query_params.get("guardado") == "1"
    resp = _render("asistente_datos.html", t=t, tab="settings", nivel=nivel, guardado=guardado,
                   proveedores=proveedores, proveedor_actual=proveedor_actual, turnos=_turnos_on(conn, person_id))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/ajustes/asistente")
@_cierra_conexion_async
async def nivel_datos_guardar(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await _form(request)
    nivel = f.get("nivel") or ""
    if nivel not in agente_ajustes.NIVELES:
        raise HTTPException(400)
    conn, _, _ = _ctx(person_id)
    agente_ajustes.set_nivel_datos(conn, person_id, nivel)
    proveedor = f.get("proveedor")
    if proveedor:
        if proveedor not in agente_cliente.proveedores_configurados():
            raise HTTPException(400)
        agente_ajustes.set_proveedor(conn, person_id, proveedor)
    return RedirectResponse("/ajustes/asistente?guardado=1", status_code=303)


# ---- Assistant (BODY-ASISTENTE-PROVEEDOR-1): one tool loop, streamed as SSE ------------------

MAX_PREGUNTA_CHARS = 2000
IDIOMA_LABEL = {"es": "Español", "en": "English"}
_ETIQUETA_KEYS = {
    "aplicar": "asistente.propuesta.aplicar", "descartar": "asistente.propuesta.descartar",
    "deshacer": "asistente.propuesta.deshacer", "aplicada": "asistente.propuesta.aplicada",
    "descartada": "asistente.propuesta.descartada", "deshecha": "asistente.propuesta.deshecha",
    "no_vigente": "asistente.propuesta.no_vigente", "desactualizada": "asistente.propuesta.desactualizada",
    "sin_deshacer": "asistente.propuesta.sin_deshacer",
}


def _evento_sse(nombre: str, data: dict) -> str:
    return f"event: {nombre}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _etiquetas(t: T) -> dict:
    return {k: t(v) for k, v in _ETIQUETA_KEYS.items()}


def _objetivo_txt(t: T, defs: dict, metric_key: str | None, target_value, deadline: str) -> str:
    fecha = _short_date(t, date.fromisoformat(deadline))
    if metric_key is None or target_value is None:
        return t("goals.card.milestone_date", date=fecha)
    return t("asistente.propuesta.meta_objetivo", valor=_fig_unit(t, metric_key, target_value, defs), fecha=fecha)


def _tarjeta_propuesta(t: T, defs: dict, registro: dict) -> dict:
    """The `propuesta` SSE event payload (agent-capabilities-v1 section 2, decision 5): `antes`/
    `despues` are short plain-word strings in the person's language, built here from the record's
    structured JSON -- never stored as text. `etiquetas` carries this card's button and result
    labels pre-rendered, since body/templates/base.html (outside this slice's touches) is the only
    other place assistant.js could read i18n text from."""
    accion = registro["accion"]
    despues = registro["despues_json"]
    antes_json = registro["antes_json"]
    antes_txt: str | None = None
    if accion == "proponer_meta":
        despues_txt = t("asistente.propuesta.meta_nueva", metric=t("metric." + despues["metric_key"]),
                        direccion=t("asistente.propuesta.direccion." + despues["direction"]),
                        valor=_fig_unit(t, despues["metric_key"], despues["target_value"], defs),
                        fecha=_short_date(t, date.fromisoformat(despues["deadline"])))
    elif accion == "proponer_cambio_meta":
        metric_key = despues.get("metric_key")
        antes_txt = _objetivo_txt(t, defs, metric_key, antes_json["target_value"], antes_json["deadline"])
        despues_txt = _objetivo_txt(t, defs, metric_key, despues["target_value"], despues["deadline"])
    elif accion == "proponer_confirmar_turnos":
        resumen = antes_json["resumen"]
        antes_txt = t("asistente.propuesta.turnos_actual", n=t.num(resumen["batch_count"]))
        despues_txt = t("asistente.propuesta.turnos_nuevo")
    elif accion == "proponer_idioma":
        antes_txt = IDIOMA_LABEL.get(antes_json["lang"], antes_json["lang"])
        despues_txt = IDIOMA_LABEL.get(despues["lang"], despues["lang"])
    else:
        despues_txt = ""
    if antes_txt is not None:
        antes_txt = t("asistente.propuesta.antes", valor=antes_txt)
        despues_txt = t("asistente.propuesta.despues", valor=despues_txt)
    elif despues_txt:
        despues_txt = t("asistente.propuesta.solo_propuesta", valor=despues_txt)
    return {
        "id": registro["id"], "accion": accion, "titulo": t("asistente.propuesta.titulo." + accion),
        "antes": antes_txt, "despues": despues_txt, "expira_en": registro["expira_en"],
        "deshacer": accion not in agente_propuestas.SIN_DESHACER, "estado": registro["estado"],
        "etiquetas": _etiquetas(t),
    }


@app.post("/asistente/preguntar")
async def asistente_preguntar(request: Request, person_id: int = Depends(identity.current_person_id)):
    try:
        cuerpo = await request.json()
    except Exception:
        raise HTTPException(400)
    if not isinstance(cuerpo, dict):
        raise HTTPException(400)
    pregunta = cuerpo.get("pregunta")
    if not isinstance(pregunta, str) or not (1 <= len(pregunta) <= MAX_PREGUNTA_CHARS):
        raise HTTPException(400)
    conversacion_id = cuerpo.get("conversacion_id")
    if conversacion_id is not None and not isinstance(conversacion_id, int):
        raise HTTPException(400)

    conn, _, t = _ctx(person_id)
    agente_herramientas.fijar_turnos_actual(_turnos_on(conn, person_id))
    if conversacion_id is not None:
        try:
            agente_conversacion.verificar_propiedad(conn, conversacion_id, person_id)
        except agente_conversacion.ConversacionAjena:
            conn.close()
            raise HTTPException(404)
    else:
        conversacion_id = agente_conversacion.crear(conn, person_id)

    proveedor_env = os.environ.get("BODY_AGENTE_PROVEEDOR", agente_cliente.PROVEEDOR_DEFAULT)
    if proveedor_env == "demo":
        proveedor_nombre, modelo = "demo", "demo"
    else:
        configurados = agente_cliente.proveedores_configurados()
        resuelto = agente_cliente.nombre_elegido(configurados, conn, person_id)
        proveedor_nombre = resuelto or proveedor_env
        # `resuelto` is None when nothing is configured -- `generar()` then returns on the
        # no_configurado error before `modelo` is read in production. This reads the client's OWN
        # default constant rather than a second BODY_AGENTE_MODELO lookup, so no second default
        # exists (BODY-MODELOS-ESTANDAR-1 decision 4).
        modelo = configurados[resuelto].modelo if resuelto else agente_cliente.MODELO_DEFAULT
    proveedor = agente_cliente.proveedor_configurado(conn, person_id)

    async def generar():
        try:
            if proveedor is None:
                yield _evento_sse("error", {"codigo": "no_configurado", "mensaje": t("agente.error.no_configurado")})
                return
            try:
                agente_uso.verificar_tope(conn, person_id, proveedor_nombre, modelo)
            except agente_uso.TopeRefusal:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, agente_cliente.Uso(), 0,
                                      None, "tope_diario")
                yield _evento_sse("error", {"codigo": "tope_diario", "mensaje": t("agente.error.tope_diario")})
                return

            historial = agente_conversacion.historial(conn, conversacion_id, person_id)
            historial.append({"rol": "usuario", "bloques": [{"tipo": "texto", "texto": pregunta}]})
            sistema = t("agente.sistema")
            estados = agente_propuestas.estado_conversacion(conn, person_id, conversacion_id)
            if estados:
                lineas = "\n".join(f"- #{p['id']} {p['accion']}: {p['estado']}" for p in estados)
                sistema += "\n\n" + t("agente.estado_propuestas_header") + "\n" + lineas
            try:
                resultado = agente_bucle.ejecutar(conn, person_id, proveedor, sistema, historial, conversacion_id)
            except agente_bucle.LimiteRondas as e:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, e.uso_total, e.rondas,
                                      "limite_rondas", "limite_rondas")
                yield _evento_sse("error", {"codigo": "limite_rondas", "mensaje": t("agente.error.limite_rondas")})
                return
            except agente_bucle.FalloProveedor as e:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, e.uso_total, e.rondas,
                                      None, "proveedor")
                yield _evento_sse("error", {"codigo": "proveedor", "mensaje": t("agente.error.proveedor")})
                return

            defs = _defs(conn)
            for ev in resultado.eventos:
                if isinstance(ev, agente_bucle.EventoHerramienta):
                    yield _evento_sse("herramienta", {"nombre": ev.nombre})
                elif isinstance(ev, agente_bucle.EventoPropuesta):
                    yield _evento_sse("propuesta", _tarjeta_propuesta(t, defs, ev.registro))
                else:
                    yield _evento_sse("texto", {"delta": ev.texto})

            agente_conversacion.agregar_mensaje(conn, conversacion_id, person_id, "usuario",
                                                [{"tipo": "texto", "texto": pregunta}])
            for m in resultado.nuevos_mensajes:
                agente_conversacion.agregar_mensaje(conn, conversacion_id, person_id, m["rol"], m["bloques"])
            agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, resultado.uso_total, resultado.rondas,
                                  "texto", None)
            yield _evento_sse("fin", {"conversacion_id": conversacion_id, "rondas": resultado.rondas,
                                      "motivo_fin": "texto"})
        finally:
            conn.close()

    return StreamingResponse(generar(), media_type="text/event-stream")


@app.get("/asistente/conversacion/{conversacion_id}")
@_cierra_conexion
def asistente_conversacion(conversacion_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, t = _ctx(person_id)
    try:
        turnos = agente_conversacion.turnos_visibles(conn, conversacion_id, person_id)
    except agente_conversacion.ConversacionAjena:
        conn.close()
        raise HTTPException(404)
    defs = _defs(conn)
    tarjetas = [_tarjeta_propuesta(t, defs, r) for r in agente_propuestas.listar(conn, person_id, conversacion_id)]
    conn.close()
    return JSONResponse({"turnos": turnos, "propuestas": tarjetas})


def _propuesta_respuesta(conn, person_id: int, propuesta_id: int, accion) -> JSONResponse:
    try:
        resultado = accion(conn, person_id, propuesta_id)
    except agente_propuestas.PropuestaAjena:
        conn.close()
        raise HTTPException(404)
    conn.close()
    return JSONResponse(resultado)


@app.post("/asistente/propuestas/{propuesta_id}/aplicar")
@_cierra_conexion
def propuesta_aplicar(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.aplicar)


@app.post("/asistente/propuestas/{propuesta_id}/descartar")
@_cierra_conexion
def propuesta_descartar(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.descartar)


@app.post("/asistente/propuestas/{propuesta_id}/deshacer")
@_cierra_conexion
def propuesta_deshacer(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.deshacer)


# ---- Garmin (hosted only): one connection per person, token encrypted by body/garmin_cuenta.py ----

_MFA_TTL_SECONDS = 600
_MFA_STATE: dict[str, tuple[str, float]] = {}  # session-token hash -> (client_state_json, started_at)

_GARMIN_LAST_ERROR_KEYS = {"NotSignedIn": "garmin.error.not_signed_in"}
_GARMIN_FORM_ERROR_KEYS = {"datos": "garmin.error.datos", "garmin": "garmin.error.generic",
                           "mfa_expired": "garmin.error.mfa_expired"}


def _garmin_last_error_text(t: T, error_cls: str | None) -> str | None:
    if not error_cls:
        return None
    return t(_GARMIN_LAST_ERROR_KEYS.get(error_cls, "garmin.error.pull_failed"))


def _mfa_key(request: Request) -> str:
    token = request.cookies.get(cuenta.SESSION_COOKIE) or ""
    return hashlib.sha256(token.encode()).hexdigest()


def _prune_mfa_state() -> None:
    cutoff = time.monotonic() - _MFA_TTL_SECONDS
    for k in [k for k, (_, started) in _MFA_STATE.items() if started < cutoff]:
        _MFA_STATE.pop(k, None)


def _garmin_ctx(t: T, conn, person_id: int, **extra) -> dict:
    connected_since = garmin_cuenta.conectado_desde(conn, person_id)
    ctx = {
        "t": t, "tab": "settings", "turnos": _turnos_on(conn, person_id),
        "connected": connected_since is not None,
        "connected_since": _short_date(t, datetime.fromisoformat(connected_since).date())
                           if connected_since else None,
        "error_text": _garmin_last_error_text(t, garmin_cuenta.ultimo_error(conn, person_id)),
        "form_error": None, "mfa": False,
    }
    ctx.update(extra)
    return ctx


@app.get("/garmin")
@_cierra_conexion
def garmin_view(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, t = _ctx(person_id)
    err = request.query_params.get("error")
    form_error = t(_GARMIN_FORM_ERROR_KEYS[err]) if err in _GARMIN_FORM_ERROR_KEYS else None
    return _render("garmin.html", **_garmin_ctx(t, conn, person_id, form_error=form_error))


@app.post("/garmin/conectar")
@_cierra_conexion_async
async def garmin_conectar(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await _form(request)
    email = (f.get("email") or "").strip()
    password = f.get("password") or ""
    if not email or not password:
        return RedirectResponse("/garmin?error=datos", status_code=303)
    try:
        status, result = garmin_adapter.start_sign_in(email, password)
    except Exception:
        return RedirectResponse("/garmin?error=garmin", status_code=303)
    finally:
        password = None  # noqa: F841
    if status == "needs_mfa":
        _prune_mfa_state()
        _MFA_STATE[_mfa_key(request)] = (result, time.monotonic())
        conn, _, t = _ctx(person_id)
        return _render("garmin.html", **_garmin_ctx(t, conn, person_id, mfa=True))
    conn, _, _ = _ctx(person_id)
    garmin_cuenta.guardar(conn, person_id, result)
    return RedirectResponse("/garmin", status_code=303)


@app.post("/garmin/codigo")
@_cierra_conexion_async
async def garmin_codigo(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await _form(request)
    code = (f.get("code") or "").strip()
    key = _mfa_key(request)
    entry = _MFA_STATE.get(key)
    if entry is None or time.monotonic() - entry[1] > _MFA_TTL_SECONDS:
        return RedirectResponse("/garmin?error=mfa_expired", status_code=303)
    client_state_json, _ = entry
    try:
        token = garmin_adapter.finish_sign_in(client_state_json, code)
    except Exception:
        # A wrong code may be retried within the same TTL window -- the state is kept, not popped.
        conn, _, t = _ctx(person_id)
        return _render("garmin.html",
                       **_garmin_ctx(t, conn, person_id, mfa=True, form_error=t("garmin.error.generic")))
    finally:
        code = None  # noqa: F841
    _MFA_STATE.pop(key, None)
    conn, _, _ = _ctx(person_id)
    garmin_cuenta.guardar(conn, person_id, token)
    return RedirectResponse("/garmin", status_code=303)


@app.post("/garmin/desconectar")
@_cierra_conexion
def garmin_desconectar(person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, _ = _ctx(person_id)
    garmin_cuenta.borrar(conn, person_id)
    return RedirectResponse("/garmin", status_code=303)


@app.get("/privacidad/consentimiento")
@_cierra_conexion
def consentimiento_form(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    _, _, t = _ctx(person_id)
    return_to = cuenta.safe_return_to(request.query_params.get("return_to"))
    return _render("consentimiento.html", t=t, tab=None, return_to=return_to)


@app.post("/privacidad/consentimiento")
@_cierra_conexion_async
async def consentimiento_decide(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await _form(request)
    return_to = cuenta.safe_return_to(f.get("return_to"))
    if f.get("decision") == "acepto":
        conn, _, _ = _ctx(person_id)
        privacidad.record_consent(conn, person_id)
        return RedirectResponse(return_to, status_code=303)
    conn = db.connect()
    db.init(conn)
    sid = cuenta.end_session(conn, request.cookies.get(cuenta.SESSION_COOKIE))
    conn.close()
    resp = RedirectResponse(cuenta.logout_url(sid), status_code=303)
    cuenta.clear_session_cookie(resp)
    return resp


@app.get("/privacidad")
@_cierra_conexion
def privacidad_view(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = _ctx(person_id)
    row = conn.execute(
        "SELECT accepted_at FROM consentimiento WHERE person_id = ? AND version = ?",
        (person_id, privacidad.CONSENT_VERSION),
    ).fetchone()
    accepted_date = _short_date(t, datetime.fromisoformat(row["accepted_at"]).date()) if row else None
    es_hosted = cuenta.hosted()
    reauth_ok = None
    if es_hosted and request.query_params.get("reauth") == "1":
        token = request.cookies.get(cuenta.SESSION_COOKIE)
        reauth_ok = cuenta.reciente(conn, token)
    return _render("privacidad.html", t=t, tab="settings", version=privacidad.CONSENT_VERSION,
                   accepted_date=accepted_date, delete_word=t("privacidad.borrar_palabra"),
                   error=request.query_params.get("error") == "confirmacion",
                   es_hosted=es_hosted, reauth_ok=reauth_ok, turnos=_turnos_on(conn, person_id))


@app.get("/privacidad/exportar")
@_cierra_conexion
def privacidad_exportar(person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = _ctx(person_id)
    data = privacidad.export_person(conn, person_id)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="body-{date.today().isoformat()}.json"'})


@app.post("/privacidad/borrar")
@_cierra_conexion_async
async def privacidad_borrar(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await _form(request)
    conn, _, t = _ctx(person_id)
    if cuenta.hosted():
        token = request.cookies.get(cuenta.SESSION_COOKIE)
        if not cuenta.reciente(conn, token):
            conn.close()
            return_to = quote("/privacidad?reauth=1", safe="")
            return RedirectResponse(
                f"/login/cuenta/start?prompt=login&max_age=600&return_to={return_to}", status_code=303
            )
    if (f.get("confirmacion") or "").strip() != t("privacidad.borrar_palabra"):
        conn.close()
        return RedirectResponse("/privacidad?error=confirmacion", status_code=303)
    privacidad.delete_person(conn, person_id)
    conn.close()
    if cuenta.hosted():
        return RedirectResponse(cuenta.logout_url(None), status_code=303)
    return RedirectResponse("/", status_code=303)


# ---- Device keys (hosted only): «Conexión del PC» on Ajustes, BODY-DISPOSITIVO-1 -----------------

_ALCANCE_LABELS = {"turnos:subir": "dispositivos.scope.turnos", "vida:leer": "dispositivos.scope.vida"}


def _dispositivo_row(t: T, d: dict) -> dict:
    return {
        "id": d["id"], "nombre": d["nombre"],
        "alcances": ", ".join(t(_ALCANCE_LABELS[a]) for a in d["alcances"].split() if a in _ALCANCE_LABELS),
        "creado": _short_date(t, datetime.fromisoformat(d["creado_en"]).date()),
        "usado": _short_date(t, date.fromisoformat(d["usado_en"])) if d["usado_en"] else None,
    }


@app.get("/ajustes/dispositivos")
@_cierra_conexion
def dispositivos_view(person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, t = _ctx(person_id)
    items = [_dispositivo_row(t, d) for d in dispositivo.listar(conn, person_id)]
    resp = _render("dispositivos.html", t=t, tab="settings", items=items, new_key=None,
                   turnos=_turnos_on(conn, person_id))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/ajustes/dispositivos")
@_cierra_conexion_async
async def dispositivos_crear(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await _form(request)
    conn, _, t = _ctx(person_id)
    nombre = (f.get("nombre") or "").strip()
    if not nombre:
        items = [_dispositivo_row(t, d) for d in dispositivo.listar(conn, person_id)]
        resp = _render("dispositivos.html", t=t, tab="settings", items=items, new_key=None,
                       turnos=_turnos_on(conn, person_id))
        resp.headers["Cache-Control"] = "no-store"
        return resp
    alcances = set()
    if f.get("alcance_turnos") == "1":
        alcances.add("turnos:subir")
    if f.get("alcance_vida") == "1":
        alcances.add("vida:leer")
    new_key = dispositivo.crear(conn, person_id, nombre, alcances)
    items = [_dispositivo_row(t, d) for d in dispositivo.listar(conn, person_id)]
    resp = _render("dispositivos.html", t=t, tab="settings", items=items, new_key=new_key,
                   turnos=_turnos_on(conn, person_id))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/ajustes/dispositivos/{dispositivo_id}/revocar")
@_cierra_conexion
def dispositivos_revocar(dispositivo_id: int, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, _ = _ctx(person_id)
    dispositivo.revocar(conn, person_id, dispositivo_id)
    return RedirectResponse("/ajustes/dispositivos", status_code=303)


# ---- Device-key API (hosted only): a PC script sends `Authorization: Bearer <key>` -----------------

_TURNOS_FILENAME_RE = re.compile(r"^turnos-.*\.json$")
MAX_TURNOS_BODY_BYTES = 256 * 1024


@app.post("/api/turnos")
@_cierra_conexion_async
async def api_turnos(request: Request, person_id: int = Depends(identity.person_from_device("turnos:subir"))):
    file_name = request.headers.get("x-body-archivo") or ""
    if not _TURNOS_FILENAME_RE.match(file_name):
        raise HTTPException(400)
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_TURNOS_BODY_BYTES:
        raise HTTPException(413)
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_TURNOS_BODY_BYTES:
            raise HTTPException(413)
    body = bytes(chunks)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "archivo_rechazado", "motivo": "esquema"}, status_code=422)
    if not isinstance(data, dict):
        return JSONResponse({"error": "archivo_rechazado", "motivo": "esquema"}, status_code=422)
    conn, _, _ = _ctx(person_id)
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch WHERE person_id = ?", (person_id,)
    ).fetchone()["n"]
    try:
        batch_id = shift_import.propose_data(conn, data, file_name, person_id)
    except shift_import.ImportRefused as e:
        return JSONResponse(
            {"error": "archivo_rechazado", "motivo": shift_import.refusal_motivo(e)}, status_code=422
        )
    after = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch WHERE person_id = ?", (person_id,)
    ).fetchone()["n"]
    turnos = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch_item WHERE batch_id = ?", (batch_id,)
    ).fetchone()["n"]
    return {"lote": batch_id, "turnos": turnos, "nuevo": after > before}


@app.get("/api/vida/semana")
@_cierra_conexion
def api_vida_semana(semana: str | None = None,
                     person_id: int = Depends(identity.person_from_device("vida:leer"))):
    if semana:
        try:
            monday = date.fromisoformat(semana)
        except ValueError:
            raise HTTPException(400)
        if monday.weekday() != 0:
            raise HTTPException(400)
    else:
        monday = vida_export.current_week_monday()
    conn, _, _ = _ctx(person_id)
    data = vida_export.week_section(conn, monday, person_id)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


SW_TEMPLATE = """// Body service worker: caches only static files, never health pages.
const C='body-static-HASHPLACEHOLDER';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
 if(u.origin===location.origin&&u.pathname.startsWith('/static/')){
  e.respondWith(caches.open(C).then(c=>c.match(e.request).then(r=>r||fetch(e.request).then(n=>{c.put(e.request,n.clone());return n}))));}});
"""

SW = SW_TEMPLATE.replace("HASHPLACEHOLDER", STATIC_HASH)


@app.get("/sw.js")
def sw():
    return Response(SW, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


def _ensure_streams() -> None:
    """pythonw has no console, so sys.stdout/sys.stderr are None -- give uvicorn's logging a stream to write to."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def main() -> None:
    _, ignorados = entorno.cargar()
    _ensure_streams()
    import uvicorn

    if ignorados > 0:
        print(f".env: {ignorados} line(s) ignored -- only BODY_AGENTE_* names are read from this file.")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    request_logger = logging.getLogger("body.request")
    request_logger.setLevel(logging.INFO)
    request_logger.addHandler(handler)
    request_logger.propagate = False

    p = argparse.ArgumentParser(prog="python -m body.web")
    p.add_argument("--lan", action="store_true", help="listen on the home network (for the phone)")
    p.add_argument("--port", type=int, default=8770)
    a = p.parse_args()
    print(listen.describe(a.lan, a.port))
    uvicorn.run(app, host=listen.listen_host(a.lan), port=a.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
