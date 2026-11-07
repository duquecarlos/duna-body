"""Per-person assistant setting: how much health data the assistant's tools may read.

completo (default, Carlos's own choice, BODY-ASSISTANT-DATA-LEVEL-1) = day-by-day figures a question
needs. resumen = only aggregates (means over a window, counts, verdicts in words), never one day's
value. ninguno = no health figure at all (dates, shift kinds, goal titles, settings only).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .. import db

NIVELES = ("completo", "resumen", "ninguno")
_DEFAULT = "completo"


def nivel_datos(conn: sqlite3.Connection, person_id: int = db.DEFAULT_PERSON_ID) -> str:
    row = conn.execute("SELECT nivel_datos FROM ajuste_agente WHERE person_id = ?", (person_id,)).fetchone()
    return row["nivel_datos"] if row else _DEFAULT


def set_nivel_datos(conn: sqlite3.Connection, person_id: int, nivel: str) -> None:
    if nivel not in NIVELES:
        raise ValueError(f"nivel_datos inválido: {nivel!r}; debe ser uno de {NIVELES}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO ajuste_agente (person_id, nivel_datos, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET nivel_datos = excluded.nivel_datos, "
            "updated_at = excluded.updated_at",
            (person_id, nivel, now),
        )
