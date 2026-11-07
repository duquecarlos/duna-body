"""BODY-MODELOS-ESTANDAR-1: cases that fit neither test_agente_precios.py nor
test_agente_proveedores.py -- the precio_modelo rebuild on an old-shape install, dated-price
picking across Gemini's 2027-01-01 step, and (BODY-MODELOS-ESTANDAR-1-M) the single UTC date
basis a dated price is picked by."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import precios, uso

PROVEEDOR = "anthropic"
MODELO = "claude-sonnet-5-5"


class PrecioModeloRebuildTest(unittest.TestCase):
    """An old install's `precio_modelo` (no `desde` column, an operator's own hand-set price)
    survives db.init()'s rebuild with desde '2000-01-01', and `sembrar()` never overwrites it."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        self.conn.execute(
            "CREATE TABLE precio_modelo (proveedor TEXT NOT NULL, modelo TEXT NOT NULL, "
            "tipo TEXT NOT NULL, micro_usd_por_millon INTEGER NOT NULL, "
            "PRIMARY KEY (proveedor, modelo, tipo))"
        )
        self.conn.execute(
            "INSERT INTO precio_modelo (proveedor, modelo, tipo, micro_usd_por_millon) VALUES (?, ?, ?, ?)",
            (PROVEEDOR, MODELO, "entrada", 999_999),
        )
        self.conn.commit()

    def test_the_operator_row_survives_with_desde_2000_01_01(self):
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT desde, micro_usd_por_millon FROM precio_modelo WHERE proveedor = ? AND modelo = ? AND tipo = ?",
            (PROVEEDOR, MODELO, "entrada"),
        ).fetchone()
        self.assertEqual(row["desde"], "2000-01-01")
        self.assertEqual(row["micro_usd_por_millon"], 999_999)

    def test_sembrar_never_overwrites_the_rebuilt_operator_row(self):
        db.init(self.conn)
        precios.sembrar(self.conn)
        row = self.conn.execute(
            "SELECT micro_usd_por_millon FROM precio_modelo WHERE proveedor = ? AND modelo = ? "
            "AND tipo = ? AND desde = '2000-01-01'",
            (PROVEEDOR, MODELO, "entrada"),
        ).fetchone()
        self.assertEqual(row["micro_usd_por_millon"], 999_999)  # not the seed's 2_000_000


class DatedPricePickTest(unittest.TestCase):
    """Gemini's price steps on 2027-01-01 (modelos-ia-v1.1): a usage date before that day prices
    at the first row, a date on or after it prices at the second."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
        db.init(self.conn)
        precios.sembrar(self.conn)

    def test_a_date_before_the_step_prices_at_the_first_row(self):
        self.assertEqual(
            uso._precios(self.conn, "gemini", "gemini-3.8-flash", "2026-12-31"),
            {"entrada": 750_000, "salida": 3_750_000, "cache_lectura": 750_000, "cache_escritura": 750_000},
        )

    def test_a_date_on_the_step_prices_at_the_second_row(self):
        self.assertEqual(
            uso._precios(self.conn, "gemini", "gemini-3.8-flash", "2027-01-01"),
            {"entrada": 1_500_000, "salida": 7_500_000, "cache_lectura": 1_500_000, "cache_escritura": 1_500_000},
        )


class UtcDateBasisTest(unittest.TestCase):
    """Fix 1 (BODY-MODELOS-ESTANDAR-1-M): `verificar_tope`'s "is this model priced today" check
    and `_gasto_hoy_micro_usd`'s per-row pricing must pick a dated price by the SAME date basis --
    the UTC date -- or they can disagree near a person's local midnight. "Now" is frozen at
    2027-01-01T03:00Z, Bogota local 2026-12-31 22:00: the UTC day has already stepped to
    2027-01-01 (Gemini's price step) while the local day has not."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "c.db")
        db.init(self.conn)
        precios.sembrar(self.conn)
        self._ahora = datetime(2027, 1, 1, 3, 0, tzinfo=timezone.utc)
        # Bogota 2026-12-31 21:00 -- inside the person's local "today" range, UTC date 2027-01-01.
        self.conn.execute(
            "INSERT INTO uso_agente (person_id, creado_en, proveedor, modelo, entrada, salida, "
            "cache_lectura, cache_escritura, rondas, motivo_fin, codigo_error, es_pregunta, superficie) "
            "VALUES (1, '2027-01-01T02:00:00+00:00', 'gemini', 'gemini-3.8-flash', 1000000, 0, 0, 0, "
            "1, 'texto', NULL, 1, 'asistente')"
        )
        self.conn.commit()

    def test_both_checks_pick_the_2027_01_01_gemini_row(self):
        fechas_vistas = []
        original = uso._precios

        def _precios_espia(conn, proveedor, modelo, fecha):
            fechas_vistas.append(fecha)
            return original(conn, proveedor, modelo, fecha)

        with patch("body.agente.uso._ahora_utc", return_value=self._ahora), \
             patch("body.agente.uso._precios", side_effect=_precios_espia):
            uso.verificar_tope(self.conn, 1, "gemini", "gemini-3.8-flash")

        # Both the priced check (the upcoming call) and the row pricing (the stored row) asked
        # for the SAME date -- the UTC one, 2027-01-01 -- never the local 2026-12-31.
        self.assertEqual(set(fechas_vistas), {"2027-01-01"})


if __name__ == "__main__":
    unittest.main()
