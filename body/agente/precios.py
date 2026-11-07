"""Versioned, cited prices for `precio_modelo` (BODY-AGENTE-PRECIOS-1), seeded from
`precios.json` next to this file. `precio_modelo` is DATA, not code: this module only loads
the file and inserts rows the table does not already have -- an operator's own price, set by
hand, is never overwritten (`INSERT OR IGNORE`, keyed on the four-column (proveedor, modelo,
tipo, desde) primary key, BODY-MODELOS-ESTANDAR-1). A model with no row here, and none set by an
operator, stays unpriced and the count breaker applies (agent-capabilities-v1 amendment 5).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

_RUTA_PRECIOS = Path(__file__).with_name("precios.json")
_TIPOS = ("entrada", "salida", "cache_lectura", "cache_escritura")
_FECHA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PreciosInvalidos(Exception):
    """The price file is malformed. Nothing was inserted."""


def _filas_validadas(ruta: Path) -> list[tuple[str, str, str, str, int]]:
    try:
        texto = ruta.read_text(encoding="utf-8")
    except OSError as e:
        raise PreciosInvalidos(f"cannot read {ruta}: {e}") from e
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError as e:
        raise PreciosInvalidos(f"{ruta} is not valid JSON: {e}") from e

    precios = datos.get("precios") if isinstance(datos, dict) else None
    if not isinstance(precios, list) or not precios:
        raise PreciosInvalidos(f"{ruta}: 'precios' must be a non-empty list")

    filas: list[tuple[str, str, str, str, int]] = []
    for entrada in precios:
        if not isinstance(entrada, dict):
            raise PreciosInvalidos(f"{ruta}: each price entry must be an object")
        proveedor = entrada.get("proveedor")
        modelo = entrada.get("modelo")
        desde = entrada.get("desde")
        tabla = entrada.get("micro_usd_por_millon")
        if not isinstance(proveedor, str) or not proveedor:
            raise PreciosInvalidos(f"{ruta}: missing or invalid 'proveedor'")
        if not isinstance(modelo, str) or not modelo:
            raise PreciosInvalidos(f"{ruta}: missing or invalid 'modelo'")
        if not isinstance(desde, str) or not _FECHA_RE.match(desde):
            raise PreciosInvalidos(
                f"{ruta}: {proveedor}/{modelo}: 'desde' must be a YYYY-MM-DD date, got {desde!r}"
            )
        if not isinstance(tabla, dict):
            raise PreciosInvalidos(f"{ruta}: {proveedor}/{modelo}: missing 'micro_usd_por_millon'")
        for tipo in _TIPOS:
            valor = tabla.get(tipo)
            if not isinstance(valor, int) or isinstance(valor, bool) or valor <= 0:
                raise PreciosInvalidos(
                    f"{ruta}: {proveedor}/{modelo}/{tipo} must be a positive integer, got {valor!r}"
                )
            filas.append((proveedor, modelo, tipo, desde, valor))
    return filas


def sembrar(conn: sqlite3.Connection, ruta: Path = _RUTA_PRECIOS) -> int:
    """Inserts every (proveedor, modelo, tipo, desde) from `ruta` that `precio_modelo` does not
    already have. Validates the whole file first -- a malformed file raises `PreciosInvalidos` and
    inserts nothing. Returns the number of rows actually inserted."""
    filas = _filas_validadas(ruta)
    insertadas = 0
    with conn:
        for proveedor, modelo, tipo, desde, valor in filas:
            cur = conn.execute(
                "INSERT OR IGNORE INTO precio_modelo (proveedor, modelo, tipo, desde, micro_usd_por_millon) "
                "VALUES (?, ?, ?, ?, ?)",
                (proveedor, modelo, tipo, desde, valor),
            )
            insertadas += cur.rowcount
    return insertadas


if __name__ == "__main__":
    from body import config, db

    _conn = db.connect(config.DB_PATH)
    db.init(_conn)
    try:
        print(sembrar(_conn))
    finally:
        _conn.close()
