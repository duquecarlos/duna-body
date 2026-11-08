"""Body web app. Local only: http://127.0.0.1:8770 (add --lan to open it on the phone at home).

    python -m body.web
    python -m body.web --lan

Screens follow docs/DESIGN.md. Every word comes from body/i18n (es default, en full version).
Health values never go into URLs or logs (access log is off; URLs carry only dates).
"""
from __future__ import annotations

import argparse
import hashlib
import html
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import (compartir, cuenta, db, entorno, fechas, garmin_adapter, garmin_cuenta, goals,
               history, household, identity, listen, profile, readiness, turnos_costo, tutoriales)
from .baselines import baseline
from .i18n import DEFAULT as DEFAULT_LANG
from .i18n import T
from .identity import ConsentRequired, DispositivoNoAutorizado, DispositivoSinAlcance, LoginRequired
from .request_log import RequestLogMiddleware
from .rutas import (ajustes, asistente, ayuda, comun, dispositivos, metas,
                     privacidad as privacidad_rutas, turnos)

HERE = Path(__file__).parent


@asynccontextmanager
async def _lifespan(app: FastAPI):
    cuenta.ensure_configured()
    garmin_cuenta.ensure_configured()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


class _CaminoActualMiddleware:
    """Sets comun._RUTA_ACTUAL from the raw ASGI path before routing, for base.html's one
    in-context "¿Cómo se hace?" helper (tutoriales-v1 section 5.2, decision 3) -- comun owns the
    contextvar itself (BODY-WEB-ESTRUCTURA-1-M), since the lambda that reads it lives there."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = comun._RUTA_ACTUAL.set(scope.get("path", ""))
        try:
            await self.app(scope, receive, send)
        finally:
            comun._RUTA_ACTUAL.reset(token)


app.add_middleware(_CaminoActualMiddleware)
app.add_middleware(RequestLogMiddleware)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(ayuda.router)
app.include_router(privacidad_rutas.router)
app.include_router(dispositivos.router)
app.include_router(ajustes.router)
app.include_router(asistente.router)
app.include_router(metas.router)
app.include_router(turnos.router)


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


_CUENTA_DOWN_TEMPLATE = comun.env.from_string(
    '{% extends "base.html" %}{% block main %}'
    '<div class="top"><div><h1>{{ t("app.name") }}</h1></div></div>'
    '<div class="card"><p class="read">{{ t("cuenta.down") }}</p></div>'
    "{% endblock %}"
)


def _cuenta_down_page() -> HTMLResponse:
    return HTMLResponse(_CUENTA_DOWN_TEMPLATE.render(t=T(DEFAULT_LANG), tab=None))

TODAY_CARDS = ["sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake"]
STAGES = [("sleep_deep_min", "metric.sleep_deep_min", "var(--night)"),
          ("sleep_light_min", "metric.sleep_light_min", "color-mix(in srgb, var(--night) 45%, var(--card))"),
          ("sleep_rem_min", "metric.sleep_rem_min", "var(--body)"),
          ("sleep_awake_min", "metric.sleep_awake_min", "var(--amber)")]


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
    amount = t.minutes(abs(diff)) if comun._is_minutes(d["key"]) else t.num(abs(diff))
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
        return t("sync.runs") + " " + t("sync.last", date=comun._short_date(t, last_date))
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
        return HTMLResponse(comun.env.get_template("acceso_rechazado.html").render(t=T(DEFAULT_LANG)),
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
        return comun._render("sin_acceso.html", t=T(DEFAULT_LANG),
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
    f = await comun._form(request)
    conn = db.connect()
    db.init(conn)
    ok = cuenta.handle_backchannel_logout(conn, f.get("logout_token", ""))
    conn.close()
    return Response(status_code=200 if ok else 400)


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
        if not goals.habit_due_today(conn, h, person_id, today):
            continue
        context = comun._habit_cadence(h).get("context")
        out.append({"id": h["id"], "title": h["title"], "goal_title": h["goal_title"],
                    "context": comun._context_label(t, context), "context_cls": comun._context_chip_cls(context),
                    "checked": comun._habit_checked(conn, h["id"], person_id, today.isoformat())})
    return out


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
@comun._cierra_conexion
def today(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    day = _latest_day(conn, person_id)
    proposed_count = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch WHERE person_id = ? AND status = 'proposed'",
        (person_id,),
    ).fetchone()["n"]
    primeros = tutoriales.primeros_pasos(conn, person_id, cuenta.hosted())
    primeros_pasos = primeros if any(not p["hecho"] for p in primeros) else None
    today_local = comun._today_local(person)
    turno_ctx = _hoy_turno_ctx(t, conn, person_id, today_local)
    if not day:
        return comun._render("soon.html", t=t, tab="today", title=t("today.title"),
                       text=_sync_status_text(conn, person, t), primeros_pasos=primeros_pasos,
                       proposed_count=proposed_count, turno=turno_ctx, turnos=turnos_on)
    defs = comun._defs(conn)
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
        cards.append({"key": k, "label": t("metric." + k), "value": comun._value(t, k, v),
                      "unit": comun._unit(t, defs[k]) if v is not None else "", "delta": delta, "cls": cls})
    return comun._render("today.html", t=t, tab="today", day=day, date_long=t.long_date(date.fromisoformat(day)),
                   synced=_synced(conn, person), r=r, reading=_reading(t, r), ready_avg=ready_avg,
                   ready_delta=ready_delta, ready_cls=ready_cls, cards=cards,
                   missing_txt=", ".join(t("part." + m) for m in r.missing), proposed_count=proposed_count,
                   turno=turno_ctx, turnos=turnos_on,
                   plan_habits=_today_due_habits(conn, t, person_id, today_local),
                   today_iso=today_local.isoformat(),
                   primeros_pasos=primeros_pasos,
                   hogar_compartido=_hogar_compartido_ctx(conn, t, person_id, defs, today_local))


# readiness.Part.key -> the Tendencias metric it reads from (readiness.Part carries no metric key of its
# own). BODY-PULIDO-1: how()'s part rows link to their own measure through this.
PART_METRIC = {"hrv": "hrv_last_night", "sleep": "sleep_minutes", "battery": "body_battery_wake",
               "resting_hr": "resting_hr", "stress": "stress_avg"}


@app.get("/how/{day}")
@comun._cierra_conexion
def how(day: str, person_id: int = Depends(identity.current_person_id)):
    d = _parse_day(day)
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    today_local = comun._today_local(person)
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
    return comun._render("how.html", t=t, tab="today", r=r, parts=parts, date_long=t.long_date(d),
                   missing=[m[:1].upper() + m[1:] for m in missing], turnos=turnos_on)


@app.get("/day/{day}")
@comun._cierra_conexion
def day_view(day: str, person_id: int = Depends(identity.current_person_id)):
    d = _parse_day(day)
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    today_local = comun._today_local(person)
    key = d.isoformat()
    data = db.load_day(conn, key, person_id)
    vals = {m["key"]: m["value"] for m in data["metrics"]}
    defs = comun._defs(conn)
    cards = []
    for k, df in defs.items():
        v = vals.get(k)
        delta, cls = _delta(conn, t, df, key, v, person_id)
        href = _tendencias_href(k, "dia", key, today_local) if v is not None else None
        cards.append({"key": k, "label": t("metric." + k), "value": comun._value(t, k, v), "raw": v,
                      "unit": comun._unit(t, df), "delta": delta, "cls": cls, "est": df["kind"] == "estimated",
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
        acts.append({"name": _activity_name(t, a), "detail": " · ".join(bits)})
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
    return comun._render("day.html", t=t, tab="today", date_long=t.long_date(d), cards=cards, sleep=sleep,
                   activities=acts, shifts=shifts, has_data=has_data, block_ctx=block_ctx,
                   sync_text=None if has_data else _sync_status_text(conn, person, t),
                   prev=(d - timedelta(1)).isoformat(), next=(d + timedelta(1)).isoformat(),
                   turnos=turnos_on)


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
    if key not in TREND_KEYS and not comun._es_extra_metric(key):
        return None
    todas = "&todas=1" if comun._es_extra_metric(key) else ""
    rango = _rango_que_contiene(date.fromisoformat(dia), today_local)
    r_part = f"{rango}&" if rango else ""
    return f"/trends?{r_part}desde={desde}&dia={dia}{todas}#{key}"


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

# Turnos' own cost-row hrefs need the same "lives behind the extra accordion" check Día's
# _tendencias_href makes above -- comun._es_extra_metric is the one place both read it from
# (BODY-WEB-ESTRUCTURA-6, comun.py's own docstring on _GRUPOS_EXTRA has the full reasoning).
comun._GRUPOS_EXTRA = TREND_GROUPS


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
    amount = t.minutes(abs(diff)) if comun._is_minutes(k) else t.num(abs(diff))
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
    valor_fmt = lambda v: comun._fig_unit(t, k, v, defs)
    if start:
        w = profile.window(conn, k, start, end, person_id)
        if month:
            delta, cls = _compare(t, k, d, w.mean, p.all_mean, spread, "trends.vs_hist")
        else:
            delta, cls = _compare(t, k, d, w.mean, w.prev_mean, spread, "trends.vs_prev")
        etiqueta_dia = lambda day: comun._short_date(t, day)
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
            "last30": comun._value(t, k, fig), "unit": comun._unit(t, d) if fig is not None else "", "delta": delta,
            "cls": cls, "svg": svg, "low": comun._value(t, k, p.low), "high": comun._value(t, k, p.high),
            "sparse": k in SPARSE_KEYS, "subtitle": None}
    return card, p


@app.get("/trends")
@comun._cierra_conexion
def trends(r: str = "all", m: str | None = None, todas: str | None = None, desde: str | None = None,
           dia: str | None = None, person_id: int = Depends(identity.current_person_id)):
    """r = 7 | 30 | 90 | all (the whole history, monthly). m = YYYY-MM opens one month day by day (from a tap on
    the monthly line). todas=1 keeps "Ver todas las medidas" open across a range switch. desde=turnos|dia|como
    arrives from a Turnos cost row, a Dia card or a Como lo calculo row (BODY-TURNOS-ENLACES-1, BODY-PULIDO-1)
    and shows the way back, kept on every range link; dia=<YYYY-MM-DD> names the day to go back to for
    desde=dia/como, and an unparseable dia drops both. The URL carries a range, a month or those flags, never
    a value."""
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    defs = comun._defs(conn)
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
            return comun._render("soon.html", t=t, tab="trends", title=t("nav.trends"),
                           text=t("trends.too_little", days=TREND_MIN_DAYS - have),
                           back_href=back_href, back_label=back_label, turnos=turnos_on)
        return comun._render("soon.html", t=t, tab="trends", title=t("nav.trends"),
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
            mark = wd.index(worse(present)) if spread >= (1 if not comun._is_minutes(k) else 5) else -1
            cols.append([{"text": comun._value(t, k, v), "mark": i == mark} for i, v in enumerate(wd)])
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
    return comun._render("trends.html", t=t, tab="trends", cards=cards, week=week if r == "all" else None,
                   since=t.long_date(date.fromisoformat(since)) if since else "", days=t.num(days),
                   r=r, ranges=RANGES, fig_label=fig_label, nights=bool(nights),
                   month_title=t.month_year(month) if month else None,
                   extra_groups=extra_groups, extra_count=extra_count, todas=todas == "1",
                   desde=desde, dia=desde_dia, back_href=back_href, back_label=back_label, turnos=turnos_on)


def _dia_relativo(t: T, day: str, today_local: date) -> str:
    """«hoy», «ayer», or the short date, for a shared figure's own day (BODY-HOGAR-COMPARTIR-1)."""
    d = date.fromisoformat(day)
    if d == today_local:
        return t("hogar.dia_hoy")
    if d == today_local - timedelta(1):
        return t("hogar.dia_ayer")
    return comun._short_date(t, d)


def _hogar_compartido_ctx(conn, t: T, person_id: int, defs: dict, today_local: date) -> list[dict]:
    """Hoy's «Tu hogar» card: each shared figure visible to this viewer (compartir.visibles_para),
    the app's existing value+unit formatting, and -- DESIGN rule 3's one exception, the row is
    never a link, since nothing of the owner's sits behind it for this viewer to open -- a plain-
    words provenance line (who shared it, that it came from their watch, and «hoy»/«ayer»/the short
    date) in place of one. A plain "no data" line when the owner has no value yet (never a guess)."""
    out = []
    for item in compartir.visibles_para(conn, person_id):
        sin_dato = item["value"] is None
        out.append({
            "name": item["name"],
            "metric_label": t("metric." + item["metric_key"]),
            "sin_dato": sin_dato,
            "value_text": None if sin_dato else comun._fig_unit(t, item["metric_key"], item["value"], defs),
            "procedencia": None if sin_dato else t("today.hogar.procedencia", name=item["name"],
                                                    day=_dia_relativo(t, item["day"], today_local)),
        })
    return out


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


def _activity_name(t: T, a) -> str:
    """Día's activity list (DESIGN rule 10): an unnamed activity shows its translated kind when
    Garmin's own type code is one this app knows (the i18n map `_kind_label` reads); an unknown
    code never prints the raw code, only a plain generic label."""
    if a["name"]:
        return a["name"]
    if a["type"] in history.KNOWN_ACTIVITY_TYPES:
        return t("activity.type." + a["type"])
    return t("activity.generic")


def _finding_text(t: T, defs: dict, f: dict) -> str:
    """A plain sentence, not "Your toughest {label}": the metric in its sentence form (never title case
    mid-sentence) and the direction said from `better` -- a "lower is better" metric reads worst where it
    runs highest, a "higher is better" metric reads worst where it runs lowest."""
    metric = t("metric." + f["metric"] + ".sentence")
    value = comun._value(t, f["metric"], f["value"])
    unit = comun._unit(t, defs[f["metric"]])
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
            vals.append({"text": comun._value(t, k, b["center"]) if b else "—", "mark": (k, axis, idx) in marks})
        return {"label": label, "vals": vals}

    return {"heads": [t("trends.short." + k) for k in present],
            "weekday": [row("weekday", i, t("date.days")[i][:3].capitalize()) for i in range(7)],
            "month": [row("month", i, t("date.months")[i][:3].capitalize()) for i in range(12)]}


def _shift_ctx(t: T, defs: dict, shift_data: dict) -> dict:
    if not shift_data["has_shifts"]:
        return {"has_shifts": False}
    kinds = []
    for row in shift_data["kinds"]:
        metrics = [{"label": t("metric." + k), "value": comun._value(t, k, row["metrics"].get(k)),
                    "unit": comun._unit(t, defs[k]) if row["metrics"].get(k) is not None else ""}
                   for k in history.PATTERN_KEYS]
        kinds.append({"kind": row["kind"], "kind_label": t("shift.kind." + row["kind"]),
                      "days": t("history.days_count", n=t.num(row["days"])), "metrics": metrics})
    return {"has_shifts": True, "kinds": kinds}


@app.get("/history")
@comun._cierra_conexion
def history_view(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    cov = history.coverage(conn, person_id)
    if not cov:
        return comun._render("soon.html", t=t, tab="trends", title=t("history.title"),
                       text=_sync_status_text(conn, person, t), turnos=turnos_on)
    defs = comun._defs(conn)
    train = history.training(conn, person_id)
    pat = history.patterns(conn, person_id)
    shift_data = history.by_shift(conn, person_id)

    since = comun._short_date(t, date.fromisoformat(cov["first_day"]))
    coverage_line = t("history.coverage", since=since, days=t.num(cov["days_present"]))
    gap_line = (t("history.coverage_gap", days=t.num(cov["longest_gap_days"]),
                  start=comun._short_date(t, date.fromisoformat(cov["longest_gap_start"])),
                  end=comun._short_date(t, date.fromisoformat(cov["longest_gap_end"])))
                if cov["longest_gap_days"] > 0 else t("history.coverage_no_gap"))
    hero_finding = _finding_text(t, defs, pat["findings"][0]) if pat["findings"] else None

    return comun._render("history.html", t=t, tab="trends", coverage_line=coverage_line, gap_line=gap_line,
                   activity_count=t.num(cov["activity_count"]), hero_finding=hero_finding,
                   training=_training_ctx(t, train) if train else None,
                   patterns=_pattern_rows(t, defs, pat),
                   findings=[_finding_text(t, defs, f) for f in pat["findings"][1:]],
                   shift=_shift_ctx(t, defs, shift_data), turnos=turnos_on)


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
        "t": t, "tab": "settings", "turnos": db.trabaja_turnos_si(conn, person_id),
        "connected": connected_since is not None,
        "connected_since": comun._short_date(t, datetime.fromisoformat(connected_since).date())
                           if connected_since else None,
        "error_text": _garmin_last_error_text(t, garmin_cuenta.ultimo_error(conn, person_id)),
        "form_error": None, "mfa": False,
    }
    ctx.update(extra)
    return ctx


@app.get("/garmin")
@comun._cierra_conexion
def garmin_view(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, t = comun._ctx(person_id)
    err = request.query_params.get("error")
    form_error = t(_GARMIN_FORM_ERROR_KEYS[err]) if err in _GARMIN_FORM_ERROR_KEYS else None
    return comun._render("garmin.html", **_garmin_ctx(t, conn, person_id, form_error=form_error))


@app.post("/garmin/conectar")
@comun._cierra_conexion_async
async def garmin_conectar(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await comun._form(request)
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
        conn, _, t = comun._ctx(person_id)
        return comun._render("garmin.html", **_garmin_ctx(t, conn, person_id, mfa=True))
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.guardar(conn, person_id, result)
    return RedirectResponse("/garmin", status_code=303)


@app.post("/garmin/codigo")
@comun._cierra_conexion_async
async def garmin_codigo(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await comun._form(request)
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
        conn, _, t = comun._ctx(person_id)
        return comun._render("garmin.html",
                       **_garmin_ctx(t, conn, person_id, mfa=True, form_error=t("garmin.error.generic")))
    finally:
        code = None  # noqa: F841
    _MFA_STATE.pop(key, None)
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.guardar(conn, person_id, token)
    return RedirectResponse("/garmin", status_code=303)


@app.post("/garmin/desconectar")
@comun._cierra_conexion
def garmin_desconectar(person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.borrar(conn, person_id)
    return RedirectResponse("/garmin", status_code=303)


SW_TEMPLATE = """// Body service worker: caches only static files, never health pages.
const C='body-static-HASHPLACEHOLDER';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
 if(u.origin===location.origin&&u.pathname.startsWith('/static/')){
  e.respondWith(caches.open(C).then(c=>c.match(e.request).then(r=>r||fetch(e.request).then(n=>{c.put(e.request,n.clone());return n}))));}});
"""

SW = SW_TEMPLATE.replace("HASHPLACEHOLDER", comun.STATIC_HASH)


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
