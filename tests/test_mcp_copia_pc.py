"""BODY-MCP-COPIA-PC-1: in LOCAL mode, every body/mcp.py tools/call result (a read, a proposal, an
apply, a discard, a refusal or an internal error alike) and `initialize`'s own `instructions`
begin with one line naming where the data lives and how fresh it is -- found after the Personal
desk spent a day reading a PC-local copy of Body that lagged the hosted server by many merges,
with nothing in any answer saying so (BODY-DEPLOY-E6E76C2). REMOTE mode and the hosted /mcp add
nothing. Invented demo data only, no real health figures, no network, no subprocess.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, mcp


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "mcp_copia_pc.db"
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

    def _con_garmin(self, dia: str) -> None:
        db.set_metric(self.conn, dia, "resting_hr", 58, "garmin", self.person_id)
        self.conn.commit()


_TITULO_ES = "Body en este PC (no el servidor)"
_TITULO_EN = "Body on this PC (not the server)"


class InitializeTest(_McpTestCase):
    def test_instructions_and_title_name_the_last_garmin_day(self):
        self._con_garmin("2026-11-01")
        self._con_garmin("2026-11-12")
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
        r = respuestas[0]["result"]
        self.assertEqual(r["serverInfo"]["title"], _TITULO_ES)
        self.assertEqual(r["instructions"],
                          "Body en este PC (no el servidor) -- datos del reloj hasta 12 nov 2026")
        # SERVER_INFO's own existing fields are untouched by the widening.
        self.assertEqual(r["serverInfo"]["name"], "body-local")

    def test_instructions_say_no_watch_data_for_a_fresh_home(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
        r = respuestas[0]["result"]
        self.assertEqual(r["instructions"], "Body en este PC (no el servidor) -- sin datos del reloj")
        self.assertEqual(r["serverInfo"]["title"], _TITULO_ES)

    def test_instructions_in_english(self):
        db.set_language(self.conn, "en", self.person_id)
        self.conn.commit()
        self._con_garmin("2026-11-12")
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
        r = respuestas[0]["result"]
        self.assertEqual(r["serverInfo"]["title"], _TITULO_EN)
        self.assertEqual(r["instructions"], "Body on this PC (not the server) -- watch data up to 12 Nov 2026")


class ReadToolResultTest(_McpTestCase):
    def test_a_lectura_result_begins_with_the_line_with_data(self):
        self._con_garmin("2026-11-09")
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
        ])
        contenido = respuestas[0]["result"]["content"]
        self.assertEqual(len(contenido), 2)
        self.assertEqual(contenido[0]["text"],
                          "Body en este PC (no el servidor) -- datos del reloj hasta 9 nov 2026")
        salida = json.loads(contenido[-1]["text"])
        self.assertIn("nivel_datos", salida)

    def test_a_lectura_result_begins_with_the_line_with_no_data(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
        ])
        contenido = respuestas[0]["result"]["content"]
        self.assertEqual(len(contenido), 2)
        self.assertEqual(contenido[0]["text"], "Body en este PC (no el servidor) -- sin datos del reloj")


class ProposalAndApplyResultTest(_McpTestCase):
    def test_a_propuesta_result_begins_with_the_line(self):
        self._con_garmin("2026-11-09")
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "marcar_periodo",
                        "arguments": {"kind": "viaje", "start_day": "2026-11-10"}}},
        ])
        contenido = respuestas[0]["result"]["content"]
        self.assertEqual(len(contenido), 3)
        self.assertEqual(contenido[0]["text"],
                          "Body en este PC (no el servidor) -- datos del reloj hasta 9 nov 2026")
        self.assertIn("Hoy", contenido[1]["text"])
        salida = json.loads(contenido[-1]["text"])
        self.assertIn("propuesta_id", salida)

    def test_aplicar_propuesta_result_begins_with_the_line(self):
        self._con_garmin("2026-11-09")
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "marcar_periodo",
                        "arguments": {"kind": "viaje", "start_day": "2026-11-10"}}},
        ])
        pid = json.loads(respuestas[0]["result"]["content"][-1]["text"])["propuesta_id"]

        _, respuestas2 = self._ejecutar([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        contenido = respuestas2[0]["result"]["content"]
        self.assertEqual(contenido[0]["text"],
                          "Body en este PC (no el servidor) -- datos del reloj hasta 9 nov 2026")
        salida = json.loads(contenido[-1]["text"])
        self.assertEqual(salida["estado"], "aplicada")

    def test_descartar_propuesta_result_begins_with_the_line(self):
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "marcar_periodo",
                        "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}},
        ])
        pid = json.loads(respuestas[0]["result"]["content"][-1]["text"])["propuesta_id"]

        _, respuestas2 = self._ejecutar([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_DESCARTAR, "arguments": {"propuesta_id": pid}}},
        ])
        contenido = respuestas2[0]["result"]["content"]
        self.assertEqual(contenido[0]["text"], "Body en este PC (no el servidor) -- sin datos del reloj")


class RefusalAndErrorResultTest(_McpTestCase):
    def test_an_unknown_tool_refusal_begins_with_the_line(self):
        self._con_garmin("2026-11-09")
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "esto_no_existe", "arguments": {}}},
        ])
        r = respuestas[0]["result"]
        self.assertTrue(r["isError"])
        self.assertEqual(r["content"][0]["text"],
                          "Body en este PC (no el servidor) -- datos del reloj hasta 9 nov 2026")
        self.assertEqual(len(r["content"]), 2)

    def test_a_cap_refusal_begins_with_the_line(self):
        tope = mcp.LIMITES["lecturas"]
        mensajes = [
            {"jsonrpc": "2.0", "id": i, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}}
            for i in range(tope + 1)
        ]
        _, respuestas = self._ejecutar(mensajes)
        r = respuestas[tope]["result"]
        self.assertTrue(r["isError"])
        self.assertEqual(r["content"][0]["text"], "Body en este PC (no el servidor) -- sin datos del reloj")
        self.assertIn("min", r["content"][-1]["text"])

    def test_an_internal_error_result_begins_with_the_line(self):
        def falla(*args, **kwargs):
            raise db.sqlite3.IntegrityError("CHECK constraint failed")

        with mock.patch("body.mcp.herramientas.ejecutar", side_effect=falla):
            _, respuestas = self._ejecutar([
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "marcar_periodo",
                            "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}},
            ])
        r = respuestas[0]["result"]
        self.assertTrue(r["isError"])
        self.assertEqual(r["content"][0]["text"], "Body en este PC (no el servidor) -- sin datos del reloj")
        self.assertEqual(len(r["content"]), 2)


class RemoteModeAddsNothingTest(unittest.TestCase):
    """Ruling 1: REMOTE mode (`_SesionCliente`, the stdio client half of BODY-MCP-REMOTO-
    CLIENTE-1) never carries the line -- `_con_copia_pc`'s own isinstance check, and `initialize`'s
    branch in `_procesar_mensaje`, both key on `_Sesion` only. Driven the same way
    tests/test_mcp_cliente_remoto.py drives `_SesionCliente` directly, no fake HTTP needed since
    this is a pure dispatch check."""

    def test_initialize_carries_no_instructions_or_title(self):
        sesion = mcp._SesionCliente()
        resultado = mcp._procesar_mensaje(sesion, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
        self.assertNotIn("instructions", resultado)
        self.assertNotIn("title", resultado["serverInfo"])
        self.assertEqual(resultado["serverInfo"], mcp.SERVER_INFO)

    def test_a_tools_call_result_is_never_prefixed(self):
        sesion = mcp._SesionCliente()
        contenido = mcp._con_copia_pc(sesion, [{"type": "text", "text": "x"}])
        self.assertEqual(contenido, [{"type": "text", "text": "x"}])


if __name__ == "__main__":
    unittest.main()
