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
assistant OFFER -- never silently create -- a stretch of days marked illness, travel or injury, so
`baselines.baseline` and `experimentos.dias_que_cuentan` leave those days out when they compare
with the usual. `kind` is any live `period_kind` row, never a hardcoded pair of keys, the same
template pattern `registrar_medida`/`registrar_ingesta` use for their own enums. The person's own
note is kept like `moment.note` and `ingesta.descripcion` -- never printed, logged or exported off
the PC.
"""
from __future__ import annotations

import contextvars
import json
import sqlite3
import statistics
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import anatomia, catalogo_tendencias, comidas, compartir, cuenta, db, enlaces, entreno_historial, errores, \
    experimentos, fechas, garmin_cuenta, goals, history, horizonte, household, nutriente_objetivo, profile, pull, \
    readiness, shift_import, turnos_costo, tutoriales
from ..i18n import LANGS, T
from . import ajustes, propuestas

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
# rows, never worth refusing.
LEER_SERIE_PASOS = ("dia", "semana", "mes")
_LEER_SERIE_MAX_METRICAS = 12
_LEER_SERIE_MAX_DIAS_PASO_DIA = 366
_LEER_SERIE_MAX_SEMANAS_PASO_SEMANA = 260
_PARAM_LEER_SERIE = {
    "type": "object",
    "properties": {
        "metricas": {"type": "array", "items": {"type": "string", "enum": list(TENDENCIA_METRICAS)},
                     "minItems": 1, "maxItems": _LEER_SERIE_MAX_METRICAS},
        "desde": {"type": "string", "format": "date"},
        "hasta": {"type": "string", "format": "date"},
        "paso": {"type": "string", "enum": list(LEER_SERIE_PASOS)},
    },
    "required": ["metricas", "desde", "hasta", "paso"],
    "additionalProperties": False,
}
# key -> unit, off db.METRIC_DEFS itself (template, not a second hand-typed table).
_UNIDADES_METRICA = {d[0]: d[2] for d in db.METRIC_DEFS}

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

# BODY-PROTEINA-1: metric_def rows whose daily total is derived from `ingesta` entries, in
# catalogue order -- db.INGESTA_METRIC_KEYS (BODY-COMIDA-1), read off the live table rather than a
# second hand-typed list, same template pattern as SELF_METRIC_KEYS above.
INGESTA_METRIC_KEYS = db.INGESTA_METRIC_KEYS
_PARAM_REGISTRAR_INGESTA = {
    "type": "object",
    "properties": {
        "metric_key": {"type": "string", "enum": list(INGESTA_METRIC_KEYS)},
        "amount": {"type": "number"},
        "estimado": {"type": "boolean"},
        "descripcion": {"type": "string"},
        "day": {"type": "string", "format": "date"},
        "hora": {"type": "string"},
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
        "hora": {"type": "string"},
        "descripcion": {"type": "string"},
        "cantidades": {"type": "array", "items": _PARAM_CANTIDAD_COMIDA, "minItems": 1},
        "compartida_con": {"type": "array", "items": _PARAM_COMENSAL_COMPARTIDO},
        "momento": {"type": "string"},
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
        "hora": {"type": "string"},
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
        "hora": {"type": "string"},
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
PERIODO_KIND_KEYS = tuple(k for k, _ in db.PERIOD_KINDS)
_PARAM_MARCAR_PERIODO = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(PERIODO_KIND_KEYS)},
        "start_day": {"type": "string", "format": "date"},
        "end_day": {"type": "string", "format": "date"},
        "note": {"type": "string"},
        # BODY-LESION-ZONA-1 ruling 1: set this when the person names a limited body part (today,
        # that is always kind 'lesion') -- Body then reads a lighter training set in that zona as
        # deliberate, never a drop (ruling 2), and the next plans respect it (ruling 3).
        "zonas": {"type": "array", "items": {"type": "string", "enum": list(anatomia.CATEGORIA_ENUM)}},
    },
    "required": ["kind", "start_day"],
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
    },
    "required": ["contexto", "texto"],
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


def _readiness_dict(r: readiness.Readiness) -> dict:
    return {
        "dia": r.day, "puntaje": r.score, "banda": r.band, "version": r.version,
        "faltantes": r.missing,
        "partes": [{"clave": p.key, "valor": p.value, "referencia": p.reference,
                    "subpuntaje": p.subscore, "peso": p.weight} for p in r.parts],
    }


def _limitaciones_abiertas(conn: sqlite3.Connection, person_id: int, day: str) -> list[dict]:
    """BODY-LESION-ZONA-1 ruling 3: this person's own open ('lesion', end_day IS NULL) periods as
    of `day`, as {tipo, desde, zonas} -- never the note (the same privacy ruling 2 already keeps).
    Read by leer_hoy and used as the context planear_entreno's own description leans on."""
    return [{"tipo": r["kind"], "desde": r["start_day"], "zonas": json.loads(r["zonas"]) if r["zonas"] else []}
            for r in db.periodos(conn, person_id, day, day) if r["kind"] == "lesion" and r["end_day"] is None]


def _leer_hoy(conn: sqlite3.Connection, person_id: int) -> dict:
    dias = db.days_with_data(conn, person_id)
    if not dias:
        return {"dia": None, "mensaje": "sin datos aún"}
    dia = dias[0]
    return {"dia": dia, "listo": _readiness_dict(readiness.compute(conn, dia, person_id)),
            "valores": db.day_values(conn, dia, person_id),
            "limitaciones": _limitaciones_abiertas(conn, person_id, dia),
            "objetivos_nutriente": nutriente_objetivo.estado_del_dia(conn, person_id, dia)}


def _leer_dia(conn: sqlite3.Connection, person_id: int, day: str) -> dict:
    data = db.load_day(conn, day, person_id)
    t = T(db.person(conn, person_id)["language"])
    for a in data["activities"]:
        zonas = a.pop("zonas_limitadas", [])
        a["liviano_a_proposito"] = anatomia.texto_liviano_a_proposito(t, zonas) if zonas else None
    data["objetivos_nutriente"] = nutriente_objetivo.estado_del_dia(conn, person_id, day)
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
    exactly ONE statement, whatever `desde`..`hasta` spans."""
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
        f"SELECT metric_key, day, value FROM daily_metric WHERE person_id = ? "
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
    health figure, so it rides along at this same 'ninguno' level."""
    estado = garmin_cuenta.estado_conexion(conn, person_id)
    person = db.person(conn, person_id)
    hoy = fechas.hoy_local(person)
    dias = db.days_with_data(conn, person_id)
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    return {
        "conectado": estado["conectado"],
        "conectado_desde": (datetime.fromisoformat(estado["conectado_desde"]).date().isoformat()
                             if estado["conectado_desde"] else None),
        "ultimo_error": estado["ultimo_error"],
        "ultimo_dia_con_datos": dias[0] if dias else None,
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


def _leer_entreno(conn: sqlite3.Connection, person_id: int, day: str | None = None, desde: str | None = None,
                   hasta: str | None = None) -> dict:
    """A day's or a range's training sessions (with their sets, display values plus whether each
    was corrected) and the planned session for that day, if any (BODY-ENTRENO-TOOLS-1). Reads only
    -- `db.exercise_sets` and plain SELECTs, never a writer."""
    if day is not None:
        ini, fin = day, day
    elif desde is not None and hasta is not None:
        ini, fin = desde, hasta
    else:
        raise ToolError("se necesita 'day', o 'desde' y 'hasta'")
    # BODY-ENTRENO-VINCULO-1 ruling 3: both halves of any linked pair in range are fetched by this
    # same WHERE (the watch half matches type = 'strength_training', the manual half matches
    # source = 'manual'), so `db.merge_linked_training` always sees the pair whole and folds it to
    # one session, same as `db.load_day`.
    actividades = db.merge_linked_training(conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day BETWEEN ? AND ? "
        "AND (type = 'strength_training' OR source = 'manual') ORDER BY day, start_local",
        (person_id, ini, fin),
    ).fetchall())
    t = T(db.person(conn, person_id)["language"])
    sesiones = []
    for a in actividades:
        sets = db.exercise_sets(conn, person_id, a["sets_source"], a["sets_source_id"])
        zonas_dia = db.zonas_en_recuperacion(conn, person_id, a["day"])
        sesiones.append({
            "dia": a["day"], "titulo": a["name"], "fuente": a["source"], "hora_inicio": a["start_local"],
            # BODY-SERIES-ORDEN-1 rulings 1, 3, 5: a rest slot is never a set, and a slot the
            # person marked as not done is left out too -- `db.exercise_sets` already reads a
            # corrected slot's own `set_type` as 'active', whatever the watch's alternating
            # active/rest said.
            "series": [{"orden": s["set_order"], "tipo": s["set_type"], "ejercicio": s["exercise"],
                        "categoria": s["category"], "repeticiones": s["reps"], "peso_kg": s["weight_value"],
                        "corregido": s["corrected_by_person"],
                        # BODY-LESION-ZONA-1 ruling 2: the one shared plain line, or None when this
                        # set's own categoria is not among today's open-lesion zonas.
                        "liviano_a_proposito": anatomia.texto_liviano_a_proposito(
                            t, {s["category"]} if s["category"] in zonas_dia else set())}
                       for s in sets
                       if s["set_type"] == "active" and not s["excluded_by_person"]],
            # BODY-ENTRENO-ULTIMA-VEZ-1 ruling 3: additive -- what the person did last time this
            # same exercise came up, by its effective name, so the assistant can say "last time
            # 3x8 at 40 kg" from data, never from memory.
            "ultima_vez": entreno_historial.ultimas_veces(conn, person_id, a["sets_source"], a["sets_source_id"]),
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


def _validar_habitos_plan(habitos) -> list[dict]:
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
        out.append({"titulo": titulo, "cadencia": cadencia})
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


def _validar_reglas_plan(conn: sqlite3.Connection, reglas) -> list[dict]:
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
        out.append({"contexto": contexto, "contexto_valor": contexto_valor, "texto": texto, "chequeo": chequeo})
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
    at most one of `goal_id`/`horizonte_goal_id` -- either becomes the session's own `plan.goal_id`."""
    try:
        date.fromisoformat(day)
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
    despues = {"day": day, "time": time, "titulo": titulo, "goal_id": goal_id, "ejercicios": ejercicios}
    registro = propuestas.crear(conn, person_id, conversacion_id, "planear_entreno", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _registrar_entreno(conn: sqlite3.Connection, person_id: int, conversacion_id: int, day: str, series: list,
                        titulo: str | None = None, time: str | None = None) -> dict:
    """Proposes "here is what I did" for a day (BODY-ENTRENO-TOOLS-1, Q3). When that day has a
    Garmin strength activity, maps the spoken `series` onto the watch's sets by order -- fetching
    them with `pull.traer_series` first if they are not stored yet, so the card can show both the
    watch's value and the spoken correction per set. With no activity, stages a manual session
    instead. `antes` carries, per set, enough of the pre-existing state (`sets_antes`) for
    `propuestas.deshacer` to restore it exactly, or nothing for the manual (insert-shaped) case."""
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
    actividad = conn.execute(
        "SELECT source, source_id FROM activity WHERE person_id = ? AND day = ? AND type = 'strength_training' "
        "ORDER BY start_local LIMIT 1", (person_id, day),
    ).fetchone()
    if actividad is not None:
        source, source_id = actividad["source"], actividad["source_id"]
        existentes = db.exercise_sets(conn, person_id, source, source_id)
        reloj_fallo = False
        if not existentes:
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


def _anotar_sesion(conn: sqlite3.Connection, person_id: int, conversacion_id: int, plan_id: int, nota: str,
                    hecha: bool | None = None) -> dict:
    """Proposes a note on an existing PLANNED session row (BODY-HOGAR-ESCRIBE-1 ruling 4): how the
    session actually went, matched against the plan. `hecha=true` also proposes marking the plan
    'done' (BODY-PLAN-SESION-HECHA-1 then counts it, once, toward sesiones_fuerza); left out or
    false, the plan's own status is proposed unchanged. `antes`/`despues` each carry the plan row's
    FULL note/timestamp/status state, the same update-shaped, caller-resolves-state convention
    `corregir_comida` already uses, so `propuestas.deshacer` can restore it exactly through the
    same writer (`db.anotar_sesion_plan`). Refuses a plan_id that is not this person's own, by
    name -- never silently matched to someone else's session."""
    nota = (nota or "").strip()
    if not (1 <= len(nota) <= 2000):
        raise ToolError("nota debe tener entre 1 y 2000 caracteres")
    if hecha is not None and not isinstance(hecha, bool):
        raise ToolError("hecha debe ser verdadero o falso")
    fila = conn.execute(
        "SELECT day, title, status, nota_sesion, nota_sesion_en FROM plan WHERE id = ? AND person_id = ?",
        (plan_id, person_id),
    ).fetchone()
    if fila is None:
        raise ToolError(f"plan desconocido: {plan_id!r}")
    antes = {"status": fila["status"], "nota_sesion": fila["nota_sesion"], "nota_sesion_en": fila["nota_sesion_en"]}
    nuevo_status = "done" if hecha else fila["status"]
    despues = {"plan_id": plan_id, "day": fila["day"], "titulo": fila["title"], "nota": nota,
               "hecha": bool(hecha), "status": nuevo_status}
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


def _hora_a_ts_local(day: str, hora: str | None) -> str | None:
    return f"{day}T{hora}:00" if hora else None


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
    despues = {"metric_key": metric_key, "amount": amount, "estimado": estimado, "descripcion": descripcion,
               "day": day, "ts_local": _hora_a_ts_local(day, hora)}
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
    if momento is not None and momento not in {m["clave"] for m in comidas.momentos(conn, person_id)}:
        raise ToolError(f"momento desconocido: {momento!r}")
    day = day or _hoy_local(conn, person_id)
    _dia_no_futuro(conn, person_id, day)
    despues = {"descripcion": descripcion, "cantidades": limpias, "day": day,
               "ts_local": _hora_a_ts_local(day, hora), "momento": momento}
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
    nuevo_day = day if day is not None else fila["day"]
    _dia_no_futuro(conn, person_id, nuevo_day)
    if hora is not None:
        nuevo_ts_local = _hora_a_ts_local(nuevo_day, hora)
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
    day = day or _hoy_local(conn, person_id)
    _dia_no_futuro(conn, person_id, day)
    despues = {"day": day, "ts_local": _hora_a_ts_local(day, hora), "momento": momento,
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
    exactly."""
    _dia_no_futuro(conn, person_id, day)
    h = conn.execute(
        "SELECT id, goal_id, title, assignee_person_id, active FROM habit WHERE id = ?", (habit_id,)).fetchone()
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
    despues = {"habit_id": habit_id, "day": day, "done": True, "note": nota, "habito_titulo": h["title"],
               "actividad": actividad_despues}
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
    return out


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
    habitos_v = _validar_habitos_plan(habitos)
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
                    habitos: list | None = None, reglas: list | None = None, semanas: list | None = None,
                    metas_semanales: list | None = None, checkpoints: list | None = None) -> dict:
    """Ruling 1: a whole plan is ONE card. Validates every part BEFORE the proposal is stored
    (every list defaults to empty), so a bad plan is a ToolError, never a card the person could
    tap Aplicar on. Apply writes all of it in one transaction (`db.crear_plan_horizonte`)."""
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
    despues = {
        "titulo": titulo, "why": (why or "").strip() or None, "start_day": start_day, "deadline": deadline,
        "metas_finales": _validar_metas_finales_plan(conn, metas_finales),
        "habitos": _validar_habitos_plan(habitos),
        "reglas": _validar_reglas_plan(conn, reglas),
        "semanas": _validar_semanas_plan(semanas, start_day, deadline),
        "metas_semanales": _validar_metas_semanales_plan(conn, metas_semanales, start_day, deadline),
        "checkpoints": _validar_checkpoints_plan(conn, checkpoints, start_day, deadline),
    }
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_plan", despues)
    return {"propuesta_id": registro["id"], "estado": registro["estado"], "_registro": registro}


def _ampliar_plan(conn: sqlite3.Connection, person_id: int, conversacion_id: int, goal_id: int,
                   reglas: list | None = None, semanas: list | None = None, metas_semanales: list | None = None,
                   checkpoints: list | None = None) -> dict:
    """Ruling 4: insert-only additions to an existing owned horizon. A duplicate (week, medida)
    against an existing vigente weekly line is refused, naming `corregir_meta_plan` instead."""
    goal = conn.execute(
        "SELECT start_day, deadline FROM goal WHERE id = ? AND person_id = ? AND kind = 'milestone'",
        (goal_id, person_id)).fetchone()
    if goal is None:
        raise ToolError(f"plan desconocido: {goal_id!r}")
    start_day, deadline = goal["start_day"], goal["deadline"]
    reglas_v = _validar_reglas_plan(conn, reglas)
    semanas_v = _validar_semanas_plan(semanas, start_day, deadline)
    existentes = {(r["week_start"], r["medida"]) for r in conn.execute(
        "SELECT week_start, medida FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? AND vigente = 1",
        (goal_id,))}
    metas_semanales_v = _validar_metas_semanales_plan(conn, metas_semanales, start_day, deadline,
                                                        existentes=existentes)
    checkpoints_v = _validar_checkpoints_plan(conn, checkpoints, start_day, deadline)
    if not (reglas_v or semanas_v or metas_semanales_v or checkpoints_v):
        raise ToolError("no hay nada para añadir")
    despues = {"goal_id": goal_id, "reglas": reglas_v, "semanas": semanas_v,
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


HERRAMIENTAS = [
    {"nombre": "leer_hoy", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Qué tan listo estás hoy: el puntaje de preparación, los valores del día más reciente y "
                     "las limitaciones abiertas (una lesión en recuperación, con la zona si se nombró).",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_hoy},
    {"nombre": "leer_dia", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Las métricas, el sueño, las actividades y los turnos de un día concreto.",
     "parametros": _PARAM_DIA, "funcion": _leer_dia},
    {"nombre": "leer_como_calculo", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Cómo se calculó el puntaje de preparación de un día: cada parte, su peso y su valor.",
     "parametros": _PARAM_DIA, "funcion": _leer_como_calculo},
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
     "descripcion": "Las sesiones de entrenamiento de un día o un rango de días, con sus series, y el "
                     "plan de entrenamiento de ese día si existe.",
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
                     "paso más grueso o divide el rango.",
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
     "descripcion": "Propone un plan completo: metas finales, hábitos, reglas, semanas, metas semanales y "
                     "checkpoints, todo en una sola tarjeta. No crea nada: registra la propuesta para que la "
                     "persona la apruebe en el chat; si la aprueba, todo se guarda junto o nada se guarda. "
                     "En metas_semanales y en los criterios de un checkpoint, el valor de una medida de reloj "
                     "(hora_acostarse, hora_levantarse) se escribe \"HH:MM\" en formato 24 horas, por ejemplo "
                     "\"23:00\"; el valor de cualquier otra medida es un número.",
     "parametros": _PARAM_PROPONER_PLAN, "funcion": _proponer_plan},
    {"nombre": "ampliar_plan", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone añadir reglas, semanas, metas semanales o checkpoints nuevos a un plan que la "
                     "persona ya tiene. Para cambiar el valor de una meta semanal que ya existe usa "
                     "corregir_meta_plan, no esta. No añade nada: registra la propuesta para que la persona "
                     "la apruebe en el chat. En metas_semanales y en los criterios de un checkpoint, el valor "
                     "de una medida de reloj (hora_acostarse, hora_levantarse) se escribe \"HH:MM\" en formato "
                     "24 horas, por ejemplo \"23:00\"; el valor de cualquier otra medida es un número.",
     "parametros": _PARAM_AMPLIAR_PLAN, "funcion": _ampliar_plan},
    {"nombre": "corregir_meta_plan", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone corregir el valor de una meta semanal de un plan (por ejemplo, bajar la meta de "
                     "una semana difícil). No sobrescribe nada: registra la corrección como un nuevo valor, "
                     "para que la persona la apruebe en el chat. Si esa meta es de una medida de reloj "
                     "(hora_acostarse, hora_levantarse), valor se escribe \"HH:MM\" en formato 24 horas, por "
                     "ejemplo \"23:00\"; si no, valor es un número.",
     "parametros": _PARAM_CORREGIR_META_PLAN, "funcion": _corregir_meta_plan},
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
                     "solo dices el límite. No lo crea: registra la propuesta para que la persona la apruebe "
                     "en el chat.",
     "parametros": _PARAM_PLANEAR_ENTRENO, "funcion": _planear_entreno},
    {"nombre": "registrar_entreno", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone registrar lo que la persona hizo en un entrenamiento de un día: si el reloj "
                     "ya tiene una actividad de fuerza ese día, compara serie por serie; si no, crea una "
                     "sesión manual. No lo guarda: registra la propuesta para que la persona la apruebe "
                     "en el chat.",
     "parametros": _PARAM_REGISTRAR_ENTRENO, "funcion": _registrar_entreno},
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
                     "guarda: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_REGISTRAR_MEDIDA, "funcion": _registrar_medida},
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
                     "persona recibe su porción para aplicarla ella misma, nunca entra a su registro sola. No "
                     "lo guarda: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_REGISTRAR_COMIDA, "funcion": _registrar_comida},
    {"nombre": "corregir_comida", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone corregir la hora, el día, la descripción o las cantidades de una comida que ya "
                     "existe -- toda la comida como una sola propuesta, nunca una por cada nutriente. Usa esta "
                     "herramienta para cambiar la hora, la redacción o las cantidades de una comida; para "
                     "corregir o eliminar un solo registro de consumo que no forma parte de una comida sigue "
                     "usando corregir_ingesta/borrar_ingesta. 'cantidades', cuando se da, reemplaza por "
                     "completo el conjunto de cantidades de la comida -- nunca se combina con lo que ya "
                     "tenía. No la corrige: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_CORREGIR_COMIDA, "funcion": _corregir_comida},
    {"nombre": "registrar_checkin", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone un chequeo de 10 segundos: ánimo, energía y estrés sentido, cada uno del 1 al 5, "
                     "más una nota opcional de una línea. Pregunta solo esos tres números y esa línea -- nunca "
                     "más -- y usa únicamente lo que la persona te dé, sin inventar un valor que no dijo. No lo "
                     "guarda: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_REGISTRAR_CHECKIN, "funcion": _registrar_checkin},
    {"nombre": "marcar_periodo", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone marcar unos días como un período especial (malestar, viaje o lesión) para que "
                     "Body los deje fuera cuando compare con lo habitual, sin leerlos como efecto de un "
                     "hábito o experimento. Ofrécelo cuando la persona diga que está enferma, de viaje o "
                     "lesionada -- nunca lo crees en silencio. Si la persona nombra una parte del cuerpo "
                     "limitada (por ejemplo, un hombro en recuperación), pon esa zona en 'zonas': así Body "
                     "lee un entrenamiento más liviano ahí como algo a propósito, nunca como una caída. No "
                     "lo marca: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_MARCAR_PERIODO, "funcion": _marcar_periodo},
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
                     "qué pasó realmente en ese rango.",
     "parametros": _PARAM_LEER_PLAN, "funcion": _leer_plan},
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
    """A lectura tool was unknown, unavailable at this nivel_datos, or given invalid parameters."""


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
        return entrada["funcion"](conn, person_id, **params)
    except sqlite3.OperationalError as e:
        mensaje = str(e.args[0]) if e.args else ""
        if "readonly database" not in mensaje:
            raise
        raise ToolError("lectura_intento_escribir") from None
    finally:
        conn.execute(f"PRAGMA query_only = {'ON' if previo else 'OFF'}")
