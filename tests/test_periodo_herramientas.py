"""BODY-PERIODO-1: the assistant tools -- marcar_periodo, cerrar_periodo, corregir_periodo,
borrar_periodo -- propose/apply/undo, their card text (es/en), leer_dia carrying the day's own
periods, and the person's note never leaking into a refusal message. Invented demo data only, no
real health figures.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

DAY = "2026-11-05"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.execute(
        "INSERT INTO daily_metric (person_id, day, metric_key, value, source) "
        "VALUES (1, '2026-11-01', 'resting_hr', 55.0, 'garmin')"
    )
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


# ---- marcar_periodo: propose -> apply -> undo ---------------------------------------------------

class MarcarPeriodoProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_propose_writes_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0]
        out = herramientas.ejecutar(
            self.conn, 1, "marcar_periodo", {"kind": "malestar", "start_day": DAY}, "ninguno", self.cid,
        )
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0], before)

    def test_apply_creates_the_period(self):
        out = herramientas.ejecutar(
            self.conn, 1, "marcar_periodo",
            {"kind": "viaje", "start_day": DAY, "end_day": "2026-11-07", "note": "a Medellín"},
            "ninguno", self.cid,
        )
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT kind, start_day, end_day, note FROM period WHERE person_id = 1").fetchone()
        self.assertEqual((row["kind"], row["start_day"], row["end_day"], row["note"]),
                          ("viaje", DAY, "2026-11-07", "a Medellín"))

    def test_undo_removes_the_period(self):
        out = herramientas.ejecutar(
            self.conn, 1, "marcar_periodo", {"kind": "malestar", "start_day": DAY}, "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0], 0)

    def test_start_day_before_first_data_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "marcar_periodo", {"kind": "malestar", "start_day": "2026-10-01"},
                "ninguno", self.cid,
            )

    def test_start_day_in_the_future_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "marcar_periodo", {"kind": "malestar", "start_day": "2099-01-01"},
                "ninguno", self.cid,
            )

    def test_end_day_before_start_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "marcar_periodo",
                {"kind": "malestar", "start_day": DAY, "end_day": "2026-11-01"}, "ninguno", self.cid,
            )

    def test_unknown_kind_is_refused_by_the_schema(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "marcar_periodo", {"kind": "gripe", "start_day": DAY}, "ninguno", self.cid,
            )


# ---- cerrar_periodo: propose -> apply -> undo ---------------------------------------------------

class CerrarPeriodoProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.pid = db.crear_periodo(self.conn, 1, "malestar", DAY, note="algo")
        self.conn.commit()

    def test_apply_sets_end_day(self):
        out = herramientas.ejecutar(
            self.conn, 1, "cerrar_periodo", {"period_id": self.pid, "end_day": "2026-11-08"}, "ninguno", self.cid,
        )
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT end_day FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertEqual(row["end_day"], "2026-11-08")

    def test_undo_reopens(self):
        out = herramientas.ejecutar(
            self.conn, 1, "cerrar_periodo", {"period_id": self.pid, "end_day": "2026-11-08"}, "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute("SELECT end_day FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertIsNone(row["end_day"])

    def test_already_closed_period_is_refused(self):
        db.actualizar_periodo(self.conn, 1, self.pid, end_day="2026-11-06")
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "cerrar_periodo", {"period_id": self.pid, "end_day": "2026-11-08"},
                "ninguno", self.cid,
            )

    def test_unknown_period_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "cerrar_periodo", {"period_id": 999999, "end_day": "2026-11-08"},
                "ninguno", self.cid,
            )

    def test_end_day_before_start_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "cerrar_periodo", {"period_id": self.pid, "end_day": "2026-11-01"},
                "ninguno", self.cid,
            )

    def test_another_persons_period_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 2, "cerrar_periodo", {"period_id": self.pid, "end_day": "2026-11-08"},
                "ninguno", _cid(self.conn, 2),
            )


# ---- corregir_periodo: propose -> apply -> undo -------------------------------------------------

class CorregirPeriodoProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.pid = db.crear_periodo(self.conn, 1, "malestar", DAY, "2026-11-07", "algo")
        self.conn.commit()

    def test_apply_updates_kind(self):
        out = herramientas.ejecutar(
            self.conn, 1, "corregir_periodo", {"period_id": self.pid, "kind": "lesion"}, "ninguno", self.cid,
        )
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT kind, start_day, end_day FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertEqual((row["kind"], row["start_day"], row["end_day"]), ("lesion", DAY, "2026-11-07"))

    def test_undo_restores_prior_values(self):
        out = herramientas.ejecutar(
            self.conn, 1, "corregir_periodo", {"period_id": self.pid, "kind": "lesion"}, "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute("SELECT kind FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertEqual(row["kind"], "malestar")

    def test_no_fields_given_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_periodo", {"period_id": self.pid}, "ninguno", self.cid)

    def test_unknown_period_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "corregir_periodo", {"period_id": 999999, "kind": "lesion"}, "ninguno", self.cid,
            )

    def test_another_persons_period_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 2, "corregir_periodo", {"period_id": self.pid, "kind": "lesion"}, "ninguno",
                _cid(self.conn, 2),
            )


# ---- borrar_periodo: propose -> apply -> undo ---------------------------------------------------

class BorrarPeriodoProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.pid = db.crear_periodo(self.conn, 1, "lesion", DAY, "2026-11-07", "rodilla")
        self.conn.commit()

    def test_apply_deletes_the_period(self):
        out = herramientas.ejecutar(self.conn, 1, "borrar_periodo", {"period_id": self.pid}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0], 0)

    def test_undo_restores_the_period(self):
        out = herramientas.ejecutar(self.conn, 1, "borrar_periodo", {"period_id": self.pid}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute("SELECT kind, start_day, end_day, note FROM period WHERE person_id = 1").fetchone()
        self.assertEqual((row["kind"], row["start_day"], row["end_day"], row["note"]),
                          ("lesion", DAY, "2026-11-07", "rodilla"))

    def test_unknown_period_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "borrar_periodo", {"period_id": 999999}, "ninguno", self.cid)

    def test_another_persons_period_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 2, "borrar_periodo", {"period_id": self.pid}, "ninguno", _cid(self.conn, 2),
            )


# ---- leer_dia carries the day's own periods ------------------------------------------------------

class LeerDiaIncludesPeriodosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.pid = db.crear_periodo(self.conn, 1, "malestar", "2026-11-04", "2026-11-06", "gripe")
        self.conn.commit()

    def test_leer_dia_shows_the_covering_period(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(len(out["periodos"]), 1)
        row = out["periodos"][0]
        self.assertEqual((row["id"], row["kind"], row["start_day"], row["end_day"]),
                          (self.pid, "malestar", "2026-11-04", "2026-11-06"))

    def test_leer_dia_on_an_uncovered_day_shows_none(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": "2026-12-01"}, "completo")
        self.assertEqual(out["periodos"], [])


# ---- card text (es/en) -----------------------------------------------------------------------

class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, accion: str, params: dict) -> dict:
        out = herramientas.ejecutar(self.conn, 1, accion, params, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_marcar_periodo_card_es(self):
        t = self._tarjeta("es", "marcar_periodo",
                          {"kind": "viaje", "start_day": DAY, "end_day": "2026-11-07", "note": "a Medellín"})
        self.assertIn("Viaje", t["despues"])
        self.assertIn("«a Medellín»", t["despues"])
        self.assertIn("dejará esos días fuera", t["despues"])

    def test_marcar_periodo_card_en(self):
        t = self._tarjeta("en", "marcar_periodo",
                          {"kind": "viaje", "start_day": DAY, "end_day": "2026-11-07", "note": "to Medellín"})
        self.assertIn("Travel", t["despues"])
        self.assertIn("“to Medellín”", t["despues"])
        self.assertIn("leave those days out", t["despues"])

    def test_marcar_periodo_open_card_says_since(self):
        t = self._tarjeta("es", "marcar_periodo", {"kind": "malestar", "start_day": DAY})
        self.assertIn("desde el", t["despues"])

    def test_cerrar_periodo_card_shows_antes_and_despues(self):
        pid = db.crear_periodo(self.conn, 1, "malestar", DAY)
        self.conn.commit()
        t = self._tarjeta("es", "cerrar_periodo", {"period_id": pid, "end_day": "2026-11-08"})
        self.assertIsNotNone(t["antes"])
        self.assertIn("desde el", t["antes"])
        self.assertIn("del", t["despues"])

    def test_corregir_periodo_card_shows_the_change(self):
        pid = db.crear_periodo(self.conn, 1, "malestar", DAY, "2026-11-07")
        self.conn.commit()
        t = self._tarjeta("es", "corregir_periodo", {"period_id": pid, "kind": "lesion"})
        self.assertIn("Malestar", t["antes"])
        self.assertIn("Lesión", t["despues"])

    def test_borrar_periodo_card_says_eliminado(self):
        pid = db.crear_periodo(self.conn, 1, "lesion", DAY)
        self.conn.commit()
        t = self._tarjeta("es", "borrar_periodo", {"period_id": pid})
        self.assertIn("Lesión", t["antes"])
        self.assertEqual(t["despues"], "Después: eliminado")


# ---- the note never leaks into a refusal message -------------------------------------------------

class NotaNeverLeaksTest(unittest.TestCase):
    """The person's own words (`note`) are kept on the period, their data, on their PC -- never
    printed, logged, or echoed back inside a ToolError/ValueError message."""

    SECRETO = "ZZNOTA-SECRETA-NO-DEBE-SALIR"

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.pid = db.crear_periodo(self.conn, 1, "malestar", DAY, note=self.SECRETO)
        self.conn.commit()

    def test_cross_person_tool_refusals_never_mention_the_note(self):
        for nombre, params in (
            ("cerrar_periodo", {"period_id": self.pid, "end_day": "2026-11-08"}),
            ("corregir_periodo", {"period_id": self.pid, "kind": "lesion"}),
            ("borrar_periodo", {"period_id": self.pid}),
        ):
            with self.assertRaises(herramientas.ToolError) as ctx:
                herramientas.ejecutar(self.conn, 2, nombre, params, "ninguno", _cid(self.conn, 2))
            self.assertNotIn(self.SECRETO, str(ctx.exception))

    def test_cross_person_db_refusals_never_mention_the_note(self):
        with self.assertRaises(ValueError) as ctx1:
            db.actualizar_periodo(self.conn, 2, self.pid, end_day="2026-11-08")
        self.assertNotIn(self.SECRETO, str(ctx1.exception))
        with self.assertRaises(ValueError) as ctx2:
            db.borrar_periodo(self.conn, 2, self.pid)
        self.assertNotIn(self.SECRETO, str(ctx2.exception))


if __name__ == "__main__":
    unittest.main()
