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
nights carries no percentiles, only its own `n_grupo` and a `motivo`. `con_desplazamiento`
(BODY-MINUTO-ANALISIS-7, ORCH-CARLOS-DELEGATES-TIER-1-1) is `grupo`'s seventh key: whether the
night's evening day has (true) or has not (false) a commute row (`contexto_dia.py`'s own
`desplazamiento` key), matched the same bare-presence way `con_alcohol` already is.
`altitud_cambio_min`/`altitud_cambio_max` (BODY-MINUTO-ANALISIS-6-ALTITUD, ORCH-CARLOS-DELEGATES-
TIER-1-1 ruling 3) are the eighth, grouping by the CHANGE against this person's own home
(`contexto_dia.py`'s own `altitud.cambio_m`, signed metres) -- never the absolute altitude alone,
per Carlos's own framing relayed by the desk 2026-11-20: going down usually lowers resting/sleeping
heart rate, going up raises it. A night with no `altitud` never matches either bound.

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
whole night's own shape; `buscar_noches` (BODY-MINUTO-ANALISIS-3, ORCH-CARLOS-DELEGATES-TIER-1-1
ruling 2) for the REVERSE question -- not "how does this night rank" but "WHICH of this person's
own nights match a feature or a context", e.g. "my 5 worst deep-sleep nights this year and what
they had in common", "nights after a night shift where the low came late", "every night with more
than 3 awake segments while travelling".

`rasgos_rango(conn, person_id, desde, hasta)` (ruling 1) is the ONE feature table every night in a
range needs -- `comparar` and `buscar` both build on it, rather than each reading the bulk tables
above for itself. Its own per-night dict holds every one of that night's computable features
(both MODELO families, present only when actually computable, exactly as `comparar`'s own loop
built them before this refactor) plus a `'contexto'` key: the SAME evening/wake-day merge
`contexto_dia.contexto_noche` applies to one night, mirrored here over the WHOLE range from the
bulk `contexto_dia.contexto_dias` instead of calling `contexto_noche` once per night (which would
re-cost its own `ventana_comida.comida_a_sueno` lookup every time, growing with the range -- the
exact cost `comparar`'s own module docstring above already explains). `buscar`'s own `grupo`
filter reads this SAME `'contexto'` key through `_coincide_grupo`, never a second context build.
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

GRUPO_CLAVES = ("tras_turno", "tras_turno_noche", "viaje", "periodo", "comida_tarde_min_max", "con_alcohol",
                 "con_desplazamiento", "altitud_cambio")

# BODY-MINUTO-ANALISIS-6-ALTITUD: the actual keys a `grupo` dict may carry -- identical to
# GRUPO_CLAVES except `altitud_cambio` itself is never a filter key: it splits into
# `altitud_cambio_min`/`altitud_cambio_max` (both optional, either or both may be given), the one
# dimension `_coincide_grupo`/`_claves_contexto`/`_en_comun` still name "altitud_cambio" by.
GRUPO_FILTRO_CLAVES = (frozenset(GRUPO_CLAVES) - {"altitud_cambio"}) | {"altitud_cambio_min", "altitud_cambio_max"}

# BODY-MINUTO-ANALISIS-3 ruling 2: every name `buscar`'s own 'filtros'/'orden'/'rasgos' may name --
# both MODELO feature families together, read from MODELO itself so a feature added there is
# filterable immediately, never a second hand-typed list. The five comparison ops `buscar` accepts.
RASGOS_CLAVES = frozenset(MODELO["figuras_guardadas"]) | frozenset(MODELO["figuras_forma"])
OPS_BUSCAR_NOCHES = ("<", "<=", ">", ">=", "=")
_OPS_BUSCAR_NOCHES = {
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "=": lambda a, b: a == b,
}


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


def rasgos_rango(conn, person_id: int, desde: str, hasta: str) -> dict[str, dict]:
    """Every night whose own wake day falls in [desde, hasta] -> every one of its own computable
    features (both MODELO families, each present only when actually computable) plus its own
    `'contexto'` -- see the module docstring. One bulk statement per source table below, never one
    per night, so this stays the same cost whether the range is 30 nights or the person's whole
    history (tests/test_rendimiento.py's own guard)."""
    persona = db.person(conn, person_id)
    zona = persona["timezone"]

    figuras = _figuras_rango(conn, person_id, desde, hasta)
    sesiones = _sesiones_rango(conn, person_id, desde, hasta)
    arrays = _arrays_rango(conn, person_id, desde, hasta, zona)
    desde_tarde = (date.fromisoformat(desde) - timedelta(1)).isoformat()
    hasta_tarde = (date.fromisoformat(hasta) - timedelta(1)).isoformat()
    comida_tarde = _comida_ultima_rango(conn, person_id, desde_tarde, hasta_tarde)
    dias_contexto = contexto_dia.contexto_dias(conn, person_id, desde_tarde, hasta)

    noches = sorted(set(figuras) | set(sesiones))
    out: dict[str, dict] = {}
    for dia in noches:
        valores: dict[str, float] = dict(figuras.get(dia, {}))
        sesion = sesiones.get(dia)
        arr = arrays.get(dia)
        if sesion is not None and arr:
            valores.update(_rasgos_forma(sesion, arr))
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
        wake_start = sesion[0] if sesion is not None else None
        if ultima is not None and wake_start is not None:
            minutos = (_sin_offset(wake_start) - _sin_offset(ultima)).total_seconds() / 60
            entry["comida_antes_de_dormir"] = {"minutos": round(minutos, 1)}
        valores["contexto"] = entry
        out[dia] = valores
    return out


def _sin_minutos(valores: dict) -> bool:
    """Whether a night's own `rasgos_rango` entry carries NONE of MODELO's own `figuras_forma`
    keys -- the "no per-minute payload" gap the module docstring and `comparar`'s own
    `noches_sin_minutos` already name, read here straight off the feature dict instead of a
    second look at `sesiones`/`arrays`."""
    return not any(nombre in valores for nombre in MODELO["figuras_forma"])


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

    tabla = rasgos_rango(conn, person_id, desde, hasta)
    if fecha not in tabla:
        raise SinDatos()

    noches = sorted(tabla)
    noches_sin_minutos = sum(1 for dia in noches if _sin_minutos(tabla[dia]))

    rasgos: dict[str, dict] = {}
    valores_focal = {k: v for k, v in tabla[fecha].items() if k != "contexto"}
    for nombre, propio in valores_focal.items():
        valores_todos = [tabla[d][nombre] for d in noches if nombre in tabla[d]]
        rasgos[nombre] = _entrada_rasgo(valores_todos, propio)
        if grupo:
            noches_grupo = [d for d in noches if nombre in tabla[d]
                             and _coincide_grupo(tabla[d].get("contexto", {}), grupo)]
            valores_grupo = [tabla[d][nombre] for d in noches_grupo]
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
    if "con_desplazamiento" in grupo:
        if ("desplazamiento" in entry) != grupo["con_desplazamiento"]:
            return False
    if "altitud_cambio_min" in grupo or "altitud_cambio_max" in grupo:
        altitud = entry.get("altitud")
        if altitud is None:
            return False
        cambio = altitud["cambio_m"]
        if "altitud_cambio_min" in grupo and cambio < grupo["altitud_cambio_min"]:
            return False
        if "altitud_cambio_max" in grupo and cambio > grupo["altitud_cambio_max"]:
            return False
    return True


def _claves_contexto(entry: dict) -> set[str]:
    """Which of GRUPO_CLAVES' own eight dimensions this night's context carries AT ALL -- a bare
    presence check (mirroring `_coincide_grupo`'s own reading of each key, loosened from "matches
    this specific value" to "carries any value"), for `buscar`'s own `en_comun`."""
    out: set[str] = set()
    turno = entry.get("turno") or {}
    if "tipo" in turno:
        out.add("tras_turno")
    if "bloque_noche" in turno or "dias_desde_ultimo_turno_noche" in turno:
        out.add("tras_turno_noche")
    if "viaje" in entry:
        out.add("viaje")
    if entry.get("periodos"):
        out.add("periodo")
    if "comida_antes_de_dormir" in entry:
        out.add("comida_tarde_min_max")
    if "alcohol_g" in entry:
        out.add("con_alcohol")
    if "desplazamiento" in entry:
        out.add("con_desplazamiento")
    if "altitud" in entry:
        out.add("altitud_cambio")
    return out


def _en_comun(tabla: dict[str, dict], noches: list[str], seleccionadas: list[str]) -> dict:
    """BODY-MINUTO-ANALISIS-3 ruling 2: for each of GRUPO_CLAVES' own eight context dimensions, the
    SHARE of the matching nights (`seleccionadas`) that carry it against the share among EVERY
    night in the range (`noches`) -- counts and shares only, the "what did these have in common"
    Carlos himself asked for (relayed by the desk 2026-11-20), never a cause."""
    n_sel, n_total = len(seleccionadas), len(noches)
    out: dict[str, dict] = {}
    for clave in GRUPO_CLAVES:
        en_sel = sum(1 for d in seleccionadas if clave in _claves_contexto(tabla[d].get("contexto", {})))
        en_todas = sum(1 for d in noches if clave in _claves_contexto(tabla[d].get("contexto", {})))
        entrada = {
            "coincidentes": round(en_sel / n_sel, 3) if n_sel else 0.0,
            "en_rango": round(en_todas / n_total, 3) if n_total else 0.0,
        }
        if clave == "altitud_cambio":
            # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: Carlos, relayed by the desk 2026-11-20 --
            # what matters is the CHANGE against his own home, so `en_comun` splits the matching
            # nights three ways by its SIGN, over the SAME denominator (n_sel) "coincidentes"
            # already uses -- a night with no `altitud` (not counted in `coincidentes` either)
            # contributes to none of the three.
            cambios = [tabla[d]["contexto"]["altitud"]["cambio_m"] for d in seleccionadas
                       if tabla[d].get("contexto", {}).get("altitud") is not None]
            entrada["descenso"] = round(sum(1 for c in cambios if c < 0) / n_sel, 3) if n_sel else 0.0
            entrada["igual"] = round(sum(1 for c in cambios if c == 0) / n_sel, 3) if n_sel else 0.0
            entrada["ascenso"] = round(sum(1 for c in cambios if c > 0) / n_sel, 3) if n_sel else 0.0
        out[clave] = entrada
    return out


def buscar(conn, person_id: int, desde: str, hasta: str, filtros: list[dict] | None = None,
           grupo: dict | None = None, orden: dict | None = None, limite: int = 10,
           rasgos: list[str] | None = None) -> dict:
    """The REVERSE of `comparar`: not "how does this night rank" but "WHICH of this person's own
    nights match" (BODY-MINUTO-ANALISIS-3, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2). Built on the
    SAME `rasgos_rango` table `comparar` itself reads -- filtered, grouped and ordered in pure
    Python, so this costs no further statement whatever `filtros` holds or how wide [desde, hasta]
    is. `filtros` is a list of {rasgo, op, valor}; a night missing `rasgo` altogether never matches
    (RASGOS_CLAVES' own `figuras_forma` names are absent exactly on the nights `_sin_minutos`
    already counts). `grupo` is `_coincide_grupo`'s own contract, combined with `filtros` by AND.
    Every rasgo/op/grupo-key name here is trusted already valid -- body/agente/herramientas.py's
    own wrapper refuses an unknown one by name before this is ever called, the same split
    `_comparar_noche` already keeps with `_coincide_grupo`'s own grupo keys. `orden` sorts the
    matching nights ('rasgo', 'dir' asc|desc) before `limite` is applied; a night missing `orden`'s
    own rasgo sorts after every night that has it, in either direction. `rasgos` picks which
    features each returned night carries (default: every rasgo named in `filtros`/`orden`)."""
    tabla = rasgos_rango(conn, person_id, desde, hasta)
    noches = sorted(tabla)
    n_en_rango = len(noches)
    noches_sin_minutos_en_rango = sum(1 for dia in noches if _sin_minutos(tabla[dia]))

    filtros = filtros or []
    seleccionadas = []
    for dia in noches:
        valores = tabla[dia]
        if not all(f["rasgo"] in valores and _OPS_BUSCAR_NOCHES[f["op"]](valores[f["rasgo"]], f["valor"])
                   for f in filtros):
            continue
        if grupo and not _coincide_grupo(valores.get("contexto", {}), grupo):
            continue
        seleccionadas.append(dia)
    n_coinciden = len(seleccionadas)

    if orden:
        rasgo_orden = orden["rasgo"]
        reverse = orden["dir"] == "desc"
        con_valor = sorted((d for d in seleccionadas if rasgo_orden in tabla[d]),
                            key=lambda d: tabla[d][rasgo_orden], reverse=reverse)
        sin_valor = [d for d in seleccionadas if rasgo_orden not in tabla[d]]
        orden_final = con_valor + sin_valor
    else:
        orden_final = sorted(seleccionadas, reverse=True)
    limitadas = orden_final[:limite]

    rasgos_pedidos = rasgos
    if rasgos_pedidos is None:
        rasgos_pedidos = sorted({f["rasgo"] for f in filtros} | ({orden["rasgo"]} if orden else set()))

    noches_salida = []
    for dia in limitadas:
        valores = tabla[dia]
        entrada: dict = {"fecha": dia}
        for nombre in rasgos_pedidos:
            if nombre in valores:
                entrada[nombre] = valores[nombre]
        entrada["contexto"] = valores.get("contexto", {})
        noches_salida.append(entrada)

    out = {
        "noches": noches_salida, "n_coinciden": n_coinciden, "n_en_rango": n_en_rango,
        "noches_sin_minutos_en_rango": noches_sin_minutos_en_rango,
    }
    if n_coinciden >= MIN_NOCHES_GRUPO:
        out["en_comun"] = _en_comun(tabla, noches, seleccionadas)
    return out
