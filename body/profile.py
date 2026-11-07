"""Your long-term profile from the whole Garmin history: monthly line, normal range, typical week.

Pure reads over daily_metric. Every number is against your own history, never population tables.
"""
from __future__ import annotations

import html
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from . import db


@dataclass
class MetricProfile:
    key: str
    months: list[tuple[str, float, int]]   # (YYYY-MM, mean, days)
    all_mean: float
    low: float                             # p25 over all history
    high: float                            # p75 over all history
    last30: float | None
    prev30: float | None
    weekday: list[float | None]            # Monday..Sunday means
    days: int
    first_day: str


def metric_profile(conn, key: str, today: str | None = None, *, person_id: int) -> MetricProfile | None:
    rows = conn.execute("SELECT day, value FROM daily_metric WHERE person_id=? AND metric_key=? ORDER BY day",
                        (person_id, key)).fetchall()
    if len(rows) < 14:
        return None
    vals = [r["value"] for r in rows]
    by_month: dict[str, list[float]] = {}
    by_wd: list[list[float]] = [[] for _ in range(7)]
    for r in rows:
        by_month.setdefault(r["day"][:7], []).append(r["value"])
        by_wd[date.fromisoformat(r["day"]).weekday()].append(r["value"])
    end = date.fromisoformat(today or rows[-1]["day"])
    last = [r["value"] for r in rows if end - timedelta(30) < date.fromisoformat(r["day"]) <= end]
    prev = [r["value"] for r in rows if end - timedelta(60) < date.fromisoformat(r["day"]) <= end - timedelta(30)]
    q = statistics.quantiles(vals, n=4)
    return MetricProfile(
        key=key,
        months=[(m, statistics.fmean(v), len(v)) for m, v in sorted(by_month.items()) if len(v) >= 7],
        all_mean=statistics.fmean(vals), low=q[0], high=q[2],
        last30=statistics.fmean(last) if last else None,
        prev30=statistics.fmean(prev) if prev else None,
        weekday=[statistics.fmean(w) if w else None for w in by_wd],
        days=len(vals), first_day=rows[0]["day"],
    )


def _points_attr(points: list[dict]) -> str:
    """One JSON array, html-escaped for a data-points="..." attribute (BODY-PULIDO-2): never a raw figure
    anywhere this does not already appear (titles already carry it), nothing in it but what the reader sees."""
    return html.escape(json.dumps(points, ensure_ascii=False, separators=(",", ":")), quote=True)


def chart_figure(body: str, points: list[dict] | None, *, link_label: str | None = None,
                  data_key: str | None = None) -> str:
    """Wraps a chart's svg+range markup in <figure class="chart">, optionally enhanced with data-points
    for charts.js's tap/drag readout (DESIGN rule 9). Without points, the figure carries no data-points and
    charts.js leaves it alone -- the existing hit regions are the no-JS path and stay exactly as they are."""
    if not points:
        return f'<figure class="chart">{body}</figure>'
    attrs = f' data-points="{_points_attr(points)}"'
    if link_label:
        attrs += f' data-link-label="{html.escape(link_label, quote=True)}"'
    if data_key:
        attrs += f' data-key="{html.escape(data_key, quote=True)}"'
    return f'<figure class="chart"{attrs}>{body}</figure>'


def month_svg(p: MetricProfile, width: int = 320, height: int = 120, color: str = "var(--body)", *,
              etiqueta_mes: Callable[[str], str] = lambda m: m,
              aria_label: str | None = None,
              valor_fmt: Callable[[float], str] | None = None,
              link_label: str | None = None) -> str:
    """Server-side SVG: monthly means as a line, your all-history normal range as a band.

    etiqueta_mes turns a "YYYY-MM" key into a reader's-language label (axis ends, tap titles); the default keeps
    the ISO key so older callers are unaffected. aria_label, when given, names the metric and the span in words.
    valor_fmt, when given, formats a mean for the per-point readout (BODY-PULIDO-2); link_label is the taplink
    text charts.js shows next to a point's month link (the month key is carried in data-points for that link,
    never a raw value)."""
    pts = [m[1] for m in p.months]
    if len(pts) < 2:
        return ""
    lo = min(min(pts), p.low)
    hi = max(max(pts), p.high)
    pad = (hi - lo) * 0.12 or 1
    lo, hi = lo - pad, hi + pad
    top, h = 6, height - 26

    def y(v: float) -> float:
        return top + h - (v - lo) / (hi - lo) * h

    step = width / (len(pts) - 1)
    line = " ".join(f"{i * step:.1f},{y(v):.1f}" for i, v in enumerate(pts))
    band = (f'<rect x="0" y="{y(p.high):.1f}" width="{width}" height="{max(1, y(p.low) - y(p.high)):.1f}" '
            f'style="fill:var(--inset)"/>')
    last_x, last_y = (len(pts) - 1) * step, y(pts[-1])
    first, lastm = html.escape(etiqueta_mes(p.months[0][0])), html.escape(etiqueta_mes(p.months[-1][0]))
    aria = f' aria-label="{html.escape(aria_label)}"' if aria_label else ""
    # Fixed height, stretched width: lines keep their thickness (non-scaling-stroke); labels are HTML, not SVG text.
    body = (f'<svg viewBox="0 0 {width} {top + h + 6}" width="100%" height="110" role="img"{aria} '
            f'preserveAspectRatio="none" style="display:block;margin-top:8px">{band}'
            f'<polyline fill="none" style="stroke:{color}" stroke-width="2.2" vector-effect="non-scaling-stroke" '
            f'stroke-linejoin="round" points="{line}"/>{_hits([(i * step, f"/trends?m={m[0]}#{p.key}") for i, m in enumerate(p.months)], step, top + h + 6, [etiqueta_mes(m[0]) for m in p.months])}</svg>'
            f'<div class="row k" style="font-size:12px"><span>{first}</span><span>{lastm}</span></div>')
    points = None
    if valor_fmt:
        points = [{"x": i / (len(pts) - 1), "label": f"{etiqueta_mes(m[0])}: {valor_fmt(m[1])}", "m": m[0]}
                  for i, m in enumerate(p.months)]
    return chart_figure(body, points, link_label=link_label, data_key=p.key)


def _hits(points: list[tuple[float, str]], step: float, height: float, titles: list[str]) -> str:
    """Invisible full-height tap targets, one per point, each a link (URLs carry dates only, never values).
    titles are reader's-language labels; escaped here since they land in HTML."""
    return "".join(f'<a href="{href}"><title>{html.escape(title)}</title><rect x="{max(0, x - step / 2):.1f}" y="0" '
                   f'width="{step:.1f}" height="{height:.1f}" style="fill:transparent"/></a>'
                   for (x, href), title in zip(points, titles))


@dataclass
class Window:
    start: date
    end: date
    values: dict[str, float]               # day -> value, only days that have one
    mean: float | None
    prev_mean: float | None                # the window of the same length just before


def window(conn, key: str, start: date, end: date, person_id: int) -> Window:
    vals = db.series(conn, key, start.isoformat(), end.isoformat(), person_id)
    n = (end - start).days + 1
    prev = db.series(conn, key, (start - timedelta(n)).isoformat(), (start - timedelta(1)).isoformat(), person_id)
    return Window(start, end, vals, statistics.fmean(vals.values()) if vals else None,
                  statistics.fmean(prev.values()) if prev else None)


def window_svg(w: Window, low: float, high: float, nights: set[str] = frozenset(), width: int = 320,
               height: int = 120, color: str = "var(--body)", *,
               etiqueta_dia: Callable[[date], str] = lambda d: d.isoformat(),
               aria_label: str | None = None,
               valor_fmt: Callable[[float], str] | None = None,
               link_label: str | None = None) -> str:
    """Daily line over the window with your normal range as a band; night shifts shaded; a missing day breaks the
    line (honest gaps, never a zero). Every day is a tap target that opens that day.

    etiqueta_dia turns a day into a reader's-language label (axis ends, tap titles); the default keeps the ISO
    date so older callers are unaffected. aria_label, when given, names the metric and the span in words.
    valor_fmt, when given, formats a day's value for the per-point readout (BODY-PULIDO-2); link_label is the
    taplink text charts.js shows next to a point's day link (the URL still carries only the day, never a value)."""
    days = [w.start + timedelta(i) for i in range((w.end - w.start).days + 1)]
    pts = list(w.values.values())
    if len(pts) < 2:
        return ""
    lo, hi = min(min(pts), low), max(max(pts), high)
    pad = (hi - lo) * 0.12 or 1
    lo, hi = lo - pad, hi + pad
    top, h = 6, height - 26
    full = top + h + 6

    def y(v: float) -> float:
        return top + h - (v - lo) / (hi - lo) * h

    step = width / max(1, len(days) - 1)
    out = [f'<rect x="0" y="{y(high):.1f}" width="{width}" height="{max(1, y(low) - y(high)):.1f}" '
           f'style="fill:var(--inset)"/>']
    for i, d in enumerate(days):
        if d.isoformat() in nights:
            out.append(f'<rect x="{max(0, i * step - step / 2):.1f}" y="0" width="{step:.1f}" height="{full:.1f}" '
                       f'style="fill:var(--night);opacity:.18"/>')
    runs, cur = [], []
    for i, d in enumerate(days):
        v = w.values.get(d.isoformat())
        if v is None:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append(f"{i * step:.1f},{y(v):.1f}")
    if cur:
        runs.append(cur)
    for r in runs:
        if len(r) == 1:
            x, yy = r[0].split(",")
            out.append(f'<line x1="{x}" y1="{yy}" x2="{x}" y2="{yy}" style="stroke:{color}" stroke-width="5" '
                       f'stroke-linecap="round" vector-effect="non-scaling-stroke"/>')
        else:
            out.append(f'<polyline fill="none" style="stroke:{color}" stroke-width="2.2" '
                       f'vector-effect="non-scaling-stroke" stroke-linejoin="round" points="{" ".join(r)}"/>')
    out.append(_hits([(i * step, f"/day/{d.isoformat()}") for i, d in enumerate(days)], step, full,
                     [etiqueta_dia(d) for d in days]))
    aria = f' aria-label="{html.escape(aria_label)}"' if aria_label else ""
    first, lastd = html.escape(etiqueta_dia(days[0])), html.escape(etiqueta_dia(days[-1]))
    body = (f'<svg viewBox="0 0 {width} {full}" width="100%" height="110" role="img"{aria} '
            f'preserveAspectRatio="none" style="display:block;margin-top:8px">{"".join(out)}</svg>'
            f'<div class="row k" style="font-size:12px"><span>{first}</span>'
            f'<span>{lastd}</span></div>')
    points = None
    if valor_fmt:
        points = [{"x": i / (len(days) - 1), "label": f"{etiqueta_dia(d)}: {valor_fmt(w.values[d.isoformat()])}",
                   "day": d.isoformat()}
                  for i, d in enumerate(days) if d.isoformat() in w.values]
    return chart_figure(body, points, link_label=link_label)
