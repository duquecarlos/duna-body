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
from datetime import date, timedelta

from . import db, fechas


class MetricDesconocida(Exception):
    """`serie`'s own metric_key is not in metric_def -- the route answers 404."""


class RangoDemasiadoLargo(Exception):
    """`serie`'s own [desde, hasta] spans more than MAX_SERIE_DIAS -- the route answers 400."""


MAX_SERIE_DIAS = 366


def _figuras(conn, person_id: int, dia: date) -> list[dict]:
    """Every metric_key's own latest stored day at or before `dia` -- unfiltered (ruling 2), each
    metric independently (a metric synced this morning and one not synced in days both show their
    own most recent value, never forced onto a single shared day)."""
    defs = {r["key"]: r["kind"] for r in conn.execute("SELECT key, kind FROM metric_def")}
    dia_iso = dia.isoformat()
    rows = conn.execute(
        "SELECT metric_key, value, day FROM daily_metric dm WHERE person_id = ? AND day <= ? "
        "AND day = (SELECT MAX(day) FROM daily_metric WHERE person_id = dm.person_id "
        "AND metric_key = dm.metric_key AND day <= ?) ORDER BY metric_key",
        (person_id, dia_iso, dia_iso),
    ).fetchall()
    return [{"metric_key": r["metric_key"], "value": r["value"], "kind": defs[r["metric_key"]], "day": r["day"]}
            for r in rows]


def _plan_dias(conn, person_id: int, dia: date) -> list[dict]:
    """`dia`'s and `dia`+1's plan rows (ruling 2), any goal, unfiltered. `ejercicios`
    (BODY-HOGAR-EJERCICIOS-1 ruling 5) is the row's own `ejercicios_json` parsed and passed
    verbatim -- the exact list `planear_entreno` stored -- or None when the column is null."""
    siguiente = (dia + timedelta(1)).isoformat()
    rows = conn.execute(
        "SELECT id, day, time, title, status, ejercicios_json FROM plan WHERE person_id = ? "
        "AND day IN (?, ?) ORDER BY day, time", (person_id, dia.isoformat(), siguiente),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        ejercicios_json = d.pop("ejercicios_json")
        d["ejercicios"] = json.loads(ejercicios_json) if ejercicios_json is not None else None
        out.append(d)
    return out


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


def manana(conn, person_id: int, dia: date) -> dict:
    """GET /api/hogar/manana's own shape, Hogar's draft (C:/Duna/hogar/dev-protocol/contracts/
    body-v1.md), ruling 2: `dia`'s figures, plan and the active horizon's current week.
    BODY-HOGAR-TURNOS-1 ruling 1 adds `turnos`."""
    return {
        "dia": dia.isoformat(),
        "figuras": _figuras(conn, person_id, dia),
        "plan": _plan_dias(conn, person_id, dia),
        "horizonte": _horizonte_actual(conn, person_id, dia),
        "turnos": turnos_alrededor(conn, person_id, dia),
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
