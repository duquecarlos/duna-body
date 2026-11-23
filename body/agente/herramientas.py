"""The assistant's one static tool registry. Every entry here is `lectura` (read-only): it answers a
question from an existing read function and never writes. `disponibles(nivel)` filters the registry to
what a person's `ajuste_agente.nivel_datos` allows; `ejecutar(...)` refuses a tool outside that level, an
unknown name, or a call with the wrong parameters. No provider client, no network, no screen -- this
module is plain Python called with a connection a caller already opened.

`leer_metas` and `leer_meta` read the newest EXISTING `goal_check` row with a plain SELECT. They must
never call `goals.weekly_check` or `goals.cerrar_semanas` (BODY-SEGURIDAD-3's writers) -- a `lectura`
tool must never write (BODY-ASISTENTE-OBS-1). `goals.evaluar_semana` is the read-only path a tool
could use to show the current week's verdict live, the way body/rutas/metas.py's screens do; left
unused here because an active goal with no data yet would then read as a computed "no verdict" dict
instead of "sin verificación aún" -- the shape every other read tool and existing test depends on.

The `proponer_*` tools below, plus `planear_entreno`, `registrar_entreno` (BODY-ENTRENO-TOOLS-1),
`registrar_medida` (BODY-ENTRENO-MEDIDA-1), `marcar_habito` (BODY-ENTRENO-HABITO-AUTO-1) and
`proponer_experimento`/`marcar_experimento` (BODY-EXPERIMENTOS-REGISTRO-1), are
`tipo: propuesta` (BODY-ASISTENTE-PROPUESTAS-1): they
create a server-side proposal record through `body/agente/propuestas.py` and never write the PERSON's
correction themselves -- nothing the person said is stored until they tap Aplicar. `registrar_entreno`
is the one exception, and a narrow one: when it is BUILT (not applied) it may call `pull.traer_series`
to fetch and cache the watch's own sets for that day, if they are not stored yet, so the card can show
both versions at once (BODY-ENTRENO-1-INV slice 4 design). That call only ever fills `workout_set`'s
`garmin_*` columns -- a mirror of what the watch already recorded -- and never touches a `person_*`
column or sends anything to Garmin.

`registrar_medida`'s `metric_key` is any `metric_def` row with `origen = 'self'` -- checked against
the live table at call time, never against a hardcoded pair of keys, so a metric_def row added later
is accepted the same way (template, not one person). The JSON-schema `enum` offered to the model is
built from `db.METRIC_DEFS` (the same source metric_def itself is seeded from), not typed out here.

`marcar_habito`'s access check (BODY-ENTRENO-HABITO-AUTO-1) reuses `goals.goal_access` and the same
assignee rule body/rutas/metas.py's `habit_check` route already enforces -- a habit assigned to
someone else is never the viewer's to check, even on a household goal they can otherwise read --
never a second rule invented here.

`propuestas.py` is the one module a `propuesta` tool may import to create its record -- the static
guard in tests/test_agente_registro.py checks that a `lectura` tool's call graph never reaches it.

`leer_experimentos`, `proponer_experimento` and `marcar_experimento` (BODY-EXPERIMENTOS-REGISTRO-1,
BODY-EXPERIMENTOS-1-INV slice 5) let the person start, mark and ask about an experiment from the
chat -- the same propose/apply/undo shape as `marcar_habito`, never a second one invented here.
`proponer_experimento` validates with `db.validar_experimento` and the same habit-ownership rule
`db.crear_experimento` itself enforces BEFORE the proposal is ever stored, so a bad proposal is a
ToolError, never a card the person could tap Aplicar on. `marcar_experimento` refuses an
experiment that has a `habito_id`: that experiment's log is its linked habit's `habit_check`, kept
current the usual way, through `marcar_habito`.

`marcar_periodo`/`cerrar_periodo`/`corregir_periodo`/`borrar_periodo` (BODY-PERIODO-1) let the
assistant OFFER -- never silently create -- a stretch of days marked illness, travel, injury or
(BODY-PLAN-FASES-4 ruling 2) training through a mild issue. Whether those days leave the usual
range `baselines.baseline`/`experimentos.dias_que_cuentan` compare against is the kind's own
`excluye_del_rango` row, never a hardcoded kind check. `kind` is any live `period_kind` row, never
a hardcoded pair of keys, the same template pattern `registrar_medida`/`registrar_ingesta` use for
their own enums. The person's own note is kept like `moment.note` and `ingesta.descripcion` --
never printed, logged or exported off the PC.

`importar_desplazamiento` (BODY-DESPLAZAMIENTO-ASISTENTE-1) is the one `propuesta` tool not at
`nivel` 'ninguno': it is gated at 'resumen', the same nivel_datos the commute data it imports is
itself shown at (Tendencias' own `leer_tendencias`) -- counts and dates only, never a day's own
figure, matching `ajustes.py`'s own definition of 'resumen'. It validates with the SAME
`desplazamiento_import.validar` the device route (POST /api/desplazamiento) calls, before any
proposal exists, so a refused file never becomes a card.
"""
from __future__ import annotations

import bisect
import contextvars
import json
import sqlite3
import statistics
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import anatomia, baselines, captura_sueno, catalogo_tendencias, comidas, compartir, contexto_dia, cuenta, \
    db, despertar_real, desplazamiento_import, enlaces, entreno_historial, errores, esfuerzo_sesion, \
    experimentos, fechas, garmin_adapter, garmin_cuenta, goals, history, horizonte, household, noche_rasgos, \
    nutriente_objetivo, practicas, profile, pull, readiness, reentrada, registro_cobertura, shift_import, \
    sueno_fc, turnos_costo, tutoriales, viajes
from ..i18n import LANGS, T
from . import ajustes, conversacion as agente_conversacion, propuestas

# BODY-TURNOS-OPCIONAL-3: whether the CURRENT request's person has said they work shifts, set once by
# body/web.py (the only caller of body/agente/bucle.py's ejecutar()) before it drives the tool loop --
# the same contextvar pattern web.py already uses for _RUTA_ACTUAL. disponibles() reads it to leave
# the three shift tools off the list offered to the model; body/agente/bucle.py's own
# `herramientas.disponibles(nivel)` call needs no change. Default True: a caller that never sets it
# (every existing test, a tool executed directly) sees every tool nivel already allows, unchanged.
_TURNOS_ACTUAL: contextvars.ContextVar[bool] = contextvars.ContextVar("_turnos_actual", default=True)

TURNOS_HERRAMIENTAS = frozenset({"leer_turnos", "leer_costo_turnos", "proponer_confirmar_turnos"})


def fijar_turnos_actual(valor: bool) -> None:
    _TURNOS_ACTUAL.set(bool(valor))

# BODY-AGENTE-TENDENCIAS-CATALOGO-1: the same catalogue the Trends screen shows
# (body/catalogo_tendencias.py), so a metric added there reaches leer_tendencias with no second,
# hand-typed list to keep in step.
TENDENCIA_METRICAS = catalogo_tendencias.TODAS
RANGOS = ("7", "30", "90", "all")

_SIN_PARAMETROS = {"type": "object", "properties": {}, "additionalProperties": False}
_PARAM_DIA = {
    "type": "object",
    "properties": {"day": {"type": "string", "format": "date"}},
    "required": ["day"],
    "additionalProperties": False,
}
_PARAM_META = {
    "type": "object",
    "properties": {"goal_id": {"type": "integer"}},
    "required": ["goal_id"],
    "additionalProperties": False,
}
_PARAM_TENDENCIAS = {
    "type": "object",
    "properties": {
        "rango": {"type": "string", "enum": list(RANGOS)},
        "metric": {"type": "string", "enum": list(TENDENCIA_METRICAS)},
    },
    "additionalProperties": False,
}

# BODY-AGENTE-LEER-SERIE-1 ruling 2: bounds so one call stays a read, never a dump. 'mes' is
# deliberately unbounded -- a whole watch history at that paso is already a handful of months'
# rows, never worth refusing. ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1 adds 'minuto': a window of
# full local timestamps (never a plain date, since it may cross midnight) over the minute streams
# in LEER_SERIE_MINUTO_METRICAS, for analysing a short stretch in detail -- see _leer_serie_minuto.
LEER_SERIE_PASOS = ("dia", "semana", "mes", "minuto")
_LEER_SERIE_MAX_METRICAS = 12
_LEER_SERIE_MAX_DIAS_PASO_DIA = 366
_LEER_SERIE_MAX_SEMANAS_PASO_SEMANA = 260
# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: paso='minuto''s own window cap (a stretch inside a
# night or a day, never a whole day's own minute-by-minute dump -- leer_dia/leer_noche are for
# that) and the six minute streams it can return, each present on a row only when that stream
# actually has data there (never a 0 or an invented value).
_LEER_SERIE_MAX_MIN_PASO_MINUTO = 360
LEER_SERIE_MINUTO_METRICAS = ("fc", "estres", "etapa", "vfc", "movimiento", "body_battery")
# How far a sparse sample may sit from the minute asked about and still count: vfc/hrv is commonly
# sampled every 5 minutes (forward-filled, ruling 1's own "latest at or before"); body_battery's
# own sampling is looser still, matched on either side ("nearest").
_LEER_SERIE_MINUTO_TOL_VFC_MIN = 5
_LEER_SERIE_MINUTO_TOL_BB_MIN = 15
_PARAM_LEER_SERIE = {
    "type": "object",
    "properties": {
        # paso='minuto' uses LEER_SERIE_MINUTO_METRICAS instead of TENDENCIA_METRICAS -- the union
        # here is the model's own guidance; `_leer_serie`/`_leer_serie_minuto` enforce which half
        # applies, the same paso-dependent split 'desde'/'hasta' already needs (a plain date for
        # 'dia'/'semana'/'mes', a full local timestamp for 'minuto' -- never checked by "format"
        # here, since the two shapes differ by paso).
        "metricas": {"type": "array",
                     "items": {"type": "string",
                               "enum": list(TENDENCIA_METRICAS) + list(LEER_SERIE_MINUTO_METRICAS)},
                     "minItems": 1, "maxItems": _LEER_SERIE_MAX_METRICAS},
        "desde": {"type": "string"},
        "hasta": {"type": "string"},
        "paso": {"type": "string", "enum": list(LEER_SERIE_PASOS)},
    },
    "required": ["metricas", "desde", "hasta", "paso"],
    "additionalProperties": False,
}
# key -> unit, off db.METRIC_DEFS itself (template, not a second hand-typed table).
_UNIDADES_METRICA = {d[0]: d[2] for d in db.METRIC_DEFS}

# BODY-DESPERTAR-REAL-2 ruling 1: the window cap -- a few hours of one day (waking, a workout, a
# stressful moment), never a whole day's own minute-by-minute dump (leer_dia's own bare figures are
# for that; leer_serie is for a range of days).
_LEER_INTRADIA_MAX_MIN = 4 * 60
_PARAM_LEER_INTRADIA = {
    "type": "object",
    "properties": {
        "fecha": {"type": "string", "format": "date"},
        "desde": {"type": "string"},
        "hasta": {"type": "string"},
    },
    "required": ["fecha", "desde", "hasta"],
    "additionalProperties": False,
}
# BODY-DESPERTAR-REAL-2 ruling 1: the three honest reasons an empty window can have -- never a
# hand-typed list anywhere else (test_i18n_paridad.py's own dynamic-prefix census reads this one).
MOTIVOS_INTRADIA_SIN_DATOS = ("fuera_de_alcance", "reloj_apagado", "no_traido")

# BODY-NOCHE-LECTURA-1 ruling 2: leer_noche's own window around Garmin's own sleep session --
# enough margin before sleep start and after sleep end to show the fall-asleep and the wake-up,
# capped so a corrupt or missing `end_local` can never make the window unbounded. Carlos's own
# words (relayed by the desk 2026-11-19): "I was thinking on just one big reading, and there it
# will be all the info."
_LEER_NOCHE_ANTES_MIN = 60
_LEER_NOCHE_DESPUES_MIN = 120
_LEER_NOCHE_MAX_MIN = 16 * 60

# BODY-NOCHE-LECTURA-2 ruling 2: Garmin's own sleep-stage code, ASSUMED -- never confirmed by the
# installed garminconnect package, same unconfirmed-key convention garmin_adapter's own
# _SLEEP_SERIES_SPECS already documents for this array. A code outside this table gets
# 'desconocido', never a guess; each night leer_noche reads CHECKS the mapping against that
# night's own stored totals (`_verificar_etapas` below) instead of trusting it blind.
ETAPAS_SUENO_GARMIN = {0: "profundo", 1: "ligero", 2: "rem", 3: "despierto"}
# The SAME four keys the day page's own stage bar reads (body/rutas/hoy.py's STAGES) -- never a
# second name for the same total.
_ETAPA_METRICA = {"profundo": "sleep_deep_min", "ligero": "sleep_light_min",
                   "rem": "sleep_rem_min", "despierto": "sleep_awake_min"}
# BODY-NOCHE-LECTURA-2 ruling 1: when rango_30_dias is null, the z fallback below reuses
# baselines.baseline's own formula (mean/sd/quantiles) over a window wide enough to reach the
# person's whole history -- never a second formula, and never what rango_historia itself shows
# (that one stays profile.metric_profile's own low/high, unexcluded, matching Tendencias exactly).
_VENTANA_HISTORIA_DIAS = 3650
_PARAM_LEER_NOCHE = {
    "type": "object",
    "properties": {"fecha": {"type": "string", "format": "date"}},
    "required": ["fecha"],
    "additionalProperties": False,
}

# BODY-MINUTO-ANALISIS-2 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2): `grupo`'s six optional keys,
# each checked by body/noche_rasgos.py's own `_coincide_grupo` -- a key never named here is refused
# by `additionalProperties: False` before the model's own call ever reaches that module.
_PARAM_COMPARAR_NOCHE = {
    "type": "object",
    "properties": {
        "fecha": {"type": "string", "format": "date"},
        "desde": {"type": "string", "format": "date"},
        "grupo": {
            "type": "object",
            "properties": {
                "tras_turno": {"type": "string", "enum": list(db.SHIFT_KINDS)},
                "tras_turno_noche": {"type": "boolean"},
                "viaje": {"type": "boolean"},
                "periodo": {"type": "string"},
                "comida_tarde_min_max": {"type": "integer"},
                "con_alcohol": {"type": "boolean"},
                "con_desplazamiento": {"type": "boolean"},
                "altitud_cambio_min": {"type": "integer"},
                "altitud_cambio_max": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    "required": ["fecha"],
    "additionalProperties": False,
}

# BODY-MINUTO-ANALISIS-3 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2): buscar_noches' own bounds --
# 'limite' default/cap, so a model asking for "my worst nights" without a number still gets a
# short, useful answer, never an unbounded dump.
_BUSCAR_NOCHES_LIMITE_DEFECTO = 10
_BUSCAR_NOCHES_LIMITE_MAX = 60
_PARAM_BUSCAR_NOCHES = {
    "type": "object",
    "properties": {
        "desde": {"type": "string", "format": "date"},
        "hasta": {"type": "string", "format": "date"},
        "filtros": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rasgo": {"type": "string"},
                    "op": {"type": "string", "enum": list(noche_rasgos.OPS_BUSCAR_NOCHES)},
                    "valor": {"type": "number"},
                },
                "required": ["rasgo", "op", "valor"],
                "additionalProperties": False,
            },
        },
        "grupo": _PARAM_COMPARAR_NOCHE["properties"]["grupo"],
        "orden": {
            "type": "object",
            "properties": {
                "rasgo": {"type": "string"},
                "dir": {"type": "string", "enum": ["asc", "desc"]},
            },
            "required": ["rasgo", "dir"],
            "additionalProperties": False,
        },
        "limite": {"type": "integer", "minimum": 1, "maximum": _BUSCAR_NOCHES_LIMITE_MAX},
        "rasgos": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

# BODY-DESPERTAR-REAL-4 ruling 2: the two things a person can correct -- the same pair
# despertar_real.PROPUESTA_TIPOS lists (BODY-DESPERTAR-REAL-6A-R2 ruling 2: an empty `propuestas`
# list is "nothing to propose" now, never a third tipo value here).
TIPOS_CORREGIR_DESPERTAR = ("sueno", "levantado")
_PARAM_CORREGIR_DESPERTAR = {
    "type": "object",
    "properties": {
        "fecha": {"type": "string", "format": "date"},
        "tipo": {"type": "string", "enum": list(TIPOS_CORREGIR_DESPERTAR)},
        "hora": {"type": "string"},
    },
    "required": ["fecha", "tipo", "hora"],
    "additionalProperties": False,
}

DIRECTIONS = ("raise", "lower", "keep")

# BODY-PLAN-PROPUESTAS-1 ruling 2: a habit on a goal -- the person's own cadence shape
# (body/rutas/metas.py's `_parse_new_habit`), described here for the model's guidance only; the
# real enforcement is `_validar_cadencia_habito` below, since `_validar_parametros` never recurses
# into an array item's own properties (the same limitation _PARAM_CANTIDAD_COMIDA's own comment
# names).
_CONTEXTOS_HABITO = ("before_night", "after_night", "off_days")
_PARAM_HABITO_PLAN = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "cadencia": {
            "type": "object",
            "properties": {
                "per": {"type": "string", "enum": ["day", "week", "month"]},
                "times": {"type": "integer"},
                "context": {"type": "string", "enum": list(_CONTEXTOS_HABITO)},
            },
        },
        # BODY-HISTORIA-VIDA-2 ruling 4: additive -- the practice this habit's own evidence comes
        # from, when the person is bringing one back. Described here for the model's guidance only;
        # the real enforcement is `_validar_habitos_plan` below (own, active practice), the same
        # limitation `cadencia`'s own comment names.
        "practica_id": {"type": "integer"},
    },
    "required": ["titulo", "cadencia"],
}
_PARAM_PROPONER_META = {
    "type": "object",
    "properties": {
        "metric_key": {"type": "string"},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "target_value": {"type": "number"},
        "deadline": {"type": "string", "format": "date"},
        "titulo": {"type": "string"},
        # BODY-PLAN-PROPUESTAS-1 ruling 2: additive. metric_key/direction/target_value stay out of
        # "required" below -- `_proponer_meta` itself refuses a 'milestone' that carries them, or
        # a 'metric' (the default) that lacks any of them, in plain Python, the same division
        # `_planear_entreno`'s own per-exercise checks already use.
        "tipo": {"type": "string", "enum": ["metric", "milestone"]},
        "habitos": {"type": "array", "items": _PARAM_HABITO_PLAN},
        "horizonte_goal_id": {"type": "integer"},
    },
    "required": ["deadline", "titulo"],
    "additionalProperties": False,
}
_PARAM_PROPONER_CAMBIO_META = {
    "type": "object",
    "properties": {
        "goal_id": {"type": "integer"},
        "target_value": {"type": "number"},
        "deadline": {"type": "string", "format": "date"},
    },
    "required": ["goal_id"],
    "additionalProperties": False,
}
_PARAM_PROPONER_IDIOMA = {
    "type": "object",
    "properties": {"lang": {"type": "string", "enum": list(LANGS)}},
    "required": ["lang"],
    "additionalProperties": False,
}
_PARAM_LEER_TUTORIALES = {
    "type": "object",
    "properties": {"consulta": {"type": "string"}},
    "additionalProperties": False,
}
_LEER_TUTORIALES_MAX = 5

_PARAM_LEER_ENTRENO = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "desde": {"type": "string", "format": "date"},
        "hasta": {"type": "string", "format": "date"},
    },
    "additionalProperties": False,
}
_LEER_DIAS_LIBRES_MIN_DIAS = 1
_LEER_DIAS_LIBRES_MAX_DIAS = 14
_PARAM_LEER_DIAS_LIBRES = {
    "type": "object",
    "properties": {
        "desde": {"type": "string", "format": "date"},
        "dias": {"type": "integer", "minimum": _LEER_DIAS_LIBRES_MIN_DIAS, "maximum": _LEER_DIAS_LIBRES_MAX_DIAS},
    },
    "additionalProperties": False,
}
_PARAM_PLANEAR_ENTRENO = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "titulo": {"type": "string"},
        "time": {"type": "string"},
        "goal_id": {"type": "integer"},
        # BODY-PLAN-PROPUESTAS-1 ruling 5: additive, distinct from `goal_id` above -- a plan the
        # person owns specifically (kind='milestone'), checked with `_plan_propio` below, rather
        # than `goal_id`'s own looser "any owned goal" check. Give at most one of the two.
        "horizonte_goal_id": {"type": "integer"},
        "ejercicios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ejercicio": {"type": "string"},
                    "series": {"type": "integer"},
                    "reps": {},
                    "categoria": {"type": "string", "enum": list(anatomia.CATEGORIA_ENUM)},
                },
                "required": ["ejercicio", "series"],
            },
        },
    },
    "required": ["day", "titulo", "ejercicios"],
    "additionalProperties": False,
}
_PARAM_REGISTRAR_ENTRENO = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "titulo": {"type": "string"},
        "time": {"type": "string"},
        "series": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ejercicio": {"type": "string"},
                    "categoria": {"type": "string", "enum": list(anatomia.CATEGORIA_ENUM)},
                    "reps": {"type": ["integer", "null"], "minimum": 0},
                    "peso_kg": {"type": ["number", "null"], "minimum": 0},
                    "nota": {"type": "string"},
                },
                "required": ["ejercicio"],
            },
        },
    },
    "required": ["day", "series"],
    "additionalProperties": False,
}
# BODY-CORREGIR-MEDIDA-ENTRENO-1-W ruling B: id-addressed like corregir_ingesta -- `orden` names the
# set's position as the training-session page shows it, never a row id the person never sees.
_PARAM_CORREGIR_ENTRENO = {
    "type": "object",
    "properties": {
        "dia": {"type": "string", "format": "date"},
        "orden": {"type": "integer", "minimum": 1},
        "ejercicio": {"type": "string"},
        "reps": {"type": ["integer", "null"], "minimum": 0},
        "peso_kg": {"type": ["number", "null"], "minimum": 0},
    },
    "required": ["dia", "orden"],
    "additionalProperties": False,
}
# BODY-HOGAR-ESCRIBE-1 ruling 4: plan_id names an existing PLANNED session row (the id the Hogar
# feed already returns) -- never a title, which can be misheard.
_PARAM_ANOTAR_SESION = {
    "type": "object",
    "properties": {
        "plan_id": {"type": "integer"},
        "nota": {"type": "string"},
        "hecha": {"type": "boolean"},
    },
    "required": ["plan_id", "nota"],
    "additionalProperties": False,
}
_PARAM_DESCARTAR_SERIES = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "series": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
    },
    "required": ["day", "series"],
    "additionalProperties": False,
}
# BODY-ENTRENO-VINCULO-1 ruling 4: source_id names either half of the linked pair --
# _resolver_par_vinculado finds the other one.
_PARAM_SEPARAR_SESION = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "source_id": {"type": "string"},
    },
    "required": ["day", "source_id"],
    "additionalProperties": False,
}
_PARAM_UNIR_SESIONES = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
    },
    "required": ["day"],
    "additionalProperties": False,
}

# BODY-PROTEINA-1: an intake key (agregado = 'suma_ingesta') is excluded from registrar_medida's own
# enum -- it has exactly one writer, registrar_ingesta, below. BODY-CHECKIN-1: the three check-in
# keys are excluded the same way -- registrar_checkin is their one writer.
SELF_METRIC_KEYS = tuple(d[0] for d in db.METRIC_DEFS if d[5] == "self" and d[6] != "suma_ingesta"
                          and d[0] not in db.CHECKIN_METRIC_KEYS)
_PARAM_REGISTRAR_MEDIDA = {
    "type": "object",
    "properties": {
        "metric_key": {"type": "string", "enum": list(SELF_METRIC_KEYS)},
        "value": {"type": "number"},
        "day": {"type": "string", "format": "date"},
    },
    "required": ["metric_key", "value"],
    "additionalProperties": False,
}

# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: durante/despues each 0-10 (db.DOLOR_ESCALA_MIN/MAX),
# both optional -- the same "each optional" shape the session screen's own card gives.
_PARAM_REGISTRAR_DOLOR = {
    "type": "object",
    "properties": {
        "dia": {"type": "string", "format": "date"},
        "ejercicio": {"type": "string"},
        "dolor_durante": {"type": ["integer", "null"], "minimum": db.DOLOR_ESCALA_MIN, "maximum": db.DOLOR_ESCALA_MAX},
        "dolor_despues": {"type": ["integer", "null"], "minimum": db.DOLOR_ESCALA_MIN, "maximum": db.DOLOR_ESCALA_MAX},
        "movimiento": {"type": ["string", "null"]},
    },
    "required": ["dia", "ejercicio"],
    "additionalProperties": False,
}

# BODY-PROTEINA-1: metric_def rows whose daily total is derived from `ingesta` entries, in
# catalogue order -- db.INGESTA_METRIC_KEYS (BODY-COMIDA-1), read off the live table rather than a
# second hand-typed list, same template pattern as SELF_METRIC_KEYS above.
INGESTA_METRIC_KEYS = db.INGESTA_METRIC_KEYS
# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the accepted `hora` forms, named here for the model
# (every schema below that takes `hora` shares this one description, the same string `_hora_a_ts_local`
# itself validates against).
_HORA_PARAM_DESCRIPCION = (
    "Hora local, en una de dos formas: 'HH:MM' (o 'H:MM'), que usa el día de 'day'/'fecha'; o una "
    "fecha-hora ISO completa (AAAA-MM-DDTHH:MM:SS), con o sin desplazamiento horario -- se convierte "
    "a la zona horaria de la persona, y su fecha local debe coincidir con 'day'/'fecha' si se da, o "
    "lo reemplaza si no se da. Cualquier otra forma se rechaza nombrando estas dos.")
_PARAM_REGISTRAR_INGESTA = {
    "type": "object",
    "properties": {
        "metric_key": {"type": "string", "enum": list(INGESTA_METRIC_KEYS)},
        "amount": {"type": "number"},
        "estimado": {"type": "boolean"},
        "descripcion": {"type": "string"},
        "day": {"type": "string", "format": "date"},
        "hora": {"type": "string", "description": _HORA_PARAM_DESCRIPCION},
    },
    "required": ["metric_key", "amount", "estimado"],
    "additionalProperties": False,
}
_PARAM_CORREGIR_INGESTA = {
    "type": "object",
    "properties": {"ingesta_id": {"type": "integer"}, "amount": {"type": "number"}},
    "required": ["ingesta_id", "amount"],
    "additionalProperties": False,
}
_PARAM_BORRAR_INGESTA = {
    "type": "object",
    "properties": {"ingesta_id": {"type": "integer"}},
    "required": ["ingesta_id"],
    "additionalProperties": False,
}

# BODY-COMIDA-1 ruling 3: one spoken meal, several nutrients at once -- `cantidades` is described
# here only for the model's own guidance (the real enforcement, one amount per key and a door-
# validated number, is `_registrar_comida` itself, since `_validar_parametros` never recurses into
# an array item's own properties).
_PARAM_CANTIDAD_COMIDA = {
    "type": "object",
    "properties": {
        "metric_key": {"type": "string", "enum": list(INGESTA_METRIC_KEYS)},
        "amount": {"type": "number"},
        "estimado": {"type": "boolean"},
    },
    "required": ["metric_key", "amount"],
}
# BODY-COMIDA-COMPARTIDA-1 ruling 4: who else ate this meal -- `nombre` is resolved against the
# author's own household (body/household.py's household_of + each member's display_name), never a
# person_id from outside; `nutrientes` is THAT eater's own portion (metric_key -> amount), the same
# shape body/comida_compartida.py's own porcion row carries. Described here only for the model's
# own guidance, same reason _PARAM_CANTIDAD_COMIDA above is.
_PARAM_COMENSAL_COMPARTIDO = {
    "type": "object",
    "properties": {
        "nombre": {"type": "string"},
        "porcion_texto": {"type": "string"},
        "nutrientes": {"type": "object"},
    },
    "required": ["nombre", "nutrientes"],
    "additionalProperties": False,
}
# BODY-HOGAR-ESCRIBE-1-W ruling 3: `momento` is any clave the PERSON already has (their own
# momento_comida rows, or the DEFAULT_MOMENTOS claves when they have none) -- checked in CODE
# against the live `comidas.momentos` at call time, never a hardcoded enum, the same template
# pattern `registrar_medida`'s metric_key already uses for a per-person live source.
_PARAM_REGISTRAR_COMIDA = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "hora": {"type": "string", "description": _HORA_PARAM_DESCRIPCION},
        "descripcion": {"type": "string"},
        "cantidades": {"type": "array", "items": _PARAM_CANTIDAD_COMIDA, "minItems": 1},
        "compartida_con": {"type": "array", "items": _PARAM_COMENSAL_COMPARTIDO},
        "momento": {"type": "string", "description": (
            "Una de las claves propias de momento de comida de la persona (comidas.momentos: sus "
            "propios momentos si ya tiene, o los predeterminados -- desayuno/almuerzo/merienda/cena "
            "-- si no). Se deja vacío cuando no se da; nunca se adivina a partir de una palabra "
            "distinta ('mañana' no es un momento). Un valor que no sea una de esas claves se "
            "rechaza nombrando las que sí son válidas para esa persona.")},
    },
    "required": ["descripcion", "cantidades"],
    "additionalProperties": False,
}
# BODY-COMIDA-CORREGIR-1: a field left out keeps its current value, the same convention
# _PARAM_CORREGIR_PERIODO already uses; 'cantidades', when given, REPLACES the whole set (never
# merged with the existing one) -- the same shape _PARAM_REGISTRAR_COMIDA's own field uses.
_PARAM_CORREGIR_COMIDA = {
    "type": "object",
    "properties": {
        "comida_id": {"type": "integer"},
        "hora": {"type": "string", "description": _HORA_PARAM_DESCRIPCION},
        "day": {"type": "string", "format": "date"},
        "descripcion": {"type": "string"},
        "cantidades": {"type": "array", "items": _PARAM_CANTIDAD_COMIDA, "minItems": 1},
    },
    "required": ["comida_id"],
    "additionalProperties": False,
}

# BODY-CHECKIN-1 ruling 3: momento is a short key validated in CODE, never a DB CHECK -- a new
# moment word is a code change here, not a migration. mood/energy/stress_felt are typed as a LIST
# (["integer", "string", "null"]), not the bare string "integer": _validar_parametros only enforces
# the bare-string shapes, so a plain numeric string ("4") reaches _numero_checkin below to coerce,
# the same choice _PARAM_REGISTRAR_ENTRENO's reps/peso_kg already made (BODY-ENTRENO-NUMEROS-1).
MOMENTOS_CHECKIN = ("despertar", "post_turno", "otro")
_PARAM_REGISTRAR_CHECKIN = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "hora": {"type": "string", "description": _HORA_PARAM_DESCRIPCION},
        "momento": {"type": "string", "enum": list(MOMENTOS_CHECKIN)},
        "mood": {"type": ["integer", "string", "null"], "minimum": 1, "maximum": 5},
        "energy": {"type": ["integer", "string", "null"], "minimum": 1, "maximum": 5},
        "stress_felt": {"type": ["integer", "string", "null"], "minimum": 1, "maximum": 5},
        "nota": {"type": "string"},
    },
    "required": ["momento"],
    "additionalProperties": False,
}

# BODY-NUTRIENTE-OBJETIVO-1: a daily target for one of the eight intake keys -- generic over all
# of them, the same INGESTA_METRIC_KEYS template pattern above. `amount` is nullable in the schema
# (kind 'off' carries none); `_proponer_objetivo_nutriente` itself enforces it otherwise.
_PARAM_PROPONER_OBJETIVO_NUTRIENTE = {
    "type": "object",
    "properties": {
        "metric_key": {"type": "string", "enum": list(INGESTA_METRIC_KEYS)},
        "kind": {"type": "string", "enum": list(db.NUTRIENT_TARGET_KINDS)},
        "amount": {"type": ["number", "null"]},
        "sentido": {"type": "string", "enum": list(db.NUTRIENT_TARGET_SENTIDOS)},
        "start_day": {"type": "string", "format": "date"},
    },
    "required": ["metric_key", "kind", "sentido"],
    "additionalProperties": False,
}

# BODY-PERIODO-1: the seeded period_kind rows, read off the live table's own seed list rather than
# typed out here a second time -- the same template pattern INGESTA_METRIC_KEYS above uses.
PERIODO_KIND_KEYS = tuple(k for k, *_ in db.PERIOD_KINDS)
_PARAM_MARCAR_PERIODO = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(PERIODO_KIND_KEYS)},
        "start_day": {"type": "string", "format": "date"},
        "end_day": {"type": "string", "format": "date"},
        "note": {"type": "string"},
        # BODY-LESION-ZONA-1 ruling 1: set this when the person names a limited body part (today,
        # kind 'lesion' or, BODY-PLAN-FASES-4 ruling 2, 'molestia' -- training through it) -- Body
        # then reads a lighter training set in that zona as deliberate, never a drop (ruling 2), and
        # the next plans respect it (ruling 3).
        "zonas": {"type": "array", "items": {"type": "string", "enum": list(anatomia.CATEGORIA_ENUM)}},
    },
    "required": ["kind", "start_day"],
    "additionalProperties": False,
}
# BODY-MINUTO-ANALISIS-6-ALTITUD (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4): no place name, no
# coordinates -- only es_casa/start_day/end_day/altitud_m/viaje_ref. `fuente` is never a model-
# facing field: this tool's own wrapper always records 'asistente'.
_PARAM_REGISTRAR_ALTITUD = {
    "type": "object",
    "properties": {
        "es_casa": {"type": "boolean"},
        "start_day": {"type": "string", "format": "date"},
        "end_day": {"type": "string", "format": "date"},
        "altitud_m": {"type": "integer", "minimum": -500, "maximum": 9000},
        "viaje_ref": {"type": "string"},
    },
    "required": ["es_casa", "start_day", "altitud_m"],
    "additionalProperties": False,
}
_PARAM_LEER_ALTITUDES = {
    "type": "object",
    "properties": {
        "desde": {"type": "string", "format": "date"},
        "hasta": {"type": "string", "format": "date"},
    },
    "additionalProperties": False,
}
_PARAM_CERRAR_PERIODO = {
    "type": "object",
    "properties": {"period_id": {"type": "integer"}, "end_day": {"type": "string", "format": "date"}},
    "required": ["period_id", "end_day"],
    "additionalProperties": False,
}
_PARAM_CORREGIR_PERIODO = {
    "type": "object",
    "properties": {
        "period_id": {"type": "integer"},
        "kind": {"type": "string", "enum": list(PERIODO_KIND_KEYS)},
        "start_day": {"type": "string", "format": "date"},
        "end_day": {"type": "string", "format": "date"},
        "note": {"type": "string"},
        # BODY-LESION-ZONA-1 ruling 1: left out keeps the current zonas; an empty list clears it.
        "zonas": {"type": "array", "items": {"type": "string", "enum": list(anatomia.CATEGORIA_ENUM)}},
    },
    "required": ["period_id"],
    "additionalProperties": False,
}
_PARAM_BORRAR_PERIODO = {
    "type": "object",
    "properties": {"period_id": {"type": "integer"}},
    "required": ["period_id"],
    "additionalProperties": False,
}

_PARAM_ACTIVIDAD_EVIDENCIA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "source_id": {"type": "string"},
    },
    "required": ["source", "source_id"],
    "additionalProperties": False,
}
_PARAM_MARCAR_HABITO = {
    "type": "object",
    "properties": {
        "habit_id": {"type": "integer"},
        "day": {"type": "string", "format": "date"},
        "actividad": _PARAM_ACTIVIDAD_EVIDENCIA,
        "nota": {"type": "string"},
    },
    "required": ["habit_id", "day"],
    "additionalProperties": False,
}

_PARAM_LEER_EXPERIMENTOS = {
    "type": "object",
    "properties": {"experimento_id": {"type": "integer"}},
    "additionalProperties": False,
}

_PARAM_LEER_PRACTICAS = {
    "type": "object",
    "properties": {"practica_id": {"type": "integer"}},
    "additionalProperties": False,
}
# regla is one of practicas.FUENTES' own three shapes -- described here for the model's guidance
# only; the real enforcement is practicas.FUENTES[fuente](regla) itself, called from
# _proponer_practica before anything is stored (same convention as _PARAM_VENTANA/_PARAM_FILTRO_DIAS
# above).
_PARAM_PROPONER_PRACTICA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "fuente": {"type": "string", "enum": list(practicas.FUENTES)},
        "regla": {"type": "object"},
        "meta_dias_semana": {"type": "integer", "minimum": 1, "maximum": 7},
    },
    "required": ["titulo", "fuente", "regla"],
    "additionalProperties": False,
}

# ventana/filtro_dias are the same two JSON shapes db.validar_experimento checks -- described here
# only for the model's own guidance; the real enforcement is db.validar_experimento itself, called
# from _proponer_experimento before anything is stored (_validar_parametros above never recurses
# into a nested object's own properties/required).
_PARAM_VENTANA = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": ["dia", "intradia"]},
        "ancla": {"type": "string", "enum": ["despertar", "momento"]},
        "antes_min": {"type": "integer"},
        "despues_min": {"type": "integer"},
    },
    "required": ["tipo"],
}
_PARAM_FILTRO_DIAS = {
    "type": "object",
    "properties": {
        "turno_en": {"type": "array", "items": {"type": "string", "enum": list(db.SHIFT_KINDS)}},
        "turno_fuera": {"type": "array", "items": {"type": "string", "enum": list(db.SHIFT_KINDS)}},
        "excluir_tras_noche": {"type": "boolean"},
    },
    "required": ["excluir_tras_noche"],
}
_PARAM_PROPONER_EXPERIMENTO = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "metricas": {"type": "array", "items": {"type": "string"}},
        "ventana": _PARAM_VENTANA,
        "filtro_dias": _PARAM_FILTRO_DIAS,
        "fecha_inicio": {"type": "string", "format": "date"},
        "fecha_fin": {"type": "string", "format": "date"},
        "dias_prueba_objetivo": {"type": "integer"},
        "habito_id": {"type": "integer"},
        # BODY-PLAN-PROPUESTAS-1 ruling 5: additive -- a plan the person owns, otherwise refused.
        "horizonte_goal_id": {"type": "integer"},
    },
    "required": ["titulo", "metricas", "ventana", "filtro_dias"],
    "additionalProperties": False,
}
_PARAM_MARCAR_EXPERIMENTO = {
    "type": "object",
    "properties": {
        "experimento_id": {"type": "integer"},
        "day": {"type": "string", "format": "date"},
        "ocurrio": {"type": "boolean"},
        "nota": {"type": "string"},
    },
    "required": ["experimento_id", "day", "ocurrio"],
    "additionalProperties": False,
}

# BODY-PLAN-PROPUESTAS-1: the whole-plan tools' own item shapes and bounded list sizes (ruling 1).
# Every cap is a per-call bound on the lists `proponer_plan`/`ampliar_plan` are willing to validate
# and write in one go, named here rather than left as a bare number -- generous enough for a real
# plan (a year of weekly targets, a couple of dozen rules or checkpoints), never unbounded.
_PLAN_MAX_METAS_FINALES = 10
_PLAN_MAX_HABITOS = 10
_PLAN_MAX_REGLAS = 20
_PLAN_MAX_SEMANAS = 104
_PLAN_MAX_METAS_SEMANALES = 300
_PLAN_MAX_CHECKPOINTS = 30
# BODY-PLAN-FASES-2 ruling 3: a plan's own phases, same bounded-list reasoning as the caps above --
# generous for a real plan (Grecia/Kilimanjaro/El Cocuy run to a handful of training blocks), never
# unbounded.
_PLAN_MAX_FASES = 20

_PARAM_META_FINAL_PLAN = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "metric_key": {"type": "string"},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "target_value": {"type": "number"},
    },
    "required": ["titulo", "metric_key", "direction", "target_value"],
}
_PARAM_REGLA_PLAN = {
    "type": "object",
    "properties": {
        "contexto": {"type": "string", "enum": ["siempre", "turno", "tras_noche", "periodo", "descanso"]},
        "contexto_valor": {"type": ["string", "null"]},
        "texto": {"type": "string"},
        "chequeo": {"type": ["object", "null"]},
        "fase_index": {"type": "integer"},
    },
    "required": ["contexto", "texto"],
}
_PARAM_FASE_PLAN = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "start_day": {"type": "string", "format": "date"},
        "end_day": {"type": "string", "format": "date"},
        "nota": {"type": "string"},
    },
    "required": ["titulo", "start_day", "end_day"],
}
_PARAM_SEMANA_PLAN = {
    "type": "object",
    "properties": {
        "week_start": {"type": "string", "format": "date"},
        "titulo": {"type": "string"},
        "nota": {"type": "string"},
    },
    "required": ["week_start"],
}
_PARAM_META_SEMANAL_PLAN = {
    "type": "object",
    "properties": {
        "week_start": {"type": "string", "format": "date"},
        "medida": {"type": "string"},
        "sentido": {"type": "string", "enum": ["al_menos", "como_maximo", "antes_de", "despues_de"]},
        "valor": {"type": ["number", "string"]},
        "nota": {"type": "string"},
    },
    "required": ["week_start", "medida", "sentido", "valor"],
}
_PARAM_CHECKPOINT_PLAN = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "titulo": {"type": "string"},
        "criterios": {"type": "array"},
        "nota": {"type": "string"},
    },
    "required": ["day", "titulo", "criterios"],
}
_PARAM_PROPONER_PLAN = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "why": {"type": "string"},
        "start_day": {"type": "string", "format": "date"},
        "deadline": {"type": "string", "format": "date"},
        "metas_finales": {"type": "array", "items": _PARAM_META_FINAL_PLAN, "maxItems": _PLAN_MAX_METAS_FINALES},
        "habitos": {"type": "array", "items": _PARAM_HABITO_PLAN, "maxItems": _PLAN_MAX_HABITOS},
        "fases": {"type": "array", "items": _PARAM_FASE_PLAN, "maxItems": _PLAN_MAX_FASES},
        "reglas": {"type": "array", "items": _PARAM_REGLA_PLAN, "maxItems": _PLAN_MAX_REGLAS},
        "semanas": {"type": "array", "items": _PARAM_SEMANA_PLAN, "maxItems": _PLAN_MAX_SEMANAS},
        "metas_semanales": {"type": "array", "items": _PARAM_META_SEMANAL_PLAN,
                             "maxItems": _PLAN_MAX_METAS_SEMANALES},
        "checkpoints": {"type": "array", "items": _PARAM_CHECKPOINT_PLAN, "maxItems": _PLAN_MAX_CHECKPOINTS},
    },
    "required": ["titulo", "start_day", "deadline"],
    "additionalProperties": False,
}
_PARAM_AMPLIAR_PLAN = {
    "type": "object",
    "properties": {
        "goal_id": {"type": "integer"},
        "fases": {"type": "array", "items": _PARAM_FASE_PLAN, "maxItems": _PLAN_MAX_FASES},
        "reglas": {"type": "array", "items": _PARAM_REGLA_PLAN, "maxItems": _PLAN_MAX_REGLAS},
        "semanas": {"type": "array", "items": _PARAM_SEMANA_PLAN, "maxItems": _PLAN_MAX_SEMANAS},
        "metas_semanales": {"type": "array", "items": _PARAM_META_SEMANAL_PLAN,
                             "maxItems": _PLAN_MAX_METAS_SEMANALES},
        "checkpoints": {"type": "array", "items": _PARAM_CHECKPOINT_PLAN, "maxItems": _PLAN_MAX_CHECKPOINTS},
    },
    "required": ["goal_id"],
    "additionalProperties": False,
}
_PARAM_CORREGIR_META_PLAN = {
    "type": "object",
    "properties": {
        "meta_id": {"type": "integer"},
        "valor": {"type": ["number", "string"]},
        "nota": {"type": "string"},
    },
    "required": ["meta_id", "valor"],
    "additionalProperties": False,
}
_PARAM_CORREGIR_FASE = {
    "type": "object",
    "properties": {
        "fase_id": {"type": "integer"},
        "titulo": {"type": "string"},
        "start_day": {"type": "string", "format": "date"},
        "end_day": {"type": "string", "format": "date"},
        "nota": {"type": ["string", "null"]},
        "quitar": {"type": "boolean"},
    },
    "required": ["fase_id"],
    "additionalProperties": False,
}
_PARAM_LEER_PLAN = {
    "type": "object",
    "properties": {
        "goal_id": {"type": "integer"},
        "desde": {"type": "string", "format": "date"},
        "hasta": {"type": "string", "format": "date"},
    },
    "required": ["goal_id"],
    "additionalProperties": False,
}
_PARAM_LEER_PENDIENTES_REGISTRO = {
    "type": "object",
    "properties": {"dia": {"type": "string", "format": "date"}},
    "additionalProperties": False,
}


def _readiness_dict(r: readiness.Readiness) -> dict:
    return {
        "dia": r.day, "puntaje": r.score, "banda": r.band, "version": r.version,
        "faltantes": r.missing,
        "partes": [{"clave": p.key, "valor": p.value, "referencia": p.reference,
                    "subpuntaje": p.subscore, "peso": p.weight} for p in r.parts],
    }


def _limitaciones_abiertas(conn: sqlite3.Connection, person_id: int, day: str) -> list[dict]:
    """BODY-LESION-ZONA-1 ruling 3: this person's own open periods of a kind whose lleva_zona=1
    (BODY-PLAN-FASES-4 ruling 3: 'lesion' or 'molestia' today, data, never a hardcoded kind string)
    as of `day`, as {tipo, desde, zonas} -- never the note (the same privacy ruling 2 already
    keeps). Read by leer_hoy and used as the context planear_entreno's own description leans on."""
    return [{"tipo": r["kind"], "desde": r["start_day"], "zonas": json.loads(r["zonas"]) if r["zonas"] else []}
            for r in db.periodos(conn, person_id, day, day) if r["_lleva_zona"] and r["end_day"] is None]


def _comidas_del_dia_con_id(conn: sqlite3.Connection, person_id: int, day: str) -> list[dict]:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: `db.comidas_del_dia`'s own grouped list, each entry
    carrying the id a later `corregir_comida` call needs -- `comida_id` for a real meal, or
    `ingesta_id` for a legacy stand-alone row (db.py is NOT edited; this is the second, bounded
    query the spec's own ruling allows). The id list is built from the SAME WHERE and ORDER BY
    `db.comidas_del_dia` groups with, so its Nth first-occurrence lines up with that function's own
    Nth entry without re-reading any descriptive column.

    BODY-COMIDA-HORA-2 (ORCH-CARLOS-DELEGATES-TIER-1-1, BODY-MINUTO-ANALISIS-7): each entry also
    gains `hora` ("HH:MM", the same `ts_local[11:16]` slice body/rutas/asistente.py's own
    `_hora_o_fecha_corta` already takes), beside `ts_local` which stays -- absent, never None, on
    an untimed meal."""
    comidas = db.comidas_del_dia(conn, person_id, day)
    for entrada in comidas:
        if entrada["ts_local"]:
            entrada["hora"] = entrada["ts_local"][11:16]
    filas = conn.execute(
        "SELECT i.id, i.comida_id FROM ingesta i WHERE i.person_id = ? AND i.day = ? "
        "ORDER BY COALESCE(i.ts_local, ''), i.id", (person_id, day),
    ).fetchall()
    orden: list = []
    vistos: set = set()
    for r in filas:
        clave = ("comida", r["comida_id"]) if r["comida_id"] is not None else ("ingesta", r["id"])
        if clave not in vistos:
            vistos.add(clave)
            orden.append(clave)
    for entrada, (tipo, id_) in zip(comidas, orden):
        entrada["comida_id" if tipo == "comida" else "ingesta_id"] = id_
    return comidas


def _leer_hoy(conn: sqlite3.Connection, person_id: int) -> dict:
    dias = db.days_with_data(conn, person_id)
    if not dias:
        return {"dia": None, "mensaje": "sin datos aún"}
    dia = dias[0]
    out = {"dia": dia, "listo": _readiness_dict(readiness.compute(conn, dia, person_id)),
           "valores": db.day_values(conn, dia, person_id),
           "limitaciones": _limitaciones_abiertas(conn, person_id, dia),
           "objetivos_nutriente": nutriente_objetivo.estado_del_dia(conn, person_id, dia)}
    # ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6) ruling 4: today's own reentrada/tras_carga
    # signals, present only when they apply (reentrada.senales' own "no key" omission) -- the same
    # context planear_entreno's own description leans on, next to `limitaciones` above.
    out.update(reentrada.senales(conn, person_id, date.fromisoformat(dia)))
    return out


def _leer_dia(conn: sqlite3.Connection, person_id: int, day: str) -> dict:
    data = db.load_day(conn, day, person_id)
    t = T(db.person(conn, person_id)["language"])
    # BODY-HISTORIA-VIDA-LUGARES-2-R ruling 4: this one day against the person's own cached
    # trips -- the same one-statement helper every trip-aware screen goes through (ruling 0).
    data["de_viaje"] = day in viajes.dias_de_viaje(conn, person_id, day, day)
    for a in data["activities"]:
        zonas = a.pop("zonas_limitadas", [])
        a["liviano_a_proposito"] = anatomia.texto_liviano_a_proposito(t, zonas) if zonas else None
    data["objetivos_nutriente"] = nutriente_objetivo.estado_del_dia(conn, person_id, day)
    # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: `data["ingesta"]` (from db.load_day) lists each
    # amount on its own with no description -- a logged meal's own words never reach it, since
    # registrar_comida stores them on the `comida` row, not on its `ingesta` children. `comidas`
    # adds `db.comidas_del_dia`'s own grouping (description, time, amounts per meal, a legacy
    # stand-alone ingesta row as its own one-item meal) -- kept ALONGSIDE `data["ingesta"]`, never
    # replacing it, since tests/test_proteina.py's own LeerDiaIncludesIngestaTest already reads
    # that list through this same tool.
    data["comidas"] = _comidas_del_dia_con_id(conn, person_id, day)
    # BODY-MINUTO-ANALISIS-5-CONTEXTO ruling 3: this day's own derived context -- the shift/night-
    # block standing, a trip or marked period, the day's activity load, its last-meal-to-sleep gap
    # and its alcohol figure, each key present only when that day actually has it (body/
    # contexto_dia.py's own module docstring). Never entered data -- every key here is derived on
    # read from tables the person already fed through Garmin, their shift schedule, Lugares or a
    # logged meal/period, never a new figure for them to type in.
    data["contexto"] = contexto_dia.contexto_dias(conn, person_id, day, day).get(day, {})
    # BODY-DESPERTAR-REAL-4 ruling 3, widened by BODY-DESPERTAR-REAL-6A-R2 ruling 5: the day's own
    # correction(s), next to Garmin's own sleep -- both shown, each labelled by kind. Only when
    # Garmin's own sleep exists (a correction cannot exist without one, ruling 2's own refusal at
    # creation), never a second source of truth for the sleep window itself.
    if data["sleep"] is not None:
        correcciones = db.despertar_correcciones(conn, person_id, day)
        if correcciones:
            data["sleep"] = {**data["sleep"], "correcciones": correcciones}
            # BODY-DESPERTAR-REAL-6B ruling 4: `data["metrics"]`' own sleep_minutes value already
            # came back corrected (db.load_day reads daily_metric_efectiva); Garmin's own total is
            # given alongside it, labelled, so the assistant never states only one figure.
            if "sueno" in correcciones:
                garmin_total = db.series_garmin(conn, "sleep_minutes", day, day, person_id).get(day)
                if garmin_total is not None:
                    data["sleep"]["sleep_minutes_garmin"] = garmin_total
    return data


def _leer_como_calculo(conn: sqlite3.Connection, person_id: int, day: str) -> dict:
    return _readiness_dict(readiness.compute(conn, day, person_id))


def _tendencia_metric(conn: sqlite3.Connection, person_id: int, key: str, rango: str, last_day: str | None) -> dict | None:
    # A self-measured key (origen 'self') has no watch-sized cadence to wait on, so it gets a
    # figure from its first value, same as the Trends screen's own _trend_card (BODY-AGENTE-
    # TENDENCIAS-CATALOGO-1) -- the default min_days=14 would otherwise hide flex_alcance_cm and
    # snoring_min from the assistant for two weeks after the person starts measuring them.
    min_days = 1 if key in SELF_METRIC_KEYS else 14
    p = profile.metric_profile(conn, key, person_id=person_id, min_days=min_days)
    if p is None:
        return None
    out = {"dias_con_datos": p.days, "primer_dia": p.first_day, "media_historica": p.all_mean,
           "p25": p.low, "p75": p.high, "media_30d": p.last30, "media_30d_previa": p.prev30}
    if rango in ("7", "30", "90") and last_day:
        end = date.fromisoformat(last_day)
        start = end - timedelta(int(rango) - 1)
        w = profile.window(conn, key, start, end, person_id)
        out["media_rango"], out["media_rango_previa"] = w.mean, w.prev_mean
    return out


def _leer_tendencias(conn: sqlite3.Connection, person_id: int, rango: str = "30", metric: str | None = None) -> dict:
    dias = db.days_with_data(conn, person_id)
    last_day = dias[0] if dias else None
    claves = (metric,) if metric else TENDENCIA_METRICAS
    metricas = {k: v for k in claves if (v := _tendencia_metric(conn, person_id, k, rango, last_day)) is not None}
    # BODY-TENDENCIAS-LEYENDA-PATRON-1 ruling 3: patron_turnos carries the same sentence SPEC
    # (kind + offsets + direction) Trends' own cards show, additive and only for a metric that is
    # both in the result above and has a reference in the shift-cost model -- shared once across
    # all of them (turnos_costo.perfiles_patron), never a read per metric.
    con_patron = [k for k in metricas if k in turnos_costo.MODELO["metricas"]]
    if con_patron:
        hoy = fechas.hoy_local(db.person(conn, person_id))
        perfiles = turnos_costo.perfiles_patron(conn, person_id, hoy)
        for k in con_patron:
            frase = turnos_costo.frase_patron(perfiles[k], k)
            if frase is not None:
                metricas[k]["patron_turnos"] = frase
    return {"rango": rango, "metricas": metricas}


def _agrupar_serie(filas: list, paso: str) -> list[dict]:
    """BODY-AGENTE-LEER-SERIE-1 ruling 1: 'dia' is one point per row read, already honest about
    gaps (a missing day is simply a row that was never there). 'semana'/'mes' group the SAME rows
    by the week's Monday (`fechas.lunes_de`) or the month's first day, averaging with
    `statistics.fmean` -- the exact function `body/profile.py`'s own `_build_profile` uses for
    Trends' monthly line, never a second averaging rule -- so a point here for a given month
    matches that screen's figure for the same key and month. A group with zero rows never exists
    in the first place (dict.setdefault only ever runs for a day that IS in `filas`), so a gap
    stays absent rather than becoming a 0."""
    if paso == "dia":
        return [{"fecha": r["day"], "valor": r["value"], "n_dias": 1} for r in filas]
    grupos: dict[str, list[float]] = {}
    for r in filas:
        if paso == "semana":
            clave = fechas.lunes_de(date.fromisoformat(r["day"])).isoformat()
        else:
            clave = r["day"][:7] + "-01"
        grupos.setdefault(clave, []).append(r["value"])
    return [{"fecha": clave, "valor": statistics.fmean(vals), "n_dias": len(vals)}
            for clave, vals in sorted(grupos.items())]


def _leer_serie(conn: sqlite3.Connection, person_id: int, nivel: str, metricas: list, desde: str, hasta: str,
                 paso: str) -> dict:
    """BODY-AGENTE-LEER-SERIE-1: one bounded range read of day-by-day figures over up to
    `_LEER_SERIE_MAX_METRICAS` metrics at once, so a whole watch history is a handful of calls
    inside bucle.MAX_LECTURAS instead of one leer_dia per day. Ruling 3: 'dia' needs
    nivel_datos='completo' (the SAME rule leer_dia itself enforces at the registry, reapplied here
    because this one tool's OWN registry nivel ('resumen') has to admit 'semana'/'mes' too); the
    caller is given `nivel` for exactly this reason -- `ejecutar()` passes it only to a tool whose
    registry entry asks for it (`necesita_nivel`), everybody else is unchanged. Ruling 2: refuses
    before touching the database whenever a bound is crossed, and reads `daily_metric` with
    exactly ONE statement, whatever `desde`..`hasta` spans. ORCH-CARLOS-DELEGATES-TIER-1-1 ruling
    1: paso='minuto' is a different shape entirely (a window of full timestamps over the minute
    streams, never a day-by-day point), dispatched to `_leer_serie_minuto` before any of the
    date-only parsing below, which would reject a full timestamp."""
    if paso == "minuto":
        return _leer_serie_minuto(conn, person_id, nivel, metricas, desde, hasta)
    if desconocidas := [m for m in metricas if m not in TENDENCIA_METRICAS]:
        raise ToolError(f"métrica(s) desconocida(s): {desconocidas}")
    try:
        desde_d, hasta_d = date.fromisoformat(desde), date.fromisoformat(hasta)
    except ValueError:
        raise ToolError("'desde'/'hasta' deben ser fechas ISO (AAAA-MM-DD)")
    if desde_d > hasta_d:
        raise ToolError(f"'desde' ({desde}) no puede ser posterior a 'hasta' ({hasta})")
    hoy = fechas.hoy_local(db.person(conn, person_id))
    if hasta_d > hoy:
        raise ToolError(f"'hasta' no puede ser una fecha futura: {hasta!r}")
    if paso == "dia":
        if nivel == "resumen":
            raise ToolError("leer_serie con paso='dia' necesita nivel_datos='completo'; con 'resumen' "
                             "usa paso='semana' o paso='mes'")
        dias_pedidos = (hasta_d - desde_d).days + 1
        if dias_pedidos > _LEER_SERIE_MAX_DIAS_PASO_DIA:
            raise ToolError(f"paso='dia' admite hasta {_LEER_SERIE_MAX_DIAS_PASO_DIA} días por llamada "
                             f"({dias_pedidos} pedidos); usa paso='semana'/'mes' o divide el rango")
    elif paso == "semana":
        semanas_pedidas = (fechas.lunes_de(hasta_d) - fechas.lunes_de(desde_d)).days // 7 + 1
        if semanas_pedidas > _LEER_SERIE_MAX_SEMANAS_PASO_SEMANA:
            raise ToolError(f"paso='semana' admite hasta {_LEER_SERIE_MAX_SEMANAS_PASO_SEMANA} semanas por "
                             f"llamada ({semanas_pedidas} pedidas); usa paso='mes' o divide el rango")
    placeholders = ",".join("?" * len(metricas))
    filas = conn.execute(
        f"SELECT metric_key, day, value FROM daily_metric_efectiva WHERE person_id = ? "
        f"AND metric_key IN ({placeholders}) AND day BETWEEN ? AND ? ORDER BY metric_key, day",
        (person_id, *metricas, desde, hasta),
    ).fetchall()
    por_metrica: dict[str, list] = {m: [] for m in metricas}
    for r in filas:
        por_metrica[r["metric_key"]].append(r)
    return {
        "paso": paso, "desde": desde, "hasta": hasta,
        "metricas": {m: {"unidad": _UNIDADES_METRICA.get(m), "puntos": _agrupar_serie(por_metrica[m], paso)}
                     for m in metricas},
    }


def _bateria_corporal_minuto(conn: sqlite3.Connection, person_id: int, day: str,
                              zona: str) -> tuple[tuple[str, float], ...]:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: one calendar day's own body-battery minute array,
    parsed on read from that day's stored 'body_battery' raw_payload -- the SAME extraction
    garmin_adapter's own high/low fallback uses (garmin_adapter.py:572-578: each sample is
    [timestamp_ms, value, ...], timestamp an epoch-ms UTC instant like every other intraday array
    in this family), kept as its own small parser HERE rather than in the adapter (never edited by
    this slice). () for a day never stored, a malformed payload, or one with no usable sample --
    never a crash, the same contract garmin_adapter.parse_sleep_series documents for its own
    arrays. Same source precedence as db.intradia: the live API's own row wins when both exist."""
    row = conn.execute(
        "SELECT payload FROM raw_payload WHERE person_id = ? AND day = ? AND kind = 'body_battery' "
        "ORDER BY (source != ?) LIMIT 1",
        (person_id, day, garmin_adapter.SOURCE),
    ).fetchone()
    if row is None:
        return ()
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return ()
    arr = payload[0].get("bodyBatteryValuesArray")
    if not isinstance(arr, list):
        return ()
    out = []
    for muestra in arr:
        if not isinstance(muestra, list) or len(muestra) <= 1:
            continue
        valor = garmin_adapter._num(muestra[1])
        if valor is None:
            continue
        local_time = garmin_adapter._utc_to_local_iso(muestra[0], zona)
        if local_time is None:
            continue
        out.append((local_time, valor))
    return tuple(out)


def _mas_reciente_dentro(ts_ordenados: list, muestras: list, minuto: datetime, tol_min: int):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the LAST (ts, value) in `muestras` (parallel to
    `ts_ordenados`, both sorted ascending) at or before `minuto`, kept only if within `tol_min`
    minutes -- vfc's own forward-fill, since HRV is commonly sampled every few minutes rather than
    every one. None past the tolerance, or with nothing at or before `minuto` at all."""
    idx = bisect.bisect_right(ts_ordenados, minuto)
    if idx == 0:
        return None
    ts, valor = muestras[idx - 1]
    return valor if minuto - ts <= timedelta(minutes=tol_min) else None


def _mas_cercano_dentro(ts_ordenados: list, muestras: list, minuto: datetime, tol_min: int):
    """The single NEAREST (ts, value) to `minuto` on EITHER side, kept only if within `tol_min`
    minutes -- body_battery's own match, since its sampling can land a little before or after the
    minute asked about rather than reliably before it."""
    idx = bisect.bisect_left(ts_ordenados, minuto)
    candidatos = []
    if idx < len(muestras):
        candidatos.append(muestras[idx])
    if idx > 0:
        candidatos.append(muestras[idx - 1])
    if not candidatos:
        return None
    ts, valor = min(candidatos, key=lambda par: abs(par[0] - minuto))
    return valor if abs(ts - minuto) <= timedelta(minutes=tol_min) else None


def _filas_leer_serie_minuto(conn: sqlite3.Connection, person_id: int, zona: str, metricas: list,
                              desde_dt: datetime, hasta_dt: datetime) -> list[dict]:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: one row per minute in [desde_dt, hasta_dt) that
    carries data on at least one requested stream, each key present only when THAT stream has a
    usable sample there. 'fc'/'estres'/'movimiento' are minute-exact joins (db.intradia/the
    night's own 'movement' array already carry one sample per minute); 'etapa' is the Garmin stage
    segment covering that minute (ETAPAS_SUENO_GARMIN, the same label leer_noche already shows,
    each segment's own end being the next segment's start or the night's own end); 'vfc'/
    'body_battery' are sparser, matched with `_mas_reciente_dentro`/`_mas_cercano_dentro`.

    Bounded statement count independent of window length (tests/test_leer_serie_minuto.py's own
    guard): the window is capped at `_LEER_SERIE_MAX_MIN_PASO_MINUTO` minutes (under 24h), so the
    calendar dates it and its own lookup margin touch are always at most two, and the SLEEP-NIGHT
    candidates (a night's own day may be either side of a calendar date -- Garmin files a night
    under its WAKE day) are that same small, fixed set read in ONE statement (an IN clause), never
    one query per candidate. `db.intradia`/`_bateria_corporal_minuto`/`db.sueno_minuto_a_minuto`
    each cost exactly one statement per calendar day or per overlapping night, never per minute."""
    margen = timedelta(minutes=max(_LEER_SERIE_MINUTO_TOL_VFC_MIN, _LEER_SERIE_MINUTO_TOL_BB_MIN))
    dias_ventana = sorted({desde_dt.date(), (hasta_dt - timedelta(minutes=1)).date()})
    dias_margen = sorted({(desde_dt - margen).date(), (hasta_dt + margen).date()})

    fc_por_ts: dict[str, float] = {}
    estres_por_ts: dict[str, float] = {}
    if "fc" in metricas:
        for dia in dias_ventana:
            for ts, v in db.intradia(conn, person_id, dia.isoformat(), "intradia_fc", zona):
                if desde_dt <= experimentos._sin_offset(ts) < hasta_dt:
                    fc_por_ts[ts] = v
    if "estres" in metricas:
        for dia in dias_ventana:
            for ts, v in db.intradia(conn, person_id, dia.isoformat(), "intradia_estres", zona):
                if desde_dt <= experimentos._sin_offset(ts) < hasta_dt:
                    estres_por_ts[ts] = v

    bb_muestras: list[tuple[datetime, float]] = []
    if "body_battery" in metricas:
        for dia in dias_margen:
            for ts, v in _bateria_corporal_minuto(conn, person_id, dia.isoformat(), zona):
                t = experimentos._sin_offset(ts)
                if desde_dt - margen <= t <= hasta_dt + margen:
                    bb_muestras.append((t, v))
        bb_muestras.sort(key=lambda par: par[0])
    bb_ts = [t for t, _ in bb_muestras]

    etapa_por_ts: dict[str, str] = {}
    movimiento_por_ts: dict[str, float] = {}
    vfc_muestras: list[tuple[datetime, float]] = []
    if {"etapa", "vfc", "movimiento"} & set(metricas):
        candidatos_noche = sorted({d + timedelta(days=delta) for d in dias_margen for delta in (0, 1)})
        placeholders = ",".join("?" * len(candidatos_noche))
        filas_noche = conn.execute(
            f"SELECT day, start_local, end_local FROM sleep_session WHERE person_id = ? "
            f"AND day IN ({placeholders})",
            (person_id, *[d.isoformat() for d in candidatos_noche]),
        ).fetchall()
        for fila in filas_noche:
            if not fila["start_local"] or not fila["end_local"]:
                continue
            inicio_noche = experimentos._sin_offset(fila["start_local"])
            fin_noche = experimentos._sin_offset(fila["end_local"])
            if fin_noche < desde_dt - margen or inicio_noche > hasta_dt + margen:
                continue
            arrays = db.sueno_minuto_a_minuto(conn, person_id, fila["day"], zona)
            if "movimiento" in metricas:
                for ts, v in arrays.get("movement", ()):
                    if desde_dt <= experimentos._sin_offset(ts) < hasta_dt:
                        movimiento_por_ts[ts] = v
            if "vfc" in metricas:
                for ts, v in arrays.get("hrv", ()):
                    t = experimentos._sin_offset(ts)
                    if desde_dt - margen <= t <= hasta_dt + margen:
                        vfc_muestras.append((t, v))
            if "etapa" in metricas:
                puntos = sorted(arrays.get("stages", ()), key=lambda par: par[0])
                for i, (ts, codigo) in enumerate(puntos):
                    seg_inicio = experimentos._sin_offset(ts)
                    seg_fin = (experimentos._sin_offset(puntos[i + 1][0]) if i + 1 < len(puntos)
                               else fin_noche)
                    ini, fin = max(seg_inicio, desde_dt), min(seg_fin, hasta_dt)
                    if ini >= fin:
                        continue
                    etiqueta = ETAPAS_SUENO_GARMIN.get(codigo, "desconocido")
                    minuto_etapa = ini
                    while minuto_etapa < fin:
                        etapa_por_ts[minuto_etapa.isoformat(timespec="minutes")] = etiqueta
                        minuto_etapa += timedelta(minutes=1)
    vfc_muestras.sort(key=lambda par: par[0])
    vfc_ts = [t for t, _ in vfc_muestras]

    filas: list[dict] = []
    minuto = desde_dt
    while minuto < hasta_dt:
        ts = minuto.isoformat(timespec="minutes")
        valores: dict = {}
        if ts in fc_por_ts:
            valores["fc"] = fc_por_ts[ts]
        if ts in estres_por_ts:
            valores["estres"] = estres_por_ts[ts]
        if ts in etapa_por_ts:
            valores["etapa"] = etapa_por_ts[ts]
        if ts in movimiento_por_ts:
            valores["movimiento"] = movimiento_por_ts[ts]
        if "vfc" in metricas:
            valor = _mas_reciente_dentro(vfc_ts, vfc_muestras, minuto, _LEER_SERIE_MINUTO_TOL_VFC_MIN)
            if valor is not None:
                valores["vfc"] = valor
        if "body_battery" in metricas:
            valor = _mas_cercano_dentro(bb_ts, bb_muestras, minuto, _LEER_SERIE_MINUTO_TOL_BB_MIN)
            if valor is not None:
                valores["body_battery"] = valor
        if valores:
            filas.append({"ts": ts, **valores})
        minuto += timedelta(minutes=1)
    return filas


def _leer_serie_minuto(conn: sqlite3.Connection, person_id: int, nivel: str, metricas: list, desde: str,
                        hasta: str) -> dict:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: Carlos's own words, relayed by the desk
    2026-11-20: "I want between you and body design specialized tools to analyze in full detail
    every minute of the metrics." A bounded window of full LOCAL timestamps (never a plain date,
    since the window may cross midnight) over the minute streams in LEER_SERIE_MINUTO_METRICAS --
    see `_filas_leer_serie_minuto`. Needs nivel_datos='completo', the same rule paso='dia' already
    enforces (the registry's own nivel is 'resumen' so 'semana'/'mes' stay reachable there)."""
    if nivel == "resumen":
        raise ToolError("leer_serie con paso='minuto' necesita nivel_datos='completo'; con 'resumen' "
                         "usa paso='semana' o paso='mes'")
    if desconocidas := [m for m in metricas if m not in LEER_SERIE_MINUTO_METRICAS]:
        raise ToolError(f"paso='minuto' solo admite estas métricas: {list(LEER_SERIE_MINUTO_METRICAS)} "
                         f"(desconocida(s): {desconocidas})")
    try:
        desde_dt = experimentos._sin_offset(desde).replace(second=0, microsecond=0)
        hasta_dt = experimentos._sin_offset(hasta).replace(second=0, microsecond=0)
    except ValueError:
        raise ToolError("'desde'/'hasta' deben ser fecha-hora ISO completas (AAAA-MM-DDTHH:MM) "
                         "con paso='minuto'")
    if hasta_dt <= desde_dt:
        raise ToolError("'hasta' debe ser posterior a 'desde'")
    minutos_pedidos = (hasta_dt - desde_dt).total_seconds() / 60
    if minutos_pedidos > _LEER_SERIE_MAX_MIN_PASO_MINUTO:
        raise ToolError(f"paso='minuto' admite hasta {_LEER_SERIE_MAX_MIN_PASO_MINUTO} minutos por "
                         f"llamada ({minutos_pedidos:.0f} pedidos); divide el rango")
    person = db.person(conn, person_id)
    zona = person["timezone"]
    ahora = datetime.now(ZoneInfo(zona)).replace(tzinfo=None, second=0, microsecond=0)
    if hasta_dt > ahora:
        raise ToolError("'hasta' no puede ser una fecha-hora futura")
    filas = _filas_leer_serie_minuto(conn, person_id, zona, metricas, desde_dt, hasta_dt)
    return {"paso": "minuto", "desde": desde, "hasta": hasta, "zona": zona, "filas": filas}


def _hora_local(valor: str):
    try:
        return datetime.strptime(valor, "%H:%M").time()
    except ValueError:
        raise ToolError(f"{valor!r} debe ser una hora local HH:MM")


def _motivo_intradia_sin_datos(conn: sqlite3.Connection, person_id: int, fecha: str, hoy: date) -> str:
    """BODY-DESPERTAR-REAL-2 ruling 1: the three honest reasons an empty window can have, read-only
    (never the refresh attempt itself, which `ejecutar()` already ran before this function sees the
    database -- see `pull.actualizar_intradia_si_corresponde`)."""
    dia_d = date.fromisoformat(fecha)
    if dia_d < hoy - timedelta(days=pull.INTRADIA_ALCANCE_DIAS):
        return "fuera_de_alcance"
    conectado = (garmin_cuenta.leer(conn, person_id) is not None) if cuenta.hosted() else garmin_adapter.has_token()
    return "no_traido" if conectado else "reloj_apagado"


def _leer_intradia(conn: sqlite3.Connection, person_id: int, fecha: str, desde: str, hasta: str,
                    actualizacion_fallo: bool = False) -> dict:
    """BODY-DESPERTAR-REAL-2 ruling 1: the heart-rate and stress points for a window of up to
    `_LEER_INTRADIA_MAX_MIN` inside one day (local time), plus -- for the part of the window inside
    Garmin's own sleep session -- the sleep minute arrays and Garmin's sleep start/end for that day.
    `desde`/`hasta` are local HH:MM; every minute without a stored value is simply absent, never
    a 0 (db.intradia/db.sueno_minuto_a_minuto's own contract). `actualizacion_fallo` is set by
    `ejecutar()` (ruling 2's on-demand refresh, never run from here -- a `lectura` tool must never
    write)."""
    person = db.person(conn, person_id)
    zona = person["timezone"]
    t = T(person["language"])
    hoy = fechas.hoy_local(person)
    try:
        dia_d = date.fromisoformat(fecha)
    except ValueError:
        raise ToolError("'fecha' debe ser una fecha ISO (AAAA-MM-DD)")
    if dia_d > hoy:
        raise ToolError("'fecha' no puede ser una fecha futura")
    desde_t, hasta_t = _hora_local(desde), _hora_local(hasta)
    # Normalized to zero-padded "HH:MM" -- the form every stored timestamp's own [11:16] slice
    # already carries -- so a caller's "6:00" compares correctly against "06:05", not just "06:00".
    desde, hasta = desde_t.strftime("%H:%M"), hasta_t.strftime("%H:%M")
    if hasta_t <= desde_t:
        raise ToolError("'hasta' debe ser posterior a 'desde'")
    minutos = (hasta_t.hour * 60 + hasta_t.minute) - (desde_t.hour * 60 + desde_t.minute)
    if minutos > _LEER_INTRADIA_MAX_MIN:
        raise ToolError(f"leer_intradia admite hasta {_LEER_INTRADIA_MAX_MIN // 60} horas por llamada "
                         f"({minutos / 60:.1f} horas pedidas); divide el rango")

    # BODY-NOCHE-LECTURA-1 ruling 1 (verdict: REAL): filtering by ts[11:16] alone, with no date
    # check, let a sleep array's own minute from the CALENDAR DATE BEFORE `fecha` (Garmin's sleep
    # payload spans two dates; db.intradia's own fc/estres rows never do) pass as if it belonged
    # to this day's own window whenever its clock time happened to fall inside [desde, hasta) --
    # tests/test_leer_intradia.py's own CrossMidnightSleepTest proves it. Filtering the FULL local
    # timestamp (date + time) closes it for every series here, fc/estres included, at no cost: both
    # already carry `fecha` as their own date (BODY-INTRADIA-SPOTCHECK-1's own day-scoped fetch).
    def _en_ventana(ts: str) -> bool:
        return ts[:10] == fecha and desde <= ts[11:16] < hasta

    fc = [[ts[11:16], v] for ts, v in db.intradia(conn, person_id, fecha, "intradia_fc", zona) if _en_ventana(ts)]
    estres = [[ts[11:16], v] for ts, v in db.intradia(conn, person_id, fecha, "intradia_estres", zona)
              if _en_ventana(ts)]

    fila_sueno = conn.execute(
        "SELECT start_local, end_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, fecha),
    ).fetchone()
    sueno = None
    if fila_sueno is not None and fila_sueno["start_local"] and fila_sueno["end_local"]:
        arrays = db.sueno_minuto_a_minuto(conn, person_id, fecha, zona)
        sueno_ventana = {nombre: filtrado for nombre, serie in arrays.items()
                          if (filtrado := [[ts[11:16], v] for ts, v in serie if _en_ventana(ts)])}
        if sueno_ventana:
            sueno = {"inicio": fila_sueno["start_local"], "fin": fila_sueno["end_local"], **sueno_ventana}
    # BODY-DESPERTAR-REAL-4 ruling 3, widened by BODY-DESPERTAR-REAL-6A-R2 ruling 5: the day's own
    # correction(s), next to Garmin's own sleep window above -- both shown, each labelled by kind.
    if sueno is not None:
        correcciones = db.despertar_correcciones(conn, person_id, fecha)
        if correcciones:
            sueno["correcciones"] = correcciones

    out = {"dia": fecha, "desde": desde, "hasta": hasta, "zona": zona,
           "frecuencia_cardiaca": fc, "estres": estres, "sueno": sueno}
    if not fc and not estres and sueno is None:
        motivo = _motivo_intradia_sin_datos(conn, person_id, fecha, hoy)
        out["sin_datos"] = {"motivo": motivo, "mensaje": t("asistente.intradia.sin_datos." + motivo)}
    if actualizacion_fallo:
        out["actualizacion"] = t("asistente.intradia.no_actualizado")
    return out


def _fc_minima_noche(sueno_arrays: dict, inicio_dt: datetime, fin_dt: datetime) -> dict | None:
    """BODY-NOCHE-LECTURA-1 ruling 2: the single lowest heart-rate minute strictly inside Garmin's
    own sleep window [inicio_dt, fin_dt] -- never the window's own padding (ruling 2's before/after
    margin), and never sueno_fc.py's own robust 10th-percentile `sleep_hr_min` (a different, already
    -smoothed figure for Tendencias/baselines); this one keeps the actual minute and its own hora,
    the thing the surface asked for. None when the sleep payload carries no heart_rate array, or
    none of it falls inside the window."""
    candidatos = [(v, ts) for ts, v in (sueno_arrays.get("heart_rate") or ())
                  if inicio_dt <= experimentos._sin_offset(ts) <= fin_dt]
    if not candidatos:
        return None
    valor, hora = min(candidatos, key=lambda par: par[0])
    return {"valor": valor, "hora": hora}


def _minutos_despierto_tras_fin(resultado_modelo: dict) -> float | None:
    """BODY-NOCHE-LECTURA-1 ruling 2: how many minutes past Garmin's own sleep end
    `despertar_real.estimar` still read as 'dormitando' (dozing, still asleep) before its own
    first non-sleep segment -- 0.0 when the model's first segment is already something else (an
    immediate, confirmed wake), None when the model exposes no segment at all (ruling 4's honest
    'sin_linea_base'/'sin_sesion' gap -- nothing to derive this from)."""
    segmentos = resultado_modelo.get("segmentos")
    if not segmentos:
        return None
    primero = segmentos[0]
    if primero["tipo"] != "dormitando":
        return 0.0
    delta = datetime.fromisoformat(primero["fin"]) - datetime.fromisoformat(primero["inicio"])
    return round(delta.total_seconds() / 60, 1)


def _motivo_sin_rango_30_dias(conn: sqlite3.Connection, person_id: int, fecha: str, key: str) -> str:
    """BODY-NOCHE-LECTURA-2 ruling 1: the two honest reasons `rango_30_dias` can come back null --
    'pocos_dias' when there were not even `baselines.MIN_DAYS` values in the 30-day window BEFORE
    marked days were excluded, 'dias_marcados' when there were and the exclusion (BODY-PERIODO-1
    ruling 3) is what emptied it. One extra COUNT over the same window baselines.baseline already
    used, never the per-day exclusion math baselines.py owns."""
    d = date.fromisoformat(fecha)
    desde = (d - timedelta(baselines.WINDOW_DAYS)).isoformat()
    hasta = (d - timedelta(1)).isoformat()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = ? "
        "AND day BETWEEN ? AND ?", (person_id, key, desde, hasta),
    ).fetchone()["n"]
    return "pocos_dias" if total < baselines.MIN_DAYS else "dias_marcados"


def _rango_doble_y_z(conn: sqlite3.Connection, person_id: int, fecha: str, key: str,
                      min_days_historia: int, valor: float) -> dict:
    """BODY-NOCHE-LECTURA-2 ruling 1: both of this metric's own usual ranges for one night, each
    independently None -- `rango_30_dias` is `baselines.baseline` unchanged (the 30 days before
    this night, marked days excluded); `rango_historia` is `profile.metric_profile` unchanged (the
    SAME figure the person's own Tendencias card shows for this key, never excluding marked days)
    -- never a second range computed here. `z` prefers `rango_30_dias`'s own `.z()`; when that one
    is null, the fallback reuses `baselines.baseline` again over `_VENTANA_HISTORIA_DIAS` -- a
    spread to measure against without ever changing what `rango_historia` itself shows. `z_base`
    names which range `z` actually came from, or None with neither."""
    base_30 = baselines.baseline(conn, key, fecha, person_id=person_id)
    perfil = profile.metric_profile(conn, key, person_id=person_id, min_days=min_days_historia)
    out = {
        "rango_30_dias": {"bajo": base_30.low, "alto": base_30.high} if base_30 is not None else None,
        "rango_historia": {"bajo": perfil.low, "alto": perfil.high, "dias": perfil.days}
                           if perfil is not None else None,
    }
    if base_30 is not None:
        out["z"], out["z_base"] = base_30.z(valor), "rango_30_dias"
        return out
    out["motivo_sin_rango_30_dias"] = _motivo_sin_rango_30_dias(conn, person_id, fecha, key)
    base_amplio = baselines.baseline(conn, key, fecha, window=_VENTANA_HISTORIA_DIAS, person_id=person_id)
    out["z"] = base_amplio.z(valor) if base_amplio is not None else None
    out["z_base"] = "rango_historia" if base_amplio is not None else None
    return out


def _verificar_etapas(sueno_arrays: dict, fin_dt: datetime, valores_dia: dict) -> dict:
    """BODY-NOCHE-LECTURA-2 ruling 2: whether `ETAPAS_SUENO_GARMIN`'s assumed mapping holds for
    THIS night -- minutes summed per label from the raw 'stages' array (each point's own span to
    the next one, the LAST point's own span to `fin_dt`, since Garmin's own segments carry no end
    time here -- garmin_adapter.parse_sleep_series keeps only each segment's start) against the
    night's own stored totals (sleep_deep_min/sleep_light_min/sleep_rem_min/sleep_awake_min -- the
    SAME keys the day page's own stage bar reads, body/rutas/hoy.py's STAGES). null when a stored
    total is missing -- never invented from the array alone. A 'desconocido' stretch counts toward
    no total, on either side of the comparison."""
    totales = {metrica: valores_dia.get(metrica) for metrica in _ETAPA_METRICA.values()}
    if any(v is None for v in totales.values()):
        return {"coincide": None, "diferencia_min_max": None}
    puntos = sorted(sueno_arrays.get("stages") or (), key=lambda par: par[0])
    minutos = {metrica: 0.0 for metrica in _ETAPA_METRICA.values()}
    for i, (ts, codigo) in enumerate(puntos):
        inicio = experimentos._sin_offset(ts)
        fin = experimentos._sin_offset(puntos[i + 1][0]) if i + 1 < len(puntos) else fin_dt
        metrica = _ETAPA_METRICA.get(ETAPAS_SUENO_GARMIN.get(codigo, "desconocido"))
        if metrica is not None:
            minutos[metrica] += max(0.0, (fin - inicio).total_seconds() / 60)
    diferencia = max(abs(minutos[m] - totales[m]) for m in minutos)
    return {"coincide": diferencia <= 5, "diferencia_min_max": round(diferencia, 1)}


def _derivados_noche(conn: sqlite3.Connection, person_id: int, fecha: str, sueno_arrays: dict,
                      inicio_dt: datetime, fin_dt: datetime) -> dict:
    """BODY-NOCHE-LECTURA-1 ruling 2 (BODY-NOCHE-LECTURA-2 ruling 1 widens the two range facts and
    adds `etapas_verificacion`; ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1 splits the single minute
    from its comparison): the facts Body already knows how to compute for this one night, each
    independently `None` when ITS OWN input is missing -- never invented, and never gated on
    whether a sibling fact could be computed. `fc_minima` is the single lowest HEART-RATE MINUTE
    inside Garmin's own window (`_fc_minima_noche`) and carries NO range of its own -- a single
    sample is never compared against sueno_fc.py's own ROBUST low (a percentile of hundreds of
    samples), the apples-to-oranges bug a real read surfaced (the desk, relayed 2026-11-20).
    `fc_baja_robusta` is that SAME robust figure for this wake day, read from
    `daily_metric_efectiva` (`valores_dia`, the same value `leer_serie`/`leer_dia` already show,
    tagged 'estimated') -- `fc_baja_robusta_vs_rango` is the one comparison against the usual
    range, moved here from the single minute. `fc_baja_robusta_vs_rango` and `vfc_noche`'s own
    ranges reuse `baselines.baseline` and `profile.metric_profile` (see `_rango_doble_y_z`), the
    SAME helpers every screen that shows a person's own normal already calls -- never a second
    range computed here. `despertar_modelo` is `despertar_real.estimar`'s own, unedited dict
    (body/despertar_real.py) -- never reimplemented."""
    fc_minima = _fc_minima_noche(sueno_arrays, inicio_dt, fin_dt)

    valores_dia = db.day_values(conn, fecha, person_id)
    # BODY-NOCHE-SIN-REM-3: the RAW stage totals (db.valores_etapas_garmin), never valores_dia --
    # a marked night's own sleep_rem_min row is now OMITTED from daily_metric_efectiva, so feeding
    # the view's dict here would silently stop both `etapas_verificacion` and `captura` from ever
    # seeing the 0 they exist to check.
    raw_etapas = db.valores_etapas_garmin(conn, fecha, person_id)
    fc_baja_robusta = valores_dia.get(sueno_fc.CLAVE_MIN)
    fc_baja_robusta_vs_rango = None
    if fc_baja_robusta is not None:
        min_days = 1 if sueno_fc.CLAVE_MIN in SELF_METRIC_KEYS else 14
        fc_baja_robusta_vs_rango = _rango_doble_y_z(conn, person_id, fecha, sueno_fc.CLAVE_MIN,
                                                      min_days, fc_baja_robusta)

    vfc_valor = valores_dia.get("hrv_last_night")
    vfc_noche = None
    if vfc_valor is not None:
        min_days = 1 if "hrv_last_night" in SELF_METRIC_KEYS else 14
        vfc_noche = {"valor": vfc_valor,
                     **_rango_doble_y_z(conn, person_id, fecha, "hrv_last_night", min_days, vfc_valor)}

    despertar_modelo = despertar_real.estimar(conn, person_id, fecha)

    return {
        "fc_minima": fc_minima,
        "fc_baja_robusta": fc_baja_robusta,
        "fc_baja_robusta_vs_rango": fc_baja_robusta_vs_rango,
        "vfc_noche": vfc_noche,
        "despertar_modelo": despertar_modelo,
        "minutos_despierto_tras_fin": _minutos_despierto_tras_fin(despertar_modelo),
        "etapas_verificacion": _verificar_etapas(sueno_arrays, fin_dt, raw_etapas),
        # BODY-NOCHE-SIN-REM-1 (BODY-NOCHE-SIN-REM-3: now fed raw_etapas, not valores_dia -- see
        # above): whether this night's own zero REM is a capture gap (the watch's fit or signal),
        # never a reading -- None when REM is missing, above zero, or the night is too short to
        # expect a full cycle.
        "captura": captura_sueno.evaluar(raw_etapas),
    }


def _leer_noche(conn: sqlite3.Connection, person_id: int, fecha: str, actualizacion_fallo: bool = False) -> dict:
    """BODY-NOCHE-LECTURA-1 ruling 2: Carlos's own words (relayed by the desk 2026-11-19): "please,
    can you check my sleep each day more deeply ... Can you analyze and check my heart rate minute
    by minute and tell me interesting stuff? ... So I'm wide with several calls per night. I was
    thinking on just one big reading, and there it will be all the info." ONE call for the whole
    night Garmin files under `fecha` (the WAKE day): the window from Garmin's own sleep start minus
    `_LEER_NOCHE_ANTES_MIN` to its own end plus `_LEER_NOCHE_DESPUES_MIN`, capped at
    `_LEER_NOCHE_MAX_MIN` and crossing midnight on purpose -- the heart-rate/stress minute points in
    it, each with its OWN FULL local timestamp (ruling 1: never just HH:MM, since this window spans
    two calendar dates), the sleep minute arrays, Garmin's own start/end, the person's own
    correction(s), and the derived facts (`_derivados_noche`). No sleep session at all for `fecha`
    -> `sin_datos`, the SAME three reasons (`MOTIVOS_INTRADIA_SIN_DATOS`) leer_intradia already
    gives, read by the same `_motivo_intradia_sin_datos`. `actualizacion_fallo` set by `ejecutar()`
    exactly like leer_intradia's own (never run from here -- a `lectura` tool must never write)."""
    person = db.person(conn, person_id)
    zona = person["timezone"]
    t = T(person["language"])
    hoy = fechas.hoy_local(person)
    try:
        dia_d = date.fromisoformat(fecha)
    except ValueError:
        raise ToolError("'fecha' debe ser una fecha ISO (AAAA-MM-DD)")
    if dia_d > hoy:
        raise ToolError("'fecha' no puede ser una fecha futura")

    fila_sueno = conn.execute(
        "SELECT start_local, end_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, fecha),
    ).fetchone()
    if fila_sueno is None or not fila_sueno["start_local"] or not fila_sueno["end_local"]:
        motivo = _motivo_intradia_sin_datos(conn, person_id, fecha, hoy)
        out = {"dia": fecha, "zona": zona,
               "sin_datos": {"motivo": motivo, "mensaje": t("asistente.intradia.sin_datos." + motivo)}}
        if actualizacion_fallo:
            out["actualizacion"] = t("asistente.intradia.no_actualizado")
        return out

    sueno_inicio_dt = experimentos._sin_offset(fila_sueno["start_local"])
    sueno_fin_dt = experimentos._sin_offset(fila_sueno["end_local"])
    ventana_inicio_dt = sueno_inicio_dt - timedelta(minutes=_LEER_NOCHE_ANTES_MIN)
    ventana_fin_dt = min(sueno_fin_dt + timedelta(minutes=_LEER_NOCHE_DESPUES_MIN),
                          ventana_inicio_dt + timedelta(minutes=_LEER_NOCHE_MAX_MIN))

    def _en_ventana(ts: str) -> bool:
        return ventana_inicio_dt <= experimentos._sin_offset(ts) <= ventana_fin_dt

    # BODY-NOCHE-LECTURA-1 ruling 3: at most two calendar dates regardless of the window's own
    # span (capped under 24h), so this stays two statements per series whether the night is 2
    # hours or the full 16-hour cap -- tests/test_leer_noche.py's own statement-count guard.
    dias = sorted({ventana_inicio_dt.date().isoformat(), ventana_fin_dt.date().isoformat()})
    fc: list = []
    estres: list = []
    for dia in dias:
        fc.extend([ts, v] for ts, v in db.intradia(conn, person_id, dia, "intradia_fc", zona) if _en_ventana(ts))
        estres.extend([ts, v] for ts, v in db.intradia(conn, person_id, dia, "intradia_estres", zona)
                       if _en_ventana(ts))
    fc.sort(key=lambda p: p[0])
    estres.sort(key=lambda p: p[0])

    sueno_arrays = db.sueno_minuto_a_minuto(conn, person_id, fecha, zona)
    # BODY-NOCHE-LECTURA-2 ruling 2: 'stages' carries Garmin's own code AND its assumed label
    # together ([hora, codigo, etiqueta]) -- every other array keeps its plain [hora, valor] shape.
    sueno = {"inicio": fila_sueno["start_local"], "fin": fila_sueno["end_local"],
             **{nombre: ([[ts, v, ETAPAS_SUENO_GARMIN.get(v, "desconocido")] for ts, v in serie]
                         if nombre == "stages" else [[ts, v] for ts, v in serie])
                for nombre, serie in sueno_arrays.items()}}
    correcciones = db.despertar_correcciones(conn, person_id, fecha)
    if correcciones:
        sueno["correcciones"] = correcciones

    out = {"dia": fecha, "zona": zona, "desde": ventana_inicio_dt.isoformat(), "hasta": ventana_fin_dt.isoformat(),
           "frecuencia_cardiaca": fc, "estres": estres, "sueno": sueno,
           # BODY-MINUTO-ANALISIS-5-CONTEXTO ruling 3: this night's own derived context -- the
           # EVENING's shift, load and alcohol (the day that produced this night, body/
           # contexto_dia.py's own module docstring), plus any trip/period covering either
           # calendar date and the gap from that evening's last meal to this night's own sleep.
           # Never entered data -- derived on read, same promise leer_dia's own `contexto` carries.
           "contexto": contexto_dia.contexto_noche(conn, person_id, fecha),
           **_derivados_noche(conn, person_id, fecha, sueno_arrays, sueno_inicio_dt, sueno_fin_dt)}
    if actualizacion_fallo:
        out["actualizacion"] = t("asistente.intradia.no_actualizado")
    return out


def _comparar_noche(conn: sqlite3.Connection, person_id: int, fecha: str, desde: str | None = None,
                     grupo: dict | None = None) -> dict:
    """BODY-MINUTO-ANALISIS-2 (ORCH-CARLOS-DELEGATES-TIER-1-1): one night's own features ranked
    against this person's own history -- body/noche_rasgos.py does every statement; this wrapper
    only validates the agent-facing date fields the same way `_leer_noche` does, and turns a night
    with nothing at all to rank into the same kind of ToolError every other lectura tool raises."""
    person = db.person(conn, person_id)
    t = T(person["language"])
    hoy = fechas.hoy_local(person)
    try:
        dia_d = date.fromisoformat(fecha)
    except ValueError:
        raise ToolError("'fecha' debe ser una fecha ISO (AAAA-MM-DD)")
    if dia_d > hoy:
        raise ToolError("'fecha' no puede ser una fecha futura")
    if desde is not None:
        try:
            date.fromisoformat(desde)
        except ValueError:
            raise ToolError("'desde' debe ser una fecha ISO (AAAA-MM-DD)")
    if grupo:
        extra = sorted(set(grupo) - set(noche_rasgos.GRUPO_FILTRO_CLAVES))
        if extra:
            raise ToolError(f"'grupo' tiene clave(s) no reconocida(s): {extra}")
        if ("altitud_cambio_min" in grupo and "altitud_cambio_max" in grupo
                and grupo["altitud_cambio_min"] > grupo["altitud_cambio_max"]):
            raise ToolError("'grupo.altitud_cambio_min' no puede ser mayor que 'grupo.altitud_cambio_max'")
    try:
        out = noche_rasgos.comparar(conn, person_id, fecha, desde=desde, grupo=grupo)
    except noche_rasgos.SinDatos:
        raise ToolError(t("agente.error.comparar_noche_sin_noche"))
    # BODY-NOCHE-SIN-REM-1 (BODY-NOCHE-SIN-REM-3: db.valores_etapas_garmin, the RAW totals, not
    # db.day_values -- a marked night's row is omitted from the view it reads): the SAME capture-
    # gap key `_leer_noche` carries, for this one focal night (`fecha`) -- `noche_rasgos.comparar`
    # ranks features, not raw totals, so this reads the night's own stored values the same way
    # `_derivados_noche` does, no change to that module.
    out["captura"] = captura_sueno.evaluar(db.valores_etapas_garmin(conn, fecha, person_id))
    return out


def _buscar_noches(conn: sqlite3.Connection, person_id: int, desde: str | None = None, hasta: str | None = None,
                    filtros: list[dict] | None = None, grupo: dict | None = None, orden: dict | None = None,
                    limite: int | None = None, rasgos: list[str] | None = None) -> dict:
    """BODY-MINUTO-ANALISIS-3 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2): the REVERSE of
    comparar_noche -- WHICH of this person's own nights match, not how one night ranks.
    body/noche_rasgos.py's `buscar` does every statement; this wrapper validates every agent-facing
    field BY NAME before it ever runs, the same way `_comparar_noche` already validates its own
    'grupo' against `noche_rasgos.GRUPO_CLAVES`."""
    person = db.person(conn, person_id)
    hoy = fechas.hoy_local(person)
    if hasta is not None:
        try:
            hasta_d = date.fromisoformat(hasta)
        except ValueError:
            raise ToolError("'hasta' debe ser una fecha ISO (AAAA-MM-DD)")
        if hasta_d > hoy:
            raise ToolError("'hasta' no puede ser una fecha futura")
    else:
        hasta_d = hoy
        hasta = hasta_d.isoformat()
    if desde is not None:
        try:
            desde_d = date.fromisoformat(desde)
        except ValueError:
            raise ToolError("'desde' debe ser una fecha ISO (AAAA-MM-DD)")
        if desde_d > hasta_d:
            raise ToolError("'desde' no puede ser posterior a 'hasta'")
    else:
        desde = (hasta_d - timedelta(noche_rasgos.HISTORIA_DIAS_DEFECTO)).isoformat()

    for f in (filtros or []):
        if f["rasgo"] not in noche_rasgos.RASGOS_CLAVES:
            raise ToolError(f"'filtros' tiene un rasgo desconocido: {f['rasgo']!r}")
        if f["op"] not in noche_rasgos.OPS_BUSCAR_NOCHES:
            raise ToolError(f"'filtros' tiene un operador desconocido: {f['op']!r}")
    if orden is not None and orden["rasgo"] not in noche_rasgos.RASGOS_CLAVES:
        raise ToolError(f"'orden' tiene un rasgo desconocido: {orden['rasgo']!r}")
    for nombre in (rasgos or []):
        if nombre not in noche_rasgos.RASGOS_CLAVES:
            raise ToolError(f"'rasgos' tiene un rasgo desconocido: {nombre!r}")
    if grupo:
        extra = sorted(set(grupo) - set(noche_rasgos.GRUPO_FILTRO_CLAVES))
        if extra:
            raise ToolError(f"'grupo' tiene clave(s) no reconocida(s): {extra}")
        if ("altitud_cambio_min" in grupo and "altitud_cambio_max" in grupo
                and grupo["altitud_cambio_min"] > grupo["altitud_cambio_max"]):
            raise ToolError("'grupo.altitud_cambio_min' no puede ser mayor que 'grupo.altitud_cambio_max'")

    limite = limite if limite is not None else _BUSCAR_NOCHES_LIMITE_DEFECTO
    if not (1 <= limite <= _BUSCAR_NOCHES_LIMITE_MAX):
        raise ToolError(f"'limite' debe estar entre 1 y {_BUSCAR_NOCHES_LIMITE_MAX}")

    return noche_rasgos.buscar(conn, person_id, desde, hasta, filtros=filtros, grupo=grupo, orden=orden,
                                limite=limite, rasgos=rasgos)


def _despertar_hora_opuesta(conn: sqlite3.Connection, person_id: int, fecha: str, tipo_opuesto: str,
                             correcciones: dict) -> datetime | None:
    """BODY-DESPERTAR-REAL-6A-R2 ruling 3: the OTHER tipo's own hora for this day, whether already
    applied (`correcciones`, read once by the caller) or still a PENDING, unapplied propuesta
    ('co-proposed') -- the ordering check below must see either."""
    fila = correcciones.get(tipo_opuesto)
    if fila is not None:
        return datetime.combine(date.fromisoformat(fecha), _hora_local(fila["hora_local"]))
    filas = conn.execute(
        "SELECT despues_json FROM propuesta_agente WHERE person_id = ? AND accion = 'corregir_despertar' "
        "AND estado = 'propuesta' ORDER BY creada_en DESC", (person_id,),
    ).fetchall()
    for f in filas:
        despues = json.loads(f["despues_json"])
        if despues.get("fecha") == fecha and despues.get("tipo") == tipo_opuesto:
            return datetime.combine(date.fromisoformat(fecha), _hora_local(despues["hora_local"]))
    return None


def _corregir_despertar(conn: sqlite3.Connection, person_id: int, conversacion_id: int, fecha: str, tipo: str,
                         hora: str) -> dict:
    """Proposes Body's own correction to Garmin's own recorded sleep for one night (Carlos's own
    ruling, dev-protocol DECISIONS.md BODY-DESPERTAR-REAL-RULING: the evidence decides, dozing
    read as real sleep, quiet awake time in bed only bounds the get-up time, Garmin's own record
    never edited). 'sueno' extends sleep to `hora`; 'levantado' names the actual get-up time.
    Refuses a day with no Garmin sleep, an `hora` at or before Garmin's own recorded end, past
    despertar_real.MODELO['horizonte_horas'] (the same horizon despertar_real.estimar itself never
    looks past), or one tipo's hora crossing the OTHER tipo's own hora for that day --
    BODY-DESPERTAR-REAL-6A-R2 ruling 3: a 'levantado' hora earlier than an existing or co-proposed
    'sueno' hora, or a 'sueno' hora later than an existing or co-proposed 'levantado' hora. The
    proposal carries despertar_real.estimar's own classification and confidence for that day
    (BODY-DESPERTAR-REAL-4 ruling 2), for the card's own evidence line. `antes` carries the day's
    own EXISTING correction for THIS tipo, if any, so `deshacer` restores exactly that row, or
    removes it when there was none -- the OTHER tipo's own row is never touched."""
    _dia_no_futuro(conn, person_id, fecha)
    t = T(db.person(conn, person_id)["language"])
    fila = conn.execute(
        "SELECT end_local FROM sleep_session WHERE person_id = ? AND day = ?", (person_id, fecha),
    ).fetchone()
    if fila is None or fila["end_local"] is None:
        raise ToolError(t("agente.error.despertar_sin_sueno"))
    sueno_fin = fila["end_local"]
    sleep_end_dt = experimentos._sin_offset(sueno_fin)
    hora_t = _hora_local(hora)
    hora_dt = datetime.combine(date.fromisoformat(fecha), hora_t)
    if hora_dt <= sleep_end_dt:
        raise ToolError(t("agente.error.despertar_hora_no_posterior"))
    horizonte_fin_dt = sleep_end_dt + timedelta(hours=despertar_real.MODELO["horizonte_horas"])
    if hora_dt > horizonte_fin_dt:
        raise ToolError(t("agente.error.despertar_hora_fuera_horizonte"))
    correcciones = db.despertar_correcciones(conn, person_id, fecha)
    tipo_opuesto = "levantado" if tipo == "sueno" else "sueno"
    hora_opuesta_dt = _despertar_hora_opuesta(conn, person_id, fecha, tipo_opuesto, correcciones)
    if hora_opuesta_dt is not None:
        if (tipo == "levantado" and hora_dt < hora_opuesta_dt) or (tipo == "sueno" and hora_dt > hora_opuesta_dt):
            raise ToolError(t("agente.error.despertar_levantado_antes_de_sueno"))
    resultado = despertar_real.estimar(conn, person_id, fecha)
    antes_fila = correcciones.get(tipo)
    antes = {"existia": antes_fila is not None, **(antes_fila or {})}
    despues = {"fecha": fecha, "tipo": tipo, "hora_local": hora_dt.strftime("%H:%M"),
               "modelo_version": despertar_real.MODELO["version"], "sueno_fin": sueno_fin,
               "confianza": resultado["confianza"], "evidencia": resultado["evidencia"]}
    registro = propuestas.crear(conn, person_id, conversacion_id, "corregir_despertar", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _leer_historia(conn: sqlite3.Connection, person_id: int) -> dict:
    out = {"cobertura": history.coverage(conn, person_id), "entrenamiento": history.training(conn, person_id),
           "patrones": history.patterns(conn, person_id)}
    if db.trabaja_turnos_si(conn, person_id):
        out["por_turno"] = history.by_shift(conn, person_id)
    return out


def _ultima_verificacion(conn: sqlite3.Connection, goal_id: int, person_id: int):
    row = conn.execute(
        "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? ORDER BY week_start DESC LIMIT 1",
        (goal_id, person_id),
    ).fetchone()
    return dict(row) if row else "sin verificación aún"


def _leer_metas(conn: sqlite3.Connection, person_id: int) -> dict:
    """Own goals plus any household goal shared with this person (BODY-HOGAR-MIEMBRO-1 rule 7) --
    `goals.goal_scope_sql`, the same scope body/web.py's goals_list uses. Only this person's OWN
    weekly check (`_ultima_verificacion` is keyed by `person_id`), never another member's."""
    hh = household.household_of(conn, person_id)
    where, params = goals.goal_scope_sql(person_id, hh)
    rows = conn.execute(f"SELECT * FROM goal WHERE {where} AND status = 'active' ORDER BY id", params).fetchall()
    return {"metas": [{"id": g["id"], "titulo": g["title"], "tipo": g["kind"], "area": g["area"],
                       "compartida": g["visibility"] == "household",
                       "ultima_verificacion": _ultima_verificacion(conn, g["id"], person_id)} for g in rows]}


def _habito_asignado_a(conn: sqlite3.Connection, person_id: int, h) -> str:
    if h["assignee_person_id"] is None:
        return "ambos"
    if h["assignee_person_id"] == person_id:
        return "tú"
    other = conn.execute("SELECT display_name FROM person WHERE id = ?", (h["assignee_person_id"],)).fetchone()
    return other["display_name"] if other else "?"


def _leer_meta(conn: sqlite3.Connection, person_id: int, goal_id: int) -> dict:
    """Creator or household member may read G (`goals.goal_access`, BODY-HOGAR-MIEMBRO-1 rule 7);
    `verificaciones` stays keyed by `person_id` -- this viewer's own weekly checks only."""
    if goals.goal_access(conn, goal_id, person_id) is None:
        return {"encontrada": False}
    g = conn.execute("SELECT * FROM goal WHERE id = ?", (goal_id,)).fetchone()
    habitos = conn.execute(
        "SELECT id, title, cadence, assignee_person_id FROM habit WHERE goal_id = ? AND active = 1 ORDER BY id",
        (goal_id,)).fetchall()
    verificaciones = conn.execute(
        "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? ORDER BY week_start DESC LIMIT 8",
        (goal_id, person_id),
    ).fetchall()
    return {
        "encontrada": True,
        "meta": {"id": g["id"], "titulo": g["title"], "tipo": g["kind"], "area": g["area"], "estado": g["status"],
                 "metric_key": g["metric_key"], "direccion": g["direction"], "valor_objetivo": g["target_value"],
                 "valor_base": g["baseline_value"], "inicio": g["start_day"], "plazo": g["deadline"],
                 "compartida": g["visibility"] == "household",
                 "enlaces": [{"app": e["app"], "url": e["url"]} for e in enlaces.enlaces_de(conn, goal_id)]},
        "habitos": [{"id": h["id"], "titulo": h["title"], "cadencia": json.loads(h["cadence"]),
                     "asignado_a": _habito_asignado_a(conn, person_id, h)} for h in habitos],
        "verificaciones": [dict(c) for c in verificaciones] if verificaciones else "sin verificación aún",
    }


def _leer_turnos(conn: sqlite3.Connection, person_id: int) -> dict:
    confirmado = shift_import.latest_confirmed(conn, person_id)
    return {
        "propuestos": shift_import.summary_proposed(conn, person_id),
        "ultimo_confirmado": ({"recibido": confirmado["received"], "dias": confirmado["day_count"]}
                               if confirmado else None),
    }


def _leer_costo_turnos(conn: sqlite3.Connection, person_id: int) -> dict:
    """Aggregates over a week or more (block counts, lengths, recovery days, per-metric mean
    differences) -- never a single day's value, so nivel 'resumen' may read it (BODY-TURNOS-COSTO-1)."""
    return turnos_costo.resumen(conn, person_id)


def _leer_dias_libres(conn: sqlite3.Connection, person_id: int, desde: str | None = None,
                       dias: int = 7) -> dict:
    """BODY-TURNO-DIA-PLAN-1 ruling 2: which days of [desde, desde+dias) are free to train
    according to confirmed shifts, and for each day that is not, the reason -- so planear_entreno
    (its own description points here first) never proposes a day the person is on shift or just
    came off a night. `desde` defaults to today in the person's own zone. Shift times are not a
    health figure (the same reasoning leer_turnos' own nivel 'ninguno' already rests on), so this
    needs no higher nivel_datos either."""
    desde = desde or _hoy_local(conn, person_id)
    try:
        desde_d = date.fromisoformat(desde)
    except ValueError:
        raise ToolError("'desde' debe ser una fecha ISO (AAAA-MM-DD)")
    if not (_LEER_DIAS_LIBRES_MIN_DIAS <= dias <= _LEER_DIAS_LIBRES_MAX_DIAS):
        raise ToolError(f"'dias' debe estar entre {_LEER_DIAS_LIBRES_MIN_DIAS} y {_LEER_DIAS_LIBRES_MAX_DIAS}")
    entradas = turnos_costo.dias_libres(conn, person_id, desde_d, dias)
    # ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6) ruling 4: each day's own reentrada/
    # tras_carga signals, present only when they apply -- planear_entreno's own description points
    # here first, so the model sees them before it ever proposes a day.
    for entrada in entradas:
        entrada.update(reentrada.senales(conn, person_id, date.fromisoformat(entrada["dia"])))
    return {"dias_libres": entradas}


def _leer_tutoriales(conn: sqlite3.Connection, person_id: int, consulta: str | None = None) -> dict:
    """tutoriales-v1 section 5.4: answers "¿cómo hago X?" from the catalog, never invented steps.
    `consulta`, when given, keeps only tasks whose title or chapter title matches case-insensitively;
    either way the result is capped at `_LEER_TUTORIALES_MAX`."""
    data = tutoriales.catalogo()
    capitulo_titulo = {c["id"]: c["titulo"] for c in data["capitulos"]}
    tareas = data["tareas"]
    if consulta:
        q = consulta.strip().lower()
        tareas = [t for t in tareas
                  if q in t["titulo"].lower() or q in capitulo_titulo.get(t["capitulo"], "").lower()]
    out = []
    for t in tareas[:_LEER_TUTORIALES_MAX]:
        out.append({
            "id": t["id"], "titulo": t["titulo"], "capitulo": capitulo_titulo.get(t["capitulo"], t["capitulo"]),
            "pantalla": t.get("pantalla"), "agente": t["agente"],
            "has_video": tutoriales.video(t["id"]) is not None, "url": f"/ayuda#{t['id']}",
        })
    return {"tareas": out}


def _leer_ajustes(conn: sqlite3.Connection, person_id: int) -> dict:
    p = db.person(conn, person_id)
    return {"nombre": p["display_name"], "zona_horaria": p["timezone"], "idioma": p["language"],
            "nivel_datos": ajustes.nivel_datos(conn, person_id)}


def _leer_estado_reloj(conn: sqlite3.Connection, person_id: int) -> dict:
    """BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 3: the sync state itself -- never a health figure, so
    nivel 'ninguno' (BODY-ASISTENTE-OBS-1) offers it at every data level, for the assistant and the
    Personal desk alike. Shares garmin_cuenta.estado_conexion/dias_sin_datos with Hoy's own sync
    card (ruling 2) and body/pull.py's healing gap-fill (ruling 1), never a copy.

    BODY-SYNC-VIVO-2 ruling 3 adds `estado_vivo`: the SAME plain sentence (or None) Hoy's own «Tu
    reloj» live card shows -- `pull.texto_estado_vivo`, shared, never a copy -- still never a
    health figure, so it rides along at this same 'ninguno' level.

    BODY-GARMIN-DIA-VACIO-1 ruling 3: `ultimo_dia_con_datos` is the newest day of
    `garmin_cuenta.dias_garmin_con_valor` (no window -- this person's whole history), never
    `db.days_with_data`, which counts an all-NULL/empty Garmin day as data."""
    estado = garmin_cuenta.estado_conexion(conn, person_id)
    person = db.person(conn, person_id)
    hoy = fechas.hoy_local(person)
    dias_con_valor = garmin_cuenta.dias_garmin_con_valor(conn, person_id)
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    return {
        "conectado": estado["conectado"],
        "conectado_desde": (datetime.fromisoformat(estado["conectado_desde"]).date().isoformat()
                             if estado["conectado_desde"] else None),
        "ultimo_error": estado["ultimo_error"],
        "ultimo_dia_con_datos": max(dias_con_valor) if dias_con_valor else None,
        "dias_sin_datos_14": garmin_cuenta.dias_sin_datos(conn, person_id, hoy),
        "estado_vivo": pull.texto_estado_vivo(T(person.get("language", "es")), conn, person_id, tz, hoy),
    }


def _hoy_local(conn: sqlite3.Connection, person_id: int) -> str:
    return fechas.hoy_local(db.person(conn, person_id)).isoformat()


def _dia_no_futuro(conn: sqlite3.Connection, person_id: int, day: str) -> None:
    """Parses `day` as an ISO date and refuses one later than the person's own local today
    (BODY-REVISION-9B-1-INV finding 4): for a tool that records something that already happened,
    never for a plan or a goal deadline, which name the future on purpose."""
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        raise ToolError("day debe ser una fecha ISO (AAAA-MM-DD)")
    hoy = fechas.hoy_local(db.person(conn, person_id))
    if not fechas.fecha_real_valida(parsed, hoy):
        raise ToolError(f"day no puede ser futuro: {day!r}")


# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: the tool's own three-way word for a figure that falls
# inside esfuerzo_sesion.clasifica's own band -- "como" (as usual) in place of that function's own
# "igual", the same translation body/rutas/entreno_sesion.py's `_ESFUERZO_PALABRA` already needs for
# the page, kept here as a plain code rather than a rendered sentence.
_CODIGO_HABITUAL = {"mas": "mas", "menos": "menos", "igual": "como"}


def _entreno_esfuerzo(conn: sqlite3.Connection, person_id: int, a: dict) -> dict | None:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: the SAME effort summary the per-session page's own
    card shows, read ONLY from `esfuerzo_sesion.tarjeta`'s return -- never recomputed. None when
    that function itself returns None (no stored 'detalle' or 'zonas' at all). Carried for every
    session that has one, strength included (ruling 2's own deliberate difference from the page,
    which never shows this card for a strength session -- Carlos's own gym sessions are this same
    person's data, and the desk asked about games, not only strength)."""
    detalle = esfuerzo_sesion.tarjeta(conn, person_id, a)
    if detalle is None:
        return None
    esf, zon, clasificacion = detalle["esfuerzos"], detalle["zonas"], detalle["clasificacion"]
    zonas_min = {n: zon["minutos"][n] for n in range(1, 6) if zon["minutos"][n] > 0} if zon is not None else {}
    zona_alta_pct = zon["alta_share"] * 100 if zon is not None and zon["alta_share"] is not None else None
    # Ruling 5's own comparable-session count -- the same selection `esfuerzo_sesion.historial`
    # already draws from internally, read again here only to report its size (never its figures).
    comparables = esfuerzo_sesion._sesiones_comparables(conn, person_id, a)
    return {
        "modelo": esfuerzo_sesion.VERSION,
        "zonas_min": zonas_min,
        "zona_alta_pct": zona_alta_pct,
        "esfuerzos_fuertes": esf["n_picos"],
        "caida_1min_ppm": esf["recuperacion"] if esf["suficiente"] else None,
        "datos_suficientes": esf["suficiente"],
        # "mas" en recuperacion significa que el pulso bajó más rápido de lo habitual, nunca más alto.
        "frente_a_tu_habitual": {
            "zona_alta": _CODIGO_HABITUAL.get(clasificacion["zona_alta"]),
            "recuperacion": _CODIGO_HABITUAL.get(clasificacion["caida"]),
        },
        "sesiones_comparadas": len(comparables),
    }


def _leer_entreno(conn: sqlite3.Connection, person_id: int, day: str | None = None, desde: str | None = None,
                   hasta: str | None = None) -> dict:
    """A day's or a range's training sessions (with their sets, display values plus whether each
    was corrected) and the planned session for that day, if any (BODY-ENTRENO-TOOLS-1). Reads only
    -- `db.exercise_sets` and plain SELECTs, never a writer. ORCH-CARLOS-DELEGATES-TIER-1-1 ruling
    5: each session's own entreno_dolor rows ride along as `dolor`.

    ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: every session of the person in range, any sport, not
    only strength/manual -- the WHERE clause below carries no `type` filter any more. Ruling 2 adds
    `esfuerzo` (see `_entreno_esfuerzo`), read for every session, strength included."""
    if day is not None:
        ini, fin = day, day
    elif desde is not None and hasta is not None:
        ini, fin = desde, hasta
    else:
        raise ToolError("se necesita 'day', o 'desde' y 'hasta'")
    # BODY-ENTRENO-VINCULO-1 ruling 3: both halves of any linked pair in range are fetched by this
    # same WHERE, so `db.merge_linked_training` always sees the pair whole and folds it to one
    # session, same as `db.load_day` -- unaffected by ruling 1's own widened WHERE, which already
    # fetches every row either half could be.
    actividades = db.merge_linked_training(conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day BETWEEN ? AND ? ORDER BY day, start_local",
        (person_id, ini, fin),
    ).fetchall())
    t = T(db.person(conn, person_id)["language"])
    sesiones = []
    for a in actividades:
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the exact predicate the old WHERE clause used,
        # now applied per session instead of in SQL -- strength-only fields stay real work only for
        # a strength/manual session; any other sport pays none of these four extra statements.
        es_fuerza = a["type"] == "strength_training" or a["source"] == "manual"
        if es_fuerza:
            sets = db.exercise_sets(conn, person_id, a["sets_source"], a["sets_source_id"])
            zonas_dia = db.zonas_en_recuperacion(conn, person_id, a["day"])
            # BODY-SERIES-ORDEN-1 rulings 1, 3, 5: a rest slot is never a set, and a slot the
            # person marked as not done is left out too -- `db.exercise_sets` already reads a
            # corrected slot's own `set_type` as 'active', whatever the watch's alternating
            # active/rest said.
            series = [{"orden": s["set_order"], "tipo": s["set_type"], "ejercicio": s["exercise"],
                       "categoria": s["category"], "repeticiones": s["reps"], "peso_kg": s["weight_value"],
                       "corregido": s["corrected_by_person"],
                       # BODY-LESION-ZONA-1 ruling 2: the one shared plain line, or None when this
                       # set's own categoria is not among today's open-lesion zonas.
                       "liviano_a_proposito": anatomia.texto_liviano_a_proposito(
                           t, {s["category"]} if s["category"] in zonas_dia else set())}
                      for s in sets
                      if s["set_type"] == "active" and not s["excluded_by_person"]]
            # BODY-ENTRENO-ULTIMA-VEZ-1 ruling 3: additive -- what the person did last time this
            # same exercise came up, by its effective name, so the assistant can say "last time
            # 3x8 at 40 kg" from data, never from memory.
            ultima_vez = entreno_historial.ultimas_veces(conn, person_id, a["sets_source"], a["sets_source_id"])
            # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: durante/después/mañana only -- `movimiento`
            # is the one free-text column this table carries and it never leaves the host (same
            # line ruling 1 drew for privacidad's own export), so it is left out here too, since
            # this read can end up in front of a model provider (body/agente/cliente.py).
            dolor = [{"ejercicio": d["ejercicio"], "zona": d["zona"], "dolor_durante": d["dolor_durante"],
                      "dolor_despues": d["dolor_despues"], "dolor_manana": d["dolor_manana"]}
                     for d in db.dolor_de_sesion(conn, person_id, a["sets_source"], a["sets_source_id"])]
        else:
            # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: present, honestly empty -- a caller never
            # branches on a missing key.
            series, ultima_vez, dolor = [], {}, []
        sesiones.append({
            "dia": a["day"], "titulo": a["name"], "fuente": a["source"], "hora_inicio": a["start_local"],
            "tipo": a["type"],
            "duracion_min": a["duration_s"] / 60 if a["duration_s"] is not None else None,
            "series": series, "ultima_vez": ultima_vez, "dolor": dolor,
            "esfuerzo": _entreno_esfuerzo(conn, person_id, a),
        })
    planes = conn.execute(
        "SELECT * FROM plan WHERE person_id = ? AND day BETWEEN ? AND ? AND ejercicios_json IS NOT NULL "
        "ORDER BY day",
        (person_id, ini, fin),
    ).fetchall()
    planeado = [{"dia": p["day"], "titulo": p["title"], "hora": p["time"],
                 "ejercicios": json.loads(p["ejercicios_json"])} for p in planes]
    return {"sesiones": sesiones, "planeado": planeado}


def _numero_entreno(t: T, ejercicio: str, valor, *, entero: bool, clave_error: str):
    """Validates/coerces one series item's `reps` or `peso_kg` at the door (BODY-ENTRENO-NUMEROS-1
    ruling 2): None passes through; a bool is refused even though Python's `bool` is technically an
    `int`; an int/float already of the right shape passes through; a string that is EXACTLY a plain
    number ("12", "17.5") is coerced, since outside callers (body.mcp included) often send numbers
    as strings -- anything else ("12-15", "12 reps", "") is refused with `clave_error`, naming
    `ejercicio`, in the person's own language."""
    if valor is None:
        return None
    if isinstance(valor, bool):
        raise ToolError(t(clave_error, ejercicio=ejercicio))
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        if entero:
            raise ToolError(t(clave_error, ejercicio=ejercicio))
        return valor
    if isinstance(valor, str):
        texto = valor.strip()
        try:
            return int(texto) if entero else float(texto)
        except ValueError:
            raise ToolError(t(clave_error, ejercicio=ejercicio))
    raise ToolError(t(clave_error, ejercicio=ejercicio))


def _categoria_entreno(t: T, ejercicio: str, valor):
    """Validates/normalises one exercise or series item's optional `categoria` at the door
    (BODY-CUERPO-VISTA-1-M ruling 2, the BODY-ENTRENO-NUMEROS-1 pattern): None/missing passes
    through; anything not in `anatomia.CATEGORIA_ENUM` (case/whitespace folded) is refused, naming
    the allowed words, in the person's own language -- never stored."""
    if valor is None:
        return None
    normalizado = valor.strip().lower() if isinstance(valor, str) else None
    if normalizado not in anatomia.CATEGORIA_ENUM:
        raise ToolError(t("agente.error.entreno_categoria_invalida", ejercicio=ejercicio,
                           categorias=", ".join(anatomia.CATEGORIA_ENUM)))
    return normalizado


def _zonas_periodo(t: T, valor: list) -> list[str] | None:
    """Validates/normalises marcar_periodo/corregir_periodo's optional `zonas` at the door
    (BODY-LESION-ZONA-1 ruling 1), the same _categoria_entreno pattern for a list instead of one
    value: each entry must be one of `anatomia.CATEGORIA_ENUM` (case/whitespace folded), refused
    naming the allowed words, in the person's own language. An empty list means the same as no
    zonas at all."""
    out = []
    for z in valor:
        normalizado = z.strip().lower() if isinstance(z, str) else None
        if normalizado not in anatomia.CATEGORIA_ENUM:
            raise ToolError(t("agente.error.periodo_zona_invalida", categorias=", ".join(anatomia.CATEGORIA_ENUM)))
        out.append(normalizado)
    return out or None


# -- BODY-PLAN-PROPUESTAS-1: shared validation for the whole-plan tools (proponer_plan,
# ampliar_plan) and for the two additive horizonte_goal_id links (planear_entreno,
# proponer_experimento) -- every refusal is a ToolError naming the item and why, raised BEFORE
# `propuestas.crear` ever stores the proposal (the same discipline `_proponer_experimento` already
# follows with db.validar_experimento).

def _plan_propio(conn: sqlite3.Connection, person_id: int, goal_id: int) -> bool:
    """A plan is a milestone goal this person created -- never a shared household one, and never a
    plain metric goal, even one the person owns."""
    return conn.execute(
        "SELECT 1 FROM goal WHERE id = ? AND person_id = ? AND kind = 'milestone'", (goal_id, person_id)
    ).fetchone() is not None


def _validar_cadencia_habito(cadencia) -> dict:
    if not isinstance(cadencia, dict):
        raise ToolError("cadencia: debe ser un objeto")
    per = cadencia.get("per", "week")
    if per not in ("day", "week", "month"):
        raise ToolError(f"cadencia.per inválido: {per!r}")
    times = cadencia.get("times", 1)
    if not isinstance(times, int) or isinstance(times, bool) or times < 1:
        raise ToolError("cadencia.times debe ser un entero >= 1")
    context = cadencia.get("context")
    if context is not None and context not in _CONTEXTOS_HABITO:
        raise ToolError(f"cadencia.context inválido: {context!r}")
    out = {"per": per, "times": times}
    if context is not None:
        out["context"] = context
    return out


def _validar_habitos_plan(conn: sqlite3.Connection, person_id: int, habitos) -> list[dict]:
    if habitos is None:
        return []
    if not isinstance(habitos, list) or len(habitos) > _PLAN_MAX_HABITOS:
        raise ToolError(f"habitos: lista inválida o con más de {_PLAN_MAX_HABITOS} elementos")
    out = []
    for i, h in enumerate(habitos):
        titulo = (h.get("titulo") or "").strip() if isinstance(h, dict) else ""
        if not titulo:
            raise ToolError(f"habitos[{i}].titulo: obligatorio")
        try:
            cadencia = _validar_cadencia_habito(h.get("cadencia") if isinstance(h, dict) else None)
        except ToolError as e:
            raise ToolError(f"habitos[{i}].{e.args[0]}")
        item = {"titulo": titulo, "cadencia": cadencia}
        # BODY-HISTORIA-VIDA-2 ruling 4: optional, validated the person's own, active practice --
        # never another person's, never an archived one.
        practica_id = h.get("practica_id") if isinstance(h, dict) else None
        if practica_id is not None:
            if not practicas.propia_activa(conn, person_id, practica_id):
                raise ToolError(f"habitos[{i}].practica_id desconocida: {practica_id!r}")
            item["practica_id"] = practica_id
        out.append(item)
    return out


def _validar_metas_finales_plan(conn: sqlite3.Connection, metas_finales) -> list[dict]:
    if metas_finales is None:
        return []
    if not isinstance(metas_finales, list) or len(metas_finales) > _PLAN_MAX_METAS_FINALES:
        raise ToolError(f"metas_finales: lista inválida o con más de {_PLAN_MAX_METAS_FINALES} elementos")
    out = []
    for i, mf in enumerate(metas_finales):
        if not isinstance(mf, dict):
            raise ToolError(f"metas_finales[{i}]: debe ser un objeto")
        titulo = (mf.get("titulo") or "").strip()
        if not titulo:
            raise ToolError(f"metas_finales[{i}].titulo: obligatorio")
        direction = mf.get("direction")
        if direction not in DIRECTIONS:
            raise ToolError(f"metas_finales[{i}].direction inválida: {direction!r}")
        metric_key = mf.get("metric_key")
        if conn.execute("SELECT 1 FROM metric_def WHERE key = ?", (metric_key,)).fetchone() is None:
            raise ToolError(f"metas_finales[{i}].metric_key desconocida: {metric_key!r}")
        target_value = mf.get("target_value")
        if not isinstance(target_value, (int, float)) or isinstance(target_value, bool):
            raise ToolError(f"metas_finales[{i}].target_value debe ser un número")
        out.append({"titulo": titulo, "metric_key": metric_key, "direction": direction,
                    "target_value": float(target_value)})
    return out


def _validar_fases_plan(fases, start_day: str, deadline: str) -> list[dict]:
    """BODY-PLAN-FASES-2 ruling 3: the plan's own phases, in the order the person gave them --
    that order IS what `reglas[i].fase_index` points into, so `db.validar_fases_horizonte` (the
    ONE overlap/bounds check, never duplicated here) runs against this exact list before anything
    is proposed."""
    if fases is None:
        return []
    if not isinstance(fases, list) or len(fases) > _PLAN_MAX_FASES:
        raise ToolError(f"fases: lista inválida o con más de {_PLAN_MAX_FASES} elementos")
    out = []
    for i, f in enumerate(fases):
        if not isinstance(f, dict):
            raise ToolError(f"fases[{i}]: debe ser un objeto")
        titulo = (f.get("titulo") or "").strip()
        if not titulo:
            raise ToolError(f"fases[{i}].titulo: obligatorio")
        start_f, end_f = f.get("start_day"), f.get("end_day")
        if not isinstance(start_f, str) or not isinstance(end_f, str):
            raise ToolError(f"fases[{i}]: start_day/end_day obligatorios")
        out.append({"titulo": titulo, "start_day": start_f, "end_day": end_f, "nota": f.get("nota")})
    try:
        db.validar_fases_horizonte(out, start_day, deadline)
    except ValueError as e:
        raise ToolError(e.args[0])
    return out


def _validar_reglas_plan(conn: sqlite3.Connection, reglas, n_fases: int = 0) -> list[dict]:
    if reglas is None:
        return []
    if not isinstance(reglas, list) or len(reglas) > _PLAN_MAX_REGLAS:
        raise ToolError(f"reglas: lista inválida o con más de {_PLAN_MAX_REGLAS} elementos")
    out = []
    for i, r in enumerate(reglas):
        if not isinstance(r, dict):
            raise ToolError(f"reglas[{i}]: debe ser un objeto")
        contexto, contexto_valor = r.get("contexto"), r.get("contexto_valor")
        try:
            db.validar_regla_horizonte(conn, contexto, contexto_valor)
        except ValueError as e:
            raise ToolError(f"reglas[{i}].{e.args[0]}")
        texto = (r.get("texto") or "").strip()
        if not texto:
            raise ToolError(f"reglas[{i}].texto: obligatorio")
        chequeo = r.get("chequeo")
        try:
            horizonte.validar_chequeo(chequeo)
        except ValueError as e:
            raise ToolError(f"reglas[{i}].{e.args[0]}")
        fase_index = r.get("fase_index")
        if fase_index is not None:
            if not isinstance(fase_index, int) or isinstance(fase_index, bool) or not (0 <= fase_index < n_fases):
                raise ToolError(f"reglas[{i}].fase_index fuera de rango: {fase_index!r}")
        out.append({"contexto": contexto, "contexto_valor": contexto_valor, "texto": texto, "chequeo": chequeo,
                    "fase_index": fase_index})
    return out


def _validar_semana_en_horizonte(week_start, start_day: str, deadline: str) -> None:
    if not isinstance(week_start, str):
        raise ToolError("week_start: obligatorio")
    try:
        d = date.fromisoformat(week_start)
    except ValueError:
        raise ToolError(f"week_start debe ser una fecha ISO (AAAA-MM-DD): {week_start!r}")
    if fechas.lunes_de(d) != d:
        raise ToolError(f"week_start debe ser un lunes: {week_start!r}")
    if week_start < start_day or week_start > deadline:
        raise ToolError(f"week_start fuera del horizonte ({start_day!r}..{deadline!r}): {week_start!r}")


def _validar_dia_en_horizonte(day, start_day: str, deadline: str) -> None:
    if not isinstance(day, str):
        raise ToolError("day: obligatorio")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ToolError(f"day debe ser una fecha ISO (AAAA-MM-DD): {day!r}")
    if day < start_day or day > deadline:
        raise ToolError(f"day fuera del horizonte ({start_day!r}..{deadline!r}): {day!r}")


def _validar_semanas_plan(semanas, start_day: str, deadline: str) -> list[dict]:
    if semanas is None:
        return []
    if not isinstance(semanas, list) or len(semanas) > _PLAN_MAX_SEMANAS:
        raise ToolError(f"semanas: lista inválida o con más de {_PLAN_MAX_SEMANAS} elementos")
    out = []
    for i, s in enumerate(semanas):
        if not isinstance(s, dict):
            raise ToolError(f"semanas[{i}]: debe ser un objeto")
        try:
            _validar_semana_en_horizonte(s.get("week_start"), start_day, deadline)
        except ToolError as e:
            raise ToolError(f"semanas[{i}].{e.args[0]}")
        out.append({"week_start": s["week_start"], "titulo": s.get("titulo"), "nota": s.get("nota")})
    return out


# BODY-PLAN-HORA-ENTRADA-1 ruling 1: every horizonte.MEDIDAS entry whose unit is a clock time
# (never a hardcoded pair -- read off MEDIDAS itself, so a future clock measure is covered for
# free). Nothing in this module told the model, or the desk through body/mcp.py, that
# horizonte.py stores a clock as minutes after 12:00 noon -- the natural guess (minutes since
# midnight) was silently wrong by 12 hours.
_MEDIDAS_HORA = frozenset(k for k, v in horizonte.MEDIDAS.items() if v["unit"] == "hora")


def _valor_reloj(campo: str, valor) -> float:
    """A clock measure's `valor` crosses the tool boundary as "HH:MM" (24h) -- converted here to
    horizonte's own minutes-after-noon storage convention (its own _parse_hhmm/
    _minutos_despues_mediodia, the only module that knows it; horizonte.py itself is unchanged).
    Anything else -- a raw number, "24:00", "7pm", "" -- is refused naming `campo`."""
    if isinstance(valor, str):
        try:
            hh, mm = horizonte._parse_hhmm(valor)
        except ValueError:
            pass
        else:
            return float(horizonte._minutos_despues_mediodia(hh, mm))
    raise ToolError(f'{campo}: una hora se escribe "HH:MM", p. ej. "23:00"')


def _hora_desde_minutos(total: float) -> str:
    """The inverse conversion, for what a tool returns (ruling 1, out direction) -- the same
    arithmetic as body/rutas/asistente.py's own `_hora_desde_minutos` (minutes after 12:00 noon,
    wrapping across midnight): 690 -> "23:30", 765 -> "00:45". Duplicated rather than imported,
    the same choice every other small helper in this module already makes."""
    total = int(round(total))
    hh = ((total // 60) + 12) % 24
    mm = total % 60
    return f"{hh:02d}:{mm:02d}"


def _validar_metas_semanales_plan(conn: sqlite3.Connection, metas_semanales, start_day: str, deadline: str, *,
                                   existentes: set | None = None) -> list[dict]:
    """`existentes` (ruling 4) is the set of (week_start, medida) pairs that already have a vigente
    weekly target for this horizon -- ampliar_plan's own duplicate-vs-database check, named
    'use corregir_meta_plan' instead of a bare 'duplicate'. proponer_plan passes none (the horizon
    does not exist yet), so only the within-list check ('at most one line per (week, medida)',
    ruling 1) ever fires for it."""
    if metas_semanales is None:
        return []
    if not isinstance(metas_semanales, list) or len(metas_semanales) > _PLAN_MAX_METAS_SEMANALES:
        raise ToolError(f"metas_semanales: lista inválida o con más de {_PLAN_MAX_METAS_SEMANALES} elementos")
    vistos: set = set()
    out = []
    for i, m in enumerate(metas_semanales):
        if not isinstance(m, dict):
            raise ToolError(f"metas_semanales[{i}]: debe ser un objeto")
        try:
            _validar_semana_en_horizonte(m.get("week_start"), start_day, deadline)
        except ToolError as e:
            raise ToolError(f"metas_semanales[{i}].{e.args[0]}")
        medida, sentido = m.get("medida"), m.get("sentido")
        try:
            horizonte.validar_medida(conn, medida, sentido)
        except ValueError as e:
            raise ToolError(f"metas_semanales[{i}].{e.args[0]}")
        valor = m.get("valor")
        if medida in _MEDIDAS_HORA:
            valor = _valor_reloj(f"metas_semanales[{i}].valor", valor)
        elif not isinstance(valor, (int, float)) or isinstance(valor, bool):
            raise ToolError(f"metas_semanales[{i}].valor debe ser un número")
        clave = (m["week_start"], medida)
        if existentes and clave in existentes:
            raise ToolError(f"metas_semanales[{i}]: ya hay una meta vigente para {medida!r} en la semana "
                            f"{m['week_start']!r} -- usa corregir_meta_plan")
        if clave in vistos:
            raise ToolError(f"metas_semanales[{i}]: {medida!r} aparece dos veces en la semana "
                            f"{m['week_start']!r}")
        vistos.add(clave)
        out.append({"week_start": m["week_start"], "medida": medida, "sentido": sentido, "valor": float(valor),
                    "nota": m.get("nota")})
    return out


def _validar_checkpoints_plan(conn: sqlite3.Connection, checkpoints, start_day: str, deadline: str) -> list[dict]:
    if checkpoints is None:
        return []
    if not isinstance(checkpoints, list) or len(checkpoints) > _PLAN_MAX_CHECKPOINTS:
        raise ToolError(f"checkpoints: lista inválida o con más de {_PLAN_MAX_CHECKPOINTS} elementos")
    out = []
    for i, c in enumerate(checkpoints):
        if not isinstance(c, dict):
            raise ToolError(f"checkpoints[{i}]: debe ser un objeto")
        try:
            _validar_dia_en_horizonte(c.get("day"), start_day, deadline)
        except ToolError as e:
            raise ToolError(f"checkpoints[{i}].{e.args[0]}")
        titulo = (c.get("titulo") or "").strip()
        if not titulo:
            raise ToolError(f"checkpoints[{i}].titulo: obligatorio")
        criterios = _convertir_criterios_reloj(f"checkpoints[{i}].criterios", c.get("criterios"))
        try:
            horizonte.validar_criterios(conn, criterios)
        except ValueError as e:
            raise ToolError(f"checkpoints[{i}].{e.args[0]}")
        out.append({"day": c["day"], "titulo": titulo, "criterios": criterios, "nota": c.get("nota")})
    return out


def _convertir_criterios_reloj(campo: str, criterios):
    """Ruling 1: a checkpoint criterio's `valor` is "HH:MM" when its medida is a clock measure --
    converted to minutes-after-noon BEFORE horizonte.validar_criterios sees it (that validator
    requires a number for every medida and is outside these touches). Anything not a list of
    dicts is passed through unchanged: horizonte.validar_criterios names that shape error itself."""
    if not isinstance(criterios, list):
        return criterios
    out = []
    for j, c in enumerate(criterios):
        if isinstance(c, dict) and c.get("medida") in _MEDIDAS_HORA:
            c = dict(c)
            c["valor"] = _valor_reloj(f"{campo}[{j}].valor", c.get("valor"))
        out.append(c)
    return out


def _planear_entreno(conn: sqlite3.Connection, person_id: int, conversacion_id: int, day: str, titulo: str,
                      ejercicios: list, time: str | None = None, goal_id: int | None = None,
                      horizonte_goal_id: int | None = None) -> dict:
    """Proposes one `plan` row for a day: a title, an optional time and a list of planned
    exercises `{ejercicio, series, reps?, categoria?}` (BODY-ENTRENO-TOOLS-1, Q3; categoria added
    BODY-CUERPO-VISTA-1-M ruling 2, validated at the door against `anatomia.CATEGORIA_ENUM`). Hoy's
    own body view lights its glow groups from this categoria (BODY-CUERPO-VISTA-1 ruling 3).
    Insert-shaped, like `proponer_meta`: there is no 'before'. Unlike `_registrar_entreno`/
    `_registrar_medida`, a future `day` is allowed on purpose here: a plan is for later, not for
    what already happened. `horizonte_goal_id` (BODY-PLAN-PROPUESTAS-1 ruling 5) is additive: give
    at most one of `goal_id`/`horizonte_goal_id` -- either becomes the session's own `plan.goal_id`.

    ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6) rulings 1-2: `reentrada.senales` for `day`
    decides, after every exercise is validated -- a qualifying gap scales every exercise's own
    `series` by its factor (fewer sets, never more than what was asked); a heavy day before `day`
    drops any exercise whose own categoria shares a loaded group, UNLESS that would leave nothing
    at all (the model's own list stands rather than an empty session). Either signal, present,
    rides along in `despues` for the card (`body/rutas/asistente.py`'s own `_tarjeta_planear_entreno`)
    to word -- never pre-rendered text, the same "structured now, worded at render" split every
    other `despues` field here already keeps."""
    try:
        dia_d = date.fromisoformat(day)
    except ValueError:
        raise ToolError("day debe ser una fecha ISO (AAAA-MM-DD)")
    titulo = (titulo or "").strip()
    if not titulo:
        raise ToolError("falta el título del plan")
    if not ejercicios:
        raise ToolError("se necesita al menos un ejercicio")
    t = T(db.person(conn, person_id)["language"])
    for e in ejercicios:
        ejercicio = (e.get("ejercicio") or "").strip()
        if not ejercicio:
            raise ToolError("cada ejercicio necesita 'ejercicio'")
        if not isinstance(e.get("series"), int) or e["series"] < 1:
            raise ToolError("cada ejercicio necesita 'series' (entero >= 1)")
        e["categoria"] = _categoria_entreno(t, ejercicio, e.get("categoria"))
    if goal_id is not None and horizonte_goal_id is not None:
        raise ToolError("usa goal_id o horizonte_goal_id, no los dos")
    if goal_id is not None:
        g = conn.execute("SELECT 1 FROM goal WHERE id = ? AND person_id = ?", (goal_id, person_id)).fetchone()
        if g is None:
            raise ToolError(f"meta desconocida: {goal_id!r}")
    if horizonte_goal_id is not None:
        if not _plan_propio(conn, person_id, horizonte_goal_id):
            raise ToolError(f"plan desconocido: {horizonte_goal_id!r}")
        goal_id = horizonte_goal_id
    senales = reentrada.senales(conn, person_id, dia_d)
    if "reentrada" in senales:
        factor = senales["reentrada"]["factor"]
        for e in ejercicios:
            e["series"] = max(1, round(e["series"] * factor))
    if "tras_carga" in senales:
        grupos_cargados = frozenset(senales["tras_carga"]["grupos"])
        filtrados = [e for e in ejercicios
                     if not (anatomia.grupos_de_categorias([e["categoria"]]) & grupos_cargados)]
        if filtrados:
            ejercicios = filtrados
    despues = {"day": day, "time": time, "titulo": titulo, "goal_id": goal_id, "ejercicios": ejercicios}
    if "reentrada" in senales:
        despues["reentrada"] = senales["reentrada"]
    if "tras_carga" in senales:
        despues["tras_carga"] = senales["tras_carga"]
    registro = propuestas.crear(conn, person_id, conversacion_id, "planear_entreno", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _resolver_sesion_entreno_dia(conn: sqlite3.Connection, person_id: int, day: str) -> tuple | None:
    """The day's own training session for a correction (BODY-CORREGIR-MEDIDA-ENTRENO-1 ruling 2):
    the watch's own strength activity (`type = 'strength_training'`) when there is one, else the
    person's existing manual session for that day -- told apart by exactly what
    `db.create_manual_session` writes, `source = 'manual'` with no `type` (NULL, there is no manual
    type) -- never any other watch activity (a run, a ride). Returns (source, source_id), or None
    when the day has neither."""
    actividad = conn.execute(
        "SELECT source, source_id FROM activity WHERE person_id = ? AND day = ? AND type = 'strength_training' "
        "ORDER BY start_local LIMIT 1", (person_id, day),
    ).fetchone()
    if actividad is None:
        actividad = conn.execute(
            "SELECT source, source_id FROM activity WHERE person_id = ? AND day = ? AND source = 'manual' "
            "AND type IS NULL ORDER BY start_local LIMIT 1", (person_id, day),
        ).fetchone()
    if actividad is None:
        return None
    return actividad["source"], actividad["source_id"]


def _registrar_entreno(conn: sqlite3.Connection, person_id: int, conversacion_id: int, day: str, series: list,
                        titulo: str | None = None, time: str | None = None) -> dict:
    """Proposes "here is what I did" for a day (BODY-ENTRENO-TOOLS-1, Q3). When that day has a
    Garmin strength activity, maps the spoken `series` onto the watch's sets by order -- fetching
    them with `pull.traer_series` first if they are not stored yet, so the card can show both the
    watch's value and the spoken correction per set. With no watch activity but an existing MANUAL
    session for that day, maps onto ITS sets the same way instead (BODY-CORREGIR-MEDIDA-ENTRENO-1
    ruling 2: a second `registrar_entreno` for a day already logged manually corrects that same
    session, never a duplicate one). With neither, stages a brand-new manual session. `antes`
    carries, per set, enough of the pre-existing state (`sets_antes`) for `propuestas.deshacer` to
    restore it exactly, or nothing for the manual-insert (brand-new session) case."""
    _dia_no_futuro(conn, person_id, day)
    if not series:
        raise ToolError("se necesita al menos una serie")
    t = T(db.person(conn, person_id)["language"])
    for s in series:
        ejercicio = (s.get("ejercicio") or "").strip()
        if not ejercicio:
            raise ToolError("cada serie necesita 'ejercicio'")
        s["reps"] = _numero_entreno(t, ejercicio, s.get("reps"), entero=True,
                                     clave_error="agente.error.entreno_reps_invalidas")
        s["peso_kg"] = _numero_entreno(t, ejercicio, s.get("peso_kg"), entero=False,
                                        clave_error="agente.error.entreno_peso_invalido")
        s["categoria"] = _categoria_entreno(t, ejercicio, s.get("categoria"))
    sesion = _resolver_sesion_entreno_dia(conn, person_id, day)
    if sesion is not None:
        source, source_id = sesion
        existentes = db.exercise_sets(conn, person_id, source, source_id)
        reloj_fallo = False
        if not existentes and source == "garmin":
            try:
                pull.traer_series(conn, person_id, source_id)
                existentes = db.exercise_sets(conn, person_id, source, source_id)
            except Exception:
                # Ruling 3 (BODY-SERIES-UNIDAD-1): the watch's sets are a help, not a
                # precondition -- any failure here (Garmin unreachable, a bad payload, a DB
                # error) still lets the person's own spoken sets be saved, mapped onto no
                # watch set (reloj: null for every one), rather than losing the proposal.
                existentes = []
                reloj_fallo = True
        # BODY-SERIES-ORDEN-1 ruling 2: a spoken set maps onto the watch's own ACTIVE slots, in
        # order -- never onto a rest slot, which Garmin's exerciseSets alternates in between every
        # active one. `existentes` is already ordered by set_order, so this is a plain filter.
        activos = [s for s in existentes if s["set_type"] == "active"]
        ultimo_orden = existentes[-1]["set_order"] if existentes else 0
        siguiente_orden = ultimo_orden + 1
        mapeo, sets_antes = [], []
        for hablado in series:
            if activos:
                watch = activos.pop(0)
                destino = watch["set_order"]
            else:
                # The spoken series ran longer than the watch's own active slots: a slot the
                # watch never reported, numbered right after the last one it did.
                watch = None
                destino = siguiente_orden
                siguiente_orden += 1
            mapeo.append({
                "set_order": destino,
                "reloj": ({"ejercicio": watch["exercise"], "reps": watch["reps"],
                           "peso_kg": watch["weight_value"]} if watch else None),
                "tu": hablado,
            })
            sets_antes.append({
                "set_order": destino, "existia": watch is not None,
                "person_category": watch["person"]["category"] if watch else None,
                "person_exercise": watch["person"]["exercise"] if watch else None,
                "person_reps": watch["person"]["reps"] if watch else None,
                "person_weight_kg": watch["person"]["weight_kg"] if watch else None,
                "person_note": watch["person"]["note"] if watch else None,
                "corrected_by_person": bool(watch["corrected_by_person"]) if watch else False,
                "garmin_exercise": watch["garmin"]["exercise"] if watch else None,
                "garmin_reps": watch["garmin"]["reps"] if watch else None,
                "garmin_weight_value": watch["garmin"]["weight_value"] if watch else None,
                "garmin_weight_unit": watch["garmin"]["weight_unit"] if watch else None,
                "garmin_category": watch["garmin"]["category"] if watch else None,
            })
        # BODY-SERIES-ORDEN-1 rulings 3, 4: the watch's own active slots the spoken series never
        # reached (fewer spoken sets than active watch slots) are proposed for exclusion -- applied
        # alongside the correction, undone alongside it too.
        no_emparejadas = [{"set_order": s["set_order"], "excluded_antes": s["excluded_by_person"],
                            "ejercicio": s["exercise"], "reps": s["reps"], "peso_kg": s["weight_value"]}
                           for s in activos]
        despues = {"manual": False, "day": day, "source": source, "source_id": source_id, "series": series,
                   "mapeo": mapeo, "reloj_fallo": reloj_fallo, "no_emparejadas": no_emparejadas}
        antes = {"manual": False, "sets_antes": sets_antes}
    else:
        despues = {"manual": True, "day": day, "time": time, "titulo": ((titulo or "").strip() or None),
                   "series": series}
        antes = {"manual": True}
    registro = propuestas.crear(conn, person_id, conversacion_id, "registrar_entreno", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _corregir_entreno(conn: sqlite3.Connection, person_id: int, conversacion_id: int, dia: str, orden: int,
                       ejercicio: str | None = None, reps=None, peso_kg=None) -> dict:
    """Proposes correcting ONE set of a session already logged that day, by its position
    (BODY-CORREGIR-MEDIDA-ENTRENO-1-W ruling B: id-addressed like corregir_ingesta, so "the third
    one was 8 reps" fixes just that slot without resending every set). Resolves the day's own
    session the same way ruling 2's `_resolver_sesion_entreno_dia` does -- a Garmin strength day or
    an existing manual one, never any other watch activity. `antes` carries that slot's full
    pre-correction state (both `person_*` and `garmin_*` columns, plus `corrected_by_person`), so
    `propuestas.deshacer` restores it EXACTLY through the same direct UPDATE on `workout_set`
    `registrar_entreno`'s own undo already uses for a Garmin-matched slot -- `correct_exercise_set`
    only ever moves a slot INTO "corrected", never back out of it."""
    _dia_no_futuro(conn, person_id, dia)
    if isinstance(orden, bool) or not isinstance(orden, int):
        raise ToolError("orden debe ser un número entero")
    if orden < 1:
        raise ToolError("orden debe ser un número entero mayor o igual a 1")
    if ejercicio is None and reps is None and peso_kg is None:
        raise ToolError("se necesita al menos uno de ejercicio, reps o peso_kg")
    t = T(db.person(conn, person_id)["language"])
    sesion = _resolver_sesion_entreno_dia(conn, person_id, dia)
    if sesion is None:
        raise ToolError(t("agente.error.entreno_sin_sesion"))
    source, source_id = sesion
    existentes = db.exercise_sets(conn, person_id, source, source_id)
    # A rest slot (Garmin alternates active/rest, BODY-SERIES-ORDEN-1) is never a "serie" to
    # correct -- the same active-only filter `_leer_entreno`/`_registrar_entreno` already apply.
    actual = next((s for s in existentes if s["set_order"] == orden and s["set_type"] == "active"), None)
    if actual is None:
        raise ToolError(t("agente.error.entreno_sin_serie", orden=orden))
    nombre = (ejercicio or actual["exercise"] or "?").strip() or "?"
    if ejercicio is not None:
        ejercicio = ejercicio.strip()
        if not ejercicio:
            raise ToolError("ejercicio no puede quedar vacío")
    reps = (_numero_entreno(t, nombre, reps, entero=True, clave_error="agente.error.entreno_reps_invalidas")
            if reps is not None else None)
    peso_kg = (_numero_entreno(t, nombre, peso_kg, entero=False, clave_error="agente.error.entreno_peso_invalido")
               if peso_kg is not None else None)
    antes = {
        "set_order": orden, "existia": True,
        "person_category": actual["person"]["category"], "person_exercise": actual["person"]["exercise"],
        "person_reps": actual["person"]["reps"], "person_weight_kg": actual["person"]["weight_kg"],
        "person_note": actual["person"]["note"], "corrected_by_person": actual["corrected_by_person"],
        "garmin_exercise": actual["garmin"]["exercise"], "garmin_reps": actual["garmin"]["reps"],
        "garmin_weight_value": actual["garmin"]["weight_value"], "garmin_weight_unit": actual["garmin"]["weight_unit"],
        "garmin_category": actual["garmin"]["category"],
    }
    despues = {"dia": dia, "source": source, "source_id": source_id, "set_order": orden,
               "ejercicio": ejercicio, "reps": reps, "peso_kg": peso_kg}
    registro = propuestas.crear(conn, person_id, conversacion_id, "corregir_entreno", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _anotar_sesion(conn: sqlite3.Connection, person_id: int, conversacion_id: int, plan_id: int, nota: str,
                    hecha: bool | None = None) -> dict:
    """Proposes a note on an existing PLANNED session row (BODY-HOGAR-ESCRIBE-1 ruling 4): how the
    session actually went, matched against the plan. `hecha=true` also proposes marking the plan
    'done' (BODY-PLAN-SESION-HECHA-1 then counts it, once, toward sesiones_fuerza); left out or
    false, the plan's own status is proposed unchanged. `antes`/`despues` each carry the plan row's
    FULL note/timestamp/status/origen state, the same update-shaped, caller-resolves-state
    convention `corregir_comida` already uses, so `propuestas.deshacer` can restore it exactly
    through the same writer (`db.anotar_sesion_plan`). Refuses a plan_id that is not this person's
    own, by name -- never silently matched to someone else's session.

    BODY-HOGAR-NOTA-SESION-GUARD-1 rulings 2-3: the writer is resolved from THIS call's own
    conversation -- 'hogar' when `conversacion_id`'s `conversacion_agente.origen` is 'hogar' (set
    only by body/rutas/dispositivos.py's own Hogar write-through), 'persona' for the in-app
    assistant (origen None) and the person's own MCP assistant (origen 'mcp') alike -- never from
    anything the caller sends. When the writer is Hogar and the plan's CURRENT note is non-empty
    and was not written by Hogar (including a legacy NULL-origin note, the conservative reading),
    this refuses BEFORE any proposal is created, with a stable `codigo` an outside caller can
    branch on without parsing the person-language message."""
    nota = (nota or "").strip()
    if not (1 <= len(nota) <= 2000):
        raise ToolError("nota debe tener entre 1 y 2000 caracteres")
    if hecha is not None and not isinstance(hecha, bool):
        raise ToolError("hecha debe ser verdadero o falso")
    fila = conn.execute(
        "SELECT day, title, status, nota_sesion, nota_sesion_en, nota_sesion_origen FROM plan "
        "WHERE id = ? AND person_id = ?",
        (plan_id, person_id),
    ).fetchone()
    if fila is None:
        raise ToolError(f"plan desconocido: {plan_id!r}")
    escribe_hogar = agente_conversacion.origen_de(conn, conversacion_id) == "hogar"
    if escribe_hogar and (fila["nota_sesion"] or "").strip() and fila["nota_sesion_origen"] != "hogar":
        t = T(db.person(conn, person_id)["language"])
        raise ToolError(t("agente.error.nota_sesion_existente"), codigo="nota_de_la_persona")
    antes = {"status": fila["status"], "nota_sesion": fila["nota_sesion"], "nota_sesion_en": fila["nota_sesion_en"],
             "nota_sesion_origen": fila["nota_sesion_origen"]}
    nuevo_status = "done" if hecha else fila["status"]
    nuevo_origen = "hogar" if escribe_hogar else "persona"
    despues = {"plan_id": plan_id, "day": fila["day"], "titulo": fila["title"], "nota": nota,
               "hecha": bool(hecha), "status": nuevo_status, "nota_sesion_origen": nuevo_origen}
    registro = propuestas.crear(conn, person_id, conversacion_id, "anotar_sesion", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _descartar_series(conn: sqlite3.Connection, person_id: int, conversacion_id: int, day: str,
                       series: list) -> dict:
    """Proposes marking existing watch slots of an already-logged day's strength activity as not
    done (BODY-SERIES-ORDEN-1 ruling 4): refuses a slot the person already filled, a rest slot, or
    an unknown order, naming the reason in the person's own language. This is the tool for a day
    whose watch slots were already read -- `registrar_entreno` handles the unmatched leftovers of
    its own, fresh call on its own."""
    _dia_no_futuro(conn, person_id, day)
    if not series:
        raise ToolError("se necesita al menos una serie")
    t = T(db.person(conn, person_id)["language"])
    actividad = conn.execute(
        "SELECT source, source_id FROM activity WHERE person_id = ? AND day = ? AND type = 'strength_training' "
        "ORDER BY start_local LIMIT 1", (person_id, day),
    ).fetchone()
    if actividad is None:
        raise ToolError(t("agente.error.entreno_sin_actividad_fuerza", day=day))
    source, source_id = actividad["source"], actividad["source_id"]
    existentes = {s["set_order"]: s for s in db.exercise_sets(conn, person_id, source, source_id)}
    vistos, entradas = set(), []
    for orden in series:
        if isinstance(orden, bool) or not isinstance(orden, int):
            raise ToolError(t("agente.error.entreno_orden_desconocido", orden=orden))
        if orden in vistos:
            continue
        vistos.add(orden)
        slot = existentes.get(orden)
        if slot is None:
            raise ToolError(t("agente.error.entreno_orden_desconocido", orden=orden))
        if slot["corrected_by_person"]:
            raise ToolError(t("agente.error.entreno_orden_tuya", orden=orden))
        if slot["set_type"] != "active":
            raise ToolError(t("agente.error.entreno_orden_descanso", orden=orden))
        entradas.append({
            "set_order": orden, "excluded_antes": slot["excluded_by_person"],
            "ejercicio": slot["garmin"]["exercise"], "categoria": slot["garmin"]["category"],
            "reps": slot["garmin"]["reps"], "peso_kg": slot["weight_value"],
        })
    despues = {"day": day, "source": source, "source_id": source_id, "series": entradas}
    registro = propuestas.crear(conn, person_id, conversacion_id, "descartar_series", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _resolver_par_vinculado(conn: sqlite3.Connection, t: T, person_id: int, day: str,
                             source_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    """`separar_sesion`'s own `source_id` resolves either half of a linked pair (ruling 4) --
    returns (reloj, manual) for whichever one that day actually links. Refuses a day/source_id with
    no linked pair at all, naming why in the person's own language."""
    fila = conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day = ? AND source_id = ?",
        (person_id, day, source_id),
    ).fetchone()
    if fila is None:
        raise ToolError(t("agente.error.entreno_sesion_desconocida", source_id=source_id))
    if fila["source"] == "manual":
        reloj = conn.execute(
            "SELECT * FROM activity WHERE person_id = ? AND day = ? AND source != 'manual' AND "
            "vinculo_manual = ?", (person_id, day, fila["source_id"]),
        ).fetchone()
        manual = fila
    else:
        reloj = fila
        manual = (conn.execute(
            "SELECT * FROM activity WHERE person_id = ? AND source = 'manual' AND source_id = ?",
            (person_id, fila["vinculo_manual"]),
        ).fetchone() if fila["vinculo_manual"] else None)
    if reloj is None or manual is None:
        raise ToolError(t("agente.error.entreno_no_unida", day=day))
    return reloj, manual


def _separar_sesion(conn: sqlite3.Connection, person_id: int, conversacion_id: int, day: str,
                     source_id: str) -> dict:
    """Proposes splitting a day's linked watch/manual pair back into two sessions (BODY-ENTRENO-
    VINCULO-1 ruling 4): the person's own undo of an automatic or a chat-raised link. `source_id`
    may name either half -- `_resolver_par_vinculado` finds the other one."""
    _dia_no_futuro(conn, person_id, day)
    t = T(db.person(conn, person_id)["language"])
    reloj, manual = _resolver_par_vinculado(conn, t, person_id, day, source_id)
    despues = {"day": day, "source": reloj["source"], "source_id": reloj["source_id"],
               "manual_source_id": manual["source_id"], "reloj_titulo": reloj["name"],
               "reloj_hora": reloj["start_local"], "manual_titulo": manual["name"],
               "manual_hora": manual["start_local"]}
    registro = propuestas.crear(conn, person_id, conversacion_id, "separar_sesion", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _unir_sesiones(conn: sqlite3.Connection, person_id: int, conversacion_id: int, day: str) -> dict:
    """Proposes joining a day that already holds a watch strength session and a manual session as
    two separate rows (BODY-ENTRENO-VINCULO-1 ruling 5) -- how a day stored before this slice, or
    one `separar_sesion` already split, gets the same match the auto-link makes at sync time.
    Refuses when that day does not hold exactly one of each, naming why in the person's own
    language."""
    _dia_no_futuro(conn, person_id, day)
    t = T(db.person(conn, person_id)["language"])
    candidatos = db.candidatos_union_entreno(conn, person_id, day)
    if candidatos is None:
        raise ToolError(t("agente.error.entreno_unir_ambiguo", day=day))
    reloj, manual = candidatos["reloj"], candidatos["manual"]
    despues = {"day": day, "source": reloj["source"], "source_id": reloj["source_id"],
               "manual_source_id": manual["source_id"], "reloj_titulo": reloj["name"],
               "manual_titulo": manual["name"]}
    registro = propuestas.crear(conn, person_id, conversacion_id, "unir_sesiones", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _registrar_medida(conn: sqlite3.Connection, person_id: int, conversacion_id: int, metric_key: str, value,
                       day: str | None = None) -> dict:
    """Proposes a number the person measured themself for a day (BODY-ENTRENO-MEDIDA-1): checked
    against the live metric_def row, never a hardcoded pair of keys, and refused if that key is
    'garmin' -- the watch, not the person, owns those. `antes` carries that day's existing value (or
    None), so `propuestas.deshacer` can restore it exactly. Besides the unit/type checks
    `_validar_parametros` already ran, the only sanity checks are rejecting a negative figure and,
    for a key listed in `db.RANGOS_MEDIDA_PERSONAL` (BODY-MEDIDA-PESO-1), a typo-sized figure
    outside that key's plausible range -- plus, for a key listed in `db.ENTEROS_MEDIDA_PERSONAL`
    (BODY-AGUJETAS-1 ruling 2), a non-whole-number figure. BODY-PROTEINA-1: an intake key
    (agregado = 'suma_ingesta') is refused here too -- checked against the live row,
    defense-in-depth past the enum -- pointing at registrar_ingesta, its one writer."""
    fila = conn.execute("SELECT agregado FROM metric_def WHERE key = ? AND origen = 'self'",
                        (metric_key,)).fetchone()
    if fila is None:
        raise ToolError(f"medida desconocida o no autogestionada: {metric_key!r}")
    if fila["agregado"] == "suma_ingesta":
        raise ToolError(f"{metric_key!r} se registra con registrar_ingesta, no con registrar_medida")
    if metric_key in db.CHECKIN_METRIC_KEYS:
        raise ToolError(f"{metric_key!r} se registra con registrar_checkin, no con registrar_medida")
    day = day or _hoy_local(conn, person_id)
    _dia_no_futuro(conn, person_id, day)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ToolError("value debe ser un número")
    if value < 0:
        raise ToolError(f"{metric_key!r} no puede ser negativo")
    rango = db.RANGOS_MEDIDA_PERSONAL.get(metric_key)
    if rango is not None and not (rango[0] <= value <= rango[1]):
        raise ToolError(f"{metric_key!r} fuera de rango plausible ({rango[0]}-{rango[1]}): {value!r}")
    if metric_key in db.ENTEROS_MEDIDA_PERSONAL and not value.is_integer():
        raise ToolError(f"{metric_key!r} debe ser un número entero: {value!r}")
    anterior = conn.execute(
        "SELECT value FROM daily_metric WHERE person_id = ? AND day = ? AND metric_key = ?",
        (person_id, day, metric_key),
    ).fetchone()
    antes = {"value": anterior["value"] if anterior else None}
    despues = {"metric_key": metric_key, "value": value, "day": day}
    registro = propuestas.crear(conn, person_id, conversacion_id, "registrar_medida", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _registrar_dolor(conn: sqlite3.Connection, person_id: int, conversacion_id: int, dia: str, ejercicio: str,
                      dolor_durante=None, dolor_despues=None, movimiento: str | None = None) -> dict:
    """Proposes how one exercise of a day's own training session felt (ORCH-CARLOS-DELEGATES-
    TIER-1-1 ruling 5): resolves the day's session the same way `corregir_entreno` does
    (`_resolver_sesion_entreno_dia`), and `zona` comes from that session's own matching set, never
    freely typed -- so this can only name an exercise the person actually trained that day. `antes`
    carries the existing entreno_dolor row's own durante/despues/movimiento (or None), so
    `propuestas.deshacer` restores it exactly through the same writer `registrar_dolor_entreno`
    uses. Never medical advice: this only records the number and the person's own words."""
    t = T(db.person(conn, person_id)["language"])
    _dia_no_futuro(conn, person_id, dia)
    sesion = _resolver_sesion_entreno_dia(conn, person_id, dia)
    if sesion is None:
        raise ToolError(t("agente.error.dolor_sin_sesion", day=dia))
    source, source_id = sesion
    sets = db.exercise_sets(conn, person_id, source, source_id)
    coincidencia = next((s for s in sets if s["exercise"] and not s["excluded_by_person"]
                         and s["exercise"].strip().lower() == ejercicio.strip().lower()), None)
    if coincidencia is None:
        raise ToolError(t("agente.error.dolor_ejercicio_desconocido", ejercicio=ejercicio, day=dia))
    if dolor_durante is None and dolor_despues is None and not (movimiento and movimiento.strip()):
        raise ToolError(t("agente.error.dolor_nada_que_registrar"))
    for campo, valor in (("dolor_durante", dolor_durante), ("dolor_despues", dolor_despues)):
        if valor is not None and (isinstance(valor, bool) or not isinstance(valor, int)
                                   or not (db.DOLOR_ESCALA_MIN <= valor <= db.DOLOR_ESCALA_MAX)):
            raise ToolError(t("agente.error.dolor_fuera_de_rango", campo=campo, minimo=db.DOLOR_ESCALA_MIN,
                               maximo=db.DOLOR_ESCALA_MAX))
    movimiento = movimiento.strip() if movimiento and movimiento.strip() else None
    ejercicio_exacto, zona = coincidencia["exercise"], coincidencia["category"]
    anterior = conn.execute(
        "SELECT dolor_durante, dolor_despues, movimiento FROM entreno_dolor WHERE person_id = ? AND "
        "sets_source = ? AND sets_source_id = ? AND ejercicio = ?",
        (person_id, source, source_id, ejercicio_exacto),
    ).fetchone()
    antes = {"dolor_durante": anterior["dolor_durante"], "dolor_despues": anterior["dolor_despues"],
             "movimiento": anterior["movimiento"]} if anterior else None
    despues = {"sets_source": source, "sets_source_id": source_id, "day": dia, "ejercicio": ejercicio_exacto,
               "zona": zona, "dolor_durante": dolor_durante, "dolor_despues": dolor_despues,
               "movimiento": movimiento}
    registro = propuestas.crear(conn, person_id, conversacion_id, "registrar_dolor", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


_HORA_FORMATOS_ACEPTADOS = "'HH:MM' o una fecha-hora ISO completa (con o sin desplazamiento horario)"


def _hora_a_ts_local(conn: sqlite3.Connection, person_id: int, day: str | None,
                      hora: str | None) -> tuple[str | None, str | None]:
    """ONE validating parser for `hora`, shared by every assistant tool that takes it
    (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1; census: `_registrar_comida`, `_corregir_comida`,
    `_registrar_ingesta`, `_registrar_checkin`) -- replacing the old `f"{day}T{hora}:00"`, which
    stored a NESTED ts_local (`<day>T<day>T<hh:mm:ss><offset>:00`) on three applied meals when
    `hora` was already a full timestamp. Accepts 'HH:MM'/'H:MM', or a full ISO date-time with or
    without a UTC offset -- an offset is converted to the person's own `timezone`. Returns
    (day, hora_hhmmss): `hora_hhmmss` is None only when `hora` is None; `day` is the date-time's own
    local date when `day` was None, checked to MATCH `day` when one was given (a mismatch is
    refused BY NAME) -- a bare clock time carries no date, so `day` comes back unchanged (possibly
    still None; the caller supplies its own default, today or an existing row's day). Anything else
    (seconds-only, '25:00', an empty string) is refused naming the accepted forms."""
    if hora is None:
        return day, None
    valor = hora.strip() if isinstance(hora, str) else ""
    if not valor:
        raise ToolError(f"hora inválida: se acepta {_HORA_FORMATOS_ACEPTADOS}")
    try:
        reloj = datetime.strptime(valor, "%H:%M")
    except ValueError:
        reloj = None
    if reloj is not None:
        return day, reloj.strftime("%H:%M:%S")
    fecha_hora = None
    if len(valor) > 10:
        try:
            fecha_hora = datetime.fromisoformat(valor)
        except ValueError:
            fecha_hora = None
    if fecha_hora is None:
        raise ToolError(f"hora inválida: {hora!r}; se acepta {_HORA_FORMATOS_ACEPTADOS}")
    if fecha_hora.tzinfo is not None:
        zona = db.person(conn, person_id)["timezone"]
        fecha_hora = fecha_hora.astimezone(ZoneInfo(zona)).replace(tzinfo=None)
    dia_local = fecha_hora.date().isoformat()
    if day is not None and day != dia_local:
        raise ToolError(f"hora {hora!r} no corresponde al día {day!r} (su fecha local es {dia_local!r})")
    return dia_local, fecha_hora.strftime("%H:%M:%S")


def _registrar_ingesta(conn: sqlite3.Connection, person_id: int, conversacion_id: int, metric_key: str,
                        amount, estimado: bool, descripcion: str | None = None, day: str | None = None,
                        hora: str | None = None) -> dict:
    """Proposes one thing the person ate or drank, for a day (BODY-PROTEINA-1): estimate grams from
    the person's own words when they give no exact number, say it is an estimate and what was
    assumed, one entry per meal, and ask only when the food is too vague to estimate. Checked
    against the live metric_def row (agregado = 'suma_ingesta'), never a hardcoded key -- the same
    template pattern `_registrar_medida` uses for 'self'. Insert-shaped, like `planear_entreno`:
    there is no 'before'."""
    fila = conn.execute("SELECT 1 FROM metric_def WHERE key = ? AND agregado = 'suma_ingesta'",
                        (metric_key,)).fetchone()
    if fila is None:
        raise ToolError(f"ingesta desconocida o no acumulable: {metric_key!r}")
    day, hora_hhmmss = _hora_a_ts_local(conn, person_id, day, hora)
    day = day or _hoy_local(conn, person_id)
    _dia_no_futuro(conn, person_id, day)
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ToolError("amount debe ser un número")
    if amount <= 0:
        raise ToolError(f"{metric_key!r} debe ser mayor que cero")
    if not isinstance(estimado, bool):
        raise ToolError("estimado debe ser verdadero o falso")
    descripcion = (descripcion or "").strip() or None
    ts_local = f"{day}T{hora_hhmmss}" if hora_hhmmss is not None else None
    despues = {"metric_key": metric_key, "amount": amount, "estimado": estimado, "descripcion": descripcion,
               "day": day, "ts_local": ts_local}
    registro = propuestas.crear(conn, person_id, conversacion_id, "registrar_ingesta", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _corregir_ingesta(conn: sqlite3.Connection, person_id: int, conversacion_id: int, ingesta_id: int,
                       amount) -> dict:
    """Proposes correcting the amount of one of the person's own intake entries (BODY-PROTEINA-1).
    `antes` carries that entry's existing amount, so `propuestas.deshacer` can restore it exactly."""
    fila = conn.execute(
        "SELECT metric_key, day, amount, estimado FROM ingesta WHERE id = ? AND person_id = ?",
        (ingesta_id, person_id)).fetchone()
    if fila is None:
        raise ToolError(f"registro de ingesta desconocido: {ingesta_id!r}")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ToolError("amount debe ser un número")
    if amount <= 0:
        raise ToolError("amount debe ser mayor que cero")
    antes = {"amount": fila["amount"]}
    despues = {"ingesta_id": ingesta_id, "amount": amount, "metric_key": fila["metric_key"], "day": fila["day"],
               "estimado": bool(fila["estimado"])}
    registro = propuestas.crear(conn, person_id, conversacion_id, "corregir_ingesta", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _borrar_ingesta(conn: sqlite3.Connection, person_id: int, conversacion_id: int, ingesta_id: int) -> dict:
    """Proposes deleting one of the person's own intake entries (BODY-PROTEINA-1). `antes` carries
    the whole entry, so `propuestas.deshacer` can restore it (as a new row with the same values,
    the same "functionally equivalent" restoration `proponer_experimento`'s own undo uses)."""
    fila = conn.execute(
        "SELECT metric_key, day, ts_local, amount, estimado, descripcion FROM ingesta "
        "WHERE id = ? AND person_id = ?", (ingesta_id, person_id)).fetchone()
    if fila is None:
        raise ToolError(f"registro de ingesta desconocido: {ingesta_id!r}")
    antes = {"metric_key": fila["metric_key"], "day": fila["day"], "ts_local": fila["ts_local"],
             "amount": fila["amount"], "estimado": bool(fila["estimado"]), "descripcion": fila["descripcion"]}
    despues = {"ingesta_id": ingesta_id}
    registro = propuestas.crear(conn, person_id, conversacion_id, "borrar_ingesta", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


# -- proponer_objetivo_nutriente (BODY-NUTRIENTE-OBJETIVO-1: update-shaped like registrar_medida --
# this module calls db.set_nutrient_target/db.borrar_nutrient_target directly) --------------------

def _proponer_objetivo_nutriente(conn: sqlite3.Connection, person_id: int, conversacion_id: int, metric_key: str,
                                  kind: str, sentido: str, amount=None, start_day: str | None = None) -> dict:
    """Proposes a daily target for one of the eight intake keys (BODY-NUTRIENTE-OBJETIVO-1): a
    fixed amount or a per-kg-of-body-weight ratio, to reach or as a limit. The person sets every
    value; this tool never invents one. `antes` carries that exact day's existing row (or None), so
    `propuestas.deshacer` can restore it exactly -- the same update-shaped pattern
    `_registrar_medida` uses on `daily_metric`."""
    fila = conn.execute("SELECT unit FROM metric_def WHERE key = ? AND agregado = 'suma_ingesta'",
                        (metric_key,)).fetchone()
    if fila is None:
        raise ToolError(f"ingesta desconocida o no acumulable: {metric_key!r}")
    if kind not in db.NUTRIENT_TARGET_KINDS:
        raise ToolError(f"kind inválido: {kind!r}; debe ser uno de {db.NUTRIENT_TARGET_KINDS}")
    if sentido not in db.NUTRIENT_TARGET_SENTIDOS:
        raise ToolError(f"sentido inválido: {sentido!r}; debe ser uno de {db.NUTRIENT_TARGET_SENTIDOS}")
    if kind == "per_kg" and fila["unit"] != "g":
        raise ToolError(f"{metric_key!r} no se mide en gramos: per_kg no aplica")
    if kind == "off":
        amount = None
    else:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise ToolError("amount debe ser un número")
        if amount <= 0:
            raise ToolError("amount debe ser mayor que cero")
    start_day = start_day or _hoy_local(conn, person_id)
    try:
        date.fromisoformat(start_day)
    except ValueError:
        raise ToolError("start_day debe ser una fecha ISO (AAAA-MM-DD)")
    antes_fila = conn.execute(
        "SELECT kind, amount, sentido FROM nutrient_target WHERE person_id = ? AND metric_key = ? "
        "AND start_day = ?", (person_id, metric_key, start_day)).fetchone()
    antes = dict(antes_fila) if antes_fila is not None else None
    despues = {"metric_key": metric_key, "kind": kind, "amount": amount, "sentido": sentido,
               "start_day": start_day}
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_objetivo_nutriente", despues,
                                 antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _numero_cantidad_comida(t: T, metric_key: str, valor, *, clave_error: str) -> float:
    """BODY-COMIDA-1 ruling 3: door validation for one cantidad's `amount`, the same shape
    `_numero_entreno` (BODY-ENTRENO-NUMEROS-1 ruling 2) built for reps/peso_kg -- a bool is refused
    even though Python's `bool` is technically an `int`; an int/float already of the right shape
    passes through; a plain numeric string ("150", "12.5") is coerced, since outside callers
    (body.mcp included) often send numbers as strings; anything else is refused naming the
    metric's own catalogue label (BODY-COMIDA-1-M fix 3: "calories", never the raw key "kcal"),
    lower-cased since it sits mid-sentence. Zero or negative is refused the same way:
    `ingesta.amount`'s own CHECK (> 0, BODY-PROTEINA-1) never changed, so a meal's amount is held
    to the same rule as a standalone intake entry's."""
    etiqueta = t("metric." + metric_key).lower()
    if isinstance(valor, bool):
        raise ToolError(t(clave_error, metrica=etiqueta))
    if isinstance(valor, (int, float)):
        numero = float(valor)
    elif isinstance(valor, str):
        try:
            numero = float(valor.strip())
        except ValueError:
            raise ToolError(t(clave_error, metrica=etiqueta))
    else:
        raise ToolError(t(clave_error, metrica=etiqueta))
    if numero <= 0:
        raise ToolError(t(clave_error, metrica=etiqueta))
    return numero


def _resolver_comensal_nombre(conn: sqlite3.Connection, t: T, autor_person_id: int, nombre: str) -> int:
    """BODY-COMIDA-COMPARTIDA-1 ruling 4: `nombre` resolved against the author's own active
    household, by display_name (case-insensitive) -- never a person_id the model could invent or
    copy from elsewhere. No match, or more than one (two members sharing a display_name), is the
    same plain refusal: the assistant names what the person said, never an internal id."""
    hh = household.household_of(conn, autor_person_id)
    candidatos = []
    if hh is not None:
        for pid in hh.member_ids:
            if pid == autor_person_id:
                continue
            if db.person(conn, pid)["display_name"].strip().casefold() == nombre.strip().casefold():
                candidatos.append(pid)
    if len(candidatos) != 1:
        raise ToolError(t("agente.error.comensal_no_encontrado", nombre=nombre))
    return candidatos[0]


def _validar_comensal_nutrientes(t: T, nutrientes) -> dict:
    """One shared-meal eater's own portion (metric_key -> amount), validated through the SAME door
    `_numero_cantidad_comida` already guards the author's own `cantidades` with."""
    if not isinstance(nutrientes, dict) or not nutrientes:
        raise ToolError("se necesita al menos una cantidad para cada comensal")
    catalogo = set(db.INGESTA_METRIC_KEYS)
    limpio = {}
    for metric_key, valor in nutrientes.items():
        if metric_key not in catalogo:
            raise ToolError(f"ingesta desconocida o no acumulable: {metric_key!r}")
        limpio[metric_key] = _numero_cantidad_comida(t, metric_key, valor,
                                                       clave_error="agente.error.comida_cantidad_invalida")
    return limpio


def _registrar_comida(conn: sqlite3.Connection, person_id: int, conversacion_id: int, descripcion: str,
                       cantidades: list, day: str | None = None, hora: str | None = None,
                       compartida_con: list | None = None, momento: str | None = None) -> dict:
    """Proposes one spoken meal as a single call (BODY-COMIDA-1 ruling 3): estimate every nutrient
    you can from the person's own words -- never ask them to weigh food -- say which amounts are
    estimates, and name caffeine/alcohol with the time when the person gives one. Insert-shaped,
    like `registrar_ingesta`: there is no 'before'. Every number is validated at the door
    (`_numero_cantidad_comida`) before anything is stored: one bad amount refuses the WHOLE call,
    no row written, naming the field -- never a partial meal.

    BODY-COMIDA-COMPARTIDA-1 ruling 4: `compartida_con` names who else ate it -- resolved against
    the household, refused BY NAME (`agente.error.comida_sin_consentimiento`) before the proposal
    is even created when one of them has not turned on receiving shared meals, the same "say so
    plainly" rule as every other door here. The proposal is still addressed to the author alone;
    applying it is what actually shares the meal (body/agente/propuestas.py).

    BODY-HOGAR-ESCRIBE-1-W ruling 3: `momento` names which of the person's own meal moments this
    is (any clave `comidas.momentos` returns for them); left out, the moment is derived from
    `ts_local` as before this argument existed. Refused BY NAME when it is not one of theirs."""
    descripcion = (descripcion or "").strip()
    if not descripcion:
        raise ToolError("falta la descripción de la comida")
    if not cantidades:
        raise ToolError("se necesita al menos una cantidad")
    t = T(db.person(conn, person_id)["language"])
    catalogo = set(db.INGESTA_METRIC_KEYS)
    vistas = set()
    limpias = []
    for c in cantidades:
        if not isinstance(c, dict):
            raise ToolError("cada cantidad debe ser un objeto con metric_key y amount")
        metric_key = c.get("metric_key")
        if metric_key not in catalogo:
            raise ToolError(f"ingesta desconocida o no acumulable: {metric_key!r}")
        if metric_key in vistas:
            raise ToolError(f"cada cantidad debe ser de una métrica distinta: {metric_key!r} repetida")
        vistas.add(metric_key)
        amount = _numero_cantidad_comida(t, metric_key, c.get("amount"),
                                          clave_error="agente.error.comida_cantidad_invalida")
        estimado = c.get("estimado", True)
        if not isinstance(estimado, bool):
            raise ToolError("estimado debe ser verdadero o falso")
        limpias.append({"metric_key": metric_key, "amount": amount, "estimado": estimado})
    if momento is not None:
        claves_momento = [m["clave"] for m in comidas.momentos(conn, person_id)]
        if momento not in claves_momento:
            raise ToolError(t("agente.error.comida_momento_invalido", momento=momento,
                               opciones=", ".join(claves_momento)))
    day, hora_hhmmss = _hora_a_ts_local(conn, person_id, day, hora)
    day = day or _hoy_local(conn, person_id)
    _dia_no_futuro(conn, person_id, day)
    ts_local = f"{day}T{hora_hhmmss}" if hora_hhmmss is not None else None
    despues = {"descripcion": descripcion, "cantidades": limpias, "day": day,
               "ts_local": ts_local, "momento": momento}
    if compartida_con:
        comensales_limpios = []
        vistos_pid = set()
        for c in compartida_con:
            if not isinstance(c, dict):
                raise ToolError("cada comensal debe ser un objeto con nombre y nutrientes")
            pid = _resolver_comensal_nombre(conn, t, person_id, c.get("nombre", ""))
            if pid in vistos_pid:
                raise ToolError(f"cada comensal debe aparecer una vez: {c.get('nombre')!r} repetido")
            vistos_pid.add(pid)
            if not compartir.recibe_comidas(conn, pid):
                nombre_mostrado = db.person(conn, pid)["display_name"]
                raise ToolError(t("agente.error.comida_sin_consentimiento", nombre=nombre_mostrado))
            nutrientes_limpios = _validar_comensal_nutrientes(t, c.get("nutrientes"))
            comensales_limpios.append({"person_id": pid, "nutrientes": nutrientes_limpios,
                                        "porcion_texto": c.get("porcion_texto")})
        despues["compartida_con"] = comensales_limpios
    registro = propuestas.crear(conn, person_id, conversacion_id, "registrar_comida", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _corregir_comida(conn: sqlite3.Connection, person_id: int, conversacion_id: int, comida_id: int,
                      hora: str | None = None, day: str | None = None, descripcion: str | None = None,
                      cantidades: list | None = None) -> dict:
    """Proposes correcting a MEAL as a unit (ORCH-CARLOS-DELEGATES-TIER-1-1, BODY-COMIDA-CORREGIR-1:
    one correction the person means as one change is ONE proposal, never one per nutrient row).
    A field left out keeps its current value, the same convention `_corregir_periodo` already
    uses; `cantidades`, when given, REPLACES the meal's whole set of amounts, never merged with
    what it already had -- the same door-validated, all-or-nothing shape `_registrar_comida`
    itself writes with, so one bad amount refuses the WHOLE call, nothing proposed. `antes`/
    `despues` each carry the meal's FULL state (day, ts_local, descripcion, every amount), so
    `propuestas.deshacer` can restore it exactly through the same writer (`db.corregir_comida`).
    For a single standalone intake row that is not part of a meal, `corregir_ingesta`/
    `borrar_ingesta` stay the way to change it."""
    if hora is None and day is None and descripcion is None and cantidades is None:
        raise ToolError("se necesita al menos un campo para corregir")
    fila = conn.execute(
        "SELECT day, ts_local, descripcion FROM comida WHERE id = ? AND person_id = ?",
        (comida_id, person_id)).fetchone()
    if fila is None:
        raise ToolError(f"comida desconocida: {comida_id!r}")
    cantidades_actuales = [
        {"metric_key": r["metric_key"], "amount": r["amount"], "estimado": bool(r["estimado"])}
        for r in conn.execute(
            "SELECT metric_key, amount, estimado FROM ingesta WHERE comida_id = ? ORDER BY id", (comida_id,))
    ]
    day_derivado, hora_hhmmss = _hora_a_ts_local(conn, person_id, day, hora)
    nuevo_day = day_derivado if day_derivado is not None else fila["day"]
    _dia_no_futuro(conn, person_id, nuevo_day)
    if hora_hhmmss is not None:
        nuevo_ts_local = f"{nuevo_day}T{hora_hhmmss}"
    elif day is not None and fila["ts_local"]:
        nuevo_ts_local = f"{nuevo_day}T{fila['ts_local'][11:]}"
    elif day is not None:
        nuevo_ts_local = None
    else:
        nuevo_ts_local = fila["ts_local"]
    if descripcion is not None:
        descripcion = descripcion.strip()
        if not descripcion:
            raise ToolError("falta la descripción de la comida")
        nueva_descripcion = descripcion
    else:
        nueva_descripcion = fila["descripcion"]
    if cantidades is not None:
        if not cantidades:
            raise ToolError("se necesita al menos una cantidad")
        t = T(db.person(conn, person_id)["language"])
        catalogo = set(db.INGESTA_METRIC_KEYS)
        vistas = set()
        nuevas_cantidades = []
        for c in cantidades:
            if not isinstance(c, dict):
                raise ToolError("cada cantidad debe ser un objeto con metric_key y amount")
            metric_key = c.get("metric_key")
            if metric_key not in catalogo:
                raise ToolError(f"ingesta desconocida o no acumulable: {metric_key!r}")
            if metric_key in vistas:
                raise ToolError(f"cada cantidad debe ser de una métrica distinta: {metric_key!r} repetida")
            vistas.add(metric_key)
            amount = _numero_cantidad_comida(t, metric_key, c.get("amount"),
                                              clave_error="agente.error.comida_cantidad_invalida")
            estimado = c.get("estimado", True)
            if not isinstance(estimado, bool):
                raise ToolError("estimado debe ser verdadero o falso")
            nuevas_cantidades.append({"metric_key": metric_key, "amount": amount, "estimado": estimado})
    else:
        nuevas_cantidades = cantidades_actuales
    antes = {"day": fila["day"], "ts_local": fila["ts_local"], "descripcion": fila["descripcion"],
             "cantidades": cantidades_actuales}
    despues = {"comida_id": comida_id, "day": nuevo_day, "ts_local": nuevo_ts_local,
               "descripcion": nueva_descripcion, "cantidades": nuevas_cantidades}
    registro = propuestas.crear(conn, person_id, conversacion_id, "corregir_comida", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _numero_checkin(t: T, metric_key: str, valor, *, clave_error: str) -> int | None:
    """BODY-CHECKIN-1 ruling 3: door validation for mood/energy/stress_felt, the same shape
    `_numero_cantidad_comida` built for a meal's amount -- None passes through (the field is
    optional); a bool is refused; an int/float already a whole number passes through; a plain
    numeric string ("4") is coerced; anything else ("4-5", "4.5", "") is refused naming the
    field's own catalogue label. 1-5 inclusive, refused the same way outside that range."""
    if valor is None:
        return None
    etiqueta = t("metric." + metric_key).lower()
    if isinstance(valor, bool):
        raise ToolError(t(clave_error, metrica=etiqueta))
    if isinstance(valor, int):
        numero = valor
    elif isinstance(valor, float):
        if not valor.is_integer():
            raise ToolError(t(clave_error, metrica=etiqueta))
        numero = int(valor)
    elif isinstance(valor, str):
        try:
            numero = int(valor.strip())
        except ValueError:
            raise ToolError(t(clave_error, metrica=etiqueta))
    else:
        raise ToolError(t(clave_error, metrica=etiqueta))
    if not (1 <= numero <= 5):
        raise ToolError(t(clave_error, metrica=etiqueta))
    return numero


def _registrar_checkin(conn: sqlite3.Connection, person_id: int, conversacion_id: int, momento: str,
                        day: str | None = None, hora: str | None = None, mood=None, energy=None,
                        stress_felt=None, nota: str | None = None) -> dict:
    """Proposes a 10-second self-report for a day (BODY-CHECKIN-1): mood, energy and stress_felt,
    each 1-5, plus an optional one-line note -- at least one of the four must be given. Every
    number is validated at the door (`_numero_checkin`) before anything is proposed, naming the
    field, so one bad value refuses the whole call. Insert-shaped, like `registrar_comida`: there
    is no 'before'. The note is private by default (ruling 4) -- carried in `despues` only so
    `propuestas.aplicar` can write it, never read back by any tool afterwards."""
    if momento not in MOMENTOS_CHECKIN:
        raise ToolError(f"momento desconocido: {momento!r}")
    t = T(db.person(conn, person_id)["language"])
    mood = _numero_checkin(t, "mood", mood, clave_error="agente.error.checkin_valor_invalido")
    energy = _numero_checkin(t, "energy", energy, clave_error="agente.error.checkin_valor_invalido")
    stress_felt = _numero_checkin(t, "stress_felt", stress_felt, clave_error="agente.error.checkin_valor_invalido")
    nota = (nota or "").strip() or None
    if mood is None and energy is None and stress_felt is None and nota is None:
        raise ToolError(t("agente.error.checkin_vacio"))
    day, hora_hhmmss = _hora_a_ts_local(conn, person_id, day, hora)
    day = day or _hoy_local(conn, person_id)
    _dia_no_futuro(conn, person_id, day)
    ts_local = f"{day}T{hora_hhmmss}" if hora_hhmmss is not None else None
    despues = {"day": day, "ts_local": ts_local, "momento": momento,
               "mood": mood, "energy": energy, "stress_felt": stress_felt, "nota": nota}
    registro = propuestas.crear(conn, person_id, conversacion_id, "registrar_checkin", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _marcar_periodo(conn: sqlite3.Connection, person_id: int, conversacion_id: int, kind: str, start_day: str,
                     end_day: str | None = None, note: str | None = None, zonas: list | None = None) -> dict:
    """Proposes marking a stretch of days as a period (BODY-PERIODO-1): illness, travel or injury,
    so Body leaves those days out when it compares with the usual, never reading them as a habit
    or experiment effect. OFFER this when the person says they are ill, travelling or injured --
    never create it silently. Insert-shaped, like registrar_ingesta: there is no 'before'.
    start_day may be in the past, bounded like _proponer_experimento's own fecha_inicio check: not
    before the person's first data day, not in the future. `zonas` (BODY-LESION-ZONA-1 ruling 1):
    set it when the person names a limited body part."""
    _dia_no_futuro(conn, person_id, start_day)
    dias = db.days_with_data(conn, person_id)
    if dias and start_day < dias[-1]:
        raise ToolError(f"start_day no puede ser anterior a tu primer día con datos: {dias[-1]!r}")
    if end_day is not None:
        _dia_no_futuro(conn, person_id, end_day)
        if end_day < start_day:
            raise ToolError("end_day no puede ser anterior a start_day")
    note = (note or "").strip() or None
    t = T(db.person(conn, person_id)["language"])
    zonas = _zonas_periodo(t, zonas) if zonas is not None else None
    despues = {"kind": kind, "start_day": start_day, "end_day": end_day, "note": note, "zonas": zonas}
    registro = propuestas.crear(conn, person_id, conversacion_id, "marcar_periodo", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _registrar_altitud(conn: sqlite3.Connection, person_id: int, conversacion_id: int, es_casa: bool,
                        start_day: str, altitud_m: int, end_day: str | None = None,
                        viaje_ref: str | None = None) -> dict:
    """Proposes recording one stretch's own altitude -- home (es_casa=True) or a stay away from
    it (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1/4, second stage of BODY-MINUTO-ANALISIS-4-INV).
    An altitude and dates only: never a place name, never coordinates. Insert-shaped, like
    marcar_periodo: there is no 'before'. `fuente` is always 'asistente' here -- never a
    model-facing field. Refused when it would overlap one of this person's own existing rows of
    the SAME kind (home with home, stay with stay) -- db.validar_altitud_sin_solape is the one
    validator both this proposal and its later apply (body/agente/propuestas.py's own
    _aplicar_registrar_altitud) run against, so a conflict is caught here, before any proposal is
    even created."""
    if not isinstance(es_casa, bool):
        raise ToolError("es_casa debe ser verdadero o falso")
    _dia_no_futuro(conn, person_id, start_day)
    if end_day is not None:
        _dia_no_futuro(conn, person_id, end_day)
        if end_day < start_day:
            raise ToolError("end_day no puede ser anterior a start_day")
    if not isinstance(altitud_m, int) or isinstance(altitud_m, bool) or not (-500 <= altitud_m <= 9000):
        raise ToolError("altitud_m debe ser un entero entre -500 y 9000")
    try:
        db.validar_altitud_sin_solape(conn, person_id, es_casa, start_day, end_day)
    except ValueError as e:
        raise ToolError(e.args[0])
    despues = {"es_casa": es_casa, "start_day": start_day, "end_day": end_day, "altitud_m": altitud_m,
               "fuente": "asistente", "viaje_ref": viaje_ref}
    registro = propuestas.crear(conn, person_id, conversacion_id, "registrar_altitud", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _leer_altitudes(conn: sqlite3.Connection, person_id: int, desde: str | None = None,
                     hasta: str | None = None) -> dict:
    """This person's own home and stay rows overlapping [desde, hasta] (ORCH-CARLOS-DELEGATES-
    TIER-1-1 ruling 4) -- an altitude and dates only, read through db.altitud_estancia_rango.
    'desde' defaults to the same 365-day window comparar_noche/buscar_noches already use; an open
    row (no end_day) shows regardless of how far back that default reaches."""
    person = db.person(conn, person_id)
    hoy = fechas.hoy_local(person)
    if hasta is not None:
        try:
            hasta_d = date.fromisoformat(hasta)
        except ValueError:
            raise ToolError("'hasta' debe ser una fecha ISO (AAAA-MM-DD)")
        if hasta_d > hoy:
            raise ToolError("'hasta' no puede ser una fecha futura")
    else:
        hasta_d = hoy
        hasta = hasta_d.isoformat()
    if desde is not None:
        try:
            desde_d = date.fromisoformat(desde)
        except ValueError:
            raise ToolError("'desde' debe ser una fecha ISO (AAAA-MM-DD)")
        if desde_d > hasta_d:
            raise ToolError("'desde' no puede ser posterior a 'hasta'")
    else:
        desde = (hasta_d - timedelta(noche_rasgos.HISTORIA_DIAS_DEFECTO)).isoformat()
    filas = db.altitud_estancia_rango(conn, person_id, desde, hasta)
    return {"altitudes": [
        {"id": f["id"], "es_casa": bool(f["es_casa"]), "start_day": f["start_day"], "end_day": f["end_day"],
         "altitud_m": f["altitud_m"], "fuente": f["fuente"], "viaje_ref": f["viaje_ref"]}
        for f in filas
    ]}


def _cerrar_periodo(conn: sqlite3.Connection, person_id: int, conversacion_id: int, period_id: int,
                     end_day: str) -> dict:
    """Proposes closing an open period, saying since when the person got better (BODY-PERIODO-1):
    "I'm better since Thursday". despues carries the period's own kind/start_day/note/zonas too, so
    the card and propuestas.TABLA's writer never need a second read."""
    fila = conn.execute("SELECT kind, start_day, end_day, note, zonas FROM period WHERE id = ? AND person_id = ?",
                        (period_id, person_id)).fetchone()
    if fila is None:
        raise ToolError(f"período desconocido: {period_id!r}")
    if fila["end_day"] is not None:
        raise ToolError(f"ese período ya está cerrado: {period_id!r}")
    _dia_no_futuro(conn, person_id, end_day)
    if end_day < fila["start_day"]:
        raise ToolError("end_day no puede ser anterior al inicio del período")
    despues = {"period_id": period_id, "kind": fila["kind"], "start_day": fila["start_day"], "end_day": end_day,
               "note": fila["note"], "zonas": json.loads(fila["zonas"]) if fila["zonas"] else None}
    registro = propuestas.crear(conn, person_id, conversacion_id, "cerrar_periodo", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _corregir_periodo(conn: sqlite3.Connection, person_id: int, conversacion_id: int, period_id: int,
                       kind: str | None = None, start_day: str | None = None, end_day: str | None = None,
                       note: str | None = None, zonas: list | None = None) -> dict:
    """Proposes correcting the kind, the dates, the note or the zonas of one of the person's own
    periods (BODY-PERIODO-1; zonas added by BODY-LESION-ZONA-1 ruling 1). A field left out keeps
    its current value, the same convention `_proponer_cambio_meta` already uses -- for zonas, an
    empty list clears it instead (the same "" convention `note` already uses). `antes` carries the
    full prior row, so `propuestas.deshacer` can restore it exactly."""
    if kind is None and start_day is None and end_day is None and note is None and zonas is None:
        raise ToolError("se necesita al menos un campo para corregir")
    fila = conn.execute("SELECT kind, start_day, end_day, note, zonas FROM period WHERE id = ? AND person_id = ?",
                        (period_id, person_id)).fetchone()
    if fila is None:
        raise ToolError(f"período desconocido: {period_id!r}")
    nuevo_kind = kind if kind is not None else fila["kind"]
    nuevo_inicio = start_day if start_day is not None else fila["start_day"]
    nuevo_fin = end_day if end_day is not None else fila["end_day"]
    _dia_no_futuro(conn, person_id, nuevo_inicio)
    if nuevo_fin is not None:
        _dia_no_futuro(conn, person_id, nuevo_fin)
        if nuevo_fin < nuevo_inicio:
            raise ToolError("end_day no puede ser anterior a start_day")
    t = T(db.person(conn, person_id)["language"])
    zonas_antes = json.loads(fila["zonas"]) if fila["zonas"] else None
    nuevas_zonas = _zonas_periodo(t, zonas) if zonas is not None else zonas_antes
    nueva_nota = (note.strip() or None) if note is not None else fila["note"]
    antes = {"kind": fila["kind"], "start_day": fila["start_day"], "end_day": fila["end_day"], "note": fila["note"],
             "zonas": zonas_antes}
    despues = {"period_id": period_id, "kind": nuevo_kind, "start_day": nuevo_inicio, "end_day": nuevo_fin,
               "note": nueva_nota, "zonas": nuevas_zonas}
    registro = propuestas.crear(conn, person_id, conversacion_id, "corregir_periodo", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _borrar_periodo(conn: sqlite3.Connection, person_id: int, conversacion_id: int, period_id: int) -> dict:
    """Proposes deleting one of the person's own periods (BODY-PERIODO-1). `antes` carries the
    whole entry, so `propuestas.deshacer` can restore it (as a new row with the same values, the
    same "functionally equivalent" restoration `borrar_ingesta`'s own undo uses)."""
    fila = conn.execute("SELECT kind, start_day, end_day, note, zonas FROM period WHERE id = ? AND person_id = ?",
                        (period_id, person_id)).fetchone()
    if fila is None:
        raise ToolError(f"período desconocido: {period_id!r}")
    antes = {"kind": fila["kind"], "start_day": fila["start_day"], "end_day": fila["end_day"], "note": fila["note"],
             "zonas": json.loads(fila["zonas"]) if fila["zonas"] else None}
    despues = {"period_id": period_id}
    registro = propuestas.crear(conn, person_id, conversacion_id, "borrar_periodo", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _marcar_habito(conn: sqlite3.Connection, person_id: int, conversacion_id: int, habit_id: int, day: str,
                    actividad: dict | None = None, nota: str | None = None) -> dict:
    """Proposes checking one habit off for a day, optionally naming the watch activity that is
    its evidence (BODY-ENTRENO-HABITO-AUTO-1, BODY-ENTRENO-1-INV slice 5): ball sports is one use
    of this generic tool, never a hardcoded activity-type list. `antes` carries that day's existing
    habit_check row for this person (done, note) or None, so `propuestas.deshacer` can restore it
    exactly. When the habit cites a practice (BODY-HISTORIA-VIDA-2 ruling 4), `despues` also carries
    that practice's own latest week as `practica_evidencia` -- what the watch already shows, never
    a reason to mark it: the person's own tap is what this tool proposes either way."""
    _dia_no_futuro(conn, person_id, day)
    h = conn.execute(
        "SELECT id, goal_id, title, assignee_person_id, active, practica_id FROM habit WHERE id = ?",
        (habit_id,)).fetchone()
    if h is None:
        raise ToolError(f"hábito desconocido: {habit_id!r}")
    if goals.goal_access(conn, h["goal_id"], person_id) is None:
        raise ToolError(f"hábito desconocido: {habit_id!r}")
    if not h["active"]:
        raise ToolError(f"hábito inactivo: {habit_id!r}")
    if h["assignee_person_id"] is not None and h["assignee_person_id"] != person_id:
        raise ToolError(f"ese hábito no es tuyo: {habit_id!r}")
    actividad_despues = None
    if actividad is not None:
        source, source_id = actividad.get("source"), actividad.get("source_id")
        a = conn.execute(
            "SELECT name, type, duration_s, day FROM activity WHERE person_id = ? AND source = ? "
            "AND source_id = ?", (person_id, source, source_id)).fetchone()
        if a is None or a["day"] != day:
            raise ToolError(f"actividad desconocida ese día: {source!r}/{source_id!r}")
        actividad_despues = {"source": source, "source_id": source_id, "nombre": a["name"], "tipo": a["type"],
                              "duracion_s": a["duration_s"]}
    nota = (nota or "").strip() or None
    anterior = conn.execute(
        "SELECT done, note FROM habit_check WHERE habit_id = ? AND person_id = ? AND day = ?",
        (habit_id, person_id, day)).fetchone()
    antes = {"done": bool(anterior["done"]), "note": anterior["note"]} if anterior is not None else None
    practica_evidencia = None
    if h["practica_id"] is not None and practicas.propia_activa(conn, person_id, h["practica_id"]):
        e = practicas.estado(conn, person_id, h["practica_id"])
        if e["semanas"]:
            s = e["semanas"][-1]
            practica_evidencia = {"hecho": s["hecho"], "meta": e["definicion"]["meta_dias_semana"]}
    despues = {"habit_id": habit_id, "day": day, "done": True, "note": nota, "habito_titulo": h["title"],
               "actividad": actividad_despues, "practica_evidencia": practica_evidencia}
    registro = propuestas.crear(conn, person_id, conversacion_id, "marcar_habito", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _dias_marcados_experimento(conn: sqlite3.Connection, person_id: int, row: sqlite3.Row) -> list[dict]:
    if row["habito_id"] is not None:
        rows = conn.execute(
            "SELECT day, done FROM habit_check WHERE habit_id = ? AND person_id = ? ORDER BY day",
            (row["habito_id"], person_id)).fetchall()
        return [{"dia": r["day"], "ocurrio": bool(r["done"])} for r in rows]
    rows = conn.execute(
        "SELECT day, ocurrio FROM experimento_registro WHERE experimento_id = ? AND person_id = ? ORDER BY day",
        (row["id"], person_id)).fetchall()
    return [{"dia": r["day"], "ocurrio": bool(r["ocurrio"])} for r in rows]


def _experimento_dict(conn: sqlite3.Connection, person_id: int, row: sqlite3.Row, hoy: date) -> dict:
    ver = experimentos.veredicto(conn, person_id, row["id"], hoy=hoy)
    return {
        "id": row["id"], "titulo": row["titulo"], "habito_id": row["habito_id"],
        "metricas": json.loads(row["metricas"]), "ventana": json.loads(row["ventana"]),
        "filtro_dias": json.loads(row["filtro_dias"]), "dias_prueba_objetivo": row["dias_prueba_objetivo"],
        "fecha_inicio": row["fecha_inicio"], "fecha_fin": row["fecha_fin"], "estado": row["estado"],
        "dias_marcados": _dias_marcados_experimento(conn, person_id, row),
        "veredicto": ver["metricas"],
    }


def _leer_experimentos(conn: sqlite3.Connection, person_id: int, experimento_id: int | None = None) -> dict:
    """Active experiments first, then closed (BODY-EXPERIMENTOS-REGISTRO-1): each with its own
    setup, the days marked so far and, per metric, `experimentos.veredicto`'s own figures --
    `resultado` ('mas_bajo' | 'mas_alto' | 'sin_cambio' | 'pocos_dias'; 'aun_no' stays a valid code
    but nothing produces it today, BODY-EXPERIMENTOS-INTRADIA-MODELO-1), n_prueba,
    n_base, media_prueba, media_base, diferencia. `nivel: 'resumen'`, by analogy with `leer_meta`:
    both answer with a figure already compared over many days -- a mean, a target's progress --
    never a single day's raw reading, which stays behind 'completo'."""
    hoy = fechas.hoy_local(db.person(conn, person_id))
    if experimento_id is not None:
        row = conn.execute(
            "SELECT * FROM experimento WHERE id = ? AND person_id = ?", (experimento_id, person_id)
        ).fetchone()
        if row is None:
            return {"experimentos": []}
        return {"experimentos": [_experimento_dict(conn, person_id, row, hoy)]}
    filas = list(db.experimentos(conn, person_id, "activo")) + list(db.experimentos(conn, person_id, "cerrado"))
    return {"experimentos": [_experimento_dict(conn, person_id, r, hoy) for r in filas]}


def _convertir_lineas_reloj(lineas) -> None:
    for linea in lineas or ():
        if linea.get("medida") not in _MEDIDAS_HORA:
            continue
        if linea.get("valor") is not None:
            linea["valor"] = _hora_desde_minutos(linea["valor"])
        if linea.get("real") is not None:
            linea["real"] = _hora_desde_minutos(linea["real"])


def _convertir_plan_salida(plan: dict) -> dict:
    """Ruling 1 (out direction): every clock-measure figure a `horizonte.resumen` dict carries --
    the current week's lineas and the next checkpoint's criterios, each a `valor`/`real` pair --
    as "HH:MM", never the raw minutes-after-noon integer horizonte stores and returns.
    horizonte.resumen itself is unchanged; this is the tool layer's own conversion of its output."""
    if plan is None:
        return plan
    _convertir_lineas_reloj(plan.get("semana_actual", {}).get("lineas"))
    siguiente = plan.get("siguiente_checkpoint")
    if siguiente:
        _convertir_lineas_reloj(siguiente.get("criterios"))
    return plan


def _leer_planes(conn: sqlite3.Connection, person_id: int) -> dict:
    """Every horizon this person can see (BODY-PLAN-PROPUESTAS-1 ruling 6) -- their own plus any
    shared household milestone, active plans first -- each summarised by `horizonte.resumen` (`nivel:
    'resumen'`, by analogy with `leer_experimentos`: a plan's own figures, never recomputed here).
    `horizonte.horizontes` is the ONE definition of a plan (BODY-PLAN-HOY-1 ruling 1): a milestone
    goal with no `horizonte_goal_id` of its own AND at least one plan row."""
    hoy = fechas.hoy_local(db.person(conn, person_id))
    return {"planes": [_convertir_plan_salida(horizonte.resumen(conn, gid, person_id, hoy=hoy))
                        for gid in horizonte.horizontes(conn, person_id, hoy)]}


def _leer_plan(conn: sqlite3.Connection, person_id: int, goal_id: int, desde: str | None = None,
                hasta: str | None = None) -> dict:
    """One horizon's own summary (BODY-PLAN-PROPUESTAS-1 ruling 6), plus `horizonte.plan_vs_real`
    for [desde, hasta] when both are given. `nivel: 'resumen'`. Access goes through
    `goals.goal_access` first -- `horizonte.resumen` itself does not check it for the horizon
    goal, only for its children."""
    if goals.goal_access(conn, goal_id, person_id) is None:
        return {"encontrada": False}
    hoy = fechas.hoy_local(db.person(conn, person_id))
    plan = horizonte.resumen(conn, goal_id, person_id, hoy=hoy)
    if plan is None:
        return {"encontrada": False}
    out = {"encontrada": True, "plan": _convertir_plan_salida(plan)}
    if desde and hasta:
        semanas = horizonte.plan_vs_real(conn, goal_id, person_id, desde, hasta, hoy=hoy)
        for s in semanas:
            _convertir_lineas_reloj(s.get("lineas"))
        out["plan_vs_real"] = semanas
    # BODY-REGISTRO-COBERTURA-1 ruling 2: the CAPTURE side -- sleep/meals/protein/weight/waist/
    # training logged so far against this plan's own date range, never this plan's own weekly
    # targets (plan_vs_real above is a different question).
    out["registro"] = registro_cobertura.cobertura(conn, person_id, goal_id)
    return out


def _leer_pendientes_registro(conn: sqlite3.Connection, person_id: int, dia: str | None = None) -> dict:
    """BODY-REGISTRO-COBERTURA-1 ruling 2: what the CAPTURE side of every active dated plan is
    still missing on `dia` (today, in the person's own timezone, when not given) -- the desk's own
    daily nudge asks this instead of keeping its own copy of what each stream needs. Never a health
    figure, never a verdict: only which log streams have nothing yet and which have never been used
    at all (`sin_uso`, never counted as a miss)."""
    person = db.person(conn, person_id)
    hoy = fechas.hoy_local(person)
    if dia is not None:
        try:
            dia_d = date.fromisoformat(dia)
        except ValueError:
            raise ToolError("'dia' debe ser una fecha ISO (AAAA-MM-DD)")
    else:
        dia_d = hoy
    return registro_cobertura.pendientes(conn, person_id, dia_d)


def _proponer_experimento(conn: sqlite3.Connection, person_id: int, conversacion_id: int, titulo: str,
                           metricas: list, ventana: dict, filtro_dias: dict, fecha_inicio: str | None = None,
                           fecha_fin: str | None = None, dias_prueba_objetivo: int | None = None,
                           habito_id: int | None = None, horizonte_goal_id: int | None = None) -> dict:
    """Proposes a new experiment (BODY-EXPERIMENTOS-REGISTRO-1): insert-shaped, like
    `planear_entreno` -- there is no 'before'. Validated with `db.validar_experimento` and the same
    habit-ownership rule `db.crear_experimento` itself enforces, BEFORE the proposal is stored, so
    a bad proposal is a ToolError, never a card the person could tap Aplicar on.
    `horizonte_goal_id` (BODY-PLAN-PROPUESTAS-1 ruling 5) is additive: a plan the person owns,
    stored as `experimento.goal_id`."""
    titulo = (titulo or "").strip()
    if not titulo:
        raise ToolError("falta el título del experimento")
    try:
        db.validar_experimento(metricas, ventana, filtro_dias)
    except db.ExperimentoInvalido as e:
        raise ToolError(errores.texto_propio(e))
    if habito_id is not None:
        propio = conn.execute(
            "SELECT 1 FROM habit h JOIN goal g ON g.id = h.goal_id WHERE h.id = ? AND g.person_id = ?",
            (habito_id, person_id)).fetchone()
        if propio is None:
            raise ToolError(f"hábito desconocido: {habito_id!r}")
    if horizonte_goal_id is not None and not _plan_propio(conn, person_id, horizonte_goal_id):
        raise ToolError(f"plan desconocido: {horizonte_goal_id!r}")
    fecha_inicio = fecha_inicio or _hoy_local(conn, person_id)
    _dia_no_futuro(conn, person_id, fecha_inicio)
    dias = db.days_with_data(conn, person_id)
    if dias and fecha_inicio < dias[-1]:
        raise ToolError(f"fecha_inicio no puede ser anterior a tu primer día con datos: {dias[-1]!r}")
    if fecha_fin is not None:
        try:
            date.fromisoformat(fecha_fin)
        except ValueError:
            raise ToolError("fecha_fin debe ser una fecha ISO (AAAA-MM-DD)")
    despues = {"titulo": titulo, "metricas": metricas, "ventana": ventana, "filtro_dias": filtro_dias,
               "fecha_inicio": fecha_inicio, "fecha_fin": fecha_fin, "dias_prueba_objetivo": dias_prueba_objetivo,
               "habito_id": habito_id, "horizonte_goal_id": horizonte_goal_id}
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_experimento", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


_LEER_PRACTICAS_DIAS_RECIENTES = 90


def _practica_resumen_lista(conn: sqlite3.Connection, person_id: int, p: dict) -> dict:
    """One entry per active practice for the no-id list shape: its own definition plus the current
    streak and the most recent week's days -- `practicas.estado`'s own figures, never recomputed
    here, by analogy with `_leer_experimentos`/`_leer_planes`."""
    e = practicas.estado(conn, person_id, p["id"])
    return {
        **p,
        "racha_actual": e["racha_actual"] if e else None,
        "semana_actual": e["semanas"][-1] if e and e["semanas"] else None,
    }


def _leer_practicas(conn: sqlite3.Connection, person_id: int, practica_id: int | None = None) -> dict:
    """BODY-HISTORIA-VIDA-2 ruling 2: without `practica_id`, every active practice (`nivel:
    'resumen'`, by analogy with `leer_experimentos`/`leer_plan`) with its current streak and most
    recent week, plus not-yet-created suggestions (`practicas.sugerencias`) -- nothing is written.
    With `practica_id`, the full detail -- weeks, streaks, temporadas, baseline, all computed over
    `practicas.estado`'s own [desde, hasta] -- bounded here to the last
    `_LEER_PRACTICAS_DIAS_RECIENTES` days, so a long-lived practice does not hand the model a year
    of per-day entries it did not ask to see. `encontrada: False` for another person's practica or
    an unknown id, never a ToolError, by analogy with `_leer_plan`."""
    if practica_id is not None:
        hoy = fechas.hoy_local(db.person(conn, person_id))
        desde = hoy - timedelta(days=_LEER_PRACTICAS_DIAS_RECIENTES - 1)
        e = practicas.estado(conn, person_id, practica_id, desde=desde, hasta=hoy)
        if e is None:
            return {"encontrada": False}
        return {"encontrada": True, "practica": e}
    activas = practicas.listar(conn, person_id)
    return {
        "practicas": [_practica_resumen_lista(conn, person_id, p) for p in activas],
        "sugerencias": practicas.sugerencias(conn, person_id),
    }


def _proponer_practica(conn: sqlite3.Connection, person_id: int, conversacion_id: int, titulo: str,
                        fuente: str, regla: dict, meta_dias_semana: int = 5) -> dict:
    """BODY-HISTORIA-VIDA-2 ruling 3: proposes a new practice -- insert-shaped, like
    `proponer_experimento`, there is no 'before'. Validated with `practicas.FUENTES` and the same
    bounds `practicas.crear` itself checks, BEFORE the proposal is stored, so a bad proposal is a
    ToolError, never a card the person could tap Aplicar on. Apply calls `practicas.crear` with
    `origen='asistente'` (ruling 3)."""
    titulo = (titulo or "").strip()
    if not titulo:
        raise ToolError("falta el título de la práctica")
    if fuente not in practicas.FUENTES:
        raise ToolError(f"fuente inválida: {fuente!r}")
    razon = practicas.FUENTES[fuente](regla)
    if razon:
        raise ToolError(f"regla inválida: {razon}")
    if (not isinstance(meta_dias_semana, int) or isinstance(meta_dias_semana, bool)
            or not (1 <= meta_dias_semana <= 7)):
        raise ToolError("meta_dias_semana debe ser un entero entre 1 y 7")
    despues = {"titulo": titulo, "fuente": fuente, "regla": regla, "meta_dias_semana": meta_dias_semana}
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_practica", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _marcar_experimento(conn: sqlite3.Connection, person_id: int, conversacion_id: int, experimento_id: int,
                         day: str, ocurrio: bool, nota: str | None = None) -> dict:
    """Proposes marking, for a day, whether an experiment's own practice happened
    (BODY-EXPERIMENTOS-REGISTRO-1) -- refused for an experiment that is not the person's own, one
    that is closed, a day outside [fecha_inicio, fecha_fin], and one linked to a habit (its log is
    that habit's own `habit_check`, kept current with `marcar_habito` instead). `antes` carries
    that day's existing registro row (or None), so `propuestas.deshacer` can restore it exactly."""
    if not isinstance(ocurrio, bool):
        raise ToolError("ocurrio debe ser verdadero o falso")
    _dia_no_futuro(conn, person_id, day)
    row = conn.execute(
        "SELECT * FROM experimento WHERE id = ? AND person_id = ?", (experimento_id, person_id)).fetchone()
    if row is None:
        raise ToolError(f"experimento desconocido: {experimento_id!r}")
    if row["estado"] == "cerrado":
        raise ToolError(f"experimento cerrado: {experimento_id!r}")
    if row["habito_id"] is not None:
        raise ToolError("ese experimento está ligado a un hábito: márcalo con marcar_habito")
    if day < row["fecha_inicio"] or (row["fecha_fin"] is not None and day > row["fecha_fin"]):
        raise ToolError(f"day fuera del rango del experimento: {day!r}")
    nota = (nota or "").strip() or None
    anterior = conn.execute(
        "SELECT ocurrio, nota FROM experimento_registro WHERE experimento_id = ? AND person_id = ? AND day = ?",
        (experimento_id, person_id, day)).fetchone()
    antes = {"ocurrio": bool(anterior["ocurrio"]), "nota": anterior["nota"]} if anterior is not None else None
    despues = {"experimento_id": experimento_id, "day": day, "ocurrio": ocurrio, "nota": nota,
               "titulo": row["titulo"]}
    registro = propuestas.crear(conn, person_id, conversacion_id, "marcar_experimento", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _proponer_meta(conn: sqlite3.Connection, person_id: int, conversacion_id: int, deadline: str, titulo: str,
                    tipo: str = "metric", metric_key: str | None = None, direction: str | None = None,
                    target_value=None, habitos: list | None = None, horizonte_goal_id: int | None = None) -> dict:
    """BODY-PLAN-PROPUESTAS-1 ruling 2: `tipo`/`habitos`/`horizonte_goal_id` are additive -- a call
    giving none of them (today's own shape) proposes a metric goal exactly as before. A
    'milestone' needs a deadline (always required) and no metric fields; a 'metric' (the default)
    needs all three."""
    titulo = (titulo or "").strip()
    if not titulo:
        raise ToolError("falta el título de la meta")
    if tipo not in ("metric", "milestone"):
        raise ToolError(f"tipo inválido: {tipo!r}; debe ser 'metric' o 'milestone'")
    try:
        date.fromisoformat(deadline)
    except ValueError:
        raise ToolError("deadline debe ser una fecha ISO (AAAA-MM-DD)")
    if tipo == "milestone":
        if metric_key is not None or direction is not None or target_value is not None:
            raise ToolError("una meta de tipo milestone no lleva metric_key/direction/target_value")
    else:
        if direction not in DIRECTIONS:
            raise ToolError(f"direction inválida: {direction!r}; debe ser una de {DIRECTIONS}")
        if conn.execute("SELECT 1 FROM metric_def WHERE key = ?", (metric_key,)).fetchone() is None:
            raise ToolError(f"metric_key desconocida: {metric_key!r}")
        if target_value is None:
            raise ToolError("falta target_value")
    habitos_v = _validar_habitos_plan(conn, person_id, habitos)
    if horizonte_goal_id is not None and not _plan_propio(conn, person_id, horizonte_goal_id):
        raise ToolError(f"plan desconocido: {horizonte_goal_id!r}")
    despues = {"tipo": tipo, "metric_key": metric_key, "direction": direction,
               "target_value": float(target_value) if target_value is not None else None,
               "deadline": deadline, "titulo": titulo, "start_day": _hoy_local(conn, person_id),
               "habitos": habitos_v, "horizonte_goal_id": horizonte_goal_id}
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_meta", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _proponer_plan(conn: sqlite3.Connection, person_id: int, conversacion_id: int, titulo: str, start_day: str,
                    deadline: str, why: str | None = None, metas_finales: list | None = None,
                    habitos: list | None = None, fases: list | None = None, reglas: list | None = None,
                    semanas: list | None = None, metas_semanales: list | None = None,
                    checkpoints: list | None = None) -> dict:
    """Ruling 1: a whole plan is ONE card. Validates every part BEFORE the proposal is stored
    (every list defaults to empty), so a bad plan is a ToolError, never a card the person could
    tap Aplicar on. Apply writes all of it in one transaction (`db.crear_plan_horizonte`).
    BODY-PLAN-FASES-2 ruling 3: fases are validated FIRST, so reglas[i].fase_index can be checked
    against their own count."""
    titulo = (titulo or "").strip()
    if not titulo:
        raise ToolError("falta el título del plan")
    try:
        date.fromisoformat(start_day)
        date.fromisoformat(deadline)
    except ValueError:
        raise ToolError("start_day/deadline deben ser fechas ISO (AAAA-MM-DD)")
    if deadline <= start_day:
        raise ToolError("deadline debe ser posterior a start_day")
    fases_v = _validar_fases_plan(fases, start_day, deadline)
    despues = {
        "titulo": titulo, "why": (why or "").strip() or None, "start_day": start_day, "deadline": deadline,
        "metas_finales": _validar_metas_finales_plan(conn, metas_finales),
        "habitos": _validar_habitos_plan(conn, person_id, habitos),
        "fases": fases_v,
        "reglas": _validar_reglas_plan(conn, reglas, n_fases=len(fases_v)),
        "semanas": _validar_semanas_plan(semanas, start_day, deadline),
        "metas_semanales": _validar_metas_semanales_plan(conn, metas_semanales, start_day, deadline),
        "checkpoints": _validar_checkpoints_plan(conn, checkpoints, start_day, deadline),
    }
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_plan", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _ampliar_plan(conn: sqlite3.Connection, person_id: int, conversacion_id: int, goal_id: int,
                   fases: list | None = None, reglas: list | None = None, semanas: list | None = None,
                   metas_semanales: list | None = None, checkpoints: list | None = None) -> dict:
    """Ruling 4: insert-only additions to an existing owned horizon. A duplicate (week, medida)
    against an existing vigente weekly line is refused, naming `corregir_meta_plan` instead.
    BODY-PLAN-FASES-2 ruling 3: a new phase must also clear the plan's EXISTING phases (never just
    the other new ones) -- `_validar_fases_plan` checks the new list is internally sound, then this
    checks it against what the plan already has, through the same ONE validator. reglas[i].fase_index
    names a position in THIS call's own fases list only, never an older phase (db.ampliar_plan_horizonte's
    own rule)."""
    goal = conn.execute(
        "SELECT start_day, deadline FROM goal WHERE id = ? AND person_id = ? AND kind = 'milestone'",
        (goal_id, person_id)).fetchone()
    if goal is None:
        raise ToolError(f"plan desconocido: {goal_id!r}")
    start_day, deadline = goal["start_day"], goal["deadline"]
    fases_v = _validar_fases_plan(fases, start_day, deadline)
    if fases_v:
        existentes_fases = [dict(f) for f in conn.execute(
            "SELECT titulo, start_day, end_day FROM horizonte_fase WHERE horizonte_goal_id = ?", (goal_id,))]
        try:
            db.validar_fases_horizonte(existentes_fases + fases_v, start_day, deadline)
        except ValueError as e:
            raise ToolError(e.args[0])
    reglas_v = _validar_reglas_plan(conn, reglas, n_fases=len(fases_v))
    semanas_v = _validar_semanas_plan(semanas, start_day, deadline)
    existentes = {(r["week_start"], r["medida"]) for r in conn.execute(
        "SELECT week_start, medida FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? AND vigente = 1",
        (goal_id,))}
    metas_semanales_v = _validar_metas_semanales_plan(conn, metas_semanales, start_day, deadline,
                                                        existentes=existentes)
    checkpoints_v = _validar_checkpoints_plan(conn, checkpoints, start_day, deadline)
    if not (fases_v or reglas_v or semanas_v or metas_semanales_v or checkpoints_v):
        raise ToolError("no hay nada para añadir")
    despues = {"goal_id": goal_id, "fases": fases_v, "reglas": reglas_v, "semanas": semanas_v,
               "metas_semanales": metas_semanales_v, "checkpoints": checkpoints_v}
    registro = propuestas.crear(conn, person_id, conversacion_id, "ampliar_plan", despues, objetivo_id=goal_id)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _corregir_meta_plan(conn: sqlite3.Connection, person_id: int, conversacion_id: int, meta_id: int, valor,
                         nota: str | None = None) -> dict:
    """Ruling 3: a correction is another append, never an UPDATE of the weekly target's own value.
    `antes`/`despues` carry the full before/after so the card can show 'before -> after' with the
    measure's unit, and so undo can append the restoring third row."""
    fila = conn.execute(
        "SELECT horizonte_goal_id, week_start, medida, sentido, valor, nota, vigente FROM horizonte_meta_semanal "
        "WHERE id = ? AND person_id = ?", (meta_id, person_id)).fetchone()
    if fila is None:
        raise ToolError(f"meta semanal desconocida: {meta_id!r}")
    if not fila["vigente"]:
        raise ToolError("esa meta semanal ya fue corregida")
    if fila["medida"] in _MEDIDAS_HORA:
        valor = _valor_reloj("valor", valor)
    else:
        try:
            valor = float(valor)
        except (TypeError, ValueError):
            raise ToolError("valor debe ser un número")
    nota = (nota or "").strip() or None
    antes = {"meta_id": meta_id, "valor": fila["valor"], "nota": fila["nota"], "medida": fila["medida"],
             "sentido": fila["sentido"], "week_start": fila["week_start"],
             "horizonte_goal_id": fila["horizonte_goal_id"]}
    despues = {"meta_id": meta_id, "valor": valor, "nota": nota, "medida": fila["medida"],
               "sentido": fila["sentido"], "week_start": fila["week_start"],
               "horizonte_goal_id": fila["horizonte_goal_id"]}
    registro = propuestas.crear(conn, person_id, conversacion_id, "corregir_meta_plan", despues,
                                 objetivo_id=fila["horizonte_goal_id"], antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _corregir_fase(conn: sqlite3.Connection, person_id: int, conversacion_id: int, fase_id: int,
                    titulo: str | None = None, start_day: str | None = None, end_day: str | None = None,
                    nota: str | None = None, quitar: bool = False) -> dict:
    """BODY-PLAN-FASES-2 ruling 3: the same update-shaped pattern as `_corregir_meta_plan`, except
    a phase's dates carry no append-only history of their own (`db.actualizar_fase_horizonte` is a
    plain UPDATE) -- `antes` carries the full row, so undo restores it exactly either way.
    `quitar` (never combined with a field edit) detaches this plan's rules from the phase rather
    than deleting them; the phase itself is removed, not soft-dropped, since nothing else of the
    person's own history ever points at a phase id the way it points at a goal or a habit."""
    fila = conn.execute(
        "SELECT horizonte_goal_id, titulo, start_day, end_day, nota, orden, creado_en FROM horizonte_fase "
        "WHERE id = ? AND person_id = ?", (fase_id, person_id)).fetchone()
    if fila is None:
        raise ToolError(f"fase desconocida: {fase_id!r}")
    antes = {"fase_id": fase_id, "horizonte_goal_id": fila["horizonte_goal_id"], "titulo": fila["titulo"],
             "start_day": fila["start_day"], "end_day": fila["end_day"], "nota": fila["nota"],
             "orden": fila["orden"], "creado_en": fila["creado_en"]}
    if quitar:
        if titulo is not None or start_day is not None or end_day is not None or nota is not None:
            raise ToolError("quitar no se combina con otros campos")
        despues = {"fase_id": fase_id, "horizonte_goal_id": fila["horizonte_goal_id"], "quitar": True}
    else:
        nuevo_titulo = (titulo or fila["titulo"]).strip() if titulo is not None else fila["titulo"]
        nuevo_inicio = start_day if start_day is not None else fila["start_day"]
        nuevo_fin = end_day if end_day is not None else fila["end_day"]
        nueva_nota = nota if nota is not None else fila["nota"]
        if not nuevo_titulo:
            raise ToolError("titulo: no puede quedar vacío")
        hermanas = [dict(f) for f in conn.execute(
            "SELECT titulo, start_day, end_day FROM horizonte_fase WHERE horizonte_goal_id = ? AND id != ?",
            (fila["horizonte_goal_id"], fase_id))]
        hermanas.append({"titulo": nuevo_titulo, "start_day": nuevo_inicio, "end_day": nuevo_fin})
        goal = conn.execute("SELECT start_day, deadline FROM goal WHERE id = ?",
                            (fila["horizonte_goal_id"],)).fetchone()
        try:
            db.validar_fases_horizonte(hermanas, goal["start_day"], goal["deadline"])
        except ValueError as e:
            raise ToolError(e.args[0])
        despues = {"fase_id": fase_id, "horizonte_goal_id": fila["horizonte_goal_id"], "titulo": nuevo_titulo,
                   "start_day": nuevo_inicio, "end_day": nuevo_fin, "nota": nueva_nota, "quitar": False}
    registro = propuestas.crear(conn, person_id, conversacion_id, "corregir_fase", despues,
                                 objetivo_id=fila["horizonte_goal_id"], antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _proponer_cambio_meta(conn: sqlite3.Connection, person_id: int, conversacion_id: int, goal_id: int,
                           target_value=None, deadline: str | None = None) -> dict:
    if target_value is None and deadline is None:
        raise ToolError("se necesita target_value y/o deadline")
    g = conn.execute("SELECT * FROM goal WHERE id = ? AND person_id = ?", (goal_id, person_id)).fetchone()
    if g is None:
        raise ToolError(f"meta desconocida: {goal_id!r}")
    if target_value is not None and g["kind"] != "metric":
        raise ToolError("target_value solo aplica a metas de tipo métrica")
    if deadline is not None:
        try:
            date.fromisoformat(deadline)
        except ValueError:
            raise ToolError("deadline debe ser una fecha ISO (AAAA-MM-DD)")
    antes = {"target_value": g["target_value"], "deadline": g["deadline"]}
    despues = {"target_value": float(target_value) if target_value is not None else g["target_value"],
               "deadline": deadline if deadline is not None else g["deadline"],
               "metric_key": g["metric_key"], "titulo": g["title"]}
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_cambio_meta", despues,
                                 objetivo_id=goal_id, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _proponer_confirmar_turnos(conn: sqlite3.Connection, person_id: int, conversacion_id: int) -> dict:
    resumen = shift_import.summary_proposed(conn, person_id)
    if resumen["batch_count"] == 0:
        raise ToolError("no hay turnos propuestos por confirmar")
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_confirmar_turnos", {},
                                 antes={"resumen": resumen})
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _proponer_idioma(conn: sqlite3.Connection, person_id: int, conversacion_id: int, lang: str) -> dict:
    if lang not in LANGS:
        raise ToolError(f"idioma inválido: {lang!r}; debe ser uno de {LANGS}")
    actual = db.person(conn, person_id)["language"]
    if actual == lang:
        raise ToolError("ya está en ese idioma")
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_idioma", {"lang": lang},
                                 antes={"lang": actual})
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


_PARAM_IMPORTAR_DESPLAZAMIENTO = {
    "type": "object",
    "properties": {"contenido": {"type": "string"}},
    "required": ["contenido"],
    "additionalProperties": False,
}


def _importar_desplazamiento(conn: sqlite3.Connection, person_id: int, conversacion_id: int,
                              contenido: str) -> dict:
    """Proposes importing Personal's whole commute-summary CSV (BODY-DESPLAZAMIENTO-ASISTENTE-1
    ruling 1): validated by the SAME `desplazamiento_import.validar` the device route (POST
    /api/desplazamiento) itself calls, before any proposal exists -- a refused file answers only
    its reason CLASS, never a figure or a date, and no proposal is created. The card (ruling 2)
    names only counts and dates -- the day count, the first and last day, and how many days would
    actually change, 0 when this is the file already imported, the same shortcut `importar` itself
    takes on a matching computed_at. `antes` snapshots the current `fuente_importacion` row (or
    None) and, unless this is that no-op, every (day, key) value the file would touch, so
    `propuestas.deshacer` (ruling 3) can restore exactly what was there. Applying it calls the SAME
    `desplazamiento_import.importar` the device route calls -- one import path, two doors."""
    try:
        vista = desplazamiento_import.preview(conn, person_id, contenido)
    except desplazamiento_import.ImportRefused as e:
        raise ToolError(e.motivo, codigo=e.motivo)
    rows = desplazamiento_import.validar(contenido)
    dias = sorted(r["day"] for r in rows)
    fuente_fila = conn.execute(
        "SELECT primer_dia, ultimo_dia, computed_at, dias, importado_at, ultimo_error, ultimo_error_at "
        "FROM fuente_importacion WHERE person_id = ? AND fuente = ?",
        (person_id, desplazamiento_import.FUENTE),
    ).fetchone()
    fuente_antes = dict(fuente_fila) if fuente_fila is not None else None
    sin_cambios = fuente_antes is not None and fuente_antes["computed_at"] == rows[0]["computed_at"]
    if sin_cambios:
        valores = []
    else:
        actuales = desplazamiento_import.valores_commute(conn, person_id, dias[0], dias[-1])
        valores = [{"day": r["day"], "key": k, "value": actuales.get((r["day"], k))}
                   for r in rows for k in desplazamiento_import.COUNT_COLUMNS]
    despues = {"contenido": contenido, "dias": vista["dias"], "primer_dia": vista["primer_dia"],
               "ultimo_dia": vista["ultimo_dia"], "dias_cambio": vista["dias_cambio"]}
    antes = {"fuente": fuente_antes, "sin_cambios": sin_cambios, "valores": valores}
    registro = propuestas.crear(conn, person_id, conversacion_id, "importar_desplazamiento", despues, antes=antes)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


# BODY-MINUTO-ANALISIS-3 ruling 3: every filterable rasgo's name and unit, read straight from
# body/noche_rasgos.py's own MODELO (figuras_guardadas, then figuras_forma) so this sentence can
# never drift from what buscar_noches actually accepts -- never a second, hand-typed list.
_RASGOS_BUSCAR_NOCHES_DESC = ", ".join(
    f"{nombre} ({unidad})" for nombre, (unidad, _fuente) in
    {**noche_rasgos.MODELO["figuras_guardadas"], **noche_rasgos.MODELO["figuras_forma"]}.items()
)

HERRAMIENTAS = [
    {"nombre": "leer_hoy", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Qué tan listo estás hoy: el puntaje de preparación, los valores del día más reciente y "
                     "las limitaciones abiertas (una lesión en recuperación, con la zona si se nombró). "
                     "Incluye 'reentrada' si vuelves de una semana o más sin entrenar fuerza (con la sesión "
                     "y el volumen sugerido) y 'tras_carga' si ayer tuvo una actividad de carga alta (con "
                     "los grupos musculares que cargó) -- ninguna de las dos aparece cuando no aplican.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_hoy},
    {"nombre": "leer_dia", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Las métricas, el sueño, las actividades y los turnos de un día concreto, sus "
                     "'comidas' registradas (cada una con su propia 'hora' en HH:MM además de "
                     "'ts_local'), más su 'contexto': turno/bloque de noche, viaje o período marcado, "
                     "carga de actividad, desplazamiento diario importado, última comida antes de "
                     "dormir y alcohol -- todo DERIVADO al leer, nunca datos que la persona haya "
                     "escrito.",
     "parametros": _PARAM_DIA, "funcion": _leer_dia},
    {"nombre": "leer_como_calculo", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Cómo se calculó el puntaje de preparación de un día: cada parte, su peso y su valor.",
     "parametros": _PARAM_DIA, "funcion": _leer_como_calculo},
    {"nombre": "leer_intradia", "tipo": "lectura", "nivel": "completo",
     "descripcion": "El pulso y el estrés minuto a minuto en una ventana de hasta 4 horas dentro de un día "
                     "(por ejemplo, el momento de despertar, un entrenamiento o un momento de estrés), con el "
                     "sueño minuto a minuto para la parte de la ventana que cae dentro de esa noche. Para un "
                     "rango de varios días usa leer_serie; para el resto de ese mismo día usa leer_dia. Pasada "
                     f"la ventana de {_LEER_INTRADIA_MAX_MIN // 60} horas se rechaza sin leer nada: divide el "
                     "rango.",
     "parametros": _PARAM_LEER_INTRADIA, "funcion": _leer_intradia, "necesita_actualizacion": True},
    {"nombre": "leer_noche", "tipo": "lectura", "nivel": "completo",
     "descripcion": "La noche completa de un día de despertar, en una sola llamada: el pulso y el estrés "
                     "minuto a minuto desde antes de dormirse hasta después de despertar (cruzando la "
                     "medianoche), el sueño minuto a minuto, el inicio y el fin que registró el reloj, tus "
                     "propias correcciones, y lo que Body ya sabe calcular sobre esa noche (el pulso mínimo: "
                     "el minuto más bajo registrado esa noche, sin rango propio; el pulso bajo robusto de esa "
                     "noche, el que sí se compara con tu rango habitual; tu VFC nocturna y su rango habitual; "
                     "y el modelo de despertar real). Cada rango habitual viene en dos versiones, cada una ausente "
                     "por su cuenta: rango_30_dias (los 30 días antes de esta noche, sin los días marcados) y "
                     "rango_historia (toda tu historia, la misma que ves en Tendencias). Las etapas del sueño "
                     "llevan su propia etiqueta (profundo/ligero/rem/despierto) junto con una verificación de "
                     "esa etiqueta noche por noche contra tus totales ya guardados. 'captura' indica si el "
                     "REM en cero de esa noche es un vacío de captura del reloj, nunca una lectura real -- "
                     "cuando 'captura.rem_no_capturado' es true, ese REM no debe compararse ni reportarse como "
                     "una cifra. 'contexto' trae el turno/"
                     "bloque de noche, el viaje o período marcado, la carga de actividad, el desplazamiento "
                     "y el alcohol de la TARDE/NOCHE ANTERIOR (el día que produjo esta noche), más el hueco "
                     "entre esa última comida y el inicio del sueño -- todo DERIVADO al leer, nunca datos que "
                     "la persona haya escrito. Para esa misma noche úsala en vez de leer_intradia -- ella "
                     "sola reemplaza varias llamadas de leer_intradia. Para un momento puntual de hasta 4 "
                     "horas (no una noche completa) usa leer_intradia; para varios días usa leer_dia o "
                     "leer_serie. Para comparar esta misma noche con tu historia usa comparar_noche; para "
                     "encontrar qué "
                     "noches cumplen una condición (en vez de mirar una sola) usa buscar_noches.",
     "parametros": _PARAM_LEER_NOCHE, "funcion": _leer_noche, "necesita_actualizacion": True},
    {"nombre": "comparar_noche", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Las características de UNA noche (la fecha de despertar) comparadas con tus propias "
                     "noches anteriores -- nunca con otras personas. Dos familias de características, cada "
                     "una presente solo cuando se puede calcular: las cifras ya guardadas de esa noche "
                     "(pulso bajo robusto, pulso promedio, VFC nocturna, puntaje de sueño, los minutos de "
                     "cada etapa, el total de sueño y la carga de Body Battery durante la noche si está "
                     "guardada) y la FORMA de esa noche a partir de su detalle minuto a minuto (hora de "
                     "dormirse y de despertar, en qué momento de la noche cae el pulso más bajo, el pulso "
                     "en el primer tercio frente al último, los segmentos despierto y sus minutos, el bloque "
                     "de sueño profundo más largo sin interrupción, y la VFC de la primera mitad frente a la "
                     "segunda) -- esta segunda familia solo existe para noches con ese detalle guardado; "
                     "muchas noches antiguas no lo tienen. Por cada característica devuelve su valor, "
                     "cuántas noches tuyas la tienen, los percentiles 25/50/75 y en qué percentil (0-100) "
                     "cae esta noche entre tus propias noches del rango -- 'desde' por defecto son los "
                     "últimos 365 días. Con 'grupo' (tras_turno, tras_turno_noche, viaje, periodo, "
                     "comida_tarde_min_max, con_alcohol, con_desplazamiento, altitud_cambio_min/"
                     "altitud_cambio_max -- este último agrupa por el CAMBIO de altitud frente a tu propia "
                     "casa, en metros con signo, nunca la altitud absoluta sola) repite esos mismos cinco "
                     "datos solo dentro de las "
                     "noches que comparten ese mismo contexto, por ejemplo 'las noches después de un turno "
                     "de noche' -- un grupo con menos de 5 noches no da percentiles, solo su propio conteo. "
                     "'captura' indica si el REM en cero de esa noche es un vacío de captura del reloj, "
                     "nunca una lectura real -- cuando 'captura.rem_no_capturado' es true, ese REM no debe "
                     "compararse ni reportarse como una cifra. "
                     "Nunca un dato crudo minuto a minuto ni una causa. Para UNA noche comparada con tu "
                     "propia historia usa esta herramienta; para el detalle crudo completo de esa misma "
                     "noche usa leer_noche; para mirar una ventana puntual dentro de una noche o un día usa "
                     "leer_serie con paso='minuto'; para encontrar QUÉ noches (no solo esta) cumplen una "
                     "condición usa buscar_noches.",
     "parametros": _PARAM_COMPARAR_NOCHE, "funcion": _comparar_noche},
    {"nombre": "buscar_noches", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Encuentra CUÁLES de tus propias noches cumplen una condición, en vez de clasificar una "
                     "sola -- por ejemplo 'mis 5 peores noches de sueño profundo este año y qué tenían en "
                     "común', 'noches después de un turno de noche en las que el pulso bajo llegó tarde', "
                     "'toda noche con más de 3 segmentos despierto mientras viajabas'. 'filtros' es una "
                     "lista de objetos {rasgo, op, valor} -- op es uno de <, <=, >, >=, = -- sobre estas "
                     f"características, cada una con su unidad: {_RASGOS_BUSCAR_NOCHES_DESC}. Un rasgo o un "
                     "operador no reconocido se rechaza por su nombre, antes de leer nada. Con 'grupo' (los "
                     "mismos datos que comparar_noche: tras_turno, tras_turno_noche, viaje, periodo, "
                     "comida_tarde_min_max, con_alcohol, con_desplazamiento, altitud_cambio_min/"
                     "altitud_cambio_max) solo cuentan las noches que "
                     "además comparten ese "
                     "contexto -- se combina con 'filtros' como un Y, nunca un O. 'orden' ({rasgo, dir: "
                     "asc|desc}) ordena las noches que coinciden antes de aplicar 'limite' (por defecto "
                     f"{_BUSCAR_NOCHES_LIMITE_DEFECTO}, máximo {_BUSCAR_NOCHES_LIMITE_MAX}). 'rasgos' elige "
                     "qué características devolver por noche (por defecto, las nombradas en 'filtros'/"
                     "'orden'). 'desde'/'hasta' acotan la búsqueda (por defecto los últimos 365 días; como "
                     "máximo, toda tu historia). Cada noche devuelta lleva su fecha, las características "
                     "pedidas y su 'contexto'. El resultado también da 'n_coinciden' (todas las que cumplen, "
                     "antes de 'limite'), 'n_en_rango' y 'noches_sin_minutos_en_rango' (sin detalle minuto a "
                     "minuto -- nunca pueden cumplir un filtro de forma). Con 5 noches coincidentes o más, "
                     "'en_comun' da, por cada dato de contexto, qué fracción de las noches coincidentes lo "
                     "lleva frente a qué fracción de todas las noches del rango lo lleva -- para altitud_cambio "
                     "además da qué fracción de las noches coincidentes bajó, se mantuvo igual o subió frente "
                     "a tu propia casa -- solo conteos y "
                     "fracciones, nunca una causa. Para clasificar UNA noche frente a tu historia usa "
                     "comparar_noche; para el detalle crudo completo de una noche usa leer_noche; para una "
                     "ventana puntual dentro de una noche o un día usa leer_serie con paso='minuto'.",
     "parametros": _PARAM_BUSCAR_NOCHES, "funcion": _buscar_noches},
    {"nombre": "leer_tendencias", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Promedios de un rango de días frente a tu historia, sin bajar al dato de un día.",
     "parametros": _PARAM_TENDENCIAS, "funcion": _leer_tendencias},
    {"nombre": "leer_historia", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Cobertura, entrenamiento, patrones por semana/mes y comparación por tipo de turno.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_historia},
    {"nombre": "leer_metas", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Tus metas activas y su última verificación semanal ya existente.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_metas},
    {"nombre": "leer_meta", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "El detalle de una meta: su plan, sus hábitos y sus verificaciones semanales ya existentes.",
     "parametros": _PARAM_META, "funcion": _leer_meta},
    {"nombre": "leer_turnos", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Los turnos propuestos pendientes de confirmar y el último lote confirmado.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_turnos},
    {"nombre": "leer_costo_turnos", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Cuánto cuesta una semana de turnos nocturnos y cuántos días tarda en recuperarse, "
                     "comparado con sus semanas de día: bloques de noches, su costo por medida y su "
                     "recuperación, nunca el valor de un día. Incluye perfil_hrv (BODY-TURNOS-PERFIL-DIA-1): "
                     "cómo se ve su HRV día por día alrededor de un bloque de noches (el día antes, cada "
                     "noche, y hasta 4 días después), para responder cuándo está más bajo y cuándo vuelve "
                     "a su rango.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_costo_turnos},
    {"nombre": "leer_dias_libres", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Qué días de un rango están libres para entrenar según sus turnos confirmados, y "
                     "para cada día que no lo está, el motivo (un turno ese día, o que sale de un turno "
                     "de noche esa mañana). Cada día incluye 'reentrada'/'tras_carga' cuando aplican, la "
                     "misma señal de leer_hoy. Úsalo antes de proponer un día.",
     "parametros": _PARAM_LEER_DIAS_LIBRES, "funcion": _leer_dias_libres},
    {"nombre": "leer_ajustes", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Tus datos de perfil (nombre, zona horaria, idioma) y tu nivel de datos del asistente.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_ajustes},
    {"nombre": "leer_estado_reloj", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Si tu reloj Garmin está conectado, desde cuándo, el último error de sincronización si "
                     "hay uno, el día más reciente con datos del reloj, y qué días de los últimos 14 no "
                     "tienen datos. Nunca una cifra de salud: solo el estado de la sincronización.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_estado_reloj},
    {"nombre": "leer_tutoriales", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Los tutoriales del catálogo de Body: su capítulo, su pantalla, si el asistente ya lo hace "
                     "y si ya existe un video. Úsala para responder cómo se hace algo en la app.",
     "parametros": _PARAM_LEER_TUTORIALES, "funcion": _leer_tutoriales},
    {"nombre": "leer_entreno", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Las sesiones de entrenamiento de un día o un rango de días -- de cualquier deporte, no "
                     "solo fuerza -- cada una con su resumen de esfuerzo cuando hay datos suficientes: los "
                     "minutos por zona de frecuencia cardíaca, el porcentaje en zona alta, cuántos esfuerzos "
                     "fuertes tuvo, qué tan rápido bajó el pulso después de cada uno, y cómo se compara todo "
                     "eso con lo habitual de esa persona en ese mismo tipo de sesión ('más' en la "
                     "recuperación significa que el pulso bajó más rápido, nunca más alto). Las sesiones de "
                     "fuerza llevan además sus series, y el plan de entrenamiento de ese día si existe.",
     "parametros": _PARAM_LEER_ENTRENO, "funcion": _leer_entreno},
    {"nombre": "leer_serie", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Una o más métricas, día por día (o por semana/mes) a lo largo de un rango de fechas -- "
                     "úsala para cualquier rango de días; leer_dia es para el detalle completo de un solo "
                     "día. Un día o período sin dato simplemente no aparece (nunca un 0), y una métrica sin "
                     "ningún dato en el rango aparece con una lista vacía. paso='dia' admite hasta "
                     f"{_LEER_SERIE_MAX_DIAS_PASO_DIA} días por llamada y necesita nivel_datos='completo'; "
                     f"paso='semana' admite hasta {_LEER_SERIE_MAX_SEMANAS_PASO_SEMANA} semanas; paso='mes' "
                     "no tiene límite -- una historia completa del reloj a paso='mes' es una sola llamada. "
                     "Pasado el límite, o con una fecha futura en 'hasta', se rechaza sin leer nada: usa un "
                     "paso más grueso o divide el rango. paso='minuto' es distinto: 'desde'/'hasta' son "
                     "fecha-hora completas (pueden cruzar la medianoche), hasta "
                     f"{_LEER_SERIE_MAX_MIN_PASO_MINUTO} minutos, y da una fila por minuto con alguno de "
                     f"estos (nunca otros): {list(LEER_SERIE_MINUTO_METRICAS)}. Úsalo para mirar con "
                     "detalle una ventana concreta dentro de una noche o de un día (por ejemplo, qué pasó "
                     "entre las 04:30 y las 05:10); para TODA una noche de una sola vez usa leer_noche, y "
                     "para horas de un día que no son de sueño sigue existiendo leer_intradia. Para "
                     "encontrar qué noches comparten una característica o un contexto (en vez de mirar una "
                     "ventana) usa buscar_noches.",
     "parametros": _PARAM_LEER_SERIE, "funcion": _leer_serie, "necesita_nivel": True},
    {"nombre": "proponer_meta", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone crear una meta nueva, de tipo métrica (con metric_key/direction/target_value) "
                     "o de tipo milestone (sin métrica, solo con deadline); puede llevar hábitos y puede "
                     "colgar de un plan (horizonte_goal_id). No la crea: registra la propuesta para que la "
                     "persona la apruebe en el chat.",
     "parametros": _PARAM_PROPONER_META, "funcion": _proponer_meta},
    {"nombre": "proponer_cambio_meta", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone cambiar el valor objetivo y/o la fecha de una meta existente de la persona. "
                     "No la cambia: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_PROPONER_CAMBIO_META, "funcion": _proponer_cambio_meta},
    {"nombre": "proponer_plan", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone un plan completo: metas finales, hábitos, fases, reglas, semanas, metas "
                     "semanales y checkpoints, todo en una sola tarjeta. No crea nada: registra la propuesta "
                     "para que la persona la apruebe en el chat; si la aprueba, todo se guarda junto o nada "
                     "se guarda. Una fase es un bloque del plan con su propio título y fechas (por ejemplo "
                     "'Base', 'Pico'); no se solapan entre sí y quedan dentro de las fechas del plan. Una "
                     "regla puede pertenecer a una fase con fase_index: la posición de esa fase (0, 1, 2...) "
                     "dentro de la lista fases de esta misma llamada, nunca un id; sin fase_index la regla "
                     "aplica en todo el plan. En metas_semanales y en los criterios de un checkpoint, el "
                     "valor de una medida de reloj (hora_acostarse, hora_levantarse) se escribe \"HH:MM\" en "
                     "formato 24 horas, por ejemplo \"23:00\"; el valor de cualquier otra medida es un número.",
     "parametros": _PARAM_PROPONER_PLAN, "funcion": _proponer_plan},
    {"nombre": "ampliar_plan", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone añadir fases, reglas, semanas, metas semanales o checkpoints nuevos a un plan "
                     "que la persona ya tiene. Para cambiar el valor de una meta semanal que ya existe usa "
                     "corregir_meta_plan, no esta; para cambiar o quitar una fase que ya existe usa "
                     "corregir_fase. No añade nada: registra la propuesta para que la persona la apruebe en "
                     "el chat. Una regla puede unirse a una fase NUEVA de esta misma llamada con fase_index "
                     "(su posición en la lista fases de esta llamada); no puede unirse a una fase que el "
                     "plan ya tenía. En metas_semanales y en los criterios de un checkpoint, el valor de una "
                     "medida de reloj (hora_acostarse, hora_levantarse) se escribe \"HH:MM\" en formato 24 "
                     "horas, por ejemplo \"23:00\"; el valor de cualquier otra medida es un número.",
     "parametros": _PARAM_AMPLIAR_PLAN, "funcion": _ampliar_plan},
    {"nombre": "corregir_meta_plan", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone corregir el valor de una meta semanal de un plan (por ejemplo, bajar la meta de "
                     "una semana difícil). No sobrescribe nada: registra la corrección como un nuevo valor, "
                     "para que la persona la apruebe en el chat. Si esa meta es de una medida de reloj "
                     "(hora_acostarse, hora_levantarse), valor se escribe \"HH:MM\" en formato 24 horas, por "
                     "ejemplo \"23:00\"; si no, valor es un número.",
     "parametros": _PARAM_CORREGIR_META_PLAN, "funcion": _corregir_meta_plan},
    {"nombre": "corregir_fase", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone corregir el título, las fechas o la nota de una fase de un plan, o quitarla "
                     "(quitar: true, sin ningún otro campo). Quitar una fase nunca borra sus reglas: quedan "
                     "sin fase, aplicando a todo el plan de nuevo. No lo cambia: registra la propuesta para "
                     "que la persona la apruebe en el chat.",
     "parametros": _PARAM_CORREGIR_FASE, "funcion": _corregir_fase},
    {"nombre": "proponer_confirmar_turnos", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone confirmar todos los lotes de turnos propuestos pendientes. No los confirma: "
                     "registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _SIN_PARAMETROS, "funcion": _proponer_confirmar_turnos},
    {"nombre": "proponer_idioma", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone cambiar el idioma de la aplicación. No lo cambia: registra la propuesta "
                     "para que la persona la apruebe en el chat.",
     "parametros": _PARAM_PROPONER_IDIOMA, "funcion": _proponer_idioma},
    {"nombre": "planear_entreno", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone un plan de entrenamiento para un día: título, hora opcional y una lista de "
                     "ejercicios con sus series y repeticiones. Pon siempre la categoría muscular de cada "
                     "ejercicio (pierna, gluteo, espalda, pecho, hombro, brazo, core, cardio, movilidad u "
                     "otro): es lo que ilumina el cuerpo de la persona en la pantalla Hoy. Revisa primero las "
                     "'limitaciones' de leer_hoy: si una zona está en recuperación, mantén la carga de esa "
                     "zona por debajo de lo normal y dilo en el plan -- tú nunca inventas una carga concreta, "
                     "solo dices el límite. Si leer_hoy o leer_dias_libres marcan 'reentrada' (vuelve de una "
                     "semana o más sin fuerza) o 'tras_carga' (ayer tuvo una carga alta), esta misma "
                     "herramienta ya ajusta el volumen o quita los ejercicios de esos grupos sola -- no lo "
                     "hagas tú, solo tenlo en cuenta al elegir qué proponer. No lo crea: registra la "
                     "propuesta para que la persona la apruebe en el chat. Antes de proponer un día, mira "
                     "leer_dias_libres: propón un día libre. Si ninguno lo está, dilo con el motivo de cada "
                     "día y deja que la persona elija; nunca inventes una fecha.",
     "parametros": _PARAM_PLANEAR_ENTRENO, "funcion": _planear_entreno},
    {"nombre": "registrar_entreno", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone registrar lo que la persona hizo en un entrenamiento de un día: si el reloj "
                     "ya tiene una actividad de fuerza ese día, compara serie por serie; si no, crea una "
                     "sesión manual. No lo guarda: registra la propuesta para que la persona la apruebe "
                     "en el chat. Si la persona corrige un entreno que ya registró ese día, llama a esta "
                     "misma herramienta: corrige ese entreno, nunca crea otro.",
     "parametros": _PARAM_REGISTRAR_ENTRENO, "funcion": _registrar_entreno},
    {"nombre": "corregir_entreno", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Corrige UNA serie de un entreno ya registrado (por ejemplo \"la tercera fueron 8 "
                     "repeticiones\"), sin repetir las demás. 'orden' es la posición de la serie como la "
                     "muestra la pantalla del entrenamiento, nunca un id. No la corrige: registra la "
                     "propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_CORREGIR_ENTRENO, "funcion": _corregir_entreno},
    {"nombre": "anotar_sesion", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone una nota sobre cómo fue una sesión de entrenamiento ya planeada (plan_id es el id "
                     "del plan, nunca su título). Di si la sesión quedó hecha con 'hecha': true -- déjalo en "
                     "falso o sin dar si la persona todavía no la hizo o solo está comentando el plan. No "
                     "guarda la nota: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_ANOTAR_SESION, "funcion": _anotar_sesion},
    {"nombre": "descartar_series", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone marcar como no hechas una o más series del reloj de un día ya "
                     "registrado (por ejemplo una serie que quedó de un ejercicio que la persona "
                     "no hizo). Rechaza una serie que la persona ya registró, un descanso del "
                     "reloj o un número de serie que no existe. No las marca: registra la "
                     "propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_DESCARTAR_SERIES, "funcion": _descartar_series},
    {"nombre": "separar_sesion", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone separar de nuevo un día cuya sesión de fuerza del reloj y una sesión "
                     "manual quedaron unidas como una sola -- por ejemplo si en realidad fueron dos "
                     "entrenamientos distintos ese día. 'source_id' puede nombrar cualquiera de las "
                     "dos sesiones. No las separa: registra la propuesta para que la persona la "
                     "apruebe en el chat.",
     "parametros": _PARAM_SEPARAR_SESION, "funcion": _separar_sesion},
    {"nombre": "unir_sesiones", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone unir, para un día, la sesión de fuerza del reloj y la sesión manual que "
                     "quedaron registradas por separado -- por ejemplo un día guardado antes de que "
                     "Body empezara a unirlas solas, o un día que la persona ya separó y quiere "
                     "volver a unir. Solo funciona cuando ese día tiene exactamente una sesión de "
                     "fuerza del reloj y una sesión manual, ninguna ya unida a otra. No las une: "
                     "registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_UNIR_SESIONES, "funcion": _unir_sesiones},
    {"nombre": "registrar_medida", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone registrar un número que la persona mide ella misma para un día "
                     "(por ejemplo su alcance de flexibilidad, sus minutos de ronquido o qué tan "
                     "adolorido siente un grupo muscular). Si la persona dice que un músculo quedó "
                     "adolorido (por ejemplo \"las piernas me quedaron adoloridas, como un 3\"), "
                     "propone una lectura de agujetas de 0 a 5 para ese grupo -- una propuesta por "
                     "cada grupo que mencione. Por convención esa lectura es cómo se siente al día "
                     "siguiente de la sesión, nunca el día del entrenamiento: usa el día en que la "
                     "persona dice sentir el dolor, nunca lo infiere del día en que entrenó. No lo "
                     "guarda: registra la propuesta para que la persona la apruebe en el chat. Si la "
                     "persona corrige un valor que ya registró ese día, llama a esta misma herramienta "
                     "con el valor correcto: reemplaza el anterior, nunca suma un segundo registro.",
     "parametros": _PARAM_REGISTRAR_MEDIDA, "funcion": _registrar_medida},
    {"nombre": "registrar_dolor", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone registrar cómo sintió un ejercicio ya entrenado ese día, en una zona que le "
                     "molesta -- dolor durante y después del ejercicio, cada uno de 0 a 10, y en qué "
                     "movimiento o ángulo dolió, en sus propias palabras. 'ejercicio' debe ser uno que la "
                     "persona entrenó ese día; la zona se toma de esa misma serie, nunca se pide. Da al menos "
                     "un dato (durante, después o el movimiento). Nunca es consejo médico: solo registra el "
                     "número y sus palabras. Si la persona corrige un valor que ya registró para ese "
                     "ejercicio ese día, llama a esta misma herramienta con el valor correcto: reemplaza el "
                     "anterior, nunca suma un segundo registro. No lo guarda: registra la propuesta para que "
                     "la persona la apruebe en el chat.",
     "parametros": _PARAM_REGISTRAR_DOLOR, "funcion": _registrar_dolor},
    {"nombre": "registrar_ingesta", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone registrar algo que la persona comió o bebió, para un día: estima los gramos a "
                     "partir de sus palabras cuando no da un número exacto, dice que es una estimación y qué "
                     "asumió, un registro por comida, y solo pregunta si la comida es demasiado vaga para "
                     "estimarla. No lo guarda: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_REGISTRAR_INGESTA, "funcion": _registrar_ingesta},
    {"nombre": "corregir_ingesta", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone corregir la cantidad de un registro de consumo que ya existe. No lo corrige: "
                     "registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_CORREGIR_INGESTA, "funcion": _corregir_ingesta},
    {"nombre": "borrar_ingesta", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone eliminar un registro de consumo que ya existe. No lo elimina: registra la "
                     "propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_BORRAR_INGESTA, "funcion": _borrar_ingesta},
    {"nombre": "proponer_objetivo_nutriente", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone una meta diaria para un nutriente (calorías, proteína, carbohidratos, grasa, "
                     "fibra, agua, cafeína o alcohol): una cantidad fija o una proporción por kg de peso "
                     "corporal (solo para un nutriente medido en gramos), y si es una cantidad a alcanzar "
                     "(al_menos) o un límite a no superar (como_maximo). Para cafeína y alcohol lo normal "
                     "es un límite; para el resto, alcanzar la cantidad. 'off' quita la meta diaria de ese "
                     "nutriente desde ese día. No la crea: registra la propuesta para que la persona la "
                     "apruebe en el chat.",
     "parametros": _PARAM_PROPONER_OBJETIVO_NUTRIENTE, "funcion": _proponer_objetivo_nutriente},
    {"nombre": "registrar_comida", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone registrar una comida completa en una sola llamada: una descripción y una lista "
                     "de cantidades, una por nutriente (proteína, calorías, carbohidratos, grasa, fibra, agua, "
                     "cafeína, alcohol). Estima cada nutriente que puedas a partir de las palabras de la "
                     "persona -- nunca le pidas que pese la comida -- incluye todos los que puedas estimar, "
                     "marca como estimado cada cantidad que no sea un número exacto, y nombra la hora para "
                     "cafeína y alcohol cuando la persona la dé. Si la persona dice que comió con alguien de "
                     "su hogar, agrega 'compartida_con': una entrada por esa persona (su nombre, su propia "
                     "porción en nutrientes, y el texto de la porción si lo dio, por ejemplo 'la mitad'); esa "
                     "persona recibe su porción para aplicarla ella misma, nunca entra a su registro sola. "
                     "'momento', si lo das, debe ser una de las claves propias de la persona (ver la "
                     "descripción de ese campo); un valor que no sea una de ellas se rechaza nombrando las "
                     "que sí son válidas para esa persona. No lo guarda: registra la propuesta para que la "
                     "persona la apruebe en el chat.",
     "parametros": _PARAM_REGISTRAR_COMIDA, "funcion": _registrar_comida},
    {"nombre": "corregir_comida", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone corregir la hora, el día, la descripción o las cantidades de una comida que ya "
                     "existe -- toda la comida como una sola propuesta, nunca una por cada nutriente. "
                     "'comida_id' se toma del campo 'comida_id' de una de las entradas de la lista 'comidas' "
                     "que devuelve leer_dia, nunca inventado. Usa esta herramienta para cambiar la hora, la "
                     "redacción o las cantidades de una comida; para corregir o eliminar un solo registro de "
                     "consumo que no forma parte de una comida sigue usando corregir_ingesta/borrar_ingesta. "
                     "'cantidades', cuando se da, reemplaza por completo el conjunto de cantidades de la "
                     "comida -- nunca se combina con lo que ya tenía. No la corrige: registra la propuesta "
                     "para que la persona la apruebe en el chat.",
     "parametros": _PARAM_CORREGIR_COMIDA, "funcion": _corregir_comida},
    {"nombre": "registrar_checkin", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone un chequeo de 10 segundos: ánimo, energía y estrés sentido, cada uno del 1 al 5, "
                     "más una nota opcional de una línea. Pregunta solo esos tres números y esa línea -- nunca "
                     "más -- y usa únicamente lo que la persona te dé, sin inventar un valor que no dijo. No lo "
                     "guarda: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_REGISTRAR_CHECKIN, "funcion": _registrar_checkin},
    {"nombre": "marcar_periodo", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone marcar unos días como un período especial (malestar, viaje, lesión o molestia) "
                     "para que Body sepa si debe dejarlos fuera cuando compare con lo habitual, sin leerlos "
                     "como efecto de un hábito o experimento. Ofrécelo cuando la persona diga que está "
                     "enferma, de viaje, lesionada o entrenando con una molestia -- nunca lo crees en "
                     "silencio. 'lesión' y 'malestar'/'viaje' dejan esos días fuera del rango habitual; "
                     "'molestia' (entrena igual, solo con una molestia) los deja DENTRO -- úsala cuando la "
                     "persona siga entrenando a pesar de la molestia, nunca 'lesión' solo porque la sesión "
                     "fue completa. Si la persona nombra una parte del cuerpo limitada (por ejemplo, un "
                     "hombro en recuperación), pon esa zona en 'zonas' con 'lesión' o 'molestia': así Body "
                     "lee un entrenamiento más liviano ahí como algo a propósito, nunca como una caída. No "
                     "lo marca: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_MARCAR_PERIODO, "funcion": _marcar_periodo},
    {"nombre": "registrar_altitud", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone registrar la altitud de tu casa o de una estancia fuera de ella (de viaje, por "
                     "ejemplo), para que las comparaciones de sueño puedan agruparse por el CAMBIO de altitud "
                     "frente a tu propia casa -- bajar suele reducir el pulso en reposo y durante el sueño, "
                     "subir suele subirlo. 'es_casa'=true registra desde cuándo vives a esa altitud (una "
                     "mudanza es una nueva fila de casa, nunca reemplaza la anterior); 'es_casa'=false "
                     "registra una estancia fuera, con 'end_day' si ya terminó. Solo metros y fechas -- nunca "
                     "un nombre de lugar ni coordenadas, y nunca le pidas esos datos a la persona. Se rechaza "
                     "si se superpone con otra fila ya registrada del mismo tipo (otra casa, u otra "
                     "estancia). No lo guarda: registra la propuesta para que la persona la apruebe en el "
                     "chat.",
     "parametros": _PARAM_REGISTRAR_ALTITUD, "funcion": _registrar_altitud},
    {"nombre": "leer_altitudes", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Tus propias filas de altitud -- casa y estancias -- en un rango de fechas ('desde'/"
                     "'hasta', por defecto los últimos 365 días). Cada fila trae si es casa o estancia, sus "
                     "fechas, la altitud en metros y de dónde viene el dato. Solo metros y fechas, nunca un "
                     "nombre de lugar ni coordenadas.",
     "parametros": _PARAM_LEER_ALTITUDES, "funcion": _leer_altitudes},
    {"nombre": "cerrar_periodo", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone cerrar un período abierto, diciendo desde cuándo mejoró la persona. No lo "
                     "cierra: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_CERRAR_PERIODO, "funcion": _cerrar_periodo},
    {"nombre": "corregir_periodo", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone corregir el tipo, las fechas, la nota o las zonas de un período que ya existe. "
                     "Pon 'zonas' cuando la persona nombre o corrija la parte del cuerpo limitada; una lista "
                     "vacía la quita. No lo corrige: registra la propuesta para que la persona la apruebe "
                     "en el chat.",
     "parametros": _PARAM_CORREGIR_PERIODO, "funcion": _corregir_periodo},
    {"nombre": "borrar_periodo", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone eliminar un período que ya existe. No lo elimina: registra la propuesta "
                     "para que la persona la apruebe en el chat.",
     "parametros": _PARAM_BORRAR_PERIODO, "funcion": _borrar_periodo},
    {"nombre": "marcar_habito", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone marcar como hecho, para un día, un hábito activo que la persona puede "
                     "marcar, opcionalmente citando la actividad del reloj que lo cumple como "
                     "evidencia. No lo marca: registra la propuesta para que la persona la apruebe "
                     "en el chat.",
     "parametros": _PARAM_MARCAR_HABITO, "funcion": _marcar_habito},
    {"nombre": "leer_experimentos", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Los experimentos de la persona, activos primero y luego cerrados: qué miden, qué días "
                     "cuentan, los días ya marcados y, por métrica, el resultado de comparar los días en que "
                     "ocurrió con los propios días normales de la persona, nunca con una tabla de otra gente: "
                     "mas_bajo, mas_alto, sin_cambio (ya hay días suficientes pero no cambió) o pocos_dias "
                     "(todavía no hay suficientes, o falta el dato de ese día). Si la ventana es dentro del "
                     "día (por ejemplo, los minutos desde que despierta o desde un momento que registró), "
                     "compara ese mismo tramo de minutos en los días de prueba contra el de sus propios días "
                     "normales, con la misma regla. Un día que no se marcó no cuenta ni como prueba ni como "
                     "normal. Las palabras del veredicto las muestra la pantalla; usa estos códigos para "
                     "explicarlo con tus propias palabras, nunca como consejo médico.",
     "parametros": _PARAM_LEER_EXPERIMENTOS, "funcion": _leer_experimentos},
    {"nombre": "leer_planes", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Los planes (horizontes) de la persona, activos primero: título, deadline, metas "
                     "finales con su propio veredicto, experimentos ligados, la semana actual y el próximo "
                     "checkpoint, nunca el dato crudo de un solo día.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_planes},
    {"nombre": "leer_plan", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Un plan (horizonte) de la persona por su id, con lo mismo que leer_planes trae por "
                     "plan; si además das desde/hasta, agrega semana por semana qué decía el plan contra "
                     "qué pasó realmente en ese rango. 'registro' trae, por corriente (sueño, comidas, "
                     "proteína, peso, cintura, entreno), cuántos días o semanas ya tienen algo registrado "
                     "contra cuántos se esperaban desde el inicio del plan, y las fechas que todavía faltan.",
     "parametros": _PARAM_LEER_PLAN, "funcion": _leer_plan},
    {"nombre": "leer_pendientes_registro", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Qué falta registrar hoy (o en 'dia', si lo da) en cada plan con fecha activo de la "
                     "persona: sueño y comidas y proteína cuando ese día todavía no tiene nada, peso y "
                     "cintura y entreno cuando esta semana todavía no tiene nada -- y, por separado, qué "
                     "corriente nunca se ha usado ('sin_uso'), para no insistir con algo que nunca fue "
                     "parte de su rutina. Nunca trae una cifra de salud ni un veredicto, solo qué está "
                     "registrado o qué falta; pensada para que puedas preguntar por lo que falta, nunca "
                     "para dar consejo médico.",
     "parametros": _PARAM_LEER_PENDIENTES_REGISTRO, "funcion": _leer_pendientes_registro},
    {"nombre": "proponer_experimento", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone empezar un experimento: compara los días en que la persona hace algo con sus "
                     "propios días normales, nunca con una tabla de otra gente. Un día que no se marca no "
                     "cuenta ni como prueba ni como normal. No lo crea: registra la propuesta para que la "
                     "persona la apruebe en el chat.",
     "parametros": _PARAM_PROPONER_EXPERIMENTO, "funcion": _proponer_experimento},
    {"nombre": "marcar_experimento", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone marcar, para un día, si ocurrió o no la práctica de un experimento sin hábito "
                     "vinculado (uno con hábito se marca con marcar_habito, que es su registro). No lo marca: "
                     "registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_MARCAR_EXPERIMENTO, "funcion": _marcar_experimento},
    {"nombre": "importar_desplazamiento", "tipo": "propuesta", "nivel": "resumen",
     "descripcion": "Propone importar el archivo de resumen diario de desplazamiento que entrega el escritorio "
                     "personal de la persona: un CSV con un día por fila (minutos y viajes en vehículo, minutos "
                     "caminando, minutos en bicicleta). 'contenido' es el texto completo del archivo, sin "
                     "cambios. Rechaza el archivo COMPLETO si tiene cualquier defecto (encabezado distinto, una "
                     "fecha mala, un día repetido, un conteo no entero o negativo, un día futuro, o el archivo "
                     "mismo demasiado grande) -- nombrando solo el motivo, nunca guarda nada en ese caso. No lo "
                     "importa: registra la propuesta para que la persona la apruebe en el chat, mostrando solo "
                     "cuántos días cubre, desde cuándo hasta cuándo, y cuántos días cambiarían (0 si es el mismo "
                     "archivo que ya se importó).",
     "parametros": _PARAM_IMPORTAR_DESPLAZAMIENTO, "funcion": _importar_desplazamiento},
    {"nombre": "leer_practicas", "tipo": "lectura", "nivel": "resumen",
     "descripcion": "Sin id: las prácticas activas de la persona (lo que el reloj muestra que hace, nunca lo "
                     "que se comprometió a hacer) con su racha actual y la semana más reciente, más sugerencias "
                     "de prácticas que todavía no existen como tales. Con id: el detalle de una, acotado a los "
                     "últimos 90 días -- estado por día, semanas, racha, temporadas y la base de toda su "
                     "historia. encontrada: false si el id no es suyo o no existe, nunca un error.",
     "parametros": _PARAM_LEER_PRACTICAS, "funcion": _leer_practicas},
    {"nombre": "proponer_practica", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone crear una práctica nueva: una regla sobre lo que el reloj ya mide (un tipo de "
                     "actividad, un umbral de métrica, un horario de sueño) para que la pantalla muestre, día "
                     "por día, si se cumplió -- nunca algo que la persona marca ella misma, eso es un hábito. "
                     "No la crea: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_PROPONER_PRACTICA, "funcion": _proponer_practica},
    {"nombre": "corregir_despertar", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone corregir el sueño o la hora de despertar que registró el reloj, cuando la "
                     "persona cuenta que en realidad seguía dormitando o que se levantó antes/después de lo "
                     "que marcó Garmin. 'sueno' cuenta como sueño real hasta 'hora' (dormitó, no estaba "
                     "despierta). 'levantado' dice que a esa hora ya estaba levantada (el rato en la cama no "
                     "cuenta como sueño). 'hora' es local HH:MM, siempre posterior al fin de sueño que "
                     "registró Garmin y dentro de las horas que ese mismo día todavía puede corregir -- nunca "
                     "antes ni muchas horas después. No la aplica: registra la propuesta para que la persona "
                     "la apruebe en el chat. Garmin nunca se modifica, esto es la corrección de la persona al "
                     "lado de su dato.",
     "parametros": _PARAM_CORREGIR_DESPERTAR, "funcion": _corregir_despertar},
]

# BODY-ASISTENTE-CONFORMIDAD-1 ruling 2 (agent-capabilities-v1 section 5): the plan field is
# reserved on every entry -- nobody fills it, since Body has no plan tiers yet, but `disponibles`
# already filters on it so a plan tier is data, not a rewrite, the day one exists.
for _h in HERRAMIENTAS:
    _h["plan_minimo"] = None
del _h

_BY_NAME = {h["nombre"]: h for h in HERRAMIENTAS}
_RANK = {"ninguno": 0, "resumen": 1, "completo": 2}


class ToolError(Exception):
    """A lectura tool was unknown, unavailable at this nivel_datos, or given invalid parameters.
    `codigo`, when given, is a stable machine word an outside caller (never this module's own
    local-stdio/in-app callers) can branch on without parsing the localized message -- body/
    rutas/mcp_remoto.py's own `llamar_herramienta` carries it onto the wire as `motivo`
    (BODY-HOGAR-NOTA-SESION-GUARD-1 ruling 3 is the first raise to set it; every other raise in
    this module leaves it None, unchanged). mcp.py's own `RehusoMCP` IS-A ToolError and keeps
    setting `self.codigo` itself, so it is unaffected by this."""

    def __init__(self, mensaje: str, codigo: str | None = None):
        super().__init__(mensaje)
        self.codigo = codigo


# BODY-SYNC-VIVO-2 ruling 2: the `lectura` tools whose answer is TODAY's own figures -- leer_hoy
# and leer_estado_reloj, named explicitly by the spec. No other tool here qualifies today:
# leer_dia/leer_entreno answer an arbitrary day or range the caller names, never specifically
# "today" by their own contract, so a call for today's date through either of them is still an
# ordinary day query, not this trigger's concern. A future tool whose answer IS specifically
# today's figures joins this set.
HERRAMIENTAS_HOY = frozenset({"leer_hoy", "leer_estado_reloj"})


def _lanzar_pull_hoy_en_hilo(person_id: int) -> None:
    """BODY-SYNC-VIVO-2 ruling 2: starts `pull.pull_hoy_ahora(person_id)` on its own thread and
    returns immediately -- the lectura tool's own answer, called right after this, uses whatever
    is stored right now; `pull_hoy_ahora`'s own checks (hosted, connected, `puede_intentar`, quiet
    hours, the shared `_PULL_EN_CURSO` lock) decide for themselves whether anything actually runs,
    the same as Hoy's own BackgroundTask trigger. A thread, never asyncio: `ejecutar` is called
    from three different places (the in-app assistant's own sync loop, the local MCP stdio
    server, and -- through `mcp._llamar_herramienta`'s `_SesionRemota` path -- the hosted MCP
    door), none of them async. `cuenta.hosted()` is checked here too, cheaply, so a local-mode
    call never spawns a thread at all -- `pull_hoy_ahora` would just return "sin_cuenta" anyway."""
    if not cuenta.hosted():
        return
    threading.Thread(target=pull.pull_hoy_ahora, args=(person_id,), daemon=True).start()


def disponibles(nivel: str) -> list[dict]:
    if nivel not in ajustes.NIVELES:
        raise ToolError(f"nivel inválido: {nivel!r}; debe ser uno de {ajustes.NIVELES}")
    turnos_on = _TURNOS_ACTUAL.get()
    # BODY-ASISTENTE-CONFORMIDAD-1 ruling 2: a plan tier, the day one exists, is filtered here,
    # the same place the role filter (Body's one role per person, so every entry passes today)
    # and the nivel filter already run -- never a second place to keep in step.
    return [h for h in HERRAMIENTAS if _RANK[h["nivel"]] <= _RANK[nivel]
            and (turnos_on or h["nombre"] not in TURNOS_HERRAMIENTAS)
            and h["plan_minimo"] is None]


def tipo_de(nombre: str) -> str | None:
    entrada = _BY_NAME.get(nombre)
    return entrada["tipo"] if entrada else None


def _validar_parametros(schema: dict, params: dict) -> None:
    props = schema.get("properties", {})
    for clave in schema.get("required", ()):
        if clave not in params:
            raise ToolError(f"falta el parámetro requerido: {clave!r}")
    if not schema.get("additionalProperties", True):
        extra = sorted(set(params) - set(props))
        if extra:
            raise ToolError(f"parámetro(s) no reconocido(s): {extra}")
    for clave, valor in params.items():
        spec = props.get(clave)
        if spec is None:
            continue
        if "enum" in spec and valor not in spec["enum"]:
            raise ToolError(f"{clave!r} debe ser uno de {spec['enum']}")
        tipo = spec.get("type")
        if tipo == "integer" and not isinstance(valor, int):
            raise ToolError(f"{clave!r} debe ser un entero")
        if tipo == "string" and not isinstance(valor, str):
            raise ToolError(f"{clave!r} debe ser una cadena")
        if spec.get("format") == "date" and isinstance(valor, str):
            try:
                date.fromisoformat(valor)
            except ValueError:
                raise ToolError(f"{clave!r} debe ser una fecha ISO (AAAA-MM-DD)")


def ejecutar(conn: sqlite3.Connection, person_id: int, nombre: str, params: dict | None, nivel: str,
             conversacion_id: int | None = None):
    if nivel not in ajustes.NIVELES:
        raise ToolError(f"nivel inválido: {nivel!r}; debe ser uno de {ajustes.NIVELES}")
    entrada = _BY_NAME.get(nombre)
    if entrada is None:
        raise ToolError(f"herramienta desconocida: {nombre!r}")
    if _RANK[entrada["nivel"]] > _RANK[nivel]:
        raise ToolError(f"{nombre!r} requiere nivel_datos >= {entrada['nivel']!r}; el ajuste actual es {nivel!r}")
    params = params or {}
    _validar_parametros(entrada["parametros"], params)
    if entrada["tipo"] == "propuesta":
        if conversacion_id is None:
            raise ToolError(f"{nombre!r} requiere una conversación")
        return entrada["funcion"](conn, person_id, conversacion_id, **params)
    # BODY-ASISTENTE-CONFORMIDAD-1 ruling 1 (agent-capabilities-v1 section 1): a `lectura` tool
    # can never write, enforced by the connection itself -- never by review. `query_only` is
    # restored to whatever it was before, in a `finally`, so a nested call (there is none today)
    # or a later caller on the same connection never finds it left on.
    previo = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only = ON")
    try:
        # BODY-SYNC-VIVO-2 ruling 2: fired before the tool's own read, never awaited -- the read
        # below answers with whatever is stored right now, and says how fresh it is itself
        # (leer_estado_reloj's own `estado_vivo`, ruling 3).
        if nombre in HERRAMIENTAS_HOY:
            _lanzar_pull_hoy_en_hilo(person_id)
        # BODY-AGENTE-LEER-SERIE-1: a lectura tool whose registry entry asks for `nivel` (leer_serie's
        # own 'dia' vs 'resumen' split) gets it as its third positional argument -- everybody else's
        # call shape is unchanged.
        if entrada.get("necesita_nivel"):
            return entrada["funcion"](conn, person_id, nivel, **params)
        # BODY-DESPERTAR-REAL-2 ruling 2: leer_intradia's own on-demand refresh, SYNCHRONOUS (unlike
        # HERRAMIENTAS_HOY's fire-and-forget thread above) so the window just asked about is filled
        # before the read below runs -- called here, never from the tool's own body, for the same
        # reason ruling 2's docstring on pull.actualizar_intradia_si_corresponde gives.
        if entrada.get("necesita_actualizacion"):
            fallo = pull.actualizar_intradia_si_corresponde(person_id, params["fecha"])
            return entrada["funcion"](conn, person_id, actualizacion_fallo=fallo, **params)
        return entrada["funcion"](conn, person_id, **params)
    except sqlite3.OperationalError as e:
        mensaje = str(e.args[0]) if e.args else ""
        if "readonly database" not in mensaje:
            raise
        raise ToolError("lectura_intento_escribir") from None
    finally:
        conn.execute(f"PRAGMA query_only = {'ON' if previo else 'OFF'}")
