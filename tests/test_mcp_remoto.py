"""BODY-MCP-REMOTO-SERVIDOR-1: the hosted MCP door (GET/POST /api/asistente/mcp/...), authenticated
only by a device key carrying the `asistente:mcp` scope. Invented demo data only, no real health
figures, no network.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, dispositivo, mcp, web
from body.agente import conversacion as agente_conversacion
from body.agente import propuestas as agente_propuestas


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "mcp_remoto.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


class _HostedTestCase(unittest.TestCase):
    """Person 1 ("Me", from db.init) and person 2 ("B"), hosted mode on so device-key routes are
    reachable -- same shape tests/test_dispositivo.py's own DispositivoHostedTestCase uses."""

    def setUp(self):
        self.conn = _reset_db(self)
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.client = TestClient(web.app, follow_redirects=False)


class LocalModeTest(unittest.TestCase):
    def test_both_routes_404_in_local_mode(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        client = TestClient(web.app, follow_redirects=False)
        self.assertEqual(client.get("/api/asistente/mcp/herramientas").status_code, 404)
        self.assertEqual(client.post("/api/asistente/mcp/llamar", json={}).status_code, 404)


class AuthTest(_HostedTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC de Carlos", {"asistente:mcp"})

    def test_no_header_401(self):
        r = self.client.get("/api/asistente/mcp/herramientas")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("www-authenticate"), "Bearer")

    def test_bad_key_401(self):
        r = self.client.get("/api/asistente/mcp/herramientas",
                             headers={"Authorization": "Bearer bdy_does-not-exist"})
        self.assertEqual(r.status_code, 401)

    def test_revoked_key_401(self):
        row = self.conn.execute("SELECT id FROM dispositivo WHERE person_id = 1").fetchone()
        dispositivo.revocar(self.conn, 1, row["id"])
        r = self.client.get("/api/asistente/mcp/herramientas",
                             headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 401)

    def test_key_without_scope_403(self):
        key_sin_alcance = dispositivo.crear(self.conn, 1, "sin alcance", {"vida:leer"})
        r = self.client.get("/api/asistente/mcp/herramientas",
                             headers={"Authorization": f"Bearer {key_sin_alcance}"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json(), {"error": "alcance"})

    def test_llamar_also_needs_the_scope(self):
        key_sin_alcance = dispositivo.crear(self.conn, 1, "sin alcance", {"vida:leer"})
        r = self.client.post("/api/asistente/mcp/llamar",
                              headers={"Authorization": f"Bearer {key_sin_alcance}"},
                              json={"nombre": "leer_ajustes", "argumentos": {}})
        self.assertEqual(r.status_code, 403)


class HerramientasTest(_HostedTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC de Carlos", {"asistente:mcp"})

    def test_list_matches_the_local_core_for_the_same_person(self):
        sesion_local = mcp._Sesion(self.conn, 1)
        esperado = mcp._lista_herramientas(sesion_local)
        nivel_esperado = mcp._nivel(sesion_local)

        r = self.client.get("/api/asistente/mcp/herramientas", headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["herramientas"], esperado)
        self.assertEqual(out["nivel"], nivel_esperado)
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_list_includes_the_persons_interface_language(self):
        # BODY-MCP-REMOTO-CLIENTE-1 ruling 3: the PC client has no DB of its own to read this
        # from, so the server names it.
        r = self.client.get("/api/asistente/mcp/herramientas", headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.json()["idioma"], "es")
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        r = self.client.get("/api/asistente/mcp/herramientas", headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.json()["idioma"], "en")

    def test_aplicar_mcp_on_adds_the_two_extra_tools(self):
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        r = self.client.get("/api/asistente/mcp/herramientas", headers={"Authorization": f"Bearer {self.key}"})
        nombres = {h["name"] for h in r.json()["herramientas"]}
        self.assertIn(mcp.NOMBRE_APLICAR, nombres)
        self.assertIn(mcp.NOMBRE_DESCARTAR, nombres)


class LlamarLecturaTest(_HostedTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC de Carlos", {"asistente:mcp"})

    def test_lectura_call_matches_the_local_core(self):
        sesion_local = mcp._Sesion(self.conn, 1)
        tipo, salida, aviso = mcp._llamar_herramienta(sesion_local, "leer_ajustes", {})

        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": "leer_ajustes", "argumentos": {}})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["tipo"], tipo)
        self.assertEqual(out["salida"], salida)
        self.assertEqual(out["aviso_reloj"], aviso)
        self.assertIsNone(out["conversacion_id"])

    def test_unknown_tool_is_the_generic_code(self):
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": "esto_no_existe", "argumentos": {}})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["error"], "herramienta_rechazada")
        # BODY-MCP-REMOTO-CLIENTE-1 ruling 3: carries the same unlocalized message the local
        # stdio door already shows verbatim for a plain ToolError (errores.texto_propio(e)).
        self.assertIn("herramienta desconocida", out["mensaje"])


class LlamarPropuestaTest(_HostedTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC de Carlos", {"asistente:mcp"})

    def test_a_proposal_creates_an_mcp_origin_conversation_the_web_panel_lists(self):
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": "proponer_idioma", "argumentos": {"lang": "en"}})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["tipo"], "propuesta")
        propuesta_id = out["salida"]["propuesta_id"]
        cid = out["conversacion_id"]
        self.assertIsNotNone(cid)
        self.assertEqual(agente_conversacion.origen_de(self.conn, cid), "mcp")
        row = self.conn.execute("SELECT conversacion_id FROM propuesta_agente WHERE id = ?",
                                (propuesta_id,)).fetchone()
        self.assertEqual(row["conversacion_id"], cid)

        from body import identity
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        r_panel = self.client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r_panel.status_code, 200)
        self.assertEqual(len(r_panel.json()["propuestas"]), 1)

    def test_a_second_call_with_no_conversacion_id_creates_a_separate_one(self):
        """Ruling 3: the conversation id is CLIENT-held -- this stateless route remembers nothing
        between calls, so a client that omits it again gets a brand-new conversation, never the
        first one back. `test_sending_the_conversacion_id_back_keeps_using_it` below is the real
        client's own shape: it must send the id back itself to reuse it."""
        r1 = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                               json={"nombre": "proponer_idioma", "argumentos": {"lang": "en"}})
        cid1 = r1.json()["conversacion_id"]
        r2 = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                               json={"nombre": "marcar_periodo",
                                     "argumentos": {"kind": "viaje", "start_day": "2020-01-01"}})
        cid2 = r2.json()["conversacion_id"]
        self.assertNotEqual(cid1, cid2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversacion_agente WHERE person_id = 1").fetchone()[0], 2)

    def test_sending_the_conversacion_id_back_keeps_using_it(self):
        r1 = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                               json={"nombre": "proponer_idioma", "argumentos": {"lang": "en"}})
        cid = r1.json()["conversacion_id"]
        r2 = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                               json={"nombre": "marcar_periodo",
                                     "argumentos": {"kind": "viaje", "start_day": "2020-01-01"},
                                     "conversacion_id": cid})
        self.assertEqual(r2.json()["conversacion_id"], cid)

    def test_a_conversacion_id_that_is_not_mcp_origin_is_refused(self):
        cid_en_app = agente_conversacion.crear(self.conn, 1)  # origen=None, the in-app chat's own
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": "marcar_periodo",
                                    "argumentos": {"kind": "viaje", "start_day": "2020-01-01"},
                                    "conversacion_id": cid_en_app})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"error": "conversacion_ajena"})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM propuesta_agente WHERE conversacion_id = ?",
                              (cid_en_app,)).fetchone()[0], 0)

    def test_a_conversacion_id_of_another_person_is_refused(self):
        cid_b = agente_conversacion.crear(self.conn, 2, origen="mcp")
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": "marcar_periodo",
                                    "argumentos": {"kind": "viaje", "start_day": "2020-01-01"},
                                    "conversacion_id": cid_b})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"error": "conversacion_ajena"})


class LlamarAplicarDescartarTest(_HostedTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC de Carlos", {"asistente:mcp"})

    def _crear_propuesta(self) -> int:
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": "proponer_idioma", "argumentos": {"lang": "en"}})
        return r.json()["salida"]["propuesta_id"]

    def test_aplicar_refused_while_the_setting_is_off(self):
        pid = self._crear_propuesta()
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": mcp.NOMBRE_APLICAR, "argumentos": {"propuesta_id": pid}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"error": "aplicar_apagado"})

    def test_aplicar_only_when_aplicar_mcp_is_on(self):
        pid = self._crear_propuesta()
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": mcp.NOMBRE_APLICAR, "argumentos": {"propuesta_id": pid}})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["tipo"], "aplicacion")
        self.assertEqual(out["salida"]["estado"], "aplicada")
        row = self.conn.execute("SELECT estado, aplicada_via FROM propuesta_agente WHERE id = ?",
                                (pid,)).fetchone()
        self.assertEqual((row["estado"], row["aplicada_via"]), ("aplicada", "mcp"))

    def test_aplicar_on_another_persons_proposal_is_refused(self):
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        cid_b = agente_conversacion.crear(self.conn, 2, origen="mcp")
        propuesta_b = agente_propuestas.crear(self.conn, 2, cid_b, "proponer_idioma", {"lang": "en"})
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": mcp.NOMBRE_APLICAR, "argumentos": {"propuesta_id": propuesta_b["id"]}})
        self.assertEqual(r.json(), {"error": "aplicar_no_propia"})
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                (propuesta_b["id"],)).fetchone()
        self.assertEqual(row["estado"], "propuesta")

    def test_aplicar_on_an_already_applied_proposal_is_no_vigente(self):
        pid = self._crear_propuesta()
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        agente_propuestas.aplicar(self.conn, 1, pid)
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": mcp.NOMBRE_APLICAR, "argumentos": {"propuesta_id": pid}})
        self.assertEqual(r.json(), {"error": "aplicar_no_vigente"})

    def test_aplicar_on_an_expired_proposal_names_the_raw_timestamp(self):
        pid = self._crear_propuesta()
        db.set_aplicar_mcp(self.conn, 1, True)
        vencido = "2026-11-10T08:30:00+00:00"
        self.conn.execute("UPDATE propuesta_agente SET expira_en = ? WHERE id = ?", (vencido, pid))
        self.conn.commit()
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": mcp.NOMBRE_APLICAR, "argumentos": {"propuesta_id": pid}})
        self.assertEqual(r.json(), {"error": "aplicar_expirada", "expira_en": vencido})
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["estado"], "expirada")

    def test_descartar_refused_while_the_setting_is_off(self):
        pid = self._crear_propuesta()
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": mcp.NOMBRE_DESCARTAR, "argumentos": {"propuesta_id": pid}})
        self.assertEqual(r.json(), {"error": "descartar_apagado"})

    def test_descartar_its_own_pending_proposal(self):
        pid = self._crear_propuesta()
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": mcp.NOMBRE_DESCARTAR, "argumentos": {"propuesta_id": pid}})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["tipo"], "descarte")
        self.assertEqual(out["salida"]["estado"], "descartada")

    def test_descartar_an_in_app_proposal_is_refused(self):
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        cid = agente_conversacion.crear(self.conn, 1)
        propuesta = agente_propuestas.crear(self.conn, 1, cid, "proponer_idioma", {"lang": "en"})
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              json={"nombre": mcp.NOMBRE_DESCARTAR, "argumentos": {"propuesta_id": propuesta["id"]}})
        self.assertEqual(r.json(), {"error": "aplicar_no_propia"})


class BodySizeTest(_HostedTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC de Carlos", {"asistente:mcp"})

    def test_over_64kb_413(self):
        body = ("x" * (mcp_remoto_max() + 1)).encode()
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              content=body)
        self.assertEqual(r.status_code, 413)

    def test_oversized_content_length_413_without_reading_the_body(self):
        headers = {"Authorization": f"Bearer {self.key}", "Content-Length": str(300 * 1024)}
        r = self.client.post("/api/asistente/mcp/llamar", headers=headers, content=b"{}")
        self.assertEqual(r.status_code, 413)

    def test_malformed_json_is_esquema_422(self):
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                              content=b"not json")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json(), {"error": "esquema"})


def mcp_remoto_max() -> int:
    from body.rutas import mcp_remoto
    return mcp_remoto.MAX_BODY_BYTES


class CapsTest(_HostedTestCase):
    """BODY-MCP-REMOTO-SERVIDOR-1 ruling 4: caps counted from the audit table, one shared budget
    across both doors. The cap check reads real wall-clock time (`_TopeDB`'s default reloj, like
    `db.registrar_uso_herramienta_externa`'s own write), so "the window has rolled past" is proven
    by aging the existing audit rows back in the DB directly, never by injecting a clock the audit
    write itself would not also honour."""

    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC de Carlos", {"asistente:mcp"})

    def _llamar(self, nombre: str, argumentos: dict):
        return self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key}"},
                                 json={"nombre": nombre, "argumentos": argumentos})

    def _envejecer_ventana(self, person_id: int = 1) -> None:
        viejo = (datetime.now(timezone.utc) - timedelta(minutes=mcp.LIMITES["ventana_min"] + 5)
                 ).isoformat(timespec="seconds")
        self.conn.execute("UPDATE uso_herramienta_externa SET llamada_en = ? WHERE person_id = ?",
                          (viejo, person_id))
        self.conn.commit()

    def test_lectura_cap_reached_then_allowed_again_after_the_window(self):
        tope = mcp.LIMITES["lecturas"]
        for _ in range(tope):
            r = self._llamar("leer_ajustes", {})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("error", r.json())
        r_over = self._llamar("leer_ajustes", {})
        out = r_over.json()
        self.assertEqual(out["error"], "limite")
        self.assertGreaterEqual(out["minutos"], 1)

        self._envejecer_ventana()
        r_again = self._llamar("leer_ajustes", {})
        self.assertNotIn("error", r_again.json())

    def test_local_calls_count_toward_the_same_remote_cap(self):
        """Ruling 4: ONE shared budget, not one per door -- a local stdio session's own successful
        reads already used up most of the window, so the remote door hits the cap sooner."""
        tope = mcp.LIMITES["lecturas"]
        sesion_local = mcp._Sesion(self.conn, 1)
        for _ in range(tope - 1):
            mcp._llamar_herramienta(sesion_local, "leer_ajustes", {})

        r = self._llamar("leer_ajustes", {})
        self.assertNotIn("error", r.json())
        r_over = self._llamar("leer_ajustes", {})
        self.assertEqual(r_over.json()["error"], "limite")

    def test_aplicaciones_cap_is_the_tighter_one(self):
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        tope = mcp.LIMITES["aplicaciones"]
        for _ in range(tope):
            pid = self._crear_propuesta()
            r = self._llamar(mcp.NOMBRE_APLICAR, {"propuesta_id": pid})
            self.assertNotIn("error", r.json())
        pid_over = self._crear_propuesta()
        r_over = self._llamar(mcp.NOMBRE_APLICAR, {"propuesta_id": pid_over})
        out = r_over.json()
        self.assertEqual(out["error"], "limite")
        self.assertGreaterEqual(out["minutos"], 1)

    def _crear_propuesta(self) -> int:
        r = self._llamar("marcar_periodo", {"kind": "viaje", "start_day": "2020-01-01"})
        return r.json()["salida"]["propuesta_id"]


class AislamientoEntrePersonasTest(_HostedTestCase):
    """Ruling 6: a key of person A can never read, propose, apply or discard for person B,
    including when A hands the route B's own conversacion_id."""

    def setUp(self):
        super().setUp()
        self.key_a = dispositivo.crear(self.conn, 1, "PC de A", {"asistente:mcp"})
        self.key_b = dispositivo.crear(self.conn, 2, "PC de B", {"asistente:mcp"})

    def test_herramientas_list_is_person_scoped(self):
        db.set_aplicar_mcp(self.conn, 2, True)
        self.conn.commit()
        r_a = self.client.get("/api/asistente/mcp/herramientas", headers={"Authorization": f"Bearer {self.key_a}"})
        nombres_a = {h["name"] for h in r_a.json()["herramientas"]}
        self.assertNotIn(mcp.NOMBRE_APLICAR, nombres_a)

    def test_a_cannot_apply_bs_proposal(self):
        db.set_aplicar_mcp(self.conn, 1, True)
        db.set_aplicar_mcp(self.conn, 2, True)
        self.conn.commit()
        cid_b = agente_conversacion.crear(self.conn, 2, origen="mcp")
        propuesta_b = agente_propuestas.crear(self.conn, 2, cid_b, "proponer_idioma", {"lang": "en"})
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key_a}"},
                              json={"nombre": mcp.NOMBRE_APLICAR, "argumentos": {"propuesta_id": propuesta_b["id"]}})
        self.assertEqual(r.json(), {"error": "aplicar_no_propia"})
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                (propuesta_b["id"],)).fetchone()
        self.assertEqual(row["estado"], "propuesta")

    def test_a_cannot_discard_bs_proposal(self):
        db.set_aplicar_mcp(self.conn, 1, True)
        db.set_aplicar_mcp(self.conn, 2, True)
        self.conn.commit()
        cid_b = agente_conversacion.crear(self.conn, 2, origen="mcp")
        propuesta_b = agente_propuestas.crear(self.conn, 2, cid_b, "proponer_idioma", {"lang": "en"})
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key_a}"},
                              json={"nombre": mcp.NOMBRE_DESCARTAR, "argumentos": {"propuesta_id": propuesta_b["id"]}})
        self.assertEqual(r.json(), {"error": "aplicar_no_propia"})
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                (propuesta_b["id"],)).fetchone()
        self.assertEqual(row["estado"], "propuesta")

    def test_a_passing_bs_conversacion_id_is_refused_and_creates_no_card_there(self):
        r_b = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key_b}"},
                                json={"nombre": "proponer_idioma", "argumentos": {"lang": "en"}})
        cid_b = r_b.json()["conversacion_id"]
        before = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE conversacion_id = ?", (cid_b,)).fetchone()[0]

        r_a = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key_a}"},
                                json={"nombre": "marcar_periodo",
                                      "argumentos": {"kind": "viaje", "start_day": "2020-01-01"},
                                      "conversacion_id": cid_b})
        self.assertEqual(r_a.json(), {"error": "conversacion_ajena"})
        after = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE conversacion_id = ?", (cid_b,)).fetchone()[0]
        self.assertEqual(before, after)

    def test_a_cannot_read_bs_figures_through_a_lectura_tool(self):
        db.set_metric(self.conn, "2026-01-01", "resting_hr", 187, "garmin", 2)
        self.conn.commit()
        r = self.client.post("/api/asistente/mcp/llamar", headers={"Authorization": f"Bearer {self.key_a}"},
                              json={"nombre": "leer_hoy", "argumentos": {}})
        self.assertNotIn("187", r.text)


if __name__ == "__main__":
    unittest.main()
