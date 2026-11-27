"""BODY-COMIDA-PRODUCTO-1: the phone-first picker at /comida/productos -- "¿Qué comiste?" (ruling
4). Plain HTML GET/POST, no JS required: the meal draft travels as repeated `linea` fields (each
encoded "producto_id:cantidad:unidad"), round-tripped through hidden inputs on every form so it
survives a search or an add without a session of its own. Saving calls the SAME writer the
assistant's own registrar_comida tool ultimately reaches (db.crear_comida, through
body/productos.py's calcular_lineas) -- never a second one -- but writes immediately: unlike the
assistant's propuesta flow, there is no chat confirmation step for a form the person just submitted
themselves.

BODY-ETIQUETA-FOTO-1 (ORCH-CARLOS-DELEGATES-TIER-1-1, Carlos's own yes to proposal 25): the
«New product» card also offers "Leer etiqueta" -- a photo of a food label, re-encoded on the
device (body/static/etiqueta_foto.js) before it ever reaches this module. The upload route below
follows body/rutas/dispositivos.py's own streamed-byte-ceiling pattern, stores no byte of the
photo anywhere, and asks the configured provider (body/agente/cliente.py, Tier 1) for the SAME
crear_producto tool call the chat assistant already proposes with -- through
`herramientas._crear_producto`, never a second validation path. Confirming, discarding or undoing
that proposal reuses the existing propuesta machinery (body/agente/propuestas.py's
aplicar/descartar/deshacer) exactly as the chat panel's own Aplicar/Descartar/Deshacer buttons do;
this module only edits a still-pending proposal's own despues_json first
(`propuestas.editar_antes_de_aplicar`), so what gets applied is what the person confirmed on the
editable card, never silently the model's own first guess.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import db, identity
from ..agente import cliente as agente_cliente
from ..agente import conversacion as agente_conversacion
from ..agente import herramientas as agente_herramientas
from ..agente import propuestas as agente_propuestas
from ..i18n import T
from .. import productos as productos_mod
from . import comun

router = APIRouter()

# BODY-ETIQUETA-FOTO-1 ruling 2/3: the same conservative byte ceiling body/static/etiqueta_foto.js
# enforces client-side (MAX_BYTES there) -- named once here as the server's own floor, never
# guessed per vendor (ruling 4).
MAX_ETIQUETA_BYTES = 1_000_000

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


def _asistente_disponible(conn, person_id: int) -> bool:
    return agente_cliente.proveedor_configurado(conn, person_id) is not None


def _etiqueta_pendiente_ctx(conn, person_id: int, propuesta_id: int) -> dict | None:
    """BODY-ETIQUETA-FOTO-1 ruling 6: the still-pending crear_producto proposal this label-read
    photo made, read directly off `propuesta_agente` the same way body/rutas/hoy.py's own
    `_despertar_propuesta_pendiente` reads a pending proposal for its own inline card -- never
    through body/agente/propuestas.py's private row helpers, which this module does not need
    beyond `aplicar`/`descartar`/`deshacer`/`editar_antes_de_aplicar`."""
    row = conn.execute(
        "SELECT despues_json FROM propuesta_agente WHERE id = ? AND person_id = ? AND accion = 'crear_producto' "
        "AND estado = 'propuesta'", (propuesta_id, person_id),
    ).fetchone()
    if row is None:
        return None
    despues = json.loads(row["despues_json"])
    nutrientes = {k: _sin_decimal_entero(v) for k, v in (despues.get("nutrientes") or {}).items()}
    return {"id": propuesta_id, "nombre": despues.get("nombre") or "", "marca": despues.get("marca") or "",
            "tamano_neto": _sin_decimal_entero(despues.get("tamano_neto")),
            "tamano_unidad": despues.get("tamano_unidad") or "", "nutrientes": nutrientes}


def _sin_decimal_entero(v):
    """An edit field's own value, shown as "120" rather than "120.0" -- `_numero_cantidad_comida`
    always returns a float, but a whole one reads as a typo waiting to confuse the person editing
    this card before they ever save it."""
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _render_picker(conn, person_id: int, person: dict, t: T, q: str | None, lineas: list[dict], *,
                    error: str | None = None, etiqueta_pendiente: dict | None = None,
                    etiqueta_aplicada_id: int | None = None):
    draft = _resolver_draft(conn, person_id, t, lineas)
    nutriente_campos = [{"key": k, "label": t("metric." + k)} for k in db.INGESTA_METRIC_KEYS]
    return comun._render(
        "comida_productos.html", t=t, tab="today", q=q or "", productos=_productos_ctx(conn, person_id, q),
        draft=draft, unidad_opciones=_unidad_opciones(t), nutriente_campos=nutriente_campos, error=error,
        turnos=db.trabaja_turnos_si(conn, person_id),
        etiqueta_pendiente=etiqueta_pendiente, etiqueta_aplicada_id=etiqueta_aplicada_id,
        asistente_disponible=_asistente_disponible(conn, person_id),
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
                           propuesta_id: int | None = None, aplicado_id: int | None = None,
                           person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    lineas = _decodificar_lineas(linea)
    etiqueta_pendiente = _etiqueta_pendiente_ctx(conn, person_id, propuesta_id) if propuesta_id else None
    resp = comun._render(
        "comida_productos.html", t=t, tab="today", q=q or "", productos=_productos_ctx(conn, person_id, q),
        draft=_resolver_draft(conn, person_id, t, lineas), unidad_opciones=_unidad_opciones(t),
        nutriente_campos=[{"key": k, "label": t("metric." + k)} for k in db.INGESTA_METRIC_KEYS], error=None,
        turnos=db.trabaja_turnos_si(conn, person_id),
        etiqueta_pendiente=etiqueta_pendiente, etiqueta_aplicada_id=aplicado_id or None,
        asistente_disponible=_asistente_disponible(conn, person_id),
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


def _leer_campos_producto(f: dict[str, list[str]]) -> tuple[str, str | None, float | None, str | None, dict]:
    """The manual-entry form's own fields (nombre/marca/tamano_neto/tamano_unidad/valor_<key>) --
    shared by `comida_productos_nuevo` and, BODY-ETIQUETA-FOTO-1, the label-read confirm route
    below, since the confirm card is the SAME form, pre-filled rather than blank."""
    nombre = _uno(f, "nombre")
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
    return nombre, marca, tamano_neto, tamano_unidad, valores


@router.post("/comida/productos/nuevo")
@comun._cierra_conexion_async
async def comida_productos_nuevo(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await _form_multi(request)
    conn, person, t = comun._ctx(person_id)
    q = _uno(f, "q") or None
    lineas_actuales = f.get("linea", [])
    nombre, marca, tamano_neto, tamano_unidad, valores = _leer_campos_producto(f)
    if not nombre:
        lineas = _decodificar_lineas(lineas_actuales)
        return _render_picker(conn, person_id, person, t, q, lineas, error=t("comida.productos.error_vacio"))
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


# ---- BODY-ETIQUETA-FOTO-1: a photo of a food label fills "New product" -- Carlos's own yes to
# proposal 25 (ORCH-CARLOS-DELEGATES-TIER-1-1). Upload, then confirm/discard/undo through the
# existing crear_producto proposal machinery -- never a second write path. -------------------

class EtiquetaNoLegible(Exception):
    """The provider could not be reached, or never named crear_producto with a usable 'nombre' --
    ruling 7's one honest error, never a guess at what the label said."""


# Not localized: sent to the model, never shown to the person (ruling 7's own error text is what
# the person sees, built in the route below).
_SISTEMA_ETIQUETA = (
    "Lee la etiqueta nutricional de la foto adjunta y llama a la herramienta crear_producto con "
    "los campos que puedas leer con certeza. Deja vacio (nulo) cualquier campo que no puedas leer "
    "-- nunca inventes ni estimes un valor. Responde unicamente con esa llamada a la herramienta."
)


def _herramienta_crear_producto() -> dict:
    """Ruling 4: the SAME tool spec body/agente/herramientas.py already declares for the chat
    assistant's own crear_producto, read off its one public table rather than restated here."""
    espec = next(h for h in agente_herramientas.HERRAMIENTAS if h["nombre"] == "crear_producto")
    return {"nombre": espec["nombre"], "descripcion": espec["descripcion"], "parametros": espec["parametros"]}


def _leer_etiqueta(proveedor: agente_cliente.Proveedor, imagen_b64: str) -> dict:
    mensajes = [{"rol": "usuario", "bloques": [
        {"tipo": "imagen", "media_type": "image/jpeg", "datos_b64": imagen_b64}]}]
    try:
        resp = proveedor.responder(_SISTEMA_ETIQUETA, mensajes, [_herramienta_crear_producto()])
    except agente_cliente.ProveedorError:
        raise EtiquetaNoLegible()
    llamada = next((b for b in resp.bloques
                     if isinstance(b, agente_cliente.LlamadaHerramienta) and b.nombre == "crear_producto"), None)
    if llamada is None:
        raise EtiquetaNoLegible()
    return llamada.parametros


class _EtiquetaRechazada(Exception):
    def __init__(self, motivo: str):
        self.motivo = motivo


async def _leer_cuerpo_etiqueta(request: Request) -> bytes:
    """Ruling 3: the same streamed-byte-ceiling shape body/rutas/dispositivos.py's own device
    routes already use -- read in chunks, abort past MAX_ETIQUETA_BYTES, never UploadFile or
    multipart (python-multipart is not installed)."""
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_ETIQUETA_BYTES:
        raise _EtiquetaRechazada("muy_grande")
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_ETIQUETA_BYTES:
            raise _EtiquetaRechazada("muy_grande")
    return bytes(chunks)


@router.post("/comida/productos/etiqueta")
@comun._cierra_conexion_async
async def comida_productos_etiqueta(request: Request, person_id: int = Depends(identity.current_person_id)):
    """Ruling 3: stores nothing -- the photo's bytes live only in this one request's own memory,
    base64-encoded once to reach the provider adapter and discarded when this function returns.
    Never written to disk, the database, a log or a URL."""
    if request.headers.get("content-type") != "image/jpeg":
        return JSONResponse({"ok": False, "error": "no_imagen"}, status_code=415,
                             headers={"Cache-Control": "no-store"})
    try:
        cuerpo = await _leer_cuerpo_etiqueta(request)
    except _EtiquetaRechazada as e:
        return JSONResponse({"ok": False, "error": e.motivo}, status_code=413,
                             headers={"Cache-Control": "no-store"})
    conn, _, _ = comun._ctx(person_id)
    proveedor = agente_cliente.proveedor_configurado(conn, person_id)
    if proveedor is None:
        return JSONResponse({"ok": False, "error": "sin_asistente"}, status_code=409,
                             headers={"Cache-Control": "no-store"})
    imagen_b64 = base64.b64encode(cuerpo).decode("ascii")
    try:
        campos = _leer_etiqueta(proveedor, imagen_b64)
        nombre = (campos.get("nombre") or "").strip() if isinstance(campos, dict) else ""
        if not nombre:
            raise EtiquetaNoLegible()
        conversacion_id = agente_conversacion.crear(conn, person_id)
        resultado = agente_herramientas._crear_producto(
            conn, person_id, conversacion_id, nombre, marca=campos.get("marca"),
            tamano_neto=campos.get("tamano_neto"), tamano_unidad=campos.get("tamano_unidad"),
            nutrientes=campos.get("nutrientes"))
    except (EtiquetaNoLegible, agente_herramientas.ToolError):
        return JSONResponse({"ok": False, "error": "no_legible"}, status_code=422,
                             headers={"Cache-Control": "no-store"})
    despues = resultado["_registro"]["despues_json"]
    return JSONResponse({"ok": True, "propuesta_id": resultado["propuesta_id"], "campos": despues},
                         headers={"Cache-Control": "no-store"})


def _volver_a_productos(f: dict[str, list[str]], **extra: str) -> RedirectResponse:
    pares = [("linea", x) for x in f.get("linea", [])]
    q = _uno(f, "q")
    if q:
        pares.append(("q", q))
    for k, v in extra.items():
        pares.append((k, v))
    return RedirectResponse(f"/comida/productos?{urlencode(pares)}" if pares else "/comida/productos",
                             status_code=303)


@router.post("/comida/productos/etiqueta/{propuesta_id}/confirmar")
@comun._cierra_conexion_async
async def comida_productos_etiqueta_confirmar(propuesta_id: int, request: Request,
                                                person_id: int = Depends(identity.current_person_id)):
    """Ruling 6: the person's own edits on the confirm card -- never the model's first guess
    silently applied -- overwrite the still-pending proposal's despues_json, then the SAME
    `agente_propuestas.aplicar` the chat panel's own Aplicar button calls writes the product."""
    f = await _form_multi(request)
    conn, person, t = comun._ctx(person_id)
    q = _uno(f, "q") or None
    lineas = _decodificar_lineas(f.get("linea", []))
    nombre, marca, tamano_neto, tamano_unidad, valores = _leer_campos_producto(f)
    if not nombre:
        return _render_picker(conn, person_id, person, t, q, lineas, error=t("comida.productos.error_vacio"),
                               etiqueta_pendiente=_etiqueta_pendiente_ctx(conn, person_id, propuesta_id))
    despues = {"nombre": nombre, "marca": marca, "tamano_neto": tamano_neto, "tamano_unidad": tamano_unidad,
               "nutrientes": valores}
    try:
        agente_propuestas.editar_antes_de_aplicar(conn, person_id, propuesta_id, despues)
    except agente_propuestas.PropuestaAjena:
        return _render_picker(conn, person_id, person, t, q, lineas,
                               error=t("comida.productos.etiqueta_error_no_legible"))
    resultado = agente_propuestas.aplicar(conn, person_id, propuesta_id)
    if resultado["estado"] != "aplicada":
        return _render_picker(conn, person_id, person, t, q, lineas,
                               error=t("comida.productos.etiqueta_error_no_legible"))
    return _volver_a_productos(f, aplicado_id=str(propuesta_id))


@router.post("/comida/productos/etiqueta/{propuesta_id}/descartar")
@comun._cierra_conexion_async
async def comida_productos_etiqueta_descartar(propuesta_id: int, request: Request,
                                                person_id: int = Depends(identity.current_person_id)):
    f = await _form_multi(request)
    conn, _, _ = comun._ctx(person_id)
    try:
        agente_propuestas.descartar(conn, person_id, propuesta_id)
    except agente_propuestas.PropuestaAjena:
        pass
    return _volver_a_productos(f)


@router.post("/comida/productos/etiqueta/{propuesta_id}/deshacer")
@comun._cierra_conexion_async
async def comida_productos_etiqueta_deshacer(propuesta_id: int, request: Request,
                                               person_id: int = Depends(identity.current_person_id)):
    f = await _form_multi(request)
    conn, _, _ = comun._ctx(person_id)
    try:
        agente_propuestas.deshacer(conn, person_id, propuesta_id)
    except agente_propuestas.PropuestaAjena:
        pass
    return _volver_a_productos(f)
