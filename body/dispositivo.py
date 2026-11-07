"""Device keys: one per linked PC per person, carrying explicit scopes (BODY-DISPOSITIVO-1,
ORCH-CARLOS-DELEGATES-TIER-1-1's design decision). The only module touching `dispositivo`.

A key is shown in the clear exactly once, the string `crear` returns -- only its sha256 hex ever
reaches the database. Never logs, prints or returns the plain key again once it has been returned.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import date, datetime, timezone

ALCANCES = {"turnos:subir", "vida:leer"}
KEY_PREFIX = "bdy_"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _encode_alcances(alcances) -> str:
    return " ".join(sorted(a for a in alcances if a in ALCANCES))


def crear(conn: sqlite3.Connection, person_id: int, nombre: str, alcances) -> str:
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO dispositivo (person_id, nombre, token_hash, alcances, creado_en) "
            "VALUES (?, ?, ?, ?, ?)",
            (person_id, nombre, _hash(key), _encode_alcances(alcances), now),
        )
    return key


def listar(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, nombre, alcances, creado_en, usado_en FROM dispositivo "
        "WHERE person_id = ? AND revocado_en IS NULL ORDER BY creado_en",
        (person_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def revocar(conn: sqlite3.Connection, person_id: int, dispositivo_id: int) -> bool:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "UPDATE dispositivo SET revocado_en = ? WHERE id = ? AND person_id = ? AND revocado_en IS NULL",
            (now, dispositivo_id, person_id),
        )
    return cur.rowcount > 0


def resolver(conn: sqlite3.Connection, key: str) -> tuple[int, set[str]] | None:
    """(person_id, alcances) for a live, unrevoked key, or None. Marks `usado_en` (date precision)
    on a successful resolve."""
    computed = _hash(key)
    row = conn.execute(
        "SELECT person_id, token_hash, alcances, revocado_en FROM dispositivo WHERE token_hash = ?",
        (computed,),
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], computed):
        return None
    if row["revocado_en"] is not None:
        return None
    with conn:
        conn.execute(
            "UPDATE dispositivo SET usado_en = ? WHERE token_hash = ?",
            (date.today().isoformat(), computed),
        )
    return row["person_id"], set(row["alcances"].split())
