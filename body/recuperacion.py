"""Carlos's study 1 (vida/estudios.md, relayed by the Personal desk, 2026-11-09): how long until his
heart rate returns to his resting range after a workout, and what changes it.

Pure reads over a connection and a person id, no web imports -- the same shape as
body/turnos_costo.py and body/baselines.py. Every number here is an ESTIMATE, versioned
(`MODELO["version"]`), against the person's OWN resting range and OWN history, never a population
table (docs/DESIGN.md rules 4, 5, 12). The intraday window-reading helpers (`_muestras_dias`,
`_espaciado_mediana_min`) are IMPORTED from body/experimentos.py, not duplicated
(ORCH-CARLOS-DELEGATES-TIER-1-1's approval reason requires this).
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import baselines, cuenta, db
from .experimentos import _espaciado_mediana_min, _muestras_dias, _sin_offset

VERSION = "recuperacion-v1"
MODELO = {
    "version": VERSION,
    # bpm added to the person's own resting-range upper bound (baselines.baseline('resting_hr',
    # day).high): "at the edge of normal" should count as recovered, not a strict <=.
    "margen_bpm": 3,
    # Minutes heart rate must STAY at or under the cutoff before it counts as recovered -- rules
    # out a brief dip mid-recovery that rises again (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2).
    "sostenido_min": 5,
    # Hours after the activity's end this module searches -- beyond this, an honest
    # "fuera_de_horizonte" rather than an open-ended search (ruling 3).
    "horizonte_h": 4,
    # Below this fraction of the samples the window's own observed spacing implies, the window is
    # a coverage gap, not a thin-but-honest reading -- same convention as
    # experimentos.COBERTURA_MIN, versioned here since it is this model's own floor.
    "cobertura_min": 0.5,
    # Fewer sessions than this in a factor group: "pocos datos", never a median that is really
    # 1-2 points (ruling 4).
    "min_n_grupo": 5,
    # Below this many minutes, a group difference is noise, not a finding -- roughly what
    # day-to-day measurement noise in a single recovery read could produce (ruling 4).
    "ruido_min_diferencia_min": 10.0,
    "max_hallazgos": 3,
}

REASON_CODES = db.RECUPERACION_MOTIVOS


@dataclass
class Resultado:
    minutos: float | None
    motivo: str | None  # one of REASON_CODES, or None when `minutos` is set


def _fin_actividad(activity_row) -> datetime | None:
    start_local = activity_row["start_local"]
    duration_s = activity_row["duration_s"]
    if start_local is None or duration_s is None:
        return None
    return _sin_offset(start_local) + timedelta(seconds=duration_s)


def recuperacion_actividad(conn, person_id: int, activity_row) -> Resultado:
    """Minutes from this activity's END (start_local + duration_s) to "back to resting": the first
    minute from which heart rate stays at or under the person's OWN usual resting-range upper
    bound plus MODELO's margin, for MODELO's sustained span -- or a reason code (REASON_CODES)
    when that cannot be said honestly."""
    day = activity_row["day"]
    if db.dias_marcados(conn, person_id, day, day):
        return Resultado(None, "marcado")

    fin = _fin_actividad(activity_row)
    if fin is None:
        return Resultado(None, "sin_cobertura")

    base = baselines.baseline(conn, "resting_hr", day, person_id=person_id)
    if base is None:
        return Resultado(None, "sin_rango")
    cutoff = base.high + MODELO["margen_bpm"]

    zona = db.person(conn, person_id)["timezone"]
    limite = fin + timedelta(hours=MODELO["horizonte_h"])
    dias_ventana = sorted({fin.date().isoformat(), limite.date().isoformat()})
    muestras = _muestras_dias(conn, person_id, "intradia_fc", dias_ventana, zona)
    en_ventana = sorted((ts, v) for ts, v in muestras if fin <= _sin_offset(ts) <= limite)
    if not en_ventana:
        return Resultado(None, "sin_cobertura")

    espaciado = _espaciado_mediana_min(en_ventana)
    if espaciado is None:
        return Resultado(None, "sin_cobertura")
    esperadas = (limite - fin).total_seconds() / 60 / espaciado
    if len(en_ventana) < MODELO["cobertura_min"] * esperadas:
        return Resultado(None, "sin_cobertura")

    muestras_t = sorted((_sin_offset(ts), v) for ts, v in en_ventana)
    for i, (t, v) in enumerate(muestras_t):
        if v > cutoff:
            continue
        tope = t + timedelta(minutes=MODELO["sostenido_min"])
        if all(v2 <= cutoff for t2, v2 in muestras_t[i:] if t2 <= tope):
            return Resultado(max(round((t - fin).total_seconds() / 60), 0), None)
    return Resultado(None, "fuera_de_horizonte")


def recalcular(conn, person_id: int) -> dict:
    """Fills `recuperacion_actividad` for every activity of this person with no row yet under
    MODELO's current version -- idempotent, so a re-run after the first is cheap. Returns counts
    only (never a health figure): how many rows were written and a tally of reason codes among
    them."""
    version = MODELO["version"]
    rows = conn.execute(
        "SELECT a.source AS source, a.source_id AS source_id, a.day AS day, "
        "a.start_local AS start_local, a.duration_s AS duration_s FROM activity a "
        "WHERE a.person_id = ? AND NOT EXISTS ("
        "SELECT 1 FROM recuperacion_actividad r WHERE r.person_id = a.person_id "
        "AND r.activity_source = a.source AND r.activity_source_id = a.source_id "
        "AND r.modelo_version = ?)",
        (person_id, version),
    ).fetchall()
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    motivos: dict[str, int] = {}
    nuevas = 0
    with conn:
        for r in rows:
            resultado = recuperacion_actividad(conn, person_id, r)
            conn.execute(
                "INSERT OR REPLACE INTO recuperacion_actividad "
                "(person_id, activity_source, activity_source_id, modelo_version, minutos, motivo, "
                "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (person_id, r["source"], r["source_id"], version, resultado.minutos, resultado.motivo, ahora),
            )
            nuevas += 1
            if resultado.motivo is not None:
                motivos[resultado.motivo] = motivos.get(resultado.motivo, 0) + 1
    return {"nuevas": nuevas, "motivos": motivos}


def _bucket_hora(start_local: str | None) -> str:
    if not start_local:
        return "desconocida"
    hora = _sin_offset(start_local).hour
    if 5 <= hora <= 11:
        return "manana"
    if 12 <= hora <= 18:
        return "tarde"
    return "noche"


def _bucket_tercil(duration_s: float, terciles: list[float]) -> str:
    if duration_s < terciles[0]:
        return "corta"
    if duration_s < terciles[1]:
        return "media"
    return "larga"


def estudio(conn, person_id: int) -> dict:
    """Over this person's own derived rows (current MODELO version, `minutos` not NULL): the
    overall median and spread (p25-p75) with n, each factor's groups (ruling 4) with their own
    median and n, and the top MODELO['max_hallazgos'] group differences as ready-to-word findings
    -- a difference under MODELO's noise floor is never a finding. One SQL statement for the
    derived rows (joined to `activity` for day/start_local/duration_s/type), plus one each for the
    shift kind and the sleep/HRV figures of the days involved -- three statements total, however
    many rows this person has."""
    version = MODELO["version"]
    rows = conn.execute(
        "SELECT ra.minutos AS minutos, a.day AS day, a.start_local AS start_local, "
        "a.duration_s AS duration_s, a.type AS type FROM recuperacion_actividad ra "
        "JOIN activity a ON a.person_id = ra.person_id AND a.source = ra.activity_source "
        "AND a.source_id = ra.activity_source_id "
        "WHERE ra.person_id = ? AND ra.modelo_version = ? AND ra.minutos IS NOT NULL",
        (person_id, version),
    ).fetchall()
    if not rows:
        return {"n": 0, "mediana": None, "bajo": None, "alto": None, "hallazgos": []}

    minutos_all = [r["minutos"] for r in rows]
    cuartiles = statistics.quantiles(minutos_all, n=4) if len(minutos_all) >= 4 else None
    out = {
        "n": len(rows),
        "mediana": round(statistics.median(minutos_all)),
        "bajo": round(cuartiles[0]) if cuartiles else None,
        "alto": round(cuartiles[2]) if cuartiles else None,
    }

    dias = sorted({r["day"] for r in rows})
    placeholders = ",".join("?" * len(dias))
    shift_rows = conn.execute(
        f"SELECT day, kind FROM shift WHERE person_id = ? AND day IN ({placeholders}) ORDER BY start_local",
        (person_id, *dias),
    ).fetchall()
    kind_por_dia: dict[str, str] = {}
    for r in shift_rows:
        kind_por_dia.setdefault(r["day"], r["kind"])

    metric_rows = conn.execute(
        f"SELECT day, metric_key, value FROM daily_metric WHERE person_id = ? "
        f"AND metric_key IN ('sleep_minutes', 'hrv_last_night') AND day IN ({placeholders})",
        (person_id, *dias),
    ).fetchall()
    sleep_por_dia: dict[str, float] = {}
    hrv_por_dia: dict[str, float] = {}
    for r in metric_rows:
        (sleep_por_dia if r["metric_key"] == "sleep_minutes" else hrv_por_dia)[r["day"]] = r["value"]
    sleep_mediana = statistics.median(sleep_por_dia.values()) if sleep_por_dia else None
    hrv_mediana = statistics.median(hrv_por_dia.values()) if hrv_por_dia else None

    duraciones = [r["duration_s"] for r in rows if r["duration_s"] is not None]
    terciles = statistics.quantiles(duraciones, n=3) if len(duraciones) >= 3 else None

    grupos: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        m, day = r["minutos"], r["day"]
        if day in sleep_por_dia and sleep_mediana is not None:
            grupos["sueno_anoche"]["corta" if sleep_por_dia[day] < sleep_mediana else "normal"].append(m)
        if day in hrv_por_dia and hrv_mediana is not None:
            grupos["hrv_anoche"]["baja" if hrv_por_dia[day] < hrv_mediana else "normal"].append(m)
        kind = kind_por_dia.get(day)
        if kind is not None:
            grupos["turno"][kind].append(m)
        grupos["hora_dia"][_bucket_hora(r["start_local"])].append(m)
        grupos["tipo_sesion"][r["type"] or "otro"].append(m)
        if terciles is not None and r["duration_s"] is not None:
            grupos["duracion_tercil"][_bucket_tercil(r["duration_s"], terciles)].append(m)

    hallazgos = []
    for factor, por_grupo in grupos.items():
        calificados = {k: v for k, v in por_grupo.items() if len(v) >= MODELO["min_n_grupo"]}
        if len(calificados) < 2:
            continue
        medianas = {k: statistics.median(v) for k, v in calificados.items()}
        peor_k = max(medianas, key=lambda k: medianas[k])
        mejor_k = min(medianas, key=lambda k: medianas[k])
        diff = medianas[peor_k] - medianas[mejor_k]
        if diff < MODELO["ruido_min_diferencia_min"]:
            continue
        hallazgos.append({
            "factor": factor, "peor": peor_k, "mejor": mejor_k,
            "min_peor": round(medianas[peor_k]), "min_mejor": round(medianas[mejor_k]),
            "n_peor": len(calificados[peor_k]), "n_mejor": len(calificados[mejor_k]),
            "diff": diff,
        })
    hallazgos.sort(key=lambda h: -h["diff"])

    out["hallazgos"] = hallazgos[:MODELO["max_hallazgos"]]
    return out


def main(argv: list[str] | None = None) -> int:
    """`python -m body.recuperacion` -- the vivo task `recuperacion` (body/vivo.py's TAREAS), run
    once for the first backfill and safe to re-run (`recalcular` is idempotent). Hosted: every
    connected person; local: the one local person. Prints totals only, never a person id or a
    health figure."""
    conn = db.connect()
    try:
        db.ensure_ready(conn)
        from . import garmin_cuenta
        person_ids = garmin_cuenta.every_person_id(conn) if cuenta.hosted() else [db.local_person_id(conn)]
        nuevas = 0
        motivos: dict[str, int] = {}
        for person_id in person_ids:
            resultado = recalcular(conn, person_id)
            nuevas += resultado["nuevas"]
            for k, v in resultado["motivos"].items():
                motivos[k] = motivos.get(k, 0) + v
        print(f"recuperacion: {nuevas} filas nuevas en {len(person_ids)} persona(s), motivos {motivos}",
              flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
