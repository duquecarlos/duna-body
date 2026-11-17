"""BODY-APERTURA-AUTOMATICA-1 ruling 2: /garmin shows the SAME live «Tu reloj» card Hoy shows,
built by the SAME context code (body/rutas/garmin.py's own `_garmin_sync_ctx`/`_reloj_vivo_ctx`,
now shared with body/rutas/hoy.py's own today()) -- so a pull started right after connecting is
visible on the connect screen itself, never a second copy of the card or its logic. Connected ->
the card's root element is in /garmin's own HTML; not connected -> absent; GET still writes
nothing (tests/test_get_sin_escritura.py's own guard, exercised again here for this one route with
the new card present). Invented demo data only, no real health figures, no network.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, demo, garmin_cuenta, identity, web
from body.i18n import T
from tests._rutas import contar_sql

_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only", garmin_cuenta.KEY_ENV: "11" * 32}


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "garmin_conectar_vivo.db"
    conn = db.connect()
    db.ensure_ready(conn)
    demo._seed(conn, dias=30, idioma="es")
    conn.commit()
    return conn, orig


class _BaseTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.person_id = db.local_person_id(self.conn)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        patcher = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(web.app, follow_redirects=False)


class CardOnGarminTest(_BaseTest):
    def test_connected_with_an_error_shows_the_cards_root_element(self):
        garmin_cuenta.guardar(self.conn, self.person_id, "tok-demo")
        garmin_cuenta.registrar_error(self.conn, self.person_id, "NotSignedIn")
        self.conn.commit()
        r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertIn(T("es")("reloj_vivo.titulo"), r.text)
        self.assertIn('class="card"', r.text)

    def test_not_connected_shows_no_card(self):
        r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(T("es")("reloj_vivo.titulo"), r.text)

    def test_same_context_code_as_hoy_same_sentence(self):
        """The ruling's own "never a second copy": the exact sentence Hoy's `/` renders for this
        state (BODY-GARMIN-SYNC-CONSISTENTE-1's own not_signed_in text) is the one /garmin shows
        too -- proof the two routes read the same context function, not two independently-worded
        copies."""
        garmin_cuenta.guardar(self.conn, self.person_id, "tok-demo")
        garmin_cuenta.registrar_error(self.conn, self.person_id, "NotSignedIn")
        self.conn.commit()
        r_hoy = self.client.get("/")
        r_garmin = self.client.get("/garmin")
        self.assertIn(T("es")("today.garmin_sync.not_signed_in"), r_hoy.text)
        self.assertIn(T("es")("today.garmin_sync.not_signed_in"), r_garmin.text)

    def test_get_writes_nothing_connected(self):
        garmin_cuenta.guardar(self.conn, self.person_id, "tok-demo")
        garmin_cuenta.registrar_error(self.conn, self.person_id, "NotSignedIn")
        self.conn.commit()
        with contar_sql() as stats:
            r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(stats["writes"], 0)

    def test_get_writes_nothing_not_connected(self):
        with contar_sql() as stats:
            r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(stats["writes"], 0)


if __name__ == "__main__":
    unittest.main()
