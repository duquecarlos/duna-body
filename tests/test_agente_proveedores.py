"""BODY-ASISTENTE-PROVEEDORES-1: every provider available at once, the selection rule across
them, the OpenAI adapter's house shape (duna-lotes-api agente/proveedores/openai.py), and
Gemini's thoughtSignature carry-through (fix for LOTES-API-GEMINI-THOUGHT-SIGNATURE-1's measured
400 on a thinking model's second tool-call turn)."""
import os
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import ajustes, bucle, cliente, conversacion

_ENV_NAMES = ("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_MODELO",
              "BODY_AGENTE_ANTHROPIC_API_KEY", "BODY_AGENTE_MODELO_ANTHROPIC",
              "BODY_AGENTE_OPENAI_API_KEY", "BODY_AGENTE_MODELO_OPENAI",
              "BODY_AGENTE_GEMINI_API_KEY", "BODY_AGENTE_MODELO_GEMINI")


def _borrar_env(*nombres):
    guardado = {n: os.environ.pop(n, None) for n in nombres}

    def _restaurar():
        for n, v in guardado.items():
            if v is not None:
                os.environ[n] = v
            else:
                os.environ.pop(n, None)
    return _restaurar


def _conn(case: unittest.TestCase):
    c = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


class MembershipTest(unittest.TestCase):
    """Decision 1: a provider is present only when BOTH its key AND its model are set."""

    def setUp(self):
        self.addCleanup(_borrar_env(*_ENV_NAMES))
        for n in _ENV_NAMES:
            os.environ.pop(n, None)

    def test_key_alone_configures_openai_and_gemini_at_the_standard_model(self):
        # BODY-MODELOS-ESTANDAR-1 decision 1: code defaults for all three providers mean a key
        # alone turns a provider on, at Duna's standard model -- unchanged from decision 1's "key
        # AND model" gate in code, since the model line now always resolves to a default.
        os.environ["BODY_AGENTE_OPENAI_API_KEY"] = "sk-fake"
        os.environ["BODY_AGENTE_GEMINI_API_KEY"] = "key-fake"
        configurados = cliente.proveedores_configurados()
        self.assertEqual(set(configurados), {"openai", "gemini"})
        self.assertEqual(configurados["openai"].modelo, cliente.MODELO_DEFAULT_OPENAI)
        self.assertEqual(configurados["gemini"].modelo, cliente.MODELO_DEFAULT_GEMINI)

    def test_model_alone_does_not_configure_openai_or_gemini(self):
        os.environ["BODY_AGENTE_MODELO_OPENAI"] = "gpt-x"
        os.environ["BODY_AGENTE_MODELO_GEMINI"] = "gemini-x"
        self.assertEqual(cliente.proveedores_configurados(), {})

    def test_key_and_model_together_configure_every_provider(self):
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-a"
        os.environ["BODY_AGENTE_OPENAI_API_KEY"] = "sk-o"
        os.environ["BODY_AGENTE_MODELO_OPENAI"] = "gpt-x"
        os.environ["BODY_AGENTE_GEMINI_API_KEY"] = "key-g"
        os.environ["BODY_AGENTE_MODELO_GEMINI"] = "gemini-x"
        configurados = cliente.proveedores_configurados()
        self.assertEqual(set(configurados), {"anthropic", "openai", "gemini"})
        self.assertIsInstance(configurados["anthropic"], cliente.Anthropic)
        self.assertIsInstance(configurados["openai"], cliente.OpenAI)
        self.assertIsInstance(configurados["gemini"], cliente.Gemini)

    def test_anthropic_keeps_working_with_only_body_agente_modelo_or_nothing(self):
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-a"
        configurados = cliente.proveedores_configurados()
        self.assertEqual(configurados["anthropic"].modelo, cliente.MODELO_DEFAULT)

        os.environ["BODY_AGENTE_MODELO"] = "claude-otro"
        configurados = cliente.proveedores_configurados()
        self.assertEqual(configurados["anthropic"].modelo, "claude-otro")

        os.environ["BODY_AGENTE_MODELO_ANTHROPIC"] = "claude-especifico"
        configurados = cliente.proveedores_configurados()
        self.assertEqual(configurados["anthropic"].modelo, "claude-especifico")


class SelectionOrderTest(unittest.TestCase):
    """Decision 2: person's stored choice > BODY_AGENTE_PROVEEDOR > first configured."""

    def setUp(self):
        self.addCleanup(_borrar_env(*_ENV_NAMES))
        for n in _ENV_NAMES:
            os.environ.pop(n, None)
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-a"
        os.environ["BODY_AGENTE_OPENAI_API_KEY"] = "sk-o"
        os.environ["BODY_AGENTE_MODELO_OPENAI"] = "gpt-x"
        self.conn = _conn(self)

    def test_first_configured_wins_with_no_env_default_and_no_person_choice(self):
        disponibles = cliente.proveedores_configurados()
        self.assertEqual(cliente.nombre_elegido(disponibles, self.conn, 1), "anthropic")

    def test_env_default_overrides_the_order_when_it_is_configured(self):
        os.environ["BODY_AGENTE_PROVEEDOR"] = "openai"
        disponibles = cliente.proveedores_configurados()
        self.assertEqual(cliente.nombre_elegido(disponibles, self.conn, 1), "openai")

    def test_the_persons_own_choice_overrides_the_env_default(self):
        os.environ["BODY_AGENTE_PROVEEDOR"] = "anthropic"
        ajustes.set_proveedor(self.conn, 1, "openai")
        disponibles = cliente.proveedores_configurados()
        self.assertEqual(cliente.nombre_elegido(disponibles, self.conn, 1), "openai")

    def test_an_unconfigured_stored_choice_falls_through_to_the_env_default(self):
        ajustes.set_proveedor(self.conn, 1, "gemini")  # not configured: no gemini key/model
        os.environ["BODY_AGENTE_PROVEEDOR"] = "openai"
        disponibles = cliente.proveedores_configurados()
        self.assertEqual(cliente.nombre_elegido(disponibles, self.conn, 1), "openai")

    def test_an_unconfigured_stored_choice_with_no_env_default_falls_to_first_configured(self):
        ajustes.set_proveedor(self.conn, 1, "gemini")
        disponibles = cliente.proveedores_configurados()
        self.assertEqual(cliente.nombre_elegido(disponibles, self.conn, 1), "anthropic")


class OpenAiHouseShapeTest(unittest.TestCase):
    def test_request_matches_the_house_shape(self):
        seen = {}

        def transporte(payload):
            seen["payload"] = payload
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

        prov = cliente.OpenAI("gpt-x", "sk-fake", transporte=transporte)
        mensajes = [
            {"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "hola"}]},
            {"rol": "asistente", "bloques": [
                {"tipo": "texto", "texto": "ok"},
                {"tipo": "llamada_herramienta", "id": "call_1", "nombre": "leer_hoy", "parametros": {}},
            ]},
            {"rol": "usuario", "bloques": [
                {"tipo": "resultado_herramienta", "id": "call_1", "nombre": "leer_hoy", "resultado": {"ok": True}},
            ]},
        ]
        prov.responder("eres el asistente", mensajes,
                       [{"nombre": "leer_hoy", "descripcion": "x", "parametros": {}}])

        self.assertEqual(cliente.OpenAI.URL, "https://api.openai.com/v1/responses")
        payload = seen["payload"]
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["instructions"], "eres el asistente")
        self.assertNotIn("stream", payload)
        items = payload["input"]
        self.assertEqual(items[0], {"role": "user", "content": [{"type": "input_text", "text": "hola"}]})
        self.assertEqual(items[1], {"role": "assistant", "content": [{"type": "output_text", "text": "ok"}]})
        self.assertEqual(items[2], {"type": "function_call", "call_id": "call_1", "name": "leer_hoy",
                                    "arguments": "{}"})
        self.assertEqual(items[3], {"type": "function_call_output", "call_id": "call_1",
                                    "output": '{"ok": true}'})
        self.assertEqual(payload["tools"], [{"type": "function", "name": "leer_hoy", "description": "x",
                                             "parameters": {}}])

    def test_text_and_two_function_calls_yield_both_calls_in_order(self):
        data = {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Mirando..."}]},
            {"type": "function_call", "call_id": "call_1", "name": "leer_hoy", "arguments": "{}"},
            {"type": "function_call", "call_id": "call_2", "name": "leer_turnos", "arguments": "{}"},
        ], "usage": {"input_tokens": 5, "output_tokens": 2}}
        prov = cliente.OpenAI("gpt-x", "sk-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        self.assertEqual(resp.bloques, [
            cliente.BloqueTexto("Mirando..."),
            cliente.LlamadaHerramienta("call_1", "leer_hoy", {}),
            cliente.LlamadaHerramienta("call_2", "leer_turnos", {}),
        ])

    def test_usage_entrada_excludes_cached_tokens(self):
        data = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 500, "output_tokens": 20,
                          "input_tokens_details": {"cached_tokens": 450}}}
        prov = cliente.OpenAI("gpt-x", "sk-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        self.assertEqual(resp.uso, cliente.Uso(entrada=50, salida=20, cache_lectura=450, cache_escritura=0))

    def test_incomplete_details_is_not_a_complete_answer(self):
        data = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "a medias"}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "incomplete_details": {"reason": "max_output_tokens"}}
        prov = cliente.OpenAI("gpt-x", "sk-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        self.assertEqual(resp.motivo_fin, "max_output_tokens")
        self.assertNotEqual(resp.motivo_fin, "completed")


class GeminiThoughtSignatureTest(unittest.TestCase):
    def test_first_call_signed_second_not_calls_carry_firma_or_none(self):
        data = {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "leer_hoy", "args": {}}, "thoughtSignature": "firma-1"},
            {"functionCall": {"name": "leer_turnos", "args": {}}},
        ]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}}
        prov = cliente.Gemini("gemini-x", "key-fake", transporte=lambda payload: data)
        resp = prov.responder("sis", [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "x"}]}], [])
        llamadas = [b for b in resp.bloques if isinstance(b, cliente.LlamadaHerramienta)]
        self.assertEqual(llamadas[0].firma, "firma-1")
        self.assertIsNone(llamadas[1].firma)

    def test_next_request_echoes_the_signature_as_a_sibling_on_that_part_only(self):
        seen = {}

        def transporte(payload):
            seen["payload"] = payload
            return {"candidates": [{"content": {"parts": [{"text": "listo"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {}}

        prov = cliente.Gemini("gemini-x", "key-fake", transporte=transporte)
        mensajes = [
            {"rol": "asistente", "bloques": [
                {"tipo": "llamada_herramienta", "id": "leer_hoy", "nombre": "leer_hoy", "parametros": {},
                 "firma": "firma-1"},
                {"tipo": "llamada_herramienta", "id": "leer_turnos", "nombre": "leer_turnos", "parametros": {}},
            ]},
            {"rol": "usuario", "bloques": [
                {"tipo": "resultado_herramienta", "id": "leer_hoy", "nombre": "leer_hoy", "resultado": {}},
                {"tipo": "resultado_herramienta", "id": "leer_turnos", "nombre": "leer_turnos", "resultado": {}},
            ]},
        ]
        prov.responder("sis", mensajes, [])
        partes = seen["payload"]["contents"][0]["parts"]
        self.assertEqual(partes[0]["thoughtSignature"], "firma-1")
        self.assertIn("functionCall", partes[0])
        self.assertNotIn("thoughtSignature", partes[1])


class GeminiSignaturePersistedAcrossConversationTest(unittest.TestCase):
    """Decision 5: the signature is persisted with the tool call (in the existing JSON column) so
    a later QUESTION in the same conversation still echoes it, not only the next round of the
    same question."""

    def test_signature_persisted_and_echoed_on_a_later_question(self):
        conn = _conn(self)
        conv_id = conversacion.crear(conn, 1)

        respuestas_1 = [
            {"candidates": [{"content": {"parts": [
                {"functionCall": {"name": "leer_ajustes", "args": {}}, "thoughtSignature": "firma-persistida"},
            ]}, "finishReason": "STOP"}], "usageMetadata": {}},
            {"candidates": [{"content": {"parts": [{"text": "listo"}]}, "finishReason": "STOP"}],
             "usageMetadata": {}},
        ]
        prov1 = cliente.Gemini("gemini-x", "key-fake", transporte=lambda payload: respuestas_1.pop(0))
        resultado = bucle.ejecutar(conn, 1, prov1, "sis",
                                   [{"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "pregunta uno"}]}],
                                   conv_id)

        conversacion.agregar_mensaje(conn, conv_id, 1, "usuario", [{"tipo": "texto", "texto": "pregunta uno"}])
        for m in resultado.nuevos_mensajes:
            conversacion.agregar_mensaje(conn, conv_id, 1, m["rol"], m["bloques"])

        historial = conversacion.historial(conn, conv_id, 1)
        llamada_guardada = next(
            b for m in historial for b in m["bloques"] if b.get("tipo") == "llamada_herramienta"
        )
        self.assertEqual(llamada_guardada.get("firma"), "firma-persistida")

        seen = {}

        def transporte2(payload):
            seen["payload"] = payload
            return {"candidates": [{"content": {"parts": [{"text": "segunda respuesta"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {}}

        prov2 = cliente.Gemini("gemini-x", "key-fake", transporte=transporte2)
        historial.append({"rol": "usuario", "bloques": [{"tipo": "texto", "texto": "pregunta dos"}]})
        bucle.ejecutar(conn, 1, prov2, "sis", historial, conv_id)

        parte_llamada = next(
            parte for mensaje in seen["payload"]["contents"] for parte in mensaje["parts"]
            if "functionCall" in parte
        )
        self.assertEqual(parte_llamada["thoughtSignature"], "firma-persistida")


if __name__ == "__main__":
    unittest.main()
