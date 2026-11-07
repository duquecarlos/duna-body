"""BODY-ASISTENTE-PROVEEDOR-1: the provider interface. Every adapter takes an injectable transport
so no test ever touches the network or reads a real API key."""
import os
import unittest
from unittest.mock import patch

import httpx

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body.agente import cliente


def _borrar_env(*nombres):
    guardado = {n: os.environ.pop(n, None) for n in nombres}

    def _restaurar():
        for n, v in guardado.items():
            if v is not None:
                os.environ[n] = v
            else:
                os.environ.pop(n, None)
    return _restaurar


class AnthropicAdapterTest(unittest.TestCase):
    def test_disjoint_usage_from_a_scripted_response(self):
        data = {
            "content": [{"type": "text", "text": "Dormiste 7 h 10 min."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 400,
                      "cache_creation_input_tokens": 50},
        }
        prov = cliente.Anthropic("claude-sonnet-5-5", "sk-fake", transporte=lambda payload: data)
        resp = prov.responder("eres el asistente", [{"rol": "usuario", "bloques": [
            {"tipo": "texto", "texto": "¿cómo dormí?"}]}], [])
        self.assertEqual(resp.uso, cliente.Uso(entrada=120, salida=30, cache_lectura=400, cache_escritura=50))
        self.assertEqual(resp.bloques, [cliente.BloqueTexto("Dormiste 7 h 10 min.")])
        self.assertEqual(resp.motivo_fin, "end_turn")

    def test_tool_use_block_is_mapped_to_a_llamada_herramienta(self):
        data = {
            "content": [{"type": "tool_use", "id": "call_1", "name": "leer_hoy", "input": {}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 50, "output_tokens": 10},
        }
        prov = cliente.Anthropic("claude-sonnet-5-5", "sk-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "hola"}]}],
                              [{"nombre": "leer_hoy", "descripcion": "x", "parametros": {}}])
        self.assertEqual(resp.bloques, [cliente.LlamadaHerramienta("call_1", "leer_hoy", {})])
        self.assertEqual(resp.uso, cliente.Uso(entrada=50, salida=10))

    def test_tool_result_round_trips_through_the_request_payload(self):
        seen = {}

        def transporte(payload):
            seen["payload"] = payload
            return {"content": [{"type": "text", "text": "listo"}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

        prov = cliente.Anthropic("claude-sonnet-5-5", "sk-fake", transporte=transporte)
        mensajes = [
            {"rol": "asistente", "bloques": [{"tipo": "llamada_herramienta", "id": "call_1",
                                              "nombre": "leer_hoy", "parametros": {}}]},
            {"rol": "usuario", "bloques": [{"tipo": "resultado_herramienta", "id": "call_1",
                                            "nombre": "leer_hoy", "resultado": {"dia": "2026-11-01"}}]},
        ]
        prov.responder("sis", mensajes, [])
        tool_result = seen["payload"]["messages"][1]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "call_1")
        self.assertIn("2026-11-01", tool_result["content"])


class OpenAiAdapterTest(unittest.TestCase):
    def test_prompt_tokens_including_cached_is_normalised_to_disjoint_counts(self):
        # House shape (duna-lotes-api agente/proveedores/openai.py): /v1/responses, usage under
        # input_tokens/input_tokens_details.cached_tokens, not the old chat-completions shape.
        data = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Listo."}]}],
            "usage": {"input_tokens": 500, "output_tokens": 20,
                      "input_tokens_details": {"cached_tokens": 400}},
        }
        prov = cliente.OpenAI("gpt-x", "sk-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "hola"}]}], [])
        # OpenAI's input_tokens (500) INCLUDES the 400 cached tokens -- entrada excludes them.
        self.assertEqual(resp.uso, cliente.Uso(entrada=100, salida=20, cache_lectura=400, cache_escritura=0))

    def test_tool_call_is_mapped_to_a_llamada_herramienta(self):
        data = {
            "output": [{"type": "function_call", "call_id": "call_9", "name": "leer_turnos", "arguments": "{}"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        prov = cliente.OpenAI("gpt-x", "sk-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        self.assertEqual(resp.bloques, [cliente.LlamadaHerramienta("call_9", "leer_turnos", {})])


class GeminiAdapterTest(unittest.TestCase):
    def test_prompt_token_count_including_cached_is_normalised(self):
        data = {
            "candidates": [{"content": {"parts": [{"text": "Hola"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 15, "cachedContentTokenCount": 250},
        }
        prov = cliente.Gemini("gemini-x", "key-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "hola"}]}], [])
        self.assertEqual(resp.uso, cliente.Uso(entrada=50, salida=15, cache_lectura=250, cache_escritura=0))

    def test_function_call_is_mapped_to_a_llamada_herramienta(self):
        data = {
            "candidates": [{"content": {"parts": [{"functionCall": {"name": "leer_ajustes", "args": {}}}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
        }
        prov = cliente.Gemini("gemini-x", "key-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        self.assertEqual(resp.bloques, [cliente.LlamadaHerramienta("leer_ajustes", "leer_ajustes", {})])


_ENV_PROVEEDORES = ("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_MODELO", "BODY_AGENTE_ANTHROPIC_API_KEY",
                    "BODY_AGENTE_MODELO_ANTHROPIC", "BODY_AGENTE_OPENAI_API_KEY", "BODY_AGENTE_MODELO_OPENAI",
                    "BODY_AGENTE_GEMINI_API_KEY", "BODY_AGENTE_MODELO_GEMINI")


class ProveedorConfiguradoTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_borrar_env(*_ENV_PROVEEDORES))
        for n in _ENV_PROVEEDORES:
            os.environ.pop(n, None)

    def test_no_key_is_not_configured(self):
        self.assertIsNone(cliente.proveedor_configurado())

    def test_anthropic_key_configures_the_default_provider(self):
        # BODY-ASISTENTE-PROVEEDORES-1 decision 1: anthropic keeps working with no model env var
        # at all, falling back to MODELO_DEFAULT -- today's config keeps working unchanged.
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-fake"
        prov = cliente.proveedor_configurado()
        self.assertIsInstance(prov, cliente.Anthropic)

    def test_openai_selected_by_env_configures_at_the_standard_model_once_keyed(self):
        os.environ["BODY_AGENTE_PROVEEDOR"] = "openai"
        self.assertIsNone(cliente.proveedor_configurado())  # nothing configured at all
        os.environ["BODY_AGENTE_OPENAI_API_KEY"] = "sk-fake-openai"
        # Key alone now configures it, at Duna's standard model (BODY-MODELOS-ESTANDAR-1 decision 1).
        prov = cliente.proveedor_configurado()
        self.assertIsInstance(prov, cliente.OpenAI)
        self.assertEqual(prov.modelo, cliente.MODELO_DEFAULT_OPENAI)
        os.environ["BODY_AGENTE_MODELO_OPENAI"] = "gpt-x"
        self.assertEqual(cliente.proveedor_configurado().modelo, "gpt-x")


class NoKeyInUrlTest(unittest.TestCase):
    """BODY-ASISTENTE-PROVEEDOR-1-M fix (a): the key travels in exactly one auth header, never in
    a URL or query parameter -- no HTTP log can carry it. A recording fake at the `httpx.post`
    boundary, for all three adapters."""

    @staticmethod
    def _transporte_grabador(respuesta_json):
        llamadas = []

        def _post(url, *, json=None, timeout=None, headers=None, params=None):
            llamadas.append({"url": url, "params": params, "headers": dict(headers or {})})

            class _Respuesta:
                def raise_for_status(self):
                    pass

                def json(self):
                    return respuesta_json
            return _Respuesta()
        return llamadas, _post

    def test_anthropic_key_is_only_in_the_x_api_key_header(self):
        llamadas, post = self._transporte_grabador(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}})
        with patch("httpx.post", side_effect=post):
            prov = cliente.Anthropic("claude-sonnet-5-5", "sk-clave-secreta")
            prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        llamada = llamadas[0]
        self.assertNotIn("sk-clave-secreta", llamada["url"])
        self.assertIsNone(llamada["params"])
        self.assertEqual(llamada["headers"].get("x-api-key"), "sk-clave-secreta")

    def test_openai_key_is_only_in_the_authorization_header(self):
        llamadas, post = self._transporte_grabador(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}})
        with patch("httpx.post", side_effect=post):
            prov = cliente.OpenAI("gpt-x", "sk-clave-secreta-openai")
            prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        llamada = llamadas[0]
        self.assertNotIn("sk-clave-secreta-openai", llamada["url"])
        self.assertIsNone(llamada["params"])
        self.assertEqual(llamada["headers"].get("authorization"), "Bearer sk-clave-secreta-openai")

    def test_gemini_key_is_only_in_the_x_goog_api_key_header_never_in_params(self):
        llamadas, post = self._transporte_grabador(
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
             "usageMetadata": {}})
        with patch("httpx.post", side_effect=post):
            prov = cliente.Gemini("gemini-x", "key-clave-secreta")
            prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        llamada = llamadas[0]
        self.assertNotIn("key-clave-secreta", llamada["url"])
        self.assertIsNone(llamada["params"])
        self.assertEqual(llamada["headers"].get("x-goog-api-key"), "key-clave-secreta")


class DemoProviderTest(unittest.TestCase):
    """BODY-ASISTENTE-PANEL-1 decision 8: demo is selected only by the env flag, never default,
    and makes no network call."""

    def setUp(self):
        self.addCleanup(_borrar_env("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_ANTHROPIC_API_KEY"))
        os.environ.pop("BODY_AGENTE_PROVEEDOR", None)
        os.environ.pop("BODY_AGENTE_ANTHROPIC_API_KEY", None)

    def test_demo_is_never_selected_without_the_env_flag(self):
        self.assertIsNone(cliente.proveedor_configurado())
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-fake"
        self.assertNotIsInstance(cliente.proveedor_configurado(), cliente.Demo)

    def test_demo_env_flag_selects_it_with_no_key_needed(self):
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"
        self.assertIsInstance(cliente.proveedor_configurado(), cliente.Demo)

    def test_demo_makes_no_network_call_across_its_two_scripted_rounds(self):
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"
        with patch("httpx.post", side_effect=AssertionError("Demo must never call the network")):
            prov = cliente.proveedor_configurado()
            self.assertIsInstance(prov, cliente.Demo)
            mensajes = [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "¿cómo voy?"}]}]
            r1 = prov.responder("sis", mensajes, [])
            self.assertEqual(r1.motivo_fin, "tool_use")
            self.assertEqual(r1.uso, cliente.Uso())
            self.assertEqual(r1.bloques[0].nombre, "leer_ajustes")
            r2 = prov.responder("sis", mensajes, [])
            self.assertEqual(r2.motivo_fin, "end_turn")
            self.assertEqual(r2.uso, cliente.Uso())


class NoLoggedBodyTest(unittest.TestCase):
    """Decision 1: never log a request or response body, a prompt, a tool result, or a key --
    only an error's class name or HTTP status."""

    def test_a_failed_call_logs_only_the_error_class_never_the_prompt_or_key(self):
        # No `transporte` injected: this exercises the real `_post_real` path (its except clauses,
        # where the logging happens), with httpx.post itself replaced so nothing reaches the network.
        prov = cliente.Anthropic("claude-sonnet-5-5", "sk-super-secreta")
        with patch("httpx.post", side_effect=httpx.ConnectError("no network in this test")):
            with self.assertLogs("body.agente.cliente", level="WARNING") as captura:
                with self.assertRaises(cliente.ProveedorError):
                    prov.responder("un secreto de salud del sistema", [
                        {"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "mi FC en reposo es 55"}]}
                    ], [])
        texto_logs = " ".join(captura.output)
        self.assertNotIn("sk-super-secreta", texto_logs)
        self.assertNotIn("FC en reposo", texto_logs)
        self.assertNotIn("secreto de salud", texto_logs)


if __name__ == "__main__":
    unittest.main()
