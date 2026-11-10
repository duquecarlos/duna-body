"""Per-day intraday cache, activity-hour exclusion, and the person's own hourly usual -- moved out
of body/recuperacion.py (BODY-TURNO-FC-1) so body/turno_fc.py can share them, with no behaviour
change for recuperacion.py's own callers. The intraday spacing/offset helpers (`_espaciado_mediana_
min`, `_sin_offset`) stay IMPORTED from body/experimentos.py, not duplicated here either (the same
rule BODY-RECUPERACION-1's own docstring already carries).

Every model constant `usual_hora` needs (`dias_historia`, `min_dias_horario`, `cobertura_min`) is
passed in by the caller rather than read from a shared MODELO -- recuperacion.py and turno_fc.py
each keep their own versioned MODELO with their own figures for these.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import db
from .experimentos import _espaciado_mediana_min, _sin_offset


def leer_dia(conn, person_id: int, dia: str, zona: str, cache: dict, kind: str = "intradia_fc") -> dict:
    """This one day's intraday series for `kind`, read via db.intradia AT MOST ONCE per (`dia`,
    `kind`) pair in `cache` (a per-run dict passed in by the caller) -- the bucket-by-clock-hour
    view and the day's own observed spacing are derived from that single read and cached alongside
    it. `kind` defaults to "intradia_fc" (recuperacion.py's only use), so its own per-run cache
    behaviour is unchanged; turno_fc.py also reads "intradia_estres" through the same cache."""
    dias_cache = cache.setdefault("dias", {})
    clave = (dia, kind)
    if clave in dias_cache:
        return dias_cache[clave]
    muestras = db.intradia(conn, person_id, dia, kind, zona)
    por_hora: dict[int, list[float]] = defaultdict(list)
    for ts, v in muestras:
        por_hora[_sin_offset(ts).hour].append(v)
    entry = {"muestras": muestras, "por_hora": por_hora, "espaciado": _espaciado_mediana_min(muestras)}
    dias_cache[clave] = entry
    return entry


def horas_actividad_por_dia(conn, person_id: int, cache: dict) -> dict[str, set[int]]:
    """Every clock hour, on every day, touched by one of this person's OWN activities -- read in
    ONE query per `cache` (every activity this person has, regardless of model version), not once
    per activity per look-back day. `usual_hora` excludes a (day, hour) pair from "ordinary" when
    it is here, per ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: a desk hour's usual must never include
    the activity itself."""
    if "horas_actividad" in cache:
        return cache["horas_actividad"]
    rows = conn.execute(
        "SELECT start_local, duration_s FROM activity WHERE person_id = ? "
        "AND start_local IS NOT NULL AND duration_s IS NOT NULL",
        (person_id,),
    ).fetchall()
    out: dict[str, set[int]] = defaultdict(set)
    for r in rows:
        inicio = _sin_offset(r["start_local"])
        fin = inicio + timedelta(seconds=r["duration_s"])
        t = inicio.replace(minute=0, second=0, microsecond=0)
        while t <= fin:
            out[t.date().isoformat()].add(t.hour)
            t += timedelta(hours=1)
    cache["horas_actividad"] = out
    return out


def _dias_descanso_por_persona(conn, person_id: int, cache: dict) -> dict[str, str]:
    """Cached day -> shift.kind mapping, read once per `cache` -- only built when `usual_hora` is
    asked to restrict to days off (BODY-TURNO-FC-1's `solo_descanso`); recuperacion.py's own calls
    never touch this. Earliest start_local wins on the rare day with more than one shift row, same
    convention body/turnos_costo.py's `_shift_on` uses."""
    if "turnos" in cache:
        return cache["turnos"]
    rows = conn.execute(
        "SELECT day, kind FROM shift WHERE person_id = ? ORDER BY start_local", (person_id,)
    ).fetchall()
    kind_por_dia: dict[str, str] = {}
    for r in rows:
        kind_por_dia.setdefault(r["day"], r["kind"])
    cache["turnos"] = kind_por_dia
    return kind_por_dia


def usual_hora(conn, person_id: int, zona: str, dia_referencia: str, hora: int, cache: dict, *,
               dias_historia: int, min_dias_horario: int, cobertura_min: float,
               solo_descanso: bool = False) -> tuple[float | None, int]:
    """The person's OWN usual heart rate for clock hour `hora`, as the median of every sample
    recorded during that hour over `dias_historia` days strictly before `dia_referencia` (that day
    excluded, same convention as baselines.baseline) -- counting only (day, hour) pairs where the
    day is not marked, that hour holds no activity of the person's own (`horas_actividad_por_dia`),
    that hour's own sample count meets `cobertura_min` against the day's own observed spacing, and
    -- when `solo_descanso` (BODY-TURNO-FC-1 ruling 2: the days-off comparison) -- the day's own
    confirmed shift.kind is 'off' (a day with no shift row at all is never counted as off). Returns
    (usual, n_dias_calificados); usual is None (honest "sin_rango") when n_dias_calificados <
    min_dias_horario. Memoized per (dia_referencia, hora, solo_descanso) in `cache`."""
    memo = cache.setdefault("usual", {})
    clave = (dia_referencia, hora, solo_descanso)
    if clave in memo:
        return memo[clave]

    d = date.fromisoformat(dia_referencia)
    desde = d - timedelta(days=dias_historia)
    hasta = d - timedelta(days=1)
    marcados = db.dias_marcados(conn, person_id, desde.isoformat(), hasta.isoformat())
    horas_actividad = horas_actividad_por_dia(conn, person_id, cache)
    kind_por_dia = _dias_descanso_por_persona(conn, person_id, cache) if solo_descanso else None

    valores: list[float] = []
    dias_calificados = 0
    dia_iter = desde
    while dia_iter <= hasta:
        dia_iso = dia_iter.isoformat()
        dia_iter += timedelta(days=1)
        if dia_iso in marcados or hora in horas_actividad.get(dia_iso, ()):
            continue
        if solo_descanso and kind_por_dia.get(dia_iso) != "off":
            continue
        entry = leer_dia(conn, person_id, dia_iso, zona, cache)
        if entry["espaciado"] is None:
            continue
        muestras_hora = entry["por_hora"].get(hora, [])
        esperadas = 60 / entry["espaciado"]
        if len(muestras_hora) < cobertura_min * esperadas:
            continue
        valores.extend(muestras_hora)
        dias_calificados += 1

    resultado = ((statistics.median(valores), dias_calificados) if dias_calificados >= min_dias_horario
                 else (None, dias_calificados))
    memo[clave] = resultado
    return resultado
