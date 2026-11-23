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
import unicodedata
from datetime import datetime, timedelta

from . import db

# Ruling 2: the median of the same type's last 10 OTHER sessions -- fewer than this many of those
# carrying a given figure means no comparison for it, an honest gap rather than a guess.
HISTORIAL_N = 10
HISTORIAL_MIN = 3
# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: how far back `sesiones_anteriores` reads before it picks,
# per figure, the newest HISTORIAL_N of those that actually carry it -- bounds the read so a sparse
# figure never forces a full-history scan. esfuerzo_sesion.py reads this same constant.
HISTORIAL_CANDIDATAS = 60
COMPARABLE_KEYS = ("avg_hr", "max_hr", "calories")

# BODY-ENTRENO-VISTA-SESION-1-C ruling 4: Garmin's own "other" type is itself a known, labelled
# bucket (activity.type.other -- "Otros"/"Other"), not the generic-code fallback (activity.generic,
# a raw type body/history.py's KNOWN_ACTIVITY_TYPES has never heard of). A session typed "other"
# compares only against the person's OTHER sessions that carry the SAME name.
TIPO_GENERICO = "other"


def _normaliza_nombre(nombre: str | None) -> str:
    """Case- and accent-insensitive fold for ruling 4's "same name" grouping -- NFKD strips each
    accent to a separate combining mark, which the ascii encode then drops."""
    if not nombre:
        return ""
    return unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().casefold()


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


def _es_anterior(a: dict, f) -> bool:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: `f` started strictly before `a` -- by `start_local`
    when BOTH carry one, by `day` when either side lacks it (never the session itself and never a
    later one)."""
    if a.get("start_local") and f["start_local"]:
        return f["start_local"] < a["start_local"]
    return f["day"] < a["day"]


def sesiones_anteriores(conn, person_id: int, a: dict, columnas: str) -> list:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 rulings 1/2: the newest HISTORIAL_CANDIDATAS same-kind
    sessions (ruling 4: Garmin's generic "other" bucket groups by the same folded name instead of
    type) that started before `a` (`_es_anterior`) -- the ONE selection `comparaciones` below and
    esfuerzo_sesion.historial both read, so the two pools can never drift apart (tests/
    test_esfuerzo_sesion.py proves it). `columnas` is the caller's own SELECT list, besides the
    day/start_local (and, for the generic bucket, name) every caller needs for the "earlier"
    filter -- `day <= a's own day` bounds the SQL read itself, since a session's `day` always
    matches its own `start_local`'s local date."""
    tipo = a.get("type")
    if not tipo:
        return []
    if tipo == TIPO_GENERICO:
        nombre_norm = _normaliza_nombre(a.get("name"))
        candidatas = conn.execute(
            f"SELECT {columnas}, name, day, start_local FROM activity WHERE person_id = ? AND type = ? "
            "AND NOT (source = ? AND source_id = ?) AND day <= ? ORDER BY day DESC, start_local DESC",
            (person_id, tipo, a["source"], a["source_id"], a["day"])).fetchall()
        filas = [f for f in candidatas if _normaliza_nombre(f["name"]) == nombre_norm and _es_anterior(a, f)]
    else:
        candidatas = conn.execute(
            f"SELECT {columnas}, day, start_local FROM activity WHERE person_id = ? AND type = ? "
            "AND NOT (source = ? AND source_id = ?) AND day <= ? ORDER BY day DESC, start_local DESC",
            (person_id, tipo, a["source"], a["source_id"], a["day"])).fetchall()
        filas = [f for f in candidatas if _es_anterior(a, f)]
    return filas[:HISTORIAL_CANDIDATAS]


def comparaciones(conn, person_id: int, a: dict) -> dict:
    """Ruling 2: {"n": {key: how many of the earlier same-kind sessions actually carry that figure,
    capped at HISTORIAL_N}, "valores": {key: median or None}} -- per figure, the newest HISTORIAL_N
    EARLIER (sesiones_anteriores) sessions that carry it, so a data-less session never crowds out
    one with data. {} shaped outright for an activity with no type (a manual session the watch
    never recorded one for).

    Ruling 4: when `a`'s own type is Garmin's generic "other" bucket, the pool is drawn from the
    person's "other" sessions that carry the SAME name (case/accent-insensitive) -- "other" alone
    groups unrelated sports (a basketball game with a yoga class) that happen to share no real
    type code, which the median would otherwise blend into a meaningless figure."""
    if not a.get("type"):
        return {"n": {k: 0 for k in COMPARABLE_KEYS}, "valores": {k: None for k in COMPARABLE_KEYS}}
    filas = sesiones_anteriores(conn, person_id, a, "avg_hr, max_hr, calories")
    valores, n = {}, {}
    for k in COMPARABLE_KEYS:
        vals = [f[k] for f in filas if f[k] is not None][:HISTORIAL_N]
        n[k] = len(vals)
        valores[k] = statistics.median(vals) if len(vals) >= HISTORIAL_MIN else None
    return {"n": n, "valores": valores}


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


def ejercicios_con_zona_abierta(sets: list[dict], zonas_dia: frozenset[str]) -> list[dict]:
    """BODY-PLAN-FASES-5 ruling 2: this session's own exercises (active sets the person never
    excluded) whose categoria falls inside `zonas_dia` -- db.zonas_en_recuperacion's own open-
    period zonas for this session's day -- each named once, in the order its own exercise first
    appears. [] outright when none match: no open zona period, or none of this session's own
    categorias fall inside it, changes nothing on the screen (ruling 2's own "data, never a list
    of 'pressing' names")."""
    out = []
    seen = set()
    for s in sets:
        ejercicio, categoria = s["exercise"], s["category"]
        if ejercicio and categoria in zonas_dia and ejercicio not in seen and not s["excluded_by_person"]:
            seen.add(ejercicio)
            out.append({"ejercicio": ejercicio, "zona": categoria})
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
