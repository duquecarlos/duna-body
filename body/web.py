"""Body web app. Local only: http://127.0.0.1:8770 (add --lan to open it on the phone at home).

    python -m body.web
    python -m body.web --lan

Screens follow docs/DESIGN.md. Every word comes from body/i18n (es default, en full version).
Health values never go into URLs or logs (access log is off; URLs carry only dates).
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, garmin_adapter, readiness
from .baselines import baseline
from .i18n import LANGS, T

HERE = Path(__file__).parent
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())

UNITS = {"bpm": "unit.bpm", "ms": "unit.ms", "pts": "unit.pts", "steps": "unit.steps", "kcal": "unit.kcal",
         "brpm": "unit.brpm"}
OF_100 = {"readiness", "stress_avg", "body_battery_high", "body_battery_low", "body_battery_wake", "sleep_score"}
TODAY_CARDS = ["sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake"]
STAGES = [("sleep_deep_min", "metric.sleep_deep_min", "var(--night)"),
          ("sleep_light_min", "metric.sleep_light_min", "color-mix(in srgb, var(--night) 45%, var(--card))"),
          ("sleep_rem_min", "metric.sleep_rem_min", "var(--body)"),
          ("sleep_awake_min", "metric.sleep_awake_min", "var(--amber)")]


def _ctx():
    conn = db.connect()
    db.init(conn)
    person = db.person(conn)
    return conn, person, T(person.get("language", "es"))


def _render(name: str, **kw) -> HTMLResponse:
    return HTMLResponse(env.get_template(name).render(**kw))


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


def _delta(conn, t: T, d: dict, day: str, v) -> tuple[str, str]:
    """Delta vs your 30-day average: text and css class (good/bad/flat)."""
    if v is None:
        return "", "flat"
    b = baseline(conn, d["key"], day)
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


def _latest_day(conn) -> str | None:
    days = db.days_with_data(conn)
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
    ts = db.last_sync(conn)
    if not ts:
        return None
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    local = datetime.fromisoformat(ts).astimezone(tz)
    return local.strftime("%H:%M") if local.date() == datetime.now(tz).date() else None


def _parse_day(day: str) -> date:
    try:
        return date.fromisoformat(day)
    except ValueError:
        raise HTTPException(404)


@app.get("/")
def today():
    conn, person, t = _ctx()
    day = _latest_day(conn)
    if not day:
        cmd = "python -m body.pull" if garmin_adapter.has_token() else "python -m body.login"
        return _render("soon.html", t=t, tab="today", title=t("today.title"), text=t("today.empty", cmd=cmd))
    defs = _defs(conn)
    vals = db.day_values(conn, day)
    r = readiness.compute(conn, day)
    rb = baseline(conn, "readiness", day)
    ready_avg = ready_delta = None
    ready_cls = "flat"
    if r.score is not None and rb:
        ready_avg = round(rb.mean)
        ready_delta, ready_cls = _delta(conn, t, defs["readiness"], day, r.score)
    cards = []
    for k in TODAY_CARDS:
        v = vals.get(k)
        delta, cls = _delta(conn, t, defs[k], day, v)
        cards.append({"key": k, "label": t("metric." + k), "value": _value(t, k, v),
                      "unit": _unit(t, defs[k]) if v is not None else "", "delta": delta, "cls": cls})
    return _render("today.html", t=t, tab="today", day=day, date_long=t.long_date(date.fromisoformat(day)),
                   synced=_synced(conn, person), r=r, reading=_reading(t, r), ready_avg=ready_avg,
                   ready_delta=ready_delta, ready_cls=ready_cls, cards=cards,
                   missing_txt=", ".join(t("part." + m) for m in r.missing))


@app.get("/how/{day}")
def how(day: str):
    d = _parse_day(day)
    conn, person, t = _ctx()
    r = readiness.compute(conn, d.isoformat())
    parts = []
    for p in r.parts:
        ref_note = ""
        if p.key == "sleep":
            value, ref, ref_note = t.minutes(p.value), t.minutes(p.reference), t("how.need_short")
        else:
            value, ref = t.num(p.value), (t.num(p.reference) if p.reference is not None else "—")
        label = t("part." + p.key)
        parts.append({"label": label[:1].upper() + label[1:], "value": value, "ref": ref, "ref_note": ref_note,
                      "score": t.num(p.subscore), "weight": f"{round(p.weight * 100)} %"})
    missing = [t("part." + m) for m in r.missing]
    return _render("how.html", t=t, tab="today", r=r, parts=parts, date_long=t.long_date(d),
                   missing=[m[:1].upper() + m[1:] for m in missing])


@app.get("/day/{day}")
def day_view(day: str):
    d = _parse_day(day)
    conn, person, t = _ctx()
    key = d.isoformat()
    data = db.load_day(conn, key)
    vals = {m["key"]: m["value"] for m in data["metrics"]}
    defs = _defs(conn)
    cards = []
    for k, df in defs.items():
        v = vals.get(k)
        delta, cls = _delta(conn, t, df, key, v)
        cards.append({"key": k, "label": t("metric." + k), "value": _value(t, k, v), "raw": v,
                      "unit": _unit(t, df), "delta": delta, "cls": cls, "est": df["kind"] == "estimated"})
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
    shifts = [{"kind": s["kind"], "text": f"{s['kind']}{' (' + s['code'] + ')' if s['code'] else ''} "
               f"{(s['start_local'] or '')[11:16]} → {(s['end_local'] or '')[11:16]}"} for s in data["shifts"]]
    return _render("day.html", t=t, tab="today", date_long=t.long_date(d), cards=cards, sleep=sleep,
                   activities=acts, shifts=shifts, has_data=any(v is not None for v in vals.values()),
                   prev=(d - timedelta(1)).isoformat(), next=(d + timedelta(1)).isoformat())


@app.get("/shifts")
def shifts():
    _, _, t = _ctx()
    return _render("soon.html", t=t, tab="shifts", title=t("nav.shifts"), text=t("soon.shifts"))


@app.get("/trends")
def trends():
    _, _, t = _ctx()
    return _render("soon.html", t=t, tab="trends", title=t("nav.trends"), text=t("soon.trends"))


@app.get("/goals")
def goals():
    _, _, t = _ctx()
    return _render("soon.html", t=t, tab="goals", title=t("nav.goals"), text=t("soon.goals"))


@app.get("/settings")
def settings():
    _, _, t = _ctx()
    return _render("settings.html", t=t, tab="settings")


@app.post("/settings/language")
async def set_language(request: Request):
    lang = parse_qs((await request.body()).decode()).get("lang", [""])[0]
    if lang in LANGS:
        conn, _, _ = _ctx()
        db.set_language(conn, lang)
    return RedirectResponse("/settings", status_code=303)


SW = """// Body service worker: caches only static files, never health pages.
const C='body-static-v2';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
 if(u.origin===location.origin&&u.pathname.startsWith('/static/')){
  e.respondWith(caches.open(C).then(c=>c.match(e.request).then(r=>r||fetch(e.request).then(n=>{c.put(e.request,n.clone());return n}))));}});
"""


@app.get("/sw.js")
def sw():
    return Response(SW, media_type="text/javascript")


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(prog="python -m body.web")
    p.add_argument("--lan", action="store_true", help="listen on the home network (for the phone)")
    p.add_argument("--port", type=int, default=8770)
    a = p.parse_args()
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    print(f"Body app on http://{'<this-PC-IP>' if a.lan else '127.0.0.1'}:{a.port}")
    uvicorn.run(app, host=host, port=a.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
