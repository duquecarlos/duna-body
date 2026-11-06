"""The page. Local only: http://127.0.0.1:8770 (add --lan to open it on the phone at home).

    python -m body.web
    python -m body.web --lan
"""
import argparse
from datetime import date, datetime, timedelta
from html import escape

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, garmin_adapter

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

CSS = """
:root { --bg:#f6f7f9; --card:#fff; --ink:#15181d; --muted:#667085; --line:#e4e7ec; --accent:#0e7c66; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --card:#181b21; --ink:#e8eaed; --muted:#98a2b3; --line:#2a2f37; --accent:#3fc1a0; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:16px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width:720px; margin:0 auto; padding:16px; }
header { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:16px; }
header h1 { font-size:20px; margin:0; }
nav a { color:var(--accent); text-decoration:none; padding:8px 10px; border:1px solid var(--line);
        border-radius:8px; background:var(--card); display:inline-block; min-width:44px; text-align:center; }
.day { color:var(--muted); font-size:14px; }
.grid { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:10px; }
@media (min-width:600px) { .grid { grid-template-columns:repeat(3, minmax(0,1fr)); } }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px; }
.card .label { color:var(--muted); font-size:13px; }
.card .value { font-size:24px; font-weight:600; font-variant-numeric:tabular-nums; margin-top:2px; }
.card .unit { font-size:13px; color:var(--muted); font-weight:400; margin-left:3px; }
.empty .value { color:var(--muted); font-weight:400; }
h2 { font-size:16px; margin:20px 0 8px; }
ul.list { list-style:none; margin:0; padding:0; }
ul.list li { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:10px 12px; margin-bottom:8px; }
.note { color:var(--muted); font-size:14px; }
code { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:1px 5px; }
"""


def _fmt(key: str, value):
    if value is None:
        return "—", ""
    if key == "sleep_minutes":
        h, mn = divmod(int(value), 60)
        return f"{h} h {mn:02d}", ""
    if key == "steps":
        return f"{int(value):,}".replace(",", " "), ""
    return (f"{value:.0f}" if float(value).is_integer() or abs(value) >= 10 else f"{value:.1f}"), None


def _t(iso):
    """'2026-11-04T23:00' -> 'Wed 23:00'."""
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).strftime("%a %H:%M")
    except ValueError:
        return iso


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='referrer' content='no-referrer'>"
        f"<title>{escape(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


@app.get("/")
def home():
    conn = db.connect()
    db.init(conn)
    days = db.days_with_data(conn)
    if not days:
        signed = garmin_adapter.has_token()
        step = ("Run <code>python -m body.pull</code> to bring in your days."
                if signed else "Sign in first: <code>python -m body.login</code>, then <code>python -m body.pull</code>.")
        return _page("Body", f"<header><h1>Body</h1></header><p class='note'>No data yet. {step}</p>")
    return RedirectResponse(f"/day/{days[0]}")


@app.get("/day/{day}")
def day_view(day: str):
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(404)
    conn = db.connect()
    db.init(conn)
    data = db.load_day(conn, d.isoformat())
    prev_d, next_d = (d - timedelta(1)).isoformat(), (d + timedelta(1)).isoformat()

    cards = []
    for m in data["metrics"]:
        val, unit = _fmt(m["key"], m["value"])
        if m.get("kind") == "estimated" and m["value"] is not None:
            val = "≈ " + val
        unit = m["unit"] if unit is None else unit
        cls = "card empty" if m["value"] is None else "card"
        unit_html = f"<span class='unit'>{escape(unit)}</span>" if unit and m["value"] is not None else ""
        cards.append(f"<div class='{cls}'><div class='label'>{escape(m['label'])}</div>"
                     f"<div class='value'>{escape(val)}{unit_html}</div></div>")

    parts = [
        "<header><h1>Body</h1><nav>"
        f"<a href='/day/{prev_d}' aria-label='Previous day'>&#8249;</a> "
        f"<a href='/day/{next_d}' aria-label='Next day'>&#8250;</a></nav></header>",
        f"<p class='day'>{escape(d.strftime('%A %d %B %Y'))}</p>",
        f"<section class='grid'>{''.join(cards)}</section>",
    ]
    if data["sleep"]:
        s = data["sleep"]
        parts.append(f"<h2>Sleep</h2><ul class='list'><li>{escape(_t(s['start_local']))} → "
                     f"{escape(_t(s['end_local']))}</li></ul>")
    parts.append("<h2>Shift</h2>")
    if data["shifts"]:
        parts.append("<ul class='list'>" + "".join(
            f"<li>{escape(sh['kind'])}{' (' + escape(sh['code']) + ')' if sh['code'] else ''} "
            f"{escape(_t(sh['start_local']))} → {escape(_t(sh['end_local']))}</li>"
            for sh in data["shifts"]) + "</ul>")
    else:
        parts.append("<p class='note'>No shift recorded for this day yet.</p>")
    parts.append("<h2>Activities</h2>")
    if data["activities"]:
        items = []
        for a in data["activities"]:
            mins = f"{a['duration_s'] / 60:.0f} min" if a["duration_s"] else ""
            km = f" · {a['distance_m'] / 1000:.1f} km" if a["distance_m"] else ""
            hr = f" · {a['avg_hr']:.0f} bpm avg" if a["avg_hr"] else ""
            items.append(f"<li><strong>{escape(a['name'] or a['type'] or 'Activity')}</strong><br>"
                         f"<span class='note'>{escape(mins + km + hr)}</span></li>")
        parts.append("<ul class='list'>" + "".join(items) + "</ul>")
    else:
        parts.append("<p class='note'>No activities.</p>")
    return _page(f"Body · {d.isoformat()}", "".join(parts))


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
