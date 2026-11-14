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

`descartar_propuesta` (ruling 3) sits beside `aplicar_propuesta`, gated by the same setting: it
retires one of THIS caller's own still-pending proposals (a correction that left an older same-day
proposal obsolete, for instance) through the SAME `agente_propuestas.descartar` the web route's
own Descartar button uses. It writes no health data, so it needs none of `aplicar_propuesta`'s
"the person already said yes" precondition. A proposal this server creates now lives through the
end of the person's own NEXT local day, not 15 minutes (ruling 1, body/agente/propuestas.py's
EXPIRA_MCP_DIAS_SIGUIENTES) -- long enough for a relayed, asynchronous yes to still land.

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
from zoneinfo import ZoneInfo

from . import db, errores
from .agente import ajustes, bucle
from .agente import conversacion as agente_conversacion
from .agente import herramientas
from .agente import propuestas as agente_propuestas
from .i18n import T

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "body-local", "version": "1"}
CALLER = "mcp-local"
# BODY-MCP-REMOTO-SERVIDOR-1: the hosted routes' own caller tag, so an audit row names which door
# made the call -- the cap query below (ruling 4) deliberately does NOT filter on this column: it
# is one shared, cross-door budget per person, never a separate one per door.
CALLER_REMOTO = "mcp-remoto"
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

# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: a sibling tool, listed and gated by the exact same
# `aplicar_mcp` setting as NOMBRE_APLICAR above -- but it writes no health data itself (it only
# moves a proposal OF THIS SAME CALLER's own making from 'propuesta' to the existing 'descartada'
# state, through the same agente_propuestas.descartar the web route's own Descartar button uses),
# so it needs no "the person already said yes" precondition the way aplicar_propuesta does.
NOMBRE_DESCARTAR = "descartar_propuesta"
_PARAM_DESCARTAR_PROPUESTA = {
    "type": "object",
    "properties": {"propuesta_id": {"type": "integer"}},
    "required": ["propuesta_id"],
    "additionalProperties": False,
}
_DESCRIPCION_DESCARTAR_PROPUESTA = (
    "Descarta una propuesta que TÚ (este mismo asistente externo) ya creaste con otra herramienta "
    "y que sigue pendiente -- por ejemplo, una propuesta vieja que una corrección tuya volvió "
    "obsoleta. No necesitas que la persona diga que sí primero: esto no guarda ningún dato de "
    "salud, solo retira la propuesta vieja para que no la vean dos veces."
)

# A rolling window, not a per-process total (BODY-MCP-LOCAL-1-B): "ventana_min" bounds how often
# this caller may call in AT ALL -- a short while, unrelated to how long any ONE proposal stays
# valid now that ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1 gave an mcp-made proposal its own, much
# longer life. It reuses body/agente/propuestas.py's own EXPIRA_MINUTOS constant only because both
# happen to want "a short while" -- read here rather than restated as a bare number; "lecturas"/
# "propuestas" mirror body/agente/bucle.py's own per-question caps (MAX_LECTURAS, MAX_RONDAS) --
# the same ceiling the in-app assistant holds itself to for one question, now the ceiling for one
# window instead of one session.
#
# "aplicaciones" (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 6): an apply is a real
# write, not a mere proposal, so it gets bucle.MAX_LECTURAS -- the same tighter ceiling a read
# already carries, instead of the looser MAX_RONDAS "propuestas" reuses.
LIMITES = {"ventana_min": agente_propuestas.EXPIRA_MINUTOS, "lecturas": bucle.MAX_LECTURAS,
           "propuestas": bucle.MAX_RONDAS, "aplicaciones": bucle.MAX_LECTURAS}

# BODY-MCP-REMOTO-SERVIDOR-1 ruling 4: every tool name that counts as "lectura"/"propuesta" for
# the DB-backed remote caps, read off the SAME registry the local in-memory _Tope's own dispatch
# already keys on (herramientas.tipo_de) -- never a second, hand-typed list. NOMBRE_APLICAR is its
# own cap (one tool, not a set); NOMBRE_DESCARTAR is uncapped, same as the local door.
_NOMBRES_LECTURA = frozenset(h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "lectura")
_NOMBRES_PROPUESTA = frozenset(h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta")

_PARSE_ERROR = {"code": -32700, "message": "Parse error"}
_METHOD_NOT_FOUND = {"code": -32601, "message": "Method not found"}


class RehusoMCP(herramientas.ToolError):
    """A refusal the shared core (below) raises for one of ITS OWN named reasons (a cap, the
    aplicar_mcp setting, a proposal that is not this caller's own) -- carrying a machine `codigo`
    and raw, unlocalized `campos` alongside the usual localized message. IS-A `herramientas.
    ToolError`, so the local stdio door's existing `except herramientas.ToolError` handling (and
    its `errores.texto_propio(e)` text) needs no change (BODY-MCP-REMOTO-SERVIDOR-1 ruling 2): the
    message is still exactly what it always raised. The remote door (body/rutas/mcp_remoto.py)
    reads `codigo`/`campos` instead -- `campos` carries only a raw ISO timestamp or a plain count,
    never pre-localised text, so the PC's own future client can render it with these same
    `_mensaje_*` functions, reading the person's OWN language setting locally."""

    def __init__(self, mensaje: str, codigo: str, **campos):
        super().__init__(mensaje)
        self.codigo = codigo
        self.campos = campos


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
    LOCAL-1-B) plus a plain running total of each, for main()'s own stderr summary.

    This is the local stdio door's own half of the shared core's duck-typed contract (conn,
    person_id, caller, conversacion(), tope_lecturas/tope_propuestas/tope_aplicaciones, contar_*)
    -- `_SesionRemota` below is the remote door's other half, read by the exact same functions
    (BODY-MCP-REMOTO-SERVIDOR-1 ruling 2: one implementation, two doors)."""

    caller = CALLER

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

    def contar_lectura(self) -> None:
        self.total_lecturas += 1

    def contar_propuesta(self) -> None:
        self.total_propuestas += 1

    def contar_aplicacion(self) -> None:
        self.total_aplicaciones += 1


class _TopeDB:
    """A cap backed by the audit table itself (BODY-MCP-REMOTO-SERVIDOR-1 ruling 4) rather than an
    in-memory window: one bounded query -- COUNT and the oldest matching row's timestamp together,
    cached on first use so `permite()` followed by `minutos_para_reintentar()` costs one query, not
    two -- counting ALL of this person's `uso_herramienta_externa` rows (either door, ruling 4
    deliberately gives both doors ONE shared budget) whose `herramienta` is one of `nombres`, in
    the trailing `ventana_min` minutes.

    `_llamar_herramienta`'s own first line writes THIS call's own audit row before any cap is ever
    consulted (the same order the local stdio door already used, ruling 5's "every call, refused
    or not, is audited"), so the count this query reads already includes the current attempt --
    `permite()` allows it through at `<= cupo`, not `< cupo`, for exactly that reason. `registrar()`
    is a no-op: the write that already happened is the count."""

    def __init__(self, conn, person_id: int, nombres, ventana_min: int, cupo: int, reloj):
        self._conn = conn
        self._person_id = person_id
        self._nombres = tuple(nombres)
        self._ventana_min = ventana_min
        self._cupo = cupo
        self._reloj = reloj
        self._consultado = False
        self._n = 0
        self._oldest: str | None = None
        self._ahora: datetime | None = None

    def _consultar(self) -> None:
        if self._consultado:
            return
        self._ahora = self._reloj()
        corte = (self._ahora - timedelta(minutes=self._ventana_min)).isoformat(timespec="seconds")
        placeholders = ",".join("?" * len(self._nombres))
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n, MIN(llamada_en) AS oldest FROM uso_herramienta_externa "
            f"WHERE person_id = ? AND llamada_en >= ? AND herramienta IN ({placeholders})",
            (self._person_id, corte, *self._nombres),
        ).fetchone()
        self._n, self._oldest = row["n"], row["oldest"]
        self._consultado = True

    def permite(self) -> bool:
        self._consultar()
        return self._n <= self._cupo

    def registrar(self) -> None:
        pass

    def minutos_para_reintentar(self) -> int:
        self._consultar()
        if self._oldest is None:
            return 0
        resto = (datetime.fromisoformat(self._oldest) + timedelta(minutes=self._ventana_min)) - self._ahora
        return max(1, math.ceil(resto.total_seconds() / 60))


class _SesionRemota:
    """One HTTP call's own context for the shared MCP core (BODY-MCP-REMOTO-SERVIDOR-1 ruling 2):
    the same shape `_Sesion` carries, so `_lista_herramientas`/`_llamar_herramienta`/
    `_aplicar_propuesta_mcp`/`_descartar_propuesta_mcp` run UNCHANGED for either door -- but every
    cap is a `_TopeDB` read from the audit table instead of an in-memory window, and
    `conversacion_id` is the CLIENT's own, already verified by the route (ruling 3) before this is
    ever built -- `conversacion()` only ever creates one when the client sent none, exactly once,
    never twice, for this one call."""

    caller = CALLER_REMOTO

    def __init__(self, conn, person_id: int, conversacion_id: int | None, reloj=None):
        self.conn = conn
        self.person_id = person_id
        self.conversacion_id = conversacion_id
        reloj = reloj or (lambda: datetime.now(timezone.utc))
        self.tope_lecturas = _TopeDB(conn, person_id, _NOMBRES_LECTURA, LIMITES["ventana_min"],
                                      LIMITES["lecturas"], reloj)
        self.tope_propuestas = _TopeDB(conn, person_id, _NOMBRES_PROPUESTA, LIMITES["ventana_min"],
                                        LIMITES["propuestas"], reloj)
        self.tope_aplicaciones = _TopeDB(conn, person_id, (NOMBRE_APLICAR,), LIMITES["ventana_min"],
                                          LIMITES["aplicaciones"], reloj)

    def conversacion(self) -> int:
        if self.conversacion_id is None:
            self.conversacion_id = agente_conversacion.crear(self.conn, self.person_id, origen=ORIGEN)
        return self.conversacion_id

    def contar_lectura(self) -> None:
        pass

    def contar_propuesta(self) -> None:
        pass

    def contar_aplicacion(self) -> None:
        pass


def _herramienta_mcp(especificacion: dict) -> dict:
    return {"name": especificacion["nombre"], "description": especificacion["descripcion"],
            "inputSchema": especificacion["parametros"]}


def _nivel(sesion: _Sesion) -> str:
    herramientas.fijar_turnos_actual(db.trabaja_turnos_si(sesion.conn, sesion.person_id))
    return ajustes.nivel_datos(sesion.conn, sesion.person_id)


def _herramienta_aplicar() -> dict:
    return {"name": NOMBRE_APLICAR, "description": _DESCRIPCION_APLICAR_PROPUESTA,
            "inputSchema": _PARAM_APLICAR_PROPUESTA}


def _herramienta_descartar() -> dict:
    return {"name": NOMBRE_DESCARTAR, "description": _DESCRIPCION_DESCARTAR_PROPUESTA,
            "inputSchema": _PARAM_DESCARTAR_PROPUESTA}


def _lista_herramientas(sesion: _Sesion) -> list[dict]:
    nivel = _nivel(sesion)
    tools = [_herramienta_mcp(e) for e in bucle._tool_specs(nivel)]
    if db.get_aplicar_mcp(sesion.conn, sesion.person_id):
        tools.append(_herramienta_aplicar())
        tools.append(_herramienta_descartar())
    return tools


def _idioma(sesion: _Sesion) -> str:
    return db.person(sesion.conn, sesion.person_id)["language"]


def _mensaje_tope_lecturas(idioma: str, minutos: int) -> str:
    return T(idioma)("asistente.mcp.limite_lecturas", minutos=minutos)


def _mensaje_tope_propuestas(idioma: str, minutos: int) -> str:
    return T(idioma)("asistente.mcp.limite_propuestas", minutos=minutos)


def _mensaje_tope_aplicaciones(idioma: str, minutos: int) -> str:
    return T(idioma)("asistente.mcp.limite_aplicaciones", minutos=minutos)


def _mensaje_aplicar_apagado(sesion: _Sesion) -> str:
    return T(_idioma(sesion))("asistente.mcp.aplicar_apagado")


def _mensaje_aplicar_no_propia(sesion: _Sesion) -> str:
    return T(_idioma(sesion))("asistente.mcp.aplicar_no_propia")


def _mensaje_aplicar_no_vigente(sesion: _Sesion) -> str:
    return T(_idioma(sesion))("asistente.mcp.aplicar_no_vigente")


def _mensaje_aplicar_expirada(sesion: _Sesion, expira_en: str) -> str:
    """Ruling 4: when `aplicar_propuesta` meets a proposal that turned out to be expired, the
    message names WHEN it expired -- in the person's own language and local time -- and says to
    propose again, never just the generic "no longer pending" every other non-apply outcome still
    gets from `_mensaje_aplicar_no_vigente`."""
    t = T(_idioma(sesion))
    tz = ZoneInfo(db.person(sesion.conn, sesion.person_id).get("timezone") or "America/Bogota")
    local = datetime.fromisoformat(expira_en).astimezone(tz)
    return t("asistente.mcp.aplicar_expirada", fecha=t.long_date(local.date()),
             hora=f"{local.hour:02d}:{local.minute:02d}")


def _mensaje_descartar_apagado(sesion: _Sesion) -> str:
    return T(_idioma(sesion))("asistente.mcp.descartar_apagado")


def _aplicar_propuesta_mcp(sesion: _Sesion, argumentos: dict) -> dict:
    """`aplicar_propuesta` (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 rulings 2, 3, 6):
    refuses with a plain tool error when the setting is OFF, when `propuesta_id` is not an int,
    when the row does not exist, is not this person's, was not made by this same outside caller
    (its conversation's `origen` must be 'mcp' -- never a proposal the in-app assistant made), or
    is no longer in state 'propuesta' (already applied, discarded, expired, or drifted). Applies
    through the SAME `agente_propuestas.aplicar` the web route uses, then ties the row to this
    outside apply (db.marcar_aplicada_via_mcp) for the panel's own label."""
    if not db.get_aplicar_mcp(sesion.conn, sesion.person_id):
        raise RehusoMCP(_mensaje_aplicar_apagado(sesion), "aplicar_apagado")
    if not sesion.tope_aplicaciones.permite():
        minutos = sesion.tope_aplicaciones.minutos_para_reintentar()
        raise RehusoMCP(_mensaje_tope_aplicaciones(_idioma(sesion), minutos), "limite", minutos=minutos)
    propuesta_id = argumentos.get("propuesta_id")
    if not isinstance(propuesta_id, int):
        raise herramientas.ToolError("propuesta_id debe ser un entero")
    fila = sesion.conn.execute(
        "SELECT conversacion_id FROM propuesta_agente WHERE id = ? AND person_id = ?",
        (propuesta_id, sesion.person_id),
    ).fetchone()
    if fila is None or agente_conversacion.origen_de(sesion.conn, fila["conversacion_id"]) != ORIGEN:
        raise RehusoMCP(_mensaje_aplicar_no_propia(sesion), "aplicar_no_propia")
    resultado = agente_propuestas.aplicar(sesion.conn, sesion.person_id, propuesta_id)
    if resultado["estado"] == "expirada":
        raise RehusoMCP(_mensaje_aplicar_expirada(sesion, resultado["expira_en"]), "aplicar_expirada",
                         expira_en=resultado["expira_en"])
    if resultado["estado"] != "aplicada":
        raise RehusoMCP(_mensaje_aplicar_no_vigente(sesion), "aplicar_no_vigente")
    db.marcar_aplicada_via_mcp(sesion.conn, propuesta_id)
    sesion.tope_aplicaciones.registrar()
    sesion.contar_aplicacion()
    return {"propuesta_id": propuesta_id, "estado": resultado["estado"]}


def _descartar_propuesta_mcp(sesion: _Sesion, argumentos: dict) -> dict:
    """`descartar_propuesta` (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3): refuses with a plain tool
    error when the setting is OFF, when `propuesta_id` is not an int, when the row does not exist,
    is not this person's, or was not made by this same outside caller (its conversation's `origen`
    must be 'mcp'), or is no longer in state 'propuesta' (already applied, discarded, expired, or
    drifted) -- naming why in each case, in the person's own language, the same two refusal
    messages `aplicar_propuesta` above already carries. Discards through the SAME
    `agente_propuestas.descartar` the web route's own Descartar button uses; writes no health
    data itself, so it carries no drift check and needs no "the person already said yes"
    precondition."""
    if not db.get_aplicar_mcp(sesion.conn, sesion.person_id):
        raise RehusoMCP(_mensaje_descartar_apagado(sesion), "descartar_apagado")
    propuesta_id = argumentos.get("propuesta_id")
    if not isinstance(propuesta_id, int):
        raise herramientas.ToolError("propuesta_id debe ser un entero")
    fila = sesion.conn.execute(
        "SELECT conversacion_id FROM propuesta_agente WHERE id = ? AND person_id = ?",
        (propuesta_id, sesion.person_id),
    ).fetchone()
    if fila is None or agente_conversacion.origen_de(sesion.conn, fila["conversacion_id"]) != ORIGEN:
        raise RehusoMCP(_mensaje_aplicar_no_propia(sesion), "aplicar_no_propia")
    resultado = agente_propuestas.descartar(sesion.conn, sesion.person_id, propuesta_id)
    if resultado["estado"] != "descartada":
        raise RehusoMCP(_mensaje_aplicar_no_vigente(sesion), "aplicar_no_vigente")
    return {"propuesta_id": propuesta_id, "estado": resultado["estado"]}


def _llamar_herramienta(sesion: _Sesion, nombre, argumentos: dict) -> tuple[str, dict, bool]:
    """Dispatches one `tools/call` -- every attempt, successful or refused, is audited first
    (ruling 5). A read goes straight to `herramientas.ejecutar`; a proposal gets this session's one
    lazily-created, 'mcp'-tagged conversation; `aplicar_propuesta` (BODY-MCP-APLICAR-1) goes to
    `_aplicar_propuesta_mcp`, which carries its own refusals. Raises `herramientas.ToolError`
    (caught by the caller) for an unknown name, a cap reached (its message naming when to try
    again, BODY-MCP-LOCAL-1-B), or anything `herramientas.ejecutar` itself refuses. Returns (tipo,
    salida, aviso_reloj) -- the caller needs `tipo` to decide whether the result also gets the
    "you'll see this on Hoy" line (a plain proposal) or the "applied, undo it in Body" line (an
    apply); `aviso_reloj` (BODY-SERIES-UNIDAD-1 ruling 3) is True only for a `registrar_entreno`
    proposal whose own watch-sets read failed, so the caller can add the one extra line telling
    the person's sets were saved without the watch's."""
    nombre_str = nombre if isinstance(nombre, str) else str(nombre)
    db.registrar_uso_herramienta_externa(sesion.conn, sesion.person_id, nombre_str, sesion.caller)
    if nombre_str == NOMBRE_APLICAR:
        return "aplicacion", _aplicar_propuesta_mcp(sesion, argumentos), False
    if nombre_str == NOMBRE_DESCARTAR:
        return "descarte", _descartar_propuesta_mcp(sesion, argumentos), False
    nivel = _nivel(sesion)
    tipo = herramientas.tipo_de(nombre) if isinstance(nombre, str) else None
    if tipo is None:
        raise herramientas.ToolError(f"herramienta desconocida: {nombre_str!r}")
    if tipo == "lectura":
        if not sesion.tope_lecturas.permite():
            minutos = sesion.tope_lecturas.minutos_para_reintentar()
            raise RehusoMCP(_mensaje_tope_lecturas(_idioma(sesion), minutos), "limite", minutos=minutos)
        salida = herramientas.ejecutar(sesion.conn, sesion.person_id, nombre, argumentos, nivel)
        sesion.tope_lecturas.registrar()
        sesion.contar_lectura()
        return tipo, salida, False
    if not sesion.tope_propuestas.permite():
        minutos = sesion.tope_propuestas.minutos_para_reintentar()
        raise RehusoMCP(_mensaje_tope_propuestas(_idioma(sesion), minutos), "limite", minutos=minutos)
    conversacion_id = sesion.conversacion()
    salida = herramientas.ejecutar(sesion.conn, sesion.person_id, nombre, argumentos, nivel, conversacion_id)
    registro = salida.pop("_registro", None)
    aviso_reloj = bool(registro and registro.get("despues_json", {}).get("reloj_fallo"))
    sesion.tope_propuestas.registrar()
    sesion.contar_propuesta()
    return tipo, salida, aviso_reloj


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
            tipo, salida, aviso_reloj = _llamar_herramienta(sesion, nombre, argumentos)
        except herramientas.ToolError as e:
            texto = errores.texto_propio(e)
            return _responder(id_, resultado={"content": [{"type": "text", "text": texto}], "isError": True})
        except Exception as e:
            # Ruling 4 (BODY-SERIES-UNIDAD-1): the server never dies on a tool call -- anything
            # else `_llamar_herramienta` raises (a bad Garmin payload, a DB error, ...) answers
            # THIS request only, rolls back whatever it left half-done, and the next line is still
            # read. Only the exception's TYPE and the tool's own name reach stderr (ruling 6
            # already in force above); never its message, never the call's arguments.
            sesion.conn.rollback()
            nombre_str = nombre if isinstance(nombre, str) else str(nombre)
            print(f"mcp tools/call error: {errores.texto_seguro(e)} herramienta={nombre_str!r}", file=sys.stderr)
            t = T(_idioma(sesion))
            return _responder(id_, resultado={
                "content": [{"type": "text", "text": t("asistente.mcp.error_interno")}], "isError": True,
            })
        contenido = [{"type": "text", "text": json.dumps(salida, ensure_ascii=False)}]
        if tipo == "propuesta":
            # BODY-MCP-LOCAL-1-B: a proposal only ever creates a card on Body's own Hoy screen --
            # this plain sentence is the one place the caller is told so, since a bare
            # propuesta_id/estado pair names nothing the caller could show the person itself.
            t = T(_idioma(sesion))
            contenido.insert(0, {"type": "text",
                                  "text": t("asistente.mcp.resultado_ubicacion", pantalla=t("nav.today"))})
            if aviso_reloj:
                # Ruling 3 (BODY-SERIES-UNIDAD-1): the watch's sets were a help, not a
                # precondition -- this is the one place the caller (who cannot see the Hoy card
                # directly) is told the proposal still saved on the person's own spoken sets.
                contenido.insert(0, {"type": "text", "text": t("asistente.mcp.entreno_reloj_fallo")})
        elif tipo == "aplicacion":
            # BODY-MCP-APLICAR-1: says what was applied and that it can be undone in Body (THE
            # WORK step 2) -- never "tap Aplicar", since this call already did that.
            t = T(_idioma(sesion))
            contenido.insert(0, {"type": "text",
                                  "text": t("asistente.mcp.aplicar_resultado", pantalla=t("nav.today"))})
        elif tipo == "descarte":
            # Ruling 3: this caller never sees the Hoy card directly, so this is the one place it
            # is told the old proposal is gone.
            t = T(_idioma(sesion))
            contenido.insert(0, {"type": "text", "text": t("asistente.mcp.descartar_resultado")})
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
