"""BODY-MINUTO-ANALISIS-2 (ORCH-CARLOS-DELEGATES-TIER-1-1): Carlos, relayed by the desk 2026-11-20,
wants "between you and body specialized tools to analyze in full detail every minute of the
metrics" -- today an assistant that wants to know "was last night's shape unusual for me, and
unusual for nights like it" must pull whole nights (leer_noche, body/agente/herramientas.py) and
compare arrays itself. This module gives it NIGHT FEATURES, versioned (`MODELO`), computed on read
with no derived table, plus `comparar` -- one night's own features ranked against the person's own
history, never a population.

Two feature families, both documented in `MODELO` with their own unit and source:

  - `figuras_guardadas`: the night's own STORED daily figures (sleep_hr_min, sleep_hr_avg,
    hrv_last_night, sleep_score, the four stage minutes, sleep_minutes and
    body_battery_sleep_charge), read for the whole range in ONE query over
    `daily_metric_efectiva` -- the same table/view every other screen already reads, never
    recomputed here.
  - `figuras_forma`: SHAPE features from the night's own per-minute arrays (heart rate, HRV,
    sleep stages) -- sleep onset/wake time, the "nadir timing" (how far into the night the
    robust-low heart-rate moment falls, as a fraction of the night), the first-third/last-third
    heart rate, the awake segments and their total minutes, the longest uninterrupted deep block,
    and the HRV first-half/second-half means. MANY OLD NIGHTS LACK THESE -- a night with no
    per-minute 'sleep' payload simply has none of these keys (counted in `noches_sin_minutos`),
    never a 0 or an invented value standing in for "no data".

`comparar` ranks ONE night (`fecha`, the wake day) against every night in [`desde`, `fecha`] of
this SAME person (`desde` default 365 days back) -- for each feature actually present on that
night: its own value, how many of the person's own nights carry that feature, the 25th/50th/75th
percentile among them, and this night's own PERCENTILE RANK (0-100) among them. The tie rule: a
night's percentile is 100 * (count of nights with a value <= this one, minus one) / (n - 1) -- so
the single highest value always reads 100, the single lowest always reads 0, and a night tied with
the maximum still reads 100 (ties share the higher rank). With `grupo`, the SAME five figures are
added again, this time only over the nights whose own derived context matches -- a group under 5
nights carries no percentiles, only its own `n_grupo` and a `motivo`.

Bounded cost (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3): every bulk reader below is ONE statement
over the WHOLE [desde, hasta] range, never one per night -- `daily_metric_efectiva` (stored
figures), `raw_payload` kind='sleep' (the minute payloads, parsed in pure Python per night after
one SELECT -- `db.sueno_minuto_a_minuto`, body/db.py:2933, is the same read for a SINGLE night;
there is no bulk equivalent there to call, and db.py is not this slice's to widen, so this module
issues its own bulk SELECT over the same table/kind/source-preference instead of looping that
single-night reader), `sleep_session`, and one `ingesta` aggregate for the "last meal before sleep"
gap. Every night's own CONTEXT for `grupo` matching is built by merging `contexto_dia.contexto_dias`
(body/contexto_dia.py:182, ALREADY one bulk call for a whole range) over the extended range in pure
Python -- `contexto_dia.contexto_noche` (body/contexto_dia.py:224) is the exact, canonical per-night
merge this mirrors (its own module docstring is the contract followed here), but calling it ONCE
PER NIGHT would re-cost its own `ventana_comida.comida_a_sueno` lookup every time, growing with the
range -- so `contexto_noche` itself is called exactly ONCE, for the single focal night's own
`contexto` returned to the caller, never for `grupo` matching across the range.

Precedents reused, never re-implemented: `sueno_fc.MODELO["percentil_bajo"]` (body/sueno_fc.py:40)
for the nadir's own robust-low rank -- the SAME 10th-percentile-of-samples rule `sueno_fc.noche`
uses for its own stored value, applied here to the timestamped samples to find WHEN it happened,
something the stored value alone cannot say. `sueno_fc.CLAVE_MIN`/`CLAVE_AVG` name the two stored
heart-rate keys rather than retyping them. Never medical language, never a cause -- a feature here
is a measurement and a rank among this person's own nights, nothing else.

When to use which assistant tool: `comparar_noche` (this module, through
body/agente/herramientas.py) for ONE night's features ranked against history, with or without a
context group; `leer_noche` for that SAME night's own full raw detail (every minute, every stage,
no ranking); `leer_serie` with paso='minuto' for a short window inside a night or a day, not a
whole night's own shape.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, time, timedelta

from . import contexto_dia, db, garmin_adapter, sueno_fc

VERSION = "noche-rasgos-v1"

HISTORIA_DIAS_DEFECTO = 365
MIN_NOCHES_PERCENTIL = 2
MIN_NOCHES_GRUPO = 5

# Garmin's own sleepLevels.activityLevel codes (body/agente/herramientas.py's own
# ETAPAS_SUENO_GARMIN, not imported here -- herramientas.py imports this module to register the
# comparar_noche tool, so importing back from it would be circular). Only the two codes this
# module's own shape features need.
_ETAPA_PROFUNDO = 0
_ETAPA_DESPIERTO = 3

MODELO = {
    "version": VERSION,
    "historia_dias_defecto": HISTORIA_DIAS_DEFECTO,
    "minimo_noches_percentil": MIN_NOCHES_PERCENTIL,
    "minimo_noches_grupo": MIN_NOCHES_GRUPO,
    "percentil_bajo_nadir": sueno_fc.MODELO["percentil_bajo"],
    # name -> (unit, source). Every key here is a real daily_metric_efectiva metric_key.
    "figuras_guardadas": {
        sueno_fc.CLAVE_MIN: ("bpm", "daily_metric_efectiva"),
        sueno_fc.CLAVE_AVG: ("bpm", "daily_metric_efectiva"),
        "hrv_last_night": ("ms", "daily_metric_efectiva"),
        "sleep_score": ("pts", "daily_metric_efectiva"),
        "sleep_deep_min": ("min", "daily_metric_efectiva"),
        "sleep_light_min": ("min", "daily_metric_efectiva"),
        "sleep_rem_min": ("min", "daily_metric_efectiva"),
        "sleep_awake_min": ("min", "daily_metric_efectiva"),
        "sleep_minutes": ("min", "daily_metric_efectiva"),
        "body_battery_sleep_charge": ("pts", "daily_metric_efectiva"),
    },
    # name -> (unit, source). Present only on a night with a per-minute 'sleep' payload.
    "figuras_forma": {
        "inicio_sueno_min_mediodia": ("min desde el mediodía anterior (local)", "sleep_session"),
        "fin_sueno_min_mediodia": ("min desde el mediodía anterior (local)", "sleep_session"),
        "nadir_fraccion": ("fracción 0-1 de la noche", "raw_payload:sleep (heart_rate)"),
        "fc_primer_tercio": ("bpm", "raw_payload:sleep (heart_rate)"),
        "fc_ultimo_tercio": ("bpm", "raw_payload:sleep (heart_rate)"),
        "segmentos_despierto": ("conteo", "raw_payload:sleep (stages)"),
        "minutos_despierto": ("min", "raw_payload:sleep (stages)"),
        "bloque_profundo_max_min": ("min", "raw_payload:sleep (stages)"),
        "vfc_primera_mitad": ("ms", "raw_payload:sleep (hrv)"),
        "vfc_segunda_mitad": ("ms", "raw_payload:sleep (hrv)"),
    },
}

GRUPO_CLAVES = ("tras_turno", "tras_turno_noche", "viaje", "periodo", "comida_tarde_min_max", "con_alcohol")


class SinDatos(Exception):
    """Raised by `comparar` when `fecha` carries neither a stored figure nor a sleep_session --
    nothing at all to rank. Caught by body/agente/herramientas.py's `_comparar_noche`, which turns
    it into a localized ToolError; this module stays language-agnostic."""


def _sin_offset(ts: str) -> datetime:
    """A local timestamp read back as naive -- the same reading body/ventana_comida.py's own
    `_sin_offset` gives, duplicated here rather than imported to keep this module's own dependency
    list to the bulk readers it actually needs."""
    dt = datetime.fromisoformat(ts)
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _min_desde_mediodia(dt: datetime) -> float:
    """Minutes since the local NOON before `dt` -- monotonic across midnight, so an onset at
    23:00 (660) and a wake at 07:00 the next day (1140) compare and percentile-rank sensibly as
    one "time of night" axis, the same way a night-shift schedule is usually read."""
    mediodia = datetime.combine(dt.date(), time(12, 0))
    if dt.time() < time(12, 0):
        mediodia -= timedelta(1)
    return round((dt - mediodia).total_seconds() / 60, 1)


def _percentil_rango(ordenados: list[float], p: float) -> float:
    """Nearest-rank percentile over an already-sorted list -- the SAME formula
    sueno_fc.py's own `_percentil_bajo` uses (not imported, private to that module)."""
    n = len(ordenados)
    idx = min(n - 1, max(0, round(p / 100 * (n - 1))))
    return ordenados[idx]


def _figuras_rango(conn, person_id: int, desde: str, hasta: str) -> dict[str, dict[str, float]]:
    """day -> {metric_key: value}, ONE query over daily_metric_efectiva for every stored figure
    this module names, for the whole range."""
    claves = tuple(MODELO["figuras_guardadas"])
    filas = conn.execute(
        "SELECT day, metric_key, value FROM daily_metric_efectiva WHERE person_id = ? AND day BETWEEN ? AND ? "
        f"AND metric_key IN ({','.join('?' for _ in claves)})",
        (person_id, desde, hasta, *claves),
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for f in filas:
        out.setdefault(f["day"], {})[f["metric_key"]] = f["value"]
    return out


def _sesiones_rango(conn, person_id: int, desde: str, hasta: str) -> dict[str, tuple[str, str]]:
    """day -> (start_local, end_local), ONE query over sleep_session for the whole range."""
    filas = conn.execute(
        "SELECT day, start_local, end_local FROM sleep_session WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, desde, hasta),
    ).fetchall()
    return {f["day"]: (f["start_local"], f["end_local"]) for f in filas
            if f["start_local"] and f["end_local"]}


def _arrays_rango(conn, person_id: int, desde: str, hasta: str, zona: str) -> dict[str, dict]:
    """day -> garmin_adapter.parse_sleep_series(...), from ONE query over raw_payload for the
    whole range -- db.sueno_minuto_a_minuto's own per-day SELECT (body/db.py:2933), widened to a
    range so this stays one statement regardless of how many nights it covers, with the SAME
    source preference (a non-Garmin override for a day wins over Garmin's own payload)."""
    filas = conn.execute(
        "SELECT day, payload, source FROM raw_payload WHERE person_id = ? AND day BETWEEN ? AND ? "
        "AND kind = 'sleep' ORDER BY day, (source != ?)",
        (person_id, desde, hasta, garmin_adapter.SOURCE),
    ).fetchall()
    out: dict[str, dict] = {}
    for f in filas:
        if f["day"] in out:
            continue
        arrays = garmin_adapter.parse_sleep_series(json.loads(f["payload"]), zona)
        if arrays:
            out[f["day"]] = arrays
    return out


def _comida_ultima_rango(conn, person_id: int, desde: str, hasta: str) -> dict[str, str]:
    """evening day -> that day's last TIMED `ingesta.ts_local`, ONE query over the whole range --
    the same rows body/ventana_comida.py's `ventana` reads one day at a time; this bulk form
    exists only because that module has no range version and is not this slice's to widen."""
    filas = conn.execute(
        "SELECT day, MAX(ts_local) AS ultima FROM ingesta WHERE person_id = ? AND day BETWEEN ? AND ? "
        "AND ts_local IS NOT NULL GROUP BY day",
        (person_id, desde, hasta),
    ).fetchall()
    return {f["day"]: f["ultima"] for f in filas}


def _segmentos(stage_pts: tuple, fin_dt: datetime, codigo: int) -> list[tuple[datetime, datetime]]:
    """Maximal runs of one stage code, each point's own span running to the next point's start and
    the LAST point's own span running to `fin_dt` -- the same point-to-next-point convention
    body/agente/herramientas.py's `_verificar_etapas` (body/agente/herramientas.py:1458) uses for
    its own etapas_verificacion, not re-derived from scratch."""
    puntos = sorted(stage_pts, key=lambda p: _sin_offset(p[0]))
    segmentos: list[tuple[datetime, datetime]] = []
    inicio_actual: datetime | None = None
    fin_actual: datetime | None = None
    for i, (ts, c) in enumerate(puntos):
        inicio = _sin_offset(ts)
        fin = _sin_offset(puntos[i + 1][0]) if i + 1 < len(puntos) else fin_dt
        if int(round(c)) == codigo:
            if inicio_actual is None:
                inicio_actual = inicio
            fin_actual = fin
        elif inicio_actual is not None:
            segmentos.append((inicio_actual, fin_actual))
            inicio_actual = None
    if inicio_actual is not None:
        segmentos.append((inicio_actual, fin_actual))
    return segmentos


def _rasgos_forma(sesion: tuple[str, str], arrays: dict) -> dict[str, float]:
    """One night's own SHAPE features from its per-minute arrays -- see the module docstring for
    which feature needs which array, each present only when its own input actually allows it."""
    inicio_dt, fin_dt = _sin_offset(sesion[0]), _sin_offset(sesion[1])
    total_s = (fin_dt - inicio_dt).total_seconds()
    out: dict[str, float] = {
        "inicio_sueno_min_mediodia": _min_desde_mediodia(inicio_dt),
        "fin_sueno_min_mediodia": _min_desde_mediodia(fin_dt),
    }
    if total_s <= 0:
        return out

    hr = arrays.get("heart_rate")
    if hr:
        en_ventana = sorted((_sin_offset(ts), v) for ts, v in hr if inicio_dt <= _sin_offset(ts) <= fin_dt)
        if en_ventana:
            p = MODELO["percentil_bajo_nadir"]
            por_valor = sorted(en_ventana, key=lambda par: par[1])
            idx = min(len(por_valor) - 1, max(0, round(p / 100 * (len(por_valor) - 1))))
            nadir_ts = por_valor[idx][0]
            out["nadir_fraccion"] = round((nadir_ts - inicio_dt).total_seconds() / total_s, 4)
            t1 = inicio_dt + (fin_dt - inicio_dt) / 3
            t2 = inicio_dt + (fin_dt - inicio_dt) * 2 / 3
            primer_tercio = [v for ts, v in en_ventana if ts < t1]
            ultimo_tercio = [v for ts, v in en_ventana if ts >= t2]
            if primer_tercio:
                out["fc_primer_tercio"] = round(statistics.fmean(primer_tercio), 1)
            if ultimo_tercio:
                out["fc_ultimo_tercio"] = round(statistics.fmean(ultimo_tercio), 1)

    stages = arrays.get("stages")
    if stages:
        despierto = _segmentos(stages, fin_dt, _ETAPA_DESPIERTO)
        out["segmentos_despierto"] = len(despierto)
        out["minutos_despierto"] = round(sum((f - i).total_seconds() / 60 for i, f in despierto), 1)
        profundo = _segmentos(stages, fin_dt, _ETAPA_PROFUNDO)
        bloques_profundo = [(f - i).total_seconds() / 60 for i, f in profundo]
        out["bloque_profundo_max_min"] = round(max(bloques_profundo, default=0.0), 1)

    hrv = arrays.get("hrv")
    if hrv:
        en_ventana_hrv = [(_sin_offset(ts), v) for ts, v in hrv if inicio_dt <= _sin_offset(ts) <= fin_dt]
        if en_ventana_hrv:
            medio = inicio_dt + (fin_dt - inicio_dt) / 2
            primera_mitad = [v for ts, v in en_ventana_hrv if ts < medio]
            segunda_mitad = [v for ts, v in en_ventana_hrv if ts >= medio]
            if primera_mitad:
                out["vfc_primera_mitad"] = round(statistics.fmean(primera_mitad), 1)
            if segunda_mitad:
                out["vfc_segunda_mitad"] = round(statistics.fmean(segunda_mitad), 1)

    return out


def comparar(conn, person_id: int, fecha: str, desde: str | None = None, grupo: dict | None = None) -> dict:
    """ONE night's own features (`fecha`, the wake day), ranked against this person's own nights
    in [`desde`, `fecha`] -- see the module docstring for the full contract. Raises `SinDatos` when
    `fecha` carries neither a stored figure nor a sleep_session."""
    persona = db.person(conn, person_id)
    zona = persona["timezone"]
    dia_d = date.fromisoformat(fecha)
    if desde is None:
        desde = (dia_d - timedelta(HISTORIA_DIAS_DEFECTO)).isoformat()
    elif desde > fecha:
        desde = fecha
    hasta = fecha

    figuras = _figuras_rango(conn, person_id, desde, hasta)
    sesiones = _sesiones_rango(conn, person_id, desde, hasta)
    if fecha not in figuras and fecha not in sesiones:
        raise SinDatos()

    arrays = _arrays_rango(conn, person_id, desde, hasta, zona)
    desde_tarde = (date.fromisoformat(desde) - timedelta(1)).isoformat()
    hasta_tarde = (dia_d - timedelta(1)).isoformat()
    comida_tarde = _comida_ultima_rango(conn, person_id, desde_tarde, hasta_tarde)
    dias_contexto = contexto_dia.contexto_dias(conn, person_id, desde_tarde, hasta)

    noches = sorted(set(figuras) | set(sesiones))
    valores_por_noche: dict[str, dict[str, float]] = {}
    contexto_por_noche: dict[str, dict] = {}
    for dia in noches:
        valores: dict[str, float] = dict(figuras.get(dia, {}))
        sesion = sesiones.get(dia)
        arr = arrays.get(dia)
        if sesion is not None and arr:
            valores.update(_rasgos_forma(sesion, arr))
        valores_por_noche[dia] = valores
        evening = (date.fromisoformat(dia) - timedelta(1)).isoformat()
        entry = dict(dias_contexto.get(evening, {}))
        wake_entry = dias_contexto.get(dia, {})
        if "viaje" not in entry and "viaje" in wake_entry:
            entry["viaje"] = wake_entry["viaje"]
        if "periodos" in wake_entry:
            ya = {p["kind"] for p in entry.get("periodos", [])}
            extra = [p for p in wake_entry["periodos"] if p["kind"] not in ya]
            if extra:
                entry["periodos"] = entry.get("periodos", []) + extra
        ultima = comida_tarde.get(evening)
        wake_start = sesiones.get(dia, (None, None))[0]
        if ultima is not None and wake_start is not None:
            minutos = (_sin_offset(wake_start) - _sin_offset(ultima)).total_seconds() / 60
            entry["comida_antes_de_dormir"] = {"minutos": round(minutos, 1)}
        contexto_por_noche[dia] = entry

    noches_sin_minutos = sum(1 for dia in sesiones if not arrays.get(dia))

    rasgos: dict[str, dict] = {}
    valores_focal = valores_por_noche.get(fecha, {})
    for nombre, propio in valores_focal.items():
        valores_todos = [valores_por_noche[d][nombre] for d in noches if nombre in valores_por_noche[d]]
        rasgos[nombre] = _entrada_rasgo(valores_todos, propio)
        if grupo:
            noches_grupo = [d for d in noches if nombre in valores_por_noche[d]
                             and _coincide_grupo(contexto_por_noche.get(d, {}), grupo)]
            valores_grupo = [valores_por_noche[d][nombre] for d in noches_grupo]
            rasgos[nombre]["grupo"] = _entrada_grupo(valores_grupo, propio)

    return {
        "dia": fecha, "zona": zona, "rasgos": rasgos, "noches_sin_minutos": noches_sin_minutos,
        "contexto": contexto_dia.contexto_noche(conn, person_id, fecha),
    }


def _entrada_rasgo(valores: list[float], propio: float) -> dict:
    n = len(valores)
    out: dict = {"valor": propio, "n": n}
    if n < MIN_NOCHES_PERCENTIL:
        return out
    ordenados = sorted(valores)
    menor_o_igual = sum(1 for v in ordenados if v <= propio)
    out["p25"] = _percentil_rango(ordenados, 25)
    out["p50"] = _percentil_rango(ordenados, 50)
    out["p75"] = _percentil_rango(ordenados, 75)
    out["percentil"] = round(100 * (menor_o_igual - 1) / (n - 1), 1)
    return out


def _entrada_grupo(valores: list[float], propio: float) -> dict:
    n_grupo = len(valores)
    if n_grupo < MIN_NOCHES_GRUPO:
        return {"n_grupo": n_grupo, "motivo": "pocas_noches_grupo"}
    ordenados = sorted(valores)
    menor_o_igual = sum(1 for v in ordenados if v <= propio)
    return {
        "n_grupo": n_grupo,
        "p25": _percentil_rango(ordenados, 25), "p50": _percentil_rango(ordenados, 50),
        "p75": _percentil_rango(ordenados, 75),
        "percentil": round(100 * (menor_o_igual - 1) / (n_grupo - 1), 1),
    }


def _coincide_grupo(entry: dict, grupo: dict) -> bool:
    """Whether one night's own merged context matches every criterion named in `grupo` -- see the
    module docstring and body/agente/herramientas.py's own comparar_noche param schema for each
    key's shape. A key absent from `grupo` is never checked."""
    if "tras_turno" in grupo:
        turno = entry.get("turno") or {}
        if turno.get("tipo") != grupo["tras_turno"]:
            return False
    if "tras_turno_noche" in grupo:
        turno = entry.get("turno") or {}
        es = "bloque_noche" in turno or "dias_desde_ultimo_turno_noche" in turno
        if es != grupo["tras_turno_noche"]:
            return False
    if "viaje" in grupo:
        if ("viaje" in entry) != grupo["viaje"]:
            return False
    if "periodo" in grupo:
        kinds = {p["kind"] for p in entry.get("periodos", [])}
        if grupo["periodo"] not in kinds:
            return False
    if "comida_tarde_min_max" in grupo:
        comida = entry.get("comida_antes_de_dormir")
        if comida is None or comida["minutos"] > grupo["comida_tarde_min_max"]:
            return False
    if "con_alcohol" in grupo:
        if ("alcohol_g" in entry) != grupo["con_alcohol"]:
            return False
    return True
