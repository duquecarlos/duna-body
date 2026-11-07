"""Shared PC-side client for the hosted Body (BODY-PC-CLIENTE-1): the device-key API that
BODY-DISPOSITIVO-1 built, called over `urllib.request` (stdlib only, TLS verified by default --
httpx stays a dev-only test dependency). The link file holds the key; nothing here ever prints it,
puts it in an exception message, or passes it through argv.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import config

ENLACE_NAME = "dispositivo.json"
TIMEOUT_S = 30


class EnlaceFaltante(Exception):
    """No dispositivo.json yet. The message never carries a key -- it only names the fix."""


class UrlRefusada(Exception):
    """The URL is neither https:// nor http:// to localhost/127.0.0.1."""


def _enlace_path() -> Path:
    return config.BODY_HOME / ENLACE_NAME


def _url_permitida(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def escribir_enlace(url: str, clave: str) -> None:
    if not _url_permitida(url):
        raise UrlRefusada("Esa URL no está permitida (debe ser https://, o http:// a localhost).")
    config.ensure_home()
    path = _enlace_path()
    path.write_text(json.dumps({"url": url, "clave": clave}), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def borrar_enlace() -> None:
    path = _enlace_path()
    if path.exists():
        path.unlink()


def leer_enlace() -> dict:
    path = _enlace_path()
    if not path.exists():
        raise EnlaceFaltante("Este PC no está conectado. Corre: python -m body.conectar_pc")
    return json.loads(path.read_text(encoding="utf-8"))


def _abrir(req: urllib.request.Request):
    """The one call that touches the network. Tests patch this name with a fake, never the socket."""
    return urllib.request.urlopen(req, timeout=TIMEOUT_S)


def llamar(method: str, ruta: str, *, body: bytes | None = None,
           headers: dict | None = None) -> tuple[int, dict | None]:
    """Calls <url><ruta> with the saved key as a Bearer token. Returns (status, json-or-None) for
    both a success and an HTTP error status -- callers switch on the status code. A connection
    failure (DNS, refused, timeout) raises urllib.error.URLError and is never caught here: callers
    report it by exception CLASS only, never by its message, which can carry the request URL."""
    enlace = leer_enlace()
    url = enlace["url"]
    if not _url_permitida(url):
        raise UrlRefusada("El enlace guardado tiene una URL no permitida.")
    req_headers = {"Authorization": f"Bearer {enlace['clave']}"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url.rstrip("/") + ruta, data=body, headers=req_headers, method=method)
    try:
        with _abrir(req) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    data = None
    if raw:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
    return status, data
