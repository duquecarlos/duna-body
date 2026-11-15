"""Each hosted person's Garmin token, encrypted at rest (BODY-GARMIN-HOST-1, sensitive-data
amendment rule 2): AES-256-GCM, key from the hex env BODY_TOKEN_KEY, a random 12-byte nonce per
write. Associated data binds a ciphertext to the person_id it was encrypted for, so a row moved to
another person's id fails to decrypt instead of silently handing over their Garmin access.

Never logs, prints or returns the key, the plaintext token or a password -- only token strings in
and out, to and from the caller.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import cuenta

KEY_ENV = "BODY_TOKEN_KEY"
_NONCE_LEN = 12

# BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 1: the window the daily pull's gap-fill heals and the
# window the sync-status read (ruling 3's leer_estado_reloj, ruling 2's Today card) reports on --
# one named constant, shared, so the two never drift apart.
VENTANA_RELLENO = 14


def _decode_key(hex_key: str) -> bytes:
    """Refuses anything but exactly 32 bytes of hex -- the message names the variable and how to
    generate a valid one, never the value itself."""
    try:
        raw = bytes.fromhex(hex_key)
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise RuntimeError(
            f"{KEY_ENV} must be exactly 32 bytes of hex (64 hex characters). "
            f"Generate one with: openssl rand -hex 32"
        )
    return raw


def _key() -> bytes:
    hex_key = os.environ.get(KEY_ENV)
    if not hex_key:
        raise RuntimeError(f"{KEY_ENV} is not set")
    return _decode_key(hex_key)


def ensure_configured() -> None:
    """Called once from body/web.py's lifespan, before the app accepts any request. Hosted mode
    with no key, or a key that is not exactly 32 bytes of hex, refuses to start; the message never
    carries a value. Mirrors cuenta.ensure_configured()'s pattern."""
    if not cuenta.hosted():
        return
    hex_key = os.environ.get(KEY_ENV)
    if not hex_key:
        raise RuntimeError(f"{KEY_ENV} is required when {cuenta.ISSUER_ENV} is set")
    _decode_key(hex_key)


def _aad(person_id: int) -> bytes:
    return b"body-garmin:" + str(person_id).encode()


def guardar(conn: sqlite3.Connection, person_id: int, token_str: str) -> None:
    aesgcm = AESGCM(_key())
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, token_str.encode(), _aad(person_id))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO garmin_cuenta (person_id, token_cifrado, nonce, conectado_en, ultimo_error) "
            "VALUES (?, ?, ?, ?, NULL) "
            "ON CONFLICT(person_id) DO UPDATE SET token_cifrado = excluded.token_cifrado, "
            "nonce = excluded.nonce, ultimo_error = NULL",
            (person_id, ciphertext, nonce, now),
        )


def leer(conn: sqlite3.Connection, person_id: int) -> str | None:
    row = conn.execute(
        "SELECT token_cifrado, nonce FROM garmin_cuenta WHERE person_id = ?", (person_id,)
    ).fetchone()
    if row is None:
        return None
    aesgcm = AESGCM(_key())
    plaintext = aesgcm.decrypt(row["nonce"], row["token_cifrado"], _aad(person_id))
    return plaintext.decode()


def borrar(conn: sqlite3.Connection, person_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM garmin_cuenta WHERE person_id = ?", (person_id,))


def conectado(conn: sqlite3.Connection, person_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM garmin_cuenta WHERE person_id = ?", (person_id,)
    ).fetchone() is not None


def conectado_desde(conn: sqlite3.Connection, person_id: int) -> str | None:
    row = conn.execute(
        "SELECT conectado_en FROM garmin_cuenta WHERE person_id = ?", (person_id,)
    ).fetchone()
    return row["conectado_en"] if row else None


def ultimo_error(conn: sqlite3.Connection, person_id: int) -> str | None:
    row = conn.execute(
        "SELECT ultimo_error FROM garmin_cuenta WHERE person_id = ?", (person_id,)
    ).fetchone()
    return row["ultimo_error"] if row else None


def registrar_error(conn: sqlite3.Connection, person_id: int, error_cls: str) -> None:
    with conn:
        conn.execute(
            "UPDATE garmin_cuenta SET ultimo_error = ? WHERE person_id = ?", (error_cls, person_id)
        )


def limpiar_error(conn: sqlite3.Connection, person_id: int) -> None:
    with conn:
        conn.execute("UPDATE garmin_cuenta SET ultimo_error = NULL WHERE person_id = ?", (person_id,))


def every_person_id(conn: sqlite3.Connection) -> list[int]:
    return [r["person_id"] for r in conn.execute("SELECT person_id FROM garmin_cuenta ORDER BY person_id")]


def marcador_backfill(conn: sqlite3.Connection, person_id: int) -> str | None:
    """The oldest day (YYYY-MM-DD) this person's hosted --backfill has already attempted -- with
    or without data -- so it is never asked again. None if backfill hasn't started yet."""
    row = conn.execute(
        "SELECT backfill_hasta FROM garmin_cuenta WHERE person_id = ?", (person_id,)
    ).fetchone()
    return row["backfill_hasta"] if row else None


def avanzar_backfill(conn: sqlite3.Connection, person_id: int, hasta: str) -> None:
    with conn:
        conn.execute("UPDATE garmin_cuenta SET backfill_hasta = ? WHERE person_id = ?", (hasta, person_id))


def estado_conexion(conn: sqlite3.Connection, person_id: int) -> dict:
    """One query: whether this person has a Garmin connection, since when, and their last
    recorded pull error -- shared by Hoy's sync card (ruling 2) and leer_estado_reloj (ruling 3)
    instead of three separate single-column reads."""
    row = conn.execute(
        "SELECT conectado_en, ultimo_error FROM garmin_cuenta WHERE person_id = ?", (person_id,)
    ).fetchone()
    if row is None:
        return {"conectado": False, "conectado_desde": None, "ultimo_error": None}
    return {"conectado": True, "conectado_desde": row["conectado_en"], "ultimo_error": row["ultimo_error"]}


def dias_sin_datos(conn: sqlite3.Connection, person_id: int, hoy: date) -> list[str]:
    """Every day in the last VENTANA_RELLENO days (today inclusive) with NO stored Garmin daily
    row (`daily_metric` where `source = 'garmin'`), oldest first -- one query (the window's own
    stored days), then a plain set difference, never one query per candidate day. Shared by
    body/pull.py's healing gap-fill (ruling 1) and the sync-status read (ruling 2's Today card,
    ruling 3's leer_estado_reloj), never duplicated between them."""
    limite = (hoy - timedelta(VENTANA_RELLENO - 1)).isoformat()
    almacenados = {r["day"] for r in conn.execute(
        "SELECT DISTINCT day FROM daily_metric WHERE person_id = ? AND source = 'garmin' AND day >= ?",
        (person_id, limite)).fetchall()}
    return [d for d in (
        (hoy - timedelta(n)).isoformat() for n in range(VENTANA_RELLENO - 1, -1, -1)
    ) if d not in almacenados]
