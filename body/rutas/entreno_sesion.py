"""Per-session detail (BODY-ENTRENO-VISTA-SESION-1, ruling 1): `GET /day/{day}/entreno/{source_id}`
-- Día's own per-activity drill-down, reached from each activity row Día already lists. Person-
scoped exactly like every other Día route: another person's source_id, or a day/source_id this
person has no activity under, 404s. Logic lives in body/entreno_sesion.py (no web imports there);
this module formats with t() and renders the template.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from .. import db, entreno_sesion, history, identity, recuperacion
from ..i18n import T
from . import comun

router = APIRouter()

# Duplicated from body/rutas/hoy.py's own `_RECOVERY_REASON_KEY`/`_recovery_line` -- neither route
# module may import the other for one helper (the same choice hoy.py's own `_momento_checkin_txt`
# docstring already explains for a different pair of functions).
_RECOVERY_REASON_KEY = {
    "sin_cobertura": "day.recovery.reason.sin_cobertura",
    "sin_rango": "day.recovery.reason.sin_rango",
    "fuera_de_horizonte": "day.recovery.reason.fuera_de_horizonte",
}


def _recovery_line(t: T, row) -> str | None:
    if row is None or row["motivo"] == "marcado":
        return None
    if row["motivo"] == "fuera_de_horizonte":
        return t(_RECOVERY_REASON_KEY["fuera_de_horizonte"], horas=t.num(recuperacion.MODELO["horizonte_h"]))
    if row["motivo"] is not None:
        return t(_RECOVERY_REASON_KEY[row["motivo"]])
    return t("day.recovery.ok", min=t.num(round(row["minutos"])))


def _nombre(t: T, a: dict) -> str:
    """Duplicated from body/rutas/hoy.py's own `_activity_name` (DESIGN rule 10): an unnamed
    activity shows its translated kind when Garmin's own type code is one this app knows; an
    unknown code never prints the raw code, only a plain generic label."""
    if a["name"]:
        return a["name"]
    if a["type"] in history.KNOWN_ACTIVITY_TYPES:
        return t("activity.type." + a["type"])
    return t("activity.generic")


def _tipo_label(t: T, tipo: str | None) -> str | None:
    if not tipo:
        return None
    if tipo in history.KNOWN_ACTIVITY_TYPES:
        return t("activity.type." + tipo)
    return t("activity.generic")


def _detalle_cabecera(t: T, a: dict) -> str:
    bits = []
    fin = entreno_sesion._fin_local(a["start_local"], a["duration_s"])
    if a["start_local"] and fin is not None:
        bits.append(f"{a['start_local'][11:16]} → {fin.strftime('%H:%M')}")
    if a["duration_s"]:
        bits.append(t.minutes(a["duration_s"] / 60))
    if a["distance_m"]:
        bits.append(f"{t.num(a['distance_m'] / 1000, 1)} km")
    return " · ".join(bits)


def _figura_ctx(t: T, label_key: str, valor, habitual, tipo_label: str | None, unidad: str) -> dict | None:
    if valor is None:
        return None
    valor_txt = f"{t.num(valor)} {unidad}" if unidad else t.num(valor)
    if habitual is not None and tipo_label:
        habitual_txt = f"{t.num(habitual)} {unidad}" if unidad else t.num(habitual)
        texto = t("entreno_sesion.comparacion", valor=valor_txt, tipo=tipo_label, habitual=habitual_txt)
    else:
        texto = valor_txt
    return {"label": t(label_key), "texto": texto}


def _figuras_ctx(t: T, a: dict, comp: dict, tipo_label: str | None) -> list[dict]:
    out = []
    for key, label_key, unidad in (
        ("avg_hr", "entreno_sesion.avg_hr", t("unit.bpm")),
        ("max_hr", "entreno_sesion.max_hr", t("unit.bpm")),
        ("calories", "entreno_sesion.calorias", "kcal"),
    ):
        f = _figura_ctx(t, label_key, a.get(key), comp["valores"].get(key), tipo_label, unidad)
        if f is not None:
            out.append(f)
    return out


def _peso_txt(t: T, s: dict) -> str | None:
    if s["weight_value"] is None:
        return None
    unidad = s["weight_unit"] or "kg"
    return f"{t.num(s['weight_value'], 1)} {unidad}"


def _descanso_txt(t: T, rest_after_s: float | None) -> str | None:
    if rest_after_s is None:
        return None
    return t("entreno_sesion.descanso", dur=t.minutes(rest_after_s / 60))


def _series_ctx(t: T, sets: list[dict], curva_ctx: dict | None) -> list[dict]:
    """Ruling 3 + ruling 4's per-set peak and per-rest drop: one row per active set, in order,
    grouped into consecutive-same-exercise blocks by the template (`grupo_nuevo`)."""
    bpm = t("unit.bpm")
    out = []
    ultimo_ejercicio = object()
    for s in sets:
        pico = caida = None
        if curva_ctx is not None:
            v = curva_ctx["picos"].get(s["set_order"])
            if v is not None:
                pico = t("entreno_sesion.pico", valor=f"{t.num(v)} {bpm}")
            v = curva_ctx["caidas"].get(s["set_order"])
            if v is not None:
                caida = t("entreno_sesion.caida", valor=f"{t.num(v)} {bpm}")
        out.append({
            "exercise": s["exercise"] or "—",
            "reps": s["reps"],
            "peso": _peso_txt(t, s),
            "hora": s["start_local"][11:16] if s["start_local"] else None,
            "duracion": t.minutes(s["duration_s"] / 60) if s["duration_s"] else None,
            "corregida": s["corrected_by_person"],
            "excluida": s["excluded_by_person"],
            "descanso": _descanso_txt(t, s["rest_after_s"]),
            "pico": pico,
            "caida": caida,
            "grupo_nuevo": s["exercise"] != ultimo_ejercicio,
        })
        ultimo_ejercicio = s["exercise"]
    return out


def _curva_svg(t: T, sets: list[dict], curva_ctx: dict, width: int = 320, height: int = 140) -> str:
    """Ruling 4: the session's own HR curve, each active set's window shaded (`.hr-set`) and each
    rest marked (`.hr-rest`, a lighter shade) -- inline SVG, server-built, with no inline styling
    (DESIGN rule 13 BODY-ESTILOS-1): every colour comes from a fixed class, never a `var(...)`
    string in markup. The legend names only what is actually drawn (BODY-ESTADOS-VACIOS-2-M2)."""
    puntos = curva_ctx["puntos"]
    lo, hi = curva_ctx["min"], curva_ctx["max"]
    pad = (hi - lo) * 0.15 or 1
    lo, hi = lo - pad, hi + pad
    top, h = 6, height - 20
    span_s = (curva_ctx["fin"] - curva_ctx["inicio"]).total_seconds() or 1

    def x(ts: datetime) -> float:
        return max(0.0, min(1.0, (ts - curva_ctx["inicio"]).total_seconds() / span_s)) * width

    def y(v: float) -> float:
        return top + h - (v - lo) / (hi - lo) * h

    shapes = []
    hay_sets = hay_descansos = False
    for s in sets:
        if not s["start_local"] or s["duration_s"] is None:
            continue
        inicio_s = datetime.fromisoformat(s["start_local"])
        fin_s = inicio_s + timedelta(seconds=s["duration_s"])
        shapes.append(f'<rect x="{x(inicio_s):.1f}" y="{top}" width="{max(0.0, x(fin_s) - x(inicio_s)):.1f}" '
                      f'height="{h}" class="hr-set"/>')
        hay_sets = True
        if s["rest_after_s"] is not None:
            fin_r = fin_s + timedelta(seconds=s["rest_after_s"])
            shapes.append(f'<rect x="{x(fin_s):.1f}" y="{top}" width="{max(0.0, x(fin_r) - x(fin_s)):.1f}" '
                          f'height="{h}" class="hr-rest"/>')
            hay_descansos = True

    line_pts = " ".join(f"{x(ts):.1f},{y(v):.1f}" for ts, v in puntos)
    shapes.append(f'<polyline fill="none" class="stroke-body" stroke-width="2" '
                  f'vector-effect="non-scaling-stroke" stroke-linejoin="round" points="{line_pts}"/>')

    aria = html.escape(t("entreno_sesion.pulso_aria", min=t.num(curva_ctx["min"]), max=t.num(curva_ctx["max"]),
                          unidad=t("unit.bpm")))
    svg = (f'<svg viewBox="0 0 {width} {top + h + 6}" width="100%" height="140" role="img" aria-label="{aria}" '
           f'preserveAspectRatio="none" class="chart-svg">{"".join(shapes)}</svg>')
    legend_items = [f'<span><i class="c-body legend-line"></i>{t("entreno_sesion.leyenda_pulso")}</span>']
    if hay_sets:
        legend_items.append(f'<span><i class="hr-set"></i>{t("entreno_sesion.leyenda_serie")}</span>')
    if hay_descansos:
        legend_items.append(f'<span><i class="hr-rest"></i>{t("entreno_sesion.leyenda_descanso")}</span>')
    return svg + '<div class="legend mt-4">' + "".join(legend_items) + "</div>"


@router.get("/day/{day}/entreno/{source_id}")
@comun._cierra_conexion
def entreno_sesion_view(day: str, source_id: str, person_id: int = Depends(identity.current_person_id)):
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(404)
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    a = entreno_sesion.actividad(conn, person_id, d.isoformat(), source_id)
    if a is None:
        raise HTTPException(404)

    zona = person.get("timezone") or "America/Bogota"
    tipo_label = _tipo_label(t, a.get("type"))
    comp = entreno_sesion.comparaciones(conn, person_id, a)
    figuras = _figuras_ctx(t, a, comp, tipo_label)
    sin_historial = None
    if a.get("type") and comp["n"] < entreno_sesion.HISTORIAL_MIN and any(
            a.get(k) is not None for k in entreno_sesion.COMPARABLE_KEYS):
        sin_historial = t("entreno_sesion.sin_historial", tipo=tipo_label)

    es_fuerza = a.get("type") == "strength_training"
    sets = entreno_sesion.series(conn, person_id, a["sets_source"], a["sets_source_id"]) if es_fuerza else []
    curva_ctx = entreno_sesion.curva(conn, person_id, d.isoformat(), zona, a["start_local"], a["duration_s"], sets)

    recuperacion_row = conn.execute(
        "SELECT minutos, motivo FROM recuperacion_actividad WHERE person_id = ? AND activity_source = ? AND "
        "activity_source_id = ? AND modelo_version = ?",
        (person_id, a["source"], a["source_id"], recuperacion.MODELO["version"])).fetchone()

    return comun._render(
        "entreno_sesion.html", t=t, tab="today", day=d.isoformat(), turnos=turnos_on,
        nombre=_nombre(t, a), tipo_label=tipo_label, date_long=t.long_date(d),
        detalle=_detalle_cabecera(t, a), figuras=figuras, sin_historial=sin_historial,
        es_fuerza=es_fuerza, sets=_series_ctx(t, sets, curva_ctx) if es_fuerza else [],
        pulso_svg=_curva_svg(t, sets, curva_ctx) if curva_ctx is not None else None,
        pulso_pendiente=t("entreno_sesion.curva_pendiente"),
        recovery=_recovery_line(t, recuperacion_row),
    )
