"""The Cuenta Duna assistant key (BODY-MCP-HTTP-1, ORCH-ASISTENTE-PRODUCCION-1): the ONE key
Cuenta Duna issues per person, carrying every Duna app that person ticked when they granted it.
body/rutas/mcp_http.py's one endpoint authenticates every call with it -- never with Body's own
device-key table (body/dispositivo.py), which is the EXISTING hosted door's
(body/rutas/mcp_remoto.py) own auth, unchanged by this module.

A `Bearer <key>` header is checked against Cuenta's own `POST /oauth/introspect`, client_secret_basic
as `(cuenta.CLIENT_ID, cuenta.client_secret())` -- Body's OWN client credentials, never the
person's key, which travels only as the `token` form field. `producto == "body"` on the answer is
what proves Body was one of the apps this key was granted for; `persona_id` is Cuenta's own
`str(persona.id)` (the same value its OIDC `sub` carries), resolved against Body's own
`person.cuenta_sub` -- never a new identity concept, and never written here (body/cuenta.py, Tier
1, owns that column; this module only ever reads it).

A positive answer (`active: true`) is cached at most CACHE_TTL_SEGUNDOS, in process memory, keyed
by the key's OWN sha256 -- the key itself is never stored, logged, or put in any report. A
negative answer or any network/server fault is NEVER cached, so a revoked key stops working within
that same window and a freshly granted one works on its very first call.

Imports body/cuenta.py for its FOUR public names only (`hosted`, `issuer`, `client_secret`,
`CLIENT_ID`) -- never edits it.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

from . import cuenta

_HTTP_TIMEOUT = 5.0
CACHE_TTL_SEGUNDOS = 60.0
PRODUCTO = "body"
PERMISOS = ("lectura", "escritura")


class LlaveInvalida(Exception):
    """No header, not Bearer, an inactive key, a key not granted for Body, or an answer this
    module cannot parse -- body/rutas/mcp_http.py answers 401 with WWW-Authenticate: Bearer,
    never saying which of these it was (the contract's own rule: a wrong guess must learn nothing
    from the answer)."""


class PersonaSinBody(Exception):
    """The key is active and granted for Body, but no Body person has ever signed in under this
    same Cuenta identity (no `person.cuenta_sub` row matches) -- body/rutas/mcp_http.py answers
    401 with a sentence asking the person to sign in to Body once first."""


class CuentaNoDisponible(Exception):
    """Cuenta could not be reached, answered with a server error, or refused/throttled Body's OWN
    client credentials (401/429 on the introspection call itself) -- never 401 for the caller's
    key, which was never judged. `retry_after` carries Cuenta's own header verbatim, when it sent
    one, for body/rutas/mcp_http.py to pass through."""

    def __init__(self, retry_after: str | None = None):
        super().__init__()
        self.retry_after = retry_after


@dataclass(frozen=True)
class Autorizacion:
    person_id: int
    permiso: str
    llave_id: str | None


# Process-local, never persisted: {sha256(key): (vence_monotonic, Autorizacion)}.
_cache: dict[str, tuple[float, Autorizacion]] = {}


def _ahora() -> float:
    return time.monotonic()


def reset_cache() -> None:
    """Test-only: this cache is process-global state that must not leak between test cases."""
    _cache.clear()


def _clave_cache(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---- network seam (patched in tests, same shape as body/cuenta.py's own `_post_form`) ------------

def _post_introspect(key: str) -> httpx.Response:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
        return c.post(f"{cuenta.issuer()}/oauth/introspect", data={"token": key},
                       auth=(cuenta.CLIENT_ID, cuenta.client_secret()))


def _resolver_person_id(conn, persona_id: str) -> int:
    row = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (persona_id,)).fetchone()
    if row is None:
        raise PersonaSinBody()
    return row["id"]


def verificar(conn, key: str, *, reloj=None) -> Autorizacion:
    """Raises LlaveInvalida, PersonaSinBody or CuentaNoDisponible; otherwise returns the Body
    person and permission this key carries. `reloj`: an injectable clock for the cache's own TTL
    (real monotonic time in production), so a test can cross the 60 s window without sleeping."""
    reloj = reloj or _ahora
    h = _clave_cache(key)
    cached = _cache.get(h)
    if cached is not None:
        vence, autorizacion = cached
        if reloj() < vence:
            return autorizacion
        del _cache[h]

    try:
        r = _post_introspect(key)
    except httpx.HTTPError as e:
        raise CuentaNoDisponible() from e
    if r.status_code in (401, 429) or r.status_code >= 500:
        raise CuentaNoDisponible(retry_after=r.headers.get("retry-after"))
    if r.status_code != 200:
        raise LlaveInvalida()
    try:
        data = r.json()
    except ValueError:
        raise LlaveInvalida()
    if not isinstance(data, dict) or data.get("active") is not True or data.get("producto") != PRODUCTO:
        raise LlaveInvalida()
    persona_id = data.get("persona_id")
    permiso = data.get("permiso")
    if not isinstance(persona_id, str) or permiso not in PERMISOS:
        raise LlaveInvalida()
    llave_id = data.get("llave_id")

    person_id = _resolver_person_id(conn, persona_id)
    autorizacion = Autorizacion(person_id=person_id, permiso=permiso,
                                 llave_id=llave_id if isinstance(llave_id, str) else None)
    _cache[h] = (reloj() + CACHE_TTL_SEGUNDOS, autorizacion)
    return autorizacion
