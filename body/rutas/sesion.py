"""The Cuenta Duna sign-in session (BODY-WEB-ESTRUCTURA-8), moved out of body/web.py as one
domain: `/login`, `/login/cuenta/start`, `/login/cuenta/callback`, `/logout` and
`/login/cuenta/logout-token`. Pure move -- same paths, same bodies.

`_cuenta_down_page` and `_CUENTA_DOWN_TEMPLATE` move here too, not left in web.py: they have
exactly one caller, `login_callback`'s `CuentaUnavailable` branch, and a router module may never
import body.web -- so the only place they can live, once their caller moves, is here.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import cuenta, db
from ..i18n import DEFAULT as DEFAULT_LANG
from ..i18n import T
from . import comun

router = APIRouter()

_CUENTA_DOWN_TEMPLATE = comun.env.from_string(
    '{% extends "base.html" %}{% block main %}'
    '<div class="top"><div><h1>{{ t("app.name") }}</h1></div></div>'
    '<div class="card"><p class="read">{{ t("cuenta.down") }}</p></div>'
    "{% endblock %}"
)


def _cuenta_down_page() -> HTMLResponse:
    return HTMLResponse(_CUENTA_DOWN_TEMPLATE.render(t=T(DEFAULT_LANG), tab=None))


@router.get("/login")
@router.get("/login/cuenta/start")
def login_start(request: Request):
    if not cuenta.hosted():
        raise HTTPException(404)
    return_to = cuenta.safe_return_to(request.query_params.get("return_to"))
    prompt = request.query_params.get("prompt")
    prompt = prompt if prompt in ("none", "login") else None
    max_age = 600 if request.query_params.get("max_age") == "600" else None
    conn = db.connect()
    try:
        db.init(conn)
        url, flujo_id = cuenta.start_login(conn, return_to, prompt=prompt, max_age=max_age)
    finally:
        conn.close()
    resp = RedirectResponse(url, status_code=303)
    cuenta.set_flujo_cookie(resp, flujo_id)
    return resp


@router.get("/login/cuenta/callback")
def login_callback(request: Request):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn = db.connect()
    try:
        db.init(conn)
        try:
            result = cuenta.handle_callback(conn, request.cookies.get(cuenta.FLUJO_COOKIE),
                                            dict(request.query_params))
        except cuenta.FlowRefused:
            return HTMLResponse(comun.env.get_template("acceso_rechazado.html").render(t=T(DEFAULT_LANG)),
                                status_code=400)
        except cuenta.CuentaUnavailable:
            return _cuenta_down_page()
        if result.kind == "reauth":
            return RedirectResponse(f"/login?return_to={quote(result.return_to or '/', safe='')}",
                                    status_code=303)
        if result.kind == "sin_acceso":
            return comun._render("sin_acceso.html", t=T(DEFAULT_LANG),
                           switcher_url=cuenta.SWITCHER_URL, logout_url=cuenta.logout_url(None))
        resp = RedirectResponse(result.return_to or "/", status_code=303)
        cuenta.set_session_cookie(resp, result.session_token)
        cuenta.clear_flujo_cookie(resp)
        return resp
    finally:
        conn.close()


@router.post("/logout")
def logout(request: Request):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn = db.connect()
    try:
        db.init(conn)
        sid = cuenta.end_session(conn, request.cookies.get(cuenta.SESSION_COOKIE))
    finally:
        conn.close()
    resp = RedirectResponse(cuenta.logout_url(sid), status_code=303)
    cuenta.clear_session_cookie(resp)
    return resp


_BACKCHANNEL_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-forwarded-host", "x-real-ip")


@router.post("/login/cuenta/logout-token")
async def backchannel_logout(request: Request):
    if not cuenta.hosted():
        raise HTTPException(404)
    host = request.client.host if request.client else None
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(403)
    if any(h in request.headers for h in _BACKCHANNEL_PROXY_HEADERS):
        # Behind Caddy, a proxied request also arrives from 127.0.0.1 -- the client-address
        # check alone is not enough. Caddy's reverse_proxy adds X-Forwarded-For.
        raise HTTPException(404)
    f = await comun._form(request)
    conn = db.connect()
    try:
        db.init(conn)
        ok = cuenta.handle_backchannel_logout(conn, f.get("logout_token", ""))
    finally:
        conn.close()
    return Response(status_code=200 if ok else 400)
