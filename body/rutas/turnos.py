"""Turnos screens (BODY-WEB-ESTRUCTURA-6): the shift import review, the cost table on /shifts, how
it's calculated, and the typed-pattern form -- the last domain moved out of body/web.py. Pure move.
"""
from __future__ import annotations

import html
import math
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import anatomia, baselines, db, identity, patron_turnos, readiness, shift_import, turno_fc, turnos_costo
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


def _turnos_ultima_filas(t: T, defs: dict, metricas: list[dict]) -> list[dict]:
    filas = []
    for m in metricas:
        key = m["metric"]
        d = defs.get(key, {})
        amount = t.minutes(m["diff"]) if comun._is_minutes(key) else t.num(m["diff"], 1)
        unit = comun._unit(t, d)
        veredicto = None
        if m["veredicto"] == "mejor":
            veredicto = t("shifts.ultima.word.mejor")
        elif m["veredicto"] == "peor":
            veredicto = t("shifts.ultima.word.peor")
        filas.append({"label": t("metric." + key),
                      "valor": f"{amount}" + (f" {unit}" if unit else ""),
                      "direccion": t("shifts.word." + m["direccion"]),
                      "veredicto": veredicto})
    return filas


def _singular_key(base: str, n: int) -> str:
    return f"{base}_one" if n == 1 else base


def _turnos_ultima_recuperacion(t: T, u: dict) -> str:
    if u["recuperacion_dias"] is None:
        return t("shifts.ultima.recuperacion_sin_dato")
    d = u["recuperacion_dias"]
    if u["recuperacion_mediana"] is None:
        return t(_singular_key("shifts.ultima.recuperacion_sin_anteriores", d), d=d)
    m = u["recuperacion_mediana"]
    mediana = t(_singular_key("shifts.ultima.recuperacion_mediana", m), m=m)
    return t(_singular_key("shifts.ultima.recuperacion", d), d=d, mediana=mediana)


def _turnos_ultima_ctx(t: T, defs: dict, resumen: dict) -> dict | None:
    """BODY-TURNOS-REFERENCIA-NOCHE-1: the last complete night block against the person's own
    earlier night blocks -- None when there is no completed block at all; a gap card when no metric
    reached MIN_BLOQUES_PREVIOS; otherwise the metric rows and the recovery line."""
    u = resumen.get("ultima_semana")
    if u is None:
        return None
    if u["motivo"] is not None:
        return {"gap": True, "faltan": t("shifts.ultima.faltan", n=turnos_costo.MIN_BLOQUES_PREVIOS)}
    return {
        "gap": False,
        "sub": t("shifts.ultima.sub", inicio=comun._short_date(t, date.fromisoformat(u["inicio"])),
                  fin=comun._short_date(t, date.fromisoformat(u["fin"])), n=u["n"]),
        "filas": _turnos_ultima_filas(t, defs, u["metricas"]),
        "recuperacion": _turnos_ultima_recuperacion(t, u),
    }


_PERFIL_WIDTH, _PERFIL_HEIGHT = 320, 128
_PERFIL_TOP = 10
_PERFIL_LABEL_DY = 13      # baseline offset below the plot area for the x-axis labels
_PERFIL_BOTTOM_PAD = 6
_PERFIL_CHAR_W = 6.0       # approx glyph width (px) at this chart's axis font-size (10px, class
                           # "tick") -- an approximation from character count, since real glyph
                           # metrics are not available server-side; generous enough that two labels'
                           # approximate boxes never touch
_PERFIL_LABEL_GAP = 4.0    # minimum clear gap kept between two adjacent x-axis label boxes
_PERFIL_GUTTER_PAD = 6.0   # clearance between the widest y-tick text and the plot area
_PERFIL_EDGE_PAD = 2.0     # inside the viewBox's own left/right edge
_PERFIL_TICK_DIVISIONS = 4  # target divisions for the nice-number y-tick step


def _perfil_offset_palabra(t: T, offset: str) -> str:
    return t("shifts.perfil.offset." + offset)


def _perfil_vuelta_frase(t: T, perfil: dict) -> str:
    if perfil["vuelta"] is None:
        return t("shifts.perfil.vuelta_ausente")
    return t("shifts.perfil.vuelta", offset=_perfil_offset_palabra(t, perfil["vuelta"]))


def _perfil_caption(t: T, perfil: dict) -> str:
    """BODY-TURNOS-PERFIL-DIA-1 ruling 3: built from the data every time, never a fixed sentence --
    `punto` and the extreme offset follow whichever direction turnos_costo.perfil_por_dia found
    worse for this metric (mas_bajo/mas_alto), and the closing clause follows whether (and when) the
    metric came back inside the band. BODY-TURNOS-PERFIL-DIA-1-M fix 1: the ONLY builder -- both the
    <svg>'s aria-label and the visible <p> under the chart call this, never a second copy."""
    if "mas_bajo" in perfil:
        punto, extremo = t("shifts.perfil.punto.bajo"), perfil["mas_bajo"]
    else:
        punto, extremo = t("shifts.perfil.punto.alto"), perfil["mas_alto"]
    key = _singular_key("shifts.perfil.caption", perfil["noches_tipicas"])
    return t(key, n=perfil["noches_tipicas"], punto=punto, offset=_perfil_offset_palabra(t, extremo),
              vuelta=_perfil_vuelta_frase(t, perfil))


def _perfil_eje_label(t: T, offset: str, n: int) -> str:
    if offset.startswith("post_"):
        return "+" + offset.split("_")[1]
    return t("shifts.perfil.eje." + offset, n=n)


def _perfil_tick(t: T, v: float, unit: str = "") -> str:
    v = int(round(v))
    texto = "0" if v == 0 else ("+" if v > 0 else "-") + t.num(abs(v), 0)
    return f"{texto} {unit}" if unit else texto


def _nice_step(raw: float) -> float:
    """The smallest 'nice' step (1, 2, 5 or 10 times a power of ten) at least as large as `raw` --
    the usual axis-tick step, so a y-tick value rounds to a whole number a reader expects, instead
    of carrying a decimal ("+10.8") nobody asked for."""
    if raw <= 0:
        return 1.0
    exp = math.floor(math.log10(raw))
    base = 10 ** exp
    for m in (1, 2, 5, 10):
        step = m * base
        if raw <= step:
            return step
    return 10 * base


def _perfil_ticks(rango_bajo: float, rango_alto: float) -> tuple[float, float]:
    """The bottom and top y-tick VALUES, both rounded to the nearest multiple of one shared nice
    step (BODY-TURNOS-PERFIL-DIA-1-M fix 2). 0 is always the middle tick (rango_bajo <= 0 <=
    rango_alto by construction) and is never rounded away."""
    step = _nice_step((rango_alto - rango_bajo) / _PERFIL_TICK_DIVISIONS) or 1.0
    bajo = round(rango_bajo / step) * step if rango_bajo else 0.0
    alto = round(rango_alto / step) * step if rango_alto else 0.0
    return bajo, alto


def _label_px(s: str) -> float:
    return len(s) * _PERFIL_CHAR_W


def _perfil_svg(t: T, perfil: dict, caption: str, unit: str, *, color_cls: str = "body") -> str:
    """Server-rendered SVG (Body's CSP forbids inline style/script): the band as a light rect, the
    night offsets shaded, a line through each offset's own mean with a point per offset, an x-axis
    label under each point -- drawn INSIDE the svg, centred on that point's own x by construction
    (BODY-TURNOS-PERFIL-DIA-1-M fix 2, replacing the independent HTML label row that never lined up
    with it) -- and at most three y ticks in a left gutter the plotted line and its points never
    enter: whole numbers, on one shared nice step (`_perfil_ticks`), the unit carried once on
    whichever tick is not zero.

    The chart's own width grows past _PERFIL_WIDTH (never shrinks below it) only as far as the
    widest adjacent pair of x-axis labels needs, so two labels' approximate boxes -- `_label_px`, an
    approximation from character count -- never overlap, whatever language or offsets are in play;
    the same gutter/inset arithmetic keeps the first and last points and their labels fully inside
    the viewBox (fix 3)."""
    offsets = perfil["offsets"]
    if len(offsets) < 2:
        return ""
    height = _PERFIL_HEIGHT
    banda = perfil["banda"]
    vals = [o["media"] for o in offsets]
    rango_bajo = min(min(vals), banda["bajo"], 0.0)
    rango_alto = max(max(vals), banda["alto"], 0.0)
    pad = (rango_alto - rango_bajo) * 0.15 or 1.0
    lo, hi = rango_bajo - pad, rango_alto + pad
    top = _PERFIL_TOP
    h = height - top - _PERFIL_LABEL_DY - _PERFIL_BOTTOM_PAD
    label_y = top + h + _PERFIL_LABEL_DY

    def y(v: float) -> float:
        return top + h - (v - lo) / (hi - lo) * h

    n = perfil["noches_tipicas"]
    labels = [_perfil_eje_label(t, o["offset"], n) for o in offsets]
    tick_bajo, tick_alto = _perfil_ticks(rango_bajo, rango_alto)
    texto_alto = _perfil_tick(t, tick_alto, unit if tick_alto != 0 else "")
    texto_bajo = _perfil_tick(t, tick_bajo, unit if tick_alto == 0 and tick_bajo != 0 else "")
    texto_cero = _perfil_tick(t, 0.0, unit if tick_alto == 0 and tick_bajo == 0 else "")
    gutter = _PERFIL_EDGE_PAD + max(_label_px(s) for s in (texto_alto, texto_cero, texto_bajo)) + _PERFIL_GUTTER_PAD

    left_half, right_half = _label_px(labels[0]) / 2, _label_px(labels[-1]) / 2
    plot_left = gutter + left_half + _PERFIL_EDGE_PAD
    pitch = max((_label_px(a) / 2 + _label_px(b) / 2 + _PERFIL_LABEL_GAP
                 for a, b in zip(labels, labels[1:])), default=0.0)
    min_width = plot_left + pitch * (len(offsets) - 1) + right_half + _PERFIL_EDGE_PAD
    width = max(_PERFIL_WIDTH, min_width)
    plot_right = width - right_half - _PERFIL_EDGE_PAD
    step = (plot_right - plot_left) / (len(offsets) - 1)

    def cx(i: int) -> float:
        return plot_left + i * step

    out = [f'<rect x="0" y="{y(banda["alto"]):.1f}" width="{width:.1f}" '
           f'height="{max(1.0, y(banda["bajo"]) - y(banda["alto"])):.1f}" class="c-inset"/>']
    for i, o in enumerate(offsets):
        if o["offset"] in ("noche_1", "noche_ultima"):
            out.append(f'<rect x="{max(0.0, cx(i) - step / 2):.1f}" y="0" width="{step:.1f}" '
                       f'height="{height:.1f}" class="c-night op-18"/>')
    out.append(f'<line x1="0" y1="{y(0):.1f}" x2="{width:.1f}" y2="{y(0):.1f}" class="stroke-line" '
               f'stroke-width="1" stroke-dasharray="2 3"/>')
    line_pts = " ".join(f"{cx(i):.1f},{y(o['media']):.1f}" for i, o in enumerate(offsets))
    out.append(f'<polyline fill="none" class="stroke-{color_cls}" stroke-width="2.2" '
               f'vector-effect="non-scaling-stroke" stroke-linejoin="round" points="{line_pts}"/>')
    for i, o in enumerate(offsets):
        out.append(f'<circle cx="{cx(i):.1f}" cy="{y(o["media"]):.1f}" r="3" class="c-{color_cls}"/>')
    for i, label in enumerate(labels):
        out.append(f'<text x="{cx(i):.1f}" y="{label_y:.1f}" text-anchor="middle" '
                   f'class="tick">{html.escape(label)}</text>')
    if tick_bajo != 0:
        out.append(f'<text x="{_PERFIL_EDGE_PAD:.1f}" y="{y(tick_bajo) - 3:.1f}" class="tick">{texto_bajo}</text>')
    out.append(f'<text x="{_PERFIL_EDGE_PAD:.1f}" y="{y(0) - 3:.1f}" class="tick">{texto_cero}</text>')
    if tick_alto != 0:
        out.append(f'<text x="{_PERFIL_EDGE_PAD:.1f}" y="{y(tick_alto) + 9:.1f}" class="tick">{texto_alto}</text>')
    aria = html.escape(caption)
    return (f'<svg viewBox="0 0 {width:.1f} {height}" width="100%" height="120" role="img" aria-label="{aria}" '
            f'preserveAspectRatio="none" class="chart-svg">{"".join(out)}</svg>')


def _perfil_hrv_ctx(t: T, perfil: dict | None, defs: dict) -> dict | None:
    """BODY-TURNOS-PERFIL-DIA-1 ruling 3: no chart, no empty frame, when there is no profile yet."""
    if perfil is None or perfil.get("motivo") is not None:
        return None
    caption = _perfil_caption(t, perfil)
    unit = comun._unit(t, defs[perfil["metric"]])
    svg = _perfil_svg(t, perfil, caption, unit)
    if not svg:
        return None
    return {
        "svg": svg,
        "caption": caption,
        "n_bloques": t("shifts.perfil.n_bloques", k=perfil["n_bloques"]),
    }


# BODY-TURNO-FC-1: the two words `t("shifts.pulso." + direccion)` resolves to -- read by
# tests/test_i18n_paridad.py's own dynamic-prefix coverage, not hand-typed there.
_PULSO_DIRECCION_WORDS = ("bajo", "alto")
_PULSO_KIND_ORDER = ["morning", "afternoon", "night", "day", "other"]


def _turno_fc_direccion(diff: float) -> str | None:
    """None (read as "about the same") below MODELO's own noise floor; otherwise which side of the
    days-off reference this kind's own median falls on."""
    if abs(diff) < turno_fc.MODELO["ruido_min_diferencia_bpm"]:
        return None
    return "bajo" if diff > 0 else "alto"


def _turno_fc_kind_linea(t: T, kind: str, data: dict) -> str:
    etiqueta = t("shift.kind." + kind)
    if data.get("motivo") == "pocos_datos":
        return t("shifts.pulso.pocos_datos", kind=etiqueta, n=data["n"])
    fc, n = t.num(data["fc_mediana"]), data["n"]
    if data["referencia"] is None:
        return t("shifts.pulso.sin_referencia", kind=etiqueta, fc=fc, n=n)
    direccion = _turno_fc_direccion(data["diff"])
    if direccion is None:
        return t("shifts.pulso.linea_igual", kind=etiqueta, fc=fc, n=n)
    return t("shifts.pulso.linea", kind=etiqueta, fc=fc, diff=t.num(abs(data["diff"])),
             direccion=t("shifts.pulso." + direccion), n=n)


def _turno_fc_sueno_lineas(t: T, data: dict) -> list[str]:
    """Only when BOTH the short-night and normal-night groups reach MODELO's own min_n_grupo --
    a split with one side is not a comparison -- as one line naming both."""
    sleep = data.get("sleep", {})
    corta, normal = sleep.get("corta"), sleep.get("normal")
    if corta is None or normal is None:
        return []
    if corta.get("motivo") == "pocos_datos" or normal.get("motivo") == "pocos_datos":
        return []
    return [t("shifts.pulso.sueno_split", fc_corta=t.num(corta["fc_mediana"]), n_corta=corta["n"],
              fc_normal=t.num(normal["fc_mediana"]), n_normal=normal["n"])]


def _turno_fc_hallazgo_linea(t: T, h: dict) -> str:
    direccion = _turno_fc_direccion(h["diff"])
    return t("shifts.pulso.hallazgo", kind=t("shift.kind." + h["kind"]), diff=t.num(abs(h["diff"])),
             direccion=t("shifts.pulso." + direccion))


def _turno_fc_ctx(t: T, est: dict) -> dict | None:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 rulings 3-6: one line per shift kind with data, the
    previous-night's-sleep split inside it, the top findings, and the published-schedule note --
    None (section hidden, same convention as turnos_costo's own costo_vacio) when this person has
    no turno_fc rows at all yet."""
    kinds = est["kinds"]
    if not kinds:
        return None
    filas = []
    for kind in _PULSO_KIND_ORDER:
        if kind not in kinds:
            continue
        data = kinds[kind]
        filas.append({"linea": _turno_fc_kind_linea(t, kind, data),
                      "sueno": _turno_fc_sueno_lineas(t, data) if "motivo" not in data else []})
    hallazgos = [_turno_fc_hallazgo_linea(t, h) for h in est["hallazgos"]]
    return {"filas": filas, "hallazgos": hallazgos, "nota": t("shifts.pulso.horario_publicado")}


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
            result_msg += " " + t(_singular_key("shifts.confirm_all_kept", kept), n=kept)
    today_local = comun._today_local(person)
    resumen = turnos_costo.resumen(conn, person_id, hoy=today_local)
    strip, strip_legend = _turnos_strip(conn, person_id, today_local, t)
    defs = comun._defs(conn)
    filas = _turnos_filas(t, defs, resumen)
    # BODY-SHIFTS-REDUNDANT-EMPTY-TABLE-1: the hero card above already names the one reason there is
    # no estimate (shifts.reason.*) -- five more rows each saying "sin datos del reloj" would only
    # repeat it. Drop the whole card when every row is empty; a partial row (at least one figure) is
    # informative on its own and keeps rendering exactly as before.
    costo_vacio = all(f["href"] is None for f in filas)
    ultima = _turnos_ultima_ctx(t, defs, resumen)
    # BODY-TURNO-FC-1: pure reads over the already-derived `turno_fc` table (two cheap SELECTs,
    # body/turno_fc.py's own estudio()) -- never a live recompute on this page render.
    pulso = _turno_fc_ctx(t, turno_fc.estudio(conn, person_id))
    perfil_hrv = _perfil_hrv_ctx(t, resumen.get("perfil_hrv"), defs)
    cuerpo_ctx = {"vistas": anatomia.contexto(t, "turnos", frozenset({"cerebro"})),
                  "caption": t("cuerpo.caption.turnos"), "recorte": "cabeza"}
    costo_ctx = {"hero_dias": resumen["dias_recuperacion"], "motivo": resumen["motivo"],
                 "filas": filas, "costo_vacio": costo_vacio, "strip": strip, "strip_legend": strip_legend,
                 "ultima": ultima, "pulso": pulso, "perfil_hrv": perfil_hrv, "cuerpo": cuerpo_ctx}
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
    own dict, without changing its existing keys. `kept_note` (BODY-TURNOS-PERFIL-DIA-1-M fix 4) is
    the singular/plural sentence for `prev['kept_days']`'s own count, through the same `_singular_key`
    mechanism `shifts.perfil.caption` already uses -- None when there is nothing kept to report."""
    kept_n = len(prev["kept_days"])
    return {**prev,
            "from_date": comun._short_date(t, date.fromisoformat(prev["first_day"])) if prev["first_day"] else None,
            "to_date": comun._short_date(t, date.fromisoformat(prev["last_day"])) if prev["last_day"] else None,
            "kept_note": t(_singular_key("shifts.kept_note", kept_n), n=kept_n) if kept_n else None}


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
