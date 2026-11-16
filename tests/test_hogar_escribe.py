"""BODY-HOGAR-ESCRIBE-1: the 'hogar' origin (ruling 1) and anotar_sesion (ruling 4). Invented demo
data only, never a real figure."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import anatomia, config, db, goals, horizonte, identity, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.agente.herramientas import ToolError

_EJERCICIOS = [{"ejercicio": "sentadilla", "series": 3, "categoria": anatomia.CATEGORIA_ENUM[0]}]


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "h.db"
    conn = db.connect()
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn, orig


def _cid(conn, person_id: int = 1, origen: str | None = None) -> int:
    return agente_conversacion.crear(conn, person_id, origen=origen)


class HogarOrigenTest(unittest.TestCase):
    """Ruling 1: 'hogar' is a valid origen/aplicada_via value, same CHECK-widening pattern as the
    existing 'mcp' one, with its own card label."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def test_conversacion_accepts_hogar_as_its_origen(self):
        cid = _cid(self.conn, origen="hogar")
        self.assertEqual(agente_conversacion.origen_de(self.conn, cid), "hogar")

    def test_marcar_aplicada_via_hogar_sets_the_column(self):
        cid = _cid(self.conn)
        registro = propuestas.crear(self.conn, 1, cid, "marcar_periodo",
                                    {"kind": "viaje", "start_day": "2021-01-01", "end_day": None, "note": None})
        propuestas.aplicar(self.conn, 1, registro["id"])
        db.marcar_aplicada_via(self.conn, registro["id"], "hogar")
        row = self.conn.execute("SELECT aplicada_via FROM propuesta_agente WHERE id = ?",
                                (registro["id"],)).fetchone()
        self.assertEqual(row["aplicada_via"], "hogar")

    def test_marcar_aplicada_via_mcp_is_unchanged(self):
        cid = _cid(self.conn)
        registro = propuestas.crear(self.conn, 1, cid, "marcar_periodo",
                                    {"kind": "viaje", "start_day": "2021-02-01", "end_day": None, "note": None})
        propuestas.aplicar(self.conn, 1, registro["id"])
        db.marcar_aplicada_via_mcp(self.conn, registro["id"])
        row = self.conn.execute("SELECT aplicada_via FROM propuesta_agente WHERE id = ?",
                                (registro["id"],)).fetchone()
        self.assertEqual(row["aplicada_via"], "mcp")

    def _tarjetas_de(self, cid: int) -> list:
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        client = TestClient(web.app, follow_redirects=False)
        r = client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r.status_code, 200)
        return r.json()["propuestas"]

    def test_card_label_from_hogar_when_not_yet_applied(self):
        cid = _cid(self.conn, origen="hogar")
        propuestas.crear(self.conn, 1, cid, "marcar_periodo",
                         {"kind": "viaje", "start_day": "2021-03-01", "end_day": None, "note": None})
        tarjetas = self._tarjetas_de(cid)
        self.assertEqual(tarjetas[0]["origen_etiqueta"], "Desde Hogar")

    def test_card_label_applied_from_hogar(self):
        cid = _cid(self.conn, origen="hogar")
        registro = propuestas.crear(self.conn, 1, cid, "marcar_periodo",
                                    {"kind": "viaje", "start_day": "2021-04-01", "end_day": None, "note": None})
        propuestas.aplicar(self.conn, 1, registro["id"])
        db.marcar_aplicada_via(self.conn, registro["id"], "hogar")
        tarjetas = self._tarjetas_de(cid)
        self.assertEqual(tarjetas[0]["origen_etiqueta"], "Aplicado desde Hogar")


class AnotarSesionTest(unittest.TestCase):
    """Ruling 4: anotar_sesion(plan_id, nota, hecha?) -- a note on an existing planned session."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.horizon = goals.propose(self.conn, 1, "Plan de prueba", "2026-10-05", kind="milestone",
                                     deadline="2027-01-04")
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "planear_entreno", {
            "day": "2026-11-10", "titulo": "Sesión de piernas", "ejercicios": _EJERCICIOS,
        }, "ninguno", cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        self.plan_id = self.conn.execute(
            "SELECT id FROM plan WHERE person_id = 1 AND title = 'Sesión de piernas'").fetchone()["id"]

    def _anotar(self, plan_id: int, nota: str, hecha: bool | None = None, person_id: int = 1) -> dict:
        cid = _cid(self.conn, person_id)
        args = {"plan_id": plan_id, "nota": nota}
        if hecha is not None:
            args["hecha"] = hecha
        return herramientas.ejecutar(self.conn, person_id, "anotar_sesion", args, "ninguno", cid)

    def test_apply_note_only_leaves_status_unchanged(self):
        out = self._anotar(self.plan_id, "Me costó la última serie")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT status, nota_sesion, nota_sesion_en FROM plan WHERE id = ?",
                                (self.plan_id,)).fetchone()
        self.assertEqual(row["status"], "proposed")
        self.assertEqual(row["nota_sesion"], "Me costó la última serie")
        self.assertIsNotNone(row["nota_sesion_en"])

    def test_apply_with_hecha_marks_done_and_counts_once_in_sesiones_fuerza(self):
        out = self._anotar(self.plan_id, "La hice completa", hecha=True)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT status FROM plan WHERE id = ?", (self.plan_id,)).fetchone()
        self.assertEqual(row["status"], "done")

        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-09", "sesiones_fuerza", "al_menos", 1)
        semana = horizonte.semana(self.conn, self.horizon, 1, "2026-11-09", hoy=None)
        linea = semana["lineas"][0]
        self.assertEqual(linea["real"], 1)

    def test_undo_restores_note_timestamp_and_status_exactly(self):
        out = self._anotar(self.plan_id, "La hice completa", hecha=True)
        pid = out["propuesta_id"]
        propuestas.aplicar(self.conn, 1, pid)
        propuestas.deshacer(self.conn, 1, pid)
        row = self.conn.execute("SELECT status, nota_sesion, nota_sesion_en FROM plan WHERE id = ?",
                                (self.plan_id,)).fetchone()
        self.assertEqual(row["status"], "proposed")
        self.assertIsNone(row["nota_sesion"])
        self.assertIsNone(row["nota_sesion_en"])

    def test_undo_after_a_note_only_apply_restores_the_prior_empty_state(self):
        out = self._anotar(self.plan_id, "Primera nota")
        pid = out["propuesta_id"]
        propuestas.aplicar(self.conn, 1, pid)
        propuestas.deshacer(self.conn, 1, pid)
        row = self.conn.execute("SELECT status, nota_sesion, nota_sesion_en FROM plan WHERE id = ?",
                                (self.plan_id,)).fetchone()
        self.assertIsNone(row["nota_sesion"])
        self.assertIsNone(row["nota_sesion_en"])
        self.assertEqual(row["status"], "proposed")

    def test_another_persons_plan_id_is_refused_by_name(self):
        with self.assertRaisesRegex(ToolError, str(self.plan_id)):
            self._anotar(self.plan_id, "No es mía", person_id=2)

    def test_drift_refuses_when_the_plan_changed_since_the_proposal(self):
        out = self._anotar(self.plan_id, "Nota original")
        db.anotar_sesion_plan(self.conn, 1, self.plan_id, "otra nota distinta",
                              "2020-05-11T00:00:00+00:00", "proposed")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")


if __name__ == "__main__":
    unittest.main()
