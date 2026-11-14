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

BODY-PROPUESTAS-TABLA-1: each action is declared once, in TABLA below, as an `_Accion` naming its
four callables (drift-on-apply, drift-on-undo, apply writer, undo writer). `aplicar` / `descartar`
/ `deshacer` look an action up in TABLA instead of walking four parallel if-chains, so every
action's four answers sit together and a missing one cannot compile silently.

`proponer_experimento` (BODY-EXPERIMENTOS-REGISTRO-1) is insert-shaped like `planear_entreno`: this
module is the one writer of `experimento`, through `db.crear_experimento` and, on undo,
`db.borrar_experimento` -- refused (by both the drift check and the writer itself) once any day
has been marked against it. `marcar_experimento` calls `db.registrar_dia_experimento` directly,
the same upsert-only writer shape as `marcar_habito`'s `goals.check_habit`: its undo, when there
was no prior `experimento_registro` row, is the same "restore no row at all" exception as
`marcar_habito`'s own undo, a direct DELETE on `experimento_registro`.

`marcar_periodo` (BODY-PERIODO-1) is insert-shaped like `registrar_ingesta`: this module is the
one writer of `period`, through `db.crear_periodo`/`db.borrar_periodo`. `cerrar_periodo` and
`corregir_periodo` call `db.actualizar_periodo` directly, the same update-only writer shape
`registrar_medida` uses on `daily_metric`. `borrar_periodo`'s undo re-inserts through
`db.crear_periodo` -- a new row with the same values, not the same id, the same "functionally
equivalent" restoration `borrar_ingesta`'s own undo uses.

`descartar_series` (BODY-SERIES-ORDEN-1) calls `db.set_series_excluida` directly, toggling
`workout_set.excluded_by_person` -- `correct_exercise_set` has no equivalent, since it only ever
moves a slot INTO "corrected". `registrar_entreno`'s own apply/undo (ruling 3) call the same
writer, for the watch's own active slots its spoken series left unmatched.

`separar_sesion` and `unir_sesiones` (BODY-ENTRENO-VINCULO-1 rulings 4, 5) call `db.set_vinculo_manual`
directly, the one writer of `activity`'s own `vinculo_manual`/`vinculo_separado` columns -- the pair
the automatic sync-time link (`db.save_day`) also writes through a narrower helper of its own.
`separar_sesion`'s apply clears the link and sets `vinculo_separado`, so a later sync never re-guesses
the same match; its undo restores the link and clears that flag. `unir_sesiones`'s apply sets the
link (clearing `vinculo_separado`, overriding any earlier split); its undo clears it back to plain
unlinked -- never `vinculo_separado`, since joining was never the same as a person-made split.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import comida_compartida, compartir, db, fechas, goals, household, shift_import
from ..i18n import T
from . import conversacion as agente_conversacion

EXPIRA_MINUTOS = 15
# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: a body.mcp proposal is relayed by the person's own
# personal assistant and often answered an hour or more later -- the 15-minute window that fits an
# in-app chat turn would silently lose it. It survives through the END of the person's own NEXT
# local day (the day it was made, plus the following one), never a fixed span of minutes.
# body/mcp.py reads this name instead of restating its own number.
EXPIRA_MCP_DIAS_SIGUIENTES = 1

_DriftFn = Callable[[sqlite3.Connection, sqlite3.Row], bool]
_AplicarFn = Callable[[sqlite3.Connection, sqlite3.Row], dict | None]
_DeshacerFn = Callable[[sqlite3.Connection, sqlite3.Row], None]


class PropuestaAjena(Exception):
    """A propuesta_id that exists but belongs to another person, or does not exist at all."""


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _fin_siguiente_dia_local(ahora: datetime, person: dict) -> datetime:
    """The UTC instant of local midnight at the START of the local day AFTER the one
    EXPIRA_MCP_DIAS_SIGUIENTES names (ruling 1): the end of the person's own NEXT local day,
    counted from `ahora`'s own local date in their timezone -- so a proposal made at any local
    time on day D stays applicable through all of day D+1, expiring only at the start of D+2."""
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    hoy = ahora.astimezone(tz).date()
    limite_local = datetime.combine(hoy + timedelta(days=EXPIRA_MCP_DIAS_SIGUIENTES + 1), time.min, tzinfo=tz)
    return limite_local.astimezone(timezone.utc)


def _row_a_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["antes_json"] = json.loads(d["antes_json"]) if d["antes_json"] is not None else None
    d["despues_json"] = json.loads(d["despues_json"])
    return d


def crear(conn: sqlite3.Connection, person_id: int, conversacion_id: int, accion: str, despues: dict, *,
          objetivo_id: int | None = None, antes: dict | None = None) -> dict:
    ahora = _ahora()
    if agente_conversacion.origen_de(conn, conversacion_id) == "mcp":
        expira = _fin_siguiente_dia_local(ahora, db.person(conn, person_id))
    else:
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
    """BODY-PLAN-PROPUESTAS-1 ruling 2: `tipo`/`habitos`/`horizonte_goal_id` are additive -- a
    pre-slice row carries none of them, so `.get()` here reproduces exactly today's metric-only
    write. `goals.propose` already accepts `kind='milestone'` (BODY-PLAN-ESQUEMA-1); this is still
    the one path that creates a goal, never a hand-written INSERT. `horizonte_goal_id` is set by a
    direct UPDATE after creation: `goals.propose` has no such parameter (goals.py is not this
    slice's to widen), the same kind of narrow exception `separar_sesion`'s own direct write on
    `activity` already takes above. Each habit's own id is returned (merged into despues_json by
    `aplicar`'s generic handling) so `_deshacer_meta` can deactivate exactly those, never every
    habit of the goal."""
    despues = json.loads(row["despues_json"])
    goal_id = goals.propose(conn, row["person_id"], despues["titulo"], despues["start_day"],
                             kind=despues.get("tipo", "metric"), metric_key=despues.get("metric_key"),
                             direction=despues.get("direction"), target_value=despues.get("target_value"),
                             deadline=despues["deadline"])
    horizonte_goal_id = despues.get("horizonte_goal_id")
    if horizonte_goal_id is not None:
        with conn:
            conn.execute("UPDATE goal SET horizonte_goal_id = ? WHERE id = ?", (horizonte_goal_id, goal_id))
    habito_ids = [goals.add_habit(conn, goal_id, h["titulo"], h["cadencia"]) for h in despues.get("habitos") or []]
    resultado = {"goal_id": goal_id}
    if habito_ids:
        resultado["habito_ids"] = habito_ids
    return resultado


def _deshacer_meta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    goals.drop(conn, row["objetivo_id"])
    habito_ids = despues.get("habito_ids") or []
    if habito_ids:
        placeholders = ",".join("?" * len(habito_ids))
        with conn:
            conn.execute(f"UPDATE habit SET active = 0 WHERE id IN ({placeholders})", habito_ids)


# -- proponer_meta / proponer_cambio_meta share one drift-on-undo check: both leave a `goal` row
# whose target_value/deadline must still match what was applied. proponer_meta's own habits
# (ruling 2) add a second condition: none of them may carry a habit_check yet, the same drift
# rule ruling 1's own undo uses for a plan's horizon-level habits -- proponer_cambio_meta's own
# despues_json never has "habito_ids", so this is a no-op for it.

def _drift_deshacer_meta(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    g = conn.execute("SELECT target_value, deadline FROM goal WHERE id = ? AND person_id = ?",
                     (row["objetivo_id"], row["person_id"])).fetchone()
    if g is None or g["target_value"] != despues["target_value"] or g["deadline"] != despues["deadline"]:
        return True
    habito_ids = despues.get("habito_ids") or []
    if habito_ids:
        placeholders = ",".join("?" * len(habito_ids))
        if conn.execute(f"SELECT 1 FROM habit_check WHERE habit_id IN ({placeholders})",
                        habito_ids).fetchone() is not None:
            return True
    return False


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

def _entreno_series_invalida(despues: dict) -> bool:
    """True when `despues['series']` holds a `reps`/`peso_kg` the door validation added by
    BODY-ENTRENO-NUMEROS-1 (ruling 2, `herramientas._numero_entreno`) would now refuse -- only
    possible on a row stored before this slice, since `_registrar_entreno` itself coerces or
    refuses every value before a proposal is ever created. Treated as drift so `aplicar` refuses
    it the same way it refuses any other out-of-date proposal: state stays `propuesta`, no write,
    no 500 (ruling 3)."""
    for s in despues["series"]:
        reps, peso = s.get("reps"), s.get("peso_kg")
        if reps is not None and (isinstance(reps, bool) or not isinstance(reps, int)):
            return True
        if peso is not None and (isinstance(peso, bool) or not isinstance(peso, (int, float))):
            return True
    return False


def _drift_aplicar_registrar_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """The manual case is insert-shaped and never drifts. The Garmin-matched case drifts when a
    later `pull`/`traer_series` changed the watch's own reading for a touched set. Either case
    drifts first when the stored series itself is no longer valid (`_entreno_series_invalida`)."""
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else {}
    despues = json.loads(row["despues_json"])
    if _entreno_series_invalida(despues):
        return True
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
    # BODY-SERIES-ORDEN-1 rulings 3, 4: the watch's own active slots left unmatched at propose
    # time must still be active, still uncorrected and still carry the excluded_by_person value
    # this proposal captured, or something changed them since.
    for ne in despues.get("no_emparejadas", ()):
        actual = actuales.get(ne["set_order"])
        if (actual is None or actual["corrected_by_person"] or actual["set_type"] != "active"
                or actual["excluded_by_person"] != ne["excluded_antes"]):
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
    for m in despues["mapeo"]:
        actual = next((a for a in actuales if a["set_order"] == m["set_order"]), None)
        s = m["tu"]
        if (actual is None or not actual["corrected_by_person"]
                or actual["exercise"] != s.get("ejercicio") or actual["reps"] != s.get("reps")):
            return True
    for ne in despues.get("no_emparejadas", ()):
        actual = next((a for a in actuales if a["set_order"] == ne["set_order"]), None)
        if actual is None or not actual["excluded_by_person"]:
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
    for m in despues["mapeo"]:
        s = m["tu"]
        db.correct_exercise_set(conn, person_id, despues["source"], despues["source_id"], m["set_order"],
                                 exercise=s.get("ejercicio"), category=s.get("categoria"),
                                 reps=s.get("reps"), weight_kg=s.get("peso_kg"), note=s.get("nota"))
    # BODY-SERIES-ORDEN-1 rulings 3, 4: the watch's own active slots this call never reached are
    # excluded alongside the correction -- never left to look like a missed set forever.
    for ne in despues.get("no_emparejadas", ()):
        db.set_series_excluida(conn, person_id, despues["source"], despues["source_id"], ne["set_order"], True)
    return None


def _deshacer_registrar_entreno(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """The manual case deletes the session it created. The Garmin-matched case restores each
    touched set's exact pre-correction state -- including turning `corrected_by_person` back to
    0 -- which `correct_exercise_set` cannot express (it only ever moves a set INTO "corrected"),
    so this is a direct UPDATE/DELETE on `workout_set`. The unmatched active slots `aplicar`
    excluded are restored to their own pre-propose `excluded_by_person` value the same way."""
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
        for ne in despues.get("no_emparejadas", ()):
            conn.execute(
                "UPDATE workout_set SET excluded_by_person = ? WHERE person_id = ? AND activity_source = ? "
                "AND activity_source_id = ? AND set_order = ?",
                (int(ne["excluded_antes"]), person_id, source, source_id, ne["set_order"]),
            )


# -- descartar_series (BODY-SERIES-ORDEN-1 ruling 4: this module writes workout_set.excluded_by_person
# directly, through db.set_series_excluida -- correct_exercise_set has no equivalent toggle) --------

def _drift_aplicar_descartar_series(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actuales = {s["set_order"]: s for s in db.exercise_sets(conn, row["person_id"], despues["source"],
                                                             despues["source_id"])}
    for entrada in despues["series"]:
        actual = actuales.get(entrada["set_order"])
        if (actual is None or actual["corrected_by_person"] or actual["set_type"] != "active"
                or actual["excluded_by_person"] != entrada["excluded_antes"]):
            return True
    return False


def _drift_deshacer_descartar_series(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Drifted if a slot `aplicar` excluded is no longer excluded -- something else changed it."""
    despues = json.loads(row["despues_json"])
    actuales = {s["set_order"]: s for s in db.exercise_sets(conn, row["person_id"], despues["source"],
                                                             despues["source_id"])}
    for entrada in despues["series"]:
        actual = actuales.get(entrada["set_order"])
        if actual is None or not actual["excluded_by_person"]:
            return True
    return False


def _aplicar_descartar_series(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    for entrada in despues["series"]:
        db.set_series_excluida(conn, row["person_id"], despues["source"], despues["source_id"],
                                entrada["set_order"], True)


def _deshacer_descartar_series(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    for entrada in despues["series"]:
        db.set_series_excluida(conn, row["person_id"], despues["source"], despues["source_id"],
                                entrada["set_order"], bool(entrada["excluded_antes"]))


# -- separar_sesion / unir_sesiones (BODY-ENTRENO-VINCULO-1 rulings 4, 5: this module calls
# db.set_vinculo_manual directly -- the one writer of activity's own link columns) ------------------

def _fila_reloj(conn: sqlite3.Connection, row: sqlite3.Row, despues: dict) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT vinculo_manual, vinculo_separado FROM activity WHERE person_id = ? AND source = ? "
        "AND source_id = ?", (row["person_id"], despues["source"], despues["source_id"]),
    ).fetchone()


def _drift_aplicar_separar_sesion(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    reloj = _fila_reloj(conn, row, despues)
    return reloj is None or reloj["vinculo_manual"] != despues["manual_source_id"]


def _drift_deshacer_separar_sesion(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    reloj = _fila_reloj(conn, row, despues)
    return reloj is None or reloj["vinculo_manual"] is not None or not reloj["vinculo_separado"]


def _aplicar_separar_sesion(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.set_vinculo_manual(conn, row["person_id"], despues["source"], despues["source_id"], None, True)


def _deshacer_separar_sesion(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.set_vinculo_manual(conn, row["person_id"], despues["source"], despues["source_id"],
                           despues["manual_source_id"], False)


def _drift_aplicar_unir_sesiones(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    reloj = _fila_reloj(conn, row, despues)
    if reloj is None or reloj["vinculo_manual"] is not None:
        return True
    otro = conn.execute(
        "SELECT 1 FROM activity WHERE person_id = ? AND vinculo_manual = ?",
        (row["person_id"], despues["manual_source_id"]),
    ).fetchone()
    return otro is not None


def _drift_deshacer_unir_sesiones(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    reloj = _fila_reloj(conn, row, despues)
    return reloj is None or reloj["vinculo_manual"] != despues["manual_source_id"]


def _aplicar_unir_sesiones(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.set_vinculo_manual(conn, row["person_id"], despues["source"], despues["source_id"],
                           despues["manual_source_id"], False)


def _deshacer_unir_sesiones(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.set_vinculo_manual(conn, row["person_id"], despues["source"], despues["source_id"], None, False)


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


# -- proponer_experimento (BODY-EXPERIMENTOS-REGISTRO-1: insert-shaped, like planear_entreno -- this
# module is the one writer of `experimento` through db.crear_experimento/db.borrar_experimento) ---

def _drift_deshacer_proponer_experimento(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Drifted if the row no longer matches what was applied, OR if a day has already been marked
    against it -- the same "no dependent row" condition `db.borrar_experimento` itself refuses on,
    checked here first so a genuine mismatch reads as the usual 'propuesta_desactualizada', never
    a raised exception."""
    despues = json.loads(row["despues_json"])
    eid = despues.get("experimento_id")
    e = conn.execute(
        "SELECT titulo, metricas, ventana, filtro_dias, fecha_inicio FROM experimento "
        "WHERE id = ? AND person_id = ?", (eid, row["person_id"])).fetchone()
    if e is None:
        return True
    if (e["titulo"] != despues["titulo"] or json.loads(e["metricas"]) != despues["metricas"]
            or json.loads(e["ventana"]) != despues["ventana"] or json.loads(e["filtro_dias"]) != despues["filtro_dias"]
            or e["fecha_inicio"] != despues["fecha_inicio"]):
        return True
    tiene_registro = conn.execute(
        "SELECT 1 FROM experimento_registro WHERE experimento_id = ?", (eid,)).fetchone()
    return tiene_registro is not None


def _aplicar_proponer_experimento(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    eid = db.crear_experimento(
        conn, row["person_id"], despues["titulo"], despues["metricas"], despues["ventana"], despues["filtro_dias"],
        habito_id=despues.get("habito_id"), dias_prueba_objetivo=despues.get("dias_prueba_objetivo"),
        fecha_inicio=despues["fecha_inicio"], goal_id=despues.get("horizonte_goal_id"),
    )
    return {"experimento_id": eid}


def _deshacer_proponer_experimento(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    eid = despues.get("experimento_id")
    if eid is not None:
        db.borrar_experimento(conn, row["person_id"], eid)


# -- marcar_experimento (this module calls db.registrar_dia_experimento directly) ---------------

def _drift_aplicar_marcar_experimento(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else None
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT ocurrio, nota FROM experimento_registro WHERE experimento_id = ? AND person_id = ? AND day = ?",
        (despues["experimento_id"], row["person_id"], despues["day"])).fetchone()
    if antes is None:
        return actual is not None
    return (actual is None or bool(actual["ocurrio"]) != antes.get("ocurrio") or actual["nota"] != antes.get("nota"))


def _drift_deshacer_marcar_experimento(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT ocurrio, nota FROM experimento_registro WHERE experimento_id = ? AND person_id = ? AND day = ?",
        (despues["experimento_id"], row["person_id"], despues["day"])).fetchone()
    return (actual is None or bool(actual["ocurrio"]) != despues["ocurrio"]
            or actual["nota"] != despues.get("nota"))


def _aplicar_marcar_experimento(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.registrar_dia_experimento(conn, despues["experimento_id"], row["person_id"], despues["day"],
                                  despues["ocurrio"], despues.get("nota"))


def _deshacer_marcar_experimento(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """When there was no prior registro row, restoring "no row at all" is not expressible through
    `db.registrar_dia_experimento` (which only ever upserts), so this is a direct DELETE -- the
    same exception `marcar_habito`'s own undo takes on `habit_check`."""
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else None
    despues = json.loads(row["despues_json"])
    with conn:
        if antes is None:
            conn.execute(
                "DELETE FROM experimento_registro WHERE experimento_id = ? AND person_id = ? AND day = ?",
                (despues["experimento_id"], row["person_id"], despues["day"]))
        else:
            conn.execute(
                "UPDATE experimento_registro SET ocurrio = ?, nota = ? "
                "WHERE experimento_id = ? AND person_id = ? AND day = ?",
                (1 if antes["ocurrio"] else 0, antes.get("nota"), despues["experimento_id"], row["person_id"],
                 despues["day"]))


# -- registrar_ingesta (BODY-PROTEINA-1: insert-shaped, like planear_entreno/proponer_experimento --
# this module is the one writer of `ingesta` through db.registrar_ingesta/db.borrar_ingesta) ------

def _drift_deshacer_registrar_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    iid = despues.get("ingesta_id")
    r = conn.execute(
        "SELECT amount, metric_key, day FROM ingesta WHERE id = ? AND person_id = ?",
        (iid, row["person_id"])).fetchone()
    return (r is None or r["amount"] != despues["amount"] or r["metric_key"] != despues["metric_key"]
            or r["day"] != despues["day"])


def _aplicar_registrar_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    iid = db.registrar_ingesta(conn, row["person_id"], despues["day"], despues["metric_key"], despues["amount"],
                                despues["estimado"], despues.get("descripcion"), despues.get("ts_local"))
    return {"ingesta_id": iid}


def _deshacer_registrar_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    iid = despues.get("ingesta_id")
    if iid is not None:
        db.borrar_ingesta(conn, row["person_id"], iid)


# -- corregir_ingesta (this module calls db.corregir_ingesta directly, update-shaped like
# registrar_medida) -------------------------------------------------------------------------------

def _drift_aplicar_corregir_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT amount FROM ingesta WHERE id = ? AND person_id = ?",
        (despues["ingesta_id"], row["person_id"])).fetchone()
    return actual is None or actual["amount"] != antes["amount"]


def _drift_deshacer_corregir_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT amount FROM ingesta WHERE id = ? AND person_id = ?",
        (despues["ingesta_id"], row["person_id"])).fetchone()
    return actual is None or actual["amount"] != despues["amount"]


def _aplicar_corregir_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.corregir_ingesta(conn, row["person_id"], despues["ingesta_id"], despues["amount"])


def _deshacer_corregir_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    db.corregir_ingesta(conn, row["person_id"], despues["ingesta_id"], antes["amount"])


# -- borrar_ingesta (this module calls db.borrar_ingesta directly; undo re-inserts through
# db.registrar_ingesta -- a new row with the same values, not the same id, the same "functionally
# equivalent" restoration proponer_experimento's own undo uses) -----------------------------------

def _drift_aplicar_borrar_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT amount, metric_key, day FROM ingesta WHERE id = ? AND person_id = ?",
        (despues["ingesta_id"], row["person_id"])).fetchone()
    if actual is None:
        return True
    return (actual["amount"] != antes["amount"] or actual["metric_key"] != antes["metric_key"]
            or actual["day"] != antes["day"])


def _drift_deshacer_borrar_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Drifted if the row is no longer gone -- something else re-created it since apply deleted it."""
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT 1 FROM ingesta WHERE id = ? AND person_id = ?",
        (despues["ingesta_id"], row["person_id"])).fetchone()
    return actual is not None


def _aplicar_borrar_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.borrar_ingesta(conn, row["person_id"], despues["ingesta_id"])


def _deshacer_borrar_ingesta(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"])
    db.registrar_ingesta(conn, row["person_id"], antes["day"], antes["metric_key"], antes["amount"],
                          antes["estimado"], antes.get("descripcion"), antes.get("ts_local"))


# -- registrar_comida (BODY-COMIDA-1: insert-shaped, like registrar_ingesta -- this module is the
# one writer of `comida` through db.crear_comida/db.borrar_comida; apply writes the `comida` row
# plus every one of its own `ingesta` children in one transaction, undo removes them together) ----

def _drift_deshacer_registrar_comida(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    cid = despues.get("comida_id")
    r = conn.execute(
        "SELECT descripcion, day FROM comida WHERE id = ? AND person_id = ?",
        (cid, row["person_id"])).fetchone()
    if r is None or r["descripcion"] != despues["descripcion"] or r["day"] != despues["day"]:
        return True
    cantidades_actuales = {c["metric_key"]: c["amount"] for c in conn.execute(
        "SELECT metric_key, amount FROM ingesta WHERE comida_id = ?", (cid,))}
    cantidades_despues = {c["metric_key"]: c["amount"] for c in despues["cantidades"]}
    return cantidades_actuales != cantidades_despues


def _drift_aplicar_registrar_comida(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """BODY-COMIDA-COMPARTIDA-1: the one case `registrar_comida` CAN drift before apply -- a
    `compartida_con` eater who left the household, or turned consent off, since the proposal was
    made. `_sin_drift`'s own reasoning ("insert-shaped, no value before") still holds for a plain
    meal (no `compartida_con`), so this is the only extra check."""
    despues = json.loads(row["despues_json"])
    compartida_con = despues.get("compartida_con")
    if not compartida_con:
        return False
    hh = household.household_of(conn, row["person_id"])
    if hh is None:
        return True
    miembros = set(hh.member_ids)
    return any(c["person_id"] not in miembros or not compartir.recibe_comidas(conn, c["person_id"])
               for c in compartida_con)


def _aplicar_registrar_comida(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    compartida_con = despues.get("compartida_con")
    if compartida_con:
        comensales = [{"person_id": row["person_id"],
                       "nutrientes": {c["metric_key"]: c["amount"] for c in despues["cantidades"]}}]
        comensales += [{"person_id": c["person_id"], "nutrientes": c["nutrientes"],
                         "porcion_texto": c.get("porcion_texto")} for c in compartida_con]
        resultado = comida_compartida.compartir_comida(
            conn, row["person_id"], day=despues["day"], ts_local=despues.get("ts_local"),
            descripcion=despues.get("descripcion"), comensales=comensales, origen="body",
            origen_ref=str(row["id"]))
        autor_porcion = conn.execute(
            "SELECT comida_id FROM comida_compartida_porcion WHERE comida_compartida_id = ? AND person_id = ?",
            (resultado["comida_compartida_id"], row["person_id"]),
        ).fetchone()
        return {"comida_id": autor_porcion["comida_id"], "comida_compartida_id": resultado["comida_compartida_id"]}
    cid = db.crear_comida(conn, row["person_id"], despues["day"], despues["cantidades"],
                           despues.get("descripcion"), despues.get("ts_local"))
    return {"comida_id": cid}


def _deshacer_registrar_comida(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    ccid = despues.get("comida_compartida_id")
    if ccid is not None:
        # BODY-COMIDA-COMPARTIDA-1 ruling 4: removes the author's own comida and discards every
        # still-pending portion of this same meal -- an already-applied portion of another eater is
        # theirs and is left exactly as it is.
        comida_compartida.deshacer_autor(conn, row["person_id"], ccid)
        return
    cid = despues.get("comida_id")
    if cid is not None:
        db.borrar_comida(conn, row["person_id"], cid)


# -- corregir_comida (BODY-COMIDA-CORREGIR-1: this module calls db.corregir_comida directly,
# update-shaped like corregir_periodo -- `antes`/`despues` each carry the meal's FULL state (day,
# ts_local, descripcion, every amount), so apply writes `despues` and undo restores `antes`,
# through the SAME writer, symmetrically. Not in ACCIONES_REEMPLAZABLES: two corrections of two
# different meals on the same day are both real, never a same-day replacement.) -------------------

def _comida_estado_coincide(conn: sqlite3.Connection, person_id: int, comida_id: int, estado: dict) -> bool:
    fila = conn.execute("SELECT day, ts_local, descripcion FROM comida WHERE id = ? AND person_id = ?",
                        (comida_id, person_id)).fetchone()
    if fila is None:
        return False
    if (fila["day"] != estado["day"] or fila["ts_local"] != estado.get("ts_local")
            or fila["descripcion"] != estado.get("descripcion")):
        return False
    cantidades_actuales = {r["metric_key"]: r["amount"] for r in conn.execute(
        "SELECT metric_key, amount FROM ingesta WHERE comida_id = ?", (comida_id,))}
    cantidades_esperadas = {c["metric_key"]: c["amount"] for c in estado.get("cantidades", [])}
    return cantidades_actuales == cantidades_esperadas


def _drift_aplicar_corregir_comida(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    return not _comida_estado_coincide(conn, row["person_id"], despues["comida_id"], antes)


def _drift_deshacer_corregir_comida(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    return not _comida_estado_coincide(conn, row["person_id"], despues["comida_id"], despues)


def _aplicar_corregir_comida(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.corregir_comida(conn, row["person_id"], despues["comida_id"], ts_local=despues.get("ts_local"),
                       day=despues["day"], descripcion=despues["descripcion"], cantidades=despues["cantidades"])


def _deshacer_corregir_comida(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    db.corregir_comida(conn, row["person_id"], despues["comida_id"], ts_local=antes.get("ts_local"),
                       day=antes["day"], descripcion=antes["descripcion"], cantidades=antes["cantidades"])


# -- registrar_checkin (BODY-CHECKIN-1: insert-shaped, like registrar_comida -- this module is the
# one writer of `checkin` through db.crear_checkin/db.borrar_checkin, which write/remove the note
# (`checkin_nota`) in the SAME transaction, ruling 3) -------------------------------------------

def _drift_deshacer_registrar_checkin(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    checkin_id = despues.get("checkin_id")
    r = conn.execute(
        "SELECT day, momento, mood, energy, stress_felt FROM checkin WHERE id = ? AND person_id = ?",
        (checkin_id, row["person_id"])).fetchone()
    if r is None:
        return True
    actual = (r["day"], r["momento"], r["mood"], r["energy"], r["stress_felt"])
    esperado = (despues["day"], despues["momento"], despues.get("mood"), despues.get("energy"),
                despues.get("stress_felt"))
    if actual != esperado:
        return True
    nota_row = conn.execute("SELECT nota FROM checkin_nota WHERE id = ?", (checkin_id,)).fetchone()
    return (nota_row["nota"] if nota_row else None) != despues.get("nota")


def _aplicar_registrar_checkin(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    checkin_id = db.crear_checkin(conn, row["person_id"], despues["day"], despues["momento"],
                                   ts_local=despues.get("ts_local"), mood=despues.get("mood"),
                                   energy=despues.get("energy"), stress_felt=despues.get("stress_felt"),
                                   nota=despues.get("nota"))
    return {"checkin_id": checkin_id}


def _deshacer_registrar_checkin(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    checkin_id = despues.get("checkin_id")
    if checkin_id is not None:
        db.borrar_checkin(conn, row["person_id"], checkin_id)


# -- marcar_periodo (BODY-PERIODO-1: insert-shaped, like registrar_ingesta -- this module is the
# one writer of `period` through db.crear_periodo/db.borrar_periodo) -------------------------------

def _drift_deshacer_marcar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    pid = despues.get("period_id")
    r = conn.execute(
        "SELECT kind, start_day, end_day FROM period WHERE id = ? AND person_id = ?",
        (pid, row["person_id"])).fetchone()
    return (r is None or r["kind"] != despues["kind"] or r["start_day"] != despues["start_day"]
            or r["end_day"] != despues.get("end_day"))


def _aplicar_marcar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    pid = db.crear_periodo(conn, row["person_id"], despues["kind"], despues["start_day"],
                            despues.get("end_day"), despues.get("note"), despues.get("zonas"))
    return {"period_id": pid}


def _deshacer_marcar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    pid = despues.get("period_id")
    if pid is not None:
        db.borrar_periodo(conn, row["person_id"], pid)


# -- cerrar_periodo (this module calls db.actualizar_periodo directly, update-shaped like
# registrar_medida) -----------------------------------------------------------------------------

def _drift_aplicar_cerrar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = conn.execute("SELECT end_day FROM period WHERE id = ? AND person_id = ?",
                          (despues["period_id"], row["person_id"])).fetchone()
    return actual is None or actual["end_day"] is not None


def _drift_deshacer_cerrar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = conn.execute("SELECT end_day FROM period WHERE id = ? AND person_id = ?",
                          (despues["period_id"], row["person_id"])).fetchone()
    return actual is None or actual["end_day"] != despues["end_day"]


def _aplicar_cerrar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.actualizar_periodo(conn, row["person_id"], despues["period_id"], end_day=despues["end_day"])


def _deshacer_cerrar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.actualizar_periodo(conn, row["person_id"], despues["period_id"], end_day=None)


# -- corregir_periodo (this module calls db.actualizar_periodo directly) ------------------------

def _zonas_de(fila: sqlite3.Row) -> list | None:
    """BODY-LESION-ZONA-1: a `period` row's own zonas, parsed from its stored JSON text, so a
    drift check can compare it against the plain list antes_json/despues_json already carries."""
    return json.loads(fila["zonas"]) if fila["zonas"] else None


def _drift_aplicar_corregir_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    actual = conn.execute("SELECT kind, start_day, end_day, note, zonas FROM period WHERE id = ? AND person_id = ?",
                          (despues["period_id"], row["person_id"])).fetchone()
    return (actual is None or actual["kind"] != antes["kind"] or actual["start_day"] != antes["start_day"]
            or actual["end_day"] != antes["end_day"] or actual["note"] != antes["note"]
            or _zonas_de(actual) != antes.get("zonas"))


def _drift_deshacer_corregir_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = conn.execute("SELECT kind, start_day, end_day, note, zonas FROM period WHERE id = ? AND person_id = ?",
                          (despues["period_id"], row["person_id"])).fetchone()
    return (actual is None or actual["kind"] != despues["kind"] or actual["start_day"] != despues["start_day"]
            or actual["end_day"] != despues["end_day"] or actual["note"] != despues["note"]
            or _zonas_de(actual) != despues.get("zonas"))


def _aplicar_corregir_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.actualizar_periodo(conn, row["person_id"], despues["period_id"], kind=despues["kind"],
                          start_day=despues["start_day"], end_day=despues["end_day"], note=despues["note"],
                          zonas=despues.get("zonas"))


def _deshacer_corregir_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    db.actualizar_periodo(conn, row["person_id"], despues["period_id"], kind=antes["kind"],
                          start_day=antes["start_day"], end_day=antes["end_day"], note=antes["note"],
                          zonas=antes.get("zonas"))


# -- borrar_periodo (this module calls db.borrar_periodo directly; undo re-inserts through
# db.crear_periodo -- a new row with the same values, not the same id, the same "functionally
# equivalent" restoration borrar_ingesta's own undo uses) -------------------------------------------

def _drift_aplicar_borrar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    actual = conn.execute("SELECT kind, start_day, end_day, note, zonas FROM period WHERE id = ? AND person_id = ?",
                          (despues["period_id"], row["person_id"])).fetchone()
    if actual is None:
        return True
    return (actual["kind"] != antes["kind"] or actual["start_day"] != antes["start_day"]
            or actual["end_day"] != antes["end_day"] or actual["note"] != antes["note"]
            or _zonas_de(actual) != antes.get("zonas"))


def _drift_deshacer_borrar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Drifted if the row is no longer gone -- something else re-created it since apply deleted it."""
    despues = json.loads(row["despues_json"])
    actual = conn.execute("SELECT 1 FROM period WHERE id = ? AND person_id = ?",
                          (despues["period_id"], row["person_id"])).fetchone()
    return actual is not None


def _aplicar_borrar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    db.borrar_periodo(conn, row["person_id"], despues["period_id"])


def _deshacer_borrar_periodo(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"])
    db.crear_periodo(conn, row["person_id"], antes["kind"], antes["start_day"], antes["end_day"], antes["note"],
                     antes.get("zonas"))


# -- proponer_objetivo_nutriente (BODY-NUTRIENTE-OBJETIVO-1: this module calls
# db.set_nutrient_target/db.borrar_nutrient_target directly, update-shaped like registrar_medida on
# daily_metric -- a target's own primary key (person_id, metric_key, start_day) is the slot being
# set, never a new id) --------------------------------------------------------------------------

def _fila_nutriente(conn: sqlite3.Connection, row: sqlite3.Row, despues: dict) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT kind, amount, sentido FROM nutrient_target WHERE person_id = ? AND metric_key = ? "
        "AND start_day = ?", (row["person_id"], despues["metric_key"], despues["start_day"])).fetchone()


def _drift_aplicar_objetivo_nutriente(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else None
    despues = json.loads(row["despues_json"])
    actual = _fila_nutriente(conn, row, despues)
    actual_tup = (actual["kind"], actual["amount"], actual["sentido"]) if actual is not None else None
    antes_tup = (antes["kind"], antes["amount"], antes["sentido"]) if antes is not None else None
    return actual_tup != antes_tup


def _drift_deshacer_objetivo_nutriente(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = _fila_nutriente(conn, row, despues)
    actual_tup = (actual["kind"], actual["amount"], actual["sentido"]) if actual is not None else None
    return actual_tup != (despues["kind"], despues["amount"], despues["sentido"])


def _aplicar_objetivo_nutriente(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    with conn:
        db.set_nutrient_target(conn, row["person_id"], despues["metric_key"], despues["start_day"],
                                despues["kind"], despues["amount"], despues["sentido"])


def _deshacer_objetivo_nutriente(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    antes = json.loads(row["antes_json"]) if row["antes_json"] is not None else None
    despues = json.loads(row["despues_json"])
    with conn:
        if antes is None:
            db.borrar_nutrient_target(conn, row["person_id"], despues["metric_key"], despues["start_day"])
        else:
            db.set_nutrient_target(conn, row["person_id"], despues["metric_key"], despues["start_day"],
                                    antes["kind"], antes["amount"], antes["sentido"])


# -- proponer_plan (BODY-PLAN-PROPUESTAS-1 ruling 1: insert-shaped, like planear_entreno -- this
# module is the one writer of a whole plan, through db.crear_plan_horizonte/a direct multi-table
# delete+status-drop below; the horizon and every part it created are found by querying
# horizonte_goal_id / goal_id, never by trusting a hand-kept list) --------------------------------

def _plan_hijos_ids(conn: sqlite3.Connection, horizonte_goal_id: int) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM goal WHERE horizonte_goal_id = ?", (horizonte_goal_id,))]


def _plan_habito_ids(conn: sqlite3.Connection, horizonte_goal_id: int) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM habit WHERE goal_id = ?", (horizonte_goal_id,))]


def _aplicar_proponer_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    goal_id = db.crear_plan_horizonte(conn, row["person_id"], despues)
    return {"goal_id": goal_id}


def _drift_deshacer_proponer_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Drifted -- REFUSES the undo -- the instant anything was recorded against this plan since
    apply: a correction (every correction, including ampliar_plan's own additions, appends a row,
    so the row COUNT for each child table no longer matches despues_json's own counts), a habit
    check on one of the horizon's own habits, a linked plan session or experiment, or a goal_check
    on the horizon or any of its child goals. A person's later record is never deleted by an undo,
    so this check runs before anything else does."""
    despues = json.loads(row["despues_json"])
    horizonte_goal_id = row["objetivo_id"]
    if conn.execute("SELECT 1 FROM goal WHERE id = ?", (horizonte_goal_id,)).fetchone() is None:
        return True
    hijos = _plan_hijos_ids(conn, horizonte_goal_id)
    if len(hijos) != len(despues.get("metas_finales") or []):
        return True
    habitos = _plan_habito_ids(conn, horizonte_goal_id)
    if len(habitos) != len(despues.get("habitos") or []):
        return True
    for tabla, clave in (("horizonte_regla", "reglas"), ("horizonte_semana", "semanas"),
                         ("horizonte_meta_semanal", "metas_semanales"),
                         ("horizonte_checkpoint", "checkpoints")):
        n = conn.execute(f"SELECT COUNT(*) FROM {tabla} WHERE horizonte_goal_id = ?",
                         (horizonte_goal_id,)).fetchone()[0]
        if n != len(despues.get(clave) or []):
            return True
    if conn.execute("SELECT 1 FROM plan WHERE goal_id = ?", (horizonte_goal_id,)).fetchone() is not None:
        return True
    if conn.execute("SELECT 1 FROM experimento WHERE goal_id = ?", (horizonte_goal_id,)).fetchone() is not None:
        return True
    if habitos:
        placeholders = ",".join("?" * len(habitos))
        if conn.execute(f"SELECT 1 FROM habit_check WHERE habit_id IN ({placeholders})",
                        habitos).fetchone() is not None:
            return True
    ids_goal_check = [horizonte_goal_id] + hijos
    placeholders = ",".join("?" * len(ids_goal_check))
    if conn.execute(f"SELECT 1 FROM goal_check WHERE goal_id IN ({placeholders})",
                    ids_goal_check).fetchone() is not None:
        return True
    return False


def _deshacer_proponer_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """The horizon and its child goals follow `_deshacer_meta`'s own precedent (goals.drop, soft);
    the habits it created go `active = 0`; the horizonte_* rows it inserted are deleted outright
    (they carry no status of their own). The drift check above already proved nothing else touched
    any of this, so a plain delete/update by horizonte_goal_id is exactly what this proposal wrote,
    all in one transaction."""
    horizonte_goal_id = row["objetivo_id"]
    hijos = _plan_hijos_ids(conn, horizonte_goal_id)
    habitos = _plan_habito_ids(conn, horizonte_goal_id)
    with conn:
        for tabla in ("horizonte_regla", "horizonte_semana", "horizonte_meta_semanal", "horizonte_checkpoint"):
            conn.execute(f"DELETE FROM {tabla} WHERE horizonte_goal_id = ?", (horizonte_goal_id,))
        if habitos:
            placeholders = ",".join("?" * len(habitos))
            conn.execute(f"UPDATE habit SET active = 0 WHERE id IN ({placeholders})", habitos)
        for hijo_id in hijos:
            conn.execute("UPDATE goal SET status = 'dropped' WHERE id = ?", (hijo_id,))
        conn.execute("UPDATE goal SET status = 'dropped' WHERE id = ?", (horizonte_goal_id,))


# -- corregir_meta_plan (BODY-PLAN-PROPUESTAS-1 ruling 3: this module calls db.corregir_meta_semanal
# directly, append-only like Finanzas' own presupuesto revision -- nothing here ever UPDATEs valor)

def _drift_aplicar_corregir_meta_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    antes = json.loads(row["antes_json"])
    actual = conn.execute("SELECT valor, vigente FROM horizonte_meta_semanal WHERE id = ? AND person_id = ?",
                          (antes["meta_id"], row["person_id"])).fetchone()
    return actual is None or not actual["vigente"] or actual["valor"] != antes["valor"]


def _aplicar_corregir_meta_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    nuevo_id = db.corregir_meta_semanal(conn, row["person_id"], despues["meta_id"], despues["valor"],
                                        sentido=despues["sentido"], nota=despues.get("nota"), origen="asistente")
    return {"meta_semanal_id": nuevo_id}


def _drift_deshacer_corregir_meta_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    actual = conn.execute(
        "SELECT valor, vigente FROM horizonte_meta_semanal WHERE id = ? AND person_id = ?",
        (despues.get("meta_semanal_id"), row["person_id"])).fetchone()
    return actual is None or not actual["vigente"] or actual["valor"] != despues["valor"]


def _deshacer_corregir_meta_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Undo is ANOTHER APPEND, never a rewrite: a third row restoring the old valor, replacing the
    correction (reemplaza_id -> the correction's own id) -- the history reads in order and nothing
    already written is ever changed."""
    antes = json.loads(row["antes_json"])
    despues = json.loads(row["despues_json"])
    db.corregir_meta_semanal(conn, row["person_id"], despues["meta_semanal_id"], antes["valor"],
                              sentido=antes["sentido"], nota=antes.get("nota"), origen="asistente")


# -- ampliar_plan (BODY-PLAN-PROPUESTAS-1 ruling 4: insert-only growth of an existing horizon,
# through db.ampliar_plan_horizonte -- undo deletes EXACTLY the rows that call returned, never a
# row the original plan or a different ampliar_plan call wrote) -----------------------------------

def _aplicar_ampliar_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    despues = json.loads(row["despues_json"])
    return db.ampliar_plan_horizonte(conn, row["person_id"], despues["goal_id"], despues)


def _drift_deshacer_ampliar_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    despues = json.loads(row["despues_json"])
    for tabla, clave in (("horizonte_regla", "regla_ids"), ("horizonte_semana", "semana_ids"),
                         ("horizonte_checkpoint", "checkpoint_ids")):
        for rid in despues.get(clave) or []:
            if conn.execute(f"SELECT 1 FROM {tabla} WHERE id = ?", (rid,)).fetchone() is None:
                return True
    # A weekly target this call inserted must still be vigente -- a later correction replacing it
    # is drift, the same rule corregir_meta_plan's own undo follows.
    for rid in despues.get("meta_semanal_ids") or []:
        fila = conn.execute("SELECT vigente FROM horizonte_meta_semanal WHERE id = ?", (rid,)).fetchone()
        if fila is None or not fila["vigente"]:
            return True
    return False


def _deshacer_ampliar_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    despues = json.loads(row["despues_json"])
    with conn:
        for tabla, clave in (("horizonte_regla", "regla_ids"), ("horizonte_semana", "semana_ids"),
                             ("horizonte_meta_semanal", "meta_semanal_ids"),
                             ("horizonte_checkpoint", "checkpoint_ids")):
            ids = despues.get(clave) or []
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM {tabla} WHERE id IN ({placeholders})", ids)


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
    "proponer_experimento": _Accion(_sin_drift, _drift_deshacer_proponer_experimento,
                                     _aplicar_proponer_experimento, _deshacer_proponer_experimento),
    "marcar_experimento": _Accion(_drift_aplicar_marcar_experimento, _drift_deshacer_marcar_experimento,
                                   _aplicar_marcar_experimento, _deshacer_marcar_experimento),
    "registrar_ingesta": _Accion(_sin_drift, _drift_deshacer_registrar_ingesta, _aplicar_registrar_ingesta,
                                  _deshacer_registrar_ingesta),
    "corregir_ingesta": _Accion(_drift_aplicar_corregir_ingesta, _drift_deshacer_corregir_ingesta,
                                 _aplicar_corregir_ingesta, _deshacer_corregir_ingesta),
    "borrar_ingesta": _Accion(_drift_aplicar_borrar_ingesta, _drift_deshacer_borrar_ingesta,
                               _aplicar_borrar_ingesta, _deshacer_borrar_ingesta),
    "registrar_comida": _Accion(_drift_aplicar_registrar_comida, _drift_deshacer_registrar_comida,
                                 _aplicar_registrar_comida, _deshacer_registrar_comida),
    "corregir_comida": _Accion(_drift_aplicar_corregir_comida, _drift_deshacer_corregir_comida,
                                _aplicar_corregir_comida, _deshacer_corregir_comida),
    "registrar_checkin": _Accion(_sin_drift, _drift_deshacer_registrar_checkin, _aplicar_registrar_checkin,
                                  _deshacer_registrar_checkin),
    "marcar_periodo": _Accion(_sin_drift, _drift_deshacer_marcar_periodo, _aplicar_marcar_periodo,
                               _deshacer_marcar_periodo),
    "cerrar_periodo": _Accion(_drift_aplicar_cerrar_periodo, _drift_deshacer_cerrar_periodo,
                               _aplicar_cerrar_periodo, _deshacer_cerrar_periodo),
    "corregir_periodo": _Accion(_drift_aplicar_corregir_periodo, _drift_deshacer_corregir_periodo,
                                 _aplicar_corregir_periodo, _deshacer_corregir_periodo),
    "borrar_periodo": _Accion(_drift_aplicar_borrar_periodo, _drift_deshacer_borrar_periodo,
                               _aplicar_borrar_periodo, _deshacer_borrar_periodo),
    "descartar_series": _Accion(_drift_aplicar_descartar_series, _drift_deshacer_descartar_series,
                                 _aplicar_descartar_series, _deshacer_descartar_series),
    "separar_sesion": _Accion(_drift_aplicar_separar_sesion, _drift_deshacer_separar_sesion,
                               _aplicar_separar_sesion, _deshacer_separar_sesion),
    "unir_sesiones": _Accion(_drift_aplicar_unir_sesiones, _drift_deshacer_unir_sesiones,
                              _aplicar_unir_sesiones, _deshacer_unir_sesiones),
    "proponer_objetivo_nutriente": _Accion(_drift_aplicar_objetivo_nutriente, _drift_deshacer_objetivo_nutriente,
                                            _aplicar_objetivo_nutriente, _deshacer_objetivo_nutriente),
    "proponer_plan": _Accion(_sin_drift, _drift_deshacer_proponer_plan, _aplicar_proponer_plan,
                              _deshacer_proponer_plan),
    "corregir_meta_plan": _Accion(_drift_aplicar_corregir_meta_plan, _drift_deshacer_corregir_meta_plan,
                                   _aplicar_corregir_meta_plan, _deshacer_corregir_meta_plan),
    "ampliar_plan": _Accion(_sin_drift, _drift_deshacer_ampliar_plan, _aplicar_ampliar_plan,
                             _deshacer_ampliar_plan),
}

SIN_DESHACER = tuple(nombre for nombre, entrada in TABLA.items() if not entrada.tiene_deshacer)


# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: when one of these four is applied, every other
# still-pending proposal of the SAME tool for the SAME day is replaced by it. Reuses the existing
# 'descartada' terminal state rather than widening the `estado` CHECK (a new value there is a
# schema change) -- the replaced row's own despues_json (free-form, only CHECKed for json_valid)
# gains a 'reemplazada' marker so the card (body/rutas/asistente.py) can tell a replacement apart
# from a plain discard.
ACCIONES_REEMPLAZABLES = frozenset({"registrar_entreno", "registrar_comida", "registrar_ingesta",
                                     "registrar_checkin"})


def _reemplazar_pendientes_mismo_dia(conn: sqlite3.Connection, person_id: int, accion: str, day: str,
                                      propuesta_id_aplicada: int, ahora_iso: str) -> None:
    pendientes = conn.execute(
        "SELECT id, despues_json FROM propuesta_agente WHERE person_id = ? AND accion = ? "
        "AND estado = 'propuesta' AND id != ?", (person_id, accion, propuesta_id_aplicada),
    ).fetchall()
    for p in pendientes:
        despues = json.loads(p["despues_json"])
        if despues.get("day") != day:
            continue
        if _marcar(conn, p["id"], person_id, "propuesta", "descartada", ahora_iso):
            despues["reemplazada"] = True
            with conn:
                conn.execute("UPDATE propuesta_agente SET despues_json = ? WHERE id = ?",
                             (json.dumps(despues, ensure_ascii=False), p["id"]))


def aplicar(conn: sqlite3.Connection, person_id: int, propuesta_id: int) -> dict:
    row = _fila_o_ajena(conn, person_id, propuesta_id)
    if row["estado"] != "propuesta":
        return {"estado": "ya_resuelta"}
    ahora_iso = _iso(_ahora())
    if _vencida(row, ahora_iso):
        if _marcar(conn, propuesta_id, person_id, "propuesta", "expirada", ahora_iso):
            conn.commit()
            # Ruling 4: the honest "it expired, and when" message (body/mcp.py's own
            # aplicar_propuesta) needs the stamp that was already on this row -- never a second
            # lookup after the state has moved on.
            return {"estado": "expirada", "expira_en": row["expira_en"]}
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
        # BODY-PLAN-PROPUESTAS-1: any OTHER key `creado` carries (alongside "goal_id" or on its
        # own) is merged into despues_json -- generalised from an either/or so `_aplicar_meta`
        # can return both a goal_id (recorded as objetivo_id, above) AND the ids of the habits it
        # created (merged here, for `_deshacer_meta` to deactivate). Every existing action returns
        # EITHER "goal_id" alone OR some other key(s) alone, never both, so this is additive: their
        # own behaviour is unchanged.
        resto = {k: v for k, v in creado.items() if k != "goal_id"}
        if resto:
            nuevo_despues = json.loads(row["despues_json"])
            nuevo_despues.update(resto)
            with conn:
                conn.execute("UPDATE propuesta_agente SET despues_json = ? WHERE id = ?",
                             (json.dumps(nuevo_despues, ensure_ascii=False), propuesta_id))
    if row["accion"] in ACCIONES_REEMPLAZABLES:
        _reemplazar_pendientes_mismo_dia(conn, person_id, row["accion"], json.loads(row["despues_json"])["day"],
                                          propuesta_id, ahora_iso)
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
