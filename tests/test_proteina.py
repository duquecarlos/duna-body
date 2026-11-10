"""BODY-PROTEINA-1: a generic intake entry table (`ingesta`), one metric_def row marked
`agregado = 'suma_ingesta'` (protein_g), and the propose/apply/undo tools that log it from the
chat -- registrar_medida refuses an intake key, its one writer is db.registrar_ingesta/
corregir_ingesta/borrar_ingesta. Invented demo data only, no real health figures.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, privacidad
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

DAY = "2026-11-05"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


# ---- schema ---------------------------------------------------------------------------------------

_OLD_METRIC_DEF_SQL_SIN_AGREGADO = """
CREATE TABLE metric_def (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    unit    TEXT NOT NULL,
    better  TEXT NOT NULL CHECK (better IN ('higher', 'lower', 'neutral')),
    kind    TEXT NOT NULL DEFAULT 'measured' CHECK (kind IN ('measured', 'estimated')),
    origen  TEXT NOT NULL DEFAULT 'garmin' CHECK (origen IN ('garmin', 'self'))
);
"""


class FreshDbTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_ingesta_table_exists_with_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(ingesta)")}
        # comida_id (BODY-COMIDA-1 ruling 1): nullable, added beside the original contract columns.
        expected = {"id", "person_id", "day", "ts_local", "metric_key", "amount", "estimado",
                    "descripcion", "comida_id", "created_at", "updated_at"}
        self.assertEqual(cols, expected)

    def test_metric_def_has_agregado_column(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(metric_def)")}
        self.assertIn("agregado", cols)

    def test_protein_g_is_seeded_as_an_intake_metric(self):
        row = self.conn.execute(
            "SELECT origen, agregado, unit, better FROM metric_def WHERE key = 'protein_g'"
        ).fetchone()
        self.assertEqual((row["origen"], row["agregado"], row["unit"], row["better"]),
                         ("self", "suma_ingesta", "g", "neutral"))

    def test_amount_must_be_positive(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO ingesta (person_id, day, metric_key, amount, estimado, created_at, updated_at) "
                "VALUES (1, ?, 'protein_g', 0, 0, '2026-11-09T00:00:00+00:00', '2026-11-09T00:00:00+00:00')",
                (DAY,),
            )

    def test_propuesta_agente_accion_accepts_the_three_ingesta_actions(self):
        cid = _cid(self.conn)
        for accion in ("registrar_ingesta", "corregir_ingesta", "borrar_ingesta"):
            self.conn.execute(
                "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
                "creada_en, expira_en) VALUES (1, ?, ?, '{}', '2026-11-09T00:00:00+00:00', "
                "'2026-11-09T00:10:00+00:00')",
                (cid, accion),
            )
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE accion IN "
            "('registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta')"
        ).fetchone()[0]
        self.assertEqual(count, 3)


class OldDbMigrationTest(unittest.TestCase):
    """A database built before metric_def had `agregado` gains the column and the protein_g row,
    without disturbing an existing self-measured row."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE metric_def")
        self.conn.execute("DROP TABLE ingesta")
        self.conn.execute(_OLD_METRIC_DEF_SQL_SIN_AGREGADO)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "INSERT INTO metric_def (key, label, unit, better, kind, origen) VALUES "
            "('flex_alcance_cm', 'Flexibility reach', 'cm', 'lower', 'measured', 'self')"
        )
        self.conn.commit()

    def test_migration_adds_agregado_and_the_protein_row_keeping_the_old_one(self):
        db.init(self.conn)
        old_row = self.conn.execute(
            "SELECT agregado FROM metric_def WHERE key = 'flex_alcance_cm'"
        ).fetchone()
        self.assertIsNone(old_row["agregado"])
        new_row = self.conn.execute(
            "SELECT origen, agregado FROM metric_def WHERE key = 'protein_g'"
        ).fetchone()
        self.assertEqual((new_row["origen"], new_row["agregado"]), ("self", "suma_ingesta"))
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(ingesta)")}
        self.assertIn("amount", cols)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute("SELECT agregado FROM metric_def WHERE key = 'protein_g'").fetchone()
        self.assertEqual(row["agregado"], "suma_ingesta")


# ---- one writer of the day total ------------------------------------------------------------------

class DayTotalTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_two_entries_sum_into_one_day_total(self):
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 15.0, True, "huevos")
        row = self.conn.execute(
            "SELECT value, source FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'",
            (DAY,),
        ).fetchone()
        self.assertEqual((row["value"], row["source"]), (35.0, "self"))

    def test_correcting_one_entry_re_derives_the_total(self):
        iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 15.0, False)
        db.corregir_ingesta(self.conn, 1, iid, 30.0)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'",
            (DAY,),
        ).fetchone()
        self.assertEqual(row["value"], 45.0)

    def test_deleting_the_only_entry_removes_the_day_row(self):
        iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        db.borrar_ingesta(self.conn, 1, iid)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'",
            (DAY,),
        ).fetchone()
        self.assertIsNone(row)

    def test_deleting_one_of_two_entries_re_derives_not_removes(self):
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        iid2 = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 15.0, False)
        db.borrar_ingesta(self.conn, 1, iid2)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'",
            (DAY,),
        ).fetchone()
        self.assertEqual(row["value"], 20.0)

    def test_another_persons_entry_is_refused_by_corregir(self):
        iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        with self.assertRaises(ValueError):
            db.corregir_ingesta(self.conn, 2, iid, 30.0)

    def test_another_persons_entry_is_refused_by_borrar(self):
        iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        with self.assertRaises(ValueError):
            db.borrar_ingesta(self.conn, 2, iid)


# ---- registrar_medida refuses an intake key --------------------------------------------------------

class RegistrarMedidaRefusesIntakeTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_tool_refuses_protein_g(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                  {"metric_key": "protein_g", "value": 30.0, "day": DAY},
                                  "ninguno", self.cid)

    def test_function_itself_refuses_protein_g(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas._registrar_medida(self.conn, 1, self.cid, "protein_g", 30.0, day=DAY)

    def test_protein_g_is_not_in_registrar_medidas_own_enum(self):
        self.assertNotIn("protein_g", herramientas._PARAM_REGISTRAR_MEDIDA["properties"]["metric_key"]["enum"])


# ---- registrar_ingesta: propose -> apply -> undo ---------------------------------------------------

class RegistrarIngestaProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_propose_writes_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0]
        out = herramientas.ejecutar(
            self.conn, 1, "registrar_ingesta",
            {"metric_key": "protein_g", "amount": 35.0, "estimado": True, "descripcion": "2 huevos",
             "day": DAY, "hora": "13:00"},
            "ninguno", self.cid,
        )
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0], before)

    def test_apply_writes_the_entry_and_the_day_total(self):
        out = herramientas.ejecutar(
            self.conn, 1, "registrar_ingesta",
            {"metric_key": "protein_g", "amount": 35.0, "estimado": True, "day": DAY}, "ninguno", self.cid,
        )
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT amount, estimado FROM ingesta WHERE person_id = 1 AND day = ?", (DAY,)
        ).fetchone()
        self.assertEqual((row["amount"], bool(row["estimado"])), (35.0, True))
        total = self.conn.execute(
            "SELECT value, source FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'",
            (DAY,),
        ).fetchone()
        self.assertEqual((total["value"], total["source"]), (35.0, "self"))

    def test_undo_removes_the_entry_and_the_day_total(self):
        out = herramientas.ejecutar(
            self.conn, 1, "registrar_ingesta",
            {"metric_key": "protein_g", "amount": 35.0, "estimado": False, "day": DAY}, "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0], 0)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'", (DAY,)
        ).fetchone()
        self.assertIsNone(row)

    def test_unknown_intake_key_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "registrar_ingesta",
                {"metric_key": "resting_hr", "amount": 10.0, "estimado": False, "day": DAY}, "ninguno", self.cid,
            )

    def test_non_positive_amount_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "registrar_ingesta",
                {"metric_key": "protein_g", "amount": 0.0, "estimado": False, "day": DAY}, "ninguno", self.cid,
            )


# ---- corregir_ingesta: propose -> apply -> undo -----------------------------------------------------

class CorregirIngestaProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        self.conn.commit()

    def test_apply_updates_the_amount_and_the_day_total(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_ingesta", {"ingesta_id": self.iid, "amount": 40.0},
                                    "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT amount FROM ingesta WHERE id = ?", (self.iid,)).fetchone()
        self.assertEqual(row["amount"], 40.0)
        total = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'", (DAY,)
        ).fetchone()
        self.assertEqual(total["value"], 40.0)

    def test_undo_restores_the_prior_amount(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_ingesta", {"ingesta_id": self.iid, "amount": 40.0},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute("SELECT amount FROM ingesta WHERE id = ?", (self.iid,)).fetchone()
        self.assertEqual(row["amount"], 20.0)

    def test_unknown_entry_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_ingesta", {"ingesta_id": 999999, "amount": 40.0},
                                  "ninguno", self.cid)

    def test_another_persons_entry_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 2, "corregir_ingesta", {"ingesta_id": self.iid, "amount": 40.0},
                                  "ninguno", _cid(self.conn, 2))


# ---- borrar_ingesta: propose -> apply -> undo -------------------------------------------------------

class BorrarIngestaProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, True, "pollo")
        self.conn.commit()

    def test_apply_deletes_the_entry_and_re_derives_the_total(self):
        out = herramientas.ejecutar(self.conn, 1, "borrar_ingesta", {"ingesta_id": self.iid}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0], 0)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'", (DAY,)
        ).fetchone()
        self.assertIsNone(row)

    def test_undo_restores_the_entry_and_the_total(self):
        out = herramientas.ejecutar(self.conn, 1, "borrar_ingesta", {"ingesta_id": self.iid}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT amount, estimado, descripcion FROM ingesta WHERE person_id = 1 AND day = ?", (DAY,)
        ).fetchone()
        self.assertEqual((row["amount"], bool(row["estimado"]), row["descripcion"]), (20.0, True, "pollo"))
        total = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'", (DAY,)
        ).fetchone()
        self.assertEqual(total["value"], 20.0)

    def test_unknown_entry_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "borrar_ingesta", {"ingesta_id": 999999}, "ninguno", self.cid)

    def test_another_persons_entry_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 2, "borrar_ingesta", {"ingesta_id": self.iid}, "ninguno",
                                  _cid(self.conn, 2))


# ---- leer_dia carries the entries and the total -----------------------------------------------------

class LeerDiaIncludesIngestaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False, "arroz con pollo")
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 15.0, True, "batido")
        self.conn.commit()

    def test_leer_dia_shows_entries_and_total(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(len(out["ingesta"]), 2)
        total = next(m["value"] for m in out["metrics"] if m["key"] == "protein_g")
        self.assertEqual(total, 35.0)


# ---- card text (es/en), estimate vs exact -------------------------------------------------------

class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, **params):
        out = herramientas.ejecutar(self.conn, 1, "registrar_ingesta", params, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_estimate_card_es(self):
        t = self._tarjeta("es", metric_key="protein_g", amount=35.0, estimado=True,
                          descripcion="2 huevos y una pechuga", day=DAY, hora="13:00")
        self.assertIn("Proteína", t["despues"])
        self.assertIn("unos", t["despues"])
        self.assertIn("estimado", t["despues"])
        self.assertIn("almuerzo", t["despues"])
        self.assertIn("«2 huevos y una pechuga»", t["despues"])

    def test_estimate_card_en(self):
        t = self._tarjeta("en", metric_key="protein_g", amount=35.0, estimado=True,
                          descripcion="2 eggs and a chicken breast", day=DAY, hora="13:00")
        self.assertIn("Protein", t["despues"])
        self.assertIn("about", t["despues"])
        self.assertIn("estimate", t["despues"])
        self.assertIn("lunch", t["despues"])
        self.assertIn("“2 eggs and a chicken breast”", t["despues"])

    def test_exact_card_drops_the_estimate_wording(self):
        t = self._tarjeta("es", metric_key="protein_g", amount=35.0, estimado=False, day=DAY)
        self.assertNotIn("unos", t["despues"])
        self.assertNotIn("estimado", t["despues"])

    def test_card_with_no_hora_has_no_momento_segment(self):
        t = self._tarjeta("es", metric_key="protein_g", amount=35.0, estimado=False, day=DAY)
        for palabra in ("desayuno", "almuerzo", "merienda", "cena"):
            self.assertNotIn(palabra, t["despues"])


# ---- export / delete --------------------------------------------------------------------------------

class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, True, "desayuno invent")
        self.conn.commit()

    def test_ingesta_is_a_fully_handled_table(self):
        self.assertIn("ingesta", privacidad.FULLY_HANDLED_TABLES)

    def test_export_includes_the_entry(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["ingesta"]), 1)
        row = data["ingesta"][0]
        self.assertEqual(row["metric_key"], "protein_g")
        self.assertEqual(row["amount"], 20.0)
        self.assertEqual(row["descripcion"], "desayuno invent")

    def test_export_of_another_person_sees_nothing(self):
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["ingesta"], [])

    def test_delete_all_removes_the_entries(self):
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingesta").fetchone()[0], 0)


# ---- descripcion never reaches a log, print or error message ---------------------------------------

class DescripcionNeverLeaksTest(unittest.TestCase):
    """The person's own words (`descripcion`) are kept on the entry, their data, on their PC --
    never printed, logged, or echoed back inside a ToolError/ValueError message."""

    SECRETO = "ZZDESCRIPCION-SECRETA-NO-DEBE-SALIR"

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, True, self.SECRETO)
        self.conn.commit()

    def test_cross_person_tool_refusals_never_mention_the_description(self):
        for nombre, params in (
            ("corregir_ingesta", {"ingesta_id": self.iid, "amount": 5.0}),
            ("borrar_ingesta", {"ingesta_id": self.iid}),
        ):
            with self.assertRaises(herramientas.ToolError) as ctx:
                herramientas.ejecutar(self.conn, 2, nombre, params, "ninguno", _cid(self.conn, 2))
            self.assertNotIn(self.SECRETO, str(ctx.exception))

    def test_cross_person_db_refusals_never_mention_the_description(self):
        with self.assertRaises(ValueError) as ctx1:
            db.corregir_ingesta(self.conn, 2, self.iid, 5.0)
        self.assertNotIn(self.SECRETO, str(ctx1.exception))
        with self.assertRaises(ValueError) as ctx2:
            db.borrar_ingesta(self.conn, 2, self.iid)
        self.assertNotIn(self.SECRETO, str(ctx2.exception))

    def test_unknown_metric_refusal_never_mentions_the_description(self):
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(
                self.conn, 1, "registrar_ingesta",
                {"metric_key": "resting_hr", "amount": 10.0, "estimado": True, "descripcion": self.SECRETO,
                 "day": DAY},
                "ninguno", self.cid,
            )
        self.assertNotIn(self.SECRETO, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
