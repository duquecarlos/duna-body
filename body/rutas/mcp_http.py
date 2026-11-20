"""POST /mcp (BODY-MCP-HTTP-1): the real remote MCP endpoint -- https://body.duna.solutions/mcp --
that ANY MCP client (a Claude session, not only Body's own PC) adds by URL plus a Cuenta Duna
assistant key, with no Body code running on the client's own machine at all. Carlos, relayed by
the Personal desk 2026-11-16: "I want ... a real connection to all my production apps ... you'll
be the brain of the smart home ... you should live with the apps."

The "Streamable HTTP" transport (MCP spec 2025-03-26) in its STATELESS form: one POST carries one
JSON-RPC request or a batch, answered with one `application/json` body -- no SSE stream, no
session id. `GET /mcp` answers 405 with `Allow: POST` -- Starlette's own default, since this
router registers POST only.

Auth is body/llave_asistente.py's Cuenta introspection -- never body/dispositivo.py's device-key
table, which is the EXISTING hosted door's (body/rutas/mcp_remoto.py) own auth and stays unchanged.
Once a key resolves to a Body person and a `permiso`, every method reuses mcp_remoto.py's own
shared core (ruling 2: one dispatch, two doors): `puede_escribir` is this key's own
`permiso == "escritura"`, and `puede_aplicar` is the SAME boolean -- escritura stands in for the
person's own `aplicar_mcp` setting here (that setting never gates this endpoint at all; a write
the escritura permission allows applies directly, with no separate "tap Aplicar" step this caller
cannot take anyway).

Docs: docs/contracts/mcp-http-v1.md.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import cuenta, db, errores, llave_asistente, mcp
from ..i18n import DEFAULT as _IDIOMA_DEFECTO
from ..i18n import T
from ..i18n import pick as _idioma_de_accept_language
from . import comun, mcp_remoto

router = APIRouter()
logger = logging.getLogger("body.mcp_http")

SERVER_INFO = {"name": "body", "version": "1"}

_PARSE_ERROR = {"code": -32700, "message": "Parse error"}
_INVALID_REQUEST = {"code": -32600, "message": "Invalid Request"}
_METHOD_NOT_FOUND = {"code": -32601, "message": "Method not found"}
_CUENTA_NO_DISPONIBLE_CODE = -32001  # implementation-defined server error (JSON-RPC -32000..-32099)
_REINTENTO_DEFECTO_SEGUNDOS = "60"  # used only when Cuenta's own answer carried no Retry-After

# BODY-MCP-INICIO-SIN-LLAVE-1 ruling 1 (Main's hub item hit_01m7vs9tpsfvzwt372r246srg1, applied to
# every Duna product): these two methods, plus every notifications/* message, carry nothing
# personal -- only server name, version, protocol version, capabilities and an empty/no result
# (ruling 2's own census) -- so they answer even when Cuenta is slow, cold or unreachable, instead
# of waiting on llave_asistente.verificar's own introspection (up to _HTTP_TIMEOUT, 5 s) before the
# body is even parsed. ANY other method (tools/list, tools/call, anything unknown) still goes
# through full verification below, cache included -- tests/test_mcp_http.py's AllowListCensusTest
# fails the moment a data-returning method joins this set.
METODOS_SIN_LLAVE = frozenset({"initialize", "ping"})

# Pre-auth and introspection-fault sentences carry no known person language yet (an inactive or
# unresolved key, or Cuenta itself unreachable) -- both languages together, never a guess at
# which one to show. The one tool-level sentence this endpoint invents on its own (NOT an existing
# body/i18n key: the lectura/escritura split is new to this endpoint) is instead picked by the
# ALREADY-KNOWN person language, same as every other tool-call message below it.
_MENSAJE_401 = "Llave de asistente inválida o ausente. / Missing or invalid assistant key."
_MENSAJE_401_SIN_PERSONA = (
    "Esta llave es válida, pero todavía no has entrado a Body con esa cuenta: entra una vez a "
    "Body con Cuenta Duna para vincularla. / This key is valid, but you haven't signed in to "
    "Body with that account yet: sign in to Body once with Cuenta Duna to link it."
)
_MENSAJE_SOLO_LECTURA = {
    "es": "Esta llave es de solo lectura: no puede usar herramientas de escritura.",
    "en": "This key is read-only: it can't use write tools.",
}


def _sin_cache(data, *, status_code: int = 200, headers: dict | None = None) -> JSONResponse:
    h = {"Cache-Control": "no-store"}
    if headers:
        h.update(headers)
    return JSONResponse(data, status_code=status_code, headers=h)


def _401(mensaje: str) -> JSONResponse:
    return _sin_cache({"error": mensaje}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def _503(retry_after: str | None, idioma: str) -> JSONResponse:
    """Ruling 3: besides the transport-level 503 and its Retry-After (unchanged), the body itself
    is a JSON-RPC error -- never a bare `{"error": ...}` -- so a client reads WHY, not only a
    status code. No person is resolved yet (this is the introspection call itself failing), so
    `idioma` is read off the caller's own Accept-Language header, or the default."""
    n = retry_after or _REINTENTO_DEFECTO_SEGUNDOS
    mensaje = T(idioma)("asistente.mcp.cuenta_no_disponible", n=n)
    headers = {"Retry-After": retry_after} if retry_after else None
    cuerpo = _responder(None, error={"code": _CUENTA_NO_DISPONIBLE_CODE, "message": mensaje})
    return _sin_cache(cuerpo, status_code=503, headers=headers)


def _responder(id_, *, resultado=None, error=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = resultado
    return msg


def _mensaje_error(data: dict, nombre_str: str, idioma: str) -> str:
    """Ruling 2: every refusal becomes an MCP tool result (`isError: true`), never a transport
    error -- `solo_lectura` (new to this endpoint) is picked straight by `idioma`; every other
    code is one mcp.py's own `_error_remoto_a_rehuso` already maps to the SAME localized text the
    local stdio door and the Personal desk's own PC client already show for it. That function
    takes a `_SesionCliente` (its signature stays untouched -- a test outside this slice's
    touches calls it directly), so this endpoint builds a bare, throwaway one just to carry
    `idioma`: it holds no connection and nothing here ever reads anything else off it."""
    if data.get("error") == "solo_lectura":
        return _MENSAJE_SOLO_LECTURA.get(idioma, _MENSAJE_SOLO_LECTURA["es"])
    sesion_cliente = mcp._SesionCliente()
    sesion_cliente.idioma = idioma
    rehuso = mcp._error_remoto_a_rehuso(sesion_cliente, nombre_str, data)
    return errores.texto_propio(rehuso)


def _contenido_llamada(tipo: str, salida: dict, aviso_reloj: bool, idioma: str) -> list[dict]:
    """The exact same three location/undo lines mcp.py's own `_procesar_mensaje` inserts for a
    stdio caller (ruling 2: identical behaviour) -- rebuilt here because this endpoint has no
    `_Sesion`/`_SesionCliente` of its own to call that function with, only a plain `tipo`/`salida`
    pair from mcp_remoto.llamar_herramienta's shared dispatch."""
    t = T(idioma)
    contenido = [{"type": "text", "text": json.dumps(salida, ensure_ascii=False)}]
    if tipo == "propuesta":
        contenido.insert(0, {"type": "text",
                              "text": t("asistente.mcp.resultado_ubicacion", pantalla=t("nav.today"))})
        if aviso_reloj:
            contenido.insert(0, {"type": "text", "text": t("asistente.mcp.entreno_reloj_fallo")})
    elif tipo == "aplicacion":
        contenido.insert(0, {"type": "text",
                              "text": t("asistente.mcp.aplicar_resultado", pantalla=t("nav.today"))})
    elif tipo == "descarte":
        contenido.insert(0, {"type": "text", "text": t("asistente.mcp.descartar_resultado")})
    return contenido


def _metodo_sin_llave(item) -> bool:
    if not isinstance(item, dict):
        return False
    metodo = item.get("method")
    return isinstance(metodo, str) and (metodo in METODOS_SIN_LLAVE or metodo.startswith("notifications/"))


def _todos_sin_llave(cuerpo) -> bool:
    """True only when EVERY message in `cuerpo` (a lone object or every item of a batch) is in
    the allow-list -- a batch mixing `initialize` with `tools/list` (or anything else) answers
    False here and goes through full verification unchanged, cache included."""
    if isinstance(cuerpo, list):
        return bool(cuerpo) and all(_metodo_sin_llave(item) for item in cuerpo)
    return _metodo_sin_llave(cuerpo)


def _procesar_uno(item, conn, person_id: int, idioma: str, *, puede_aplicar: bool,
                   puede_escribir: bool) -> dict | None:
    if not isinstance(item, dict) or item.get("jsonrpc") != "2.0" or not isinstance(item.get("method"), str):
        id_ = item.get("id") if isinstance(item, dict) else None
        return _responder(id_, error=_INVALID_REQUEST)
    id_ = item.get("id")
    es_notificacion = "id" not in item
    metodo = item["method"]
    params = item.get("params")
    params = params if isinstance(params, dict) else {}

    if metodo == "initialize":
        # Ruling 1: echo the client's protocolVersion when it is one body/mcp.py already accepts,
        # else the newest body/mcp.py supports -- mcp.py knows exactly one, so both branches
        # answer the same way today; written as a real comparison so a future second version
        # keeps this correct without another look at this line.
        version = params.get("protocolVersion")
        if version != mcp.PROTOCOL_VERSION:
            version = mcp.PROTOCOL_VERSION
        return _responder(id_, resultado={
            "protocolVersion": version, "serverInfo": SERVER_INFO, "capabilities": {"tools": {}},
        })
    if metodo == "notifications/initialized":
        return None
    if metodo == "ping":
        return _responder(id_, resultado={})
    if metodo == "tools/list":
        data = mcp_remoto.listar_herramientas(conn, person_id, idioma, puede_aplicar=puede_aplicar,
                                               puede_escribir=puede_escribir)
        return _responder(id_, resultado={"tools": data["herramientas"]})
    if metodo == "tools/call":
        nombre = params.get("name")
        argumentos = params.get("arguments")
        argumentos = argumentos if isinstance(argumentos, dict) else {}
        nombre_str = nombre if isinstance(nombre, str) else str(nombre)
        # Stateless transport: no session id, so each call starts its own lazily-created
        # conversation, same as a device-door client's own first call with no conversacion_id.
        data = mcp_remoto.llamar_herramienta(conn, person_id, nombre, argumentos, None,
                                              puede_aplicar=puede_aplicar, puede_escribir=puede_escribir,
                                              auto_aplicar=puede_escribir)
        if "error" in data:
            texto = _mensaje_error(data, nombre_str, idioma)
            return _responder(id_, resultado={"content": [{"type": "text", "text": texto}], "isError": True})
        contenido = _contenido_llamada(data["tipo"], data["salida"], data["aviso_reloj"], idioma)
        return _responder(id_, resultado={"content": contenido})
    if es_notificacion:
        return None
    return _responder(id_, error=_METHOD_NOT_FOUND)


@router.post("/mcp")
@comun._cierra_conexion_async
async def mcp_http(request: Request):
    if not cuenta.hosted():
        return JSONResponse({"error": "no_disponible"}, status_code=404)
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return _401(_MENSAJE_401)
    key = auth[len("Bearer "):]

    # Ruling 1: the body is parsed BEFORE any decision about introspecting the key -- never after,
    # the way it was before this slice. A parse failure can never be classified as "every message
    # is in the allow-list" (there IS no message), so it always falls through to full verification
    # below, same precedence as before: an auth failure (401/503) still wins over a parse error.
    try:
        cuerpo = json.loads((await request.body()).decode("utf-8"))
        parse_error = False
    except (UnicodeDecodeError, ValueError):
        cuerpo = None
        parse_error = True

    if not parse_error and _todos_sin_llave(cuerpo):
        # Every message is initialize/ping/notifications/* -- answer at once, with NO call to
        # llave_asistente.verificar (no network reach for Cuenta at all). The Bearer header was
        # still required above; it is just never introspected for these methods. None of these
        # branches in `_procesar_uno` reads conn/person_id/idioma (ruling 2's own census: this
        # endpoint's `initialize` already carries no person-specific text), so the placeholders
        # below are never seen by them.
        if isinstance(cuerpo, list):
            respuestas = [r for r in (
                _procesar_uno(item, None, None, _IDIOMA_DEFECTO, puede_aplicar=False, puede_escribir=False)
                for item in cuerpo) if r is not None]
            if not respuestas:
                return Response(status_code=202)
            return _sin_cache(respuestas)
        respuesta = _procesar_uno(cuerpo, None, None, _IDIOMA_DEFECTO, puede_aplicar=False, puede_escribir=False)
        if respuesta is None:
            return Response(status_code=202)
        return _sin_cache(respuesta)

    # BODY-WEB-ESTRUCTURA-1/BODY-CONEXIONES-CERRADAS-2: a key resolves to a person only AFTER
    # Cuenta answers -- there is no person_id yet for comun._ctx to scope by, the same reason
    # body/rutas/sesion.py's own routes open a plain connection of their own instead (its module
    # docstring). This one closes itself right here; the SEPARATE connection comun._ctx opens
    # below, once person_id is known, is the one this route's own @comun._cierra_conexion_async
    # decorator closes.
    conn_auth = db.connect()
    try:
        db.ensure_ready(conn_auth)
        try:
            autorizacion = llave_asistente.verificar(conn_auth, key)
        except llave_asistente.CuentaNoDisponible as e:
            idioma_503 = _idioma_de_accept_language(request.headers.get("accept-language"))
            return _503(e.retry_after, idioma_503)
        except llave_asistente.PersonaSinBody:
            return _401(_MENSAJE_401_SIN_PERSONA)
        except llave_asistente.LlaveInvalida:
            return _401(_MENSAJE_401)
    finally:
        conn_auth.close()

    person_id = autorizacion.person_id
    puede_escribir = autorizacion.permiso == "escritura"
    puede_aplicar = puede_escribir
    conn, persona, _ = comun._ctx(person_id)
    idioma = persona.get("language", "es")

    if parse_error:
        return _sin_cache(_responder(None, error=_PARSE_ERROR))

    if isinstance(cuerpo, list):
        if not cuerpo:
            return _sin_cache(_responder(None, error=_INVALID_REQUEST))
        respuestas = [r for r in (
            _procesar_uno(item, conn, person_id, idioma, puede_aplicar=puede_aplicar,
                          puede_escribir=puede_escribir) for item in cuerpo) if r is not None]
        if not respuestas:
            return Response(status_code=202)
        return _sin_cache(respuestas)
    if isinstance(cuerpo, dict):
        respuesta = _procesar_uno(cuerpo, conn, person_id, idioma, puede_aplicar=puede_aplicar,
                                   puede_escribir=puede_escribir)
        if respuesta is None:
            return Response(status_code=202)
        return _sin_cache(respuesta)
    return _sin_cache(_responder(None, error=_INVALID_REQUEST))
