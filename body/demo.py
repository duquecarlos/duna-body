"""Seeds a scratch BODY_HOME with an invented person for tutorial recordings (tutoriales-v1 section 8,
step 2: "Demo tenant and catalog"). Growth drives this tenant with Playwright; nothing here is a real
person's data -- every figure is generated from a fixed random seed, in a plausible range, never read
from a real database.

    python -m body.demo --home <folder> [--dias 120] [--idioma es|en]

REFUSES (non-zero exit, a plain message, nothing written) when `--home` is missing, or resolves to the
default BODY_HOME (`~/.body-app`), the current `BODY_HOME` environment value, or `/srv/duna/data/body`,
or when the folder already holds a `body.db` that this script did not create (no `DEMO` marker file next
to it). Re-running into the same demo folder (the marker is there) rebuilds it from scratch, so two runs
give identical data (same seed, fresh tables) -- only the marker's and the rows' own creation timestamps
differ.

Prints only counts and the folder; never a health figure.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import fechas

SEED = 20261105
# BODY-DEMO-TURNOS-1: long enough that the last COMPLETE night block (confirmed shifts only --
# _seed_shifts holds the last 7 days back as a 'proposed' batch, never in `shift`) always has
# turnos_costo.MIN_BLOQUES_PREVIOS earlier night blocks inside turnos_costo.RESUMEN_DIAS, whichever
# day this seeds on: night weeks repeat every 28 days (the demo's 4-week cycle), so a shorter window
# can land with only 1-2 earlier blocks depending on where "today" falls in that cycle -- 120 clears
# it with margin (measured minimum 115 over a full 28-day cycle of anchors).
DIAS_DEFAULT = 120
MARKER_NAME = "DEMO"
_BANNED_HOME = Path("/srv/duna/data/body")

_CYCLE = ["morning", "afternoon", "night", "day"]
_CYCLE_ANCHOR_MONDAY = date(2024, 1, 1)  # a Monday, fixed, so the shift pattern never depends on today
_SHIFT_HOURS = {  # kind -> (start "HH:MM", end "HH:MM", end falls on the next day)
    "morning": ("06:00", "14:00", False),
    "afternoon": ("14:00", "22:00", False),
    "night": ("22:00", "06:00", True),
    "day": ("08:00", "16:00", False),
}
_SHIFT_CODE = {"morning": "M", "afternoon": "T", "night": "N", "day": "D"}

CORE_DENSE = ["resting_hr", "hrv_last_night", "sleep_minutes", "body_battery_wake", "stress_avg",
              "body_battery_high", "body_battery_low", "steps"]
SECONDARY = ["hrv_weekly_avg", "sleep_score", "active_kcal", "intensity_minutes", "sleep_need_min",
             "sleep_deep_min", "sleep_light_min", "sleep_rem_min", "sleep_awake_min",
             "body_battery_sleep_charge", "respiration_awake", "high_stress_min"]
CARDIO_SPARSE = ["vo2max", "vo2max_cycling", "fitness_age"]

# BODY-ENTRENO-SUENO-STORE-1: night-breathing figures, read off DayData's own fields (not
# DayData.metrics) by save_day -- see _DayData and _day_metrics below.
NIGHT_BREATH = ["sleep_spo2_avg", "sleep_respiration_avg", "sleep_respiration_low", "sleep_respiration_high"]

# Self-measured (BODY-ENTRENO-SCHEMA-1): the person's own figures, never the watch's -- sparser
# than daily and stored with source='self' (_seed_self_metrics), never mixed into save_day's
# per-day 'garmin' batch.
# BODY-MEDIDA-PESO-1-M: body_weight_kg and waist_cm follow flex_alcance_cm's own once-a-week
# cadence (a weigh-in or a tape measurement is not a daily habit), not snoring_min's near-daily one.
SELF_MEASURED = ["flex_alcance_cm", "snoring_min", "body_weight_kg", "waist_cm"]

# BODY-AGUJETAS-1: next-day muscle soreness, one key per muscle group (ORCH-CARLOS-DELEGATES-
# TIER-1-1 ruling 1) -- seeded by _seed_soreness below, on its own cadence (the day after an
# invented strength_training session), not this module's weekly/near-daily ones.
SORENESS_KEYS = ["soreness_legs", "soreness_glutes", "soreness_back", "soreness_chest",
                 "soreness_shoulders", "soreness_arms", "soreness_core"]

RANGES = {
    "resting_hr": (48, 62), "hrv_last_night": (40, 90), "hrv_weekly_avg": (45, 85),
    "sleep_minutes": (320, 480), "sleep_score": (40, 95), "stress_avg": (15, 45),
    "body_battery_high": (70, 100), "body_battery_low": (15, 40), "body_battery_wake": (55, 95),
    "steps": (2000, 12000), "active_kcal": (150, 700), "intensity_minutes": (0, 60),
    "sleep_need_min": (420, 480), "sleep_deep_min": (40, 110), "sleep_light_min": (120, 220),
    "sleep_rem_min": (40, 100), "sleep_awake_min": (5, 40), "body_battery_sleep_charge": (10, 60),
    "respiration_awake": (12, 18), "high_stress_min": (0, 90),
    "vo2max": (38, 46), "vo2max_cycling": (40, 48), "fitness_age": (28, 42),
    "flex_alcance_cm": (0, 25), "snoring_min": (0, 70),
    "body_weight_kg": (68, 76), "waist_cm": (78, 88),
    "protein_g": (15, 45),  # one meal's worth; several a day sum into the day's total
    "sleep_spo2_avg": (93, 98), "sleep_respiration_avg": (13, 17),
    "sleep_respiration_low": (9, 13), "sleep_respiration_high": (17, 23),
}
# A night shift (or the recovery day right after) costs sleep and raises stress -- the correlation
# the "turnos" chapter's tutorial task is built to show. Added to the sampled value, then clamped.
NIGHT_EFFECT = {"resting_hr": 6, "hrv_last_night": -12, "sleep_minutes": -70, "stress_avg": 10,
                "body_battery_wake": -15, "body_battery_low": -10, "body_battery_high": -8}
_PCT_KEYS = {"body_battery_high", "body_battery_low", "body_battery_wake", "sleep_score", "sleep_spo2_avg"}

ACTIVITY_TYPES = [
    {"type": "running", "name": "Carrera", "dur": (20, 55), "dist": (3, 10), "hr": (130, 165), "kcal": (250, 550)},
    {"type": "cycling", "name": "Ciclismo", "dur": (30, 90), "dist": (10, 35), "hr": (110, 150), "kcal": (300, 700)},
    {"type": "strength_training", "name": "Fuerza", "dur": (30, 60), "dist": None, "hr": (100, 135),
     "kcal": (180, 320)},
    {"type": "walking", "name": "Caminata", "dur": (20, 45), "dist": (1.5, 4), "hr": (90, 115), "kcal": (120, 220)},
]

_GOAL_TITLES = {
    "es": {"sleep": "Dormir más", "resting_hr": "Bajar la frecuencia cardíaca en reposo", "steps": "Caminar más"},
    "en": {"sleep": "Sleep more", "resting_hr": "Lower my resting heart rate", "steps": "Walk more"},
}
_HABIT_TITLE = {"es": "Dormir antes de un turno nocturno", "en": "Sleep before a night shift"}

# BODY-HOGAR-MIEMBRO-1: a second, invented demo person ("Ana") in one household with the demo
# person, and a shared goal SHE creates, so the demo person -- browsing as themselves -- sees the
# member view (BODY-HOGAR-PLAN-1 rulings 1-2).
_ANA_NAME = "Ana"
_HOGAR_ID = "demo-hogar-1"
_HOGAR_GOAL_TITLE = {"es": "Preparar el ascenso", "en": "Prepare for the climb"}
_HOGAR_HABIT_BOTH = {"es": "Entrenar piernas", "en": "Train legs"}
_HOGAR_HABIT_ANA = {"es": "Revisar el equipo", "en": "Check the gear"}


@dataclass
class _DayData:
    """Same shape body/db.py's save_day() reads from a real adapter's DayData -- kept local to this
    module so the demo never imports body/garmin_adapter.py (Tier 1)."""
    day: str
    source: str = "garmin"  # db.days_with_data() only counts this source; see module docstring
    raw: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    sleep_start: str | None = None
    sleep_end: str | None = None
    activities: list = field(default_factory=list)
    sleep_spo2_avg: float | None = None
    sleep_respiration_avg: float | None = None
    sleep_respiration_low: float | None = None
    sleep_respiration_high: float | None = None


def _refuse(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def _forbidden_home(target: Path) -> str | None:
    resolved = target.expanduser().resolve()
    default_home = (Path.home() / ".body-app").resolve()
    if resolved == default_home:
        return f"refusing: {target} resolves to the default BODY_HOME ({default_home}) -- pass a scratch folder"
    env_value = os.environ.get("BODY_HOME")
    if env_value:
        env_home = Path(env_value).expanduser().resolve()
        if resolved == env_home:
            return f"refusing: {target} resolves to the current BODY_HOME env value ({env_home}) -- pass a scratch folder"
    banned = _BANNED_HOME.resolve()
    if resolved == banned:
        return f"refusing: {target} is the production data folder -- pass a scratch folder"
    return None


def _week_index(d: date) -> int:
    monday = fechas.lunes_de(d)
    return (monday - _CYCLE_ANCHOR_MONDAY).days // 7


def _shift_kind_for(d: date) -> str:
    if d.weekday() >= 5:
        return "off"
    return _CYCLE[_week_index(d) % 4]


_RECOVERY_FACTOR = {0: 1.0, 1: 0.7, 2: 0.4, 3: 0.15}  # days since the block's last night -> effect scale


def _recovery_factor(d: date) -> float:
    """0 while in a night block; tapers off over a few days after it (BODY-TURNOS-COSTO-1: the demo
    must show a believable multi-day recovery, not an instant one)."""
    if _shift_kind_for(d) == "night":
        return 1.0
    for back, factor in _RECOVERY_FACTOR.items():
        if back and _shift_kind_for(d - timedelta(back)) == "night":
            return factor
    return 0.0


def _value(rng: random.Random, key: str, factor: float) -> float:
    lo, hi = RANGES[key]
    v = rng.uniform(lo, hi)
    if factor:
        v += NIGHT_EFFECT.get(key, 0) * factor
    v = max(0.0, v)
    if key in _PCT_KEYS:
        v = min(100.0, v)
    return round(v, 1)


def _import_shifts(conn, shift_import, person_id: int, days: list[date], file_name: str, received: str) -> int | None:
    if not days:
        return None
    shifts = []
    for d in days:
        kind = _shift_kind_for(d)
        if kind == "off":
            shifts.append({"day": d.isoformat(), "kind": "off"})
            continue
        start_h, end_h, crosses = _SHIFT_HOURS[kind]
        end_day = d + timedelta(1) if crosses else d
        shifts.append({
            "day": d.isoformat(), "kind": kind, "code": _SHIFT_CODE[kind],
            "start_local": f"{d.isoformat()}T{start_h}:00", "end_local": f"{end_day.isoformat()}T{end_h}:00",
        })
    extracted_at = datetime.combine(days[-1], datetime.min.time()).isoformat(timespec="seconds")
    data = {"source": {"received": received, "extracted_at": extracted_at}, "person": "Demo",
            "timezone": "America/Bogota", "shifts": shifts}
    return shift_import.propose_data(conn, data, file_name, person_id)


def _avg_metric(conn, db, key: str, days: list[date], person_id: int) -> float | None:
    vals = list(db.series(conn, key, days[0].isoformat(), days[-1].isoformat(), person_id).values())
    return round(statistics.fmean(vals), 1) if vals else None


def _seed_person(conn, db, idioma: str) -> int:
    person_id = db.local_person_id(conn)
    conn.execute("UPDATE person SET display_name = 'Demo', timezone = 'America/Bogota' WHERE id = ?", (person_id,))
    db.set_language(conn, idioma, person_id)
    return person_id


def _day_metrics(rng: random.Random, d: date, factor: float) -> tuple[dict, str | None, str | None, dict]:
    """One day's readings (CORE_DENSE, SECONDARY, the twice-monthly CARDIO_SPARSE batch), when
    sleep_minutes landed that night's sleep window and night-breathing figures -- in the same draw
    order _seed_days always used, since the two-run determinism test depends on the order, not
    just the final values."""
    metrics = {}
    for key in CORE_DENSE:
        if rng.random() < 0.92:
            metrics[key] = _value(rng, key, factor)
    for key in SECONDARY:
        if rng.random() < 0.75:
            metrics[key] = _value(rng, key, factor)
    if d.day % 14 == 0:
        for key in CARDIO_SPARSE:
            metrics[key] = _value(rng, key, 0.0)

    sleep_start = sleep_end = None
    breath = {}
    if "sleep_minutes" in metrics:
        minutes = metrics["sleep_minutes"]
        if _shift_kind_for(d - timedelta(1)) == "night":
            start_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=8, minutes=rng.randint(0, 59))
        else:
            start_dt = (datetime.combine(d - timedelta(1), datetime.min.time())
                        + timedelta(hours=22, minutes=rng.randint(0, 59)))
        end_dt = start_dt + timedelta(minutes=minutes)
        sleep_start, sleep_end = start_dt.isoformat(), end_dt.isoformat()
        # Some nights absent -- the watch sensor was off, same as any other reading.
        for key in NIGHT_BREATH:
            if rng.random() < 0.8:
                breath[key] = _value(rng, key, 0.0)

    return metrics, sleep_start, sleep_end, breath


def _day_activity(rng: random.Random, d: date) -> dict | None:
    """One invented activity on D, 30% of the time, never on a night-shift day."""
    if _shift_kind_for(d) == "night":
        return None
    if rng.random() >= 0.3:
        return None
    act = rng.choice(ACTIVITY_TYPES)
    dist = rng.uniform(*act["dist"]) * 1000 if act["dist"] else None
    return {
        "source_id": f"demo-{d.isoformat()}-1", "start_local": f"{d.isoformat()}T18:{rng.randint(0, 59):02d}:00",
        "type": act["type"], "name": act["name"], "duration_s": round(rng.uniform(*act["dur"]) * 60),
        "distance_m": round(dist) if dist else None, "avg_hr": round(rng.uniform(*act["hr"])),
        "max_hr": round(rng.uniform(*act["hr"]) + 15), "calories": round(rng.uniform(*act["kcal"])),
    }


def _seed_days(conn, db, rng: random.Random, all_days: list[date], person_id: int) -> tuple[int, int]:
    """Every day's save_day call, in order -- metrics then that day's activity, matching the draw
    order a single rng.Random(SEED) has always produced them in."""
    metric_rows = activity_rows = 0
    for d in all_days:
        factor = _recovery_factor(d)
        metrics, sleep_start, sleep_end, breath = _day_metrics(rng, d, factor)
        metric_rows += len(metrics) + len(breath)
        activity = _day_activity(rng, d)
        if activity:
            activity_rows += 1
        db.save_day(conn, _DayData(day=d.isoformat(), metrics=metrics, sleep_start=sleep_start, sleep_end=sleep_end,
                                    activities=[activity] if activity else [], **breath), person_id)
    conn.commit()
    return metric_rows, activity_rows


def _muestra_ms(t: datetime) -> int:
    """A naive LOCAL datetime (America/Bogota, UTC-5 year round -- this module's own fixed person
    timezone) to the UTC-instant millisecond epoch Garmin's own intraday arrays carry, the same
    convention body/garmin_adapter.py's `_utc_to_local_iso` reads back."""
    return int((t + timedelta(hours=5)).replace(tzinfo=timezone.utc).timestamp() * 1000)


# BODY-RECUPERACION-1-C: an invented circadian table (demo-only -- NOT the production model's own
# `_usual_hora` computation, which reads this same data back and medians it per hour) -- low
# overnight, rising through the morning, a steady desk-level plateau through the day, easing off
# in the evening. Feeds `_curva_dia_ordinario`, the background every "ordinary" demo day gets so
# `_usual_hora` has something honest to read for the hours the chosen activities' own recovery
# windows touch.
_PULSO_HORA = {h: v for h, v in enumerate(
    [54, 53, 52, 52, 53, 56, 62, 68, 71, 72, 73, 73, 74, 74, 73, 72, 71, 70, 68, 66, 64, 61, 58, 56]
)}
# Comfortably under every hour's own cutoff (_PULSO_HORA[h] + MODELO['margen_bpm']) in the table
# above, including its lowest overnight trough -- so a "recovered" demo curve always has room to
# cross the cutoff well before its own nominal recovery minute, whichever hour it lands in.
_PLATEAU_RECUPERACION = 50.0


def _curva_dia_ordinario(dia: date) -> list[tuple[datetime, float]]:
    """One ordinary day's own invented heart-rate curve, every two minutes (Garmin's own typical
    intraday spacing) -- the background `_usual_hora` reads its per-hour "usual" from, on days
    that hold none of this person's activities."""
    paso = timedelta(minutes=2)
    t = datetime.combine(dia, datetime.min.time())
    fin = t + timedelta(days=1)
    out = []
    while t < fin:
        out.append((t, float(_PULSO_HORA[t.hour])))
        t += paso
    return out


def _curva_recuperacion(inicio_actividad: datetime, duration_s: float, avg_hr: float,
                         minutos_recuperacion: float | None, horizonte_min: int,
                         sin_cobertura: bool = False) -> list[tuple[datetime, float]]:
    """BODY-RECUPERACION-1-C: an invented heart-rate curve for one activity -- elevated through
    the activity, then decaying to `_PLATEAU_RECUPERACION` over `minutos_recuperacion` minutes
    after it ends, or staying clearly above any plausible cutoff for the whole horizon when
    `minutos_recuperacion` is None (the honest "fuera_de_horizonte" case). `sin_cobertura=True`
    stops the curve strictly before the activity's own end, so the recovery window itself gets no
    sample at all -- the honest "sin_cobertura" case. Every two minutes, matching Garmin's own
    typical intraday spacing."""
    fin = inicio_actividad + timedelta(seconds=duration_s)
    paso = timedelta(minutes=2)
    limite = fin - timedelta(seconds=1) if sin_cobertura else fin + timedelta(minutes=horizonte_min + 20)
    out = []
    t = inicio_actividad
    while t <= limite:
        if t <= fin:
            valor = avg_hr
        elif minutos_recuperacion is None:
            frac = min((t - fin).total_seconds() / 60 / (horizonte_min * 2), 0.6)
            valor = avg_hr - (avg_hr - _PLATEAU_RECUPERACION) * frac
        else:
            frac = min((t - fin).total_seconds() / 60 / minutos_recuperacion, 1.0)
            valor = avg_hr - (avg_hr - _PLATEAU_RECUPERACION) * frac
        out.append((t, round(valor)))
        t += paso
    return out


# BODY-RECUPERACION-1-C: activity types that recover SLOWLY in this demo (cycling and strength
# sessions both run the heart rate harder for longer) vs those that recover FAST (an easy run or
# walk) -- a real, if invented, correlation, deterministic from each activity's own `type`, so
# «Tu historia»'s "tipo_sesion" factor has a guaranteed finding: two groups of its own `type`
# (cycling, running are the demo's own two most common) each clear MODELO['min_n_grupo'], and the
# gap between a "lento" and a "rapido" median is far past MODELO['ruido_min_diferencia_min'].
_TIPOS_LENTOS = {"cycling", "strength_training"}
_PATRON_LENTO = [160.0, 170.0, 180.0, 190.0, 200.0, 210.0]
_PATRON_RAPIDO = [15.0, 20.0, 25.0, 30.0, 35.0]
# Measured against this module's own fixed SEED (tests/test_demo.py's determinism tests pin it):
# the most recent N activities hold at least MODELO['min_n_grupo'] of both a "lento" and a
# "rapido" type. A smaller N occasionally misses that split; this margin is the same kind of
# measured safety DIAS_DEFAULT's own comment already carries for a different cycle.
N_ACTIVIDADES_RECUPERACION = 24


def _seed_recuperacion(conn, db, recuperacion, person_id: int) -> int:
    """Invented minute-by-minute heart rate for the most recent activities `_seed_days` already
    created, so «Tu historia»'s recovery section and Día's own line have something to show on the
    demo server -- never real data, same contract as every other figure in this module.
    BODY-RECUPERACION-1-C's model needs each activity's own look-back days (MODELO
    ['dias_historia']) to have ordinary background heart rate too, or `_usual_hora` would call
    every one of them "sin_rango" -- `_curva_dia_ordinario` fills every day in that combined
    look-back span, once the activity curves are in and their own days are known. Realistic mix on
    purpose: most recover within the horizon (at a "lento" or "rapido" pace correlated with
    `type`, _TIPOS_LENTOS's own doc explains why), exactly one never does ("fuera_de_horizonte")
    and exactly one has no data at all in its own window ("sin_cobertura") -- so «Tu historia»'s
    own censored count AND at least one tipo_sesion finding both show. Accumulates every day's
    samples (background AND activity) BEFORE writing (db.guardar_intradia replaces a whole day's
    row), so two sources that touch the same calendar day never erase each other."""
    rows = conn.execute(
        "SELECT source_id, start_local, duration_s, avg_hr, type FROM activity WHERE person_id = ? "
        "AND start_local IS NOT NULL AND duration_s IS NOT NULL ORDER BY day DESC LIMIT ?",
        (person_id, N_ACTIVIDADES_RECUPERACION),
    ).fetchall()
    if not rows:
        return 0
    horizonte_min = recuperacion.MODELO["horizonte_h"] * 60
    dias_historia = recuperacion.MODELO["dias_historia"]
    por_dia: dict[str, list[tuple[datetime, float]]] = defaultdict(list)

    dias_necesitan_fondo: list[date] = []
    fuera_asignado = False
    gap_asignado = False
    n_lento = n_rapido = 0
    for r in rows:
        inicio = datetime.fromisoformat(r["start_local"])
        lento = r["type"] in _TIPOS_LENTOS
        if lento and not fuera_asignado:
            patron, sin_cobertura, fuera_asignado = None, False, True
        elif not lento and not gap_asignado:
            patron, sin_cobertura, gap_asignado = None, True, True
        elif lento:
            patron, sin_cobertura = _PATRON_LENTO[n_lento % len(_PATRON_LENTO)], False
            n_lento += 1
        else:
            patron, sin_cobertura = _PATRON_RAPIDO[n_rapido % len(_PATRON_RAPIDO)], False
            n_rapido += 1

        curva = _curva_recuperacion(inicio, r["duration_s"], r["avg_hr"] or 130.0,
                                     patron, horizonte_min, sin_cobertura)
        for t, v in curva:
            por_dia[t.date().isoformat()].append((t, v))
        if not sin_cobertura:
            dias_necesitan_fondo.append(inicio.date())

    # The background curve never goes on a day one of the activity curves above already
    # touched (its own day, or the next calendar day when its horizon crosses midnight) -- calendar
    # dates already in `por_dia` at this point are exactly those. On a day being recovered, flat
    # already-"usual" background samples interleaved into the very post-activity window this
    # scenario is inventing would read as an almost-instant recovery, never at its own chosen pace.
    dias_propios = {date.fromisoformat(k) for k in por_dia}
    if dias_necesitan_fondo:
        desde = min(dias_necesitan_fondo) - timedelta(dias_historia)
        hasta = max(dias_necesitan_fondo) - timedelta(1)
        d = desde
        while d <= hasta:
            if d not in dias_propios:
                por_dia[d.isoformat()].extend(_curva_dia_ordinario(d))
            d += timedelta(1)

    for dia, muestras in por_dia.items():
        db.guardar_intradia(conn, person_id, dia, "intradia_fc",
                             {"heartRateValues": [[_muestra_ms(t), v] for t, v in sorted(muestras)]})
    conn.commit()
    recuperacion.recalcular(conn, person_id)
    return len(rows)


def _seed_self_metrics(conn, db, rng: random.Random, all_days: list[date], person_id: int) -> int:
    """The four generic self-measured metric_def rows (BODY-ENTRENO-SCHEMA-1, BODY-MEDIDA-PESO-1),
    sparser than the watch's daily figures: a flexibility reach, a weigh-in and a waist reading
    about once a week each, a snoring minutes-per-night value most nights -- all with source='self',
    via db.set_metric so they never ride along in save_day's single 'garmin'-sourced batch."""
    rows = 0
    for d in all_days:
        if d.weekday() == 0:
            db.set_metric(conn, d.isoformat(), "flex_alcance_cm", _value(rng, "flex_alcance_cm", 0.0),
                           "self", person_id)
            db.set_metric(conn, d.isoformat(), "body_weight_kg", _value(rng, "body_weight_kg", 0.0),
                           "self", person_id)
            db.set_metric(conn, d.isoformat(), "waist_cm", _value(rng, "waist_cm", 0.0), "self", person_id)
            rows += 3
        if rng.random() < 0.85:
            db.set_metric(conn, d.isoformat(), "snoring_min", _value(rng, "snoring_min", 0.0), "self", person_id)
            rows += 1
    conn.commit()
    return rows


def _seed_soreness(conn, db, rng: random.Random, all_days: list[date], person_id: int) -> int:
    """A few invented soreness readings (BODY-AGUJETAS-1), the day after an invented strength
    session already seeded by `_seed_days` (`_day_activity`'s own 'strength_training' draw) --
    one muscle group's reading per session, cycling through SORENESS_KEYS in order. A run that
    draws fewer strength sessions than there are keys still seeds every key at least once (the
    catalogue-coverage guard in tests/test_demo.py), by topping up any key a session never
    reached on the last few seeded days. Values are whole numbers (db.ENTEROS_MEDIDA_PERSONAL),
    never the rounded-to-one-decimal `_value` helper the other self-measured keys use."""
    last_day = all_days[-1]
    strength_days = [date.fromisoformat(r["day"]) for r in conn.execute(
        "SELECT day FROM activity WHERE person_id = ? AND type = 'strength_training' ORDER BY day",
        (person_id,)).fetchall()]
    rows = 0
    seeded = set()
    for i, day in enumerate(strength_days):
        next_day = day + timedelta(1)
        if next_day > last_day:
            continue
        key = SORENESS_KEYS[i % len(SORENESS_KEYS)]
        db.set_metric(conn, next_day.isoformat(), key, float(rng.randint(1, 4)), "self", person_id)
        seeded.add(key)
        rows += 1
    for j, key in enumerate(k for k in SORENESS_KEYS if k not in seeded):
        db.set_metric(conn, (last_day - timedelta(j)).isoformat(), key, float(rng.randint(1, 4)),
                       "self", person_id)
        rows += 1
    conn.commit()
    return rows


# BODY-AYUNO-1: one "HH:MM"-ish window per meal slot (breakfast, lunch, afternoon snack, dinner),
# in minutes-of-day, so the demo's 3rd/4th entries always land after the 1st/2nd -- a real window,
# never an invented one -- and the eating-window card has something to show on the demo server.
_COMIDA_MINUTOS = [(7 * 60, 8 * 60), (12 * 60, 14 * 60), (16 * 60, 17 * 60), (18 * 60, 20 * 60)]


def _seed_ingesta(conn, db, rng: random.Random, all_days: list[date], person_id: int) -> int:
    """3-4 invented protein entries most days (BODY-PROTEINA-1), each its own `ingesta` row WITH a
    time (BODY-AYUNO-1), one per slot in `_COMIDA_MINUTOS` -- one writer (db.registrar_ingesta)
    sums them into that day's protein_g total, so Tendencias shows it, and the times give Hoy's
    and Día's eating-window card a real window. Invented amounts and times only, never a real
    figure; no descripcion, since the demo seeds no words."""
    rows = 0
    for d in all_days:
        if rng.random() < 0.9:
            for lo, hi in _COMIDA_MINUTOS[:rng.randint(3, 4)]:
                minuto = rng.randint(lo, hi - 1)
                ts_local = f"{d.isoformat()}T{minuto // 60:02d}:{minuto % 60:02d}:00"
                db.registrar_ingesta(conn, person_id, d.isoformat(), "protein_g",
                                      _value(rng, "protein_g", 0.0), False, ts_local=ts_local)
                rows += 1
    conn.commit()
    return rows


def _seed_periodo(conn, db, all_days: list[date], person_id: int) -> int:
    """Two invented periods: a closed 'malestar' stretch partway through the seeded history
    (BODY-PERIODO-1), so screens and analyses that leave marked days out have something to exclude
    in the demo, and an OPEN 'lesion' period STARTING a few days before today (BODY-PERIODO-
    PANTALLAS-1-B), as a person would actually mark one -- "unwell since the 5th", not "unwell
    exactly as of this instant". dias_marcados() no longer re-reads the timezone per call while an
    open period overlaps a lookback (that cost is gone, not hidden -- db.py's own
    _periodos_de_persona), so starting it inside every baseline's 30-day lookback costs nothing
    extra here."""
    start = all_days[len(all_days) // 2]
    end = start + timedelta(2)
    db.crear_periodo(conn, person_id, "malestar", start.isoformat(), end.isoformat())
    hoy = all_days[-1]
    db.crear_periodo(conn, person_id, "lesion", (hoy - timedelta(3)).isoformat(), None)
    conn.commit()
    return 2


def _seed_shifts(conn, shift_import, turnos_costo, person_id: int,
                  all_days: list[date]) -> tuple[int | None, int | None, int, int]:
    confirmed_days, proposed_days = all_days[:-7], all_days[-7:]
    confirmed_id = _import_shifts(conn, shift_import, person_id, confirmed_days, "turnos-demo-confirmados.json",
                                   received=confirmed_days[0].isoformat())
    if confirmed_id is not None:
        shift_import.confirm(conn, confirmed_id)
    proposed_id = _import_shifts(conn, shift_import, person_id, proposed_days, "turnos-demo-propuestos.json",
                                  received=proposed_days[0].isoformat())
    night_weeks = len({_week_index(d) for d in all_days if _shift_kind_for(d) == "night"})
    night_blocks = len(turnos_costo.bloques_noche(conn, person_id, all_days[0].isoformat(), all_days[-1].isoformat()))
    return confirmed_id, proposed_id, night_weeks, night_blocks


def _seed_goals_and_habits(conn, db, goals, rng: random.Random, all_days: list[date], anchor: date,
                            first_monday: date, anchor_monday: date, person_id: int,
                            idioma: str) -> tuple[int, int]:
    """The demo person's three metric goals, one habit, their weekly checks and the habit's
    checks -- returns (goal weekly checks, habit checks)."""
    window = all_days[:14]
    base_sleep = _avg_metric(conn, db, "sleep_minutes", window, person_id) or 420.0
    base_rhr = _avg_metric(conn, db, "resting_hr", window, person_id) or 58.0
    base_steps = _avg_metric(conn, db, "steps", window, person_id) or 6000.0
    start_day = all_days[0].isoformat()
    deadline = (anchor + timedelta(days=56)).isoformat()
    titles = _GOAL_TITLES[idioma]

    gid_sleep = goals.propose(conn, person_id, titles["sleep"], start_day, kind="metric", metric_key="sleep_minutes",
                               direction="raise", target_value=round(base_sleep + 40, 1), baseline_value=base_sleep,
                               deadline=deadline)
    gid_rhr = goals.propose(conn, person_id, titles["resting_hr"], start_day, kind="metric", metric_key="resting_hr",
                             direction="lower", target_value=round(base_rhr - 5, 1), baseline_value=base_rhr,
                             deadline=deadline)
    gid_steps = goals.propose(conn, person_id, titles["steps"], start_day, kind="metric", metric_key="steps",
                               direction="raise", target_value=round(base_steps + 2000, 1), baseline_value=base_steps,
                               deadline=deadline)
    for gid in (gid_sleep, gid_rhr, gid_steps):
        goals.activate(conn, gid)

    habit_id = goals.add_habit(conn, gid_sleep, _HABIT_TITLE[idioma],
                                {"per": "week", "times": 3, "context": "before_night"})

    checks = 0
    monday = first_monday
    while monday <= anchor_monday:
        for gid in (gid_sleep, gid_rhr, gid_steps):
            if goals.weekly_check(conn, gid, person_id, monday.isoformat()):
                checks += 1
        monday += timedelta(7)

    habit_checks = 0
    recent_monday = max(first_monday, anchor_monday - timedelta(35))
    monday = recent_monday
    while monday <= anchor_monday:
        for dd in goals._context_due_days(conn, person_id, monday, "before_night"):
            if date.fromisoformat(dd) <= anchor:
                goals.check_habit(conn, habit_id, person_id, dd, rng.random() < 0.7, hoy=anchor)
                habit_checks += 1
        monday += timedelta(7)

    return checks, habit_checks


def _seed_household(conn, db, goals, enlaces, compartir, rng: random.Random, all_days: list[date], anchor: date,
                     first_monday: date, anchor_monday: date, person_id: int,
                     idioma: str) -> tuple[int, int, int]:
    """A second demo person (Ana), one household with the demo person, her shared figure, and a
    household goal she creates -- returns (her seeded metric rows, goal weekly checks, habit checks)."""
    start_day = all_days[0].isoformat()
    ana_cur = conn.execute(
        "INSERT INTO person (display_name, timezone, language) VALUES (?, 'America/Bogota', ?)",
        (_ANA_NAME, idioma),
    )
    ana_id = ana_cur.lastrowid
    hogar_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, ?)",
        [(person_id, _HOGAR_ID, hogar_now), (ana_id, _HOGAR_ID, hogar_now)])
    conn.commit()
    # Ana shares one figure with the household (BODY-HOGAR-COMPARTIR-1), so the demo person's own
    # Hoy shows the «Tu hogar» card. She needs her OWN short invented history of it -- never the
    # demo person's numbers copied -- or the card can only ever say "sin dato" and the value path
    # is unmeasurable; a few of her own days, inside the same catalogue range, are enough.
    ana_days = all_days[-5:]
    for d in ana_days:
        db.set_metric(conn, d.isoformat(), "resting_hr", _value(rng, "resting_hr", 0.0), "garmin", ana_id)
    conn.commit()
    compartir.compartir(conn, ana_id, "resting_hr")

    hogar_deadline = (anchor + timedelta(days=84)).isoformat()
    gid_hogar = goals.propose(conn, ana_id, _HOGAR_GOAL_TITLE[idioma], start_day, area="adventure",
                              kind="milestone", deadline=hogar_deadline, hogar_id=_HOGAR_ID,
                              visibility="household")
    goals.activate(conn, gid_hogar)
    # DESIGN rule 17 (BODY-HOGAR-ENLACE-1): the adventure goal links to its trip in Lugares -- an
    # invented demo id, never a real one.
    enlaces.enlazar(conn, gid_hogar, "lugares", "4821")
    habit_both_id = goals.add_habit(conn, gid_hogar, _HOGAR_HABIT_BOTH[idioma], {"per": "week", "times": 2})
    goals.add_habit(conn, gid_hogar, _HOGAR_HABIT_ANA[idioma], {"per": "week", "times": 1},
                     assignee_person_id=ana_id)

    hogar_checks = hogar_habit_checks = 0
    hogar_recent_monday = max(first_monday, anchor_monday - timedelta(21))
    monday = hogar_recent_monday
    while monday <= anchor_monday:
        for dd in (monday.isoformat(), (monday + timedelta(3)).isoformat()):
            if date.fromisoformat(dd) <= anchor:
                goals.check_habit(conn, habit_both_id, ana_id, dd, rng.random() < 0.7, hoy=anchor)
                hogar_habit_checks += 1
        if goals.weekly_check(conn, gid_hogar, ana_id, monday.isoformat()):
            hogar_checks += 1
        monday += timedelta(7)

    return len(ana_days), hogar_checks, hogar_habit_checks


def _seed(conn, *, dias: int, idioma: str) -> dict[str, int]:
    from . import compartir, db, enlaces, goals, recuperacion, shift_import, turnos_costo

    person_id = _seed_person(conn, db, idioma)

    rng = random.Random(SEED)
    anchor = date.today()
    all_days = [anchor - timedelta(dias - 1 - i) for i in range(dias)]
    first_monday, anchor_monday = fechas.lunes_de(all_days[0]), fechas.lunes_de(anchor)

    metric_rows, activity_rows = _seed_days(conn, db, rng, all_days, person_id)
    recuperacion_rows = _seed_recuperacion(conn, db, recuperacion, person_id)
    metric_rows += _seed_self_metrics(conn, db, rng, all_days, person_id)
    metric_rows += _seed_soreness(conn, db, rng, all_days, person_id)
    entradas_ingesta = _seed_ingesta(conn, db, rng, all_days, person_id)
    periodos = _seed_periodo(conn, db, all_days, person_id)

    confirmed_id, proposed_id, night_weeks, night_blocks = _seed_shifts(
        conn, shift_import, turnos_costo, person_id, all_days)

    checks, habit_checks = _seed_goals_and_habits(
        conn, db, goals, rng, all_days, anchor, first_monday, anchor_monday, person_id, idioma)

    ana_metric_rows, hogar_checks, hogar_habit_checks = _seed_household(
        conn, db, goals, enlaces, compartir, rng, all_days, anchor, first_monday, anchor_monday, person_id, idioma)
    metric_rows += ana_metric_rows

    return {
        "personas": 2, "dias_sembrados": dias, "valores_diarios": metric_rows, "actividades": activity_rows,
        "lotes_turnos_confirmados": 1 if confirmed_id is not None else 0,
        "lotes_turnos_propuestos": 1 if proposed_id is not None else 0,
        "semanas_nocturnas": night_weeks, "bloques_turno_noche": night_blocks, "metas_activas": 4, "habitos": 3,
        "verificaciones_semanales": checks + hogar_checks, "chequeos_habito": habit_checks + hogar_habit_checks,
        "entradas_ingesta": entradas_ingesta, "periodos": periodos, "recuperacion_actividades": recuperacion_rows,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m body.demo")
    ap.add_argument("--home")
    ap.add_argument("--dias", type=int, default=DIAS_DEFAULT)
    ap.add_argument("--idioma", choices=("es", "en"), default="es")
    args = ap.parse_args(argv)

    if not args.home:
        _refuse("--home is required: pass a scratch folder for the demo (never the real BODY_HOME)")
        return
    home = Path(args.home)
    reason = _forbidden_home(home)
    if reason:
        _refuse(reason)
        return
    home_resolved = home.expanduser().resolve()
    home_resolved.mkdir(parents=True, exist_ok=True)
    db_path = home_resolved / "body.db"
    marker_path = home_resolved / MARKER_NAME
    if db_path.exists() and not marker_path.exists():
        _refuse(f"refusing: {db_path} already exists and is not a demo database (no {MARKER_NAME} marker) "
                 "-- pass an empty or demo-marked folder")
        return
    if db_path.exists():
        db_path.unlink()
    if marker_path.exists():
        marker_path.unlink()

    from . import config
    config.BODY_HOME = home_resolved
    config.DB_PATH = db_path
    config.TOKEN_DIR = home_resolved / "garmin_tokens"

    from . import db
    conn = db.connect()
    try:
        db.init(conn)
        counts = _seed(conn, dias=args.dias, idioma=args.idioma)
    finally:
        conn.close()

    marker_path.write_text(
        json.dumps({"seed": SEED, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
        encoding="utf-8",
    )

    print(f"demo home: {home_resolved}")
    for k, v in counts.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
