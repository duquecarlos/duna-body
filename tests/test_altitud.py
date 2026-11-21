"""BODY-MINUTO-ANALISIS-6-ALTITUD (ORCH-CARLOS-DELEGATES-TIER-1-1, second stage of
BODY-MINUTO-ANALISIS-4-INV): the `altitud_estancia` table, its validator, and the
`registrar_altitud`/`leer_altitudes` assistant tools. An altitude and dates only -- no place name,
no coordinates. Invented homes and stays only, never a real figure, no network.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas

PERSON_A = 1
PERSON_B = 2


def _conn(case: unittest.TestCase) -> sqlite3.Connection:
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = PERSON_A) -> int:
    return agente_conversacion.crear(conn, person_id)


# ---- db.crear_altitud_estancia / validar_altitud_sin_solape ---------------------------------------

class CrearAltitudEstanciaTest(unittest.TestCase):
    def test_a_home_row_is_stored_and_read_back(self):
        conn = _conn(self)
        aid = db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual")
        conn.commit()
        rows = db.altitud_estancia_rango(conn, PERSON_A, "2020-01-01", "2020-01-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], aid)
        self.assertEqual(rows[0]["es_casa"], 1)
        self.assertEqual(rows[0]["altitud_m"], 2600)
        self.assertIsNone(rows[0]["end_day"])
        self.assertIsNone(rows[0]["viaje_ref"])

    def test_a_stay_row_carries_its_own_viaje_ref(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, False, "2020-02-01", 300, "lugares",
                                   end_day="2020-02-05", viaje_ref="zz-viaje-1")
        conn.commit()
        rows = db.altitud_estancia_rango(conn, PERSON_A, "2020-02-01", "2020-02-05")
        self.assertEqual(rows[0]["viaje_ref"], "zz-viaje-1")
        self.assertEqual(rows[0]["fuente"], "lugares")

    def test_end_day_before_start_day_refused(self):
        conn = _conn(self)
        with self.assertRaises(ValueError):
            db.crear_altitud_estancia(conn, PERSON_A, True, "2020-02-10", 100, "manual", end_day="2020-02-01")

    def test_altitud_m_out_of_range_refused(self):
        conn = _conn(self)
        with self.assertRaises(ValueError):
            db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 9001, "manual")
        with self.assertRaises(ValueError):
            db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", -501, "manual")

    def test_unknown_fuente_refused(self):
        conn = _conn(self)
        with self.assertRaises(ValueError):
            db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 100, "garmin")

    def test_overlapping_home_rows_refused_by_name(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: home rows never overlap -- a move is a NEW
        home row, never a change to the old one's own dates."""
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual", end_day="2020-06-30")
        with self.assertRaisesRegex(ValueError, "altitud_estancia"):
            db.crear_altitud_estancia(conn, PERSON_A, True, "2020-06-01", 100, "manual")

    def test_overlapping_open_home_row_refused(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual")
        with self.assertRaisesRegex(ValueError, "altitud_estancia"):
            db.crear_altitud_estancia(conn, PERSON_A, True, "2025-01-01", 100, "manual")

    def test_overlapping_stays_refused_by_name(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, False, "2020-03-01", 300, "manual", end_day="2020-03-10")
        with self.assertRaisesRegex(ValueError, "altitud_estancia"):
            db.crear_altitud_estancia(conn, PERSON_A, False, "2020-03-05", 400, "manual", end_day="2020-03-20")

    def test_a_home_row_and_a_stay_row_may_overlap(self):
        """A stay happens WHILE away from home -- the two kinds are never checked against each
        other, only against rows of their own kind."""
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual")
        aid = db.crear_altitud_estancia(conn, PERSON_A, False, "2020-03-01", 300, "manual", end_day="2020-03-10")
        conn.commit()
        self.assertIsNotNone(aid)

    def test_another_persons_overlapping_row_never_blocks(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_B, True, "2020-01-01", 2600, "manual")
        aid = db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 500, "manual")
        conn.commit()
        self.assertIsNotNone(aid)

    def test_a_non_overlapping_home_row_after_a_move_is_accepted(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual", end_day="2020-06-30")
        aid = db.crear_altitud_estancia(conn, PERSON_A, True, "2020-07-01", 100, "manual")
        conn.commit()
        self.assertIsNotNone(aid)


class BorrarAltitudEstanciaTest(unittest.TestCase):
    def test_deletes_the_row(self):
        conn = _conn(self)
        aid = db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual")
        conn.commit()
        db.borrar_altitud_estancia(conn, PERSON_A, aid)
        conn.commit()
        self.assertEqual(db.altitud_estancia_rango(conn, PERSON_A, "2020-01-01", "2020-01-01"), [])

    def test_another_persons_row_is_refused(self):
        conn = _conn(self)
        aid = db.crear_altitud_estancia(conn, PERSON_B, True, "2020-01-01", 2600, "manual")
        conn.commit()
        with self.assertRaises(ValueError):
            db.borrar_altitud_estancia(conn, PERSON_A, aid)


class AltitudEstanciaRangoTest(unittest.TestCase):
    def test_open_row_matches_regardless_of_desde(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2015-01-01", 2600, "manual")
        rows = db.altitud_estancia_rango(conn, PERSON_A, "2026-01-01", "2026-01-31")
        self.assertEqual(len(rows), 1)

    def test_a_row_entirely_before_the_range_is_excluded(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, False, "2015-01-01", 300, "manual", end_day="2015-01-10")
        rows = db.altitud_estancia_rango(conn, PERSON_A, "2026-01-01", "2026-01-31")
        self.assertEqual(rows, [])

    def test_another_persons_row_is_never_returned(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_B, True, "2020-01-01", 2600, "manual")
        rows = db.altitud_estancia_rango(conn, PERSON_A, "2020-01-01", "2020-01-01")
        self.assertEqual(rows, [])


class AltitudCasaEnTest(unittest.TestCase):
    def test_returns_the_home_row_covering_the_day(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual", end_day="2020-06-30")
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-07-01", 100, "manual")
        casa = db.altitud_casa_en(conn, PERSON_A, "2020-07-15")
        self.assertEqual(casa["altitud_m"], 100)

    def test_returns_none_with_no_covering_home_row(self):
        conn = _conn(self)
        casa = db.altitud_casa_en(conn, PERSON_A, "2020-07-15")
        self.assertIsNone(casa)


# ---- the assistant tools: registrar_altitud / leer_altitudes --------------------------------------

class RegistrarAltitudToolTest(unittest.TestCase):
    def test_creates_a_pending_proposal_never_writing_the_table(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(conn, PERSON_A, "registrar_altitud",
                                     {"es_casa": True, "start_day": "2020-01-01", "altitud_m": 2600},
                                     "ninguno", cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM altitud_estancia WHERE person_id = ?", (PERSON_A,)).fetchone()[0], 0)

    def test_applying_writes_the_row_and_deshacer_removes_it(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4: insert-shaped, like marcar_periodo -- applied
        through db.crear_altitud_estancia, undone through db.borrar_altitud_estancia."""
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(conn, PERSON_A, "registrar_altitud",
                                     {"es_casa": False, "start_day": "2020-02-01", "altitud_m": 300,
                                      "end_day": "2020-02-10"},
                                     "ninguno", cid)
        resultado = propuestas.aplicar(conn, PERSON_A, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        fila = conn.execute(
            "SELECT es_casa, altitud_m FROM altitud_estancia WHERE person_id = ?", (PERSON_A,)
        ).fetchone()
        self.assertEqual(fila["es_casa"], 0)
        self.assertEqual(fila["altitud_m"], 300)

        deshecho = propuestas.deshacer(conn, PERSON_A, out["propuesta_id"])
        self.assertEqual(deshecho["estado"], "deshecha")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM altitud_estancia WHERE person_id = ?", (PERSON_A,)).fetchone()[0], 0)

    def test_fuente_is_always_asistente_never_a_model_facing_field(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(conn, PERSON_A, "registrar_altitud",
                                     {"es_casa": True, "start_day": "2020-01-01", "altitud_m": 2600},
                                     "ninguno", cid)
        propuestas.aplicar(conn, PERSON_A, out["propuesta_id"])
        fuente = conn.execute(
            "SELECT fuente FROM altitud_estancia WHERE person_id = ?", (PERSON_A,)
        ).fetchone()[0]
        self.assertEqual(fuente, "asistente")

    def test_overlapping_home_row_refused_before_any_proposal_is_created(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual")
        conn.commit()
        cid = _cid(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, PERSON_A, "registrar_altitud",
                                   {"es_casa": True, "start_day": "2020-06-01", "altitud_m": 100},
                                   "ninguno", cid)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM propuesta_agente WHERE person_id = ? AND accion = "
                         "'registrar_altitud'", (PERSON_A,)).fetchone()[0], 0)

    def test_end_day_before_start_day_refused(self):
        conn = _conn(self)
        cid = _cid(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, PERSON_A, "registrar_altitud",
                                   {"es_casa": True, "start_day": "2020-06-10", "altitud_m": 100,
                                    "end_day": "2020-06-01"},
                                   "ninguno", cid)

    def test_another_persons_overlapping_row_never_blocks(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_B, True, "2020-01-01", 2600, "manual")
        conn.commit()
        cid = _cid(conn)
        out = herramientas.ejecutar(conn, PERSON_A, "registrar_altitud",
                                     {"es_casa": True, "start_day": "2020-01-01", "altitud_m": 100},
                                     "ninguno", cid)
        self.assertEqual(out["estado"], "propuesta")


class LeerAltitudesToolTest(unittest.TestCase):
    def test_lists_this_persons_own_rows_only(self):
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual")
        db.crear_altitud_estancia(conn, PERSON_B, True, "2020-01-01", 500, "manual")
        conn.commit()
        cid = _cid(conn)
        out = herramientas.ejecutar(conn, PERSON_A, "leer_altitudes", {}, "ninguno", cid)
        self.assertEqual(len(out["altitudes"]), 1)
        self.assertEqual(out["altitudes"][0]["altitud_m"], 2600)

    def test_never_carries_a_place_name_or_coordinates(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: metres and dates only."""
        conn = _conn(self)
        db.crear_altitud_estancia(conn, PERSON_A, True, "2020-01-01", 2600, "manual")
        conn.commit()
        cid = _cid(conn)
        out = herramientas.ejecutar(conn, PERSON_A, "leer_altitudes", {}, "ninguno", cid)
        self.assertEqual(set(out["altitudes"][0]),
                          {"id", "es_casa", "start_day", "end_day", "altitud_m", "fuente", "viaje_ref"})


if __name__ == "__main__":
    unittest.main()
