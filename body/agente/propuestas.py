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
direct UPDATE/DELETE on `workout_set` here. `registrar_medida` (BODY-ENTRENO-MEDIDA-1) calls
`db.set_metric` directly, with `source='self'` -- the same writer body/demo.py's self-measured
seeding and body/readiness.py's own score already use; no screen writes a self-measured figure yet.

Only a `propuesta`-kind tool function may import this module (the static guard in
tests/test_agente_registro.py): a `lectura` tool never reaches a writer, and this is the one route
a proposal tool has to one. `marcar_habito` (BODY-ENTRENO-HABITO-AUTO-1) calls `goals.check_habit`
directly, the same writer body/rutas/metas.py's own habit-check route uses; its `deshacer`, when
there was no prior habit_check row, is a narrow exception like `registrar_entreno`'s own:
restoring "no row at all" is not expressible through `check_habit` (which only ever upserts), so
that one restoration is a direct DELETE on `habit_check` here.

`aplicar`'s conditional UPDATE and the write it triggers share ONE transaction: the UPDATE opens it
(sqlite3's implicit-transaction mode) and the write function's own `with conn:` commits it, so a
crash between them leaves nothing applied. The one exception is `proponer_meta`'s `objetivo_id`
bookkeeping (recording the goal `propose` just created, so a later `deshacer` knows what to
remove), which happens in a short follow-up write after that commit -- a narrow, documented gap:
undo becomes unavailable for that one proposal if the process crashes in that exact instant, never
silent data loss.

BODY-PROPUESTAS-TABLA-1: the eight actions are declared once each, in TABLA below, as an `_Accion`
naming its four callables (drift-on-apply, drift-on-undo, apply writer, undo writer). `aplicar` /
`descartar` / `deshacer` look an action up in TABLA instead of walking four parallel if-chains, so
every action's four answers sit together and a missing one cannot compile silently.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import db, fechas, goals, shift_import
from ..i18n import T

EXPIRA_MINUTOS = 15

_DriftFn = Callable[[sqlite3.Connection, sqlite3.Row], bool]
_AplicarFn = Callable[[sqlite3.Connection, sqlite3.Row], dict | None]
_DeshacerFn = Callable[[sqlite3.Connection, sqlite3.Row], None]


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


def _sin_drift(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """The explicit no-op drift check, named rather than a missing branch: `proponer_meta` and
    `planear_entreno` are insert-shaped and have no value before (amendment 1a), so their
    drift-on-apply never fires; `proponer_confirmar_turnos` has no undo writer at all (below), so
    its drift-on-undo slot is filled but never actually called."""
    return False


def _sin_escritura_deshacer(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """The explicit marker for an action with no undo writer (ex-SIN_DESHACER). `deshacer` never
    calls this -- `_Accion.tiene_deshacer` is False for any entry that names it as its undo
    writer, and `deshacer` returns "sin_deshacer" before reaching the call."""
    return None


@dataclass(frozen=True)
class _Accion:
    """One action's four answers: whether the target drifted since the proposal was made (on
    apply, on undo), and the writer that applies or undoes it. An apply writer that creates a new
    row returns `{"goal_id": ...}`, `{"plan_id": ...}` or `{"source_id": ...}` for `aplicar` to
    record back onto the proposal (so a later `deshacer` can find what it must remove); any other
    writer returns None."""
    drift_aplicar: _DriftFn
    drift_deshacer: _DriftFn
    escribir_aplicacion: _AplicarFn
    escribir_deshacer: _DeshacerFn

    @property
    def tiene_deshacer(self) -> bool:
        return self.escribir_deshacer is not _sin_escritura_deshacer


# -- proponer_meta ------------------------------------------------------------------------------

def _aplicar_meta(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    goal_id = goals.propose(conn, row["person_id"], despues["titulo"], despues["start_day"],
                             metric_key=despues["metric_key"], direction=despues["direction"],
                             target_value=despues["target_value"], deadline=despues["deadline"])
    return {"goal_id": goal_id}


def _deshacer_meta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    goals.drop(conn, row["objetivo_id"])


# -- proponer_meta / proponer_cambio_meta share one drift-on-undo check: both leave a `goal` row
# whose target_value/deadline must still match what was applied.

def _drift_deshacer_meta(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    g = conn.execute("SELECT target_value, deadline FROM goal WHERE id = ? AND person_id = ?",
                     (row["objetivo_id"], row["person_id"])).fetchone()
    return g is None or g["target_value"] != despues["target_value"] or g["deadline"] != despues["deadline"]


# -- proponer_cambio_meta -----------------------------------------------------------------------

def _drift_aplicar_cambio_meta(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"])
    g = conn.execute("SELECT target_value, deadline FROM goal WHERE id = ? AND person_id = ?",
                     (row["objetivo_id"], row["person_id"])).fetchone()
    return g is None or g["target_value"] != antes.get("target_value") or g["deadline"] != antes.get("deadline")


def _aplicar_cambio_meta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.update_goal_target(conn, row["objetivo_id"], row["person_id"], target_value=despues["target_value"],
                           deadline=despues["deadline"])


def _deshacer_cambio_meta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"])
    db.update_goal_target(conn, row["objetivo_id"], row["person_id"], target_value=antes["target_value"],
                           deadline=antes["deadline"])


# -- proponer_confirmar_turnos (SIN_DESHACER: no undo writer) ----------------------------------

def _drift_aplicar_confirmar_turnos(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    return shift_import.summary_proposed(conn, row["person_id"]) != antes.get("resumen")


def _aplicar_confirmar_turnos(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    shift_import.confirm_all(conn, row["person_id"])


# -- proponer_idioma -----------------------------------------------------------------------------

def _drift_aplicar_idioma(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    return db.person(conn, row["person_id"])["language"] != antes.get("lang")


def _drift_deshacer_idioma(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    return db.person(conn, row["person_id"])["language"] != despues["lang"]


def _aplicar_idioma(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.set_language(conn, despues["lang"], row["person_id"])


def _deshacer_idioma(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"])
    db.set_language(conn, antes["lang"], row["person_id"])


# -- planear_entreno (BODY-ENTRENO-TOOLS-1: this module is the one writer of `plan`) ------------

def _drift_deshacer_planear_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    plan_id = despues.get("plan_id")
    p = conn.execute("SELECT ejercicios_json FROM plan WHERE id = ? AND person_id = ?",
                     (plan_id, row["person_id"])).fetchone()
    return p is None or p["ejercicios_json"] != json.dumps(despues["ejercicios"], ensure_ascii=False)


def _aplicar_planear_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    with conn:
        cur = conn.execute(
            "INSERT INTO plan (person_id, goal_id, day, time, title, status, ejercicios_json) "
            "VALUES (?, ?, ?, ?, ?, 'proposed', ?)",
            (row["person_id"], despues.get("goal_id"), despues["day"], despues.get("time"), despues["titulo"],
             json.dumps(despues["ejercicios"], ensure_ascii=False)),
        )
    return {"plan_id": cur.lastrowid}


def _deshacer_planear_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    plan_id = despues.get("plan_id")
    if plan_id is not None:
        with conn:
            conn.execute("DELETE FROM plan WHERE id = ? AND person_id = ?", (plan_id, row["person_id"]))


# -- registrar_entreno (manual session, or a correction of an existing Garmin-matched one) ------

def _drift_aplicar_registrar_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """The manual case is insert-shaped and never drifts. The Garmin-matched case drifts when a
    later `pull`/`traer_series` changed the watch's own reading for a touched set."""
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
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
                    or actual["garmin"]["category"] != snap["garmin_category"]
                    or ("garmin_weight_unit" in snap
                        and actual["garmin"]["weight_unit"] != snap["garmin_weight_unit"])):
                return True
        elif actual is not None:
            return True
    return False


def _drift_deshacer_registrar_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
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


def _aplicar_registrar_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> dict | None:
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
    if despues["manual"]:
        titulo = despues.get("titulo")
        if titulo is None:
            lang = db.person(conn, person_id)["language"]
            titulo = T(lang)("entreno.titulo_por_defecto")
        source_id = db.create_manual_session(
            conn, person_id, despues["day"],
            f"{despues['day']}T{despues['time']}:00" if despues.get("time") else None,
            titulo,
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


def _deshacer_registrar_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """The manual case deletes the session it created. The Garmin-matched case restores each
    touched set's exact pre-correction state -- including turning `corrected_by_person` back to
    0 -- which `correct_exercise_set` cannot express (it only ever moves a set INTO "corrected"),
    so this is a direct UPDATE/DELETE on `workout_set`."""
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
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
        return
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


# -- registrar_medida (BODY-ENTRENO-MEDIDA-1: this module calls db.set_metric with source='self') -

def _drift_aplicar_registrar_medida(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT value FROM daily_metric WHERE person_id = ? AND day = ? AND metric_key = ?",
        (row["person_id"], despues["day"], despues["metric_key"]),
    ).fetchone()
    return (actual["value"] if actual else None) != antes.get("value")


def _drift_deshacer_registrar_medida(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT value FROM daily_metric WHERE person_id = ? AND day = ? AND metric_key = ?",
        (row["person_id"], despues["day"], despues["metric_key"]),
    ).fetchone()
    return (actual["value"] if actual else None) != despues["value"]


def _aplicar_registrar_medida(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    with conn:
        db.set_metric(conn, despues["day"], despues["metric_key"], despues["value"], "self", row["person_id"])


def _deshacer_registrar_medida(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    despues = json.loads(row["despues_json"])
    with conn:
        db.set_metric(conn, despues["day"], despues["metric_key"], antes.get("value"), "self", row["person_id"])


# -- marcar_habito (BODY-ENTRENO-HABITO-AUTO-1: this module calls goals.check_habit directly) ---

def _drift_aplicar_marcar_habito(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT done, note FROM habit_check WHERE habit_id = ? AND person_id = ? AND day = ?",
        (despues["habit_id"], row["person_id"], despues["day"]),
    ).fetchone()
    if row["antes_json"] is None:
        return actual is not None
    return actual is None or bool(actual["done"]) != antes.get("done") or actual["note"] != antes.get("note")


def _drift_deshacer_marcar_habito(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT done, note FROM habit_check WHERE habit_id = ? AND person_id = ? AND day = ?",
        (despues["habit_id"], row["person_id"], despues["day"]),
    ).fetchone()
    return actual is None or not actual["done"] or actual["note"] != despues.get("note")


def _aplicar_marcar_habito(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
    hoy = fechas.hoy_local(db.person(conn, person_id))
    goals.check_habit(conn, despues["habit_id"], person_id, despues["day"], True, despues.get("note"), hoy=hoy)


def _deshacer_marcar_habito(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """When there was no prior habit_check row, restoring "no row at all" is not expressible
    through `check_habit` (which only ever upserts), so this is a direct DELETE."""
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    despues = json.loads(row["despues_json"])
    person_id = row["person_id"]
    with conn:
        if not antes:
            conn.execute("DELETE FROM habit_check WHERE habit_id = ? AND person_id = ? AND day = ?",
                         (despues["habit_id"], person_id, despues["day"]))
        else:
            conn.execute(
                "UPDATE habit_check SET done = ?, note = ? WHERE habit_id = ? AND person_id = ? AND day = ?",
                (1 if antes["done"] else 0, antes.get("note"), despues["habit_id"], person_id, despues["day"]))


TABLA: dict[str, _Accion] = {
    "proponer_meta": _Accion(_sin_drift, _drift_deshacer_meta, _aplicar_meta, _deshacer_meta),
    "proponer_cambio_meta": _Accion(_drift_aplicar_cambio_meta, _drift_deshacer_meta, _aplicar_cambio_meta,
                                     _deshacer_cambio_meta),
    "proponer_confirmar_turnos": _Accion(_drift_aplicar_confirmar_turnos, _sin_drift, _aplicar_confirmar_turnos,
                                          _sin_escritura_deshacer),
    "proponer_idioma": _Accion(_drift_aplicar_idioma, _drift_deshacer_idioma, _aplicar_idioma, _deshacer_idioma),
    "planear_entreno": _Accion(_sin_drift, _drift_deshacer_planear_entreno, _aplicar_planear_entreno,
                                _deshacer_planear_entreno),
    "registrar_entreno": _Accion(_drift_aplicar_registrar_entreno, _drift_deshacer_registrar_entreno,
                                  _aplicar_registrar_entreno, _deshacer_registrar_entreno),
    "registrar_medida": _Accion(_drift_aplicar_registrar_medida, _drift_deshacer_registrar_medida,
                                 _aplicar_registrar_medida, _deshacer_registrar_medida),
    "marcar_habito": _Accion(_drift_aplicar_marcar_habito, _drift_deshacer_marcar_habito, _aplicar_marcar_habito,
                              _deshacer_marcar_habito),
}

SIN_DESHACER = tuple(nombre for nombre, entrada in TABLA.items() if not entrada.tiene_deshacer)


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
    entrada = TABLA[row["accion"]]
    if entrada.drift_aplicar(conn, row):
        conn.rollback()
        return {"estado": "propuesta_desactualizada"}
    if not _marcar(conn, propuesta_id, person_id, "propuesta", "aplicada", ahora_iso):
        conn.rollback()
        return {"estado": "ya_resuelta"}
    creado = entrada.escribir_aplicacion(conn, row)
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
    entrada = TABLA[row["accion"]]
    if not entrada.tiene_deshacer:
        return {"estado": "sin_deshacer"}
    if row["estado"] != "aplicada":
        return {"estado": "ya_resuelta"}
    if entrada.drift_deshacer(conn, row):
        return {"estado": "propuesta_desactualizada"}
    ahora_iso = _iso(_ahora())
    if not _marcar(conn, propuesta_id, person_id, "aplicada", "deshecha", ahora_iso):
        conn.rollback()
        return {"estado": "ya_resuelta"}
    entrada.escribir_deshacer(conn, row)
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
