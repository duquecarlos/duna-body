"""Ajustes screens (BODY-WEB-ESTRUCTURA-4): language, sharing, the shift-work toggle, and «Lo que
ve el asistente» -- the screen the consent text promises. Pure move out of body/web.py.
"""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import compartir, cuenta, db, household, identity, nutriente_objetivo
from ..agente import ajustes as agente_ajustes
from ..agente import cliente as agente_cliente
from ..agente import conversacion as agente_conversacion
from ..agente import herramientas as agente_herramientas
from ..agente import propuestas as agente_propuestas
from ..i18n import LANGS
from . import comun

router = APIRouter()


def _nutriente_objetivo_txt(t, defs: dict, metric_key: str, kind: str, amount: float | None,
                             sentido: str) -> str:
    return nutriente_objetivo.texto_objetivo(t, kind, amount, sentido, comun._unit(t, defs[metric_key]))


def _nutriente_vigentes_ctx(t, conn, defs: dict, person_id: int, today_local: date) -> list[dict]:
    """Ajustes' own "Metas diarias" listing (BODY-NUTRIENTE-OBJETIVO-1 ruling 6): one row per
    intake key with a live target as of today, in the catalog's own order. BODY-NUTRIENTE-
    OBJETIVO-1-M fix 2: each row names the start day the target has been in force since."""
    vigentes = nutriente_objetivo.objetivos_vigentes(conn, person_id, today_local.isoformat())
    out = []
    for k, v in vigentes.items():
        texto = _nutriente_objetivo_txt(t, defs, k, v["kind"], v["amount"], v["sentido"])
        desde = t("settings.nutriente.desde", fecha=comun._short_date(t, date.fromisoformat(v["start_day"])))
        out.append({"key": k, "label": t("metric." + k), "texto": f"{texto} · {desde}"})
    return out


def _nutriente_pendientes_ctx(t, conn, defs: dict, person_id: int) -> list[dict]:
    """Every still-pending `proponer_objetivo_nutriente` proposal, plus the most recently applied
    one per metric (BODY-NUTRIENTE-OBJETIVO-1-M fix 4) -- the SAME list shape body/rutas/hoy.py's
    own `_propuestas_vinculo_dia_ctx` uses for separar_sesion/unir_sesiones: `aplicada` decides
    whether a row's form below posts to Aplicar/Descartar or to Deshacer, and Deshacer calls the
    SAME `agente_propuestas.deshacer` the assistant uses -- no second undo path. Once a metric's
    proposal is undone its state moves to 'deshecha' and it drops off this query on its own.
    BODY-NUTRIENTE-OBJETIVO-1 ruling 3 (fix 3): each row also names the start day."""
    filas = conn.execute(
        "SELECT id, despues_json, estado FROM propuesta_agente WHERE person_id = ? "
        "AND accion = 'proponer_objetivo_nutriente' AND estado IN ('propuesta', 'aplicada') "
        "ORDER BY creada_en DESC",
        (person_id,),
    ).fetchall()
    out = []
    vistos_aplicados: set[str] = set()
    for f in filas:
        despues = json.loads(f["despues_json"])
        metric_key = despues["metric_key"]
        aplicada = f["estado"] == "aplicada"
        if aplicada:
            if metric_key in vistos_aplicados:
                continue
            vistos_aplicados.add(metric_key)
        texto = _nutriente_objetivo_txt(t, defs, metric_key, despues["kind"], despues["amount"],
                                         despues["sentido"])
        desde = t("settings.nutriente.propuesta_desde",
                  fecha=comun._short_date(t, date.fromisoformat(despues["start_day"])))
        out.append({"id": f["id"], "metric_label": t("metric." + metric_key), "texto": f"{texto} · {desde}",
                     "aplicada": aplicada})
    return out


@router.get("/settings")
@comun._cierra_conexion
def settings(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    nivel_datos = agente_ajustes.nivel_datos(conn, person_id)
    trabaja_turnos = db.get_trabaja_turnos(conn, person_id)
    hh = household.household_of(conn, person_id)
    compartidas = {r["metric_key"] for r in compartir.mis_compartidas(conn, person_id)} if hh else set()
    metrics_compartir = [{"key": k, "label": t("metric." + k), "shared": k in compartidas}
                          for k in comun._defs(conn)] if hh else []
    defs = comun._defs(conn)
    return comun._render("settings.html", t=t, tab="settings", nivel_datos=nivel_datos,
                   trabaja_turnos=trabaja_turnos, turnos=db.trabaja_turnos_si(conn, person_id),
                   has_household=bool(hh), metrics_compartir=metrics_compartir,
                   aplicar_mcp=db.get_aplicar_mcp(conn, person_id),
                   nutriente_metric_keys=[{"key": k, "label": t("metric." + k)}
                                           for k in db.INGESTA_METRIC_KEYS],
                   nutriente_vigentes=_nutriente_vigentes_ctx(t, conn, defs, person_id,
                                                               comun._today_local(person)),
                   nutriente_pendientes=_nutriente_pendientes_ctx(t, conn, defs, person_id))


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


@router.post("/settings/aplicar_mcp")
@comun._cierra_conexion_async
async def set_aplicar_mcp(request: Request, person_id: int = Depends(identity.current_person_id)):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the ONLY way this setting ever changes -- a POST
    from the person's own session, behind the same identity.current_person_id every other Ajustes
    route uses. Neither body/mcp.py nor the in-app assistant's tool registry imports
    db.set_aplicar_mcp."""
    f = parse_qs((await request.body()).decode())
    valor = f.get("valor", [""])[0]
    if valor in ("si", "no"):
        conn, _, _ = comun._ctx(person_id)
        db.set_aplicar_mcp(conn, person_id, valor == "si")
    return RedirectResponse("/settings", status_code=303)


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
                   proveedores=proveedores, proveedor_actual=proveedor_actual,
                   turnos=db.trabaja_turnos_si(conn, person_id))
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


# ---- «Metas diarias» (BODY-NUTRIENTE-OBJETIVO-1 ruling 6): raises the SAME propose/apply/undo ----
# flow the chat and MCP tool use, never a direct write (this form is not special-cased).

@router.post("/settings/nutriente/proponer")
@comun._cierra_conexion_async
async def nutriente_proponer(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await comun._form(request)
    conn, _, _ = comun._ctx(person_id)
    conversacion_id = agente_conversacion.crear(conn, person_id)
    try:
        agente_herramientas._proponer_objetivo_nutriente(
            conn, person_id, conversacion_id, f.get("metric_key", ""), f.get("kind", ""),
            f.get("sentido", ""), amount=f.get("amount") or None,
        )
    except agente_herramientas.ToolError:
        pass
    back = cuenta.safe_return_to(f.get("back") or "/settings")
    return RedirectResponse(back, status_code=303)


@comun._cierra_conexion_async
async def _nutriente_propuesta_resolver(request: Request, propuesta_id: int, person_id: int, accion) -> RedirectResponse:
    conn, _, _ = comun._ctx(person_id)
    try:
        accion(conn, person_id, propuesta_id)
    except agente_propuestas.PropuestaAjena:
        pass
    f = await comun._form(request)
    back = cuenta.safe_return_to(f.get("back") or "/settings")
    return RedirectResponse(back, status_code=303)


@router.post("/settings/nutriente/propuestas/{propuesta_id}/aplicar")
async def nutriente_propuesta_aplicar(propuesta_id: int, request: Request,
                                       person_id: int = Depends(identity.current_person_id)):
    return await _nutriente_propuesta_resolver(request, propuesta_id, person_id, agente_propuestas.aplicar)


@router.post("/settings/nutriente/propuestas/{propuesta_id}/descartar")
async def nutriente_propuesta_descartar(propuesta_id: int, request: Request,
                                         person_id: int = Depends(identity.current_person_id)):
    return await _nutriente_propuesta_resolver(request, propuesta_id, person_id, agente_propuestas.descartar)


@router.post("/settings/nutriente/propuestas/{propuesta_id}/deshacer")
async def nutriente_propuesta_deshacer(propuesta_id: int, request: Request,
                                        person_id: int = Depends(identity.current_person_id)):
    """BODY-NUTRIENTE-OBJETIVO-1-M fix 4: the SAME `agente_propuestas.deshacer` body/rutas/hoy.py's
    own `entreno_propuesta_deshacer` calls -- no second undo path."""
    return await _nutriente_propuesta_resolver(request, propuesta_id, person_id, agente_propuestas.deshacer)
