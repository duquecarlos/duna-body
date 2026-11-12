"""BODY-COMIDA-CORREGIR-1: one correction the person means as one change is ONE proposal -- a
meal's time, its wording and its whole set of amounts corrected together, through one tool and one
apply, instead of one corregir_ingesta/borrar_ingesta per nutrient row plus a fresh
registrar_comida. Invented demo data only, no real health figures.
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
OTHER_DAY = "2026-11-06"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


_CANTIDADES = [
    {"metric_key": "protein_g", "amount": 35.0, "estimado": True},
    {"metric_key": "kcal", "amount": 650.0, "estimado": True},
]


# ---- schema: the accion CHECK widens through the existing rebuild migration -------------------

_OLD_PROPUESTA_AGENTE_SQL = """
CREATE TABLE propuesta_agente (
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
                         'separar_sesion', 'unir_sesiones', 'proponer_objetivo_nutriente')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp'))
);
"""


class AccionMigrationTest(unittest.TestCase):
    """A database built at the CHECK shape just before this slice (every other widening already
    applied, 'corregir_comida' still missing) gains it through the SAME reusable rebuild, every
    existing row preserved byte-for-byte, and the migration is idempotent."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(_OLD_PROPUESTA_AGENTE_SQL)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.cid = self.conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en) VALUES (1, '2026-11-09T00:00:00+00:00')"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'registrar_comida', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.commit()

    def _assert_migrated_state(self):
        old_proposal = self.conn.execute(
            "SELECT accion FROM propuesta_agente WHERE conversacion_id = ?", (self.cid,)
        ).fetchone()
        self.assertEqual(old_proposal["accion"], "registrar_comida")
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'corregir_comida', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE accion = 'corregir_comida'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_migration_preserves_the_old_row_and_accepts_the_new_action(self):
        db.init(self.conn)
        self._assert_migrated_state()

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self._assert_migrated_state()
        count = self.conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente").fetchone()["n"]
        self.conn.commit()
        db.init(self.conn)
        after = self.conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente").fetchone()["n"]
        self.assertEqual(count, after)


# ---- db.corregir_comida: the one writer, owner-scoped, re-deriving every touched day ----------

class CorregirComidaWriterTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid_comida = db.crear_comida(self.conn, 1, DAY, _CANTIDADES, "pollo con arroz", f"{DAY}T13:00:00")

    def test_time_only_change_keeps_descripcion_and_amounts(self):
        db.corregir_comida(self.conn, 1, self.cid_comida, ts_local=f"{DAY}T08:30:00", day=DAY,
                           descripcion="pollo con arroz")
        fila = self.conn.execute("SELECT ts_local, descripcion FROM comida WHERE id = ?",
                                 (self.cid_comida,)).fetchone()
        self.assertEqual(fila["ts_local"], f"{DAY}T08:30:00")
        self.assertEqual(fila["descripcion"], "pollo con arroz")
        rows = self.conn.execute("SELECT COUNT(*) FROM ingesta WHERE comida_id = ?",
                                 (self.cid_comida,)).fetchone()[0]
        self.assertEqual(rows, len(_CANTIDADES))

    def test_description_only_change(self):
        db.corregir_comida(self.conn, 1, self.cid_comida, ts_local=f"{DAY}T13:00:00", day=DAY,
                           descripcion="pollo con papa")
        fila = self.conn.execute("SELECT descripcion FROM comida WHERE id = ?", (self.cid_comida,)).fetchone()
        self.assertEqual(fila["descripcion"], "pollo con papa")

    def test_amounts_replaced_removes_one_adds_one_and_recalculates_both(self):
        nuevas = [{"metric_key": "protein_g", "amount": 40.0, "estimado": False},
                  {"metric_key": "fiber_g", "amount": 8.0, "estimado": True}]
        db.corregir_comida(self.conn, 1, self.cid_comida, ts_local=f"{DAY}T13:00:00", day=DAY,
                           descripcion="pollo con arroz", cantidades=nuevas)
        rows = {r["metric_key"]: r["amount"] for r in self.conn.execute(
            "SELECT metric_key, amount FROM ingesta WHERE comida_id = ?", (self.cid_comida,))}
        self.assertEqual(rows, {"protein_g": 40.0, "fiber_g": 8.0})
        kcal_total = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'kcal'", (DAY,)
        ).fetchone()
        self.assertIsNone(kcal_total)
        fiber_total = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'fiber_g'", (DAY,)
        ).fetchone()
        self.assertEqual(fiber_total["value"], 8.0)

    def test_day_moved_rederives_both_days(self):
        db.corregir_comida(self.conn, 1, self.cid_comida, ts_local=f"{OTHER_DAY}T13:00:00", day=OTHER_DAY,
                           descripcion="pollo con arroz")
        old_day_total = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'", (DAY,)
        ).fetchone()
        self.assertIsNone(old_day_total)
        new_day_total = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'",
            (OTHER_DAY,)
        ).fetchone()
        self.assertEqual(new_day_total["value"], 35.0)
        ingesta_day = self.conn.execute(
            "SELECT day FROM ingesta WHERE comida_id = ? LIMIT 1", (self.cid_comida,)
        ).fetchone()
        self.assertEqual(ingesta_day["day"], OTHER_DAY)

    def test_another_persons_meal_is_refused(self):
        with self.assertRaises(ValueError):
            db.corregir_comida(self.conn, 2, self.cid_comida, descripcion="otra cosa")

    def test_unknown_meal_is_refused(self):
        with self.assertRaises(ValueError):
            db.corregir_comida(self.conn, 1, 999999, descripcion="otra cosa")


# ---- corregir_comida tool: propose -> apply -> undo, drift, i18n ------------------------------

class CorregirComidaToolTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.comida_id = db.crear_comida(self.conn, 1, DAY, _CANTIDADES, "pollo con arroz", f"{DAY}T13:00:00")

    def _proponer(self, **extra):
        params = {"comida_id": self.comida_id}
        params.update(extra)
        return herramientas.ejecutar(self.conn, 1, "corregir_comida", params, "ninguno", self.cid)

    def test_propose_writes_nothing(self):
        before = self.conn.execute(
            "SELECT ts_local, descripcion FROM comida WHERE id = ?", (self.comida_id,)).fetchone()
        out = self._proponer(hora="08:00")
        self.assertEqual(out["estado"], "propuesta")
        after = self.conn.execute(
            "SELECT ts_local, descripcion FROM comida WHERE id = ?", (self.comida_id,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))

    def test_no_field_given_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer()

    def test_unknown_comida_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_comida", {"comida_id": 999999, "hora": "08:00"},
                                  "ninguno", self.cid)

    def test_another_persons_meal_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 2, "corregir_comida", {"comida_id": self.comida_id, "hora": "08:00"},
                                  "ninguno", _cid(self.conn, 2))

    def test_apply_changes_only_the_hora(self):
        out = self._proponer(hora="08:00")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        fila = self.conn.execute("SELECT ts_local, descripcion FROM comida WHERE id = ?",
                                 (self.comida_id,)).fetchone()
        self.assertEqual(fila["ts_local"], f"{DAY}T08:00:00")
        self.assertEqual(fila["descripcion"], "pollo con arroz")
        rows = self.conn.execute("SELECT COUNT(*) FROM ingesta WHERE comida_id = ?",
                                 (self.comida_id,)).fetchone()[0]
        self.assertEqual(rows, len(_CANTIDADES))

    def test_apply_replaces_half_the_amounts(self):
        nuevas = [{"metric_key": "protein_g", "amount": 35.0, "estimado": True},
                  {"metric_key": "fiber_g", "amount": 6.0, "estimado": False}]
        out = self._proponer(cantidades=nuevas)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        rows = {r["metric_key"]: r["amount"] for r in self.conn.execute(
            "SELECT metric_key, amount FROM ingesta WHERE comida_id = ?", (self.comida_id,))}
        self.assertEqual(rows, {"protein_g": 35.0, "fiber_g": 6.0})

    def test_undo_restores_the_exact_prior_state(self):
        out = self._proponer(hora="08:00", cantidades=[{"metric_key": "protein_g", "amount": 99.0}])
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        fila = self.conn.execute("SELECT ts_local FROM comida WHERE id = ?", (self.comida_id,)).fetchone()
        self.assertEqual(fila["ts_local"], f"{DAY}T13:00:00")
        rows = {r["metric_key"]: r["amount"] for r in self.conn.execute(
            "SELECT metric_key, amount FROM ingesta WHERE comida_id = ?", (self.comida_id,))}
        self.assertEqual(rows, {c["metric_key"]: c["amount"] for c in _CANTIDADES})

    def test_bad_amount_is_refused_with_nothing_written(self):
        before = self.conn.execute(
            "SELECT COUNT(*) FROM ingesta WHERE comida_id = ?", (self.comida_id,)).fetchone()[0]
        with self.assertRaises(herramientas.ToolError):
            self._proponer(cantidades=[{"metric_key": "protein_g", "amount": "mucho"}])
        after = self.conn.execute(
            "SELECT COUNT(*) FROM ingesta WHERE comida_id = ?", (self.comida_id,)).fetchone()[0]
        self.assertEqual(before, after)

    def test_zero_amount_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(cantidades=[{"metric_key": "protein_g", "amount": 0}])

    def test_empty_descripcion_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(descripcion="   ")

    def test_apply_refuses_when_the_meal_drifted_since_the_proposal(self):
        out = self._proponer(hora="08:00")
        db.corregir_comida(self.conn, 1, self.comida_id, descripcion="algo distinto", ts_local=f"{DAY}T13:00:00")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_apply_refuses_when_the_meal_was_deleted_since_the_proposal(self):
        out = self._proponer(hora="08:00")
        db.borrar_comida(self.conn, 1, self.comida_id)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_undo_refuses_when_the_meal_drifted_since_apply(self):
        out = self._proponer(hora="08:00")
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        db.corregir_comida(self.conn, 1, self.comida_id, descripcion="algo distinto", ts_local=f"{DAY}T08:00:00")
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")


# ---- card: one summary line, named by the meal's OLD time, built from what differs ------------

class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)
        self.comida_id = db.crear_comida(self.conn, 1, DAY, _CANTIDADES, "pollo con arroz", f"{DAY}T13:00:00")

    def _tarjeta(self, lang: str, **extra):
        params = {"comida_id": self.comida_id}
        params.update(extra)
        out = herramientas.ejecutar(self.conn, 1, "corregir_comida", params, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_card_names_the_old_time_es(self):
        t = self._tarjeta("es", hora="08:00")
        self.assertIn("13:00", t["despues"])
        self.assertIn("08:00", t["despues"])

    def test_card_names_the_old_time_en(self):
        t = self._tarjeta("en", hora="08:00")
        self.assertIn("13:00", t["despues"])
        self.assertIn("08:00", t["despues"])

    def test_card_names_a_new_descripcion(self):
        t = self._tarjeta("es", descripcion="pollo con papa")
        self.assertIn("pollo con papa", t["despues"])

    def test_card_names_an_added_and_a_removed_amount(self):
        t = self._tarjeta("es", cantidades=[{"metric_key": "protein_g", "amount": 35.0},
                                            {"metric_key": "fiber_g", "amount": 6.0}])
        self.assertIn("Fibra", t["despues"])
        self.assertIn("+", t["despues"])
        self.assertIn("Calorías", t["despues"])
        self.assertIn("-", t["despues"])

    def test_card_render_never_raises_on_a_malformed_despues(self):
        tarjeta = asistente._tarjeta_propuesta_segura(T("es"), self.defs, {
            "id": 999, "accion": "corregir_comida", "despues_json": {}, "antes_json": {},
            "estado": "propuesta", "expira_en": "2026-11-09T00:10:00+00:00", "aplicada_via": None,
        })
        self.assertIn("despues", tarjeta)


if __name__ == "__main__":
    unittest.main()
