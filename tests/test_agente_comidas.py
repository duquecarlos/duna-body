"""ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: a meal proposed through `registrar_comida` and applied
reads back through leer_dia's own `comidas` list with its description and amounts -- the full
propose/apply/read round trip, never just `db.crear_comida` called directly. Invented demo data
only, no real health figures.

ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the same round trip with a full ISO `hora` -- the
surface's own repro (a nested `<day>T<day>T<hh:mm:ss><offset>:00` on three applied meals) --
proven fixed end to end, not just at the parser (tests/test_comida_hora.py has that).
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, productos as productos_mod
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas

DAY = "2026-11-10"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _producto(conn, person_id: int = 1, *, nombre: str = "Pechuga de pollo",
              basis: str = "100g", fuente: str = "etiqueta",
              nutrientes: dict | None = None) -> int:
    producto_id = productos_mod.crear_producto_manual(conn, person_id, nombre)
    for metric_key, amount_per in (nutrientes or {"protein_g": 31.0, "kcal": 165.0}).items():
        productos_mod.establecer_nutricion(conn, producto_id, metric_key, amount_per, basis, fuente)
    return producto_id


def _otra_persona(conn, person_id: int = 2, nombre: str = "Otra") -> None:
    conn.execute("INSERT INTO person (id, display_name) VALUES (?, ?)", (person_id, nombre))


class RegistrarComidaReadsBackTest(unittest.TestCase):
    def test_a_meal_logged_through_the_applier_reads_back_in_leer_dia(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "2 huevos y una pechuga",
             "cantidades": [{"metric_key": "protein_g", "amount": 35.0, "estimado": True},
                             {"metric_key": "kcal", "amount": 450.0, "estimado": True}],
             "day": DAY, "hora": "13:00"},
            "ninguno", cid,
        )
        resultado = propuestas.aplicar(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")

        leido = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(len(leido["comidas"]), 1)
        comida = leido["comidas"][0]
        self.assertEqual(comida["descripcion"], "2 huevos y una pechuga")
        self.assertEqual(comida["ts_local"], f"{DAY}T13:00:00")
        cantidades = {c["metric_key"]: c["amount"] for c in comida["cantidades"]}
        self.assertEqual(cantidades, {"protein_g": 35.0, "kcal": 450.0})

    def test_a_full_iso_hora_reads_back_as_a_flat_ts_local_never_nested(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "2 huevos y una pechuga",
             "cantidades": [{"metric_key": "protein_g", "amount": 35.0, "estimado": True}],
             "day": DAY, "hora": f"{DAY}T13:00:00-05:00"},
            "ninguno", cid,
        )
        resultado = propuestas.aplicar(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")

        leido = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(leido["comidas"][0]["ts_local"], f"{DAY}T13:00:00")


class RegistrarComidaConProductosTest(unittest.TestCase):
    """BODY-COMIDA-PRODUCTO-1 ruling 3: `productos` sums into `cantidades` through the same
    propose/apply/read round trip."""

    def test_product_nutrition_sums_into_cantidades_and_estimado_is_false_on_a_label(self):
        conn = _conn(self)
        producto_id = _producto(conn)
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo a la plancha", "day": DAY,
             "productos": [{"producto_id": producto_id, "cantidad": 200.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        resultado = propuestas.aplicar(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")

        leido = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        comida = leido["comidas"][0]
        cantidades = {c["metric_key"]: (c["amount"], c["estimado"]) for c in comida["cantidades"]}
        self.assertEqual(cantidades["protein_g"], (62.0, False))
        self.assertEqual(cantidades["kcal"], (330.0, False))

    def test_free_text_and_product_amounts_add_up_for_the_same_nutrient(self):
        conn = _conn(self)
        producto_id = _producto(conn)
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo y algo mas", "day": DAY,
             "cantidades": [{"metric_key": "protein_g", "amount": 5.0, "estimado": True}],
             "productos": [{"producto_id": producto_id, "cantidad": 100.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        resultado = propuestas.aplicar(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        leido = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        cantidades = {c["metric_key"]: (c["amount"], c["estimado"]) for c in leido["comidas"][0]["cantidades"]}
        # 5.0 free-text (estimado) + 31.0 from the label -> still estimado, since the free-text part is.
        self.assertEqual(cantidades["protein_g"], (36.0, True))

    def test_meal_names_two_products_in_one_meal_line_set(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        arroz = _producto(conn, nombre="Arroz blanco", nutrientes={"carbs_g": 28.0, "kcal": 130.0})
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo con arroz", "day": DAY,
             "productos": [{"producto_id": pollo, "cantidad": 200.0, "unidad": "g"},
                            {"producto_id": arroz, "cantidad": 150.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out["propuesta_id"])
        filas = conn.execute(
            "SELECT producto_id, cantidad, unidad FROM comida_producto ORDER BY producto_id").fetchall()
        self.assertEqual(len(filas), 2)
        self.assertEqual({r["producto_id"] for r in filas}, {pollo, arroz})

    def test_unseen_product_is_refused_by_name(self):
        conn = _conn(self)
        cid = _cid(conn)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(
                conn, 1, "registrar_comida",
                {"descripcion": "algo", "day": DAY,
                 "productos": [{"producto_id": 999, "cantidad": 100.0, "unidad": "g"}]},
                "ninguno", cid,
            )
        self.assertIn("999", str(ctx.exception))

    def test_another_persons_manual_product_is_unseen(self):
        conn = _conn(self)
        _otra_persona(conn)
        producto_id = _producto(conn, person_id=2)
        cid = _cid(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                conn, 1, "registrar_comida",
                {"descripcion": "algo", "day": DAY,
                 "productos": [{"producto_id": producto_id, "cantidad": 100.0, "unidad": "g"}]},
                "ninguno", cid,
            )

    def test_unit_mismatch_is_refused_by_name(self):
        conn = _conn(self)
        producto_id = _producto(conn)  # basis 100g -> expects 'g'
        cid = _cid(conn)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(
                conn, 1, "registrar_comida",
                {"descripcion": "algo",
                 "productos": [{"producto_id": producto_id, "cantidad": 1.0, "unidad": "ml"}]},
                "ninguno", cid,
            )
        self.assertIn("Pechuga de pollo", str(ctx.exception))

    def test_productos_cannot_be_combined_with_compartida_con(self):
        conn = _conn(self)
        producto_id = _producto(conn)
        cid = _cid(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                conn, 1, "registrar_comida",
                {"descripcion": "algo",
                 "productos": [{"producto_id": producto_id, "cantidad": 100.0, "unidad": "g"}],
                 "compartida_con": [{"nombre": "Ana", "nutrientes": {"protein_g": 5.0}}]},
                "ninguno", cid,
            )


class CorregirComidaConProductosTest(unittest.TestCase):
    def test_correcting_productos_replaces_the_whole_line_set(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        arroz = _producto(conn, nombre="Arroz blanco", nutrientes={"carbs_g": 28.0, "kcal": 130.0})
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo",
             "productos": [{"producto_id": pollo, "cantidad": 200.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out["propuesta_id"])
        comida_id = conn.execute("SELECT id FROM comida WHERE person_id = 1").fetchone()["id"]

        out2 = herramientas.ejecutar(
            conn, 1, "corregir_comida",
            {"comida_id": comida_id,
             "cantidades": [{"metric_key": "carbs_g", "amount": 42.0, "estimado": False}],
             "productos": [{"producto_id": arroz, "cantidad": 150.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        resultado = propuestas.aplicar(conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        filas = conn.execute("SELECT producto_id FROM comida_producto WHERE comida_id = ?",
                              (comida_id,)).fetchall()
        self.assertEqual([r["producto_id"] for r in filas], [arroz])

    def test_correcting_cantidades_without_productos_leaves_product_lines_untouched(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo",
             "productos": [{"producto_id": pollo, "cantidad": 200.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out["propuesta_id"])
        comida_id = conn.execute("SELECT id FROM comida WHERE person_id = 1").fetchone()["id"]

        out2 = herramientas.ejecutar(
            conn, 1, "corregir_comida",
            {"comida_id": comida_id,
             "cantidades": [{"metric_key": "protein_g", "amount": 62.0, "estimado": False},
                             {"metric_key": "kcal", "amount": 330.0, "estimado": False}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out2["propuesta_id"])
        filas = conn.execute("SELECT producto_id FROM comida_producto WHERE comida_id = ?",
                              (comida_id,)).fetchall()
        self.assertEqual([r["producto_id"] for r in filas], [pollo])

    def test_productos_without_cantidades_is_refused(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo",
             "productos": [{"producto_id": pollo, "cantidad": 200.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out["propuesta_id"])
        comida_id = conn.execute("SELECT id FROM comida WHERE person_id = 1").fetchone()["id"]
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                conn, 1, "corregir_comida",
                {"comida_id": comida_id,
                 "productos": [{"producto_id": pollo, "cantidad": 300.0, "unidad": "g"}]},
                "ninguno", cid,
            )


class CorregirComidaProductosUndoDriftTest(unittest.TestCase):
    """BODY-COMIDA-PRODUCTO-1-B ruling A: `antes`'s own `productos` snapshot (never a hidden key
    tucked into `cantidades`) lets undo put the old lines back, and drift covers
    product lines as it covers cantidades."""

    def _registrar_y_aplicar(self, conn, cid, pollo) -> int:
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo",
             "productos": [{"producto_id": pollo, "cantidad": 200.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out["propuesta_id"])
        return conn.execute("SELECT id FROM comida WHERE person_id = 1").fetchone()["id"]

    def test_undo_restores_the_meals_prior_product_lines(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        arroz = _producto(conn, nombre="Arroz blanco", nutrientes={"carbs_g": 28.0, "kcal": 130.0})
        cid = _cid(conn)
        comida_id = self._registrar_y_aplicar(conn, cid, pollo)

        out2 = herramientas.ejecutar(
            conn, 1, "corregir_comida",
            {"comida_id": comida_id,
             "cantidades": [{"metric_key": "carbs_g", "amount": 42.0, "estimado": False}],
             "productos": [{"producto_id": arroz, "cantidad": 150.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        out2_aplicado = propuestas.aplicar(conn, 1, out2["propuesta_id"])
        self.assertEqual(out2_aplicado["estado"], "aplicada")

        resultado = propuestas.deshacer(conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        filas = conn.execute("SELECT producto_id, cantidad, unidad FROM comida_producto WHERE comida_id = ?",
                              (comida_id,)).fetchall()
        self.assertEqual([(r["producto_id"], r["cantidad"], r["unidad"]) for r in filas], [(pollo, 200.0, "g")])

    def test_undo_of_a_correction_that_never_touched_products_leaves_them_as_they_were(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        cid = _cid(conn)
        comida_id = self._registrar_y_aplicar(conn, cid, pollo)

        out2 = herramientas.ejecutar(
            conn, 1, "corregir_comida", {"comida_id": comida_id, "hora": "08:00"}, "ninguno", cid)
        propuestas.aplicar(conn, 1, out2["propuesta_id"])
        resultado = propuestas.deshacer(conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        filas = conn.execute("SELECT producto_id FROM comida_producto WHERE comida_id = ?",
                              (comida_id,)).fetchall()
        self.assertEqual([r["producto_id"] for r in filas], [pollo])

    def test_apply_refuses_when_only_the_product_lines_drifted_since_the_proposal(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        arroz = _producto(conn, nombre="Arroz blanco")
        cid = _cid(conn)
        comida_id = self._registrar_y_aplicar(conn, cid, pollo)

        out2 = herramientas.ejecutar(
            conn, 1, "corregir_comida",
            {"comida_id": comida_id,
             "cantidades": [{"metric_key": "protein_g", "amount": 70.0, "estimado": False}],
             "productos": [{"producto_id": arroz, "cantidad": 100.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        # Someone else changes ONLY the meal's product lines directly, the amounts left exactly as
        # the proposal's own `antes` recorded them -- isolating the new product-line drift check.
        db.corregir_comida(conn, 1, comida_id,
                           cantidades=[{"metric_key": "protein_g", "amount": 62.0, "estimado": False},
                                       {"metric_key": "kcal", "amount": 330.0, "estimado": False}],
                           productos=[{"producto_id": pollo, "cantidad": 50.0, "unidad": "g", "nombre": "Pollo"}])
        resultado = propuestas.aplicar(conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_undo_refuses_when_only_the_product_lines_drifted_since_apply(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        arroz = _producto(conn, nombre="Arroz blanco")
        cid = _cid(conn)
        comida_id = self._registrar_y_aplicar(conn, cid, pollo)

        out2 = herramientas.ejecutar(
            conn, 1, "corregir_comida",
            {"comida_id": comida_id,
             "cantidades": [{"metric_key": "protein_g", "amount": 70.0, "estimado": False}],
             "productos": [{"producto_id": arroz, "cantidad": 100.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out2["propuesta_id"])
        # Someone else changes ONLY the meal's product lines directly, the amounts left exactly as
        # this correction's own `despues` recorded them.
        db.corregir_comida(conn, 1, comida_id,
                           cantidades=[{"metric_key": "protein_g", "amount": 70.0, "estimado": False}],
                           productos=[{"producto_id": pollo, "cantidad": 50.0, "unidad": "g", "nombre": "Pollo"}])
        resultado = propuestas.deshacer(conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")


class RegistrarComidaProductosUndoTest(unittest.TestCase):
    def test_undo_removes_the_whole_meal_and_its_product_lines(self):
        conn = _conn(self)
        pollo = _producto(conn, nombre="Pechuga de pollo")
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo",
             "productos": [{"producto_id": pollo, "cantidad": 200.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM comida_producto").fetchone()[0], 0)


class CrearProductoToolTest(unittest.TestCase):
    """BODY-COMIDA-PRODUCTO-1-B ruling B: a PROPOSAL tool, insert-shaped like `registrar_comida`."""

    def test_propose_writes_nothing(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(conn, 1, "crear_producto", {"nombre": "Pechuga de pollo"}, "ninguno", cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM producto").fetchone()[0], 0)

    def test_empty_nombre_is_refused(self):
        conn = _conn(self)
        cid = _cid(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "crear_producto", {"nombre": "   "}, "ninguno", cid)

    def test_nutrientes_without_tamano_unidad_is_refused(self):
        conn = _conn(self)
        cid = _cid(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                conn, 1, "crear_producto",
                {"nombre": "Pechuga de pollo", "nutrientes": {"protein_g": 31.0}}, "ninguno", cid)

    def test_apply_creates_the_product_with_its_label_nutrition(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "crear_producto",
            {"nombre": "Pechuga de pollo", "marca": "Districarnes", "tamano_unidad": "g",
             "nutrientes": {"protein_g": 31.0, "kcal": 165.0}},
            "ninguno", cid,
        )
        resultado = propuestas.aplicar(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        fila = conn.execute("SELECT * FROM producto WHERE person_id = 1").fetchone()
        self.assertEqual(fila["nombre"], "Pechuga de pollo")
        self.assertEqual(fila["marca"], "Districarnes")
        nutricion = {r["metric_key"]: r["amount_per"] for r in conn.execute(
            "SELECT metric_key, amount_per FROM producto_nutricion WHERE producto_id = ?", (fila["id"],))}
        self.assertEqual(nutricion, {"protein_g": 31.0, "kcal": 165.0})

        buscado = herramientas.ejecutar(conn, 1, "buscar_productos", {"texto": "pechuga"}, "ninguno")
        self.assertEqual(len(buscado["productos"]), 1)

    def test_undo_removes_the_product(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(conn, 1, "crear_producto", {"nombre": "Pechuga de pollo"}, "ninguno", cid)
        propuestas.aplicar(conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM producto").fetchone()[0], 0)

    def test_undo_is_refused_once_a_meal_names_the_product(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "crear_producto",
            {"nombre": "Pechuga de pollo", "tamano_unidad": "g", "nutrientes": {"protein_g": 31.0}},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, out["propuesta_id"])
        producto_id = conn.execute("SELECT id FROM producto WHERE person_id = 1").fetchone()["id"]
        comida_out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "pollo otra vez",
             "productos": [{"producto_id": producto_id, "cantidad": 100.0, "unidad": "g"}]},
            "ninguno", cid,
        )
        propuestas.aplicar(conn, 1, comida_out["propuesta_id"])
        resultado = propuestas.deshacer(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        self.assertIsNotNone(conn.execute("SELECT 1 FROM producto WHERE id = ?", (producto_id,)).fetchone())


class BuscarProductosTest(unittest.TestCase):
    def test_finds_a_product_by_accent_and_case_insensitive_name(self):
        conn = _conn(self)
        _producto(conn, nombre="Yogur griego natural")
        out = herramientas.ejecutar(conn, 1, "buscar_productos", {"texto": "YOGUR GRIEGO"}, "ninguno")
        self.assertEqual(len(out["productos"]), 1)
        self.assertEqual(out["productos"][0]["nombre"], "Yogur griego natural")
        self.assertEqual(out["productos"][0]["unidad"], "g")

    def test_never_finds_another_persons_manual_product(self):
        conn = _conn(self)
        _otra_persona(conn)
        _producto(conn, person_id=2, nombre="Secreto de Ana")
        out = herramientas.ejecutar(conn, 1, "buscar_productos", {"texto": "secreto"}, "ninguno")
        self.assertEqual(out["productos"], [])

    def test_a_product_with_no_nutrition_yet_has_a_null_unidad(self):
        conn = _conn(self)
        productos_mod.crear_producto_manual(conn, 1, "Producto sin nutricion")
        out = herramientas.ejecutar(conn, 1, "buscar_productos", {"texto": "sin nutricion"}, "ninguno")
        self.assertEqual(len(out["productos"]), 1)
        self.assertIsNone(out["productos"][0]["unidad"])


if __name__ == "__main__":
    unittest.main()
