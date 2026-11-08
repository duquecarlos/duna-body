"""BODY-ASISTENTE-PANEL-1: the assistant panel that every signed-in page mounts (agent-ui-v1),
and the `demo` provider that lets it be measured in a browser with no real key."""
import os
import re
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests._rutas import rutas_planas

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
    return {r.path for r in rutas_planas(web.app.routes) if isinstance(r, APIRoute) and "GET" in r.methods}


def _fill(path: str, day: str, goal_id: int) -> str:
    return path.replace("{day}", day).replace("{goal_id}", str(goal_id))


class ConfiguredEveryPageTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig_db_path = _reset_db()
        self.addCleanup(self.conn.close)
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
        self.addCleanup(self.conn.close)
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

    def test_the_status_line_actually_hides_once_it_carries_the_hidden_attribute(self):
        # .asis-status{display:flex} alone beats the UA [hidden]{display:none} rule on origin
        # precedence, so turno.status.hidden=true in JS did nothing without this rule (fix c).
        css = Path(__file__).resolve().parent.parent / "body" / "static" / "body.css"
        text = css.read_text(encoding="utf-8")
        self.assertIn(".asis-status[hidden]{display:none}", text)


class AssistantJsSafetyTest(unittest.TestCase):
    def test_no_innerhtml_assignment_with_model_text_and_no_eval(self):
        js = Path(__file__).resolve().parent.parent / "body" / "static" / "assistant.js"
        text = js.read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", text)
        self.assertNotIn("eval(", text)


class PropuestaCardJsTest(unittest.TestCase):
    """BODY-ASISTENTE-PROPUESTAS-1: the card's Aplicar/Descartar/Deshacer buttons, built the same
    textContent-only way as every other answer node (still covered by AssistantJsSafetyTest above)."""

    @classmethod
    def setUpClass(cls):
        js = Path(__file__).resolve().parent.parent / "body" / "static" / "assistant.js"
        cls.text = js.read_text(encoding="utf-8")

    def test_crea_tarjeta_propuesta_builds_aplicar_and_descartar_buttons(self):
        self.assertIn("function crearTarjetaPropuesta(datos)", self.text)
        self.assertIn("etiquetas.aplicar", self.text)
        self.assertIn("etiquetas.descartar", self.text)
        self.assertIn("etiquetas.deshacer", self.text)

    def test_buttons_post_to_the_propuesta_routes(self):
        self.assertIn('"/asistente/propuestas/" + id + "/" + ruta', self.text)
        self.assertIn('"aplicar"', self.text)
        self.assertIn('"descartar"', self.text)
        self.assertIn('"deshacer"', self.text)

    def test_the_sse_loop_renders_the_propuesta_event_as_a_card(self):
        self.assertIn('nombre === "propuesta"', self.text)
        self.assertIn("crearTarjetaPropuesta(datos)", self.text)

    def test_loaded_conversation_also_redraws_its_proposal_cards(self):
        self.assertIn("(datos.propuestas || []).forEach", self.text)

    def test_the_card_is_queued_and_appended_after_the_steps_and_answer_not_on_arrival(self):
        # BODY-ASISTENTE-PROPUESTAS-1-M fix (b): the propuesta SSE event can arrive before the
        # turn's answer text does, so the card must not be appended to the turn on that event --
        # only queued, then appended once agregarPasos has placed the steps under the question.
        self.assertIn("propuestasPendientes.push(datos)", self.text)
        pasos_idx = self.text.index("agregarPasos(turno.turno, pasos, answerEl)")
        cards_idx = self.text.index("propuestasPendientes.forEach")
        self.assertLess(pasos_idx, cards_idx)


class AssistantJsFixesTest(unittest.TestCase):
    """Text-level checks of the five BODY-ASISTENTE-PANEL-1-M fixes (the orchestrator re-measures
    the real behaviour in a browser after the merge)."""

    @classmethod
    def setUpClass(cls):
        js = Path(__file__).resolve().parent.parent / "body" / "static" / "assistant.js"
        cls.text = js.read_text(encoding="utf-8")

    def test_enter_submits_on_a_computer_and_shift_enter_or_mobile_insert_a_line(self):
        self.assertIn('e.key !== "Enter" || e.shiftKey || esMovil()', self.text)
        self.assertIn("formEl.requestSubmit", self.text)

    def test_a_list_line_starts_or_continues_a_list_without_a_preceding_blank_line(self):
        self.assertIn("function tipoLinea(l)", self.text)
        self.assertIn("function renderLineas(lineas, contenedor)", self.text)
        # The old bug: a whole block rendered as one paragraph unless EVERY line matched the
        # list marker. The fix groups line by line instead.
        self.assertNotIn("lineas.every(function (l) { return /^[-*]", self.text)
        self.assertNotIn("lineas.every(function (l) { return /^\\d+[.)]", self.text)

    def test_the_folded_steps_are_inserted_above_the_answer_not_appended_after_it(self):
        self.assertIn("turno.insertBefore(det, answerEl)", self.text)

    def test_como_plural_is_read_from_the_pipe_separated_i18n_string(self):
        self.assertIn("function formatComo(n)", self.text)
        self.assertIn('String(i18n.como || "{n}").split("|")', self.text)
        self.assertIn("formatComo(pasos.length)", self.text)

    def test_full_size_covers_the_whole_window_and_does_not_reflow_the_main_column(self):
        self.assertIn("return window.innerWidth;", self.text)
        self.assertNotIn("innerWidth - (railVisible", self.text)
        self.assertIn('panelAbierto && !esCompleto ? px + "px" : "0px"', self.text)

    def test_the_panel_fetches_and_renders_the_stored_conversation_on_page_load(self):
        self.assertIn('fetch("/asistente/conversacion/" + id', self.text)
        self.assertIn("resp.status === 404", self.text)
        self.assertIn("limpiarConversacionId();", self.text)
        self.assertIn("var idAlCargar = leerConversacionId();", self.text)


class ComoPluralI18nTest(unittest.TestCase):
    def test_es_and_en_como_carry_a_distinct_singular_and_plural_form(self):
        import json as _json
        base = Path(__file__).resolve().parent.parent / "body" / "i18n"
        es = _json.loads((base / "es.json").read_text(encoding="utf-8"))["asistente.como"]
        en = _json.loads((base / "en.json").read_text(encoding="utf-8"))["asistente.como"]
        for valor in (es, en):
            partes = valor.split("|")
            self.assertEqual(len(partes), 2)
            self.assertNotEqual(partes[0], partes[1])
            self.assertIn("{n}", partes[0])
            self.assertIn("{n}", partes[1])


class DemoProviderTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig_db_path = _reset_db()
        self.addCleanup(self.conn.close)
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


class ConversacionEndpointTest(unittest.TestCase):
    """GET /asistente/conversacion/{id} (fix d): the panel's own turns, never a raw tool result."""

    def setUp(self):
        self.conn, self.orig_db_path = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.gid = _seed(self.conn)
        self.addCleanup(_borrar_env(*_PROVIDER_ENV_VARS))
        for n in _PROVIDER_ENV_VARS:
            os.environ.pop(n, None)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _preguntar_y_obtener_id(self, pregunta: str) -> int:
        r = self.client.post("/asistente/preguntar", json={"pregunta": pregunta, "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        m = re.search(r'"conversacion_id":\s*(\d+)', r.text)
        self.assertIsNotNone(m)
        return int(m.group(1))

    def test_returns_only_the_visible_turns_question_then_answer_with_a_step_count(self):
        cid = self._preguntar_y_obtener_id("¿cómo voy?")
        r = self.client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r.status_code, 200)
        datos = r.json()
        self.assertIn("propuestas", datos)
        turnos = datos["turnos"]
        self.assertEqual(len(turnos), 2)
        self.assertEqual(turnos[0], {"rol": "persona", "texto": "¿cómo voy?", "pasos": 0})
        self.assertEqual(turnos[1]["rol"], "asistente")
        self.assertEqual(turnos[1]["pasos"], 1)
        self.assertIn("Primer punto de ejemplo", turnos[1]["texto"])
        for turno in turnos:
            self.assertNotIn("resultado", turno)
            self.assertNotIn("leer_ajustes", turno.get("texto", ""))

    def test_unknown_conversacion_id_is_404(self):
        r = self.client.get("/asistente/conversacion/999999")
        self.assertEqual(r.status_code, 404)

    def test_another_persons_conversacion_id_is_404(self):
        cid = self._preguntar_y_obtener_id("¿cómo voy?")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 2
        r = self.client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
