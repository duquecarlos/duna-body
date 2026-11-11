"""ORCH-CARLOS-DELEGATES-TIER-1-1: a proposal's own lifetime depends on which channel made it
(ruling 1), and applying one of four tools replaces a still-pending same-day sibling of the same
tool (ruling 2). Invented demo data only, no real health figures, no network.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "vigencia.db"
    conn = db.connect()
    db.init(conn)
    conn.commit()
    return conn, orig


def _cid(conn, person_id: int = 1, origen: str | None = None) -> int:
    return agente_conversacion.crear(conn, person_id, origen=origen)


class _VigenciaTestCase(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))


class FinSiguienteDiaLocalTest(unittest.TestCase):
    """The pure boundary math, isolated from the clock and from any database (ruling 1)."""

    def test_midnight_two_local_days_after_the_day_it_was_made(self):
        persona = {"timezone": "America/Bogota"}  # UTC-5 year round, no DST
        ahora = datetime(2026, 11, 11, 23, 50, tzinfo=timezone.utc)  # 2026-11-11 18:50 local
        limite = propuestas._fin_siguiente_dia_local(ahora, persona)
        self.assertEqual(limite, datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc))

    def test_falls_back_to_bogota_with_no_timezone_on_the_row(self):
        ahora = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)  # 2025-12-31 23:00 local
        limite = propuestas._fin_siguiente_dia_local(ahora, {})
        self.assertEqual(limite, datetime(2026, 1, 2, 5, 0, tzinfo=timezone.utc))


class CrearVigenciaPorCanalTest(_VigenciaTestCase):
    def test_in_app_proposal_still_expires_in_15_minutes(self):
        cid = _cid(self.conn)  # origen None: the in-app chat panel
        registro = propuestas.crear(self.conn, 1, cid, "proponer_idioma", {"lang": "en"})
        creada = datetime.fromisoformat(registro["creada_en"])
        expira = datetime.fromisoformat(registro["expira_en"])
        self.assertEqual(expira - creada, timedelta(minutes=propuestas.EXPIRA_MINUTOS))

    def test_mcp_proposal_lives_until_end_of_the_next_local_day(self):
        cid = _cid(self.conn, origen="mcp")
        registro = propuestas.crear(self.conn, 1, cid, "proponer_idioma", {"lang": "en"})
        creada = datetime.fromisoformat(registro["creada_en"])
        esperado = propuestas._fin_siguiente_dia_local(creada, db.person(self.conn, 1))
        self.assertEqual(datetime.fromisoformat(registro["expira_en"]), esperado)

    def test_mcp_proposal_still_valid_a_day_later_but_not_two_days_later(self):
        cid = _cid(self.conn, origen="mcp")
        registro = propuestas.crear(self.conn, 1, cid, "proponer_idioma", {"lang": "en"})
        creada = datetime.fromisoformat(registro["creada_en"])
        # Safe regardless of the real time of day at creation: the minimum lifetime is "the rest
        # of today plus all of tomorrow" (just over 24h, if created a second before local
        # midnight), and the maximum is exactly 48h (if created right after local midnight).
        dentro_de_la_ventana = creada + timedelta(hours=20)
        fuera_de_la_ventana = creada + timedelta(days=2, hours=1)
        self.assertFalse(propuestas._vencida(registro, propuestas._iso(dentro_de_la_ventana)))
        self.assertTrue(propuestas._vencida(registro, propuestas._iso(fuera_de_la_ventana)))


class ReemplazoMismoDiaTest(_VigenciaTestCase):
    """Ruling 2: applying one of the four listed tools replaces a still-pending sibling of the
    SAME tool for the SAME day -- never a different day, never a different tool."""

    def test_applying_one_replaces_a_pending_sibling_of_the_same_tool_and_day(self):
        cid = _cid(self.conn)
        out1 = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                      {"day": "2026-11-11", "series": [{"ejercicio": "sentadilla", "reps": 10}]},
                                      "ninguno", cid)
        out2 = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                      {"day": "2026-11-11", "series": [{"ejercicio": "sentadilla", "reps": 12}]},
                                      "ninguno", cid)
        resultado = propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        fila = self.conn.execute("SELECT estado, despues_json FROM propuesta_agente WHERE id = ?",
                                  (out1["propuesta_id"],)).fetchone()
        self.assertEqual(fila["estado"], "descartada")
        self.assertTrue(json.loads(fila["despues_json"])["reemplazada"])
        otra_vez = propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        self.assertEqual(otra_vez["estado"], "ya_resuelta")

    def test_pending_sibling_of_a_different_day_is_left_alone(self):
        cid = _cid(self.conn)
        out1 = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                      {"day": "2026-11-10", "series": [{"ejercicio": "sentadilla", "reps": 10}]},
                                      "ninguno", cid)
        out2 = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                      {"day": "2026-11-11", "series": [{"ejercicio": "sentadilla", "reps": 12}]},
                                      "ninguno", cid)
        propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        fila = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                  (out1["propuesta_id"],)).fetchone()
        self.assertEqual(fila["estado"], "propuesta")

    def test_pending_sibling_of_a_different_tool_is_left_alone(self):
        cid = _cid(self.conn)
        out1 = herramientas.ejecutar(self.conn, 1, "registrar_checkin",
                                      {"day": "2026-11-11", "momento": "despertar", "mood": 3}, "ninguno", cid)
        out2 = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                      {"day": "2026-11-11", "series": [{"ejercicio": "sentadilla", "reps": 12}]},
                                      "ninguno", cid)
        propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        fila = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                  (out1["propuesta_id"],)).fetchone()
        self.assertEqual(fila["estado"], "propuesta")

    def test_card_shows_the_replaced_message_and_cannot_be_applied(self):
        cid = _cid(self.conn)
        out1 = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                      {"day": "2026-11-11", "series": [{"ejercicio": "sentadilla", "reps": 10}]},
                                      "ninguno", cid)
        out2 = herramientas.ejecutar(self.conn, 1, "registrar_entreno",
                                      {"day": "2026-11-11", "series": [{"ejercicio": "sentadilla", "reps": 12}]},
                                      "ninguno", cid)
        propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        client = TestClient(web.app, follow_redirects=False)
        r = client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r.status_code, 200)
        tarjetas = {t["id"]: t for t in r.json()["propuestas"]}
        tarjeta = tarjetas[out1["propuesta_id"]]
        self.assertEqual(tarjeta["despues"], "Reemplazada por la versión aplicada")
        self.assertEqual(tarjeta["estado"], "descartada")
        aplicar_de_nuevo = client.post(f"/asistente/propuestas/{out1['propuesta_id']}/aplicar")
        self.assertEqual(aplicar_de_nuevo.json()["estado"], "ya_resuelta")


if __name__ == "__main__":
    unittest.main()
