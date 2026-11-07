"""BODY-NIVEL-DATOS-UI-1: the «Lo que ve el asistente» screen -- the control the consent text
promises ("al nivel que elijas en Ajustes"). GET/POST /ajustes/asistente, the settings row, and
that the assistant's available tools change with the stored nivel_datos.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, privacidad, web
from body.agente import ajustes, herramientas

client = TestClient(web.app, follow_redirects=False)

PERSON_A = 1
PERSON_B = 2


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "nivel.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


class DefaultSelectionTest(unittest.TestCase):
    def test_completo_preselected_by_default(self):
        _reset_db(self)
        r = client.get("/ajustes/asistente")
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="completo" checked', r.text)
        self.assertNotIn('value="resumen" checked', r.text)
        self.assertNotIn('value="ninguno" checked', r.text)


class PostStoresAndChangesToolsTest(unittest.TestCase):
    def test_post_resumen_stores_it_and_changes_available_tools(self):
        conn = _reset_db(self)
        before = herramientas.disponibles(ajustes.nivel_datos(conn, PERSON_A))

        r = client.post("/ajustes/asistente", data={"nivel": "resumen"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/ajustes/asistente?guardado=1")

        stored = ajustes.nivel_datos(conn, PERSON_A)
        self.assertEqual(stored, "resumen")
        after = herramientas.disponibles(stored)
        self.assertLess(len(after), len(before))

        r = client.get("/ajustes/asistente?guardado=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="resumen" checked', r.text)
        self.assertIn("Guardado.", r.text)


class InvalidValueTest(unittest.TestCase):
    def test_invalid_value_400_and_unchanged(self):
        conn = _reset_db(self)
        r = client.post("/ajustes/asistente", data={"nivel": "mucho"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ajustes.nivel_datos(conn, PERSON_A), "completo")


class TwoPersonsIndependentTest(unittest.TestCase):
    """Hosted helpers, as test_person_scope.py does: two real Cuenta sessions, not dependency
    overrides, so each person's cookie resolves to a different person_id."""

    def setUp(self):
        self.conn = _reset_db(self)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        self.conn.commit()

        env = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        privacidad.record_consent(self.conn, PERSON_A)
        privacidad.record_consent(self.conn, PERSON_B)

        self.token_a = cuenta.create_session(self.conn, PERSON_A, "sid-nivel-a")
        self.token_b = cuenta.create_session(self.conn, PERSON_B, "sid-nivel-b")

    def _client_as(self, token: str) -> TestClient:
        c = TestClient(web.app, follow_redirects=False)
        c.cookies.set(cuenta.SESSION_COOKIE, token)
        return c

    def test_setting_one_persons_level_leaves_the_others_untouched(self):
        ca, cb = self._client_as(self.token_a), self._client_as(self.token_b)

        r = ca.post("/ajustes/asistente", data={"nivel": "ninguno"})
        self.assertEqual(r.status_code, 303)

        self.assertEqual(ajustes.nivel_datos(self.conn, PERSON_A), "ninguno")
        self.assertEqual(ajustes.nivel_datos(self.conn, PERSON_B), "completo")

        ra = ca.get("/ajustes/asistente")
        self.assertIn('value="ninguno" checked', ra.text)
        rb = cb.get("/ajustes/asistente")
        self.assertIn('value="completo" checked', rb.text)


class BothLanguagesRenderTest(unittest.TestCase):
    def test_three_options_render_in_spanish_and_english(self):
        conn = _reset_db(self)
        r = client.get("/ajustes/asistente")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tus cifras día por día", r.text)
        self.assertIn("Solo resúmenes", r.text)
        self.assertIn("Sin cifras", r.text)

        db.set_language(conn, "en", PERSON_A)
        r = client.get("/ajustes/asistente")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your day-by-day figures", r.text)
        self.assertIn("Summaries only", r.text)
        self.assertIn("No figures", r.text)


_PROVEEDOR_ENV = ("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_ANTHROPIC_API_KEY", "BODY_AGENTE_MODELO_ANTHROPIC",
                  "BODY_AGENTE_OPENAI_API_KEY", "BODY_AGENTE_MODELO_OPENAI",
                  "BODY_AGENTE_GEMINI_API_KEY", "BODY_AGENTE_MODELO_GEMINI")


class ProviderRadioTest(unittest.TestCase):
    """BODY-ASISTENTE-PROVEEDORES-1 decision 3: the provider radio shows only with two or more
    providers configured, lists only the configured ones, and a POST stores a per-person choice
    or refuses an unconfigured name."""

    def setUp(self):
        self.conn = _reset_db(self)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        self.conn.commit()
        guardado = {k: os.environ.pop(k, None) for k in _PROVEEDOR_ENV}
        self.addCleanup(lambda: [os.environ.update({k: v}) if v is not None else os.environ.pop(k, None)
                                 for k, v in guardado.items()])

    def test_radio_hidden_with_only_one_provider_configured(self):
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-a"
        r = client.get("/ajustes/asistente")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('name="proveedor"', r.text)

    def test_radio_shows_and_lists_only_configured_providers_with_two_or_more(self):
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-a"
        os.environ["BODY_AGENTE_OPENAI_API_KEY"] = "sk-o"
        os.environ["BODY_AGENTE_MODELO_OPENAI"] = "gpt-x"
        r = client.get("/ajustes/asistente")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="proveedor" value="anthropic"', r.text)
        self.assertIn('name="proveedor" value="openai"', r.text)
        self.assertNotIn('name="proveedor" value="gemini"', r.text)
        self.assertIn("Claude (Anthropic)", r.text)
        self.assertIn("ChatGPT (OpenAI)", r.text)

    def test_stores_a_valid_choice_per_person_person_b_unaffected(self):
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-a"
        os.environ["BODY_AGENTE_OPENAI_API_KEY"] = "sk-o"
        os.environ["BODY_AGENTE_MODELO_OPENAI"] = "gpt-x"

        r = client.post("/ajustes/asistente", data={"nivel": "completo", "proveedor": "openai"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(ajustes.proveedor(self.conn, PERSON_A), "openai")
        self.assertIsNone(ajustes.proveedor(self.conn, PERSON_B))

    def test_refuses_an_unconfigured_name_with_400(self):
        os.environ["BODY_AGENTE_ANTHROPIC_API_KEY"] = "sk-a"
        os.environ["BODY_AGENTE_OPENAI_API_KEY"] = "sk-o"
        os.environ["BODY_AGENTE_MODELO_OPENAI"] = "gpt-x"
        r = client.post("/ajustes/asistente", data={"nivel": "completo", "proveedor": "gemini"})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(ajustes.proveedor(self.conn, PERSON_A))


class SettingsRowShowsCurrentChoiceTest(unittest.TestCase):
    def test_settings_row_shows_current_choice_and_links_the_screen(self):
        conn = _reset_db(self)
        r = client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/ajustes/asistente"', r.text)
        self.assertIn("Tus cifras día por día", r.text)

        ajustes.set_nivel_datos(conn, PERSON_A, "resumen")
        r = client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Solo resúmenes", r.text)


if __name__ == "__main__":
    unittest.main()
