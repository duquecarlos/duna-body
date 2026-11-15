"""BODY-MCP-REMOTO-CLIENTE-1: the Personal desk's own MCP client, body/mcp.py's `_SesionCliente`,
reaching the hosted MCP door through body/pc_cliente.py instead of the local database. No network
-- every call to pc_cliente's transport is a fake. Invented demo data only, no real health figures,
no secret ever asserted to be a real one (TEST_KEY below is synthetic, as in tests/test_pc_cliente.py).
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, mcp, pc_cliente
from body.agente import herramientas
from body.i18n import T

TEST_KEY = "bdy_test-key-0123456789"


class _TempHomeTestCase(unittest.TestCase):
    """Same pattern as tests/test_pc_cliente.py's own base: a fresh BODY_HOME per test, so one
    test's dispositivo.json never leaks into another."""

    def setUp(self):
        orig_home = config.BODY_HOME
        self.addCleanup(lambda: setattr(config, "BODY_HOME", orig_home))
        config.BODY_HOME = Path(tempfile.mkdtemp(dir=TMP))


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


# ---- ruling 1: the mode is the PC's link, read once at start -----------------------------------

class ModoTest(_TempHomeTestCase):
    def test_no_link_means_local(self):
        self.assertFalse(mcp._modo_remoto())

    def test_a_link_means_remote(self):
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)
        self.assertTrue(mcp._modo_remoto())

    def test_env_override_forces_local_even_with_a_link(self):
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)
        with mock.patch.dict("os.environ", {mcp.MODO_ENV: "local"}):
            self.assertFalse(mcp._modo_remoto())


class MainNuncaAbreLaBaseLocalTest(_TempHomeTestCase):
    def test_remote_main_with_a_link_never_calls_db_connect(self):
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)

        def respuestas(method, ruta, *, body=None, headers=None):
            if method == "GET" and ruta == mcp.RUTA_HERRAMIENTAS:
                return 200, {"herramientas": [{"name": "leer_ajustes", "description": "x",
                                                "inputSchema": {}}], "nivel": "completo", "idioma": "es"}
            cuerpo = json.loads(body.decode("utf-8"))
            self.assertEqual(cuerpo["nombre"], "leer_ajustes")
            return 200, {"tipo": "lectura", "salida": {"ok": True}, "aviso_reloj": False,
                         "conversacion_id": None}

        entrada = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n" +
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "leer_ajustes", "arguments": {}}}) + "\n")
        salida, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", entrada), mock.patch.object(sys, "stdout", salida), \
             mock.patch.object(sys, "stderr", stderr), \
             mock.patch.object(pc_cliente, "llamar", side_effect=respuestas), \
             mock.patch.object(db, "connect", side_effect=AssertionError("must not open the local db")):
            mcp.main()

        lineas = [json.loads(l) for l in salida.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lineas), 3)
        self.assertEqual(lineas[0]["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertEqual([t["name"] for t in lineas[1]["result"]["tools"]], ["leer_ajustes"])
        self.assertNotIn("isError", lineas[2]["result"])
        self.assertIn("lecturas", stderr.getvalue())
        self.assertIn("1 lecturas", stderr.getvalue())


# ---- ruling 2: one envelope, two backends -------------------------------------------------------

class ListaHerramientasRemotaTest(unittest.TestCase):
    def test_success_reads_idioma_and_herramientas_from_the_server(self):
        sesion = mcp._SesionCliente()
        payload = {"herramientas": [{"name": "leer_ajustes", "description": "x", "inputSchema": {}}],
                   "nivel": "completo", "idioma": "en"}
        with mock.patch.object(pc_cliente, "llamar", return_value=(200, payload)):
            tools = mcp._lista_herramientas(sesion)  # the dispatcher, not the private function
        self.assertEqual(tools, payload["herramientas"])
        self.assertEqual(sesion.idioma, "en")

    def test_network_failure_logs_class_only_and_returns_empty(self):
        sesion = mcp._SesionCliente()
        stderr = io.StringIO()
        with mock.patch.object(pc_cliente, "llamar", side_effect=urllib.error.URLError("boom")), \
             mock.patch.object(sys, "stderr", stderr):
            tools = mcp._lista_herramientas(sesion)
        self.assertEqual(tools, [])
        self.assertIn("URLError", stderr.getvalue())
        self.assertNotIn("boom", stderr.getvalue())

    def test_bad_status_returns_empty(self):
        sesion = mcp._SesionCliente()
        with mock.patch.object(pc_cliente, "llamar", return_value=(500, None)):
            tools = mcp._lista_herramientas(sesion)
        self.assertEqual(tools, [])

    def test_malformed_body_returns_empty(self):
        sesion = mcp._SesionCliente()
        with mock.patch.object(pc_cliente, "llamar", return_value=(200, ["not", "a", "dict"])):
            tools = mcp._lista_herramientas(sesion)
        self.assertEqual(tools, [])


class LlamarHerramientaRemotaTest(unittest.TestCase):
    def test_lectura_call_counts_and_returns_tipo_salida_aviso(self):
        sesion = mcp._SesionCliente()
        with mock.patch.object(pc_cliente, "llamar", return_value=(
                200, {"tipo": "lectura", "salida": {"nivel_datos": "completo"}, "aviso_reloj": False,
                      "conversacion_id": None})):
            tipo, salida, aviso = mcp._llamar_herramienta(sesion, "leer_ajustes", {})
        self.assertEqual((tipo, salida, aviso), ("lectura", {"nivel_datos": "completo"}, False))
        self.assertEqual(sesion.total_lecturas, 1)
        self.assertIsNone(sesion.conversacion_id)

    def test_conversacion_id_flows_none_then_held_then_resent(self):
        sesion = mcp._SesionCliente()
        cuerpos = []

        def respuestas(method, ruta, *, body=None, headers=None):
            cuerpos.append(json.loads(body.decode("utf-8")))
            return 200, {"tipo": "propuesta", "salida": {"propuesta_id": 1}, "aviso_reloj": False,
                         "conversacion_id": 42}

        with mock.patch.object(pc_cliente, "llamar", side_effect=respuestas):
            mcp._llamar_herramienta(sesion, "proponer_idioma", {"lang": "en"})
            self.assertEqual(sesion.conversacion_id, 42)
            self.assertNotIn("conversacion_id", cuerpos[0])

            mcp._llamar_herramienta(sesion, "proponer_idioma", {"lang": "en"})
            self.assertEqual(cuerpos[1]["conversacion_id"], 42)

    def test_aplicacion_and_descarte_types_are_not_counted_as_lectura_or_propuesta(self):
        sesion = mcp._SesionCliente()
        with mock.patch.object(pc_cliente, "llamar", return_value=(
                200, {"tipo": "aplicacion", "salida": {"propuesta_id": 1, "estado": "aplicada"},
                      "aviso_reloj": False, "conversacion_id": None})):
            mcp._llamar_herramienta(sesion, mcp.NOMBRE_APLICAR, {"propuesta_id": 1})
        self.assertEqual(sesion.total_aplicaciones, 1)
        self.assertEqual(sesion.total_lecturas, 0)
        self.assertEqual(sesion.total_propuestas, 0)


# ---- ruling 3/4: every error code and HTTP failure renders its sentence, never a silent fallback -

class ErrorRenderingTest(unittest.TestCase):
    def _rehuso(self, idioma: str, nombre: str, data: dict) -> mcp._RehusoClienteMCP:
        sesion = mcp._SesionCliente()
        sesion.idioma = idioma
        return mcp._error_remoto_a_rehuso(sesion, nombre, data)

    def test_limite_lectura(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, "leer_ajustes", {"error": "limite", "minutos": 7})
            self.assertEqual(str(e), T(idioma)("asistente.mcp.limite_lecturas", minutos=7))

    def test_limite_propuesta(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, "proponer_idioma", {"error": "limite", "minutos": 7})
            self.assertEqual(str(e), T(idioma)("asistente.mcp.limite_propuestas", minutos=7))

    def test_limite_aplicacion(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, mcp.NOMBRE_APLICAR, {"error": "limite", "minutos": 3})
            self.assertEqual(str(e), T(idioma)("asistente.mcp.limite_aplicaciones", minutos=3))

    def test_aplicar_apagado(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, mcp.NOMBRE_APLICAR, {"error": "aplicar_apagado"})
            self.assertEqual(str(e), T(idioma)("asistente.mcp.aplicar_apagado"))

    def test_aplicar_no_propia(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, mcp.NOMBRE_APLICAR, {"error": "aplicar_no_propia"})
            self.assertEqual(str(e), T(idioma)("asistente.mcp.aplicar_no_propia"))

    def test_aplicar_no_vigente(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, mcp.NOMBRE_APLICAR, {"error": "aplicar_no_vigente"})
            self.assertEqual(str(e), T(idioma)("asistente.mcp.aplicar_no_vigente"))

    def test_descartar_apagado(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, mcp.NOMBRE_DESCARTAR, {"error": "descartar_apagado"})
            self.assertEqual(str(e), T(idioma)("asistente.mcp.descartar_apagado"))

    def test_conversacion_ajena(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, "leer_ajustes", {"error": "conversacion_ajena"})
            self.assertEqual(str(e), T(idioma)("mcp.remoto.conversacion_ajena"))

    def test_herramienta_rechazada_carries_the_servers_raw_message(self):
        e = self._rehuso("es", "esto_no_existe",
                          {"error": "herramienta_rechazada", "mensaje": "herramienta desconocida: x"})
        self.assertEqual(str(e), "herramienta desconocida: x")

    def test_herramienta_rechazada_without_a_message_falls_back(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, "esto_no_existe", {"error": "herramienta_rechazada"})
            self.assertEqual(str(e), T(idioma)("mcp.remoto.error_interno"))

    def test_unknown_code_falls_back_to_error_interno(self):
        for idioma in ("es", "en"):
            e = self._rehuso(idioma, "leer_ajustes", {"error": "algo_nuevo_no_previsto"})
            self.assertEqual(str(e), T(idioma)("mcp.remoto.error_interno"))

    def test_aplicar_expirada_delegates_to_its_own_renderer(self):
        for idioma in ("es", "en"):
            expira_en = "2026-11-10T08:30:00+00:00"
            e = self._rehuso(idioma, mcp.NOMBRE_APLICAR, {"error": "aplicar_expirada", "expira_en": expira_en})
            self.assertEqual(str(e), mcp._mensaje_aplicar_expirada_remoto(idioma, expira_en))


class AplicarExpiradaRemotoTest(unittest.TestCase):
    """The same sentence `_mensaje_aplicar_expirada` gives, but converted with an injected
    timezone instead of a DB lookup (no DB on the client side) -- byte-identical output."""

    def test_renders_in_the_given_timezone_both_languages(self):
        from zoneinfo import ZoneInfo
        bogota = ZoneInfo("America/Bogota")
        texto_es = mcp._mensaje_aplicar_expirada_remoto("es", "2026-11-10T08:30:00+00:00", zona=bogota)
        self.assertIn("03:30", texto_es)
        self.assertIn("noviembre", texto_es)
        texto_en = mcp._mensaje_aplicar_expirada_remoto("en", "2026-11-10T08:30:00+00:00", zona=bogota)
        self.assertIn("03:30", texto_en)
        self.assertIn("November", texto_en)

    def test_malformed_timestamp_falls_back_to_error_interno(self):
        for idioma in ("es", "en"):
            self.assertEqual(mcp._mensaje_aplicar_expirada_remoto(idioma, "no es una fecha"),
                              T(idioma)("mcp.remoto.error_interno"))
        self.assertEqual(mcp._mensaje_aplicar_expirada_remoto("es", None),
                          T("es")("mcp.remoto.error_interno"))


class TransporteFalloTest(unittest.TestCase):
    """Ruling 4: never a silent fallback to the local database on ANY failure -- a connection
    failure, a 401, a 403 and any other bad status each raise the right sentence; stderr carries
    the exception class or HTTP status, and the tool NAME only (ruling 5)."""

    def test_network_failure_renders_red_fallo_and_logs_class_and_tool_name_only(self):
        sesion = mcp._SesionCliente()
        stderr = io.StringIO()
        with mock.patch.object(pc_cliente, "llamar", side_effect=urllib.error.URLError("conn refused")), \
             mock.patch.object(sys, "stderr", stderr):
            with self.assertRaises(herramientas.ToolError) as ctx:
                mcp._llamar_herramienta(sesion, "leer_ajustes", {})
        self.assertEqual(str(ctx.exception), T("es")("mcp.remoto.red_fallo"))
        self.assertIn("URLError", stderr.getvalue())
        self.assertIn("leer_ajustes", stderr.getvalue())
        self.assertNotIn("conn refused", stderr.getvalue())

    def test_401_renders_clave_invalida(self):
        sesion = mcp._SesionCliente()
        sesion.idioma = "en"
        with mock.patch.object(pc_cliente, "llamar", return_value=(401, {"error": "no_autorizado"})):
            with self.assertRaises(herramientas.ToolError) as ctx:
                mcp._llamar_herramienta(sesion, "leer_ajustes", {})
        self.assertEqual(str(ctx.exception), T("en")("mcp.remoto.clave_invalida"))

    def test_403_renders_sin_permiso(self):
        sesion = mcp._SesionCliente()
        with mock.patch.object(pc_cliente, "llamar", return_value=(403, {"error": "alcance"})):
            with self.assertRaises(herramientas.ToolError) as ctx:
                mcp._llamar_herramienta(sesion, "leer_ajustes", {})
        self.assertEqual(str(ctx.exception), T("es")("mcp.remoto.sin_permiso"))

    def test_5xx_renders_error_interno(self):
        sesion = mcp._SesionCliente()
        with mock.patch.object(pc_cliente, "llamar", return_value=(500, None)):
            with self.assertRaises(herramientas.ToolError) as ctx:
                mcp._llamar_herramienta(sesion, "leer_ajustes", {})
        self.assertEqual(str(ctx.exception), T("es")("mcp.remoto.error_interno"))

    def test_malformed_success_body_renders_error_interno_never_raises_uncaught(self):
        sesion = mcp._SesionCliente()
        with mock.patch.object(pc_cliente, "llamar", return_value=(200, {"tipo": "lectura"})):
            with self.assertRaises(herramientas.ToolError) as ctx:
                mcp._llamar_herramienta(sesion, "leer_ajustes", {})
        self.assertEqual(str(ctx.exception), T("es")("mcp.remoto.error_interno"))

    def test_unexpected_exception_from_the_transport_also_renders_a_sentence(self):
        sesion = mcp._SesionCliente()
        stderr = io.StringIO()
        with mock.patch.object(pc_cliente, "llamar", side_effect=ValueError("algo raro")), \
             mock.patch.object(sys, "stderr", stderr):
            with self.assertRaises(herramientas.ToolError) as ctx:
                mcp._llamar_herramienta(sesion, "leer_ajustes", {})
        self.assertEqual(str(ctx.exception), T("es")("mcp.remoto.error_interno"))
        self.assertIn("ValueError", stderr.getvalue())
        self.assertNotIn("algo raro", stderr.getvalue())


class LlamadaRechazadaViaProcesarMensajeTest(unittest.TestCase):
    """Proves the shared JSON-RPC envelope (`_procesar_mensaje`, unchanged) renders a remote
    refusal exactly as it already renders a local one -- `except herramientas.ToolError` needs no
    new branch for `_RehusoClienteMCP`."""

    def test_tools_call_isError_carries_the_already_localized_text(self):
        sesion = mcp._SesionCliente()
        with mock.patch.object(pc_cliente, "llamar", return_value=(401, {"error": "no_autorizado"})):
            respuesta = mcp._procesar_mensaje(sesion, {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "leer_ajustes", "arguments": {}}})
        self.assertTrue(respuesta["result"]["isError"])
        self.assertEqual(respuesta["result"]["content"][0]["text"], T("es")("mcp.remoto.clave_invalida"))


# ---- ruling 5: the key never reaches a log, an error text, or a test output --------------------

class ClaveNuncaEnStderrTest(_TempHomeTestCase):
    def test_key_never_appears_in_stderr_on_a_real_transport_round_trip(self):
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)
        sesion = mcp._SesionCliente()
        stderr = io.StringIO()

        def abrir(req):
            self.assertNotIn(TEST_KEY, req.full_url)
            raise urllib.error.URLError("no network here")

        with mock.patch.object(pc_cliente, "_abrir", abrir), mock.patch.object(sys, "stderr", stderr):
            with self.assertRaises(herramientas.ToolError):
                mcp._llamar_herramienta(sesion, "leer_ajustes", {})
        self.assertNotIn(TEST_KEY, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
