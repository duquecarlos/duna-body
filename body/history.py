"""Your whole Garmin history read as patterns: what covers it, how training changed, sleep and recovery
patterns by weekday and season, and the same patterns by shift kind once shifts exist.

Pure reads over a connection and a person id, no web imports. Every function returns a plain dict (or
None when there is not enough history yet); formatting (units, "kind: measured|estimated", translated
labels) is done by the caller, the same way body/web.py already formats profile.py and baselines.py
output. Honest gaps: a comparison with nothing on one side is None, never 0.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import db

PATTERN_KEYS = ["sleep_minutes", "resting_hr", "hrv_last_night", "stress_avg", "body_battery_wake"]

# Reused from body/web.py's trends week-highlight rule (`trends()`'s week table: a weekday is only marked
# the "worst" one when `spread >= 1 if not _is_minutes(k) else 5`). Below this, a weekday/month spread is
# noise, not a pattern worth a finding.
NOISE_FLOOR_MINUTES = 5.0
NOISE_FLOOR_DEFAULT = 1.0

PATTERN_MIN_N = 4        # fewer values than this in a weekday/month bucket: no band, no finding from it
MAX_FINDINGS = 3
TRAINING_TOP_TYPES = 5   # more distinct activity types than this: the rest become "other"

# Garmin activity `type` values this app recognises and has an i18n label for (body/i18n/*.json,
# "activity.type.<key>"). Anything else falls back to its own key, prettified, never a crash.
KNOWN_ACTIVITY_TYPES = {
    "running", "trail_running", "treadmill_running", "track_running",
    "cycling", "indoor_cycling", "mountain_biking", "road_biking",
    "swimming", "lap_swimming", "open_water_swimming",
    "walking", "hiking", "strength_training", "cardio", "yoga", "elliptical", "other",
}


def _is_minutes(key: str) -> bool:
    return key.endswith("_min") or key == "sleep_minutes"


def _noise_floor(key: str) -> float:
    return NOISE_FLOOR_MINUTES if _is_minutes(key) else NOISE_FLOOR_DEFAULT


def _prettify(raw: str | None) -> str:
    if not raw:
        return "—"
    return raw.replace("_", " ").replace("-", " ").strip().capitalize()


def _kind_info(key: str | None) -> dict | None:
    if not key:
        return None
    return {"key": key, "known": key in KNOWN_ACTIVITY_TYPES, "fallback_label": _prettify(key)}


def coverage(conn, person_id: int) -> dict | None:
    """First/last day with watch data, how many days are missing in between, the longest gap, activities."""
    days = sorted(db.days_with_data(conn, person_id))
    if not days:
        return None
    present = len(days)
    span = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days + 1
    longest_gap, gap_start, gap_end = 0, None, None
    prev = date.fromisoformat(days[0])
    for day_str in days[1:]:
        d = date.fromisoformat(day_str)
        gap = (d - prev).days - 1
        if gap > longest_gap:
            longest_gap = gap
            gap_start, gap_end = prev + timedelta(1), d - timedelta(1)
        prev = d
    activity_count = conn.execute(
        "SELECT COUNT(*) AS n FROM activity WHERE person_id = ?", (person_id,)
    ).fetchone()["n"]
    return {
        "first_day": days[0],
        "last_day": days[-1],
        "days_present": present,
        "days_total": span,
        "days_missing": span - present,
        "longest_gap_days": longest_gap,
        "longest_gap_start": gap_start.isoformat() if gap_start else None,
        "longest_gap_end": gap_end.isoformat() if gap_end else None,
        "activity_count": activity_count,
    }


def _shift_year(d: date, delta: int) -> date:
    try:
        return d.replace(year=d.year + delta)
    except ValueError:  # Feb 29 with no Feb 29 a year away
        return d.replace(month=2, day=28, year=d.year + delta)


def _year_over_year(rows, last_day: date) -> dict | None:
    start = last_day - timedelta(89)
    year_start, year_end = _shift_year(start, -1), _shift_year(last_day, -1)
    cur = [r for r in rows if start <= date.fromisoformat(r["day"]) <= last_day]
    prior = [r for r in rows if year_start <= date.fromisoformat(r["day"]) <= year_end]
    if not prior:
        return None  # an honest gap: no data a year ago, never a 0
    return {
        "sessions_now": len(cur), "sessions_year_ago": len(prior),
        "minutes_now": sum(r["duration_s"] or 0 for r in cur) / 60,
        "minutes_year_ago": sum(r["duration_s"] or 0 for r in prior) / 60,
    }


def training(conn, person_id: int) -> dict | None:
    """Activities grouped by type, by month (with a by-type minute breakdown for a stacked bar), your
    usual week, and a year-over-year compare of the last 90 days (None when there is no data from a year
    ago, never a 0)."""
    rows = conn.execute(
        "SELECT day, type, duration_s, distance_m FROM activity WHERE person_id = ? ORDER BY day",
        (person_id,),
    ).fetchall()
    if not rows:
        return None

    by_type: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "duration_s": 0.0, "distance_rows": 0,
                                                     "distance_m": 0.0})
    by_month: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "duration_s": 0.0, "distance_rows": 0,
                                                      "distance_m": 0.0})
    weeks_seen: dict[tuple[int, int], int] = defaultdict(int)
    for r in rows:
        key = r["type"] or "other"
        d = date.fromisoformat(r["day"])
        for bucket in (by_type[key], by_month[r["day"][:7]]):
            bucket["sessions"] += 1
            bucket["duration_s"] += r["duration_s"] or 0
            if r["distance_m"] is not None:
                bucket["distance_rows"] += 1
                bucket["distance_m"] += r["distance_m"]
        iso_year, iso_week, _ = d.isocalendar()
        weeks_seen[(iso_year, iso_week)] += 1

    ranked = sorted(by_type.items(), key=lambda kv: kv[1]["duration_s"], reverse=True)
    top, rest = ranked[:TRAINING_TOP_TYPES], ranked[TRAINING_TOP_TYPES:]
    top_keys = {k for k, _ in top}

    def _type_row(key: str, v: dict) -> dict:
        return {**_kind_info(key), "sessions": v["sessions"], "minutes": v["duration_s"] / 60,
                "km": v["distance_m"] / 1000 if v["distance_rows"] and v["distance_m"] > 0 else None}

    types = [_type_row(k, v) for k, v in top]
    if rest:
        merged = {"sessions": sum(v["sessions"] for _, v in rest),
                  "duration_s": sum(v["duration_s"] for _, v in rest),
                  "distance_rows": sum(v["distance_rows"] for _, v in rest),
                  "distance_m": sum(v["distance_m"] for _, v in rest)}
        types.append(_type_row("other", merged))

    by_month_type: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        key = r["type"] or "other"
        bucket_key = key if key in top_keys else "other"
        by_month_type[r["day"][:7]][bucket_key] += (r["duration_s"] or 0) / 60

    months = [{"month": m, "sessions": v["sessions"], "minutes": v["duration_s"] / 60,
               "km": v["distance_m"] / 1000 if v["distance_rows"] and v["distance_m"] > 0 else None,
               "by_type": [{**_kind_info(k), "minutes": mins}
                           for k, mins in sorted(by_month_type[m].items(), key=lambda kv: -kv[1])]}
              for m, v in sorted(by_month.items())]

    longest_key = ranked[0][0] if ranked else None
    frequent_key = max(by_type.items(), key=lambda kv: kv[1]["sessions"])[0] if by_type else None

    return {
        "types": types,
        "months": months,
        "max_month_minutes": max((m["minutes"] for m in months), default=0.0),
        "usual_week_sessions": statistics.median(weeks_seen.values()) if weeks_seen else None,
        "longest_kind": _kind_info(longest_key),
        "most_frequent_kind": _kind_info(frequent_key),
        "yoy": _year_over_year(rows, date.fromisoformat(rows[-1]["day"])),
    }


def _bucket(values: list[float], use_median: bool) -> dict | None:
    if len(values) < PATTERN_MIN_N:
        return None
    q = statistics.quantiles(values, n=4)
    center = statistics.median(values) if use_median else statistics.fmean(values)
    return {"center": center, "low": q[0], "high": q[2], "n": len(values)}


def _candidate(metric: str, axis: str, buckets: list[dict | None], better: str, sd: float) -> dict | None:
    present = [(i, b) for i, b in enumerate(buckets) if b is not None]
    if len(present) < 2:
        return None
    values = [b["center"] for _, b in present]
    spread = max(values) - min(values)
    if spread < _noise_floor(metric):
        return None
    # Same rule as web.py's trends week highlight: the "worse" side is the max when lower is better,
    # else the min.
    pick = max if better == "lower" else min
    idx, bucket = pick(present, key=lambda ib: ib[1]["center"])
    return {"metric": metric, "axis": axis, "index": idx, "value": bucket["center"], "n": bucket["n"],
            "impact": spread / sd if sd else spread}


def patterns(conn, person_id: int, marcados: set[str] | None = None) -> dict:
    """Weekday and month/season profiles (your own p25-p75 band) for sleep, resting HR, HRV, stress and
    Body Battery, plus up to three plain-language findings picked by spread / your own spread, filtered by
    a noise floor so a flat week or month never produces a finding.

    BODY-PERIODO-PANTALLAS-1: a marked day (illness, travel, injury) is left OUT of every bucket, the
    same rule baselines.baseline() already applies -- a sick week should never read as your normal
    Monday. `marcados`, when given (historial.py passes its own, read once for the whole page), is
    used as is -- db.dias_marcados() itself no longer re-reads person() for "today" on every call
    (BODY-PERIODO-PANTALLAS-1-B folded the zone into _periodos_de_persona's own cached period read,
    one JOIN), but a caller with its own page-wide budget (one route, several PATTERN_KEYS) still
    reads it once and passes it in rather than re-running that query per key, the same way
    tendencias.py reads `nights` once for every card."""
    better_by_key = {r["key"]: r["better"] for r in conn.execute("SELECT key, better FROM metric_def")}
    metrics: dict[str, dict | None] = {}
    candidates: list[dict] = []
    for key in PATTERN_KEYS:
        rows = conn.execute(
            "SELECT day, value FROM daily_metric WHERE person_id = ? AND metric_key = ?", (person_id, key)
        ).fetchall()
        if rows:
            propios = (marcados if marcados is not None else
                       db.dias_marcados(conn, person_id, min(r["day"] for r in rows), max(r["day"] for r in rows)))
            rows = [r for r in rows if r["day"] not in propios]
        if len(rows) < PATTERN_MIN_N:
            metrics[key] = None
            continue
        by_wd: list[list[float]] = [[] for _ in range(7)]
        by_month: list[list[float]] = [[] for _ in range(12)]
        all_vals = []
        for r in rows:
            d = date.fromisoformat(r["day"])
            by_wd[d.weekday()].append(r["value"])
            by_month[d.month - 1].append(r["value"])
            all_vals.append(r["value"])
        sd = statistics.pstdev(all_vals) if len(all_vals) >= 2 else 0.0
        weekday = [_bucket(v, use_median=False) for v in by_wd]
        month = [_bucket(v, use_median=True) for v in by_month]
        metrics[key] = {"weekday": weekday, "month": month, "n": len(rows)}
        better = better_by_key.get(key, "neutral")
        if better != "neutral":
            for axis, buckets in (("weekday", weekday), ("month", month)):
                c = _candidate(key, axis, buckets, better, sd)
                if c:
                    candidates.append(c)
    candidates.sort(key=lambda c: (-c["impact"], PATTERN_KEYS.index(c["metric"]), c["axis"]))
    return {"metrics": metrics, "findings": candidates[:MAX_FINDINGS]}


def by_shift(conn, person_id: int, marcados: set[str] | None = None) -> dict:
    """The same metrics as patterns(), grouped by shift kind, for the days that have a shift row. An
    explicit no-shifts-yet state when there are none, for an honest empty card pointing to Turnos.

    BODY-PERIODO-PANTALLAS-1: a marked day (illness, travel, injury) is left out of every kind's own
    figures AND its own day count, the same rule patterns() and baselines.baseline() already apply --
    a shift kind's "usual" should never be pulled toward a day the person marked as off the normal
    flow. `marcados`, when given, is used as is -- see patterns()'s own docstring for why."""
    shift_rows = conn.execute(
        "SELECT DISTINCT day, kind FROM shift WHERE person_id = ?", (person_id,)
    ).fetchall()
    if not shift_rows:
        return {"has_shifts": False, "kinds": []}
    day_kind: dict[str, str] = {}
    for r in shift_rows:
        day_kind.setdefault(r["day"], r["kind"])  # one kind per day; first one wins if a day has several
    marcados = marcados if marcados is not None else db.dias_marcados(conn, person_id, min(day_kind), max(day_kind))

    placeholders = ",".join("?" for _ in PATTERN_KEYS)
    metric_rows = conn.execute(
        f"SELECT day, metric_key, value FROM daily_metric WHERE person_id = ? AND metric_key IN ({placeholders})",
        (person_id, *PATTERN_KEYS),
    ).fetchall()
    values_by_kind: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    days_by_kind: dict[str, set[str]] = defaultdict(set)
    for r in metric_rows:
        if r["day"] in marcados:
            continue
        kind = day_kind.get(r["day"])
        if kind:
            values_by_kind[kind][r["metric_key"]].append(r["value"])
            days_by_kind[kind].add(r["day"])
    for day, kind in day_kind.items():
        if day not in marcados:
            days_by_kind[kind].add(day)  # count every shift day, even one with no metric at all (honest gap)

    kinds = [
        {"kind": kind, "days": len(days_by_kind[kind]),
         "metrics": {k: (statistics.fmean(values_by_kind[kind][k]) if values_by_kind[kind].get(k) else None)
                     for k in PATTERN_KEYS}}
        for kind in sorted(days_by_kind, key=lambda k: -len(days_by_kind[k]))
    ]
    return {"has_shifts": True, "kinds": kinds}
