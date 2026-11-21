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
    # BODY-COMIDA-1: the rest of a meal's own worth, same "one meal, several a day sum" reading
    # as protein_g above. alcohol_g is grams of pure alcohol (ORCH-CARLOS-DELEGATES-TIER-1-1
    # ruling 2), not a drink count.
    "kcal": (250, 750), "carbs_g": (20, 90), "fat_g": (8, 35), "fiber_g": (2, 12),
    "water_ml": (150, 400), "caffeine_mg": (30, 120), "alcohol_g": (8, 20),
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

# BODY-TENDENCIAS-RACHA-GRAFICO-1 ruling 4: the demo person's own pick for the strength-session
# study on /trends -- a dedicated habit under the resting-heart-rate goal, since no existing habit
# is weekly-strength-shaped.
_HABIT_FUERZA_TITLE = {"es": "Entrenar fuerza", "en": "Strength training"}

# BODY-HISTORIA-VIDA-2 ruling 5: a habit under the steps goal that cites the "pasos" demo practice
# as its evidence (habit.practica_id), so the demo home shows the goal page's watch-evidence line.
_HABIT_PASOS_TITLE = {"es": "Caminar bastante casi todos los días", "en": "Walk a lot most days"}

# BODY-HOGAR-MIEMBRO-1: a second, invented demo person ("Ana") in one household with the demo
# person, and a shared goal SHE creates, so the demo person -- browsing as themselves -- sees the
# member view (BODY-HOGAR-PLAN-1 rulings 1-2).
_ANA_NAME = "Ana"
_HOGAR_ID = "demo-hogar-1"
_HOGAR_GOAL_TITLE = {"es": "Preparar el ascenso", "en": "Prepare for the climb"}
_HOGAR_HABIT_BOTH = {"es": "Entrenar piernas", "en": "Train legs"}
_HOGAR_HABIT_ANA = {"es": "Revisar el equipo", "en": "Check the gear"}

# BODY-COMIDA-COMPARTIDA-1 ruling 7: one pending shared meal each direction, so the demo person's
# own Hoy shows the card and Ana's settings show consent already on.
_COMIDA_DEMO_PERSON = {"es": "Pollo con arroz", "en": "Chicken with rice"}
_COMIDA_DEMO_ANA = {"es": "Ensalada con atún", "en": "Tuna salad"}

# BODY-CUERPO-VISTA-1-M ruling 4: the demo person's own training session for today, invented,
# categorised with anatomia.CATEGORIA_ENUM's own words -- "Upper B", the same session mock round 2
# showed, so Hoy's body view lights front (hombro) and back (espalda) at once.
_PLAN_HOY_TITLE = {"es": "Upper B", "en": "Upper B"}
_PLAN_HOY_EJERCICIOS = {
    "es": [
        {"ejercicio": "press militar", "series": 3, "reps": 8, "categoria": "hombro"},
        {"ejercicio": "curl de bíceps", "series": 3, "reps": 10, "categoria": "brazo"},
        {"ejercicio": "remo con barra", "series": 4, "reps": 8, "categoria": "espalda"},
    ],
    "en": [
        {"ejercicio": "overhead press", "series": 3, "reps": 8, "categoria": "hombro"},
        {"ejercicio": "biceps curl", "series": 3, "reps": 10, "categoria": "brazo"},
        {"ejercicio": "barbell row", "series": 4, "reps": 8, "categoria": "espalda"},
    ],
}

# BODY-LESION-ZONA-1 ruling 6: the invented group the demo's open 'lesion' period names -- the
# same "hombro" the plan above already trains, so Hoy lights AND hatches it at once, and one of
# this person's own past strength_training sets (found by _seed_periodo, below) gets corrected to
# this same categoria and exercise so a registered read shows ruling 2's line too.
_LESION_ZONA_DEMO = "hombro"
_LESION_SET_EJERCICIO = {"es": "press militar", "en": "overhead press"}
# BODY-PLAN-FASES-5 ruling 1: the invented entreno_dolor row _seed_periodo attaches to that same
# corrected set, already answered (dolor_manana filled) -- so the demo shows the session screen's
# dolor card prefilled (ruling 3) without ever adding a pending morning-after card to "/" (ruling
# 4), which would make its own statement ceiling depend on how far back the corrected day lands.
_LESION_DOLOR_MOVIMIENTO = {"es": "al bajar la barra", "en": "lowering the bar"}
# BODY-PLAN-MODULO-1-R: how far _seed_periodo's open 'lesion' period may widen to reach the most
# recent strength_training day -- an unbounded widen can mark most of stress_avg's own 30-day
# baseline window (baselines.WINDOW_DAYS) on a date whose nearest strength session happens to be
# old, starving it below baselines.MIN_DAYS (7) and turning the Hoy tile's partial-day band into a
# false "not enough data yet" instead of the usual band it should show every day the demo runs on.
# 15 leaves baselines.WINDOW_DAYS - 15 = 15 unmarked days in that window -- comfortably over
# MIN_DAYS even allowing for the ~8% chance any one of them rolled stress_avg absent.
_LESION_MAX_DIAS_ATRAS = 15

# BODY-ENTRENO-VINCULO-1 ruling 8: the manual session's own title on the one demo day seeded
# already linked to its watch session, so Día shows ruling 3's "joined" line without the person
# needing to do anything.
_ENTRENO_VINCULADO_TITULO = {"es": "Fuerza (registrada a mano)", "en": "Strength (logged by hand)"}
# BODY-ENTRENO-VINCULO-1-M: the invented watch strength activity's own name follows the demo
# language the same way, so the linked card's watch side reads "Strength" in English too.
_ENTRENO_WATCH_TITULO = {"es": "Fuerza", "en": "Strength"}


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


@dataclass
class _SetData:
    """Same shape body/db.py's save_exercise_sets() reads from a real adapter's ExerciseSet --
    kept local to this module, same reason _DayData is (demo.py never imports
    body/garmin_adapter.py, Tier 1)."""
    set_type: str = "active"
    category: str | None = None
    exercise: str | None = None
    reps: float | None = None
    weight_value: float | None = None
    weight_unit: str | None = None
    duration_s: float | None = None
    start_local: str | None = None


# BODY-ENTRENO-VISTA-SESION-1 ruling 6: the demo person's own dedicated strength session TODAY
# (two exercises, real rest gaps, one corrected set, one excluded set, a per-minute heart-rate
# series covering it) and a basketball session YESTERDAY with no series -- so both of ruling 4's
# own branches (the curve, and the honest "coming soon" line) show on the demo home regardless of
# whatever `_day_activity`'s own random draw would otherwise have put on either day. Invented
# figures only, same contract as every other demo value.
_SESION_FUERZA_TITULO = _ENTRENO_WATCH_TITULO
_SESION_BASKET_TITULO = {"es": "Baloncesto", "en": "Basketball"}
_SESION_FUERZA_EJERCICIOS = {"es": ["press banca", "sentadilla"], "en": ["bench press", "squat"]}
_SESION_FUERZA_CATEGORIAS = ["pecho", "pierna"]
# Per set (8, in order): reps, weight_kg, duration_s, rest_after_s (None on the last -- no rest
# after a session's own final set), the HR peak this set's own window reaches, and the HR this
# session settles to by the end of the rest that follows (the "valley" the drop is measured
# against) -- None on the last set, same reason as rest_after_s. Set index 1 (0-based) is the one
# the person corrects (reps 8 -> 6, a lighter top set); set index 6 is the one the person excludes
# (the watch logged it, the person says it did not happen).
_SESION_FUERZA_SETS = [
    {"reps": 8, "peso": 40.0, "dur": 40, "descanso": 75, "pico": 120, "valle": 108},
    {"reps": 8, "peso": 40.0, "dur": 40, "descanso": 75, "pico": 124, "valle": 110},
    {"reps": 8, "peso": 40.0, "dur": 40, "descanso": 75, "pico": 128, "valle": 109},
    {"reps": 8, "peso": 40.0, "dur": 40, "descanso": 150, "pico": 131, "valle": 115},
    {"reps": 10, "peso": 60.0, "dur": 45, "descanso": 90, "pico": 138, "valle": 117},
    {"reps": 10, "peso": 60.0, "dur": 45, "descanso": 90, "pico": 141, "valle": 119},
    {"reps": 10, "peso": 60.0, "dur": 45, "descanso": 90, "pico": 144, "valle": 121},
    {"reps": 10, "peso": 60.0, "dur": 45, "descanso": None, "pico": 147, "valle": None},
]
_SESION_FUERZA_SET_CORREGIDO = 1  # 0-based: the person's correction lands here (ruling 6)
_SESION_FUERZA_SET_EXCLUIDO = 6  # 0-based: the person's exclusion lands here (ruling 6)
_SESION_FUERZA_HORA = "18:00:00"
_SESION_BASKET_HORA = "18:00:00"
_SESION_BASKET_DUR_S = 50 * 60
_SESION_BASKET_HR = {"avg": 128, "max": 162, "kcal": 410}


def _sets_sesion_fuerza(dia: date, idioma: str) -> list[dict]:
    """The 8 sets above, timed back to back from `_SESION_FUERZA_HORA` -- each one's own
    `start_local`/`duration_s`/`rest_after_s`, the same shape body/entreno_sesion.py's own
    `series()` computes from stored rows, so the HR curve builder below (`_curva_sesion_fuerza`)
    can place each set's own window and each rest's own window without recomputing anything."""
    ejercicios = _SESION_FUERZA_EJERCICIOS[idioma]
    t = datetime.combine(dia, datetime.min.time()) + timedelta(
        hours=int(_SESION_FUERZA_HORA[:2]), minutes=int(_SESION_FUERZA_HORA[3:5]))
    out = []
    for i, s in enumerate(_SESION_FUERZA_SETS):
        exercise = ejercicios[0] if i < 4 else ejercicios[1]
        categoria = _SESION_FUERZA_CATEGORIAS[0] if i < 4 else _SESION_FUERZA_CATEGORIAS[1]
        out.append({
            "exercise": exercise, "category": categoria, "reps": s["reps"], "weight_kg": s["peso"],
            "start_local": t.isoformat(), "duration_s": float(s["dur"]),
            "rest_after_s": float(s["descanso"]) if s["descanso"] is not None else None,
            "pico": s["pico"], "valle": s["valle"],
        })
        t += timedelta(seconds=s["dur"])
        if s["descanso"] is not None:
            t += timedelta(seconds=s["descanso"])
    return out


def _curva_sesion_fuerza(sets: list[dict]) -> list[tuple[datetime, float]]:
    """BODY-ENTRENO-VISTA-SESION-1 ruling 6: one sample per clock MINUTE (Garmin's own real
    intraday granularity for heart rate -- garmin_adapter._utc_to_local_iso stores every sample at
    `timespec='minutes'`, so two samples inside the same minute can never be told apart once
    stored; a finer step here would silently collapse), interpolated between hand-placed keyframes
    -- each set's own `pico` at the set's own end, each rest's own `valle` at the rest's own end --
    so ruling 4's per-set peak and per-rest drop read back these exact numbers wherever a real
    per-minute sample happens to fall inside that set's or that rest's own window; a set or rest
    shorter than a minute can honestly fall between two samples and get neither, same as a real
    watch would read it."""
    paso = timedelta(minutes=1)
    keyframes: list[tuple[datetime, float]] = [
        (datetime.fromisoformat(sets[0]["start_local"]), sets[0]["pico"] - 20.0)]
    for s in sets:
        t1 = datetime.fromisoformat(s["start_local"]) + timedelta(seconds=s["duration_s"])
        keyframes.append((t1, float(s["pico"])))
        if s["rest_after_s"]:
            keyframes.append((t1 + timedelta(seconds=s["rest_after_s"]), float(s["valle"])))

    out: list[tuple[datetime, float]] = []
    t = keyframes[0][0].replace(second=0, microsecond=0)
    if t < keyframes[0][0]:
        t += paso
    idx = 0
    while t <= keyframes[-1][0]:
        while idx + 1 < len(keyframes) - 1 and keyframes[idx + 1][0] <= t:
            idx += 1
        (ta, va), (tb, vb) = keyframes[idx], keyframes[idx + 1]
        frac = (t - ta).total_seconds() / (tb - ta).total_seconds() if tb > ta else 0.0
        out.append((t, round(va + (vb - va) * frac, 1)))
        t += paso
    return out


def _seed_entreno_vista_sesion_actividades(conn, anchor: date, person_id: int, idioma: str) -> tuple[str, str]:
    """BODY-ENTRENO-VISTA-SESION-1 ruling 6, phase 1 of 2 (see `_seed_entreno_vista_sesion_detalle`
    for phase 2): replaces whatever `_seed_days` drew for `anchor` and `anchor - 1 day` with a
    dedicated strength session (today) and a dedicated basketball session (yesterday) --
    `INSERT OR REPLACE` on `activity`'s own (person_id, source, source_id) key, the SAME
    `source_id` pattern (`demo-{day}-1`) `_day_activity` already uses for that day, so this is the
    one and only activity either day ever ends up with. Called right after `_seed_days`, BEFORE
    `_seed_periodo` -- its own lesion-period correction picks "the most recent strength_training
    day before today" (`_seed_periodo`'s own query), and yesterday must already have stopped being
    one by then, or that pick could land on the basketball day itself (whatever `_day_activity`'s
    own random draw had put there). Returns (strength day, basketball day) as ISO strings, for
    `_seed_recuperacion`'s/`_seed_turno_fc`'s own exclusion sets."""
    dia_fuerza = anchor
    dia_basket = anchor - timedelta(1)
    sid_fuerza = f"demo-{dia_fuerza.isoformat()}-1"
    sid_basket = f"demo-{dia_basket.isoformat()}-1"
    sets = _sets_sesion_fuerza(dia_fuerza, idioma)
    duracion_fuerza = (datetime.fromisoformat(sets[-1]["start_local"])
                        + timedelta(seconds=sets[-1]["duration_s"])
                        - datetime.fromisoformat(sets[0]["start_local"])).total_seconds()
    curva = _curva_sesion_fuerza(sets)
    avg_hr = round(statistics.mean(v for _, v in curva))
    max_hr = round(max(v for _, v in curva))

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO activity (person_id, source_id, day, start_local, type, name, "
            "duration_s, distance_m, avg_hr, max_hr, calories, source) "
            "VALUES (?, ?, ?, ?, 'strength_training', ?, ?, NULL, ?, ?, ?, 'garmin')",
            (person_id, sid_fuerza, dia_fuerza.isoformat(), sets[0]["start_local"],
             _SESION_FUERZA_TITULO[idioma], duracion_fuerza, avg_hr, max_hr, 220),
        )
        conn.execute(
            "INSERT OR REPLACE INTO activity (person_id, source_id, day, start_local, type, name, "
            "duration_s, distance_m, avg_hr, max_hr, calories, source) "
            "VALUES (?, ?, ?, ?, 'other', ?, ?, NULL, ?, ?, ?, 'garmin')",
            (person_id, sid_basket, dia_basket.isoformat(),
             f"{dia_basket.isoformat()}T{_SESION_BASKET_HORA}", _SESION_BASKET_TITULO[idioma],
             _SESION_BASKET_DUR_S, _SESION_BASKET_HR["avg"], _SESION_BASKET_HR["max"],
             _SESION_BASKET_HR["kcal"]),
        )
    return dia_fuerza.isoformat(), dia_basket.isoformat()


def _seed_entreno_vista_sesion_detalle(conn, db, dia_fuerza: str, person_id: int, idioma: str) -> None:
    """BODY-ENTRENO-VISTA-SESION-1 ruling 6, phase 2 of 2: the strength day's own sets (one
    corrected, one excluded) and its hand-placed per-minute heart-rate series. Called AFTER
    `_seed_periodo`, not before -- `_seed_periodo` may correct an OLDER strength day's own set 1 to
    its invented lesion zona (`tests/test_demo.py`'s `test_a_past_strength_set_is_corrected_to_the_
    lesion_zona` reads back whichever `workout_set` row with `corrected_by_person = 1` SQLite
    returns first, with no ORDER BY -- rowid order, so insertion order). Running this phase after
    that correction keeps this session's own correction from ever being the one that query
    happens to read back first. Called BEFORE `_seed_recuperacion`, so its generic model never
    overwrites the per-minute series this writes."""
    sid_fuerza = f"demo-{dia_fuerza}-1"
    sets = _sets_sesion_fuerza(date.fromisoformat(dia_fuerza), idioma)
    curva = _curva_sesion_fuerza(sets)

    db.save_exercise_sets(conn, person_id, "garmin", sid_fuerza, [
        _SetData(category=s["category"], exercise=s["exercise"], reps=s["reps"],
                 weight_value=s["weight_kg"], weight_unit="kg", duration_s=s["duration_s"],
                 start_local=s["start_local"])
        for s in sets
    ])
    corregido = sets[_SESION_FUERZA_SET_CORREGIDO]
    db.correct_exercise_set(conn, person_id, "garmin", sid_fuerza, _SESION_FUERZA_SET_CORREGIDO + 1,
                             exercise=corregido["exercise"], category=corregido["category"],
                             reps=corregido["reps"] - 2, weight_kg=corregido["weight_kg"])
    db.set_series_excluida(conn, person_id, "garmin", sid_fuerza, _SESION_FUERZA_SET_EXCLUIDO + 1, True)

    db.guardar_intradia(conn, person_id, dia_fuerza, "intradia_fc",
                         {"heartRateValues": [[_muestra_ms(t), v] for t, v in curva]})
    conn.commit()


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


def _day_activity(rng: random.Random, d: date, idioma: str) -> dict | None:
    """One invented activity on D, 30% of the time, never on a night-shift day."""
    if _shift_kind_for(d) == "night":
        return None
    if rng.random() >= 0.3:
        return None
    act = rng.choice(ACTIVITY_TYPES)
    dist = rng.uniform(*act["dist"]) * 1000 if act["dist"] else None
    name = _ENTRENO_WATCH_TITULO[idioma] if act["type"] == "strength_training" else act["name"]
    return {
        "source_id": f"demo-{d.isoformat()}-1", "start_local": f"{d.isoformat()}T18:{rng.randint(0, 59):02d}:00",
        "type": act["type"], "name": name, "duration_s": round(rng.uniform(*act["dur"]) * 60),
        "distance_m": round(dist) if dist else None, "avg_hr": round(rng.uniform(*act["hr"])),
        "max_hr": round(rng.uniform(*act["hr"]) + 15), "calories": round(rng.uniform(*act["kcal"])),
    }


def _seed_days(conn, db, rng: random.Random, all_days: list[date], person_id: int, idioma: str) -> tuple[int, int]:
    """Every day's save_day call, in order -- metrics then that day's activity, matching the draw
    order a single rng.Random(SEED) has always produced them in."""
    metric_rows = activity_rows = 0
    for d in all_days:
        factor = _recovery_factor(d)
        metrics, sleep_start, sleep_end, breath = _day_metrics(rng, d, factor)
        metric_rows += len(metrics) + len(breath)
        activity = _day_activity(rng, d, idioma)
        if activity:
            activity_rows += 1
        db.save_day(conn, _DayData(day=d.isoformat(), metrics=metrics, sleep_start=sleep_start, sleep_end=sleep_end,
                                    activities=[activity] if activity else [], **breath), person_id)
    conn.commit()
    return metric_rows, activity_rows


def _seed_entreno_vinculado(conn, db, person_id: int, idioma: str) -> None:
    """BODY-ENTRENO-VINCULO-1 ruling 8: one manual-first day already linked, so Día shows ruling
    3's own "joined with your watch's session" line on the demo server without the person needing
    to do anything. The EARLIEST strength_training day `_seed_days` already made, never a random
    draw (test_demo.py's own two-runs-identical guard needs this deterministic). The link is set
    directly through `db.set_vinculo_manual` -- seeding is invented fixture data, not a real sync,
    so there is no watch activity to insert a second time through `db.save_day`'s own auto-link."""
    fila = conn.execute(
        "SELECT source, source_id, day FROM activity WHERE person_id = ? AND type = 'strength_training' "
        "ORDER BY day LIMIT 1", (person_id,),
    ).fetchone()
    if fila is None:
        return
    # A fixed id, never db.create_manual_session's own uuid4 -- this row's id must be the same on
    # every run for the same seed (test_demo.py's own two-runs-identical guard).
    manual_id = f"demo-manual-{fila['day']}"
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) "
        "VALUES (?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, 'manual')",
        (person_id, manual_id, fila["day"], _ENTRENO_VINCULADO_TITULO[idioma]),
    )
    db.set_vinculo_manual(conn, person_id, fila["source"], fila["source_id"], manual_id, False)
    conn.commit()


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


def _seed_recuperacion(conn, db, recuperacion, person_id: int,
                        excluir_dias: frozenset[str] = frozenset()) -> int:
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
    row), so two sources that touch the same calendar day never erase each other.

    `excluir_dias` (BODY-ENTRENO-VISTA-SESION-1 ruling 6): the strength/basketball pair
    `_seed_entreno_vista_sesion` already gave their own hand-placed (or deliberately absent)
    heart-rate series -- this model's own generic curve must never touch either day's activity."""
    filtro_excluir = f"AND day NOT IN ({','.join('?' * len(excluir_dias))}) " if excluir_dias else ""
    rows = conn.execute(
        "SELECT source_id, start_local, duration_s, avg_hr, type FROM activity WHERE person_id = ? "
        "AND start_local IS NOT NULL AND duration_s IS NOT NULL " + filtro_excluir +
        "ORDER BY day DESC LIMIT ?",
        (person_id, *excluir_dias, N_ACTIVIDADES_RECUPERACION),
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


_TURNO_FC_OFFSET_BPM = 10.0
N_TURNOS_FC = 8


def _dias_con_intradia_fc(conn, person_id: int) -> set[str]:
    return {r["day"] for r in conn.execute(
        "SELECT DISTINCT day FROM raw_payload WHERE person_id = ? AND kind = 'intradia_fc'",
        (person_id,)).fetchall()}


def _dias_actividades_recuperacion(conn, person_id: int, limite: int) -> set[str]:
    """The exact calendar days (and the one after, for a recovery window that crosses midnight)
    `_seed_recuperacion`'s own most-recent-`limite` activities fill with a DELIBERATE, non-flat
    curve -- the only days `_seed_turno_fc` must never touch again. Every other day, even one
    `_seed_recuperacion`'s own background fill already reached, is safe to adjust further: that
    fill is the SAME flat curve either way, and `recuperacion_actividad`'s own rows are already
    computed and stored (`recuperacion.recalcular` ran inside `_seed_recuperacion`, before this
    function is ever called) -- overwriting the raw intraday data afterwards changes nothing it
    already read."""
    rows = conn.execute(
        "SELECT day FROM activity WHERE person_id = ? AND start_local IS NOT NULL "
        "AND duration_s IS NOT NULL ORDER BY day DESC LIMIT ?",
        (person_id, limite),
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        d = date.fromisoformat(r["day"])
        out.add(d.isoformat())
        out.add((d + timedelta(1)).isoformat())
    return out


def _seed_turno_fc(conn, db, turno_fc, person_id: int, all_days: list[date],
                    excluir_dias: frozenset[str] = frozenset()) -> int:
    """Invented minute-by-minute heart rate so «Tu pulso durante el turno» (BODY-TURNO-FC-1) has
    something to show on the demo server: the person's own flat per-hour background
    (`_curva_dia_ordinario`, the SAME curve `_seed_recuperacion`'s own background fill already
    uses) on every 'off' day with no intraday data yet, so turno_fc's own days-off reference
    (`intradia_horario.usual_hora(solo_descanso=True)`) has enough qualifying days -- and a handful
    of confirmed 'morning' shifts, clearly BELOW that reference for the same hours (Carlos's own
    study 2 hypothesis), on days outside `_dias_actividades_recuperacion`'s reserved set, so at
    least one finding shows regardless of how much of the history `_seed_recuperacion`'s own
    background fill already covers. Called AFTER `_seed_recuperacion` and `_seed_shifts`.

    `excluir_dias` (BODY-ENTRENO-VISTA-SESION-1 ruling 6): the basketball day must end the whole
    seed with NO intraday series at all, so this background fill -- the one write in the whole
    pipeline that would otherwise reach it even though it holds no activity _seed_recuperacion
    would recognise -- skips it outright, same as an `off` day that already had data."""
    ya_con_datos = _dias_con_intradia_fc(conn, person_id)
    reservados = _dias_actividades_recuperacion(conn, person_id, N_ACTIVIDADES_RECUPERACION)

    for d in all_days:
        if d.isoformat() in ya_con_datos or d.isoformat() in excluir_dias or _shift_kind_for(d) != "off":
            continue
        muestras = _curva_dia_ordinario(d)
        db.guardar_intradia(conn, person_id, d.isoformat(), "intradia_fc",
                             {"heartRateValues": [[_muestra_ms(t), v] for t, v in muestras]})
        ya_con_datos.add(d.isoformat())

    start_h, end_h, _crosses = _SHIFT_HOURS["morning"]
    inicio_h, inicio_m = int(start_h[:2]), int(start_h[3:])
    fin_h, fin_m = int(end_h[:2]), int(end_h[3:])
    confirmados = all_days[:-7]
    candidatos = [d for d in confirmados if _shift_kind_for(d) == "morning"
                  and d.isoformat() not in reservados]
    elegidos = candidatos[-N_TURNOS_FC:]
    for d in elegidos:
        inicio = datetime.combine(d, datetime.min.time()) + timedelta(hours=inicio_h, minutes=inicio_m)
        fin = datetime.combine(d, datetime.min.time()) + timedelta(hours=fin_h, minutes=fin_m)
        muestras = [(t, round(max(30.0, v - _TURNO_FC_OFFSET_BPM)) if inicio <= t <= fin else v)
                    for t, v in _curva_dia_ordinario(d)]
        db.guardar_intradia(conn, person_id, d.isoformat(), "intradia_fc",
                             {"heartRateValues": [[_muestra_ms(t), v] for t, v in muestras]})
        ya_con_datos.add(d.isoformat())
    conn.commit()
    turno_fc.recalcular(conn, person_id)
    return len(elegidos)


# BODY-NOCHE-INUSUAL-1: the most recent day's own hrv_last_night/sleep_score, overridden to a
# clearly LOW invented value against this person's own 30-day normal -- two of
# noche_inusual.MODELO's three signals, enough to clear MODELO['minimo_senales'], so the demo's
# Hoy and Dia both show the "unusual night" line. The third signal, sleep_hr_avg, is left alone:
# _seed_sueno_fc only ever gives it N_NOCHES_SUENO_FC nights of history, short of
# baselines.MIN_DAYS, so it can never be judged in this fixture regardless -- an honest gap, not a
# bug this slice needs to fix.
NOCHE_INUSUAL_HRV = 18.0
NOCHE_INUSUAL_SLEEP_SCORE = 12.0

# BODY-PLAN-FASES-5-PULIDO-1-M: both overridden signals above are read against the person's own
# baselines.MIN_DAYS=7 (over UNMARKED days only -- baselines.py's own "a marked day is left out of
# the usual range" rule). _seed_periodo's own 'malestar' and 'lesion' periods (above) sit inside
# this same 30-day lookback, and WHERE they land inside it shifts with whichever real calendar day
# "today" is (the 'lesion' period's own start reaches back to the most recent strength_training
# day, itself a function of the shift cycle's fixed, calendar-ABSOLUTE anchor
# `_CYCLE_ANCHOR_MONDAY`, never of "today" itself). On 2026-11-21, with `--dias 30`, that left only
# 4 of the window's unmarked days holding a sleep_score sample -- below MIN_DAYS -- so
# noche_inusual.evaluar saw only ONE signal off and the card silently dropped. Filled in here,
# AFTER _seed_periodo already placed both periods, so this always backfills against their FINAL
# ranges: whichever unmarked days in the lookback still lack a value for either signal get one,
# until each clears MIN_DAYS with margin -- independent of which real day "today" is.
NOCHE_INUSUAL_MARGEN_DIAS = 3


def _asegurar_baseline_noche_inusual(conn, db, baselines, anchor: date, person_id: int) -> None:
    ventana_inicio = anchor - timedelta(baselines.WINDOW_DAYS)
    ventana_fin = anchor - timedelta(1)
    marcados = db.dias_marcados(conn, person_id, ventana_inicio.isoformat(), ventana_fin.isoformat())
    dias_ventana = [ventana_inicio + timedelta(i) for i in range((ventana_fin - ventana_inicio).days + 1)]
    sin_marcar = [d for d in dias_ventana if d.isoformat() not in marcados]
    minimo = baselines.MIN_DAYS + NOCHE_INUSUAL_MARGEN_DIAS
    for key in ("hrv_last_night", "sleep_score"):
        rows = conn.execute(
            "SELECT day FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = ? "
            "AND day BETWEEN ? AND ?",
            (person_id, key, ventana_inicio.isoformat(), ventana_fin.isoformat())).fetchall()
        con_valor = {r["day"] for r in rows}
        con_valor_sin_marcar = con_valor & {d.isoformat() for d in sin_marcar}
        faltan = minimo - len(con_valor_sin_marcar)
        if faltan <= 0:
            continue
        candidatos = [d for d in sin_marcar if d.isoformat() not in con_valor]
        lo, hi = RANGES[key]
        relleno = round((lo + hi) / 2, 1)
        for d in candidatos[:faltan]:
            db.set_metric(conn, d.isoformat(), key, relleno, "garmin", person_id)


def _seed_noche_inusual(conn, db, baselines, anchor: date, person_id: int) -> None:
    db.set_metric(conn, anchor.isoformat(), "hrv_last_night", NOCHE_INUSUAL_HRV, "garmin", person_id)
    db.set_metric(conn, anchor.isoformat(), "sleep_score", NOCHE_INUSUAL_SLEEP_SCORE, "garmin", person_id)
    _asegurar_baseline_noche_inusual(conn, db, baselines, anchor, person_id)
    conn.commit()


# BODY-NOCHE-SIN-REM-1: the night right before `anchor` (never `anchor` itself -- that night
# already carries _seed_noche_inusual's own overridden hrv_last_night/sleep_score, and keeping the
# two demo flags on two different Dia pages makes each easy to show on its own). Deep+light alone
# already clear captura_sueno.MODELO['rem_minimo_noche_min'] with margin, so the override never
# duplicates that threshold as a second constant here.
NOCHE_SIN_REM_DIAS_ATRAS = 1
NOCHE_SIN_REM_DEEP_MIN = 110.0
NOCHE_SIN_REM_LIGHT_MIN = 200.0
NOCHE_SIN_REM_AWAKE_MIN = 20.0


def _seed_captura_sueno(conn, db, captura_sueno, anchor: date, person_id: int) -> None:
    dia = (anchor - timedelta(NOCHE_SIN_REM_DIAS_ATRAS)).isoformat()
    deep, light, awake = NOCHE_SIN_REM_DEEP_MIN, NOCHE_SIN_REM_LIGHT_MIN, NOCHE_SIN_REM_AWAKE_MIN
    assert deep + light >= captura_sueno.MODELO["rem_minimo_noche_min"]
    db.set_metric(conn, dia, "sleep_deep_min", deep, "garmin", person_id)
    db.set_metric(conn, dia, "sleep_light_min", light, "garmin", person_id)
    db.set_metric(conn, dia, "sleep_rem_min", 0.0, "garmin", person_id)
    db.set_metric(conn, dia, "sleep_awake_min", awake, "garmin", person_id)
    db.set_metric(conn, dia, "sleep_minutes", deep + light + awake, "garmin", person_id)
    conn.commit()


# A handful of the most recent nights, so Dia's own line and the Tendencias "night" card have
# something to show -- not every night in the history.
N_NOCHES_SUENO_FC = 5


def _curva_sueno(inicio: datetime, fin: datetime) -> list[tuple[datetime, float]]:
    """BODY-SUENO-FC-1: one sleep window's own invented heart-rate curve, every two minutes
    (Garmin's own typical intraday spacing) -- settling from _PULSO_HORA's own evening level down
    to a believable overnight low in the first half of the window, then holding there, so the
    robust low (sueno_fc.MODELO['percentil_bajo']) reads a real sustained trough, never a single
    sample."""
    paso = timedelta(minutes=2)
    total_min = (fin - inicio).total_seconds() / 60
    out = []
    t = inicio
    while t <= fin:
        frac = min((t - inicio).total_seconds() / 60 / (total_min / 2 or 1), 1.0) if total_min else 1.0
        valor = 62.0 - 12.0 * frac
        out.append((t, round(valor, 1)))
        t += paso
    return out


def _seed_sueno_fc(conn, db, sueno_fc, person_id: int) -> int:
    """Invented minute-by-minute heart rate across a handful of this person's most recent sleep
    windows, so Dia's own line (BODY-SUENO-FC-1) and the Tendencias "night" card have something to
    show on the demo server -- merged with whatever intradia_fc background the day already holds
    (read back via db.intradia and kept, same accumulate-before-write rule `_seed_recuperacion`'s
    own docstring explains), never erasing it. Called AFTER `_seed_recuperacion` and
    `_seed_turno_fc`, since both of those already wrote intradia_fc for most of the history."""
    rows = conn.execute(
        "SELECT day, start_local, end_local FROM sleep_session WHERE person_id = ? "
        "AND start_local IS NOT NULL AND end_local IS NOT NULL ORDER BY day DESC LIMIT ?",
        (person_id, N_NOCHES_SUENO_FC),
    ).fetchall()
    if not rows:
        return 0
    zona = db.person(conn, person_id)["timezone"]
    ventanas = [(datetime.fromisoformat(r["start_local"]), datetime.fromisoformat(r["end_local"]))
                for r in rows]
    dias_tocados = {dt.date().isoformat() for inicio, fin in ventanas for dt in (inicio, fin)}

    por_dia: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for dia in dias_tocados:
        for ts, v in db.intradia(conn, person_id, dia, "intradia_fc", zona):
            t = datetime.fromisoformat(ts)
            if not any(inicio <= t <= fin for inicio, fin in ventanas):
                por_dia[dia].append((t, v))
    for inicio, fin in ventanas:
        for t, v in _curva_sueno(inicio, fin):
            por_dia[t.date().isoformat()].append((t, v))

    for dia, muestras in por_dia.items():
        db.guardar_intradia(conn, person_id, dia, "intradia_fc",
                             {"heartRateValues": [[_muestra_ms(t), v] for t, v in sorted(muestras)]})
    conn.commit()
    sueno_fc.recalcular(conn, person_id)
    return len(rows)


def _seed_despertar_correccion(conn, db, person_id: int) -> int:
    """BODY-DESPERTAR-REAL-4 ruling 1: one invented correction, on the most recent night this
    person has a Garmin sleep session for -- a plain "got up a little after Garmin's own record"
    demo row, never a real figure. Called AFTER `_seed_sueno_fc`, which is what gives most of this
    history its own sleep_session rows in the first place. BODY-DESPERTAR-REAL-6A-R2 ruling 1's
    own census: a SECOND, earlier night (so the most-recent one above stays the single-kind
    fixture existing tests already read) gets BOTH kinds -- dozed past Garmin's own end (sueno),
    then got up later still (levantado), the exact shape a morning can now hold."""
    row = conn.execute(
        "SELECT day, end_local FROM sleep_session WHERE person_id = ? AND end_local IS NOT NULL "
        "ORDER BY day DESC LIMIT 1", (person_id,),
    ).fetchone()
    if row is None:
        return 0
    fin = datetime.fromisoformat(row["end_local"])
    hora_local = (fin + timedelta(minutes=25)).strftime("%H:%M")
    db.guardar_despertar_correccion(conn, person_id, row["day"], "levantado", hora_local,
                                     "despertar-real-v2", "asistente")
    count = 1

    otra = conn.execute(
        "SELECT day, end_local FROM sleep_session WHERE person_id = ? AND end_local IS NOT NULL "
        "AND day != ? ORDER BY day DESC LIMIT 1", (person_id, row["day"]),
    ).fetchone()
    if otra is not None:
        fin2 = datetime.fromisoformat(otra["end_local"])
        sueno_hora = (fin2 + timedelta(minutes=20)).strftime("%H:%M")
        levantado_hora = (fin2 + timedelta(minutes=55)).strftime("%H:%M")
        db.guardar_despertar_correccion(conn, person_id, otra["day"], "sueno", sueno_hora,
                                         "despertar-real-v2", "persona")
        db.guardar_despertar_correccion(conn, person_id, otra["day"], "levantado", levantado_hora,
                                         "despertar-real-v2", "persona")
        count += 2
    conn.commit()
    return count


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


# BODY-DESPLAZAMIENTO-3 ruling 5: ultimo_dia lands this many days before "today" (anchor), never
# today itself, so the Tendencias freshness line has a real "datos hasta" date to show rather than
# always reading as perfectly current.
DESPLAZAMIENTO_DIAS_ATRAS = 3
_DESPLAZAMIENTO_RANGOS = {
    "vehicle_minutes": (0, 45), "vehicle_trips": (0, 4), "walking_minutes": (0, 40), "cycling_minutes": (0, 30),
}


def _seed_desplazamiento(conn, desplazamiento_import, rng: random.Random, all_days: list[date],
                          anchor: date, person_id: int) -> int:
    """BODY-DESPLAZAMIENTO-3 ruling 5: invented commute figures for the seeded window, some days
    absent, through the REAL importer (body/desplazamiento_import.py) -- so the Tendencias
    commute group, its freshness line and the per-source delete all have something to show on the
    demo server. The LAST day in the window is always present, so `ultimo_dia` is deterministic
    across two runs regardless of which other days the same rng draw happens to skip."""
    ultimo_dia = anchor - timedelta(DESPLAZAMIENTO_DIAS_ATRAS)
    dias_commute = [d for d in all_days if d <= ultimo_dia]
    if not dias_commute:
        return 0
    computed_at = datetime.combine(ultimo_dia, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    filas = [desplazamiento_import.HEADER]
    for d in dias_commute:
        if d != ultimo_dia and rng.random() < 0.25:
            continue
        valores = [round(rng.uniform(*_DESPLAZAMIENTO_RANGOS[k]))
                   for k in ("vehicle_minutes", "vehicle_trips", "walking_minutes", "cycling_minutes")]
        filas.append([d.isoformat(), *valores, computed_at])
    texto = "\n".join(",".join(str(x) for x in fila) for fila in filas) + "\n"
    resultado = desplazamiento_import.importar(conn, person_id, texto)
    conn.commit()
    return resultado["dias"] if resultado["ok"] else 0


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


_COMIDA_NUTRIENTES_DEMO = ("protein_g", "kcal", "carbs_g", "fat_g", "fiber_g")

# BODY-COMIDA-1-M fix 4: each forced single-nutrient entry gets its own invented time -- coffee and
# water stay in the morning slot, but alcohol moves to an evening one (never the morning slot a
# coffee would plausibly sit in).
_COMIDA_FORZADA_MINUTOS = {
    "caffeine_mg": _COMIDA_MINUTOS[0],
    "water_ml": _COMIDA_MINUTOS[0],
    "alcohol_g": (20 * 60, 21 * 60),
}


def _seed_ingesta(conn, db, rng: random.Random, all_days: list[date], person_id: int) -> tuple[int, int]:
    """3-4 invented meals most days (BODY-COMIDA-1), each its own `comida` row grouping several
    invented nutrient amounts WITH a time (BODY-AYUNO-1), one slot per `_COMIDA_MINUTOS` entry --
    one writer (db.crear_comida) sums every nutrient into that day's own total, so Tendencias
    shows it and Hoy/Día's eating-window card and meal list have real meals to show. Three more
    single-nutrient meals, each on a FIXED day index (first/middle/last of the run), guarantee
    caffeine_mg, water_ml and alcohol_g each appear at least once regardless of the per-day 0.9
    coin flip below (tests/test_demo.py's catalogue-coverage guard), even on a short --dias run.
    Invented amounts and times only, never a real figure; no descripcion, since the demo seeds no
    words."""
    rows = 0
    comidas = 0
    forzado_indices = {0: "caffeine_mg", len(all_days) // 2: "water_ml", len(all_days) - 1: "alcohol_g"}
    for i, d in enumerate(all_days):
        if rng.random() < 0.9:
            for lo, hi in _COMIDA_MINUTOS[:rng.randint(3, 4)]:
                minuto = rng.randint(lo, hi - 1)
                ts_local = f"{d.isoformat()}T{minuto // 60:02d}:{minuto % 60:02d}:00"
                cantidades = [{"metric_key": k, "amount": _value(rng, k, 0.0), "estimado": True}
                              for k in _COMIDA_NUTRIENTES_DEMO]
                db.crear_comida(conn, person_id, d.isoformat(), cantidades, ts_local=ts_local)
                comidas += 1
                rows += len(cantidades)
        forzada = forzado_indices.get(i)
        if forzada:
            lo, hi = _COMIDA_FORZADA_MINUTOS[forzada]
            minuto = rng.randint(lo, hi - 1)
            ts_local = f"{d.isoformat()}T{minuto // 60:02d}:{minuto % 60:02d}:00"
            db.crear_comida(conn, person_id, d.isoformat(),
                             [{"metric_key": forzada, "amount": _value(rng, forzada, 0.0), "estimado": True}],
                             ts_local=ts_local)
            comidas += 1
            rows += 1
    conn.commit()
    return rows, comidas


def _seed_nutriente_objetivo(conn, db, all_days: list[date], person_id: int) -> int:
    """Two invented daily targets (BODY-NUTRIENTE-OBJETIVO-1 ruling 8), live for the whole seeded
    run: protein_g per_kg 1.6 al_menos (a below-target line against this demo's own seeded protein
    and weight) and caffeine_mg absolute 400 como_maximo (a within-limit line). Invented amounts
    only, never a real figure."""
    start_day = all_days[0].isoformat()
    with conn:
        db.set_nutrient_target(conn, person_id, "protein_g", start_day, "per_kg", 1.6, "al_menos")
        db.set_nutrient_target(conn, person_id, "caffeine_mg", start_day, "absolute", 400.0, "como_maximo")
    return 2


def _seed_periodo(conn, db, all_days: list[date], person_id: int, idioma: str) -> int:
    """Two invented periods: a closed 'malestar' stretch partway through the seeded history
    (BODY-PERIODO-1), so screens and analyses that leave marked days out have something to exclude
    in the demo, and an OPEN 'lesion' period STARTING a few days before today (BODY-PERIODO-
    PANTALLAS-1-B), as a person would actually mark one -- "unwell since the 5th", not "unwell
    exactly as of this instant". dias_marcados() no longer re-reads the timezone per call while an
    open period overlaps a lookback (that cost is gone, not hidden -- db.py's own
    _periodos_de_persona), so starting it inside every baseline's 30-day lookback costs nothing
    extra here.

    BODY-LESION-ZONA-1 ruling 6: the 'lesion' period names _LESION_ZONA_DEMO. Its start reaches
    back far enough to cover the most recent strength_training activity _seed_days already made
    (widening past the usual 3 days when that activity is older, capped at _LESION_MAX_DIAS_ATRAS
    so the period itself never outgrows what stress_avg's own baseline window can afford to lose),
    and that one activity's first set is corrected to the same categoria -- so a registered
    training read shows ruling 2's "lighter on purpose" line, not only the future plan and Hoy's
    hatch."""
    start = all_days[len(all_days) // 2]
    end = start + timedelta(2)
    db.crear_periodo(conn, person_id, "malestar", start.isoformat(), end.isoformat())
    hoy = all_days[-1]
    inicio_lesion = hoy - timedelta(3)
    fuerza = conn.execute(
        "SELECT source, source_id, day FROM activity WHERE person_id = ? AND type = 'strength_training' "
        "AND day < ? ORDER BY day DESC LIMIT 1",
        (person_id, hoy.isoformat()),
    ).fetchone()
    if fuerza is not None:
        dia_fuerza = date.fromisoformat(fuerza["day"])
        if dia_fuerza < inicio_lesion:
            inicio_lesion = max(dia_fuerza, hoy - timedelta(_LESION_MAX_DIAS_ATRAS))
        db.correct_exercise_set(conn, person_id, fuerza["source"], fuerza["source_id"], 1,
                                 category=_LESION_ZONA_DEMO, exercise=_LESION_SET_EJERCICIO[idioma],
                                 reps=8, weight_kg=20.0)
        db.registrar_dolor_entreno(
            conn, person_id, fuerza["day"], fuerza["source"], fuerza["source_id"],
            [{"ejercicio": _LESION_SET_EJERCICIO[idioma], "zona": _LESION_ZONA_DEMO,
              "dolor_durante": 3, "dolor_despues": 2, "movimiento": _LESION_DOLOR_MOVIMIENTO[idioma]}])
        db.registrar_dolor_manana(conn, person_id, fuerza["day"], _LESION_ZONA_DEMO, 1)
    db.crear_periodo(conn, person_id, "lesion", inicio_lesion.isoformat(), None, zonas=[_LESION_ZONA_DEMO])
    conn.commit()
    return 2


_MOMENTOS_CHECKIN_DEMO = ("despertar", "post_turno", "otro")
_NOTA_CHECKIN_DEMO = "turno largo, dormí poco"


def _seed_checkins(conn, db, rng: random.Random, all_days: list[date], person_id: int) -> int:
    """A handful of invented check-ins (BODY-CHECKIN-1): roughly three days out of five, a quick
    self-report -- mood, energy, stress_felt, each 1-5. The FIRST seeded day always gives all three
    numbers AND a note, and the SECOND gives numbers with no note (tests/test_demo.py's catalogue-
    coverage guard, plus the slice's own "with and without a note"), so even a short --dias run
    shows both shapes on Día."""
    rows = 0
    for i, d in enumerate(all_days):
        if i > 1 and rng.random() >= 0.6:
            continue
        momento = _MOMENTOS_CHECKIN_DEMO[i % len(_MOMENTOS_CHECKIN_DEMO)]
        nota = _NOTA_CHECKIN_DEMO if (i == 0 or (i > 1 and rng.random() < 0.3)) else None
        db.crear_checkin(conn, person_id, d.isoformat(), momento, ts_local=f"{d.isoformat()}T07:00:00",
                          mood=rng.randint(2, 5), energy=rng.randint(2, 5), stress_felt=rng.randint(1, 4),
                          nota=nota)
        rows += 1
    conn.commit()
    return rows


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
                            idioma: str, practica_pasos_id: int) -> tuple[int, int, int]:
    """The demo person's three metric goals, three habits, their weekly checks and the habits'
    checks -- returns (goal weekly checks, habit checks, invented strength sessions)."""
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

    # "hora" (BODY-MOCK-R2-HOY-1 ruling 8, "a timed habit"): additive inside the cadence JSON blob,
    # never read by goals.habit_due_today/habit_progress -- a habit with no "hora" at all behaves
    # exactly as before.
    habit_id = goals.add_habit(conn, gid_sleep, _HABIT_TITLE[idioma],
                                {"per": "week", "times": 3, "context": "before_night", "hora": "21:30"})

    # BODY-TENDENCIAS-RACHA-GRAFICO-1 ruling 4: the demo person's own floor pick for the
    # strength-session study -- 3 clean weeks of 3 sessions each, ending two weeks before the
    # current one, so the study shows a real "longest run" but no still-open "current run"
    # (racha_semanal treats the current and the immediately-previous week as still open).
    habit_fuerza_id = goals.add_habit(conn, gid_rhr, _HABIT_FUERZA_TITLE[idioma], {"per": "week", "times": 3})
    db.set_racha_piso(conn, person_id, habit_fuerza_id)
    goals.add_habit(conn, gid_steps, _HABIT_PASOS_TITLE[idioma], {"per": "week", "times": 5},
                     practica_id=practica_pasos_id)
    racha_sesiones = 0
    for semanas_atras in (2, 3, 4):
        lunes = anchor_monday - timedelta(7 * semanas_atras)
        for offset in (0, 2, 4):
            dia = lunes + timedelta(offset)
            conn.execute(
                "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
                "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, 'strength_training', "
                "?, 2400, NULL, 115, 140, 240, 'garmin')",
                (person_id, f"demo-racha-{dia.isoformat()}-1", dia.isoformat(),
                 f"{dia.isoformat()}T18:00:00", _ENTRENO_WATCH_TITULO[idioma]),
            )
            racha_sesiones += 1
    conn.commit()

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

    return checks, habit_checks, racha_sesiones


# BODY-PLAN-PANTALLA-1 ruling 6: one invented plan (a horizon milestone, its own child goals,
# habits, rules, a week title/note and two checkpoints) -- generic, never a city, sport, employer,
# shift code or real figure, the same "template, not one person" contract every other demo fixture
# here already follows.
_PLAN_TEXTOS = {
    "es": {
        "horizonte": "Mejorar el sueño y la fuerza",
        "hijo_hrv": "Mejorar mi variabilidad del pulso",
        "hijo_estres": "Bajar mi estrés",
        "habito_caminar": "Caminar después del almuerzo",
        "habito_revisar": "Revisar el plan los domingos",
        "regla_siempre": "Mantén una hora de dormir estable, incluso los días libres.",
        "regla_turno": "Antes de un turno de noche, acuéstate temprano.",
        "regla_tras_noche": "El día después de una noche, duerme al menos 7 horas.",
        "regla_periodo": "Mientras dure la lesión, evita sesiones de fuerza.",
        "fase_base": "Base",
        "fase_construccion": "Construcción",
        "fase_pico": "Pico",
        "semana_titulo": "Semana de referencia",
        "semana_nota": "Ajusta el entrenamiento según cómo te sientas.",
        "checkpoint_pasado": "Revisión de mitad de plan",
        "checkpoint_futuro": "Revisión final",
        "sesion_fuerza": "Entrenamiento de fuerza",
        "sesion_caminata": "Caminata",
        "experimento": "Dormir media hora más temprano",
    },
    "en": {
        "horizonte": "Improve sleep and strength",
        "hijo_hrv": "Improve my heart-rate variability",
        "hijo_estres": "Lower my stress",
        "habito_caminar": "Walk after lunch",
        "habito_revisar": "Review the plan on Sundays",
        "regla_siempre": "Keep a steady bedtime, even on days off.",
        "regla_turno": "Before a night shift, go to bed early.",
        "regla_tras_noche": "The day after a night, sleep at least 7 hours.",
        "regla_periodo": "While the injury lasts, skip strength sessions.",
        "fase_base": "Base",
        "fase_construccion": "Build",
        "fase_pico": "Peak",
        "semana_titulo": "Reference week",
        "semana_nota": "Adjust training to how you feel.",
        "checkpoint_pasado": "Mid-plan review",
        "checkpoint_futuro": "Final review",
        "sesion_fuerza": "Strength training",
        "sesion_caminata": "Walk",
        "experimento": "Go to sleep half an hour earlier",
    },
}
# horizonte._minutos_despues_mediodia's own convention: 23:00 -> ((23-12) % 24) * 60 = 660.
_PLAN_HORA_ACOSTARSE_VALOR = 660.0

# BODY-HISTORIA-VIDA-1 ruling 8: two invented practices on the demo person, one `actividad` and
# one `metrica_umbral`, so the demo home shows the shape -- titles only, no computed state
# (practicas.estado's own job on read, BODY-HISTORIA-VIDA-2's screen).
_PRACTICA_TITULOS = {
    "es": {"correr": "Correr", "pasos": "Caminar bastante"},
    "en": {"correr": "Running", "pasos": "Walk a lot"},
}


def _seed_practicas(conn, practicas, person_id: int, idioma: str) -> tuple[int, int]:
    """Returns (count, pasos_id) -- `pasos_id` is BODY-HISTORIA-VIDA-2 ruling 5's own link target,
    the "Caminar bastante" practice that the demo's steps-goal habit cites as evidence."""
    txt = _PRACTICA_TITULOS[idioma]
    practicas.crear(conn, person_id, txt["correr"], "actividad", {"tipos": ["running"]})
    pasos_id = practicas.crear(conn, person_id, txt["pasos"], "metrica_umbral",
                                {"metric_key": "steps", "op": ">=", "valor": 8000})
    conn.commit()
    return 2, pasos_id


def _seed_plan_horizonte(conn, db, goals, person_id: int, anchor: date, anchor_monday: date,
                          idioma: str) -> dict:
    """Ruling 6: a horizon from 3 weeks ago to 5 weeks ahead -- two child metric goals, two habits
    on the horizon itself, four rules (one per contexto kind the demo's shifts/periods allow:
    siempre, turno, tras_noche, periodo -- 'siempre' text-only, chequeo=None), a weekly line set
    for every week (strength sessions, cardio sessions, a bedtime, sleep minutes), one corrected
    line (db.corregir_meta_semanal, so the history has a replaced row), two checkpoints (one past,
    one ahead), two planned sessions this week linked to the horizon, and one linked experiment --
    all through db.crear_plan_horizonte / db.corregir_meta_semanal / goals.activate /
    db.crear_experimento, never a hand-written INSERT for any of those. The `plan` table's own two
    sessions are the one exception: it has no db.py writer of its own, the same gap
    `_seed_plan_hoy` above already lives with."""
    txt = _PLAN_TEXTOS[idioma]
    start_monday = anchor_monday - timedelta(weeks=3)
    end_monday = anchor_monday + timedelta(weeks=5)
    start_day, deadline = start_monday.isoformat(), (end_monday + timedelta(6)).isoformat()

    semanas_medidas = []
    m = start_monday
    while m <= end_monday:
        ws = m.isoformat()
        semanas_medidas += [
            {"week_start": ws, "medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 3.0},
            {"week_start": ws, "medida": "sesiones_cardio", "sentido": "al_menos", "valor": 2.0},
            {"week_start": ws, "medida": "hora_acostarse", "sentido": "antes_de",
             "valor": _PLAN_HORA_ACOSTARSE_VALOR},
            {"week_start": ws, "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420.0},
        ]
        m += timedelta(7)

    plan = {
        "titulo": txt["horizonte"], "start_day": start_day, "deadline": deadline,
        "metas_finales": [
            {"titulo": txt["hijo_hrv"], "metric_key": "hrv_last_night", "direction": "raise", "target_value": 70.0},
            {"titulo": txt["hijo_estres"], "metric_key": "stress_avg", "direction": "lower", "target_value": 25.0},
        ],
        "habitos": [
            {"titulo": txt["habito_caminar"], "cadencia": {"per": "week", "times": 3}},
            {"titulo": txt["habito_revisar"], "cadencia": {"per": "week", "times": 1}},
        ],
        "fases": [
            {"titulo": txt["fase_base"], "start_day": start_day,
             "end_day": (start_monday + timedelta(days=20)).isoformat()},
            {"titulo": txt["fase_construccion"], "start_day": (start_monday + timedelta(days=21)).isoformat(),
             "end_day": (start_monday + timedelta(days=41)).isoformat()},
            {"titulo": txt["fase_pico"], "start_day": (start_monday + timedelta(days=42)).isoformat(),
             "end_day": deadline},
        ],
        "reglas": [
            {"contexto": "siempre", "contexto_valor": None, "texto": txt["regla_siempre"], "chequeo": None,
             "fase_index": 0},
            {"contexto": "turno", "contexto_valor": "night", "texto": txt["regla_turno"],
             "chequeo": {"tipo": "acostarse_antes", "hora": "21:30"}},
            {"contexto": "tras_noche", "contexto_valor": "1", "texto": txt["regla_tras_noche"],
             "chequeo": {"tipo": "sueno_minimo", "minutos": 420}},
            {"contexto": "periodo", "contexto_valor": "lesion", "texto": txt["regla_periodo"],
             "chequeo": {"tipo": "sin_sesion", "clase": "fuerza"}},
        ],
        "semanas": [{"week_start": anchor_monday.isoformat(), "titulo": txt["semana_titulo"],
                     "nota": txt["semana_nota"]}],
        "checkpoints": [
            {"day": (anchor - timedelta(10)).isoformat(), "titulo": txt["checkpoint_pasado"],
             "criterios": [{"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 3.0, "ventana": "semana"}]},
            {"day": (anchor + timedelta(20)).isoformat(), "titulo": txt["checkpoint_futuro"],
             "criterios": [{"medida": "sueno_minutos", "sentido": "al_menos", "valor": 420.0, "ventana": "semana"}]},
        ],
        "metas_semanales": semanas_medidas,
    }
    horizonte_id = db.crear_plan_horizonte(conn, person_id, plan)
    conn.commit()

    corregido = conn.execute(
        "SELECT id FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? AND week_start = ? "
        "AND medida = 'sesiones_fuerza' AND vigente = 1",
        (horizonte_id, (anchor_monday - timedelta(weeks=2)).isoformat()),
    ).fetchone()
    db.corregir_meta_semanal(conn, person_id, corregido["id"], 4.0)

    hijo_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM goal WHERE horizonte_goal_id = ? ORDER BY id", (horizonte_id,))]
    goals.activate(conn, horizonte_id)
    for hid in hijo_ids:
        goals.activate(conn, hid)

    martes, jueves = anchor_monday + timedelta(1), anchor_monday + timedelta(3)
    conn.execute(
        "INSERT INTO plan (person_id, goal_id, day, time, title, status) VALUES (?, ?, ?, ?, ?, 'done')",
        (person_id, horizonte_id, martes.isoformat(), "18:00", txt["sesion_fuerza"]))
    conn.execute(
        "INSERT INTO plan (person_id, goal_id, day, time, title, status) VALUES (?, ?, ?, ?, ?, 'accepted')",
        (person_id, horizonte_id, jueves.isoformat(), "07:00", txt["sesion_caminata"]))
    conn.commit()

    experimento_id = db.crear_experimento(
        conn, person_id, txt["experimento"], ["sleep_minutes"], {"tipo": "dia"},
        {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False},
        fecha_inicio=start_day, goal_id=horizonte_id)
    conn.commit()

    return {"goal_id": horizonte_id, "hijos": len(hijo_ids), "metas_semanales": len(semanas_medidas),
            "checkpoints": 2, "experimento_id": experimento_id}


_PLAN_HOY_HORA = "15:30"  # BODY-MOCK-R2-HOY-1 ruling 8: "a planned session with a time".


def _seed_plan_hoy(conn, anchor: date, person_id: int, idioma: str) -> int:
    """BODY-CUERPO-VISTA-1-M ruling 4: today's own accepted plan, categorised, so the demo Hoy is
    lit rather than reading "no session planned" (BODY-CUERPO-VISTA-1's own gap). BODY-MOCK-R2-
    HOY-1 ruling 8 adds a time, so "The day ahead" shows a real timed session, not an untimed one."""
    conn.execute(
        "INSERT INTO plan (person_id, day, time, title, status, ejercicios_json) "
        "VALUES (?, ?, ?, ?, 'accepted', ?)",
        (person_id, anchor.isoformat(), _PLAN_HOY_HORA, _PLAN_HOY_TITLE[idioma],
         json.dumps(_PLAN_HOY_EJERCICIOS[idioma], ensure_ascii=False)),
    )
    conn.commit()
    return 1


# BODY-MOCK-R2-HOY-1 ruling 8: "a shift today" -- BODY-DEMO-TURNOS-1 always leaves the last 7 days
# (today included) in a PROPOSED batch, never confirmed, so without this Hoy's "day ahead" card
# would have no shift to show on any given run. A confirmed 'day' shift, regardless of where the
# historical cycle would otherwise land today, so the demo is lit the same way every run.
_TURNO_HOY_KIND = "day"


def _seed_turno_hoy_confirmado(conn, anchor: date, person_id: int) -> None:
    start_h, end_h, _crosses = _SHIFT_HOURS[_TURNO_HOY_KIND]
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'manual')",
        (person_id, anchor.isoformat(), _TURNO_HOY_KIND, _SHIFT_CODE[_TURNO_HOY_KIND],
         f"{anchor.isoformat()}T{start_h}:00", f"{anchor.isoformat()}T{end_h}:00"),
    )
    conn.commit()


# BODY-MOCK-R2-HOY-1 ruling 8: "a night block starting in 3 days" -- a FUTURE confirmed night
# block, inserted directly (never through the past-only `_shift_kind_for` cycle, which has no
# future days at all), so Hoy's own "Night week ahead" card always has one to show, whichever real
# calendar date `python -m body.demo` is actually run on.
NOCHE_FUTURA_INICIO_DIAS = 3
NOCHE_FUTURA_LARGO = 2


def _seed_bloque_noche_futuro(conn, anchor: date, person_id: int) -> None:
    inicio = anchor + timedelta(NOCHE_FUTURA_INICIO_DIAS)
    for i in range(NOCHE_FUTURA_LARGO):
        d = inicio + timedelta(i)
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source) "
            "VALUES (?, ?, 'night', 'N', ?, ?, 'manual')",
            (person_id, d.isoformat(), f"{d.isoformat()}T22:00:00", f"{(d + timedelta(1)).isoformat()}T06:00:00"),
        )
    conn.commit()


def _seed_lugares(conn, person_id: int, anchor: date) -> int:
    """Invented demo trips, written directly (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4) -- the demo
    never touches Lugares or the network: one past trip of 2+ days, one past single day, and one
    planned trip ahead of `anchor`, plus a lugares_sync row recording that sync as if it had just
    succeeded."""
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    viajes = (
        ("demo-viaje-1", anchor - timedelta(20), anchor - timedelta(16)),
        ("demo-viaje-2", anchor - timedelta(8), anchor - timedelta(8)),
        ("demo-viaje-3", anchor + timedelta(10), anchor + timedelta(13)),
    )
    for ref, inicio, fin in viajes:
        conn.execute(
            "INSERT INTO viaje_lugares (person_id, ref, starts_at, ends_at, fetched_at) VALUES "
            "(?, ?, ?, ?, ?)",
            (person_id, ref, inicio.isoformat(), fin.isoformat(), ahora),
        )
    conn.execute(
        "INSERT INTO lugares_sync (person_id, intento_at, ok_at, motivo) VALUES (?, ?, ?, NULL)",
        (person_id, ahora, ahora),
    )
    conn.commit()
    return len(viajes)


def _seed_altitud(conn, person_id: int, anchor: date) -> int:
    """Invented demo altitude (BODY-MINUTO-ANALISIS-6-ALTITUD, ORCH-CARLOS-DELEGATES-TIER-1-1,
    second stage of BODY-MINUTO-ANALISIS-4-INV): one open home row, and one past stay LOWER than
    home over the SAME dates as _seed_lugares' own demo-viaje-1 just above -- so buscar_noches'
    own `altitud_cambio` grouping (ruling 3) has a real descent to show, not an invented value
    standing in for one. Metres and dates only, written directly (never through the network, like
    _seed_lugares above) -- no place name, no coordinates."""
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    home_start = (anchor - timedelta(3650)).isoformat()
    conn.execute(
        "INSERT INTO altitud_estancia (person_id, es_casa, start_day, end_day, altitud_m, fuente, "
        "viaje_ref, creado_en) VALUES (?, 1, ?, NULL, 2600, 'manual', NULL, ?)",
        (person_id, home_start, ahora),
    )
    conn.execute(
        "INSERT INTO altitud_estancia (person_id, es_casa, start_day, end_day, altitud_m, fuente, "
        "viaje_ref, creado_en) VALUES (?, 0, ?, ?, 300, 'manual', 'demo-viaje-1', ?)",
        (person_id, (anchor - timedelta(20)).isoformat(), (anchor - timedelta(16)).isoformat(), ahora),
    )
    conn.commit()
    return 2


def _seed_household(conn, db, goals, enlaces, compartir, comida_compartida, rng: random.Random,
                     all_days: list[date], anchor: date,
                     first_monday: date, anchor_monday: date, person_id: int,
                     idioma: str) -> tuple[int, int, int, int]:
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

    # BODY-COMIDA-COMPARTIDA-1 ruling 7: both have opted in, and one meal each cooked for the other
    # sits pending -- never applied, so the demo shows the card an eater taps, not an already-done
    # action.
    compartir.activar_recibir_comidas(conn, ana_id)
    compartir.activar_recibir_comidas(conn, person_id)
    meal_day = anchor.isoformat()
    comida_compartida.compartir_comida(
        conn, person_id, day=meal_day, descripcion=_COMIDA_DEMO_PERSON[idioma],
        comensales=[{"person_id": person_id, "nutrientes": {"protein_g": 35.0, "kcal": 520.0}},
                    {"person_id": ana_id, "nutrientes": {"protein_g": 18.0, "kcal": 260.0}}],
        origen="demo", origen_ref="demo-persona-ana")
    comida_compartida.compartir_comida(
        conn, ana_id, day=meal_day, descripcion=_COMIDA_DEMO_ANA[idioma],
        comensales=[{"person_id": ana_id, "nutrientes": {"protein_g": 22.0, "kcal": 310.0}},
                    {"person_id": person_id, "nutrientes": {"protein_g": 14.0, "kcal": 190.0}}],
        origen="demo", origen_ref="demo-ana-persona")

    hogar_deadline = (anchor + timedelta(days=84)).isoformat()
    gid_hogar = goals.propose(conn, ana_id, _HOGAR_GOAL_TITLE[idioma], start_day, area="adventure",
                              kind="milestone", deadline=hogar_deadline, hogar_id=_HOGAR_ID,
                              visibility="household")
    goals.activate(conn, gid_hogar)
    # DESIGN rule 17 (BODY-HOGAR-ENLACE-1): the adventure goal links to its trip in Lugares -- an
    # invented demo id, never a real one.
    enlaces.enlazar(conn, gid_hogar, "lugares", "4821")
    # "hora" (BODY-MOCK-R2-HOY-1 ruling 8, "a timed habit"): this household habit is the one that
    # actually falls due on the demo's own anchor day (sleep_sid's "before_night" habit is not, on
    # most anchor days) -- additive inside the cadence JSON blob, same as sleep_sid's own habit.
    habit_both_id = goals.add_habit(conn, gid_hogar, _HOGAR_HABIT_BOTH[idioma],
                                    {"per": "week", "times": 2, "hora": "18:00"})
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

    comidas_compartidas_pendientes = (len(comida_compartida.pendientes(conn, person_id))
                                       + len(comida_compartida.pendientes(conn, ana_id)))
    return len(ana_days), hogar_checks, hogar_habit_checks, comidas_compartidas_pendientes


def _seed_today_stress(conn, db, baselines, anchor: date, person_id: int) -> int:
    """BODY-PLAN-MODULO-1-R: `_seed_days` samples `anchor`'s stress_avg the same way as any other
    day, so whether it lands inside the person's own 25th-75th-percentile baseline depends on which
    real date `anchor` happens to be -- the Hoy tile's partial-day stress tile must read the usual
    band whichever day the demo runs on (the demo is the reviewer's own surface). Overwritten here
    to the midpoint of the SAME baseline body/rutas/hoy.py's `_nivel_tile` compares against (the
    30 days before `anchor`, excluding `anchor` itself) -- never a hardcoded figure. Returns 1 when
    this added a reading that `_seed_days` had rolled absent, else 0, so the printed count stays
    honest."""
    b = baselines.baseline(conn, "stress_avg", anchor.isoformat(), person_id=person_id)
    if b is None:
        return 0
    tenia = db.day_values(conn, anchor.isoformat(), person_id).get("stress_avg") is not None
    db.set_metric(conn, anchor.isoformat(), "stress_avg", round((b.low + b.high) / 2, 1), "garmin", person_id)
    return 0 if tenia else 1


def _seed(conn, *, dias: int, idioma: str, today: date | None = None) -> dict[str, int]:
    from . import (baselines, captura_sueno, comida_compartida, compartir, db, desplazamiento_import, enlaces,
                   goals, practicas, recuperacion, shift_import, sueno_fc, turno_fc, turnos_costo)

    person_id = _seed_person(conn, db, idioma)

    rng = random.Random(SEED)
    anchor = today if today is not None else date.today()
    all_days = [anchor - timedelta(dias - 1 - i) for i in range(dias)]
    first_monday, anchor_monday = fechas.lunes_de(all_days[0]), fechas.lunes_de(anchor)

    metric_rows, activity_rows = _seed_days(conn, db, rng, all_days, person_id, idioma)
    dia_fuerza, dia_basket = _seed_entreno_vista_sesion_actividades(conn, anchor, person_id, idioma)
    _seed_entreno_vinculado(conn, db, person_id, idioma)
    metric_rows += _seed_self_metrics(conn, db, rng, all_days, person_id)
    metric_rows += _seed_soreness(conn, db, rng, all_days, person_id)
    desplazamiento_dias = _seed_desplazamiento(conn, desplazamiento_import, rng, all_days, anchor, person_id)
    entradas_ingesta, comidas_ingesta = _seed_ingesta(conn, db, rng, all_days, person_id)
    objetivos_nutriente = _seed_nutriente_objetivo(conn, db, all_days, person_id)
    periodos = _seed_periodo(conn, db, all_days, person_id, idioma)
    # BODY-ENTRENO-VISTA-SESION-1 ruling 6: AFTER _seed_periodo, not before -- see this function's
    # own docstring for the rowid-ordering reason.
    _seed_entreno_vista_sesion_detalle(conn, db, dia_fuerza, person_id, idioma)
    # BODY-PLAN-MODULO-1-R: AFTER _seed_periodo, not before -- baselines.baseline() excludes
    # marked days (BODY-PERIODO-1 ruling 3), so the band this picks `anchor`'s own stress_avg to
    # fall inside must be read once every period that can exclude a day is already seeded, or it
    # can land inside a band the live route recomputes differently once those periods are in.
    metric_rows += _seed_today_stress(conn, db, baselines, anchor, person_id)
    checkins = _seed_checkins(conn, db, rng, all_days, person_id)
    # BODY-RECUPERACION-1-M: AFTER _seed_periodo, not before -- recuperacion.recalcular() checks
    # db.dias_marcados() per activity at computation time, so an activity on a day inside a marked
    # period must only be recomputed once that period's row already exists, or it is scored by its
    # invented curve instead of read honestly as "marcado".
    recuperacion_rows = _seed_recuperacion(conn, db, recuperacion, person_id,
                                            excluir_dias=frozenset({dia_fuerza, dia_basket}))

    confirmed_id, proposed_id, night_weeks, night_blocks = _seed_shifts(
        conn, shift_import, turnos_costo, person_id, all_days)
    _seed_turno_hoy_confirmado(conn, anchor, person_id)
    _seed_bloque_noche_futuro(conn, anchor, person_id)
    turno_fc_turnos = _seed_turno_fc(conn, db, turno_fc, person_id, all_days,
                                      excluir_dias=frozenset({dia_basket}))
    sueno_fc_noches = _seed_sueno_fc(conn, db, sueno_fc, person_id)
    despertar_correcciones = _seed_despertar_correccion(conn, db, person_id)
    _seed_noche_inusual(conn, db, baselines, anchor, person_id)
    _seed_captura_sueno(conn, db, captura_sueno, anchor, person_id)
    planes_hoy = _seed_plan_hoy(conn, anchor, person_id, idioma)

    practicas_count, practica_pasos_id = _seed_practicas(conn, practicas, person_id, idioma)
    viajes_lugares = _seed_lugares(conn, person_id, anchor)
    altitudes = _seed_altitud(conn, person_id, anchor)

    checks, habit_checks, racha_sesiones = _seed_goals_and_habits(
        conn, db, goals, rng, all_days, anchor, first_monday, anchor_monday, person_id, idioma,
        practica_pasos_id)

    plan_horizonte = _seed_plan_horizonte(conn, db, goals, person_id, anchor, anchor_monday, idioma)

    ana_metric_rows, hogar_checks, hogar_habit_checks, comidas_compartidas_pendientes = _seed_household(
        conn, db, goals, enlaces, compartir, comida_compartida, rng, all_days, anchor, first_monday,
        anchor_monday, person_id, idioma)
    metric_rows += ana_metric_rows

    return {
        "personas": 2, "dias_sembrados": dias, "valores_diarios": metric_rows, "actividades": activity_rows,
        "lotes_turnos_confirmados": 1 if confirmed_id is not None else 0,
        "lotes_turnos_propuestos": 1 if proposed_id is not None else 0,
        "semanas_nocturnas": night_weeks, "bloques_turno_noche": night_blocks,
        "metas_activas": 4 + 1 + plan_horizonte["hijos"], "habitos": 4 + 2 + 1,
        "plan_horizonte_metas_semanales": plan_horizonte["metas_semanales"],
        "plan_horizonte_checkpoints": plan_horizonte["checkpoints"],
        "verificaciones_semanales": checks + hogar_checks, "chequeos_habito": habit_checks + hogar_habit_checks,
        "racha_fuerza_sesiones": racha_sesiones,
        "entradas_ingesta": entradas_ingesta, "comidas": comidas_ingesta,
        "metas_nutriente": objetivos_nutriente, "periodos": periodos,
        "checkins": checkins,
        "recuperacion_actividades": recuperacion_rows,
        "turno_fc_turnos": turno_fc_turnos, "sueno_fc_noches": sueno_fc_noches,
        "despertar_correcciones": despertar_correcciones,
        "planes_hoy": planes_hoy, "desplazamiento_dias": desplazamiento_dias,
        "comidas_compartidas_pendientes": comidas_compartidas_pendientes,
        "practicas": practicas_count,
        "viajes_lugares": viajes_lugares,
        "altitudes": altitudes,
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
