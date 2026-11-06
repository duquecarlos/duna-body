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

from . import db, garmin_adapter, history, listen, profile, readiness, shift_import
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


def _batch_date(t: T, received: str) -> str:
    return _short_date(t, datetime.fromisoformat(received).date())


@app.get("/")
def today():
    conn, person, t = _ctx()
    day = _latest_day(conn)
    proposed_count = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch WHERE person_id = ? AND status = 'proposed'",
        (db.DEFAULT_PERSON_ID,),
    ).fetchone()["n"]
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
                   missing_txt=", ".join(t("part." + m) for m in r.missing), proposed_count=proposed_count)


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
def shifts(request: Request):
    conn, person, t = _ctx()
    rows = conn.execute(
        "SELECT * FROM shift_batch WHERE person_id = ? AND status = 'proposed' ORDER BY received, extracted_at",
        (db.DEFAULT_PERSON_ID,),
    ).fetchall()
    result_msg = None
    qp = request.query_params
    if qp.get("done") == "1":
        kept = int(qp.get("kept_manual", 0)) + int(qp.get("kept_newer", 0))
        result_msg = t("shifts.confirm_all_result", batches=qp.get("batches", "0"), applied=qp.get("applied", "0"))
        if kept:
            result_msg += " " + t("shifts.confirm_all_kept", n=kept)
    if len(rows) > 1:
        summary = shift_import.summary_proposed(conn)
        batches = [
            {"batch": dict(b), "received_date": _batch_date(t, b["received"]),
             "preview": shift_import.preview(conn, b["id"])}
            for b in rows
        ]
        return _render("shifts.html", t=t, tab="shifts", batch=None, many=True, batches=batches,
                       summary_from=t.month_year(date.fromisoformat(summary["first_day"])),
                       summary_to=t.month_year(date.fromisoformat(summary["last_day"])),
                       summary_days=summary["day_count"], summary_nights=summary["night_count"],
                       result_msg=result_msg)
    if rows:
        proposed = rows[0]
        prev = shift_import.preview(conn, proposed["id"])
        return _render("shifts.html", t=t, tab="shifts", batch=dict(proposed), many=False,
                       received_date=_batch_date(t, proposed["received"]), preview=prev, result_msg=result_msg)
    confirmed = shift_import.latest_confirmed(conn)
    return _render("shifts.html", t=t, tab="shifts", batch=None, many=False,
                   confirmed_date=_batch_date(t, confirmed["received"]) if confirmed else None,
                   result_msg=result_msg)


@app.post("/shifts/confirm")
async def shifts_confirm(request: Request):
    batch_id = parse_qs((await request.body()).decode()).get("batch_id", [""])[0]
    if batch_id.isdigit():
        conn, _, _ = _ctx()
        try:
            shift_import.confirm(conn, int(batch_id))
        except (shift_import.ImportRefused, shift_import.SlotIndexMissing):
            pass
    return RedirectResponse("/shifts", status_code=303)


@app.post("/shifts/confirm-all")
async def shifts_confirm_all():
    conn, _, _ = _ctx()
    try:
        result = shift_import.confirm_all(conn)
    except (shift_import.ImportRefused, shift_import.SlotIndexMissing):
        return RedirectResponse("/shifts", status_code=303)
    return RedirectResponse(
        f"/shifts?done=1&batches={result['batches']}&applied={result['days_applied']}"
        f"&kept_manual={result['days_kept_manual']}&kept_newer={result['days_kept_newer']}",
        status_code=303,
    )


@app.post("/shifts/dismiss")
async def shifts_dismiss(request: Request):
    batch_id = parse_qs((await request.body()).decode()).get("batch_id", [""])[0]
    if batch_id.isdigit():
        conn, _, _ = _ctx()
        shift_import.dismiss(conn, int(batch_id))
    return RedirectResponse("/shifts", status_code=303)


TREND_KEYS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake", "steps"]
WEEK_KEYS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr"]


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


def _nights(conn, start: date, end: date) -> set[str]:
    return {r["day"] for r in conn.execute("SELECT day FROM shift WHERE person_id = ? AND kind = 'night' AND day "
                                           "BETWEEN ? AND ?", (db.DEFAULT_PERSON_ID, start.isoformat(),
                                                               end.isoformat()))}


@app.get("/trends")
def trends(r: str = "all", m: str | None = None):
    """r = 7 | 30 | 90 | all (the whole history, monthly). m = YYYY-MM opens one month day by day (from a tap on
    the monthly line). The URL carries a range or a month, never a value."""
    conn, person, t = _ctx()
    defs = _defs(conn)
    last = _latest_day(conn)
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
    nights = _nights(conn, start, end) if start else set()
    cards, since, days = [], None, 0
    for k in TREND_KEYS:
        p = profile.metric_profile(conn, k)
        if not p:
            continue
        d = defs[k]
        since = since or p.first_day
        days = max(days, p.days)
        spread = p.high - p.low
        if start:
            w = profile.window(conn, k, start, end)
            if month:
                delta, cls = _compare(t, k, d, w.mean, p.all_mean, spread, "trends.vs_hist")
            else:
                delta, cls = _compare(t, k, d, w.mean, w.prev_mean, spread, "trends.vs_prev")
            fig, svg = w.mean, profile.window_svg(w, p.low, p.high, nights)
        else:
            delta, cls = _compare(t, k, d, p.last30, p.all_mean, spread, "trends.vs_hist")
            fig, svg = p.last30, profile.month_svg(p)
        cards.append({"key": k, "question": t("trends.q." + k), "est": d["kind"] == "estimated",
                      "last30": _value(t, k, fig), "unit": _unit(t, d) if fig is not None else "", "delta": delta,
                      "cls": cls, "svg": svg, "low": _value(t, k, p.low), "high": _value(t, k, p.high)})
    if not cards:
        return _render("soon.html", t=t, tab="trends", title=t("nav.trends"), text=t("soon.trends"))
    week = None
    profs = [profile.metric_profile(conn, k) for k in WEEK_KEYS]
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
    all_days = db.days_with_data(conn)
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
                   month_title=t.month_year(month) if month else None)


def _short_date(t: T, d: date) -> str:
    """19 jun 2024 style: day, abbreviated month, year -- no weekday, unlike t.long_date."""
    return f"{d.day} {t('date.months')[d.month - 1][:3]} {d.year}"


def _kind_label(t: T, info: dict | None) -> str:
    if not info:
        return "—"
    return t("activity.type." + info["key"]) if info["known"] else info["fallback_label"]


def _finding_text(t: T, defs: dict, f: dict) -> str:
    label = t("metric." + f["metric"])
    value = _value(t, f["metric"], f["value"])
    unit = _unit(t, defs[f["metric"]])
    if f["axis"] == "weekday":
        return t("history.find.weekday", metric=label, day=t("date.days")[f["index"]], value=value, unit=unit)
    return t("history.find.month", metric=label, month=t("date.months")[f["index"]], value=value, unit=unit)


TRAINING_COLORS = ["var(--body)", "var(--night)", "var(--amber)", "var(--info)", "var(--est)"]
OTHER_COLOR = "var(--muted)"


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
    yoy, yoy_ctx = train["yoy"], None
    if yoy:
        yoy_ctx = {"now": t("history.training_yoy_now", sessions=t.num(yoy["sessions_now"]),
                             minutes=t.minutes(yoy["minutes_now"])),
                   "prev": t("history.training_yoy_prev", sessions=t.num(yoy["sessions_year_ago"]),
                             minutes=t.minutes(yoy["minutes_year_ago"]))}
    return {"types": types, "months": months,
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
def history_view():
    conn, person, t = _ctx()
    cov = history.coverage(conn)
    if not cov:
        return _render("soon.html", t=t, tab="trends", title=t("history.title"), text=t("soon.trends"))
    defs = _defs(conn)
    train = history.training(conn)
    pat = history.patterns(conn)
    shift_data = history.by_shift(conn)

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
                   findings=[_finding_text(t, defs, f) for f in pat["findings"]],
                   shift=_shift_ctx(t, defs, shift_data))


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
    print(listen.describe(a.lan, a.port))
    uvicorn.run(app, host=listen.listen_host(a.lan), port=a.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
