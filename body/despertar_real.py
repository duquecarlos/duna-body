"""Carlos's ruling, relayed by the desk 2026-11-18 (BODY-DESPERTAR-REAL-3's own surface): "I think
that body should be like the app that reflects reality in most accurate about my body so because
it's the mix between the data that is stored by the watch and and all the other info that can
gather, use as my timeline, GPS, the things that I report to you, eating, training with detail, I
mean everything." The orchestrator's reading (dev-protocol DECISIONS.md ## BODY-DESPERTAR-REAL-
RULING): the evidence decides -- dozing after Garmin's own sleep end is proposed as SLEEP, quiet
awake time in bed only bounds the GET-UP time, "cannot tell" is said plainly, and Garmin's own
record is never edited.

This module is the MODEL only (BODY-DESPERTAR-REAL-3): no screen, no tool, no schema -- `estimar`
is a pure read over a connection and a person id, no web imports, the same shape as
body/recuperacion.py, body/turno_fc.py and body/sueno_fc.py. Every threshold lives in MODELO,
versioned (`MODELO["version"]`), and every one is RELATIVE to the person's own heart rate in the
last part of that night's sleep (ruling 2 below) -- never an absolute bpm, and never tuned to one
person. Read only: writes nothing, calls no network. The day cache and the spacing/offset helpers
are shared with body/sueno_fc.py and body/turno_fc.py (body/intradia_horario.py,
body/experimentos.py), never duplicated here.

The orchestrator's rulings this module is built to (Tier 2, quality):
  (1) `estimar(conn, person_id, day) -> dict`: Garmin's own sleep start/end, a list of `segmentos`
      from Garmin's own end up to MODELO['horizonte_horas'] (each one `tipo` 'dormitando' |
      'despierto_quieto' | 'levantado' | 'sin_dato', with its own `inicio`/`fin`),
      `primera_levantada` (the first 'levantado' moment, if any), `propuestas` (BODY-DESPERTAR-
      REAL-6A-R2 ruling 2: a list, at most one of each tipo, ordered sueno then levantado -- a
      'sueno' entry when the first segment after Garmin's own end is a dormitando run, a
      'levantado' entry when a sustained rise or a bounding event exists, dropped when its own
      hora would equal the sueno entry's hora, [] when neither applies), a `confianza` ('alta' |
      'media' | 'baja') for the whole morning, and the `evidencia` used, by source.
  (2) thresholds relative to the person's OWN heart rate near the end of that night's sleep (the
      sleep-window array, `db.sueno_minuto_a_minuto`) -- falling back to their resting heart rate
      (`daily_metric`, 'resting_hr') ONLY where that sleep-window baseline itself cannot be read,
      never a bare constant. Dozing = heart rate within a small margin of that baseline with
      stress inside its own rest range; up = a SUSTAINED rise (MODELO['minutos_sostenidos']) above
      a larger margin with stress having left that rest range; in between = despierto_quieto;
      gaps (watch off, no points) are sin_dato and never classified.
  (3) every source Body already has, besides the watch: a timed event logged for that day AFTER
      Garmin's own end is evidence he was up by then -- a logged meal, a check-in, a training
      session or activity start (`comida.ts_local` body/db.py:810, `checkin.ts_local`
      body/db.py:1016, `activity.start_local` body/db.py:406) -- used as an upper bound on the
      get-up time, in the evidence by its own source name, never its content (never the meal's
      words, the check-in's mood/energy/stress_felt, the activity's own type). `desplazamiento_
      import.py` stores only a DAILY count summary (vehicle_minutes/vehicle_trips/walking_minutes/
      cycling_minutes, body/desplazamiento_import.py:27-28), no per-event departure timestamp --
      a named gap, not a source used here. Timeline/GPS beyond what Body already imports waits on
      Lugares' own read API (BODY-HISTORIA-VIDA-LUGARES-1) -- `_eventos_lugares` below is that
      named hook, returning [] until that API exists.
  (4) honest output: no baseline at all, or a gap covering more than MODELO['cobertura_sin_dato_
      max_media'] of the horizon with no logged event to bound it -> `confianza` 'baja' and
      `propuesta` 'ninguna', never a guess. No figure in the result belongs to anyone but the
      person asked for (tests/test_persona_fuga.py).
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from . import db, intradia_horario
from .experimentos import _sin_offset

VERSION = "despertar-real-v2"
MODELO = {
    "version": VERSION,
    # How far past Garmin's own sleep end this model ever looks.
    "horizonte_horas": 4,
    # Minutes right before Garmin's own sleep end used to read the person's OWN baseline heart
    # rate and stress for that night (ruling 2) -- long enough to smooth a stray reading, short
    # enough to stay "the last part of sleep", never the whole night.
    "ventana_base_min": 30,
    # Fewer baseline samples than this and the sleep-window baseline cannot be trusted -- the
    # model falls back to resting_hr (ruling 2's "where present"), then to an honest "no baseline"
    # rather than a figure built from one or two points.
    "cobertura_min_base": 3,
    # A heart rate within this many bpm of the person's OWN baseline, with stress inside its own
    # rest range, still reads as dozing -- a device-noise-sized margin, not a diagnosis.
    "margen_dormitando_bpm": 5,
    # A heart rate at or above this many bpm over the person's OWN baseline, with stress having
    # left its own rest range, is a CANDIDATE rise -- confirmed only once it holds for
    # MODELO['minutos_sostenidos'] (ruling 2's "sustained rise").
    "margen_levantado_bpm": 15,
    # A stress reading within this many points of the person's OWN baseline stress still reads as
    # that night's own rest range; above it, stress has "left its rest range" (ruling 2).
    "margen_estres_descanso": 10,
    # How close, in minutes, a stress reading must be to a heart-rate reading to be paired with it
    # for one point's own classification -- intradia_fc samples roughly every 2 minutes,
    # intradia_estres roughly every 3 (BODY-DESPERTAR-REAL-1-INV), so this must cover at least one
    # full stress cycle.
    "tolerancia_pareo_estres_min": 5,
    # A candidate rise must hold for at least this many minutes to read as actually 'levantado' --
    # shorter than this, it is a blip (a turn in bed, a stretch) and stays 'despierto_quieto'.
    "minutos_sostenidos": 10,
    # A gap between consecutive heart-rate samples wider than this MANY TIMES the night's own
    # observed spacing is the watch off, not a thin-but-honest reading -- the same multiple-of-
    # observed-spacing convention body/sueno_fc.py's and body/turno_fc.py's own MODELO['cobertura_
    # min'] use, expressed here as a gap factor since one wide gap, not an overall density, is
    # what marks 'sin_dato'.
    "factor_hueco_espaciado": 3,
    # When the night's own heart-rate spacing cannot be measured (fewer than two samples), this is
    # what Garmin's own intraday interval is documented as (BODY-DESPERTAR-REAL-1-INV: 2-minute
    # heart rate) -- used only to size a gap when there is nothing else to measure it from.
    "espaciado_asumido_min": 2,
    # Share of the horizon spent 'sin_dato' above which the reading no longer counts as 'alta'.
    "cobertura_sin_dato_max_alta": 0.1,
    # Share of the horizon spent 'sin_dato' above which the evidence is thin (ruling 4): 'baja'
    # (and 'propuesta' falls back to 'ninguna' unless a logged event still bounds it).
    "cobertura_sin_dato_max_media": 0.5,
}

SEGMENT_KINDS = ("dormitando", "despierto_quieto", "levantado", "sin_dato")
# BODY-DESPERTAR-REAL-6A-R2 ruling 2: the two kinds a `propuestas` entry can carry -- 'ninguna'
# (despertar-real-v1's own third value, "nothing to propose") is now simply [] (ruling 2).
PROPUESTA_TIPOS = ("sueno", "levantado")
CONFIANZAS = ("alta", "media", "baja")


def _eventos_lugares(conn, person_id: int, desde: datetime, hasta: datetime) -> list[dict]:
    """Named hook (ruling 3): once Lugares' own read API exists (BODY-HISTORIA-VIDA-LUGARES-1),
    timeline/GPS evidence plugs in here, in the same {"fuente", "hora"} shape every other event
    source below uses. [] until then -- Body imports no per-event Timeline/GPS today."""
    return []


def _baseline(conn, person_id: int, day: str, zona: str, sleep_end_dt: datetime, cache: dict
              ) -> tuple[float | None, str | None]:
    """The person's OWN heart-rate baseline (ruling 2): the median of their sleep-window heart
    rate over MODELO['ventana_base_min'] minutes right before Garmin's own sleep end -- read first
    from `db.sueno_minuto_a_minuto`'s own per-minute array (the sleep session's own sensor
    stream), falling back to `db.intradia`'s 'intradia_fc' over the same window when that array
    holds no heart rate. Falls back again to `resting_hr` (ruling 2's "where present") only when
    neither gives MODELO['cobertura_min_base'] points. (None, None) when nothing does -- an
    honest "no baseline", never a figure built from too few points."""
    ventana_inicio = sleep_end_dt - timedelta(minutes=MODELO["ventana_base_min"])
    sueno = db.sueno_minuto_a_minuto(conn, person_id, day, zona)
    hr_sueno = sueno.get("heart_rate", ())
    muestras = [v for ts, v in hr_sueno if ventana_inicio <= _sin_offset(ts) <= sleep_end_dt]
    if len(muestras) >= MODELO["cobertura_min_base"]:
        return round(statistics.median(muestras), 1), "sueno_minuto_a_minuto"

    dias_base = sorted({ventana_inicio.date().isoformat(), sleep_end_dt.date().isoformat()})
    muestras_fc: list[float] = []
    for dia in dias_base:
        entry = intradia_horario.leer_dia(conn, person_id, dia, zona, cache, kind="intradia_fc")
        muestras_fc.extend(v for ts, v in entry["muestras"] if ventana_inicio <= _sin_offset(ts) <= sleep_end_dt)
    if len(muestras_fc) >= MODELO["cobertura_min_base"]:
        return round(statistics.median(muestras_fc), 1), "intradia_fc"

    row = conn.execute(
        "SELECT value FROM daily_metric_efectiva WHERE person_id = ? AND day = ? AND metric_key = 'resting_hr'",
        (person_id, day),
    ).fetchone()
    if row is not None:
        return row["value"], "resting_hr"
    return None, None


def _baseline_estres(conn, person_id: int, zona: str, sleep_end_dt: datetime, cache: dict) -> float | None:
    """The person's OWN stress baseline over the same window `_baseline` uses, read from
    `db.intradia`'s 'intradia_estres' (the sleep array carries no stress series --
    BODY-DESPERTAR-REAL-1-INV). None below MODELO['cobertura_min_base'] points -- stress then
    simply never gates a classification (the None-tolerant branches in `_clasificar_punto`),
    never a guessed figure."""
    ventana_inicio = sleep_end_dt - timedelta(minutes=MODELO["ventana_base_min"])
    dias = sorted({ventana_inicio.date().isoformat(), sleep_end_dt.date().isoformat()})
    muestras: list[float] = []
    for dia in dias:
        entry = intradia_horario.leer_dia(conn, person_id, dia, zona, cache, kind="intradia_estres")
        muestras.extend(v for ts, v in entry["muestras"] if ventana_inicio <= _sin_offset(ts) <= sleep_end_dt)
    if len(muestras) >= MODELO["cobertura_min_base"]:
        return round(statistics.median(muestras), 1)
    return None


def _valor_cercano(puntos: list[tuple[datetime, float]], objetivo: datetime, tol_min: float) -> float | None:
    mejor, mejor_delta = None, None
    for dt, v in puntos:
        delta = abs((dt - objetivo).total_seconds()) / 60
        if delta <= tol_min and (mejor_delta is None or delta < mejor_delta):
            mejor, mejor_delta = v, delta
    return mejor


def _clasificar_punto(hr_val: float, base_fc: float, estres_val: float | None,
                       base_estres: float | None) -> str:
    """One heart-rate point's own provisional label (ruling 2) -- 'candidato_levantado' still
    needs to hold for MODELO['minutos_sostenidos'] before `_segmentos` confirms it as
    'levantado'. Stress is None-tolerant on both branches: with no paired stress reading, the
    heart-rate margin decides alone."""
    delta_fc = hr_val - base_fc
    delta_estres = (estres_val - base_estres) if (estres_val is not None and base_estres is not None) else None
    en_descanso = delta_estres is None or delta_estres <= MODELO["margen_estres_descanso"]
    fuera_descanso = delta_estres is None or delta_estres > MODELO["margen_estres_descanso"]
    if delta_fc <= MODELO["margen_dormitando_bpm"] and en_descanso:
        return "dormitando"
    if delta_fc >= MODELO["margen_levantado_bpm"] and fuera_descanso:
        return "candidato_levantado"
    return "despierto_quieto"


def _fundir(crudos: list[tuple[str, datetime, datetime]]) -> list[tuple[str, datetime, datetime]]:
    """Merges adjacent, contiguous, same-kind intervals into one run."""
    fundidos: list[list] = []
    for tipo, inicio, fin in crudos:
        if fundidos and fundidos[-1][0] == tipo and fundidos[-1][2] == inicio:
            fundidos[-1][2] = fin
        else:
            fundidos.append([tipo, inicio, fin])
    return [tuple(f) for f in fundidos]


def _segmentos(fc_puntos: list[tuple[datetime, str]], sleep_end_dt: datetime, horizonte_fin_dt: datetime,
               espaciado_min: float) -> list[dict]:
    """Tiles [sleep_end_dt, horizonte_fin_dt] from the already-classified heart-rate points:
    forward-looking (a point's own label governs the interval UP TO the next point -- 'this is
    what the watch said held, until it said otherwise'), 'sin_dato' wherever a gap exceeds
    MODELO['factor_hueco_espaciado'] times the night's own observed spacing, then merges adjacent
    intervals of the same kind into runs, then (ruling 2's "sustained rise") downgrades any
    'candidato_levantado' run shorter than MODELO['minutos_sostenidos'] to 'despierto_quieto' and
    re-merges. Never returns an empty list: a horizon with no heart-rate point at all is one
    whole 'sin_dato' run -- an honest "watch off", never silence."""
    hueco_min = MODELO["factor_hueco_espaciado"] * espaciado_min
    if not fc_puntos:
        return [{"tipo": "sin_dato", "inicio": sleep_end_dt, "fin": horizonte_fin_dt}]

    crudos: list[tuple[str, datetime, datetime]] = []
    prev_dt = sleep_end_dt
    for i, (dt, label) in enumerate(fc_puntos):
        if i == 0:
            gap = (dt - prev_dt).total_seconds() / 60
            crudos.append(("sin_dato" if gap > hueco_min else label, prev_dt, dt))
        else:
            dt_prev, label_prev = fc_puntos[i - 1]
            gap = (dt - dt_prev).total_seconds() / 60
            crudos.append(("sin_dato" if gap > hueco_min else label_prev, dt_prev, dt))
        prev_dt = dt
    ultimo_dt, ultimo_label = fc_puntos[-1]
    gap_final = (horizonte_fin_dt - ultimo_dt).total_seconds() / 60
    crudos.append(("sin_dato" if gap_final > hueco_min else ultimo_label, ultimo_dt, horizonte_fin_dt))

    reetiquetados = []
    for tipo, inicio, fin in _fundir(crudos):
        if tipo == "candidato_levantado":
            minutos = (fin - inicio).total_seconds() / 60
            tipo = "levantado" if minutos >= MODELO["minutos_sostenidos"] else "despierto_quieto"
        reetiquetados.append((tipo, inicio, fin))
    return [{"tipo": tipo, "inicio": inicio, "fin": fin} for tipo, inicio, fin in _fundir(reetiquetados)]


def _eventos(conn, person_id: int, dias: list[str], sleep_end_dt: datetime, horizonte_fin_dt: datetime
             ) -> list[dict]:
    """Every timed event Body already has for `dias` that falls strictly after Garmin's own sleep
    end and no later than the horizon (ruling 3): a logged meal (`comida.ts_local`,
    body/db.py:810), a check-in (`checkin.ts_local`, body/db.py:1016), or a training session/
    activity start (`activity.start_local`, body/db.py:406) -- by source name and time ONLY, never
    the meal's words, the check-in's mood/energy/stress_felt, or the activity's own type. One
    query per source regardless of how many days or events (bounded, tests/test_rendimiento.py).
    Sorted earliest first; `_eventos_lugares` (always [] today) is appended the same way."""
    placeholders = ",".join("?" * len(dias))
    out: list[dict] = []
    for tabla, columna, fuente in (
        ("comida", "ts_local", "comida"),
        ("checkin", "ts_local", "checkin"),
        ("activity", "start_local", "actividad"),
    ):
        rows = conn.execute(
            f"SELECT {columna} AS hora FROM {tabla} WHERE person_id = ? AND day IN ({placeholders}) "
            f"AND {columna} IS NOT NULL",
            (person_id, *dias),
        ).fetchall()
        for r in rows:
            dt = _sin_offset(r["hora"])
            if sleep_end_dt < dt <= horizonte_fin_dt:
                out.append({"fuente": fuente, "hora": r["hora"]})
    out.extend(_eventos_lugares(conn, person_id, sleep_end_dt, horizonte_fin_dt))
    out.sort(key=lambda e: _sin_offset(e["hora"]))
    return out


def _resultado_vacio(sueno_inicio: str | None, sueno_fin: str | None, motivo: str) -> dict:
    return {
        "version": MODELO["version"],
        "sueno_inicio": sueno_inicio,
        "sueno_fin": sueno_fin,
        "horizonte_hasta": None,
        "segmentos": [],
        "primera_levantada": None,
        "propuestas": [],
        "confianza": "baja",
        "evidencia": {"motivo": motivo},
    }


def estimar(conn, person_id: int, day: str) -> dict:
    """Carlos's own ruling (module docstring above): dozing after Garmin's own sleep end is
    proposed as SLEEP, quiet awake time in bed only bounds the GET-UP time, and 'cannot tell' is
    said plainly. Read-only, no network; every query below is bounded regardless of how many
    samples a day holds (tests/test_rendimiento.py's own ceiling), and every one is scoped to
    `person_id` (tests/test_persona_fuga.py)."""
    cache: dict = {}
    row = conn.execute(
        "SELECT start_local, end_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, day),
    ).fetchone()
    if row is None or row["start_local"] is None or row["end_local"] is None:
        return _resultado_vacio(None, None, "sin_sesion")

    zona = db.person(conn, person_id)["timezone"]
    sueno_inicio, sueno_fin = row["start_local"], row["end_local"]
    sleep_end_dt = _sin_offset(sueno_fin)
    horizonte_fin_dt = sleep_end_dt + timedelta(hours=MODELO["horizonte_horas"])

    base_fc, fuente_base = _baseline(conn, person_id, day, zona, sleep_end_dt, cache)
    if base_fc is None:
        return _resultado_vacio(sueno_inicio, sueno_fin, "sin_linea_base")
    base_estres = _baseline_estres(conn, person_id, zona, sleep_end_dt, cache)

    dias_horizonte = sorted({sleep_end_dt.date().isoformat(), horizonte_fin_dt.date().isoformat()})
    fc_crudo: list[tuple[datetime, float]] = []
    estres_crudo: list[tuple[datetime, float]] = []
    for dia in dias_horizonte:
        entry_fc = intradia_horario.leer_dia(conn, person_id, dia, zona, cache, kind="intradia_fc")
        fc_crudo.extend((_sin_offset(ts), v) for ts, v in entry_fc["muestras"])
        entry_estres = intradia_horario.leer_dia(conn, person_id, dia, zona, cache, kind="intradia_estres")
        estres_crudo.extend((_sin_offset(ts), v) for ts, v in entry_estres["muestras"])

    fc_ventana = sorted((dt, v) for dt, v in fc_crudo if sleep_end_dt <= dt <= horizonte_fin_dt)
    estres_ventana = sorted((dt, v) for dt, v in estres_crudo if sleep_end_dt <= dt <= horizonte_fin_dt)

    if len(fc_ventana) >= 2:
        tiempos = [dt for dt, _ in fc_ventana]
        diffs = [(b - a).total_seconds() / 60 for a, b in zip(tiempos, tiempos[1:]) if b > a]
        espaciado = statistics.median(diffs) if diffs else MODELO["espaciado_asumido_min"]
    else:
        espaciado = MODELO["espaciado_asumido_min"]

    fc_etiquetados = [
        (dt, _clasificar_punto(v, base_fc, _valor_cercano(estres_ventana, dt, MODELO["tolerancia_pareo_estres_min"]),
                                base_estres))
        for dt, v in fc_ventana
    ]
    segmentos = _segmentos(fc_etiquetados, sleep_end_dt, horizonte_fin_dt, espaciado)

    eventos = _eventos(conn, person_id, dias_horizonte, sleep_end_dt, horizonte_fin_dt)

    primera_levantada_dt = next((s["inicio"] for s in segmentos if s["tipo"] == "levantado"), None)
    evento_dt = _sin_offset(eventos[0]["hora"]) if eventos else None

    minutos_totales = (horizonte_fin_dt - sleep_end_dt).total_seconds() / 60
    minutos_sin_dato = sum(
        (s["fin"] - s["inicio"]).total_seconds() / 60 for s in segmentos if s["tipo"] == "sin_dato")
    cobertura_sin_dato = minutos_sin_dato / minutos_totales if minutos_totales > 0 else 1.0

    if cobertura_sin_dato > MODELO["cobertura_sin_dato_max_media"]:
        confianza = "baja"
    elif cobertura_sin_dato > MODELO["cobertura_sin_dato_max_alta"]:
        confianza = "media"
    else:
        confianza = "alta"

    # BODY-DESPERTAR-REAL-6A-R2 ruling 2: the two kinds computed independently -- a morning can
    # truly have BOTH (dozed past Garmin's own end, then got up later still).
    primer_tipo = segmentos[0]["tipo"]
    sueno_hora_dt = segmentos[0]["fin"] if primer_tipo == "dormitando" else None

    if primer_tipo == "levantado":
        # Ruling: an immediate rise right at Garmin's own end means Garmin was already right --
        # nothing to propose for 'levantado'.
        levantado_hora_dt = None
    elif primera_levantada_dt is not None:
        levantado_hora_dt, fuente_hora = primera_levantada_dt, "intradia"
        if evento_dt is not None and evento_dt < levantado_hora_dt:
            levantado_hora_dt, fuente_hora = evento_dt, "evento"
        if fuente_hora == "evento" and confianza == "alta":
            confianza = "media"
    elif evento_dt is not None:
        levantado_hora_dt = evento_dt
        if confianza == "alta":
            confianza = "media"
    else:
        levantado_hora_dt = None

    propuestas: list[dict] = []
    if sueno_hora_dt is not None:
        propuestas.append({"tipo": "sueno", "hora": sueno_hora_dt.isoformat()})
    if levantado_hora_dt is not None and levantado_hora_dt != sueno_hora_dt:
        propuestas.append({"tipo": "levantado", "hora": levantado_hora_dt.isoformat()})

    return {
        "version": MODELO["version"],
        "sueno_inicio": sueno_inicio,
        "sueno_fin": sueno_fin,
        "horizonte_hasta": horizonte_fin_dt.isoformat(),
        "segmentos": [
            {"tipo": s["tipo"], "inicio": s["inicio"].isoformat(), "fin": s["fin"].isoformat()}
            for s in segmentos
        ],
        "primera_levantada": primera_levantada_dt.isoformat() if primera_levantada_dt else None,
        "propuestas": propuestas,
        "confianza": confianza,
        "evidencia": {
            "linea_base": {"fuente": fuente_base},
            "linea_base_estres": {"presente": base_estres is not None},
            "intradia_fc_muestras": len(fc_ventana),
            "intradia_estres_muestras": len(estres_ventana),
            "eventos": eventos,
        },
    }
