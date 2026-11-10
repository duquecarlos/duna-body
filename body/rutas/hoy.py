"""Hoy and Día (BODY-WEB-ESTRUCTURA-7): the readiness home screen (`/`), "¿Cómo se calcula?"
(`/how/{day}`) and the single day's full card set (`/day/{day}`) -- the last two screens still in
body/web.py before this slice. Pure move: path, method, templates and bodies identical.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from markupsafe import Markup

from .. import (compartir, cuenta, db, goals, history, household, identity, readiness, recuperacion, turnos_costo,
                tutoriales, ventana_comida)
from ..agente import conversacion as agente_conversacion
from ..baselines import baseline
from ..i18n import T
from . import comun, experimentos

router = APIRouter()

TODAY_CARDS = ["sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake"]
# BODY-SUENO-RESPIRACION-VISTA-1: the four night figures BODY-ENTRENO-SUENO-STORE-1 already pulls
# -- out of the generic "Todas las medidas" grid, into one honest line inside (or beside) the sleep
# card, with the caveat that this is a proxy, not a snoring or apnea reading (BODY-ENTRENO-1-INV
# Q6, DESIGN rule 12).
SUENO_RESP_KEYS = ("sleep_spo2_avg", "sleep_respiration_avg", "sleep_respiration_low", "sleep_respiration_high")
# Each stage's fixed palette class (body/static/body.css), never a var(...) string in markup (BODY-ESTILOS-1).
STAGES = [("sleep_deep_min", "metric.sleep_deep_min", "c-night"),
          ("sleep_light_min", "metric.sleep_light_min", "c-night-light"),
          ("sleep_rem_min", "metric.sleep_rem_min", "c-body"),
          ("sleep_awake_min", "metric.sleep_awake_min", "c-amber")]


def _delta(conn, t: T, d: dict, day: str, v, person_id: int) -> tuple[str, str]:
    """Delta vs your 30-day average: text and css class (good/bad/flat)."""
    if v is None:
        return "", "flat"
    b = baseline(conn, d["key"], day, person_id=person_id)
    if b is None:
        return "", "flat"
    diff = v - b.mean
    z = b.z(v)
    if z is None or abs(z) < 0.5:
        return t("delta.same"), "flat"
    arrow = "↑" if diff > 0 else "↓"
    amount = t.minutes(abs(diff)) if comun._is_minutes(d["key"]) else t.num(abs(diff))
    better = d["better"]
    cls = "flat" if better == "neutral" else ("good" if (diff > 0) == (better == "higher") else "bad")
    return f"{arrow} {amount} {t('delta.vs')}", cls


def _reading(t: T, r: readiness.Readiness) -> str:
    if not r.parts:
        return ""
    weakest = min(r.parts, key=lambda p: p.subscore)
    strongest = max(r.parts, key=lambda p: p.subscore)
    out = t("today.all_normal") if weakest.subscore >= 60 else t("today.weakest", part=t("part." + weakest.key))
    if strongest.subscore >= 80 and strongest is not weakest:
        out += " " + t("today.strongest", part=t("part." + strongest.key))
    return out


def _stale_ctx(t: T, day: str, today_local: date) -> Markup | None:
    """BODY-HOY-STALE-DATA-1: the readiness card renders the latest day WITH data, however old --
    honest (every figure is that day's own), but a returning person could mistake a stale card for
    today's. dias 0 or 1 is the normal morning state (today, or last night not yet synced) and
    changes nothing; dias >= 2 says the figures' own age once, plainly, above the score.

    BODY-ESTADOS-VACIOS-2-M2: hosted links to /garmin, the one page that actually shows the
    connection and the last sync error. Local gets NO link at all -- /settings has no sync row
    (comun._sync_status_text's own docstring: the sync itself is a per-install Task Scheduler
    setting this app cannot read or trigger), and a link that promises a page that does not exist
    is worse than the sentence standing alone."""
    dias = (today_local - date.fromisoformat(day)).days
    if dias < 2:
        return None
    text = t("today.stale", date=comun._short_date(t, date.fromisoformat(day)), n=dias)
    if not cuenta.hosted():
        return Markup(html.escape(text))
    return Markup(html.escape(text) + ' <a class="taplink" href="/garmin">' +
                  html.escape(t("today.stale_link")) + "</a>")


def _synced(conn, person) -> str | None:
    ts = db.last_sync(conn, person["id"])
    if not ts:
        return None
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    local = datetime.fromisoformat(ts).astimezone(tz)
    return local.strftime("%H:%M") if local.date() == datetime.now(tz).date() else None


def _parse_day(day: str) -> date:
    try:
        return date.fromisoformat(day)
    except ValueError:
        raise HTTPException(404)


def _today_due_habits(conn, t: T, person_id: int, today: date) -> list[dict]:
    hh = household.household_of(conn, person_id)
    scope_sql, scope_params = goals.goal_scope_sql(person_id, hh)
    rows = conn.execute(
        f"SELECT h.*, g.title AS goal_title FROM habit h JOIN goal g ON g.id = h.goal_id "
        f"WHERE g.status = 'active' AND {scope_sql} AND h.active = 1 "
        f"AND (h.assignee_person_id IS NULL OR h.assignee_person_id = ?) ORDER BY h.id",
        (*scope_params, person_id)).fetchall()
    out = []
    for h in rows:
        if not goals.habit_due_today(conn, h, person_id, today):
            continue
        context = comun._habit_cadence(h).get("context")
        out.append({"id": h["id"], "title": h["title"], "goal_title": h["goal_title"],
                    "context": comun._context_label(t, context), "context_cls": comun._context_chip_cls(context),
                    "checked": comun._habit_checked(conn, h["id"], person_id, today.isoformat())})
    return out


def _short_weekday_date(t: T, d: date) -> str:
    """jue 13 nov style: abbreviated weekday, day, abbreviated month -- no year, for a shift within
    the next 14 days (BODY-HOY-TURNO-1)."""
    return f"{t('date.days')[d.weekday()][:3]} {d.day} {t('date.months')[d.month - 1][:3]}"


def _dia_mes(t: T, d: date) -> str:
    """"5 nov" style: day, abbreviated month, no year -- same shape as experimentos.py's own
    _dia_mes, not reused directly since neither module imports the other."""
    return f"{d.day} {t('date.months')[d.month - 1][:3]}"


def _periodo_hoy_ctx(t: T, conn, person_id: int, today_local: date) -> dict | None:
    """BODY-PERIODO-PANTALLAS-1: one plain line on the readiness card while a period (illness,
    travel, injury) covers today -- open, or closed after today -- naming the kind and when it
    started, plus a real link to the assistant (the only place a period is closed, BODY-PERIODO-1),
    never the person's own note text (DESIGN: that stays inside the assistant panel's own card).
    None when no period covers today. db.periodos' own per-connection cache means this costs no
    extra SQL on a page that already calls baseline() for the readiness card and TODAY_CARDS."""
    hoy_iso = today_local.isoformat()
    cubren = db.periodos(conn, person_id, hoy_iso, hoy_iso)
    if not cubren:
        return None
    p = cubren[-1]
    fecha = _dia_mes(t, date.fromisoformat(p["start_day"]))
    return {"texto": t("today.periodo", kind=t("periodo.kind." + p["kind"]), fecha=fecha),
            "enlace": t("today.periodo_enlace")}


def _mcp_pendientes_ctx(conn, person_id: int) -> dict | None:
    """BODY-MCP-LOCAL-1-B: Hoy's own line while the person's personal assistant (the local MCP
    server) has proposed something still waiting in the chat panel -- None while there is none, so
    the line never shows an empty count. `conversacion_id` is the newest such conversation, for the
    line's own link to open the panel on."""
    n, conversacion_id = agente_conversacion.pendientes_mcp(conn, person_id)
    return {"n": n, "conversacion_id": conversacion_id} if n else None


def _hoy_turno_ctx(t: T, conn, person_id: int, today_local: date) -> dict:
    """Hoy's second promise (BODY-HOY-TURNO-1): today's shift, the next one, and a recovery line --
    one vocabulary with Turnos' strip (shift.kind.*, the same band classes)."""
    turno = turnos_costo.turno_de_hoy(conn, person_id, today_local)
    hoy, siguiente, recuperacion = turno["hoy"], turno["siguiente"], turno["recuperacion"]

    hoy_texto = None
    if hoy is not None:
        palabra = t("shift.kind." + hoy["kind"])
        if hoy["start_local"] and hoy["end_local"]:
            hoy_texto = t("today.shift_today_hours", turno=palabra,
                         inicio=hoy["start_local"][11:16], fin=hoy["end_local"][11:16])
        else:
            hoy_texto = t("today.shift_today", turno=palabra)

    siguiente_texto = None
    if siguiente is not None and siguiente["start_local"]:
        palabra = t("shift.kind." + siguiente["kind"])
        fecha = _short_weekday_date(t, date.fromisoformat(siguiente["start_local"][:10]))
        if siguiente["end_local"]:
            siguiente_texto = t("today.shift_next_hours", turno=palabra, fecha=fecha,
                                inicio=siguiente["start_local"][11:16], fin=siguiente["end_local"][11:16])
        else:
            siguiente_texto = t("today.shift_next", turno=palabra, fecha=fecha)

    recuperacion_texto = None
    if recuperacion is not None:
        recuperacion_texto = t("today.recovery", dia=recuperacion["dia"], estimado=recuperacion["estimado"])

    return {"clase": hoy["kind"] if hoy is not None else None, "hoy_texto": hoy_texto,
            "siguiente_texto": siguiente_texto, "recuperacion_texto": recuperacion_texto}


def _dia_relativo(t: T, day: str, today_local: date) -> str:
    """«hoy», «ayer», or the short date, for a shared figure's own day (BODY-HOGAR-COMPARTIR-1)."""
    d = date.fromisoformat(day)
    if d == today_local:
        return t("hogar.dia_hoy")
    if d == today_local - timedelta(1):
        return t("hogar.dia_ayer")
    return comun._short_date(t, d)


def _hogar_compartido_ctx(conn, t: T, person_id: int, defs: dict, today_local: date) -> list[dict]:
    """Hoy's «Tu hogar» card: each shared figure visible to this viewer (compartir.visibles_para),
    the app's existing value+unit formatting, and -- DESIGN rule 3's one exception, the row is
    never a link, since nothing of the owner's sits behind it for this viewer to open -- a plain-
    words provenance line (who shared it, that it came from their watch, and «hoy»/«ayer»/the short
    date) in place of one. A plain "no data" line when the owner has no value yet (never a guess)."""
    out = []
    for item in compartir.visibles_para(conn, person_id):
        sin_dato = item["value"] is None
        out.append({
            "name": item["name"],
            "metric_label": t("metric." + item["metric_key"]),
            "sin_dato": sin_dato,
            "value_text": None if sin_dato else comun._fig_unit(t, item["metric_key"], item["value"], defs),
            "procedencia": None if sin_dato else t("today.hogar.procedencia", name=item["name"],
                                                    day=_dia_relativo(t, item["day"], today_local)),
        })
    return out


def _ventana_base(t: T, v: ventana_comida.Ventana | None) -> dict:
    """The part of the eating-window card shared by Hoy and Día: which state the day is in, and
    the figures that state needs -- Hoy adds "fasting now"/yesterday's meal-to-sleep gap, Día adds
    the overnight fast before it (BODY-AYUNO-1)."""
    if v is None:
        estado = "sin_registro"
    elif v.primera is None:
        estado = "sin_hora"
    elif v.duracion_min == 0:
        estado = "una_comida"
    else:
        estado = "con_ventana"
    return {
        "estado": estado,
        "duracion": t.minutes(v.duracion_min) if v and v.duracion_min else None,
        "primera": v.primera[11:16] if v and v.primera else None,
        "ultima": v.ultima[11:16] if v and v.ultima else None,
        "entradas": v.entradas if v else None,
    }


def _ventana_ctx(t: T, conn, person: dict, person_id: int, today_local: date) -> dict:
    """Hoy's eating-window card: today's own window, plus the running "fasting now" count (from
    the last timed meal, whichever day it fell on) and whether last night's sleep followed
    yesterday's last meal by a few hours -- the 3-4h check Carlos is watching this week."""
    day = today_local.isoformat()
    ctx = _ventana_base(t, ventana_comida.ventana(conn, person_id, day))
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    ahora_h = ventana_comida.ayuno_ahora(conn, person_id, datetime.now(tz).replace(tzinfo=None))
    cs = ventana_comida.comida_a_sueno(conn, person_id, (today_local - timedelta(1)).isoformat())
    ctx["ayuno_ahora"] = t.minutes(round(ahora_h * 60)) if ahora_h is not None else None
    ctx["comida_sueno"] = t.minutes(round(cs.horas * 60)) if cs is not None else None
    return ctx


def _ventana_dia_ctx(t: T, conn, person_id: int, d: date) -> dict:
    """Día's eating-window card: that day's own window plus the overnight fast before it."""
    day = d.isoformat()
    ctx = _ventana_base(t, ventana_comida.ventana(conn, person_id, day))
    previo = ventana_comida.ayuno_previo(conn, person_id, day)
    ctx["ayuno_previo"] = t.minutes(round(previo.horas * 60)) if previo is not None else None
    return ctx


@router.get("/")
@comun._cierra_conexion
def today(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    day = comun._latest_day(conn, person_id)
    proposed_count = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_batch WHERE person_id = ? AND status = 'proposed'",
        (person_id,),
    ).fetchone()["n"]
    primeros = tutoriales.primeros_pasos(conn, person_id, cuenta.hosted())
    primeros_pasos = primeros if any(not p["hecho"] for p in primeros) else None
    today_local = comun._today_local(person)
    turno_ctx = _hoy_turno_ctx(t, conn, person_id, today_local)
    mcp_pendientes = _mcp_pendientes_ctx(conn, person_id)
    if not day:
        return comun._render("soon.html", t=t, tab="today", title=t("today.title"),
                       text=comun._sync_status_text(conn, person, t), primeros_pasos=primeros_pasos,
                       proposed_count=proposed_count, turno=turno_ctx, turnos=turnos_on)
    defs = comun._defs(conn)
    vals = db.day_values(conn, day, person_id)
    r = readiness.compute(conn, day, person_id)
    rb = baseline(conn, "readiness", day, person_id=person_id)
    ready_avg = ready_delta = None
    ready_cls = "flat"
    if r.score is not None and rb:
        ready_avg = round(rb.mean)
        ready_delta, ready_cls = _delta(conn, t, defs["readiness"], day, r.score, person_id)
    cards = []
    for k in TODAY_CARDS:
        v = vals.get(k)
        delta, cls = _delta(conn, t, defs[k], day, v, person_id)
        cards.append({"key": k, "label": t("metric." + k), "value": comun._value(t, k, v),
                      "unit": comun._unit(t, defs[k]) if v is not None else "", "delta": delta, "cls": cls})
    return comun._render("today.html", t=t, tab="today", day=day, date_long=t.long_date(date.fromisoformat(day)),
                   synced=_synced(conn, person), stale=_stale_ctx(t, day, today_local),
                   r=r, reading=_reading(t, r), ready_avg=ready_avg,
                   ready_delta=ready_delta, ready_cls=ready_cls, cards=cards,
                   missing_txt=", ".join(t("part." + m) for m in r.missing), proposed_count=proposed_count,
                   turno=turno_ctx, turnos=turnos_on, mcp_pendientes=mcp_pendientes,
                   plan_habits=_today_due_habits(conn, t, person_id, today_local),
                   plan_experimentos=experimentos.hoy_contexto(conn, t, person_id, today_local),
                   today_iso=today_local.isoformat(),
                   primeros_pasos=primeros_pasos,
                   hogar_compartido=_hogar_compartido_ctx(conn, t, person_id, defs, today_local),
                   ventana=_ventana_ctx(t, conn, person, person_id, today_local),
                   periodo=_periodo_hoy_ctx(t, conn, person_id, today_local))


# readiness.Part.key -> the Tendencias metric it reads from (readiness.Part carries no metric key of its
# own). BODY-PULIDO-1: how()'s part rows link to their own measure through this.
PART_METRIC = {"hrv": "hrv_last_night", "sleep": "sleep_minutes", "battery": "body_battery_wake",
               "resting_hr": "resting_hr", "stress": "stress_avg"}


def _rango_que_contiene(dia: date, today_local: date) -> str:
    """The Tendencias range whose window actually contains `dia`, relative to the person's own today
    (BODY-PULIDO-1-M: r=30 opened for a day outside that 30-day window). Within the last 30 days -> r=30;
    within 90 -> r=90; older -> no r at all, the monthly/all-time view that reaches any day."""
    age = (today_local - dia).days
    if age <= 30:
        return "r=30"
    if age <= 90:
        return "r=90"
    return ""


def _tendencias_href(key: str, desde: str, dia: str, today_local: date) -> str | None:
    """A measure's own Tendencias card, with a way back to where the tap came from -- Dia's cards and
    Como lo calculo's part rows (BODY-PULIDO-1), same shape as Turnos' cost rows. None when `key` has
    no card there (comun.TREND_KEYS or an extra group), so the caller leaves the figure unlinked."""
    if key not in comun.TREND_KEYS and not comun._es_extra_metric(key):
        return None
    todas = "&todas=1" if comun._es_extra_metric(key) else ""
    rango = _rango_que_contiene(date.fromisoformat(dia), today_local)
    r_part = f"{rango}&" if rango else ""
    return f"/trends?{r_part}desde={desde}&dia={dia}{todas}#{key}"


@router.get("/how/{day}")
@comun._cierra_conexion
def how(day: str, person_id: int = Depends(identity.current_person_id)):
    d = _parse_day(day)
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    today_local = comun._today_local(person)
    r = readiness.compute(conn, d.isoformat(), person_id)
    parts = []
    for p in r.parts:
        ref_note = ""
        if p.key == "sleep":
            value, ref, ref_note = t.minutes(p.value), t.minutes(p.reference), t("how.need_short")
        else:
            value, ref = t.num(p.value), (t.num(p.reference) if p.reference is not None else "—")
        label = t("part." + p.key)
        metric_key = PART_METRIC.get(p.key)
        href = (_tendencias_href(metric_key, "como", d.isoformat(), today_local)
                if metric_key and p.value is not None else None)
        parts.append({"label": label[:1].upper() + label[1:], "value": value, "ref": ref, "ref_note": ref_note,
                      "score": t.num(p.subscore), "weight": f"{round(p.weight * 100)} %", "href": href})
    missing = [t("part." + m) for m in r.missing]
    return comun._render("how.html", t=t, tab="today", r=r, parts=parts, date_long=t.long_date(d),
                   missing=[m[:1].upper() + m[1:] for m in missing], turnos=turnos_on)


def _sueno_resp_linea(t: T, vals: dict, defs: dict) -> str | None:
    """BODY-SUENO-RESPIRACION-VISTA-1: Día's one line for SpO2 and breathing while asleep -- a
    missing figure drops its own part (never 0, never "—" standing in for it); with none of the
    four, no line at all. The range (low-high) and the average read as one respiration figure --
    Garmin's sleep DTO gives both together or neither -- so a lone average still gets its own
    wording rather than being dropped with the range."""
    spo2 = vals.get("sleep_spo2_avg")
    avg = vals.get("sleep_respiration_avg")
    low = vals.get("sleep_respiration_low")
    high = vals.get("sleep_respiration_high")
    if spo2 is None and avg is None and low is None and high is None:
        return None
    parts = []
    if spo2 is not None:
        parts.append(t("day.sueno_resp.oxigeno", valor=comun._fig_unit(t, "sleep_spo2_avg", spo2, defs)))
    if low is not None and high is not None and avg is not None:
        parts.append(t("day.sueno_resp.respiracion", low=t.num(low), high=t.num(high), avg=t.num(avg)))
    elif low is not None and high is not None:
        parts.append(t("day.sueno_resp.respiracion_rango", low=t.num(low), high=t.num(high)))
    elif avg is not None:
        parts.append(t("day.sueno_resp.respiracion_prom", avg=t.num(avg)))
    return " · ".join(parts)


def _activity_name(t: T, a) -> str:
    """Día's activity list (DESIGN rule 10): an unnamed activity shows its translated kind when
    Garmin's own type code is one this app knows (the i18n map `_kind_label` reads); an unknown
    code never prints the raw code, only a plain generic label."""
    if a["name"]:
        return a["name"]
    if a["type"] in history.KNOWN_ACTIVITY_TYPES:
        return t("activity.type." + a["type"])
    return t("activity.generic")


def _recovery_lookup(conn, person_id: int, activities: list[dict]) -> dict[tuple[str, str], object]:
    """BODY-RECUPERACION-1: one SQL statement for every one of this day's activities' own
    recovery row (current model version), keyed by (source, source_id) -- never one query per
    activity."""
    if not activities:
        return {}
    ids = [a["source_id"] for a in activities]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT activity_source, activity_source_id, minutos, motivo FROM recuperacion_actividad "
        f"WHERE person_id = ? AND modelo_version = ? AND activity_source_id IN ({placeholders})",
        (person_id, recuperacion.MODELO["version"], *ids),
    ).fetchall()
    return {(r["activity_source"], r["activity_source_id"]): r for r in rows}


_RECOVERY_REASON_KEY = {
    "sin_cobertura": "day.recovery.reason.sin_cobertura",
    "sin_rango": "day.recovery.reason.sin_rango",
    "fuera_de_horizonte": "day.recovery.reason.fuera_de_horizonte",
}


def _recovery_line(t: T, row) -> str | None:
    """BODY-RECUPERACION-1: the Día line under an activity -- None (no line at all) when there is
    no row yet, or when the day itself is marked (the marked-day line elsewhere already says so).
    The reason -> key map is explicit, never a concatenated "day.recovery.reason." + motivo, so a
    new reason code is a one-line addition here, never an unreviewed dynamic key."""
    if row is None or row["motivo"] == "marcado":
        return None
    if row["motivo"] == "fuera_de_horizonte":
        return t(_RECOVERY_REASON_KEY["fuera_de_horizonte"], horas=t.num(recuperacion.MODELO["horizonte_h"]))
    if row["motivo"] is not None:
        return t(_RECOVERY_REASON_KEY[row["motivo"]])
    return t("day.recovery.ok", min=t.num(round(row["minutos"])))


def _periodo_dia_ctx(t: T, conn, person_id: int, dia_iso: str) -> str | None:
    """BODY-PERIODO-PANTALLAS-1: Día's own line, in words, naming every kind that covers this one
    day -- never the person's own note text (DESIGN: stays inside the assistant panel's own card)."""
    cubren = db.periodos(conn, person_id, dia_iso, dia_iso)
    if not cubren:
        return None
    kinds = ", ".join(t("periodo.kind." + p["kind"]) for p in cubren)
    return t("day.periodo", kind=kinds)


@router.get("/day/{day}")
@comun._cierra_conexion
def day_view(day: str, person_id: int = Depends(identity.current_person_id)):
    d = _parse_day(day)
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    today_local = comun._today_local(person)
    key = d.isoformat()
    periodo_dia = _periodo_dia_ctx(t, conn, person_id, key)
    data = db.load_day(conn, key, person_id)
    vals = {m["key"]: m["value"] for m in data["metrics"]}
    defs = comun._defs(conn)
    cards = []
    for k, df in defs.items():
        if k in SUENO_RESP_KEYS:
            continue
        v = vals.get(k)
        delta, cls = _delta(conn, t, df, key, v, person_id)
        href = _tendencias_href(k, "dia", key, today_local) if v is not None else None
        cards.append({"key": k, "label": t("metric." + k), "value": comun._value(t, k, v), "raw": v,
                      "unit": comun._unit(t, df), "delta": delta, "cls": cls, "est": df["kind"] == "estimated",
                      "href": href})
    sueno_resp_linea = _sueno_resp_linea(t, vals, defs)
    sleep = None
    if vals.get("sleep_minutes") is not None:
        total_stage = sum(vals.get(s[0]) or 0 for s in STAGES)
        stages = [{"label": t(lbl), "value": t.minutes(vals[k]), "cls": cls,
                   "pct": round(100 * vals[k] / total_stage, 1)}
                  for k, lbl, cls in STAGES if vals.get(k)] if total_stage else []
        win = data["sleep"] or {}
        window = " → ".join(x[11:16] for x in (win.get("start_local"), win.get("end_local")) if x)
        sleep = {"total": t.minutes(vals["sleep_minutes"]), "window": window, "stages": stages}
    recuperaciones = _recovery_lookup(conn, person_id, data["activities"])
    acts = []
    for a in data["activities"]:
        bits = []
        if a["duration_s"]:
            bits.append(t.minutes(a["duration_s"] / 60))
        if a["distance_m"]:
            bits.append(f"{t.num(a['distance_m'] / 1000, 1)} km")
        if a["avg_hr"]:
            bits.append(f"{t.num(a['avg_hr'])} {t('unit.bpm')}")
        recovery_row = recuperaciones.get((a["source"], a["source_id"]))
        acts.append({"name": _activity_name(t, a), "detail": " · ".join(bits),
                     "recovery": _recovery_line(t, recovery_row)})
    shifts = []
    for s in data["shifts"]:
        kind = s["kind"]
        key = "shift.kind." + kind
        label = t(key)
        if label == key:
            label = kind
        shifts.append({"kind": kind, "text": f"{label}{' (' + s['code'] + ')' if s['code'] else ''} "
                       f"{(s['start_local'] or '')[11:16]} → {(s['end_local'] or '')[11:16]}"})
    has_data = any(v is not None for v in vals.values())
    ctx = turnos_costo.contexto_dia(conn, person_id, d)
    block_ctx = None
    if ctx is not None:
        if ctx["tipo"] == "noche":
            block_ctx = t("day.shift_ctx_noche", n=ctx["n"], de=ctx["de"])
        else:
            block_ctx = t("day.shift_ctx_despues", n=ctx["n"])
    return comun._render("day.html", t=t, tab="today", date_long=t.long_date(d), cards=cards, sleep=sleep,
                   sueno_resp_linea=sueno_resp_linea,
                   activities=acts, shifts=shifts, has_data=has_data, block_ctx=block_ctx,
                   sync_text=None if has_data else comun._sync_status_text(conn, person, t),
                   prev=(d - timedelta(1)).isoformat(), next=(d + timedelta(1)).isoformat(),
                   turnos=turnos_on, ventana=_ventana_dia_ctx(t, conn, person_id, d), periodo=periodo_dia)
