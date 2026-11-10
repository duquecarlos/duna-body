"""BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 rulings 1-7: a reversible apply path for the
local MCP server (body/mcp.py), behind a per-person setting OFF by default and changed only
through Body's own /settings POST. Invented demo data only, no real health figures, no network, no
subprocess.
"""
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, mcp, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "mcp_aplicar.db"
    conn = db.connect()
    db.init(conn)
    conn.commit()
    return conn, orig


def _linea(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _respuestas(salida: io.StringIO) -> list[dict]:
    return [json.loads(l) for l in salida.getvalue().splitlines() if l.strip()]


class _McpAplicarTestCase(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.person_id = db.local_person_id(self.conn)

    def _ejecutar(self, mensajes: list[dict], reloj=None):
        entrada = io.StringIO("".join(_linea(m) for m in mensajes))
        salida = io.StringIO()
        sesion = mcp.ejecutar(entrada, salida, self.conn, self.person_id, reloj=reloj)
        return sesion, _respuestas(salida)

    def _crear_propuesta_mcp(self, start_day: str = "2020-01-01") -> tuple:
        """An mcp-origin pending proposal, as if this same outside caller had made it with
        another tool. Returns (conversacion_id, propuesta_id)."""
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "marcar_periodo", "arguments": {"kind": "viaje", "start_day": start_day}}},
        ])
        salida = json.loads(respuestas[0]["result"]["content"][-1]["text"])
        pid = salida["propuesta_id"]
        cid = self.conn.execute("SELECT conversacion_id FROM propuesta_agente WHERE id = ?",
                                (pid,)).fetchone()["conversacion_id"]
        return cid, pid

    def _crear_propuesta_en_app(self) -> int:
        """A proposal the IN-APP assistant made (origen IS NULL), never aplicar_propuesta's to touch."""
        from body.agente import propuestas as agente_propuestas
        cid = agente_conversacion.crear(self.conn, self.person_id)
        registro = agente_propuestas.crear(self.conn, self.person_id, cid, "marcar_periodo",
                                           {"kind": "viaje", "start_day": "2021-01-01", "end_day": None,
                                            "note": None})
        return registro["id"]


class OffByDefaultTest(_McpAplicarTestCase):
    def test_tool_is_absent_from_tools_list(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in respuestas[0]["result"]["tools"]}
        self.assertNotIn(mcp.NOMBRE_APLICAR, nombres)

    def test_calling_it_anyway_is_refused(self):
        _, pid = self._crear_propuesta_mcp()
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        r = respuestas[0]["result"]
        self.assertTrue(r["isError"])
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["estado"], "propuesta")


class OnTest(_McpAplicarTestCase):
    def setUp(self):
        super().setUp()
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        self.conn.commit()

    def test_tool_is_listed_once_on(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        tools = {t["name"]: t for t in respuestas[0]["result"]["tools"]}
        self.assertIn(mcp.NOMBRE_APLICAR, tools)
        self.assertTrue(tools[mcp.NOMBRE_APLICAR]["description"])
        self.assertEqual(tools[mcp.NOMBRE_APLICAR]["inputSchema"]["required"], ["propuesta_id"])

    def test_applies_its_own_mcp_origin_proposal(self):
        cid, pid = self._crear_propuesta_mcp()
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        contenido = r["content"]
        self.assertEqual(len(contenido), 2)
        self.assertIn("Hoy", contenido[0]["text"])
        salida = json.loads(contenido[-1]["text"])
        self.assertEqual(salida["estado"], "aplicada")
        row = self.conn.execute("SELECT estado, aplicada_via FROM propuesta_agente WHERE id = ?",
                                (pid,)).fetchone()
        self.assertEqual(row["estado"], "aplicada")
        self.assertEqual(row["aplicada_via"], "mcp")
        # The setting itself is never flipped by the apply call (ruling 1: only the web POST may).
        self.assertTrue(db.get_aplicar_mcp(self.conn, self.person_id))

    def test_applying_writes_the_existing_audit_row(self):
        _, pid = self._crear_propuesta_mcp()
        self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        rows = self.conn.execute(
            "SELECT herramienta, caller FROM uso_herramienta_externa WHERE person_id = ? ORDER BY id DESC LIMIT 1",
            (self.person_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["herramienta"], mcp.NOMBRE_APLICAR)
        self.assertEqual(rows[0]["caller"], mcp.CALLER)

    def test_deshacer_still_works_on_it_in_body(self):
        from body.agente import propuestas as agente_propuestas
        _, pid = self._crear_propuesta_mcp()
        self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        resultado = agente_propuestas.deshacer(self.conn, self.person_id, pid)
        self.assertEqual(resultado["estado"], "deshecha")

    def test_refuses_an_in_app_proposal(self):
        pid = self._crear_propuesta_en_app()
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        self.assertTrue(respuestas[0]["result"]["isError"])
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["estado"], "propuesta")

    def test_refuses_a_non_pending_proposal(self):
        _, pid = self._crear_propuesta_mcp()
        from body.agente import propuestas as agente_propuestas
        agente_propuestas.descartar(self.conn, self.person_id, pid)
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        self.assertTrue(respuestas[0]["result"]["isError"])

    def test_refuses_another_persons_proposal(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        cid_b = agente_conversacion.crear(self.conn, 2, origen="mcp")
        from body.agente import propuestas as agente_propuestas
        registro_b = agente_propuestas.crear(self.conn, 2, cid_b, "marcar_periodo",
                                             {"kind": "viaje", "start_day": "2022-01-01", "end_day": None,
                                              "note": None})
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": registro_b["id"]}}},
        ])
        self.assertTrue(respuestas[0]["result"]["isError"])
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                (registro_b["id"],)).fetchone()
        self.assertEqual(row["estado"], "propuesta")

    def test_card_label_says_applied_by_the_personal_assistant(self):
        cid, pid = self._crear_propuesta_mcp()
        self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        client = TestClient(web.app, follow_redirects=False)
        r = client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r.status_code, 200)
        tarjetas = r.json()["propuestas"]
        self.assertEqual(tarjetas[0]["origen_etiqueta"], "Aplicado por tu asistente personal")

    def test_cap_reached_is_a_tool_error(self):
        tope = mcp.LIMITES["aplicaciones"]
        ids = [self._crear_propuesta_mcp(start_day=f"2020-01-{i + 1:02d}")[1] for i in range(tope + 1)]
        mensajes = [
            {"jsonrpc": "2.0", "id": i, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}}
            for i, pid in enumerate(ids)
        ]
        _, respuestas = self._ejecutar(mensajes)
        for i in range(tope):
            self.assertNotIn("isError", respuestas[i]["result"], f"apply {i} should not have errored yet")
        self.assertTrue(respuestas[tope]["result"]["isError"])
        self.assertIn("min", respuestas[tope]["result"]["content"][0]["text"])


class TurningOffMidSessionTest(_McpAplicarTestCase):
    def test_hides_the_tool_on_the_next_tools_list(self):
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        self.conn.commit()
        entrada = io.StringIO(_linea({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        salida = io.StringIO()
        sesion = mcp.ejecutar(entrada, salida, self.conn, self.person_id)
        primero = _respuestas(salida)[0]["result"]["tools"]
        self.assertIn(mcp.NOMBRE_APLICAR, {t["name"] for t in primero})

        db.set_aplicar_mcp(self.conn, self.person_id, False)
        self.conn.commit()
        respuesta = mcp._procesar_mensaje(sesion, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        segundo = respuesta["result"]["tools"]
        self.assertNotIn(mcp.NOMBRE_APLICAR, {t["name"] for t in segundo})


class VentanaRotanteAplicacionesTest(_McpAplicarTestCase):
    """BODY-MCP-APLICAR-1 ruling 6: the cap bounds a RATE, not a session -- past the window, the
    same long-lived session may apply again, with an injectable clock so the test needs no
    sleeping (same shape tests/test_mcp.py's own VentanaRotanteTest already uses)."""

    def test_allowed_again_after_the_window(self):
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        self.conn.commit()
        ahora = {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}

        def reloj():
            return ahora["t"]

        tope = mcp.LIMITES["aplicaciones"]
        ids = [self._crear_propuesta_mcp(start_day=f"2020-02-{i + 1:02d}")[1] for i in range(tope + 1)]
        entrada = io.StringIO("".join(
            _linea({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}})
            for i, pid in enumerate(ids)
        ))
        salida = io.StringIO()
        sesion = mcp.ejecutar(entrada, salida, self.conn, self.person_id, reloj=reloj)
        respuestas = _respuestas(salida)
        for i in range(tope):
            self.assertNotIn("isError", respuestas[i]["result"])
        self.assertTrue(respuestas[tope]["result"]["isError"])

        # Past the window: the one still-pending proposal (the refused call above was never
        # applied) can now be applied on this same session.
        ahora["t"] += timedelta(minutes=mcp.LIMITES["ventana_min"] + 1)
        respuesta = mcp._procesar_mensaje(sesion, {
            "jsonrpc": "2.0", "id": 999, "method": "tools/call",
            "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": ids[-1]}}})
        self.assertNotIn("isError", respuesta["result"])


if __name__ == "__main__":
    unittest.main()
