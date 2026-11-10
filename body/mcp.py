"""A local Model Context Protocol server over stdio (BODY-MCP-LOCAL-1, ORCH-CARLOS-DELEGATES-
TIER-1-1): the SAME tool registry, at the SAME privacy level, the in-app assistant gets for the
person this PC resolves to (`db.local_person_id`, exactly as body/identity.py's own local-mode
path) -- for an outside caller on this same PC. Run with `python -m body.mcp`. Stdio only: no
socket, no network, no port; nothing here ever reaches anywhere else on its own initiative.
CONNECTING a caller to this server (adding it to that caller's own MCP config) is a separate step,
outside this module, that happens only once the person has said yes -- building this sends nothing
anywhere (ruling 7).

Every tool call goes through the SAME `herramientas.ejecutar` the in-app assistant's own loop
(body/agente/bucle.py) calls -- never a shortcut around it, so this server can never read or
propose more than that level already allows. There is no apply/undo/delete tool anywhere in
`herramientas.HERRAMIENTAS`, so none is ever listed or callable here either (ruling 3): a
`propuesta` tool only ever creates a card in Body's own assistant panel -- in ONE conversation per
MCP session, created lazily on the session's first proposal and tagged `origen='mcp'`
(body/agente/conversacion.py) -- for the person to tap Aplicar there themselves. Whether this desk
may later apply on the person's spoken yes is a separate, later decision; nothing here forecloses
or enables it.

Caps (ruling 4) mirror body/agente/bucle.py's own per-question caps (MAX_LECTURAS, MAX_RONDAS),
applied per MCP session instead of per question: an outside caller can never read or propose more
in one session than the in-app assistant could answer one question with. Every call -- a read or a
proposal, whether it succeeds or is refused -- writes one row to `uso_herramienta_externa`
(db.registrar_uso_herramienta_externa): the tool's own name, the UTC time and the caller id, never
its params or its result (ruling 5). Nothing is ever written to stdout but JSON-RPC frames; the one
line this module writes elsewhere goes to stderr, and names counts only, never a tool's params or
result (ruling 6).
"""
from __future__ import annotations

import contextlib
import json
import sys
from typing import IO

from . import db, errores
from .agente import ajustes, bucle
from .agente import conversacion as agente_conversacion
from .agente import herramientas

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "body-local", "version": "1"}
CALLER = "mcp-local"
ORIGEN = "mcp"

# Mirrors body/agente/bucle.py's own per-question caps, applied per MCP SESSION instead of per
# question (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4): bucle.py allows at most one proposal per
# round and MAX_RONDAS rounds per question, so MAX_RONDAS is the matching per-session ceiling on
# proposals here.
LIMITES_SESION = {"lecturas": bucle.MAX_LECTURAS, "propuestas": bucle.MAX_RONDAS}

_PARSE_ERROR = {"code": -32700, "message": "Parse error"}
_METHOD_NOT_FOUND = {"code": -32601, "message": "Method not found"}


class _Sesion:
    """One MCP session's own state: the connection and person it serves, the one conversation its
    proposals land in (created lazily), and this session's own read/proposal counters."""

    def __init__(self, conn, person_id: int):
        self.conn = conn
        self.person_id = person_id
        self.conversacion_id: int | None = None
        self.lecturas = 0
        self.propuestas = 0

    def conversacion(self) -> int:
        if self.conversacion_id is None:
            self.conversacion_id = agente_conversacion.crear(self.conn, self.person_id, origen=ORIGEN)
        return self.conversacion_id


def _herramienta_mcp(especificacion: dict) -> dict:
    return {"name": especificacion["nombre"], "description": especificacion["descripcion"],
            "inputSchema": especificacion["parametros"]}


def _nivel(sesion: _Sesion) -> str:
    herramientas.fijar_turnos_actual(db.trabaja_turnos_si(sesion.conn, sesion.person_id))
    return ajustes.nivel_datos(sesion.conn, sesion.person_id)


def _lista_herramientas(sesion: _Sesion) -> list[dict]:
    nivel = _nivel(sesion)
    return [_herramienta_mcp(e) for e in bucle._tool_specs(nivel)]


def _llamar_herramienta(sesion: _Sesion, nombre, argumentos: dict) -> dict:
    """Dispatches one `tools/call` -- every attempt, successful or refused, is audited first
    (ruling 5). A read goes straight to `herramientas.ejecutar`; a proposal gets this session's one
    lazily-created, 'mcp'-tagged conversation. Raises `herramientas.ToolError` (caught by the
    caller) for an unknown name, a cap reached, or anything `herramientas.ejecutar` itself refuses."""
    nombre_str = nombre if isinstance(nombre, str) else str(nombre)
    db.registrar_uso_herramienta_externa(sesion.conn, sesion.person_id, nombre_str, CALLER)
    nivel = _nivel(sesion)
    tipo = herramientas.tipo_de(nombre) if isinstance(nombre, str) else None
    if tipo is None:
        raise herramientas.ToolError(f"herramienta desconocida: {nombre_str!r}")
    if tipo == "lectura":
        if sesion.lecturas >= LIMITES_SESION["lecturas"]:
            raise herramientas.ToolError("límite de lecturas de esta sesión alcanzado")
        salida = herramientas.ejecutar(sesion.conn, sesion.person_id, nombre, argumentos, nivel)
        sesion.lecturas += 1
        return salida
    if sesion.propuestas >= LIMITES_SESION["propuestas"]:
        raise herramientas.ToolError("límite de propuestas de esta sesión alcanzado")
    conversacion_id = sesion.conversacion()
    salida = herramientas.ejecutar(sesion.conn, sesion.person_id, nombre, argumentos, nivel, conversacion_id)
    salida.pop("_registro", None)
    sesion.propuestas += 1
    return salida


def _responder(id_, *, resultado=None, error=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = resultado
    return msg


def _procesar_mensaje(sesion: _Sesion, msg: dict) -> dict | None:
    id_ = msg.get("id")
    es_notificacion = "id" not in msg
    metodo = msg.get("method")
    if metodo == "initialize":
        return _responder(id_, resultado={
            "protocolVersion": PROTOCOL_VERSION, "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}},
        })
    if metodo == "notifications/initialized":
        return None
    if metodo == "tools/list":
        return _responder(id_, resultado={"tools": _lista_herramientas(sesion)})
    if metodo == "tools/call":
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        nombre = params.get("name")
        argumentos = params.get("arguments")
        argumentos = argumentos if isinstance(argumentos, dict) else {}
        try:
            salida = _llamar_herramienta(sesion, nombre, argumentos)
        except herramientas.ToolError as e:
            texto = errores.texto_propio(e)
            return _responder(id_, resultado={"content": [{"type": "text", "text": texto}], "isError": True})
        texto = json.dumps(salida, ensure_ascii=False)
        return _responder(id_, resultado={"content": [{"type": "text", "text": texto}]})
    if es_notificacion:
        return None
    return _responder(id_, error=_METHOD_NOT_FOUND)


def ejecutar(entrada: IO[str], salida: IO[str], conn, person_id: int) -> _Sesion:
    """Reads newline-delimited JSON-RPC requests from `entrada`, writes newline-delimited JSON-RPC
    responses to `salida`, until `entrada` is exhausted (the caller's pipe closed). Returns the
    session, so a caller (tests/test_mcp.py, or main() below for its own stderr summary) can read
    its final counters."""
    sesion = _Sesion(conn, person_id)
    for linea in entrada:
        linea = linea.strip()
        if not linea:
            continue
        try:
            msg = json.loads(linea)
        except ValueError:
            respuesta = _responder(None, error=_PARSE_ERROR)
        else:
            respuesta = _procesar_mensaje(sesion, msg) if isinstance(msg, dict) else _responder(
                None, error=_PARSE_ERROR)
        if respuesta is not None:
            salida.write(json.dumps(respuesta, ensure_ascii=False))
            salida.write("\n")
            salida.flush()
    return sesion


def main() -> None:
    conn = db.connect()
    with contextlib.closing(conn):
        db.ensure_ready(conn)
        person_id = db.local_person_id(conn)
        sesion = ejecutar(sys.stdin, sys.stdout, conn, person_id)
        print(f"body.mcp: {sesion.lecturas} lecturas, {sesion.propuestas} propuestas", file=sys.stderr)


if __name__ == "__main__":
    main()
