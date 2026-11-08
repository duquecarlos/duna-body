"""Ayuda (tutoriales-v1 section 5.1): chapters, their tasks, and the recorrido completo -- plus the
liveness probe. The first domain moved out of body/web.py (BODY-WEB-ESTRUCTURA-1), proving the
pattern every later domain's move repeats.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .. import cuenta, identity, tutoriales
from . import comun

router = APIRouter()


@router.get("/healthz")
def healthz():
    return Response("ok", media_type="text/plain")


def _tarea_ayuda_ctx(tarea: dict, hosted: bool) -> dict:
    objetivo = tutoriales.ir_a_pantalla(tarea)
    solo_en_linea = objetivo in tutoriales.SOLO_EN_LINEA and not hosted
    return {
        "id": tarea["id"], "titulo": tarea["titulo"], "video": tutoriales.video(tarea["id"]),
        "pantalla_link": None if solo_en_linea else objetivo, "solo_en_linea": solo_en_linea,
        "con_asistente": tarea["agente"] == "si",
    }


@router.get("/ayuda")
@comun._cierra_conexion
def ayuda(person_id: int = Depends(identity.current_person_id)):
    conn, _, t = comun._ctx(person_id)
    turnos_on = comun._turnos_on(conn, person_id)
    hosted = cuenta.hosted()
    capitulos = []
    recorrido = []
    for c in tutoriales.capitulos_con_tareas(incluir_turnos=turnos_on):
        tareas = [_tarea_ayuda_ctx(tarea, hosted) for tarea in c["tareas"]]
        capitulos.append({"id": c["id"], "titulo": c["titulo"], "tareas": tareas})
        recorrido.extend(tarea for tarea in tareas if tarea["video"])
    return comun._render("ayuda.html", t=t, tab="settings", capitulos=capitulos, recorrido=recorrido,
                         turnos=turnos_on)
