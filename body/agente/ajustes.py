"""Per-person assistant setting: how much health data the assistant's tools may read.

completo (default, Carlos's own choice, BODY-ASSISTANT-DATA-LEVEL-1) = day-by-day figures a question
needs. resumen = only aggregates (means over a window, counts, verdicts in words), never one day's
value. ninguno = no health figure at all (dates, shift kinds, goal titles, settings only).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

NIVELES = ("completo", "resumen", "ninguno")
_DEFAULT = "completo"
NOMBRES_PROVEEDOR = ("anthropic", "openai", "gemini")


def nivel_datos(conn: sqlite3.Connection, person_id: int) -> str:
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


def proveedor(conn: sqlite3.Connection, person_id: int) -> str | None:
    """The person's own stored choice of provider, or None when they have not chosen one."""
    row = conn.execute("SELECT proveedor FROM ajuste_agente WHERE person_id = ?", (person_id,)).fetchone()
    return row["proveedor"] if row else None


def set_proveedor(conn: sqlite3.Connection, person_id: int, nombre: str) -> None:
    if nombre not in NOMBRES_PROVEEDOR:
        raise ValueError(f"proveedor inválido: {nombre!r}; debe ser uno de {NOMBRES_PROVEEDOR}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO ajuste_agente (person_id, proveedor, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET proveedor = excluded.proveedor, "
            "updated_at = excluded.updated_at",
            (person_id, nombre, now),
        )
