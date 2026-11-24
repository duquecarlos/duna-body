"""BODY-COMIDA-PRODUCTO-1: body/productos.py -- the one module for products Body knows. Schema/
ownership isolation (ruling 1 CORRECTED: `producto` carries person_id XOR hogar_id, every read
through `household.household_of`), nutrition kept once per product (ruling 2), search, and the
compute rules a meal line's own nutrient contribution follows (ruling 3). Invented data only, no
real health figures."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, household, productos


def _conn(case: unittest.TestCase) -> sqlite3.Connection:
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


def _otra_persona(conn, person_id: int = 2, nombre: str = "Otra") -> None:
    conn.execute("INSERT INTO person (id, display_name) VALUES (?, ?)", (person_id, nombre))


def _hogar(conn, hogar_id: str, *person_ids: int) -> None:
    for pid in person_ids:
        conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, fuente, actualizado_at) "
            "VALUES (?, ?, 'local', '2026-01-01T00:00:00+00:00')", (pid, hogar_id))


class CrearProductoManualTest(unittest.TestCase):
    def test_creates_a_manual_product_owned_by_the_person(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Pechuga de pollo", marca="Demo",
                                                        tamano_neto=500.0, tamano_unidad="g")
        fila = conn.execute("SELECT * FROM producto WHERE id = ?", (producto_id,)).fetchone()
        self.assertEqual(fila["person_id"], 1)
        self.assertIsNone(fila["hogar_id"])
        self.assertEqual(fila["origen"], "manual")
        self.assertEqual(fila["nombre"], "Pechuga de pollo")
        self.assertEqual(fila["marca"], "Demo")
        self.assertEqual(fila["tamano_neto"], 500.0)
        self.assertEqual(fila["tamano_unidad"], "g")

    def test_name_is_required(self):
        conn = _conn(self)
        with self.assertRaises(ValueError):
            productos.crear_producto_manual(conn, 1, "   ")

    def test_unknown_tamano_unidad_is_refused(self):
        conn = _conn(self)
        with self.assertRaises(ValueError):
            productos.crear_producto_manual(conn, 1, "Algo", tamano_unidad="litros")

    def test_non_positive_tamano_neto_is_refused(self):
        conn = _conn(self)
        with self.assertRaises(ValueError):
            productos.crear_producto_manual(conn, 1, "Algo", tamano_neto=0)

    def test_brand_and_size_are_optional(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Algo simple")
        fila = conn.execute("SELECT marca, tamano_neto, tamano_unidad FROM producto WHERE id = ?",
                             (producto_id,)).fetchone()
        self.assertIsNone(fila["marca"])
        self.assertIsNone(fila["tamano_neto"])
        self.assertIsNone(fila["tamano_unidad"])


class EstablecerNutricionTest(unittest.TestCase):
    def test_sets_one_figure_per_metric_key(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, producto_id, "protein_g", 31.0, "100g", "etiqueta")
        fila = conn.execute(
            "SELECT amount_per, basis, fuente FROM producto_nutricion WHERE producto_id = ? AND metric_key = ?",
            (producto_id, "protein_g")).fetchone()
        self.assertEqual((fila["amount_per"], fila["basis"], fila["fuente"]), (31.0, "100g", "etiqueta"))

    def test_a_later_call_for_the_same_metric_replaces_it(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, producto_id, "protein_g", 31.0, "100g", "etiqueta")
        productos.establecer_nutricion(conn, producto_id, "protein_g", 28.0, "100g", "consulta")
        filas = conn.execute(
            "SELECT amount_per, fuente FROM producto_nutricion WHERE producto_id = ? AND metric_key = ?",
            (producto_id, "protein_g")).fetchall()
        self.assertEqual(len(filas), 1)
        self.assertEqual((filas[0]["amount_per"], filas[0]["fuente"]), (28.0, "consulta"))

    def test_every_row_of_the_same_product_must_share_one_basis(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, producto_id, "protein_g", 31.0, "100g", "etiqueta")
        with self.assertRaises(ValueError):
            productos.establecer_nutricion(conn, producto_id, "kcal", 165.0, "unidad", "etiqueta")

    def test_unknown_metric_key_is_refused(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Pollo")
        with self.assertRaises(ValueError):
            productos.establecer_nutricion(conn, producto_id, "resting_hr", 50.0, "100g", "etiqueta")

    def test_unknown_basis_is_refused(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Pollo")
        with self.assertRaises(ValueError):
            productos.establecer_nutricion(conn, producto_id, "protein_g", 31.0, "100kg", "etiqueta")

    def test_unknown_fuente_is_refused(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Pollo")
        with self.assertRaises(ValueError):
            productos.establecer_nutricion(conn, producto_id, "protein_g", 31.0, "100g", "medido")

    def test_non_positive_amount_per_is_refused(self):
        conn = _conn(self)
        producto_id = productos.crear_producto_manual(conn, 1, "Pollo")
        with self.assertRaises(ValueError):
            productos.establecer_nutricion(conn, producto_id, "protein_g", 0, "100g", "etiqueta")


class OwnershipIsolationTest(unittest.TestCase):
    """Ruling 1 CORRECTED: a manual product is person-owned; a household product is household-
    owned -- every read goes through `productos_visibles`/`buscar`, scoped by `household.household_of`."""

    def test_person_a_never_sees_person_b_s_manual_product(self):
        conn = _conn(self)
        _otra_persona(conn)
        productos.crear_producto_manual(conn, 2, "Secreto de Otra")
        visibles = productos.productos_visibles(conn, 1)
        self.assertEqual(visibles, [])
        self.assertEqual(productos.buscar(conn, 1, "secreto"), [])

    def test_a_household_product_is_visible_to_every_member(self):
        conn = _conn(self)
        _otra_persona(conn)
        _hogar(conn, "hogar-1", 1, 2)
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO producto (hogar_id, origen, nombre, created_at) VALUES (?, 'despensa', ?, ?)",
            ("hogar-1", "Leche entera", now))
        visibles_1 = productos.productos_visibles(conn, 1)
        visibles_2 = productos.productos_visibles(conn, 2)
        self.assertEqual([p["nombre"] for p in visibles_1], ["Leche entera"])
        self.assertEqual([p["nombre"] for p in visibles_2], ["Leche entera"])

    def test_one_household_never_sees_another_household_s_product(self):
        conn = _conn(self)
        _otra_persona(conn, 2, "En otro hogar")
        _hogar(conn, "hogar-1", 1)
        _hogar(conn, "hogar-2", 2)
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO producto (hogar_id, origen, nombre, created_at) VALUES (?, 'despensa', ?, ?)",
            ("hogar-2", "No es mio", now))
        self.assertEqual(productos.productos_visibles(conn, 1), [])

    def test_the_ownership_check_refuses_both_owners_at_once(self):
        conn = _conn(self)
        now = "2026-01-01T00:00:00+00:00"
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO producto (person_id, hogar_id, origen, nombre, created_at) "
                "VALUES (1, 'hogar-1', 'manual', 'x', ?)", (now,))

    def test_the_ownership_check_refuses_neither_owner(self):
        conn = _conn(self)
        now = "2026-01-01T00:00:00+00:00"
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO producto (origen, nombre, created_at) VALUES ('manual', 'x', ?)", (now,))


class ProductosVisiblesOrderTest(unittest.TestCase):
    def test_recently_eaten_by_this_person_sorts_before_the_rest_then_alphabetical(self):
        conn = _conn(self)
        banana = productos.crear_producto_manual(conn, 1, "Banana")
        arroz = productos.crear_producto_manual(conn, 1, "Arroz")
        zanahoria = productos.crear_producto_manual(conn, 1, "Zanahoria")
        productos.establecer_nutricion(conn, banana, "kcal", 90.0, "100g", "etiqueta")
        from body import db as db_mod
        db_mod.crear_comida(conn, 1, "2026-11-10", [{"metric_key": "kcal", "amount": 90.0, "estimado": False}],
                             productos=[{"producto_id": banana, "cantidad": 100.0, "unidad": "g",
                                         "nombre": "Banana"}])
        visibles = productos.productos_visibles(conn, 1)
        self.assertEqual([p["nombre"] for p in visibles], ["Banana", "Arroz", "Zanahoria"])

    def test_without_any_history_sorts_alphabetically_by_own_language_name(self):
        conn = _conn(self)
        productos.crear_producto_manual(conn, 1, "Zanahoria")
        productos.crear_producto_manual(conn, 1, "Arroz")
        productos.crear_producto_manual(conn, 1, "Banana")
        visibles = productos.productos_visibles(conn, 1)
        self.assertEqual([p["nombre"] for p in visibles], ["Arroz", "Banana", "Zanahoria"])

    def test_sorts_by_this_person_s_own_language_display_name(self):
        conn = _conn(self)
        db.set_language(conn, "en", 1)
        p1 = productos.crear_producto_manual(conn, 1, "Zanahoria")
        conn.execute("UPDATE producto SET nombre_en = 'Carrot' WHERE id = ?", (p1,))
        p2 = productos.crear_producto_manual(conn, 1, "Banana")
        conn.execute("UPDATE producto SET nombre_en = 'Banana' WHERE id = ?", (p2,))
        visibles = productos.productos_visibles(conn, 1)
        self.assertEqual([p["nombre_mostrado"] for p in visibles], ["Banana", "Carrot"])


class BuscarTest(unittest.TestCase):
    def test_is_accent_and_case_insensitive_across_nombre_es_en(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Yogur griego natural")
        conn.execute("UPDATE producto SET nombre_en = 'Plain Greek yogurt' WHERE id = ?", (p,))
        self.assertEqual(len(productos.buscar(conn, 1, "YOGUR")), 1)
        self.assertEqual(len(productos.buscar(conn, 1, "griego")), 1)
        self.assertEqual(len(productos.buscar(conn, 1, "GREEK")), 1)
        self.assertEqual(productos.buscar(conn, 1, "inexistente"), [])

    def test_matches_an_alias(self):
        conn = _conn(self)
        import json
        p = productos.crear_producto_manual(conn, 1, "Churrasco")
        conn.execute("UPDATE producto SET alias = ? WHERE id = ?", (json.dumps(["Districarnes"]), p))
        self.assertEqual(len(productos.buscar(conn, 1, "districarnes")), 1)

    def test_empty_text_finds_nothing(self):
        conn = _conn(self)
        productos.crear_producto_manual(conn, 1, "Algo")
        self.assertEqual(productos.buscar(conn, 1, ""), [])


class BasisDeProductoTest(unittest.TestCase):
    def test_returns_the_unit_for_the_products_own_basis(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, p, "protein_g", 31.0, "100g", "etiqueta")
        self.assertEqual(productos.basis_de_producto(conn, p), "g")

    def test_none_when_the_product_has_no_nutrition_yet(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Sin nutricion")
        self.assertIsNone(productos.basis_de_producto(conn, p))


class CalcularLineasTest(unittest.TestCase):
    def test_sums_one_products_nutrients_scaled_by_cantidad_over_100(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, p, "protein_g", 31.0, "100g", "etiqueta")
        productos.establecer_nutricion(conn, p, "kcal", 165.0, "100g", "etiqueta")
        cantidades, detalle = productos.calcular_lineas(
            conn, 1, [{"producto_id": p, "cantidad": 200.0, "unidad": "g"}])
        por_clave = {c["metric_key"]: c for c in cantidades}
        self.assertEqual(por_clave["protein_g"]["amount"], 62.0)
        self.assertEqual(por_clave["kcal"]["amount"], 330.0)
        self.assertEqual(detalle, [{"producto_id": p, "cantidad": 200.0, "unidad": "g", "nombre": "Pollo"}])

    def test_unidad_basis_scales_by_cantidad_directly_not_over_100(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Barra de proteina")
        productos.establecer_nutricion(conn, p, "protein_g", 20.0, "unidad", "etiqueta")
        cantidades, _ = productos.calcular_lineas(conn, 1, [{"producto_id": p, "cantidad": 3.0, "unidad": "unidad"}])
        self.assertEqual(cantidades[0]["amount"], 60.0)

    def test_sums_across_two_products_naming_the_same_nutrient(self):
        conn = _conn(self)
        p1 = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, p1, "protein_g", 31.0, "100g", "etiqueta")
        p2 = productos.crear_producto_manual(conn, 1, "Yogur")
        productos.establecer_nutricion(conn, p2, "protein_g", 10.0, "100g", "etiqueta")
        cantidades, _ = productos.calcular_lineas(
            conn, 1, [{"producto_id": p1, "cantidad": 100.0, "unidad": "g"},
                      {"producto_id": p2, "cantidad": 100.0, "unidad": "g"}])
        self.assertEqual(cantidades[0]["amount"], 41.0)

    def test_estimado_is_false_only_when_every_contributing_figure_is_etiqueta(self):
        conn = _conn(self)
        p1 = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, p1, "protein_g", 31.0, "100g", "etiqueta")
        p2 = productos.crear_producto_manual(conn, 1, "Yogur")
        productos.establecer_nutricion(conn, p2, "protein_g", 10.0, "100g", "estimado")
        cantidades, _ = productos.calcular_lineas(
            conn, 1, [{"producto_id": p1, "cantidad": 100.0, "unidad": "g"},
                      {"producto_id": p2, "cantidad": 100.0, "unidad": "g"}])
        self.assertTrue(cantidades[0]["estimado"])

    def test_a_product_with_no_nutrition_for_a_key_contributes_nothing_to_it(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Agua")
        cantidades, detalle = productos.calcular_lineas(
            conn, 1, [{"producto_id": p, "cantidad": 250.0, "unidad": "g"}])
        self.assertEqual(cantidades, [])
        self.assertEqual(len(detalle), 1)

    def test_unseen_product_raises_producto_desconocido_naming_the_id(self):
        conn = _conn(self)
        with self.assertRaises(productos.ProductoDesconocido) as ctx:
            productos.calcular_lineas(conn, 1, [{"producto_id": 999, "cantidad": 100.0, "unidad": "g"}])
        self.assertEqual(ctx.exception.producto_id, 999)

    def test_unit_mismatch_raises_unidad_no_coincide_naming_the_product(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, p, "protein_g", 31.0, "100g", "etiqueta")
        with self.assertRaises(productos.UnidadNoCoincide) as ctx:
            productos.calcular_lineas(conn, 1, [{"producto_id": p, "cantidad": 1.0, "unidad": "ml"}])
        self.assertEqual(ctx.exception.nombre, "Pollo")
        self.assertEqual(ctx.exception.esperada, "g")
        self.assertEqual(ctx.exception.recibida, "ml")

    def test_non_positive_cantidad_is_refused(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Pollo")
        with self.assertRaises(ValueError):
            productos.calcular_lineas(conn, 1, [{"producto_id": p, "cantidad": 0, "unidad": "g"}])

    def test_unknown_unidad_is_refused(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Pollo")
        with self.assertRaises(ValueError):
            productos.calcular_lineas(conn, 1, [{"producto_id": p, "cantidad": 1.0, "unidad": "litros"}])


# ---- borrar_producto_manual/lineas_de_comida (BODY-COMIDA-PRODUCTO-1-B ruling B) ----------------

class BorrarProductoManualTest(unittest.TestCase):
    def test_deletes_the_product_and_its_own_nutrition(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, p, "protein_g", 31.0, "100g", "etiqueta")
        productos.borrar_producto_manual(conn, 1, p)
        self.assertIsNone(conn.execute("SELECT 1 FROM producto WHERE id = ?", (p,)).fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM producto_nutricion WHERE producto_id = ?", (p,)).fetchone())

    def test_another_persons_product_raises_producto_desconocido(self):
        conn = _conn(self)
        _otra_persona(conn)
        p = productos.crear_producto_manual(conn, 2, "Secreto de Otra")
        with self.assertRaises(productos.ProductoDesconocido):
            productos.borrar_producto_manual(conn, 1, p)

    def test_unknown_product_raises_producto_desconocido(self):
        conn = _conn(self)
        with self.assertRaises(productos.ProductoDesconocido):
            productos.borrar_producto_manual(conn, 1, 999)

    def test_a_product_named_in_a_meal_raises_producto_en_uso_naming_it(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Pechuga de pollo")
        productos.establecer_nutricion(conn, p, "protein_g", 31.0, "100g", "etiqueta")
        cantidades, detalle = productos.calcular_lineas(conn, 1, [{"producto_id": p, "cantidad": 200.0,
                                                                     "unidad": "g"}])
        db.crear_comida(conn, 1, "2026-11-20", cantidades, "pollo", productos=detalle)
        with self.assertRaises(productos.ProductoEnUso) as ctx:
            productos.borrar_producto_manual(conn, 1, p)
        self.assertEqual(ctx.exception.nombre, "Pechuga de pollo")
        self.assertIsNotNone(conn.execute("SELECT 1 FROM producto WHERE id = ?", (p,)).fetchone())


class LineasDeComidaTest(unittest.TestCase):
    def test_a_meal_with_no_products_returns_an_empty_list(self):
        conn = _conn(self)
        cid = db.crear_comida(conn, 1, "2026-11-20", [{"metric_key": "protein_g", "amount": 10.0}], "agua")
        self.assertEqual(productos.lineas_de_comida(conn, cid), [])

    def test_a_meal_with_products_returns_its_own_lines(self):
        conn = _conn(self)
        p = productos.crear_producto_manual(conn, 1, "Pollo")
        productos.establecer_nutricion(conn, p, "protein_g", 31.0, "100g", "etiqueta")
        cantidades, detalle = productos.calcular_lineas(conn, 1, [{"producto_id": p, "cantidad": 200.0,
                                                                     "unidad": "g"}])
        cid = db.crear_comida(conn, 1, "2026-11-20", cantidades, "pollo", productos=detalle)
        self.assertEqual(productos.lineas_de_comida(conn, cid),
                          [{"producto_id": p, "cantidad": 200.0, "unidad": "g"}])


if __name__ == "__main__":
    unittest.main()
