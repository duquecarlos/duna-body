"""Body's own readiness score (estimated). Version 1, 2026-11-06.

Carlos chose an own score over Garmin's Body Battery as the hero on Hoy. Rules it follows:
* It is an ESTIMATE: shown with «≈», rounded, with a breakdown ("how I calculate it").
* Against YOUR baseline (30 days before the day), never population tables.
* Transparent: each part has a sub-score, a weight and the numbers behind it.
* Honest gaps: a missing part is left out and the rest re-weighted; with too little data there is no score.
* Not medical advice.

Parts (weight): last-night HRV vs normal (30%), sleep vs what you needed (25%), Body Battery at wake (20%),
resting HR vs normal (15%), yesterday's stress vs normal (10%).
Shift context (nights in a row, sleep debt across a block) is planned for v2, once shifts are imported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db
from .baselines import baseline

VERSION = "body-v1"
METRIC = "readiness"

WEIGHTS = {"hrv": 0.30, "sleep": 0.25, "battery": 0.20, "resting_hr": 0.15, "stress": 0.10}
MIN_PARTS = 3

# Every metric a `compute` call can touch, across its own vals and every baseline it looks up --
# the set a caller builds a Lector with before looping `compute` over many days (BODY-RENDIMIENTO-2).
METRICS = ("hrv_last_night", "sleep_minutes", "sleep_need_min", "body_battery_wake", "resting_hr", "stress_avg")


@dataclass
class Part:
    key: str
    value: float | None
    reference: float | None  # baseline mean or sleep need
    subscore: float
    weight: float


@dataclass
class Readiness:
    day: str
    score: int | None
    parts: list[Part] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    version: str = VERSION

    @property
    def band(self) -> str | None:
        if self.score is None:
            return None
        return "high" if self.score >= 75 else "medium" if self.score >= 55 else "low"


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def _vs_baseline(conn, metric: str, day: str, value, higher_is_better: bool, person_id: int, lector=None):
    """70 = your normal; +/-20 per standard deviation."""
    if value is None:
        return None
    b = baseline(conn, metric, day, person_id=person_id, lector=lector)
    if b is None:
        return None
    z = b.z(value)
    if z is None:
        return None
    return _clamp(70 + 20 * (z if higher_is_better else -z)), b.mean


def compute(conn, day: str, person_id: int, *, lector=None) -> Readiness:
    """`lector`, when given, is a `body.lector.Lector` built over a window that covers `day` and the
    30 days before it -- every value and baseline below then comes from memory instead of a fresh
    query (BODY-RENDIMIENTO-2). None (the default) keeps the original per-call queries, unchanged."""
    if lector is not None:
        vals = lector.day_values(day)
    else:
        vals = {r["metric_key"]: r["value"] for r in conn.execute(
            "SELECT metric_key, value FROM daily_metric WHERE person_id=? AND day=?", (person_id, day))}
    yesterday = (date.fromisoformat(day) - timedelta(1)).isoformat()
    y_series = lector.series("stress_avg", yesterday, yesterday) if lector is not None \
        else db.series(conn, "stress_avg", yesterday, yesterday, person_id)
    y_stress = y_series.get(yesterday)

    raw: dict[str, tuple[float, float | None, float | None] | None] = {}

    r = _vs_baseline(conn, "hrv_last_night", day, vals.get("hrv_last_night"), True, person_id, lector)
    raw["hrv"] = (r[0], vals.get("hrv_last_night"), r[1]) if r else None

    slept, need = vals.get("sleep_minutes"), vals.get("sleep_need_min")
    if slept is not None:
        if not need:
            b = baseline(conn, "sleep_minutes", day, person_id=person_id, lector=lector)
            need = max(b.mean, 420) if b else 480
        ratio = slept / need
        raw["sleep"] = (_clamp(100 - (1 - ratio) * 160), slept, need)  # 90% of need -> 84, 70% -> 52
    else:
        raw["sleep"] = None

    bb = vals.get("body_battery_wake")
    bb_base = baseline(conn, "body_battery_wake", day, person_id=person_id, lector=lector)
    raw["battery"] = (_clamp(bb), bb, bb_base.mean if bb_base else None) if bb is not None else None

    r = _vs_baseline(conn, "resting_hr", day, vals.get("resting_hr"), False, person_id, lector)
    raw["resting_hr"] = (r[0], vals.get("resting_hr"), r[1]) if r else None

    r = _vs_baseline(conn, "stress_avg", yesterday, y_stress, False, person_id, lector)
    raw["stress"] = (r[0], y_stress, r[1]) if r else None

    parts = [Part(k, v[1], v[2], round(v[0], 1), WEIGHTS[k]) for k, v in raw.items() if v]
    missing = [k for k, v in raw.items() if not v]
    have_core = any(p.key in ("hrv", "sleep") for p in parts)
    if len(parts) < MIN_PARTS or not have_core:
        return Readiness(day, None, parts, missing)
    total_w = sum(p.weight for p in parts)
    score = sum(p.subscore * p.weight for p in parts) / total_w
    return Readiness(day, round(score), parts, missing)


def update(conn, day: str, person_id: int) -> Readiness:
    res = compute(conn, day, person_id)
    with conn:
        db.set_metric(conn, day, METRIC, res.score, VERSION, person_id)
    return res


def recompute_all(conn, person_id: int) -> int:
    days = sorted(db.days_with_data(conn, person_id))
    for d in days:
        update(conn, d, person_id)
    return len(days)
