"""BODY-HOY-LO-QUE-VENCE-1 (HOGAR-HOY-LO-QUE-VENCE-1, ORCH-CARLOS-DELEGATES-TIER-1-1): the single
"coming due" strip Hoy shows below the readiness hero -- every dated fact this app knows about,
that Hoy does not already name somewhere else on the same screen, soonest first, overdue on top.

Pure functions over a connection (the same sense body/horizonte.py and body/registro_cobertura.py
already use the word in): `lineas()` reads, through a few small, bounded queries of its own plus
whatever the caller already computed, and returns plain dicts. It never writes, never prints and
never renders -- body/rutas/hoy.py turns each dict into the strip's own words (i18n, the relative-
date helper) and today.html draws the card.

Left out, and why (BODY-HOY-LO-QUE-VENCE-1-INV's own census): shared-meal portions and shift
imports carry no date to sort by; the pain follow-up and the "still going?" period check are
already their own interactive cards; a future plan session belongs to Metas' own week view; the
night-shift week's card stays exactly as it is -- it carries a recovery estimate the strip would
only repeat, never a date fact of its own the strip would otherwise be the only place to say; the
sleep stream (BODY-HOY-LO-QUE-VENCE-1.W ruling 3, same reasoning as BODY-HOY-PENDIENTES-REGISTRO-1
ruling 3) stays out too -- a missing night is the watch's own sync problem, already Hoy's own sync
card, never this strip's to repeat.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import fechas, goals, household

VENCE_MODELO = {
    "version": "vence-v1",
    "max_lineas": 4,        # the card's own display cap -- ruling 3: the 5th+ line collapses into "+N more"
    "horizonte_dias": 14,   # how far ahead a fact counts as "coming due" at all -- ruling 1
    "horizontes_max": 2,    # plan horizons BEYOND the plan card's own cap this strip still reads for
                             # a checkpoint/deadline it has not already named -- ruling 2
}

_ESTADO_ORDEN = {"vencido": 0, "hoy": 1, "pronto": 2}


def _estado(dia: date, hoy: date) -> str | None:
    """vencido/hoy/pronto against `hoy`, or None once `dia` sits further ahead than
    VENCE_MODELO's own horizonte_dias -- not yet coming due, so not this strip's job. No ceiling
    looking backward: an overdue fact stays overdue, however old, until it is resolved."""
    delta = (dia - hoy).days
    if delta > VENCE_MODELO["horizonte_dias"]:
        return None
    return "vencido" if delta < 0 else ("hoy" if delta == 0 else "pronto")


def _linea(tipo: str, dia: date, hoy: date, href: str, datos: dict) -> dict | None:
    estado = _estado(dia, hoy)
    if estado is None:
        return None
    return {"tipo": tipo, "dia": dia.isoformat(), "estado": estado, "href": href, "datos": datos}


def _lineas_mcp(hoy: date, mcp_pendientes: dict | None) -> list[dict]:
    """Ruling 2: the assistant's own proposals still waiting, one line -- its own `dia` is the
    SOONEST of their `expira_en` instants (the one closest to running out), never guessed: a
    source with no proposal in force (`mcp_pendientes` is None) has no date to sort by and is
    dropped before this ever runs."""
    if not mcp_pendientes:
        return []
    dia = date.fromisoformat(mcp_pendientes["expira"][:10])
    href = f"/?asis_conversacion={mcp_pendientes['conversacion_id']}#asis-btn"
    linea = _linea("mcp", dia, hoy, href, {"n": mcp_pendientes["n"]})
    return [linea] if linea else []


def _lineas_cobertura(hoy: date, cobertura_pendientes: dict, cadencia: dict) -> list[dict]:
    """Ruling 2: one line per missing log stream -- daily streams due TODAY (`dia = hoy`), weekly
    streams due by this ISO week's own Sunday (`pronto` until then, `vencido` after). `sueno`'s own
    cadence is 'nightly' (registro_cobertura.MODELO), neither 'daily' nor 'weekly', so it never
    reaches a `dia` here at all -- a missing night is the watch's own sync problem, already Hoy's
    own sync card, never this strip's to repeat. `datos` carries `cadencia` alongside `stream`
    (BODY-HOY-LO-QUE-VENCE-PULIDO-1 ruling 2) -- body/rutas/hoy.py's own `_vence_texto` reads it to
    pick the daily or weekly wording, never guessing the cadence back from the stream name."""
    datos = next(iter(cobertura_pendientes.values()), None)
    if not datos:
        return []
    domingo = fechas.lunes_de(hoy) + timedelta(days=6)
    out = []
    for stream in datos["faltan"]:
        cad = cadencia.get(stream)
        if cad == "daily":
            dia = hoy
        elif cad == "weekly":
            dia = domingo
        else:
            continue
        linea = _linea("cobertura", dia, hoy, "#asis-btn", {"stream": stream, "cadencia": cad})
        if linea:
            out.append(linea)
    return out


def _lineas_horizontes_extra(conn, hoy: date, ids_extra: list[int]) -> list[dict]:
    """Ruling 2: plan checkpoints and deadlines for the horizons BEYOND the plan card's own cap --
    their deadline, and their single SOONEST not-yet-passed checkpoint, never the heavier
    criteria-evaluating `horizonte.checkpoints()`/`horizonte.resumen()`: this strip only ever says
    WHEN a checkpoint falls, never whether it was met. Two bounded queries total, never one per
    horizon. `ids_extra` is capped to VENCE_MODELO's own horizontes_max HERE, not trusted from the
    caller -- a horizon beyond that cap adds nothing, however many the caller passes in."""
    ids_extra = ids_extra[:VENCE_MODELO["horizontes_max"]]
    if not ids_extra:
        return []
    out = []
    placeholders = ",".join("?" * len(ids_extra))
    for g in conn.execute(f"SELECT id, title, deadline FROM goal WHERE id IN ({placeholders})", ids_extra):
        if not g["deadline"]:
            continue
        linea = _linea("deadline", date.fromisoformat(g["deadline"]), hoy, f"/goals/{g['id']}",
                        {"titulo": g["title"]})
        if linea:
            out.append(linea)
    vistos = set()
    for c in conn.execute(
            f"SELECT horizonte_goal_id, day, titulo FROM horizonte_checkpoint "
            f"WHERE horizonte_goal_id IN ({placeholders}) AND day >= ? ORDER BY horizonte_goal_id, day",
            (*ids_extra, hoy.isoformat())):
        gid = c["horizonte_goal_id"]
        if gid in vistos:
            continue
        vistos.add(gid)
        linea = _linea("checkpoint", date.fromisoformat(c["day"]), hoy, f"/goals/{gid}", {"titulo": c["titulo"]})
        if linea:
            out.append(linea)
    return out


def _lineas_metas(conn, person_id: int, hoy: date) -> list[dict]:
    """Ruling 2: every active METRIC goal's own deadline inside the horizon (or already past it),
    scoped with goals.py's own goal_scope_sql -- household included, the same scope Metas already
    uses. One statement, never one per goal: the date filter (`deadline <= ?`) is pushed into the
    SQL itself rather than fetched unbounded and filtered in Python, since an inactive person could
    carry years of old metric goals."""
    hh = household.household_of(conn, person_id)
    scope_sql, scope_params = goals.goal_scope_sql(person_id, hh)
    limite = (hoy + timedelta(days=VENCE_MODELO["horizonte_dias"])).isoformat()
    out = []
    for g in conn.execute(
            f"SELECT id, title, deadline FROM goal WHERE status = 'active' AND kind = 'metric' "
            f"AND deadline IS NOT NULL AND deadline <= ? AND {scope_sql}",
            (limite, *scope_params)):
        linea = _linea("deadline", date.fromisoformat(g["deadline"]), hoy, f"/goals/{g['id']}",
                        {"titulo": g["title"]})
        if linea:
            out.append(linea)
    return out


def lineas(conn, person_id: int, hoy: date, *, mcp_pendientes: dict | None,
           cobertura_pendientes: dict, cadencia: dict, ids_extra: list[int]) -> list[dict]:
    """Every dated fact in scope (ruling 2's four sources), sorted overdue-first and then
    soonest-first within each state (ruling 1: a single date sort would put overdue items last).
    `mcp_pendientes` and `cobertura_pendientes` are sources Hoy already computed for its own other
    cards, passed in rather than read again; `ids_extra` is the person's own active plan horizons
    beyond the plan card's cap, in any number -- `_lineas_horizontes_extra` caps it to
    horizontes_max itself."""
    out: list[dict] = []
    out += _lineas_mcp(hoy, mcp_pendientes)
    out += _lineas_cobertura(hoy, cobertura_pendientes, cadencia)
    out += _lineas_horizontes_extra(conn, hoy, ids_extra)
    out += _lineas_metas(conn, person_id, hoy)
    out.sort(key=lambda l: (_ESTADO_ORDEN[l["estado"]], l["dia"]))
    return out


def cuando(t, dia: str, hoy: date) -> str:
    """BODY-HOY-LO-QUE-VENCE-1.W ruling 2: the strip's OWN relative-time words, never an absolute
    date however far ahead `dia` falls -- `body/rutas/hoy.py`'s own `_dia_relativo` looks only
    backward (hoy/ayer, falling back to an absolute date otherwise) and other screens depend on
    that, so it is left unchanged; this is a separate, bidirectional function, used by the strip
    alone. Literal `t(...)` calls only, each a plain es/en pair with the app's own singular/plural
    convention -- `en_dias_one`/`hace_dias_one` never fire through this function today (a 1-day gap
    is always "mañana"/"ayer" first), kept for the same reason every other counted pair in this app
    keeps its singular form even where a caller rarely reaches it."""
    delta = (date.fromisoformat(dia) - hoy).days
    if delta == 0:
        return t("vence.cuando.hoy")
    if delta == 1:
        return t("vence.cuando.manana")
    if delta == -1:
        return t("vence.cuando.ayer")
    if delta > 0:
        return t("vence.cuando.en_dias_one") if delta == 1 else t("vence.cuando.en_dias_other", n=t.num(delta))
    n = -delta
    return t("vence.cuando.hace_dias_one") if n == 1 else t("vence.cuando.hace_dias_other", n=t.num(n))
