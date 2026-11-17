"""BODY-HOGAR-ESCRIBE-2 ruling 2: Hogar's own write routes (GET /api/hogar/herramientas, POST
/api/hogar/registrar), gated by the separate `hogar:escribir` device-key scope. Synthetic data
only, no real health figures.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, dispositivo, web
from body.agente import conversacion as agente_conversacion
from body.agente import propuestas as agente_propuestas
from body.rutas import mcp_remoto


def _reset_db(tc: unittest.TestCase, name: str):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)
    return conn


class DeviceRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "escribe_rutas.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.client = TestClient(web.app, follow_redirects=False)


# ---- GET /api/hogar/herramientas -----------------------------------------------------------------

class ApiHogarHerramientasAuthTest(DeviceRoutesTestCase):
    def test_no_header_401(self):
        r = self.client.get("/api/hogar/herramientas")
        self.assertEqual(r.status_code, 401)

    def test_hogar_leer_only_key_403(self):
        key = dispositivo.crear(self.conn, 1, "PC", {"hogar:leer"})
        r = self.client.get("/api/hogar/herramientas", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 403)

    def test_revoked_key_401(self):
        key = dispositivo.crear(self.conn, 1, "PC", {"hogar:escribir"})
        row = self.conn.execute("SELECT id FROM dispositivo WHERE person_id = 1").fetchone()
        dispositivo.revocar(self.conn, 1, row["id"])
        r = self.client.get("/api/hogar/herramientas", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 401)


class ApiHogarHerramientasTest(DeviceRoutesTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC", {"hogar:escribir"})

    def test_exactly_two_tools_no_store(self):
        r = self.client.get("/api/hogar/herramientas", headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        out = r.json()
        self.assertEqual(set(out.keys()), {"herramientas"})
        self.assertEqual({h["name"] for h in out["herramientas"]}, {"registrar_comida", "anotar_sesion"})

    def test_schemas_are_exactly_tools_lists_own_schemas(self):
        r = self.client.get("/api/hogar/herramientas", headers={"Authorization": f"Bearer {self.key}"})
        out = r.json()
        idioma = db.person(self.conn, 1)["language"]
        todas = mcp_remoto.listar_herramientas(self.conn, 1, idioma, puede_aplicar=False)
        por_nombre = {h["name"]: h for h in todas["herramientas"]}
        for h in out["herramientas"]:
            self.assertEqual(h, por_nombre[h["name"]])
            self.assertEqual(set(h.keys()), {"name", "description", "inputSchema"})


# ---- POST /api/hogar/registrar -------------------------------------------------------------------

class ApiHogarRegistrarAuthTest(DeviceRoutesTestCase):
    def _post(self, key=None):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return self.client.post("/api/hogar/registrar", json={"herramienta": "registrar_comida", "argumentos": {}},
                                 headers=headers)

    def test_no_header_401(self):
        self.assertEqual(self._post().status_code, 401)

    def test_hogar_leer_only_key_403(self):
        key = dispositivo.crear(self.conn, 1, "PC", {"hogar:leer"})
        self.assertEqual(self._post(key).status_code, 403)

    def test_revoked_key_401(self):
        key = dispositivo.crear(self.conn, 1, "PC", {"hogar:escribir"})
        row = self.conn.execute("SELECT id FROM dispositivo WHERE person_id = 1").fetchone()
        dispositivo.revocar(self.conn, 1, row["id"])
        self.assertEqual(self._post(key).status_code, 401)


class ApiHogarRegistrarTest(DeviceRoutesTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC", {"hogar:escribir"})

    def _post(self, body: dict):
        return self.client.post("/api/hogar/registrar", json=body,
                                 headers={"Authorization": f"Bearer {self.key}"})

    def test_registrar_comida_applies_directly_with_hogar_origin_and_is_undoable(self):
        r = self._post({"herramienta": "registrar_comida",
                         "argumentos": {"descripcion": "Arroz con pollo",
                                        "cantidades": [{"metric_key": "protein_g", "amount": 35}]}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        out = r.json()
        self.assertTrue(out["ok"])
        propuesta_id = out["propuesta_id"]
        self.assertEqual(out["resultado"]["propuesta_id"], propuesta_id)
        self.assertEqual(out["resultado"]["estado"], "aplicada")

        row = self.conn.execute(
            "SELECT aplicada_via, conversacion_id FROM propuesta_agente WHERE id = ?", (propuesta_id,)
        ).fetchone()
        self.assertEqual(row["aplicada_via"], "hogar")
        self.assertEqual(agente_conversacion.origen_de(self.conn, row["conversacion_id"]), "hogar")

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1").fetchone()["n"], 1)

        resultado = agente_propuestas.deshacer(self.conn, 1, propuesta_id)
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1").fetchone()["n"], 0)

    def test_anotar_sesion_with_another_persons_plan_id_is_refused_by_name(self):
        self.conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status) VALUES "
            "(2, '2026-11-15', '18:00', 'Sesión de B', 'proposed')")
        self.conn.commit()
        plan_id = self.conn.execute("SELECT id FROM plan WHERE person_id = 2").fetchone()["id"]
        r = self._post({"herramienta": "anotar_sesion", "argumentos": {"plan_id": plan_id, "nota": "No es mía"}})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertIn(str(plan_id), out["error"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente WHERE person_id = 1").fetchone()["n"], 0)

    def test_unknown_tool_name_404(self):
        r = self._post({"herramienta": "leer_hoy", "argumentos": {}})
        self.assertEqual(r.status_code, 404)

    def test_aplicar_propuesta_tool_name_404(self):
        # never offered to Hogar even though it exists in the shared registry
        r = self._post({"herramienta": "aplicar_propuesta", "argumentos": {"propuesta_id": 1}})
        self.assertEqual(r.status_code, 404)

    def test_malformed_json_body_400(self):
        r = self.client.post("/api/hogar/registrar", content=b"not json",
                              headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 400)

    def test_body_not_an_object_400(self):
        r = self.client.post("/api/hogar/registrar", content=b"[1, 2, 3]",
                              headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 400)

    def test_two_people_are_two_separate_calls_under_their_own_keys(self):
        key_b = dispositivo.crear(self.conn, 2, "PC de B", {"hogar:escribir"})
        r_a = self._post({"herramienta": "registrar_comida",
                           "argumentos": {"descripcion": "A", "cantidades": [{"metric_key": "protein_g",
                                                                               "amount": 10}]}})
        r_b = self.client.post(
            "/api/hogar/registrar", json={"herramienta": "registrar_comida",
                                           "argumentos": {"descripcion": "B", "cantidades": [
                                               {"metric_key": "protein_g", "amount": 20}]}},
            headers={"Authorization": f"Bearer {key_b}"})
        self.assertTrue(r_a.json()["ok"])
        self.assertTrue(r_b.json()["ok"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1").fetchone()["n"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM comida WHERE person_id = 2").fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main()
