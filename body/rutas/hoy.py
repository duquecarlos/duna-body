"""Hoy and Día (BODY-WEB-ESTRUCTURA-7): the readiness home screen (`/`), "¿Cómo se calcula?"
(`/how/{day}`) and the single day's full card set (`/day/{day}`) -- the last two screens still in
body/web.py before this slice. Pure move: path, method, templates and bodies identical.
"""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from markupsafe import Markup

from .. import (anatomia, compartir, cuenta, db, goals, history, household, identity, noche_inusual, readiness,
                recuperacion, turnos_costo, tutoriales, ventana_comida)
from ..agente import conversacion as agente_conversacion
from ..agente import herramientas as agente_herramientas
from ..agente import propuestas as agente_propuestas
from ..baselines import baseline
from ..i18n import T
from . import comun, experimentos

router = APIRouter()

TODAY_CARDS = ["sleep_minutes", "hrv_last_night", "resting_hr", "body_battery_wake", "stress_avg", "steps"]
# BODY-HOY-FICHAS-EXTRA-1 (ruling 4): these two tiles get a band word against the person's own
# 25th-75th-percentile range (never a generic table) and, on today's own row only, a partial-day
# caption -- the other four tiles are fixed-at-wake figures with neither.
PARTIAL_TODAY_KEYS = frozenset({"stress_avg", "steps"})
# BODY-SUENO-RESPIRACION-VISTA-1: the four night figures BODY-ENTRENO-SUENO-STORE-1 already pulls
# -- out of the generic "Todas las medidas" grid, into one honest line inside (or beside) the sleep
# card, with the caveat that this is a proxy, not a snoring or apnea reading (BODY-ENTRENO-1-INV
# Q6, DESIGN rule 12).
SUENO_RESP_KEYS = ("sleep_spo2_avg", "sleep_respiration_avg", "sleep_respiration_low", "sleep_respiration_high")
# BODY-SUENO-FC-1: the sleep-window heart rate, out of the generic grid and into its own line
# inside the sleep card, same reasoning SUENO_RESP_KEYS above already carries.
SUENO_FC_KEYS = ("sleep_hr_avg", "sleep_hr_min")
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


def _nivel_tile(b, v) -> str | None:
    """BODY-HOY-FICHAS-EXTRA-1 (ruling 4): low/usual/high against the person's OWN 25th-75th-
    percentile range (baselines.Baseline.low/high) -- never a generic table. None with no baseline
    or no value yet (the honest gap)."""
    if b is None or v is None:
        return None
    if v < b.low:
        return "low"
    if v > b.high:
        return "high"
    return "usual"


def _nivel_label(t: T, nivel: str | None) -> str | None:
    if nivel == "low":
        return t("today.tile.banda.low")
    if nivel == "high":
        return t("today.tile.banda.high")
    if nivel == "usual":
        return t("today.tile.banda.usual")
    return None


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
        cadence = comun._habit_cadence(h)
        context = cadence.get("context")
        out.append({"id": h["id"], "title": h["title"], "goal_title": h["goal_title"],
                    "context": comun._context_label(t, context), "context_cls": comun._context_chip_cls(context),
                    "checked": comun._habit_checked(conn, h["id"], person_id, today.isoformat()),
                    # BODY-HOY-DIA-POR-DELANTE-1 (ruling 2): an optional clock time, additive inside
                    # the existing cadence JSON blob -- never read by habit_due_today/habit_progress,
                    # so a habit with no "hora" behaves exactly as before.
                    "hora": cadence.get("hora")})
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


def _mcp_aplicado_ctx(conn, person_id: int, ahora: datetime | None = None) -> dict | None:
    """BODY-MCP-APLICAR-1 ruling 5: Hoy's own aviso line for 24 h after the person's personal
    assistant applied a change on its own (through body/mcp.py's `aplicar_propuesta`), so nothing
    lands silently -- None once every such apply has aged past the window, same "no line, no empty
    count" shape _mcp_pendientes_ctx above already uses. `ahora`: an injectable clock for tests
    that must cross the 24h edge without sleeping; real UTC now in production."""
    ahora = ahora or datetime.now(timezone.utc)
    n, conversacion_id = agente_conversacion.aplicados_mcp_recientes(conn, person_id, ahora)
    return {"n": n, "conversacion_id": conversacion_id} if n else None


def _lista_natural(t: T, items: list[str]) -> str:
    """Joins 2+ phrases the way each language actually reads a short list -- "A y B" / "A, B y C"
    (es), "A and B" / "A, B, and C" (en) -- never a bare comma list (BODY-NOCHE-INUSUAL-1: the
    unusual-night line's own signals, always 2 or 3 of them, ruling 2's "one signal alone never
    flags")."""
    if len(items) == 1:
        return items[0]
    if t.lang == "en":
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    if len(items) == 2:
        return f"{items[0]} y {items[1]}"
    return ", ".join(items[:-1]) + f" y {items[-1]}"


def _noche_inusual_senales(t: T, noche: noche_inusual.Noche) -> str:
    return _lista_natural(t, [t("noche.senal." + key) for key, _ in noche.senales])


def _noche_inusual_hoy_ctx(t: T, conn, person_id: int, day: str, vals: dict) -> dict | None:
    """BODY-NOCHE-INUSUAL-1 ruling 1: Hoy's own plain `aviso` line, the morning after a night that
    read as unusual against the person's own normal -- None once the person dismissed it (ruling
    5) or the night was normal/unjudgeable (body/noche_inusual.py's own `evaluar`, ruling 3): in
    both cases, no line and no "normal" claim either."""
    if noche_inusual.vista(conn, person_id, day):
        return None
    noche = noche_inusual.evaluar(conn, person_id, day, vals)
    if noche is None:
        return None
    return {"texto": t("today.noche_inusual", senales=_noche_inusual_senales(t, noche)),
            "enlace": t("today.noche_inusual_link"), "day": day,
            "nota": noche_inusual.nota(conn, person_id, day)}


def _noche_inusual_dia_ctx(t: T, conn, person_id: int, day: str, vals: dict) -> dict | None:
    """BODY-NOCHE-INUSUAL-1 ruling 1: Día's own line inside that night's sleep card, with the "Ya lo
    vi" dismiss -- same "None means no line, no normal claim" rule as Hoy's own line above."""
    if noche_inusual.vista(conn, person_id, day):
        return None
    noche = noche_inusual.evaluar(conn, person_id, day, vals)
    if noche is None:
        return None
    return {"texto": t("day.noche_inusual", senales=_noche_inusual_senales(t, noche))}


def _plan_categorias_hoy(conn, person_id: int, today_local: date) -> list | None:
    """BODY-CUERPO-VISTA-1: today's own training plan, if any -- `plan.ejercicios_json` is "shape
    owned by the tool that writes it" (body/db.py), so an exercise's own `categoria` key is read
    only if present, never guessed from its name. `planear_entreno` (BODY-CUERPO-VISTA-1-M ruling 2)
    validates that key against `anatomia.CATEGORIA_ENUM` before it is ever stored; a row stored
    before that validation existed, under the old free-text vocabulary, just reads as unrecognised
    (body/anatomia.py's own CATEGORIA_GRUPOS lights nothing for it). None when no plan with
    exercises covers today at all, so the caption can tell "no session" from "session, no
    recognised category" (both read as "no session" today, but only the first one really is)."""
    row = _plan_hoy_row(conn, person_id, today_local)
    if row is None:
        return None
    return [e.get("categoria") for e in json.loads(row["ejercicios_json"])]


def _plan_hoy_row(conn, person_id: int, today_local: date):
    """The one `plan` row BODY-CUERPO-VISTA-1's body view and BODY-HOY-DIA-POR-DELANTE-1's "day
    ahead" session row both read -- one query, shared, never two (same row either way)."""
    return conn.execute(
        "SELECT title, time, ejercicios_json FROM plan WHERE person_id = ? AND day = ? AND status != 'skipped' "
        "AND ejercicios_json IS NOT NULL ORDER BY id LIMIT 1",
        (person_id, today_local.isoformat())).fetchone()


def _plan_hoy_item(t: T, conn, person_id: int, today_local: date) -> dict | None:
    """BODY-HOY-DIA-POR-DELANTE-1 (ruling 2): today's own planned session as one "day ahead" row --
    an honest exercise count, never an invented duration the plan row does not carry. None with no
    session planned for today."""
    row = _plan_hoy_row(conn, person_id, today_local)
    if row is None:
        return None
    ejercicios = json.loads(row["ejercicios_json"])
    return {"tipo": "sesion", "hora": row["time"], "titulo": row["title"],
            "linea": t("today.dia.sesion_n", n=len(ejercicios))}


def _actividades_hoy(conn, person_id: int, today_local: date) -> list[dict]:
    """BODY-HOY-DIA-POR-DELANTE-1 (ruling 2): today's own already-logged activities, each with its
    own start time -- a future activity cannot appear here before the watch has synced it, the same
    honest-gap rule every other figure in this app follows."""
    # BODY-ENTRENO-VINCULO-1 ruling 3: a linked pair is one row here too -- `db.merge_linked_training`
    # needs every column (the link itself, both halves' own name/day) to fold the pair, so this
    # reads `SELECT *` rather than naming the four columns the caller actually wants.
    rows = db.merge_linked_training(conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day = ? ORDER BY start_local",
        (person_id, today_local.isoformat())).fetchall())
    return [{"name": r["name"], "type": r["type"], "start_local": r["start_local"],
             "duration_s": r["duration_s"]} for r in rows]


def _dia_por_delante_ctx(t: T, conn, person_id: int, today_local: date, turno_hoy: dict | None) -> list[dict]:
    """BODY-HOY-DIA-POR-DELANTE-1 (rulings 1, 2): one time-ordered list for today, merging today's
    shift, today's planned session, today's logged activities, and today's due habits/experiments
    -- each row carries its own time when a source gives one; an item with no time keeps the
    SOURCE order below (shift, session, activities, habits, experiments), stable-sorted after every
    timed row (never invents a time or a duration no source gives -- DESIGN rule 6). Never the
    autonomous-sounding copy the mock's own "Body moved Lower B to Saturday" implies (DESIGN rule
    14; BODY-MOCK-R2-CENSO-1-INV conflict #3) -- Body proposes, the person confirms; this is a read.
    """
    items: list[dict] = []

    if turno_hoy is not None:
        if turno_hoy["start_local"] and turno_hoy["end_local"]:
            items.append({
                "tipo": "turno", "hora": turno_hoy["start_local"][11:16],
                "titulo": t("shift.kind.title." + turno_hoy["kind"]),
                "linea": t("today.dia.turno_hasta", fin=turno_hoy["end_local"][11:16]),
            })
        else:
            items.append({"tipo": "turno", "hora": None,
                          "titulo": t("shift.kind.title." + turno_hoy["kind"]), "linea": None})

    sesion = _plan_hoy_item(t, conn, person_id, today_local)
    if sesion is not None:
        items.append(sesion)

    for a in _actividades_hoy(conn, person_id, today_local):
        items.append({
            "tipo": "actividad", "hora": a["start_local"][11:16] if a["start_local"] else None,
            "titulo": _activity_name(t, a),
            "linea": t.minutes(a["duration_s"] / 60) if a["duration_s"] else None,
        })

    for h in _today_due_habits(conn, t, person_id, today_local):
        items.append({"tipo": "habito", "hora": h["hora"], "titulo": h["title"], "linea": h["goal_title"],
                      "id": h["id"], "checked": h["checked"],
                      "context": h["context"], "context_cls": h["context_cls"]})

    for e in experimentos.hoy_contexto(conn, t, person_id, today_local):
        items.append({"tipo": "experimento", "hora": None, "titulo": e["linea"], "linea": None,
                      "id": e["id"], "marcado": e["marcado"], "habito": e["habito"]})

    items.sort(key=lambda it: (it["hora"] is None, it["hora"] or ""))
    return items


def _semana_nocturna_ctx(t: T, conn, person_id: int, today_local: date) -> dict | None:
    """BODY-HOY-SEMANA-NOCTURNA-1 (ruling 6): Hoy's own forward-looking card for the next night
    block within 7 days -- the block's own dates and the person's own historical recovery estimate
    (turnos_costo.resumen's `dias_recuperacion`), never a claim Body will remind or already acted
    (DESIGN rule 14; BODY-MOCK-R2-CENSO-1-INV conflict #1/#3). None when no night block starts that
    soon."""
    bloque = turnos_costo.siguiente_bloque_noche(conn, person_id, today_local)
    if bloque is None:
        return None
    inicio, fin = date.fromisoformat(bloque["inicio"]), date.fromisoformat(bloque["fin"])
    fechas = (_short_weekday_date(t, inicio) if inicio == fin
              else t("today.semana_nocturna.rango", desde=_short_weekday_date(t, inicio),
                     hasta=_short_weekday_date(t, fin)))
    dias_recuperacion = turnos_costo.resumen(conn, person_id, today_local)["dias_recuperacion"]
    recuperacion = (t("today.semana_nocturna.recuperacion", dias=dias_recuperacion)
                    if dias_recuperacion is not None else t("today.semana_nocturna.sin_dato"))
    return {"fechas": fechas, "recuperacion": recuperacion}


def _cuerpo_hoy_ctx(t: T, conn, person_id: int, today_local: date) -> dict:
    """Hoy's own body view (BODY-CUERPO-VISTA-1, ruling 4): Músculos/Espalda/Corazón y vasos, lit
    for today's planned session's own muscle groups -- the empty, "no session" caption with no
    plan, or with a plan whose categories none are recognised (never an empty parenthesis).
    BODY-LESION-ZONA-1 ruling 4: a second, quiet layer for any zona an open lesion period names for
    today -- drawn as a hatch, never the training glow, with its own caption below."""
    categorias = _plan_categorias_hoy(conn, person_id, today_local)
    grupos = anatomia.grupos_de_categorias(categorias) if categorias is not None else frozenset()
    if not grupos:
        caption = t("cuerpo.caption.hoy_sin_sesion")
    else:
        nombres = [t("cuerpo.grupo." + g) for g in anatomia.grupos_ordenados(grupos)]
        caption = t("cuerpo.caption.hoy_con_sesion", grupos=_lista_natural(t, nombres))
    zonas = db.zonas_en_recuperacion(conn, person_id, today_local.isoformat())
    grupos_limitados = anatomia.grupos_de_categorias(zonas)
    caption_limitada = None
    if grupos_limitados:
        nombres_zonas = [t("entreno.categoria." + z) for z in anatomia.CATEGORIA_ENUM if z in zonas]
        caption_limitada = t("cuerpo.caption.en_recuperacion", zonas=_lista_natural(t, nombres_zonas))
    return {"vistas": anatomia.contexto(t, "hoy", grupos, grupos_limitados), "caption": caption,
            "caption_limitada": caption_limitada, "recorte": "completo"}


def _hoy_turno_ctx(t: T, turno: dict) -> dict:
    """Hoy's second promise (BODY-HOY-TURNO-1): today's shift, the next one, and a recovery line --
    one vocabulary with Turnos' strip (shift.kind.*, the same band classes). `turno` is the caller's
    own single `turnos_costo.turno_de_hoy` read (BODY-HOY-DIA-POR-DELANTE-1 shares it with "The day
    ahead" list, rather than reading it twice)."""
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
    primeros = tutoriales.primeros_pasos(conn, person_id, cuenta.hosted(), t)
    primeros_pasos = primeros if any(not p["hecho"] for p in primeros) else None
    today_local = comun._today_local(person)
    turno_de_hoy = turnos_costo.turno_de_hoy(conn, person_id, today_local)
    turno_ctx = _hoy_turno_ctx(t, turno_de_hoy)
    cuerpo_ctx = _cuerpo_hoy_ctx(t, conn, person_id, today_local)
    mcp_pendientes = _mcp_pendientes_ctx(conn, person_id)
    mcp_aplicado = _mcp_aplicado_ctx(conn, person_id)
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
    synced = _synced(conn, person)
    es_hoy = day == today_local.isoformat()
    cards = []
    for k in TODAY_CARDS:
        v = vals.get(k)
        delta, cls = _delta(conn, t, defs[k], day, v, person_id)
        label = t("today.tile.stress_so_far") if k == "stress_avg" else t("metric." + k)
        banda = caption = None
        if k in PARTIAL_TODAY_KEYS:
            b = baseline(conn, k, day, person_id=person_id)
            banda = _nivel_label(t, _nivel_tile(b, v))
            if es_hoy and synced:
                caption = t("today.tile.hasta", hora=synced)
        cards.append({"key": k, "label": label, "value": comun._value(t, k, v),
                      "unit": comun._unit(t, defs[k]) if v is not None else "", "delta": delta, "cls": cls,
                      "banda": banda, "caption": caption})
    return comun._render("today.html", t=t, tab="today", day=day, date_long=t.long_date(date.fromisoformat(day)),
                   synced=synced, stale=_stale_ctx(t, day, today_local), cuerpo=cuerpo_ctx,
                   r=r, reading=_reading(t, r), ready_avg=ready_avg,
                   ready_delta=ready_delta, ready_cls=ready_cls, cards=cards,
                   missing_txt=", ".join(t("part." + m) for m in r.missing), proposed_count=proposed_count,
                   turno=turno_ctx, turnos=turnos_on, mcp_pendientes=mcp_pendientes, mcp_aplicado=mcp_aplicado,
                   dia_por_delante=_dia_por_delante_ctx(t, conn, person_id, today_local, turno_de_hoy["hoy"]),
                   semana_nocturna=_semana_nocturna_ctx(t, conn, person_id, today_local),
                   today_iso=today_local.isoformat(),
                   primeros_pasos=primeros_pasos,
                   hogar_compartido=_hogar_compartido_ctx(conn, t, person_id, defs, today_local),
                   ventana=_ventana_ctx(t, conn, person, person_id, today_local),
                   periodo=_periodo_hoy_ctx(t, conn, person_id, today_local),
                   noche_inusual=_noche_inusual_hoy_ctx(t, conn, person_id, day, vals))


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


def _sueno_fc_linea(t: T, vals: dict, defs: dict) -> str | None:
    """BODY-SUENO-FC-1: Dia's one line for the sleep-window heart rate -- avg and low always come
    from the same `sueno_fc.noche()` call, so either both are present or neither is; nothing when
    the night has no value (the honest gap). The unit rides on the average alone (comun._fig_unit),
    never repeated for the low -- same shape the spec's own example uses."""
    avg = vals.get("sleep_hr_avg")
    low = vals.get("sleep_hr_min")
    if avg is None or low is None:
        return None
    return t("day.sueno_fc.linea", avg=comun._fig_unit(t, "sleep_hr_avg", avg, defs), low=t.num(low))


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


def _comidas_dia_ctx(t: T, conn, person_id: int, defs: dict, dia_iso: str) -> list[dict]:
    """Día's own meals (BODY-COMIDA-1): one line per meal -- a `registrar_comida` group or a
    legacy single-amount `ingesta` row, `db.comidas_del_dia` groups both the same way -- each
    amount in the catalog's own order (db.INGESTA_METRIC_KEYS), marked «≈» when estimated."""
    out = []
    for m in db.comidas_del_dia(conn, person_id, dia_iso):
        por_clave = {c["metric_key"]: c for c in m["cantidades"]}
        cantidades = [{"label": t("metric." + k), "valor": comun._fig_unit(t, k, por_clave[k]["amount"], defs),
                       "estimado": por_clave[k]["estimado"]}
                      for k in db.INGESTA_METRIC_KEYS if k in por_clave]
        out.append({"hora": m["ts_local"][11:16] if m["ts_local"] else None,
                     "descripcion": m["descripcion"], "cantidades": cantidades})
    return out


def _momento_checkin_txt(t: T, momento: str) -> str:
    """BODY-CHECKIN-1: three literal t() calls, the same choice body/rutas/asistente.py's own copy
    of this helper makes, for the same reason (_periodo_dia_ctx's neighbour, test_i18n_paridad.py's
    dynamic-prefix guard needs no new entry). Duplicated rather than shared -- neither module may
    import the other for one helper."""
    if momento == "despertar":
        return t("checkin.momento.despertar")
    if momento == "post_turno":
        return t("checkin.momento.post_turno")
    return t("checkin.momento.otro")


def _checkins_dia_ctx(t: T, conn, person_id: int, defs: dict, vals: dict, dia_iso: str) -> dict | None:
    """Día's own «Cómo te sentiste» section (BODY-CHECKIN-1 ruling 5): absent (None) on a day with
    no check-in, no nag. The note (ruling 4, private by default) is read only here, through
    db.checkins_del_dia -- never through db.load_day, which the assistant's leer_dia also reads.
    The watch-vs-felt stress line is ONE plain line, shown only when both sides exist for this same
    day (`vals`, already loaded by the caller) -- never a combined score, never a cause."""
    eventos = db.checkins_del_dia(conn, person_id, dia_iso)
    if not eventos:
        return None
    salida = []
    for e in eventos:
        valores = [{"etiqueta": t("metric." + k), "valor": comun._fig_unit(t, k, e[k], defs)}
                   for k in db.CHECKIN_METRIC_KEYS if e[k] is not None]
        salida.append({
            "hora": e["ts_local"][11:16] if e["ts_local"] else None,
            "momento": _momento_checkin_txt(t, e["momento"]),
            "valores": valores,
            "nota": t("asistente.propuesta.checkin_nota", nota=e["nota"]) if e["nota"] else None,
        })
    comparacion = None
    reloj, felt = vals.get("stress_avg"), vals.get("stress_felt")
    if reloj is not None and felt is not None:
        comparacion = t("day.checkin.comparacion", reloj=t.num(reloj, 0), felt=t.num(felt, 0))
    return {"eventos": salida, "comparacion": comparacion}


def _sesion_con_hora_txt(titulo: str, hora_local: str | None) -> str:
    hora = hora_local[11:16] if hora_local else None
    return f"{titulo} ({hora})" if hora else titulo


def _entreno_vinculo_txt(t: T, accion: str, despues: dict) -> str:
    """BODY-ENTRENO-VINCULO-1 rulings 4, 5: the same two plain-word card texts body/rutas/
    asistente.py's own `_tarjeta_separar_sesion`/`_tarjeta_unir_sesiones` build, duplicated rather
    than shared -- neither module may import the other for one helper (the same choice
    `_momento_checkin_txt` above already makes)."""
    if accion == "separar_sesion":
        reloj = _sesion_con_hora_txt(despues["reloj_titulo"], despues.get("reloj_hora"))
        manual = _sesion_con_hora_txt(despues["manual_titulo"], despues.get("manual_hora"))
        return t("asistente.propuesta.entreno_separar", reloj=reloj, manual=manual)
    return t("asistente.propuesta.entreno_unir", reloj=despues["reloj_titulo"], manual=despues["manual_titulo"])


def _propuestas_vinculo_dia_ctx(conn, t: T, person_id: int, day: str) -> list[dict]:
    """Día's own inline cards for a pending or applied `separar_sesion`/`unir_sesiones` proposal on
    this day (BODY-ENTRENO-VINCULO-1 rulings 4, 5): plain text plus which buttons apply, built here
    rather than reusing the chat panel's own card machinery (DESIGN: a Día-raised proposal has no
    conversation the person would otherwise open)."""
    filas = conn.execute(
        "SELECT * FROM propuesta_agente WHERE person_id = ? AND accion IN ('separar_sesion', "
        "'unir_sesiones') AND estado IN ('propuesta', 'aplicada') ORDER BY creada_en DESC",
        (person_id,),
    ).fetchall()
    out = []
    for f in filas:
        despues = json.loads(f["despues_json"])
        if despues.get("day") != day:
            continue
        out.append({"id": f["id"], "aplicada": f["estado"] == "aplicada",
                     "texto": _entreno_vinculo_txt(t, f["accion"], despues)})
    return out


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
        if k in SUENO_RESP_KEYS or k in SUENO_FC_KEYS:
            continue
        v = vals.get(k)
        delta, cls = _delta(conn, t, df, key, v, person_id)
        href = _tendencias_href(k, "dia", key, today_local) if v is not None else None
        cards.append({"key": k, "label": t("metric." + k), "value": comun._value(t, k, v), "raw": v,
                      "unit": comun._unit(t, df), "delta": delta, "cls": cls, "est": df["kind"] == "estimated",
                      "href": href})
    sueno_resp_linea = _sueno_resp_linea(t, vals, defs)
    sueno_fc_linea = _sueno_fc_linea(t, vals, defs)
    noche_inusual_ctx = _noche_inusual_dia_ctx(t, conn, person_id, key, vals)
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
                     "recovery": _recovery_line(t, recovery_row),
                     # BODY-LESION-ZONA-1 ruling 2: the one shared plain line, from db.load_day's
                     # own zonas_limitadas (raw zona codes -- never the period's own note).
                     "lesion": anatomia.texto_liviano_a_proposito(t, a["zonas_limitadas"]),
                     # BODY-ENTRENO-VINCULO-1 ruling 3: the "joined" line and the Separar control
                     # -- source_id always names the watch half (db.merge_linked_training's own
                     # identity), which `_separar_sesion` resolves either way.
                     "vinculado": a["vinculado"], "source_id": a["source_id"]})
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
    # BODY-ENTRENO-VINCULO-1 ruling 5: shown only when this day has exactly one unlinked manual
    # session and one unlinked watch strength session -- never a guess.
    union_oferta = db.candidatos_union_entreno(conn, person_id, d.isoformat()) is not None
    vinculo_tarjetas = _propuestas_vinculo_dia_ctx(conn, t, person_id, d.isoformat())
    return comun._render("day.html", t=t, tab="today", day=d.isoformat(), date_long=t.long_date(d), cards=cards,
                   sleep=sleep, sueno_resp_linea=sueno_resp_linea, sueno_fc_linea=sueno_fc_linea,
                   noche_inusual=noche_inusual_ctx,
                   activities=acts, shifts=shifts, has_data=has_data, block_ctx=block_ctx,
                   sync_text=None if has_data else comun._sync_status_text(conn, person, t),
                   prev=(d - timedelta(1)).isoformat(), next=(d + timedelta(1)).isoformat(),
                   turnos=turnos_on, ventana=_ventana_dia_ctx(t, conn, person_id, d), periodo=periodo_dia,
                   comidas=_comidas_dia_ctx(t, conn, person_id, defs, d.isoformat()),
                   checkin=_checkins_dia_ctx(t, conn, person_id, defs, vals, d.isoformat()),
                   union_oferta=union_oferta, vinculo_tarjetas=vinculo_tarjetas)


@router.post("/day/{day}/noche-inusual/visto")
@comun._cierra_conexion_async
async def noche_inusual_visto(day: str, request: Request, person_id: int = Depends(identity.current_person_id)):
    """BODY-NOCHE-INUSUAL-1 ruling 5: "Ya lo vi" -- only ever acts on the signed-in person's OWN
    dismissal (noche_inusual.marcar_vista is scoped by person_id like every write in this app), so
    there is no other person's id to check against here. BODY-HOY-NOCHE-NOTA-1: an optional `back`
    field, read the same way metas.py's habit_check already does (cuenta.safe_return_to), so Hoy's
    own "Body noticed" card can dismiss without leaving Hoy -- a bare POST (Día's own button, no
    `back` field) keeps returning to the day page exactly as before. The fallback is passed THROUGH
    safe_return_to too (never used unwrapped), so tests/test_seguridad.py's static scan -- which
    only recognises a bare `cuenta.safe_return_to(...)` call, not a branch around it -- can still
    prove this call site safe without a named ALLOWLIST entry."""
    d = _parse_day(day)
    conn, _, _ = comun._ctx(person_id)
    noche_inusual.marcar_vista(conn, person_id, d.isoformat())
    f = await comun._form(request)
    back = cuenta.safe_return_to(f.get("back") or f"/day/{day}")
    return RedirectResponse(back, status_code=303)


@router.post("/day/{day}/noche-inusual/nota")
@comun._cierra_conexion_async
async def noche_inusual_nota(day: str, request: Request, person_id: int = Depends(identity.current_person_id)):
    """BODY-HOY-NOCHE-NOTA-1 (ruling 5): the person's own note on this one unusual night -- typed
    by hand, the same explicit exception DESIGN rule 1 already carries for checkin_nota, since only
    the person can say why. An empty or whitespace-only note is never stored."""
    d = _parse_day(day)
    conn, _, _ = comun._ctx(person_id)
    f = await comun._form(request)
    texto = (f.get("nota") or "").strip()
    if texto:
        noche_inusual.guardar_nota(conn, person_id, d.isoformat(), texto)
    back = cuenta.safe_return_to(f.get("back") or f"/day/{day}")
    return RedirectResponse(back, status_code=303)


@router.post("/day/{day}/entreno/{source_id}/separar")
@comun._cierra_conexion_async
async def entreno_separar(day: str, source_id: str, request: Request,
                           person_id: int = Depends(identity.current_person_id)):
    """BODY-ENTRENO-VINCULO-1 ruling 4: Día's own "Separar" control -- raises the SAME
    `separar_sesion` proposal a chat/MCP call would, through `herramientas._separar_sesion` and
    `agente_propuestas.crear`, in a fresh conversation this one control owns. A day/source_id with
    no linked pair left (already separated, a stale link) is a no-op redirect, never a 500 -- the
    control is only ever shown next to a currently-linked activity, so this is purely defensive."""
    d = _parse_day(day)
    conn, _, _ = comun._ctx(person_id)
    conversacion_id = agente_conversacion.crear(conn, person_id)
    try:
        agente_herramientas._separar_sesion(conn, person_id, conversacion_id, d.isoformat(), source_id)
    except agente_herramientas.ToolError:
        pass
    f = await comun._form(request)
    back = cuenta.safe_return_to(f.get("back") or f"/day/{day}")
    return RedirectResponse(back, status_code=303)


@router.post("/day/{day}/entreno/unir")
@comun._cierra_conexion_async
async def entreno_unir(day: str, request: Request, person_id: int = Depends(identity.current_person_id)):
    """BODY-ENTRENO-VINCULO-1 ruling 5: Día's own "Unir" control -- same shape as `entreno_separar`
    above, for `unir_sesiones`. A day with no exact one-manual-one-watch pair left is a no-op
    redirect -- the control is only shown while `db.candidatos_union_entreno` finds one."""
    d = _parse_day(day)
    conn, _, _ = comun._ctx(person_id)
    conversacion_id = agente_conversacion.crear(conn, person_id)
    try:
        agente_herramientas._unir_sesiones(conn, person_id, conversacion_id, d.isoformat())
    except agente_herramientas.ToolError:
        pass
    f = await comun._form(request)
    back = cuenta.safe_return_to(f.get("back") or f"/day/{day}")
    return RedirectResponse(back, status_code=303)


@comun._cierra_conexion_async
async def _entreno_propuesta_resolver(day: str, propuesta_id: int, request: Request, person_id: int,
                                       accion) -> RedirectResponse:
    """BODY-ENTRENO-VINCULO-1 ruling 4: the one place Día's own Aplicar/Descartar/Deshacer controls
    call into -- the SAME `agente_propuestas` functions body/rutas/asistente.py's own generic JSON
    routes call (no second apply path), redirecting back to Día instead of returning JSON. A stale
    or someone-else's id is a no-op redirect, same as a drifted/expired one already resolves to a
    plain estado string rather than raising."""
    conn, _, _ = comun._ctx(person_id)
    try:
        accion(conn, person_id, propuesta_id)
    except agente_propuestas.PropuestaAjena:
        pass
    f = await comun._form(request)
    back = cuenta.safe_return_to(f.get("back") or f"/day/{day}")
    return RedirectResponse(back, status_code=303)


@router.post("/day/{day}/entreno/propuestas/{propuesta_id}/aplicar")
@comun._cierra_conexion_async
async def entreno_propuesta_aplicar(day: str, propuesta_id: int, request: Request,
                                     person_id: int = Depends(identity.current_person_id)):
    return await _entreno_propuesta_resolver(day, propuesta_id, request, person_id, agente_propuestas.aplicar)


@router.post("/day/{day}/entreno/propuestas/{propuesta_id}/descartar")
@comun._cierra_conexion_async
async def entreno_propuesta_descartar(day: str, propuesta_id: int, request: Request,
                                       person_id: int = Depends(identity.current_person_id)):
    return await _entreno_propuesta_resolver(day, propuesta_id, request, person_id, agente_propuestas.descartar)


@router.post("/day/{day}/entreno/propuestas/{propuesta_id}/deshacer")
@comun._cierra_conexion_async
async def entreno_propuesta_deshacer(day: str, propuesta_id: int, request: Request,
                                      person_id: int = Depends(identity.current_person_id)):
    return await _entreno_propuesta_resolver(day, propuesta_id, request, person_id, agente_propuestas.deshacer)
