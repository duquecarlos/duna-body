"""Resolves WHO a web request serves. The one seam: every route in body/web.py reaches a
person only through `current_person_id`, wired in as a FastAPI dependency
(`Depends(current_person_id)`), so a test can swap it with `app.dependency_overrides` to
answer as a different person without touching any route.

Today Body has no Cuenta sign-in, so every request resolves to the LOCAL person -- the one
row body/db.py's `init()` ensures exists on this PC. The Cuenta sign-in slice (wave 8) will
replace this function's body with a real session/cookie lookup; nothing else needs to change.
"""
from __future__ import annotations

from fastapi import Request

from . import db


def current_person_id(request: Request) -> int:
    conn = db.connect()
    try:
        db.init(conn)
        return db.local_person_id(conn)
    finally:
        conn.close()
