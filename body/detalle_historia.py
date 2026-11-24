"""python -m body.detalle_historia --cuenta-sub <sub> [--dry-run] [--max N]

BODY-DETALLE-HISTORIA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1): a manual, paced, resumable backfill of
full activity detail (heart-rate curve, laps, heart-rate zones) over a hosted person's WHOLE
Garmin history, in priority order (basketball, soccer, strength, everything else -- newest first
within each group) -- body.pull.traer_detalle_historia does the fetching and storing; this module
only resolves the person, prints the before/after coverage table, and reports counts.

`--dry-run` (default off): prints the BEFORE table only, makes NO Garmin call. Without it, the
Garmin token is read, the backfill runs (capped at `--max`, default 20, 1-200), and an AFTER
table is printed too, plus this run's own counts and stop reason.

Mirrors body/cobertura.py's own shape: `--cuenta-sub` resolves the destination person the same
way (required when hosted; the local person otherwise), read-only until the backfill itself
writes, and every printed line is counts, group names and months only -- never a figure, a
source_id or an activity name.
"""
from __future__ import annotations

import argparse
import sqlite3
from typing import Any

from . import cuenta, db, errores, garmin_adapter, garmin_cuenta, pull

# The three columns every group/month row carries -- 'con_detalle' (all three kinds stored, at
# least one with real content), 'garmin_no_tiene' (all three stored, every one the empty marker),
# 'pendientes' (fewer than three stored). Named here once so both the table builder and the
# printer share the same column order.
COLUMNAS = ("con_detalle", "garmin_no_tiene", "pendientes")


def _resolver_persona(conn: sqlite3.Connection, cuenta_sub: str | None) -> int:
    if cuenta.hosted() and not cuenta_sub:
        raise ValueError("--cuenta-sub is required when hosted")
    if cuenta_sub:
        row = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (cuenta_sub,)).fetchone()
        if row is None:
            raise ValueError("no person found for that --cuenta-sub")
        return row["id"]
    return db.default_person_id()


def _mes_de(dia: str) -> str:
    return dia[:7]  # "YYYY-MM-DD" -> "YYYY-MM"


def _clasificar_actividad(conn: sqlite3.Connection, person_id: int, source_id: str) -> str:
    """'con_detalle' / 'garmin_no_tiene' / 'pendientes' for one activity -- read the same
    activity_detail rows db.detalle_actividad_almacenado counts, but distinguishing a stored
    EMPTY MARKER (payload == '{}', pull.traer_detalle_actividad's own convention for a 404,
    db.py:472-473) from a kind Garmin actually answered with content."""
    filas = conn.execute(
        "SELECT payload FROM activity_detail WHERE person_id = ? AND activity_source = ? AND "
        "activity_source_id = ?",
        (person_id, garmin_adapter.SOURCE, source_id),
    ).fetchall()
    if len(filas) < len(db.DETALLE_KINDS):
        return "pendientes"
    tiene_contenido = any(f["payload"] != "{}" for f in filas)
    return "con_detalle" if tiene_contenido else "garmin_no_tiene"


def _recopilar_tabla(conn: sqlite3.Connection, person_id: int) -> dict[str, Any]:
    """Every one of this person's own Garmin activities, grouped by _PRIORIDAD_HISTORIA group and
    calendar month, with a count per COLUMNAS outcome -- plus the oldest month with any
    'con_detalle' activity and the newest month with any 'garmin_no_tiene' one (months only, the
    "does Garmin still serve <year>?" line). Direct SQL over `activity`, the same read-only style
    body.cobertura's own helpers already use over this table; db.actividades_sin_detalle only
    ever returns the still-pending subset, never a full census."""
    filas = conn.execute(
        "SELECT source_id, type, name, day FROM activity WHERE person_id = ? AND source = ?",
        (person_id, garmin_adapter.SOURCE),
    ).fetchall()
    tabla: dict[str, dict[str, dict[str, int]]] = {}
    meses_con_detalle: set[str] = set()
    meses_sin_garmin: set[str] = set()
    for fila in filas:
        grupo = pull.GRUPOS_PRIORIDAD_HISTORIA_NOMBRES[
            pull._grupo_prioridad_historia(fila["type"], fila["name"])]
        mes = _mes_de(fila["day"])
        clase = _clasificar_actividad(conn, person_id, fila["source_id"])
        fila_tabla = tabla.setdefault(grupo, {}).setdefault(mes, {c: 0 for c in COLUMNAS})
        fila_tabla[clase] += 1
        if clase == "con_detalle":
            meses_con_detalle.add(mes)
        elif clase == "garmin_no_tiene":
            meses_sin_garmin.add(mes)
    return {
        "tabla": tabla,
        "mes_mas_antiguo_con_detalle": min(meses_con_detalle) if meses_con_detalle else None,
        "mes_mas_nuevo_garmin_no_tiene": max(meses_sin_garmin) if meses_sin_garmin else None,
    }


def _imprimir_tabla(etiqueta: str, resultado: dict[str, Any]) -> None:
    for grupo in pull.GRUPOS_PRIORIDAD_HISTORIA_NOMBRES:
        for mes in sorted(resultado["tabla"].get(grupo, {})):
            fila = resultado["tabla"][grupo][mes]
            print(
                f"{etiqueta}: grupo={grupo} mes={mes} con_detalle={fila['con_detalle']} "
                f"garmin_no_tiene={fila['garmin_no_tiene']} pendientes={fila['pendientes']}"
            )
    print(
        f"{etiqueta}: mes_mas_antiguo_con_detalle={resultado['mes_mas_antiguo_con_detalle']} "
        f"mes_mas_nuevo_garmin_no_tiene={resultado['mes_mas_nuevo_garmin_no_tiene']}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.detalle_historia")
    p.add_argument("--cuenta-sub", help="the hosted person's Cuenta sub (required when hosted)")
    p.add_argument("--dry-run", action="store_true",
                    help="print the before table only; makes no Garmin call")
    p.add_argument("--max", type=int, default=20,
                    help="activities to fetch this run, 1-200 (default 20)")
    a = p.parse_args(argv)

    if not (1 <= a.max <= 200):
        print(f"refused: --max must be between 1 and 200 (got {a.max})")
        return 1

    conn = db.connect()
    try:
        db.init(conn)
        try:
            person_id = _resolver_persona(conn, a.cuenta_sub)
        except ValueError as exc:
            print(f"refused: {errores.texto_seguro(exc)}")
            return 1

        _imprimir_tabla("antes", _recopilar_tabla(conn, person_id))
        if a.dry_run:
            return 0

        token = garmin_cuenta.leer(conn, person_id)
        if token is None:
            print("refused: no Garmin token stored for this person")
            return 1

        resultado = pull.traer_detalle_historia(
            conn, person_id, token_str=token, max_actividades=a.max)

        _imprimir_tabla("despues", _recopilar_tabla(conn, person_id))
        detenida_por = resultado["detenida_por"] or "ninguna"
        print(
            f"corrida: resueltas={resultado['resueltas']} restantes={resultado['restantes']} "
            f"detenida_por={detenida_por}"
        )
        if resultado["fallidas_transitorias"]:
            detalle = ", ".join(
                f"{nombre} x{n}" for nombre, n in sorted(resultado["fallidas_transitorias"].items())
            )
            print(f"corrida: fallidas_transitorias={detalle}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
