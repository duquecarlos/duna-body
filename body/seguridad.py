"""Same-origin check for unsafe methods, and the fixed security-header set on every response
(BODY-SEGURIDAD-1), as one ASGI middleware -- body/web.py only adds it (BODY-WEB-ESTRUCTURA-PLAN-1
keeps web.py the assembler; this is where the behaviour lives).

Origin rule, for POST/PUT/PATCH/DELETE only: a request with neither an Origin nor a Referer
header is never a browser (the PC client with its device key, a server-to-server call such as
/login/cuenta/logout-token, curl, a test) -- the session cookie is not ambient there, so it is
allowed through unchecked. `Origin: null` is always refused: it is what a browser sends for a
same-origin POST too, once a page's referrer policy is `no-referrer` (the Fetch standard strips
Origin to "null" whenever Referrer-Policy says so) -- which is why base.html and the three
standalone pages that used to say `no-referrer` now say `same-origin` instead. Anything else is
allowed only when its host[:port] matches this request's own Host header, or the host of the
hosted deployment's public URL -- HOST, never scheme: behind Caddy (deploy/host/Caddyfile.body,
`reverse_proxy 127.0.0.1:8770`) request.url.scheme can read http while a browser's Origin reads
https, but Caddy passes the original Host through unchanged either way.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import HTMLResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import cuenta, i18n
from .i18n import T
from .rutas import comun

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Content-Security-Policy (BODY-SEGURIDAD-2), built from parts so a test can read each one.
# style-src keeps 'unsafe-inline': 22 templates carry style="..." attributes, and server-built SVG
# (trends/charts) does too -- that gap is recorded in docs/DESIGN.md and the ledger, not silently
# dropped. form-action is 'self' plus Cuenta's issuer origin, and only when hosted: a Body form's
# POST (/logout, /privacidad/consentimiento, /privacidad/borrar) can 303-redirect to Cuenta's
# /oauth/logout, built by cuenta.logout_url from cuenta.issuer() -- Chrome applies form-action to
# the redirect that follows a form submission, not only to the form's own action= attribute.
_CSP_FIXED_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "manifest-src 'self'",
    "worker-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _form_action_sources() -> list[str]:
    sources = ["'self'"]
    if cuenta.hosted():
        sources.append(_origin(cuenta.issuer()))
    return sources


def csp_directives() -> list[str]:
    """The policy's directives, in order -- a plain function so a test can read the parts instead
    of re-parsing a long header literal."""
    return [*_CSP_FIXED_DIRECTIVES, "form-action " + " ".join(_form_action_sources())]


def content_security_policy() -> str:
    return "; ".join(csp_directives())


def _security_headers() -> tuple[tuple[bytes, bytes], ...]:
    return (
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"x-frame-options", b"DENY"),
        (b"content-security-policy", content_security_policy().encode("latin-1")),
        (b"cross-origin-opener-policy", b"same-origin"),
    )


def _normalize_host(host_port: str) -> str:
    host_port = host_port.strip().lower()
    if host_port.endswith(":80") or host_port.endswith(":443"):
        host_port = host_port.rsplit(":", 1)[0]
    return host_port


def _host_from_url(value: str) -> str | None:
    """The normalized host[:port] `value` (an Origin or a Referer) names, or None when it has no
    parseable host at all -- never treated as a match."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return _normalize_host(parts.netloc)


def _allowed_hosts(headers: dict[bytes, bytes]) -> set[str]:
    allowed = set()
    host = headers.get(b"host")
    if host:
        allowed.add(_normalize_host(host.decode("latin-1")))
    if cuenta.hosted():
        allowed.add(_normalize_host(urlsplit(cuenta.public_url()).netloc))
    return allowed


def same_origin(scope: Scope) -> bool:
    headers = dict(scope["headers"])
    origin = headers.get(b"origin")
    if origin is None:
        referer = headers.get(b"referer")
        if referer is None:
            return True
        host = _host_from_url(referer.decode("latin-1"))
        return host is not None and host in _allowed_hosts(headers)
    value = origin.decode("latin-1")
    if value == "null":
        return False
    host = _host_from_url(value)
    return host is not None and host in _allowed_hosts(headers)


def _origen_rechazado(scope: Scope) -> HTMLResponse:
    headers = dict(scope["headers"])
    accept_language = headers.get(b"accept-language")
    lang = i18n.pick(accept_language.decode("latin-1") if accept_language is not None else None)
    html = comun.env.get_template("origen_rechazado.html").render(t=T(lang))
    return HTMLResponse(html, status_code=403)


def _send_with_security_headers(send: Send) -> Send:
    async def wrapped(message: dict) -> None:
        if message["type"] == "http.response.start":
            headers = message.setdefault("headers", [])
            present = {name.lower() for name, _ in headers}
            headers.extend((name, value) for name, value in _security_headers() if name not in present)
        await send(message)

    return wrapped


class SeguridadMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        send = _send_with_security_headers(send)
        if scope["method"] in UNSAFE_METHODS and not same_origin(scope):
            await _origen_rechazado(scope)(scope, receive, send)
            return
        await self.app(scope, receive, send)
