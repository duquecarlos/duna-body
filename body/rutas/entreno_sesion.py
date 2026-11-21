"""Per-session detail (BODY-ENTRENO-VISTA-SESION-1, ruling 1): `GET /day/{day}/entreno/{source_id}`
-- Día's own per-activity drill-down, reached from each activity row Día already lists. Person-
scoped exactly like every other Día route: another person's source_id, or a day/source_id this
person has no activity under, 404s. Logic lives in body/entreno_sesion.py (no web imports there);
this module formats with t() and renders the template.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import anatomia, db, entreno_sesion, history, identity, recuperacion
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

# Duplicated from body/rutas/hoy.py's own `_lista_natural` -- same reason as above.
def _lista_natural(t: T, items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if t.lang == "en":
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    if len(items) == 2:
        return f"{items[0]} y {items[1]}"
    return ", ".join(items[:-1]) + f" y {items[-1]}"


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


def _tipo_subtitulo(t: T, tipo: str | None) -> str | None:
    """Ruling 4: Garmin's generic "other" bucket names nothing useful next to the date ("Otros ·
    14 de noviembre" says less than the date alone, since the session's own name already carries
    the real subject) -- the subtitle shows the date only for it. Any other type, known or not,
    keeps today's label."""
    if tipo == entreno_sesion.TIPO_GENERICO:
        return None
    return _tipo_label(t, tipo)


def _nombre_generico(a: dict) -> str | None:
    """Ruling 4: the session's own name, lowercased, when Garmin typed it generically as "other"
    -- entreno_sesion.comparaciones groups the history by this same name. None for any other
    type, and for a nameless generic session (nothing to fall back to)."""
    if a.get("type") != entreno_sesion.TIPO_GENERICO:
        return None
    nombre = (a.get("name") or "").strip()
    return nombre.lower() if nombre else None


def _tipo_sin_historial(t: T, a: dict) -> str | None:
    """The {tipo} text for the "not enough sessions" line: the generic bucket's own name
    (lowercased, ruling 4) instead of "Otros"; any other type keeps today's label untouched
    (ruling 3's own "as today" clause for the no-history case)."""
    if a.get("type") == entreno_sesion.TIPO_GENERICO:
        return _nombre_generico(a)
    return _tipo_label(t, a.get("type"))


def _tipo_comparacion(t: T, a: dict) -> str | None:
    """The {tipo} text for the new comparison caption (ruling 3): the generic bucket's own name
    (lowercased, ruling 4); the translated type label otherwise, lowercased mid-sentence in
    Spanish, kept as the app's own label casing in English."""
    if a.get("type") == entreno_sesion.TIPO_GENERICO:
        return _nombre_generico(a)
    label = _tipo_label(t, a.get("type"))
    return label.lower() if (label and t.lang == "es") else label


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


def _figura_ctx(t: T, label_key: str, valor, habitual, unidad: str) -> dict | None:
    """Ruling 3: the figure's own value, and -- separately -- its "habitual: ..." line when a
    comparison exists. The comparison sentence itself is said once, above all three tiles
    (entreno_sesion_view's own comparacion_titulo), never repeated per tile."""
    if valor is None:
        return None
    valor_txt = f"{t.num(valor)} {unidad}" if unidad else t.num(valor)
    habitual_txt = None
    if habitual is not None:
        habitual_valor = f"{t.num(habitual)} {unidad}" if unidad else t.num(habitual)
        habitual_txt = t("entreno_sesion.habitual", habitual=habitual_valor)
    return {"label": t(label_key), "texto": valor_txt, "habitual": habitual_txt}


def _figuras_ctx(t: T, a: dict, comp: dict) -> list[dict]:
    out = []
    for key, label_key, unidad in (
        ("avg_hr", "entreno_sesion.avg_hr", t("unit.bpm")),
        ("max_hr", "entreno_sesion.max_hr", t("unit.bpm")),
        ("calories", "entreno_sesion.calorias", "kcal"),
    ):
        f = _figura_ctx(t, label_key, a.get(key), comp["valores"].get(key), unidad)
        if f is not None:
            out.append(f)
    return out


def _peso_txt(t: T, s: dict) -> str | None:
    """Ruling 6: the app's own number formatting -- no decimal at all on a whole value (the same
    trim body/nutriente_objetivo.py:37's _fmt_cantidad already applies to an amount), one on a
    fractional one (40 kg, 42.5 kg, never "40,0 kg")."""
    if s["weight_value"] is None:
        return None
    unidad = s["weight_unit"] or "kg"
    valor = s["weight_value"]
    decimales = 0 if float(valor).is_integer() else 1
    return f"{t.num(valor, decimales)} {unidad}"


def _set_txt(t: T, reps, peso_txt: str | None) -> str | None:
    """Ruling 6: "8 × 40 kg" (reps × weight); a bodyweight set with no weight reads "8 rep"/"8
    reps" instead (entreno_sesion.reps_solo) -- never a bare "8" with no sign of what it counts."""
    if reps is not None and peso_txt is not None:
        return f"{t.num(reps)} × {peso_txt}"
    if reps is not None:
        return t("entreno_sesion.reps_solo", n=t.num(reps))
    return peso_txt


def _exercise_display(nombre: str | None) -> str:
    """Ruling 6: a capital first letter for display only -- the stored garmin_exercise/
    person_exercise name is never touched."""
    texto = nombre or "—"
    return texto[:1].upper() + texto[1:]


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
            "exercise": _exercise_display(s["exercise"]),
            "detalle": _set_txt(t, s["reps"], _peso_txt(t, s)),
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


# BODY-PLAN-FASES-5 ruling 3: the 0-10 scale's own options, read once off db.py's own named
# constant -- never a second hand-typed "10" in this module.
_ESCALA_OPCIONES = list(range(db.DOLOR_ESCALA_MIN, db.DOLOR_ESCALA_MAX + 1))


def _dolor_ctx(t: T, conn, person_id: int, day: str, a: dict, sets: list[dict]) -> dict | None:
    """Ruling 2/3: the session's own pain card, or None when no exercise of this session loads an
    open zona -- nothing changes on the screen then. `filas` carries each relevant exercise with
    its OWN previously-saved durante/despues/movimiento (None the first time), so editing later is
    the same form, prefilled (ruling 3's own "editing later is the same form prefilled")."""
    zonas_dia = db.zonas_en_recuperacion(conn, person_id, day)
    relevantes = entreno_sesion.ejercicios_con_zona_abierta(sets, zonas_dia)
    if not relevantes:
        return None
    guardado = {f["ejercicio"]: f for f in db.dolor_de_sesion(conn, person_id, a["sets_source"], a["sets_source_id"])}
    zonas_tarjeta = [z for z in anatomia.CATEGORIA_ENUM if z in {e["zona"] for e in relevantes}]
    zona_txt = _lista_natural(t, [t("entreno.categoria." + z) for z in zonas_tarjeta])
    filas = []
    for e in relevantes:
        prev = guardado.get(e["ejercicio"])
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1/2: the visible exercise text and each field's own
        # accessible name both read through _exercise_display -- the same display form Series
        # already uses on this screen (ruling 2) -- while the hidden input below keeps posting the
        # raw stored `ejercicio` key unchanged. Three fields share one row, so without the exercise
        # named in each one's own aria-label a screen reader would hear "During" / "After" / the
        # movement question repeated identically for every exercise on the card.
        nombre = _exercise_display(e["ejercicio"])
        filas.append({
            "ejercicio": e["ejercicio"], "ejercicio_display": nombre, "zona": e["zona"],
            "durante": prev["dolor_durante"] if prev else None,
            "despues": prev["dolor_despues"] if prev else None,
            "movimiento": (prev["movimiento"] if prev else None) or "",
            "durante_aria": t("entreno_sesion.dolor_campo_aria",
                              campo=t("entreno_sesion.dolor_durante_label"), ejercicio=nombre),
            "despues_aria": t("entreno_sesion.dolor_campo_aria",
                              campo=t("entreno_sesion.dolor_despues_label"), ejercicio=nombre),
            "movimiento_aria": t("entreno_sesion.dolor_campo_aria",
                                 campo=t("entreno_sesion.dolor_movimiento_label"), ejercicio=nombre),
        })
    return {"titulo": t("entreno_sesion.dolor_titulo", zona=zona_txt), "filas": filas}


def _parse_escala(raw: str | None) -> int | None:
    """A select's own value: '' (ruling 3's own "each optional") is None; anything outside
    db.DOLOR_ESCALA_MIN..MAX, or not a plain integer, is also None rather than a 500 -- a hand-
    crafted POST is the only way to reach that path, and the table's own CHECK refuses it either
    way."""
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if db.DOLOR_ESCALA_MIN <= v <= db.DOLOR_ESCALA_MAX else None


@router.get("/day/{day}/entreno/{source_id}")
@comun._cierra_conexion
def entreno_sesion_view(day: str, source_id: str, request: Request,
                         person_id: int = Depends(identity.current_person_id)):
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
    tipo_label = _tipo_subtitulo(t, a.get("type"))
    tipo_sin_historial = _tipo_sin_historial(t, a)
    tipo_comp = _tipo_comparacion(t, a)
    comp = entreno_sesion.comparaciones(conn, person_id, a)
    figuras = _figuras_ctx(t, a, comp)
    sin_historial = None
    comparacion_titulo = None
    if tipo_sin_historial and comp["n"] < entreno_sesion.HISTORIAL_MIN and any(
            a.get(k) is not None for k in entreno_sesion.COMPARABLE_KEYS):
        sin_historial = t("entreno_sesion.sin_historial", tipo=tipo_sin_historial)
    elif tipo_comp and any(f["habitual"] for f in figuras):
        comparacion_titulo = t("entreno_sesion.comparacion_titulo", tipo=tipo_comp)

    es_fuerza = a.get("type") == "strength_training"
    sets = entreno_sesion.series(conn, person_id, a["sets_source"], a["sets_source_id"]) if es_fuerza else []
    curva_ctx = entreno_sesion.curva(conn, person_id, d.isoformat(), zona, a["start_local"], a["duration_s"], sets)

    recuperacion_row = conn.execute(
        "SELECT minutos, motivo FROM recuperacion_actividad WHERE person_id = ? AND activity_source = ? AND "
        "activity_source_id = ? AND modelo_version = ?",
        (person_id, a["source"], a["source_id"], recuperacion.MODELO["version"])).fetchone()

    dolor = _dolor_ctx(t, conn, person_id, d.isoformat(), a, sets) if es_fuerza else None
    dolor_msg = t("entreno_sesion.dolor_guardado") if request.query_params.get("dolor_guardado") == "1" else None

    return comun._render(
        "entreno_sesion.html", t=t, tab="today", day=d.isoformat(), source_id=source_id, turnos=turnos_on,
        nombre=_nombre(t, a), tipo_label=tipo_label, date_long=t.long_date(d),
        detalle=_detalle_cabecera(t, a), figuras=figuras, sin_historial=sin_historial,
        comparacion_titulo=comparacion_titulo,
        es_fuerza=es_fuerza, sets=_series_ctx(t, sets, curva_ctx) if es_fuerza else [],
        pulso_svg=_curva_svg(t, sets, curva_ctx) if curva_ctx is not None else None,
        pulso_pendiente=t("entreno_sesion.curva_pendiente"),
        recovery=_recovery_line(t, recuperacion_row),
        dolor=dolor, dolor_msg=dolor_msg, escala=_ESCALA_OPCIONES,
    )


@router.post("/day/{day}/entreno/{source_id}/dolor")
@comun._cierra_conexion_async
async def entreno_dolor_registrar(day: str, source_id: str, request: Request,
                                   person_id: int = Depends(identity.current_person_id)):
    """Ruling 3: the session screen's own card -- upserts durante/despues/movimiento for every
    exercise the card named, through db.registrar_dolor_entreno, the SAME writer the assistant's
    own `registrar_dolor` proposal applies through (ruling 5). A day/source_id this person has no
    activity under 404s, same as the GET route above."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(404)
    conn, _, _ = comun._ctx(person_id)
    a = entreno_sesion.actividad(conn, person_id, d.isoformat(), source_id)
    if a is None:
        raise HTTPException(404)
    f = await comun._form(request)
    try:
        n = int(f.get("n") or "0")
    except ValueError:
        n = 0
    filas = []
    for i in range(n):
        ejercicio = f.get(f"ejercicio_{i}")
        zona = f.get(f"zona_{i}")
        if not ejercicio or not zona:
            continue
        filas.append({
            "ejercicio": ejercicio, "zona": zona,
            "dolor_durante": _parse_escala(f.get(f"durante_{i}")),
            "dolor_despues": _parse_escala(f.get(f"despues_{i}")),
            "movimiento": (f.get(f"movimiento_{i}") or "").strip() or None,
        })
    if filas:
        db.registrar_dolor_entreno(conn, person_id, a["day"], a["sets_source"], a["sets_source_id"], filas)
    return RedirectResponse(f"/day/{day}/entreno/{source_id}?dolor_guardado=1{comun._ancla('dolor')}",
                            status_code=303)
