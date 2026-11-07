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


def baseline(conn, metric: str, day: str, window: int = WINDOW_DAYS, *, person_id: int) -> Baseline | None:
    d = date.fromisoformat(day)
    start, end = (d - timedelta(window)).isoformat(), (d - timedelta(1)).isoformat()
    vals = list(db.series(conn, metric, start, end, person_id).values())
    if len(vals) < MIN_DAYS:
        return None
    q = statistics.quantiles(vals, n=4)
    return Baseline(metric, statistics.fmean(vals), statistics.pstdev(vals), q[0], q[2], len(vals))
