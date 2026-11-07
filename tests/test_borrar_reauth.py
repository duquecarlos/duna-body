"""BODY-BORRAR-REAUTH-1: deleting everything on the hosted Body requires a Cuenta sign-in within
the last 10 minutes, besides the typed word (Main's sensitive-data pattern, hub
ORCH-HOST-SENSITIVE-DATA-1, part 3). Invented demo data only, no real health figures.
"""
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, privacidad, web
from tests.test_cuenta_rp import CuentaRpTestCase, ISSUER, SUB


class _ReauthTestCase(CuentaRpTestCase):
    """Signs in, consents, and gives subclasses the resulting person_id and session token."""

    def _sign_in_and_consent(self, **kw) -> tuple[int, str]:
        r = self._sign_in(**kw)
        token = r.cookies.get(cuenta.SESSION_COOKIE)
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id)
        conn.close()
        return person_id, token


class CallbackStoresAuthTimeTest(_ReauthTestCase):
    def test_valid_token_stores_auth_time(self):
        _, token = self._sign_in_and_consent()
        conn = db.connect()
        row = conn.execute(
            "SELECT auth_time FROM sesion WHERE id_hash = ?", (cuenta._sha256_hex(token),)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row["auth_time"])
        self.assertTrue(cuenta.reciente(db.connect(), token))

    def test_token_without_auth_time_stores_null(self):
        _, token = self._sign_in_and_consent(auth_time=None)
        conn = db.connect()
        row = conn.execute(
            "SELECT auth_time FROM sesion WHERE id_hash = ?", (cuenta._sha256_hex(token),)
        ).fetchone()
        conn.close()
        self.assertIsNone(row["auth_time"])

    def test_non_integer_auth_time_stores_null(self):
        _, token = self._sign_in_and_consent(auth_time=None, extra={"auth_time": "not-a-number"})
        conn = db.connect()
        row = conn.execute(
            "SELECT auth_time FROM sesion WHERE id_hash = ?", (cuenta._sha256_hex(token),)
        ).fetchone()
        conn.close()
        self.assertIsNone(row["auth_time"])


class PreMigrationSessionIsStaleTest(_ReauthTestCase):
    def test_a_session_row_with_no_auth_time_is_stale(self):
        """An existing session row from before this slice has NULL auth_time (the column is
        additive); `cuenta.reciente` must treat it as stale, never as recent."""
        conn = db.connect()
        conn.execute(
            "INSERT INTO sesion (id_hash, person_id, created_at, last_seen_at, expires_at) VALUES "
            "('pre-migration-hash', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
            "'2099-01-01T00:00:00+00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM sesion WHERE id_hash = 'pre-migration-hash'").fetchone()
        self.assertIsNone(row["auth_time"])
        self.assertFalse(cuenta.reciente(conn, row))
        conn.close()


class StartLoginMaxAgeTest(_ReauthTestCase):
    def test_start_login_with_max_age_sends_it_alongside_prompt(self):
        conn = db.connect()
        url, _ = cuenta.start_login(conn, "/privacidad", prompt="login", max_age=600)
        conn.close()
        qp = parse_qs(urlparse(url).query)
        self.assertEqual(qp["prompt"][0], "login")
        self.assertEqual(qp["max_age"][0], "600")

    def test_start_login_without_max_age_omits_it(self):
        conn = db.connect()
        url, _ = cuenta.start_login(conn, "/privacidad")
        conn.close()
        self.assertNotIn("max_age", parse_qs(urlparse(url).query))

    def test_login_start_route_accepts_only_literal_600(self):
        auth = self._authorize_params(self.client.get("/login/cuenta/start?prompt=login&max_age=600"))
        self.assertEqual(auth["max_age"], "600")
        self.assertEqual(auth["prompt"], "login")

    def test_login_start_route_ignores_any_other_max_age(self):
        auth = self._authorize_params(self.client.get("/login/cuenta/start?max_age=3600"))
        self.assertNotIn("max_age", auth)

        auth2 = self._authorize_params(self.client.get("/login/cuenta/start?max_age=abc"))
        self.assertNotIn("max_age", auth2)


class BorrarGateTest(_ReauthTestCase):
    def test_stale_session_deletes_nothing_and_redirects_to_fresh_sign_in(self):
        person_id, _ = self._sign_in_and_consent(auth_time=None)
        r = self.client.post("/privacidad/borrar", data={"confirmacion": "BORRAR"})
        self.assertEqual(r.status_code, 303)
        location = r.headers["location"]
        self.assertTrue(location.startswith("/login/cuenta/start?"))
        self.assertIn("prompt=login", location)
        self.assertIn("max_age=600", location)
        return_to_encoded = parse_qs(urlparse(location).query)["return_to"][0]
        self.assertEqual(unquote(return_to_encoded), "/privacidad?reauth=1")
        # nothing deleted
        self.assertEqual(
            self._db_count("SELECT COUNT(*) FROM person WHERE id = ?", person_id), 1
        )
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion"), 1)

    def test_round_trip_never_carries_the_typed_word(self):
        self._sign_in_and_consent(auth_time=None)
        r = self.client.post("/privacidad/borrar", data={"confirmacion": "BORRAR"})
        location = r.headers["location"]
        self.assertNotIn("confirmacion", location)
        self.assertNotIn("BORRAR", location)

    def test_recent_session_and_right_word_deletes(self):
        person_id, _ = self._sign_in_and_consent()
        r = self.client.post("/privacidad/borrar", data={"confirmacion": "BORRAR"})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith(f"{ISSUER}/oauth/logout?"))
        self.assertEqual(
            self._db_count("SELECT COUNT(*) FROM person WHERE id = ?", person_id), 0
        )
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion"), 0)

    def test_recent_session_and_wrong_word_is_unchanged_error_path(self):
        person_id, _ = self._sign_in_and_consent()
        r = self.client.post("/privacidad/borrar", data={"confirmacion": "nope"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/privacidad?error=confirmacion")
        self.assertEqual(
            self._db_count("SELECT COUNT(*) FROM person WHERE id = ?", person_id), 1
        )


class ReauthScreenTest(_ReauthTestCase):
    def test_reauth_ok_line_shown_when_session_is_recent_es(self):
        self._sign_in_and_consent()
        r = self.client.get("/privacidad?reauth=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Listo, ya confirmaste que eres tú", r.text)

    def test_reauth_ok_line_shown_when_session_is_recent_en(self):
        person_id, _ = self._sign_in_and_consent()
        conn = db.connect()
        db.set_language(conn, "en", person_id)
        conn.close()
        r = self.client.get("/privacidad?reauth=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Done, you confirmed it", r.text)

    def test_reauth_fallo_line_shown_when_session_is_stale_es(self):
        self._sign_in_and_consent(auth_time=None)
        r = self.client.get("/privacidad?reauth=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No pudimos confirmar que eres tú", r.text)

    def test_reauth_fallo_line_shown_when_session_is_stale_en(self):
        person_id, _ = self._sign_in_and_consent(auth_time=None)
        conn = db.connect()
        db.set_language(conn, "en", person_id)
        conn.close()
        r = self.client.get("/privacidad?reauth=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("We couldn&#39;t confirm it&#39;s you", r.text)

    def test_reauth_note_shown_only_when_hosted(self):
        self._sign_in_and_consent()
        r = self.client.get("/privacidad")
        self.assertIn("iniciar sesión de nuevo", r.text)

    def test_no_reauth_line_without_the_query_flag(self):
        self._sign_in_and_consent()
        r = self.client.get("/privacidad")
        self.assertNotIn("Listo, ya confirmaste", r.text)
        self.assertNotIn("No pudimos confirmar", r.text)


class ReauthWindowBoundsTest(unittest.TestCase):
    """BODY-BORRAR-REAUTH-1-M: `reciente` must reject an auth_time in the future beyond the
    ID-token verification's own clock-skew allowance (`MARGEN_SEGUNDOS`), not treat a skewed or
    malformed claim as recent indefinitely."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local.db"
        self.conn = db.connect()
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def _row_with_auth_time(self, auth_time_iso):
        id_hash = "hash-" + auth_time_iso
        self.conn.execute(
            "INSERT INTO sesion (id_hash, person_id, created_at, last_seen_at, expires_at, auth_time) "
            "VALUES (?, 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
            "'2099-01-01T00:00:00+00:00', ?)",
            (id_hash, auth_time_iso),
        )
        self.conn.commit()
        return self.conn.execute("SELECT * FROM sesion WHERE id_hash = ?", (id_hash,)).fetchone()

    def _reciente_at_offset(self, offset_seconds: int) -> bool:
        """Builds a session whose `auth_time` is `offset_seconds` before a fixed `now`, then calls
        `reciente` with `cuenta.datetime.now` patched to that same fixed instant -- so the edge
        cases land exactly on the second instead of drifting with how long the DB round-trip
        takes. A negative offset puts `auth_time` in the future."""
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        auth_time = now - timedelta(seconds=offset_seconds)
        row = self._row_with_auth_time(auth_time.isoformat(timespec="seconds"))
        with mock.patch("body.cuenta.datetime") as d:
            d.now.return_value = now
            d.fromisoformat = datetime.fromisoformat
            return cuenta.reciente(self.conn, row)

    def test_auth_time_an_hour_in_the_future_is_not_recent(self):
        self.assertFalse(self._reciente_at_offset(-3600))

    def test_auth_time_a_few_seconds_in_the_future_is_recent(self):
        self.assertTrue(self._reciente_at_offset(-5))

    def test_auth_time_exactly_at_the_600s_edge_is_recent(self):
        self.assertTrue(self._reciente_at_offset(600))

    def test_auth_time_just_past_the_600s_edge_is_not_recent(self):
        self.assertFalse(self._reciente_at_offset(601))


class LocalModeUnchangedTest(unittest.TestCase):
    """Local mode never gates: one person, on their own PC, as before this slice."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        self.client = TestClient(web.app, follow_redirects=False)

    def test_local_mode_deletes_with_the_word_alone(self):
        r = self.client.post("/privacidad/borrar", data={"confirmacion": "BORRAR"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        conn = db.connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM person WHERE id = 1").fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
