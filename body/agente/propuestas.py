"""The durable proposal record (agent-capabilities-v1 section 2 + amendment 1, BODY-ASISTENTE-
PROPUESTAS-1): a `propuesta`-kind tool in body/agente/herramientas.py calls `crear` -- nothing on
the target changes until the person taps Aplicar in the chat, which calls `aplicar` here.

Applying and undoing call the SAME write functions the screens already use: body/goals.py's
`propose` and `drop`, body/db.py's `update_goal_target` and `set_language`, body/shift_import.py's
`confirm_all`. This module never writes a target table's row itself.

Only a `propuesta`-kind tool function may import this module (the static guard in
tests/test_agente_registro.py): a `lectura` tool never reaches a writer, and this is the one route
a proposal tool has to one.

`aplicar`'s conditional UPDATE and the write it triggers share ONE transaction: the UPDATE opens it
(sqlite3's implicit-transaction mode) and the write function's own `with conn:` commits it, so a
crash between them leaves nothing applied. The one exception is `proponer_meta`'s `objetivo_id`
bookkeeping (recording the goal `propose` just created, so a later `deshacer` knows what to
remove), which happens in a short follow-up write after that commit -- a narrow, documented gap:
undo becomes unavailable for that one proposal if the process crashes in that exact instant, never
silent data loss.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .. import db, goals, shift_import

EXPIRA_MINUTOS = 15
SIN_DESHACER = ("proponer_confirmar_turnos",)


class PropuestaAjena(Exception):
    """A propuesta_id that exists but belongs to another person, or does not exist at all."""


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _row_a_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["antes_json"] = json.loads(d["antes_json"]) if d["antes_json"] is not None else None
    d["despues_json"] = json.loads(d["despues_json"])
    return d


def crear(conn: sqlite3.Connection, person_id: int, conversacion_id: int, accion: str, despues: dict, *,
          objetivo_id: int | None = None, antes: dict | None = None) -> dict:
    ahora = _ahora()
    expira = ahora + timedelta(minutes=EXPIRA_MINUTOS)
    with conn:
        cur = conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, objetivo_id, antes_json, "
            "despues_json, estado, creada_en, expira_en) VALUES (?, ?, ?, ?, ?, ?, 'propuesta', ?, ?)",
            (person_id, conversacion_id, accion, objetivo_id,
             json.dumps(antes, ensure_ascii=False) if antes is not None else None,
             json.dumps(despues, ensure_ascii=False), _iso(ahora), _iso(expira)),
        )
    row = conn.execute("SELECT * FROM propuesta_agente WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_a_dict(row)


def _fila_o_ajena(conn: sqlite3.Connection, person_id: int, propuesta_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()
    if row is None or row["person_id"] != person_id:
        raise PropuestaAjena()
    return row


def _vencida(row: sqlite3.Row, ahora_iso: str) -> bool:
    return ahora_iso > row["expira_en"]


def _marcar(conn: sqlite3.Connection, propuesta_id: int, person_id: int, desde: str, hasta: str,
            ahora_iso: str) -> bool:
    """The conditional update that makes approval single-use durably: True iff exactly one row
    moved desde -> hasta."""
    cur = conn.execute(
        "UPDATE propuesta_agente SET estado = ?, resuelta_en = ? WHERE id = ? AND person_id = ? AND estado = ?",
        (hasta, ahora_iso, propuesta_id, person_id, desde),
    )
    return cur.rowcount == 1


def _drift_aplicar(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """True iff the target moved since the proposal was created. `proponer_meta` is insert-shaped
    and has no value before (amendment 1a), so it never drifts."""
    accion = row["accion"]
    if accion == "proponer_meta":
        return False
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    if accion == "proponer_cambio_meta":
        g = conn.execute("SELECT target_value, deadline FROM goal WHERE id = ? AND person_id = ?",
                         (row["objetivo_id"], row["person_id"])).fetchone()
        return g is None or g["target_value"] != antes.get("target_value") or g["deadline"] != antes.get("deadline")
    if accion == "proponer_confirmar_turnos":
        return shift_import.summary_proposed(conn, row["person_id"]) != antes.get("resumen")
    if accion == "proponer_idioma":
        return db.person(conn, row["person_id"])["language"] != antes.get("lang")
    return False


def _drift_deshacer(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """True iff the target no longer holds the value this proposal applied -- `deshacer` refuses
    rather than overwrite someone else's later change."""
    accion = row["accion"]
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
    if accion in ("proponer_meta", "proponer_cambio_meta"):
        g = conn.execute("SELECT target_value, deadline FROM goal WHERE id = ? AND person_id = ?",
                         (row["objetivo_id"], person_id)).fetchone()
        return g is None or g["target_value"] != despues["target_value"] or g["deadline"] != despues["deadline"]
    if accion == "proponer_idioma":
        return db.person(conn, person_id)["language"] != despues["lang"]
    return False


def _escribir_aplicacion(conn: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    """Calls the same write function the screens use. Returns a newly created goal's id for
    `proponer_meta` (so `deshacer` can find it later), None otherwise."""
    accion = row["accion"]
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
    if accion == "proponer_meta":
        return goals.propose(conn, person_id, despues["titulo"], despues["start_day"],
                              metric_key=despues["metric_key"], direction=despues["direction"],
                              target_value=despues["target_value"], deadline=despues["deadline"])
    if accion == "proponer_cambio_meta":
        db.update_goal_target(conn, row["objetivo_id"], person_id, target_value=despues["target_value"],
                               deadline=despues["deadline"])
    elif accion == "proponer_confirmar_turnos":
        shift_import.confirm_all(conn, person_id)
    elif accion == "proponer_idioma":
        db.set_language(conn, despues["lang"], person_id)
    return None


def _escribir_deshacer(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    accion = row["accion"]
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    person_id = row["person_id"]
    if accion == "proponer_meta":
        goals.drop(conn, row["objetivo_id"])
    elif accion == "proponer_cambio_meta":
        db.update_goal_target(conn, row["objetivo_id"], person_id, target_value=antes["target_value"],
                               deadline=antes["deadline"])
    elif accion == "proponer_idioma":
        db.set_language(conn, antes["lang"], person_id)


def aplicar(conn: sqlite3.Connection, person_id: int, propuesta_id: int) -> dict:
    row = _fila_o_ajena(conn, person_id, propuesta_id)
    if row["estado"] != "propuesta":
        return {"estado": "ya_resuelta"}
    ahora_iso = _iso(_ahora())
    if _vencida(row, ahora_iso):
        if _marcar(conn, propuesta_id, person_id, "propuesta", "expirada", ahora_iso):
            conn.commit()
            return {"estado": "expirada"}
        conn.rollback()
        return {"estado": "ya_resuelta"}
    if _drift_aplicar(conn, row):
        conn.rollback()
        return {"estado": "propuesta_desactualizada"}
    if not _marcar(conn, propuesta_id, person_id, "propuesta", "aplicada", ahora_iso):
        conn.rollback()
        return {"estado": "ya_resuelta"}
    goal_id_creado = _escribir_aplicacion(conn, row)
    if goal_id_creado is not None:
        with conn:
            conn.execute("UPDATE propuesta_agente SET objetivo_id = ? WHERE id = ?", (goal_id_creado, propuesta_id))
    return {"estado": "aplicada"}


def descartar(conn: sqlite3.Connection, person_id: int, propuesta_id: int) -> dict:
    row = _fila_o_ajena(conn, person_id, propuesta_id)
    if row["estado"] != "propuesta":
        return {"estado": "ya_resuelta"}
    ahora_iso = _iso(_ahora())
    if _vencida(row, ahora_iso):
        if _marcar(conn, propuesta_id, person_id, "propuesta", "expirada", ahora_iso):
            conn.commit()
            return {"estado": "expirada"}
        conn.rollback()
        return {"estado": "ya_resuelta"}
    if _marcar(conn, propuesta_id, person_id, "propuesta", "descartada", ahora_iso):
        conn.commit()
        return {"estado": "descartada"}
    conn.rollback()
    return {"estado": "ya_resuelta"}


def deshacer(conn: sqlite3.Connection, person_id: int, propuesta_id: int) -> dict:
    row = _fila_o_ajena(conn, person_id, propuesta_id)
    if row["accion"] in SIN_DESHACER:
        return {"estado": "sin_deshacer"}
    if row["estado"] != "aplicada":
        return {"estado": "ya_resuelta"}
    if _drift_deshacer(conn, row):
        return {"estado": "propuesta_desactualizada"}
    ahora_iso = _iso(_ahora())
    if not _marcar(conn, propuesta_id, person_id, "aplicada", "deshecha", ahora_iso):
        conn.rollback()
        return {"estado": "ya_resuelta"}
    _escribir_deshacer(conn, row)
    return {"estado": "deshecha"}


def estado_conversacion(conn: sqlite3.Connection, person_id: int, conversacion_id: int) -> list[dict]:
    """(id, accion, estado) for every proposal of this conversation -- what the system prompt's
    status block (agent-capabilities-v1 section 3) tells the model, authoritative over its own
    earlier turns."""
    rows = conn.execute(
        "SELECT id, accion, estado FROM propuesta_agente WHERE person_id = ? AND conversacion_id = ? ORDER BY id",
        (person_id, conversacion_id),
    ).fetchall()
    return [dict(r) for r in rows]


def listar(conn: sqlite3.Connection, person_id: int, conversacion_id: int) -> list[dict]:
    """Every proposal of this conversation, full rows -- for redrawing cards after a page change."""
    rows = conn.execute(
        "SELECT * FROM propuesta_agente WHERE person_id = ? AND conversacion_id = ? ORDER BY id",
        (person_id, conversacion_id),
    ).fetchall()
    return [_row_a_dict(r) for r in rows]
