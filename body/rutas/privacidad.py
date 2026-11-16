"""Privacidad (BODY-WEB-ESTRUCTURA-2): consent gate, the settings screen, data export and
account deletion. Moved out of body/web.py as a pure move -- same paths, same bodies.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .. import cuenta, db, fuentes, identity, privacidad
from . import comun

router = APIRouter()


def _fuentes_ctx(t, conn, person_id: int, confirmar: str | None) -> list[dict]:
    """BODY-DESPLAZAMIENTO-3 ruling 4: each imported source, in words -- `confirmando` marks the
    one row (if any) whose "Borrar estos datos" the person just tapped, for the template's own
    inline confirmation step."""
    out = []
    for row in privacidad.fuentes_importadas(conn, person_id):
        datos = fuentes.FUENTES.get(row["fuente"])
        if datos is None:
            continue
        label = t(datos["label_key"])
        out.append({
            "fuente": row["fuente"], "label": label,
            "resumen": t("privacidad.fuente.resumen", n=row["dias"],
                         primer=comun._short_date(t, date.fromisoformat(row["primer_dia"])),
                         ultimo=comun._short_date(t, date.fromisoformat(row["ultimo_dia"]))),
            "confirmando": row["fuente"] == confirmar,
        })
    return out


@router.get("/privacidad/consentimiento")
@comun._cierra_conexion
def consentimiento_form(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    conn, _, t = comun._ctx(person_id)
    return_to = cuenta.safe_return_to(request.query_params.get("return_to"))
    cambio = privacidad.acepto_version_anterior(conn, person_id)
    return comun._render("consentimiento.html", t=t, tab=None, return_to=return_to, cambio=cambio)


@router.post("/privacidad/consentimiento")
@comun._cierra_conexion_async
async def consentimiento_decide(request: Request, person_id: int = Depends(identity.current_person_id)):
    if not cuenta.hosted():
        raise HTTPException(404)
    f = await comun._form(request)
    return_to = cuenta.safe_return_to(f.get("return_to"))
    if f.get("decision") == "acepto":
        conn, _, _ = comun._ctx(person_id)
        privacidad.record_consent(conn, person_id)
        return RedirectResponse(return_to, status_code=303)
    conn, _, _ = comun._ctx(person_id)
    sid = cuenta.end_session(conn, request.cookies.get(cuenta.SESSION_COOKIE))
    resp = RedirectResponse(cuenta.logout_url(sid), status_code=303)
    cuenta.clear_session_cookie(resp)
    return resp


@router.get("/privacidad")
@comun._cierra_conexion
def privacidad_view(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    row = conn.execute(
        "SELECT accepted_at FROM consentimiento WHERE person_id = ? AND version = ?",
        (person_id, privacidad.CONSENT_VERSION),
    ).fetchone()
    accepted_date = comun._short_date(t, datetime.fromisoformat(row["accepted_at"]).date()) if row else None
    es_hosted = cuenta.hosted()
    reauth_ok = None
    if es_hosted and request.query_params.get("reauth") == "1":
        token = request.cookies.get(cuenta.SESSION_COOKIE)
        reauth_ok = cuenta.reciente(conn, token)
    confirmar = request.query_params.get("confirmar")
    fuente_borrada = request.query_params.get("fuente_borrada")
    datos_borrada = fuentes.FUENTES.get(fuente_borrada) if fuente_borrada else None
    borrada_texto = t("privacidad.fuente.borrada", fuente=t(datos_borrada["label_key"])) if datos_borrada else None
    return comun._render("privacidad.html", t=t, tab="settings", version=privacidad.CONSENT_VERSION,
                   accepted_date=accepted_date, delete_word=t("privacidad.borrar_palabra"),
                   error=request.query_params.get("error") == "confirmacion",
                   es_hosted=es_hosted, reauth_ok=reauth_ok, turnos=db.trabaja_turnos_si(conn, person_id),
                   fuentes=_fuentes_ctx(t, conn, person_id, confirmar), borrada_texto=borrada_texto)


@router.get("/privacidad/exportar")
@comun._cierra_conexion
def privacidad_exportar(person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    data = privacidad.export_person(conn, person_id)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="body-{date.today().isoformat()}.json"'})


@router.post("/privacidad/borrar")
@comun._cierra_conexion_async
async def privacidad_borrar(request: Request, person_id: int = Depends(identity.current_person_id)):
    f = await comun._form(request)
    conn, _, t = comun._ctx(person_id)
    if cuenta.hosted():
        token = request.cookies.get(cuenta.SESSION_COOKIE)
        if not cuenta.reciente(conn, token):
            conn.close()
            return_to = quote("/privacidad?reauth=1", safe="")
            return RedirectResponse(
                f"/login/cuenta/start?prompt=login&max_age=600&return_to={return_to}", status_code=303
            )
    if (f.get("confirmacion") or "").strip() != t("privacidad.borrar_palabra"):
        conn.close()
        return RedirectResponse("/privacidad?error=confirmacion", status_code=303)
    privacidad.delete_person(conn, person_id)
    conn.close()
    if cuenta.hosted():
        return RedirectResponse(cuenta.logout_url(None), status_code=303)
    return RedirectResponse("/", status_code=303)


@router.post("/privacidad/fuente/borrar")
@comun._cierra_conexion_async
async def privacidad_fuente_borrar(request: Request, person_id: int = Depends(identity.current_person_id)):
    """BODY-DESPLAZAMIENTO-3 ruling 4: deletes exactly this person's rows from one imported source
    -- an unknown fuente, or one this person never imported, is refused (404), never a silent
    no-op that would mislead the confirmation screen."""
    f = await comun._form(request)
    fuente = (f.get("fuente") or "").strip()
    conn, _, _ = comun._ctx(person_id)
    ok = privacidad.delete_fuente(conn, person_id, fuente)
    if not ok:
        raise HTTPException(404)
    return RedirectResponse(f"/privacidad?fuente_borrada={quote(fuente, safe='')}", status_code=303)
