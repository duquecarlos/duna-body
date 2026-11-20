"""BODY-PLAN-FASES-5 (ORCH-CARLOS-DELEGATES-TIER-1-1): the `entreno_dolor` schema and db.py's own
read/write functions -- registrar_dolor_entreno, dolor_de_sesion, pendientes_dolor_manana,
registrar_dolor_manana. Invented data only, no real health figure.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from fastapi.testclient import TestClient

from body import config, db, garmin_adapter, identity, web
from body.agente import herramientas, propuestas
from body.agente import conversacion as agente_conversacion
from body.rutas import comun

DAY = "2026-11-19"

client = TestClient(web.app, follow_redirects=False)


def _as(tc: unittest.TestCase, person_id: int) -> None:
    tc.addCleanup(web.app.dependency_overrides.clear)
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _route_conn(tc: unittest.TestCase, name: str) -> sqlite3.Connection:
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _conn(case: unittest.TestCase) -> sqlite3.Connection:
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


# ---- schema -------------------------------------------------------------------------------------

class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(entreno_dolor)")}
        self.assertEqual(cols, {
            "id", "person_id", "day", "sets_source", "sets_source_id", "ejercicio", "zona",
            "dolor_durante", "dolor_despues", "movimiento", "dolor_manana",
            "manana_registrado_en", "creado_en",
        })

    def test_dolor_durante_out_of_range_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO entreno_dolor (person_id, day, sets_source, sets_source_id, ejercicio, "
                "zona, dolor_durante, creado_en) VALUES (1, ?, 'garmin', 's1', 'press militar', "
                "'hombro', 11, '2026-11-19T00:00:00+00:00')", (DAY,),
            )

    def test_dolor_despues_out_of_range_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO entreno_dolor (person_id, day, sets_source, sets_source_id, ejercicio, "
                "zona, dolor_despues, creado_en) VALUES (1, ?, 'garmin', 's1', 'press militar', "
                "'hombro', -1, '2026-11-19T00:00:00+00:00')", (DAY,),
            )

    def test_dolor_manana_out_of_range_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO entreno_dolor (person_id, day, sets_source, sets_source_id, ejercicio, "
                "zona, dolor_manana, creado_en) VALUES (1, ?, 'garmin', 's1', 'press militar', "
                "'hombro', 11, '2026-11-19T00:00:00+00:00')", (DAY,),
            )
        # 0 and 10 are both IN range -- the CHECK above only ever refuses outside it.
        self.conn.execute(
            "INSERT INTO entreno_dolor (person_id, day, sets_source, sets_source_id, ejercicio, "
            "zona, dolor_manana, creado_en) VALUES (1, ?, 'garmin', 's1', 'press militar', "
            "'hombro', 0, '2026-11-19T00:00:00+00:00')", (DAY,),
        )

    def test_null_values_pass(self):
        self.conn.execute(
            "INSERT INTO entreno_dolor (person_id, day, sets_source, sets_source_id, ejercicio, "
            "zona, creado_en) VALUES (1, ?, 'garmin', 's1', 'press militar', 'hombro', "
            "'2026-11-19T00:00:00+00:00')", (DAY,),
        )


# ---- registrar_dolor_entreno / dolor_de_sesion --------------------------------------------------

class RegistrarDolorTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_upserts_one_row_per_exercise(self):
        db.registrar_dolor_entreno(self.conn, 1, DAY, "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3, "dolor_despues": 2,
             "movimiento": "al bajar la barra"},
            {"ejercicio": "curl de biceps", "zona": "brazo", "dolor_durante": 0, "dolor_despues": None,
             "movimiento": None},
        ])
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s1")
        self.assertEqual(len(filas), 2)
        por_ejercicio = {f["ejercicio"]: f for f in filas}
        self.assertEqual(por_ejercicio["press militar"]["dolor_durante"], 3)
        self.assertEqual(por_ejercicio["press militar"]["dolor_despues"], 2)
        self.assertEqual(por_ejercicio["press militar"]["movimiento"], "al bajar la barra")
        self.assertEqual(por_ejercicio["curl de biceps"]["dolor_durante"], 0)
        self.assertIsNone(por_ejercicio["curl de biceps"]["dolor_despues"])

    def test_saving_again_for_the_same_exercise_edits_the_same_row_never_a_second_one(self):
        db.registrar_dolor_entreno(self.conn, 1, DAY, "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3, "dolor_despues": 2,
             "movimiento": None},
        ])
        db.registrar_dolor_entreno(self.conn, 1, DAY, "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 5, "dolor_despues": 4,
             "movimiento": "ahora mas"},
        ])
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s1")
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["dolor_durante"], 5)
        self.assertEqual(filas[0]["dolor_despues"], 4)
        self.assertEqual(filas[0]["movimiento"], "ahora mas")

    def test_another_persons_rows_never_show(self):
        db.registrar_dolor_entreno(self.conn, 1, DAY, "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3, "dolor_despues": 2,
             "movimiento": None},
        ])
        self.assertEqual(db.dolor_de_sesion(self.conn, 2, "garmin", "s1"), [])


# ---- pendientes_dolor_manana / registrar_dolor_manana -------------------------------------------

class PendienteMananaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        db.registrar_dolor_entreno(self.conn, 1, "2026-11-17", "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3, "dolor_despues": 2,
             "movimiento": None},
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3, "dolor_despues": 2,
             "movimiento": None},
        ])

    def test_due_the_day_after(self):
        pend = db.pendientes_dolor_manana(self.conn, 1, "2026-11-18")
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["day"], "2026-11-17")
        self.assertEqual(pend[0]["zona"], "hombro")

    def test_still_due_two_days_after(self):
        pend = db.pendientes_dolor_manana(self.conn, 1, "2026-11-19")
        self.assertEqual(len(pend), 1)

    def test_gone_after_two_days_pass(self):
        pend = db.pendientes_dolor_manana(self.conn, 1, "2026-11-20")
        self.assertEqual(pend, [])

    def test_not_due_the_same_day_it_was_trained(self):
        pend = db.pendientes_dolor_manana(self.conn, 1, "2026-11-17")
        self.assertEqual(pend, [])

    def test_answering_removes_it(self):
        db.registrar_dolor_manana(self.conn, 1, "2026-11-17", "hombro", 4)
        pend = db.pendientes_dolor_manana(self.conn, 1, "2026-11-18")
        self.assertEqual(pend, [])
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s1")
        self.assertTrue(all(f["dolor_manana"] == 4 for f in filas))
        self.assertTrue(all(f["manana_registrado_en"] for f in filas))

    def test_another_persons_pendiente_never_shows(self):
        self.assertEqual(db.pendientes_dolor_manana(self.conn, 2, "2026-11-18"), [])


# ---- Hoy's own 'morning after' card (ruling 4) ---------------------------------------------------

class HoyDolorMananaTest(unittest.TestCase):
    """BODY-PLAN-FASES-5 ruling 4: Hoy's own due 'morning after' card -- appears the day after a
    trained day with an unanswered entreno_dolor row, stays through the day after that, then
    disappears; the POST fills dolor_manana and the card is gone next render."""

    TRAINED_DAY = "2026-11-17"

    def setUp(self):
        self.conn = _route_conn(self, "hoy_dolor.db")
        _as(self, 1)
        self.conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES "
            "(1, ?, 'resting_hr', 55, 'garmin')", (self.TRAINED_DAY,))
        self.conn.commit()
        db.registrar_dolor_entreno(self.conn, 1, self.TRAINED_DAY, "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3, "dolor_despues": 2,
             "movimiento": None},
        ])

    def test_no_card_the_same_day_it_was_trained(self):
        with mock.patch.object(comun, "_today_local", return_value=date.fromisoformat(self.TRAINED_DAY)):
            r = client.get("/")
        self.assertNotIn("dolor-manana", r.text)

    def test_card_appears_the_day_after(self):
        with mock.patch.object(comun, "_today_local", return_value=date(2026, 11, 18)):
            r = client.get("/")
        self.assertIn(f"/day/{self.TRAINED_DAY}/dolor-manana", r.text)
        self.assertIn("press militar", r.text)

    def test_card_gone_after_two_days_pass(self):
        with mock.patch.object(comun, "_today_local", return_value=date(2026, 11, 20)):
            r = client.get("/")
        self.assertNotIn("dolor-manana", r.text)

    def test_post_fills_dolor_manana_and_the_card_is_gone(self):
        r = client.post(f"/day/{self.TRAINED_DAY}/dolor-manana", data={"zona": "hombro", "valor": "4"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s1")
        self.assertEqual(filas[0]["dolor_manana"], 4)
        self.assertIsNotNone(filas[0]["manana_registrado_en"])
        with mock.patch.object(comun, "_today_local", return_value=date(2026, 11, 18)):
            r2 = client.get("/")
        self.assertNotIn("dolor-manana", r2.text)

    def test_out_of_range_value_is_ignored_never_a_500(self):
        r = client.post(f"/day/{self.TRAINED_DAY}/dolor-manana", data={"zona": "hombro", "valor": "99"})
        self.assertEqual(r.status_code, 303)
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s1")
        self.assertIsNone(filas[0]["dolor_manana"])

    def test_another_persons_pendiente_never_shows(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES "
            "(2, ?, 'resting_hr', 50, 'garmin')", (self.TRAINED_DAY,))
        self.conn.commit()
        _as(self, 2)
        with mock.patch.object(comun, "_today_local", return_value=date(2026, 11, 18)):
            r = client.get("/")
        self.assertNotIn("dolor-manana", r.text)


# ---- the assistant's own registrar_dolor proposal tool (ruling 5) -----------------------------

class AgenteRegistrarDolorTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: the proposal tool writes through the same
    db.registrar_dolor_entreno every other writer uses, only once the person applies it."""

    DIA = "2026-11-10"

    def setUp(self):
        self.conn = _conn(self)
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 's-dolor', ?, ?, 'strength_training', NULL, 'garmin')",
            (self.DIA, f"{self.DIA}T07:00:00"))
        db.save_exercise_sets(self.conn, 1, "garmin", "s-dolor", [garmin_adapter.ExerciseSet(
            set_type="active", category="hombro", exercise="press militar", reps=8.0,
            weight_value=20.0, weight_unit="kg", duration_s=30.0, start_local=f"{self.DIA}T07:00:00",
        )])
        self.conn.commit()
        self.cid = agente_conversacion.crear(self.conn, 1)

    def test_unknown_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                   {"dia": "2026-01-01", "ejercicio": "press militar", "dolor_durante": 3},
                                   "completo", self.cid)

    def test_unknown_exercise_that_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                   {"dia": self.DIA, "ejercicio": "sentadilla", "dolor_durante": 3},
                                   "completo", self.cid)

    def test_nothing_to_register_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                   {"dia": self.DIA, "ejercicio": "press militar"}, "completo", self.cid)

    def test_out_of_range_value_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                   {"dia": self.DIA, "ejercicio": "press militar", "dolor_durante": 11},
                                   "completo", self.cid)

    def test_proposing_writes_nothing_until_applied(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                     {"dia": self.DIA, "ejercicio": "press militar", "dolor_durante": 3,
                                      "dolor_despues": 2, "movimiento": "al bajar la barra"},
                                     "completo", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(db.dolor_de_sesion(self.conn, 1, "garmin", "s-dolor"), [])
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s-dolor")
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["ejercicio"], "press militar")
        self.assertEqual(filas[0]["zona"], "hombro")
        self.assertEqual(filas[0]["dolor_durante"], 3)
        self.assertEqual(filas[0]["dolor_despues"], 2)
        self.assertEqual(filas[0]["movimiento"], "al bajar la barra")

    def test_applying_again_edits_the_same_row_never_a_second_one(self):
        out1 = herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                      {"dia": self.DIA, "ejercicio": "press militar", "dolor_durante": 3},
                                      "completo", self.cid)
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        out2 = herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                      {"dia": self.DIA, "ejercicio": "press militar", "dolor_durante": 6},
                                      "completo", self.cid)
        propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s-dolor")
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["dolor_durante"], 6)

    def test_undo_restores_the_prior_value(self):
        out1 = herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                      {"dia": self.DIA, "ejercicio": "press militar", "dolor_durante": 3},
                                      "completo", self.cid)
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        out2 = herramientas.ejecutar(self.conn, 1, "registrar_dolor",
                                      {"dia": self.DIA, "ejercicio": "press militar", "dolor_durante": 6},
                                      "completo", self.cid)
        propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s-dolor")
        self.assertEqual(filas[0]["dolor_durante"], 3)

    def test_leer_entreno_carries_the_numbers_never_the_movimiento(self):
        """Ruling 5's own census: _leer_entreno is the one tool that returns a session's sets, and
        it must include that session's entreno_dolor rows -- numbers only, never the free-text
        movimiento (the one column that never leaves the host)."""
        db.registrar_dolor_entreno(self.conn, 1, self.DIA, "garmin", "s-dolor",
                                    [{"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 4,
                                      "dolor_despues": 1, "movimiento": "al bajar la barra, secreto"}])
        self.conn.commit()
        salida = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": self.DIA}, "completo", self.cid)
        dolor = salida["sesiones"][0]["dolor"]
        self.assertEqual(dolor, [{"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 4,
                                   "dolor_despues": 1, "dolor_manana": None}])
        self.assertNotIn("movimiento", dolor[0])
        self.assertNotIn("secreto", str(salida))

    def test_another_persons_exercise_is_never_matched(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 2, "registrar_dolor",
                                   {"dia": self.DIA, "ejercicio": "press militar", "dolor_durante": 3},
                                   "completo", agente_conversacion.crear(self.conn, 2))


if __name__ == "__main__":
    unittest.main()
