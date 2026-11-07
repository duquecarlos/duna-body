"""BODY-ASISTENTE-PANEL-1: the assistant panel that every signed-in page mounts (agent-ui-v1),
and the `demo` provider that lets it be measured in a browser with no real key."""
import os
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from body import config, db, goals, identity, web

# Routes that answer with something other than a signed-in base.html page: a non-HTML API, a
# static asset, a liveness check, or sign-in/consent plumbing the assistant never mounts on.
_NO_BASE_PAGE = {
    "/sw.js", "/healthz", "/login", "/login/cuenta/start", "/login/cuenta/callback",
    "/privacidad/consentimiento", "/privacidad/exportar", "/api/vida/semana",
}

_PROVIDER_ENV_VARS = ("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_MODELO", "BODY_AGENTE_ANTHROPIC_API_KEY",
                      "BODY_AGENTE_OPENAI_API_KEY", "BODY_AGENTE_GEMINI_API_KEY")


def _borrar_env(*nombres):
    guardado = {n: os.environ.pop(n, None) for n in nombres}

    def _restaurar():
        for n, v in guardado.items():
            if v is not None:
                os.environ[n] = v
            else:
                os.environ.pop(n, None)
    return _restaurar


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "g.db"
    conn = db.connect()
    db.init(conn)
    return conn, orig


def _seed(conn) -> int:
    for i in range(20):
        day = f"2026-10-{i + 1:02d}"
        db.set_metric(conn, day, "hrv_last_night", 55 + i % 5, "garmin", 1)
        db.set_metric(conn, day, "sleep_minutes", 400 + i, "garmin", 1)
        db.set_metric(conn, day, "resting_hr", 58 - i % 3, "garmin", 1)
        db.set_metric(conn, day, "body_battery_wake", 70 + i % 10, "garmin", 1)
        db.set_metric(conn, day, "stress_avg", 20 + i % 5, "garmin", 1)
        db.set_metric(conn, day, "steps", 7000 + i * 50, "garmin", 1)
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, source) VALUES "
        "(1, '2026-10-05', 'night', '2026-10-05T22:00:00', 'manual')"
    )
    gid = goals.propose(conn, 1, "Bajar FC en reposo", "2026-10-01", metric_key="resting_hr",
                        direction="lower", target_value=50.0, baseline_value=60.0, deadline="2026-12-01")
    goals.activate(conn, gid)
    conn.commit()
    return gid


def _get_paths():
    return {r.path for r in web.app.routes if isinstance(r, APIRoute) and "GET" in r.methods}


def _fill(path: str, day: str, goal_id: int) -> str:
    return path.replace("{day}", day).replace("{goal_id}", str(goal_id))


class ConfiguredEveryPageTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig_db_path = _reset_db()
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.gid = _seed(self.conn)
        self.addCleanup(_borrar_env(*_PROVIDER_ENV_VARS))
        for n in _PROVIDER_ENV_VARS:
            os.environ.pop(n, None)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"
        self.client = TestClient(web.app, follow_redirects=False)

    def test_every_base_page_renders_the_button_and_loads_the_versioned_script(self):
        script_tag = f'src="{web.static_url("assistant.js")}"'
        pages_checked = 0
        for path in _get_paths():
            if path in _NO_BASE_PAGE:
                continue
            r = self.client.get(_fill(path, "2026-10-05", self.gid))
            if r.headers.get("content-type", "").split(";")[0] != "text/html":
                continue
            pages_checked += 1
            self.assertEqual(r.status_code, 200, path)
            self.assertIn('id="asis-btn"', r.text, path)
            self.assertIn('id="asis-panel"', r.text, path)
            self.assertIn(script_tag, r.text, path)
        self.assertGreater(pages_checked, 5)

    def test_panel_markup_has_aria_labels_and_the_size_control(self):
        r = self.client.get("/")
        self.assertIn('aria-label=', r.text)
        self.assertIn('id="asis-size"', r.text)
        self.assertIn('role="dialog"', r.text)
        self.assertIn('aria-modal="true"', r.text)


class NotConfiguredNoButtonTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig_db_path = _reset_db()
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(_borrar_env(*_PROVIDER_ENV_VARS))
        for n in _PROVIDER_ENV_VARS:
            os.environ.pop(n, None)
        self.client = TestClient(web.app, follow_redirects=False)

    def test_no_provider_configured_renders_no_button_and_no_script(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('id="asis-btn"', r.text)
        self.assertNotIn("assistant.js", r.text)


class CssContentTest(unittest.TestCase):
    def test_message_list_contains_the_scroll_containment_rule(self):
        css = Path(__file__).resolve().parent.parent / "body" / "static" / "body.css"
        text = css.read_text(encoding="utf-8")
        self.assertIn("overscroll-behavior: contain", text)

    def test_the_sub_768px_full_screen_rule_exists(self):
        css = Path(__file__).resolve().parent.parent / "body" / "static" / "body.css"
        text = css.read_text(encoding="utf-8")
        self.assertIn("max-width:767.98px", text)
        self.assertIn(".asis-panel", text)


class AssistantJsSafetyTest(unittest.TestCase):
    def test_no_innerhtml_assignment_with_model_text_and_no_eval(self):
        js = Path(__file__).resolve().parent.parent / "body" / "static" / "assistant.js"
        text = js.read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", text)
        self.assertNotIn("eval(", text)


class DemoProviderTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig_db_path = _reset_db()
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.gid = _seed(self.conn)
        self.addCleanup(_borrar_env(*_PROVIDER_ENV_VARS))
        for n in _PROVIDER_ENV_VARS:
            os.environ.pop(n, None)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def test_a_full_demo_exchange_streams_herramienta_then_texto_then_fin_in_order(self):
        r = self.client.post("/asistente/preguntar",
                             json={"pregunta": "¿cómo voy?", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        herramienta_idx = r.text.index("event: herramienta")
        texto_idx = r.text.index("event: texto")
        fin_idx = r.text.index("event: fin")
        self.assertLess(herramienta_idx, texto_idx)
        self.assertLess(texto_idx, fin_idx)
        self.assertIn("leer_ajustes", r.text)


if __name__ == "__main__":
    unittest.main()
