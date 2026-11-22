"""Carlos's idea, relayed by the Personal desk 2026-11-22: "is basketball improving my cardio, and
what kind of game and preparation makes me play best?" -- BODY-PARTIDO-1, the session card. Pure
reads over a connection and a person id, no web imports, no t() -- body/rutas/entreno_sesion.py
formats and renders (the same split body/entreno_sesion.py and body/recuperacion.py already use).

BODY-PARTIDO-1-INV found: a session's own heart-rate curve, laps and heart-rate-zone breakdown are
already stored, per activity, in `activity_detail` (body/db.py's `detalle_actividad`,
DETALLE_KINDS) -- the parser truncates every 'detalle' sample to the minute on read
(garmin_adapter.py:874). Nothing here is specific to basketball or to one person: the same model
runs for any non-strength session that carries stored detail, against that same person's own
history of same-type (or, for Garmin's generic "other" bucket, same-name) sessions.

Every number below is an ESTIMATE, versioned (MODELO['version']), against the person's OWN
history, never a population table (docs/DESIGN.md rules 4, 5, 12).
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta

from . import db, entreno_sesion, garmin_adapter

VERSION = "esfuerzo-v1"
MODELO = {
    "version": VERSION,
    # A minute counts as a hard effort only at or above this percentile of the session's own
    # per-minute heart rate (ruling 3).
    "percentil_pico": 80,
    # Two detected peaks closer together than this keep only the higher one (ruling 3).
    "separacion_min": 3,
    # The drop is HR(peak) - HR(peak + this many minutes) -- named so a later version can widen
    # the window without touching the code that reads it (ruling 3).
    "caida_minutos": 1,
    # Fewer measured drops than this and there is no honest recovery figure for the session
    # (ruling 3).
    "caidas_min": 3,
    # Fewer minutes of per-minute data than this and nothing is computed at all -- the honest gap
    # (ruling 3).
    "minutos_min": 10,
    # Below this fraction of the session's own minutes actually covered by stored samples, the
    # same honest gap applies (ruling 3).
    "cobertura_min": 0.6,
    # Three-way comparison bands, one per compared figure (ruling 5): within the band is "as
    # usual", outside it is "more"/"less" (or, for the recovery drop, "faster"/"slower").
    "banda_zona_alta_pts": 5,   # percentage points of the zones-4/5 share
    "banda_caida_ppm": 3,       # bpm, on the recovery drop
}

# Ruling 5: the SAME comparison pool entreno_sesion.comparaciones already selects. HISTORIAL_N/
# HISTORIAL_MIN are entreno_sesion.py's own named constants (entreno_sesion.py:22-23), read here
# rather than redefined, so one number still governs both.
HISTORIAL_N = entreno_sesion.HISTORIAL_N
HISTORIAL_MIN = entreno_sesion.HISTORIAL_MIN


def _contiguos(a: datetime, b: datetime) -> bool:
    """Ruling 2: two minutes are contiguous only when exactly one minute apart."""
    return b - a == timedelta(minutes=1)


def _percentil(valores: list[float], p: float) -> float:
    """Linear-interpolated percentile (the "inclusive" method) -- `valores` must be non-empty."""
    datos = sorted(valores)
    if len(datos) == 1:
        return datos[0]
    k = (len(datos) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return datos[int(k)]
    return datos[f] + (datos[c] - datos[f]) * (k - f)


def serie_por_minuto(muestras, start_local: str | None, duration_s: float | None) -> list[tuple[datetime, float]]:
    """Ruling 2: `muestras` (db.detalle_actividad's own 'detalle' tuples, already truncated to the
    minute on parse -- garmin_adapter.py:874) folded to one mean per minute, sorted by time,
    restricted to the activity's own window (start_local .. start_local + duration_s)."""
    if not muestras or not start_local or duration_s is None:
        return []
    inicio = datetime.fromisoformat(start_local)
    fin = inicio + timedelta(seconds=duration_s)
    por_minuto: dict[datetime, list[float]] = {}
    for t, v in muestras:
        dt = datetime.fromisoformat(t)
        if inicio <= dt <= fin:
            por_minuto.setdefault(dt, []).append(v)
    return sorted((dt, statistics.fmean(vs)) for dt, vs in por_minuto.items())


def esfuerzos(muestras, start_local: str | None, duration_s: float | None) -> dict:
    """Ruling 3: this session's own hard efforts (peaks) and the heart-rate drop in the minute(s)
    after each one. {"suficiente": False, ...} -- the honest gap, never a guess -- under
    MODELO['minutos_min'] minutes of data or under MODELO['cobertura_min'] coverage of the
    session's own span. Otherwise {"suficiente": True, "n_picos": int, "pico": float | None (the
    highest detected peak), "recuperacion": float | None (the median drop, only with at least
    MODELO['caidas_min'] measured ones)}."""
    serie = serie_por_minuto(muestras, start_local, duration_s)
    total_min = (duration_s or 0) / 60
    cobertura = len(serie) / total_min if total_min else 0.0
    if len(serie) < MODELO["minutos_min"] or cobertura < MODELO["cobertura_min"]:
        return {"suficiente": False, "n_picos": 0, "pico": None, "recuperacion": None}

    umbral = _percentil([v for _, v in serie], MODELO["percentil_pico"])
    candidatas = []
    for i, (t, v) in enumerate(serie):
        if v < umbral:
            continue
        if i > 0 and _contiguos(serie[i - 1][0], t) and not (v > serie[i - 1][1]):
            continue
        if i + 1 < len(serie) and _contiguos(t, serie[i + 1][0]) and not (v >= serie[i + 1][1]):
            continue
        candidatas.append(i)

    separacion = timedelta(minutes=MODELO["separacion_min"])
    picos: list[tuple[int, datetime, float]] = []
    for i in candidatas:
        t, v = serie[i]
        if picos and (t - picos[-1][1]) < separacion:
            if v > picos[-1][2]:
                picos[-1] = (i, t, v)
        else:
            picos.append((i, t, v))

    caidas = []
    for i, t, v in picos:
        if i + 1 < len(serie):
            t2, v2 = serie[i + 1]
            if t2 - t == timedelta(minutes=MODELO["caida_minutos"]):
                caidas.append(v - v2)

    recuperacion = statistics.median(caidas) if len(caidas) >= MODELO["caidas_min"] else None
    pico = max((v for _, _, v in picos), default=None)
    return {"suficiente": True, "n_picos": len(picos), "pico": pico, "recuperacion": recuperacion}


def zonas(filas) -> dict | None:
    """Ruling 4: minutes per zone 1-5 and the zones-4/5 share, from the activity's own stored
    'zonas' detail (db.detalle_actividad's own (zone_number, seconds_in_zone, zone_low_bpm)
    tuples). None for an empty/never-stored marker -- no zone row, never a dict of zeros."""
    if not filas:
        return None
    minutos = {z: 0.0 for z in range(1, 6)}
    for zona_n, segundos, _low in filas:
        if zona_n in minutos:
            minutos[zona_n] += segundos / 60.0
    total = sum(minutos.values())
    alta = minutos[4] + minutos[5]
    return {"minutos": minutos, "total_min": total, "alta_min": alta,
            "alta_share": (alta / total) if total else None}


def _sesiones_comparables(conn, person_id: int, a: dict) -> list:
    """Ruling 5: the SAME selection entreno_sesion.comparaciones already makes (entreno_sesion.
    py:61) -- same type, or (its own ruling 4) the same folded name for Garmin's generic "other"
    bucket, the activity itself excluded, newest HISTORIAL_N first. Replicated rather than shared
    (entreno_sesion.py is not in this slice's touches); tests/test_esfuerzo_sesion.py proves both
    pick the same sessions for the same fixtures."""
    tipo = a.get("type")
    if not tipo:
        return []
    if tipo == entreno_sesion.TIPO_GENERICO:
        nombre_norm = entreno_sesion._normaliza_nombre(a.get("name"))
        candidatas = conn.execute(
            "SELECT source, source_id, name, start_local, duration_s FROM activity WHERE person_id = ? "
            "AND type = ? AND NOT (source = ? AND source_id = ?) ORDER BY day DESC, start_local DESC",
            (person_id, tipo, a["source"], a["source_id"])).fetchall()
        return [f for f in candidatas
                if entreno_sesion._normaliza_nombre(f["name"]) == nombre_norm][:HISTORIAL_N]
    return conn.execute(
        "SELECT source, source_id, start_local, duration_s FROM activity WHERE person_id = ? AND type = ? "
        "AND NOT (source = ? AND source_id = ?) ORDER BY day DESC, start_local DESC LIMIT ?",
        (person_id, tipo, a["source"], a["source_id"], HISTORIAL_N)).fetchall()


def historial(conn, person_id: int, a: dict) -> dict:
    """Ruling 5/8: the median, across the comparable sessions above that carry stored detail, of
    each figure the card compares -- zones-4/5 share, recovery drop -- or None under HISTORIAL_MIN.
    ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the session peak-minute heart rate is no longer one of
    them (the page's own max-HR comparison already covers that ground, with the watch's own true
    max rather than this model's per-minute mean). Read in ONE query against activity_detail,
    grouped by `source` (normally one group, since every Garmin activity shares the same source)
    rather than through db.detalle_actividad per session (ruling 8: up to HISTORIAL_N sessions x 2
    kinds would otherwise be one query each) -- parsed with the adapter's own parsers
    (garmin_adapter.parse_activity_hr_series/parse_hr_zones), the same db.detalle_actividad
    precedent (db.py:3086) without its per-activity query."""
    filas = _sesiones_comparables(conn, person_id, a)
    if not filas:
        return {"zona_alta": None, "caida": None}

    por_fuente: dict[str, list[str]] = {}
    for f in filas:
        por_fuente.setdefault(f["source"], []).append(f["source_id"])
    detalle_por_id: dict[tuple[str, str], list] = {}
    for source, ids in por_fuente.items():
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT activity_source_id AS source_id, kind, payload FROM activity_detail WHERE "
            f"person_id = ? AND activity_source = ? AND kind IN ('detalle', 'zonas') AND "
            f"activity_source_id IN ({placeholders})",
            (person_id, source, *ids)).fetchall()
        for r in rows:
            detalle_por_id.setdefault((source, r["source_id"]), []).append(r)

    if not detalle_por_id:
        return {"zona_alta": None, "caida": None}
    zona_persona = db.person(conn, person_id)["timezone"]

    alta_vals, caida_vals = [], []
    for f in filas:
        rows = detalle_por_id.get((f["source"], f["source_id"]))
        if not rows:
            continue
        muestras = zonas_filas = ()
        for r in rows:
            payload = json.loads(r["payload"])
            if r["kind"] == "detalle":
                muestras = garmin_adapter.parse_activity_hr_series(payload, zona_persona)
            else:
                zonas_filas = garmin_adapter.parse_hr_zones(payload)
        if muestras:
            esf = esfuerzos(muestras, f["start_local"], f["duration_s"])
            if esf["suficiente"] and esf["recuperacion"] is not None:
                caida_vals.append(esf["recuperacion"])
        if zonas_filas:
            z = zonas(zonas_filas)
            if z is not None and z["alta_share"] is not None:
                alta_vals.append(z["alta_share"])

    def _mediana(vals: list[float]) -> float | None:
        return statistics.median(vals) if len(vals) >= HISTORIAL_MIN else None

    return {"zona_alta": _mediana(alta_vals), "caida": _mediana(caida_vals)}


def clasifica(actual: dict, habituales: dict) -> dict:
    """Ruling 5: each figure's own three-way code against its own MODELO band -- "mas" (more/
    higher/faster), "menos" (less/lower/slower), "igual" inside the band, None when either side is
    missing."""
    def _codigo(valor, habitual, banda):
        if valor is None or habitual is None:
            return None
        diff = valor - habitual
        if diff > banda:
            return "mas"
        if diff < -banda:
            return "menos"
        return "igual"

    zona_alta_pct = actual.get("zona_alta") * 100 if actual.get("zona_alta") is not None else None
    zona_alta_habitual_pct = habituales["zona_alta"] * 100 if habituales["zona_alta"] is not None else None
    return {
        "zona_alta": _codigo(zona_alta_pct, zona_alta_habitual_pct, MODELO["banda_zona_alta_pts"]),
        "caida": _codigo(actual.get("caida"), habituales["caida"], MODELO["banda_caida_ppm"]),
    }


def tarjeta(conn, person_id: int, a: dict) -> dict | None:
    """Ruling 1/6: everything the per-session page's effort card needs, read through
    db.detalle_actividad only -- None when this activity has no stored 'detalle' or 'zonas' at
    all, the card's own presence gate (ruling 6): the page is then unchanged."""
    muestras = db.detalle_actividad(conn, person_id, a["source"], a["source_id"], "detalle")
    zonas_filas = db.detalle_actividad(conn, person_id, a["source"], a["source_id"], "zonas")
    if not muestras and not zonas_filas:
        return None
    esf = esfuerzos(muestras, a["start_local"], a["duration_s"])
    zon = zonas(zonas_filas)
    actual = {
        "zona_alta": zon["alta_share"] if zon is not None else None,
        "caida": esf["recuperacion"] if esf["suficiente"] else None,
    }
    habituales = historial(conn, person_id, a)
    return {
        "tiene_detalle": bool(muestras), "esfuerzos": esf, "zonas": zon,
        "clasificacion": clasifica(actual, habituales),
    }


def curva_actividad(conn, person_id: int, a: dict) -> dict | None:
    """Ruling 7: the activity's own per-minute series (ruling 2), in entreno_sesion.curva's own
    shape (puntos/inicio/fin/min/max) -- for the page's existing _curva_svg when the day-level
    series is absent. `picos`/`caidas` are left empty (this slice's own choice, named in
    DECISIONS.md): this fallback curve carries no per-set annotation, only the raw line."""
    if not a["start_local"] or a["duration_s"] is None:
        return None
    muestras = db.detalle_actividad(conn, person_id, a["source"], a["source_id"], "detalle")
    serie = serie_por_minuto(muestras, a["start_local"], a["duration_s"])
    if not serie:
        return None
    inicio = datetime.fromisoformat(a["start_local"])
    return {
        "puntos": serie, "inicio": inicio, "fin": inicio + timedelta(seconds=a["duration_s"]),
        "min": min(v for _, v in serie), "max": max(v for _, v in serie),
        "picos": {}, "caidas": {},
    }
