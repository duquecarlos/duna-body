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
propose more than that level already allows. There is still no apply/undo/delete tool anywhere in
`herramientas.HERRAMIENTAS`, so none of THOSE is ever listed or callable here either: a
`propuesta` tool only ever creates a card in Body's own assistant panel -- in ONE conversation per
MCP session, created lazily on the session's first proposal and tagged `origen='mcp'`
(body/agente/conversacion.py) -- for the person to tap Aplicar there themselves, or (BODY-MCP-
APLICAR-1) to say yes out loud and have this same caller tap it for them.

`aplicar_propuesta` (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2) is the one
exception, and it lives OUTSIDE `herramientas.HERRAMIENTAS` on purpose -- never callable by the
in-app assistant, only by this outside caller. It is listed, and callable, only while the person's
own `aplicar_mcp` setting (body/db.py, changed only through Body's own /settings POST) is ON; it
applies only a proposal THIS caller made (its conversation's `origen` is 'mcp') that is still
`propuesta`, through the SAME `agente_propuestas.aplicar` the web route uses -- never undo, never
delete, never another person's or the in-app assistant's own proposal. Turning the setting back
OFF takes effect on this same long-lived process's very next `tools/list` or `tools/call`, since
both read it fresh (ruling 2).

Caps (ruling 4, BODY-MCP-LOCAL-1-B) bound a RATE, not a session: a rolling window of
`LIMITES["ventana_min"]` minutes holds at most `LIMITES["lecturas"]` reads and `LIMITES["propuestas"]`
proposals, the same ceilings body/agente/bucle.py's own per-question caps (MAX_LECTURAS, MAX_RONDAS)
use, now per window instead of per session -- a long-lived MCP server process (a Claude Code
session keeps one for hours or days) can never be locked out of reading past its first window, the
way a per-process counter would. A capped call's tool error names when to try again, in the
person's own language. Every call -- a read or a proposal, whether it succeeds or is refused --
writes one row to `uso_herramienta_externa` (db.registrar_uso_herramienta_externa): the tool's own
name, the UTC time and the caller id, never its params or its result (ruling 5). Nothing is ever
written to stdout but JSON-RPC frames, both streams UTF-8 regardless of the console's own code
page (BODY-MCP-LOCAL-1-B: MCP stdio is UTF-8, never the platform default); the one line this
module writes elsewhere goes to stderr, and names counts only, never a tool's params or result
(ruling 6).
"""
from __future__ import annotations

import contextlib
import json
import math
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import IO

from . import db, errores
from .agente import ajustes, bucle
from .agente import conversacion as agente_conversacion
from .agente import herramientas
from .agente import propuestas as agente_propuestas
from .i18n import T

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "body-local", "version": "1"}
CALLER = "mcp-local"
ORIGEN = "mcp"

# BODY-MCP-APLICAR-1 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2): the one apply tool, listed only
# while the person's own `aplicar_mcp` setting is ON (checked per tools/list AND per call, so
# turning it off takes effect without restarting this process) -- never part of
# herramientas.HERRAMIENTAS, since the in-app assistant may never call it (ruling 2: "never a
# proposal the in-app assistant made" cuts both ways).
NOMBRE_APLICAR = "aplicar_propuesta"
_PARAM_APLICAR_PROPUESTA = {
    "type": "object",
    "properties": {"propuesta_id": {"type": "integer"}},
    "required": ["propuesta_id"],
    "additionalProperties": False,
}
_DESCRIPCION_APLICAR_PROPUESTA = (
    "Aplica, directamente y sin que la persona tenga que tocar Aplicar en Body, una propuesta que "
    "TÚ (este mismo asistente externo) ya creaste con otra herramienta y que sigue pendiente. "
    "Llama esta herramienta SOLO después de que la persona haya dicho que sí, en palabras "
    "explícitas, en ESTA MISMA conversación -- un 'sí' que te cuenten de otra conversación no "
    "cuenta. La persona lo verá en la pantalla Hoy de Body y lo puede deshacer ahí en cualquier "
    "momento."
)

# A rolling window, not a per-process total (BODY-MCP-LOCAL-1-B): "ventana_min" mirrors
# body/agente/propuestas.py's own EXPIRA_MINUTOS (a proposal this server creates lives exactly that
# long before the person can no longer act on it, so a caller that could still be proposing past
# that point was never going to get an "aplicar" anyway); "lecturas"/"propuestas" mirror
# body/agente/bucle.py's own per-question caps (MAX_LECTURAS, MAX_RONDAS) -- the same ceiling the
# in-app assistant holds itself to for one question, now the ceiling for one window instead of one
# session.
#
# "aplicaciones" (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 6): an apply is a real
# write, not a mere proposal, so it gets bucle.MAX_LECTURAS -- the same tighter ceiling a read
# already carries, instead of the looser MAX_RONDAS "propuestas" reuses.
LIMITES = {"ventana_min": 15, "lecturas": bucle.MAX_LECTURAS, "propuestas": bucle.MAX_RONDAS,
           "aplicaciones": bucle.MAX_LECTURAS}

_PARSE_ERROR = {"code": -32700, "message": "Parse error"}
_METHOD_NOT_FOUND = {"code": -32601, "message": "Method not found"}


class _Tope:
    """One capability's own rolling window: at most `cupo` calls in the last `ventana_min`
    minutes. `reloj` is a zero-argument callable returning the current UTC time -- real time in
    production, an injectable fake in tests, so a test can cross the window without sleeping."""

    def __init__(self, ventana_min: int, cupo: int, reloj):
        self._ventana = timedelta(minutes=ventana_min)
        self._cupo = cupo
        self._reloj = reloj
        self._llamadas: deque = deque()

    def _purgar(self) -> None:
        corte = self._reloj() - self._ventana
        while self._llamadas and self._llamadas[0] < corte:
            self._llamadas.popleft()

    def permite(self) -> bool:
        self._purgar()
        return len(self._llamadas) < self._cupo

    def registrar(self) -> None:
        self._llamadas.append(self._reloj())

    def minutos_para_reintentar(self) -> int:
        self._purgar()
        if not self._llamadas:
            return 0
        resto = (self._llamadas[0] + self._ventana) - self._reloj()
        return max(1, math.ceil(resto.total_seconds() / 60))


class _Sesion:
    """One MCP session's own state: the connection and person it serves, the one conversation its
    proposals land in (created lazily), and this session's own rolling-window caps (BODY-MCP-
    LOCAL-1-B) plus a plain running total of each, for main()'s own stderr summary."""

    def __init__(self, conn, person_id: int, reloj=None):
        self.conn = conn
        self.person_id = person_id
        self.conversacion_id: int | None = None
        reloj = reloj or (lambda: datetime.now(timezone.utc))
        self.tope_lecturas = _Tope(LIMITES["ventana_min"], LIMITES["lecturas"], reloj)
        self.tope_propuestas = _Tope(LIMITES["ventana_min"], LIMITES["propuestas"], reloj)
        self.tope_aplicaciones = _Tope(LIMITES["ventana_min"], LIMITES["aplicaciones"], reloj)
        self.total_lecturas = 0
        self.total_propuestas = 0
        self.total_aplicaciones = 0

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


def _herramienta_aplicar() -> dict:
    return {"name": NOMBRE_APLICAR, "description": _DESCRIPCION_APLICAR_PROPUESTA,
            "inputSchema": _PARAM_APLICAR_PROPUESTA}


def _lista_herramientas(sesion: _Sesion) -> list[dict]:
    nivel = _nivel(sesion)
    tools = [_herramienta_mcp(e) for e in bucle._tool_specs(nivel)]
    if db.get_aplicar_mcp(sesion.conn, sesion.person_id):
        tools.append(_herramienta_aplicar())
    return tools


def _idioma(sesion: _Sesion) -> str:
    return db.person(sesion.conn, sesion.person_id)["language"]


def _mensaje_tope_lecturas(sesion: _Sesion) -> str:
    t = T(_idioma(sesion))
    return t("asistente.mcp.limite_lecturas", minutos=sesion.tope_lecturas.minutos_para_reintentar())


def _mensaje_tope_propuestas(sesion: _Sesion) -> str:
    t = T(_idioma(sesion))
    return t("asistente.mcp.limite_propuestas", minutos=sesion.tope_propuestas.minutos_para_reintentar())


def _mensaje_tope_aplicaciones(sesion: _Sesion) -> str:
    t = T(_idioma(sesion))
    return t("asistente.mcp.limite_aplicaciones", minutos=sesion.tope_aplicaciones.minutos_para_reintentar())


def _mensaje_aplicar_apagado(sesion: _Sesion) -> str:
    return T(_idioma(sesion))("asistente.mcp.aplicar_apagado")


def _mensaje_aplicar_no_propia(sesion: _Sesion) -> str:
    return T(_idioma(sesion))("asistente.mcp.aplicar_no_propia")


def _mensaje_aplicar_no_vigente(sesion: _Sesion) -> str:
    return T(_idioma(sesion))("asistente.mcp.aplicar_no_vigente")


def _aplicar_propuesta_mcp(sesion: _Sesion, argumentos: dict) -> dict:
    """`aplicar_propuesta` (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 rulings 2, 3, 6):
    refuses with a plain tool error when the setting is OFF, when `propuesta_id` is not an int,
    when the row does not exist, is not this person's, was not made by this same outside caller
    (its conversation's `origen` must be 'mcp' -- never a proposal the in-app assistant made), or
    is no longer in state 'propuesta' (already applied, discarded, expired, or drifted). Applies
    through the SAME `agente_propuestas.aplicar` the web route uses, then ties the row to this
    outside apply (db.marcar_aplicada_via_mcp) for the panel's own label."""
    if not db.get_aplicar_mcp(sesion.conn, sesion.person_id):
        raise herramientas.ToolError(_mensaje_aplicar_apagado(sesion))
    if not sesion.tope_aplicaciones.permite():
        raise herramientas.ToolError(_mensaje_tope_aplicaciones(sesion))
    propuesta_id = argumentos.get("propuesta_id")
    if not isinstance(propuesta_id, int):
        raise herramientas.ToolError("propuesta_id debe ser un entero")
    fila = sesion.conn.execute(
        "SELECT conversacion_id FROM propuesta_agente WHERE id = ? AND person_id = ?",
        (propuesta_id, sesion.person_id),
    ).fetchone()
    if fila is None or agente_conversacion.origen_de(sesion.conn, fila["conversacion_id"]) != ORIGEN:
        raise herramientas.ToolError(_mensaje_aplicar_no_propia(sesion))
    resultado = agente_propuestas.aplicar(sesion.conn, sesion.person_id, propuesta_id)
    if resultado["estado"] != "aplicada":
        raise herramientas.ToolError(_mensaje_aplicar_no_vigente(sesion))
    db.marcar_aplicada_via_mcp(sesion.conn, propuesta_id)
    sesion.tope_aplicaciones.registrar()
    sesion.total_aplicaciones += 1
    return {"propuesta_id": propuesta_id, "estado": resultado["estado"]}


def _llamar_herramienta(sesion: _Sesion, nombre, argumentos: dict) -> tuple[str, dict]:
    """Dispatches one `tools/call` -- every attempt, successful or refused, is audited first
    (ruling 5). A read goes straight to `herramientas.ejecutar`; a proposal gets this session's one
    lazily-created, 'mcp'-tagged conversation; `aplicar_propuesta` (BODY-MCP-APLICAR-1) goes to
    `_aplicar_propuesta_mcp`, which carries its own refusals. Raises `herramientas.ToolError`
    (caught by the caller) for an unknown name, a cap reached (its message naming when to try
    again, BODY-MCP-LOCAL-1-B), or anything `herramientas.ejecutar` itself refuses. Returns (tipo,
    salida) -- the caller needs `tipo` to decide whether the result also gets the "you'll see this
    on Hoy" line (a plain proposal) or the "applied, undo it in Body" line (an apply)."""
    nombre_str = nombre if isinstance(nombre, str) else str(nombre)
    db.registrar_uso_herramienta_externa(sesion.conn, sesion.person_id, nombre_str, CALLER)
    if nombre_str == NOMBRE_APLICAR:
        return "aplicacion", _aplicar_propuesta_mcp(sesion, argumentos)
    nivel = _nivel(sesion)
    tipo = herramientas.tipo_de(nombre) if isinstance(nombre, str) else None
    if tipo is None:
        raise herramientas.ToolError(f"herramienta desconocida: {nombre_str!r}")
    if tipo == "lectura":
        if not sesion.tope_lecturas.permite():
            raise herramientas.ToolError(_mensaje_tope_lecturas(sesion))
        salida = herramientas.ejecutar(sesion.conn, sesion.person_id, nombre, argumentos, nivel)
        sesion.tope_lecturas.registrar()
        sesion.total_lecturas += 1
        return tipo, salida
    if not sesion.tope_propuestas.permite():
        raise herramientas.ToolError(_mensaje_tope_propuestas(sesion))
    conversacion_id = sesion.conversacion()
    salida = herramientas.ejecutar(sesion.conn, sesion.person_id, nombre, argumentos, nivel, conversacion_id)
    salida.pop("_registro", None)
    sesion.tope_propuestas.registrar()
    sesion.total_propuestas += 1
    return tipo, salida


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
            tipo, salida = _llamar_herramienta(sesion, nombre, argumentos)
        except herramientas.ToolError as e:
            texto = errores.texto_propio(e)
            return _responder(id_, resultado={"content": [{"type": "text", "text": texto}], "isError": True})
        contenido = [{"type": "text", "text": json.dumps(salida, ensure_ascii=False)}]
        if tipo == "propuesta":
            # BODY-MCP-LOCAL-1-B: a proposal only ever creates a card on Body's own Hoy screen --
            # this plain sentence is the one place the caller is told so, since a bare
            # propuesta_id/estado pair names nothing the caller could show the person itself.
            t = T(_idioma(sesion))
            contenido.insert(0, {"type": "text",
                                  "text": t("asistente.mcp.resultado_ubicacion", pantalla=t("nav.today"))})
        elif tipo == "aplicacion":
            # BODY-MCP-APLICAR-1: says what was applied and that it can be undone in Body (THE
            # WORK step 2) -- never "tap Aplicar", since this call already did that.
            t = T(_idioma(sesion))
            contenido.insert(0, {"type": "text",
                                  "text": t("asistente.mcp.aplicar_resultado", pantalla=t("nav.today"))})
        return _responder(id_, resultado={"content": contenido})
    if es_notificacion:
        return None
    return _responder(id_, error=_METHOD_NOT_FOUND)


def ejecutar(entrada: IO[str], salida: IO[str], conn, person_id: int, reloj=None) -> _Sesion:
    """Reads newline-delimited JSON-RPC requests from `entrada`, writes newline-delimited JSON-RPC
    responses to `salida`, until `entrada` is exhausted (the caller's pipe closed). Returns the
    session, so a caller (tests/test_mcp.py, or main() below for its own stderr summary) can read
    its final counters. `reloj`: BODY-MCP-LOCAL-1-B's injectable clock for the rolling-window caps,
    real time (the default) in production, a fake in tests that must cross a window without
    sleeping."""
    sesion = _Sesion(conn, person_id, reloj)
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
    # MCP stdio is UTF-8, never whatever code page the console happens to be in (BODY-MCP-LOCAL-1-B):
    # on Windows, a tool description carrying "é" written in the console's own encoding arrives as a
    # single byte a UTF-8 client cannot decode, and a client's own UTF-8 "ñ" on stdin would be
    # mis-decoded the same way without this. `hasattr` guards a test double (e.g. io.StringIO) that
    # carries no `reconfigure` of its own; a real stdin/stdout always has one.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", newline="\n")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    conn = db.connect()
    with contextlib.closing(conn):
        db.ensure_ready(conn)
        person_id = db.local_person_id(conn)
        sesion = ejecutar(sys.stdin, sys.stdout, conn, person_id)
        print(f"body.mcp: {sesion.total_lecturas} lecturas, {sesion.total_propuestas} propuestas, "
              f"{sesion.total_aplicaciones} aplicaciones", file=sys.stderr)


if __name__ == "__main__":
    main()
