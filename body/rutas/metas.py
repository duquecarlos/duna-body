"""Metas: goals, their weekly check, and habits (BODY-WEB-ESTRUCTURA-5, pure move out of
body/web.py): `/goals`, `/goals/new` GET+POST, `/goals/{goal_id}` and its actions, and
`/habits/{habit_id}/check`.
"""
from __future__ import annotations

import html
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import cuenta, db, enlaces, fechas, goals, horizonte, household, identity, profile
from ..baselines import baselines
from ..i18n import T
from . import comun, experimentos

router = APIRouter()

AREAS = ["body", "money", "adventure", "growth", "work", "home"]
DIRECTIONS = ["raise", "lower", "keep"]
CONTEXTS = ("before_night", "after_night", "off_days")
BETTER_TO_DIRECTION = {"higher": "raise", "lower": "lower", "neutral": "keep"}

# BODY-PLAN-PANTALLA-1 ruling 2: every estado a weekly target line or a checkpoint criterion can
# carry, mapped to the design's own "good / neutral / bad / muted" chip colours -- reused here
# from the readiness-band family (ok=good, warn=amber/caution, low=red/bad, off=muted gray),
# never a new chip colour. 'pendiente' is a checkpoint-only extra (a criterion not due yet).
PLAN_ESTADOS = ("cumple", "en_curso", "no_cumple", "sin_dato", "pendiente")
_PLAN_ESTADO_CLS = {"cumple": "ok", "en_curso": "warn", "no_cumple": "low", "sin_dato": "off", "pendiente": "off"}


def _cadence_text(t: T, cadence: dict) -> str:
    per = t("goals.cadence.per." + cadence.get("per", "week"))
    return t("goals.cadence", n=cadence.get("times", 1), per=per)


def _parse_new_habit(f: dict, prefix: str = "") -> tuple[str | None, dict | None]:
    """Reads title/times/per/context fields the same way for the goal detail's 'add a habit' form
    and goal creation's optional first-habit block (BODY-SIN-FRICCION-1, prefix='habit_' there so
    its fields never collide with the goal's own 'title'). Empty title = no habit, (None, None)."""
    title = (f.get(prefix + "title") or "").strip()
    if not title:
        return None, None
    per = f.get(prefix + "per") if f.get(prefix + "per") in ("day", "week", "month") else "week"
    try:
        times = max(1, int(f.get(prefix + "times", "1")))
    except ValueError:
        times = 1
    context = f.get(prefix + "context") or None
    cadence = {"per": per, "times": times}
    if context in CONTEXTS:
        cadence["context"] = context
    return title, cadence


def _habits_for_goal(conn, goal_id: int):
    """Every active habit of G, whoever it is assigned to -- goal_detail renders a habit assigned
    to another household member as theirs (name only), never this viewer's own checkbox."""
    return conn.execute("SELECT * FROM habit WHERE goal_id = ? AND active = 1 ORDER BY id", (goal_id,)).fetchall()


def _week_checks(conn, habit_ids: list[int], person_id: int, monday: date, sunday: date) -> dict[int, set[str]]:
    """One SELECT for every weekly habit of this goal that is this person's own (BODY-METAS-HABITO-
    SEMANA-TIRA-1 ruling 2), instead of one comun._habit_checked call per habit -- {habit_id: {day
    iso, ...}} of this week's done=1 checks, reused below for both the week strip and
    `checked_today`. No habit ids -> no statement at all, so a goal with zero weekly own habits
    costs nothing extra."""
    if not habit_ids:
        return {}
    placeholders = ",".join("?" * len(habit_ids))
    rows = conn.execute(
        f"SELECT habit_id, day FROM habit_check WHERE habit_id IN ({placeholders}) AND person_id = ? "
        f"AND day BETWEEN ? AND ? AND done = 1",
        (*habit_ids, person_id, monday.isoformat(), sunday.isoformat())).fetchall()
    out: dict[int, set[str]] = {hid: set() for hid in habit_ids}
    for r in rows:
        out[r["habit_id"]].add(r["day"])
    return out


def _week_strip(t: T, monday: date, today: date, done_days: set[str]) -> list[dict]:
    """Ruling 1: Monday..Sunday day cells for a weekly habit's own progress line. The visible
    letter reuses t('date.days') -- en 'M T W T F S S', es whatever that same list's own initials
    give -- never a second, hand-typed letter list. A done=1 check always wins the cell's state,
    even on today or (inside the one-day tolerance) a near-future day; otherwise today/future/past
    decide it. The accessible text names the day and the state in words, never just the letter."""
    names = t("date.days")
    state_words = {
        "hecho": t("goals.week_strip.done"),
        "hoy": t("goals.week_strip.today"),
        "futuro": t("goals.week_strip.upcoming"),
        "pasado": t("goals.week_strip.unchecked"),
    }
    cells = []
    for i in range(7):
        d = monday + timedelta(i)
        name = names[i]
        if d.isoformat() in done_days:
            state = "hecho"
        elif d == today:
            state = "hoy"
        elif d > today:
            state = "futuro"
        else:
            state = "pasado"
        cells.append({"letter": name[:1].upper(), "state": state,
                      "aria": t("goals.week_strip.cell_aria", day=name, state=state_words[state])})
    return cells


def _shared_line(conn, t: T, g, person_id: int) -> str | None:
    """The quiet line a household goal carries wherever it's shown -- Metas' list card (rule 1 asked
    for it there too, BODY-HOGAR-MIEMBRO-1-M) and the goal page: creator's own line, or who created it
    for a member."""
    if g["visibility"] != "household":
        return None
    if g["person_id"] == person_id:
        return t("goals.shared_by_you")
    creator = conn.execute("SELECT display_name FROM person WHERE id = ?", (g["person_id"],)).fetchone()
    return t("goals.shared_by", name=creator["display_name"] if creator else "?")


def _verdict(t: T, on_track) -> tuple[str, str]:
    if on_track is None:
        return "", t("goals.verdict.no_data")
    return ("ok", t("goals.verdict.on")) if on_track else ("warn", t("goals.verdict.off"))


def _metric_pct(g, current) -> float | None:
    if g["target_value"] is None or g["baseline_value"] is None:
        return None
    span = g["target_value"] - g["baseline_value"]
    if span == 0:
        return 100.0
    if current is None:
        return 0.0
    return max(0.0, min(100.0, (current - g["baseline_value"]) / span * 100))


def _time_pct(g, today: date) -> float | None:
    if not g["deadline"] or not g["start_day"]:
        return None
    s, e = date.fromisoformat(g["start_day"]), date.fromisoformat(g["deadline"])
    if e <= s:
        return 100.0
    return max(0.0, min(100.0, (today - s).days / (e - s).days * 100))


def _goal_target_text(t: T, defs: dict, g) -> str:
    date_txt = comun._short_date(t, date.fromisoformat(g["deadline"])) if g["deadline"] else "—"
    if g["kind"] == "metric":
        return t("goals.card.target", target=comun._fig_unit(t, g["metric_key"], g["target_value"], defs), date=date_txt)
    return t("goals.card.milestone_date", date=date_txt)


def _current_week_check(conn, goal_id: int, person_id: int, week_start: str) -> dict:
    """The current week's verdict, computed live on every view (BODY-SEGURIDAD-3): never a stored
    row, which would freeze the week's verdict at whoever opened it first and never update again
    that week. `goals.evaluar_semana` writes nothing; the one writer is body/pull.py's
    `cerrar_semanas`, run by the daily pull."""
    result = goals.evaluar_semana(conn, goal_id, person_id, week_start)
    return result or {"on_track": None, "metric_avg": None, "habits_done": 0, "habits_due": 0}


def _goal_line_svg(t: T, defs: dict, g, checks, today: date, width: int = 320, height: int = 120,
                    color_cls: str = "body") -> str:
    """The straight target line from baseline at start_day to target at deadline, with the
    actual weekly averages plotted over it and today marked. Small and local to this screen,
    since body/profile.py's chart helpers are built for daily_metric history, not a goal's plan.

    Accessible name, legend and a per-week tap/drag readout (DESIGN rule 9, BODY-PULIDO-2): the aria-label and
    legend name the plan and its dates, never a raw baseline or target figure; weekly points in data-points
    carry a label only, no day -- a week is not a day, so charts.js shows no link for them."""
    start, end = date.fromisoformat(g["start_day"]), date.fromisoformat(g["deadline"])
    if end <= start:
        return ""
    baseline_v, target_v = g["baseline_value"], g["target_value"]
    lo, hi = min(baseline_v, target_v), max(baseline_v, target_v)
    pad = (hi - lo) * 0.15 or 1
    lo, hi = lo - pad, hi + pad
    top, h = 6, height - 26
    span_days = (end - start).days

    def x(d: date) -> float:
        return max(0.0, min(1.0, (d - start).days / span_days)) * width

    def y(v: float) -> float:
        return top + h - (v - lo) / (hi - lo) * h

    out = [f'<line x1="{x(start):.1f}" y1="{y(baseline_v):.1f}" x2="{x(end):.1f}" y2="{y(target_v):.1f}" '
           f'class="stroke-muted" stroke-width="1.6" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>']
    pts = sorted((date.fromisoformat(c["week_start"]), date.fromisoformat(c["week_start"]) + timedelta(6),
                  c["metric_avg"]) for c in checks)
    pts = [(ws, d, v) for ws, d, v in pts if start <= d <= end]
    if len(pts) >= 2:
        line_pts = " ".join(f"{x(d):.1f},{y(v):.1f}" for _, d, v in pts)
        out.append(f'<polyline fill="none" class="stroke-{color_cls}" stroke-width="2.2" '
                   f'vector-effect="non-scaling-stroke" stroke-linejoin="round" points="{line_pts}"/>')
    elif len(pts) == 1:
        out.append(f'<circle cx="{x(pts[0][1]):.1f}" cy="{y(pts[0][2]):.1f}" r="4" class="c-{color_cls}"/>')
    today_x = x(min(max(today, start), end))
    out.append(f'<line x1="{today_x:.1f}" y1="{top}" x2="{today_x:.1f}" y2="{top + h}" '
               f'class="stroke-line" stroke-width="1"/>')
    aria = html.escape(f"{g['title']}: {t('goals.chart_aria', start=comun._short_date(t, start), end=comun._short_date(t, end))}")
    svg = (f'<svg viewBox="0 0 {width} {top + h + 6}" width="100%" height="110" role="img" aria-label="{aria}" '
           f'preserveAspectRatio="none" class="chart-svg">{"".join(out)}</svg>')
    # BODY-ESTADOS-VACIOS-2-M2: the legend names only what is actually drawn -- `pts` (not
    # `checks` before the start/end filter above) is the same condition the polyline/circle branch
    # just used, so zero weekly points in the plotted range means no "Tu promedio semanal" entry.
    avg_entry = (f'<span><i class="c-{color_cls} legend-line"></i>{t("goals.legend_avg")}</span>' if pts else "")
    legend = (
        '<div class="legend mt-4">'
        f'<span><i class="legend-dash"></i>{t("goals.legend_plan")}</span>'
        f'{avg_entry}'
        f'<span><i class="legend-today"></i>{t("goals.legend_today")}</span>'
        '</div>')
    data_pts = [{"x": max(0.0, min(1.0, (d - start).days / span_days)),
                 "label": t("goals.chart_week_label", date=comun._short_date(t, ws), value=comun._fig_unit(t, g["metric_key"], v, defs))}
                for ws, d, v in pts]
    return profile.chart_figure(svg + legend, data_pts)


def _verdict_sentence(t: T, defs: dict, g, check: dict) -> str:
    _, verdict = _verdict(t, check["on_track"])
    if g["kind"] == "metric" and check["metric_avg"] is not None:
        return t("goals.history.metric", verdict=verdict, value=comun._fig_unit(t, g["metric_key"], check["metric_avg"], defs))
    if g["kind"] == "milestone" and check["habits_due"]:
        return t("goals.history.milestone", verdict=verdict, done=check["habits_done"], due=check["habits_due"])
    return verdict


def _plan_estado_chip(t: T, estado: str) -> tuple[str, str]:
    return _PLAN_ESTADO_CLS.get(estado, ""), t("goals.plan.estado." + estado)


def _plan_hora_desde_minutos(total: float) -> str:
    """Duplicated from body/rutas/asistente.py's `_hora_desde_minutos` (that module is outside
    this slice's touches, same reasoning its own docstrings already give for duplicating out of
    this one): the inverse of horizonte._minutos_despues_mediodia's convention (minutes after
    12:00 noon, wrapping across midnight) -- 690 -> 23:30, 765 -> 00:45."""
    total = int(round(total))
    hh = ((total // 60) + 12) % 24
    mm = total % 60
    return f"{hh:02d}:{mm:02d}"


def _plan_medida_label(t: T, medida: str) -> str:
    if medida in horizonte.MEDIDAS:
        return t("asistente.propuesta.plan_medida." + medida)
    return t("metric." + medida)


def _plan_minutos_txt(t: T, valor) -> str:
    """Ruling 2 (BODY-PLAN-PANTALLA-1-R): one measure, one unit on a row -- the weekly target for
    a `min`-unit measure is written with the SAME formatter its real figure already uses on this
    row (t.minutes), so "sueño: al menos 420 min" beside "6 h 51 de media" becomes "al menos 7 h"
    beside it instead -- never two units on one row. A whole hour drops t.minutes' own trailing
    ":00" (420 -> "7 h", not "7 h 00")."""
    txt = t.minutes(valor)
    return txt[:-3] if txt.endswith(" 00") else txt


def _plan_target_txt(t: T, defs: dict, medida: str, sentido: str, valor) -> str:
    """The weekly target in plain words, reusing the same asistente.propuesta.plan_* vocabulary
    BODY-PLAN-PROPUESTAS-1-R already built for this exact phrasing (session/clock/minute shapes
    for horizonte.MEDIDAS, comun._fig_unit for any other metric_def key)."""
    info = horizonte.MEDIDAS.get(medida)
    sentido_txt = t("asistente.propuesta.plan_sentido." + sentido)
    if info is not None and info["unit"] == "hora":
        hora = _plan_hora_desde_minutos(valor)
        if medida == "hora_acostarse":
            clave = ("asistente.propuesta.plan_reloj_acostarse_antes" if sentido == "antes_de"
                     else "asistente.propuesta.plan_reloj_acostarse_despues")
        else:
            clave = ("asistente.propuesta.plan_reloj_levantarse_antes" if sentido == "antes_de"
                     else "asistente.propuesta.plan_reloj_levantarse_despues")
        return t(clave, hora=hora)
    if info is not None and info["unit"] == "sesiones":
        uno = valor == 1
        if medida == "sesiones_fuerza":
            nombre = (t("asistente.propuesta.plan_sesion_fuerza_one") if uno
                      else t("asistente.propuesta.plan_sesion_fuerza"))
        else:
            nombre = (t("asistente.propuesta.plan_sesion_cardio_one") if uno
                      else t("asistente.propuesta.plan_sesion_cardio"))
        return t("asistente.propuesta.plan_sesion_valor", sentido=sentido_txt, n=t.num(valor), medida=nombre)
    if info is not None:  # unit 'min'
        valor_txt = _plan_minutos_txt(t, valor)
    else:
        valor_txt = comun._fig_unit(t, medida, valor, defs)
    return t("asistente.propuesta.plan_medida_valor", medida=_plan_medida_label(t, medida), sentido=sentido_txt,
              valor=valor_txt)


def _plan_real_txt(t: T, defs: dict, medida: str, real) -> str:
    """The real figure in plain words, or the honest gap (DESIGN rule 6) when nothing was measured
    -- "2 so far" for a session count, "23:10 typical" for a clock-time median, a plain minutes
    figure for a sum/average, never 0 standing in for "no data"."""
    if real is None:
        return t("goals.plan.sin_dato_reloj")
    info = horizonte.MEDIDAS.get(medida)
    if info is not None and info["unit"] == "hora":
        return t("goals.plan.real_hora", hora=_plan_hora_desde_minutos(real))
    if info is not None and info["unit"] == "sesiones":
        return t("goals.plan.real_sesiones", n=t.num(real))
    if info is not None:  # unit 'min'
        return t("goals.plan.real_minutos", valor=t.minutes(real))
    return comun._fig_unit(t, medida, real, defs)


def _plan_zona_nota(t: T, linea: dict) -> str | None:
    """DESIGN rule 6 / ruling 2: a cardio line is counted by session, never by heart-rate zone
    (horizonte._real_medida's own `zona_medida: False`) -- said once, plainly, not left implicit."""
    return t("goals.plan.zona_no_medida") if linea.get("zona_medida") is False else None


def _plan_regla_txt(t: T, r: dict) -> str | None:
    """Ruling 2: a measurable rule gets its own "N of M days" this week; a text-only one (no
    chequeo) is never judged -- the person's own words stand alone."""
    if r["solo_texto"]:
        return None
    return t("goals.plan.rule_progress", cumple=t.num(r["dias_cumple"]), aplica=t.num(r["dias_aplica"]))


def _plan_sesiones_dias(t: T, sesiones: list[dict]) -> list[dict]:
    """Groups this week's planned `plan` rows by day, in day order (already guaranteed by
    horizonte.semana's own Monday..Sunday iteration) -- each group a short weekday+day label and
    its sessions, done/not done (ruling 2)."""
    out: list[dict] = []
    grupos: dict[str, dict] = {}
    for s in sesiones:
        grupo = grupos.get(s["day"])
        if grupo is None:
            d = date.fromisoformat(s["day"])
            # BODY-PLAN-PANTALLA-1: 'sesiones', never 'items' -- a dict's own `.items` method
            # shadows a same-named key under Jinja's attribute-then-item lookup, so
            # `{% for it in d.items %}` would iterate the builtin method, not this list.
            grupo = {"label": f"{t('date.days')[d.weekday()]} {d.day}", "sesiones": []}
            grupos[s["day"]] = grupo
            out.append(grupo)
        grupo["sesiones"].append({"title": s["title"], "time": s["time"], "done": s["status"] == "done"})
    return out


def _plan_checkpoint_ctx(t: T, defs: dict, c: dict) -> dict:
    cls, chip = _plan_estado_chip(t, c["estado"])
    criterios = []
    for crit in c["criterios"]:
        ccls, cchip = _plan_estado_chip(t, crit["estado"] or "pendiente")
        criterios.append({
            "target": _plan_target_txt(t, defs, crit["medida"], crit["sentido"], crit["valor"]),
            "real": _plan_real_txt(t, defs, crit["medida"], crit["real"]) if crit["estado"] else None,
            "cls": ccls, "chip": cchip,
        })
    return {"id": c["id"], "date": comun._short_date(t, date.fromisoformat(c["day"])), "titulo": c["titulo"],
            "nota": c.get("nota"), "cls": cls, "chip": chip, "criterios": criterios}


def _es_horizonte(conn, goal_id: int) -> bool:
    """Ruling 1: a milestone goal is a horizon once it carries ANY plan row -- a child goal, a
    rule, a week, a weekly target line or a checkpoint. One bounded EXISTS (ruling 5), independent
    of how many of any of those this goal has."""
    return bool(conn.execute(
        "SELECT EXISTS("
        "SELECT 1 FROM goal WHERE horizonte_goal_id = ? "
        "UNION SELECT 1 FROM horizonte_regla WHERE horizonte_goal_id = ? "
        "UNION SELECT 1 FROM horizonte_semana WHERE horizonte_goal_id = ? "
        "UNION SELECT 1 FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? "
        "UNION SELECT 1 FROM horizonte_checkpoint WHERE horizonte_goal_id = ?"
        ")", (goal_id,) * 5).fetchone()[0])


def _build_plan_ctx(conn, t: T, defs: dict, g, person_id: int, today: date, semana_q: str | None) -> dict:
    """Ruling 1-3: the plan section's whole context, built ONLY from horizonte.semana and
    horizonte.checkpoints -- never a second horizonte_* read. `semana_q` is the `?semana=` query
    param (a date, clamped to the horizon's own start_day..deadline, ruling 2's week navigation)."""
    goal_id = g["id"]
    start_monday = fechas.lunes_de(date.fromisoformat(g["start_day"]))
    end_monday = fechas.lunes_de(date.fromisoformat(g["deadline"]))
    this_monday = fechas.lunes_de(today)
    try:
        chosen = fechas.lunes_de(date.fromisoformat(semana_q)) if semana_q else this_monday
    except ValueError:
        chosen = this_monday
    chosen = max(start_monday, min(chosen, end_monday))

    semana_data = horizonte.semana(conn, goal_id, person_id, chosen.isoformat(), hoy=today)
    checkpoints_data = horizonte.checkpoints(conn, goal_id, person_id, hoy=today)

    lineas = []
    for linea in semana_data["lineas"]:
        cls, chip = _plan_estado_chip(t, linea["estado"])
        lineas.append({
            "target": _plan_target_txt(t, defs, linea["medida"], linea["sentido"], linea["valor"]),
            "real": _plan_real_txt(t, defs, linea["medida"], linea["real"]),
            "cls": cls, "chip": chip, "zona_nota": _plan_zona_nota(t, linea), "nota": linea.get("nota"),
        })

    reglas = [{"texto": r["texto"], "progreso": _plan_regla_txt(t, r)}
              for r in semana_data["reglas"] if r["dias_aplica"] > 0]

    n = (date.fromisoformat(g["deadline"]) - today).days
    if n < 0:
        days_left_text = t("goals.plan.ended")
    elif n == 1:
        days_left_text = t("goals.plan.days_left_one")
    else:
        days_left_text = t("goals.plan.days_left", n=t.num(n))

    targets = []
    for h in conn.execute("SELECT id, title FROM goal WHERE horizonte_goal_id = ? ORDER BY id", (goal_id,)):
        check = _current_week_check(conn, h["id"], person_id, this_monday.isoformat())
        cls, verdict = _verdict(t, check["on_track"])
        targets.append({"id": h["id"], "title": h["title"], "cls": cls, "verdict": verdict})

    experiments = [dict(r) for r in conn.execute(
        "SELECT id, titulo FROM experimento WHERE goal_id = ? AND person_id = ? ORDER BY id",
        (goal_id, person_id))]

    siguiente = next((c for c in checkpoints_data if c["estado"] == "pendiente"), None)
    pasados = [c for c in checkpoints_data if c["estado"] != "pendiente"]

    prev_monday, next_monday = chosen - timedelta(7), chosen + timedelta(7)

    return {
        "days_left_text": days_left_text,
        "week": {
            "label": t("goals.week_of", date=comun._short_date(t, chosen)),
            "titulo": semana_data["titulo"], "nota": semana_data["nota"], "lineas": lineas,
            "dias": _plan_sesiones_dias(t, semana_data["sesiones_planeadas"]), "reglas": reglas,
            "prev": prev_monday.isoformat() if prev_monday >= start_monday else None,
            "next": next_monday.isoformat() if next_monday <= end_monday else None,
        },
        "targets": targets,
        "checkpoint_next": _plan_checkpoint_ctx(t, defs, siguiente) if siguiente else None,
        "checkpoints_past": [_plan_checkpoint_ctx(t, defs, c) for c in pasados],
        "experiments": experiments,
    }


def _nest_horizontes(rows, built_cards: list[dict]) -> list[dict]:
    """Ruling 4: within this SAME /goals section, a horizon's own child goals nest under its card
    (`c['children']`) instead of standing as separate top-level rows -- no extra query, `rows` is
    already this section's own fetched goal rows. A child whose horizon is not in this same list
    (not accessible to this viewer, or sitting in a different status section) is simply never
    matched here, so it stays top-level."""
    by_id = {r["id"]: r for r in rows}
    cards_by_id = {c["id"]: c for c in built_cards}
    for c in built_cards:
        c["children"] = [cards_by_id[rid] for rid, r in by_id.items() if r["horizonte_goal_id"] == c["id"]]
    return [c for c in built_cards if by_id[c["id"]]["horizonte_goal_id"] not in by_id]


@router.get("/goals")
@comun._cierra_conexion
def goals_list(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    today = comun._today_local(person)
    week_start = fechas.lunes_de(today).isoformat()
    defs = comun._defs(conn)

    hh = household.household_of(conn, person_id)
    scope_sql, scope_params = goals.goal_scope_sql(person_id, hh)
    active = conn.execute(f"SELECT * FROM goal WHERE status = 'active' AND {scope_sql} ORDER BY id",
                          scope_params).fetchall()
    proposed = conn.execute(f"SELECT * FROM goal WHERE status = 'proposed' AND {scope_sql} ORDER BY id",
                            scope_params).fetchall()
    archived = conn.execute(
        f"SELECT * FROM goal WHERE status IN ('paused', 'done', 'dropped') AND {scope_sql} ORDER BY id",
        scope_params).fetchall()

    cards = []
    for g in active:
        check = _current_week_check(conn, g["id"], person_id, week_start)
        cls, verdict = _verdict(t, check["on_track"])
        if g["kind"] == "metric":
            fig = comun._fig_unit(t, g["metric_key"], check["metric_avg"], defs)
            started = (t("goals.card.started", value=comun._fig_unit(t, g["metric_key"], g["baseline_value"], defs))
                      if g["baseline_value"] is not None else None)
            pct = _metric_pct(g, check["metric_avg"])
        else:
            fig = t("goals.card.milestone_fig", done=check["habits_done"] or 0, due=check["habits_due"] or 0)
            started = None
            pct = _time_pct(g, today)
        cards.append({"id": g["id"], "title": g["title"], "cls": cls, "verdict": verdict, "fig": fig,
                      "period": t("goals.card.this_week"), "target_text": _goal_target_text(t, defs, g),
                      "started": started, "pct": round(pct) if pct is not None else None,
                      "shared_line": _shared_line(conn, t, g, person_id)})

    proposed_cards = [{"id": g["id"], "title": g["title"], "sub": _goal_target_text(t, defs, g),
                       "shared_line": _shared_line(conn, t, g, person_id)} for g in proposed]
    archived_cards = [{"id": g["id"], "title": g["title"], "status_text": t("goals.status." + g["status"]),
                       "shared_line": _shared_line(conn, t, g, person_id)} for g in archived]
    experiments_active, experiments_closed = experimentos.resumen_metas(conn, t, person_id, today)

    cards = _nest_horizontes(active, cards)
    proposed_cards = _nest_horizontes(proposed, proposed_cards)
    archived_cards = _nest_horizontes(archived, archived_cards)

    return comun._render("goals.html", t=t, tab="goals", cards=cards, proposed=proposed_cards, archived=archived_cards,
                   empty=not (active or proposed or archived), turnos=db.trabaja_turnos_si(conn, person_id),
                   experiments_active=experiments_active, experiments_closed=experiments_closed,
                   experiments_empty=not (experiments_active or experiments_closed))


@router.get("/goals/new")
@comun._cierra_conexion
def goal_new_form(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    defs = comun._defs(conn)
    today = comun._today_local(person).isoformat()
    bases = baselines(conn, list(defs), today, person_id=person_id)
    metrics = []
    for k, d in defs.items():
        b = bases[k]
        metrics.append({"key": k, "label": t("metric." + k), "unit": comun._unit(t, d),
                        "direction": BETTER_TO_DIRECTION[d["better"]], "baseline": b.mean if b else None})
    hh = household.household_of(conn, person_id)
    return comun._render("goal_new.html", t=t, tab="goals", metrics=metrics, areas=AREAS, directions=DIRECTIONS,
                   has_household=bool(hh and len(hh.member_ids) > 1), turnos=db.trabaja_turnos_si(conn, person_id))


@router.post("/goals/new")
@comun._cierra_conexion_async
async def goal_new_create(request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    f = await comun._form(request)
    kind = f.get("kind")
    title = (f.get("title") or "").strip()
    if not title or kind not in ("metric", "milestone"):
        return RedirectResponse("/goals/new", status_code=303)
    today = comun._today_local(person).isoformat()
    why = (f.get("why") or "").strip() or None
    area = f.get("area") if f.get("area") in AREAS else "body"
    hh = household.household_of(conn, person_id)
    # A household goal is a milestone in v1 (BODY-HOGAR-MIEMBRO-1-M): the server decides this, not
    # the form -- goals.propose refuses kind='metric' with visibility='household' too, but a metric
    # goal is always private whatever the (possibly tampered or JS-less) form sent.
    visibility = "household" if kind == "milestone" and f.get("visibility") == "household" and hh else "private"
    hogar_id = hh.id if visibility == "household" else None
    if kind == "metric":
        defs = comun._defs(conn)
        metric_key, direction = f.get("metric_key"), f.get("direction")
        if metric_key not in defs or direction not in DIRECTIONS or not f.get("deadline"):
            return RedirectResponse("/goals/new", status_code=303)
        try:
            target_value = float(f.get("target_value", ""))
            baseline_value = float(f.get("baseline_value", ""))
        except ValueError:
            return RedirectResponse("/goals/new", status_code=303)
        gid = goals.propose(conn, person_id, title, today, area=area, why=why, kind="metric",
                            metric_key=metric_key, direction=direction, target_value=target_value,
                            baseline_value=baseline_value, deadline=f["deadline"], hogar_id=hogar_id,
                            visibility=visibility)
    else:
        if not f.get("deadline"):
            return RedirectResponse("/goals/new", status_code=303)
        gid = goals.propose(conn, person_id, title, today, area=area, why=why, kind="milestone",
                            deadline=f["deadline"], hogar_id=hogar_id, visibility=visibility)
    habit_title, habit_cadence = _parse_new_habit(f, "habit_")
    if habit_title:
        goals.add_habit(conn, gid, habit_title, habit_cadence)
    return RedirectResponse(f"/goals/{gid}", status_code=303)


@router.get("/goals/{goal_id}")
@comun._cierra_conexion
def goal_detail(goal_id: int, request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    access = goals.goal_access(conn, goal_id, person_id)
    if access is None:
        raise HTTPException(404)
    g = conn.execute("SELECT * FROM goal WHERE id = ?", (goal_id,)).fetchone()
    defs = comun._defs(conn)
    today = comun._today_local(person)
    monday = fechas.lunes_de(today)
    week_start = monday.isoformat()
    # The current week is computed live, never stored by a GET (BODY-SEGURIDAD-3): a row for it,
    # if one exists from the daily pull, is excluded from the queries below and replaced with this
    # fresher value everywhere it would otherwise show, so it is never shown twice.
    current_check = goals.evaluar_semana(conn, goal_id, person_id, week_start)

    chart = None
    chart_sin_puntos = False
    if g["kind"] == "metric" and g["deadline"] and g["baseline_value"] is not None and g["target_value"] is not None:
        checks_for_chart = [dict(r) for r in conn.execute(
            "SELECT week_start, metric_avg FROM goal_check WHERE goal_id = ? AND person_id = ? "
            "AND metric_avg IS NOT NULL AND week_start != ? ORDER BY week_start",
            (goal_id, person_id, week_start)).fetchall()]
        if current_check is not None and current_check["metric_avg"] is not None:
            checks_for_chart.append({"week_start": week_start, "metric_avg": current_check["metric_avg"]})
        chart = _goal_line_svg(t, defs, g, checks_for_chart, today)
        # BODY-GOAL-CHART-NO-CAPTION-1: the dashed plan line draws with zero actual weekly points
        # (useful on its own -- it shows the path to the target before any data arrives), but
        # goals.chart_hint invites tapping/dragging weeks that are not there. Caption on the real
        # gap, goals.chart_soon, instead -- the same honest-gap sentence the no-chart-at-all branch
        # already uses. charts.js never attaches a tap affordance here either way: _goal_line_svg
        # builds data_pts from these same checks, so zero checks means an empty list, and
        # profile.chart_figure omits data-points entirely for an empty list (`if not points`) --
        # confirmed by reading chart_figure, not assumed.
        chart_sin_puntos = not checks_for_chart

    history_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? AND week_start != ? "
        "ORDER BY week_start DESC LIMIT 7", (goal_id, person_id, week_start)).fetchall()]
    if current_check is not None:
        history_rows.insert(0, current_check)
    history_list = [{"week": t("goals.week_of", date=comun._short_date(t, date.fromisoformat(r["week_start"]))),
                     "text": _verdict_sentence(t, defs, g, r)} for r in history_rows]

    habits = _habits_for_goal(conn, goal_id)
    sunday = monday + timedelta(6)
    weekly_mine_ids = [h["id"] for h in habits
                       if (h["assignee_person_id"] is None or h["assignee_person_id"] == person_id)
                       and comun._habit_cadence(h).get("per", "week") == "week"]
    week_checks = _week_checks(conn, weekly_mine_ids, person_id, monday, sunday)

    habit_rows = []
    for h in habits:
        cadence = comun._habit_cadence(h)
        is_mine = h["assignee_person_id"] is None or h["assignee_person_id"] == person_id
        row = {"id": h["id"], "title": h["title"], "cadence": _cadence_text(t, cadence),
               "context": comun._context_label(t, cadence.get("context")),
               "context_cls": comun._context_chip_cls(cadence.get("context")), "mine": is_mine}
        if is_mine:
            done, due, period = goals.habit_progress(conn, h, person_id, monday, today)
            if period == "month":
                progress = t("goals.habit.progress_month", done=done, due=due)
            elif due:
                progress = t("goals.habit.progress", done=done, due=due)
            else:
                progress = t("goals.habit.not_due")
            if cadence.get("per", "week") == "week":
                done_days = week_checks.get(h["id"], set())
                checked_today = today.isoformat() in done_days
                week_strip = _week_strip(t, monday, today, done_days)
            else:
                checked_today = comun._habit_checked(conn, h["id"], person_id, today.isoformat())
                week_strip = None
            row.update({
                "progress": progress,
                "due_today": goals.habit_due_today(conn, h, person_id, today),
                "checked_today": checked_today,
                "week_strip": week_strip,
            })
        else:
            other = conn.execute("SELECT display_name FROM person WHERE id = ?",
                                 (h["assignee_person_id"],)).fetchone()
            row["assignee_name"] = t("goals.habit_assignee", name=other["display_name"] if other else "?")
        habit_rows.append(row)

    members = None
    shared_line = None
    if g["visibility"] == "household":
        hh = household.household_of(conn, person_id)
        other_ids = [m for m in (hh.member_ids if hh else ()) if m != person_id]
        if other_ids:
            members = []
            for mid in other_ids:
                mrow = conn.execute("SELECT display_name FROM person WHERE id = ?", (mid,)).fetchone()
                crow = conn.execute(
                    "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? ORDER BY week_start DESC LIMIT 1",
                    (goal_id, mid)).fetchone()
                member_cls, verdict = _verdict(t, crow["on_track"] if crow else None)
                members.append({"name": mrow["display_name"] if mrow else f"#{mid}", "verdict": verdict,
                                "cls": member_cls})
        shared_line = _shared_line(conn, t, g, person_id)

    latest_on_track = history_rows[0]["on_track"] if history_rows else None
    cls, verdict = _verdict(t, latest_on_track)

    enlace_error_app = request.query_params.get("app")
    if request.query_params.get("enlace") != "invalido" or enlace_error_app not in enlaces.APPS:
        enlace_error_app = None

    # Ruling 1: the plan lives IN goal detail -- only for a milestone goal that is itself a
    # horizon (_es_horizonte); a metric goal never is, so it costs this view nothing extra.
    plan = None
    if g["kind"] == "milestone" and _es_horizonte(conn, goal_id):
        plan = _build_plan_ctx(conn, t, defs, g, person_id, today, request.query_params.get("semana"))

    # Ruling 4: a child goal's own "part of the plan" line, linking back to its horizon -- only
    # when this viewer can also see that horizon (goals.goal_access, the same check every other
    # cross-goal link in this app already goes through).
    part_of = None
    if g["horizonte_goal_id"] is not None:
        hrow = conn.execute("SELECT id, title FROM goal WHERE id = ?", (g["horizonte_goal_id"],)).fetchone()
        if hrow is not None and goals.goal_access(conn, hrow["id"], person_id) is not None:
            part_of = {"id": hrow["id"], "title": hrow["title"]}

    return comun._render("goal.html", t=t, tab="goals", goal=g, cls=cls, verdict=verdict, chart=chart,
                   chart_sin_puntos=chart_sin_puntos,
                   history=history_list, habits=habit_rows, members=members, shared_line=shared_line,
                   is_creator=(access == "creator"), enlaces=enlaces.enlaces_de(conn, goal_id),
                   apps_por_enlazar=enlaces.apps_por_enlazar(conn, goal_id), enlace_error_app=enlace_error_app,
                   target_text=_goal_target_text(t, defs, g), area_label=t("goals.area." + g["area"]),
                   today=today.isoformat(), turnos=db.trabaja_turnos_si(conn, person_id),
                   plan=plan, part_of=part_of)


def _require_creator(conn, goal_id: int, person_id: int) -> None:
    """Editing G and activate/pause/finish/drop stay with the creator in v1 (BODY-HOGAR-MIEMBRO-1
    rule 5) -- a member, or a stranger, gets 404 exactly like an unknown goal."""
    if goals.goal_access(conn, goal_id, person_id) != "creator":
        raise HTTPException(404)


@router.post("/goals/{goal_id}/activate")
@comun._cierra_conexion
def goal_activate(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    goals.activate(conn, goal_id)
    return RedirectResponse(f"/goals/{goal_id}", status_code=303)


@router.post("/goals/{goal_id}/pause")
@comun._cierra_conexion
def goal_pause(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    goals.pause(conn, goal_id)
    return RedirectResponse(f"/goals/{goal_id}", status_code=303)


@router.post("/goals/{goal_id}/finish")
@comun._cierra_conexion
def goal_finish(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    goals.finish(conn, goal_id)
    return RedirectResponse("/goals", status_code=303)


@router.post("/goals/{goal_id}/drop")
@comun._cierra_conexion
def goal_drop(goal_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    goals.drop(conn, goal_id)
    return RedirectResponse("/goals", status_code=303)


@router.post("/goals/{goal_id}/habits")
@comun._cierra_conexion_async
async def goal_add_habit(goal_id: int, request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    if goals.goal_access(conn, goal_id, person_id) is None:
        raise HTTPException(404)
    g = conn.execute("SELECT visibility FROM goal WHERE id = ?", (goal_id,)).fetchone()
    f = await comun._form(request)
    title, cadence = _parse_new_habit(f)
    if title:
        # BODY-HOGAR-MIEMBRO-1 rule 4: on a household goal, the adder (creator or member) picks
        # "for me" (assignee = themselves) or "for both" (assignee = NULL, the default).
        assignee = person_id if g["visibility"] == "household" and f.get("for") == "me" else None
        goals.add_habit(conn, goal_id, title, cadence, assignee_person_id=assignee)
    return RedirectResponse(f"/goals/{goal_id}", status_code=303)


@router.post("/goals/{goal_id}/enlaces")
@comun._cierra_conexion_async
async def goal_add_enlace(goal_id: int, request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    f = await comun._form(request)
    app_key = f.get("app")
    ref = enlaces.ref_desde_entrada(app_key, f.get("entrada"))
    if ref is None:
        suffix = f"&app={app_key}" if app_key in enlaces.APPS else ""
        return RedirectResponse(f"/goals/{goal_id}?enlace=invalido{suffix}#enlaces", status_code=303)
    enlaces.enlazar(conn, goal_id, app_key, ref)
    return RedirectResponse(f"/goals/{goal_id}#enlaces", status_code=303)


@router.post("/goals/{goal_id}/enlaces/{app_key}/quitar")
@comun._cierra_conexion
def goal_quitar_enlace(goal_id: int, app_key: str, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    _require_creator(conn, goal_id, person_id)
    enlaces.quitar(conn, goal_id, app_key)
    return RedirectResponse(f"/goals/{goal_id}#enlaces", status_code=303)


@router.post("/habits/{habit_id}/check")
@comun._cierra_conexion_async
async def habit_check(habit_id: int, request: Request, person_id: int = Depends(identity.current_person_id)):
    conn, person, _ = comun._ctx(person_id)
    f = await comun._form(request)
    day = f.get("day")
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise HTTPException(404)
    row = conn.execute(
        "SELECT h.assignee_person_id, h.goal_id FROM habit h WHERE h.id = ?", (habit_id,)).fetchone()
    if row is None or goals.goal_access(conn, row["goal_id"], person_id) is None:
        raise HTTPException(404)
    # BODY-HOGAR-MIEMBRO-1 rule 2: a member checks off a habit only when it is for both (NULL) or
    # for themselves; a habit assigned to someone else cannot be checked by this viewer.
    if row["assignee_person_id"] is not None and row["assignee_person_id"] != person_id:
        raise HTTPException(404)
    today = comun._today_local(person)
    try:
        goals.check_habit(conn, habit_id, person_id, day, f.get("done") == "1", hoy=today)
    except ValueError:
        raise HTTPException(404)
    # BODY-SEMANAS-TARDIAS-1: cerrar_semanas (the daily pull) only ever re-closes the current and
    # previous week, so a check for a day in an OLDER week would otherwise sit unreflected in
    # goal_check until a future backfill happens to revisit it. Re-close that one week, now, in
    # this same request -- a POST, so it may write. A day in this week or last is left to the
    # daily pull, which already owns both (goals.weekly_check no-ops for a non-active goal).
    week_start = fechas.lunes_de(date.fromisoformat(day))
    last_monday = fechas.lunes_de(today) - timedelta(7)
    if week_start < last_monday:
        goals.weekly_check(conn, row["goal_id"], person_id, week_start.isoformat())
    back = cuenta.safe_return_to(f.get("back"))
    return RedirectResponse(back, status_code=303)
