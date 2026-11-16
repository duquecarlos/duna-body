"""The Garmin connection (hosted only), moved out of body/web.py as one domain (BODY-WEB-
ESTRUCTURA-8): `/garmin`, `/garmin/conectar`, `/garmin/codigo`, `/garmin/desconectar`, with the
MFA state they share. Pure move -- same paths, same bodies. One connection per person, the token
encrypted by body/garmin_cuenta.py.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import cuenta, db, garmin_adapter, garmin_cuenta, identity, pull
from ..i18n import T
from . import comun

router = APIRouter()


def _lanzar_pull_conexion(person_id: int) -> None:
    """The BackgroundTasks target for ruling 4b: runs pull.pull_persona_ahora for this person
    unless one is already running for them. BODY-SYNC-VIVO-2 ruling 1: the guard set itself moved
    to body/pull.py (`pull._PULL_EN_CURSO`), shared with the on-demand live pull
    (`pull.pull_hoy_ahora`) -- reached here as `pull._PULL_EN_CURSO`, never re-bound under a local
    name (tests/test_guardia_parches.py's NoShimAliasGuardTest forbids that shape)."""
    if person_id in pull._PULL_EN_CURSO:
        return
    pull._PULL_EN_CURSO.add(person_id)
    try:
        pull.pull_persona_ahora(person_id)
    finally:
        pull._PULL_EN_CURSO.discard(person_id)

_MFA_TTL_SECONDS = 600
_MFA_MAX_ATTEMPTS = 5
_MFA_STATE: dict[str, tuple[str, float, int]] = {}  # session-token hash -> (client_state_json, started_at, wrong_codes)

_SIGNIN_MAX_ATTEMPTS = 5
_SIGNIN_WINDOW_SECONDS = 3600
# person_id -> start timestamps within the sliding window, for every /garmin/conectar start that
# did not end with a token stored -- a plain failure, a needs_mfa that is never finished, one whose
# MFA state is later dropped (5 wrong codes) or expires. Cleared only when a token is finally stored.
_SIGNIN_ATTEMPTS: dict[int, list[float]] = {}

_GARMIN_LAST_ERROR_KEYS = {"NotSignedIn": "garmin.error.not_signed_in"}
_GARMIN_FORM_ERROR_KEYS = {"datos": "garmin.error.datos", "demasiados": "garmin.error.demasiados",
                           "garmin": "garmin.error.generic", "mfa_expired": "garmin.error.mfa_expired",
                           "mfa_demasiados": "garmin.error.mfa_demasiados"}


def _garmin_last_error_text(t: T, error_cls: str | None) -> str | None:
    if not error_cls:
        return None
    return t(_GARMIN_LAST_ERROR_KEYS.get(error_cls, "garmin.error.pull_failed"))


def _mfa_key(request: Request) -> str:
    token = request.cookies.get(cuenta.SESSION_COOKIE) or ""
    return hashlib.sha256(token.encode()).hexdigest()


def _prune_mfa_state() -> None:
    cutoff = time.monotonic() - _MFA_TTL_SECONDS
    for k in [k for k, (_, started, _wrong) in _MFA_STATE.items() if started < cutoff]:
        _MFA_STATE.pop(k, None)


def _prune_signin_attempts() -> None:
    cutoff = time.monotonic() - _SIGNIN_WINDOW_SECONDS
    for pid in list(_SIGNIN_ATTEMPTS):
        kept = [ts for ts in _SIGNIN_ATTEMPTS[pid] if ts > cutoff]
        if kept:
            _SIGNIN_ATTEMPTS[pid] = kept
        else:
            _SIGNIN_ATTEMPTS.pop(pid, None)


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


def _reloj_vivo_ctx(t: T, conn, person_id: int) -> dict | None:
    """BODY-SYNC-VIVO-2 ruling 3: the «Tu reloj» live card's own context -- shared by Hoy's `/`
    (body/rutas/hoy.py) and this module's own polling endpoint below, so the two can never render
    a different card for the same state. None while nothing of the three states applies (no card,
    no claim) -- the same "honest gap" rule Hoy's other cards already follow."""
    person = db.person(conn, person_id)
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    hoy_local = comun._today_local(person)
    texto = pull.texto_estado_vivo(t, conn, person_id, tz, hoy_local)
    if texto is None:
        return None
    return {"texto": texto, "sincronizando": person_id in pull._PULL_EN_CURSO}


@router.get("/garmin/estado-vivo")
@comun._cierra_conexion
def garmin_estado_vivo(person_id: int = Depends(identity.current_person_id)):
    """BODY-SYNC-VIVO-2 ruling 3: the small GET `static/reloj-sync.js` polls while the «Tu reloj»
    card shows a pull in progress -- renders the SAME partial (`_reloj.html`) Hoy's own card uses,
    so the browser can just swap the card's markup in place. `reloj_vivo` is None when none of the
    three live states apply any more (an edge case: a person who has never once watch-synced, and
    whose last attempt failed with no backoff) -- the partial then renders a plain, textless card,
    which carries no `data-sincronizando` attribute either, so the poller reads that as "done"."""
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, t = comun._ctx(person_id)
    return comun._render("_reloj.html", t=t, reloj_vivo=_reloj_vivo_ctx(t, conn, person_id))


@router.post("/garmin/conectar")
@comun._cierra_conexion_async
async def garmin_conectar(request: Request, background_tasks: BackgroundTasks,
                           person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await comun._form(request)
    email = (f.get("email") or "").strip()
    password = f.get("password") or ""
    if not email or not password:
        return RedirectResponse("/garmin?error=datos", status_code=303)
    _prune_signin_attempts()
    if len(_SIGNIN_ATTEMPTS.get(person_id, [])) >= _SIGNIN_MAX_ATTEMPTS:
        # Refused before the adapter is called -- the password is never sent to Garmin, and
        # Garmin never sees another start from this host on this person's behalf.
        return RedirectResponse("/garmin?error=demasiados", status_code=303)
    try:
        status, result = garmin_adapter.start_sign_in(email, password)
    except Exception:
        _SIGNIN_ATTEMPTS.setdefault(person_id, []).append(time.monotonic())
        return RedirectResponse("/garmin?error=garmin", status_code=303)
    finally:
        password = None  # noqa: F841
    if status == "needs_mfa":
        # This start counts now, whatever becomes of the MFA state -- finished with a right code
        # clears it below in garmin_codigo, anything else (wrong-code drop, expiry) leaves it counted.
        _SIGNIN_ATTEMPTS.setdefault(person_id, []).append(time.monotonic())
        _prune_mfa_state()
        _MFA_STATE[_mfa_key(request)] = (result, time.monotonic(), 0)
        conn, _, t = comun._ctx(person_id)
        return comun._render("garmin.html", **_garmin_ctx(t, conn, person_id, mfa=True))
    _SIGNIN_ATTEMPTS.pop(person_id, None)
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.guardar(conn, person_id, result)
    # Ruling 4b: starts this person's own healing pull right away, after the response is sent.
    background_tasks.add_task(_lanzar_pull_conexion, person_id)
    return RedirectResponse("/garmin", status_code=303)


@router.post("/garmin/codigo")
@comun._cierra_conexion_async
async def garmin_codigo(request: Request, background_tasks: BackgroundTasks,
                         person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await comun._form(request)
    code = (f.get("code") or "").strip()
    key = _mfa_key(request)
    entry = _MFA_STATE.get(key)
    if entry is None or time.monotonic() - entry[1] > _MFA_TTL_SECONDS:
        return RedirectResponse("/garmin?error=mfa_expired", status_code=303)
    client_state_json, started_at, wrong_codes = entry
    try:
        token = garmin_adapter.finish_sign_in(client_state_json, code)
    except Exception:
        # A wrong code may be retried within the same TTL window, up to _MFA_MAX_ATTEMPTS -- past
        # that the state is dropped so the person starts again from the email form. The start that
        # opened this state is already in _SIGNIN_ATTEMPTS and stays there; it is not cleared here.
        wrong_codes += 1
        if wrong_codes >= _MFA_MAX_ATTEMPTS:
            _MFA_STATE.pop(key, None)
            return RedirectResponse("/garmin?error=mfa_demasiados", status_code=303)
        _MFA_STATE[key] = (client_state_json, started_at, wrong_codes)
        conn, _, t = comun._ctx(person_id)
        return comun._render("garmin.html",
                       **_garmin_ctx(t, conn, person_id, mfa=True, form_error=t("garmin.error.generic")))
    finally:
        code = None  # noqa: F841
    _MFA_STATE.pop(key, None)
    _SIGNIN_ATTEMPTS.pop(person_id, None)
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.guardar(conn, person_id, token)
    # Ruling 4b: starts this person's own healing pull right away, after the response is sent.
    background_tasks.add_task(_lanzar_pull_conexion, person_id)
    return RedirectResponse("/garmin", status_code=303)


@router.post("/garmin/desconectar")
@comun._cierra_conexion
def garmin_desconectar(person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, _ = comun._ctx(person_id)
    garmin_cuenta.borrar(conn, person_id)
    return RedirectResponse("/garmin", status_code=303)
