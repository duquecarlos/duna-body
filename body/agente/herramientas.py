"""The assistant's one static tool registry. Every entry here is `lectura` (read-only): it answers a
question from an existing read function and never writes. `disponibles(nivel)` filters the registry to
what a person's `ajuste_agente.nivel_datos` allows; `ejecutar(...)` refuses a tool outside that level, an
unknown name, or a call with the wrong parameters. No provider client, no network, no screen -- this
module is plain Python called with a connection a caller already opened.

`leer_metas` and `leer_meta` read the newest EXISTING `goal_check` row with a plain SELECT. They must
never call `goals.weekly_check` (what `body/web.py`'s `_goal_check_row` does) -- that function upserts a
row, and a `lectura` tool must never write (BODY-ASISTENTE-OBS-1).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from .. import db, history, profile, readiness, shift_import
from . import ajustes

TENDENCIA_METRICAS = ("readiness", "sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake", "steps")
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
    return {"cobertura": history.coverage(conn, person_id), "entrenamiento": history.training(conn, person_id),
            "patrones": history.patterns(conn, person_id), "por_turno": history.by_shift(conn, person_id)}


def _ultima_verificacion(conn: sqlite3.Connection, goal_id: int, person_id: int):
    row = conn.execute(
        "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? ORDER BY week_start DESC LIMIT 1",
        (goal_id, person_id),
    ).fetchone()
    return dict(row) if row else "sin verificación aún"


def _leer_metas(conn: sqlite3.Connection, person_id: int) -> dict:
    rows = conn.execute("SELECT * FROM goal WHERE person_id = ? AND status = 'active' ORDER BY id",
                        (person_id,)).fetchall()
    return {"metas": [{"id": g["id"], "titulo": g["title"], "tipo": g["kind"], "area": g["area"],
                       "ultima_verificacion": _ultima_verificacion(conn, g["id"], person_id)} for g in rows]}


def _leer_meta(conn: sqlite3.Connection, person_id: int, goal_id: int) -> dict:
    g = conn.execute("SELECT * FROM goal WHERE id = ? AND person_id = ?", (goal_id, person_id)).fetchone()
    if g is None:
        return {"encontrada": False}
    habitos = conn.execute("SELECT id, title, cadence FROM habit WHERE goal_id = ? AND active = 1 ORDER BY id",
                           (goal_id,)).fetchall()
    verificaciones = conn.execute(
        "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? ORDER BY week_start DESC LIMIT 8",
        (goal_id, person_id),
    ).fetchall()
    return {
        "encontrada": True,
        "meta": {"id": g["id"], "titulo": g["title"], "tipo": g["kind"], "area": g["area"], "estado": g["status"],
                 "metric_key": g["metric_key"], "direccion": g["direction"], "valor_objetivo": g["target_value"],
                 "valor_base": g["baseline_value"], "inicio": g["start_day"], "plazo": g["deadline"]},
        "habitos": [{"id": h["id"], "titulo": h["title"], "cadencia": json.loads(h["cadence"])} for h in habitos],
        "verificaciones": [dict(c) for c in verificaciones] if verificaciones else "sin verificación aún",
    }


def _leer_turnos(conn: sqlite3.Connection, person_id: int) -> dict:
    confirmado = shift_import.latest_confirmed(conn, person_id)
    return {
        "propuestos": shift_import.summary_proposed(conn, person_id),
        "ultimo_confirmado": ({"recibido": confirmado["received"], "dias": confirmado["day_count"]}
                               if confirmado else None),
    }


def _leer_ajustes(conn: sqlite3.Connection, person_id: int) -> dict:
    p = db.person(conn, person_id)
    return {"nombre": p["display_name"], "zona_horaria": p["timezone"], "idioma": p["language"],
            "nivel_datos": ajustes.nivel_datos(conn, person_id)}


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
    {"nombre": "leer_ajustes", "tipo": "lectura", "nivel": "ninguno",
     "descripcion": "Tus datos de perfil (nombre, zona horaria, idioma) y tu nivel de datos del asistente.",
     "parametros": _SIN_PARAMETROS, "funcion": _leer_ajustes},
]

_BY_NAME = {h["nombre"]: h for h in HERRAMIENTAS}
_RANK = {"ninguno": 0, "resumen": 1, "completo": 2}


class ToolError(Exception):
    """A lectura tool was unknown, unavailable at this nivel_datos, or given invalid parameters."""


def disponibles(nivel: str) -> list[dict]:
    if nivel not in ajustes.NIVELES:
        raise ToolError(f"nivel inválido: {nivel!r}; debe ser uno de {ajustes.NIVELES}")
    return [h for h in HERRAMIENTAS if _RANK[h["nivel"]] <= _RANK[nivel]]


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


def ejecutar(conn: sqlite3.Connection, person_id: int, nombre: str, params: dict | None, nivel: str):
    if nivel not in ajustes.NIVELES:
        raise ToolError(f"nivel inválido: {nivel!r}; debe ser uno de {ajustes.NIVELES}")
    entrada = _BY_NAME.get(nombre)
    if entrada is None:
        raise ToolError(f"herramienta desconocida: {nombre!r}")
    if _RANK[entrada["nivel"]] > _RANK[nivel]:
        raise ToolError(f"{nombre!r} requiere nivel_datos >= {entrada['nivel']!r}; el ajuste actual es {nivel!r}")
    params = params or {}
    _validar_parametros(entrada["parametros"], params)
    return entrada["funcion"](conn, person_id, **params)
