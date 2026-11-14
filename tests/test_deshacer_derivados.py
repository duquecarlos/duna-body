"""BODY-DESHACER-DERIVADOS-1: an undo that deletes a row must also remove what was DERIVED from
it, in the same transaction -- proven here for `registrar_entreno`'s manual case, whose undo
deletes the session's own `activity` row while the hosted pull's own recompute
(`body/recuperacion.py`'s `recalcular`) may already have written a `recuperacion_actividad` row
for it (a FOREIGN KEY to `activity`). Invented demo data only, no real person.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, recuperacion
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "p.db"
    conn = db.connect()
    db.init(conn)
    conn.commit()
    return conn, orig


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


class DeshacerRegistrarEntrenoManualTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def _aplicar_sesion_manual(self) -> tuple[int, str]:
        """Applies a manual registrar_entreno proposal, then computes its own
        recuperacion_actividad row through the SAME module the hosted pull uses
        (`recuperacion.recalcular`) -- never a hand INSERT that could drift from what that module
        actually writes. Returns (propuesta_id, the manual session's own source_id)."""
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
            "day": "2026-11-10", "titulo": "Pierna en casa",
            "series": [{"ejercicio": "sentadilla", "reps": 10, "peso_kg": 40.0}],
        }, "ninguno", cid)
        propuesta_id = out["propuesta_id"]
        resultado = propuestas.aplicar(self.conn, 1, propuesta_id)
        self.assertEqual(resultado["estado"], "aplicada")
        self.conn.commit()
        recuperacion.recalcular(self.conn, 1)
        row = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()
        source_id = json.loads(row["despues_json"])["source_id"]
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM recuperacion_actividad WHERE person_id = 1 AND activity_source = "
                "'manual' AND activity_source_id = ?", (source_id,)).fetchone()[0], 1,
            "recalcular() should have written exactly one recuperacion_actividad row for the session")
        return propuesta_id, source_id

    def test_deshacer_removes_the_session_and_its_derived_recovery_row(self):
        """Red on main (unpatched `_deshacer_registrar_entreno`): `propuestas.deshacer` raises
        `sqlite3.IntegrityError` here, because the undo deletes `activity` before its own derived
        `recuperacion_actividad` row, which still carries a FOREIGN KEY to it. Green on this
        branch: the undo removes the derived row first and the whole session goes cleanly."""
        propuesta_id, source_id = self._aplicar_sesion_manual()
        resultado = propuestas.deshacer(self.conn, 1, propuesta_id)
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity WHERE person_id = 1 AND source = 'manual' AND source_id = ?",
                (source_id,)).fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM workout_set WHERE person_id = 1 AND activity_source = 'manual' "
                "AND activity_source_id = ?", (source_id,)).fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM recuperacion_actividad WHERE person_id = 1 AND activity_source = "
                "'manual' AND activity_source_id = ?", (source_id,)).fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT estado FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()[0],
            "deshecha")

    def test_deshacer_unlinks_a_watch_activity_that_pointed_at_the_deleted_session(self):
        """BODY-ENTRENO-VINCULO-1's own vinculo_manual, left pointing at a session that no longer
        exists, is unlinked -- never a dangling TEXT reference, and never the watch's own row
        deleted alongside it."""
        propuesta_id, source_id = self._aplicar_sesion_manual()
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source, "
            "vinculo_manual) VALUES (1, 'g1', '2026-11-10', '2026-11-10T07:00:00', 'strength_training', "
            "NULL, 'garmin', ?)", (source_id,))
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, propuesta_id)
        self.assertEqual(resultado["estado"], "deshecha")
        fila = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND "
            "source_id = 'g1'").fetchone()
        self.assertIsNotNone(fila, "the watch's own activity must not be deleted")
        self.assertIsNone(fila["vinculo_manual"])

    def test_the_mark_stays_aplicada_when_escribir_deshacer_raises(self):
        """Ruling 2: `_marcar` and `escribir_deshacer` share one transaction -- a raise from the
        undo writer, for ANY reason, leaves the proposal 'aplicada', never 'deshecha' with only
        part of its effect undone. Forced independently of ruling 1's own fix, by swapping in a
        writer that always raises for this one action."""
        propuesta_id, source_id = self._aplicar_sesion_manual()
        entrada = propuestas.TABLA["registrar_entreno"]

        def _siempre_falla(conn, row):
            raise RuntimeError("boom")

        parche = propuestas._Accion(entrada.drift_aplicar, entrada.drift_deshacer,
                                     entrada.escribir_aplicacion, _siempre_falla)
        with mock.patch.dict(propuestas.TABLA, {"registrar_entreno": parche}):
            with self.assertRaises(RuntimeError):
                propuestas.deshacer(self.conn, 1, propuesta_id)
        self.assertEqual(
            self.conn.execute(
                "SELECT estado FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()[0],
            "aplicada")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity WHERE person_id = 1 AND source = 'manual' AND source_id = ?",
                (source_id,)).fetchone()[0], 1, "nothing should have been partially removed either")


if __name__ == "__main__":
    unittest.main()
