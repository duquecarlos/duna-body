"""BODY-NUTRIENTE-OBJETIVO-1: a daily target for a `suma_ingesta` metric (`nutrient_target`),
generic over all eight -- a propose/apply/undo tool, the Today/Día line and Ajustes' own form.
Invented demo data only, no real health figures.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, nutriente_objetivo, privacidad, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import comun

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


if __name__ == "__main__":
    unittest.main()
