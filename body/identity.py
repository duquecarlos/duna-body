"""Resolves WHO a web request serves. The one seam: every route in body/web.py reaches a
person only through `current_person_id`, wired in as a FastAPI dependency
(`Depends(current_person_id)`), so a test can swap it with `app.dependency_overrides` to
answer as a different person without touching any route.

Local mode (no Cuenta sign-in configured) resolves every request to the LOCAL person -- the one
row body/db.py's `init()` ensures exists on this PC, exactly as before BODY-CUENTA-RP-1. Hosted
mode resolves the signed-in person from `body_sesion`; a request with none, or an expired one,
raises `LoginRequired` (a page GET) or a plain 401 (a POST) -- body/web.py turns `LoginRequired`
into the redirect to /login.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from . import cuenta, db


class LoginRequired(Exception):
    def __init__(self, return_to: str, prompt: str | None = None):
        self.return_to = return_to
        self.prompt = prompt


def current_person_id(request: Request) -> int:
    conn = db.connect()
    try:
        db.init(conn)
        if not cuenta.hosted():
            return db.local_person_id(conn)
        token = request.cookies.get(cuenta.SESSION_COOKIE)
        person_id, existed = cuenta.resolve_session(conn, token)
    finally:
        conn.close()
    if person_id is not None:
        return person_id
    if request.method == "POST":
        raise HTTPException(401)
    raise LoginRequired(cuenta.safe_return_to(request.url.path), prompt="none" if existed else None)
