"""BODY-MCP-HTTP-1: POST /mcp, the real MCP endpoint. Cuenta Duna is a stubbed transport
(`llave_asistente._post_introspect`), never a real network call; no real health figures.
"""
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

import httpx
from fastapi.testclient import TestClient

from body import config, cuenta, db, llave_asistente, mcp, web
from body.agente import herramientas
from body.rutas import mcp_http

SUB_A = "sub-zz-mcp-http-a"
SUB_B = "sub-zz-mcp-http-b"
ISSUER = "https://fake-cuenta.test"


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "mcp_http.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    conn.execute("UPDATE person SET cuenta_sub = ? WHERE id = 1", (SUB_A,))
    conn.execute("INSERT INTO person (id, display_name, cuenta_sub) VALUES (2, 'B', ?)", (SUB_B,))
    conn.commit()
    return conn


class _HostedTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self)
        env = {cuenta.ISSUER_ENV: ISSUER, cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test"}
        patcher_env = mock.patch.dict(os.environ, env)
        patcher_env.start()
        self.addCleanup(patcher_env.stop)

        llave_asistente.reset_cache()
        self.addCleanup(llave_asistente.reset_cache)

        self._grants: dict[str, dict] = {}
        patcher = mock.patch.object(llave_asistente, "_post_introspect", self._fake_introspect)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = TestClient(web.app, follow_redirects=False)

    def _fake_introspect(self, key: str) -> httpx.Response:
        grant = self._grants.get(key)
        if grant is None:
            return httpx.Response(200, json={"active": False})
        return httpx.Response(200, json=grant)

    def _grant(self, key: str, permiso: str, *, persona_id: str = SUB_A, llave_id: str = "llave-1") -> None:
        self._grants[key] = {"active": True, "producto": "body", "persona_id": persona_id,
                              "permiso": permiso, "llave_id": llave_id}

    def _post(self, body, key: str | None = "k-escritura"):
        headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
        return self.client.post("/mcp", headers=headers, json=body)


class LocalModeTest(unittest.TestCase):
    def test_404_in_local_mode(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        client = TestClient(web.app, follow_redirects=False)
        self.assertEqual(client.post("/mcp", json={}).status_code, 404)


class MethodTest(_HostedTestCase):
    def test_get_is_405_with_allow_post(self):
        r = self.client.get("/mcp")
        self.assertEqual(r.status_code, 405)
        self.assertIn("POST", r.headers.get("allow", ""))


class AuthTest(_HostedTestCase):
    def test_no_header_401(self):
        r = self._post({}, key=None)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("www-authenticate"), "Bearer")

    def test_not_bearer_401(self):
        r = self.client.post("/mcp", headers={"Authorization": "Basic xyz"}, json={})
        self.assertEqual(r.status_code, 401)

    def test_unknown_key_401(self):
        r = self._post({}, key="never-granted")
        self.assertEqual(r.status_code, 401)

    def test_inactive_key_401(self):
        self._grants["k"] = {"active": False}
        r = self._post({}, key="k")
        self.assertEqual(r.status_code, 401)

    def test_wrong_producto_401(self):
        self._grants["k"] = {"active": True, "producto": "finanzas", "persona_id": SUB_A,
                              "permiso": "lectura", "llave_id": "x"}
        r = self._post({}, key="k")
        self.assertEqual(r.status_code, 401)

    def test_no_body_person_401_with_a_distinct_sentence(self):
        self._grant("k", "lectura", persona_id="sub-nobody-signed-in")
        r_generic = self._post({}, key="never-granted")
        r_sin_persona = self._post({}, key="k")
        self.assertEqual(r_sin_persona.status_code, 401)
        self.assertNotEqual(r_generic.json()["error"], r_sin_persona.json()["error"])

    def test_cuenta_timeout_503_never_401(self):
        def fake(key):
            raise httpx.ConnectTimeout("boom")
        with mock.patch.object(llave_asistente, "_post_introspect", fake):
            r = self._post({}, key="k")
        self.assertEqual(r.status_code, 503)

    def test_cuenta_429_passes_retry_after_through(self):
        def fake(key):
            return httpx.Response(429, headers={"retry-after": "42"})
        with mock.patch.object(llave_asistente, "_post_introspect", fake):
            r = self._post({}, key="k")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers.get("retry-after"), "42")

    def test_inactive_key_401_carries_no_retry_after(self):
        """A real {active: false} is the 'invalid key' 401 -- never the could-not-ask 503, so it
        carries no Retry-After at all (BODY-MCP-503-RETRY-AFTER-1)."""
        self._grants["k"] = {"active": False}
        r = self._post({}, key="k")
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(r.headers.get("retry-after"))


class AllowListCensusTest(_HostedTestCase):
    """BODY-MCP-INICIO-SIN-LLAVE-1 ruling 1, ruling 4's census: every method
    body/rutas/mcp_http.py's own dispatcher (`_procesar_uno`) answers, and which of them may skip
    `llave_asistente.verificar`. Adding `tools/list`, `tools/call` or any other data-returning
    method to `METODOS_SIN_LLAVE` -- or widening the `notifications/` prefix rule to catch
    something it should not -- fails this test."""

    # Every method name tests/test_mcp_http.py's own ProtocolTest above already drives through
    # `_procesar_uno` -- the dispatcher's own full method vocabulary.
    _METODOS_DEL_DISPATCHER = ("initialize", "notifications/initialized", "ping", "tools/list", "tools/call")

    def test_allow_list_is_exactly_initialize_and_ping(self):
        self.assertEqual(mcp_http.METODOS_SIN_LLAVE, frozenset({"initialize", "ping"}))

    def test_every_dispatcher_method_is_classified_correctly(self):
        sin_llave = {m for m in self._METODOS_DEL_DISPATCHER
                     if mcp_http._metodo_sin_llave({"jsonrpc": "2.0", "id": 1, "method": m})}
        self.assertEqual(sin_llave, {"initialize", "notifications/initialized", "ping"})

    def test_an_unlisted_notifications_method_is_still_sin_llave_by_prefix(self):
        self.assertTrue(mcp_http._metodo_sin_llave({"jsonrpc": "2.0", "method": "notifications/cancelled"}))

    def test_an_unknown_method_outside_the_list_is_not_sin_llave(self):
        self.assertFalse(mcp_http._metodo_sin_llave({"jsonrpc": "2.0", "id": 1, "method": "nope"}))

    def test_a_non_dict_or_methodless_item_is_not_sin_llave(self):
        self.assertFalse(mcp_http._metodo_sin_llave("not a dict"))
        self.assertFalse(mcp_http._metodo_sin_llave({"jsonrpc": "2.0", "id": 1}))

    def test_an_empty_batch_is_not_sin_llave(self):
        self.assertFalse(mcp_http._todos_sin_llave([]))

    def test_a_batch_mixing_initialize_and_tools_list_is_not_sin_llave(self):
        self.assertFalse(mcp_http._todos_sin_llave([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]))


class NoLlaveMethodsTest(_HostedTestCase):
    """Ruling 1: `initialize`, `ping` and every `notifications/*` message answer even when Cuenta
    is unreachable -- no call to `llave_asistente.verificar` (no introspection) is ever made for
    them. The Bearer header is still required (401 at once without it, unchanged)."""

    def test_initialize_answers_at_once_with_no_introspection_call(self):
        mock_introspect = mock.Mock(side_effect=httpx.ConnectTimeout("boom"))
        with mock.patch.object(llave_asistente, "_post_introspect", mock_introspect):
            r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, key="k")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        mock_introspect.assert_not_called()

    def test_ping_answers_at_once_with_no_introspection_call(self):
        mock_introspect = mock.Mock(side_effect=httpx.ConnectTimeout("boom"))
        with mock.patch.object(llave_asistente, "_post_introspect", mock_introspect):
            r = self._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key="k")
        self.assertEqual(r.json(), {"jsonrpc": "2.0", "id": 1, "result": {}})
        mock_introspect.assert_not_called()

    def test_notifications_initialized_answers_at_once_with_no_introspection_call(self):
        mock_introspect = mock.Mock(side_effect=httpx.ConnectTimeout("boom"))
        with mock.patch.object(llave_asistente, "_post_introspect", mock_introspect):
            r = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, key="k")
        self.assertEqual(r.status_code, 202)
        mock_introspect.assert_not_called()

    def test_a_batch_of_only_sin_llave_methods_answers_with_no_introspection_call(self):
        mock_introspect = mock.Mock(side_effect=httpx.ConnectTimeout("boom"))
        with mock.patch.object(llave_asistente, "_post_introspect", mock_introspect):
            r = self._post([
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ], key="k")
        self.assertEqual(r.status_code, 200)
        mock_introspect.assert_not_called()

    def test_without_a_bearer_initialize_is_still_401_at_once(self):
        """The allow-list bypass skips introspection, never the header-presence check."""
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, key=None)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("www-authenticate"), "Bearer")

    def test_a_batch_mixing_initialize_and_tools_list_is_verified(self):
        mock_introspect = mock.Mock(return_value=httpx.Response(200, json={"active": False}))
        with mock.patch.object(llave_asistente, "_post_introspect", mock_introspect):
            r = self._post([
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ], key="never-granted")
        self.assertEqual(r.status_code, 401)
        mock_introspect.assert_called_once()

    def test_tools_list_alone_is_still_verified(self):
        mock_introspect = mock.Mock(return_value=httpx.Response(200, json={"active": False}))
        with mock.patch.object(llave_asistente, "_post_introspect", mock_introspect):
            r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="never-granted")
        self.assertEqual(r.status_code, 401)
        mock_introspect.assert_called_once()


class CuentaDownJsonRpcErrorTest(_HostedTestCase):
    """Ruling 3: a verified method (tools/list, tools/call) with Cuenta unreachable answers,
    besides the transport-level 503 and its Retry-After, a JSON-RPC error body with a fixed code
    and a sentence naming when to retry -- never a bare `{"error": ...}`."""

    def _sin_cuenta(self):
        def fake(key):
            raise httpx.ConnectTimeout("boom")
        return mock.patch.object(llave_asistente, "_post_introspect", fake)

    def test_tools_list_with_cuenta_down_is_503_with_a_jsonrpc_error_body(self):
        with self._sin_cuenta():
            r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        self.assertEqual(r.status_code, 503)
        out = r.json()
        self.assertEqual(out["jsonrpc"], "2.0")
        self.assertIsNone(out["id"])
        self.assertIsInstance(out["error"]["code"], int)
        self.assertIn("reintenta en", out["error"]["message"])
        self.assertIn("60", out["error"]["message"])

    def test_tools_call_with_cuenta_down_is_503_with_a_jsonrpc_error_body(self):
        with self._sin_cuenta():
            r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "leer_ajustes", "arguments": {}}}, key="k")
        self.assertEqual(r.status_code, 503)
        self.assertIn("error", r.json())

    def test_message_is_in_english_with_accept_language_en(self):
        with self._sin_cuenta():
            r = self.client.post("/mcp", headers={"Authorization": "Bearer k",
                                                   "Accept-Language": "en-US,en;q=0.9"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("retry in", r.json()["error"]["message"])

    def test_message_defaults_to_spanish_with_no_accept_language(self):
        with self._sin_cuenta():
            r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        self.assertIn("reintenta en", r.json()["error"]["message"])

    def test_cuentas_own_retry_after_number_appears_in_the_message(self):
        def fake(key):
            return httpx.Response(429, headers={"retry-after": "42"})
        with mock.patch.object(llave_asistente, "_post_introspect", fake):
            r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers.get("retry-after"), "42")

    def test_every_503_case_carries_retry_after_equal_to_the_messages_number(self):
        """BODY-MCP-503-RETRY-AFTER-1: a could-not-ask 503 -- timeout, connection error, or a 5xx
        with no Retry-After of its own -- answers the default (`_REINTENTO_DEFECTO_SEGUNDOS`), and
        a 429 that did carry one passes it through verbatim. The header and the message's own
        number are always the same `n`."""
        def timeout(key):
            raise httpx.ConnectTimeout("boom")

        def connect_error(key):
            raise httpx.ConnectError("boom")

        def quinientos_sin_retry_after(key):
            return httpx.Response(500)

        def cuatrocientos_veintinueve_con_retry_after(key):
            return httpx.Response(429, headers={"retry-after": "42"})

        casos = {
            "timeout": (timeout, "60"),
            "connection error": (connect_error, "60"),
            "500 sin retry-after": (quinientos_sin_retry_after, "60"),
            "429 con retry-after": (cuatrocientos_veintinueve_con_retry_after, "42"),
        }
        for nombre, (fake, esperado) in casos.items():
            with self.subTest(nombre):
                with mock.patch.object(llave_asistente, "_post_introspect", fake):
                    r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.headers.get("retry-after"), esperado)
                self.assertIn(esperado, r.json()["error"]["message"])
        self.assertIn("42", r.json()["error"]["message"])


class PositiveCacheShortCircuitsTest(_HostedTestCase):
    """Ruling 4: the positive cache (body/llave_asistente.py's own, unchanged) still short-
    circuits a verified call -- the reordering in this slice must not re-ask Cuenta on a second
    call with the same key."""

    def test_a_cached_key_is_not_re_introspected_on_a_second_verified_call(self):
        self._grant("k", "escritura")
        mock_introspect = mock.Mock(side_effect=self._fake_introspect)
        with mock.patch.object(llave_asistente, "_post_introspect", mock_introspect):
            self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
            self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, key="k")
        mock_introspect.assert_called_once()


class GraciaRouteTest(_HostedTestCase):
    """BODY-LLAVE-GRACIA-1 rulings 1-2 at the route: a confirmed key answers 200 while Cuenta
    cannot be reached at all, for up to GRACIA_SEGUNDOS past its last positive answer, and 503
    once that window has passed."""

    @staticmethod
    def _fake_timeout(key):
        raise httpx.ConnectTimeout("boom")

    def test_tools_list_with_a_confirmed_key_answers_200_inside_grace_and_503_after_it(self):
        self._grant("k", "lectura")
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        self.assertEqual(r.status_code, 200)

        h = llave_asistente._clave_cache("k")
        del llave_asistente._cache[h]  # force the next call past the positive cache

        with mock.patch.object(llave_asistente, "_post_introspect", self._fake_timeout):
            r_dentro = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, key="k")
        self.assertEqual(r_dentro.status_code, 200)

        desde, autorizacion = llave_asistente._ultimo_bueno[h]
        llave_asistente._ultimo_bueno[h] = (
            desde - llave_asistente.GRACIA_SEGUNDOS - 1, autorizacion)

        with mock.patch.object(llave_asistente, "_post_introspect", self._fake_timeout):
            r_fuera = self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, key="k")
        self.assertEqual(r_fuera.status_code, 503)


class ProtocolTest(_HostedTestCase):
    def setUp(self):
        super().setUp()
        self._grant("k", "escritura")

    def test_initialize(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": mcp.PROTOCOL_VERSION}}, key="k")
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertEqual(out["result"]["serverInfo"]["name"], "body")

    def test_initialize_echoes_newest_supported_for_an_unknown_client_version(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "1999-01-01"}}, key="k")
        self.assertEqual(r.json()["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)

    def test_notifications_initialized_is_202_no_body(self):
        r = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, key="k")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.content, b"")

    def test_ping(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key="k")
        self.assertEqual(r.json(), {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_tools_list(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        self.assertEqual(r.status_code, 200)
        tools = r.json()["result"]["tools"]
        self.assertTrue(any(t["name"] == "leer_hoy" for t in tools))

    def test_tools_call_round_trip(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "leer_ajustes", "arguments": {}}}, key="k")
        self.assertEqual(r.status_code, 200)
        out = r.json()["result"]
        self.assertNotIn("isError", out)
        self.assertEqual(out["content"][0]["type"], "text")

    def test_batch(self):
        r = self._post([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ], key="k")
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(len(out), 2)
        self.assertEqual({o["id"] for o in out}, {1, 2})

    def test_batch_of_only_notifications_is_202_no_body(self):
        r = self._post([{"jsonrpc": "2.0", "method": "notifications/initialized"}], key="k")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.content, b"")

    def test_malformed_json_is_parse_error(self):
        r = self.client.post("/mcp", headers={"Authorization": "Bearer k"}, content=b"not json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"]["code"], -32700)

    def test_wrong_shape_is_invalid_request(self):
        r = self._post({"not": "jsonrpc"}, key="k")
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_empty_batch_is_invalid_request(self):
        r = self._post([], key="k")
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_unknown_method_is_method_not_found(self):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "nope"}, key="k")
        self.assertEqual(r.json()["error"]["code"], -32601)


class PermisoTest(_HostedTestCase):
    def test_lectura_hides_write_tools(self):
        self._grant("k", "lectura")
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        tools = r.json()["result"]["tools"]
        nombres = {t["name"] for t in tools}
        self.assertNotIn(mcp.NOMBRE_APLICAR, nombres)
        self.assertNotIn(mcp.NOMBRE_DESCARTAR, nombres)
        for nombre in nombres:
            self.assertEqual(herramientas.tipo_de(nombre), "lectura")

    def test_escritura_lists_write_tools(self):
        self._grant("k", "escritura")
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        nombres = {t["name"] for t in r.json()["result"]["tools"]}
        self.assertIn(mcp.NOMBRE_APLICAR, nombres)
        self.assertIn(mcp.NOMBRE_DESCARTAR, nombres)
        self.assertTrue(any(herramientas.tipo_de(n) == "propuesta" for n in nombres))

    def test_corregir_entreno_is_offered_over_http(self):
        """BODY-CORREGIR-MEDIDA-ENTRENO-1-W ruling B: catalog parity -- the same registry entry
        tools/list already serves over stdio (tests/test_mcp.py) also reaches this HTTP route,
        through the same `mcp._lista_herramientas`."""
        self._grant("k", "escritura")
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        nombres = {t["name"] for t in r.json()["result"]["tools"]}
        self.assertIn("corregir_entreno", nombres)

    def test_lectura_calling_a_write_tool_is_iserror_never_a_transport_error(self):
        self._grant("k", "lectura")
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "proponer_idioma", "arguments": {"lang": "en"}}}, key="k")
        self.assertEqual(r.status_code, 200)
        out = r.json()["result"]
        self.assertTrue(out["isError"])
        self.assertNotIn("error", r.json())

    def test_lectura_tool_is_proven_write_free(self):
        self._grant("k", "lectura")
        before = self.conn.total_changes
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "leer_hoy", "arguments": {}}}, key="k")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.conn.total_changes, before)

    def test_escritura_applies_a_write_directly_with_aplicar_mcp_off(self):
        self._grant("k", "escritura")
        self.assertFalse(db.get_aplicar_mcp(self.conn, 1))
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "proponer_idioma", "arguments": {"lang": "en"}}}, key="k")
        self.assertEqual(r.status_code, 200)
        out = r.json()["result"]
        self.assertNotIn("isError", out)
        row = self.conn.execute(
            "SELECT estado, aplicada_via FROM propuesta_agente WHERE person_id = 1 "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row["estado"], row["aplicada_via"]), ("aplicada", "mcp"))


class ParityWithStdioTest(_HostedTestCase):
    def test_escritura_tools_list_matches_the_stdio_server_with_aplicar_on(self):
        self._grant("k", "escritura")
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        esperado = mcp._lista_herramientas(mcp._Sesion(self.conn, 1))

        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        self.assertEqual(r.json()["result"]["tools"], esperado)

    def test_lectura_tools_list_matches_the_read_only_subset(self):
        self._grant("k", "lectura")
        db.set_aplicar_mcp(self.conn, 1, True)
        self.conn.commit()
        esperado = [h for h in mcp._lista_herramientas(mcp._Sesion(self.conn, 1))
                    if herramientas.tipo_de(h["name"]) == "lectura"]

        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key="k")
        self.assertEqual(r.json()["result"]["tools"], esperado)


class IsolationTest(_HostedTestCase):
    def test_a_cannot_read_bs_figures(self):
        db.set_metric(self.conn, "2026-01-01", "resting_hr", 187, "garmin", 2)
        self.conn.commit()
        self._grant("k-a", "lectura", persona_id=SUB_A)
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "leer_hoy", "arguments": {}}}, key="k-a")
        self.assertNotIn("187", r.text)


class LogLeakTest(_HostedTestCase):
    def test_no_key_hash_or_persona_id_in_any_log_record(self):
        self._grant("k", "escritura", llave_id="llave-xyz")
        capturados = []

        class _Captura(logging.Handler):
            def emit(self, record):
                capturados.append(record.getMessage())

        handler = _Captura()
        logger_remoto = logging.getLogger("body.mcp_remoto")
        logger_http = logging.getLogger("body.mcp_http")
        logger_remoto.addHandler(handler)
        logger_http.addHandler(handler)
        self.addCleanup(logger_remoto.removeHandler, handler)
        self.addCleanup(logger_http.removeHandler, handler)

        self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "esto_no_existe", "arguments": {}}}, key="k")
        self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "leer_hoy", "arguments": {}}}, key="k")

        for mensaje in capturados:
            self.assertNotIn("k", mensaje.split())  # the bare key never appears as a token
            self.assertNotIn(SUB_A, mensaje)
            self.assertNotIn("llave-xyz", mensaje)


if __name__ == "__main__":
    unittest.main()
