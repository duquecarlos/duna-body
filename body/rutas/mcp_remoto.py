"""The hosted MCP door (BODY-MCP-REMOTO-SERVIDOR-1, surface BODY-MUDANZA-PC-A-SERVIDOR-1): the
Personal desk's own MCP client, once it moves off the PC (BODY-MCP-REMOTO-CLIENTE-1, a later
slice), reaches Carlos's health data on body.duna.solutions through these two routes instead of
body/mcp.py's stdio pipe. Authenticated ONLY by a device key carrying the `asistente:mcp` scope
(body/identity.py's `person_from_device`, hosted-only -- 404 in local mode, same as every other
device route) -- never by the signed-in Cuenta session these other screens use.

Both routes call body/mcp.py's own shared core (`_lista_herramientas`, `_llamar_herramienta`)
UNCHANGED -- the local stdio door's own behaviour and tests do not change (ruling 2: refactor,
don't copy). The one thing that differs between the two doors is the SHAPE a refusal takes:
local wraps a `RehusoMCP` into its existing localized JSON-RPC text; this module reads `codigo`/
`campos` off the SAME exception instead, so a refusal never crosses the wire as pre-localised
text -- the PC's own future client renders it with mcp.py's own `_mensaje_*` functions, reading
the person's language setting locally. A plain `herramientas.ToolError` with no such fields (an
unknown tool, a bad parameter, a nivel too low -- the tool registry's own validation, meant for a
model to read and retry, never localized in the first place) collapses to one generic code,
`herramienta_rechazada`.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import db, errores, identity, mcp
from ..agente import conversacion as agente_conversacion
from ..agente import herramientas
from . import comun

router = APIRouter()
logger = logging.getLogger("body.mcp_remoto")

MAX_BODY_BYTES = 64 * 1024


def _sin_cache(data: dict, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})


@router.get("/api/asistente/mcp/herramientas")
@comun._cierra_conexion
def mcp_remoto_herramientas(person_id: int = Depends(identity.person_from_device("asistente:mcp"))):
    conn, _, _ = comun._ctx(person_id)
    sesion = mcp._SesionRemota(conn, person_id, None)
    nivel = mcp._nivel(sesion)
    return _sin_cache({"herramientas": mcp._lista_herramientas(sesion), "nivel": nivel})


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
    if not _conversacion_valida(conn, person_id, conversacion_id):
        return _sin_cache({"error": "conversacion_ajena"})

    sesion = mcp._SesionRemota(conn, person_id, conversacion_id)
    try:
        tipo, salida, aviso_reloj = mcp._llamar_herramienta(sesion, nombre, argumentos)
    except mcp.RehusoMCP as e:
        return _sin_cache({"error": e.codigo, **e.campos})
    except herramientas.ToolError:
        return _sin_cache({"error": "herramienta_rechazada"})
    except Exception as e:
        conn.rollback()
        nombre_str = nombre if isinstance(nombre, str) else str(nombre)
        logger.info("mcp remoto error interno: %s herramienta=%r", errores.texto_seguro(e), nombre_str)
        return _sin_cache({"error": "error_interno"})
    return _sin_cache({"tipo": tipo, "salida": salida, "aviso_reloj": aviso_reloj,
                        "conversacion_id": sesion.conversacion_id})
