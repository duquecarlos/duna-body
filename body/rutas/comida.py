"""BODY-COMIDA-PRODUCTO-1: the phone-first picker at /comida/productos -- "¿Qué comiste?" (ruling
4). Plain HTML GET/POST, no JS required: the meal draft travels as repeated `linea` fields (each
encoded "producto_id:cantidad:unidad"), round-tripped through hidden inputs on every form so it
survives a search or an add without a session of its own. Saving calls the SAME writer the
assistant's own registrar_comida tool ultimately reaches (db.crear_comida, through
body/productos.py's calcular_lineas) -- never a second one -- but writes immediately: unlike the
assistant's propuesta flow, there is no chat confirmation step for a form the person just submitted
themselves.
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from .. import db, identity
from ..i18n import T
from .. import productos as productos_mod
from . import comun

router = APIRouter()

_UNIDADES = ("g", "ml", "unidad")
_UNIDAD_LABEL_KEYS = {"g": "comida.productos.unidad.g", "ml": "comida.productos.unidad.ml",
                       "unidad": "comida.productos.unidad.unidad"}


def _unidad_label(t: T, unidad: str) -> str:
    return t(_UNIDAD_LABEL_KEYS.get(unidad, "comida.productos.unidad.g"))


def _codificar_linea(l: dict) -> str:
    return f"{l['producto_id']}:{l['cantidad']}:{l['unidad']}"


def _decodificar_lineas(valores: list[str]) -> list[dict]:
    out = []
    for v in valores:
        partes = v.split(":")
        if len(partes) != 3:
            continue
        try:
            producto_id = int(partes[0])
            cantidad = float(partes[1])
        except ValueError:
            continue
        unidad = partes[2]
        if unidad not in _UNIDADES or cantidad <= 0:
            continue
        out.append({"producto_id": producto_id, "cantidad": cantidad, "unidad": unidad})
    return out


def _resolver_draft(conn, person_id: int, t: T, lineas: list[dict]) -> list[dict]:
    visibles = {p["id"]: p for p in productos_mod.productos_visibles(conn, person_id)}
    out = []
    for l in lineas:
        p = visibles.get(l["producto_id"])
        if p is None:
            continue
        out.append({"producto_id": l["producto_id"], "nombre": p["nombre_mostrado"],
                     "cantidad": l["cantidad"], "unidad": l["unidad"],
                     "unidad_label": _unidad_label(t, l["unidad"]), "codificada": _codificar_linea(l)})
    return out


def _productos_ctx(conn, person_id: int, q: str | None) -> list[dict]:
    resultados = productos_mod.buscar(conn, person_id, q) if q else productos_mod.productos_visibles(conn, person_id)
    return [{
        "id": p["id"], "nombre": p["nombre_mostrado"], "marca": p["marca"],
        "unidad": productos_mod.basis_de_producto(conn, p["id"]) or "g",
    } for p in resultados]


def _unidad_opciones(t: T) -> list[dict]:
    return [{"value": u, "label": _unidad_label(t, u)} for u in _UNIDADES]


def _render_picker(conn, person_id: int, person: dict, t: T, q: str | None, lineas: list[dict], *,
                    error: str | None = None):
    draft = _resolver_draft(conn, person_id, t, lineas)
    nutriente_campos = [{"key": k, "label": t("metric." + k)} for k in db.INGESTA_METRIC_KEYS]
    return comun._render(
        "comida_productos.html", t=t, tab="today", q=q or "", productos=_productos_ctx(conn, person_id, q),
        draft=draft, unidad_opciones=_unidad_opciones(t), nutriente_campos=nutriente_campos, error=error,
        turnos=db.trabaja_turnos_si(conn, person_id),
    )


async def _form_multi(request: Request) -> dict[str, list[str]]:
    """Same raw parse `comun._form` builds its dict from, kept apart from it: `comun._form`
    collapses to one value per key (`{k: v[0] ...}`), which would silently drop every product
    line but the first one this page's own repeated `linea` fields carry."""
    return parse_qs((await request.body()).decode())


def _uno(f: dict[str, list[str]], clave: str) -> str:
    return (f.get(clave, [""])[0] or "").strip()


@router.get("/comida/productos")
@comun._cierra_conexion
def comida_productos_view(q: str | None = None, linea: list[str] = Query(default=[]),
                           person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    lineas = _decodificar_lineas(linea)
    resp = comun._render(
        "comida_productos.html", t=t, tab="today", q=q or "", productos=_productos_ctx(conn, person_id, q),
        draft=_resolver_draft(conn, person_id, t, lineas), unidad_opciones=_unidad_opciones(t),
        nutriente_campos=[{"key": k, "label": t("metric." + k)} for k in db.INGESTA_METRIC_KEYS], error=None,
        turnos=db.trabaja_turnos_si(conn, person_id),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/comida/productos/agregar")
@comun._cierra_conexion
def comida_productos_agregar(producto_id: int | None = None, cantidad: float | None = None,
                              unidad: str | None = None, q: str | None = None,
                              linea: list[str] = Query(default=[]),
                              person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    visibles = {p["id"] for p in productos_mod.productos_visibles(conn, person_id)}
    lineas = list(linea)
    if (producto_id is not None and producto_id in visibles and unidad in _UNIDADES
            and cantidad is not None and cantidad > 0):
        lineas = lineas + [_codificar_linea({"producto_id": producto_id, "cantidad": cantidad, "unidad": unidad})]
    pares = [("linea", x) for x in lineas]
    if q:
        pares.append(("q", q))
    return RedirectResponse(f"/comida/productos?{urlencode(pares)}", status_code=303)


@router.post("/comida/productos/guardar")
@comun._cierra_conexion_async
async def comida_productos_guardar(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await _form_multi(request)
    conn, person, t = comun._ctx(person_id)
    q = _uno(f, "q") or None
    descripcion = _uno(f, "descripcion") or None
    lineas = _decodificar_lineas(f.get("linea", []))
    if not lineas:
        return _render_picker(conn, person_id, person, t, q, lineas, error=t("comida.productos.error_vacio"))
    try:
        cantidades, detalle = productos_mod.calcular_lineas(conn, person_id, lineas)
    except productos_mod.ProductoDesconocido as e:
        error = t("agente.error.producto_desconocido", producto_id=e.producto_id)
        return _render_picker(conn, person_id, person, t, q, lineas, error=error)
    except productos_mod.UnidadNoCoincide as e:
        error = t("agente.error.producto_unidad_no_coincide", producto=e.nombre, esperada=e.esperada,
                   recibida=e.recibida)
        return _render_picker(conn, person_id, person, t, q, lineas, error=error)
    if not cantidades:
        return _render_picker(conn, person_id, person, t, q, lineas, error=t("comida.productos.error_vacio"))
    day = comun._today_local(person).isoformat()
    ahora = datetime.now(ZoneInfo(person.get("timezone") or "America/Bogota"))
    ts_local = f"{day}T{ahora.strftime('%H:%M:%S')}"
    db.crear_comida(conn, person_id, day, cantidades, descripcion, ts_local, productos=detalle)
    return RedirectResponse("/", status_code=303)


@router.post("/comida/productos/nuevo")
@comun._cierra_conexion_async
async def comida_productos_nuevo(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await _form_multi(request)
    conn, person, t = comun._ctx(person_id)
    q = _uno(f, "q") or None
    lineas_actuales = f.get("linea", [])
    nombre = _uno(f, "nombre")
    if not nombre:
        lineas = _decodificar_lineas(lineas_actuales)
        return _render_picker(conn, person_id, person, t, q, lineas, error=t("comida.productos.error_vacio"))
    marca = _uno(f, "marca") or None
    tamano_neto_s = _uno(f, "tamano_neto")
    tamano_neto = None
    if tamano_neto_s:
        try:
            tamano_neto = float(tamano_neto_s)
        except ValueError:
            tamano_neto = None
    tamano_unidad = _uno(f, "tamano_unidad") or None
    valores = {}
    for key in db.INGESTA_METRIC_KEYS:
        valor_s = _uno(f, f"valor_{key}")
        if not valor_s:
            continue
        try:
            valor = float(valor_s)
        except ValueError:
            continue
        if valor > 0:
            valores[key] = valor
    if valores and tamano_unidad is None:
        tamano_unidad = "g"
    producto_id = productos_mod.crear_producto_manual(conn, person_id, nombre, marca=marca,
                                                        tamano_neto=tamano_neto, tamano_unidad=tamano_unidad)
    if valores:
        basis = {"g": "100g", "ml": "100ml", "unidad": "unidad"}[tamano_unidad]
        for key, valor in valores.items():
            productos_mod.establecer_nutricion(conn, producto_id, key, valor, basis, "etiqueta")
    pares = [("linea", x) for x in lineas_actuales]
    if q:
        pares.append(("q", q))
    return RedirectResponse(f"/comida/productos?{urlencode(pares)}", status_code=303)
