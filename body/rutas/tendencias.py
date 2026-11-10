"""Tendencias (BODY-WEB-ESTRUCTURA-7): the `/trends` screen -- range cards, the week table, the
month view and "Ver todas las medidas". Pure move: path, method, templates and bodies identical.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException

from .. import baselines, db, identity, profile
from ..catalogo_tendencias import TODAS
from ..i18n import T
from . import comun

router = APIRouter()

WEEK_KEYS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr"]
TREND_MIN_DAYS = 14  # body/profile.py's metric_profile() needs this many rows before a metric gets a card
SPARSE_KEYS = {"vo2max", "vo2max_cycling", "fitness_age"}  # Garmin updates these only after qualifying activities
RANGES = ["7", "30", "90", "all"]
GARMIN_MIN_MONTH_POINTS = 7  # body/profile.py's metric_profile() default -- a near-daily watch cadence
SELF_MIN_MONTH_POINTS = 2  # a weekly self-measured key's own floor (BODY-MEDIDA-PESO-1-M2), per origin
# BODY-SUENO-RESPIRACION-VISTA-1: comun.TREND_GROUPS' "night" group -- same honest caveat as
# Día's (BODY-ENTRENO-1-INV Q6), carried as the card's own note like trends.self_note.
NIGHT_CAVEAT_KEYS = {"sleep_spo2_avg", "sleep_respiration_avg"}


def _trend_days_have(conn, person_id: int) -> int:
    """How many days the best-covered trend metric already has, for the honest-gap message below
    TREND_MIN_DAYS -- a count, never a figure (BODY-SIN-FRICCION-1)."""
    placeholders = ",".join("?" * len(comun.TREND_KEYS))
    rows = conn.execute(
        f"SELECT COUNT(*) AS n FROM daily_metric WHERE person_id = ? AND metric_key IN ({placeholders}) "
        "GROUP BY metric_key",
        (person_id, *comun.TREND_KEYS),
    ).fetchall()
    return max((r["n"] for r in rows), default=0)


def _compare(t: T, k: str, d: dict, value, ref, spread: float, vs_key: str) -> tuple[str, str]:
    if value is None or ref is None:
        return "", "flat"
    diff = value - ref
    if abs(diff) < (spread or 1) * 0.15:
        return t("delta.same"), "flat"
    is_min = comun._is_minutes(k)
    dec = comun._decimales(k)
    # BODY-MEDIDA-PESO-1-M2: an arrow is only honest when the amount next to it is not "0" -- a
    # diff that rounds to zero AT THE METRIC'S OWN PRECISION (whole minutes for a minutes key,
    # `dec` decimals otherwise) gets the same "no real change" wording as the spread check above,
    # never an arrow pointing at nothing.
    if (round(abs(diff)) if is_min else round(abs(diff), dec)) == 0:
        return t("delta.same"), "flat"
    amount = t.minutes(abs(diff)) if is_min else t.num(abs(diff), dec)
    cls = "flat" if d["better"] == "neutral" else ("good" if (diff > 0) == (d["better"] == "higher") else "bad")
    return f"{'↑' if diff > 0 else '↓'} {amount} {t(vs_key)}", cls


def _nights(conn, start: date, end: date, person_id: int) -> set[str]:
    return {r["day"] for r in conn.execute("SELECT day FROM shift WHERE person_id = ? AND kind = 'night' AND day "
                                           "BETWEEN ? AND ?", (person_id, start.isoformat(), end.isoformat()))}


def _chart_aria(t: T, metric: str, start_label: str, end_label: str, months: bool = False) -> str:
    """Accessible name for a trend chart: the metric and its span in words, no figure in it (BODY-EJES-FECHAS-1).
    The connector words come from trends.aria_days / trends.aria_months in i18n, not literals here. A month span
    reads mid-sentence in Spanish, so its month names go lowercase here -- t.month_year itself still capitalizes,
    for the axis ends where they stand alone (BODY-EJES-FECHAS-1-M)."""
    if months and t.lang == "es":
        start_label, end_label = start_label.lower(), end_label.lower()
    key = "trends.aria_months" if months else "trends.aria_days"
    return t(key, metric=metric, desde=start_label, hasta=end_label)


def _medidas_count_txt(t: T, n: int) -> str:
    return t("trends.medidas_count_one") if n == 1 else t("trends.medidas_count_other", n=n)


def _trend_card(t: T, defs: dict, k: str, start, end, month, nights, profiles: dict, windows_by_key: dict):
    """One card -- own baseline, honest gaps, night shading, the month tap -- built the same way for the
    default 9 and every extra metric behind "Ver todas las medidas" (DESIGN rules 3, 6, 7). A
    self-measured key (BODY-ENTRENO-MEDIDA-1) gets a card from its first value instead of waiting
    for TREND_MIN_DAYS worth of watch-sized history -- there is no cadence to wait on. Below
    baselines.MIN_DAYS of self-measured values, p.low/p.high are not yet an honest normal range (as
    few as one point collapses them to a single repeated value) -- the card carries no delta, no
    normal-range legend and no band, and a count line instead (BODY-ENTRENO-TOOLS-1-M fix 3).

    profiles and windows_by_key are read once for every key the whole route needs (BODY-AGUJETAS-1-B)
    -- this card never queries the database itself, so its own cost does not grow with the catalogue."""
    d = defs[k]
    p = profiles.get(k)
    if not p:
        return None, None
    pocas = d["origen"] == "self" and p.days < baselines.MIN_DAYS
    spread = p.high - p.low
    metric_label = t("metric." + k)
    valor_fmt = lambda v: comun._fig_unit(t, k, v, defs)
    if start:
        w = windows_by_key[k]
        if pocas:
            delta, cls = "", "flat"
        elif month:
            delta, cls = _compare(t, k, d, w.mean, p.all_mean, spread, "trends.vs_hist")
        else:
            delta, cls = _compare(t, k, d, w.mean, w.prev_mean, spread, "trends.vs_prev")
        etiqueta_dia = lambda day: comun._short_date(t, day)
        aria = _chart_aria(t, metric_label, etiqueta_dia(w.start), etiqueta_dia(w.end))
        fig, svg = w.mean, profile.window_svg(w, p.low, p.high, nights, etiqueta_dia=etiqueta_dia, aria_label=aria,
                                              valor_fmt=valor_fmt, link_label=t("trends.chart_open_day"),
                                              banda=not pocas)
    else:
        delta, cls = ("", "flat") if pocas else _compare(t, k, d, p.last30, p.all_mean, spread, "trends.vs_hist")
        etiqueta_mes = lambda m: t.month_year(date.fromisoformat(m + "-01"))
        aria = (_chart_aria(t, metric_label, etiqueta_mes(p.months[0][0]), etiqueta_mes(p.months[-1][0]), months=True)
                if len(p.months) >= 2 else None)
        fig, svg = p.last30, profile.month_svg(p, etiqueta_mes=etiqueta_mes, aria_label=aria,
                                               valor_fmt=valor_fmt, link_label=t("trends.chart_open_month"))
    card = {"key": k, "question": t("trends.q." + k), "est": d["kind"] == "estimated",
            "last30": comun._value(t, k, fig), "unit": comun._unit(t, d) if fig is not None else "", "delta": delta,
            "cls": cls, "svg": svg, "low": comun._value(t, k, p.low), "high": comun._value(t, k, p.high),
            "sparse": k in SPARSE_KEYS, "self": d["origen"] == "self", "subtitle": None,
            "pocas": pocas, "conteo": _medidas_count_txt(t, p.days) if pocas else None,
            "caveat": k in NIGHT_CAVEAT_KEYS}
    return card, p


@router.get("/trends")
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
    todas_keys = list(TODAS)
    # BODY-MEDIDA-PESO-1-M2 / BODY-ENTRENO-MEDIDA-1: a self-measured key's own lower thresholds,
    # same reasoning _trend_card used per key before BODY-AGUJETAS-1-B batched this read.
    min_days_por_clave = {k: (1 if defs[k]["origen"] == "self" else TREND_MIN_DAYS) for k in todas_keys}
    min_month_points_por_clave = {k: (SELF_MIN_MONTH_POINTS if defs[k]["origen"] == "self"
                                       else GARMIN_MIN_MONTH_POINTS) for k in todas_keys}
    profiles = profile.metric_profiles(conn, todas_keys, person_id=person_id,
                                       min_days_por_clave=min_days_por_clave,
                                       min_month_points_por_clave=min_month_points_por_clave)
    last = comun._latest_day(conn, person_id)
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
    windows_by_key = profile.windows(conn, todas_keys, start, end, person_id=person_id) if start else {}
    cards, since, days = [], None, 0
    for k in comun.TREND_KEYS:
        card, p = _trend_card(t, defs, k, start, end, month, nights, profiles, windows_by_key)
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
                       text=comun._sync_status_text(conn, person, t),
                       back_href=back_href, back_label=back_label, turnos=turnos_on)
    extra_groups, extra_count = [], 0
    for group_key, keys in comun.TREND_GROUPS:
        group_cards = []
        for k in keys:
            card, p = _trend_card(t, defs, k, start, end, month, nights, profiles, windows_by_key)
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
    # WEEK_KEYS is a subset of TODAS, read with the same (default) thresholds above -- reusing the
    # prefetched profiles here, instead of a second query per key, is what BODY-AGUJETAS-1-B batched.
    profs = [profiles.get(k) for k in WEEK_KEYS]
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
