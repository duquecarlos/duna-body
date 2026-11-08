"""BODY-PRIMER-DIA-1: three first-day fixes from BODY-PRIMER-DIA-CENSO-1 (OBSERVED 2026-11-07) --
the Cuenta callback's refusal is localised (not plain English), the hosted empty state says WHEN
data will arrive, and «Primera mirada a Hoy» no longer waits on «Vincular este PC». Invented demo
data only, no real health figures.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, dispositivo, garmin_cuenta, identity, privacidad, tutoriales, web
from body.i18n import T

DUMMY_CIPHERTEXT = b"\x00" * 16  # invented, not a real token: _conectado() only checks row existence
DUMMY_NONCE = b"\x00" * 12


def _esc(s: str) -> str:
    """Jinja autoescapes i18n text into HTML entities; match what the page actually renders."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


def _fresh_client(case: unittest.TestCase) -> tuple[TestClient, "db.sqlite3.Connection"]:
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return TestClient(web.app, follow_redirects=False), conn


class _HostedModeTestCase(unittest.TestCase):
    """Hosted mode, person resolved by overriding the dependency directly (BODY-DISPOSITIVO-1's own
    test convention)."""

    def setUp(self):
        self.addCleanup(tutoriales._manifest_cache.clear)
        self.client, self.conn = _fresh_client(self)
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1

    def _connect_garmin(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO garmin_cuenta (person_id, token_cifrado, nonce, conectado_en, ultimo_error) "
                "VALUES (1, ?, ?, '2026-11-01T00:00:00+00:00', NULL)",
                (DUMMY_CIPHERTEXT, DUMMY_NONCE),
            )


# ---- 1. Sign-in refused: localised, not a plain-English dead end -----------------------------------

class CallbackRefusedTest(_HostedModeTestCase):
    def _get(self, **headers):
        with mock.patch.object(cuenta, "handle_callback", side_effect=cuenta.FlowRefused("code")):
            return self.client.get("/login/cuenta/callback", params={"code": "c", "state": "s"},
                                    headers=headers)

    def test_status_400_html(self):
        r = self._get()
        self.assertEqual(r.status_code, 400)
        self.assertIn("text/html", r.headers["content-type"])

    def test_spanish_text_and_retry_link(self):
        r = self._get()
        self.assertIn(_t("es", "acceso_rechazado.title"), r.text)
        self.assertIn(_t("es", "acceso_rechazado.text"), r.text)
        self.assertIn('href="/login"', r.text)
        self.assertIn(_t("es", "acceso_rechazado.retry"), r.text)

    def test_no_english_on_the_spanish_page(self):
        r = self._get()
        self.assertNotIn("Sign-in refused", r.text)
        self.assertNotIn("Sign in again", r.text)

    def test_no_internal_reason_in_body(self):
        r = self._get()
        self.assertNotIn("FlowRefused", r.text)
        self.assertNotIn("Traceback", r.text)

    def test_accept_language_does_not_change_it(self):
        """The person is not known yet. Measured (not assumed): no route in body/web.py reads
        Accept-Language -- sin_acceso and the Cuenta-down page both always render in T(DEFAULT_LANG)
        regardless of the request's headers, and this page follows that same, actual pattern."""
        r = self._get(**{"Accept-Language": "en"})
        self.assertIn(_t("es", "acceso_rechazado.title"), r.text)


# ---- 2. Empty state says WHEN data will arrive, in hosted mode -------------------------------------

class HostedEmptySyncTextTest(_HostedModeTestCase):
    def test_connected_watch_says_backfill_is_coming(self):
        self._connect_garmin()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "sync.hosted_connected"), r.text)
        self.assertNotIn(_t("es", "sync.none_yet"), r.text)

    def test_connected_text_names_no_clock_time(self):
        self._connect_garmin()
        r = self.client.get("/")
        for hhmm in (":00", ":30", "AM", "PM", "am", "pm"):
            self.assertNotIn(hhmm, _t("es", "sync.hosted_connected"))

    def test_not_connected_keeps_old_text_and_adds_garmin_link(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "sync.runs"), r.text)
        self.assertIn(_t("es", "sync.none_yet"), r.text)
        self.assertIn('href="/garmin"', r.text)
        self.assertIn(_t("es", "sync.connect_watch"), r.text)

    def test_english_connected(self):
        self._connect_garmin()
        self.client.post("/settings/language", data={"lang": "en"})
        r = self.client.get("/")
        self.assertIn(_t("en", "sync.hosted_connected"), r.text)


class LocalModeSyncTextUnchangedTest(unittest.TestCase):
    """Local mode keeps the plain sentence: BODY-PRIMER-DIA-1 only changes hosted mode."""

    def test_no_sync_yet_local_mode(self):
        self.assertNotIn(cuenta.ISSUER_ENV, os.environ)
        client, conn = _fresh_client(self)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "sync.runs"), r.text)
        self.assertIn(_t("es", "sync.none_yet"), r.text)
        self.assertNotIn('href="/garmin"', r.text)
        self.assertNotIn(_t("es", "sync.hosted_connected"), r.text)


# ---- 3. Primeros pasos: «Primera mirada a Hoy» no longer gated by «Vincular este PC» ---------------

class PrimerosPasosNoPcGateTest(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def test_consented_and_has_data_without_pc_completes_primera_mirada(self):
        """BODY-TURNOS-OPCIONAL-4: `vincular-este-pc` only applies to a shift worker or someone who
        already linked a device, so this subject answers "works shifts" to keep seeing the item."""
        privacidad.record_consent(self.conn, 1)
        db.set_metric(self.conn, "2026-01-01", "resting_hr", 58, "garmin", 1)
        db.set_trabaja_turnos(self.conn, True, 1)
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertTrue(items["primera-mirada-a-hoy"])
        self.assertFalse(items["vincular-este-pc"])

    def test_consented_without_data_does_not_complete_primera_mirada(self):
        privacidad.record_consent(self.conn, 1)
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertFalse(items["primera-mirada-a-hoy"])

    def test_vincular_este_pc_still_its_own_item(self):
        privacidad.record_consent(self.conn, 1)
        db.set_metric(self.conn, "2026-01-01", "resting_hr", 58, "garmin", 1)
        dispositivo.crear(self.conn, 1, "Mi PC", {"turnos:subir"})
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertTrue(items["vincular-este-pc"])
        self.assertTrue(items["primera-mirada-a-hoy"])


if __name__ == "__main__":
    unittest.main()
