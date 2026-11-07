"""Resolves WHO a web request serves. The one seam: every route in body/web.py reaches a
person only through `current_person_id`, wired in as a FastAPI dependency
(`Depends(current_person_id)`), so a test can swap it with `app.dependency_overrides` to
answer as a different person without touching any route.

Local mode (no Cuenta sign-in configured) resolves every request to the LOCAL person -- the one
row body/db.py's `init()` ensures exists on this PC, exactly as before BODY-CUENTA-RP-1. Hosted
mode resolves the signed-in person from `body_sesion`; a request with none, or an expired one,
raises `LoginRequired` (a page GET) or a plain 401 (a POST) -- body/web.py turns `LoginRequired`
into the redirect to /login.

Hosted mode also gates on consent (BODY-PRIVACIDAD-1, sensitive-data amendment rule 6): a signed-in
person with no accepted row for body/privacidad.py's current CONSENT_VERSION cannot reach any page
except the consent routes themselves, raising `ConsentRequired` (a page GET) or a plain 403 (a POST)
instead. Local mode never gates: one person, on their own PC.

A second seam, `person_from_device`, resolves WHO a device-key API request serves (BODY-DISPOSITIVO-1):
hosted only (body/web.py 404s these routes in local mode before this is ever reached), reading
`Authorization: Bearer <key>` rather than a cookie, and never gated by consent -- a key exists only
after the person has already consented and signed in to mint it.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from . import cuenta, db, dispositivo, privacidad

# The consent routes themselves, exempt from the gate below so accepting or declining is reachable.
_CONSENT_EXEMPT_PATHS = {"/privacidad/consentimiento"}


class LoginRequired(Exception):
    def __init__(self, return_to: str, prompt: str | None = None):
        self.return_to = return_to
        self.prompt = prompt


class ConsentRequired(Exception):
    def __init__(self, return_to: str):
        self.return_to = return_to


class DispositivoNoAutorizado(Exception):
    """No key, an unknown key, or a revoked one. body/web.py answers 401 with WWW-Authenticate."""


class DispositivoSinAlcance(Exception):
    """A live key whose scopes do not cover the route. body/web.py answers 403."""


def current_person_id(request: Request) -> int:
    conn = db.connect()
    try:
        db.init(conn)
        if not cuenta.hosted():
            return db.local_person_id(conn)
        token = request.cookies.get(cuenta.SESSION_COOKIE)
        person_id, existed = cuenta.resolve_session(conn, token)
        if person_id is not None and request.url.path not in _CONSENT_EXEMPT_PATHS:
            if not privacidad.has_consented(conn, person_id):
                if request.method == "POST":
                    raise HTTPException(403)
                raise ConsentRequired(cuenta.safe_return_to(request.url.path))
    finally:
        conn.close()
    if person_id is not None:
        return person_id
    if request.method == "POST":
        raise HTTPException(401)
    raise LoginRequired(cuenta.safe_return_to(request.url.path), prompt="none" if existed else None)


def person_from_device(scope: str):
    """A FastAPI dependency factory for the device-key API routes: returns the key's person_id
    when the key is live and carries `scope`, else raises `DispositivoNoAutorizado` (401) or
    `DispositivoSinAlcance` (403). Local mode 404s before this is ever reached."""

    def _dep(request: Request) -> int:
        if not cuenta.hosted():
            raise HTTPException(404)
        auth = request.headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            raise DispositivoNoAutorizado()
        key = auth[len("Bearer "):]
        conn = db.connect()
        try:
            db.init(conn)
            result = dispositivo.resolver(conn, key)
        finally:
            conn.close()
        if result is None:
            raise DispositivoNoAutorizado()
        person_id, alcances = result
        if scope not in alcances:
            raise DispositivoSinAlcance()
        return person_id

    return _dep
