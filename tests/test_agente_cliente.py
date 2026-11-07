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
        data = {
            "choices": [{"message": {"role": "assistant", "content": "Listo."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 20,
                      "prompt_tokens_details": {"cached_tokens": 400}},
        }
        prov = cliente.OpenAI("gpt-x", "sk-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "hola"}]}], [])
        # OpenAI's prompt_tokens (500) INCLUDES the 400 cached tokens -- entrada excludes them.
        self.assertEqual(resp.uso, cliente.Uso(entrada=100, salida=20, cache_lectura=400, cache_escritura=0))

    def test_tool_call_is_mapped_to_a_llamada_herramienta(self):
        data = {
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_9", "type": "function",
                 "function": {"name": "leer_turnos", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
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


class ProveedorConfiguradoTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_borrar_env("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_MODELO",
                                    "BODY_AGENTE_ANTHROPIC_API_KEY", "BODY_AGENTE_OPENAI_API_KEY",
                                    "BODY_AGENTE_GEMINI_API_KEY"))
        for n in ("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_MODELO", "BODY_AGENTE_ANTHROPIC_API_KEY",
                 "BODY_AGENTE_OPENAI_API_KEY", "BODY_AGENTE_GEMINI_API_KEY"):
            os.environ.pop(n, None)

    def test_no_key_is_not_configured(self):
        self.assertIsNone(cliente.proveedor_configurado())

    def test_anthropic_key_configures_the_default_provider(self):
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-fake"
        prov = cliente.proveedor_configurado()
        self.assertIsInstance(prov, cliente.Anthropic)

    def test_openai_selected_by_env_needs_its_own_key(self):
        os.environ["BODY_AGENTE_PROVEEDOR"] = "openai"
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-fake"  # the wrong provider's key
        self.assertIsNone(cliente.proveedor_configurado())
        os.environ["BODY_AGENTE_OPENAI_API_KEY"] = "sk-fake-openai"
        self.assertIsInstance(cliente.proveedor_configurado(), cliente.OpenAI)


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
