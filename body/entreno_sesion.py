"""Per-session detail (BODY-ENTRENO-VISTA-SESION-1): one activity's own figures against the
person's own history, its strength sets with the rest between them, and -- only on a day the
day's per-minute heart-rate series is already stored and consented -- the curve across the
session with each set's window and each rest marked. Pure reads over a connection and a person
id, no web imports, no t() -- body/rutas/entreno_sesion.py formats and renders.

BODY-MINUTO-A-MINUTO-1-INV found: `activity` keeps a summary only, `workout_set` keeps each
strength set with Garmin's own timing and the person's corrections, and the day's per-minute
HR/stress series is fetched only on an experiment day. This module reads exactly those three
things, through the functions body/db.py already has -- no new db function, no schema change.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from . import db

# Ruling 2: the median of the same type's last 10 OTHER sessions -- fewer than this many of those
# carrying a given figure means no comparison for it, an honest gap rather than a guess.
HISTORIAL_N = 10
HISTORIAL_MIN = 3
COMPARABLE_KEYS = ("avg_hr", "max_hr", "calories")


def actividad(conn, person_id: int, day: str, source_id: str) -> dict | None:
    """This one activity, read the same merged way Día's own list already folds a linked pair
    (BODY-ENTRENO-VINCULO-1 ruling 3) -- `source_id` is always the merged (watch) identity for a
    linked pair, the same id Día's own row carries. None for another person's id, or a day/
    source_id this person has no activity under (ruling 1's 404)."""
    filas = db.merge_linked_training(conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day = ? ORDER BY start_local",
        (person_id, day)).fetchall())
    for f in filas:
        if f["source_id"] == source_id:
            return dict(f)
    return None


def _fin_local(start_local: str | None, duration_s: float | None) -> datetime | None:
    if not start_local or duration_s is None:
        return None
    return datetime.fromisoformat(start_local) + timedelta(seconds=duration_s)


def comparaciones(conn, person_id: int, a: dict) -> dict:
    """Ruling 2: {"n": how many of the last 10 other sessions of this type were found (capped at
    HISTORIAL_N), "valores": {key: median or None}} -- the SAME 10 sessions back every key, each
    counting only the sessions among them that actually carry that figure. {} outright for an
    activity with no type (a manual session the watch never recorded one for)."""
    tipo = a.get("type")
    if not tipo:
        return {"n": 0, "valores": {k: None for k in COMPARABLE_KEYS}}
    filas = conn.execute(
        "SELECT avg_hr, max_hr, calories FROM activity WHERE person_id = ? AND type = ? AND "
        "NOT (source = ? AND source_id = ?) ORDER BY day DESC, start_local DESC LIMIT ?",
        (person_id, tipo, a["source"], a["source_id"], HISTORIAL_N)).fetchall()
    valores = {}
    for k in COMPARABLE_KEYS:
        vals = [f[k] for f in filas if f[k] is not None]
        valores[k] = statistics.median(vals) if len(vals) >= HISTORIAL_MIN else None
    return {"n": len(filas), "valores": valores}


def series(conn, person_id: int, sets_source: str, sets_source_id: str) -> list[dict]:
    """Ruling 3: this activity's own sets in order -- active slots only (a watch 'rest' slot is
    never a list item of its own; the gap between active slots is computed instead), each with
    `rest_after_s`: the next slot's own start minus this slot's own end, None on a negative or
    missing value, or on the last slot (no next one to rest before)."""
    activos = [s for s in db.exercise_sets(conn, person_id, sets_source, sets_source_id)
               if s["set_type"] == "active"]
    out = []
    for i, s in enumerate(activos):
        rest_after_s = None
        if i + 1 < len(activos):
            fin = _fin_local(s["start_local"], s["duration_s"])
            siguiente_inicio = activos[i + 1]["start_local"]
            if fin is not None and siguiente_inicio:
                rest_after_s = (datetime.fromisoformat(siguiente_inicio) - fin).total_seconds()
                if rest_after_s < 0:
                    rest_after_s = None
        out.append({**s, "rest_after_s": rest_after_s})
    return out


def curva(conn, person_id: int, day: str, zona: str, inicio_local: str | None,
          duracion_s: float | None, sets: list[dict]) -> dict | None:
    """Ruling 4: the day's own per-minute heart-rate series, sliced to the session window, with
    each active set's own peak and each rest's own HR drop (its value at the start of the rest
    minus its value at the end) -- one forward pass over the sliced, time-sorted series against
    the session's own chronologically-sorted set/rest windows (ruling 7). None when the day's
    series is not stored at all, or none of it falls inside this session's own window -- the
    caller then shows the honest "coming soon" line, never an empty chart."""
    if not inicio_local or duracion_s is None:
        return None
    serie = db.intradia(conn, person_id, day, "intradia_fc", zona)
    if not serie:
        return None
    ventana_inicio = datetime.fromisoformat(inicio_local)
    ventana_fin = ventana_inicio + timedelta(seconds=duracion_s)
    puntos = sorted((datetime.fromisoformat(t), v) for t, v in serie
                    if ventana_inicio <= datetime.fromisoformat(t) <= ventana_fin)
    if not puntos:
        return None

    # Chronological windows this session's own sets and rests cover -- a "set" window keyed by its
    # own set_order (for the peak), a "rest" window keyed by the PRECEDING set's order (for the
    # drop, the same key `series()` already uses for rest_after_s).
    ventanas = []
    for s in sets:
        inicio_s = datetime.fromisoformat(s["start_local"]) if s["start_local"] else None
        fin_s = _fin_local(s["start_local"], s["duration_s"])
        if inicio_s is not None and fin_s is not None:
            ventanas.append((inicio_s, fin_s, "set", s["set_order"]))
        if s["rest_after_s"] is not None and fin_s is not None:
            fin_r = fin_s + timedelta(seconds=s["rest_after_s"])
            ventanas.append((fin_s, fin_r, "rest", s["set_order"]))
    ventanas.sort(key=lambda v: v[0])

    picos: dict[int, float] = {}
    descanso_inicio: dict[int, float] = {}
    descanso_fin: dict[int, float] = {}
    descanso_n: dict[int, int] = {}
    idx = 0
    for t, v in puntos:
        while idx + 1 < len(ventanas) and ventanas[idx][1] < t:
            idx += 1
        if idx < len(ventanas):
            inicio_v, fin_v, tipo, orden = ventanas[idx]
            if inicio_v <= t <= fin_v:
                if tipo == "set":
                    if orden not in picos or v > picos[orden]:
                        picos[orden] = v
                else:
                    descanso_fin[orden] = v
                    descanso_n[orden] = descanso_n.get(orden, 0) + 1
                    if orden not in descanso_inicio:
                        descanso_inicio[orden] = v

    # A drop needs two DIFFERENT samples inside the same rest (a start and an end) -- a rest this
    # sparse a series only ever catches once gives no honest "before" to compare against, so it is
    # left out here rather than reported as a flat (and misleading) zero.
    caidas = {orden: (descanso_inicio[orden] - descanso_fin[orden]) for orden in descanso_inicio
              if descanso_n[orden] >= 2}
    return {
        "puntos": puntos, "inicio": ventana_inicio, "fin": ventana_fin,
        "min": min(v for _, v in puntos), "max": max(v for _, v in puntos),
        "picos": picos, "caidas": caidas,
    }
