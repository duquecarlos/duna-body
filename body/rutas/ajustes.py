"""Ajustes screens (BODY-WEB-ESTRUCTURA-4): language, sharing, the shift-work toggle, and «Lo que
ve el asistente» -- the screen the consent text promises. Pure move out of body/web.py.
"""
from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import compartir, db, household, identity
from ..agente import ajustes as agente_ajustes
from ..agente import cliente as agente_cliente
from ..i18n import LANGS
from . import comun

router = APIRouter()


@router.get("/settings")
@comun._cierra_conexion
def settings(person_id: int = Depends(identity.current_person_id)):
    conn, _, t = comun._ctx(person_id)
    nivel_datos = agente_ajustes.nivel_datos(conn, person_id)
    trabaja_turnos = db.get_trabaja_turnos(conn, person_id)
    hh = household.household_of(conn, person_id)
    compartidas = {r["metric_key"] for r in compartir.mis_compartidas(conn, person_id)} if hh else set()
    metrics_compartir = [{"key": k, "label": t("metric." + k), "shared": k in compartidas}
                          for k in comun._defs(conn)] if hh else []
    return comun._render("settings.html", t=t, tab="settings", nivel_datos=nivel_datos,
                   trabaja_turnos=trabaja_turnos, turnos=comun._turnos_on(conn, person_id),
                   has_household=bool(hh), metrics_compartir=metrics_compartir)


@router.post("/settings/compartir")
@comun._cierra_conexion_async
async def post_compartir(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await comun._form(request)
    metric_key, accion = f.get("metric_key", ""), f.get("accion", "")
    conn, _, _ = comun._ctx(person_id)
    if accion == "compartir":
        try:
            compartir.compartir(conn, person_id, metric_key)
        except ValueError:
            pass
    elif accion == "retirar":
        compartir.retirar(conn, person_id, metric_key)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/language")
@comun._cierra_conexion_async
async def set_language(request: Request, person_id: int = Depends(identity.current_person_id)):
    lang = parse_qs((await request.body()).decode()).get("lang", [""])[0]
    if lang in LANGS:
        conn, _, _ = comun._ctx(person_id)
        db.set_language(conn, lang, person_id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/trabaja_turnos")
@comun._cierra_conexion_async
async def set_trabaja_turnos(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = parse_qs((await request.body()).decode())
    valor = f.get("valor", [""])[0]
    if valor in ("si", "no"):
        conn, _, _ = comun._ctx(person_id)
        db.set_trabaja_turnos(conn, valor == "si", person_id)
    volver = f.get("volver", ["/settings"])[0]
    if volver not in ("/", "/settings"):
        volver = "/settings"
    return RedirectResponse(volver, status_code=303)


# ---- «Lo que ve el asistente» (BODY-NIVEL-DATOS-UI-1): the screen the consent text promises -------

PROVEEDOR_LABEL_KEYS = {
    "anthropic": "ajustes.asistente.proveedor.anthropic",
    "openai": "ajustes.asistente.proveedor.openai",
    "gemini": "ajustes.asistente.proveedor.gemini",
}


@router.get("/ajustes/asistente")
@comun._cierra_conexion
def nivel_datos_view(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, t = comun._ctx(person_id)
    nivel = agente_ajustes.nivel_datos(conn, person_id)
    configurados = agente_cliente.proveedores_configurados()
    proveedores = None
    proveedor_actual = None
    if len(configurados) >= 2:
        proveedores = [{"nombre": n, "etiqueta": t(PROVEEDOR_LABEL_KEYS[n])} for n in configurados]
        proveedor_actual = agente_cliente.nombre_elegido(configurados, conn, person_id)
    guardado = request.query_params.get("guardado") == "1"
    resp = comun._render("asistente_datos.html", t=t, tab="settings", nivel=nivel, guardado=guardado,
                   proveedores=proveedores, proveedor_actual=proveedor_actual, turnos=comun._turnos_on(conn, person_id))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/ajustes/asistente")
@comun._cierra_conexion_async
async def nivel_datos_guardar(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await comun._form(request)
    nivel = f.get("nivel") or ""
    if nivel not in agente_ajustes.NIVELES:
        raise HTTPException(400)
    conn, _, _ = comun._ctx(person_id)
    agente_ajustes.set_nivel_datos(conn, person_id, nivel)
    proveedor = f.get("proveedor")
    if proveedor:
        if proveedor not in agente_cliente.proveedores_configurados():
            raise HTTPException(400)
        agente_ajustes.set_proveedor(conn, person_id, proveedor)
    return RedirectResponse("/ajustes/asistente?guardado=1", status_code=303)
