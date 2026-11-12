"""BODY-NUTRIENTE-OBJETIVO-1: a daily target for a `suma_ingesta` metric (`nutrient_target`),
generic over all eight -- a propose/apply/undo tool, the Today/Día line and Ajustes' own form.
Invented demo data only, no real health figures.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, nutriente_objetivo, privacidad, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "body" / "static"

DAY = "2026-11-05"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


# ---- schema ---------------------------------------------------------------------------------------

class FreshDbTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_nutrient_target_table_exists_with_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(nutrient_target)")}
        self.assertEqual(cols, {"person_id", "metric_key", "start_day", "kind", "amount", "sentido",
                                 "created_at"})

    def test_amount_must_be_positive_for_a_live_target(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO nutrient_target (person_id, metric_key, start_day, kind, amount, sentido, "
                "created_at) VALUES (1, 'protein_g', ?, 'absolute', 0, 'al_menos', '2026-11-05T00:00:00+00:00')",
                (DAY,),
            )

    def test_off_requires_no_amount(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO nutrient_target (person_id, metric_key, start_day, kind, amount, sentido, "
                "created_at) VALUES (1, 'protein_g', ?, 'off', 50, 'al_menos', '2026-11-05T00:00:00+00:00')",
                (DAY,),
            )

    def test_propuesta_agente_accion_accepts_proponer_objetivo_nutriente(self):
        cid = _cid(self.conn)
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'proponer_objetivo_nutriente', '{}', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00')",
            (cid,),
        )
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE accion = 'proponer_objetivo_nutriente'"
        ).fetchone()[0]
        self.assertEqual(count, 1)


_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_NUTRIENTE = """
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito',
                         'proponer_experimento', 'marcar_experimento',
                         'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                         'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                         'registrar_comida', 'registrar_checkin', 'descartar_series',
                         'separar_sesion', 'unir_sesiones')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp'))
"""


class OldDbMigrationTest(unittest.TestCase):
    """A database built before `nutrient_target` existed, and before its own `accion` lacked
    `proponer_objetivo_nutriente`, gains the table and the widened CHECK, keeping existing rows."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        db.init(self.conn)
        self.conn.execute("DROP TABLE nutrient_target")
        self.cid = _cid(self.conn)
        self.iid = db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(f"CREATE TABLE propuesta_agente ({_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_NUTRIENTE})")
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'registrar_ingesta', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_recreates_nutrient_target_and_widens_accion_keeping_existing_rows(self):
        db.init(self.conn)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(nutrient_target)")}
        self.assertIn("sentido", cols)
        row = self.conn.execute("SELECT amount FROM ingesta WHERE person_id = 1 AND day = ?", (DAY,)).fetchone()
        self.assertEqual(row["amount"], 20.0)
        old_row = self.conn.execute(
            "SELECT accion FROM propuesta_agente WHERE conversacion_id = ?", (self.cid,)
        ).fetchone()
        self.assertEqual(old_row["accion"], "registrar_ingesta")
        count = self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'proponer_objetivo_nutriente', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (self.cid,),
        )
        self.conn.commit()
        self.assertEqual(count.rowcount, 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(nutrient_target)")}
        self.assertIn("kind", cols)


# ---- writer / history -------------------------------------------------------------------------------

class WriterHistoryTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_set_then_read_resolves_absolute(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 130.0, "al_menos")
        self.conn.commit()
        out = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-05")
        self.assertEqual((out["kind"], out["amount"], out["sentido"], out["objetivo"], out["falta_peso"]),
                         ("absolute", 130.0, "al_menos", 130.0, False))

    def test_change_on_day_10_day_9_reads_the_old_target_day_10_the_new(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 100.0, "al_menos")
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-10", "absolute", 150.0, "al_menos")
        self.conn.commit()
        dia9 = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-09")
        dia10 = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-10")
        self.assertEqual(dia9["objetivo"], 100.0)
        self.assertEqual(dia10["objetivo"], 150.0)

    def test_same_day_change_replaces_that_days_row(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 100.0, "al_menos")
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 120.0, "al_menos")
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM nutrient_target WHERE person_id = 1 AND metric_key = 'protein_g'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        out = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-05")
        self.assertEqual(out["objetivo"], 120.0)

    def test_off_ends_a_target(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 100.0, "al_menos")
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-10", "off", None, "al_menos")
        self.conn.commit()
        antes = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-09")
        despues = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-10")
        self.assertIsNotNone(antes)
        self.assertIsNone(despues)

    def test_borrar_removes_exactly_that_days_row(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 100.0, "al_menos")
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-10", "absolute", 150.0, "al_menos")
        self.conn.commit()
        db.borrar_nutrient_target(self.conn, 1, "protein_g", "2026-11-10")
        self.conn.commit()
        out = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-10")
        self.assertEqual(out["objetivo"], 100.0)  # falls back to the earlier row, history intact


class PerKgTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "per_kg", 1.6, "al_menos")
        self.conn.commit()

    def test_resolves_against_a_recent_weight(self):
        db.set_metric(self.conn, "2026-11-03", "body_weight_kg", 75.0, "self", 1)
        self.conn.commit()
        out = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-05")
        self.assertAlmostEqual(out["objetivo"], 120.0)
        self.assertFalse(out["falta_peso"])

    def test_weight_more_than_30_days_old_is_not_used(self):
        db.set_metric(self.conn, "2026-09-01", "body_weight_kg", 75.0, "self", 1)
        self.conn.commit()
        out = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-05")
        self.assertIsNone(out["objetivo"])
        self.assertTrue(out["falta_peso"])

    def test_no_weight_at_all_is_an_honest_gap(self):
        out = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "protein_g", "2026-11-05")
        self.assertIsNone(out["objetivo"])
        self.assertTrue(out["falta_peso"])


class RestanteTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_al_menos_restante_floors_at_zero(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 100.0, "al_menos")
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 130.0, False)
        self.conn.commit()
        out = nutriente_objetivo.estado_del_dia(self.conn, 1, DAY)
        self.assertEqual(out["protein_g"]["restante"], 0.0)

    def test_al_menos_restante_before_reaching(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 100.0, "al_menos")
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 30.0, False)
        self.conn.commit()
        out = nutriente_objetivo.estado_del_dia(self.conn, 1, DAY)
        self.assertEqual(out["protein_g"]["restante"], 70.0)
        self.assertEqual(out["protein_g"]["n"], 1)

    def test_como_maximo_restante_may_go_negative(self):
        db.set_nutrient_target(self.conn, 1, "caffeine_mg", "2026-11-01", "absolute", 400.0, "como_maximo")
        db.registrar_ingesta(self.conn, 1, DAY, "caffeine_mg", 500.0, False)
        self.conn.commit()
        out = nutriente_objetivo.estado_del_dia(self.conn, 1, DAY)
        self.assertEqual(out["caffeine_mg"]["restante"], -100.0)

    def test_zero_entries_shows_zero_not_nothing(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 100.0, "al_menos")
        self.conn.commit()
        out = nutriente_objetivo.estado_del_dia(self.conn, 1, DAY)
        self.assertEqual((out["protein_g"]["total"], out["protein_g"]["n"]), (0.0, 0))

    def test_a_day_with_no_target_has_no_entry(self):
        out = nutriente_objetivo.estado_del_dia(self.conn, 1, DAY)
        self.assertEqual(out, {})

    def test_off_metric_is_excluded_from_estado_del_dia(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-01", "absolute", 100.0, "al_menos")
        db.set_nutrient_target(self.conn, 1, "protein_g", "2026-11-04", "off", None, "al_menos")
        self.conn.commit()
        out = nutriente_objetivo.estado_del_dia(self.conn, 1, DAY)
        self.assertNotIn("protein_g", out)


# ---- a key that is not summed intake is refused by the writer at the SQL level --------------------

class NonIntakeKeyTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_objetivo_del_dia_on_a_non_intake_key_simply_finds_no_target(self):
        out = nutriente_objetivo.objetivo_del_dia(self.conn, 1, "resting_hr", DAY)
        self.assertIsNone(out)


# ---- proponer_objetivo_nutriente: propose -> apply -> undo -----------------------------------------

class ProponerObjetivoProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_propose_writes_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM nutrient_target").fetchone()[0]
        out = herramientas.ejecutar(
            self.conn, 1, "proponer_objetivo_nutriente",
            {"metric_key": "protein_g", "kind": "per_kg", "amount": 1.6, "sentido": "al_menos", "start_day": DAY},
            "ninguno", self.cid,
        )
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nutrient_target").fetchone()[0], before)

    def test_apply_writes_the_row(self):
        out = herramientas.ejecutar(
            self.conn, 1, "proponer_objetivo_nutriente",
            {"metric_key": "protein_g", "kind": "absolute", "amount": 130.0, "sentido": "al_menos",
             "start_day": DAY},
            "ninguno", self.cid,
        )
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT kind, amount, sentido FROM nutrient_target WHERE person_id = 1 AND metric_key = 'protein_g' "
            "AND start_day = ?", (DAY,),
        ).fetchone()
        self.assertEqual((row["kind"], row["amount"], row["sentido"]), ("absolute", 130.0, "al_menos"))

    def test_undo_with_no_prior_target_removes_the_row(self):
        out = herramientas.ejecutar(
            self.conn, 1, "proponer_objetivo_nutriente",
            {"metric_key": "protein_g", "kind": "absolute", "amount": 130.0, "sentido": "al_menos",
             "start_day": DAY},
            "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT 1 FROM nutrient_target WHERE person_id = 1 AND metric_key = 'protein_g' AND start_day = ?",
            (DAY,),
        ).fetchone()
        self.assertIsNone(row)

    def test_undo_with_a_prior_target_on_the_same_day_restores_it(self):
        db.set_nutrient_target(self.conn, 1, "protein_g", DAY, "absolute", 100.0, "al_menos")
        self.conn.commit()
        out = herramientas.ejecutar(
            self.conn, 1, "proponer_objetivo_nutriente",
            {"metric_key": "protein_g", "kind": "absolute", "amount": 130.0, "sentido": "al_menos",
             "start_day": DAY},
            "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        row = self.conn.execute(
            "SELECT kind, amount, sentido FROM nutrient_target WHERE person_id = 1 AND metric_key = 'protein_g' "
            "AND start_day = ?", (DAY,),
        ).fetchone()
        self.assertEqual((row["kind"], row["amount"], row["sentido"]), ("absolute", 100.0, "al_menos"))

    def test_apply_refuses_on_drift_when_the_row_changed_after_proposing(self):
        out = herramientas.ejecutar(
            self.conn, 1, "proponer_objetivo_nutriente",
            {"metric_key": "protein_g", "kind": "absolute", "amount": 130.0, "sentido": "al_menos",
             "start_day": DAY},
            "ninguno", self.cid,
        )
        db.set_nutrient_target(self.conn, 1, "protein_g", DAY, "absolute", 999.0, "al_menos")
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        row = self.conn.execute(
            "SELECT amount FROM nutrient_target WHERE person_id = 1 AND metric_key = 'protein_g' AND start_day = ?",
            (DAY,),
        ).fetchone()
        self.assertEqual(row["amount"], 999.0)

    def test_undo_refuses_on_drift_when_the_row_changed_after_applying(self):
        out = herramientas.ejecutar(
            self.conn, 1, "proponer_objetivo_nutriente",
            {"metric_key": "protein_g", "kind": "absolute", "amount": 130.0, "sentido": "al_menos",
             "start_day": DAY},
            "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        db.set_nutrient_target(self.conn, 1, "protein_g", DAY, "absolute", 999.0, "al_menos")
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        row = self.conn.execute(
            "SELECT amount FROM nutrient_target WHERE person_id = 1 AND metric_key = 'protein_g' AND start_day = ?",
            (DAY,),
        ).fetchone()
        self.assertEqual(row["amount"], 999.0)

    def test_non_accumulable_metric_key_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "proponer_objetivo_nutriente",
                {"metric_key": "resting_hr", "kind": "absolute", "amount": 50.0, "sentido": "al_menos"},
                "ninguno", self.cid,
            )

    def test_per_kg_on_a_non_gram_metric_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "proponer_objetivo_nutriente",
                {"metric_key": "caffeine_mg", "kind": "per_kg", "amount": 0.1, "sentido": "al_menos"},
                "ninguno", self.cid,
            )

    def test_non_positive_amount_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "proponer_objetivo_nutriente",
                {"metric_key": "protein_g", "kind": "absolute", "amount": 0.0, "sentido": "al_menos"},
                "ninguno", self.cid,
            )

    def test_invalid_kind_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "proponer_objetivo_nutriente",
                {"metric_key": "protein_g", "kind": "bogus", "amount": 100.0, "sentido": "al_menos"},
                "ninguno", self.cid,
            )

    def test_invalid_sentido_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "proponer_objetivo_nutriente",
                {"metric_key": "protein_g", "kind": "absolute", "amount": 100.0, "sentido": "bogus"},
                "ninguno", self.cid,
            )

    def test_malformed_start_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "proponer_objetivo_nutriente",
                {"metric_key": "protein_g", "kind": "absolute", "amount": 100.0, "sentido": "al_menos",
                 "start_day": "not-a-date"},
                "ninguno", self.cid,
            )

    def test_off_ignores_any_amount_given(self):
        out = herramientas.ejecutar(
            self.conn, 1, "proponer_objetivo_nutriente",
            {"metric_key": "protein_g", "kind": "off", "amount": 999.0, "sentido": "al_menos", "start_day": DAY},
            "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        row = self.conn.execute(
            "SELECT amount FROM nutrient_target WHERE person_id = 1 AND metric_key = 'protein_g' AND start_day = ?",
            (DAY,),
        ).fetchone()
        self.assertIsNone(row["amount"])


# ---- the assistant card: words in the person's language ---------------------------------------------

class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, **params):
        out = herramientas.ejecutar(self.conn, 1, "proponer_objetivo_nutriente", params, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_absolute_al_menos_card_es(self):
        t = self._tarjeta("es", metric_key="protein_g", kind="absolute", amount=130.0, sentido="al_menos",
                          start_day=DAY)
        self.assertIn("Proteína", t["despues"])
        self.assertIn("al menos", t["despues"])
        self.assertIn("130", t["despues"])
        self.assertIn("g", t["despues"])

    def test_como_maximo_card_en(self):
        t = self._tarjeta("en", metric_key="caffeine_mg", kind="absolute", amount=400.0, sentido="como_maximo",
                          start_day=DAY)
        self.assertIn("Caffeine", t["despues"])
        self.assertIn("at most", t["despues"])
        self.assertIn("400", t["despues"])

    def test_per_kg_card_en(self):
        t = self._tarjeta("en", metric_key="protein_g", kind="per_kg", amount=1.6, sentido="al_menos",
                          start_day=DAY)
        self.assertIn("per kg", t["despues"])
        self.assertIn("1.6", t["despues"])

    def test_off_card_es(self):
        t = self._tarjeta("es", metric_key="protein_g", kind="off", sentido="al_menos", start_day=DAY)
        self.assertIn("sin meta diaria", t["despues"])


# ---- Today / Día / Ajustes screens -------------------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


class PantallasTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def _seed_dos_metas(self, conn, hoy):
        """A below-target per_kg line (protein_g) and a within-limit absolute line (caffeine_mg),
        the same two shapes ruling 8's demo data shows -- invented amounts only."""
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", 1)
        db.set_metric(conn, hoy.isoformat(), "body_weight_kg", 75.0, "self", 1)
        db.registrar_ingesta(conn, 1, hoy.isoformat(), "protein_g", 60.0, False)
        db.registrar_ingesta(conn, 1, hoy.isoformat(), "caffeine_mg", 100.0, False)
        with conn:
            db.set_nutrient_target(conn, 1, "protein_g", hoy.isoformat(), "per_kg", 1.6, "al_menos")
            db.set_nutrient_target(conn, 1, "caffeine_mg", hoy.isoformat(), "absolute", 400.0, "como_maximo")

    def test_today_shows_both_lines(self):
        conn = self._reset_db("hoy_nutriente.db")
        hoy = date(2026, 6, 10)
        self._seed_dos_metas(conn, hoy)
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "metric.protein_g"), r.text)
        self.assertIn(_t("es", "metric.caffeine_mg"), r.text)
        self.assertIn(_t("es", "nutriente.hoy.progreso", total="60", objetivo="120", unidad="g", n=1,
                          restante="60"), r.text)
        self.assertIn(_t("es", "nutriente.hoy.limite_quedan", restante="300", unidad="mg"), r.text)

    def test_day_shows_both_lines_for_a_past_day(self):
        conn = self._reset_db("dia_nutriente.db")
        hoy = date(2026, 6, 10)
        self._seed_dos_metas(conn, hoy)
        r = self.client.get(f"/day/{hoy.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "metric.protein_g"), r.text)
        self.assertIn(_t("es", "metric.caffeine_mg"), r.text)
        self.assertIn(_t("es", "nutriente.hoy.progreso", total="60", objetivo="120", unidad="g", n=1,
                          restante="60"), r.text)
        self.assertIn(_t("es", "nutriente.hoy.limite_quedan", restante="300", unidad="mg"), r.text)

    def test_falta_peso_shows_its_own_line_with_no_number_guessed(self):
        conn = self._reset_db("falta_peso.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", 1)
        db.registrar_ingesta(conn, 1, hoy.isoformat(), "protein_g", 60.0, False)
        with conn:
            db.set_nutrient_target(conn, 1, "protein_g", hoy.isoformat(), "per_kg", 1.6, "al_menos")
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "nutriente.hoy.falta_peso"), r.text)

    def test_settings_lists_the_live_target(self):
        conn = self._reset_db("settings_nutriente.db")
        hoy = date(2026, 6, 10)
        with conn:
            db.set_nutrient_target(conn, 1, "protein_g", hoy.isoformat(), "per_kg", 1.6, "al_menos")
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "metric.protein_g"), r.text)
        self.assertIn(_t("es", "nutriente.objetivo.per_kg", cantidad="1,6", unidad="g"), r.text)

    def test_proposing_then_applying_through_settings_writes_the_row(self):
        self._reset_db("settings_nutriente_form.db")
        r = self.client.post("/settings/nutriente/proponer",
                              data={"metric_key": "protein_g", "kind": "absolute", "amount": "130",
                                    "sentido": "al_menos", "back": "/settings"})
        self.assertEqual(r.status_code, 303)
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM nutrient_target").fetchone()[0], 0)
        propuesta_id = conn.execute(
            "SELECT id FROM propuesta_agente WHERE accion = 'proponer_objetivo_nutriente'").fetchone()[0]

        r = self.client.get("/settings")
        self.assertIn("130", r.text)

        r = self.client.post(f"/settings/nutriente/propuestas/{propuesta_id}/aplicar",
                              data={"back": "/settings"})
        self.assertEqual(r.status_code, 303)
        row = conn.execute("SELECT kind, amount FROM nutrient_target WHERE metric_key = 'protein_g'").fetchone()
        self.assertEqual((row["kind"], row["amount"]), ("absolute", 130.0))

    def test_discarding_through_settings_writes_nothing(self):
        self._reset_db("settings_nutriente_descartar.db")
        self.client.post("/settings/nutriente/proponer",
                          data={"metric_key": "protein_g", "kind": "absolute", "amount": "130",
                                "sentido": "al_menos", "back": "/settings"})
        conn = db.connect()
        self.addCleanup(conn.close)
        propuesta_id = conn.execute(
            "SELECT id FROM propuesta_agente WHERE accion = 'proponer_objetivo_nutriente'").fetchone()[0]

        r = self.client.post(f"/settings/nutriente/propuestas/{propuesta_id}/descartar",
                              data={"back": "/settings"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM nutrient_target").fetchone()[0], 0)
        estado = conn.execute(
            "SELECT estado FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()[0]
        self.assertEqual(estado, "descartada")


# ---- BODY-NUTRIENTE-OBJETIVO-1-M: six presentation/flow fixes from the AWAITING_APPROVAL review ---

class SvgBarraHeightTest(unittest.TestCase):
    """Fix 1: svg.barra's viewBox stays 100x6, but a fixed CSS height keeps the rendered bar thin
    at every width -- without it the bar follows the viewBox over the full width (~19px at 375px)."""

    def test_svg_barra_has_a_fixed_thin_height(self):
        css = (STATIC_DIR / "body.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"svg\.barra\{[^}]*height:6px")


class AjustesStartDayTest(unittest.TestCase):
    """Fixes 2 and 3: Ajustes' own "Metas diarias" listing and its pending-proposal card both name
    the target's start day -- rulings 6 and 3 respectively."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_vigentes_listing_names_its_start_day(self):
        conn = self._reset_db("vigentes_desde.db")
        with conn:
            db.set_nutrient_target(conn, 1, "protein_g", "2026-10-01", "absolute", 130.0, "al_menos")
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        fecha = comun._short_date(T("es"), date(2026, 10, 1))
        self.assertIn(_t("es", "settings.nutriente.desde", fecha=fecha), r.text)

    def test_pending_proposal_card_names_its_start_day(self):
        conn = self._reset_db("pendiente_desde.db")
        cid = _cid(conn)
        despues = json.dumps({"metric_key": "protein_g", "kind": "absolute", "amount": 130.0,
                               "sentido": "al_menos", "start_day": "2026-10-05"})
        conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'proponer_objetivo_nutriente', ?, 'propuesta', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00')",
            (cid, despues),
        )
        conn.commit()
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        fecha = comun._short_date(T("es"), date(2026, 10, 5))
        self.assertIn(_t("es", "settings.nutriente.propuesta_desde", fecha=fecha), r.text)


class AjustesUndoTest(unittest.TestCase):
    """Fix 4: Ajustes shows Deshacer on an applied target proposal, posting to the SAME
    `agente_propuestas.deshacer` the assistant uses -- no second undo path."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def _proponer(self, conn, person_id: int) -> int:
        cid = _cid(conn, person_id)
        out = herramientas.ejecutar(
            conn, person_id, "proponer_objetivo_nutriente",
            {"metric_key": "protein_g", "kind": "absolute", "amount": 130.0, "sentido": "al_menos",
             "start_day": DAY},
            "ninguno", cid,
        )
        conn.commit()
        return out["propuesta_id"]

    def test_apply_then_undo_through_settings_restores_the_prior_row(self):
        conn = self._reset_db("ajustes_deshacer.db")
        with conn:
            db.set_nutrient_target(conn, 1, "protein_g", DAY, "absolute", 100.0, "al_menos")
        propuesta_id = self._proponer(conn, 1)

        r = self.client.post(f"/settings/nutriente/propuestas/{propuesta_id}/aplicar", data={"back": "/settings"})
        self.assertEqual(r.status_code, 303)
        row = conn.execute("SELECT amount FROM nutrient_target WHERE metric_key = 'protein_g'").fetchone()
        self.assertEqual(row["amount"], 130.0)

        r = self.client.get("/settings")
        self.assertIn(_t("es", "asistente.propuesta.deshacer"), r.text)

        r = self.client.post(f"/settings/nutriente/propuestas/{propuesta_id}/deshacer", data={"back": "/settings"})
        self.assertEqual(r.status_code, 303)
        row = conn.execute("SELECT amount FROM nutrient_target WHERE metric_key = 'protein_g'").fetchone()
        self.assertEqual(row["amount"], 100.0)

    def test_undo_with_no_prior_target_removes_the_row_through_settings(self):
        conn = self._reset_db("ajustes_deshacer_vacio.db")
        propuesta_id = self._proponer(conn, 1)
        self.client.post(f"/settings/nutriente/propuestas/{propuesta_id}/aplicar", data={"back": "/settings"})

        r = self.client.post(f"/settings/nutriente/propuestas/{propuesta_id}/deshacer", data={"back": "/settings"})
        self.assertEqual(r.status_code, 303)
        row = conn.execute("SELECT 1 FROM nutrient_target WHERE metric_key = 'protein_g'").fetchone()
        self.assertIsNone(row)

    def test_a_strangers_proposal_id_is_refused(self):
        conn = self._reset_db("ajustes_deshacer_ajena.db")
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        propuesta_id = self._proponer(conn, 2)
        propuestas.aplicar(conn, 2, propuesta_id)
        conn.commit()

        r = self.client.post(f"/settings/nutriente/propuestas/{propuesta_id}/deshacer", data={"back": "/settings"})
        self.assertEqual(r.status_code, 303)
        estado = conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()[0]
        self.assertEqual(estado, "aplicada")


class CumplidaWordingTest(unittest.TestCase):
    """Fix 6: Today keeps "Today's target reached" / "Meta del día cumplida"; Día on a day other
    than today never says "today" in English."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_today_screen_keeps_todays_wording(self):
        conn = self._reset_db("cumplida_hoy.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", 1)
        db.registrar_ingesta(conn, 1, hoy.isoformat(), "protein_g", 130.0, False)
        with conn:
            db.set_nutrient_target(conn, 1, "protein_g", hoy.isoformat(), "absolute", 100.0, "al_menos")
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "nutriente.hoy.cumplida"), r.text)

    def test_day_screen_on_a_past_day_never_says_today_in_english(self):
        conn = self._reset_db("cumplida_pasado.db")
        hoy = date(2026, 6, 10)
        pasado = date(2026, 6, 5)
        db.set_metric(conn, pasado.isoformat(), "resting_hr", 55.0, "garmin", 1)
        db.registrar_ingesta(conn, 1, pasado.isoformat(), "protein_g", 130.0, False)
        with conn:
            db.set_nutrient_target(conn, 1, "protein_g", pasado.isoformat(), "absolute", 100.0, "al_menos")
        self.client.post("/settings/language", data={"lang": "en"})
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get(f"/day/{pasado.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("en", "nutriente.hoy.cumplida_dia"), r.text)
        self.assertNotIn(_t("en", "nutriente.hoy.cumplida"), r.text)


if __name__ == "__main__":
    unittest.main()
