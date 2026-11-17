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

from . import cuenta, db

KEY_ENV = "BODY_TOKEN_KEY"
_NONCE_LEN = 12

# BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 1: the window the daily pull's gap-fill heals and the
# window the sync-status read (ruling 3's leer_estado_reloj, ruling 2's Today card) reports on --
# one named constant, shared, so the two never drift apart.
VENTANA_RELLENO = 14

# BODY-GARMIN-RELLENO-ANTIGUO-1 ruling 1: at most this many gap days OLDER than VENTANA_RELLENO
# (and still inside the backfill's own 90-day horizon) the daily hosted pull retries per person
# per run, on top of the 14-day window above -- never displacing it, only taking the room left
# under pull.MAX_DIAS_POR_CORRIDA.
RELLENO_ANTIGUO_POR_CORRIDA = 5


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
            "INSERT INTO garmin_cuenta (person_id, token_cifrado, nonce, conectado_en, ultimo_error, "
            "reintentar_despues_de) VALUES (?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(person_id) DO UPDATE SET token_cifrado = excluded.token_cifrado, "
            "nonce = excluded.nonce, ultimo_error = NULL, reintentar_despues_de = NULL",
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


def dias_garmin_con_valor(conn: sqlite3.Connection, person_id: int, desde: str | None = None) -> set[str]:
    """BODY-GARMIN-DIA-VACIO-1 ruling 1: the days with at least one Garmin `daily_metric` row
    whose `value IS NOT NULL` -- one query, `desde` narrowing it to that day and later (left out,
    every day this person was ever pulled). A day Garmin answered with nothing (every metric key
    None or absent, `db.save_day`'s own INSERT OR REPLACE still runs) does NOT count here, so it
    stays a gap for the healing pull to retry and for `dias_sin_datos`/`ultimo_dia_con_datos` to
    skip -- unlike the old `SELECT DISTINCT day ... source = 'garmin'` (no value filter) that
    counted it as stored. A manual row (`source != 'garmin'`) or another person's row never
    counts, same as before."""
    sql = "SELECT DISTINCT day FROM daily_metric WHERE person_id = ? AND source = 'garmin' AND value IS NOT NULL"
    params: list = [person_id]
    if desde is not None:
        sql += " AND day >= ?"
        params.append(desde)
    return {r["day"] for r in conn.execute(sql, params).fetchall()}


def dias_sin_datos(conn: sqlite3.Connection, person_id: int, hoy: date) -> list[str]:
    """Every day in the last VENTANA_RELLENO days (today inclusive) with NO stored Garmin daily
    value (`dias_garmin_con_valor`, ruling 1 -- an all-NULL/empty day counts as a gap here too),
    oldest first -- one query (the window's own stored days), then a plain set difference, never
    one query per candidate day. Shared by body/pull.py's healing gap-fill (ruling 1) and the
    sync-status read (ruling 2's Today card, ruling 3's leer_estado_reloj), never duplicated
    between them."""
    limite = (hoy - timedelta(VENTANA_RELLENO - 1)).isoformat()
    almacenados = dias_garmin_con_valor(conn, person_id, desde=limite)
    return [d for d in (
        (hoy - timedelta(n)).isoformat() for n in range(VENTANA_RELLENO - 1, -1, -1)
    ) if d not in almacenados]


def dias_sin_datos_antiguos(conn: sqlite3.Connection, person_id: int, hoy: date, limite_dias: int) -> list[str]:
    """BODY-GARMIN-RELLENO-ANTIGUO-1 ruling 1: every day older than VENTANA_RELLENO's own window
    and inside the last `limite_dias` days (the caller's own backfill horizon, e.g.
    pull.BACKFILL_DIAS) with NO stored Garmin daily value -- the same "has data" rule
    `dias_sin_datos` already uses (`dias_garmin_con_valor`, an all-NULL/empty day still a gap
    here) -- one query for the whole range (the range's own stored days, `desde` narrowed to the
    range's oldest day), then a plain set difference, NEWEST first (the daily pull's own retry
    order, ruling 3 -- the days closest to falling out of the window get tried first). [] once
    `limite_dias` does not reach past VENTANA_RELLENO at all."""
    mas_antiguo = hoy - timedelta(limite_dias - 1)
    mas_nuevo = hoy - timedelta(VENTANA_RELLENO)
    if mas_nuevo < mas_antiguo:
        return []
    almacenados = dias_garmin_con_valor(conn, person_id, desde=mas_antiguo.isoformat())
    total_dias = (mas_nuevo - mas_antiguo).days + 1
    candidatos = ((mas_antiguo + timedelta(i)).isoformat() for i in range(total_dias))
    return [d for d in reversed(list(candidatos)) if d not in almacenados]


# BODY-SYNC-VIVO-ESTADO-1 ruling 2: a failed attempt's backoff window, chosen from the Garmin-
# reported HTTP status (body/garmin_adapter.estado_http) a caller passes to registrar_intento.
# 429 (rate-limited) waits the longest; a 5xx or a confirmed connection/timeout failure (estado_
# http's own 0 sentinel -- "transient, no numeric status") waits less; anything else (an auth
# failure, an unrecognised error) sets no backoff at all -- that case keeps today's handling (the
# token flow asks the person to reconnect; reconnecting is the fix, not a timer).
BACKOFF_429_MIN = 120
BACKOFF_TRANSITORIO_MIN = 15


def registrar_intento(
    conn: sqlite3.Connection, person_id: int, ahora: datetime, ok: bool, estado_http: int | None = None,
) -> None:
    """Records ONE pull attempt for this person, success or not (BODY-SYNC-VIVO-ESTADO-1 ruling
    1): ultimo_intento_en is always set to `ahora`. On success, reintentar_despues_de is cleared.
    On failure, it is set from `estado_http` by ruling 2's table above, or cleared when the
    failure carries none of those statuses (no backoff -- today's handling applies). Always a
    full overwrite, never a conditional preserve: one call, one clean record of what just
    happened."""
    if ok:
        espera_min = None
    elif estado_http == 429:
        espera_min = BACKOFF_429_MIN
    elif estado_http is not None and (estado_http == 0 or 500 <= estado_http < 600):
        espera_min = BACKOFF_TRANSITORIO_MIN
    else:
        espera_min = None
    reintentar = (ahora + timedelta(minutes=espera_min)).isoformat() if espera_min is not None else None
    with conn:
        conn.execute(
            "UPDATE garmin_cuenta SET ultimo_intento_en = ?, reintentar_despues_de = ? WHERE person_id = ?",
            (ahora.isoformat(), reintentar, person_id),
        )


def puede_intentar(conn: sqlite3.Connection, person_id: int, ahora: datetime, espacio_min: float) -> bool:
    """False while a backoff set by registrar_intento has not yet expired (`ahora` before
    reintentar_despues_de), or while the last attempt (any outcome) was less than `espacio_min`
    minutes ago -- a defensive minimum between attempts, independent of the backoff window. True
    for a person never attempted (no row, or a row with both columns NULL)."""
    row = conn.execute(
        "SELECT ultimo_intento_en, reintentar_despues_de FROM garmin_cuenta WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    if row is None:
        return True
    if row["reintentar_despues_de"] is not None and ahora < datetime.fromisoformat(row["reintentar_despues_de"]):
        return False
    if row["ultimo_intento_en"] is not None:
        if ahora - datetime.fromisoformat(row["ultimo_intento_en"]) < timedelta(minutes=espacio_min):
            return False
    return True


def registrar_reloj_sincronizo(conn: sqlite3.Connection, person_id: int, cuando: datetime) -> None:
    """The watch's own last-contact instant with Garmin (BODY-SYNC-VIVO-ESTADO-1 ruling 4) --
    written only when a pull's read of it succeeded; a failed or unparseable read leaves the
    last known value in place rather than clearing it (the caller only calls this when it has a
    value)."""
    with conn:
        conn.execute(
            "UPDATE garmin_cuenta SET reloj_sincronizo_en = ? WHERE person_id = ?",
            (cuando.isoformat(), person_id),
        )


def estado_sync(conn: sqlite3.Connection, person_id: int) -> dict:
    """One read of everything BODY-SYNC-VIVO-ESTADO-1 adds, plus the existing ultimo_ok_en
    (db.last_sync, unchanged) and ultimo_error (unchanged) -- shared by the live status views
    this slice's approval exists for, instead of five separate single-column reads."""
    row = conn.execute(
        "SELECT ultimo_intento_en, reintentar_despues_de, reloj_sincronizo_en, ultimo_error "
        "FROM garmin_cuenta WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    ultimo_ok_en = db.last_sync(conn, person_id)
    if row is None:
        return {
            "ultimo_intento_en": None, "ultimo_ok_en": ultimo_ok_en,
            "reintentar_despues_de": None, "reloj_sincronizo_en": None, "ultimo_error": None,
        }
    return {
        "ultimo_intento_en": row["ultimo_intento_en"],
        "ultimo_ok_en": ultimo_ok_en,
        "reintentar_despues_de": row["reintentar_despues_de"],
        "reloj_sincronizo_en": row["reloj_sincronizo_en"],
        "ultimo_error": row["ultimo_error"],
    }
