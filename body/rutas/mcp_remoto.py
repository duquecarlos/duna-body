"""The hosted MCP door (BODY-MCP-REMOTO-SERVIDOR-1, surface BODY-MUDANZA-PC-A-SERVIDOR-1): the
Personal desk's own MCP client (BODY-MCP-REMOTO-CLIENTE-1, body/mcp.py's `_SesionCliente`) reaches
Carlos's health data on body.duna.solutions through these two routes instead of body/mcp.py's old
stdio pipe to the local database. Authenticated ONLY by a device key carrying the `asistente:mcp`
scope (body/identity.py's `person_from_device`, hosted-only -- 404 in local mode, same as every
other device route) -- never by the signed-in Cuenta session these other screens use.

Both routes call body/mcp.py's own shared core (`_lista_herramientas`, `_llamar_herramienta`)
UNCHANGED -- the local stdio door's own behaviour and tests do not change (ruling 2: refactor,
don't copy). The one thing that differs between the two doors is the SHAPE a refusal takes:
local wraps a `RehusoMCP` into its existing localized JSON-RPC text; this module reads `codigo`/
`campos` off the SAME exception instead, so a refusal never crosses the wire as pre-localised
text -- the PC's own client (`_SesionCliente`) renders it with mcp.py's own i18n keys, reading the
person's language off this module's own `"idioma"` field (BODY-MCP-REMOTO-CLIENTE-1 ruling 3). A
plain `herramientas.ToolError` with no such fields (an unknown tool, a bad parameter, a nivel too
low -- the tool registry's own validation, meant for a model to read and retry, never localized in
the first place) collapses to the generic code `herramienta_rechazada`, carrying the exception's
own unlocalized `mensaje` (`errores.texto_propio(e)`) -- the exact same text the local door already
shows verbatim for the same error, never a new localized sentence.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import db, errores, identity, mcp
from ..agente import conversacion as agente_conversacion
from ..agente import herramientas
from ..agente import propuestas as agente_propuestas
from . import comun

router = APIRouter()
logger = logging.getLogger("body.mcp_remoto")

MAX_BODY_BYTES = 64 * 1024


def _sin_cache(data: dict, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})


def listar_herramientas(conn, person_id: int, idioma: str, *, puede_aplicar: bool,
                         puede_escribir: bool = True) -> dict:
    """The shared core behind GET /herramientas (BODY-MCP-HTTP-1 ruling 2): builds the SAME tool
    list body/mcp.py's local stdio door and this hosted door already share, now also reusable by
    body/rutas/mcp_http.py -- `puede_aplicar` decides whether `aplicar_propuesta`/
    `descartar_propuesta` are listed (this door passes its own `aplicar_mcp` setting; /mcp passes
    `permiso == "escritura"`); `puede_escribir` (new, /mcp-only -- this door always passes the
    default True, so its own behaviour does not change) additionally hides every write-type tool
    (every `propuesta` tool, plus apply/discard) for a read-only caller."""
    sesion = mcp._SesionRemota(conn, person_id, None, puede_aplicar)
    nivel = mcp._nivel(sesion)
    tools = mcp._lista_herramientas(sesion)
    if not puede_escribir:
        tools = [t for t in tools if herramientas.tipo_de(t["name"]) == "lectura"]
    return {"herramientas": tools, "nivel": nivel, "idioma": idioma}


def llamar_herramienta(conn, person_id: int, nombre, argumentos: dict, conversacion_id, *,
                        puede_aplicar: bool, puede_escribir: bool = True, auto_aplicar: bool = False,
                        origin: str = mcp.ORIGEN) -> dict:
    """The shared core behind POST /llamar (BODY-MCP-HTTP-1 ruling 2): returns the EXACT same
    shape this door already puts on the wire -- `{"tipo", "salida", "aviso_reloj",
    "conversacion_id"}` on success, `{"error": codigo, ...campos}` on a refusal -- so
    body/rutas/mcp_http.py can build its own MCP envelope from it without this door's own JSON
    response shape changing at all. `puede_escribir=False` (lectura, /mcp-only) refuses any
    write-type tool (a `propuesta` tool, or apply/discard) with the new `solo_lectura` code,
    BEFORE the shared dispatch ever runs it -- never a read tool, which stays allowed either way.

    `auto_aplicar=True` (/mcp-only, escritura: "a write applies directly") immediately applies a
    proposal this SAME call just created, through the plain `agente_propuestas.aplicar` the web
    route's own Aplicar button uses -- never through `mcp._aplicar_propuesta_mcp`'s own "is this
    caller's own proposal" check, which has nothing to verify here: it is always the proposal this
    exact call just made. This door's own default (False) keeps its existing propuesta-then-
    manual-apply flow -- ruling 3, nothing about it changes.

    `origin` (BODY-HOGAR-ESCRIBE-2 ruling 2, new: every existing call site -- this door's own
    route below, and /mcp -- keeps the default `mcp.ORIGEN` ("mcp"), unchanged) tags BOTH the
    conversation a fresh propuesta call lazily creates (`agente_conversacion.crear(...,
    origen=origin)`, done HERE rather than inside `mcp._SesionRemota.conversacion()` -- the one
    thing that module still hardcodes to 'mcp', outside this slice's `touches:` -- by pre-creating
    the conversation whenever the caller sent none and `origin` differs from it) and, on
    `auto_aplicar`, the proposal's own `aplicada_via` (`db.marcar_aplicada_via`, already general --
    `db.marcar_aplicada_via_mcp` was always a thin 'mcp'-only caller over it, so this is the one
    other call site that needed widening, and it already existed for exactly this)."""
    if not _conversacion_valida(conn, person_id, conversacion_id):
        return {"error": "conversacion_ajena"}
    nombre_str = nombre if isinstance(nombre, str) else str(nombre)
    if not puede_escribir and (nombre_str in (mcp.NOMBRE_APLICAR, mcp.NOMBRE_DESCARTAR)
                                or herramientas.tipo_de(nombre_str) == "propuesta"):
        return {"error": "solo_lectura"}
    if conversacion_id is None and origin != mcp.ORIGEN and herramientas.tipo_de(nombre_str) == "propuesta":
        conversacion_id = agente_conversacion.crear(conn, person_id, origen=origin)
    sesion = mcp._SesionRemota(conn, person_id, conversacion_id, puede_aplicar)
    try:
        tipo, salida, aviso_reloj = mcp._llamar_herramienta(sesion, nombre, argumentos)
    except mcp.RehusoMCP as e:
        return {"error": e.codigo, **e.campos}
    except herramientas.ToolError as e:
        # BODY-MCP-REMOTO-CLIENTE-1 ruling 3: the PC client renders "herramienta_rechazada" with
        # the exact same text the local stdio door already shows for a plain ToolError
        # (`errores.texto_propio(e)`) -- never localized to begin with (it is meant for a model to
        # read and retry), so sending it is not a new customer-facing sentence, only the same
        # unlocalized text the local door already displays verbatim.
        return {"error": "herramienta_rechazada", "mensaje": errores.texto_propio(e)}
    except Exception as e:
        conn.rollback()
        logger.info("mcp remoto error interno: %s herramienta=%r", errores.texto_seguro(e), nombre_str)
        return {"error": "error_interno"}
    if auto_aplicar and tipo == "propuesta":
        propuesta_id = salida.get("propuesta_id")
        if isinstance(propuesta_id, int):
            resultado = agente_propuestas.aplicar(conn, person_id, propuesta_id)
            if resultado["estado"] == "aplicada":
                db.marcar_aplicada_via(conn, propuesta_id, origin)
                salida = {**salida, "estado": resultado["estado"]}
                tipo = "aplicacion"
    return {"tipo": tipo, "salida": salida, "aviso_reloj": aviso_reloj, "conversacion_id": sesion.conversacion_id}


@router.get("/api/asistente/mcp/herramientas")
@comun._cierra_conexion
def mcp_remoto_herramientas(person_id: int = Depends(identity.person_from_device("asistente:mcp"))):
    conn, person, _ = comun._ctx(person_id)
    # BODY-MCP-REMOTO-CLIENTE-1 ruling 3: the PC client has no DB of its own to read the person's
    # interface language from -- read the same way the web reads it (comun._ctx's own `person`).
    idioma = person.get("language", "es")
    puede_aplicar = db.get_aplicar_mcp(conn, person_id)
    return _sin_cache(listar_herramientas(conn, person_id, idioma, puede_aplicar=puede_aplicar))


def _conversacion_valida(conn, person_id: int, conversacion_id) -> bool:
    """Ruling 3: a `conversacion_id` the client sent that is not an int, is not this person's own,
    or whose `origen` is not 'mcp' is refused outright -- never reaches the shared core. `None`
    (the client's first call) is always valid: the core creates one lazily."""
    if conversacion_id is None:
        return True
    if not isinstance(conversacion_id, int):
        return False
    try:
        agente_conversacion.verificar_propiedad(conn, conversacion_id, person_id)
    except agente_conversacion.ConversacionAjena:
        return False
    return agente_conversacion.origen_de(conn, conversacion_id) == mcp.ORIGEN


@router.post("/api/asistente/mcp/llamar")
@comun._cierra_conexion_async
async def mcp_remoto_llamar(request: Request,
                             person_id: int = Depends(identity.person_from_device("asistente:mcp"))):
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        return _sin_cache({"error": "cuerpo_demasiado_grande"}, status_code=413)
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_BODY_BYTES:
            return _sin_cache({"error": "cuerpo_demasiado_grande"}, status_code=413)
    try:
        cuerpo = json.loads(bytes(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _sin_cache({"error": "esquema"}, status_code=422)
    if not isinstance(cuerpo, dict):
        return _sin_cache({"error": "esquema"}, status_code=422)

    nombre = cuerpo.get("nombre")
    argumentos = cuerpo.get("argumentos")
    argumentos = argumentos if isinstance(argumentos, dict) else {}
    conversacion_id = cuerpo.get("conversacion_id")

    conn, _, _ = comun._ctx(person_id)
    puede_aplicar = db.get_aplicar_mcp(conn, person_id)
    data = llamar_herramienta(conn, person_id, nombre, argumentos, conversacion_id, puede_aplicar=puede_aplicar)
    return _sin_cache(data)
