"""BODY-HOGAR-ESCRIBE-1: the 'hogar' origin (ruling 1) and anotar_sesion (ruling 4). Invented demo
data only, never a real figure."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import anatomia, comidas, config, db, goals, horizonte, identity, web
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


class MomentosComidaTest(unittest.TestCase):
    """Ruling 2: defaults when a person has no rows of their own; their own rows win once they
    have any; momento_de at each boundary hour."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def test_defaults_when_no_rows_of_their_own(self):
        claves = [m["clave"] for m in comidas.momentos(self.conn, 1)]
        self.assertEqual(claves, ["desayuno", "almuerzo", "merienda", "cena"])

    def test_own_rows_win_over_the_defaults(self):
        self.conn.execute(
            "INSERT INTO momento_comida (person_id, clave, desde_hora, hasta_hora, esperado, orden) "
            "VALUES (1, 'brunch', 9, 12, 1, 1)")
        self.conn.commit()
        claves = [m["clave"] for m in comidas.momentos(self.conn, 1)]
        self.assertEqual(claves, ["brunch"])

    def test_another_persons_rows_never_affect_this_one(self):
        self.conn.execute(
            "INSERT INTO momento_comida (person_id, clave, desde_hora, hasta_hora, esperado, orden) "
            "VALUES (2, 'brunch', 9, 12, 1, 1)")
        self.conn.commit()
        claves = [m["clave"] for m in comidas.momentos(self.conn, 1)]
        self.assertEqual(claves, ["desayuno", "almuerzo", "merienda", "cena"])

    def test_momento_de_at_each_boundary_hour(self):
        casos = [
            ("2026-11-10T04:59:00", None),
            ("2026-11-10T05:00:00", "desayuno"),
            ("2026-11-10T10:59:00", "desayuno"),
            ("2026-11-10T11:00:00", "almuerzo"),
            ("2026-11-10T15:59:00", "almuerzo"),
            ("2026-11-10T16:00:00", "merienda"),
            ("2026-11-10T18:59:00", "merienda"),
            ("2026-11-10T19:00:00", "cena"),
            ("2026-11-10T22:59:00", "cena"),
            ("2026-11-10T23:00:00", None),
        ]
        for ts_local, esperado in casos:
            with self.subTest(ts_local=ts_local):
                self.assertEqual(comidas.momento_de(self.conn, 1, ts_local), esperado)

    def test_momento_de_with_no_ts_local_is_none(self):
        self.assertIsNone(comidas.momento_de(self.conn, 1, None))


class RegistrarComidaMomentoTest(unittest.TestCase):
    """Ruling 3: registrar_comida's optional momento argument, stored on apply -- absent -> null
    and derived from ts_local as before; a clave the person does not have is refused by name."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def _registrar(self, person_id: int = 1, **kwargs) -> dict:
        cid = _cid(self.conn, person_id)
        args = {"descripcion": "Pollo con arroz",
                "cantidades": [{"metric_key": "kcal", "amount": 500.0}]}
        args.update(kwargs)
        return herramientas.ejecutar(self.conn, person_id, "registrar_comida", args, "ninguno", cid)

    def test_absent_momento_is_stored_as_null(self):
        out = self._registrar(day="2026-11-10", hora="20:00")
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        row = self.conn.execute("SELECT momento FROM comida WHERE person_id = 1").fetchone()
        self.assertIsNone(row["momento"])

    def test_given_momento_is_stored(self):
        out = self._registrar(day="2026-11-10", hora="20:00", momento="cena")
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        row = self.conn.execute("SELECT momento FROM comida WHERE person_id = 1").fetchone()
        self.assertEqual(row["momento"], "cena")

    def test_a_custom_momento_the_person_has_is_accepted(self):
        self.conn.execute(
            "INSERT INTO momento_comida (person_id, clave, desde_hora, hasta_hora, esperado, orden) "
            "VALUES (1, 'brunch', 9, 12, 1, 1)")
        self.conn.commit()
        out = self._registrar(momento="brunch")
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        row = self.conn.execute("SELECT momento FROM comida WHERE person_id = 1").fetchone()
        self.assertEqual(row["momento"], "brunch")

    def test_a_clave_the_person_does_not_have_is_refused_by_name(self):
        with self.assertRaisesRegex(ToolError, "brunch"):
            self._registrar(momento="brunch")

    def test_undo_removes_the_row_with_its_momento(self):
        out = self._registrar(momento="cena")
        pid = out["propuesta_id"]
        propuestas.aplicar(self.conn, 1, pid)
        propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM comida WHERE person_id = 1").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
