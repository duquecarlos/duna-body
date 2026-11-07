"""Conversations with Body's assistant, stored per person (BODY-ASISTENTE-PROVEEDOR-1, decision
4). Tool results are stored too -- they are the person's own data, on Body's own disk -- so the
history sent back to the provider is rebuilt server-side from these rows (agent-capabilities-v1
section 7: a server-stored history needs no signing).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


class ConversacionAjena(Exception):
    """A conversacion_id that exists but belongs to another person, or does not exist at all."""


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def crear(conn: sqlite3.Connection, person_id: int, titulo: str | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en, titulo) VALUES (?, ?, ?)",
            (person_id, _ahora(), titulo),
        )
    return cur.lastrowid


def verificar_propiedad(conn: sqlite3.Connection, conversacion_id: int, person_id: int) -> None:
    row = conn.execute(
        "SELECT person_id FROM conversacion_agente WHERE id = ?", (conversacion_id,)
    ).fetchone()
    if row is None or row["person_id"] != person_id:
        raise ConversacionAjena()


def agregar_mensaje(conn: sqlite3.Connection, conversacion_id: int, person_id: int, rol: str,
                     bloques: list[dict]) -> None:
    with conn:
        conn.execute(
            "INSERT INTO mensaje_agente (conversacion_id, person_id, rol, contenido_json, creado_en) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversacion_id, person_id, rol, json.dumps(bloques, ensure_ascii=False), _ahora()),
        )


def historial(conn: sqlite3.Connection, conversacion_id: int, person_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT rol, contenido_json FROM mensaje_agente WHERE conversacion_id = ? AND person_id = ? ORDER BY id",
        (conversacion_id, person_id),
    ).fetchall()
    return [{"rol": r["rol"], "bloques": json.loads(r["contenido_json"])} for r in rows]
