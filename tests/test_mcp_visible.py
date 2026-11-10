"""BODY-MCP-LOCAL-1-B: a proposal made through the local MCP server (body/mcp.py, origen='mcp')
must be visible to the person, not just to a database row -- Hoy's own line (none/one/two, gone
once the last pending card is resolved, in both languages), the line's link naming the right
conversation, and the assistant panel's own default when it opens with no conversation named.
Invented demo data only, no real health figures, no network.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, identity, web
from body.agente import conversacion as agente_conversacion
from body.agente import propuestas as agente_propuestas
from body.rutas import hoy as web_hoy
from tests._rutas import contar_sql
from tests.test_rendimiento import _CEILINGS


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "mcp_visible.db"
    conn = db.connect()
    db.ensure_ready(conn)
    demo._seed(conn, dias=30, idioma="es")
    conn.commit()
    return conn, orig


def _crear_propuesta_mcp(conn, person_id: int, start_day: str = "2020-01-01") -> tuple:
    """A proposal as if it had arrived through body/mcp.py: a conversation tagged `origen='mcp'`
    holding one `propuesta_agente` row still in state 'propuesta'. Returns (conversacion_id,
    propuesta_id)."""
    conversacion_id = agente_conversacion.crear(conn, person_id, origen="mcp")
    registro = agente_propuestas.crear(conn, person_id, conversacion_id, "marcar_periodo",
                                        {"kind": "viaje", "start_day": start_day, "end_day": None, "note": None})
    return conversacion_id, registro["id"]


def _crear_aplicada_mcp(conn, person_id: int, resuelta_en: str, start_day: str = "2020-01-01") -> int:
    """A proposal as if body/mcp.py's own `aplicar_propuesta` tool had already applied it
    (BODY-MCP-APLICAR-1): an mcp-origin conversation holding one `propuesta_agente` row in state
    'aplicada', with a controllable `resuelta_en` so a test can place it inside or outside Hoy's
    24h window without sleeping. Returns the conversation id."""
    conversacion_id = agente_conversacion.crear(conn, person_id, origen="mcp")
    registro = agente_propuestas.crear(conn, person_id, conversacion_id, "marcar_periodo",
                                        {"kind": "viaje", "start_day": start_day, "end_day": None, "note": None})
    conn.execute(
        "UPDATE propuesta_agente SET estado = 'aplicada', resuelta_en = ?, aplicada_via = 'mcp' WHERE id = ?",
        (resuelta_en, registro["id"]),
    )
    conn.commit()
    return conversacion_id


class _BaseTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.person_id = db.local_person_id(self.conn)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        self.client = TestClient(web.app, follow_redirects=False)


class HoyLineaTest(_BaseTest):
    def test_no_line_when_there_is_no_pending_mcp_proposal(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("asis_conversacion", r.text)

    def test_one_pending_proposal_singular_wording_and_link(self):
        cid, _ = _crear_propuesta_mcp(self.conn, self.person_id)
        r = self.client.get("/")
        self.assertIn("Tu asistente personal te propone 1 cambio · Revisar", r.text)
        self.assertIn(f"/?asis_conversacion={cid}#asis-btn", r.text)

    def test_two_pending_proposals_plural_wording_and_newest_link(self):
        _crear_propuesta_mcp(self.conn, self.person_id, start_day="2020-01-01")
        cid2, _ = _crear_propuesta_mcp(self.conn, self.person_id, start_day="2020-02-01")
        r = self.client.get("/")
        self.assertIn("Tu asistente personal te propone 2 cambios · Revisar", r.text)
        self.assertIn(f"/?asis_conversacion={cid2}#asis-btn", r.text)

    def test_english_wording(self):
        db.set_language(self.conn, "en", self.person_id)
        self.conn.commit()
        _crear_propuesta_mcp(self.conn, self.person_id)
        r = self.client.get("/")
        self.assertIn("Your personal assistant suggests 1 change · Review", r.text)

    def test_line_disappears_after_applying_the_last_pending_card(self):
        _, pid = _crear_propuesta_mcp(self.conn, self.person_id)
        resultado = agente_propuestas.aplicar(self.conn, self.person_id, pid)
        self.assertEqual(resultado["estado"], "aplicada")
        r = self.client.get("/")
        self.assertNotIn("asis_conversacion", r.text)

    def test_line_disappears_after_dismissing_the_last_pending_card(self):
        _, pid = _crear_propuesta_mcp(self.conn, self.person_id)
        resultado = agente_propuestas.descartar(self.conn, self.person_id, pid)
        self.assertEqual(resultado["estado"], "descartada")
        r = self.client.get("/")
        self.assertNotIn("asis_conversacion", r.text)

    def test_statement_ceiling_for_slash_is_unchanged_by_the_new_line(self):
        _crear_propuesta_mcp(self.conn, self.person_id)
        with contar_sql() as stats:
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(stats["statements"], _CEILINGS["/"])


class HoyAplicadoLineaTest(_BaseTest):
    """BODY-MCP-APLICAR-1 ruling 5: Hoy's own aviso line for 24h after an outside apply, in both
    languages, gone once every such apply has aged past the window -- an injectable clock so the
    edge case needs no sleeping."""

    def test_no_line_when_there_is_no_recent_apply(self):
        r = self.client.get("/")
        self.assertNotIn("Tu asistente personal aplicó", r.text)

    def test_one_recent_apply_singular_wording_and_link(self):
        ahora = datetime.now(timezone.utc)
        cid = _crear_aplicada_mcp(self.conn, self.person_id, ahora.isoformat(timespec="seconds"))
        r = self.client.get("/")
        self.assertIn("Tu asistente personal aplicó 1 cambio · Ver", r.text)
        self.assertIn(f"/?asis_conversacion={cid}#asis-btn", r.text)

    def test_two_recent_applies_plural_wording_and_newest_link(self):
        ahora = datetime.now(timezone.utc)
        _crear_aplicada_mcp(self.conn, self.person_id, ahora.isoformat(timespec="seconds"), start_day="2020-01-01")
        cid2 = _crear_aplicada_mcp(self.conn, self.person_id, ahora.isoformat(timespec="seconds"),
                                   start_day="2020-02-01")
        r = self.client.get("/")
        self.assertIn("Tu asistente personal aplicó 2 cambios · Ver", r.text)
        self.assertIn(f"/?asis_conversacion={cid2}#asis-btn", r.text)

    def test_english_wording(self):
        db.set_language(self.conn, "en", self.person_id)
        self.conn.commit()
        ahora = datetime.now(timezone.utc)
        _crear_aplicada_mcp(self.conn, self.person_id, ahora.isoformat(timespec="seconds"))
        r = self.client.get("/")
        self.assertIn("Your personal assistant applied 1 change · View", r.text)

    def test_line_is_gone_past_the_24h_window(self):
        viejo = datetime.now(timezone.utc) - timedelta(hours=25)
        _crear_aplicada_mcp(self.conn, self.person_id, viejo.isoformat(timespec="seconds"))
        r = self.client.get("/")
        self.assertNotIn("Tu asistente personal aplicó", r.text)

    def test_injectable_clock_crosses_the_edge_with_no_sleeping(self):
        ahora = datetime(2026, 1, 2, tzinfo=timezone.utc)
        resuelta = (ahora - timedelta(hours=23)).isoformat(timespec="seconds")
        _crear_aplicada_mcp(self.conn, self.person_id, resuelta)
        dentro = web_hoy._mcp_aplicado_ctx(self.conn, self.person_id, ahora=ahora)
        self.assertEqual(dentro["n"], 1)
        fuera = web_hoy._mcp_aplicado_ctx(self.conn, self.person_id, ahora=ahora + timedelta(hours=2))
        self.assertIsNone(fuera)


class PanelDefaultTest(_BaseTest):
    def test_no_conversation_named_resolves_to_the_newest_pending_mcp_conversation(self):
        _crear_propuesta_mcp(self.conn, self.person_id, start_day="2020-01-01")
        cid2, _ = _crear_propuesta_mcp(self.conn, self.person_id, start_day="2020-02-01")
        r = self.client.get("/asistente/conversacion/pendiente")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["conversacion_id"], cid2)

    def test_pendiente_is_404_when_there_is_none(self):
        r = self.client.get("/asistente/conversacion/pendiente")
        self.assertEqual(r.status_code, 404)

    def test_pendiente_ignores_an_already_resolved_proposal(self):
        _, pid = _crear_propuesta_mcp(self.conn, self.person_id)
        agente_propuestas.aplicar(self.conn, self.person_id, pid)
        r = self.client.get("/asistente/conversacion/pendiente")
        self.assertEqual(r.status_code, 404)

    def test_a_real_numeric_id_still_resolves_as_before(self):
        cid, _ = _crear_propuesta_mcp(self.conn, self.person_id)
        r = self.client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["conversacion_id"], cid)

    def test_unknown_numeric_id_is_still_404(self):
        r = self.client.get("/asistente/conversacion/999999")
        self.assertEqual(r.status_code, 404)


class AssistantJsReadsTheNamedConversationTest(unittest.TestCase):
    """Source checks (the same style test_asistente_panel.py already uses): assistant.js must read
    a conversation named in the URL, open the panel for it, and otherwise default to the
    'pendiente' sentinel route when nothing is named at all."""

    @classmethod
    def setUpClass(cls):
        js = Path(__file__).resolve().parent.parent / "body" / "static" / "assistant.js"
        cls.text = js.read_text(encoding="utf-8")

    def test_reads_asis_conversacion_from_the_url_and_opens_the_panel(self):
        self.assertIn('"asis_conversacion"', self.text)
        self.assertIn("cargarConversacion(idUrl);\n    abrir();", self.text)

    def test_falls_back_to_the_pendiente_sentinel_when_nothing_is_named(self):
        self.assertIn('cargarConversacion(idAlCargar || "pendiente")', self.text)

    def test_remembers_the_conversation_id_the_server_resolves(self):
        self.assertIn("if (datos.conversacion_id) guardarConversacionId(datos.conversacion_id);", self.text)


if __name__ == "__main__":
    unittest.main()
