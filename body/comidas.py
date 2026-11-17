"""Per-person meal moments (BODY-HOGAR-ESCRIBE-1-W ruling 2): today's hour-to-moment display rule,
now data instead of a hardcoded rule, so a person can eventually set their own windows (no screen
yet -- BODY-MOMENTOS-AJUSTES-1). A person with no `momento_comida` rows of their own reads
DEFAULT_MOMENTOS below, which is exactly the rule body/rutas/asistente.py's `_momento_ingesta_txt`
used before this table existed.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

DEFAULT_MOMENTOS: tuple[dict, ...] = (
    {"clave": "desayuno", "desde_hora": 5, "hasta_hora": 11, "esperado": 1, "orden": 1},
    {"clave": "almuerzo", "desde_hora": 11, "hasta_hora": 16, "esperado": 1, "orden": 2},
    {"clave": "merienda", "desde_hora": 16, "hasta_hora": 19, "esperado": 0, "orden": 3},
    {"clave": "cena", "desde_hora": 19, "hasta_hora": 23, "esperado": 1, "orden": 4},
)


def momentos(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """This person's own meal moments, in `orden` -- their own rows when they have any,
    DEFAULT_MOMENTOS otherwise."""
    rows = [dict(r) for r in conn.execute(
        "SELECT clave, desde_hora, hasta_hora, esperado, orden FROM momento_comida "
        "WHERE person_id = ? ORDER BY orden", (person_id,))]
    if rows:
        return rows
    return [dict(d) for d in DEFAULT_MOMENTOS]


def momento_de(conn: sqlite3.Connection, person_id: int, ts_local: str | None) -> str | None:
    """The clave whose [desde_hora, hasta_hora) window contains `ts_local`'s own hour, or None when
    there is no `ts_local` or its hour falls inside no window -- the same "sometime that day only"
    silence the old hardcoded rule always had."""
    if not ts_local:
        return None
    try:
        hora = datetime.fromisoformat(ts_local).hour
    except ValueError:
        return None
    for m in momentos(conn, person_id):
        if m["desde_hora"] <= hora < m["hasta_hora"]:
            return m["clave"]
    return None
