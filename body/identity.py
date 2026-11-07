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
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from . import cuenta, db, privacidad

# The consent routes themselves, exempt from the gate below so accepting or declining is reachable.
_CONSENT_EXEMPT_PATHS = {"/privacidad/consentimiento"}


class LoginRequired(Exception):
    def __init__(self, return_to: str, prompt: str | None = None):
        self.return_to = return_to
        self.prompt = prompt


class ConsentRequired(Exception):
    def __init__(self, return_to: str):
        self.return_to = return_to


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
