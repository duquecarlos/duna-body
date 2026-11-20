"""BODY-PLAN-FASES-5 (ORCH-CARLOS-DELEGATES-TIER-1-1): the `entreno_dolor` schema and db.py's own
read/write functions -- registrar_dolor_entreno, dolor_de_sesion, pendientes_dolor_manana,
registrar_dolor_manana. Invented data only, no real health figure.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db

DAY = "2026-11-19"


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


if __name__ == "__main__":
    unittest.main()
