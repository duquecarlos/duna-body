"""BODY-REGISTRO-COBERTURA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1): the CAPTURE side of a dated plan.
Carlos, relayed by the desk 2026-11-19 (the "Greece 2027 log" track): a dated plan's log must be
complete by its date -- sleep (from the watch), meals with protein, a weekly weight and waist,
workouts. Photos are his own call and NOT part of this slice. The desk keeps its own nudge
schedule (Body has no push channel); it asks only for what Body says is MISSING, never for
everything.

Built GENERIC (docs/DESIGN.md rules 4, 5, 12): a dated plan's log coverage, never Greece-hardcoded,
never one person's schedule. A dated plan is the milestone goal body/horizonte.py already models
(`horizontes`, `resumen`, `fases` -- never edited here, only called). This module adds nothing to
that model; it only counts, against the plan's own date range, how many of each log stream's own
expected units are present.

Every stream's cadence and unit is a VERSIONED MODEL (`MODELO`), the reasoning beside each value,
the same convention body/reentrada.py and body/noche_inusual.py already keep for their own
thresholds -- never a constant named for one person. No weekday is hardcoded for a weekly stream:
`fechas.lunes_de` groups by ISO week, and a week is covered by any reading inside it, whichever day
it fell on.

Ruling (bounded cost): `cobertura` reads each stream with exactly one ranged query (`entreno` reads
two -- `activity` plus `horizonte._planes_hechos_fuerza`'s own one -- since a done plan session is
a second, separate source of the same fact) plus one small EXISTS query per stream for the
"ever used at all" check below. Never one statement per day: a day/week is matched in Python
against the set of day-strings the ranged query already returned. The whole range is walked once,
so `cobertura`'s own statement count does not grow with how many days or weeks it spans
(tests/test_rendimiento.py's own cost-independent-of-span convention).

A stream whose source the person has NEVER used at all (no row of that kind, ever, in their whole
history) is reported `sin_uso` rather than as a run of misses: the desk must never nag about a
stream that was never part of this person's routine. `sin_uso` is its own EXISTS query per stream,
unbounded by date range on purpose -- it asks "ever", not "in this window".
"""
from __future__ import annotations

from datetime import date, timedelta

from . import db, fechas, horizonte

VERSION = "registro-cobertura-v1"

# Every log stream the CAPTURE side of a dated plan can be asked about, its cadence and its own
# reason -- never training's photos (Carlos's own call, pending, out of this slice) and never a
# weekday fixed to any one stream.
MODELO = {
    "version": VERSION,
    "cadencia": {
        # One chance per night: the watch either filed a sleep_session for it or it did not.
        "sueno": "nightly",
        # At least one spoken/logged meal that calendar day -- not a nutrient, just "did you log
        # eating that day at all".
        "comidas": "daily",
        # Protein is its own stream (not folded into "comidas"): a meal can be logged with no
        # protein amount attached, and the Greece track is specifically about protein.
        "proteina": "daily",
        # Self-measured, so there is no daily expectation -- a once-a-week reading is already the
        # product's own cadence for this figure (BODY-MEDIDA-PESO-1-M).
        "peso": "weekly",
        "cintura": "weekly",
        # A training week is met by ANY session that week, watch-recorded or a plan row marked
        # done -- never a fixed day, never a fixed count.
        "entreno": "weekly",
    },
    # The log unit counted for each stream -- documentation only, read by the ledger entry and this
    # module's own docstrings; `_FUENTES` below is what actually runs.
    "unidad": {
        "sueno": "sleep_session (noche)",
        "comidas": "fila comida (dia)",
        "proteina": "fila ingesta metric_key='protein_g' (dia)",
        "peso": "lectura daily_metric body_weight_kg (dentro de la semana ISO)",
        "cintura": "lectura daily_metric waist_cm (dentro de la semana ISO)",
        "entreno": "fila activity, o sesion de plan con status='done' (dentro de la semana ISO)",
    },
}

STREAMS = tuple(MODELO["cadencia"])  # the six stream keys, in the order `cobertura`/`pendientes` report them

# How many missing dates/week-starts `cobertura` lists per stream, newest first -- plenty for a
# spot check (the desk's own nudge, or a person scrolling a long-running plan's own screen later),
# never the full history of a plan that spans a year.
FALTAN_LIMITE = 12


def _dias_cubiertos(conn, person_id: int, tabla_sql: str, params: tuple, d0: str, d1: str) -> set[str]:
    return {r["day"] for r in conn.execute(tabla_sql, params + (d0, d1))}


def _cubiertos_sueno(conn, person_id: int, d0: str, d1: str) -> set[str]:
    return _dias_cubiertos(
        conn, person_id, "SELECT day FROM sleep_session WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id,), d0, d1)


def _cubiertos_comidas(conn, person_id: int, d0: str, d1: str) -> set[str]:
    return _dias_cubiertos(
        conn, person_id, "SELECT DISTINCT day FROM comida WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id,), d0, d1)


def _cubiertos_proteina(conn, person_id: int, d0: str, d1: str) -> set[str]:
    return _dias_cubiertos(
        conn, person_id,
        "SELECT DISTINCT day FROM ingesta WHERE person_id = ? AND metric_key = 'protein_g' "
        "AND day BETWEEN ? AND ?",
        (person_id,), d0, d1)


def _cubiertos_peso(conn, person_id: int, d0: str, d1: str) -> set[str]:
    # tests/test_sueno_corregido.py ruling 2: every daily_metric FIGURE read goes through the view,
    # never the bare table -- harmless here (its CASE only ever touches 'sleep_minutes') but this
    # keeps the one census clean rather than adding a second allow-list entry for it.
    return _dias_cubiertos(
        conn, person_id,
        "SELECT DISTINCT day FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = 'body_weight_kg' "
        "AND day BETWEEN ? AND ?",
        (person_id,), d0, d1)


def _cubiertos_cintura(conn, person_id: int, d0: str, d1: str) -> set[str]:
    return _dias_cubiertos(
        conn, person_id,
        "SELECT DISTINCT day FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = 'waist_cm' "
        "AND day BETWEEN ? AND ?",
        (person_id,), d0, d1)


def _cubiertos_entreno(conn, person_id: int, d0: str, d1: str) -> set[str]:
    dias = _dias_cubiertos(
        conn, person_id, "SELECT DISTINCT day FROM activity WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id,), d0, d1)
    dias |= {d.isoformat() for d in
             horizonte._planes_hechos_fuerza(conn, person_id, date.fromisoformat(d0), date.fromisoformat(d1))}
    return dias


_FUENTES = {
    "sueno": _cubiertos_sueno,
    "comidas": _cubiertos_comidas,
    "proteina": _cubiertos_proteina,
    "peso": _cubiertos_peso,
    "cintura": _cubiertos_cintura,
    "entreno": _cubiertos_entreno,
}


def _nunca_sueno(conn, person_id: int) -> bool:
    return not conn.execute("SELECT EXISTS(SELECT 1 FROM sleep_session WHERE person_id = ?)",
                            (person_id,)).fetchone()[0]


def _nunca_comidas(conn, person_id: int) -> bool:
    return not conn.execute("SELECT EXISTS(SELECT 1 FROM comida WHERE person_id = ?)",
                            (person_id,)).fetchone()[0]


def _nunca_proteina(conn, person_id: int) -> bool:
    return not conn.execute(
        "SELECT EXISTS(SELECT 1 FROM ingesta WHERE person_id = ? AND metric_key = 'protein_g')",
        (person_id,)).fetchone()[0]


def _nunca_peso(conn, person_id: int) -> bool:
    return not conn.execute(
        "SELECT EXISTS(SELECT 1 FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = 'body_weight_kg')",
        (person_id,)).fetchone()[0]


def _nunca_cintura(conn, person_id: int) -> bool:
    return not conn.execute(
        "SELECT EXISTS(SELECT 1 FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = 'waist_cm')",
        (person_id,)).fetchone()[0]


def _nunca_entreno(conn, person_id: int) -> bool:
    tiene_activity = conn.execute("SELECT EXISTS(SELECT 1 FROM activity WHERE person_id = ?)",
                                  (person_id,)).fetchone()[0]
    tiene_plan_hecho = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM plan WHERE person_id = ? AND status = 'done' "
        "AND ejercicios_json IS NOT NULL)", (person_id,)).fetchone()[0]
    return not (tiene_activity or tiene_plan_hecho)


_NUNCA = {
    "sueno": _nunca_sueno,
    "comidas": _nunca_comidas,
    "proteina": _nunca_proteina,
    "peso": _nunca_peso,
    "cintura": _nunca_cintura,
    "entreno": _nunca_entreno,
}


def _rango_del_plan(conn, person_id: int, goal_id: int) -> tuple[date, date] | None:
    """The plan's own [start, end] (ruling 2's "from its start (or its first phase's start) to
    plan end"): `goal.start_day`, or the earliest phase's own start_day when the plan has phases
    (a phased plan's real work can start after the goal's own nominal start_day), to `goal.deadline`
    -- a dated plan is a milestone, so `deadline` is never NULL (db.py's own CHECK). None when the
    goal does not exist or is not a milestone with a deadline (nothing for `cobertura` to range
    over)."""
    fila = conn.execute("SELECT start_day, deadline, kind FROM goal WHERE id = ?", (goal_id,)).fetchone()
    if fila is None or fila["kind"] != "milestone" or fila["deadline"] is None:
        return None
    fases = horizonte.fases(conn, goal_id)
    inicio = fases[0]["start_day"] if fases else fila["start_day"]
    return date.fromisoformat(inicio), date.fromisoformat(fila["deadline"])


def _semanas_de(desde: date, hasta: date) -> list[date]:
    """Every ISO week's own Monday from `desde`'s week to `hasta`'s week, inclusive."""
    primera, ultima = fechas.lunes_de(desde), fechas.lunes_de(hasta)
    n = (ultima - primera).days // 7 + 1
    return [primera + timedelta(weeks=i) for i in range(n)]


def _stream_diario(conn, person_id: int, stream: str, desde: date, hasta: date) -> dict:
    if _NUNCA[stream](conn, person_id):
        return {"cadencia": MODELO["cadencia"][stream], "sin_uso": True}
    if desde > hasta:
        return {"cadencia": MODELO["cadencia"][stream], "sin_uso": False, "cubiertos": 0, "esperados": 0,
                "faltan": []}
    cubiertos_set = _FUENTES[stream](conn, person_id, desde.isoformat(), hasta.isoformat())
    dias = [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]
    faltantes = [d for d in dias if d.isoformat() not in cubiertos_set]
    faltantes.sort(reverse=True)
    return {"cadencia": MODELO["cadencia"][stream], "sin_uso": False, "cubiertos": len(dias) - len(faltantes),
            "esperados": len(dias), "faltan": [d.isoformat() for d in faltantes[:FALTAN_LIMITE]]}


def _stream_semanal(conn, person_id: int, stream: str, desde: date, hasta: date) -> dict:
    if _NUNCA[stream](conn, person_id):
        return {"cadencia": MODELO["cadencia"][stream], "sin_uso": True}
    if desde > hasta:
        return {"cadencia": MODELO["cadencia"][stream], "sin_uso": False, "cubiertos": 0, "esperados": 0,
                "faltan": []}
    semanas = _semanas_de(desde, hasta)
    # The query window pads to each ISO week's own full [lunes, domingo] -- a reading on the Monday
    # before `desde` (same ISO week) still covers that first week (ruling 1: "a week is covered by
    # any reading inside it", never clipped to the plan's own start/end day).
    d0, d1 = semanas[0].isoformat(), (semanas[-1] + timedelta(days=6)).isoformat()
    cubiertos_dias = _FUENTES[stream](conn, person_id, d0, d1)
    semanas_cubiertas = {fechas.lunes_de(date.fromisoformat(d)) for d in cubiertos_dias}
    faltantes = [s for s in semanas if s not in semanas_cubiertas]
    faltantes.sort(reverse=True)
    return {"cadencia": MODELO["cadencia"][stream], "sin_uso": False,
            "cubiertos": len(semanas) - len(faltantes), "esperados": len(semanas),
            "faltan": [s.isoformat() for s in faltantes[:FALTAN_LIMITE]]}


_SEMANALES = {"peso", "cintura", "entreno"}


def cobertura(conn, person_id: int, goal_id: int, hasta: date | None = None) -> dict:
    """Ruling 2: per stream, {cadencia, cubiertos, esperados, faltan} (or {cadencia, sin_uso: True}
    for a stream never used) over the plan's own range -- from its start, or its first phase's
    start, to min(hasta or today, plan end). `hasta=None` means "as of today", in the person's own
    timezone. `streams` is an empty dict when `goal_id` is not this plan shape (not found, not a
    milestone, no deadline) -- the caller (herramientas._leer_plan) already resolves access through
    goals.goal_access before this is ever reached."""
    rango = _rango_del_plan(conn, person_id, goal_id)
    if rango is None:
        return {"goal_id": goal_id, "streams": {}}
    inicio, fin = rango
    hoy = fechas.hoy_local(db.person(conn, person_id))
    tope = min(hasta or hoy, fin)
    streams = {}
    for stream in STREAMS:
        fn = _stream_semanal if stream in _SEMANALES else _stream_diario
        streams[stream] = fn(conn, person_id, stream, inicio, tope)
    return {"goal_id": goal_id, "desde": inicio.isoformat(), "hasta": tope.isoformat(), "streams": streams}


def pendientes(conn, person_id: int, dia: date) -> dict:
    """Ruling 3: for every ACTIVE dated plan covering `dia` (horizonte.horizontes solo_activos) --
    what is MISSING, never a full report: daily streams checked on `dia` itself, weekly streams
    checked over `dia`'s own ISO week so far (its Monday up to and including `dia`), `sueno` for the
    night ending on `dia` (sleep_session's own day IS the wake day). A stream the person has never
    used is listed under `sin_uso`, never under `faltan` -- the desk must not nag about a stream
    that was never part of their routine. {} when no dated plan is active on `dia`; the same
    {faltan, sin_uso} is repeated under every active plan's own goal_id, since these streams are the
    person's, not any one plan's."""
    activos = horizonte.horizontes(conn, person_id, dia, solo_activos=True)
    if not activos:
        return {}
    lunes = fechas.lunes_de(dia)
    faltan: list[str] = []
    sin_uso: list[str] = []
    for stream in STREAMS:
        if _NUNCA[stream](conn, person_id):
            sin_uso.append(stream)
            continue
        if stream in _SEMANALES:
            d0, d1 = lunes.isoformat(), dia.isoformat()
        else:
            d0 = d1 = dia.isoformat()
        cubiertos = _FUENTES[stream](conn, person_id, d0, d1)
        if not cubiertos:
            faltan.append(stream)
    resultado = {"faltan": faltan, "sin_uso": sin_uso}
    return {goal_id: resultado for goal_id in activos}
