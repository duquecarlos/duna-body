"""The durable proposal record (agent-capabilities-v1 section 2 + amendment 1, BODY-ASISTENTE-
PROPUESTAS-1): a `propuesta`-kind tool in body/agente/herramientas.py calls `crear` -- nothing on
the target changes until the person taps Aplicar in the chat, which calls `aplicar` here.

Applying and undoing call the SAME write functions the screens already use: body/goals.py's
`propose` and `drop`, body/db.py's `update_goal_target`, `set_language`, `create_manual_session` and
`correct_exercise_set`, body/shift_import.py's `confirm_all`. `planear_entreno` is the one
exception (BODY-ENTRENO-TOOLS-1): no screen writes the `plan` table yet, so this module inserts and
deletes that one row directly. `registrar_entreno`'s `deshacer`, when it corrected an existing
Garmin set, is a second, narrower exception: restoring a set's exact pre-correction state --
including turning `corrected_by_person` back to 0 -- is not expressible through
`correct_exercise_set` (which only ever moves a set INTO "corrected"), so that one restoration is a
direct UPDATE/DELETE on `workout_set` here.

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
    """True iff the target moved since the proposal was created. `proponer_meta` and
    `planear_entreno` are insert-shaped and have no value before (amendment 1a), so they never
    drift. `registrar_entreno`'s manual case is insert-shaped too; its Garmin-matched case drifts
    when a later `pull`/`traer_series` changed the watch's own reading for a touched set."""
    accion = row["accion"]
    if accion in ("proponer_meta", "planear_entreno"):
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
    if accion == "registrar_entreno":
        despues = json.loads(row["despues_json"])
        if despues["manual"]:
            return False
        source, source_id = despues["source"], despues["source_id"]
        actuales = {s["set_order"]: s for s in db.exercise_sets(conn, row["person_id"], source, source_id)}
        for snap in antes["sets_antes"]:
            actual = actuales.get(snap["set_order"])
            if snap["existia"]:
                if actual is None:
                    return True
                if (actual["garmin"]["exercise"] != snap["garmin_exercise"]
                        or actual["garmin"]["reps"] != snap["garmin_reps"]
                        or actual["garmin"]["weight_value"] != snap["garmin_weight_value"]
                        or actual["garmin"]["category"] != snap["garmin_category"]):
                    return True
            elif actual is not None:
                return True
        return False
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
    if accion == "planear_entreno":
        plan_id = despues.get("plan_id")
        p = conn.execute("SELECT ejercicios_json FROM plan WHERE id = ? AND person_id = ?",
                         (plan_id, person_id)).fetchone()
        return p is None or p["ejercicios_json"] != json.dumps(despues["ejercicios"], ensure_ascii=False)
    if accion == "registrar_entreno":
        if despues["manual"]:
            source_id = despues.get("source_id")
            if source_id is None:
                return True
            actuales = db.exercise_sets(conn, person_id, "manual", source_id)
            if len(actuales) != len(despues["series"]):
                return True
            for i, s in enumerate(despues["series"], start=1):
                actual = next((a for a in actuales if a["set_order"] == i), None)
                if actual is None or actual["exercise"] != s.get("ejercicio") or actual["reps"] != s.get("reps"):
                    return True
            return False
        source, source_id = despues["source"], despues["source_id"]
        actuales = db.exercise_sets(conn, person_id, source, source_id)
        for i, s in enumerate(despues["series"], start=1):
            actual = next((a for a in actuales if a["set_order"] == i), None)
            if (actual is None or not actual["corrected_by_person"]
                    or actual["exercise"] != s.get("ejercicio") or actual["reps"] != s.get("reps")):
                return True
        return False
    return False


def _escribir_aplicacion(conn: sqlite3.Connection, row: sqlite3.Row) -> dict | None:
    """Calls the same write function the screens use. Returns what `aplicar` must write back into
    the proposal record itself, so a later `deshacer` can find what was created: `{"goal_id": ...}`
    for `proponer_meta` (into the `objetivo_id` column), `{"plan_id": ...}` for `planear_entreno`
    or `{"source_id": ...}` for a manual `registrar_entreno` (both merged into `despues_json`,
    which carries no such FK constraint) -- or None when nothing new was created."""
    accion = row["accion"]
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
    if accion == "proponer_meta":
        goal_id = goals.propose(conn, person_id, despues["titulo"], despues["start_day"],
                                 metric_key=despues["metric_key"], direction=despues["direction"],
                                 target_value=despues["target_value"], deadline=despues["deadline"])
        return {"goal_id": goal_id}
    if accion == "proponer_cambio_meta":
        db.update_goal_target(conn, row["objetivo_id"], person_id, target_value=despues["target_value"],
                               deadline=despues["deadline"])
    elif accion == "proponer_confirmar_turnos":
        shift_import.confirm_all(conn, person_id)
    elif accion == "proponer_idioma":
        db.set_language(conn, despues["lang"], person_id)
    elif accion == "planear_entreno":
        with conn:
            cur = conn.execute(
                "INSERT INTO plan (person_id, goal_id, day, time, title, status, ejercicios_json) "
                "VALUES (?, ?, ?, ?, ?, 'proposed', ?)",
                (person_id, despues.get("goal_id"), despues["day"], despues.get("time"), despues["titulo"],
                 json.dumps(despues["ejercicios"], ensure_ascii=False)),
            )
        return {"plan_id": cur.lastrowid}
    elif accion == "registrar_entreno":
        if despues["manual"]:
            source_id = db.create_manual_session(
                conn, person_id, despues["day"],
                f"{despues['day']}T{despues['time']}:00" if despues.get("time") else None,
                despues["titulo"],
            )
            for i, s in enumerate(despues["series"], start=1):
                db.correct_exercise_set(conn, person_id, "manual", source_id, i, exercise=s.get("ejercicio"),
                                         category=s.get("categoria"), reps=s.get("reps"),
                                         weight_kg=s.get("peso_kg"), note=s.get("nota"))
            return {"source_id": source_id}
        for i, s in enumerate(despues["series"], start=1):
            db.correct_exercise_set(conn, person_id, despues["source"], despues["source_id"], i,
                                     exercise=s.get("ejercicio"), category=s.get("categoria"),
                                     reps=s.get("reps"), weight_kg=s.get("peso_kg"), note=s.get("nota"))
    return None


def _escribir_deshacer(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    accion = row["accion"]
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
    if accion == "proponer_meta":
        goals.drop(conn, row["objetivo_id"])
    elif accion == "proponer_cambio_meta":
        db.update_goal_target(conn, row["objetivo_id"], person_id, target_value=antes["target_value"],
                               deadline=antes["deadline"])
    elif accion == "proponer_idioma":
        db.set_language(conn, antes["lang"], person_id)
    elif accion == "planear_entreno":
        plan_id = despues.get("plan_id")
        if plan_id is not None:
            with conn:
                conn.execute("DELETE FROM plan WHERE id = ? AND person_id = ?", (plan_id, person_id))
    elif accion == "registrar_entreno":
        if despues["manual"]:
            source_id = despues.get("source_id")
            if source_id is not None:
                with conn:
                    conn.execute(
                        "DELETE FROM workout_set WHERE person_id = ? AND activity_source = 'manual' "
                        "AND activity_source_id = ?", (person_id, source_id))
                    conn.execute(
                        "DELETE FROM activity WHERE person_id = ? AND source = 'manual' AND source_id = ?",
                        (person_id, source_id))
        else:
            source, source_id = despues["source"], despues["source_id"]
            with conn:
                for s in antes["sets_antes"]:
                    if s["existia"]:
                        conn.execute(
                            "UPDATE workout_set SET person_category = ?, person_exercise = ?, person_reps = ?, "
                            "person_weight_kg = ?, person_note = ?, corrected_by_person = ? "
                            "WHERE person_id = ? AND activity_source = ? AND activity_source_id = ? "
                            "AND set_order = ?",
                            (s["person_category"], s["person_exercise"], s["person_reps"], s["person_weight_kg"],
                             s["person_note"], int(s["corrected_by_person"]), person_id, source, source_id,
                             s["set_order"]),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM workout_set WHERE person_id = ? AND activity_source = ? AND "
                            "activity_source_id = ? AND set_order = ?",
                            (person_id, source, source_id, s["set_order"]),
                        )


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
    creado = _escribir_aplicacion(conn, row)
    if creado is not None:
        if "goal_id" in creado:
            with conn:
                conn.execute("UPDATE propuesta_agente SET objetivo_id = ? WHERE id = ?",
                             (creado["goal_id"], propuesta_id))
        else:
            nuevo_despues = json.loads(row["despues_json"])
            nuevo_despues.update(creado)
            with conn:
                conn.execute("UPDATE propuesta_agente SET despues_json = ? WHERE id = ?",
                             (json.dumps(nuevo_despues, ensure_ascii=False), propuesta_id))
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
