"""Reads of "the previous session's same exercise's reps/weight" (BODY-ENTRENO-ULTIMA-VEZ-1), needed
before the Session screen's "last time -> today" comparisons (BODY-METAS-SESION-PANTALLA-1) and used
by the assistant's `leer_entreno` (body/agente/herramientas.py). Reads only: plain SELECTs plus
`db.exercise_sets` for display values -- never a second copy of the correction/kg conversion logic.

Ruling 1: the exercise identity is the effective exercise of a set -- `person_exercise` when the
person corrected the slot, else `garmin_exercise` -- compared as stored, never translated. This is
exactly what `db.exercise_sets` already computes as a row's own `exercise` field, so every function
here reads that field rather than re-deriving it. Only 'active', non-excluded sets count (`_cuenta`).
"""
from __future__ import annotations

import sqlite3

from . import db


def _cuenta(s: dict) -> bool:
    """One set counts toward a "last time" when it is an actual worked set: 'active' and not
    marked by the person as not having happened (BODY-SERIES-ORDEN-1 rulings 1, 3)."""
    return s["set_type"] == "active" and not s["excluded_by_person"]


def _resumen(filas: list[dict]) -> dict:
    """The display shape for one exercise's sets in one session: `sets` in set order, and `mejor`,
    the heaviest one (ties -> more reps). A bodyweight exercise (every `weight_kg` is None) compares
    by reps instead -- the same sort key does both: a set with a real weight always outranks one
    without, and only among equally-weighted sets (including "none", the bodyweight case) does reps
    decide."""
    series = [{"reps": r["reps"], "weight_kg": r["weight_value"], "corrected": r["corrected_by_person"]}
              for r in filas]
    mejor = max(series, key=lambda s: (s["weight_kg"] is not None, s["weight_kg"] or 0, s["reps"] or 0))
    return {"sets": series, "mejor": mejor}


def ultima_vez(conn: sqlite3.Connection, person_id: int, ejercicio: str, *, antes_de: str | None = None) -> dict | None:
    """The most recent session (by the activity's local start, strictly before `antes_de` -- a
    local ISO datetime or date -- when given) with at least one counting set whose effective
    exercise is exactly `ejercicio`. None when there is none. Two statements: the search below (the
    effective-exercise CASE mirrors `db.exercise_sets`'s own corrected-or-watch rule, so the search
    and the display value can never disagree), then `db.exercise_sets` on the session it finds."""
    fila = conn.execute(
        "SELECT a.source AS source, a.source_id AS source_id, a.day AS day "
        "FROM workout_set w JOIN activity a ON a.person_id = w.person_id "
        "AND a.source = w.activity_source AND a.source_id = w.activity_source_id "
        "WHERE w.person_id = ? AND w.set_type = 'active' AND w.excluded_by_person = 0 "
        "AND (CASE WHEN w.corrected_by_person THEN w.person_exercise ELSE w.garmin_exercise END) = ? "
        "AND (? IS NULL OR COALESCE(a.start_local, a.day) < ?) "
        "ORDER BY COALESCE(a.start_local, a.day) DESC LIMIT 1",
        (person_id, ejercicio, antes_de, antes_de),
    ).fetchone()
    if fila is None:
        return None
    propias = [s for s in db.exercise_sets(conn, person_id, fila["source"], fila["source_id"])
               if _cuenta(s) and s["exercise"] == ejercicio]
    if not propias:
        return None
    return {"source": fila["source"], "source_id": fila["source_id"], "day": fila["day"], **_resumen(propias)}


def ultimas_veces(conn: sqlite3.Connection, person_id: int, source: str, source_id: str) -> dict[str, dict | None]:
    """`ultima_vez` before THIS session's own start, for every exercise this session has
    (BODY-ENTRENO-ULTIMA-VEZ-1 ruling 2). A bounded number of statements independent of the number
    of exercises: one query for this session's own sets, one for its own start, one for the
    candidate earlier sessions (each already known to hold at least one counting set), then
    `db.exercise_sets` only for the earlier sessions actually opened -- opening stops the moment
    every exercise has been found, so a session that already covers every exercise this time costs
    one extra statement no matter how many exercises there are."""
    propias = db.exercise_sets(conn, person_id, source, source_id)
    necesarios = {s["exercise"] for s in propias if _cuenta(s)}
    if not necesarios:
        return {}
    fila = conn.execute(
        "SELECT day, start_local FROM activity WHERE person_id = ? AND source = ? AND source_id = ?",
        (person_id, source, source_id),
    ).fetchone()
    out: dict[str, dict | None] = {nombre: None for nombre in necesarios}
    if fila is None:
        return out
    limite = fila["start_local"] if fila["start_local"] is not None else fila["day"]
    pendientes = set(necesarios)
    candidatos = conn.execute(
        "SELECT DISTINCT a.source AS source, a.source_id AS source_id, a.day AS day "
        "FROM workout_set w JOIN activity a ON a.person_id = w.person_id "
        "AND a.source = w.activity_source AND a.source_id = w.activity_source_id "
        "WHERE w.person_id = ? AND w.set_type = 'active' AND w.excluded_by_person = 0 "
        "AND NOT (a.source = ? AND a.source_id = ?) "
        "AND COALESCE(a.start_local, a.day) < ? "
        "ORDER BY COALESCE(a.start_local, a.day) DESC",
        (person_id, source, source_id, limite),
    ).fetchall()
    for c in candidatos:
        if not pendientes:
            break
        anteriores = [s for s in db.exercise_sets(conn, person_id, c["source"], c["source_id"]) if _cuenta(s)]
        por_ejercicio: dict[str, list] = {}
        for s in anteriores:
            por_ejercicio.setdefault(s["exercise"], []).append(s)
        for nombre in list(pendientes):
            filas = por_ejercicio.get(nombre)
            if filas:
                out[nombre] = {"source": c["source"], "source_id": c["source_id"], "day": c["day"], **_resumen(filas)}
                pendientes.discard(nombre)
    return out
