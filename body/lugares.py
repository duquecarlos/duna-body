"""The ONLY module that talks to Lugares (BODY-HISTORIA-VIDA-LUGARES-1, like
body/garmin_adapter.py is the only module that imports garminconnect).

Reads Lugares' own read-only trips API (`GET /api/vida/eventos`, LUGARES-VIDA-VIAJES-LECTURA-1)
for ONE person -- never writes anything to Lugares, never sends a health figure. The only inputs
are this person's own Cuenta `sub` (`person.cuenta_sub`, never a config value, never an email,
never `BODY_PERSON_KEY` or vida-v1's local key -- ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1) and two
dates. No sub, or no `config.lugares_token()` configured: no request at all (ruling 1-2) -- the
person is simply «sin conectar».

Nothing calls `sincronizar` yet except body/pull.py's own daily safe wrapper -- NOTHING IS LIVE
until Carlos sets `BODY_LUGARES_TOKEN` at deploy and runs `python -m body.vincular_cuenta` once.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from . import config, enlaces

_HTTP_TIMEOUT = 5.0

# BODY-HISTORIA-VIDA-LUGARES-1-WINDOW: a single [hoy - 3650, hoy + 365] request is 4015 days,
# OVER Lugares' own maximum span (Main's wire, verbatim: "max hasta - desde = 3660 days"), which
# would get every live sync a 422 `rango_invalido`. Carlos asked for «all my historical data», so
# the past is not shortened -- instead `sincronizar` makes TWO requests, each inside the maximum:
# the past [hoy - 3650, hoy] (3650 days) and the planned [hoy + 1, hoy + 365] (364 days). See
# MAX_SPAN_DIAS below.
VENTANA_PASADO_DIAS = 3650
VENTANA_FUTURO_DIAS = 365

# Main's wire, verbatim: "max hasta - desde = 3660 days".
MAX_SPAN_DIAS = 3660

MOTIVO_RED = "red"
MOTIVO_FORMATO = "formato"


# ---- network seam (patched in every test) ---------------------------------------------------------

def _get_json(url: str, token: str) -> tuple[int, dict]:
    """GETs `url` with Lugares' own Bearer token -- never logged, never put in an exception
    message. Returns (0, {}) for anything that never got an HTTP response at all (DNS, timeout,
    connection refused): 0 is never a real HTTP status, so `sincronizar` reads it as a network
    error, same bucket as a connection that came back but made no sense."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return 0, {}
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def _cuenta_sub(conn: sqlite3.Connection, person_id: int) -> str | None:
    row = conn.execute("SELECT cuenta_sub FROM person WHERE id = ?", (person_id,)).fetchone()
    return row["cuenta_sub"] if row else None


_MOTIVO_POR_ESTADO = {401: "token_invalido", 422: "rango_invalido", 503: "vida_no_configurado"}


def _validar_evento(ev) -> tuple[str, str, str] | None:
    """One wire event -> (ref, starts_at, ends_at), or None when it fails any check (counted as
    invalid by the caller, never stored, never raises)."""
    if not isinstance(ev, dict) or ev.get("kind") != "trip":
        return None
    ref = ev.get("ref")
    if ref is None:
        return None
    starts_at, ends_at = ev.get("starts_at"), ev.get("ends_at")
    if not isinstance(starts_at, str) or not isinstance(ends_at, str):
        return None
    try:
        d_starts, d_ends = date.fromisoformat(starts_at), date.fromisoformat(ends_at)
    except ValueError:
        return None
    if d_starts > d_ends:
        return None
    return str(ref), starts_at, ends_at


def _registrar_intento(conn: sqlite3.Connection, person_id: int, ahora: str, motivo: str) -> None:
    """Keeps the cache as it is (ruling 3) -- `ok_at` is never touched here, only on a 200."""
    with conn:
        conn.execute(
            "INSERT INTO lugares_sync (person_id, intento_at, ok_at, motivo) VALUES (?, ?, NULL, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET intento_at = excluded.intento_at, motivo = excluded.motivo",
            (person_id, ahora, motivo),
        )


def _url(sub: str, desde: date, hasta: date) -> str:
    assert desde <= hasta and (hasta - desde).days <= MAX_SPAN_DIAS
    return (f"{enlaces.origen('lugares')}/api/vida/eventos?person={quote(sub, safe='')}"
            f"&desde={desde.isoformat()}&hasta={hasta.isoformat()}")


def sincronizar(conn: sqlite3.Connection, person_id: int, hoy: date) -> dict:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3, corrected by
    BODY-HISTORIA-VIDA-LUGARES-1-WINDOW: {"estado": "sin_conectar"} with no request at all when
    this person has no Cuenta sub or the host has no token configured; otherwise TWO GETs, each
    inside Lugares' MAX_SPAN_DIAS -- the past `[hoy - VENTANA_PASADO_DIAS, hoy]` and the planned
    `[hoy + 1, hoy + VENTANA_FUTURO_DIAS]` -- with the same person, Bearer header and seam. Both
    must answer 200 with a valid body for the person's cache to be REPLACED, in one transaction; a
    trip whose ref appears in both answers (one that spans today) is stored once, its dates as
    Lugares sent them -- {"estado": "ok", "viajes": n, "invalidos": n}. If either call returns
    401/422/503, a network error (`_get_json`'s own (0, {})) or a malformed body (no `eventos`
    list), the cache is left as it is and that failing call's motivo is recorded --
    {"estado": "error", "motivo": ...}. Never raises."""
    sub = _cuenta_sub(conn, person_id)
    if not sub:
        return {"estado": "sin_conectar", "viajes": 0}
    token = config.lugares_token()
    if not token:
        return {"estado": "sin_conectar", "viajes": 0}

    url_pasado = _url(sub, hoy - timedelta(days=VENTANA_PASADO_DIAS), hoy)
    url_planeado = _url(sub, hoy + timedelta(days=1), hoy + timedelta(days=VENTANA_FUTURO_DIAS))
    respuestas = [_get_json(url_pasado, token), _get_json(url_planeado, token)]
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for estado_http, _ in respuestas:
        if estado_http == 0:
            _registrar_intento(conn, person_id, ahora, MOTIVO_RED)
            return {"estado": "error", "viajes": 0, "motivo": MOTIVO_RED}
        if estado_http != 200:
            motivo = _MOTIVO_POR_ESTADO.get(estado_http, str(estado_http))
            _registrar_intento(conn, person_id, ahora, motivo)
            return {"estado": "error", "viajes": 0, "motivo": motivo}

    eventos_combinados = []
    for _, body in respuestas:
        eventos = body.get("eventos") if isinstance(body, dict) else None
        if not isinstance(eventos, list):
            _registrar_intento(conn, person_id, ahora, MOTIVO_FORMATO)
            return {"estado": "error", "viajes": 0, "motivo": MOTIVO_FORMATO}
        eventos_combinados.extend(eventos)

    validos: dict[str, tuple[str, str, str]] = {}
    invalidos = 0
    for ev in eventos_combinados:
        fila = _validar_evento(ev)
        if fila is None:
            invalidos += 1
        else:
            ref, s, e = fila
            validos[ref] = (ref, s, e)

    with conn:
        conn.execute("DELETE FROM viaje_lugares WHERE person_id = ?", (person_id,))
        conn.executemany(
            "INSERT INTO viaje_lugares (person_id, ref, starts_at, ends_at, fetched_at) VALUES (?, ?, ?, ?, ?)",
            [(person_id, ref, s, e, ahora) for ref, s, e in validos.values()],
        )
        conn.execute(
            "INSERT INTO lugares_sync (person_id, intento_at, ok_at, motivo) VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(person_id) DO UPDATE SET intento_at = excluded.intento_at, "
            "ok_at = excluded.ok_at, motivo = NULL",
            (person_id, ahora, ahora),
        )
    return {"estado": "ok", "viajes": len(validos), "invalidos": invalidos}
