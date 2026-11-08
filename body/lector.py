"""One in-memory read of a person's daily metrics over a window (BODY-RENDIMIENTO-OBS-1): a screen
that needs a metric for many days reads the window once, never once per day. `Lector` answers
`series` and `day_values` with EXACTLY `body/db.py`'s own semantics (same keys, same inclusive
bounds, same missing-day behaviour, same value types) from memory, loaded by one SELECT per
metric at construction time -- its own read-only query against `daily_metric`, the same columns
`db.series` reads, so this module never imports `body/db.py` (Tier 1).
"""
from __future__ import annotations


class Lector:
    def __init__(self, conn, person_id: int, desde: str, hasta: str, metrics) -> None:
        self._series: dict[str, dict[str, float]] = {}
        for metric in metrics:
            rows = conn.execute(
                "SELECT day, value FROM daily_metric WHERE person_id=? AND metric_key=? AND day BETWEEN ? AND ?",
                (person_id, metric, desde, hasta),
            ).fetchall()
            self._series[metric] = {r["day"]: r["value"] for r in rows}

    def series(self, metric: str, start: str, end: str) -> dict[str, float]:
        """Same as `db.series`: values of `metric` for start <= day <= end, from the loaded window.
        ISO day strings compare the same way lexically as the SQL BETWEEN they replace."""
        return {day: value for day, value in self._series.get(metric, {}).items() if start <= day <= end}

    def day_values(self, day: str) -> dict[str, float]:
        """Same as `db.day_values`, restricted to the metrics this reader was built with -- every
        caller here already reads `day_values` through a fixed, known metric list."""
        return {metric: serie[day] for metric, serie in self._series.items() if day in serie}
