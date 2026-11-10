"""Your own normal: the 30 days before a given day (that day excluded).

Every comparison in Body is against this, never against population tables.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from . import db

WINDOW_DAYS = 30
MIN_DAYS = 7  # fewer than this and there is no baseline yet (honest "not enough data")


@dataclass
class Baseline:
    metric: str
    mean: float
    sd: float
    low: float   # normal range: 25th percentile
    high: float  # normal range: 75th percentile
    n: int

    def z(self, value: float) -> float | None:
        if self.sd <= 0:
            return None
        return (value - self.mean) / self.sd


def baseline(conn, metric: str, day: str, window: int = WINDOW_DAYS, *, person_id: int, lector=None) -> Baseline | None:
    if lector is not None:
        d = date.fromisoformat(day)
        start, end = (d - timedelta(window)).isoformat(), (d - timedelta(1)).isoformat()
        serie = lector.series(metric, start, end)
        # BODY-PERIODO-1 ruling 3: a marked day (illness, travel, injury) is left OUT of the usual
        # range, never re-weighted -- the one place every caller of baseline() gets this for free.
        marcados = db.dias_marcados(conn, person_id, start, end)
        vals = [v for dia, v in serie.items() if dia not in marcados]
        if len(vals) < MIN_DAYS:
            return None
        q = statistics.quantiles(vals, n=4)
        return Baseline(metric, statistics.fmean(vals), statistics.pstdev(vals), q[0], q[2], len(vals))
    return baselines(conn, [metric], day, window, person_id=person_id)[metric]


def baselines(conn, keys: list[str], day: str, window: int = WINDOW_DAYS, *,
              person_id: int) -> dict[str, Baseline | None]:
    """Batched baseline: one SELECT across every key's window, instead of one per key
    (BODY-AGUJETAS-1-B) -- a page whose row count tracks the metric catalogue (BODY-MEDIDA-PESO-1
    already hit this once) must not cost a query per catalogue row. Same per-key result as calling
    baseline() for each key alone, including the marked-days exclusion (BODY-PERIODO-1 ruling 3) --
    read once here, same as dias_marcados' own per-connection cache already gave every per-key call."""
    if not keys:
        return {}
    d = date.fromisoformat(day)
    start, end = (d - timedelta(window)).isoformat(), (d - timedelta(1)).isoformat()
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT metric_key, day, value FROM daily_metric WHERE person_id=? AND metric_key IN ({placeholders}) "
        "AND day BETWEEN ? AND ?",
        (person_id, *keys, start, end)).fetchall()
    marcados = db.dias_marcados(conn, person_id, start, end)
    by_key: dict[str, list[float]] = {k: [] for k in keys}
    for r in rows:
        if r["day"] not in marcados:
            by_key[r["metric_key"]].append(r["value"])
    out: dict[str, Baseline | None] = {}
    for k in keys:
        vals = by_key[k]
        if len(vals) < MIN_DAYS:
            out[k] = None
        else:
            q = statistics.quantiles(vals, n=4)
            out[k] = Baseline(k, statistics.fmean(vals), statistics.pstdev(vals), q[0], q[2], len(vals))
    return out
