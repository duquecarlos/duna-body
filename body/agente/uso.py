"""Usage rows and daily ceilings for Body's assistant (BODY-ASISTENTE-PROVEEDOR-1, decisions 5-6;
agent-capabilities-v1 section 6, amendments 2 and 5).

One `uso_agente` row per question, including failed ones. Prices are DATA (`precio_modelo`),
seeded EMPTY -- never invented here. The daily ceiling is a spend ceiling only when every row
counted today is priced in full for its OWN model (a person may switch model mid-day); otherwise
it falls back to the COUNT breaker (amendment 5). A ceiling whose own read fails REFUSES -- fail
closed, never open. "Today" is the person's own local day (their `person.timezone`, default
`America/Bogota`), not UTC's.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import precios

DEFAULT_TOPE_DIARIO_MICRO_USD = 2_000_000  # 2 USD
DEFAULT_CONTEO_DIARIO = 200
_TIPOS_PRECIO = ("entrada", "salida", "cache_lectura", "cache_escritura")

_logger = logging.getLogger("body.agente.uso")
_precios_sembrados = False


def _asegurar_precios_sembrados(conn: sqlite3.Connection) -> None:
    """Fills `precio_modelo` from the seed file once per process, before the first spend
    computation, so a fresh install gets prices with no hand step. Never overwrites an
    operator's own price (`precios.sembrar` is `INSERT OR IGNORE`). A seeding failure is logged
    by its class name only and otherwise ignored -- the count breaker still applies either way
    (fail closed, amendment 5)."""
    global _precios_sembrados
    if _precios_sembrados:
        return
    _precios_sembrados = True
    try:
        precios.sembrar(conn)
    except Exception as e:
        _logger.warning("precios.sembrar failed: %s", type(e).__name__)


class TopeRefusal(Exception):
    """The daily ceiling (spend or count) is already reached, or its own read failed."""


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ahora_utc() -> datetime:
    """Separated from `_ahora` so a test can freeze "now" without touching the stored format."""
    return datetime.now(timezone.utc)


def _zona_persona(conn: sqlite3.Connection, person_id: int) -> ZoneInfo:
    row = conn.execute("SELECT timezone FROM person WHERE id = ?", (person_id,)).fetchone()
    nombre = (row["timezone"] if row is not None else None) or "America/Bogota"
    return ZoneInfo(nombre)


def _rango_dia_local_utc(conn: sqlite3.Connection, person_id: int) -> tuple[str, str]:
    """[local midnight, next local midnight) in the person's own timezone, as UTC-isoformat bounds
    comparable to `creado_en` (same construction as `_ahora`: `timezone.utc`, seconds timespec)."""
    tz = _zona_persona(conn, person_id)
    hoy_local = _ahora_utc().astimezone(tz).date()
    inicio_local = datetime(hoy_local.year, hoy_local.month, hoy_local.day, tzinfo=tz)
    fin_local = inicio_local + timedelta(days=1)
    inicio_utc = inicio_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    fin_utc = fin_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    return inicio_utc, fin_utc


def _topes_persona(conn: sqlite3.Connection, person_id: int) -> tuple[int, int]:
    diario = int(os.environ.get("BODY_AGENTE_TOPE_DIARIO_MICRO_USD", DEFAULT_TOPE_DIARIO_MICRO_USD))
    conteo = int(os.environ.get("BODY_AGENTE_CONTEO_DIARIO", DEFAULT_CONTEO_DIARIO))
    row = conn.execute(
        "SELECT diario_micro_usd, conteo_diario FROM tope_agente WHERE person_id = ?", (person_id,)
    ).fetchone()
    if row is not None:
        if row["diario_micro_usd"] is not None:
            diario = row["diario_micro_usd"]
        if row["conteo_diario"] is not None:
            conteo = row["conteo_diario"]
    return diario, conteo


def _precios(conn: sqlite3.Connection, proveedor: str, modelo: str) -> dict[str, int] | None:
    rows = conn.execute(
        "SELECT tipo, micro_usd_por_millon FROM precio_modelo WHERE proveedor = ? AND modelo = ?",
        (proveedor, modelo),
    ).fetchall()
    if not rows:
        return None
    precios = {r["tipo"]: r["micro_usd_por_millon"] for r in rows}
    return precios if all(t in precios for t in _TIPOS_PRECIO) else None


def _gasto_hoy_micro_usd(conn: sqlite3.Connection, person_id: int, inicio_utc: str, fin_utc: str) -> int | None:
    """Sums today's rows at EACH ROW'S OWN (proveedor, modelo) prices -- a person may switch model
    mid-day. Returns None (fall back to the count breaker, amendment 5) when any of today's rows
    was made with a model that has no full price."""
    rows = conn.execute(
        "SELECT proveedor, modelo, entrada, salida, cache_lectura, cache_escritura FROM uso_agente "
        "WHERE person_id = ? AND creado_en >= ? AND creado_en < ?",
        (person_id, inicio_utc, fin_utc),
    ).fetchall()
    total_micro = 0
    precios_vistos: dict[tuple[str, str], dict[str, int] | None] = {}
    for r in rows:
        clave = (r["proveedor"], r["modelo"])
        if clave not in precios_vistos:
            precios_vistos[clave] = _precios(conn, *clave)
        precios = precios_vistos[clave]
        if precios is None:
            return None
        for tipo in _TIPOS_PRECIO:
            total_micro += r[tipo] * precios[tipo]
    return total_micro // 1_000_000


def _conteo_hoy(conn: sqlite3.Connection, person_id: int, inicio_utc: str, fin_utc: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM uso_agente "
        "WHERE person_id = ? AND creado_en >= ? AND creado_en < ? AND es_pregunta = 1",
        (person_id, inicio_utc, fin_utc),
    ).fetchone()
    return row["n"]


def verificar_tope(conn: sqlite3.Connection, person_id: int, proveedor: str, modelo: str) -> None:
    """Checked BEFORE the first model round. Raises `TopeRefusal` when today's ceiling -- today in
    the PERSON'S OWN local day, not UTC's -- is already reached, or when the ceiling's own read
    fails for any reason."""
    try:
        _asegurar_precios_sembrados(conn)
        diario, tope_conteo = _topes_persona(conn, person_id)
        inicio_utc, fin_utc = _rango_dia_local_utc(conn, person_id)
        gasto = None
        if _precios(conn, proveedor, modelo) is not None:
            gasto = _gasto_hoy_micro_usd(conn, person_id, inicio_utc, fin_utc)
        if gasto is not None:
            if gasto >= diario:
                raise TopeRefusal()
        else:
            if _conteo_hoy(conn, person_id, inicio_utc, fin_utc) >= tope_conteo:
                raise TopeRefusal()
    except TopeRefusal:
        raise
    except Exception as e:
        raise TopeRefusal() from e


def registrar(conn: sqlite3.Connection, person_id: int, proveedor: str, modelo: str, uso,
              rondas: int, motivo_fin: str | None, codigo_error: str | None,
              es_pregunta: bool = True, superficie: str = "asistente") -> None:
    with conn:
        conn.execute(
            "INSERT INTO uso_agente (person_id, creado_en, proveedor, modelo, entrada, salida, "
            "cache_lectura, cache_escritura, rondas, motivo_fin, codigo_error, es_pregunta, superficie) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (person_id, _ahora(), proveedor, modelo, uso.entrada, uso.salida, uso.cache_lectura,
             uso.cache_escritura, rondas, motivo_fin, codigo_error, 1 if es_pregunta else 0, superficie),
        )
