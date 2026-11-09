"""Wave 9's whole-day verdict (BODY-EXPERIMENTOS-MODELO-1): did a WHOLE-DAY experiment
(`ventana.tipo == "dia"`) move a metric, judged against the person's OWN day-to-day noise, never
a population table (docs/DESIGN.md rules 4, 5, 12). Pure reads over a connection and a person id,
the style of body/turnos_costo.py and body/baselines.py. An INTRADAY window (`ventana.tipo ==
"intradia"`, BODY-EXPERIMENTOS-INTRADIA-MODELO-1) feeds the SAME rule a per-day WINDOW mean
instead of a whole day's own figure -- see `_veredicto_intradia`. 'aun_no' is no longer produced
by either path; the code stays valid (screens and the assistant still know it) for a later day
the rule genuinely cannot reach yet -- today nothing returns it.

Carlos's cold-shower week (2026-11-09..13, afternoon-shift days not after a night) is the first
real use of the whole-day path; its verdict was due 2026-11-14 (BODY-EXPERIMENTOS-1-INV slice 4).
Carlos's morning-grounding experiment (vida/experimento-manana.md, his Personal desk's own file)
is the first intraday use, aimed at Sat 2026-11-14 (BODY-EXPERIMENTOS-1-INV slice 8).
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, time, timedelta

from . import baselines, db, turnos_costo

BASE_DIAS = 120                 # the "before fecha_inicio" baseline window -- same idea as
                                 # turnos_costo's REFERENCIA_DIAS, the person's own recent normal,
                                 # never a population figure.
MIN_PRUEBA = 3                  # fewer test days than this and there is no honest mean to compare.
MIN_BASE = baselines.MIN_DAYS   # reused, not copied -- one "not enough data yet" floor.

# ORCH-CARLOS-DELEGATES-TIER-1-1 decision (a): the noise test is on the standard error of the
# DIFFERENCE of two means, not a single day's z-score, because a multi-day test mean is far less
# noisy than one day -- a day-level 0.5 z would call ordinary chance a change. UMBRAL is in units
# of that difference's own standard error (ee = sd_base * sqrt(1/n_prueba + 1/n_base)): 2.0 is the
# familiar "about two standard errors" bar -- a practical threshold, not a p-value.
UMBRAL = 2.0

# Per-metric day offset: which day a metric's VALUE belongs to, relative to the day the practice
# happened (ORCH-CARLOS-DELEGATES-TIER-1-1 decision (b)). body/garmin_adapter.py's parse_day(day,
# raw) reads a calendar day's sleep/hrv/wake fields from Garmin's own sleep/hrv endpoints for that
# day -- and Garmin keys those endpoints by the morning the sleep session ENDED, so `day`'s
# sleep/hrv/wake value is really about the NIGHT FROM day-1 INTO day. A practice on day P meant to
# affect that night's sleep therefore shows up at day P+1 -- offset +1. `resting_hr` comes from the
# same per-day user_summary call, and its lowest reading is typically set during the small hours
# of that same preceding night, so it gets the same +1. `snoring_min` is self-measured but is a
# nighttime reading, same convention.
# Metrics NOT in this dict -- including any key `metric_def` gains later -- default to 0: stress_avg,
# steps, active_kcal, body_battery_high/low, intensity_minutes, respiration_awake, high_stress_min,
# readiness, hrv_weekly_avg (a 7-day rolling average, not one night), vo2max(_cycling), fitness_age,
# flex_alcance_cm -- all computed over the practice day's own daytime, or over many days, never one
# specific following night.
OFFSET_DIA = {
    "sleep_minutes": 1,
    "sleep_score": 1,
    "sleep_deep_min": 1,
    "sleep_light_min": 1,
    "sleep_rem_min": 1,
    "sleep_awake_min": 1,
    "sleep_need_min": 1,
    "body_battery_sleep_charge": 1,
    "sleep_spo2_avg": 1,
    "sleep_respiration_avg": 1,
    "sleep_respiration_low": 1,
    "sleep_respiration_high": 1,
    "hrv_last_night": 1,
    "body_battery_wake": 1,
    "resting_hr": 1,
    "snoring_min": 1,
}

# BODY-EXPERIMENTOS-INTRADIA-MODELO-1: an intraday window with fewer than half the samples its
# OWN observed spacing implies is treated as a day with no data, not as a thin-but-honest mean --
# a watch not worn, or a sync gap, drops samples without lowering Garmin's nominal interval, so a
# half-empty window is a coverage problem, not a true physiological average.
COBERTURA_MIN = 0.5

# db.METRICAS_INTRADIA's own keys -> the raw_payload kind db.intradia reads for each (db.
# INTRADIA_KINDS). Kept here, not in db.py, since this is the one caller that walks from a
# metric key to a stored series.
_KIND_INTRADIA = {"heart_rate": "intradia_fc", "stress": "intradia_estres"}


def dias_que_cuentan(conn, person_id: int, filtro_dias: dict, desde: str, hasta: str) -> list[str]:
    """ISO days in [desde, hasta] matching `filtro_dias`: `turno_en` (the day's shift kind must be
    in this list, when given), `turno_fuera` (the day's shift kind must NOT be in this list, when
    given) and `excluir_tras_noche` (drops days inside the post-night recovery window --
    turnos_costo's own definition, `turnos_costo.dias_post_noche`, reused here rather than
    redefined). A day with no shift row has no kind: it matches `turno_en` only when `turno_en` is
    None, and it never matches `turno_fuera` (it is not any of the listed kinds) -- turnos_costo
    never treats a missing shift row as kind 'off' (`bloques_noche` looks only at rows with
    kind='night'), so neither does this."""
    turno_en = filtro_dias.get("turno_en")
    turno_fuera = filtro_dias.get("turno_fuera")
    rows = conn.execute(
        "SELECT day, kind FROM shift WHERE person_id = ? AND day BETWEEN ? AND ? "
        "ORDER BY day, start_local",
        (person_id, desde, hasta),
    ).fetchall()
    kind_por_dia: dict[str, str] = {}
    for r in rows:
        kind_por_dia.setdefault(r["day"], r["kind"])

    excluidos_noche: set[str] = set()
    if filtro_dias.get("excluir_tras_noche"):
        # Buffered backward by ventana_post, same reason turnos_costo._dias_excluidos buffers its
        # own window: a block whose last night falls just before `desde` can still have its
        # recovery tail bleed into [desde, hasta].
        buffer_desde = (date.fromisoformat(desde) - timedelta(turnos_costo.MODELO["ventana_post"])).isoformat()
        excluidos_noche = turnos_costo.dias_post_noche(conn, person_id, buffer_desde, hasta)

    out: list[str] = []
    d, fin = date.fromisoformat(desde), date.fromisoformat(hasta)
    while d <= fin:
        day = d.isoformat()
        kind = kind_por_dia.get(day)
        if turno_en is not None and kind not in turno_en:
            d += timedelta(1)
            continue
        if turno_fuera is not None and kind in turno_fuera:
            d += timedelta(1)
            continue
        if day in excluidos_noche:
            d += timedelta(1)
            continue
        out.append(day)
        d += timedelta(1)
    return out


def _ocurrio_por_dia(conn, person_id: int, experimento_id: int, habito_id: int | None,
                      dias: list[str]) -> dict[str, bool]:
    if not dias:
        return {}
    placeholders = ",".join("?" * len(dias))
    if habito_id is not None:
        rows = conn.execute(
            f"SELECT day, done FROM habit_check WHERE habit_id = ? AND person_id = ? "
            f"AND day IN ({placeholders})",
            (habito_id, person_id, *dias),
        ).fetchall()
        return {r["day"]: bool(r["done"]) for r in rows}
    rows = conn.execute(
        f"SELECT day, ocurrio FROM experimento_registro WHERE experimento_id = ? AND person_id = ? "
        f"AND day IN ({placeholders})",
        (experimento_id, person_id, *dias),
    ).fetchall()
    return {r["day"]: bool(r["ocurrio"]) for r in rows}


def _dias_de_otros_experimentos(conn, person_id: int, experimento_id: int, hoy: date) -> set[str]:
    """Every day inside another of this person's experiments' own [fecha_inicio, fecha_fin or hoy]
    span -- kept out of THIS experiment's baseline so the same days never serve as both another
    experiment's test days and this one's "ordinary life" reference."""
    rows = conn.execute(
        "SELECT fecha_inicio, fecha_fin FROM experimento WHERE person_id = ? AND id != ?",
        (person_id, experimento_id),
    ).fetchall()
    hoy_iso = hoy.isoformat()
    ocupados: set[str] = set()
    for r in rows:
        fin_iso = min(r["fecha_fin"] or hoy_iso, hoy_iso)
        d, fin = date.fromisoformat(r["fecha_inicio"]), date.fromisoformat(fin_iso)
        while d <= fin:
            ocupados.add(d.isoformat())
            d += timedelta(1)
    return ocupados


def _valores_con_offset(conn, person_id: int, metric: str, dias: list[str], offset: int) -> list[float]:
    """One value per day in `dias`, read from day+offset -- missing days (no shift-less gap, no
    watch reading, or shifted past today) simply contribute no value, the same honest-gap
    treatment turnos_costo and baselines use."""
    if not dias:
        return []
    desplazados = [(date.fromisoformat(d) + timedelta(offset)).isoformat() for d in dias]
    serie = db.series(conn, metric, min(desplazados), max(desplazados), person_id)
    return [serie[d] for d in desplazados if d in serie]


def _veredicto_metrica(prueba_vals: list[float], base_vals: list[float]) -> dict:
    n_prueba, n_base = len(prueba_vals), len(base_vals)
    media_prueba = statistics.fmean(prueba_vals) if prueba_vals else None
    media_base = statistics.fmean(base_vals) if base_vals else None
    diferencia = (media_prueba - media_base) if media_prueba is not None and media_base is not None else None

    resultado = "pocos_dias"
    if n_prueba >= MIN_PRUEBA and n_base >= MIN_BASE:
        sd_base = statistics.pstdev(base_vals)
        if sd_base:
            ee = sd_base * math.sqrt(1 / n_prueba + 1 / n_base)
            if diferencia <= -UMBRAL * ee:
                resultado = "mas_bajo"
            elif diferencia >= UMBRAL * ee:
                resultado = "mas_alto"
            else:
                resultado = "sin_cambio"

    return {
        "resultado": resultado,
        "n_prueba": n_prueba,
        "n_base": n_base,
        "media_prueba": media_prueba,
        "media_base": media_base,
        "diferencia": diferencia,
    }


def _sin_offset(ts: str) -> datetime:
    """A local timestamp read back as a naive datetime, dropping any UTC offset a caller
    attached. `moment.ts_local` is free text that existing fixtures write WITH an offset (e.g.
    "...T06:30:00-05:00"); this module's own local timestamps -- `sleep_session.end_local`,
    db.intradia's own (local_iso, value) pairs -- never carry one. The value is already local
    either way, so comparing it as naive is the honest reading, never a timezone conversion."""
    dt = datetime.fromisoformat(ts)
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _ancla_despertar(conn, person_id: int, dia: str) -> str | None:
    """Day `dia`'s own wake anchor: `sleep_session.end_local` for the row whose `day` IS `dia` --
    body/db.py's save_day (like Garmin itself) files sleep under the morning the session ENDED, so
    this is already "that day's wake time", no OFFSET_DIA shift needed (unlike a whole-day
    metric's own night-after reading). None for a day with no sleep row, or no end_local yet."""
    row = conn.execute(
        "SELECT end_local FROM sleep_session WHERE person_id = ? AND day = ?", (person_id, dia)
    ).fetchone()
    return row["end_local"] if row is not None else None


def _anclas_momento(conn, person_id: int, experimento_id: int, dias: list[str]) -> dict[str, str]:
    """ts_local of each day's own logged moment, keyed by day (`experimento_registro.momento_id`
    -> `moment.ts_local`). A day whose registro has no momento_id, or whose moment row is gone, is
    simply absent -- no anchor."""
    if not dias:
        return {}
    placeholders = ",".join("?" * len(dias))
    rows = conn.execute(
        f"SELECT er.day AS day, m.ts_local AS ts_local FROM experimento_registro er "
        f"JOIN moment m ON m.id = er.momento_id "
        f"WHERE er.experimento_id = ? AND er.person_id = ? AND er.day IN ({placeholders})",
        (experimento_id, person_id, *dias),
    ).fetchall()
    return {r["day"]: r["ts_local"] for r in rows}


def _hora_mediana_local(ts_locales: list[str]) -> time:
    """The median local clock time of day across several local timestamps -- compared as
    seconds-since-midnight so the median is well-defined even though the timestamps' own calendar
    days differ."""
    segundos = sorted(_sin_offset(ts).hour * 3600 + _sin_offset(ts).minute * 60 + _sin_offset(ts).second
                       for ts in ts_locales)
    mediana = statistics.median(segundos)
    return (datetime.min + timedelta(seconds=mediana)).time()


def _anclas_base_momento(dias_base: list[str], anclas_prueba: dict[str, str]) -> dict[str, str]:
    """BODY-EXPERIMENTOS-INTRADIA-MODELO-1 judgement: a baseline day never has a moment of its own
    (nothing was logged on an ordinary day), so every baseline day's anchor is the MEDIAN local
    clock time of the test days' own moments, applied to that baseline day's own date -- the
    closest honest stand-in for "when this would have happened on an ordinary day". {} (no anchor
    for any baseline day) when no test day has a moment yet to take the median from."""
    validos = [ts for ts in anclas_prueba.values() if ts]
    if not validos:
        return {}
    hora = _hora_mediana_local(validos)
    return {d: datetime.combine(date.fromisoformat(d), hora).isoformat(timespec="minutes") for d in dias_base}


def _espaciado_mediana_min(muestras: list[tuple[str, float]]) -> float | None:
    """The median gap, in minutes, between this set of samples' OWN consecutive timestamps -- the
    day's own observed spacing (a watch not worn drops samples without changing Garmin's nominal
    interval), never a fixed constant. None when fewer than two samples exist to compare, or every
    gap is zero (duplicate timestamps)."""
    if len(muestras) < 2:
        return None
    tiempos = sorted(_sin_offset(ts) for ts, _ in muestras)
    diffs = [(b - a).total_seconds() / 60 for a, b in zip(tiempos, tiempos[1:]) if b > a]
    if not diffs:
        return None
    return statistics.median(diffs)


def _muestras_dias(conn, person_id: int, kind: str, dias: list[str], zona: str) -> list[tuple[str, float]]:
    todas: list[tuple[str, float]] = []
    for dia in dias:
        todas.extend(db.intradia(conn, person_id, dia, kind, zona))
    return todas


def _valor_ventana(conn, person_id: int, metric: str, ancla_local: str | None, zona: str,
                    antes_min: int, despues_min: int) -> float | None:
    """This day's window mean for `metric` -- None when the day has no anchor, its intraday series
    is not stored for either calendar day the window touches, or the window's own coverage falls
    under COBERTURA_MIN (half the samples its own observed spacing implies)."""
    if ancla_local is None:
        return None
    ancla = _sin_offset(ancla_local)
    inicio = ancla - timedelta(minutes=antes_min)
    fin = ancla + timedelta(minutes=despues_min)
    dias_ventana = sorted({inicio.date().isoformat(), fin.date().isoformat()})
    muestras = _muestras_dias(conn, person_id, _KIND_INTRADIA[metric], dias_ventana, zona)
    if not muestras:
        return None
    espaciado = _espaciado_mediana_min(muestras)
    if espaciado is None:
        return None
    en_ventana = [v for ts, v in muestras if inicio <= _sin_offset(ts) <= fin]
    if not en_ventana:
        return None
    esperadas = (fin - inicio).total_seconds() / 60 / espaciado
    if len(en_ventana) < COBERTURA_MIN * esperadas:
        return None
    return statistics.fmean(en_ventana)


def _veredicto_intradia(conn, person_id: int, experimento_id: int, row, hoy: date) -> dict:
    """BODY-EXPERIMENTOS-INTRADIA-MODELO-1: `_veredicto_metrica`'s own whole-day rule (MIN_PRUEBA,
    MIN_BASE, UMBRAL), fed a per-day WINDOW mean instead of a whole day's own figure. Each
    counting day's window is [anchor - antes_min, anchor + despues_min], anchored at that day's
    own wake time ('despertar') or its own logged moment ('momento') -- see _ancla_despertar /
    _anclas_momento. A baseline day has no moment of its own, so a 'momento' baseline is anchored
    at the median local clock time of the test days' own moments (_anclas_base_momento, a
    judgement call recorded there and in the ledger)."""
    ventana = json.loads(row["ventana"])
    metricas = json.loads(row["metricas"])
    filtro_dias = json.loads(row["filtro_dias"])
    estado = row["estado"]
    ancla_tipo = ventana["ancla"]
    antes_min = ventana["antes_min"] or 0
    despues_min = ventana["despues_min"]
    zona = db.person(conn, person_id)["timezone"]

    hoy_iso = hoy.isoformat()
    fecha_inicio = row["fecha_inicio"]
    fin_prueba = min(row["fecha_fin"] or hoy_iso, hoy_iso)

    candidatos_prueba = dias_que_cuentan(conn, person_id, filtro_dias, fecha_inicio, fin_prueba)
    ocurrio = _ocurrio_por_dia(conn, person_id, experimento_id, row["habito_id"], candidatos_prueba)
    dias_prueba = [d for d in candidatos_prueba if ocurrio.get(d)]
    dias_excluidos_no_ocurrio = [d for d in candidatos_prueba if not ocurrio.get(d)]

    base_hasta = (date.fromisoformat(fecha_inicio) - timedelta(1)).isoformat()
    base_desde = (date.fromisoformat(fecha_inicio) - timedelta(BASE_DIAS)).isoformat()
    candidatos_base = dias_que_cuentan(conn, person_id, filtro_dias, base_desde, base_hasta)
    ocupados_por_otros = _dias_de_otros_experimentos(conn, person_id, experimento_id, hoy)
    dias_base = [d for d in candidatos_base if d not in ocupados_por_otros]

    if ancla_tipo == "despertar":
        anclas_prueba = {d: _ancla_despertar(conn, person_id, d) for d in dias_prueba}
        anclas_base = {d: _ancla_despertar(conn, person_id, d) for d in dias_base}
    else:
        anclas_prueba = _anclas_momento(conn, person_id, experimento_id, dias_prueba)
        anclas_base = _anclas_base_momento(dias_base, anclas_prueba)

    metricas_out = {}
    sin_dato_prueba: set[str] = set()
    sin_dato_base: set[str] = set()
    for metric in metricas:
        prueba_vals = []
        for d in dias_prueba:
            v = _valor_ventana(conn, person_id, metric, anclas_prueba.get(d), zona, antes_min, despues_min)
            if v is None:
                sin_dato_prueba.add(d)
            else:
                prueba_vals.append(v)
        base_vals = []
        for d in dias_base:
            v = _valor_ventana(conn, person_id, metric, anclas_base.get(d), zona, antes_min, despues_min)
            if v is None:
                sin_dato_base.add(d)
            else:
                base_vals.append(v)
        metricas_out[metric] = _veredicto_metrica(prueba_vals, base_vals)

    return {
        "metricas": metricas_out,
        "dias_prueba": dias_prueba,
        "dias_excluidos_no_ocurrio": dias_excluidos_no_ocurrio,
        "dias_sin_dato": {"prueba": sorted(sin_dato_prueba), "base": sorted(sin_dato_base)},
        "estado": estado,
    }


def veredicto(conn, person_id: int, experimento_id: int, hoy: date | None = None) -> dict:
    """Per metric: resultado ('mas_bajo' | 'mas_alto' | 'sin_cambio' | 'pocos_dias'), n_prueba,
    n_base, media_prueba, media_base, diferencia -- plus dias_prueba (ISO days actually used),
    dias_excluidos_no_ocurrio and the experiment's own estado. `ventana.tipo == 'intradia'`
    (BODY-EXPERIMENTOS-INTRADIA-MODELO-1) feeds the same rule a per-day WINDOW mean instead of a
    whole day's own figure -- see `_veredicto_intradia`, whose result also carries
    `dias_sin_dato` (test/base days dropped for a missing anchor or thin intraday coverage)."""
    hoy = hoy or date.today()
    row = conn.execute(
        "SELECT * FROM experimento WHERE id = ? AND person_id = ?", (experimento_id, person_id)
    ).fetchone()
    if row is None:
        raise ValueError("experimento_id: not this person's own experiment")

    ventana = json.loads(row["ventana"])
    if ventana["tipo"] == "intradia":
        return _veredicto_intradia(conn, person_id, experimento_id, row, hoy)

    metricas = json.loads(row["metricas"])
    filtro_dias = json.loads(row["filtro_dias"])
    estado = row["estado"]

    hoy_iso = hoy.isoformat()
    fecha_inicio = row["fecha_inicio"]
    fin_prueba = min(row["fecha_fin"] or hoy_iso, hoy_iso)

    candidatos_prueba = dias_que_cuentan(conn, person_id, filtro_dias, fecha_inicio, fin_prueba)
    ocurrio = _ocurrio_por_dia(conn, person_id, experimento_id, row["habito_id"], candidatos_prueba)
    dias_prueba = [d for d in candidatos_prueba if ocurrio.get(d)]
    dias_excluidos_no_ocurrio = [d for d in candidatos_prueba if not ocurrio.get(d)]

    base_hasta = (date.fromisoformat(fecha_inicio) - timedelta(1)).isoformat()
    base_desde = (date.fromisoformat(fecha_inicio) - timedelta(BASE_DIAS)).isoformat()
    candidatos_base = dias_que_cuentan(conn, person_id, filtro_dias, base_desde, base_hasta)
    ocupados_por_otros = _dias_de_otros_experimentos(conn, person_id, experimento_id, hoy)
    dias_base = [d for d in candidatos_base if d not in ocupados_por_otros]

    metricas_out = {}
    for metric in metricas:
        offset = OFFSET_DIA.get(metric, 0)
        prueba_vals = _valores_con_offset(conn, person_id, metric, dias_prueba, offset)
        base_vals = _valores_con_offset(conn, person_id, metric, dias_base, offset)
        metricas_out[metric] = _veredicto_metrica(prueba_vals, base_vals)

    return {
        "metricas": metricas_out,
        "dias_prueba": dias_prueba,
        "dias_excluidos_no_ocurrio": dias_excluidos_no_ocurrio,
        "estado": estado,
    }
