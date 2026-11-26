"""BODY-SUENO-DETECTOR-1 (ORCH-CARLOS-DELEGATES-TIER-1-1): a rule-based WITHIN-night wake/arousal
detector -- Body reading his nights itself (BODY-SUENO-CLASIFICACION-PROPIA-INV's own surface)
instead of only repeating Garmin's own sleep stages. A sibling of body/despertar_real.py, never a
change to it: despertar_real covers the hours AFTER Garmin's own sleep end (body/despertar_real.py:
294, 310-311 -- `sleep_end_dt` and the horizon that starts there); this module covers the window
BETWEEN Garmin's own sleep start and end, [sleep_session.start_local, sleep_session.end_local]
inclusive. The two windows never overlap.

Pure read over a connection and a person id, no web imports, the same shape as
body/despertar_real.py, body/recuperacion.py, body/sueno_fc.py and body/turno_fc.py: no writes,
no prints, no logging, no schema, no screen. Every threshold lives in MODELO, versioned
(`MODELO["version"]`), auditable and changeable without touching the detection logic.

Baselines are the night's own and the person's own, never a constant and never cross-person
(ruling 3): `base_fc_noche` is the median heart rate of the night's own quietest quartile
(body/sueno_fc.py's percentil_bajo convention, sueno_fc.py:51/66-71); `base_hrv_noche` is the
median of the night's own HRV samples; the movement threshold is the night's own median movement
level, a relative threshold, never an absolute code. Paired stress reuses despertar_real's own
nearest-timestamp pairing and rest baseline (despertar_real.py:158-181, `_baseline_estres` and
`_valor_cercano`) by calling those helpers as they stand -- never by editing despertar_real.py.

Cadence is measured, never assumed (ruling 5): each series is tiled on the night's OWN observed
median spacing (the day cache and spacing helpers come from body/experimentos.py, shared with
despertar_real.py/sueno_fc.py/turno_fc.py, never duplicated here); a gap wider than
MODELO["factor_hueco_espaciado"] times that spacing is `sin_dato` for that series, counted toward
neither sleep nor wake. When a series' own spacing cannot be measured at all (fewer than two
samples), its gaps are never judged -- never an assumed interval, since Garmin documents none for
the sleep arrays (unlike intradia_fc's known 2-minute cadence despertar_real.py relies on).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db, despertar_real, intradia_horario
from .experimentos import _espaciado_mediana_min, _sin_offset

VERSION = "sueno-arousal-v1"
MODELO = {
    "version": VERSION,
    # A heart rate at or above this many bpm over the night's OWN base_fc_noche, while movement is
    # above the night's own median, is one of the three signals a moving minute can be a wake
    # candidate on (ruling 4).
    "margen_arousal_bpm": 6,
    # An HRV drop of at least this many percent under the night's OWN base_hrv_noche, while
    # movement is above the night's own median, is the second of those three signals.
    "margen_hrv_caida_pct": 20,
    # A moving candidate run shorter than this many minutes folds back to 'dormido' -- a turn in
    # bed, not a wake.
    "min_duracion_arousal_min": 2,
    # A moving (or still-but-racing) candidate run at or above this many minutes is 'despierto',
    # not merely 'arousal'.
    "min_duracion_despierto_min": 10,
    # A STILL minute (movement at or under the night's own median) with heart rate at or above
    # this many bpm over base_fc_noche is the quiet-wakefulness candidate ruling 4 names
    # separately -- the case movement alone and heart rate alone each miss.
    "margen_despierto_quieto_bpm": 12,
    # A gap wider than this many times a series' own observed median spacing is 'sin_dato' for
    # that series -- despertar_real.py:212-247's own gap-factor convention, restated here since
    # each module's MODELO holds every constant it uses.
    "factor_hueco_espaciado": 3,
}

ETIQUETAS = ("dormido", "arousal", "despierto", "sin_dato")


@dataclass(frozen=True)
class Noche:
    version: str
    minutos: tuple[tuple[str, str], ...]
    bouts: tuple[tuple[str, str, str], ...]
    totales_min: dict[str, int]
    cobertura: float
    espaciado_s: dict[str, float]
    motivo: str | None


def _en_ventana(puntos: tuple[tuple[str, float], ...], desde: datetime, hasta: datetime
                 ) -> list[tuple[datetime, float]]:
    return sorted((_sin_offset(ts), v) for ts, v in puntos if desde <= _sin_offset(ts) <= hasta)


def _espaciado_min(puntos: list[tuple[datetime, float]]) -> float | None:
    """The night's own observed median spacing for one already-windowed series, in minutes --
    `experimentos._espaciado_mediana_min`'s own median-of-consecutive-gaps logic, reused rather
    than restated, over timestamps already parsed to datetimes."""
    return _espaciado_mediana_min([(dt.isoformat(), v) for dt, v in puntos])


def _tile(puntos: list[tuple[datetime, float]], desde: datetime, hasta: datetime,
          hueco_min: float | None) -> list[tuple[datetime, datetime, float | None]]:
    """despertar_real._segmentos's own forward-fill tiling (despertar_real.py:212-247), restated
    here over a single series' raw values instead of a classification label: a sample's value
    governs the interval up to the next sample, the very first gap is filled by the first
    sample's own value (there is nothing earlier to fill it with), and a gap wider than
    `hueco_min` is `None` ('sin_dato') instead of either side's value. `hueco_min=None` means the
    night's own spacing could not be measured -- no gap is ever judged, since there is nothing to
    judge it against (module docstring, ruling 5)."""
    if not puntos:
        return [(desde, hasta, None)]
    crudos: list[tuple[datetime, datetime, float | None]] = []
    prev_dt = desde
    for i, (dt, v) in enumerate(puntos):
        if i == 0:
            gap = (dt - prev_dt).total_seconds() / 60
            crudos.append((prev_dt, dt, None if (hueco_min is not None and gap > hueco_min) else v))
        else:
            dt_prev, v_prev = puntos[i - 1]
            gap = (dt - dt_prev).total_seconds() / 60
            crudos.append((dt_prev, dt, None if (hueco_min is not None and gap > hueco_min) else v_prev))
        prev_dt = dt
    ultimo_dt, ultimo_v = puntos[-1]
    gap_final = (hasta - ultimo_dt).total_seconds() / 60
    crudos.append((ultimo_dt, hasta, None if (hueco_min is not None and gap_final > hueco_min) else ultimo_v))
    return crudos


def _en_tile(tiles: list[tuple[datetime, datetime, float | None]], t: datetime, puntero: int
             ) -> tuple[float | None, int]:
    while puntero < len(tiles) - 1 and t >= tiles[puntero][1]:
        puntero += 1
    return tiles[puntero][2], puntero


def _base_fc_noche(hr_ventana: list[tuple[datetime, float]]) -> float | None:
    """The median heart rate of the night's own quietest quartile (ruling 3) -- sort the night's
    own heart-rate samples ascending (lower is quieter) and take the median of the lowest quarter,
    sueno_fc.py's own percentil_bajo convention (sueno_fc.py:51/66-71) applied to a quartile
    instead of a single rank, so one stray low sample cannot set the whole night's baseline."""
    if not hr_ventana:
        return None
    valores = sorted(v for _, v in hr_ventana)
    corte = max(1, len(valores) // 4)
    return round(statistics.median(valores[:corte]), 1)


def _agrupar(etiquetas: list[str]) -> list[tuple[str, int, int]]:
    """Contiguous same-label runs over an index-ordered list, as (label, start_idx, end_idx)."""
    grupos: list[list] = []
    for i, e in enumerate(etiquetas):
        if grupos and grupos[-1][0] == e:
            grupos[-1][2] = i
        else:
            grupos.append([e, i, i])
    return [tuple(g) for g in grupos]


def _noche_sin_dato(tiempos: list[datetime], motivo: str) -> Noche:
    return Noche(
        version=VERSION,
        minutos=tuple((t.isoformat(), "sin_dato") for t in tiempos),
        bouts=(),
        totales_min={"dormido": 0, "arousal": 0, "despierto": 0, "sin_dato": len(tiempos)},
        cobertura=0.0,
        espaciado_s={},
        motivo=motivo,
    )


def estimar(conn, person_id: int, day: str) -> Noche | None:
    """The whole night's own wake/arousal reading (module docstring, rulings 1-6). `None` when
    `day` holds no `sleep_session` row for this person. Read-only, no network; every query is
    scoped to `person_id` (tests/test_persona_fuga.py's own convention)."""
    row = conn.execute(
        "SELECT start_local, end_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, day),
    ).fetchone()
    if row is None or row["start_local"] is None or row["end_local"] is None:
        return None

    zona = db.person(conn, person_id)["timezone"]
    inicio_dt = _sin_offset(row["start_local"])
    fin_dt = _sin_offset(row["end_local"])
    if fin_dt < inicio_dt:
        return None

    n_minutos = int((fin_dt - inicio_dt).total_seconds() // 60) + 1
    tiempos = [inicio_dt + timedelta(minutes=i) for i in range(n_minutos)]

    series = db.sueno_minuto_a_minuto(conn, person_id, day, zona)
    movimiento = _en_ventana(series.get("movement", ()), inicio_dt, fin_dt)
    if not movimiento:
        return _noche_sin_dato(tiempos, "sin_movimiento")

    hr = _en_ventana(series.get("heart_rate", ()), inicio_dt, fin_dt)
    hrv = _en_ventana(series.get("hrv", ()), inicio_dt, fin_dt)

    cache: dict = {}
    base_estres = despertar_real._baseline_estres(conn, person_id, zona, fin_dt, cache)
    dias_noche = sorted({inicio_dt.date().isoformat(), fin_dt.date().isoformat()})
    estres_crudo: list[tuple[datetime, float]] = []
    for dia in dias_noche:
        entry = intradia_horario.leer_dia(conn, person_id, dia, zona, cache, kind="intradia_estres")
        estres_crudo.extend((_sin_offset(ts), v) for ts, v in entry["muestras"])
    estres = sorted((dt, v) for dt, v in estres_crudo if inicio_dt <= dt <= fin_dt)

    mediana_mov = statistics.median(v for _, v in movimiento)
    base_fc = _base_fc_noche(hr)
    base_hrv = round(statistics.median(v for _, v in hrv), 1) if hrv else None

    factor = MODELO["factor_hueco_espaciado"]
    espaciado_mov = _espaciado_min(movimiento)
    espaciado_hr = _espaciado_min(hr)
    espaciado_hrv = _espaciado_min(hrv)
    espaciado_estres = _espaciado_min(estres)

    mov_tiles = _tile(movimiento, inicio_dt, fin_dt,
                       factor * espaciado_mov if espaciado_mov is not None else None)
    hr_tiles = _tile(hr, inicio_dt, fin_dt, factor * espaciado_hr if espaciado_hr is not None else None)
    hrv_tiles = _tile(hrv, inicio_dt, fin_dt, factor * espaciado_hrv if espaciado_hrv is not None else None)

    tol_estres = despertar_real.MODELO["tolerancia_pareo_estres_min"]
    margen_estres = despertar_real.MODELO["margen_estres_descanso"]

    mp = hp = hvp = 0
    crudos: list[str] = []
    for t in tiempos:
        mov_val, mp = _en_tile(mov_tiles, t, mp)
        if mov_val is None:
            crudos.append("sin_dato")
            continue

        hr_val, hp = (_en_tile(hr_tiles, t, hp) if hr_tiles else (None, hp))
        hrv_val, hvp = (_en_tile(hrv_tiles, t, hvp) if hrv_tiles else (None, hvp))
        estres_val = despertar_real._valor_cercano(estres, t, tol_estres) if estres else None

        movement_up = mov_val > mediana_mov
        hr_delta = (hr_val - base_fc) if (hr_val is not None and base_fc is not None) else None
        hrv_caida_pct = (
            (base_hrv - hrv_val) / base_hrv * 100
            if (hrv_val is not None and base_hrv not in (None, 0)) else None
        )
        delta_estres = (
            (estres_val - base_estres) if (estres_val is not None and base_estres is not None) else None
        )
        estres_fuera = delta_estres is not None and delta_estres > margen_estres

        candidato_mov = movement_up and (
            (hr_delta is not None and hr_delta >= MODELO["margen_arousal_bpm"])
            or (hrv_caida_pct is not None and hrv_caida_pct >= MODELO["margen_hrv_caida_pct"])
            or estres_fuera
        )
        candidato_quieto = (
            not movement_up and hr_delta is not None and hr_delta >= MODELO["margen_despierto_quieto_bpm"]
        )

        if candidato_mov:
            crudos.append("candidato_movimiento")
        elif candidato_quieto:
            crudos.append("candidato_quieto")
        else:
            crudos.append("dormido")

    finales = list(crudos)
    for tipo, i0, i1 in _agrupar(crudos):
        duracion = i1 - i0 + 1
        if tipo == "candidato_movimiento":
            if duracion < MODELO["min_duracion_arousal_min"]:
                etiqueta = "dormido"
            elif duracion < MODELO["min_duracion_despierto_min"]:
                etiqueta = "arousal"
            else:
                etiqueta = "despierto"
        elif tipo == "candidato_quieto":
            etiqueta = "despierto" if duracion >= MODELO["min_duracion_despierto_min"] else "dormido"
        else:
            etiqueta = tipo
        for i in range(i0, i1 + 1):
            finales[i] = etiqueta

    totales_min = {clave: 0 for clave in ETIQUETAS}
    for e in finales:
        totales_min[e] += 1
    cobertura = (n_minutos - totales_min["sin_dato"]) / n_minutos if n_minutos else 0.0

    bouts = tuple(
        (tiempos[i0].isoformat(), (tiempos[i1] + timedelta(minutes=1)).isoformat(), tipo)
        for tipo, i0, i1 in _agrupar(finales) if tipo in ("arousal", "despierto")
    )

    espaciado_s: dict[str, float] = {}
    if espaciado_mov is not None:
        espaciado_s["movement"] = espaciado_mov * 60
    if espaciado_hr is not None:
        espaciado_s["heart_rate"] = espaciado_hr * 60
    if espaciado_hrv is not None:
        espaciado_s["hrv"] = espaciado_hrv * 60
    if espaciado_estres is not None:
        espaciado_s["stress"] = espaciado_estres * 60

    return Noche(
        version=VERSION,
        minutos=tuple((t.isoformat(), e) for t, e in zip(tiempos, finales)),
        bouts=bouts,
        totales_min=totales_min,
        cobertura=round(cobertura, 4),
        espaciado_s=espaciado_s,
        motivo=None,
    )
