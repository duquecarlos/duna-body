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

The four `proponer_*` tools below are `tipo: propuesta` (BODY-ASISTENTE-PROPUESTAS-1): they create a
server-side proposal record through `body/agente/propuestas.py` and never write the target
themselves. `propuestas.py` is the one module a `propuesta` tool may import -- the static guard in
tests/test_agente_registro.py checks that a `lectura` tool's call graph never reaches it.
"""
from __future__ import annotations

import contextvars
import json
import sqlite3
from datetime import date, timedelta

from .. import db, enlaces, fechas, goals, history, household, profile, readiness, shift_import, turnos_costo, \
    tutoriales
from ..i18n import LANGS
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

TENDENCIA_METRICAS = ("readiness", "sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake", "steps",
                      "vo2max", "fitness_age",
                      "sleep_score", "sleep_deep_min", "sleep_rem_min", "sleep_light_min", "sleep_awake_min",
                      "body_battery_high", "body_battery_low", "body_battery_sleep_charge",
                      "stress_avg", "high_stress_min", "intensity_minutes", "active_kcal", "respiration_awake")
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


def _readiness_dict(r: readiness.Readiness) -> dict:
    return {
        "dia": r.day, "puntaje": r.score, "banda": r.band, "version": r.version,
        "faltantes": r.missing,
        "partes": [{"clave": p.key, "valor": p.value, "referencia": p.reference,
                    "subpuntaje": p.subscore, "peso": p.weight} for p in r.parts],
    }


def _leer_hoy(conn: sqlite3.Connection, person_id: int) -> dict:
    dias = db.days_with_data(conn, person_id)
    if not dias:
        return {"dia": None, "mensaje": "sin datos aún"}
    dia = dias[0]
    return {"dia": dia, "listo": _readiness_dict(readiness.compute(conn, dia, person_id)),
            "valores": db.day_values(conn, dia, person_id)}


def _leer_dia(conn: sqlite3.Connection, person_id: int, day: str) -> dict:
    return db.load_day(conn, day, person_id)


def _leer_como_calculo(conn: sqlite3.Connection, person_id: int, day: str) -> dict:
    return _readiness_dict(readiness.compute(conn, day, person_id))


def _tendencia_metric(conn: sqlite3.Connection, person_id: int, key: str, rango: str, last_day: str | None) -> dict | None:
    p = profile.metric_profile(conn, key, person_id=person_id)
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
     "descripcion": "Qué tan listo estás hoy: el puntaje de preparación y los valores del día más reciente.",
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
                     "recuperación, nunca el valor de un día.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_costo_turnos},
    {"nombre": "leer_ajustes", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Tus datos de perfil (nombre, zona horaria, idioma) y tu nivel de datos del asistente.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_ajustes},
    {"nombre": "leer_tutoriales", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Los tutoriales del catálogo de Body: su capítulo, su pantalla, si el asistente ya lo hace "
                     "y si ya existe un video. Úsala para responder cómo se hace algo en la app.",
     "parametros": _PARAM_LEER_TUTORIALES, "funcion": _leer_tutoriales},
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
