"""The assistant (BODY-ASISTENTE-PROVEEDOR-1): one tool loop, streamed as SSE, plus the
conversation GET and the Aplicar/Descartar/Deshacer propuesta actions (BODY-WEB-ESTRUCTURA-4,
pure move out of body/web.py).

`asistente_preguntar` is the connection guard's one documented exception
(tests/test_conexiones_cerradas.py): its StreamingResponse generator keeps using the connection
after the route function itself returns, and closes it in the generator's own `finally` -- it
carries neither `_cierra_conexion` decorator, by name alone (no other route in this app is named
`asistente_preguntar`).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import db, history, identity
from ..agente import bucle as agente_bucle
from ..agente import cliente as agente_cliente
from ..agente import conversacion as agente_conversacion
from ..agente import herramientas as agente_herramientas
from ..agente import propuestas as agente_propuestas
from ..agente import uso as agente_uso
from ..i18n import T
from . import comun

router = APIRouter()

MAX_PREGUNTA_CHARS = 2000
IDIOMA_LABEL = {"es": "Español", "en": "English"}
_ETIQUETA_KEYS = {
    "aplicar": "asistente.propuesta.aplicar", "descartar": "asistente.propuesta.descartar",
    "deshacer": "asistente.propuesta.deshacer", "aplicada": "asistente.propuesta.aplicada",
    "descartada": "asistente.propuesta.descartada", "deshecha": "asistente.propuesta.deshecha",
    "no_vigente": "asistente.propuesta.no_vigente", "desactualizada": "asistente.propuesta.desactualizada",
    "sin_deshacer": "asistente.propuesta.sin_deshacer",
    "entreno_reloj": "asistente.propuesta.entreno_reloj", "entreno_tu": "asistente.propuesta.entreno_tu",
}


def _evento_sse(nombre: str, data: dict) -> str:
    return f"event: {nombre}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _etiquetas(t: T) -> dict:
    return {k: t(v) for k, v in _ETIQUETA_KEYS.items()}


def _objetivo_txt(t: T, defs: dict, metric_key: str | None, target_value, deadline: str) -> str:
    fecha = comun._short_date(t, date.fromisoformat(deadline))
    if metric_key is None or target_value is None:
        return t("goals.card.milestone_date", date=fecha)
    return t("asistente.propuesta.meta_objetivo", valor=comun._fig_unit(t, metric_key, target_value, defs), fecha=fecha)


def _ejercicio_plan_txt(t: T, e: dict) -> str:
    if e.get("reps") is not None:
        return t("asistente.propuesta.entreno_ejercicio_reps", ejercicio=e["ejercicio"],
                 series=t.num(e["series"]), reps=e["reps"])
    return t("asistente.propuesta.entreno_ejercicio", ejercicio=e["ejercicio"], series=t.num(e["series"]))


def _nombre_legible(codigo: str | None) -> str | None:
    """Garmin's own code ("BARBELL_ROW") made readable for display, underscores as spaces and only
    the first letter capital (BODY-ENTRENO-TOOLS-1-M fix 1) -- never the stored garmin_exercise/
    garmin_category themselves, which stay the raw code."""
    if not codigo:
        return None
    texto = codigo.replace("_", " ").strip()
    return f"{texto[:1].upper()}{texto[1:].lower()}" if texto else None


def _serie_txt(t: T, s: dict | None, nombre: str | None = None) -> str | None:
    """One set's short display text for the watch-vs-you table: "<ejercicio> · <reps/weight>",
    or None for an empty cell (no watch set at that order). nombre, when given, overrides the
    exercise text (the watch side's readable name, built by _nombre_legible from the raw
    garmin_exercise/garmin_category -- fix 1)."""
    if s is None:
        return None
    ejercicio = (nombre if nombre is not None else (s.get("ejercicio") or "")).strip()
    reps, peso = s.get("reps"), s.get("peso_kg")
    if reps is not None and peso is not None:
        detalle = t("asistente.propuesta.entreno_reps_peso", reps=t.num(reps), peso=t.num(peso))
    elif reps is not None:
        detalle = t("asistente.propuesta.entreno_reps", reps=t.num(reps))
    elif peso is not None:
        detalle = t("asistente.propuesta.entreno_peso", peso=t.num(peso))
    else:
        detalle = None
    if ejercicio and detalle:
        return f"{ejercicio} · {detalle}"
    return ejercicio or detalle or t("asistente.propuesta.entreno_sin_dato")


def _actividad_txt(t: T, actividad: dict) -> str:
    """The watch activity that is a habit's evidence, by name plus duration -- mirrors body/rutas/
    hoy.py's `_activity_name` fallback (an unnamed activity shows its translated known type, or a
    generic label) and `t.minutes`, since that module is outside this slice's touches."""
    nombre = actividad.get("nombre")
    if not nombre:
        tipo = actividad.get("tipo")
        nombre = t("activity.type." + tipo) if tipo in history.KNOWN_ACTIVITY_TYPES else t("activity.generic")
    duracion = actividad.get("duracion_s")
    return f"{nombre} · {t.minutes(duracion / 60)}" if duracion else nombre


def _tarjeta_meta_nueva(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    despues_txt = t("asistente.propuesta.meta_nueva", metric=t("metric." + despues["metric_key"]),
                    direccion=t("asistente.propuesta.direccion." + despues["direction"]),
                    valor=comun._fig_unit(t, despues["metric_key"], despues["target_value"], defs),
                    fecha=comun._short_date(t, date.fromisoformat(despues["deadline"])))
    return None, despues_txt, None


def _tarjeta_cambio_meta(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    metric_key = despues.get("metric_key")
    antes_txt = _objetivo_txt(t, defs, metric_key, antes_json["target_value"], antes_json["deadline"])
    despues_txt = _objetivo_txt(t, defs, metric_key, despues["target_value"], despues["deadline"])
    return antes_txt, despues_txt, None


def _tarjeta_confirmar_turnos(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    resumen = antes_json["resumen"]
    antes_txt = t("asistente.propuesta.turnos_actual", n=t.num(resumen["batch_count"]))
    despues_txt = t("asistente.propuesta.turnos_nuevo")
    return antes_txt, despues_txt, None


def _tarjeta_idioma(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    antes_txt = IDIOMA_LABEL.get(antes_json["lang"], antes_json["lang"])
    despues_txt = IDIOMA_LABEL.get(despues["lang"], despues["lang"])
    return antes_txt, despues_txt, None


def _tarjeta_planear_entreno(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    ejercicios_txt = ", ".join(_ejercicio_plan_txt(t, e) for e in despues["ejercicios"])
    despues_txt = t("asistente.propuesta.entreno_plan", titulo=despues["titulo"],
                    fecha=comun._short_date(t, date.fromisoformat(despues["day"])), ejercicios=ejercicios_txt)
    return None, despues_txt, None


def _tarjeta_registrar_entreno(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    titulo_manual = despues.get("titulo") if despues["manual"] else None
    if titulo_manual:
        despues_txt = t("asistente.propuesta.entreno_registrado_titulo", titulo=titulo_manual, fecha=fecha)
    else:
        despues_txt = t("asistente.propuesta.entreno_registrado", fecha=fecha)
    if despues["manual"]:
        series_tarjeta = [{"orden": i, "reloj": None, "tu": _serie_txt(t, s)}
                           for i, s in enumerate(despues["series"], start=1)]
    else:
        # The watch side's readable name comes from the raw garmin_exercise/garmin_category in
        # antes_json["sets_antes"] -- never from m["reloj"]["ejercicio"], which can already be a
        # prior person correction (fix 1).
        crudos = {s["set_order"]: s for s in antes_json["sets_antes"]}
        series_tarjeta = []
        for m in despues["mapeo"]:
            crudo = crudos.get(m["set_order"], {})
            nombre = _nombre_legible(crudo.get("garmin_exercise")) or _nombre_legible(crudo.get("garmin_category"))
            series_tarjeta.append({"orden": m["set_order"], "reloj": _serie_txt(t, m["reloj"], nombre=nombre),
                                    "tu": _serie_txt(t, m["tu"])})
    return None, despues_txt, series_tarjeta


def _tarjeta_registrar_medida(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    metric_key = despues["metric_key"]
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    antes_txt = None
    if antes_json and antes_json.get("value") is not None:
        antes_txt = t("asistente.propuesta.medida_valor", metric=t("metric." + metric_key),
                      valor=comun._fig_unit(t, metric_key, antes_json["value"], defs), fecha=fecha)
    despues_txt = t("asistente.propuesta.medida_valor", metric=t("metric." + metric_key),
                    valor=comun._fig_unit(t, metric_key, despues["value"], defs), fecha=fecha)
    return antes_txt, despues_txt, None


def _momento_ingesta_txt(t: T, ts_local: str | None) -> str | None:
    """BODY-PROTEINA-1: a plain-word time-of-day label from `ts_local`'s own hour -- "lunch" style,
    never shown when there is no `ts_local` (the entry is "sometime that day" only)."""
    if not ts_local:
        return None
    try:
        hora = datetime.fromisoformat(ts_local).hour
    except ValueError:
        return None
    if 5 <= hora < 11:
        return t("asistente.propuesta.ingesta_momento_desayuno")
    if 11 <= hora < 16:
        return t("asistente.propuesta.ingesta_momento_almuerzo")
    if 16 <= hora < 19:
        return t("asistente.propuesta.ingesta_momento_merienda")
    if 19 <= hora < 23:
        return t("asistente.propuesta.ingesta_momento_cena")
    return None


def _ingesta_valor_txt(t: T, defs: dict, metric_key: str, amount: float, estimado: bool, fecha: str) -> str:
    valor = comun._fig_unit(t, metric_key, amount, defs)
    if estimado:
        valor = t("asistente.propuesta.ingesta_valor_estimado", valor=valor)
    return t("asistente.propuesta.ingesta_valor", metric=t("metric." + metric_key), valor=valor, fecha=fecha)


def _tarjeta_registrar_ingesta(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    partes = [_ingesta_valor_txt(t, defs, despues["metric_key"], despues["amount"], despues["estimado"], fecha)]
    momento = _momento_ingesta_txt(t, despues.get("ts_local"))
    if momento:
        partes.append(momento)
    if despues.get("descripcion"):
        partes.append(t("asistente.propuesta.ingesta_descripcion", descripcion=despues["descripcion"]))
    return None, " · ".join(partes), None


def _tarjeta_corregir_ingesta(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    antes_txt = _ingesta_valor_txt(t, defs, despues["metric_key"], antes_json["amount"], despues["estimado"], fecha)
    despues_txt = _ingesta_valor_txt(t, defs, despues["metric_key"], despues["amount"], despues["estimado"], fecha)
    return antes_txt, despues_txt, None


def _tarjeta_borrar_ingesta(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    fecha = comun._short_date(t, date.fromisoformat(antes_json["day"]))
    partes = [_ingesta_valor_txt(t, defs, antes_json["metric_key"], antes_json["amount"], antes_json["estimado"],
                                 fecha)]
    momento = _momento_ingesta_txt(t, antes_json.get("ts_local"))
    if momento:
        partes.append(momento)
    if antes_json.get("descripcion"):
        partes.append(t("asistente.propuesta.ingesta_descripcion", descripcion=antes_json["descripcion"]))
    antes_txt = " · ".join(partes)
    despues_txt = t("asistente.propuesta.ingesta_eliminado")
    return antes_txt, despues_txt, None


def _filtro_dias_txt(t: T, filtro_dias: dict) -> str:
    """"which days count" in plain words (BODY-EXPERIMENTOS-REGISTRO-1): the shift kinds a day must
    or must not have, plus whether a post-night recovery day is excluded -- built from
    `shift.kind.*`'s own labels, never a second hand-typed name per kind."""
    partes = []
    if filtro_dias.get("turno_en"):
        nombres = ", ".join(t("shift.kind." + k) for k in filtro_dias["turno_en"]).lower()
        partes.append(t("asistente.propuesta.experimento_turno_en", turnos=nombres))
    if filtro_dias.get("turno_fuera"):
        nombres = ", ".join(t("shift.kind." + k) for k in filtro_dias["turno_fuera"]).lower()
        partes.append(t("asistente.propuesta.experimento_turno_fuera", turnos=nombres))
    if filtro_dias.get("excluir_tras_noche"):
        partes.append(t("asistente.propuesta.experimento_sin_tras_noche"))
    if not partes:
        return t("asistente.propuesta.experimento_todos_los_dias")
    return ", ".join(partes)


def _tarjeta_proponer_experimento(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    metricas_txt = ", ".join(t("metric." + m) for m in despues["metricas"])
    dias_txt = _filtro_dias_txt(t, despues["filtro_dias"])
    fecha = comun._short_date(t, date.fromisoformat(despues["fecha_inicio"]))
    despues_txt = t("asistente.propuesta.experimento_nuevo", titulo=despues["titulo"], metricas=metricas_txt,
                    dias=dias_txt, fecha=fecha)
    return None, despues_txt, None


def _tarjeta_marcar_experimento(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    titulo = despues["titulo"]
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    clave = "asistente.propuesta.experimento_hecho" if despues["ocurrio"] else "asistente.propuesta.experimento_no_hecho"
    despues_txt = t(clave, experimento=titulo, fecha=fecha)
    antes_txt = None
    if antes_json is not None:
        clave_antes = ("asistente.propuesta.experimento_hecho" if antes_json["ocurrio"]
                       else "asistente.propuesta.experimento_no_hecho")
        antes_txt = t(clave_antes, experimento=titulo, fecha=fecha)
    return antes_txt, despues_txt, None


def _tarjeta_marcar_habito(t: T, defs: dict, despues: dict, antes_json: dict | None) -> tuple:
    habito = despues["habito_titulo"]
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    if despues.get("actividad"):
        despues_txt = t("asistente.propuesta.habito_hecho_actividad", habito=habito, fecha=fecha,
                        actividad=_actividad_txt(t, despues["actividad"]))
    else:
        despues_txt = t("asistente.propuesta.habito_hecho", habito=habito, fecha=fecha)
    antes_txt = None
    if antes_json is not None and antes_json.get("done") is False:
        antes_txt = t("asistente.propuesta.habito_no_marcado", habito=habito, fecha=fecha)
    return antes_txt, despues_txt, None


# One card builder per action (BODY-PROPUESTAS-TABLA-1), keyed exactly like body/agente/
# propuestas.py's TABLA and body/agente/herramientas.py's `propuesta`-kind tools
# (tests/test_propuestas_tabla.py enforces the three sets match). Each builder returns
# (antes_txt, despues_txt, series_tarjeta); _tarjeta_propuesta below applies the shared frame.
TARJETAS = {
    "proponer_meta": _tarjeta_meta_nueva,
    "proponer_cambio_meta": _tarjeta_cambio_meta,
    "proponer_confirmar_turnos": _tarjeta_confirmar_turnos,
    "proponer_idioma": _tarjeta_idioma,
    "planear_entreno": _tarjeta_planear_entreno,
    "registrar_entreno": _tarjeta_registrar_entreno,
    "registrar_medida": _tarjeta_registrar_medida,
    "marcar_habito": _tarjeta_marcar_habito,
    "proponer_experimento": _tarjeta_proponer_experimento,
    "marcar_experimento": _tarjeta_marcar_experimento,
    "registrar_ingesta": _tarjeta_registrar_ingesta,
    "corregir_ingesta": _tarjeta_corregir_ingesta,
    "borrar_ingesta": _tarjeta_borrar_ingesta,
}


def _tarjeta_propuesta(t: T, defs: dict, registro: dict) -> dict:
    """The `propuesta` SSE event payload (agent-capabilities-v1 section 2, decision 5): `antes`/
    `despues` are short plain-word strings in the person's language, built here from the record's
    structured JSON -- never stored as text. `etiquetas` carries this card's button and result
    labels pre-rendered, since body/templates/base.html (outside this slice's touches) is the only
    other place assistant.js could read i18n text from."""
    accion = registro["accion"]
    despues = registro["despues_json"]
    antes_json = registro["antes_json"]
    antes_txt, despues_txt, series_tarjeta = TARJETAS[accion](t, defs, despues, antes_json)
    if antes_txt is not None:
        antes_txt = t("asistente.propuesta.antes", valor=antes_txt)
        despues_txt = t("asistente.propuesta.despues", valor=despues_txt)
    elif despues_txt:
        despues_txt = t("asistente.propuesta.solo_propuesta", valor=despues_txt)
    return {
        "id": registro["id"], "accion": accion, "titulo": t("asistente.propuesta.titulo." + accion),
        "antes": antes_txt, "despues": despues_txt, "series": series_tarjeta, "expira_en": registro["expira_en"],
        "deshacer": accion not in agente_propuestas.SIN_DESHACER, "estado": registro["estado"],
        "etiquetas": _etiquetas(t),
    }


@router.post("/asistente/preguntar")
async def asistente_preguntar(request: Request, person_id: int = Depends(identity.current_person_id)):
    try:
        cuerpo = await request.json()
    except Exception:
        raise HTTPException(400)
    if not isinstance(cuerpo, dict):
        raise HTTPException(400)
    pregunta = cuerpo.get("pregunta")
    if not isinstance(pregunta, str) or not (1 <= len(pregunta) <= MAX_PREGUNTA_CHARS):
        raise HTTPException(400)
    conversacion_id = cuerpo.get("conversacion_id")
    if conversacion_id is not None and not isinstance(conversacion_id, int):
        raise HTTPException(400)

    conn, _, t = comun._ctx(person_id)
    agente_herramientas.fijar_turnos_actual(db.trabaja_turnos_si(conn, person_id))
    if conversacion_id is not None:
        try:
            agente_conversacion.verificar_propiedad(conn, conversacion_id, person_id)
        except agente_conversacion.ConversacionAjena:
            conn.close()
            raise HTTPException(404)
    else:
        conversacion_id = agente_conversacion.crear(conn, person_id)

    proveedor_env = os.environ.get("BODY_AGENTE_PROVEEDOR", agente_cliente.PROVEEDOR_DEFAULT)
    if proveedor_env == "demo":
        proveedor_nombre, modelo = "demo", "demo"
    else:
        configurados = agente_cliente.proveedores_configurados()
        resuelto = agente_cliente.nombre_elegido(configurados, conn, person_id)
        proveedor_nombre = resuelto or proveedor_env
        # `resuelto` is None when nothing is configured -- `generar()` then returns on the
        # no_configurado error before `modelo` is read in production. This reads the client's OWN
        # default constant rather than a second BODY_AGENTE_MODELO lookup, so no second default
        # exists (BODY-MODELOS-ESTANDAR-1 decision 4).
        modelo = configurados[resuelto].modelo if resuelto else agente_cliente.MODELO_DEFAULT
    proveedor = agente_cliente.proveedor_configurado(conn, person_id)

    async def generar():
        try:
            if proveedor is None:
                yield _evento_sse("error", {"codigo": "no_configurado", "mensaje": t("agente.error.no_configurado")})
                return
            try:
                agente_uso.verificar_tope(conn, person_id, proveedor_nombre, modelo)
            except agente_uso.TopeRefusal:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, agente_cliente.Uso(), 0,
                                      None, "tope_diario")
                yield _evento_sse("error", {"codigo": "tope_diario", "mensaje": t("agente.error.tope_diario")})
                return

            historial = agente_conversacion.historial(conn, conversacion_id, person_id)
            historial.append({"rol": "usuario", "bloques": [{"tipo": "texto", "texto": pregunta}]})
            sistema = t("agente.sistema")
            estados = agente_propuestas.estado_conversacion(conn, person_id, conversacion_id)
            if estados:
                lineas = "\n".join(f"- #{p['id']} {p['accion']}: {p['estado']}" for p in estados)
                sistema += "\n\n" + t("agente.estado_propuestas_header") + "\n" + lineas
            try:
                resultado = agente_bucle.ejecutar(conn, person_id, proveedor, sistema, historial, conversacion_id)
            except agente_bucle.LimiteRondas as e:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, e.uso_total, e.rondas,
                                      "limite_rondas", "limite_rondas")
                yield _evento_sse("error", {"codigo": "limite_rondas", "mensaje": t("agente.error.limite_rondas")})
                return
            except agente_bucle.FalloProveedor as e:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, e.uso_total, e.rondas,
                                      None, "proveedor")
                yield _evento_sse("error", {"codigo": "proveedor", "mensaje": t("agente.error.proveedor")})
                return

            defs = comun._defs(conn)
            for ev in resultado.eventos:
                if isinstance(ev, agente_bucle.EventoHerramienta):
                    yield _evento_sse("herramienta", {"nombre": ev.nombre})
                elif isinstance(ev, agente_bucle.EventoPropuesta):
                    yield _evento_sse("propuesta", _tarjeta_propuesta(t, defs, ev.registro))
                else:
                    yield _evento_sse("texto", {"delta": ev.texto})

            agente_conversacion.agregar_mensaje(conn, conversacion_id, person_id, "usuario",
                                                [{"tipo": "texto", "texto": pregunta}])
            for m in resultado.nuevos_mensajes:
                agente_conversacion.agregar_mensaje(conn, conversacion_id, person_id, m["rol"], m["bloques"])
            agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, resultado.uso_total, resultado.rondas,
                                  "texto", None)
            yield _evento_sse("fin", {"conversacion_id": conversacion_id, "rondas": resultado.rondas,
                                      "motivo_fin": "texto"})
        finally:
            conn.close()

    return StreamingResponse(generar(), media_type="text/event-stream")


@router.get("/asistente/conversacion/{conversacion_id}")
@comun._cierra_conexion
def asistente_conversacion(conversacion_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, t = comun._ctx(person_id)
    try:
        turnos = agente_conversacion.turnos_visibles(conn, conversacion_id, person_id)
    except agente_conversacion.ConversacionAjena:
        conn.close()
        raise HTTPException(404)
    defs = comun._defs(conn)
    tarjetas = [_tarjeta_propuesta(t, defs, r) for r in agente_propuestas.listar(conn, person_id, conversacion_id)]
    conn.close()
    return JSONResponse({"turnos": turnos, "propuestas": tarjetas})


def _propuesta_respuesta(conn, person_id: int, propuesta_id: int, accion) -> JSONResponse:
    try:
        resultado = accion(conn, person_id, propuesta_id)
    except agente_propuestas.PropuestaAjena:
        conn.close()
        raise HTTPException(404)
    conn.close()
    return JSONResponse(resultado)


@router.post("/asistente/propuestas/{propuesta_id}/aplicar")
@comun._cierra_conexion
def propuesta_aplicar(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.aplicar)


@router.post("/asistente/propuestas/{propuesta_id}/descartar")
@comun._cierra_conexion
def propuesta_descartar(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.descartar)


@router.post("/asistente/propuestas/{propuesta_id}/deshacer")
@comun._cierra_conexion
def propuesta_deshacer(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.deshacer)
