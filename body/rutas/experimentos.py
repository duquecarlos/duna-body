"""Experiments on screen (BODY-EXPERIMENTOS-PANTALLA-1): a section on Metas, a detail page, and a
short line on Hoy for a day that counts. Reads `experimento` / `experimento_registro` only, and
writes through the existing `db.registrar_dia_experimento` and the new `db.cerrar_experimento` --
no schema change. Verdict words only, never a grading colour (DESIGN rule 11's own exception):
`experimentos.resultado.*` below, never `metric_def.better` or a `.good`/`.bad` class.

`asistente._filtro_dias_txt` (BODY-EXPERIMENTOS-REGISTRO-1) is reused by attribute access, not
copied -- this module's own card and detail builders are its only other caller today.
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import cuenta, db, experimentos, identity
from . import asistente, comun

router = APIRouter()

# resultado -> a plain direction arrow, never a grading colour (DESIGN rule 11's exception for this
# screen): 'sin_cambio', 'pocos_dias' and 'aun_no' carry no arrow at all.
_RESULTADO_ARROW = {"mas_bajo": "↓", "mas_alto": "↑"}


def _dia_mes(t, d: date) -> str:
    """"9 nov" style: day, abbreviated month, no year -- comun._short_date always carries a year,
    which these short card/detail dates do not need."""
    return f"{d.day} {t('date.months')[d.month - 1][:3]}"


def _fecha_rango_txt(t, fecha_inicio: str, fecha_fin: str | None) -> str:
    d0 = date.fromisoformat(fecha_inicio)
    if fecha_fin is None:
        return t("experimentos.dias_desde", fecha=_dia_mes(t, d0))
    return t("experimentos.dias_rango", desde=_dia_mes(t, d0), hasta=_dia_mes(t, date.fromisoformat(fecha_fin)))


def _progreso_txt(conn, t, person_id: int, row, ver: dict, today: date) -> str:
    """Counts what the person actually marked, over `ver["dias_prueba"]` (hecho, already right --
    reused, not re-derived) plus `ver["dias_excluidos_no_ocurrio"]` split by whether `_dias_marcados`
    (the same registro/habit_check source `experimentos.veredicto`'s own `_ocurrio_por_dia` reads)
    carries a mark for that day: marked false -> no_hechos, no mark at all -> sin_marcar. TODAY, when
    unmarked, counts for neither -- the day is not over (ORCH-CARLOS-DELEGATES-TIER-1-1)."""
    hechos = len(ver["dias_prueba"])
    today_iso = today.isoformat()
    marcas = {d["day"] for d in _dias_marcados(conn, person_id, row)}
    no_hechos = sum(1 for d in ver["dias_excluidos_no_ocurrio"] if d in marcas)
    sin_marcar = sum(1 for d in ver["dias_excluidos_no_ocurrio"] if d not in marcas and d != today_iso)
    partes = []
    if hechos:
        partes.append(t("experimentos.progreso_hechos", hechos=hechos))
    if no_hechos:
        partes.append(t("experimentos.progreso_no_hechos", no_hechos=no_hechos))
    if sin_marcar:
        partes.append(t("experimentos.progreso_sin_marcar", sin_marcar=sin_marcar))
    txt = " · ".join(partes) if partes else t("experimentos.progreso_vacio")
    objetivo = row["dias_prueba_objetivo"]
    if objetivo:
        txt += " · " + t("experimentos.progreso_objetivo", hechos=hechos, objetivo=objetivo)
    return txt


def _metric_line(t, metric_key: str, resultado: str) -> dict:
    arrow = _RESULTADO_ARROW.get(resultado, "")
    txt = t("experimentos.resultado." + resultado)
    return {"label": t("metric." + metric_key), "text": f"{arrow} {txt}".strip()}


def _tarjeta_desde_veredicto(conn, t, person_id: int, row, ver: dict, today: date) -> dict:
    filtro_dias = json.loads(row["filtro_dias"])
    metricas = json.loads(row["metricas"])
    card = {
        "id": row["id"], "title": row["titulo"],
        "dias_txt": asistente._filtro_dias_txt(t, filtro_dias),
        "fecha_txt": _fecha_rango_txt(t, row["fecha_inicio"], row["fecha_fin"]),
        "progreso_txt": _progreso_txt(conn, t, person_id, row, ver, today),
        "metrics": [_metric_line(t, m, ver["metricas"][m]["resultado"]) for m in metricas],
        "closed": row["estado"] == "cerrado",
    }
    if card["closed"]:
        card["status_text"] = t("experimentos.estado.cerrado")
    return card


def resumen_metas(conn, t, person_id: int, today: date) -> tuple[list, list]:
    """Metas' "Experimentos" section data: active cards, then closed cards -- `goals_list` calls
    this and renders both in `goals.html`."""
    activos = [_tarjeta_desde_veredicto(conn, t, person_id, r, experimentos.veredicto(conn, person_id, r["id"], hoy=today), today)
               for r in db.experimentos(conn, person_id, "activo")]
    cerrados = [_tarjeta_desde_veredicto(conn, t, person_id, r, experimentos.veredicto(conn, person_id, r["id"], hoy=today), today)
                for r in db.experimentos(conn, person_id, "cerrado")]
    return activos, cerrados


def hoy_contexto(conn, t, person_id: int, today: date) -> list[dict]:
    """Hoy's own line for each ACTIVE experiment whose day filter counts today, within
    [fecha_inicio, fecha_fin] -- `today()` calls this and renders it inside the plan card."""
    today_iso = today.isoformat()
    out = []
    for row in db.experimentos(conn, person_id, "activo"):
        if today_iso < row["fecha_inicio"] or (row["fecha_fin"] is not None and today_iso > row["fecha_fin"]):
            continue
        filtro_dias = json.loads(row["filtro_dias"])
        if not experimentos.dias_que_cuentan(conn, person_id, filtro_dias, today_iso, today_iso):
            continue
        if row["habito_id"] is not None:
            out.append({"id": row["id"], "habito": True, "marcado": None,
                        "linea": t("today.experimento_linea_habito", titulo=row["titulo"])})
            continue
        reg = conn.execute(
            "SELECT ocurrio FROM experimento_registro WHERE experimento_id = ? AND person_id = ? AND day = ?",
            (row["id"], person_id, today_iso)).fetchone()
        marcado = None if reg is None else ("1" if reg["ocurrio"] else "0")
        out.append({"id": row["id"], "habito": False, "marcado": marcado,
                    "linea": t("today.experimento_linea", titulo=row["titulo"])})
    return out


def _metric_detalle(t, defs: dict, metric_key: str, m: dict) -> dict:
    """Builds on `_metric_line` with the detail page's own two means (BODY-EXPERIMENTOS-PANTALLA-1
    item 3) -- `.get` throughout since an intradia experiment's own metric dict carries only
    `resultado` (`experimentos.veredicto`'s own documented shape for `ventana.tipo != 'dia'`)."""
    line = _metric_line(t, metric_key, m["resultado"])
    media_prueba = comun._fig_unit(t, metric_key, m.get("media_prueba"), defs)
    media_base = comun._fig_unit(t, metric_key, m.get("media_base"), defs)
    line["media_prueba_txt"] = t("experimentos.media_linea", etiqueta=t("experimentos.media_prueba"),
                                 n=m.get("n_prueba", 0), valor=media_prueba)
    line["media_base_txt"] = t("experimentos.media_linea", etiqueta=t("experimentos.media_base"),
                               n=m.get("n_base", 0), valor=media_base)
    line["offset_nota"] = t("experimentos.nota_offset") if experimentos.OFFSET_DIA.get(metric_key) else None
    return line


def _dias_marcados(conn, person_id: int, row) -> list[dict]:
    """The log behind the verdict: `experimento_registro` for an experiment with no habit, or the
    linked habit's own `habit_check` otherwise (the same choice `experimentos.veredicto`'s own
    `_ocurrio_por_dia` makes) -- most recent day first."""
    if row["habito_id"] is not None:
        rows = conn.execute(
            "SELECT day, done AS ocurrio, note AS nota FROM habit_check WHERE habit_id = ? AND person_id = ? "
            "ORDER BY day DESC", (row["habito_id"], person_id)).fetchall()
    else:
        rows = conn.execute(
            "SELECT day, ocurrio, nota FROM experimento_registro WHERE experimento_id = ? AND person_id = ? "
            "ORDER BY day DESC", (row["id"], person_id)).fetchall()
    return [{"day": r["day"], "ocurrio": bool(r["ocurrio"]), "nota": r["nota"]} for r in rows]


def _access(conn, person_id: int, experiment_id: int):
    """The same not-found-for-a-stranger shape `goals.goal_access`/`_require_creator` use: another
    person's id reads exactly like an unknown one."""
    row = conn.execute("SELECT * FROM experimento WHERE id = ?", (experiment_id,)).fetchone()
    if row is None or row["person_id"] != person_id:
        return None
    return row


@router.get("/experiments/{experiment_id}")
@comun._cierra_conexion
def experiment_detail(experiment_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    row = _access(conn, person_id, experiment_id)
    if row is None:
        raise HTTPException(404)
    today = comun._today_local(person)
    ver = experimentos.veredicto(conn, person_id, experiment_id, hoy=today)
    card = _tarjeta_desde_veredicto(conn, t, person_id, row, ver, today)
    defs = comun._defs(conn)
    metricas = json.loads(row["metricas"])
    card["metrics_detalle"] = [_metric_detalle(t, defs, m, ver["metricas"][m]) for m in metricas]
    card["dias_marcados"] = [
        {"fecha": _dia_mes(t, date.fromisoformat(d["day"])), "ocurrio": d["ocurrio"], "nota": d["nota"]}
        for d in _dias_marcados(conn, person_id, row)
    ]
    card["explicacion"] = t("experimentos.explicacion")
    return comun._render("experiment.html", t=t, tab="goals", experiment=card,
                         turnos=db.trabaja_turnos_si(conn, person_id))


@router.post("/experiments/{experiment_id}/close")
@comun._cierra_conexion
def experiment_close(experiment_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, person, _ = comun._ctx(person_id)
    today = comun._today_local(person)
    try:
        db.cerrar_experimento(conn, person_id, experiment_id, fecha_fin=today.isoformat())
    except ValueError:
        raise HTTPException(404)
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)


@router.post("/experiments/{experiment_id}/day")
@comun._cierra_conexion_async
async def experiment_day(experiment_id: int, request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    f = await comun._form(request)
    day = f.get("day")
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise HTTPException(404)
    row = conn.execute("SELECT habito_id FROM experimento WHERE id = ? AND person_id = ?",
                       (experiment_id, person_id)).fetchone()
    if row is None or row["habito_id"] is not None:
        raise HTTPException(404)
    try:
        db.registrar_dia_experimento(conn, experiment_id, person_id, day, f.get("ocurrio") == "1")
    except ValueError:
        raise HTTPException(404)
    back = cuenta.safe_return_to(f.get("back"))
    return RedirectResponse(back, status_code=303)
