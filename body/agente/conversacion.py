"""Conversations with Body's assistant, stored per person (BODY-ASISTENTE-PROVEEDOR-1, decision
4). Tool results are stored too -- they are the person's own data, on Body's own disk -- so the
history sent back to the provider is rebuilt server-side from these rows (agent-capabilities-v1
section 7: a server-stored history needs no signing).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone


class ConversacionAjena(Exception):
    """A conversacion_id that exists but belongs to another person, or does not exist at all."""


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def crear(conn: sqlite3.Connection, person_id: int, titulo: str | None = None,
          origen: str | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en, titulo, origen) VALUES (?, ?, ?, ?)",
            (person_id, _ahora(), titulo, origen),
        )
    return cur.lastrowid


def origen_de(conn: sqlite3.Connection, conversacion_id: int) -> str | None:
    """This conversation's `origen` column (BODY-MCP-LOCAL-1): None for one started in Body's own
    chat panel, 'mcp' for one the local MCP server started -- so the panel can label its proposal
    cards. None too for an id that does not exist; the caller already owns the ownership check."""
    row = conn.execute("SELECT origen FROM conversacion_agente WHERE id = ?", (conversacion_id,)).fetchone()
    return row["origen"] if row else None


def pendientes_mcp(conn: sqlite3.Connection, person_id: int) -> tuple[int, int | None]:
    """(count, newest conversation id) of proposals still in state 'propuesta' sitting in a
    conversation whose `origen` is the outside caller ('mcp') -- BODY-MCP-LOCAL-1-B, the person's
    own visibility into what their personal assistant proposed through the local MCP server. Hoy's
    own line and the assistant panel's default both read this, one statement, so the route cost
    the same person-level fact reaches two screens from costs only once. (0, None) when there is
    none."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(p.conversacion_id) AS id FROM propuesta_agente p "
        "JOIN conversacion_agente c ON c.id = p.conversacion_id "
        "WHERE p.person_id = ? AND p.estado = 'propuesta' AND c.origen = 'mcp'",
        (person_id,),
    ).fetchone()
    return row["n"], row["id"]


def aplicados_mcp_recientes(conn: sqlite3.Connection, person_id: int, ahora: datetime,
                             horas: int = 24) -> tuple[int, int | None]:
    """(count, newest conversation id) of proposals this OUTSIDE caller applied itself in the last
    `horas` hours, through its own `aplicar_propuesta` tool (`aplicada_via = 'mcp'`, set only by
    body/mcp.py, never by Body's own /asistente/propuestas/{id}/aplicar route) -- BODY-MCP-
    APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: Hoy's own 24h-wide aviso line after an
    outside apply, so nothing lands silently. Deliberately NOT "any mcp-origin proposal now in
    state aplicada" -- the person tapping Aplicar themselves, in Body, on a proposal their
    assistant merely proposed, is not an outside apply and must never raise this line (a case
    tests/test_mcp_visible.py's own HoyLineaTest already exercises). `ahora` is the caller's own
    clock (real time in production, an injectable fake in tests, so a test can cross the 24h edge
    without sleeping). (0, None) when there is none, or once every such apply has aged past the
    window."""
    corte = (ahora - timedelta(hours=horas)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(conversacion_id) AS id FROM propuesta_agente "
        "WHERE person_id = ? AND estado = 'aplicada' AND aplicada_via = 'mcp' AND resuelta_en >= ?",
        (person_id, corte),
    ).fetchone()
    return row["n"], row["id"]


def verificar_propiedad(conn: sqlite3.Connection, conversacion_id: int, person_id: int) -> None:
    row = conn.execute(
        "SELECT person_id FROM conversacion_agente WHERE id = ?", (conversacion_id,)
    ).fetchone()
    if row is None or row["person_id"] != person_id:
        raise ConversacionAjena()


def agregar_mensaje(conn: sqlite3.Connection, conversacion_id: int, person_id: int, rol: str,
                     bloques: list[dict]) -> None:
    with conn:
        conn.execute(
            "INSERT INTO mensaje_agente (conversacion_id, person_id, rol, contenido_json, creado_en) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversacion_id, person_id, rol, json.dumps(bloques, ensure_ascii=False), _ahora()),
        )


def historial(conn: sqlite3.Connection, conversacion_id: int, person_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT rol, contenido_json FROM mensaje_agente WHERE conversacion_id = ? AND person_id = ? ORDER BY id",
        (conversacion_id, person_id),
    ).fetchall()
    return [{"rol": r["rol"], "bloques": json.loads(r["contenido_json"])} for r in rows]


def turnos_visibles(conn: sqlite3.Connection, conversacion_id: int, person_id: int) -> list[dict]:
    """The visible turns of a conversation, for the panel to redraw after a page change: the
    person's own questions and the assistant's answers, each `{"rol": "persona"|"asistente",
    "texto": str, "pasos": int}`. Tool calls and tool results (stored as "usuario" blocks with no
    "texto") never appear -- they are rebuilt from `pasos`, a count, not raw tool output.
    Raises `ConversacionAjena` for another person's id or an id that does not exist.
    """
    verificar_propiedad(conn, conversacion_id, person_id)
    turnos: list[dict] = []
    actual: dict | None = None
    for mensaje in historial(conn, conversacion_id, person_id):
        bloques = mensaje["bloques"]
        if mensaje["rol"] == "usuario":
            textos = [b["texto"] for b in bloques if b.get("tipo") == "texto"]
            if textos:
                turnos.append({"rol": "persona", "texto": "".join(textos), "pasos": 0})
                actual = None
            continue
        textos = [b["texto"] for b in bloques if b.get("tipo") == "texto" and b.get("texto")]
        pasos_n = sum(1 for b in bloques if b.get("tipo") == "llamada_herramienta")
        if actual is None:
            actual = {"rol": "asistente", "texto": "", "pasos": 0}
            turnos.append(actual)
        if textos:
            actual["texto"] += "".join(textos)
        actual["pasos"] += pasos_n
    return turnos
