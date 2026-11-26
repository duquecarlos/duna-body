"""Body's own per-muscle-region training state (BODY-CUERPO-ESTADO-MUSCULO-1, following
BODY-CUERPO-ESTADO-INV's lead). Version 1, 2026-11-26.

Carlos's own direction, relayed: each muscle on the (hidden) 3D avatar lights on its own from his
history -- undertrained, well trained, overtrained, recovering -- not today's glow alone.

The unit is effective sets per region (training-science's usual count, so bodyweight, weighted and
timed sets add up the same way), read the same corrected/excluded way `_entrenado_hoy_categorias`
(body/rutas/hoy.py) already reads a day's workout_set rows.

v1's stated assumption (ruling 3): a watch activity with no active sets of its own (a run, a ride,
a swim) still loads the muscles it clearly works -- ACTIVIDAD_GRUPOS below -- worth
`series_equivalentes_actividad` sets per region it maps to, SKIPPED entirely when that same
activity already has its own counted sets (those sets, and only those, count then).

Pure and as-of a `day` parameter -- never "today" specially -- so a past day, or a future time
slider, calls the same function. compute() is the one entry point; everything else here is data or
a private helper.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from . import anatomia, db

VERSION = "musculo-v1"

# Every constant the model uses, together, so a later version (musculo-v2) changes data here, not
# code -- the same promise CATEGORIA_GRUPOS makes in anatomia.py.
MODELO: dict[str, float] = {
    "series_equivalentes_actividad": 3,
    "min_semanas_base": 2,
    "base_minima_series": 2,
    "ratio_sobrecarga": 1.5,
    "ratio_subentreno": 0.5,
    "horas_recuperacion": 48,
}

_DIAS_ACUTE = 7
_DIAS_BASE = 28

# Ruling 3: body/history.py's own KNOWN_ACTIVITY_TYPES, limited to the ones that clearly load a
# muscle group with no strength set of their own -- cardio, yoga and everything else map to
# nothing, never guessed.
ACTIVIDAD_GRUPOS: dict[str, frozenset[str]] = {
    "running": frozenset({"cuadriceps", "isquios", "gluteos"}),
    "trail_running": frozenset({"cuadriceps", "isquios", "gluteos"}),
    "treadmill_running": frozenset({"cuadriceps", "isquios", "gluteos"}),
    "track_running": frozenset({"cuadriceps", "isquios", "gluteos"}),
    "walking": frozenset({"cuadriceps", "isquios", "gluteos"}),
    "hiking": frozenset({"cuadriceps", "isquios", "gluteos"}),
    "cycling": frozenset({"cuadriceps", "gluteos"}),
    "indoor_cycling": frozenset({"cuadriceps", "gluteos"}),
    "mountain_biking": frozenset({"cuadriceps", "gluteos"}),
    "road_biking": frozenset({"cuadriceps", "gluteos"}),
    "swimming": frozenset({"dorsales", "hombros"}),
    "lap_swimming": frozenset({"dorsales", "hombros"}),
    "open_water_swimming": frozenset({"dorsales", "hombros"}),
    "elliptical": frozenset({"cuadriceps", "gluteos"}),
}


@dataclass
class Estado:
    estado: str
    series_7d: float
    base_semanal: float | None  # sets/week; None when fewer than MODELO["min_semanas_base"] weeks of history
    dias_desde_ultima: int | None  # None when this region has never had a counted set or mapped activity
    version: str = VERSION


def _rango(desde: date, hasta: date):
    cur = desde
    while cur <= hasta:
        yield cur.isoformat()
        cur += timedelta(1)


def _series_por_dia(conn, person_id: int, desde: date, hasta: date) -> dict[str, dict[str, int]]:
    """Effective sets per region, per day, over [desde, hasta] -- two queries total (activities,
    then their sets), never one pair per day. Linking (db.merge_linked_training) only ever pairs
    rows from the SAME day, so rows are grouped by day before merging."""
    rows = conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day BETWEEN ? AND ? ORDER BY day, start_local",
        (person_id, desde.isoformat(), hasta.isoformat())).fetchall()
    por_dia: dict[str, list] = {}
    for r in rows:
        por_dia.setdefault(r["day"], []).append(r)
    merged_por_dia: dict[str, list] = {}
    all_ids: list[str] = []
    for dia, filas in por_dia.items():
        merged = db.merge_linked_training(filas)
        merged_por_dia[dia] = merged
        all_ids.extend(a["sets_source_id"] for a in merged)

    categorias_por_actividad: dict[str, list[str]] = {}
    if all_ids:
        placeholders = ",".join("?" * len(all_ids))
        sets = conn.execute(
            f"SELECT activity_source_id, set_type, corrected_by_person, excluded_by_person, "
            f"garmin_category, person_category FROM workout_set WHERE person_id = ? AND "
            f"activity_source_id IN ({placeholders})",
            (person_id, *all_ids)).fetchall()
        for s in sets:
            corrected = bool(s["corrected_by_person"])
            if (("active" if corrected else s["set_type"]) != "active") or bool(s["excluded_by_person"]):
                continue
            cat = s["person_category"] if corrected else s["garmin_category"]
            if cat:
                categorias_por_actividad.setdefault(s["activity_source_id"], []).append(cat)

    out: dict[str, dict[str, int]] = {}
    for dia in _rango(desde, hasta):
        conteo: dict[str, int] = {}
        for a in merged_por_dia.get(dia, []):
            cats = categorias_por_actividad.get(a["sets_source_id"], [])
            if cats:
                for c in cats:
                    for region in anatomia.CATEGORIA_GRUPOS.get(c.strip().lower(), frozenset()):
                        conteo[region] = conteo.get(region, 0) + 1
            else:
                for region in ACTIVIDAD_GRUPOS.get(a["type"], frozenset()):
                    conteo[region] = conteo.get(region, 0) + MODELO["series_equivalentes_actividad"]
        out[dia] = conteo
    return out


def compute(conn, day: str, person_id: int) -> dict[str, "Estado"]:
    """One Estado per MUSCLE region of anatomia.REGIONES (organs are not this model's), as of
    `day` -- a past day is a parameter, never a special case."""
    d = date.fromisoformat(day)
    acute_start = d - timedelta(_DIAS_ACUTE - 1)
    base_end = acute_start - timedelta(1)
    base_start = base_end - timedelta(_DIAS_BASE - 1)

    dias_counts = _series_por_dia(conn, person_id, base_start, d)
    marcados = db.dias_marcados(conn, person_id, base_start.isoformat(), base_end.isoformat())

    hay_datos = any(dias_counts[dia] for dia in dias_counts)
    ultima_fecha: dict[str, str] = {}
    for dia in _rango(base_start, d):
        for region in dias_counts.get(dia, {}):
            ultima_fecha[region] = dia  # walking forward in date order: last write wins

    resultados: dict[str, Estado] = {}
    for region in anatomia.REGIONES:
        if region["tipo"] != "musculo":
            continue
        clave = region["clave"]

        series_7d = sum(dias_counts[dia].get(clave, 0) for dia in _rango(acute_start, d))

        semanas: set[tuple[int, int]] = set()
        total_base = 0
        for dia in _rango(base_start, base_end):
            if dia in marcados:
                continue
            c = dias_counts[dia].get(clave, 0)
            if c:
                total_base += c
                semanas.add(date.fromisoformat(dia).isocalendar()[:2])
        base_semanal = (total_base / 4) if len(semanas) >= MODELO["min_semanas_base"] else None

        dias_desde_ultima = (d - date.fromisoformat(ultima_fecha[clave])).days if clave in ultima_fecha else None

        if not hay_datos:
            estado = "sin_datos"
        elif (base_semanal is not None and base_semanal >= MODELO["base_minima_series"]
              and series_7d > MODELO["ratio_sobrecarga"] * base_semanal):
            estado = "sobrecargado"
        elif dias_desde_ultima is not None and dias_desde_ultima < MODELO["horas_recuperacion"] / 24:
            estado = "recuperando"
        elif series_7d == 0 or (base_semanal is not None and series_7d < MODELO["ratio_subentreno"] * base_semanal):
            estado = "subentrenado"
        else:
            estado = "en_rango"

        resultados[clave] = Estado(estado, series_7d, base_semanal, dias_desde_ultima)
    return resultados
