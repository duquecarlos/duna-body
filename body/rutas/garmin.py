"""The Garmin connection (hosted only), moved out of body/web.py as one domain (BODY-WEB-
ESTRUCTURA-8): `/garmin`, `/garmin/conectar`, `/garmin/codigo`, `/garmin/desconectar`, with the
MFA state they share. Pure move -- same paths, same bodies. One connection per person, the token
encrypted by body/garmin_cuenta.py.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import cuenta, db, garmin_adapter, garmin_cuenta, identity
from ..i18n import T
from . import comun

router = APIRouter()

_MFA_TTL_SECONDS = 600
_MFA_STATE: dict[str, tuple[str, float]] = {}  # session-token hash -> (client_state_json, started_at)

_GARMIN_LAST_ERROR_KEYS = {"NotSignedIn": "garmin.error.not_signed_in"}
_GARMIN_FORM_ERROR_KEYS = {"datos": "garmin.error.datos", "garmin": "garmin.error.generic",
                           "mfa_expired": "garmin.error.mfa_expired"}


def _garmin_last_error_text(t: T, error_cls: str | None) -> str | None:
    if not error_cls:
        return None
    return t(_GARMIN_LAST_ERROR_KEYS.get(error_cls, "garmin.error.pull_failed"))


def _mfa_key(request: Request) -> str:
    token = request.cookies.get(cuenta.SESSION_COOKIE) or ""
    return hashlib.sha256(token.encode()).hexdigest()


def _prune_mfa_state() -> None:
    cutoff = time.monotonic() - _MFA_TTL_SECONDS
    for k in [k for k, (_, started) in _MFA_STATE.items() if started < cutoff]:
        _MFA_STATE.pop(k, None)


def _garmin_ctx(t: T, conn, person_id: int, **extra) -> dict:
    connected_since = garmin_cuenta.conectado_desde(conn, person_id)
    ctx = {
        "t": t, "tab": "settings", "turnos": db.trabaja_turnos_si(conn, person_id),
        "connected": connected_since is not None,
        "connected_since": comun._short_date(t, datetime.fromisoformat(connected_since).date())
                           if connected_since else None,
        "error_text": _garmin_last_error_text(t, garmin_cuenta.ultimo_error(conn, person_id)),
        "form_error": None, "mfa": False,
    }
    ctx.update(extra)
    return ctx


@router.get("/garmin")
@comun._cierra_conexion
def garmin_view(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, t = comun._ctx(person_id)
    err = request.query_params.get("error")
    form_error = t(_GARMIN_FORM_ERROR_KEYS[err]) if err in _GARMIN_FORM_ERROR_KEYS else None
    return comun._render("garmin.html", **_garmin_ctx(t, conn, person_id, form_error=form_error))


@router.post("/garmin/conectar")
@comun._cierra_conexion_async
async def garmin_conectar(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await comun._form(request)
    email = (f.get("email") or "").strip()
    password = f.get("password") or ""
    if not email or not password:
        return RedirectResponse("/garmin?error=datos", status_code=303)
    try:
        status, result = garmin_adapter.start_sign_in(email, password)
    except Exception:
        return RedirectResponse("/garmin?error=garmin", status_code=303)
    finally:
        password = None  # noqa: F841
    if status == "needs_mfa":
        _prune_mfa_state()
        _MFA_STATE[_mfa_key(request)] = (result, time.monotonic())
        conn, _, t = comun._ctx(person_id)
        return comun._render("garmin.html", **_garmin_ctx(t, conn, person_id, mfa=True))
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.guardar(conn, person_id, result)
    return RedirectResponse("/garmin", status_code=303)


@router.post("/garmin/codigo")
@comun._cierra_conexion_async
async def garmin_codigo(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await comun._form(request)
    code = (f.get("code") or "").strip()
    key = _mfa_key(request)
    entry = _MFA_STATE.get(key)
    if entry is None or time.monotonic() - entry[1] > _MFA_TTL_SECONDS:
        return RedirectResponse("/garmin?error=mfa_expired", status_code=303)
    client_state_json, _ = entry
    try:
        token = garmin_adapter.finish_sign_in(client_state_json, code)
    except Exception:
        # A wrong code may be retried within the same TTL window -- the state is kept, not popped.
        conn, _, t = comun._ctx(person_id)
        return comun._render("garmin.html",
                       **_garmin_ctx(t, conn, person_id, mfa=True, form_error=t("garmin.error.generic")))
    finally:
        code = None  # noqa: F841
    _MFA_STATE.pop(key, None)
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.guardar(conn, person_id, token)
    return RedirectResponse("/garmin", status_code=303)


@router.post("/garmin/desconectar")
@comun._cierra_conexion
def garmin_desconectar(person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.borrar(conn, person_id)
    return RedirectResponse("/garmin", status_code=303)
