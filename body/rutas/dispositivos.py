"""Device keys and the device-key API (BODY-DISPOSITIVO-1), moved out of body/web.py
(BODY-WEB-ESTRUCTURA-2) as one module: the pairing screen on Ajustes and the API it authorises
are one domain. Pure move -- same paths, same bodies.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import cuenta, db, dispositivo, identity, shift_import, vida_export
from ..i18n import T
from . import comun

router = APIRouter()

# ---- Device keys (hosted only): «Conexión del PC» on Ajustes, BODY-DISPOSITIVO-1 -----------------

_ALCANCE_LABELS = {"turnos:subir": "dispositivos.scope.turnos", "vida:leer": "dispositivos.scope.vida"}


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
