"""BODY-MCP-PENDIENTES-VIGENCIA-1: an mcp-origin proposal's expiry is set at creation
(body/agente/propuestas.py's own EXPIRA_MINUTOS or next-local-day rule) but only marked
'expirada' lazily, the next time something touches it (`aplicar`/`descartar`) -- so a proposal
nobody ever opened stayed in state 'propuesta' past its own `expira_en` and kept counting, and
linking, on Hoy's own line and the assistant panel's default forever.
`body.agente.conversacion.pendientes_mcp` now filters on `expira_en > ahora` too, `ahora`
injectable for tests. Invented demo data only, no real health figures, no network.
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
from tests._rutas import contar_sql
from tests.test_rendimiento import _CEILINGS


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "mcp_pendientes.db"
    conn = db.connect()
    db.ensure_ready(conn)
    demo._seed(conn, dias=30, idioma="es")
    conn.commit()
    return conn, orig


def _crear_propuesta_mcp_con_vigencia(conn, person_id: int, creada_en: datetime, expira_en: datetime,
                                       start_day: str = "2020-01-01") -> tuple:
    """An mcp-origin proposal with a directly-controlled lifetime, independent of
    EXPIRA_MINUTOS/the next-local-day rule -- so a test can place `ahora` on either side of its
    own `expira_en` with no sleeping. Returns (conversacion_id, propuesta_id)."""
    conversacion_id = agente_conversacion.crear(conn, person_id, origen="mcp")
    registro = agente_propuestas.crear(conn, person_id, conversacion_id, "marcar_periodo",
                                        {"kind": "viaje", "start_day": start_day, "end_day": None, "note": None})
    conn.execute(
        "UPDATE propuesta_agente SET creada_en = ?, expira_en = ? WHERE id = ?",
        (creada_en.isoformat(timespec="seconds"), expira_en.isoformat(timespec="seconds"), registro["id"]),
    )
    conn.commit()
    return conversacion_id, registro["id"]


class _BaseTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.person_id = db.local_person_id(self.conn)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        self.client = TestClient(web.app, follow_redirects=False)


class VigenciaTest(_BaseTest):
    def test_still_in_force_is_counted_and_linked(self):
        ahora = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
        creada = ahora - timedelta(minutes=10)
        expira = creada + timedelta(minutes=15)
        cid, _ = _crear_propuesta_mcp_con_vigencia(self.conn, self.person_id, creada, expira)
        n, resuelto = agente_conversacion.pendientes_mcp(self.conn, self.person_id, ahora=ahora)
        self.assertEqual(n, 1)
        self.assertEqual(resuelto, cid)

    def test_expired_sixteen_minutes_in_is_not_counted_and_today_shows_no_line(self):
        ahora = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
        creada = ahora - timedelta(minutes=16)
        expira = creada + timedelta(minutes=15)
        _, propuesta_id = _crear_propuesta_mcp_con_vigencia(self.conn, self.person_id, creada, expira)
        n, resuelto = agente_conversacion.pendientes_mcp(self.conn, self.person_id, ahora=ahora)
        self.assertEqual((n, resuelto), (0, None))
        # Today's own line reads the real clock, not the injected one above: place the same row's
        # expira_en just before the real wall clock so the end-to-end page carries no line either,
        # with no sleeping.
        pasado = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.conn.execute("UPDATE propuesta_agente SET expira_en = ? WHERE id = ?",
                           (pasado.isoformat(timespec="seconds"), propuesta_id))
        self.conn.commit()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("asis_conversacion", r.text)

    def test_next_day_expiry_kind_is_counted_until_its_own_expira_en(self):
        creada = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)  # late in the Bogota day
        cid = agente_conversacion.crear(self.conn, self.person_id, origen="mcp")
        registro = agente_propuestas.crear(self.conn, self.person_id, cid, "marcar_periodo",
                                            {"kind": "viaje", "start_day": "2020-01-01", "end_day": None,
                                             "note": None})
        self.conn.execute("UPDATE propuesta_agente SET creada_en = ? WHERE id = ?",
                           (creada.isoformat(timespec="seconds"), registro["id"]))
        self.conn.commit()
        propia_expira = datetime.fromisoformat(
            self.conn.execute("SELECT expira_en FROM propuesta_agente WHERE id = ?",
                               (registro["id"],)).fetchone()["expira_en"])
        poco_antes = propia_expira - timedelta(minutes=1)
        poco_despues = propia_expira + timedelta(minutes=1)
        n_antes, id_antes = agente_conversacion.pendientes_mcp(self.conn, self.person_id, ahora=poco_antes)
        self.assertEqual((n_antes, id_antes), (1, cid))
        n_despues, id_despues = agente_conversacion.pendientes_mcp(self.conn, self.person_id, ahora=poco_despues)
        self.assertEqual((n_despues, id_despues), (0, None))

    def test_newest_conversation_id_skips_an_expired_newer_one(self):
        ahora = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
        cid_vieja, _ = _crear_propuesta_mcp_con_vigencia(
            self.conn, self.person_id, ahora - timedelta(minutes=10), ahora + timedelta(minutes=5),
            start_day="2020-01-01")
        cid_nueva, _ = _crear_propuesta_mcp_con_vigencia(
            self.conn, self.person_id, ahora - timedelta(minutes=1), ahora - timedelta(seconds=30),
            start_day="2020-02-01")
        self.assertGreater(cid_nueva, cid_vieja)
        n, resuelto = agente_conversacion.pendientes_mcp(self.conn, self.person_id, ahora=ahora)
        self.assertEqual((n, resuelto), (1, cid_vieja))

    def test_statement_count_of_today_does_not_grow(self):
        ahora = datetime.now(timezone.utc)
        _crear_propuesta_mcp_con_vigencia(self.conn, self.person_id, ahora - timedelta(minutes=5),
                                           ahora + timedelta(minutes=10))
        with contar_sql() as stats:
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(stats["statements"], _CEILINGS["/"])


if __name__ == "__main__":
    unittest.main()
