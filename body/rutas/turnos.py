"""Turnos screens (BODY-WEB-ESTRUCTURA-6): the shift import review, the cost table on /shifts, how
it's calculated, and the typed-pattern form -- the last domain moved out of body/web.py. Pure move.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import baselines, db, identity, patron_turnos, readiness, shift_import, turnos_costo
from ..i18n import T
from ..lector import Lector
from . import comun

router = APIRouter()


def _batch_date(t: T, received: str) -> str:
    return comun._short_date(t, datetime.fromisoformat(received).date())


def _owned_batch_or_404(conn, batch_id: int, person_id: int) -> None:
    if not conn.execute("SELECT 1 FROM shift_batch WHERE id = ? AND person_id = ?",
                        (batch_id, person_id)).fetchone():
        raise HTTPException(404)


_STRIP_WEEKS = 8
_STRIP_DAYS = _STRIP_WEEKS * 7
_STRIP_KIND_ORDER = ["morning", "afternoon", "night", "day", "off", "other", "none"]


def _turnos_filas(t: T, defs: dict, resumen: dict) -> list[dict]:
    filas = []
    for key in turnos_costo.MODELO["metricas"]:
        v = resumen["costo_promedio"].get(key)
        d = defs.get(key, {})
        if v is None:
            filas.append({"label": t("metric." + key), "sub": t("shifts.cost_sub." + key),
                         "value": None, "palabra": None, "href": None})
            continue
        diff, palabra = turnos_costo.diferencia_y_palabra(key, v)
        amount = t.minutes(abs(diff)) if comun._is_minutes(key) else t.num(abs(diff), 1)
        unit = comun._unit(t, d)
        href = f"/trends?r=90&desde=turnos{'&todas=1' if comun._es_extra_metric(key) else ''}#{key}"
        filas.append({"label": t("metric." + key), "sub": t("shifts.cost_sub." + key),
                     "value": f"{amount}" + (f" {unit}" if unit else ""),
                     "palabra": t("shifts.word." + palabra), "href": href})
    return filas


def _turnos_strip(conn, person_id: int, today_local: date, t: T) -> tuple[list[dict], list[dict]]:
    start = today_local - timedelta(_STRIP_DAYS - 1)
    rows = conn.execute(
        "SELECT day, kind FROM shift WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, start.isoformat(), today_local.isoformat()),
    ).fetchall()
    kind_by_day = {r["day"]: r["kind"] for r in rows}
    # One reader for the whole strip, covering every day's own 30-day baseline look-back
    # (BODY-RENDIMIENTO-2) -- `readiness.compute` would otherwise query it fresh per day. +1 on
    # top of the baseline window: the stress part's baseline is computed on YESTERDAY, so the
    # earliest strip day's own window reaches one day further back than WINDOW_DAYS alone.
    lector = Lector(conn, person_id, (start - timedelta(baselines.WINDOW_DAYS + 1)).isoformat(),
                     today_local.isoformat(), readiness.METRICS)
    strip = []
    present: set[str] = set()
    for i in range(_STRIP_DAYS):
        d = (start + timedelta(i)).isoformat()
        kind = kind_by_day.get(d) or "none"
        present.add(kind)
        score = readiness.compute(conn, d, person_id, lector=lector).score
        # long_date (weekday, day, month -- no year) rather than _short_date: a cell's own day is
        # always within the last 8 weeks, so the year adds nothing and only risks reading the same
        # as an unrelated batch's received date elsewhere on the page.
        fecha = t.long_date(date.fromisoformat(d))
        turno = t("shift.kind." + kind)
        label = (t("shifts.strip_cell", fecha=fecha, turno=turno, n=score) if score is not None
                 else t("shifts.strip_cell_sin_dato", fecha=fecha, turno=turno))
        strip.append({"day": d, "band": kind, "score": score, "label": label})
    legend = [{"kind": k, "label": t("shift.kind." + k)} for k in _STRIP_KIND_ORDER if k in present]
    return strip, legend


@router.get("/shifts")
@comun._cierra_conexion
def shifts(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    if not turnos_on:
        raise HTTPException(404)
    rows = conn.execute(
        "SELECT * FROM shift_batch WHERE person_id = ? AND status = 'proposed' ORDER BY received, extracted_at",
        (person_id,),
    ).fetchall()
    result_msg = None
    qp = request.query_params
    if qp.get("done") == "1":
        kept = int(qp.get("kept_manual", 0)) + int(qp.get("kept_newer", 0))
        result_msg = t("shifts.confirm_all_result", batches=qp.get("batches", "0"), applied=qp.get("applied", "0"))
        if kept:
            result_msg += " " + t("shifts.confirm_all_kept", n=kept)
    today_local = comun._today_local(person)
    resumen = turnos_costo.resumen(conn, person_id, hoy=today_local)
    strip, strip_legend = _turnos_strip(conn, person_id, today_local, t)
    filas = _turnos_filas(t, comun._defs(conn), resumen)
    # BODY-SHIFTS-REDUNDANT-EMPTY-TABLE-1: the hero card above already names the one reason there is
    # no estimate (shifts.reason.*) -- five more rows each saying "sin datos del reloj" would only
    # repeat it. Drop the whole card when every row is empty; a partial row (at least one figure) is
    # informative on its own and keeps rendering exactly as before.
    costo_vacio = all(f["href"] is None for f in filas)
    costo_ctx = {"hero_dias": resumen["dias_recuperacion"], "motivo": resumen["motivo"],
                 "filas": filas, "costo_vacio": costo_vacio, "strip": strip, "strip_legend": strip_legend}
    tiene_patron = patron_turnos.leer(conn, person_id) is not None
    if len(rows) > 1:
        summary = shift_import.summary_proposed(conn, person_id)
        batches = [
            {"batch": dict(b), "received_date": _batch_date(t, b["received"]),
             "preview": _patron_preview_ctx(t, shift_import.preview(conn, b["id"]))}
            for b in rows
        ]
        many_mixed = any(b["kind"] == "pattern" for b in rows)
        return comun._render("shifts.html", t=t, tab="shifts", batch=None, many=True, batches=batches,
                       many_mixed=many_mixed,
                       summary_from=t.month_year(date.fromisoformat(summary["first_day"])),
                       summary_to=t.month_year(date.fromisoformat(summary["last_day"])),
                       summary_days=summary["day_count"], summary_nights=summary["night_count"],
                       result_msg=result_msg, turnos=turnos_on, tiene_patron=tiene_patron, **costo_ctx)
    if rows:
        proposed = rows[0]
        prev = _patron_preview_ctx(t, shift_import.preview(conn, proposed["id"]))
        return comun._render("shifts.html", t=t, tab="shifts", batch=dict(proposed), many=False,
                       received_date=_batch_date(t, proposed["received"]), preview=prev, result_msg=result_msg,
                       turnos=turnos_on, tiene_patron=tiene_patron, **costo_ctx)
    confirmed = shift_import.latest_confirmed(conn, person_id)
    return comun._render("shifts.html", t=t, tab="shifts", batch=None, many=False,
                   confirmed_date=_batch_date(t, confirmed["received"]) if confirmed else None,
                   result_msg=result_msg, turnos=turnos_on, tiene_patron=tiene_patron, **costo_ctx)


def _patron_preview_ctx(t: T, prev: dict) -> dict:
    """Adds the short from/to dates a pattern batch's card reads ("del X al Y") to shift_import.preview()'s
    own dict, without changing its existing keys."""
    return {**prev,
            "from_date": comun._short_date(t, date.fromisoformat(prev["first_day"])) if prev["first_day"] else None,
            "to_date": comun._short_date(t, date.fromisoformat(prev["last_day"])) if prev["last_day"] else None}


def _turnos_bloque_linea(t: T, b: dict, hoy: date) -> str:
    inicio = comun._short_date(t, date.fromisoformat(b["inicio"]))
    fin_dt = date.fromisoformat(b["fin"])
    fin = comun._short_date(t, fin_dt)
    if b["recuperacion_dias"] is not None:
        recuperacion = t("shifts.como_bloque_dias", n=b["recuperacion_dias"])
    else:
        ventana_post = turnos_costo.MODELO["ventana_post"]
        ultimo_dia = fin_dt + timedelta(ventana_post + turnos_costo.MODELO["dias_estables"] - 1)
        recuperacion = (t("shifts.sin_dato_corto") if ultimo_dia > hoy
                        else t("shifts.como_bloque_mas_de", n=ventana_post))
    return t("shifts.como_bloque", inicio=inicio, fin=fin, dias=b["dias"], recuperacion=recuperacion)


@router.get("/shifts/como")
@comun._cierra_conexion
def shifts_como(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    if not db.trabaja_turnos_si(conn, person_id):
        raise HTTPException(404)
    today_local = comun._today_local(person)
    resumen = turnos_costo.resumen(conn, person_id, hoy=today_local)
    bloques = [_turnos_bloque_linea(t, b, today_local) for b in resumen["bloques"]]
    return comun._render("how.html", t=t, tab="shifts", modo="turnos", version=resumen["version"],
                   n_bloques=len(resumen["bloques"]), n_con_recuperacion=resumen["bloques_con_recuperacion"],
                   n_estables=turnos_costo.MODELO["dias_estables"], n_referencia=turnos_costo.REFERENCIA_DIAS,
                   bloques=bloques, turnos=True)


@router.post("/shifts/confirm")
@comun._cierra_conexion_async
async def shifts_confirm(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    if not db.trabaja_turnos_si(conn, person_id):
        raise HTTPException(404)
    batch_id = parse_qs((await request.body()).decode()).get("batch_id", [""])[0]
    if batch_id.isdigit():
        _owned_batch_or_404(conn, int(batch_id), person_id)
        try:
            shift_import.confirm(conn, int(batch_id))
        except (shift_import.ImportRefused, shift_import.SlotIndexMissing):
            pass
    return RedirectResponse("/shifts", status_code=303)


@router.post("/shifts/confirm-all")
@comun._cierra_conexion_async
async def shifts_confirm_all(person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    if not db.trabaja_turnos_si(conn, person_id):
        raise HTTPException(404)
    try:
        result = shift_import.confirm_all(conn, person_id)
    except (shift_import.ImportRefused, shift_import.SlotIndexMissing):
        return RedirectResponse("/shifts", status_code=303)
    return RedirectResponse(
        f"/shifts?done=1&batches={result['batches']}&applied={result['days_applied']}"
        f"&kept_manual={result['days_kept_manual']}&kept_newer={result['days_kept_newer']}",
        status_code=303,
    )


@router.post("/shifts/dismiss")
@comun._cierra_conexion_async
async def shifts_dismiss(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    if not db.trabaja_turnos_si(conn, person_id):
        raise HTTPException(404)
    batch_id = parse_qs((await request.body()).decode()).get("batch_id", [""])[0]
    if batch_id.isdigit():
        _owned_batch_or_404(conn, int(batch_id), person_id)
        shift_import.dismiss(conn, int(batch_id))
    return RedirectResponse("/shifts", status_code=303)


_PATRON_DEFAULT_DIAS = 7
_PATRON_DEFAULT_HORIZONTE = 8


def _patron_dias(raw: str | None, guardado: dict | None) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = len(guardado["cycle"]) if guardado else _PATRON_DEFAULT_DIAS
    return max(1, min(56, n))


def _patron_filas(t: T, cycle: list[dict], start_day: date, errores: dict[int, str] | None = None) -> list[dict]:
    errores = errores or {}
    filas = []
    for i, entry in enumerate(cycle):
        codigo = errores.get(i)
        filas.append({
            "idx": i,
            "n": i + 1,
            "label": t("patron.dia_label", n=i + 1, fecha=t.long_date(start_day + timedelta(i))),
            "kind": entry.get("kind") or "off",
            "start": entry.get("start") or "",
            "end": entry.get("end") or "",
            "error": t("patron.error." + codigo) if codigo else None,
        })
    return filas


@router.get("/shifts/patron")
@comun._cierra_conexion
def shifts_patron(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    if not db.trabaja_turnos_si(conn, person_id):
        raise HTTPException(404)
    guardado = patron_turnos.leer(conn, person_id)
    dias = _patron_dias(request.query_params.get("dias"), guardado)
    start_day = date.fromisoformat(guardado["start_day"]) if guardado else comun._today_local(person)
    horizon_weeks = guardado["horizon_weeks"] if guardado else _PATRON_DEFAULT_HORIZONTE
    cycle = guardado["cycle"] if guardado and len(guardado["cycle"]) == dias else (
        [{"kind": "off", "start": None, "end": None}] * dias)
    return comun._render("shift_pattern.html", t=t, tab="shifts", turnos=True, dias=dias,
                   start_day=start_day.isoformat(), horizon_weeks=horizon_weeks,
                   filas=_patron_filas(t, cycle, start_day), tiene_patron=guardado is not None)


@router.post("/shifts/patron")
@comun._cierra_conexion_async
async def shifts_patron_save(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    if not db.trabaja_turnos_si(conn, person_id):
        raise HTTPException(404)
    f = await comun._form(request)
    dias = _patron_dias(f.get("dias"), None)
    try:
        start_day = date.fromisoformat(f.get("start_day") or "")
    except ValueError:
        start_day = comun._today_local(person)
    try:
        horizon_weeks = max(1, min(26, int(f.get("horizon_weeks", ""))))
    except ValueError:
        horizon_weeks = _PATRON_DEFAULT_HORIZONTE

    cycle = []
    for i in range(dias):
        kind = f.get(f"kind_{i}") or "off"
        if kind == "off":
            cycle.append({"kind": "off", "start": None, "end": None})
        else:
            cycle.append({"kind": kind, "start": f.get(f"start_{i}") or None, "end": f.get(f"end_{i}") or None})

    try:
        patron_turnos.validar(cycle)
    except patron_turnos.PatronInvalido as e:
        errores = {err["index"]: err["code"] for err in e.errors if err["index"] is not None}
        errores_ciclo = [t("patron.error." + err["code"]) for err in e.errors if err["index"] is None]
        return comun._render("shift_pattern.html", t=t, tab="shifts", turnos=True, dias=dias,
                       start_day=start_day.isoformat(), horizon_weeks=horizon_weeks,
                       filas=_patron_filas(t, cycle, start_day, errores),
                       errores_ciclo=errores_ciclo,
                       tiene_patron=patron_turnos.leer(conn, person_id) is not None)

    patron_turnos.guardar(conn, person_id, start_day.isoformat(), cycle, horizon_weeks)
    patron_turnos.proponer(conn, person_id, comun._today_local(person))
    return RedirectResponse("/shifts", status_code=303)


@router.post("/shifts/patron/quitar")
@comun._cierra_conexion_async
async def shifts_patron_quitar(person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    if not db.trabaja_turnos_si(conn, person_id):
        raise HTTPException(404)
    patron_turnos.quitar(conn, person_id)
    for r in conn.execute(
        "SELECT id FROM shift_batch WHERE person_id = ? AND status = 'proposed' AND kind = 'pattern'",
        (person_id,),
    ).fetchall():
        shift_import.dismiss(conn, r["id"])
    return RedirectResponse("/shifts", status_code=303)
