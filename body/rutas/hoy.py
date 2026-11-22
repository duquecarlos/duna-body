"""Hoy and Día (BODY-WEB-ESTRUCTURA-7): the readiness home screen (`/`), "¿Cómo se calcula?"
(`/how/{day}`) and the single day's full card set (`/day/{day}`) -- the last two screens still in
body/web.py before this slice. Pure move: path, method, templates and bodies identical.
"""
from __future__ import annotations

import html
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from markupsafe import Markup

from .. import (anatomia, captura_sueno, comida_compartida, compartir, cuenta, db, despertar_real, fechas, fuentes,
                garmin_cuenta, goals, history, horizonte, household, identity, noche_inusual, nutriente_objetivo,
                pull, readiness, recuperacion, reentrada, registro_cobertura, turnos_costo, tutoriales,
                ventana_comida, viajes)
from ..agente import conversacion as agente_conversacion
from ..agente import herramientas as agente_herramientas
from ..agente import propuestas as agente_propuestas
from ..baselines import baseline
from ..desplazamiento_import import FUENTE as _FUENTE_COMMUTE
from ..i18n import T
from . import comun, experimentos
from . import garmin as garmin_rutas

router = APIRouter()

# BODY-PLAN-HOY-1 ruling 2: "Tu plan esta semana" shows at most this many active plans, each its
# own block; beyond it, one "y N planes más" line instead (a named constant, never a bare 2).
PLANES_HOY_MAX = 2
# Ruling 2: the same chip colours goal.html's own plan section already uses (metas.py's own
# _PLAN_ESTADO_CLS) -- duplicated here, not imported, since metas.py keeps it private and
# body/rutas/comun.py (the shared-plumbing home) is outside this slice's touches.
_PLAN_ESTADO_CLS = {"cumple": "ok", "en_curso": "warn", "no_cumple": "low", "sin_dato": "off"}

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


def _debe_lanzar_pull_hoy(conn, person_id: int) -> bool:
    """BODY-SYNC-VIVO-2 ruling 2: whether Hoy's own `GET /` should schedule `pull.pull_hoy_ahora`
    as a BackgroundTask -- hosted, with a Garmin account, and `puede_intentar` true right now
    (ESPACIO_MIN_HOY, the same ceiling `pull_hoy_ahora` itself re-checks once the task actually
    runs). This pre-check only avoids scheduling a background task `pull_hoy_ahora` would
    immediately refuse anyway on every single request; it does not re-check quiet hours (ruling
    4), which `pull_hoy_ahora` alone enforces."""
    if not cuenta.hosted():
        return False
    if not garmin_cuenta.conectado(conn, person_id):
        return False
    return garmin_cuenta.puede_intentar(conn, person_id, datetime.now(timezone.utc), pull.ESPACIO_MIN_HOY)


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
    travel, injury, or BODY-PLAN-FASES-4 ruling 2's training-through-it) covers today -- open, or
    closed after today -- naming the kind and when it started, plus a real link to the assistant
    (the only place a period is closed, BODY-PERIODO-1), never the person's own note text (DESIGN:
    that stays inside the assistant panel's own card). None when no period covers today.
    db.periodos' own per-connection cache means this costs no extra SQL on a page that already
    calls baseline() for the readiness card and TODAY_CARDS.

    BODY-PLAN-FASES-4 ruling 4: a kind with excluye_del_rango=1 keeps today's own wording
    ("today.periodo", which already says those days are left out); one with excluye_del_rango=0
    uses "today.periodo_incluido" instead, read from the data, never a hardcoded kind check."""
    hoy_iso = today_local.isoformat()
    cubren = db.periodos(conn, person_id, hoy_iso, hoy_iso)
    if not cubren:
        return None
    p = cubren[-1]
    fecha = _dia_mes(t, date.fromisoformat(p["start_day"]))
    clave = "today.periodo" if db.kind_excluye_del_rango(p["kind"]) else "today.periodo_incluido"
    return {"texto": t(clave, kind=t("periodo.kind." + p["kind"]), fecha=fecha),
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


def _hogar_aplicado_ctx(conn, person_id: int, ahora: datetime | None = None) -> dict | None:
    """BODY-APERTURA-AUTOMATICA-1 ruling 1: Hoy's own 24 h aviso line after Hogar applied a change
    on its own (`aplicada_via = 'hogar'`, BODY-HOGAR-ESCRIBE-1/2) -- its OWN line, directly after
    _mcp_aplicado_ctx's, never merged into one "your assistant" sentence: Hogar is a different
    outside app, not the person's personal assistant, so the hub's own «one line per outside
    origin» ruling applies. Same "no line, no empty count" shape, the same 24h window and the same
    injectable clock as _mcp_aplicado_ctx above; the person's own tap on a Hogar-origin proposal
    (`aplicada_via` left NULL by Body's own /asistente/propuestas/{id}/aplicar route) never raises
    this line either, for the same reason it never raises the mcp one."""
    ahora = ahora or datetime.now(timezone.utc)
    n, conversacion_id = agente_conversacion.aplicados_mcp_recientes(conn, person_id, ahora, via="hogar")
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


# BODY-PLAN-FASES-5 ruling 4: the 0-10 scale's own options, read once off db.py's own named
# constant -- never a second hand-typed "10" in this module.
_ESCALA_DOLOR_MANANA = list(range(db.DOLOR_ESCALA_MIN, db.DOLOR_ESCALA_MAX + 1))


def _dolor_manana_hoy_ctx(t: T, conn, person_id: int, today_local: date) -> list[dict]:
    """Ruling 4: Hoy's own due 'morning after' cards -- db.pendientes_dolor_manana's own reusable
    read, one card per (día entrenado, zona) still unanswered, each titled with that zona's own
    label and the natural list of exercises that loaded it. [] when nothing is due."""
    out = []
    for p in db.pendientes_dolor_manana(conn, person_id, today_local.isoformat()):
        titulo = t("hoy.dolor_manana_titulo", zona=t("entreno.categoria." + p["zona"]),
                   ejercicios=_lista_natural(t, p["ejercicios"]))
        out.append({"day": p["day"], "zona": p["zona"], "titulo": titulo})
    return out


def _plan_categorias_hoy(conn, person_id: int, today_local: date) -> list | None:
    """BODY-CUERPO-VISTA-1: today's own training plan, if any -- `plan.ejercicios_json` is "shape
    owned by the tool that writes it" (body/db.py), so an exercise's own `categoria` key is read
    only if present, never guessed from its name. `planear_entreno` (BODY-CUERPO-VISTA-1-M ruling 2)
    validates that key against `anatomia.CATEGORIA_ENUM` before it is ever stored; a row stored
    before that validation existed, under the old free-text vocabulary, just reads as unrecognised
    (body/anatomia.py's own CATEGORIA_GRUPOS lights nothing for it). None when no plan with
    exercises covers today at all, so the caption can tell "no session" from "session, no
    recognised category" (both read as "no session" today, but only the first one really is).
    BODY-HOY-ESCRITORIO-1 ruling 3: a plan already marked 'done' reads the same as None here too --
    its own categories are never "still to come" once the person marked the session finished (the
    WHERE clause below already drops 'skipped' the same way)."""
    row = _plan_hoy_row(conn, person_id, today_local)
    if row is None or row["status"] == "done":
        return None
    return [e.get("categoria") for e in json.loads(row["ejercicios_json"])]


def _plan_hoy_row(conn, person_id: int, today_local: date):
    """The one `plan` row BODY-CUERPO-VISTA-1's body view and BODY-HOY-DIA-POR-DELANTE-1's "day
    ahead" session row both read -- one query, shared, never two (same row either way). `status`
    rides along for `_plan_categorias_hoy`'s own 'done' check above; `_plan_hoy_item` ignores it."""
    return conn.execute(
        "SELECT title, time, status, ejercicios_json FROM plan WHERE person_id = ? AND day = ? "
        "AND status != 'skipped' AND ejercicios_json IS NOT NULL ORDER BY id LIMIT 1",
        (person_id, today_local.isoformat())).fetchone()


def _grupo_cargado_txt(t: T, grupos: list) -> str:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6) ruling 2: `tras_carga`'s own fine-grained
    anatomy groups, said back as the plain categoria word(s) a person actually plans sessions in
    (anatomia.CATEGORIA_ENUM's own order) -- the group name from anatomia, never 'pierna'
    hardcoded, since a different loaded group names itself here instead."""
    grupos_set = frozenset(grupos)
    categorias = [c for c in anatomia.CATEGORIA_ENUM if anatomia.CATEGORIA_GRUPOS.get(c, frozenset()) & grupos_set]
    return _lista_natural(t, [t("entreno.categoria." + c) for c in categorias])


def _reentrada_o_tras_carga_txt(t: T, senales: dict) -> str | None:
    """Ruling 3: Hoy's own planned-session line gains the SAME one-line reason the proposal card
    (body/rutas/asistente.py's own `_reentrada_txt`/`_tras_carga_txt`) already words -- a role
    note, not a warning colour. Computed fresh from today's own signals (never stored on the plan
    row itself), so it stays true even for a plan the person accepted days ago. `reentrada` is
    checked first when both apply: it speaks to the whole session's own volume, `tras_carga` to
    one day's own exercises."""
    if "reentrada" in senales:
        r = senales["reentrada"]
        return t("today.dia.reentrada", dias=t.num(r["dias_sin_fuerza"]), n=t.num(r["sesion_n"]),
                 pct=t.num(round(r["factor"] * 100)))
    if "tras_carga" in senales:
        tc = senales["tras_carga"]
        return t("today.dia.tras_carga",
                 actividad=_activity_name(t, {"name": tc["actividad"]["nombre"], "type": tc["actividad"]["tipo"]}),
                 grupo=_grupo_cargado_txt(t, tc["grupos"]))
    return None


def _plan_hoy_item(t: T, conn, person_id: int, today_local: date) -> dict | None:
    """BODY-HOY-DIA-POR-DELANTE-1 (ruling 2): today's own planned session as one "day ahead" row --
    an honest exercise count, never an invented duration the plan row does not carry. None with no
    session planned for today."""
    row = _plan_hoy_row(conn, person_id, today_local)
    if row is None:
        return None
    ejercicios = json.loads(row["ejercicios_json"])
    senales = reentrada.senales(conn, person_id, today_local)
    return {"tipo": "sesion", "hora": row["time"], "titulo": row["title"],
            "linea": t("today.dia.sesion_n", n=len(ejercicios)),
            "nota": _reentrada_o_tras_carga_txt(t, senales)}


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


def _plan_semana_cuenta_txt(t: T, estado: str, n: int) -> str:
    """Ruling 2: one count segment ("2 cumplidas", "1 en curso"), singular via the repo's own
    `_one` key convention -- every key written as a literal t(...) call (not built from an
    f-string) so tests/test_i18n_paridad.py's own scan can see each one."""
    if estado == "cumple":
        return (t("today.plan_semana.cuenta.cumple_one") if n == 1
                else t("today.plan_semana.cuenta.cumple", n=t.num(n)))
    if estado == "en_curso":
        return (t("today.plan_semana.cuenta.en_curso_one") if n == 1
                else t("today.plan_semana.cuenta.en_curso", n=t.num(n)))
    if estado == "no_cumple":
        return (t("today.plan_semana.cuenta.no_cumple_one") if n == 1
                else t("today.plan_semana.cuenta.no_cumple", n=t.num(n)))
    return (t("today.plan_semana.cuenta.sin_dato_one") if n == 1
            else t("today.plan_semana.cuenta.sin_dato", n=t.num(n)))


def _plan_semana_cuentas(t: T, lineas: list[dict]) -> list[dict]:
    """Ruling 2: this week's weekly lines as counts by estado, non-zero estados only, in the
    design's own fixed order (goals.plan.estado's own chip colours, never a new one)."""
    conteo: dict[str, int] = {}
    for linea in lineas:
        conteo[linea["estado"]] = conteo.get(linea["estado"], 0) + 1
    out = []
    for estado in ("cumple", "en_curso", "no_cumple", "sin_dato"):
        n = conteo.get(estado, 0)
        if n == 0:
            continue
        out.append({"cls": _PLAN_ESTADO_CLS[estado], "texto": _plan_semana_cuenta_txt(t, estado, n)})
    return out


def _plan_semana_bloque(t: T, resumen: dict, today_local: date) -> dict:
    """Ruling 2: one block of Today's "Tu plan esta semana" card, built ONLY from
    `horizonte.resumen` -- never a second horizonte_* read. The week position ("Semana N de M"),
    the days-left line, the current week's line counts by estado, the next checkpoint when it
    falls within 7 days, and the honest gap when this week carries no weekly lines at all."""
    start_monday = fechas.lunes_de(date.fromisoformat(resumen["start_day"]))
    end_monday = fechas.lunes_de(date.fromisoformat(resumen["deadline"]))
    semanas = max(1, (end_monday - start_monday).days // 7 + 1)
    esta_monday = date.fromisoformat(resumen["semana_actual"]["week_start"])
    semana_num = max(1, min((esta_monday - start_monday).days // 7 + 1, semanas))

    dias = resumen["dias_a_deadline"]
    if dias == 0:
        faltan = t("today.plan_semana.termina_hoy")
    elif dias == 1:
        faltan = t("today.plan_semana.faltan_one")
    else:
        faltan = t("today.plan_semana.faltan", n=t.num(dias))

    checkpoint = None
    siguiente = resumen["siguiente_checkpoint"]
    if siguiente is not None:
        dia_cp = date.fromisoformat(siguiente["day"])
        if (dia_cp - today_local).days <= 7:
            checkpoint = t("today.plan_semana.checkpoint", fecha=_dia_mes(t, dia_cp), titulo=siguiente["titulo"])

    # BODY-PLAN-FASES-2 ruling 4: the current phase's own line, from horizonte.resumen's own
    # "fase_actual" (no second horizonte_* read) -- None, so no line at all, the instant no phase
    # covers today.
    fase_actual = resumen.get("fase_actual")
    fase = None
    if fase_actual is not None:
        fase = t("today.plan_semana.fase", titulo=fase_actual["titulo"], n=t.num(fase_actual["semana"]),
                 m=t.num(fase_actual["semanas_total"]))

    return {
        "goal_id": resumen["goal_id"], "titulo": resumen["titulo"],
        "semana": t("today.plan_semana.semana", n=t.num(semana_num), m=t.num(semanas)), "faltan": faltan,
        "cuentas": _plan_semana_cuentas(t, resumen["semana_actual"]["lineas"]), "checkpoint": checkpoint,
        "fase": fase,
    }


# BODY-HOY-PENDIENTES-REGISTRO-1 (ORCH-CARLOS-DELEGATES-TIER-1-1): the lower-case, mid-sentence
# stream words for the one-line note below -- distinct from metas.py's own capitalised
# `_REGISTRO_LABEL_KEY` (that one opens a "Falta: ..." line; this one sits inside a sentence).
# `sueno` carries no key here: ruling 3 excludes it before any label is ever looked up.
_PENDIENTES_STREAM_KEY = {
    "comidas": "today.plan_semana.pendientes_stream.comidas",
    "proteina": "today.plan_semana.pendientes_stream.proteina",
    "peso": "today.plan_semana.pendientes_stream.peso",
    "cintura": "today.plan_semana.pendientes_stream.cintura",
    "entreno": "today.plan_semana.pendientes_stream.entreno",
}


def _plan_semana_pendientes_ctx(t: T, conn, person_id: int, today_local: date) -> dict | None:
    """Ruling 1-3: one muted line inside "Tu plan esta semana", after its blocks -- what
    `registro_cobertura.pendientes` says is still missing today and this ISO week, in the
    person's own register. ONE call to `pendientes` (the caller already knows at least one plan is
    active); the same {faltan, sin_uso} is repeated under every plan's own goal_id since these
    streams are the person's, not any one plan's, so only the first value is ever read. `sueno` is
    dropped before anything else (ruling 3: a missing night is the watch's own sync problem, already
    surfaced by Hoy's sync card) -- None the instant nothing besides it is missing. `sin_uso`
    streams are never named (never a nag)."""
    pendientes = registro_cobertura.pendientes(conn, person_id, today_local)
    datos = next(iter(pendientes.values()), None)
    if not datos:
        return None
    faltan = [s for s in datos["faltan"] if s != "sueno"]
    if not faltan:
        return None
    cadencia = registro_cobertura.MODELO["cadencia"]
    hoy_streams = [s for s in faltan if cadencia[s] == "daily"]
    semana_streams = [s for s in faltan if cadencia[s] == "weekly"]
    partes = []
    if hoy_streams:
        partes.append(t("today.plan_semana.pendientes_hoy",
                         items=_lista_natural(t, [t(_PENDIENTES_STREAM_KEY[s]) for s in hoy_streams])))
    if semana_streams:
        partes.append(t("today.plan_semana.pendientes_semana",
                         items=_lista_natural(t, [t(_PENDIENTES_STREAM_KEY[s]) for s in semana_streams])))
    return {"texto": " ".join(partes), "enlace": t("today.plan_semana.pendientes_link")}


def _plan_semana_ctx(t: T, conn, person_id: int, today_local: date) -> dict | None:
    """Ruling 2: Today's "Tu plan esta semana" card -- shown only when the person has at least one
    active plan (`horizonte.horizontes(..., solo_activos=True)`, ruling 1's own definition), right
    after the day-ahead card. At most PLANES_HOY_MAX blocks, each from one `horizonte.resumen`
    call (ruling 3: bounded regardless of that plan's own weekly lines, rules, weeks or
    checkpoints); the rest collapse into one "y N planes más" line to /goals. BODY-HOY-PENDIENTES-
    REGISTRO-1: one extra `registro_cobertura.pendientes` call, only reached here (never for a
    person with no active plan), feeds the card's own trailing note."""
    ids = horizonte.horizontes(conn, person_id, today_local, solo_activos=True)
    if not ids:
        return None
    bloques = [_plan_semana_bloque(t, horizonte.resumen(conn, gid, person_id, hoy=today_local), today_local)
               for gid in ids[:PLANES_HOY_MAX]]
    extra = len(ids) - PLANES_HOY_MAX
    if extra == 1:
        mas = t("today.plan_semana.mas_one")
    elif extra > 0:
        mas = t("today.plan_semana.mas", n=t.num(extra))
    else:
        mas = None
    return {"bloques": bloques, "mas": mas, "pendientes": _plan_semana_pendientes_ctx(t, conn, person_id, today_local)}


def _entrenado_hoy_categorias(conn, person_id: int, today_local: date) -> list:
    """BODY-HOY-ESCRITORIO-1 ruling 3: every active, not-excluded set's own categoria across ALL of
    today's logged training -- folded the same way `_actividades_hoy` already folds a linked pair
    (`db.merge_linked_training`'s own `sets_source`/`sets_source_id`, the manual row's sets win).
    ONE query over every one of today's activities' own workout_set rows, never one per activity
    (db.exercise_sets' own set_type/corrected_by_person/excluded_by_person selection, reproduced
    here rather than looped once per activity, since body/db.py is outside this slice's touches)."""
    rows = db.merge_linked_training(conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day = ? ORDER BY start_local",
        (person_id, today_local.isoformat())).fetchall())
    ids = [a["sets_source_id"] for a in rows]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    sets = conn.execute(
        f"SELECT set_type, corrected_by_person, excluded_by_person, garmin_category, person_category "
        f"FROM workout_set WHERE person_id = ? AND activity_source_id IN ({placeholders})",
        (person_id, *ids)).fetchall()
    out = []
    for s in sets:
        corrected = bool(s["corrected_by_person"])
        if (("active" if corrected else s["set_type"]) != "active") or bool(s["excluded_by_person"]):
            continue
        out.append(s["person_category"] if corrected else s["garmin_category"])
    return out


def _cuerpo_hoy_ctx(t: T, conn, person_id: int, today_local: date) -> dict:
    """Hoy's own body view (BODY-CUERPO-VISTA-1, ruling 4; rebuilt by BODY-HOY-ESCRITORIO-1 ruling
    3): Músculos/Espalda/Corazón y vasos, now TWO glow layers instead of one -- solid for every
    group today's LOGGED training actually worked, dashed for whatever today's plan still owes
    (minus whatever is already trained). Four captions: trained only, trained plus still-to-come,
    planned only (today's existing wording, unchanged -- nothing trained yet, the whole plan is
    still to come), and nothing at all (the existing empty caption, unchanged).
    BODY-LESION-ZONA-1 ruling 4: a third, quiet layer for any zona an open lesion period names for
    today -- drawn as a hatch above both, never the training glow, with its own caption below."""
    categorias_planeadas = _plan_categorias_hoy(conn, person_id, today_local)
    categorias_entrenadas = _entrenado_hoy_categorias(conn, person_id, today_local)
    grupos, pendientes = anatomia.grupos_entrenado_pendiente(categorias_entrenadas, categorias_planeadas)
    if grupos and pendientes:
        nombres = [t("cuerpo.grupo." + g) for g in anatomia.grupos_ordenados(grupos)]
        nombres_pend = [t("cuerpo.grupo." + g) for g in anatomia.grupos_ordenados(pendientes)]
        caption = t("cuerpo.caption.hoy_entrenado_y_pendiente", grupos=_lista_natural(t, nombres),
                    pendientes=_lista_natural(t, nombres_pend))
    elif grupos:
        nombres = [t("cuerpo.grupo." + g) for g in anatomia.grupos_ordenados(grupos)]
        caption = t("cuerpo.caption.hoy_entrenado", grupos=_lista_natural(t, nombres))
    elif pendientes:
        nombres = [t("cuerpo.grupo." + g) for g in anatomia.grupos_ordenados(pendientes)]
        caption = t("cuerpo.caption.hoy_con_sesion", grupos=_lista_natural(t, nombres))
    else:
        caption = t("cuerpo.caption.hoy_sin_sesion")
    zonas = db.zonas_en_recuperacion(conn, person_id, today_local.isoformat())
    grupos_limitados = anatomia.grupos_de_categorias(zonas)
    caption_limitada = None
    if grupos_limitados:
        nombres_zonas = [t("entreno.categoria." + z) for z in anatomia.CATEGORIA_ENUM if z in zonas]
        caption_limitada = t("cuerpo.caption.en_recuperacion", zonas=_lista_natural(t, nombres_zonas))
    return {"vistas": anatomia.contexto(t, "hoy", grupos, grupos_limitados, pendientes), "caption": caption,
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


def _comida_compartida_pendientes_ctx(t: T, conn, defs: dict, person_id: int) -> list[dict]:
    """Hoy's own card (BODY-COMIDA-COMPARTIDA-1 ruling 5): every shared-meal portion still
    waiting for THIS person's own tap, with the author's plain display_name, the words, the
    portion text if any, and the nutrients in the app's existing figure+unit formatting. Empty
    once nothing is pending -- no card at all, the same "None/empty means no line" rule every
    other Hoy card here follows."""
    out = []
    for p in comida_compartida.pendientes(conn, person_id):
        nutrientes_texto = " · ".join(
            f"{t('metric.' + k)}: {comun._fig_unit(t, k, v, defs)}" for k, v in p["nutrientes"].items()
        )
        out.append({
            "porcion_id": p["porcion_id"], "autor_nombre": p["autor_nombre"],
            "descripcion": p["descripcion"], "porcion_texto": p["porcion_texto"],
            "nutrientes_texto": nutrientes_texto,
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
def today(background_tasks: BackgroundTasks, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    # BODY-SYNC-VIVO-2 ruling 2: never blocking -- scheduled to run AFTER this response is sent,
    # on its own connection. Checked before the "no data yet" early return below too, so the very
    # first live pull can start from the empty state.
    if _debe_lanzar_pull_hoy(conn, person_id):
        background_tasks.add_task(pull.pull_hoy_ahora, person_id)
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
    hogar_aplicado = _hogar_aplicado_ctx(conn, person_id)
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
    correcciones_hoy = db.despertar_correcciones(conn, person_id, day)
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
        # BODY-DESPERTAR-REAL-6B ruling 4: a short mark, never a second line, on the sleep tile
        # alone -- the figure itself (`v`) already came back corrected (db.day_values reads
        # daily_metric_efectiva); a 'levantado'-only correction leaves this False (changes no
        # figure, ruling 3).
        corregido = k == "sleep_minutes" and correcciones_hoy.get("sueno") is not None
        cards.append({"key": k, "label": label, "value": comun._value(t, k, v),
                      "unit": comun._unit(t, defs[k]) if v is not None else "", "delta": delta, "cls": cls,
                      "banda": banda, "caption": caption, "corregido": corregido})
    return comun._render("today.html", t=t, tab="today", day=day, date_long=t.long_date(date.fromisoformat(day)),
                   synced=synced, stale=_stale_ctx(t, day, today_local),
                   garmin_sync=garmin_rutas._garmin_sync_ctx(t, conn, person_id, day, today_local),
                   reloj_vivo=garmin_rutas._reloj_vivo_ctx(t, conn, person_id, person, today_local), cuerpo=cuerpo_ctx,
                   r=r, reading=_reading(t, r), ready_avg=ready_avg,
                   ready_delta=ready_delta, ready_cls=ready_cls, cards=cards,
                   missing_txt=", ".join(t("part." + m) for m in r.missing), proposed_count=proposed_count,
                   turno=turno_ctx, turnos=turnos_on, mcp_pendientes=mcp_pendientes, mcp_aplicado=mcp_aplicado,
                   hogar_aplicado=hogar_aplicado,
                   dia_por_delante=_dia_por_delante_ctx(t, conn, person_id, today_local, turno_de_hoy["hoy"]),
                   plan_semana=_plan_semana_ctx(t, conn, person_id, today_local),
                   semana_nocturna=_semana_nocturna_ctx(t, conn, person_id, today_local),
                   today_iso=today_local.isoformat(),
                   primeros_pasos=primeros_pasos,
                   hogar_compartido=_hogar_compartido_ctx(conn, t, person_id, defs, today_local),
                   ventana=_ventana_ctx(t, conn, person, person_id, today_local),
                   periodo=_periodo_hoy_ctx(t, conn, person_id, today_local),
                   noche_inusual=_noche_inusual_hoy_ctx(t, conn, person_id, day, vals),
                   despertar=_despertar_hoy_ctx(t, conn, person_id, day, today_local, correcciones_hoy),
                   objetivos_nutriente=_objetivos_nutriente_ctx(t, conn, defs, person_id, day, es_hoy),
                   comida_compartida_pendientes=_comida_compartida_pendientes_ctx(t, conn, defs, person_id),
                   dolor_manana=_dolor_manana_hoy_ctx(t, conn, person_id, today_local),
                   escala_dolor=_ESCALA_DOLOR_MANANA)


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


def _desplazamiento_dia_ctx(t: T, vals: dict) -> str | None:
    """BODY-DESPLAZAMIENTO-3 ruling 3: Día's own one-line provenance note, shown once regardless of
    how many of the four commute keys this day carries a value for -- never a zero past
    ultimo_dia (an absent daily_metric row already reads as "—" in the generic card grid; a gap
    stays a gap, no code needed for that half)."""
    keys = fuentes.FUENTES[_FUENTE_COMMUTE]["metric_keys"]
    if any(vals.get(k) is not None for k in keys):
        return t("day.desplazamiento.fuente")
    return None


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


def _objetivos_nutriente_ctx(t: T, conn, defs: dict, person_id: int, dia_iso: str, es_hoy: bool) -> list[dict]:
    """Today's and Día's own "Metas de nutrientes" lines (BODY-NUTRIENTE-OBJETIVO-1): one per
    intake key with a live target on this day, generic over all eight -- shared by `today()` and
    `day_view()` below, since `nutriente_objetivo.estado_del_dia` is already honest about history
    (ruling 1: the target in force on THIS day, never today's). `falta_peso` (ruling 2) shows its
    own line, no number guessed. Never a progress bar in markup colour -- `pct` is a plain number,
    body/static/body.css picks the fixed class. BODY-NUTRIENTE-OBJETIVO-1-M fix 6: `es_hoy` picks
    the "reached" wording -- Día showing a PAST day must never say "today"."""
    estado = nutriente_objetivo.estado_del_dia(conn, person_id, dia_iso)
    out = []
    for k in db.INGESTA_METRIC_KEYS:
        e = estado.get(k)
        if e is None:
            continue
        obj = e["objetivo"]
        if obj["falta_peso"]:
            out.append({"key": k, "label": t("metric." + k), "texto": t("nutriente.hoy.falta_peso"), "pct": None})
            continue
        unidad = comun._unit(t, defs[k])
        total_txt = comun._value(t, k, e["total"])
        objetivo_txt = comun._value(t, k, obj["objetivo"])
        pct = round(min(e["total"] / obj["objetivo"], 1.0) * 100) if obj["objetivo"] else 0
        if obj["sentido"] == "al_menos":
            if e["restante"] == 0.0:
                texto = t("nutriente.hoy.cumplida") if es_hoy else t("nutriente.hoy.cumplida_dia")
            else:
                clave = "nutriente.hoy.progreso" if e["n"] == 1 else "nutriente.hoy.progreso_plural"
                texto = t(clave, total=total_txt, objetivo=objetivo_txt, unidad=unidad, n=e["n"],
                          restante=comun._value(t, k, e["restante"]))
        elif e["restante"] >= 0:
            texto = t("nutriente.hoy.limite_quedan", restante=comun._value(t, k, e["restante"]), unidad=unidad)
        else:
            texto = t("nutriente.hoy.limite_excedido", exceso=comun._value(t, k, -e["restante"]), unidad=unidad)
        out.append({"key": k, "label": t("metric." + k), "texto": texto, "pct": pct})
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


def _despertar_propuesta_pendiente(conn, person_id: int, day: str, tipo: str) -> dict | None:
    """BODY-DESPERTAR-REAL-7 ruling 1, widened by BODY-DESPERTAR-REAL-6A-R2 ruling 4: the most
    recent still-PENDING `corregir_despertar` propuesta for this day AND this tipo, if any -- its
    own card then shows inline with Aplicar/Descartar. An already APPLIED one is never read here:
    its own correction row (db.despertar_correcciones) is what ruling 2's state reads instead,
    which also covers a correction with no propuesta behind it at all (body/demo.py's own
    `_seed_despertar_correccion`, written directly, never through this flow)."""
    filas = conn.execute(
        "SELECT id, despues_json FROM propuesta_agente WHERE person_id = ? AND accion = 'corregir_despertar' "
        "AND estado = 'propuesta' ORDER BY creada_en DESC",
        (person_id,),
    ).fetchall()
    for f in filas:
        despues = json.loads(f["despues_json"])
        if despues.get("fecha") == day and despues.get("tipo") == tipo:
            return {"id": f["id"], "despues": despues}
    return None


def _despertar_propuesta_txt(t: T, despues: dict) -> str:
    """Duplicated from body/rutas/asistente.py's own `_tarjeta_corregir_despertar` (same reasoning
    `_entreno_vinculo_txt` above gives: neither module may import the other for one card helper) --
    same i18n keys, so Día's own card reads exactly like the chat's."""
    hora = despues["hora_local"]
    fin = despues["sueno_fin"][11:16]
    clave = ("asistente.propuesta.despertar_sueno_resumen" if despues["tipo"] == "sueno"
             else "asistente.propuesta.despertar_levantado_resumen")
    return t(clave, hora=hora, fin=fin)


def _despertar_curva_puntos(conn, person_id: int, zona: str, desde: datetime, hasta: datetime
                             ) -> list[tuple[datetime, float]]:
    """Ruling 3: the raw heart-rate points behind the curve, `db.intradia`'s own 'intradia_fc'
    series over at most the two calendar days the window can touch -- one query per day, never per
    sample (tests/test_rendimiento.py)."""
    dias = sorted({desde.date().isoformat(), hasta.date().isoformat()})
    crudo: list[tuple[datetime, float]] = []
    for dia in dias:
        for ts, v in db.intradia(conn, person_id, dia, "intradia_fc", zona):
            dt = datetime.fromisoformat(ts)
            if desde <= dt <= hasta:
                crudo.append((dt, v))
    crudo.sort()
    return crudo


def _despertar_curva_svg(t: T, puntos: list[tuple[datetime, float]], desde: datetime, hasta: datetime,
                          garmin_dt: datetime, marcas: list[datetime], width: int = 320, height: int = 100
                          ) -> str | None:
    """Ruling 3, widened by BODY-DESPERTAR-REAL-6A-R2 ruling 4 ("the curve shows Garmin's end plus
    up to two marks"): the same inline-SVG, server-built shape body/rutas/entreno_sesion.py's own
    `_curva_svg` and body/profile.py's own `window_svg` already use -- one polyline per unbroken
    run, a lone point as a dot, a gap wider than the night's own observed spacing (the same
    `factor_hueco_espaciado`/`espaciado_asumido_min` convention despertar_real.MODELO already
    defines) left undrawn rather than bridged, never a line across it. Garmin's own recorded end
    gets a dashed vertical mark; `marcas` (up to one per tipo -- the proposed/corrected time for
    sueno, for levantado, or both) each get their own, every mark a plain HH:MM label -- `.tick`,
    no other axis numbers (DESIGN rule 13 BODY-ESTILOS-1: every colour a fixed class). None when
    there is no heart-rate point in the window at all -- the caller shows nothing, never an empty
    chart inviting a tap over nothing (DESIGN rule 6)."""
    if not puntos:
        return None
    lo = min(v for _, v in puntos)
    hi = max(v for _, v in puntos)
    pad = (hi - lo) * 0.15 or 1
    lo, hi = lo - pad, hi + pad
    top, h = 6, height - 20
    span_s = (hasta - desde).total_seconds() or 1

    def x(ts: datetime) -> float:
        return max(0.0, min(1.0, (ts - desde).total_seconds() / span_s)) * width

    def y(v: float) -> float:
        return top + h - (v - lo) / (hi - lo) * h

    espaciado = despertar_real.MODELO["espaciado_asumido_min"]
    tiempos = [dt for dt, _ in puntos]
    if len(tiempos) >= 2:
        diffs = [(b - a).total_seconds() / 60 for a, b in zip(tiempos, tiempos[1:]) if b > a]
        if diffs:
            espaciado = statistics.median(diffs)
    hueco_min = despertar_real.MODELO["factor_hueco_espaciado"] * espaciado

    runs: list[list[str]] = []
    cur: list[str] = []
    prev_dt = None
    for dt, v in puntos:
        if prev_dt is not None and (dt - prev_dt).total_seconds() / 60 > hueco_min:
            if cur:
                runs.append(cur)
            cur = []
        cur.append(f"{x(dt):.1f},{y(v):.1f}")
        prev_dt = dt
    if cur:
        runs.append(cur)

    shapes = []
    for r in runs:
        if len(r) == 1:
            px, py = r[0].split(",")
            shapes.append(f'<circle cx="{px}" cy="{py}" r="2.5" class="c-body"/>')
        else:
            shapes.append(f'<polyline fill="none" class="stroke-body" stroke-width="2" '
                          f'vector-effect="non-scaling-stroke" stroke-linejoin="round" points="{" ".join(r)}"/>')

    def marca(dt: datetime, cls: str) -> str:
        xx = x(dt)
        return (f'<line x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{top + h}" class="{cls}" '
               f'stroke-width="1" stroke-dasharray="2 3"/>'
               f'<text x="{xx:.1f}" y="{top + h + 12:.1f}" text-anchor="middle" class="tick">'
               f'{html.escape(dt.strftime("%H:%M"))}</text>')

    shapes.append(marca(garmin_dt, "stroke-muted"))
    for marca_dt in marcas:
        shapes.append(marca(marca_dt, "stroke-body"))

    aria = html.escape(t("day.despertar.curva_aria", desde=desde.strftime("%H:%M"), hasta=hasta.strftime("%H:%M")))
    return (f'<svg viewBox="0 0 {width} {top + h + 20}" width="100%" height="{height}" role="img" '
            f'aria-label="{aria}" preserveAspectRatio="none" class="chart-svg">{"".join(shapes)}</svg>')


def _despertar_correccion_linea(t: T, correccion: dict) -> str:
    """Ruling 2: the person's own correction, labelled by kind -- Garmin's own `sleep_session` row
    never touched either way."""
    clave = ("day.despertar.correccion_sueno" if correccion["tipo"] == "sueno"
             else "day.despertar.correccion_levantado")
    return t(clave, hora=correccion["hora_local"])


def _despertar_dia_ctx(t: T, conn, person_id: int, zona: str, day: str, sleep_end_local: str | None,
                        correcciones: dict | None = None) -> dict | None:
    """BODY-DESPERTAR-REAL-7 rulings 1-3, widened by BODY-DESPERTAR-REAL-6A-R2 ruling 4: Día's own
    inline card beside `sueno_fc_linea` -- EACH kind (sueno first, then levantado) gets its own
    line/button, or its own correction line and Deshacer, mutually exclusive per kind the same way
    ruling 1-2 already were per morning. ONE shared "Garmin marcó..." line when at least one kind
    is already corrected, and ONE curve for the whole morning -- Garmin's own end plus up to one
    mark per kind present (ruling 4's own "Garmin's end plus up to two marks"). `despertar_real.
    estimar` runs at most once per view, and only when at least one kind still has nothing applied
    (tests/test_rendimiento.py). `correcciones`, when given (BODY-DESPERTAR-REAL-6B: `day_view`
    already reads this day's own corrections for the sleep card's provenance line), is used as is
    instead of a second query for the same row -- None (the default) keeps the original query, for
    a caller with no corrections of its own to reuse (tests/test_rendimiento.py's own isolated
    call)."""
    if sleep_end_local is None:
        return None
    sleep_end_dt = datetime.fromisoformat(sleep_end_local)
    horizonte_fin_dt = sleep_end_dt + timedelta(hours=despertar_real.MODELO["horizonte_horas"])
    correcciones = correcciones if correcciones is not None else db.despertar_correcciones(conn, person_id, day)
    resultado = despertar_real.estimar(conn, person_id, day) if len(correcciones) < 2 else None
    propuestas_por_tipo = {p["tipo"]: p for p in (resultado["propuestas"] if resultado else [])}
    confianza = resultado["confianza"] if resultado else None

    tarjetas: list[dict] = []
    marcas: list[datetime] = []
    hay_correccion = False
    for tipo in ("sueno", "levantado"):
        correccion = correcciones.get(tipo)
        if correccion is not None:
            hay_correccion = True
            marcas.append(datetime.combine(date.fromisoformat(day),
                                            datetime.strptime(correccion["hora_local"], "%H:%M").time()))
            tarjetas.append({"tipo": tipo, "correccion_linea": _despertar_correccion_linea(t, correccion),
                              "linea": None, "propuesta_texto": None, "propuesta_id": None,
                              "boton": False, "deshacer": True})
            continue
        propuesta = propuestas_por_tipo.get(tipo)
        if propuesta is None or confianza == "baja":
            continue
        hora_txt = propuesta["hora"][11:16]
        clave_linea = "day.despertar.sueno_linea" if tipo == "sueno" else "day.despertar.levantado_linea"
        linea = t(clave_linea, hora=hora_txt, fin=sleep_end_dt.strftime("%H:%M"))
        marcas.append(datetime.fromisoformat(propuesta["hora"]))
        pendiente = _despertar_propuesta_pendiente(conn, person_id, day, tipo)
        if pendiente is not None:
            tarjetas.append({"tipo": tipo, "correccion_linea": None, "linea": linea,
                              "propuesta_texto": _despertar_propuesta_txt(t, pendiente["despues"]),
                              "propuesta_id": pendiente["id"], "boton": False, "deshacer": False})
        else:
            tarjetas.append({"tipo": tipo, "correccion_linea": None, "linea": linea, "propuesta_texto": None,
                              "propuesta_id": None, "boton": True, "deshacer": False})

    if not tarjetas:
        return None

    ventana_fin = min(horizonte_fin_dt, (max(marcas) if marcas else sleep_end_dt) + timedelta(minutes=30))
    puntos = _despertar_curva_puntos(conn, person_id, zona, sleep_end_dt - timedelta(minutes=30), ventana_fin)
    curva = _despertar_curva_svg(t, puntos, sleep_end_dt - timedelta(minutes=30), ventana_fin, sleep_end_dt, marcas)
    garmin_linea = t("day.despertar.garmin_linea", hora=sleep_end_dt.strftime("%H:%M")) if hay_correccion else None
    return {"garmin_linea": garmin_linea, "tarjetas": tarjetas, "curva": curva}


def _despertar_hoy_ctx(t: T, conn, person_id: int, day: str, today_local: date,
                        correcciones: dict | None = None) -> dict | None:
    """Ruling 4: the same explanatory line as Día's own ruling 1, today's sleep only -- no button,
    no curve, nothing once ANY correction exists (ruling 2 belongs to Día alone).
    BODY-DESPERTAR-REAL-6A-R2 ruling 4: "Hoy keeps one line, the sueno one when present, else the
    levantado one" -- `propuestas` is already ordered that way. `correcciones`, when given
    (BODY-DESPERTAR-REAL-6B: `today()` already read this day's own corrections for the sleep
    tile's own mark), is used as is instead of a second query -- None (the default) keeps the
    original query, for tests/test_rendimiento.py's own isolated call."""
    if day != today_local.isoformat():
        return None
    correcciones = correcciones if correcciones is not None else db.despertar_correcciones(conn, person_id, day)
    if correcciones:
        return None
    resultado = despertar_real.estimar(conn, person_id, day)
    if not resultado["propuestas"] or resultado["confianza"] == "baja":
        return None
    propuesta = resultado["propuestas"][0]
    hora_txt = propuesta["hora"][11:16]
    fin_txt = datetime.fromisoformat(resultado["sueno_fin"]).strftime("%H:%M")
    clave = "day.despertar.sueno_linea" if propuesta["tipo"] == "sueno" else "day.despertar.levantado_linea"
    return {"texto": t(clave, hora=hora_txt, fin=fin_txt), "day": day}


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
    # BODY-HISTORIA-VIDA-LUGARES-2-R ruling 3: this one day against the person's own cached trips
    # -- one statement, through the same helper ruling 0 names for every trip-aware screen.
    de_viaje = key in viajes.dias_de_viaje(conn, person_id, key, key)
    data = db.load_day(conn, key, person_id)
    vals = {m["key"]: m["value"] for m in data["metrics"]}
    defs = comun._defs(conn)
    # BODY-DESPLAZAMIENTO-3-M fix 1 (ruling 3): a registered source with no value at all for THIS
    # day shows none of its rows here -- never a row of bare "—"s. A source with at least one value
    # keeps showing all of its keys, same as today (a missing one of them may still read "—").
    fuentes_sin_datos_hoy = {f for f, datos in fuentes.FUENTES.items()
                             if not any(vals.get(k) is not None for k in datos["metric_keys"])}
    cards = []
    for k, df in defs.items():
        if k in SUENO_RESP_KEYS or k in SUENO_FC_KEYS:
            continue
        if fuentes.fuente_de_metrica(k) in fuentes_sin_datos_hoy:
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
    correcciones = db.despertar_correcciones(conn, person_id, key)
    sleep = None
    if vals.get("sleep_minutes") is not None:
        total_stage = sum(vals.get(s[0]) or 0 for s in STAGES)
        stages = [{"label": t(lbl), "value": t.minutes(vals[k]), "cls": cls,
                   "pct": round(100 * vals[k] / total_stage, 1)}
                  for k, lbl, cls in STAGES if vals.get(k)] if total_stage else []
        win = data["sleep"] or {}
        # BODY-DESPERTAR-REAL-6B ruling 4: vals["sleep_minutes"] already came back corrected
        # (db.load_day reads daily_metric_efectiva); the window's own END shown here becomes the
        # 'sueno' correction's own hora_local too (the exact wall-clock time the view's figure is
        # built from), and Garmin's own total/end -- never edited, both always visible -- go in
        # one plain line under the card. A 'levantado'-only correction changes neither (ruling 3).
        sueno_corr = correcciones.get("sueno")
        incluye_correccion = None
        end_local = win.get("end_local")
        end_display = end_local
        if sueno_corr is not None and end_local is not None:
            fin_garmin_dt = datetime.fromisoformat(end_local)
            corr_dt = datetime.combine(fin_garmin_dt.date(),
                                        datetime.strptime(sueno_corr["hora_local"], "%H:%M").time())
            delta_min = max(0, round((corr_dt - fin_garmin_dt).total_seconds() / 60))
            end_display = corr_dt.isoformat()
            garmin_total = vals["sleep_minutes"] - delta_min
            incluye_correccion = t("day.sueno.incluye_correccion", total=t.minutes(garmin_total),
                                    fin=fin_garmin_dt.strftime("%H:%M"))
        window = " → ".join(x[11:16] for x in (win.get("start_local"), end_display) if x)
        # BODY-NOCHE-SIN-REM-1: a capture gap, not a reading -- the bar itself already drops a
        # zero-minute stage from `stages` above (never a zero-width real segment); this only adds
        # the one muted line saying so, never touching what the bar itself draws.
        # BODY-NOCHE-SIN-REM-3: fed db.valores_etapas_garmin (the RAW stage totals), not `vals` --
        # `vals` comes from db.load_day, through the view that now OMITS a marked night's own
        # sleep_rem_min row, so evaluar(vals) would see REM as missing (never flagged) rather than
        # 0 (flagged) on exactly the nights this line exists to name.
        captura = captura_sueno.evaluar(db.valores_etapas_garmin(conn, key, person_id))
        rem_no_capturado_linea = (t("day.sueno.rem_no_capturado")
                                   if captura and captura.get("rem_no_capturado") else None)
        sleep = {"total": t.minutes(vals["sleep_minutes"]), "window": window, "stages": stages,
                 "incluye_correccion": incluye_correccion,
                 "rem_no_capturado_linea": rem_no_capturado_linea}
    despertar_ctx = _despertar_dia_ctx(t, conn, person_id, person["timezone"], d.isoformat(),
                                        win.get("end_local") if sleep is not None else None, correcciones)
    # BODY-DESPERTAR-REAL-6B polish (ruling 6, ORCH-CARLOS-DELEGATES-TIER-1-1): with the sleep
    # card's own provenance line showing, Garmin's end is already said once there -- the older
    # shared line here would repeat it. No 'sueno' correction (incluye_correccion is None) leaves
    # despertar_ctx exactly as `_despertar_dia_ctx` built it.
    if despertar_ctx is not None and sleep is not None and sleep["incluye_correccion"] is not None:
        despertar_ctx = {**despertar_ctx, "garmin_linea": None}
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
                     # BODY-ENTRENO-VISTA-SESION-1 ruling 1: the row's own per-session page --
                     # `source_id` is always the merged (watch) identity for a linked pair, the
                     # same one body/entreno_sesion.py's own `actividad()` resolves by.
                     "href": f"/day/{d.isoformat()}/entreno/{a['source_id']}",
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
                   despertar=despertar_ctx, noche_inusual=noche_inusual_ctx,
                   activities=acts, shifts=shifts, has_data=has_data, block_ctx=block_ctx,
                   sync_text=None if has_data else comun._sync_status_text(conn, person, t),
                   prev=(d - timedelta(1)).isoformat(), next=(d + timedelta(1)).isoformat(),
                   turnos=turnos_on, ventana=_ventana_dia_ctx(t, conn, person_id, d), periodo=periodo_dia,
                   de_viaje=de_viaje,
                   comidas=_comidas_dia_ctx(t, conn, person_id, defs, d.isoformat()),
                   checkin=_checkins_dia_ctx(t, conn, person_id, defs, vals, d.isoformat()),
                   objetivos_nutriente=_objetivos_nutriente_ctx(t, conn, defs, person_id, d.isoformat(),
                                                                 d.isoformat() == today_local.isoformat()),
                   union_oferta=union_oferta, vinculo_tarjetas=vinculo_tarjetas,
                   desplazamiento_fuente=_desplazamiento_dia_ctx(t, vals))


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


@router.post("/day/{day}/dolor-manana")
@comun._cierra_conexion_async
async def dolor_manana_registrar(day: str, request: Request, person_id: int = Depends(identity.current_person_id)):
    """BODY-PLAN-FASES-5 ruling 4: Hoy's own due 'morning after' card -- `day` is the TRAINED day
    the card is about (db.pendientes_dolor_manana's own "day" field, like noche_inusual.day above),
    never today's own date; `zona` is a hidden field. Fills dolor_manana + manana_registrado_en on
    every still-open row of that zona on that day, through db.registrar_dolor_manana -- the same
    writer a later follow-up can share (ruling 4's own "not a one-off branch")."""
    d = _parse_day(day)
    conn, _, _ = comun._ctx(person_id)
    f = await comun._form(request)
    zona = f.get("zona")
    valor = f.get("valor")
    if zona and valor is not None and valor != "":
        try:
            v = int(valor)
        except ValueError:
            v = None
        if v is not None and db.DOLOR_ESCALA_MIN <= v <= db.DOLOR_ESCALA_MAX:
            db.registrar_dolor_manana(conn, person_id, d.isoformat(), zona, v)
    back = cuenta.safe_return_to(f.get("back") or "/")
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


@router.post("/day/{day}/despertar/proponer")
@comun._cierra_conexion_async
async def despertar_proponer(day: str, request: Request, person_id: int = Depends(identity.current_person_id)):
    """BODY-DESPERTAR-REAL-7 ruling 1, widened by BODY-DESPERTAR-REAL-6A-R2 ruling 4: Día's own
    "¿Corregir?" button, now one per kind -- `tipo` (a hidden form field, which kind's own card the
    person tapped) raises the SAME `corregir_despertar` propuesta a chat/MCP call would, through
    `herramientas._corregir_despertar` and `agente_propuestas.crear`, in a fresh conversation this
    one control owns, the exact precedent `/settings/nutriente/proponer` (body/rutas/ajustes.py)
    already sets. The hora comes from `despertar_real.estimar`'s own CURRENT propuesta for that
    tipo, never typed by the person. A day/tipo that no longer offers one (already corrected
    meanwhile, or the model's own read of it changed) is a quiet no-op redirect, never a 500 --
    the same defensive shape `entreno_separar`/`entreno_unir` above already use."""
    d = _parse_day(day)
    conn, _, _ = comun._ctx(person_id)
    f = await comun._form(request)
    tipo = f.get("tipo")
    resultado = despertar_real.estimar(conn, person_id, d.isoformat())
    propuesta = next((p for p in resultado["propuestas"] if p["tipo"] == tipo), None)
    if propuesta is not None:
        conversacion_id = agente_conversacion.crear(conn, person_id)
        try:
            agente_herramientas._corregir_despertar(conn, person_id, conversacion_id, d.isoformat(),
                                                      propuesta["tipo"], propuesta["hora"][11:16])
        except agente_herramientas.ToolError:
            pass
    back = cuenta.safe_return_to(f.get("back") or f"/day/{day}")
    return RedirectResponse(back, status_code=303)


@router.post("/day/{day}/despertar/deshacer")
@comun._cierra_conexion_async
async def despertar_deshacer(day: str, request: Request, person_id: int = Depends(identity.current_person_id)):
    """BODY-DESPERTAR-REAL-7 ruling 2, widened by BODY-DESPERTAR-REAL-6A-R2 ruling 4: "Deshacer"
    deletes THIS kind's own correction row directly (`tipo`, a hidden form field --
    `db.borrar_despertar_correccion` is already a quiet no-op on an absent row) -- never through
    the propuesta undo path, since a demo-seeded or chat-written correction may have no propuesta
    behind it at all. This button's only job is "the row is gone", never "restore whatever came
    before" (that is the propuesta `deshacer` action's own job, unaffected, still reachable from
    chat/MCP); the OTHER kind's own row, if any, is untouched."""
    d = _parse_day(day)
    conn, _, _ = comun._ctx(person_id)
    f = await comun._form(request)
    tipo = f.get("tipo")
    if tipo in ("sueno", "levantado"):
        db.borrar_despertar_correccion(conn, person_id, d.isoformat(), tipo)
        # BODY-DESPERTAR-REAL-6B ruling 3: this button deletes the row directly, outside the
        # propuesta apply/undo table -- the same re-store readiness needs either way, so a
        # removed 'sueno' correction never leaves a stale, too-high score behind.
        if tipo == "sueno":
            readiness.recompute_desde(conn, person_id, d.isoformat())
    back = cuenta.safe_return_to(f.get("back") or f"/day/{day}")
    return RedirectResponse(back, status_code=303)


@router.post("/day/{day}/despertar/propuestas/{propuesta_id}/aplicar")
async def despertar_propuesta_aplicar(day: str, propuesta_id: int, request: Request,
                                       person_id: int = Depends(identity.current_person_id)):
    """BODY-DESPERTAR-REAL-7 ruling 1: the SAME generic resolver `entreno_propuesta_aplicar` above
    calls -- `_entreno_propuesta_resolver` is generic over the proposal's own `accion`, so this is
    reuse, not a second apply path."""
    return await _entreno_propuesta_resolver(day, propuesta_id, request, person_id, agente_propuestas.aplicar)


@router.post("/day/{day}/despertar/propuestas/{propuesta_id}/descartar")
async def despertar_propuesta_descartar(day: str, propuesta_id: int, request: Request,
                                         person_id: int = Depends(identity.current_person_id)):
    return await _entreno_propuesta_resolver(day, propuesta_id, request, person_id, agente_propuestas.descartar)


@router.post("/comida-compartida/{porcion_id}/aplicar")
@comun._cierra_conexion_async
async def comida_compartida_aplicar(porcion_id: int, request: Request,
                                     person_id: int = Depends(identity.current_person_id)):
    """BODY-COMIDA-COMPARTIDA-1 ruling 5: Hoy's own "Aplicar" -- the owner only
    (comida_compartida.PorcionAjena, same guard shape as PropuestaAjena above); a stale or
    someone-else's id is a no-op redirect, never a 500."""
    conn, _, _ = comun._ctx(person_id)
    try:
        comida_compartida.aplicar_porcion(conn, person_id, porcion_id)
    except comida_compartida.PorcionAjena:
        pass
    f = await comun._form(request)
    back = cuenta.safe_return_to(f.get("back") or "/")
    return RedirectResponse(back, status_code=303)


@router.post("/comida-compartida/{porcion_id}/descartar")
@comun._cierra_conexion_async
async def comida_compartida_descartar(porcion_id: int, request: Request,
                                       person_id: int = Depends(identity.current_person_id)):
    """Hoy's own "Descartar" -- same owner-only guard as `comida_compartida_aplicar` above."""
    conn, _, _ = comun._ctx(person_id)
    try:
        comida_compartida.descartar_porcion(conn, person_id, porcion_id)
    except comida_compartida.PorcionAjena:
        pass
    f = await comun._form(request)
    back = cuenta.safe_return_to(f.get("back") or "/")
    return RedirectResponse(back, status_code=303)
