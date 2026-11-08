"""Body web app. Local only: http://127.0.0.1:8770 (add --lan to open it on the phone at home).

    python -m body.web
    python -m body.web --lan

Screens follow docs/DESIGN.md. Every word comes from body/i18n (es default, en full version).
Health values never go into URLs or logs (access log is off; URLs carry only dates).
"""
from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import cuenta, entorno, garmin_cuenta, listen
from .identity import ConsentRequired, DispositivoNoAutorizado, DispositivoSinAlcance, LoginRequired
from .request_log import RequestLogMiddleware
from .seguridad import SeguridadMiddleware
from .rutas import (ajustes, asistente, ayuda, comun, dispositivos, garmin, historial, hoy, metas,
                     privacidad as privacidad_rutas, pwa, sesion, tendencias, turnos)

HERE = Path(__file__).parent


@asynccontextmanager
async def _lifespan(app: FastAPI):
    cuenta.ensure_configured()
    garmin_cuenta.ensure_configured()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


class _CaminoActualMiddleware:
    """Sets comun._RUTA_ACTUAL from the raw ASGI path before routing, for base.html's one
    in-context "¿Cómo se hace?" helper (tutoriales-v1 section 5.2, decision 3) -- comun owns the
    contextvar itself (BODY-WEB-ESTRUCTURA-1-M), since the lambda that reads it lives there."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = comun._RUTA_ACTUAL.set(scope.get("path", ""))
        try:
            await self.app(scope, receive, send)
        finally:
            comun._RUTA_ACTUAL.reset(token)


_FUENTES_DIR = (HERE / "static" / "fonts").resolve()


class _StaticConCache(StaticFiles):
    """Static files already carry a content hash in their own URL (comun.static_url's `?v=`), so a
    request whose `v` matches comun.STATIC_HASH right now can be cached forever -- that URL can
    only ever point at these exact bytes, and the day the file changes the hash does too, giving
    the browser a new URL to fetch. Any other request (no `v`, a stale one, or a foreign one typed
    by hand) falls back to the no-cache revalidation the mount already does with its ETag/
    Last-Modified pair, instead of risking a stale asset on a permanent cache. Overriding
    `file_response` alone covers both outcomes: it runs before the mount decides between the full
    FileResponse and a 304 NotModifiedResponse, and NotModifiedResponse already carries
    Cache-Control through from the headers set here (starlette.staticfiles.NotModifiedResponse.
    NOT_MODIFIED_HEADERS).

    BODY-FUENTE-CACHE-1: a file under /static/fonts/ gets the forever-cache with or without `v`.
    The self-hosted font (BODY-FUENTES-PROPIAS-1) has no `v` to carry -- it is referenced from
    body.css's @font-face `src:url(...)`, a static file, not a template, and base.html's preload
    must keep that same literal URL or the preload is wasted -- but it is immutable anyway: every
    file under body/static/fonts/ has its sha256 pinned in tests/test_fuentes_propias.py, so an
    in-place change already fails the gate, and a new font version ships under a new path with its
    own pinned hash."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        scope = args[2] if len(args) > 2 else kwargs["scope"]
        full_path = args[0] if args else kwargs["full_path"]
        v = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v", [None])[0]
        inmutable = v == comun.STATIC_HASH or Path(full_path).resolve().is_relative_to(_FUENTES_DIR)
        response.headers["cache-control"] = (
            "public, max-age=31536000, immutable" if inmutable else "no-cache"
        )
        return response


app.add_middleware(_CaminoActualMiddleware)
# GZipMiddleware sits right next to the app (outside _CaminoActualMiddleware, inside
# SeguridadMiddleware): it compresses the actual bytes the routes and the /static mount produce,
# and whatever it adds (Content-Encoding, Vary, a recomputed Content-Length) is just more headers
# by the time SeguridadMiddleware's send wrapper appends the fixed security-header set on the way
# out (BODY-RENDIMIENTO-3) -- so every response keeps those headers whether or not it was
# compressed. Starlette's own exclude list already carries `text/event-stream` (the assistant's
# streamed answer, body/rutas/asistente.py's StreamingResponse) and `font/woff2` (already
# compressed), so neither is buffered or recompressed; GZipResponder forwards an excluded
# content-type's chunks one-for-one, immediately, same as an uncompressed response would.
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SeguridadMiddleware)
# Added last, so outermost: a SeguridadMiddleware refusal still reaches the request log
# (BODY-SEGURIDAD-1-M -- a cross-site attempt is off the normal flow and must be visible).
app.add_middleware(RequestLogMiddleware)
# Python's mimetypes has no .woff2 entry (BODY-FUENTES-PROPIAS-1: measured
# `mimetypes.guess_type("x.woff2")` == `(None, None)`), which would serve the self-hosted font as
# a guessed type instead of `font/woff2`.
mimetypes.add_type("font/woff2", ".woff2")
app.mount("/static", _StaticConCache(directory=HERE / "static"), name="static")
app.include_router(pwa.router)
app.include_router(ayuda.router)
app.include_router(privacidad_rutas.router)
app.include_router(dispositivos.router)
app.include_router(ajustes.router)
app.include_router(asistente.router)
app.include_router(metas.router)
app.include_router(turnos.router)
app.include_router(hoy.router)
app.include_router(tendencias.router)
app.include_router(historial.router)
app.include_router(sesion.router)
app.include_router(garmin.router)


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired) -> RedirectResponse:
    qs = f"return_to={quote(exc.return_to, safe='')}"
    if exc.prompt:
        qs += f"&prompt={exc.prompt}"
    return RedirectResponse(f"/login?{qs}", status_code=303)


@app.exception_handler(ConsentRequired)
def _consent_required(request: Request, exc: ConsentRequired) -> RedirectResponse:
    return RedirectResponse(
        f"/privacidad/consentimiento?return_to={quote(exc.return_to, safe='')}", status_code=303
    )


@app.exception_handler(DispositivoNoAutorizado)
def _dispositivo_no_autorizado(request: Request, exc: DispositivoNoAutorizado) -> JSONResponse:
    return JSONResponse({"error": "no_autorizado"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


@app.exception_handler(DispositivoSinAlcance)
def _dispositivo_sin_alcance(request: Request, exc: DispositivoSinAlcance) -> JSONResponse:
    return JSONResponse({"error": "alcance"}, status_code=403)


def _ensure_streams() -> None:
    """pythonw has no console, so sys.stdout/sys.stderr are None -- give uvicorn's logging a stream to write to."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def main() -> None:
    _, ignorados = entorno.cargar()
    _ensure_streams()
    import uvicorn

    if ignorados > 0:
        print(f".env: {ignorados} line(s) ignored -- only BODY_AGENTE_* names are read from this file.")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    request_logger = logging.getLogger("body.request")
    request_logger.setLevel(logging.INFO)
    request_logger.addHandler(handler)
    request_logger.propagate = False

    p = argparse.ArgumentParser(prog="python -m body.web")
    p.add_argument("--lan", action="store_true", help="listen on the home network (for the phone)")
    p.add_argument("--port", type=int, default=8770)
    a = p.parse_args()
    print(listen.describe(a.lan, a.port))
    uvicorn.run(app, host=listen.listen_host(a.lan), port=a.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
