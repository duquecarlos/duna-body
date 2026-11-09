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
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import db, identity
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


def _serie_txt(t: T, s: dict | None) -> str | None:
    """One set's short display text for the watch-vs-you table: "<ejercicio> · <reps/weight>",
    or None for an empty cell (no watch set at that order)."""
    if s is None:
        return None
    ejercicio = (s.get("ejercicio") or "").strip()
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


def _tarjeta_propuesta(t: T, defs: dict, registro: dict) -> dict:
    """The `propuesta` SSE event payload (agent-capabilities-v1 section 2, decision 5): `antes`/
    `despues` are short plain-word strings in the person's language, built here from the record's
    structured JSON -- never stored as text. `etiquetas` carries this card's button and result
    labels pre-rendered, since body/templates/base.html (outside this slice's touches) is the only
    other place assistant.js could read i18n text from."""
    accion = registro["accion"]
    despues = registro["despues_json"]
    antes_json = registro["antes_json"]
    antes_txt: str | None = None
    if accion == "proponer_meta":
        despues_txt = t("asistente.propuesta.meta_nueva", metric=t("metric." + despues["metric_key"]),
                        direccion=t("asistente.propuesta.direccion." + despues["direction"]),
                        valor=comun._fig_unit(t, despues["metric_key"], despues["target_value"], defs),
                        fecha=comun._short_date(t, date.fromisoformat(despues["deadline"])))
    elif accion == "proponer_cambio_meta":
        metric_key = despues.get("metric_key")
        antes_txt = _objetivo_txt(t, defs, metric_key, antes_json["target_value"], antes_json["deadline"])
        despues_txt = _objetivo_txt(t, defs, metric_key, despues["target_value"], despues["deadline"])
    elif accion == "proponer_confirmar_turnos":
        resumen = antes_json["resumen"]
        antes_txt = t("asistente.propuesta.turnos_actual", n=t.num(resumen["batch_count"]))
        despues_txt = t("asistente.propuesta.turnos_nuevo")
    elif accion == "proponer_idioma":
        antes_txt = IDIOMA_LABEL.get(antes_json["lang"], antes_json["lang"])
        despues_txt = IDIOMA_LABEL.get(despues["lang"], despues["lang"])
    elif accion == "planear_entreno":
        ejercicios_txt = ", ".join(_ejercicio_plan_txt(t, e) for e in despues["ejercicios"])
        despues_txt = t("asistente.propuesta.entreno_plan", titulo=despues["titulo"],
                        fecha=comun._short_date(t, date.fromisoformat(despues["day"])), ejercicios=ejercicios_txt)
    elif accion == "registrar_entreno":
        despues_txt = t("asistente.propuesta.entreno_registrado",
                        fecha=comun._short_date(t, date.fromisoformat(despues["day"])))
    elif accion == "registrar_medida":
        metric_key = despues["metric_key"]
        fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
        if antes_json and antes_json.get("value") is not None:
            antes_txt = t("asistente.propuesta.medida_valor", metric=t("metric." + metric_key),
                          valor=comun._fig_unit(t, metric_key, antes_json["value"], defs), fecha=fecha)
        despues_txt = t("asistente.propuesta.medida_valor", metric=t("metric." + metric_key),
                        valor=comun._fig_unit(t, metric_key, despues["value"], defs), fecha=fecha)
    else:
        despues_txt = ""
    series_tarjeta = None
    if accion == "registrar_entreno":
        if despues["manual"]:
            series_tarjeta = [{"orden": i, "reloj": None, "tu": _serie_txt(t, s)}
                               for i, s in enumerate(despues["series"], start=1)]
        else:
            series_tarjeta = [{"orden": m["set_order"], "reloj": _serie_txt(t, m["reloj"]),
                                "tu": _serie_txt(t, m["tu"])} for m in despues["mapeo"]]
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
