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
from datetime import date, timedelta

from .. import anatomia, catalogo_tendencias, compartir, db, enlaces, entreno_historial, errores, experimentos, \
    fechas, goals, history, household, nutriente_objetivo, profile, pull, readiness, shift_import, \
    turnos_costo, tutoriales
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

DIRECTIONS = ("raise", "lower", "keep")

_PARAM_PROPONER_META = {
    "type": "object",
    "properties": {
        "metric_key": {"type": "string"},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "target_value": {"type": "number"},
        "deadline": {"type": "string", "format": "date"},
        "titulo": {"type": "string"},
    },
    "required": ["metric_key", "direction", "target_value", "deadline", "titulo"],
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
_PARAM_REGISTRAR_COMIDA = {
    "type": "object",
    "properties": {
        "day": {"type": "string", "format": "date"},
        "hora": {"type": "string"},
        "descripcion": {"type": "string"},
        "cantidades": {"type": "array", "items": _PARAM_CANTIDAD_COMIDA, "minItems": 1},
        "compartida_con": {"type": "array", "items": _PARAM_COMENSAL_COMPARTIDO},
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


def _planear_entreno(conn: sqlite3.Connection, person_id: int, conversacion_id: int, day: str, titulo: str,
                      ejercicios: list, time: str | None = None, goal_id: int | None = None) -> dict:
    """Proposes one `plan` row for a day: a title, an optional time and a list of planned
    exercises `{ejercicio, series, reps?, categoria?}` (BODY-ENTRENO-TOOLS-1, Q3; categoria added
    BODY-CUERPO-VISTA-1-M ruling 2, validated at the door against `anatomia.CATEGORIA_ENUM`). Hoy's
    own body view lights its glow groups from this categoria (BODY-CUERPO-VISTA-1 ruling 3).
    Insert-shaped, like `proponer_meta`: there is no 'before'. Unlike `_registrar_entreno`/
    `_registrar_medida`, a future `day` is allowed on purpose here: a plan is for later, not for
    what already happened."""
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
    if goal_id is not None:
        g = conn.execute("SELECT 1 FROM goal WHERE id = ? AND person_id = ?", (goal_id, person_id)).fetchone()
        if g is None:
            raise ToolError(f"meta desconocida: {goal_id!r}")
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
                       compartida_con: list | None = None) -> dict:
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
    applying it is what actually shares the meal (body/agente/propuestas.py)."""
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
    day = day or _hoy_local(conn, person_id)
    _dia_no_futuro(conn, person_id, day)
    despues = {"descripcion": descripcion, "cantidades": limpias, "day": day,
               "ts_local": _hora_a_ts_local(day, hora)}
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


def _proponer_experimento(conn: sqlite3.Connection, person_id: int, conversacion_id: int, titulo: str,
                           metricas: list, ventana: dict, filtro_dias: dict, fecha_inicio: str | None = None,
                           fecha_fin: str | None = None, dias_prueba_objetivo: int | None = None,
                           habito_id: int | None = None) -> dict:
    """Proposes a new experiment (BODY-EXPERIMENTOS-REGISTRO-1): insert-shaped, like
    `planear_entreno` -- there is no 'before'. Validated with `db.validar_experimento` and the same
    habit-ownership rule `db.crear_experimento` itself enforces, BEFORE the proposal is stored, so
    a bad proposal is a ToolError, never a card the person could tap Aplicar on."""
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
               "habito_id": habito_id}
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


def _proponer_meta(conn: sqlite3.Connection, person_id: int, conversacion_id: int, metric_key: str,
                    direction: str, target_value, deadline: str, titulo: str) -> dict:
    titulo = (titulo or "").strip()
    if not titulo:
        raise ToolError("falta el título de la meta")
    if direction not in DIRECTIONS:
        raise ToolError(f"direction inválida: {direction!r}; debe ser una de {DIRECTIONS}")
    if conn.execute("SELECT 1 FROM metric_def WHERE key = ?", (metric_key,)).fetchone() is None:
        raise ToolError(f"metric_key desconocida: {metric_key!r}")
    try:
        date.fromisoformat(deadline)
    except ValueError:
        raise ToolError("deadline debe ser una fecha ISO (AAAA-MM-DD)")
    despues = {"metric_key": metric_key, "direction": direction, "target_value": float(target_value),
               "deadline": deadline, "titulo": titulo, "start_day": _hoy_local(conn, person_id)}
    registro = propuestas.crear(conn, person_id, conversacion_id, "proponer_meta", despues)
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
    {"nombre": "leer_tutoriales", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Los tutoriales del catálogo de Body: su capítulo, su pantalla, si el asistente ya lo hace "
                     "y si ya existe un video. Úsala para responder cómo se hace algo en la app.",
     "parametros": _PARAM_LEER_TUTORIALES, "funcion": _leer_tutoriales},
    {"nombre": "leer_entreno", "tipo": "lectura", "nivel": "completo",
     "descripcion": "Las sesiones de entrenamiento de un día o un rango de días, con sus series, y el "
                     "plan de entrenamiento de ese día si existe.",
     "parametros": _PARAM_LEER_ENTRENO, "funcion": _leer_entreno},
    {"nombre": "proponer_meta", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone crear una meta nueva de tipo métrica. No la crea: registra la propuesta "
                     "para que la persona la apruebe en el chat.",
     "parametros": _PARAM_PROPONER_META, "funcion": _proponer_meta},
    {"nombre": "proponer_cambio_meta", "tipo": "propuesta", "nivel": "ninguno",
     "descripcion": "Propone cambiar el valor objetivo y/o la fecha de una meta existente de la persona. "
                     "No la cambia: registra la propuesta para que la persona la apruebe en el chat.",
     "parametros": _PARAM_PROPONER_CAMBIO_META, "funcion": _proponer_cambio_meta},
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

_BY_NAME = {h["nombre"]: h for h in HERRAMIENTAS}
_RANK = {"ninguno": 0, "resumen": 1, "completo": 2}


class ToolError(Exception):
    """A lectura tool was unknown, unavailable at this nivel_datos, or given invalid parameters."""


def disponibles(nivel: str) -> list[dict]:
    if nivel not in ajustes.NIVELES:
        raise ToolError(f"nivel inválido: {nivel!r}; debe ser uno de {ajustes.NIVELES}")
    turnos_on = _TURNOS_ACTUAL.get()
    return [h for h in HERRAMIENTAS if _RANK[h["nivel"]] <= _RANK[nivel]
            and (turnos_on or h["nombre"] not in TURNOS_HERRAMIENTAS)]


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
    return entrada["funcion"](conn, person_id, **params)
