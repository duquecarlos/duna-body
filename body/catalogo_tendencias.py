"""The Trends screen's catalogue (BODY-AGENTE-TENDENCIAS-CATALOGO-1): TREND_KEYS, TREND_GROUPS and
the derived TODAS, the one source both the Trends screen (via body/rutas/comun.py, which imports
these same names) and the assistant's leer_tendencias (body/agente/herramientas.py) read -- so a
metric added to this catalogue reaches both with no second list to keep in step. Imports only
body.db; never a route module or an agent module, so neither reader creates an import cycle.
"""
from __future__ import annotations

from . import db

# BODY-WEB-ESTRUCTURA-7: TREND_KEYS and TREND_GROUPS live here, not in Tendencias (body/rutas/
# tendencias.py), because Día (body/rutas/hoy.py's _tendencias_href) and Turnos (body/rutas/
# turnos.py's _turnos_filas, via comun._es_extra_metric) both read them too -- two-or-more-domains
# puts a name in comun.py (ESTRUCTURA-4's own rule). ESTRUCTURA-6 could not move TREND_GROUPS here
# outright because tests/test_tendencias_todas.py, outside that slice's own `touches:`, read
# `web.TREND_GROUPS` directly; this slice's spec grants every test module, so there is no reader
# left behind this time.
TREND_KEYS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake", "steps",
              "vo2max", "vo2max_cycling", "fitness_age"]

# "Ver todas las medidas" (DESIGN rule 8): the rest of the catalogue, grouped by plain-word topic. The single
# source every reader -- the Tendencias page, tests/test_tendencias_todas.py and Turnos' own cost rows --
# reads from. sleep_need_min (Garmin's own target) is deliberately never shown here.
TREND_GROUPS = [
    ("sleep", ["sleep_score", "sleep_deep_min", "sleep_rem_min", "sleep_light_min", "sleep_awake_min"]),
    # BODY-SUENO-RESPIRACION-VISTA-1: blood oxygen and breathing while asleep, a loose proxy only --
    # low/high stay Día-only (part of the one line there), the trend is the avg alone, same honest
    # caveat as Día's (BODY-ENTRENO-1-INV Q6, DESIGN rule 12: never labelled snoring or apnea).
    # BODY-SUENO-FC-1: the sleep-window heart rate itself, same group -- origen stays 'garmin'
    # (db.METRIC_DEFS), so the per-origin monthly floor (body/rutas/tendencias.py's own
    # GARMIN_MIN_MONTH_POINTS) applies to these two exactly like every other Garmin-origin key
    # here, never the looser self-measured floor.
    ("night", ["sleep_spo2_avg", "sleep_respiration_avg", "sleep_hr_avg", "sleep_hr_min"]),
    ("energy", ["body_battery_high", "body_battery_low", "body_battery_sleep_charge"]),
    ("stress", ["stress_avg", "high_stress_min"]),
    ("activity", ["intensity_minutes", "active_kcal"]),
    ("breathing", ["respiration_awake"]),
    # Whatever the person measures themself (BODY-ENTRENO-MEDIDA-1): every metric_def row with
    # origen 'self', read off db.METRIC_DEFS rather than named here, so a future self-measured key
    # joins this group -- and the picker -- without a second place to list it (template, not one
    # person).
    ("self", [d[0] for d in db.METRIC_DEFS if d[5] == "self"]),
]

# BODY-AGENTE-TENDENCIAS-CATALOGO-1: every metric the Trends screen can show, in catalogue order
# (TREND_KEYS' own 9 first, then each group's keys in TREND_GROUPS' own order), without
# duplicates -- the assistant's leer_tendencias reads this instead of a hand-typed tuple parallel
# to the screen's own catalogue, so the next metric this catalogue gains reaches the assistant too.
TODAS = tuple(dict.fromkeys([*TREND_KEYS, *(k for _, keys in TREND_GROUPS for k in keys)]))
