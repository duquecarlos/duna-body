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

from .. import anatomia, checkin_sesion, db, entreno_sesion, esfuerzo_sesion, history, identity, recuperacion
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


# BODY-PARTIDO-1 ruling 6: zone 1 (quietest) through zone 5 (most intense) mapped onto the app's
# existing fixed-colour palette (body/static/body.css's own BODY-ESTILOS-1 classes) -- that file is
# not in this slice's touches, so no new class is added; the ramp runs from the palest/neutral
# entry to the warmest one already in the palette.
_ZONA_CLASES = {1: "c-inset", 2: "c-other", 3: "c-info", 4: "c-body", 5: "c-amber"}


def _esfuerzo_zonas_svg(t: T, filas: list[tuple[int, float]], total_min: float) -> str:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: the zone breakdown's own stacked bar AND its legend
    -- same shape as body/templates/day.html's own sleep-stage bar/legend pair (<svg class="stage">
    viewBox 0 0 100 h, one <rect> per segment carrying a <title>, then one `div.legend` of
    `<span><i class="{zone class}"></i>{label}</span>` entries) -- replacing the heavy boxed
    `ul.list` the zones used to render as. `_ZONA_CLASES`' own classes (body.css's fixed-colour
    palette, BODY-ESTILOS-1) already set `background` directly, not scoped to `.stage` or
    `.legend`, so the same class ties the bar's colour to its legend swatch with no new CSS."""
    if not total_min:
        return ""
    shapes, legend_items = [], []
    x = 0.0
    for n, mins in filas:
        w = 100 * mins / total_min
        label = html.escape(t("entreno_sesion.esfuerzo.zona", n=n, dur=t.minutes(mins)))
        cls = _ZONA_CLASES[n]
        shapes.append(f'<rect x="{x:.2f}" y="0" width="{w:.2f}" height="10" class="{cls}">'
                      f'<title>{label}</title></rect>')
        legend_items.append(f'<span><i class="{cls}"></i>{label}</span>')
        x += w
    svg = f'<svg class="stage" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">{"".join(shapes)}</svg>'
    return svg + '<div class="legend">' + "".join(legend_items) + "</div>"


_ESFUERZO_PALABRA = {
    "zona_alta": {"mas": "entreno_sesion.esfuerzo.zona_alta_mas", "igual": "entreno_sesion.esfuerzo.zona_alta_como",
                  "menos": "entreno_sesion.esfuerzo.zona_alta_menos"},
    "caida": {"mas": "entreno_sesion.esfuerzo.recuperacion_mas", "igual": "entreno_sesion.esfuerzo.recuperacion_como",
              "menos": "entreno_sesion.esfuerzo.recuperacion_menos"},
}


def _esfuerzo_ctx(t: T, tarjeta: dict, tipo_comp: str | None) -> dict:
    """Ruling 6: the effort card's own context -- every row present only when its figure exists.
    `tarjeta` is esfuerzo_sesion.tarjeta()'s own return (never None here -- the caller already
    gated the card's presence on that). ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: no second peak
    figure here -- the page's own max-HR comparison already covers that ground; ruling 2: no
    `tipo_comp`-keyed heading repeated inside the card, its comparison rows read on their own.

    Ruling 5: `sin_historial` -- the card carries at least one own figure (zona_alta or
    recuperacion) but NO comparison line could be made for either, because both are under
    HISTORIAL_MIN earlier comparables. When some figures compare and others do not, the compared
    lines show and this sentence stays None (the existing `comparacion` list is then non-empty)."""
    esf, zon, clasificacion = tarjeta["esfuerzos"], tarjeta["zonas"], tarjeta["clasificacion"]
    bpm = t("unit.bpm")

    zonas_svg = zona_alta_txt = None
    if zon is not None:
        filas = [(n, zon["minutos"][n]) for n in range(1, 6) if zon["minutos"][n] > 0]
        if filas:
            zonas_svg = _esfuerzo_zonas_svg(t, filas, zon["total_min"])
        if zon["alta_share"] is not None:
            zona_alta_txt = t("entreno_sesion.esfuerzo.zona_alta", pct=t.num(round(zon["alta_share"] * 100)))

    esfuerzos_txt = None
    if esf["suficiente"] and esf["n_picos"] >= 1:
        esfuerzos_txt = t("entreno_sesion.esfuerzo.esfuerzos", n=t.num(esf["n_picos"]))
    recuperacion_txt = None
    if esf["suficiente"] and esf["recuperacion"] is not None:
        recuperacion_txt = t("entreno_sesion.esfuerzo.recuperacion",
                             valor=f"{t.num(round(esf['recuperacion']))} {bpm}")
    pocos_datos_txt = (t("entreno_sesion.esfuerzo.pocos_datos")
                       if tarjeta["tiene_detalle"] and not esf["suficiente"] else None)

    comparacion = [t(_ESFUERZO_PALABRA[figura][codigo]) for figura in ("zona_alta", "caida")
                   if (codigo := clasificacion.get(figura)) is not None]
    sin_historial_txt = None
    if not comparacion and tipo_comp and (zona_alta_txt is not None or recuperacion_txt is not None):
        sin_historial_txt = t("entreno_sesion.esfuerzo.sin_historial", tipo=tipo_comp)

    return {
        "titulo": t("entreno_sesion.esfuerzo.titulo"), "zonas_svg": zonas_svg,
        "zona_alta": zona_alta_txt, "esfuerzos": esfuerzos_txt, "recuperacion": recuperacion_txt,
        "pocos_datos": pocos_datos_txt, "comparacion": comparacion, "sin_historial": sin_historial_txt,
        "sin_detalle": None,
    }


# BODY-PLAN-FASES-5 ruling 3: the 0-10 scale's own options, read once off db.py's own named
# constant -- never a second hand-typed "10" in this module.
_ESCALA_OPCIONES = list(range(db.DOLOR_ESCALA_MIN, db.DOLOR_ESCALA_MAX + 1))

# BODY-PARTIDO-2 ruling 3: the check-in's own 1-5 scale labels only ends 1/3/5 (2 and 4 show their
# bare number) -- a fixed i18n key per end, never a dynamically-built t() prefix (tests/
# test_i18n_paridad.py's own scan only flags string concatenation inside the call itself).
_ESCALA_CHECKIN_LABEL = {1: "checkin_sesion.escala.1", 3: "checkin_sesion.escala.3",
                         5: "checkin_sesion.escala.5"}


def _escala_checkin_etiqueta(t: T, n: int) -> str:
    clave = _ESCALA_CHECKIN_LABEL.get(n)
    return t(clave) if clave else t.num(n)


def _checkin_ctx(t: T, conn, person_id: int, a: dict, forzar_editar: bool) -> dict:
    """Ruling 2/4: the session's own post-session check-in card -- the person's OWN stored set for
    this (tipo, nombre_norm) if one exists, else the matched default (checkin_sesion.set_para),
    never copied into their own rows until the first save (ruling 2's own "not on view"). Shows
    each answered question as plain text with a "Cambiar respuestas" link once any answer exists,
    unless `?checkin_editar=1` asks for the pills again (the same form, pre-selected)."""
    tipo = a.get("type") or ""
    nombre_norm = entreno_sesion._normaliza_nombre(a.get("name"))
    fila = db.set_de_persona(conn, person_id, tipo, nombre_norm)
    preguntas = fila if fila is not None else checkin_sesion.set_para(a.get("type"), a.get("name"))
    respuestas = db.respuestas_de_sesion(conn, person_id, a["sets_source"], a["sets_source_id"])
    filas = []
    for p in preguntas:
        clave, tp = p["clave"], p["tipo_pregunta"]
        valor = respuestas.get(clave)
        texto = t(p["texto"])
        fila_ctx = {"clave": clave, "tipo_pregunta": tp, "texto": texto}
        if tp == checkin_sesion.KIND_UNICA:
            opciones = [{"clave": o["clave"], "texto": t(o["texto"])} for o in p["opciones"]]
            fila_ctx["opciones"] = opciones
            fila_ctx["respuesta_txt"] = next((o["texto"] for o in opciones if o["clave"] == valor), None)
        elif tp == checkin_sesion.KIND_ESCALA:
            fila_ctx["opciones"] = [{"clave": str(n), "texto": _escala_checkin_etiqueta(t, n)}
                                     for n in range(1, 6)]
            fila_ctx["respuesta_txt"] = _escala_checkin_etiqueta(t, int(valor)) if valor else None
        else:  # si_no
            si_txt, no_txt = t("checkin_sesion.si"), t("checkin_sesion.no")
            fila_ctx["opciones"] = [{"clave": "si", "texto": si_txt}, {"clave": "no", "texto": no_txt}]
            fila_ctx["respuesta_txt"] = si_txt if valor == "si" else (no_txt if valor == "no" else None)
        for o in fila_ctx["opciones"]:
            o["seleccionada"] = o["clave"] == valor
        filas.append(fila_ctx)
    editar = forzar_editar or not respuestas
    return {
        "titulo": t("checkin_sesion.titulo"), "nota": t("checkin_sesion.nota") if not respuestas else None,
        "preguntas": filas, "guardado": bool(respuestas), "editar": editar,
        "guardar_txt": t("checkin_sesion.guardar"), "cambiar_txt": t("checkin_sesion.cambiar"),
    }


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
    propias = [k for k in entreno_sesion.COMPARABLE_KEYS if a.get(k) is not None]
    sin_historial = None
    comparacion_titulo = None
    if tipo_sin_historial and propias and all(
            comp["n"][k] < entreno_sesion.HISTORIAL_MIN for k in propias):
        sin_historial = t("entreno_sesion.sin_historial", tipo=tipo_sin_historial)
    elif tipo_comp and any(f["habitual"] for f in figuras):
        comparacion_titulo = t("entreno_sesion.comparacion_titulo", tipo=tipo_comp)

    es_fuerza = a.get("type") == "strength_training"
    sets = entreno_sesion.series(conn, person_id, a["sets_source"], a["sets_source_id"]) if es_fuerza else []
    curva_ctx = entreno_sesion.curva(conn, person_id, d.isoformat(), zona, a["start_local"], a["duration_s"], sets)
    if curva_ctx is None:
        # BODY-PARTIDO-1 ruling 7: the day-level series is absent -- fall back to the activity's
        # own stored detail, when it has one, rather than the honest "pending" line.
        curva_ctx = esfuerzo_sesion.curva_actividad(conn, person_id, a)

    esfuerzo = None
    if not es_fuerza:
        # BODY-PARTIDO-1 ruling 6: strength sessions keep their own set/rest view untouched.
        tarjeta_esfuerzo = esfuerzo_sesion.tarjeta(conn, person_id, a)
        if tarjeta_esfuerzo is not None:
            esfuerzo = _esfuerzo_ctx(t, tarjeta_esfuerzo, tipo_comp)
        elif a["source"] != "manual" and a["duration_s"] is not None:
            # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: a watch-recorded session with no stored
            # 'detalle'/'zonas' at all still names the gap, rather than showing no effort card
            # at all -- a manual session the watch never recorded (ruling 5's own distinction,
            # entreno_sesion.py:65's "source" check) keeps showing none, exactly as before.
            esfuerzo = {
                "titulo": t("entreno_sesion.esfuerzo.titulo"), "zonas_svg": None, "zona_alta": None,
                "esfuerzos": None, "recuperacion": None, "pocos_datos": None, "comparacion": [],
                "sin_historial": None, "sin_detalle": t("entreno_sesion.esfuerzo.sin_detalle"),
            }

    recuperacion_row = conn.execute(
        "SELECT minutos, motivo FROM recuperacion_actividad WHERE person_id = ? AND activity_source = ? AND "
        "activity_source_id = ? AND modelo_version = ?",
        (person_id, a["source"], a["source_id"], recuperacion.MODELO["version"])).fetchone()

    dolor = _dolor_ctx(t, conn, person_id, d.isoformat(), a, sets) if es_fuerza else None
    dolor_msg = t("entreno_sesion.dolor_guardado") if request.query_params.get("dolor_guardado") == "1" else None
    checkin = _checkin_ctx(t, conn, person_id, a, request.query_params.get("checkin_editar") == "1")

    return comun._render(
        "entreno_sesion.html", t=t, tab="today", day=d.isoformat(), source_id=source_id, turnos=turnos_on,
        nombre=_nombre(t, a), tipo_label=tipo_label, date_long=t.long_date(d),
        detalle=_detalle_cabecera(t, a), figuras=figuras, sin_historial=sin_historial,
        comparacion_titulo=comparacion_titulo,
        es_fuerza=es_fuerza, sets=_series_ctx(t, sets, curva_ctx) if es_fuerza else [],
        pulso_svg=_curva_svg(t, sets, curva_ctx) if curva_ctx is not None else None,
        pulso_pendiente=t("entreno_sesion.curva_pendiente"),
        recovery=_recovery_line(t, recuperacion_row), esfuerzo=esfuerzo,
        dolor=dolor, dolor_msg=dolor_msg, escala=_ESCALA_OPCIONES, checkin=checkin,
    )


@router.post("/day/{day}/entreno/{source_id}/checkin")
@comun._cierra_conexion_async
async def entreno_checkin_registrar(day: str, source_id: str, request: Request,
                                     person_id: int = Depends(identity.current_person_id)):
    """Ruling 4: the session screen's own check-in card -- one POST upserts every answered
    question (an absent or blank field is simply unanswered, ruling 4's own "partial answers are
    allowed"). Ruling 2: copies the matched default into this person's own rows on this, their
    first save for this (tipo, nombre_norm), if they have none yet. A day/source_id this person has
    no activity under 404s, same as the GET route above."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(404)
    conn, _, _ = comun._ctx(person_id)
    a = entreno_sesion.actividad(conn, person_id, d.isoformat(), source_id)
    if a is None:
        raise HTTPException(404)
    tipo = a.get("type") or ""
    nombre_norm = entreno_sesion._normaliza_nombre(a.get("name"))
    preguntas = db.set_de_persona(conn, person_id, tipo, nombre_norm)
    if preguntas is None:
        preguntas = checkin_sesion.set_para(a.get("type"), a.get("name"))
        db.guardar_set_pregunta(conn, person_id, tipo, nombre_norm, preguntas)
    f = await comun._form(request)
    respuestas = {}
    for p in preguntas:
        clave, tp = p["clave"], p["tipo_pregunta"]
        valor = (f.get(clave) or "").strip()
        if not valor:
            continue
        if tp == checkin_sesion.KIND_UNICA:
            if valor in {o["clave"] for o in p["opciones"]}:
                respuestas[clave] = valor
        elif tp == checkin_sesion.KIND_ESCALA:
            if valor in {"1", "2", "3", "4", "5"}:
                respuestas[clave] = valor
        elif valor in ("si", "no"):
            respuestas[clave] = valor
    if respuestas:
        db.registrar_respuestas_sesion(conn, person_id, a["sets_source"], a["sets_source_id"], respuestas)
    return RedirectResponse(f"/day/{day}/entreno/{source_id}{comun._ancla('checkin')}", status_code=303)


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
