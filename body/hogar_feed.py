"""Hogar's own read of Body (BODY-SERVICIO-HOGAR-1), Carlos's ruling dictated 2026-11-15 ~19:45,
relayed verbatim by the Personal desk: "don't worry about the restriction between apps in body,
finances, and [Hogar]. It's all me. It's all my own Duna servers, and I'm my own user, my own
data. So, drop every restriction. Allow free communication between all of them." -- so this module
filters nothing per figure, per unit or to a latest-value-only window; the one standing gate is the
`hogar:leer` device key itself (body/dispositivo.py), minted and revoked on the device-key page.

Pure reads over a connection, no route, no HTTP shape -- body/rutas/dispositivos.py calls these and
turns the result into JSON, the same split vida_export.py and horizonte.py already use.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import comidas, db, fechas, nutriente_objetivo


class MetricDesconocida(Exception):
    """`serie`'s own metric_key is not in metric_def -- the route answers 404."""


class RangoDemasiadoLargo(Exception):
    """`serie`'s own [desde, hasta] spans more than MAX_SERIE_DIAS -- the route answers 400."""


MAX_SERIE_DIAS = 366


def _metric_defs(conn) -> dict[str, dict]:
    """Every metric_def row's own `kind` and `unit`, keyed by `key` -- one query, shared by
    `_figuras` (its `kind`) and `_metas` (its `unit`, BODY-HOGAR-METAS-INGESTA-1 ruling 1) so
    `manana`'s own metric_def read stays the single one it always was."""
    return {r["key"]: {"kind": r["kind"], "unit": r["unit"]}
            for r in conn.execute("SELECT key, kind, unit FROM metric_def")}


def _figuras(conn, person_id: int, dia: date, defs: dict[str, dict]) -> list[dict]:
    """Every metric_key's own latest stored day at or before `dia` -- unfiltered (ruling 2), each
    metric independently (a metric synced this morning and one not synced in days both show their
    own most recent value, never forced onto a single shared day)."""
    dia_iso = dia.isoformat()
    rows = conn.execute(
        "SELECT metric_key, value, day FROM daily_metric_efectiva dm WHERE person_id = ? AND day <= ? "
        "AND day = (SELECT MAX(day) FROM daily_metric_efectiva WHERE person_id = dm.person_id "
        "AND metric_key = dm.metric_key AND day <= ?) ORDER BY metric_key",
        (person_id, dia_iso, dia_iso),
    ).fetchall()
    return [{"metric_key": r["metric_key"], "value": r["value"], "kind": defs[r["metric_key"]]["kind"],
              "day": r["day"]} for r in rows]


def _plan_filas(conn, person_id: int, desde: str, hasta: str) -> list[dict]:
    """Plan rows with `day` in [`desde`, `hasta`], any goal, unfiltered, ordered by day then time --
    the one shared reader `_plan_dias` (today/tomorrow) and `_plan_bloque` (BODY-HOGAR-PLAN-RANGO-1's
    fuller training block) both read through, so the field list -- id, day, time, title, status,
    ejercicios, nota_sesion -- lives in exactly one place. `ejercicios` (BODY-HOGAR-EJERCICIOS-1
    ruling 5) is the row's own `ejercicios_json` parsed and passed verbatim -- the exact list
    `planear_entreno` stored -- or None when the column is null. `nota_sesion` (BODY-HOGAR-ESCRIBE-2
    ruling 3) is the row's own column, verbatim, null when no one has called `anotar_sesion` on it
    yet -- so Hogar knows which sessions still lack a note."""
    rows = conn.execute(
        "SELECT id, day, time, title, status, ejercicios_json, nota_sesion FROM plan WHERE person_id = ? "
        "AND day BETWEEN ? AND ? ORDER BY day, time", (person_id, desde, hasta),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        ejercicios_json = d.pop("ejercicios_json")
        d["ejercicios"] = json.loads(ejercicios_json) if ejercicios_json is not None else None
        out.append(d)
    return out


def _plan_dias(conn, person_id: int, dia: date) -> list[dict]:
    """`dia`'s and `dia`+1's plan rows (ruling 2) -- `_plan_filas` over that two-day range."""
    return _plan_filas(conn, person_id, dia.isoformat(), (dia + timedelta(1)).isoformat())


def _plan_bloque(conn, person_id: int, dia: date, horizonte: dict | None) -> dict:
    """BODY-HOGAR-PLAN-RANGO-1: the whole training block from `dia` to the active horizon's
    deadline, so Hogar's high-mountain hike pilot can show it rather than just `dia` and `dia`+1.
    `horizonte` is `manana`'s own `_horizonte_actual` result, passed in rather than re-queried.
    `hasta` is that deadline when it is on or after `dia` (`hasta_por` "horizonte"), else `dia`+27
    (`hasta_por` "28_dias"). Either way the span is capped at MAX_SERIE_DIAS counting `dia`: a
    farther deadline is cut there and `hasta_por` stays "horizonte" -- `hasta` itself says where it
    was cut. `filas` is `_plan_filas` over [`dia`, `hasta`] -- the same reader, same shape, `plan`
    already uses."""
    tope = dia + timedelta(MAX_SERIE_DIAS - 1)
    deadline = date.fromisoformat(horizonte["deadline"]) if horizonte is not None else None
    if deadline is not None and deadline >= dia:
        hasta_por = "horizonte"
        hasta = min(deadline, tope)
    else:
        hasta_por = "28_dias"
        hasta = dia + timedelta(27)
    return {"desde": dia.isoformat(), "hasta": hasta.isoformat(), "hasta_por": hasta_por,
            "filas": _plan_filas(conn, person_id, dia.isoformat(), hasta.isoformat())}


def _horizonte_actual(conn, person_id: int, dia: date) -> dict | None:
    """The person's own active preparation goal (a milestone goal with no horizonte_goal_id of its
    own, i.e. not itself a child of another plan -- the same `kind = 'milestone' AND
    horizonte_goal_id IS NULL` test body/agente/herramientas.py's `_leer_planes` already uses), and
    which Monday-starting week `dia` falls in out of the horizon's whole span. None when this
    person has no active horizon."""
    row = conn.execute(
        "SELECT id, title, deadline, start_day FROM goal WHERE person_id = ? AND kind = 'milestone' "
        "AND horizonte_goal_id IS NULL AND status = 'active' ORDER BY id DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    if row is None:
        return None
    inicio = fechas.lunes_de(date.fromisoformat(row["start_day"]))
    fin = fechas.lunes_de(date.fromisoformat(row["deadline"]))
    semanas = max(1, (fin - inicio).days // 7 + 1)
    semana = max(1, min((fechas.lunes_de(dia) - inicio).days // 7 + 1, semanas))
    return {"goal_id": row["id"], "title": row["title"], "deadline": row["deadline"],
            "semana": semana, "semanas": semanas}


def turnos_alrededor(conn, person_id: int, dia: date) -> list[dict]:
    """Every shift that touches `dia` -- BODY-HOGAR-TURNOS-1 ruling 1: rows with `day` = `dia` or
    `dia`+1, plus a row with `day` = `dia`-1 whose `end_local` falls on `dia` or later (last
    night's shift still running this morning). The item shape is the row's own columns, as
    stored -- no `source`, no person id."""
    dia_iso = dia.isoformat()
    siguiente = (dia + timedelta(1)).isoformat()
    anterior = (dia - timedelta(1)).isoformat()
    rows = conn.execute(
        "SELECT day, kind, code, start_local, end_local FROM shift WHERE person_id = ? "
        "AND (day IN (?, ?) OR (day = ? AND end_local >= ?)) ORDER BY day, start_local",
        (person_id, dia_iso, siguiente, anterior, dia_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def turnos_rango(conn, person_id: int, desde: str, hasta: str) -> list[dict]:
    """Every shift row with `day` in [desde, hasta] (BODY-HOGAR-TURNOS-1 ruling 2), same item
    shape as `turnos_alrededor`."""
    rows = conn.execute(
        "SELECT day, kind, code, start_local, end_local FROM shift WHERE person_id = ? "
        "AND day BETWEEN ? AND ? ORDER BY day, start_local",
        (person_id, desde, hasta),
    ).fetchall()
    return [dict(r) for r in rows]


def turnos(conn, person_id: int, desde: str, hasta: str) -> dict:
    """GET /api/hogar/turnos's own shape (BODY-HOGAR-TURNOS-1 ruling 2). Raises
    RangoDemasiadoLargo for the route to turn into 400 -- same MAX_SERIE_DIAS rule as `serie`."""
    dias = (date.fromisoformat(hasta) - date.fromisoformat(desde)).days + 1
    if dias > MAX_SERIE_DIAS:
        raise RangoDemasiadoLargo(dias)
    return {"desde": desde, "hasta": hasta, "turnos": turnos_rango(conn, person_id, desde, hasta)}


def _comidas(conn, person_id: int, dia: date, ahora: datetime) -> dict:
    """GET /api/hogar/manana's `comidas` key (BODY-HOGAR-ESCRIBE-2 ruling 3): the person's own
    meal moments (their own rows, or the defaults -- `comidas.momentos`), every `comida` already
    registered on `dia` with its own stored moment or the one `momento_de` derives from `ts_local`
    (null when neither), and which esperado moment still has no registered meal. `faltan` is every
    esperado moment on a PAST `dia` (regardless of hour -- the day is over), none on a FUTURE `dia`
    (nothing is due yet), and on TODAY only those whose `desde_hora` has already passed `ahora`'s
    own local hour. No meal description, no nutrient -- just the clave and when it was eaten."""
    lista = comidas.momentos(conn, person_id)
    momentos_out = [{"clave": m["clave"], "desde_hora": m["desde_hora"], "hasta_hora": m["hasta_hora"],
                      "esperado": m["esperado"]} for m in lista]
    rows = conn.execute(
        "SELECT ts_local, momento FROM comida WHERE person_id = ? AND day = ? ORDER BY id",
        (person_id, dia.isoformat()),
    ).fetchall()
    registradas = []
    vistas = set()
    for r in rows:
        clave = r["momento"] if r["momento"] is not None else comidas.momento_de(conn, person_id, r["ts_local"])
        registradas.append({"momento": clave, "ts_local": r["ts_local"]})
        if clave is not None:
            vistas.add(clave)
    hoy = ahora.date()
    if dia < hoy:
        faltan = [m["clave"] for m in lista if m["esperado"] and m["clave"] not in vistas]
    elif dia > hoy:
        faltan = []
    else:
        faltan = [m["clave"] for m in lista if m["esperado"] and m["clave"] not in vistas
                  and m["desde_hora"] <= ahora.hour]
    return {"momentos": momentos_out, "registradas": registradas, "faltan": faltan}


def _metas(conn, person_id: int, dia: date, defs: dict[str, dict]) -> list[dict]:
    """GET /api/hogar/manana's own `metas` key (BODY-HOGAR-METAS-INGESTA-1, hub ref
    HOGAR-COCINA-IDEAS-1 ruling 1): one item per metric with a live daily intake target on `dia`,
    built from `nutriente_objetivo.estado_del_dia`'s own output and nothing else, plus the unit
    `metric_def` already carries -- never a second computation of the target (the orchestrator's
    ruling: Hogar's own guessed shape -- goal_id, direction, target_value, deadline -- does not
    exist in Body, and a second reading of the target would drift from what Today and Día show;
    Body computes `restante` so Hogar never re-derives per_kg or the floor rule). Ordered by
    metric_key, `[]` when none -- never omitted, never null. A metric whose latest target is 'off'
    never appears -- `estado_del_dia` already skips it."""
    estado = nutriente_objetivo.estado_del_dia(conn, person_id, dia.isoformat())
    out = []
    for metric_key in sorted(estado):
        e = estado[metric_key]
        o = e["objetivo"]
        out.append({
            "metric_key": metric_key,
            "unidad": defs[metric_key]["unit"],
            "sentido": o["sentido"],
            "kind": o["kind"],
            "amount": o["amount"],
            "objetivo": o["objetivo"],
            "falta_peso": o["falta_peso"],
            "desde": o["start_day"],
            "total": e["total"],
            "registros": e["n"],
            "restante": e["restante"],
        })
    return out


def manana(conn, person_id: int, dia: date, ahora: datetime) -> dict:
    """GET /api/hogar/manana's own shape, Hogar's draft (C:/Duna/hogar/dev-protocol/contracts/
    body-v1.md), ruling 2: `dia`'s figures, plan and the active horizon's current week.
    BODY-HOGAR-TURNOS-1 ruling 1 adds `turnos`. BODY-HOGAR-ESCRIBE-2 ruling 3 adds `comidas` --
    `ahora` is the person's own local wall-clock now (the route's `_ahora_local`), used only to
    decide which `comidas.faltan` moments have already passed when `dia` is today.
    BODY-HOGAR-METAS-INGESTA-1 adds `metas`. BODY-HOGAR-PLAN-RANGO-1 adds `plan_bloque` -- `plan`
    itself is unchanged."""
    defs = _metric_defs(conn)
    horizonte = _horizonte_actual(conn, person_id, dia)
    return {
        "dia": dia.isoformat(),
        "figuras": _figuras(conn, person_id, dia, defs),
        "plan": _plan_dias(conn, person_id, dia),
        "plan_bloque": _plan_bloque(conn, person_id, dia, horizonte),
        "horizonte": horizonte,
        "turnos": turnos_alrededor(conn, person_id, dia),
        "comidas": _comidas(conn, person_id, dia, ahora),
        "metas": _metas(conn, person_id, dia, defs),
    }


def serie(conn, person_id: int, metric_key: str, desde: str, hasta: str) -> dict:
    """GET /api/hogar/serie's own shape: one metric's daily series over [desde, hasta], at most
    MAX_SERIE_DIAS days (ruling 2). Raises MetricDesconocida / RangoDemasiadoLargo for the route to
    turn into 404 / 400."""
    row = conn.execute("SELECT kind FROM metric_def WHERE key = ?", (metric_key,)).fetchone()
    if row is None:
        raise MetricDesconocida(metric_key)
    dias = (date.fromisoformat(hasta) - date.fromisoformat(desde)).days + 1
    if dias > MAX_SERIE_DIAS:
        raise RangoDemasiadoLargo(dias)
    valores = db.series(conn, metric_key, desde, hasta, person_id)
    puntos = [{"day": d, "value": valores[d], "kind": row["kind"]} for d in sorted(valores)]
    return {"metric_key": metric_key, "puntos": puntos}
