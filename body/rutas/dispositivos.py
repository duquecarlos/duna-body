"""Device keys and the device-key API (BODY-DISPOSITIVO-1), moved out of body/web.py
(BODY-WEB-ESTRUCTURA-2) as one module: the pairing screen on Ajustes and the API it authorises
are one domain. Pure move -- same paths, same bodies.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import cuenta, db, desplazamiento_import, dispositivo, errores, hogar_feed, identity, mcp, \
    shift_import, vida_export
from ..i18n import T
from . import comun, mcp_remoto

router = APIRouter()

# ---- Device keys (hosted only): «Conexión del PC» on Ajustes, BODY-DISPOSITIVO-1 -----------------

_ALCANCE_LABELS = {"turnos:subir": "dispositivos.scope.turnos", "vida:leer": "dispositivos.scope.vida",
                    "desplazamiento:subir": "dispositivos.scope.desplazamiento",
                    "asistente:mcp": "dispositivos.scope.asistente_mcp",
                    "hogar:leer": "dispositivos.scope.hogar",
                    "hogar:escribir": "dispositivos.scope.hogar_escribir"}


def _dispositivo_row(t: T, d: dict) -> dict:
    return {
        "id": d["id"], "nombre": d["nombre"],
        "alcances": ", ".join(t(_ALCANCE_LABELS[a]) for a in d["alcances"].split() if a in _ALCANCE_LABELS),
        "creado": comun._short_date(t, datetime.fromisoformat(d["creado_en"]).date()),
        "usado": comun._short_date(t, date.fromisoformat(d["usado_en"])) if d["usado_en"] else None,
    }


@router.get("/ajustes/dispositivos")
@comun._cierra_conexion
def dispositivos_view(person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, t = comun._ctx(person_id)
    items = [_dispositivo_row(t, d) for d in dispositivo.listar(conn, person_id)]
    resp = comun._render("dispositivos.html", t=t, tab="settings", items=items, new_key=None,
                   turnos=db.trabaja_turnos_si(conn, person_id))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/ajustes/dispositivos")
@comun._cierra_conexion_async
async def dispositivos_crear(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await comun._form(request)
    conn, _, t = comun._ctx(person_id)
    nombre = (f.get("nombre") or "").strip()
    if not nombre:
        items = [_dispositivo_row(t, d) for d in dispositivo.listar(conn, person_id)]
        resp = comun._render("dispositivos.html", t=t, tab="settings", items=items, new_key=None,
                       turnos=db.trabaja_turnos_si(conn, person_id))
        resp.headers["Cache-Control"] = "no-store"
        return resp
    alcances = set()
    if f.get("alcance_turnos") == "1":
        alcances.add("turnos:subir")
    if f.get("alcance_vida") == "1":
        alcances.add("vida:leer")
    if f.get("alcance_desplazamiento") == "1":
        alcances.add("desplazamiento:subir")
    if f.get("alcance_asistente_mcp") == "1":
        alcances.add("asistente:mcp")
    if f.get("alcance_hogar") == "1":
        alcances.add("hogar:leer")
    if f.get("alcance_hogar_escribir") == "1":
        alcances.add("hogar:escribir")
    new_key = dispositivo.crear(conn, person_id, nombre, alcances)
    items = [_dispositivo_row(t, d) for d in dispositivo.listar(conn, person_id)]
    resp = comun._render("dispositivos.html", t=t, tab="settings", items=items, new_key=new_key,
                   turnos=db.trabaja_turnos_si(conn, person_id))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/ajustes/dispositivos/{dispositivo_id}/revocar")
@comun._cierra_conexion
def dispositivos_revocar(dispositivo_id: int, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, _ = comun._ctx(person_id)
    dispositivo.revocar(conn, person_id, dispositivo_id)
    return RedirectResponse("/ajustes/dispositivos", status_code=303)


# ---- Device-key API (hosted only): a PC script sends `Authorization: Bearer <key>` -----------------

_TURNOS_FILENAME_RE = re.compile(r"^turnos-.*\.json$")
MAX_TURNOS_BODY_BYTES = 256 * 1024


@router.post("/api/turnos")
@comun._cierra_conexion_async
async def api_turnos(request: Request, person_id: int = Depends(identity.person_from_device("turnos:subir"))):
    file_name = request.headers.get("x-body-archivo") or ""
    if not _TURNOS_FILENAME_RE.match(file_name):
        raise HTTPException(400)
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_TURNOS_BODY_BYTES:
        raise HTTPException(413)
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_TURNOS_BODY_BYTES:
            raise HTTPException(413)
    body = bytes(chunks)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "archivo_rechazado", "motivo": "esquema"}, status_code=422)
    if not isinstance(data, dict):
        return JSONResponse({"error": "archivo_rechazado", "motivo": "esquema"}, status_code=422)
    conn, _, _ = comun._ctx(person_id)
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch WHERE person_id = ?", (person_id,)
    ).fetchone()["n"]
    try:
        batch_id = shift_import.propose_data(conn, data, file_name, person_id)
    except shift_import.ImportRefused as e:
        return JSONResponse(
            {"error": "archivo_rechazado", "motivo": shift_import.refusal_motivo(e)}, status_code=422
        )
    after = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch WHERE person_id = ?", (person_id,)
    ).fetchone()["n"]
    turnos = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch_item WHERE batch_id = ?", (batch_id,)
    ).fetchone()["n"]
    return {"lote": batch_id, "turnos": turnos, "nuevo": after > before}


MAX_DESPLAZAMIENTO_BODY_BYTES = 1024 * 1024


@router.post("/api/desplazamiento")
@comun._cierra_conexion_async
async def api_desplazamiento(
    request: Request, person_id: int = Depends(identity.person_from_device("desplazamiento:subir"))
):
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_DESPLAZAMIENTO_BODY_BYTES:
        raise HTTPException(413)
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_DESPLAZAMIENTO_BODY_BYTES:
            raise HTTPException(413)
    try:
        texto = bytes(chunks).decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "archivo_rechazado", "motivo": "esquema"}, status_code=422)
    conn, _, _ = comun._ctx(person_id)
    resultado = desplazamiento_import.importar(conn, person_id, texto)
    if not resultado["ok"]:
        return JSONResponse(
            {"error": "archivo_rechazado", "motivo": resultado["motivo"]}, status_code=422
        )
    return {"dias": resultado["dias"], "primer_dia": resultado["primer_dia"],
            "ultimo_dia": resultado["ultimo_dia"], "sin_cambios": resultado["sin_cambios"]}


@router.get("/api/vida/semana")
@comun._cierra_conexion
def api_vida_semana(semana: str | None = None,
                     person_id: int = Depends(identity.person_from_device("vida:leer"))):
    conn, person, _ = comun._ctx(person_id)
    if semana:
        try:
            monday = date.fromisoformat(semana)
        except ValueError:
            raise HTTPException(400)
        if monday.weekday() != 0:
            raise HTTPException(400)
    else:
        monday = vida_export.current_week_monday(hoy=comun._today_local(person))
    data = vida_export.week_section(conn, monday, person_id)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# ---- Hogar's own feed (hosted only): BODY-SERVICIO-HOGAR-1, Carlos's ruling -- a full,
# unfiltered read of his own data, gated only by the hogar:leer device key itself ------------------

def _ahora_local(person: dict) -> datetime:
    """Local wall-clock 'now' for `manana`'s own `comidas.faltan` boundary (BODY-HOGAR-ESCRIBE-2
    ruling 3) -- a route-local twin of `comun._today_local`'s own `fechas.hoy_local` wrapper, kept
    here (not in `comun.py`, outside this slice's `touches:`) since no existing helper returns the
    person's local time of day, only their local date."""
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    return datetime.now(tz).replace(tzinfo=None)


@router.get("/api/hogar/manana")
@comun._cierra_conexion
def api_hogar_manana(dia: str | None = None,
                      person_id: int = Depends(identity.person_from_device("hogar:leer"))):
    conn, person, _ = comun._ctx(person_id)
    if dia:
        try:
            d = date.fromisoformat(dia)
        except ValueError:
            raise HTTPException(400)
    else:
        d = comun._today_local(person)
    data = hogar_feed.manana(conn, person_id, d, _ahora_local(person))
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.get("/api/hogar/serie")
@comun._cierra_conexion
def api_hogar_serie(metric_key: str, desde: str, hasta: str,
                     person_id: int = Depends(identity.person_from_device("hogar:leer"))):
    conn, _, _ = comun._ctx(person_id)
    try:
        date.fromisoformat(desde)
        date.fromisoformat(hasta)
    except ValueError:
        raise HTTPException(400)
    if hasta < desde:
        raise HTTPException(400)
    try:
        data = hogar_feed.serie(conn, person_id, metric_key, desde, hasta)
    except hogar_feed.MetricDesconocida:
        raise HTTPException(404)
    except hogar_feed.RangoDemasiadoLargo:
        raise HTTPException(400)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.get("/api/hogar/turnos")
@comun._cierra_conexion
def api_hogar_turnos(desde: str, hasta: str,
                      person_id: int = Depends(identity.person_from_device("hogar:leer"))):
    conn, _, _ = comun._ctx(person_id)
    try:
        date.fromisoformat(desde)
        date.fromisoformat(hasta)
    except ValueError:
        raise HTTPException(400)
    if hasta < desde:
        raise HTTPException(400)
    try:
        data = hogar_feed.turnos(conn, person_id, desde, hasta)
    except hogar_feed.RangoDemasiadoLargo:
        raise HTTPException(400)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# ---- Hogar's own writes (hosted only): BODY-HOGAR-ESCRIBE-2 ruling 2 -- gated by the separate
# `hogar:escribir` device-key scope, dispatching through the SAME shared core GET /api/asistente/
# mcp/herramientas and POST /api/asistente/mcp/llamar already use (body/rutas/mcp_remoto.py), never
# a copied tool schema or a second dispatch path. ------------------------------------------------

_HOGAR_ESCRIBIR_HERRAMIENTAS = {"registrar_comida", "anotar_sesion"}


@router.get("/api/hogar/herramientas")
@comun._cierra_conexion
def api_hogar_herramientas(person_id: int = Depends(identity.person_from_device("hogar:escribir"))):
    conn, person, _ = comun._ctx(person_id)
    idioma = person.get("language", "es")
    todas = mcp_remoto.listar_herramientas(conn, person_id, idioma, puede_aplicar=False)
    filtradas = [h for h in todas["herramientas"] if h["name"] in _HOGAR_ESCRIBIR_HERRAMIENTAS]
    return JSONResponse({"herramientas": filtradas}, headers={"Cache-Control": "no-store"})


MAX_HOGAR_REGISTRAR_BODY_BYTES = 64 * 1024
MAX_REF_LEN = 128


def _mensaje_error_hogar(data: dict, nombre_str: str, idioma: str) -> str:
    """The person-language sentence for a refusal off `mcp_remoto.llamar_herramienta`'s wire shape
    -- the same mapping body/rutas/mcp_http.py's own `_mensaje_error` already builds for its own
    refusals off that exact shape (mcp_http.py is outside this slice's `touches:`, so this is a
    twin, not an import); `mcp._error_remoto_a_rehuso`'s own signature stays untouched, read by a
    test outside this slice too."""
    sesion_cliente = mcp._SesionCliente()
    sesion_cliente.idioma = idioma
    rehuso = mcp._error_remoto_a_rehuso(sesion_cliente, nombre_str, data)
    return errores.texto_propio(rehuso)


@router.post("/api/hogar/registrar")
@comun._cierra_conexion_async
async def api_hogar_registrar(request: Request,
                               person_id: int = Depends(identity.person_from_device("hogar:escribir"))):
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() \
            and int(content_length) > MAX_HOGAR_REGISTRAR_BODY_BYTES:
        raise HTTPException(413)
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_HOGAR_REGISTRAR_BODY_BYTES:
            raise HTTPException(413)
    try:
        cuerpo = json.loads(bytes(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400)
    if not isinstance(cuerpo, dict):
        raise HTTPException(400)
    nombre = cuerpo.get("herramienta")
    if not isinstance(nombre, str) or nombre not in _HOGAR_ESCRIBIR_HERRAMIENTAS:
        raise HTTPException(404)
    argumentos = cuerpo.get("argumentos")
    argumentos = argumentos if isinstance(argumentos, dict) else {}
    # BODY-HOGAR-REGISTRAR-IDEMPOTENTE-1 ruling 2: an OPTIONAL top-level `ref`, the writer's own
    # idempotency key for this act -- malformed the same way any other bad field on this wire is,
    # by name, before anything is looked up or run.
    ref = cuerpo.get("ref")
    if ref is not None and not (isinstance(ref, str) and 1 <= len(ref) <= MAX_REF_LEN):
        raise HTTPException(400)
    conn, person, _ = comun._ctx(person_id)
    idioma = person.get("language", "es")
    if ref is not None:
        existente = db.buscar_escritura_externa(conn, person_id, "hogar", ref)
        if existente is not None:
            # Ruling 2: the same ref reused under a different tool is a writer bug, not a retry --
            # refused by name, never silently matched to the wrong act.
            if existente["herramienta"] != nombre:
                raise HTTPException(400)
            return JSONResponse(
                {"ok": True, "propuesta_id": existente["propuesta_id"],
                 "resultado": {"propuesta_id": existente["propuesta_id"], "estado": "aplicada"},
                 "estado": "ya_estaba"},
                headers={"Cache-Control": "no-store"})
    data = mcp_remoto.llamar_herramienta(conn, person_id, nombre, argumentos, None,
                                          puede_aplicar=False, auto_aplicar=True, origin="hogar")
    if "error" in data:
        respuesta = {"ok": False, "error": _mensaje_error_hogar(data, nombre, idioma)}
        # BODY-HOGAR-NOTA-SESION-GUARD-1 ruling 3: a stable machine word Hogar can branch on
        # without parsing `error`'s own person-language sentence -- carried straight through from
        # the shared core's `motivo` (set only when the refusing ToolError carried its own codigo).
        if "motivo" in data:
            respuesta["motivo"] = data["motivo"]
        # Ruling 2: a failed or refused write records no key here either, so Hogar may retry the
        # same ref safely -- data["error"] means escritura_externa was never reached below.
        return JSONResponse(respuesta, headers={"Cache-Control": "no-store"})
    propuesta_id = data["salida"].get("propuesta_id")
    # Ruling 2: recorded only once the proposal this exact call made is actually applied -- an
    # auto-apply that did not reach "aplicada" (already resolved, drifted) is itself a write that
    # did not go through, so it records no key either.
    if ref is not None and isinstance(propuesta_id, int) and data["salida"].get("estado") == "aplicada":
        db.registrar_escritura_externa(conn, person_id, "hogar", ref, nombre, propuesta_id)
    return JSONResponse(
        {"ok": True, "propuesta_id": propuesta_id, "resultado": data["salida"]},
        headers={"Cache-Control": "no-store"})
