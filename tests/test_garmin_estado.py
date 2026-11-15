"""BODY-GARMIN-SYNC-CONSISTENTE-1 rulings 2-3: Hoy's own compact sync card (each variant, in both
languages, absent when the person has no Garmin connection and no error) and the assistant's
leer_estado_reloj tool (its shape, nivel 'ninguno'). Invented demo data only, no network.
"""
import os
import unittest
from datetime import timedelta
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import cuenta, garmin_cuenta, identity, web
from body.agente import herramientas
from body.i18n import T
from body.rutas import comun
from tests.test_estados_vacios import TODAY, _reset_db, _seed_B, _seed_F

_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only", garmin_cuenta.KEY_ENV: "11" * 32}


def _use(conn, person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = (lambda pid=person_id: pid)


class GarminSyncCardTest(unittest.TestCase):
    """Ruling 2: the compact card on "/", each variant, in es and en, present/absent as the
    ruling says. Hosted (the card reads garmin_cuenta, a hosted-only table in practice)."""

    def setUp(self):
        self.conn = _reset_db(self)
        self.addCleanup(web.app.dependency_overrides.clear)
        patcher = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(web.app, follow_redirects=False)

    def test_not_signed_in_shows_its_own_sentence_and_a_garmin_link(self):
        for lang in ("es", "en"):
            ids = _seed_B(self.conn, lang)
            garmin_cuenta.guardar(self.conn, ids["person_id"], "tok-demo")
            garmin_cuenta.registrar_error(self.conn, ids["person_id"], "NotSignedIn")
            self.conn.commit()
            _use(self.conn, ids["person_id"])
            t = T(lang)
            r = self.client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn(t("today.garmin_sync.not_signed_in"), r.text, lang)
            self.assertIn('href="/garmin"', r.text, lang)
            self.assertNotIn(t("today.garmin_sync.failed"), r.text, lang)

    def test_any_other_error_shows_the_generic_sentence(self):
        for lang in ("es", "en"):
            ids = _seed_B(self.conn, lang)
            garmin_cuenta.guardar(self.conn, ids["person_id"], "tok-demo")
            garmin_cuenta.registrar_error(self.conn, ids["person_id"], "ClientConnectorError")
            self.conn.commit()
            _use(self.conn, ids["person_id"])
            t = T(lang)
            r = self.client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn(t("today.garmin_sync.failed"), r.text, lang)
            self.assertNotIn(t("today.garmin_sync.not_signed_in"), r.text, lang)

    def test_no_error_but_stale_shows_the_stale_sentence_with_the_date(self):
        last_day = TODAY - timedelta(40)
        for lang in ("es", "en"):
            ids = _seed_F(self.conn, lang)
            garmin_cuenta.guardar(self.conn, ids["person_id"], "tok-demo")
            self.conn.commit()
            _use(self.conn, ids["person_id"])
            t = T(lang)
            expected = t("today.garmin_sync.stale", date=comun._short_date(t, last_day))
            r = self.client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn(expected, r.text, lang)

    def test_absent_when_not_connected_and_no_error(self):
        ids = _seed_B(self.conn, "es")
        _use(self.conn, ids["person_id"])
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(T("es")("today.garmin_sync.titulo"), r.text)

    def test_absent_when_connected_no_error_and_fresh(self):
        ids = _seed_B(self.conn, "es")  # today's own data, not stale
        garmin_cuenta.guardar(self.conn, ids["person_id"], "tok-demo")
        self.conn.commit()
        _use(self.conn, ids["person_id"])
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(T("es")("today.garmin_sync.titulo"), r.text)

    def test_card_uses_the_card_and_chip_classes(self):
        ids = _seed_B(self.conn, "es")
        garmin_cuenta.guardar(self.conn, ids["person_id"], "tok-demo")
        garmin_cuenta.registrar_error(self.conn, ids["person_id"], "NotSignedIn")
        self.conn.commit()
        _use(self.conn, ids["person_id"])
        r = self.client.get("/")
        start = r.text.index(T("es")("today.garmin_sync.titulo"))
        block = r.text[max(0, start - 200):start + 200]
        self.assertIn('class="card"', block)
        self.assertIn('class="chip warn"', block)


class LeerEstadoRelojTest(unittest.TestCase):
    """Ruling 3: the tool's shape, for the assistant and the desk alike."""

    def setUp(self):
        self.conn = _reset_db(self)
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: "11" * 32})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shape_with_no_garmin_connection(self):
        ids = _seed_B(self.conn, "es")
        out = herramientas.ejecutar(self.conn, ids["person_id"], "leer_estado_reloj", {}, "ninguno")
        self.assertEqual(out["conectado"], False)
        self.assertIsNone(out["conectado_desde"])
        self.assertIsNone(out["ultimo_error"])
        self.assertEqual(out["ultimo_dia_con_datos"], TODAY.isoformat())
        self.assertEqual(len(out["dias_sin_datos_14"]), garmin_cuenta.VENTANA_RELLENO - 1)  # TODAY is stored

    def test_shape_connected_with_an_error(self):
        ids = _seed_B(self.conn, "es")
        garmin_cuenta.guardar(self.conn, ids["person_id"], "tok-demo")
        garmin_cuenta.registrar_error(self.conn, ids["person_id"], "NotSignedIn")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, ids["person_id"], "leer_estado_reloj", {}, "ninguno")
        self.assertTrue(out["conectado"])
        self.assertIsNotNone(out["conectado_desde"])
        self.assertEqual(out["ultimo_error"], "NotSignedIn")

    def test_offered_at_every_data_level(self):
        for nivel in ("completo", "resumen", "ninguno"):
            nombres = {h["nombre"] for h in herramientas.disponibles(nivel)}
            self.assertIn("leer_estado_reloj", nombres, nivel)

    def test_never_writes(self):
        ids = _seed_B(self.conn, "es")
        before = self.conn.total_changes
        herramientas.ejecutar(self.conn, ids["person_id"], "leer_estado_reloj", {}, "ninguno")
        self.assertEqual(self.conn.total_changes, before)


if __name__ == "__main__":
    unittest.main()
