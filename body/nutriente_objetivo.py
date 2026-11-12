"""Daily nutrient targets (BODY-NUTRIENTE-OBJETIVO-1, ORCH-CARLOS-DELEGATES-TIER-1-1): the one
module that reads `db.nutrient_target`. The person sets every value through the existing proposal
card flow (body/agente/herramientas.py's `proponer_objetivo_nutriente`, body/agente/propuestas.py's
own apply/undo writer, both calling `db.set_nutrient_target`/`db.borrar_nutrient_target` directly);
nothing here writes.

A changed target is a NEW row (`db` ruling 1), so history stays: the target for a day is the row
with the latest `start_day <= day`. `kind = 'per_kg'` resolves against the person's own latest
`body_weight_kg` within `PESO_DIAS_VENTANA` days of that day -- with none, `objetivo` is None and
`falta_peso` is True: an honest gap, never a guessed weight.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .i18n import T

# How many days back `ultimo_valor` looks for a metric's latest value -- used here for the
# per_kg resolution's own body_weight_kg lookup (ruling 2).
PESO_DIAS_VENTANA = 30

# BODY-NUTRIENTE-OBJETIVO-1 ruling 6: the one branch Ajustes' own form needs, held as data here
# (never as a branch in the template) -- caffeine and alcohol are LIMITS by default, every other
# intake key defaults to "reach at least".
SENTIDO_DEFAULT = {"caffeine_mg": "como_maximo", "alcohol_g": "como_maximo"}


def sentido_por_defecto(metric_key: str) -> str:
    return SENTIDO_DEFAULT.get(metric_key, "al_menos")


def _fmt_cantidad(t: "T", amount: float) -> str:
    """Whole numbers show no decimals (130 g, 400 mg); a per_kg ratio (1.6) keeps one."""
    decimales = 0 if float(amount).is_integer() else 1
    return t.num(amount, decimales)


def texto_objetivo(t: "T", kind: str, amount: float | None, sentido: str, unidad: str) -> str:
    """The target in words (ruling 3's card, ruling 6's Ajustes listing): "at least 130 g a day",
    "at most 200 mg a day", "1.6 g per kg of body weight", "no daily target". `sentido` is ignored
    for `kind = 'per_kg'` -- the ratio alone is the words, the same way the mock shows it."""
    if kind == "off":
        return t("nutriente.objetivo.off")
    if kind == "per_kg":
        return t("nutriente.objetivo.per_kg", cantidad=_fmt_cantidad(t, amount), unidad=unidad)
    clave = "nutriente.objetivo.al_menos" if sentido == "al_menos" else "nutriente.objetivo.como_maximo"
    return t(clave, cantidad=_fmt_cantidad(t, amount), unidad=unidad)


def ultimo_valor(conn: sqlite3.Connection, person_id: int, metric_key: str, day: str, dias: int) -> float | None:
    """The latest `daily_metric` value of `metric_key` on or before `day`, within `dias` days --
    generic over any metric, never hardcoded to body_weight_kg (so a future per-something-else
    target can reuse it unchanged)."""
    desde = (date.fromisoformat(day) - timedelta(days=dias)).isoformat()
    row = conn.execute(
        "SELECT value FROM daily_metric WHERE person_id = ? AND metric_key = ? AND day <= ? AND day >= ? "
        "ORDER BY day DESC LIMIT 1",
        (person_id, metric_key, day, desde),
    ).fetchone()
    return row["value"] if row else None


def _resolver(fila: sqlite3.Row, peso: float | None) -> dict:
    if fila["kind"] == "per_kg":
        objetivo = fila["amount"] * peso if peso is not None else None
        falta_peso = peso is None
    else:
        objetivo = fila["amount"]
        falta_peso = False
    return {"kind": fila["kind"], "amount": fila["amount"], "sentido": fila["sentido"], "objetivo": objetivo,
            "start_day": fila["start_day"], "falta_peso": falta_peso}


def objetivo_del_dia(conn: sqlite3.Connection, person_id: int, metric_key: str, day: str) -> dict | None:
    """None when no target covers this day, or the latest one has `kind = 'off'`. Otherwise
    {kind, amount, sentido, objetivo (resolved in the metric's unit, or None with falta_peso True),
    start_day, falta_peso}."""
    row = conn.execute(
        "SELECT start_day, kind, amount, sentido FROM nutrient_target WHERE person_id = ? AND metric_key = ? "
        "AND start_day <= ? ORDER BY start_day DESC LIMIT 1",
        (person_id, metric_key, day),
    ).fetchone()
    if row is None or row["kind"] == "off":
        return None
    peso = ultimo_valor(conn, person_id, "body_weight_kg", day, PESO_DIAS_VENTANA) if row["kind"] == "per_kg" \
        else None
    return _resolver(row, peso)


def _vigentes(conn: sqlite3.Connection, person_id: int, day: str) -> dict[str, sqlite3.Row]:
    """Every metric_key's own latest `nutrient_target` row with `start_day <= day`, one query
    (batched, never one per metric_key -- BODY-RENDIMIENTO-1's own per-route query ceiling),
    including an 'off' row (the caller decides what that means)."""
    filas = conn.execute(
        "SELECT metric_key, start_day, kind, amount, sentido FROM nutrient_target "
        "WHERE person_id = ? AND start_day <= ? ORDER BY metric_key, start_day DESC",
        (person_id, day),
    ).fetchall()
    vigentes: dict[str, sqlite3.Row] = {}
    for fila in filas:
        vigentes.setdefault(fila["metric_key"], fila)
    return vigentes


def objetivos_vigentes(conn: sqlite3.Connection, person_id: int, day: str) -> dict[str, dict]:
    """Every metric's current target as of `day` (Ajustes' own "Metas diarias" listing, ruling 6):
    {metric_key: {kind, amount, sentido, start_day}}, skipping a key an 'off' row ended."""
    return {k: {"kind": f["kind"], "amount": f["amount"], "sentido": f["sentido"], "start_day": f["start_day"]}
            for k, f in _vigentes(conn, person_id, day).items() if f["kind"] != "off"}


def estado_del_dia(conn: sqlite3.Connection, person_id: int, day: str) -> dict[str, dict]:
    """For every metric with a target on `day` (never 'off'): the total told that day (the
    daily_metric value registrar_ingesta keeps, 0.0 with none), the number of intake entries told,
    the resolved target, and `restante` (al_menos: target - total, floored at 0; como_maximo:
    target - total, may go negative = over the limit; None with falta_peso). Shared by Today, Día
    and the assistant's leer_hoy/leer_dia."""
    con_objetivo = {k: f for k, f in _vigentes(conn, person_id, day).items() if f["kind"] != "off"}
    if not con_objetivo:
        return {}
    peso = None
    if any(f["kind"] == "per_kg" for f in con_objetivo.values()):
        peso = ultimo_valor(conn, person_id, "body_weight_kg", day, PESO_DIAS_VENTANA)
    claves = tuple(con_objetivo)
    marcadores = ",".join("?" * len(claves))
    totales = {r["metric_key"]: r["value"] for r in conn.execute(
        f"SELECT metric_key, value FROM daily_metric WHERE person_id = ? AND day = ? AND metric_key IN ({marcadores})",
        (person_id, day, *claves))}
    conteos = {r["metric_key"]: r["n"] for r in conn.execute(
        f"SELECT metric_key, COUNT(*) AS n FROM ingesta WHERE person_id = ? AND day = ? "
        f"AND metric_key IN ({marcadores}) GROUP BY metric_key",
        (person_id, day, *claves))}
    out = {}
    for metric_key, fila in con_objetivo.items():
        objetivo = _resolver(fila, peso if fila["kind"] == "per_kg" else None)
        total = totales.get(metric_key, 0.0)
        n = conteos.get(metric_key, 0)
        restante = None
        if objetivo["objetivo"] is not None:
            diff = objetivo["objetivo"] - total
            restante = max(diff, 0.0) if fila["sentido"] == "al_menos" else diff
        out[metric_key] = {"total": total, "n": n, "objetivo": objetivo, "restante": restante}
    return out
