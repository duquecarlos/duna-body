"""BODY-COMIDA-1: one spoken meal = one call. A `comida` parent row groups several per-nutrient
`ingesta` children (protein_g plus six more catalogue keys), one tool (registrar_comida) proposes
the whole meal, one card renders it, apply/undo write and remove the comida row and all of its
children together. Invented demo data only, no real health figures.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from fastapi.testclient import TestClient

from body import config, db, identity, privacidad, productos as productos_mod, ventana_comida, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

DAY = "2026-11-05"
client = TestClient(web.app, follow_redirects=False)


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


_CANTIDADES_COMPLETAS = [
    {"metric_key": "protein_g", "amount": 35.0, "estimado": True},
    {"metric_key": "kcal", "amount": 650.0, "estimado": True},
    {"metric_key": "carbs_g", "amount": 70.0, "estimado": True},
    {"metric_key": "fat_g", "amount": 20.0, "estimado": True},
    {"metric_key": "fiber_g", "amount": 8.0, "estimado": False},
]


# ---- schema -----------------------------------------------------------------------------------

class FreshDbTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_comida_table_exists_with_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(comida)")}
        expected = {"id", "person_id", "day", "ts_local", "descripcion", "created_at", "updated_at", "momento"}
        self.assertEqual(cols, expected)

    def test_ingesta_gained_a_nullable_comida_id(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(ingesta)")}
        self.assertIn("comida_id", cols)
        iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        row = self.conn.execute("SELECT comida_id FROM ingesta WHERE id = ?", (iid,)).fetchone()
        self.assertIsNone(row["comida_id"])

    def test_the_six_new_catalogue_rows_are_seeded_as_intake_metrics(self):
        for key in ("kcal", "carbs_g", "fat_g", "fiber_g", "water_ml", "caffeine_mg", "alcohol_g"):
            with self.subTest(key=key):
                row = self.conn.execute(
                    "SELECT origen, agregado FROM metric_def WHERE key = ?", (key,)
                ).fetchone()
                self.assertIsNotNone(row, f"{key} missing from metric_def")
                self.assertEqual((row["origen"], row["agregado"]), ("self", "suma_ingesta"))

    def test_ingesta_metric_keys_includes_all_seven(self):
        self.assertEqual(set(db.INGESTA_METRIC_KEYS),
                          {"protein_g", "kcal", "carbs_g", "fat_g", "fiber_g", "water_ml",
                           "caffeine_mg", "alcohol_g"})

    def test_propuesta_agente_accion_accepts_registrar_comida(self):
        cid = _cid(self.conn)
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'registrar_comida', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (cid,),
        )
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE accion = 'registrar_comida'"
        ).fetchone()[0]
        self.assertEqual(count, 1)


class OldDbMigrationTest(unittest.TestCase):
    """A database built before `comida` existed and before `ingesta` had `comida_id` gains both,
    without disturbing an existing legacy ingesta row (never backfilled -- ruling 1)."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE ingesta")
        self.conn.execute("DROP TABLE comida")
        self.conn.execute(
            "CREATE TABLE ingesta ("
            "id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, day TEXT NOT NULL, ts_local TEXT, "
            "metric_key TEXT NOT NULL, amount REAL NOT NULL CHECK (amount > 0), "
            "estimado INTEGER NOT NULL DEFAULT 0, descripcion TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "INSERT INTO ingesta (person_id, day, metric_key, amount, estimado, created_at, updated_at) "
            "VALUES (1, ?, 'protein_g', 20.0, 0, '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00')",
            (DAY,),
        )
        self.conn.commit()

    def test_migration_adds_comida_and_comida_id_keeping_the_old_row(self):
        db.init(self.conn)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("comida", tables)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(ingesta)")}
        self.assertIn("comida_id", cols)
        row = self.conn.execute(
            "SELECT amount, comida_id FROM ingesta WHERE person_id = 1 AND day = ?", (DAY,)
        ).fetchone()
        self.assertEqual(row["amount"], 20.0)
        self.assertIsNone(row["comida_id"])

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT comida_id FROM ingesta WHERE person_id = 1 AND day = ?", (DAY,)
        ).fetchone()
        self.assertIsNone(row["comida_id"])


# ---- db.crear_comida / borrar_comida: one transaction, together -------------------------------

class CrearComidaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_creates_one_comida_and_n_ingesta_rows(self):
        cid = db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS, "pollo con arroz", "13:00")
        rows = self.conn.execute("SELECT COUNT(*) FROM ingesta WHERE comida_id = ?", (cid,)).fetchone()[0]
        self.assertEqual(rows, len(_CANTIDADES_COMPLETAS))
        comida = self.conn.execute("SELECT descripcion, day FROM comida WHERE id = ?", (cid,)).fetchone()
        self.assertEqual((comida["descripcion"], comida["day"]), ("pollo con arroz", DAY))

    def test_each_nutrient_sums_into_its_own_day_total(self):
        db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS)
        for c in _CANTIDADES_COMPLETAS:
            with self.subTest(key=c["metric_key"]):
                row = self.conn.execute(
                    "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = ?",
                    (DAY, c["metric_key"]),
                ).fetchone()
                self.assertEqual(row["value"], c["amount"])

    def test_borrar_comida_removes_the_meal_and_its_children_together(self):
        cid = db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS)
        db.borrar_comida(self.conn, 1, cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 0)
        for c in _CANTIDADES_COMPLETAS:
            row = self.conn.execute(
                "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = ?",
                (DAY, c["metric_key"]),
            ).fetchone()
            self.assertIsNone(row)

    def test_another_persons_meal_is_refused_by_borrar(self):
        cid = db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS)
        with self.assertRaises(ValueError):
            db.borrar_comida(self.conn, 2, cid)


# ---- registrar_comida: propose -> apply -> undo ------------------------------------------------

class RegistrarComidaProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def _proponer(self, **extra):
        params = {"day": DAY, "hora": "13:00", "descripcion": "pollo con arroz y ensalada",
                  "cantidades": _CANTIDADES_COMPLETAS}
        params.update(extra)
        return herramientas.ejecutar(self.conn, 1, "registrar_comida", params, "ninguno", self.cid)

    def test_propose_writes_nothing(self):
        before_c = self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0]
        before_i = self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0]
        out = self._proponer()
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], before_c)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0], before_i)

    def test_apply_writes_one_comida_and_five_ingesta_rows(self):
        out = self._proponer()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 1)
        cid = self.conn.execute("SELECT id FROM comida").fetchone()[0]
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM ingesta WHERE comida_id = ?", (cid,)
        ).fetchone()[0]
        self.assertEqual(rows, 5)

    def test_undo_removes_the_meal_and_every_child_together(self):
        out = self._proponer()
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0], 0)

    def test_bad_number_is_refused_with_no_row_written(self):
        before_c = self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0]
        with self.assertRaises(herramientas.ToolError):
            self._proponer(cantidades=[{"metric_key": "protein_g", "amount": "mucho", "estimado": True}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], before_c)

    def test_zero_amount_is_refused_with_no_row_written(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(cantidades=[{"metric_key": "protein_g", "amount": 0, "estimado": True}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 0)

    def test_numeric_string_amount_is_coerced(self):
        out = self._proponer(cantidades=[{"metric_key": "protein_g", "amount": "35", "estimado": True}])
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT amount FROM ingesta").fetchone()
        self.assertEqual(row["amount"], 35.0)

    def test_unknown_metric_key_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(cantidades=[{"metric_key": "resting_hr", "amount": 10.0}])

    def test_duplicate_metric_key_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(cantidades=[
                {"metric_key": "protein_g", "amount": 10.0},
                {"metric_key": "protein_g", "amount": 20.0},
            ])

    def test_empty_cantidades_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(cantidades=[])

    def test_estimado_defaults_to_true(self):
        out = self._proponer(cantidades=[{"metric_key": "protein_g", "amount": 35.0}])
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT estimado FROM ingesta").fetchone()
        self.assertTrue(bool(row["estimado"]))


# ---- db.crear_comida/corregir_comida(productos=...): the picker's own direct writer door --------

class CrearComidaConProductosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.producto_id = productos_mod.crear_producto_manual(self.conn, 1, "Pechuga de pollo")
        productos_mod.establecer_nutricion(self.conn, self.producto_id, "protein_g", 31.0, "100g", "etiqueta")

    def test_crear_comida_writes_a_comida_producto_row(self):
        cid = db.crear_comida(
            self.conn, 1, DAY, [{"metric_key": "protein_g", "amount": 62.0, "estimado": False}],
            productos=[{"producto_id": self.producto_id, "cantidad": 200.0, "unidad": "g",
                        "nombre": "Pechuga de pollo"}])
        fila = self.conn.execute(
            "SELECT producto_id, cantidad, unidad FROM comida_producto WHERE comida_id = ?", (cid,)).fetchone()
        self.assertEqual((fila["producto_id"], fila["cantidad"], fila["unidad"]), (self.producto_id, 200.0, "g"))

    def test_comidas_del_dia_resolves_the_product_lines(self):
        db.crear_comida(
            self.conn, 1, DAY, [{"metric_key": "protein_g", "amount": 62.0, "estimado": False}],
            ts_local=f"{DAY}T13:00:00",
            productos=[{"producto_id": self.producto_id, "cantidad": 200.0, "unidad": "g",
                        "nombre": "Pechuga de pollo"}])
        grupos = db.comidas_del_dia(self.conn, 1, DAY)
        self.assertEqual(len(grupos), 1)
        self.assertEqual(grupos[0]["productos"],
                          [{"producto_id": self.producto_id, "nombre": "Pechuga de pollo",
                            "cantidad": 200.0, "unidad": "g"}])

    def test_a_meal_with_no_products_has_an_empty_productos_list(self):
        db.crear_comida(self.conn, 1, DAY, [{"metric_key": "protein_g", "amount": 10.0, "estimado": True}],
                         ts_local=f"{DAY}T08:00:00")
        grupos = db.comidas_del_dia(self.conn, 1, DAY)
        self.assertEqual(grupos[0]["productos"], [])

    def test_a_legacy_standalone_ingesta_entry_has_an_empty_productos_list(self):
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False, ts_local=f"{DAY}T08:00:00")
        grupos = db.comidas_del_dia(self.conn, 1, DAY)
        self.assertEqual(grupos[0]["productos"], [])

    def test_borrar_comida_removes_the_product_lines_too(self):
        cid = db.crear_comida(
            self.conn, 1, DAY, [{"metric_key": "protein_g", "amount": 62.0, "estimado": False}],
            productos=[{"producto_id": self.producto_id, "cantidad": 200.0, "unidad": "g",
                        "nombre": "Pechuga de pollo"}])
        db.borrar_comida(self.conn, 1, cid)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM comida_producto").fetchone()[0], 0)

    def test_corregir_comida_with_an_explicit_productos_kwarg_replaces_the_line_set(self):
        cid = db.crear_comida(
            self.conn, 1, DAY, [{"metric_key": "protein_g", "amount": 62.0, "estimado": False}],
            productos=[{"producto_id": self.producto_id, "cantidad": 200.0, "unidad": "g",
                        "nombre": "Pechuga de pollo"}])
        otro = productos_mod.crear_producto_manual(self.conn, 1, "Arroz blanco")
        productos_mod.establecer_nutricion(self.conn, otro, "carbs_g", 28.0, "100g", "etiqueta")
        db.corregir_comida(
            self.conn, 1, cid, cantidades=[{"metric_key": "carbs_g", "amount": 42.0, "estimado": False}],
            productos=[{"producto_id": otro, "cantidad": 150.0, "unidad": "g", "nombre": "Arroz blanco"}])
        filas = self.conn.execute(
            "SELECT producto_id FROM comida_producto WHERE comida_id = ?", (cid,)).fetchall()
        self.assertEqual([r["producto_id"] for r in filas], [otro])

    def test_corregir_comida_without_a_productos_kwarg_leaves_lines_untouched(self):
        cid = db.crear_comida(
            self.conn, 1, DAY, [{"metric_key": "protein_g", "amount": 62.0, "estimado": False}],
            productos=[{"producto_id": self.producto_id, "cantidad": 200.0, "unidad": "g",
                        "nombre": "Pechuga de pollo"}])
        db.corregir_comida(self.conn, 1, cid,
                            cantidades=[{"metric_key": "protein_g", "amount": 70.0, "estimado": False}])
        filas = self.conn.execute(
            "SELECT producto_id FROM comida_producto WHERE comida_id = ?", (cid,)).fetchall()
        self.assertEqual([r["producto_id"] for r in filas], [self.producto_id])

    def test_productos_kwarg_none_leaves_comida_producto_untouched_on_create(self):
        """BODY-COMIDA-PRODUCTO-1-B: the earlier smuggled-metadata channel is gone --
        `productos=None` (the default, no kwarg at all) means "nothing to write", never a channel
        read off `cantidades` itself."""
        cid = db.crear_comida(self.conn, 1, DAY, [{"metric_key": "protein_g", "amount": 62.0, "estimado": False}])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM comida_producto WHERE comida_id = ?", (cid,)).fetchone()[0], 0)


# ---- card: one block, fixed catalogue order, never raises --------------------------------------

class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, **extra):
        params = {"day": DAY, "hora": "13:00", "descripcion": "pollo con arroz",
                  "cantidades": _CANTIDADES_COMPLETAS}
        params.update(extra)
        out = herramientas.ejecutar(self.conn, 1, "registrar_comida", params, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_card_names_the_meal_time_and_every_amount_es(self):
        t = self._tarjeta("es")
        self.assertIn("pollo con arroz", t["despues"])
        self.assertIn("Proteína", t["despues"])
        self.assertIn("Calorías", t["despues"])
        self.assertIn("Carbohidratos", t["despues"])
        self.assertIn("Grasa", t["despues"])
        self.assertIn("Fibra", t["despues"])

    def test_card_names_the_meal_time_and_every_amount_en(self):
        t = self._tarjeta("en")
        self.assertIn("pollo con arroz", t["despues"])
        self.assertIn("Protein", t["despues"])
        self.assertIn("Calories", t["despues"])
        self.assertIn("Carbs", t["despues"])

    def test_estimated_amount_is_marked(self):
        t = self._tarjeta("es", cantidades=[{"metric_key": "protein_g", "amount": 35.0, "estimado": True}])
        self.assertIn("≈", t["despues"])

    def test_exact_amount_is_not_marked(self):
        t = self._tarjeta("es", cantidades=[{"metric_key": "protein_g", "amount": 35.0, "estimado": False}])
        self.assertNotIn("≈", t["despues"])

    def test_amounts_render_in_catalogue_order_not_input_order(self):
        t = self._tarjeta("es", cantidades=[
            {"metric_key": "fiber_g", "amount": 8.0, "estimado": False},
            {"metric_key": "protein_g", "amount": 35.0, "estimado": False},
        ])
        self.assertLess(t["despues"].index("Proteína"), t["despues"].index("Fibra"))

    def test_card_render_never_raises_on_a_malformed_despues(self):
        tarjeta = asistente._tarjeta_propuesta_segura(T("es"), self.defs, {
            "id": 999, "accion": "registrar_comida", "despues_json": {}, "antes_json": None,
            "estado": "propuesta", "expira_en": "2026-11-09T00:10:00+00:00", "aplicada_via": None,
        })
        self.assertIn("despues", tarjeta)


# ---- ventana_comida counts MEALS, not rows -----------------------------------------------------

class VentanaCuentaComidasTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_one_multi_nutrient_meal_counts_as_one_entry(self):
        db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS, ts_local=f"{DAY}T13:00:00")
        v = ventana_comida.ventana(self.conn, 1, DAY)
        self.assertEqual(v.entradas, 1)
        self.assertEqual(v.sin_hora, 0)

    def test_two_meals_count_as_two_not_rows(self):
        db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS, ts_local=f"{DAY}T08:00:00")
        db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS, ts_local=f"{DAY}T13:00:00")
        v = ventana_comida.ventana(self.conn, 1, DAY)
        self.assertEqual(v.entradas, 2)

    def test_legacy_single_row_entries_still_count_one_each(self):
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False, ts_local=f"{DAY}T08:00:00")
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 15.0, False, ts_local=f"{DAY}T13:00:00")
        v = ventana_comida.ventana(self.conn, 1, DAY)
        self.assertEqual(v.entradas, 2)

    def test_meal_with_no_hora_counts_toward_sin_hora_once(self):
        db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS)
        v = ventana_comida.ventana(self.conn, 1, DAY)
        self.assertEqual(v.entradas, 1)
        self.assertEqual(v.sin_hora, 1)


# ---- registrar_comida is a registered propuesta tool (body.mcp lists it generically, tested in
# tests/test_mcp.py's own ToolsListTest and its new RegistrarComidaTest) --------------------------

class ToolRegistryTest(unittest.TestCase):
    def test_registrar_comida_is_registered_as_a_propuesta_tool(self):
        nombres = {h["nombre"]: h for h in herramientas.disponibles("ninguno")}
        self.assertIn("registrar_comida", nombres)
        self.assertEqual(nombres["registrar_comida"]["tipo"], "propuesta")


# ---- /comida/productos picker: route level (BODY-COMIDA-PRODUCTO-1 ruling 4). No dedicated route
# test file is in this slice's own touches -- the picker lives in `body/rutas/comida.py`, which is
# not itself a touched test file -- so these route-level checks live here too, the same precedent
# tests/test_comida_compartida.py's own "ruling 5" section set for a route module not in touches.

def _reset_web_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "comida-web.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    conn.commit()
    web.app.dependency_overrides[identity.current_person_id] = lambda: 1
    return conn


class PickerRouteTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_web_db(self)
        self.pollo = productos_mod.crear_producto_manual(self.conn, 1, "Pechuga de pollo")
        productos_mod.establecer_nutricion(self.conn, self.pollo, "protein_g", 31.0, "100g", "etiqueta")
        self.conn.commit()

    def test_get_lists_visible_products(self):
        r = client.get("/comida/productos")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Pechuga de pollo", r.text)

    def test_search_filters_the_list(self):
        r = client.get("/comida/productos", params={"q": "pechuga"})
        self.assertIn("Pechuga de pollo", r.text)
        r2 = client.get("/comida/productos", params={"q": "zzz-no-existe"})
        self.assertNotIn("Pechuga de pollo", r2.text)

    def test_agregar_adds_an_encoded_draft_line_and_redirects_back(self):
        r = client.get("/comida/productos/agregar",
                        params={"producto_id": self.pollo, "cantidad": 200.0, "unidad": "g"})
        self.assertEqual(r.status_code, 303)
        self.assertIn(f"linea={self.pollo}%3A200.0%3Ag", r.headers["location"])

    def test_agregar_ignores_an_unseen_product(self):
        r = client.get("/comida/productos/agregar", params={"producto_id": 999, "cantidad": 1.0, "unidad": "g"})
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("linea=", r.headers["location"])

    def test_guardar_writes_a_meal_through_the_same_writer_as_the_assistant(self):
        r = client.post("/comida/productos/guardar",
                         data={"linea": f"{self.pollo}:200:g", "descripcion": "pollo a la plancha", "q": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        fila = self.conn.execute("SELECT descripcion FROM comida WHERE person_id = 1").fetchone()
        self.assertEqual(fila["descripcion"], "pollo a la plancha")
        producto_fila = self.conn.execute(
            "SELECT producto_id, cantidad, unidad FROM comida_producto WHERE comida_id = "
            "(SELECT id FROM comida WHERE person_id = 1)").fetchone()
        self.assertEqual((producto_fila["producto_id"], producto_fila["cantidad"], producto_fila["unidad"]),
                          (self.pollo, 200.0, "g"))

    def test_guardar_with_no_lines_shows_an_error_and_writes_nothing(self):
        r = client.post("/comida/productos/guardar", data={"descripcion": "nada"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 0)

    def test_guardar_with_a_unit_mismatch_shows_the_error_and_writes_nothing(self):
        r = client.post("/comida/productos/guardar", data={"linea": f"{self.pollo}:1:ml"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Pechuga de pollo", r.text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 0)

    def test_nuevo_creates_a_manual_product_with_label_nutrition(self):
        r = client.post("/comida/productos/nuevo", data={
            "nombre": "Arroz blanco", "marca": "Demo", "tamano_neto": "1000", "tamano_unidad": "g",
            "valor_carbs_g": "28", "valor_kcal": "130",
        })
        self.assertEqual(r.status_code, 303)
        fila = self.conn.execute("SELECT marca FROM producto WHERE nombre = 'Arroz blanco'").fetchone()
        self.assertIsNotNone(fila)
        self.assertEqual(fila["marca"], "Demo")
        nutri = {r2["metric_key"]: r2["amount_per"] for r2 in self.conn.execute(
            "SELECT metric_key, amount_per FROM producto_nutricion WHERE producto_id = "
            "(SELECT id FROM producto WHERE nombre = 'Arroz blanco')")}
        self.assertEqual(nutri, {"carbs_g": 28.0, "kcal": 130.0})

    def test_nuevo_without_a_name_shows_an_error_and_creates_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM producto").fetchone()[0]
        r = client.post("/comida/productos/nuevo", data={"nombre": "   "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM producto").fetchone()[0], before)

    def test_another_persons_product_never_appears_in_the_list(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        otro = productos_mod.crear_producto_manual(self.conn, 2, "Secreto de Otra")
        self.conn.commit()
        r = client.get("/comida/productos")
        self.assertNotIn("Secreto de Otra", r.text)
        r2 = client.get("/comida/productos/agregar", params={"producto_id": otro, "cantidad": 1.0, "unidad": "g"})
        self.assertNotIn("linea=", r2.headers["location"])


class PickerNavShiftsLinkTest(unittest.TestCase):
    """BODY-REVISION-AJUSTES-NAV-PICKER-1 finding 13: the picker never threaded `turnos` into its
    own render calls, so the nav's Shifts tab silently disappeared here even for a person who works
    shifts. Covers both the plain GET and the error-reload path (_render_picker)."""

    def setUp(self):
        self.conn = _reset_web_db(self)
        db.set_trabaja_turnos(self.conn, True, 1)
        self.conn.commit()

    def test_get_shows_the_shifts_tab(self):
        r = client.get("/comida/productos")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/shifts"', r.text)

    def test_error_reload_still_shows_the_shifts_tab(self):
        r = client.post("/comida/productos/guardar", data={"descripcion": "nada"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/shifts"', r.text)

    def test_absent_for_a_person_who_does_not_work_shifts(self):
        db.set_trabaja_turnos(self.conn, False, 1)
        self.conn.commit()
        r = client.get("/comida/productos")
        self.assertNotIn('href="/shifts"', r.text)


# ---- export / delete ------------------------------------------------------------------------------

class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid_comida = db.crear_comida(self.conn, 1, DAY, _CANTIDADES_COMPLETAS, "pollo", "13:00")
        self.conn.commit()

    def test_comida_is_a_fully_handled_table(self):
        self.assertIn("comida", privacidad.FULLY_HANDLED_TABLES)

    def test_export_includes_the_meal_and_its_amounts(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["comidas"]), 1)
        self.assertEqual(data["comidas"][0]["descripcion"], "pollo")
        ligados = [r for r in data["ingesta"] if r["comida_id"] == self.cid_comida]
        self.assertEqual(len(ligados), len(_CANTIDADES_COMPLETAS))

    def test_export_of_another_person_sees_nothing(self):
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["comidas"], [])

    def test_delete_all_removes_the_meal_and_its_children(self):
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
