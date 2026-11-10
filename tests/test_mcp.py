"""BODY-MCP-LOCAL-1: the local MCP server over stdio, driven in-process with a fake stdin/stdout.
Invented demo data only, no real health figures, no network, no subprocess.
"""
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, mcp, web
from body.agente import ajustes as agente_ajustes
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "mcp.db"
    conn = db.connect()
    db.init(conn)
    conn.commit()
    return conn, orig


def _linea(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _respuestas(salida: io.StringIO) -> list[dict]:
    return [json.loads(l) for l in salida.getvalue().splitlines() if l.strip()]


class _McpTestCase(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.person_id = db.local_person_id(self.conn)

    def _ejecutar(self, mensajes: list[dict]):
        entrada = io.StringIO("".join(_linea(m) for m in mensajes))
        salida = io.StringIO()
        sesion = mcp.ejecutar(entrada, salida, self.conn, self.person_id)
        return sesion, _respuestas(salida)


class InitializeTest(_McpTestCase):
    def test_initialize_answers_protocol_version_and_server_info(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
        self.assertEqual(len(respuestas), 1)
        r = respuestas[0]["result"]
        self.assertEqual(r["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertIn("name", r["serverInfo"])
        self.assertIn("tools", r["capabilities"])

    def test_notifications_initialized_gets_no_reply(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(respuestas, [])


class ToolsListTest(_McpTestCase):
    def test_list_matches_the_in_app_level_and_has_no_apply_undo_delete(self):
        herramientas.fijar_turnos_actual(db.trabaja_turnos_si(self.conn, self.person_id))
        nivel = agente_ajustes.nivel_datos(self.conn, self.person_id)
        esperado = {h["nombre"] for h in herramientas.disponibles(nivel)}
        self.assertGreater(len(esperado), 0)

        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        tools = respuestas[0]["result"]["tools"]
        nombres = {t["name"] for t in tools}
        self.assertEqual(nombres, esperado)
        for prohibido in ("aplicar", "descartar", "deshacer", "undo", "delete", "borrar", "apply"):
            self.assertNotIn(prohibido, nombres)
        for t in tools:
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)
            self.assertTrue(t["description"])


class ToolsCallTest(_McpTestCase):
    def test_lectura_call_answers_with_content_and_no_error(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        salida = json.loads(r["content"][0]["text"])
        self.assertIn("nivel_datos", salida)

    def test_propuesta_call_creates_the_conversation_second_proposal_reuses_it(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_idioma", "arguments": {"lang": "en"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "marcar_periodo",
                        "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}},
        ])
        self.assertNotIn("isError", respuestas[0]["result"])
        self.assertNotIn("isError", respuestas[1]["result"])
        salida1 = json.loads(respuestas[0]["result"]["content"][-1]["text"])
        salida2 = json.loads(respuestas[1]["result"]["content"][-1]["text"])

        c1 = self.conn.execute("SELECT conversacion_id FROM propuesta_agente WHERE id = ?",
                               (salida1["propuesta_id"],)).fetchone()["conversacion_id"]
        c2 = self.conn.execute("SELECT conversacion_id FROM propuesta_agente WHERE id = ?",
                               (salida2["propuesta_id"],)).fetchone()["conversacion_id"]
        self.assertEqual(c1, c2)
        self.assertEqual(agente_conversacion.origen_de(self.conn, c1), "mcp")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversacion_agente WHERE person_id = ?",
                              (self.person_id,)).fetchone()[0],
            1,
        )

    def test_proposal_can_then_be_applied_through_the_existing_route(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_idioma", "arguments": {"lang": "en"}}},
        ])
        salida = json.loads(respuestas[0]["result"]["content"][-1]["text"])
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        client = TestClient(web.app, follow_redirects=False)
        r = client.post(f"/asistente/propuestas/{salida['propuesta_id']}/aplicar")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["estado"], "aplicada")

    def test_card_from_mcp_carries_the_origin_label(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_idioma", "arguments": {"lang": "en"}}},
        ])
        salida = json.loads(respuestas[0]["result"]["content"][-1]["text"])
        cid = self.conn.execute("SELECT conversacion_id FROM propuesta_agente WHERE id = ?",
                                (salida["propuesta_id"],)).fetchone()["conversacion_id"]
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        client = TestClient(web.app, follow_redirects=False)
        r = client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r.status_code, 200)
        tarjetas = r.json()["propuestas"]
        self.assertEqual(len(tarjetas), 1)
        self.assertEqual(tarjetas[0]["origen_etiqueta"], "Desde tu asistente personal")

    def test_unknown_tool_is_a_tool_error(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "esto_no_existe", "arguments": {}}},
        ])
        r = respuestas[0]["result"]
        self.assertTrue(r["isError"])
        self.assertIn("text", r["content"][0])

    def test_caps_reached_is_a_tool_error(self):
        tope = mcp.LIMITES["lecturas"]
        mensajes = [
            {"jsonrpc": "2.0", "id": i, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}}
            for i in range(tope + 1)
        ]
        _, respuestas = self._ejecutar(mensajes)
        for i in range(tope):
            self.assertNotIn("isError", respuestas[i]["result"], f"call {i} should not have errored yet")
        self.assertTrue(respuestas[tope]["result"]["isError"])
        self.assertIn("min", respuestas[tope]["result"]["content"][0]["text"])

    def test_result_of_a_proposal_says_where_the_person_will_see_it(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "marcar_periodo",
                        "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}},
        ])
        contenido = respuestas[0]["result"]["content"]
        self.assertEqual(len(contenido), 2)
        self.assertIn("Hoy", contenido[0]["text"])
        self.assertIn("Aplicar", contenido[0]["text"])
        salida = json.loads(contenido[-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertIn("estado", salida)

    def test_result_of_a_lectura_carries_no_extra_sentence(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
        ])
        self.assertEqual(len(respuestas[0]["result"]["content"]), 1)


class VentanaRotanteTest(_McpTestCase):
    """BODY-MCP-LOCAL-1-B: caps bound a RATE, not a per-process total -- a long-lived server
    process must be able to read again once the window has rolled past its first calls, with no
    sleeping in the test (an injectable clock)."""

    def test_calls_past_the_limit_are_refused_then_allowed_again_after_the_window(self):
        ahora = {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}

        def reloj():
            return ahora["t"]

        tope = mcp.LIMITES["lecturas"]
        entrada = io.StringIO("".join(
            _linea({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "leer_ajustes", "arguments": {}}})
            for i in range(tope + 1)
        ))
        salida = io.StringIO()
        sesion = mcp.ejecutar(entrada, salida, self.conn, self.person_id, reloj=reloj)
        respuestas = _respuestas(salida)
        for i in range(tope):
            self.assertNotIn("isError", respuestas[i]["result"])
        self.assertTrue(respuestas[tope]["result"]["isError"])

        # Past the window, on the SAME session (the same process's own state, never a fresh one):
        # allowed again.
        ahora["t"] += timedelta(minutes=mcp.LIMITES["ventana_min"] + 1)
        respuesta = mcp._procesar_mensaje(sesion, {"jsonrpc": "2.0", "id": 999, "method": "tools/call",
                                                     "params": {"name": "leer_ajustes", "arguments": {}}})
        self.assertNotIn("isError", respuesta["result"])

    def test_error_names_minutes_to_wait_in_the_persons_language(self):
        ahora = {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}

        def reloj():
            return ahora["t"]

        tope = mcp.LIMITES["propuestas"]
        entrada = io.StringIO("".join(
            _linea({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "marcar_periodo",
                               "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}})
            for i in range(tope + 1)
        ))
        salida = io.StringIO()
        mcp.ejecutar(entrada, salida, self.conn, self.person_id, reloj=reloj)
        respuestas = _respuestas(salida)
        texto = respuestas[tope]["result"]["content"][0]["text"]
        self.assertIn(str(mcp.LIMITES["ventana_min"]), texto)

        db.set_language(self.conn, "en", self.person_id)
        self.conn.commit()
        entrada_en = io.StringIO("".join(
            _linea({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "marcar_periodo",
                               "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}})
            for i in range(tope + 1)
        ))
        salida_en = io.StringIO()
        mcp.ejecutar(entrada_en, salida_en, self.conn, self.person_id, reloj=reloj)
        respuestas_en = _respuestas(salida_en)
        texto_en = respuestas_en[tope]["result"]["content"][0]["text"]
        self.assertIn("min", texto_en)
        self.assertIn(str(mcp.LIMITES["ventana_min"]), texto_en)


class ProtocolErrorsTest(_McpTestCase):
    def test_unknown_method_is_minus_32601(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "no/such/method"}])
        self.assertEqual(respuestas[0]["error"]["code"], -32601)

    def test_malformed_json_is_minus_32700_without_crashing(self):
        entrada = io.StringIO("{esto no es json\n" + _linea({"jsonrpc": "2.0", "id": 2, "method": "initialize"}))
        salida = io.StringIO()
        mcp.ejecutar(entrada, salida, self.conn, self.person_id)
        respuestas = _respuestas(salida)
        self.assertEqual(len(respuestas), 2)
        self.assertEqual(respuestas[0]["error"]["code"], -32700)
        self.assertEqual(respuestas[1]["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)


class AuditTest(_McpTestCase):
    def test_every_call_writes_one_row_with_no_params_or_result(self):
        self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "esto_no_existe", "arguments": {}}},
        ])
        rows = self.conn.execute(
            "SELECT herramienta, caller FROM uso_herramienta_externa WHERE person_id = ? ORDER BY id",
            (self.person_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["herramienta"], "leer_ajustes")
        self.assertEqual(rows[0]["caller"], mcp.CALLER)
        self.assertEqual(rows[1]["herramienta"], "esto_no_existe")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(uso_herramienta_externa)")}
        self.assertEqual(cols, {"id", "person_id", "herramienta", "llamada_en", "caller"})

    def test_stdout_carries_only_protocol_frames(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
        ])
        for r in respuestas:
            self.assertEqual(r.get("jsonrpc"), "2.0")
            self.assertIn("id", r)


class MainStderrTest(_McpTestCase):
    def test_main_logs_counts_only_to_stderr_never_a_figure(self):
        db.set_metric(self.conn, "2026-11-01", "resting_hr", 171717.0, "garmin", self.person_id)
        self.conn.commit()
        entrada = io.StringIO(_linea({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "leer_hoy", "arguments": {}}}))
        salida = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdin", entrada), mock.patch.object(sys, "stdout", salida), \
             mock.patch.object(sys, "stderr", stderr), mock.patch.object(db, "connect", return_value=self.conn):
            mcp.main()
        for linea in salida.getvalue().splitlines():
            if linea.strip():
                json.loads(linea)
        self.assertNotIn("171717", stderr.getvalue())
        self.assertIn("lecturas", stderr.getvalue())


class Utf8WireTest(_McpTestCase):
    """BODY-MCP-LOCAL-1-B: MCP stdio is UTF-8, never the console's own code page. Driven with real
    byte-backed streams whose encoding main() must override -- on the old code (no reconfigure),
    writing "é" through a cp1252-encoded stdout is the single byte 0xE9, which utf-8.decode()
    below raises on; reading "ñ" through a cp1252-decoded stdin turns it into mojibake before it
    ever reaches json.loads. Both fail this test on the code before this slice."""

    def test_round_trips_accented_text_regardless_of_console_encoding(self):
        peticion_lista = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                     ensure_ascii=False).encode("utf-8") + b"\n"
        peticion_marcar = json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "marcar_periodo",
                       "arguments": {"kind": "viaje", "start_day": "2020-01-01", "note": "mañana"}},
        }, ensure_ascii=False).encode("utf-8") + b"\n"
        entrada = io.TextIOWrapper(io.BytesIO(peticion_lista + peticion_marcar), encoding="cp1252", newline="")
        salida_bytes = io.BytesIO()
        salida = io.TextIOWrapper(salida_bytes, encoding="cp1252", newline="")
        with mock.patch.object(sys, "stdin", entrada), mock.patch.object(sys, "stdout", salida), \
             mock.patch.object(sys, "stderr", io.StringIO()), \
             mock.patch.object(db, "connect", return_value=self.conn):
            mcp.main()

        crudo = salida_bytes.getvalue()
        texto = crudo.decode("utf-8")  # raises UnicodeDecodeError on the pre-slice code
        lineas = [json.loads(l) for l in texto.splitlines() if l.strip()]
        descripciones = " ".join(h["description"] for h in lineas[0]["result"]["tools"])
        self.assertIn("métrica", descripciones)

        salida_marcar = json.loads(lineas[1]["result"]["content"][-1]["text"])
        # main() closes the connection it was given (mocked to self.conn) on its own way out, so
        # the row is read back through a fresh one, same db file.
        verificacion = db.connect()
        self.addCleanup(verificacion.close)
        fila = verificacion.execute("SELECT despues_json FROM propuesta_agente WHERE id = ?",
                                     (salida_marcar["propuesta_id"],)).fetchone()
        self.assertEqual(json.loads(fila["despues_json"])["note"], "mañana")


if __name__ == "__main__":
    unittest.main()
