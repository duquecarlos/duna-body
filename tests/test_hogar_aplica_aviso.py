"""BODY-APERTURA-AUTOMATICA-1 ruling 1: Hogar's own outside applies (`aplicada_via = 'hogar'`,
BODY-HOGAR-ESCRIBE-1/2) get their OWN 24h aviso line on Hoy, directly after the personal
assistant's own mcp line -- never merged into one "your assistant" sentence, since Hogar is a
different outside app, not the person's personal assistant (the hub's own "one line per outside
origin" ruling). Mirrors tests/test_mcp_visible.py's own HoyAplicadoLineaTest, one layer up: both
lines must be able to show at once, each counting only its own origin. Invented demo data only, no
real health figures, no network.
"""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, identity, web
from body.agente import conversacion as agente_conversacion
from body.agente import propuestas as agente_propuestas
from body.rutas import comun
from body.rutas import hoy as web_hoy
from tests._rutas import contar_sql
from tests.test_rendimiento import _CEILINGS

# BODY-DEMO-HOY-FIJO-1: _reset_db below seeds the demo with no `today=` and every test here reads
# the real clock through comun._today_local (every test hits "/") -- pinned so the suite never
# depends on which real day it runs on.
_ANCHOR = date(2026, 11, 20)


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "hogar_aplica_aviso.db"
    conn = db.connect()
    db.ensure_ready(conn)
    demo._seed(conn, dias=30, idioma="es", today=_ANCHOR)
    conn.commit()
    return conn, orig


def _crear_propuesta(conn, person_id: int, origen: str, start_day: str = "2020-01-01") -> tuple:
    """A proposal still waiting, in a conversation tagged `origen` ('hogar' or 'mcp'). Returns
    (conversacion_id, propuesta_id)."""
    conversacion_id = agente_conversacion.crear(conn, person_id, origen=origen)
    registro = agente_propuestas.crear(conn, person_id, conversacion_id, "marcar_periodo",
                                        {"kind": "viaje", "start_day": start_day, "end_day": None, "note": None})
    return conversacion_id, registro["id"]


def _crear_aplicada(conn, person_id: int, via: str, resuelta_en: str, start_day: str = "2020-01-01") -> int:
    """A proposal as if that outside caller had already applied it itself (BODY-HOGAR-ESCRIBE-1/2,
    BODY-MCP-APLICAR-1): a `via`-origin conversation holding one `propuesta_agente` row in state
    'aplicada', `aplicada_via = via`, with a controllable `resuelta_en` so a test can place it
    inside or outside Hoy's 24h window without sleeping. Returns the conversation id."""
    conversacion_id, propuesta_id = _crear_propuesta(conn, person_id, via, start_day)
    conn.execute(
        "UPDATE propuesta_agente SET estado = 'aplicada', resuelta_en = ?, aplicada_via = ? WHERE id = ?",
        (resuelta_en, via, propuesta_id),
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
        patcher = mock.patch.object(comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(web.app, follow_redirects=False)


class HogarAplicadoLineaTest(_BaseTest):
    def test_no_line_when_there_is_no_recent_hogar_apply(self):
        r = self.client.get("/")
        self.assertNotIn("Hogar aplicó", r.text)

    def test_one_recent_apply_singular_wording_and_link(self):
        ahora = datetime.now(timezone.utc)
        cid = _crear_aplicada(self.conn, self.person_id, "hogar", ahora.isoformat(timespec="seconds"))
        r = self.client.get("/")
        self.assertIn("Hogar aplicó 1 cambio · Ver", r.text)
        self.assertIn(f"/?asis_conversacion={cid}#asis-btn", r.text)

    def test_two_recent_applies_plural_wording_and_newest_link(self):
        ahora = datetime.now(timezone.utc)
        _crear_aplicada(self.conn, self.person_id, "hogar", ahora.isoformat(timespec="seconds"),
                        start_day="2020-01-01")
        cid2 = _crear_aplicada(self.conn, self.person_id, "hogar", ahora.isoformat(timespec="seconds"),
                               start_day="2020-02-01")
        r = self.client.get("/")
        self.assertIn("Hogar aplicó 2 cambios · Ver", r.text)
        self.assertIn(f"/?asis_conversacion={cid2}#asis-btn", r.text)

    def test_english_wording(self):
        db.set_language(self.conn, "en", self.person_id)
        self.conn.commit()
        ahora = datetime.now(timezone.utc)
        _crear_aplicada(self.conn, self.person_id, "hogar", ahora.isoformat(timespec="seconds"))
        r = self.client.get("/")
        self.assertIn("Hogar applied 1 change · View", r.text)

    def test_line_is_gone_past_the_24h_window(self):
        viejo = datetime.now(timezone.utc) - timedelta(hours=25)
        _crear_aplicada(self.conn, self.person_id, "hogar", viejo.isoformat(timespec="seconds"))
        r = self.client.get("/")
        self.assertNotIn("Hogar aplicó", r.text)

    def test_injectable_clock_crosses_the_edge_with_no_sleeping(self):
        ahora = datetime(2026, 1, 2, tzinfo=timezone.utc)
        resuelta = (ahora - timedelta(hours=23)).isoformat(timespec="seconds")
        _crear_aplicada(self.conn, self.person_id, "hogar", resuelta)
        dentro = web_hoy._hogar_aplicado_ctx(self.conn, self.person_id, ahora=ahora)
        self.assertEqual(dentro["n"], 1)
        fuera = web_hoy._hogar_aplicado_ctx(self.conn, self.person_id, ahora=ahora + timedelta(hours=2))
        self.assertIsNone(fuera)

    def test_the_persons_own_tap_on_a_hogar_origin_proposal_never_raises_the_line(self):
        """Mirrors tests/test_mcp_visible.py's own HoyLineaTest: applying through Body's own web
        route (agente_propuestas.aplicar) never sets aplicada_via, so it is not an outside apply."""
        _, pid = _crear_propuesta(self.conn, self.person_id, "hogar")
        resultado = agente_propuestas.aplicar(self.conn, self.person_id, pid)
        self.assertEqual(resultado["estado"], "aplicada")
        r = self.client.get("/")
        self.assertNotIn("Hogar aplicó", r.text)

    def test_an_mcp_apply_and_a_hogar_apply_both_show_their_own_line_counting_only_their_own(self):
        ahora = datetime.now(timezone.utc)
        cid_mcp = _crear_aplicada(self.conn, self.person_id, "mcp", ahora.isoformat(timespec="seconds"),
                                  start_day="2020-01-01")
        cid_hogar = _crear_aplicada(self.conn, self.person_id, "hogar", ahora.isoformat(timespec="seconds"),
                                    start_day="2020-02-01")
        r = self.client.get("/")
        self.assertIn("Tu asistente personal aplicó 1 cambio · Ver", r.text)
        self.assertIn("Hogar aplicó 1 cambio · Ver", r.text)
        self.assertIn(f"/?asis_conversacion={cid_mcp}#asis-btn", r.text)
        self.assertIn(f"/?asis_conversacion={cid_hogar}#asis-btn", r.text)

    def test_another_persons_hogar_apply_is_never_counted(self):
        otro_id = [r["id"] for r in self.conn.execute("SELECT id FROM person ORDER BY id") if r["id"] != self.person_id][0]
        ahora = datetime.now(timezone.utc)
        _crear_aplicada(self.conn, otro_id, "hogar", ahora.isoformat(timespec="seconds"))
        r = self.client.get("/")
        self.assertNotIn("Hogar aplicó", r.text)

    def test_hogar_line_appears_directly_after_the_mcp_line(self):
        """Ruling 1: one line per outside origin, in order, never merged -- the hogar <div> follows
        the mcp <div> immediately in the rendered markup."""
        ahora = datetime.now(timezone.utc)
        _crear_aplicada(self.conn, self.person_id, "mcp", ahora.isoformat(timespec="seconds"),
                        start_day="2020-01-01")
        _crear_aplicada(self.conn, self.person_id, "hogar", ahora.isoformat(timespec="seconds"),
                        start_day="2020-02-01")
        r = self.client.get("/")
        idx_mcp = r.text.index("Tu asistente personal aplicó")
        idx_hogar = r.text.index("Hogar aplicó")
        self.assertLess(idx_mcp, idx_hogar)

    def test_statement_ceiling_for_slash_is_unchanged_by_the_new_line(self):
        _crear_aplicada(self.conn, self.person_id, "hogar",
                        datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with contar_sql() as stats:
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(stats["statements"], _CEILINGS["/"])


if __name__ == "__main__":
    unittest.main()
