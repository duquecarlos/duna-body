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
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import cuenta

KEY_ENV = "BODY_TOKEN_KEY"
_NONCE_LEN = 12


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
