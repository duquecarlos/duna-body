"""BODY-HISTORIA-VIDA-LUGARES-2-R ruling 0: the ONE helper every trip-aware screen and tool goes
through. A TRIP counts here only once its own span (ends_at - starts_at + 1) is 2 or more days --
a single day Lugares reports is never "a trip" anywhere in this app. One statement
(db.viajes_lugares), scoped to this person like every read in this app.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import db


def dias_de_viaje(conn: sqlite3.Connection, person_id: int, desde: str, hasta: str) -> set[str]:
    """Every day (ISO string) inside [desde, hasta] that falls inside one of this person's own
    trips of 2+ days -- a single statement, the days of a 1-day trip never included."""
    out: set[str] = set()
    d_desde, d_hasta = date.fromisoformat(desde), date.fromisoformat(hasta)
    for fila in db.viajes_lugares(conn, person_id, desde, hasta):
        s, e = date.fromisoformat(fila["starts_at"]), date.fromisoformat(fila["ends_at"])
        if (e - s).days + 1 < 2:
            continue
        d = max(s, d_desde)
        fin = min(e, d_hasta)
        while d <= fin:
            out.add(d.isoformat())
            d += timedelta(days=1)
    return out
