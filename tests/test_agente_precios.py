"""BODY-AGENTE-PRECIOS-1: the versioned, cited price seed and its loader."""
import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db
from body.agente import cliente, precios, uso

PROVEEDOR = "anthropic"
MODELO = "claude-sonnet-5-5"


def _conn():
    c = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


def _ruta_json(datos: dict) -> Path:
    ruta = Path(tempfile.mkdtemp(dir=TMP)) / "precios.json"
    ruta.write_text(json.dumps(datos), encoding="utf-8")
    return ruta


def _filas_tipo(conn, proveedor: str, modelo: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT tipo, micro_usd_por_millon FROM precio_modelo WHERE proveedor = ? AND modelo = ?",
        (proveedor, modelo),
    ).fetchall()
    return {r["tipo"]: r["micro_usd_por_millon"] for r in rows}


class SembrarTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()

    def test_sembrar_on_an_empty_table_inserts_four_rows(self):
        n = precios.sembrar(self.conn)
        self.assertEqual(n, 4)
        self.assertEqual(
            uso._precios(self.conn, PROVEEDOR, MODELO),
            {"entrada": 2_000_000, "salida": 10_000_000, "cache_lectura": 200_000, "cache_escritura": 2_500_000},
        )

    def test_a_second_run_inserts_nothing(self):
        precios.sembrar(self.conn)
        self.assertEqual(precios.sembrar(self.conn), 0)

    def test_a_preexisting_operator_row_is_kept(self):
        self.conn.execute(
            "INSERT INTO precio_modelo (proveedor, modelo, tipo, micro_usd_por_millon) VALUES (?, ?, 'entrada', ?)",
            (PROVEEDOR, MODELO, 999),
        )
        self.conn.commit()
        n = precios.sembrar(self.conn)
        self.assertEqual(n, 3)  # the other three tipos; 'entrada' was already there
        self.assertEqual(_filas_tipo(self.conn, PROVEEDOR, MODELO)["entrada"], 999)

    def test_a_file_missing_a_tipo_inserts_nothing_and_raises(self):
        ruta = _ruta_json({
            "version": "x", "fuente": "x",
            "precios": [{"proveedor": PROVEEDOR, "modelo": MODELO,
                         "micro_usd_por_millon": {"entrada": 1, "salida": 1, "cache_lectura": 1}}],
        })
        with self.assertRaises(precios.PreciosInvalidos):
            precios.sembrar(self.conn, ruta=ruta)
        self.assertEqual(_filas_tipo(self.conn, PROVEEDOR, MODELO), {})

    def test_a_file_with_a_negative_value_inserts_nothing_and_raises(self):
        ruta = _ruta_json({
            "version": "x", "fuente": "x",
            "precios": [{"proveedor": PROVEEDOR, "modelo": MODELO,
                         "micro_usd_por_millon": {"entrada": -1, "salida": 1,
                                                   "cache_lectura": 1, "cache_escritura": 1}}],
        })
        with self.assertRaises(precios.PreciosInvalidos):
            precios.sembrar(self.conn, ruta=ruta)
        self.assertEqual(_filas_tipo(self.conn, PROVEEDOR, MODELO), {})


class SpendCeilingWithTheSeedTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        precios.sembrar(self.conn)

    def test_a_days_spend_is_computed_in_micro_usd_and_the_spend_ceiling_decides(self):
        # One huge-output row at the real seed's rate blows well past the default 2 USD ceiling,
        # after only a single question -- the COUNT breaker (200/day) would never trip here.
        uso.registrar(self.conn, 1, PROVEEDOR, MODELO, cliente.Uso(entrada=0, salida=1_000_000),
                      1, "texto", None)
        with self.assertRaises(uso.TopeRefusal):
            uso.verificar_tope(self.conn, 1, PROVEEDOR, MODELO)

    def test_a_small_days_spend_at_the_seeds_rate_is_allowed(self):
        uso.registrar(self.conn, 1, PROVEEDOR, MODELO, cliente.Uso(entrada=10, salida=10),
                      1, "texto", None)
        uso.verificar_tope(self.conn, 1, PROVEEDOR, MODELO)  # does not raise

    def test_an_unpriced_model_still_uses_the_count_breaker(self):
        for _ in range(200):
            uso.registrar(self.conn, 1, PROVEEDOR, "modelo-sin-precio", cliente.Uso(entrada=1, salida=1),
                          1, "texto", None)
        with self.assertRaises(uso.TopeRefusal):
            uso.verificar_tope(self.conn, 1, PROVEEDOR, "modelo-sin-precio")


if __name__ == "__main__":
    unittest.main()
