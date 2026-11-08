"""Metas: goals, their weekly check, and habits (BODY-WEB-ESTRUCTURA-5, pure move out of
body/web.py): `/goals`, `/goals/new` GET+POST, `/goals/{goal_id}` and its actions, and
`/habits/{habit_id}/check`.
"""
from __future__ import annotations

import html
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import cuenta, db, enlaces, fechas, goals, household, identity, profile
from ..baselines import baseline
from ..i18n import T
from . import comun

router = APIRouter()

AREAS = ["body", "money", "adventure", "growth", "work", "home"]
DIRECTIONS = ["raise", "lower", "keep"]
CONTEXTS = ("before_night", "after_night", "off_days")
BETTER_TO_DIRECTION = {"higher": "raise", "lower": "lower", "neutral": "keep"}


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
    legend = (
        '<div class="legend mt-4">'
        f'<span><i class="legend-dash"></i>{t("goals.legend_plan")}</span>'
        f'<span><i class="c-{color_cls} legend-line"></i>{t("goals.legend_avg")}</span>'
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

    return comun._render("goals.html", t=t, tab="goals", cards=cards, proposed=proposed_cards, archived=archived_cards,
                   empty=not (active or proposed or archived), turnos=db.trabaja_turnos_si(conn, person_id))


@router.get("/goals/new")
@comun._cierra_conexion
def goal_new_form(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    defs = comun._defs(conn)
    today = comun._today_local(person).isoformat()
    metrics = []
    for k, d in defs.items():
        b = baseline(conn, k, today, person_id=person_id)
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
    if g["kind"] == "metric" and g["deadline"] and g["baseline_value"] is not None and g["target_value"] is not None:
        checks_for_chart = [dict(r) for r in conn.execute(
            "SELECT week_start, metric_avg FROM goal_check WHERE goal_id = ? AND person_id = ? "
            "AND metric_avg IS NOT NULL AND week_start != ? ORDER BY week_start",
            (goal_id, person_id, week_start)).fetchall()]
        if current_check is not None and current_check["metric_avg"] is not None:
            checks_for_chart.append({"week_start": week_start, "metric_avg": current_check["metric_avg"]})
        chart = _goal_line_svg(t, defs, g, checks_for_chart, today)

    history_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM goal_check WHERE goal_id = ? AND person_id = ? AND week_start != ? "
        "ORDER BY week_start DESC LIMIT 7", (goal_id, person_id, week_start)).fetchall()]
    if current_check is not None:
        history_rows.insert(0, current_check)
    history_list = [{"week": t("goals.week_of", date=comun._short_date(t, date.fromisoformat(r["week_start"]))),
                     "text": _verdict_sentence(t, defs, g, r)} for r in history_rows]

    habit_rows = []
    for h in _habits_for_goal(conn, goal_id):
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
            row.update({
                "progress": progress,
                "due_today": goals.habit_due_today(conn, h, person_id, today),
                "checked_today": comun._habit_checked(conn, h["id"], person_id, today.isoformat()),
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

    return comun._render("goal.html", t=t, tab="goals", goal=g, cls=cls, verdict=verdict, chart=chart,
                   history=history_list, habits=habit_rows, members=members, shared_line=shared_line,
                   is_creator=(access == "creator"), enlaces=enlaces.enlaces_de(conn, goal_id),
                   apps_por_enlazar=enlaces.apps_por_enlazar(conn, goal_id), enlace_error_app=enlace_error_app,
                   target_text=_goal_target_text(t, defs, g), area_label=t("goals.area." + g["area"]),
                   today=today.isoformat(), turnos=db.trabaja_turnos_si(conn, person_id))


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
