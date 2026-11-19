"""Tendencias (BODY-WEB-ESTRUCTURA-7): the `/trends` screen -- range cards, the week table, the
month view and "Ver todas las medidas". Pure move: path, method, templates and bodies identical.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import anatomia, baselines, db, entreno_racha, fuentes, goals, identity, profile, turnos_costo, viajes
from ..catalogo_tendencias import TODAS
from ..desplazamiento_import import FUENTE as _FUENTE_COMMUTE
from ..i18n import T
from . import comun

router = APIRouter()

# BODY-DESPLAZAMIENTO-3 ruling 2: every reason class desplazamiento_import.validar can raise,
# mapped to its own i18n key -- an explicit dict, never a dynamic prefix built from the class name,
# the same choice body/rutas/hoy.py's own _RECOVERY_REASON_KEY already makes for the same reason
# (a new reason code is a one-line addition here, never an unreviewed dynamic key).
_FUENTE_MOTIVO_KEY = {
    "esquema": "fuente.motivo.esquema",
    "dia_malformado": "fuente.motivo.dia_malformado",
    "dia_duplicado": "fuente.motivo.dia_duplicado",
    "dia_futuro": "fuente.motivo.dia_futuro",
    "conteo_invalido": "fuente.motivo.conteo_invalido",
    "computed_at_malformado": "fuente.motivo.computed_at_malformado",
    "computed_at_mixto": "fuente.motivo.computed_at_mixto",
    "demasiado_grande": "fuente.motivo.demasiado_grande",
}


def _fuente_texto(t: T, conn, person_id: int, hoy: date) -> str:
    """BODY-DESPLAZAMIENTO-3 ruling 2: the commute group's one freshness line -- never imported,
    fresh, stale (appended), or the last upload's own refusal reason, in that priority: an error
    is the newest fact about this source, so it replaces the "datos hasta" line rather than
    standing beside it."""
    estado = fuentes.estado_fuente(conn, person_id, _FUENTE_COMMUTE, hoy)
    if estado is None:
        return t("fuente.nunca")
    if estado["error"]:
        return t("fuente.rechazada", motivo=t(_FUENTE_MOTIVO_KEY[estado["error"]]))
    texto = t("fuente.frescura", fecha=comun._short_date(t, date.fromisoformat(estado["ultimo_dia"])),
              fecha_imp=comun._short_date(t, datetime.fromisoformat(estado["importado_at"]).date()))
    if estado["desactualizada"]:
        texto += " " + t("fuente.desactualizada", dias=fuentes.FUENTES[_FUENTE_COMMUTE]["umbral_dias"])
    return texto


WEEK_KEYS = ["readiness", "sleep_minutes", "hrv_last_night", "resting_hr"]
# BODY-TENDENCIAS-KPI-MINI-1 ruling 1: the compact strip's own four tiles, in this fixed order --
# a subset of TODAS (hrv_last_night, resting_hr live in TREND_KEYS' own primary 9; stress_avg lives
# behind "Ver todas las medidas", TREND_GROUPS' "stress" group), each shown only when its card
# exists (card_by_key below, built the same way for both groups).
KPI_KEYS = ["hrv_last_night", "resting_hr", "sleep_minutes", "stress_avg"]
TREND_MIN_DAYS = 14  # body/profile.py's metric_profile() needs this many rows before a metric gets a card
SPARSE_KEYS = {"vo2max", "vo2max_cycling", "fitness_age"}  # Garmin updates these only after qualifying activities
RANGES = ["7", "30", "90", "all"]
GARMIN_MIN_MONTH_POINTS = 7  # body/profile.py's metric_profile() default -- a near-daily watch cadence
SELF_MIN_MONTH_POINTS = 2  # a weekly self-measured key's own floor (BODY-MEDIDA-PESO-1-M2), per origin
# BODY-SUENO-RESPIRACION-VISTA-1: comun.TREND_GROUPS' "night" group -- same honest caveat as
# Día's (BODY-ENTRENO-1-INV Q6), carried as the card's own note like trends.self_note.
NIGHT_CAVEAT_KEYS = {"sleep_spo2_avg", "sleep_respiration_avg"}


def _trend_days_have(conn, person_id: int) -> int:
    """How many days the best-covered trend metric already has, for the honest-gap message below
    TREND_MIN_DAYS -- a count, never a figure (BODY-SIN-FRICCION-1)."""
    placeholders = ",".join("?" * len(comun.TREND_KEYS))
    rows = conn.execute(
        f"SELECT COUNT(*) AS n FROM daily_metric WHERE person_id = ? AND metric_key IN ({placeholders}) "
        "GROUP BY metric_key",
        (person_id, *comun.TREND_KEYS),
    ).fetchall()
    return max((r["n"] for r in rows), default=0)


def _compare(t: T, k: str, d: dict, value, ref, spread: float, vs_key: str) -> tuple[str, str]:
    if value is None or ref is None:
        return "", "flat"
    diff = value - ref
    if abs(diff) < (spread or 1) * 0.15:
        return t("delta.same"), "flat"
    is_min = comun._is_minutes(k)
    dec = comun._decimales(k)
    # BODY-MEDIDA-PESO-1-M2: an arrow is only honest when the amount next to it is not "0" -- a
    # diff that rounds to zero AT THE METRIC'S OWN PRECISION (whole minutes for a minutes key,
    # `dec` decimals otherwise) gets the same "no real change" wording as the spread check above,
    # never an arrow pointing at nothing.
    if (round(abs(diff)) if is_min else round(abs(diff), dec)) == 0:
        return t("delta.same"), "flat"
    amount = t.minutes(abs(diff)) if is_min else t.num(abs(diff), dec)
    cls = "flat" if d["better"] == "neutral" else ("good" if (diff > 0) == (d["better"] == "higher") else "bad")
    return f"{'↑' if diff > 0 else '↓'} {amount} {t(vs_key)}", cls


def _nights(conn, start: date, end: date, person_id: int) -> set[str]:
    return {r["day"] for r in conn.execute("SELECT day FROM shift WHERE person_id = ? AND kind = 'night' AND day "
                                           "BETWEEN ? AND ?", (person_id, start.isoformat(), end.isoformat()))}


def _marcas(conn, start: date, end: date, person_id: int) -> set[str]:
    """BODY-PERIODO-PANTALLAS-1: the window's own marked days (illness, travel, injury), read the
    same way `_nights` is -- one read per page, through db.dias_marcados' own per-connection cache,
    so a page with no period at all costs the one SELECT this already needed and nothing more."""
    return db.dias_marcados(conn, person_id, start.isoformat(), end.isoformat())


def _chart_aria(t: T, metric: str, start_label: str, end_label: str, months: bool = False) -> str:
    """Accessible name for a trend chart: the metric and its span in words, no figure in it (BODY-EJES-FECHAS-1).
    The connector words come from trends.aria_days / trends.aria_months in i18n, not literals here. A month span
    reads mid-sentence in Spanish, so its month names go lowercase here -- t.month_year itself still capitalizes,
    for the axis ends where they stand alone (BODY-EJES-FECHAS-1-M)."""
    if months and t.lang == "es":
        start_label, end_label = start_label.lower(), end_label.lower()
    key = "trends.aria_months" if months else "trends.aria_days"
    return t(key, metric=metric, desde=start_label, hasta=end_label)


def _medidas_count_txt(t: T, n: int) -> str:
    return t("trends.medidas_count_one") if n == 1 else t("trends.medidas_count_other", n=n)


def _kpi_banda(fig: float | None, low: float, high: float) -> str | None:
    """BODY-TENDENCIAS-KPI-MINI-1 ruling 1: a tile's status word against its OWN card's usual
    band -- the same p.low/p.high _trend_card already read, never a second read. None when the
    card carries no figure at all (no data in the picked range)."""
    if fig is None:
        return None
    if fig < low:
        return "abajo"
    if fig > high:
        return "arriba"
    return "dentro"


def _kpi_label(t: T, key: str) -> str:
    if key == "hrv_last_night":
        return t("trends.kpi.hrv_last_night")
    if key == "resting_hr":
        return t("trends.kpi.resting_hr")
    if key == "sleep_minutes":
        return t("trends.kpi.sleep_minutes")
    return t("trends.kpi.stress_avg")  # the only other KPI_KEYS member


def _kpi_estado(t: T, banda: str | None) -> tuple[str | None, str]:
    """The tile's status text and its colour class -- "en tu rango"/"in your range" may use the
    app's own positive token (ruling 1); above/below stay neutral text, never an alarm colour."""
    if banda == "dentro":
        return t("trends.kpi.estado.dentro"), "good"
    if banda == "arriba":
        return t("trends.kpi.estado.arriba"), "flat"
    if banda == "abajo":
        return t("trends.kpi.estado.abajo"), "flat"
    return None, ""


def _kpi_tile(t: T, key: str, card: dict) -> dict:
    estado, cls = _kpi_estado(t, card["banda"])
    return {"anchor": key, "label": _kpi_label(t, key), "est": card["est"],
            "valor": card["last30"], "unidad": card["unit"], "estado": estado, "cls": cls}


def _kpi_fuerza_faltan_txt(t: T, k: int) -> str:
    return t("trends.kpi.fuerza.faltan_one") if k == 1 else t("trends.kpi.fuerza.faltan_other", k=t.num(k))


def _fuerza_mini(t: T, piso: int | None, n: int, hoy: date) -> dict | None:
    """BODY-TENDENCIAS-KPI-MINI-1 ruling 2: the strip's fifth tile, built from the strength
    study's own already-loaded racha_semanal and floor -- no floor, no tile. "days left in the
    week" counts today (ISO weekday 1=Monday..7=Sunday, so 8 - isoweekday is Sunday-inclusive).
    The reachability test itself is goals.estado_cuota (BODY-PLAN-MODULO-1 ruling 4): this is now
    its only caller outside the plan module, and this tile's rendered output is unchanged."""
    if piso is None:
        return None
    dias_restantes = 8 - hoy.isoweekday()
    estado_cuota = goals.estado_cuota(n, piso, dias_restantes)
    if estado_cuota == "cumplida":
        estado, cls = t("trends.kpi.fuerza.cumplida"), "good"
    elif estado_cuota == "en_curso":
        estado, cls = t("trends.kpi.fuerza.vas_bien"), "good"
    else:
        estado, cls = _kpi_fuerza_faltan_txt(t, piso - n), "flat"
    return {"anchor": "racha", "label": t("trends.kpi.fuerza.titulo"), "est": False,
            "valor": t("trends.kpi.fuerza.figura", n=t.num(n), piso=t.num(piso)), "unidad": "",
            "estado": estado, "cls": cls}


_N_POST_OFFSETS = sum(1 for o in turnos_costo.OFFSETS if o.startswith("post_"))


def _frase_patron_texto(t: T, frase: dict | None) -> str | None:
    """BODY-TENDENCIAS-LEYENDA-PATRON-1 ruling 2: renders turnos_costo.frase_patron's data-only
    spec into the one sentence a card shows -- the only place its words are built, so the
    assistant's leer_tendencias (ruling 3) can share the same spec without ever sharing this
    rendered text. The 'cuando'/'vuelta' time phrases are per-offset i18n keys (trends.patron.
    cuando.<offset>, trends.patron.vuelta.<offset>), never built from a figure; the post-offset
    count in the no_vuelve wording is read off OFFSETS, never typed as a literal "4"."""
    if frase is None:
        return None
    if frase["kind"] == "estable":
        return t("trends.patron.estable")
    peor = t("trends.patron.cuando." + frase["peor"])
    if frase["kind"] == "vuelve":
        vuelta = t("trends.patron.vuelta." + frase["vuelta"])
        return t("trends.patron.vuelve." + frase["direccion"], peor=peor, vuelta=vuelta)
    return t("trends.patron.no_vuelve." + frase["direccion"], peor=peor, n=_N_POST_OFFSETS)


# BODY-TENDENCIAS-RACHA-GRAFICO-1 ruling 3: the study card's own SVG bar chart -- one bar per week,
# a fixed viewBox at the real 375px card width (same derivation as body/rutas/turnos.py's own
# _PERFIL_WIDTH), thin enough to fit the whole watch history with no aggregation.
_RACHA_WIDTH = 313
_RACHA_TOP = 4
_RACHA_PLOT_H = 84
_RACHA_TICK_H = 6  # the night-shift tick strip under the bars
# BODY-TENDENCIAS-RACHA-GRAFICO-1-M2 (step 3): the month axis, drawn as <text> inside the svg
# itself instead of an HTML row under it, at the app's own small-text floor (body.css's ".tick"
# class, the same one body/rutas/turnos.py's axis labels use).
_RACHA_LABEL_FONT = 12.0
_RACHA_CHAR_W = 0.6 * _RACHA_LABEL_FONT  # same character-count approximation as turnos.py's own
                                          # _PERFIL_CHAR_W, since real glyph metrics are not
                                          # available server-side
_RACHA_LABEL_GAP_X = 8.0   # minimum clear gap kept between two adjacent month labels' own boxes
_RACHA_EDGE_PAD = 2.0      # inside the viewBox's own left/right edge
_RACHA_LABEL_H = 16        # height reserved below the tick strip for the month-label row
_RACHA_LABEL_BASELINE = 12  # the label text's own baseline, within that reserved row


def _racha_label_px(s: str) -> float:
    return len(s) * _RACHA_CHAR_W


def _racha_eje(t: T, semanas: list[dict], bw: float) -> list[dict]:
    """Up to 4 month-axis labels (ruling 3), each centred at the x of the first week of that
    month -- evenly chosen among every month-start week when there are more than 4. The first
    label (chronologically earliest) and every January carry the year (BODY-TENDENCIAS-RACHA-
    GRAFICO-1-M2 step 3); every other label is the short month name alone. A label too close to
    the viewBox's own edge anchors start/end instead of middle so it is never clipped, and a
    greedy left-to-right scan drops any label whose own box (the character-count estimate above)
    would come within _RACHA_LABEL_GAP_X of the one already kept, so no two labels ever sit less
    than that gap apart (BODY-TENDENCIAS-RACHA-GRAFICO-1-M3). The first label and the last are
    never the ones dropped -- a conflicting last candidate instead pops middle labels already kept
    until it clears or none are left to pop."""
    n = len(semanas)
    arranques: list[int] = []
    mes_anterior = None
    for i in range(n):
        d = date.fromisoformat(semanas[i]["lunes"])
        clave = (d.year, d.month)
        if clave != mes_anterior:
            arranques.append(i)
            mes_anterior = clave
    if len(arranques) > 4:
        k = 4
        arranques = sorted({arranques[round(j * (len(arranques) - 1) / (k - 1))] for j in range(k)})

    candidatos = []
    for pos, i in enumerate(arranques):
        d = date.fromisoformat(semanas[i]["lunes"])
        mes = t("date.months")[d.month - 1][:3]
        texto = f"{mes} {d.year}" if pos == 0 or d.month == 1 else mes
        candidatos.append((i * bw + bw / 2, texto))

    elegidos: list[tuple[float, str]] = [candidatos[0]]
    for idx in range(1, len(candidatos)):
        x, texto = candidatos[idx]
        mitad = _racha_label_px(texto) / 2
        es_ultimo = idx == len(candidatos) - 1
        if es_ultimo:
            while len(elegidos) > 1:
                x_prev, texto_prev = elegidos[-1]
                if x - mitad >= x_prev + _racha_label_px(texto_prev) / 2 + _RACHA_LABEL_GAP_X:
                    break
                elegidos.pop()
            elegidos.append((x, texto))
        else:
            x_prev, texto_prev = elegidos[-1]
            if x - mitad >= x_prev + _racha_label_px(texto_prev) / 2 + _RACHA_LABEL_GAP_X:
                elegidos.append((x, texto))

    out = []
    for x, texto in elegidos:
        mitad = _racha_label_px(texto) / 2
        if x - mitad < _RACHA_EDGE_PAD:
            anchor, tx = "start", _RACHA_EDGE_PAD
        elif x + mitad > _RACHA_WIDTH - _RACHA_EDGE_PAD:
            anchor, tx = "end", _RACHA_WIDTH - _RACHA_EDGE_PAD
        else:
            anchor, tx = "middle", x
        out.append({"x": tx, "anchor": anchor, "texto": texto})
    return out


def _racha_svg(t: T, semanas: list[dict], piso: int | None, turnos_on: bool, aria: str) -> str:
    n = len(semanas)
    max_sesiones = max(s["sesiones"] for s in semanas)
    # Ruling 4 (BODY-TENDENCIAS-RACHA-GRAFICO-1-M2 step 4): with a floor, the scale's own top sits
    # one session ABOVE the taller of the tallest week and the floor, so the dashed floor line
    # never sits flush with the viewBox's own top edge, and a week that reaches the floor visibly
    # touches or passes the line instead of looking capped by it.
    tope = max(max_sesiones, piso) + 1 if piso is not None else max(max_sesiones, 1)
    top, h = _RACHA_TOP, _RACHA_PLOT_H
    base_y = top + h
    bw = _RACHA_WIDTH / n
    bar_w = max(bw - 0.4, 0.4)
    out = []
    for i, s in enumerate(semanas):
        x0 = i * bw
        alto = (s["sesiones"] / tope) * h
        if alto > 0:
            cls = "c-body" if s["alcanza"] else "c-other"
            out.append(f'<rect x="{x0:.1f}" y="{base_y - alto:.1f}" width="{bar_w:.1f}" height="{alto:.1f}" '
                       f'class="{cls}"/>')
        if turnos_on and s["noche"]:
            out.append(f'<rect x="{x0:.1f}" y="{base_y + 2:.1f}" width="{bar_w:.1f}" height="3" class="c-night"/>')
    if piso is not None:
        y_piso = base_y - (piso / tope) * h
        out.append(f'<line x1="0" y1="{y_piso:.1f}" x2="{_RACHA_WIDTH}" y2="{y_piso:.1f}" class="stroke-muted" '
                   f'stroke-width="1.2" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>')
    label_top = base_y + _RACHA_TICK_H
    label_y = label_top + _RACHA_LABEL_BASELINE
    for e in _racha_eje(t, semanas, bw):
        out.append(f'<text x="{e["x"]:.1f}" y="{label_y:.1f}" text-anchor="{e["anchor"]}" '
                   f'class="tick">{html.escape(e["texto"])}</text>')
    full_h = label_top + _RACHA_LABEL_H
    # BODY-TENDENCIAS-RACHA-GRAFICO-1-M2 step 3: the viewBox width already equals the card's own
    # drawn content width (_RACHA_WIDTH, same derivation as turnos.py's _PERFIL_WIDTH); the height
    # attribute is computed to equal the viewBox height too, so both axes scale 1:1 and the axis
    # text renders at its own declared size -- preserveAspectRatio="none" (which let x and y scale
    # independently and stretch the glyphs) is dropped rather than kept now that nothing needs it.
    return (f'<svg viewBox="0 0 {_RACHA_WIDTH} {full_h:.1f}" width="100%" height="{full_h:.1f}" role="img" '
            f'aria-label="{html.escape(aria)}" class="chart-svg">{"".join(out)}</svg>')


def _racha_semanas_txt(t: T, n: int) -> str:
    return t("trends.racha.semanas_one") if n == 1 else t("trends.racha.semanas_other", n=t.num(n))


def _racha_rango_meses(t: T, desde: date, hasta: date) -> str:
    """BODY-TENDENCIAS-RACHA-GRAFICO-1-M2 step 5: a figure's own month range, in short month names
    -- one month named once ("oct 2026"), two months sharing a year with the year once at the end
    ("oct–nov 2026"), two months across years with the year on both ("dic 2025–feb 2026")."""
    md, mh = t("date.months")[desde.month - 1][:3], t("date.months")[hasta.month - 1][:3]
    if (desde.year, desde.month) == (hasta.year, hasta.month):
        return f"{md} {desde.year}"
    if desde.year == hasta.year:
        return f"{md}–{mh} {hasta.year}"
    return f"{md} {desde.year}–{mh} {hasta.year}"


def _piso_y_opciones(conn, t: T, person_id: int) -> tuple[int | None, str | None, int | None, list[dict]]:
    """Rulings 1-2, resolved and read together in ONE SELECT (BODY-TENDENCIAS-RACHA-GRAFICO-1-M,
    statement ceiling): every active habit this person may read under the same scope
    `goals.goal_access`/`goal_scope_sql` apply (their own goals, or a household milestone goal's),
    joined with its own goal here instead of a separate `household.household_of` round trip -- the
    household check becomes one correlated subquery on `hogar_miembro` in this same statement. The
    person's own stored pick (body/db.py's racha_piso table) rides along as a scalar subquery on
    every row, rather than `db.racha_piso_habit_id`'s own separate read. Filtered in Python to the
    weekly, correctly-assigned habits (ruling 2's picker, and the only ids a POST to
    /trends/racha-piso may name). The floor (ruling 1) is whichever of those, if any, matches the
    pick. Zero rows (no weekly habit this person may read at all) means no pick can resolve to a
    floor either way, so the raw stored id is not worth a second statement to recover then."""
    rows = conn.execute(
        "SELECT h.id, h.title, h.cadence, h.assignee_person_id, "
        "(SELECT habit_id FROM racha_piso WHERE person_id = ?) AS elegido FROM habit h "
        "JOIN goal g ON g.id = h.goal_id WHERE h.active = 1 AND (g.person_id = ? OR "
        "(g.visibility = 'household' AND g.kind = 'milestone' AND "
        "g.hogar_id = (SELECT hogar_id FROM hogar_miembro WHERE person_id = ?)))",
        (person_id, person_id, person_id),
    ).fetchall()
    habit_id_elegido = rows[0]["elegido"] if rows else None
    opciones = []
    piso = piso_titulo = None
    for h in rows:
        if h["assignee_person_id"] is not None and h["assignee_person_id"] != person_id:
            continue
        cadence = comun._habit_cadence(h)
        if cadence.get("per") != "week":
            continue
        texto = f"{h['title']} · {t('trends.racha.piso_opcion', n=t.num(cadence.get('times')))}"
        opciones.append({"id": h["id"], "texto": texto})
        if h["id"] == habit_id_elegido:
            piso, piso_titulo = cadence.get("times"), h["title"]
    return piso, piso_titulo, habit_id_elegido, opciones


def _racha_ctx(t: T, conn, person_id: int, turnos_on: bool, hoy: date) -> dict | None:
    """The study card's whole context (ruling 3), or None when racha_semanal has nothing to show
    (no strength session ever) -- the card, including its picker, is absent then."""
    piso, _piso_titulo, piso_habit_id, opciones = _piso_y_opciones(conn, t, person_id)
    result = entreno_racha.racha_semanal(conn, person_id, piso=piso, hoy=hoy)
    if result is None:
        return None
    semanas = result["semanas"]
    titulo = t("trends.racha.titulo", mes=t.month_year(date.fromisoformat(result["desde"])))
    sub = t("trends.racha.sub", dias=t.num(result["dias_historia"]))
    piso_nota = t("trends.racha.piso_nota", piso=t.num(piso)) if piso is not None else None
    noche_nota = t("trends.racha.noche_nota") if turnos_on else None
    primera = date.fromisoformat(semanas[0]["lunes"])
    ultima = date.fromisoformat(semanas[-1]["lunes"]) + timedelta(6)
    aria = t("trends.racha.aria", desde=comun._short_date(t, primera), hasta=comun._short_date(t, ultima))
    svg = _racha_svg(t, semanas, piso, turnos_on, aria)
    figuras = []
    if piso is not None:
        mas_larga = result.get("mas_larga")
        if mas_larga:
            rango = _racha_rango_meses(t, date.fromisoformat(mas_larga["inicio"]),
                                       date.fromisoformat(mas_larga["fin"]) + timedelta(6))
            figuras.append({"valor": _racha_semanas_txt(t, mas_larga["semanas"]),
                           "texto": t("trends.racha.mas_larga_txt", piso=t.num(piso), rango=rango)})
        term = result.get("terminadas_en_noche")
        if turnos_on and term and term["de"]:
            figuras.append({"valor": t("trends.racha.terminadas_valor", n=t.num(term["n"]), de=t.num(term["de"])),
                           "texto": t("trends.racha.terminadas_txt")})
        actual = result.get("actual")
        if actual:
            figuras.append({"valor": _racha_semanas_txt(t, actual["semanas"]), "texto": t("trends.racha.actual_txt")})
    return {
        "titulo": titulo, "sub": sub, "piso_nota": piso_nota, "noche_nota": noche_nota, "svg": svg,
        "figuras": figuras, "opciones": opciones, "piso_habit_id": piso_habit_id,
        # BODY-TENDENCIAS-KPI-MINI-1 ruling 2: the strip's own strength tile reads these two raw
        # numbers -- the floor and the already-computed current week's row -- never a second read.
        "piso": piso, "semana_actual_sesiones": semanas[-1]["sesiones"],
    }


def _trend_card(t: T, defs: dict, k: str, start, end, month, nights, marcas, viaje_dias, profiles: dict,
                 windows_by_key: dict, patrones: dict):
    """One card -- own baseline, honest gaps, night shading, the month tap -- built the same way for the
    default 9 and every extra metric behind "Ver todas las medidas" (DESIGN rules 3, 6, 7). A
    self-measured key (BODY-ENTRENO-MEDIDA-1) gets a card from its first value instead of waiting
    for TREND_MIN_DAYS worth of watch-sized history -- there is no cadence to wait on. Below
    baselines.MIN_DAYS of self-measured values, p.low/p.high are not yet an honest normal range (as
    few as one point collapses them to a single repeated value) -- the card carries no delta, no
    normal-range legend and no band, and a count line instead (BODY-ENTRENO-TOOLS-1-M fix 3).

    profiles and windows_by_key are read once for every key the whole route needs (BODY-AGUJETAS-1-B)
    -- this card never queries the database itself, so its own cost does not grow with the catalogue."""
    d = defs[k]
    p = profiles.get(k)
    if not p:
        return None, None
    pocas = d["origen"] == "self" and p.days < baselines.MIN_DAYS
    spread = p.high - p.low
    metric_label = t("metric." + k)
    valor_fmt = lambda v: comun._fig_unit(t, k, v, defs)
    if start:
        w = windows_by_key[k]
        if pocas:
            delta, cls = "", "flat"
        elif month:
            delta, cls = _compare(t, k, d, w.mean, p.all_mean, spread, "trends.vs_hist")
        else:
            delta, cls = _compare(t, k, d, w.mean, w.prev_mean, spread, "trends.vs_prev")
        etiqueta_dia = lambda day: comun._short_date(t, day)
        aria = _chart_aria(t, metric_label, etiqueta_dia(w.start), etiqueta_dia(w.end))
        fig, svg = w.mean, profile.window_svg(w, p.low, p.high, nights, etiqueta_dia=etiqueta_dia, aria_label=aria,
                                              valor_fmt=valor_fmt, link_label=t("trends.chart_open_day"),
                                              banda=not pocas, marcas=marcas, viajes=viaje_dias)
    else:
        delta, cls = ("", "flat") if pocas else _compare(t, k, d, p.last30, p.all_mean, spread, "trends.vs_hist")
        etiqueta_mes = lambda m: t.month_year(date.fromisoformat(m + "-01"))
        aria = (_chart_aria(t, metric_label, etiqueta_mes(p.months[0][0]), etiqueta_mes(p.months[-1][0]), months=True)
                if len(p.months) >= 2 else None)
        fig, svg = p.last30, profile.month_svg(p, etiqueta_mes=etiqueta_mes, aria_label=aria,
                                               valor_fmt=valor_fmt, link_label=t("trends.chart_open_month"))
    card = {"key": k, "question": t("trends.q." + k), "est": d["kind"] == "estimated",
            "last30": comun._value(t, k, fig), "unit": comun._unit(t, d) if fig is not None else "", "delta": delta,
            "cls": cls, "svg": svg, "low": comun._value(t, k, p.low), "high": comun._value(t, k, p.high),
            "sparse": k in SPARSE_KEYS, "self": d["origen"] == "self", "subtitle": None,
            "pocas": pocas, "conteo": _medidas_count_txt(t, p.days) if pocas else None,
            "caveat": k in NIGHT_CAVEAT_KEYS, "patron": patrones.get(k),
            "banda": None if pocas else _kpi_banda(fig, p.low, p.high)}
    return card, p


@router.get("/trends")
@comun._cierra_conexion
def trends(r: str = "all", m: str | None = None, todas: str | None = None, desde: str | None = None,
           dia: str | None = None, person_id: int = Depends(identity.current_person_id)):
    """r = 7 | 30 | 90 | all (the whole history, monthly). m = YYYY-MM opens one month day by day (from a tap on
    the monthly line). todas=1 keeps "Ver todas las medidas" open across a range switch. desde=turnos|dia|como
    arrives from a Turnos cost row, a Dia card or a Como lo calculo row (BODY-TURNOS-ENLACES-1, BODY-PULIDO-1)
    and shows the way back, kept on every range link; dia=<YYYY-MM-DD> names the day to go back to for
    desde=dia/como, and an unparseable dia drops both. The URL carries a range, a month or those flags, never
    a value."""
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    today_local = comun._today_local(person)
    # BODY-TENDENCIAS-LEYENDA-PATRON-1 rulings 2, 4: one caption per metric the shift-cost model
    # has a reference for, read over that model's own RESUMEN_DIAS window regardless of the range
    # `r` picks -- shared once (turnos_costo.perfiles_patron) rather than per card, so this costs
    # /trends' own statement ceiling (tests/test_rendimiento.py) a fixed few statements, never one
    # per metric. Skipped outright when the person does not track night shifts at all: every
    # profile would read "pocos_bloques_previos" anyway, and no card would show a caption.
    patrones = {}
    if turnos_on:
        perfiles = turnos_costo.perfiles_patron(conn, person_id, today_local)
        patrones = {m: texto for m, p in perfiles.items()
                    if (texto := _frase_patron_texto(t, turnos_costo.frase_patron(p, m))) is not None}
    defs = comun._defs(conn)
    todas_keys = list(TODAS)
    # BODY-MEDIDA-PESO-1-M2 / BODY-ENTRENO-MEDIDA-1: a self-measured key's own lower thresholds,
    # same reasoning _trend_card used per key before BODY-AGUJETAS-1-B batched this read.
    min_days_por_clave = {k: (1 if defs[k]["origen"] == "self" else TREND_MIN_DAYS) for k in todas_keys}
    min_month_points_por_clave = {k: (SELF_MIN_MONTH_POINTS if defs[k]["origen"] == "self"
                                       else GARMIN_MIN_MONTH_POINTS) for k in todas_keys}
    profiles = profile.metric_profiles(conn, todas_keys, person_id=person_id,
                                       min_days_por_clave=min_days_por_clave,
                                       min_month_points_por_clave=min_month_points_por_clave)
    last = comun._latest_day(conn, person_id)
    desde_dia = None
    if desde in ("dia", "como") and dia:
        try:
            desde_dia = date.fromisoformat(dia).isoformat()
        except ValueError:
            desde_dia = None
    if desde not in ("turnos", "dia", "como") or (desde in ("dia", "como") and not desde_dia):
        desde = None
    back_href = back_label = None
    if desde == "turnos":
        back_href, back_label = "/shifts", t("trends.back_turnos")
    elif desde == "dia":
        back_href, back_label = f"/day/{desde_dia}", t("trends.back_dia")
    elif desde == "como":
        back_href, back_label = f"/how/{desde_dia}", t("trends.back_como")
    month = None
    if m:
        try:
            month = date.fromisoformat(m + "-01")
        except ValueError:
            raise HTTPException(404)
        r = "month"
    elif r not in RANGES:
        r = "all"
    start = end = None
    if month:
        start = month
        end = (month.replace(day=28) + timedelta(4)).replace(day=1) - timedelta(1)
    elif r != "all" and last:
        end = date.fromisoformat(last)
        start = end - timedelta(int(r) - 1)
    nights = _nights(conn, start, end, person_id) if start and turnos_on else set()
    marcas = _marcas(conn, start, end, person_id) if start else set()
    # BODY-HISTORIA-VIDA-LUGARES-2-R ruling 2, 7: one trip read for the whole route (start/end is
    # the one window every card below shares), never a read per card.
    viaje_dias = viajes.dias_de_viaje(conn, person_id, start.isoformat(), end.isoformat()) if start else set()
    windows_by_key = profile.windows(conn, todas_keys, start, end, person_id=person_id) if start else {}
    cards, since, days = [], None, 0
    # BODY-TENDENCIAS-KPI-MINI-1 ruling 1: every card this route builds, by key -- the strip reads
    # a card's own already-computed figure/unit/band off here, whichever of the two loops below
    # built it, rather than a third call to _trend_card.
    card_by_key: dict[str, dict] = {}
    for k in comun.TREND_KEYS:
        card, p = _trend_card(t, defs, k, start, end, month, nights, marcas, viaje_dias, profiles, windows_by_key,
                              patrones)
        if not card:
            continue
        since = since or p.first_day
        days = max(days, p.days)
        cards.append(card)
        card_by_key[k] = card
    if not cards:
        have = _trend_days_have(conn, person_id)
        if have:
            return comun._render("soon.html", t=t, tab="trends", title=t("nav.trends"),
                           text=t("trends.too_little", days=TREND_MIN_DAYS - have),
                           back_href=back_href, back_label=back_label, turnos=turnos_on)
        return comun._render("soon.html", t=t, tab="trends", title=t("nav.trends"),
                       text=comun._sync_status_text(conn, person, t),
                       back_href=back_href, back_label=back_label, turnos=turnos_on)
    extra_groups, extra_count = [], 0
    for group_key, keys in comun.TREND_GROUPS:
        group_cards = []
        for k in keys:
            card, p = _trend_card(t, defs, k, start, end, month, nights, marcas, viaje_dias, profiles,
                                  windows_by_key, patrones)
            if not card:
                continue
            since = since or p.first_day
            days = max(days, p.days)
            card["subtitle"] = t("trends.plain." + k)
            group_cards.append(card)
            card_by_key[k] = card
            extra_count += 1
        # BODY-DESPLAZAMIENTO-3 ruling 2: the commute group shows even with zero cards -- its own
        # freshness line is the point when there is nothing yet to chart.
        freshness = _fuente_texto(t, conn, person_id, today_local) if group_key == "commute" else None
        if group_cards or freshness:
            extra_groups.append({"title": t("trends.group." + group_key), "cards": group_cards,
                                 "freshness": freshness})
    week = None
    # WEEK_KEYS is a subset of TODAS, read with the same (default) thresholds above -- reusing the
    # prefetched profiles here, instead of a second query per key, is what BODY-AGUJETAS-1-B batched.
    profs = [profiles.get(k) for k in WEEK_KEYS]
    if all(profs):
        cols = []
        for k, p in zip(WEEK_KEYS, profs):
            wd = p.weekday
            worse = max if defs[k]["better"] == "lower" else min
            present = [v for v in wd if v is not None]
            spread = max(present) - min(present)
            mark = wd.index(worse(present)) if spread >= (1 if not comun._is_minutes(k) else 5) else -1
            cols.append([{"text": comun._value(t, k, v), "mark": i == mark} for i, v in enumerate(wd)])
        week = {"heads": [t("trends.short." + k) for k in WEEK_KEYS],
                "rows": [{"day": t("date.days")[i][:3].capitalize(), "vals": [c[i] for c in cols]} for i in range(7)]}
    all_days = db.days_with_data(conn, person_id)
    since, days = (all_days[-1], len(all_days)) if all_days else (since, days)
    if month:
        fig_label = t("trends.month_avg", month=t.month_year(month))
    elif start:
        fig_label = t("trends.avg_n", n=r)
    else:
        fig_label = t("trends.last30")
    cuerpo_ctx = {"vistas": anatomia.contexto(t, "tendencias", frozenset({"corazon"})),
                  "caption": t("cuerpo.caption.tendencias"), "recorte": "torso"}
    racha = _racha_ctx(t, conn, person_id, turnos_on, today_local)
    # BODY-TENDENCIAS-KPI-MINI-1: the strip -- four KPI tiles (each only when its card exists on
    # this page, whichever range or month is showing) plus a fifth strength tile (only with a
    # strength study AND a floor). Pure Python over data this route already computed -- zero new
    # SQL statements.
    kpi_tiles = [_kpi_tile(t, k, card_by_key[k]) for k in KPI_KEYS if k in card_by_key]
    if racha:
        fuerza_mini = _fuerza_mini(t, racha["piso"], racha["semana_actual_sesiones"], today_local)
        if fuerza_mini:
            kpi_tiles.append(fuerza_mini)
    return comun._render("trends.html", t=t, tab="trends", cards=cards, week=week if r == "all" else None,
                   since=t.long_date(date.fromisoformat(since)) if since else "", days=t.num(days),
                   r=r, ranges=RANGES, fig_label=fig_label, nights=bool(nights), marcas=bool(marcas),
                   viajes=bool(viaje_dias),
                   month_title=t.month_year(month) if month else None, racha=racha, kpi_tiles=kpi_tiles,
                   extra_groups=extra_groups, extra_count=extra_count, todas=todas == "1",
                   desde=desde, dia=desde_dia, back_href=back_href, back_label=back_label, turnos=turnos_on,
                   cuerpo=cuerpo_ctx)


@router.post("/trends/racha-piso")
@comun._cierra_conexion_async
async def trends_racha_piso(request: Request, person_id: int = Depends(identity.current_person_id)):
    """Ruling 2: sets or clears (habit_id empty = "Sin piso"/"No floor") this person's own chosen
    floor habit -- never a figure in the redirect, and the only habit ids accepted are exactly the
    ones _piso_y_opciones would have offered this person (active, weekly, readable)."""
    conn, person, t = comun._ctx(person_id)
    f = await comun._form(request)
    raw = (f.get("habit_id") or "").strip()
    habit_id = None
    if raw:
        try:
            habit_id = int(raw)
        except ValueError:
            raise HTTPException(404)
        _, _, _, opciones = _piso_y_opciones(conn, t, person_id)
        validos = {o["id"] for o in opciones}
        if habit_id not in validos:
            raise HTTPException(404)
    db.set_racha_piso(conn, person_id, habit_id)
    r = f.get("r")
    qs = f"?r={r}" if r in RANGES and r != "all" else ""
    return RedirectResponse(f"/trends{qs}", status_code=303)
