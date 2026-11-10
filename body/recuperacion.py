"""Carlos's study 1 (vida/estudios.md, relayed by the Personal desk, 2026-11-09): how long until his
heart rate returns to his OWN usual level for that clock hour after a workout, and what changes it.

Pure reads over a connection and a person id, no web imports -- the same shape as
body/turnos_costo.py and body/baselines.py. Every number here is an ESTIMATE, versioned
(`MODELO["version"]`), against the person's OWN history, never a population table (docs/DESIGN.md
rules 4, 5, 12). The intraday spacing/offset helpers (`_espaciado_mediana_min`, `_sin_offset`) are
IMPORTED from body/experimentos.py, not duplicated (ORCH-CARLOS-DELEGATES-TIER-1-1's approval
reason requires this).

BODY-RECUPERACION-1-C replaces ruling 2 of BODY-RECUPERACION-1: the orchestrator measured v1 on a
copy of the real database (counts only, copy deleted after) -- of 392 activities, 297 came out
`fuera_de_horizonte`, 39 `sin_cobertura`, 1 `sin_rango`, 55 recovered, and 36 of those 55 took
90-240 min. v1's target was the DAILY resting-HR range (baselines.baseline('resting_hr', ...),
Garmin's lowest stretch of the day, mostly asleep) -- an awake desk hour or a shift hour seldom
drops that low, so v1 called an ordinary awake afternoon "never recovered". v2's target is the
person's OWN usual heart rate FOR THAT CLOCK HOUR (`_usual_hora`): a desk hour, a shift hour and an
evening hour each have their own, computed from MODELO['dias_historia'] days of that same person's
own history, never a population figure.
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import cuenta, db
from .experimentos import _espaciado_mediana_min, _sin_offset

VERSION = "recuperacion-v2"
MODELO = {
    "version": VERSION,
    # bpm added to the person's own usual-for-that-hour level (_usual_hora): "at the edge of
    # normal" should count as recovered, not a strict <=.
    "margen_bpm": 3,
    # Minutes heart rate must STAY at or under the cutoff before it counts as recovered -- rules
    # out a brief dip mid-recovery that rises again (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2).
    "sostenido_min": 5,
    # Hours after the activity's end this module searches -- beyond this, an honest
    # "fuera_de_horizonte" rather than an open-ended search (ruling 3).
    "horizonte_h": 4,
    # Below this fraction of the samples a window's own observed spacing implies, the window is a
    # coverage gap, not a thin-but-honest reading -- same convention as experimentos.COBERTURA_MIN,
    # versioned here since it is this model's own floor. Reused for BOTH the horizon window's own
    # coverage check and each (day, hour) pair's own coverage check inside `_usual_hora`.
    "cobertura_min": 0.5,
    # How many days before the activity's own day `_usual_hora` looks back for that clock hour's
    # own usual level -- the natural default (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2), same idea
    # as baselines.WINDOW_DAYS.
    "dias_historia": 30,
    # Fewer qualifying days than this for a given clock hour (not marked, no activity of the
    # person's own in that hour, enough coverage) and there is no honest "usual" yet for that
    # hour -- "sin_rango". A little higher than baselines.MIN_DAYS (7): an hour's own history is
    # thinner than a whole day's, since many ordinary days still get excluded by the activity-hour
    # and marked-day filters.
    "min_dias_horario": 10,
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


def _leer_dia(conn, person_id: int, dia: str, zona: str, cache: dict) -> dict:
    """This one day's intraday_fc series, read via db.intradia AT MOST ONCE per `cache` (a per-run
    dict passed in by the caller) -- the bucket-by-clock-hour view and the day's own observed
    spacing are derived from that single read and cached alongside it, so neither the horizon
    search nor `_usual_hora` ever re-reads a day already in `cache`."""
    dias_cache = cache.setdefault("dias", {})
    if dia in dias_cache:
        return dias_cache[dia]
    muestras = db.intradia(conn, person_id, dia, "intradia_fc", zona)
    por_hora: dict[int, list[float]] = defaultdict(list)
    for ts, v in muestras:
        por_hora[_sin_offset(ts).hour].append(v)
    entry = {"muestras": muestras, "por_hora": por_hora, "espaciado": _espaciado_mediana_min(muestras)}
    dias_cache[dia] = entry
    return entry


def _horas_actividad_por_dia(conn, person_id: int, cache: dict) -> dict[str, set[int]]:
    """Every clock hour, on every day, touched by one of this person's OWN activities -- read in
    ONE query per `cache` (every activity this person has, regardless of model version), not once
    per activity per look-back day. `_usual_hora` excludes a (day, hour) pair from "ordinary" when
    it is here, per ruling 2: a desk hour's usual must never include the activity itself."""
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


def _usual_hora(conn, person_id: int, zona: str, dia_actividad: str, hora: int, cache: dict) -> tuple[float | None, int]:
    """The person's OWN usual heart rate for clock hour `hora`, as the median of every sample
    recorded during that hour over MODELO['dias_historia'] days strictly before `dia_actividad`
    (that day excluded, same convention as baselines.baseline) -- counting only (day, hour) pairs
    where the day is not marked, that hour holds no activity of the person's own
    (`_horas_actividad_por_dia`), and that hour's own sample count meets MODELO['cobertura_min']
    against the day's own observed spacing. Returns (usual, n_dias_calificados); usual is None
    (honest "sin_rango") when n_dias_calificados < MODELO['min_dias_horario']. Memoized per
    (dia_actividad, hora) in `cache` -- several activities on the same day sharing an hour, or the
    same activity touching several hours, never repeat this walk."""
    memo = cache.setdefault("usual", {})
    clave = (dia_actividad, hora)
    if clave in memo:
        return memo[clave]

    d = date.fromisoformat(dia_actividad)
    desde = d - timedelta(days=MODELO["dias_historia"])
    hasta = d - timedelta(days=1)
    marcados = db.dias_marcados(conn, person_id, desde.isoformat(), hasta.isoformat())
    horas_actividad = _horas_actividad_por_dia(conn, person_id, cache)

    valores: list[float] = []
    dias_calificados = 0
    dia_iter = desde
    while dia_iter <= hasta:
        dia_iso = dia_iter.isoformat()
        dia_iter += timedelta(days=1)
        if dia_iso in marcados or hora in horas_actividad.get(dia_iso, ()):
            continue
        entry = _leer_dia(conn, person_id, dia_iso, zona, cache)
        if entry["espaciado"] is None:
            continue
        muestras_hora = entry["por_hora"].get(hora, [])
        esperadas = 60 / entry["espaciado"]
        if len(muestras_hora) < MODELO["cobertura_min"] * esperadas:
            continue
        valores.extend(muestras_hora)
        dias_calificados += 1

    resultado = ((statistics.median(valores), dias_calificados) if dias_calificados >= MODELO["min_dias_horario"]
                 else (None, dias_calificados))
    memo[clave] = resultado
    return resultado


def recuperacion_actividad(conn, person_id: int, activity_row, cache: dict | None = None) -> Resultado:
    """Minutes from this activity's END (start_local + duration_s) to "back to normal": the first
    minute from which heart rate stays at or under the person's OWN usual level FOR THAT CLOCK
    HOUR (`_usual_hora`) plus MODELO's margin, for MODELO's sustained span -- or a reason code
    (REASON_CODES) when that cannot be said honestly. `cache` is a per-run dict (see `_leer_dia`)
    shared across many calls by `recalcular`; a bare call (tests, a single lookup) gets its own,
    scoped to that one call."""
    if cache is None:
        cache = {}
    day = activity_row["day"]
    if db.dias_marcados(conn, person_id, day, day):
        return Resultado(None, "marcado")

    fin = _fin_actividad(activity_row)
    if fin is None:
        return Resultado(None, "sin_cobertura")

    zona = db.person(conn, person_id)["timezone"]
    limite = fin + timedelta(hours=MODELO["horizonte_h"])
    dias_ventana = sorted({fin.date().isoformat(), limite.date().isoformat()})
    muestras: list[tuple[str, float]] = []
    for dia in dias_ventana:
        muestras.extend(_leer_dia(conn, person_id, dia, zona, cache)["muestras"])
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
    horas_tocadas = sorted({t.hour for t, _ in muestras_t})
    cutoffs: dict[int, float] = {}
    for hora in horas_tocadas:
        usual, _n_dias = _usual_hora(conn, person_id, zona, day, hora, cache)
        if usual is None:
            return Resultado(None, "sin_rango")
        cutoffs[hora] = usual + MODELO["margen_bpm"]

    for i, (t, v) in enumerate(muestras_t):
        if v > cutoffs[t.hour]:
            continue
        tope = t + timedelta(minutes=MODELO["sostenido_min"])
        if all(v2 <= cutoffs[t2.hour] for t2, v2 in muestras_t[i:] if t2 <= tope):
            return Resultado(max(round((t - fin).total_seconds() / 60), 0), None)
    return Resultado(None, "fuera_de_horizonte")


def recalcular(conn, person_id: int) -> dict:
    """Fills `recuperacion_actividad` for every activity of this person with no row yet under
    MODELO's current version -- idempotent, so a re-run after the first is cheap. A version bump
    (MODELO['version'] changed) simply recomputes: the NOT EXISTS clause below finds no row under
    the new version and fills every one, leaving the old version's rows in place (ruling 1).
    Shares ONE `cache` dict (see `_leer_dia`) across every activity in this call, so a day touched
    by several activities' horizon or look-back windows is read via db.intradia at most once for
    this whole run -- not once per activity per look-back day. Returns counts only (never a health
    figure): how many rows were written and a tally of reason codes among them."""
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
    cache: dict = {}
    with conn:
        for r in rows:
            resultado = recuperacion_actividad(conn, person_id, r, cache)
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


def _mediana_censurada(valores: list[float], censurados: int, horizonte_min: float) -> dict:
    """The median over EVERY session that has an answer (point 2 of BODY-RECUPERACION-1-C): a
    `fuera_de_horizonte` session counts too, as right-censored -- its true minute is unknown, but
    it IS more than `horizonte_min` (true even though the number itself is unknown), so it sorts
    above every finite `valores` entry. Combined n = len(valores) + censurados. The median is
    still honestly defined while UNDER half of n is censored: sort `valores` ascending, and if the
    combined median position(s) fall at or past the end of that sorted list, at least half of n is
    censored and there is no single honest number -- "mas_de" (>= horizonte_min) instead of
    "mediana"."""
    n = len(valores) + censurados
    if n == 0:
        return {"mediana": None, "mas_de": None, "n": 0}
    ordenados = sorted(valores)
    posiciones = [n // 2] if n % 2 else [n // 2 - 1, n // 2]  # 0-indexed
    if any(p >= len(ordenados) for p in posiciones):
        return {"mediana": None, "mas_de": horizonte_min, "n": n}
    return {"mediana": statistics.fmean(ordenados[p] for p in posiciones), "mas_de": None, "n": n}


def _valor_orden(resultado: dict) -> float:
    """A single comparable number for `_mediana_censurada`'s result -- its own median when one is
    defined, else `mas_de` itself: an honest LOWER bound ("at least this long"), good enough to
    rank one group against another even when neither group's own exact figure is known."""
    return resultado["mediana"] if resultado["mediana"] is not None else resultado["mas_de"]


def estudio(conn, person_id: int) -> dict:
    """Over this person's own derived rows (current MODELO version): the overall median and
    spread (p25-p75, over recovered sessions only) with n, each factor's groups (ruling 4) with
    their own median and n, and the top MODELO['max_hallazgos'] group differences as ready-to-word
    findings -- a difference under MODELO's noise floor is never a finding. Every median here is
    `_mediana_censurada`'s: a `fuera_de_horizonte` session counts as right-censored, never simply
    dropped (point 2). One SQL statement for the derived rows (joined to `activity` for
    day/start_local/duration_s/type), plus one each for the shift kind and the sleep/HRV figures
    of the days involved -- three statements total, however many rows this person has."""
    version = MODELO["version"]
    horizonte_min = MODELO["horizonte_h"] * 60
    rows = conn.execute(
        "SELECT ra.minutos AS minutos, ra.motivo AS motivo, a.day AS day, a.start_local AS start_local, "
        "a.duration_s AS duration_s, a.type AS type FROM recuperacion_actividad ra "
        "JOIN activity a ON a.person_id = ra.person_id AND a.source = ra.activity_source "
        "AND a.source_id = ra.activity_source_id "
        "WHERE ra.person_id = ? AND ra.modelo_version = ? "
        "AND (ra.minutos IS NOT NULL OR ra.motivo = 'fuera_de_horizonte')",
        (person_id, version),
    ).fetchall()
    if not rows:
        return {"n": 0, "n_censurados": 0, "mediana": None, "mas_de": None, "bajo": None, "alto": None,
                "hallazgos": []}

    minutos_all = [r["minutos"] for r in rows if r["minutos"] is not None]
    n_censurados_all = sum(1 for r in rows if r["motivo"] == "fuera_de_horizonte")
    cuartiles = statistics.quantiles(minutos_all, n=4) if len(minutos_all) >= 4 else None
    resumen = _mediana_censurada(minutos_all, n_censurados_all, horizonte_min)
    out = {
        "n": len(rows),
        "n_censurados": n_censurados_all,
        "mediana": round(resumen["mediana"]) if resumen["mediana"] is not None else None,
        "mas_de": resumen["mas_de"],
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

    # Each group holds its OWN finite minutes AND its OWN censored count (point 2: "the same rule
    # inside every factor group") -- a fuera_de_horizonte session still has a day/start_local/
    # duration_s/type, so it still belongs to a sleep bucket, a shift, an hour-of-day and so on.
    grupos: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"valores": [], "censurados": 0}))
    for r in rows:
        m, day, censurado = r["minutos"], r["day"], r["motivo"] == "fuera_de_horizonte"

        def _agregar(factor: str, grupo: str) -> None:
            celda = grupos[factor][grupo]
            if censurado:
                celda["censurados"] += 1
            else:
                celda["valores"].append(m)

        if day in sleep_por_dia and sleep_mediana is not None:
            _agregar("sueno_anoche", "corta" if sleep_por_dia[day] < sleep_mediana else "normal")
        if day in hrv_por_dia and hrv_mediana is not None:
            _agregar("hrv_anoche", "baja" if hrv_por_dia[day] < hrv_mediana else "normal")
        kind = kind_por_dia.get(day)
        if kind is not None:
            _agregar("turno", kind)
        _agregar("hora_dia", _bucket_hora(r["start_local"]))
        _agregar("tipo_sesion", r["type"] or "otro")
        if terciles is not None and r["duration_s"] is not None:
            _agregar("duracion_tercil", _bucket_tercil(r["duration_s"], terciles))

    hallazgos = []
    for factor, por_grupo in grupos.items():
        calificados = {k: v for k, v in por_grupo.items()
                       if len(v["valores"]) + v["censurados"] >= MODELO["min_n_grupo"]}
        if len(calificados) < 2:
            continue
        resultados = {k: _mediana_censurada(v["valores"], v["censurados"], horizonte_min)
                      for k, v in calificados.items()}
        peor_k = max(resultados, key=lambda k: _valor_orden(resultados[k]))
        mejor_k = min(resultados, key=lambda k: _valor_orden(resultados[k]))
        diff = _valor_orden(resultados[peor_k]) - _valor_orden(resultados[mejor_k])
        if diff < MODELO["ruido_min_diferencia_min"]:
            continue
        hallazgos.append({
            "factor": factor, "peor": peor_k, "mejor": mejor_k,
            "min_peor": round(resultados[peor_k]["mediana"]) if resultados[peor_k]["mediana"] is not None else None,
            "mas_de_peor": resultados[peor_k]["mas_de"],
            "min_mejor": round(resultados[mejor_k]["mediana"]) if resultados[mejor_k]["mediana"] is not None else None,
            "mas_de_mejor": resultados[mejor_k]["mas_de"],
            "n_peor": calificados[peor_k]["censurados"] + len(calificados[peor_k]["valores"]),
            "n_mejor": calificados[mejor_k]["censurados"] + len(calificados[mejor_k]["valores"]),
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
