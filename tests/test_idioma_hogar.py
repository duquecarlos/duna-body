"""BODY-IDIOMA-HOGAR-1: the one-tap language switch in base.html's shared header (ruling 1), and
the assistant's own "answer in the person's language" sentence appended to `agente.sistema`
(ruling 2). Synthetic homes only -- tests/_isolate.py's import guard runs first in every module.
"""
from tests import _isolate  # noqa: F401,E402

import json
import pathlib
import tempfile
import unittest

from fastapi.testclient import TestClient

from body import config, db, web
from body.rutas import sesion

ROOT = pathlib.Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / "body" / "i18n"
BASE_HTML = (ROOT / "body" / "templates" / "base.html").read_text(encoding="utf-8")


def _fresh_client() -> TestClient:
    config.DB_PATH = pathlib.Path(tempfile.mkdtemp(dir=_isolate.TMP)) / "idioma.db"
    conn = db.connect()
    db.init(conn)
    conn.close()
    return TestClient(web.app, follow_redirects=False)


class HeaderSwitchShowsOnlyTheOtherLanguageTest(unittest.TestCase):
    def setUp(self):
        self.client = _fresh_client()

    def test_spanish_page_offers_english(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="idioma-switch"', r.text)
        self.assertIn('name="lang" value="en"', r.text)
        self.assertNotIn('name="lang" value="es"', r.text)
        self.assertIn(">English<", r.text)

    def test_english_page_offers_spanish(self):
        self.client.post("/settings/language", data={"lang": "en"})
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="idioma-switch"', r.text)
        self.assertIn('name="lang" value="es"', r.text)
        self.assertNotIn('name="lang" value="en"', r.text)
        self.assertIn(">Español<", r.text)

    def test_volver_field_holds_the_current_tab_path(self):
        r = self.client.get("/")
        self.assertIn('name="volver" value="/"', r.text)
        r = self.client.get("/settings")
        self.assertIn('name="volver" value="/settings"', r.text)


class LanguageSwitchRedirectTest(unittest.TestCase):
    def setUp(self):
        self.client = _fresh_client()

    def test_volver_to_a_same_site_path_is_honoured(self):
        r = self.client.post("/settings/language", data={"lang": "en", "volver": "/history"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/history")

    def test_protocol_relative_volver_falls_back_to_settings(self):
        r = self.client.post("/settings/language", data={"lang": "en", "volver": "//evil.example"})
        self.assertEqual(r.headers["location"], "/settings")

    def test_absolute_url_volver_falls_back_to_settings(self):
        r = self.client.post("/settings/language",
                             data={"lang": "en", "volver": "https://evil.example/"})
        self.assertEqual(r.headers["location"], "/settings")

    def test_javascript_scheme_volver_falls_back_to_settings(self):
        r = self.client.post("/settings/language", data={"lang": "en", "volver": "javascript:x"})
        self.assertEqual(r.headers["location"], "/settings")

    def test_empty_volver_falls_back_to_settings(self):
        r = self.client.post("/settings/language", data={"lang": "en"})
        self.assertEqual(r.headers["location"], "/settings")


class NoButtonWithoutASignedInPersonTest(unittest.TestCase):
    """The one base.html page rendered with no real person -- Cuenta Duna unreachable mid sign-in
    (body/rutas/sesion.py's `_cuenta_down_page`) -- carries no language-switch form (`tab=None`)."""

    def test_cuenta_down_page_has_no_idioma_switch(self):
        html = sesion._cuenta_down_page().body.decode()
        self.assertNotIn('class="idioma-switch"', html)


class FormWorksWithoutJavaScriptTest(unittest.TestCase):
    """A real <form method=post> with a <button type=submit> -- no script needed to submit it."""

    def test_header_switch_is_a_plain_form(self):
        self.assertIn('<form method="post" action="/settings/language" class="idioma-switch">', BASE_HTML)
        self.assertIn('<button type="submit" class="idioma-switch-btn">', BASE_HTML)


class AgenteSistemaEndsWithTheLanguageSentenceTest(unittest.TestCase):
    def setUp(self):
        self.es = json.loads((I18N_DIR / "es.json").read_text(encoding="utf-8"))
        self.en = json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))

    def test_spanish_sentence(self):
        self.assertTrue(self.es["agente.sistema"].endswith(
            "Responde siempre en español, aunque la persona te escriba en otro idioma; "
            "los nombres de herramientas y los datos se quedan como están."))

    def test_english_sentence(self):
        self.assertTrue(self.en["agente.sistema"].endswith(
            "Always answer in English, even if the person writes in another language; "
            "tool names and data stay as they are."))


if __name__ == "__main__":
    unittest.main()
