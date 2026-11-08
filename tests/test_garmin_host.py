"""BODY-GARMIN-HOST-1: on a hosted Body, each person connects their own Garmin account from
Body's own page and the token is stored AES-256-GCM encrypted, keyed by person. A fake Garmin
client replaces garminconnect.Garmin throughout -- no network, invented demo data only.
"""
import hashlib
import json
import logging
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient

from body import config, cuenta, db, garmin_adapter, garmin_cuenta, privacidad, pull, web
from body.rutas import garmin as garmin_rutas
from tests.test_cuenta_rp import SUB, CuentaRpTestCase

TOKEN_KEY_HEX = "11" * 32
OTHER_KEY_HEX = "22" * 32

GOOD_EMAIL = "demo.garmin@example.invalid"
MFA_EMAIL = "mfa.garmin@example.invalid"
BAD_EMAIL = "bad.garmin@example.invalid"
GOOD_CODE = "123456"
BAD_CODE = "000000"
GOOD_TOKEN = "tok-ok"
MFA_TOKEN = "tok-mfa"
BAD_TOKEN = "tok-bad"
REFRESH_TOKEN = "tok-refresh"
REFRESHED_TOKEN = "tok-refreshed"
MFA_STATE = {"marker": "demo-mfa-state"}


class _FakeClient:
    def __init__(self):
        self.token = None

    def dumps(self) -> str:
        return self.token

    def loads(self, token_str: str) -> None:
        if token_str == BAD_TOKEN:
            raise RuntimeError("rejected demo token")
        self.token = REFRESHED_TOKEN if token_str == REFRESH_TOKEN else token_str


class FakeGarmin:
    """Replaces garminconnect.Garmin in these tests. Invented demo data only."""

    def __init__(self, email=None, password=None, is_cn=False, prompt_mfa=None, return_on_mfa=False, **kw):
        self.email = email
        self.password = password
        self.return_on_mfa = return_on_mfa
        self.client = _FakeClient()

    def login(self, tokenstore=None):
        if tokenstore is not None:
            self.client.loads(tokenstore)
            return None, None
        if self.email == MFA_EMAIL:
            return "needs_mfa", dict(MFA_STATE)
        if self.email == BAD_EMAIL:
            raise RuntimeError("Garmin refused these demo credentials")
        self.client.token = GOOD_TOKEN
        return None, None

    def resume_login(self, client_state, mfa_code):
        if mfa_code != GOOD_CODE:
            raise RuntimeError("Garmin refused this demo code")
        self.client.token = MFA_TOKEN
        return None, None

    def get_user_summary(self, day):
        return {}

    def get_sleep_data(self, day):
        return {}

    def get_hrv_data(self, day):
        return {}

    def get_body_battery(self, day):
        return []

    def get_activities_by_date(self, start, end):
        return []


def _fresh_db(case: unittest.TestCase, name: str):
    orig_db_path = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return conn, orig_db_path


# ---- body/garmin_cuenta.py: AES-256-GCM, keyed by person --------------------------------------

class EncryptionTest(unittest.TestCase):
    def setUp(self):
        self.conn, orig = _fresh_db(self, "enc.db")
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_round_trip(self):
        garmin_cuenta.guardar(self.conn, 1, "a-demo-token-string")
        self.assertEqual(garmin_cuenta.leer(self.conn, 1), "a-demo-token-string")

    def test_db_never_holds_the_plain_token(self):
        garmin_cuenta.guardar(self.conn, 1, "a-demo-token-string")
        row = self.conn.execute("SELECT token_cifrado FROM garmin_cuenta WHERE person_id = 1").fetchone()
        self.assertNotIn(b"a-demo-token-string", bytes(row["token_cifrado"]))

    def test_ciphertext_moved_to_another_person_fails(self):
        garmin_cuenta.guardar(self.conn, 1, "a-demo-token-string")
        row = self.conn.execute("SELECT token_cifrado, nonce FROM garmin_cuenta WHERE person_id = 1").fetchone()
        self.conn.execute(
            "INSERT INTO garmin_cuenta (person_id, token_cifrado, nonce, conectado_en) VALUES "
            "(2, ?, ?, '2026-01-01T00:00:00+00:00')",
            (row["token_cifrado"], row["nonce"]),
        )
        self.conn.commit()
        with self.assertRaises(InvalidTag):
            garmin_cuenta.leer(self.conn, 2)

    def test_wrong_key_fails(self):
        garmin_cuenta.guardar(self.conn, 1, "a-demo-token-string")
        with mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: OTHER_KEY_HEX}):
            with self.assertRaises(InvalidTag):
                garmin_cuenta.leer(self.conn, 1)

    def test_leer_with_no_row_is_none(self):
        self.assertIsNone(garmin_cuenta.leer(self.conn, 1))

    def test_conectado_and_borrar(self):
        garmin_cuenta.guardar(self.conn, 1, "tok")
        self.assertTrue(garmin_cuenta.conectado(self.conn, 1))
        garmin_cuenta.borrar(self.conn, 1)
        self.assertFalse(garmin_cuenta.conectado(self.conn, 1))
        self.assertIsNone(garmin_cuenta.leer(self.conn, 1))


class EnsureConfiguredTest(unittest.TestCase):
    def test_local_mode_never_checks(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(cuenta.ISSUER_ENV, None)
            os.environ.pop(garmin_cuenta.KEY_ENV, None)
            garmin_cuenta.ensure_configured()  # must not raise

    def test_hosted_without_key_refuses_and_names_only_the_variable(self):
        with mock.patch.dict(os.environ):
            os.environ[cuenta.ISSUER_ENV] = "https://fake-cuenta.test"
            os.environ.pop(garmin_cuenta.KEY_ENV, None)
            with self.assertRaises(RuntimeError) as ctx:
                garmin_cuenta.ensure_configured()
            self.assertIn(garmin_cuenta.KEY_ENV, str(ctx.exception))
            self.assertNotIn("=", str(ctx.exception))

    def test_hosted_with_key_passes(self):
        with mock.patch.dict(os.environ):
            os.environ[cuenta.ISSUER_ENV] = "https://fake-cuenta.test"
            os.environ[garmin_cuenta.KEY_ENV] = TOKEN_KEY_HEX
            garmin_cuenta.ensure_configured()  # must not raise

    def test_hosted_with_wrong_length_key_refuses_and_names_only_the_variable(self):
        short_key = "11" * 16  # 16 bytes, not the required 32
        with mock.patch.dict(os.environ):
            os.environ[cuenta.ISSUER_ENV] = "https://fake-cuenta.test"
            os.environ[garmin_cuenta.KEY_ENV] = short_key
            with self.assertRaises(RuntimeError) as ctx:
                garmin_cuenta.ensure_configured()
            self.assertIn(garmin_cuenta.KEY_ENV, str(ctx.exception))
            self.assertIn("openssl rand -hex 32", str(ctx.exception))
            self.assertNotIn(short_key, str(ctx.exception))

    def test_key_of_wrong_length_refuses_at_use_time_too(self):
        conn, orig = _fresh_db(self, "key_len.db")
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        with mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: "aa" * 16}):
            with self.assertRaises(RuntimeError) as ctx:
                garmin_cuenta.guardar(conn, 1, "a-demo-token-string")
            self.assertIn(garmin_cuenta.KEY_ENV, str(ctx.exception))


# ---- hosted web routes --------------------------------------------------------------------------

class GarminHostedTestCase(CuentaRpTestCase):
    def setUp(self):
        super().setUp()
        patcher_key = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher_key.start(); self.addCleanup(patcher_key.stop)
        patcher_garmin = mock.patch("garminconnect.Garmin", FakeGarmin)
        patcher_garmin.start(); self.addCleanup(patcher_garmin.stop)

    def _signed_in_person_id(self, **kw) -> int:
        self._sign_in(**kw)
        conn = db.connect()
        pid = conn.execute(
            "SELECT id FROM person WHERE cuenta_sub = ?", (kw.get("sub", SUB),)
        ).fetchone()["id"]
        privacidad.record_consent(conn, pid)
        conn.close()
        return pid


class SignInOkTest(GarminHostedTestCase):
    def test_sign_in_ok_stores_encrypted_token_never_the_plain_string(self):
        pid = self._signed_in_person_id()
        r = self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/garmin")

        conn = db.connect()
        row = conn.execute("SELECT token_cifrado FROM garmin_cuenta WHERE person_id = ?", (pid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn(GOOD_TOKEN.encode(), bytes(row["token_cifrado"]))
        self.assertEqual(garmin_cuenta.leer(conn, pid), GOOD_TOKEN)
        conn.close()

        r2 = self.client.get("/garmin")
        self.assertEqual(r2.status_code, 200)

    def test_refused_credentials_show_a_generic_message_not_the_exception(self):
        self._signed_in_person_id()
        r = self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=garmin", r.headers["location"])

    def test_missing_email_or_password_is_refused_without_calling_garmin(self):
        self._signed_in_person_id()
        r = self.client.post("/garmin/conectar", data={"email": "", "password": ""})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=datos", r.headers["location"])


class MfaFlowTest(GarminHostedTestCase):
    def setUp(self):
        super().setUp()
        garmin_rutas._MFA_STATE.clear()  # independent of any entry another test left behind in this process-wide dict
        self.addCleanup(garmin_rutas._MFA_STATE.clear)

    def test_needs_mfa_then_finish_with_code_stores_token(self):
        pid = self._signed_in_person_id()
        r1 = self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r1.status_code, 200)
        self.assertIn('name="code"', r1.text)

        r2 = self.client.post("/garmin/codigo", data={"code": GOOD_CODE})
        self.assertEqual(r2.status_code, 303)
        self.assertEqual(r2.headers["location"], "/garmin")
        conn = db.connect()
        self.assertEqual(garmin_cuenta.leer(conn, pid), MFA_TOKEN)
        conn.close()

    def test_wrong_code_re_renders_the_code_form_and_stores_nothing(self):
        pid = self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        r = self.client.post("/garmin/codigo", data={"code": BAD_CODE})
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="code"', r.text)  # the code form stays, not a redirect to email/password
        conn = db.connect()
        self.assertIsNone(garmin_cuenta.leer(conn, pid))
        conn.close()

    def test_wrong_code_can_be_retried_within_the_ttl(self):
        pid = self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.client.post("/garmin/codigo", data={"code": BAD_CODE})
        r = self.client.post("/garmin/codigo", data={"code": GOOD_CODE})
        self.assertEqual(r.status_code, 303)
        conn = db.connect()
        self.assertEqual(garmin_cuenta.leer(conn, pid), MFA_TOKEN)
        conn.close()

    def test_old_mfa_state_is_pruned_on_the_next_write(self):
        self._signed_in_person_id()
        garmin_rutas._MFA_STATE["stale-demo-key"] = (
            json.dumps(MFA_STATE), time.monotonic() - garmin_rutas._MFA_TTL_SECONDS - 1, 0
        )
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.assertNotIn("stale-demo-key", garmin_rutas._MFA_STATE)

    def test_expired_mfa_state_is_refused(self):
        self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        token = self.client.cookies.get(cuenta.SESSION_COOKIE)
        key = hashlib.sha256(token.encode()).hexdigest()
        state_json, _, wrong_codes = garmin_rutas._MFA_STATE[key]
        garmin_rutas._MFA_STATE[key] = (state_json, time.monotonic() - garmin_rutas._MFA_TTL_SECONDS - 1, wrong_codes)

        r = self.client.post("/garmin/codigo", data={"code": GOOD_CODE})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=mfa_expired", r.headers["location"])

    def test_codigo_with_no_pending_state_is_refused(self):
        self._signed_in_person_id()
        r = self.client.post("/garmin/codigo", data={"code": GOOD_CODE})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=mfa_expired", r.headers["location"])


class SignInAttemptLimitTest(GarminHostedTestCase):
    """BODY-SEGURIDAD-4-B: at most 5 /garmin/conectar starts per person in a sliding hour -- the
    6th is refused before the adapter (and so Garmin) is ever called. Every start counts unless it
    ends with a token stored, so a needs_mfa start counts too (BODY-SEGURIDAD-4-B closes the cycle
    that let a person loop conectar -> 5 codes -> conectar -> 5 codes without ever being limited)."""

    def setUp(self):
        super().setUp()
        garmin_rutas._SIGNIN_ATTEMPTS.clear()
        self.addCleanup(garmin_rutas._SIGNIN_ATTEMPTS.clear)

    def test_five_failures_then_sixth_is_refused_without_calling_the_adapter(self):
        self._signed_in_person_id()
        for _ in range(5):
            r = self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})
            self.assertIn("error=garmin", r.headers["location"])
        with mock.patch("body.rutas.garmin.garmin_adapter.start_sign_in") as adapter:
            r = self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})
        self.assertIn("error=demasiados", r.headers["location"])
        adapter.assert_not_called()

    def test_window_slides_after_an_hour_attempts_are_allowed_again(self):
        pid = self._signed_in_person_id()
        for _ in range(5):
            self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})
        stale = time.monotonic() - garmin_rutas._SIGNIN_WINDOW_SECONDS - 1
        garmin_rutas._SIGNIN_ATTEMPTS[pid] = [stale] * 5
        r = self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/garmin")

    def test_success_clears_attempts(self):
        pid = self._signed_in_person_id()
        for _ in range(4):
            self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})
        r = self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/garmin")
        self.assertNotIn(pid, garmin_rutas._SIGNIN_ATTEMPTS)

    def test_empty_form_does_not_count(self):
        pid = self._signed_in_person_id()
        for _ in range(10):
            self.client.post("/garmin/conectar", data={"email": "", "password": ""})
        self.assertNotIn(pid, garmin_rutas._SIGNIN_ATTEMPTS)

    def test_completing_mfa_clears_the_person_so_a_later_start_is_allowed(self):
        pid = self._signed_in_person_id()
        self.addCleanup(garmin_rutas._MFA_STATE.clear)
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.assertIn(pid, garmin_rutas._SIGNIN_ATTEMPTS)
        self.client.post("/garmin/codigo", data={"code": GOOD_CODE})
        self.assertNotIn(pid, garmin_rutas._SIGNIN_ATTEMPTS)

        for _ in range(5):
            r = self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=garmin", r.headers["location"])  # the 5th still reached the adapter

    def test_mfa_cycle_does_not_evade_the_hourly_limit(self):
        # BODY-SEGURIDAD-4-B: looping conectar -> 5 wrong codes -> conectar -> 5 wrong codes, without
        # ever completing MFA, used to never trip the hourly limit. Each needs_mfa start now counts.
        self._signed_in_person_id()
        self.addCleanup(garmin_rutas._MFA_STATE.clear)
        for _ in range(5):
            r = self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
            self.assertEqual(r.status_code, 200)
            for _ in range(5):
                r = self.client.post("/garmin/codigo", data={"code": BAD_CODE})
            self.assertIn("error=mfa_demasiados", r.headers["location"])

        with mock.patch("body.rutas.garmin.garmin_adapter.start_sign_in") as adapter:
            r = self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.assertIn("error=demasiados", r.headers["location"])
        adapter.assert_not_called()

    def test_a_different_person_is_unaffected(self):
        self._signed_in_person_id()
        for _ in range(5):
            self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})

        other_client = TestClient(web.app, follow_redirects=False)
        other_sub = "33333333-3333-3333-3333-333333333333"
        auth = self._authorize_params(other_client.get("/login"))
        self.issuer.next_id_token = self.issuer.id_token(nonce=auth["nonce"], sub=other_sub)
        other_client.get("/login/cuenta/callback", params={"code": "c4", "state": auth["state"]})
        conn = db.connect()
        pid_b = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (other_sub,)).fetchone()["id"]
        privacidad.record_consent(conn, pid_b)
        conn.close()

        r = other_client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/garmin")

    def test_message_renders_in_es_and_en(self):
        self._signed_in_person_id()
        for _ in range(5):
            self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})

        r_es = self.client.get("/garmin?error=demasiados")
        self.assertIn("Demasiados intentos seguidos", r_es.text)

        self.client.post("/settings/language", data={"lang": "en"})
        r_en = self.client.get("/garmin?error=demasiados")
        self.assertIn("Too many attempts in a row", r_en.text)


class MfaAttemptLimitTest(GarminHostedTestCase):
    """BODY-SEGURIDAD-4: at most 5 wrong MFA codes per state -- the 5th drops the state and sends
    the person back to the email form; a right code before that limit still connects."""

    def setUp(self):
        super().setUp()
        garmin_rutas._MFA_STATE.clear()
        self.addCleanup(garmin_rutas._MFA_STATE.clear)
        garmin_rutas._SIGNIN_ATTEMPTS.clear()
        self.addCleanup(garmin_rutas._SIGNIN_ATTEMPTS.clear)

    def test_five_wrong_codes_drop_the_state_and_redirect_to_mfa_demasiados(self):
        self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        for _ in range(4):
            r = self.client.post("/garmin/codigo", data={"code": BAD_CODE})
            self.assertEqual(r.status_code, 200)  # state kept, form re-rendered

        r = self.client.post("/garmin/codigo", data={"code": BAD_CODE})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=mfa_demasiados", r.headers["location"])
        self.assertEqual(len(garmin_rutas._MFA_STATE), 0)

    def test_mfa_demasiados_message_renders_in_es_and_en(self):
        self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        for _ in range(5):
            self.client.post("/garmin/codigo", data={"code": BAD_CODE})

        r_es = self.client.get("/garmin?error=mfa_demasiados")
        self.assertIn("Demasiados códigos equivocados", r_es.text)

        self.client.post("/settings/language", data={"lang": "en"})
        r_en = self.client.get("/garmin?error=mfa_demasiados")
        self.assertIn("Too many wrong codes", r_en.text)

    def test_expired_mfa_state_stays_counted_in_signin_attempts(self):
        pid = self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.assertIn(pid, garmin_rutas._SIGNIN_ATTEMPTS)
        token = self.client.cookies.get(cuenta.SESSION_COOKIE)
        key = hashlib.sha256(token.encode()).hexdigest()
        state_json, _, wrong_codes = garmin_rutas._MFA_STATE[key]
        garmin_rutas._MFA_STATE[key] = (state_json, time.monotonic() - garmin_rutas._MFA_TTL_SECONDS - 1, wrong_codes)

        r = self.client.post("/garmin/codigo", data={"code": GOOD_CODE})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=mfa_expired", r.headers["location"])
        self.assertIn(pid, garmin_rutas._SIGNIN_ATTEMPTS)

    def test_sixth_post_after_the_drop_gets_mfa_expired_since_no_state_remains(self):
        # The 5th wrong code already dropped the state and answered error=demasiados -- a further
        # POST finds nothing pending, which is exactly the existing no-state case (mfa_expired).
        self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        for _ in range(5):
            self.client.post("/garmin/codigo", data={"code": BAD_CODE})

        r = self.client.post("/garmin/codigo", data={"code": BAD_CODE})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=mfa_expired", r.headers["location"])

    def test_right_code_on_the_third_try_still_connects(self):
        pid = self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.client.post("/garmin/codigo", data={"code": BAD_CODE})
        self.client.post("/garmin/codigo", data={"code": BAD_CODE})
        r = self.client.post("/garmin/codigo", data={"code": GOOD_CODE})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/garmin")
        conn = db.connect()
        self.assertEqual(garmin_cuenta.leer(conn, pid), MFA_TOKEN)
        conn.close()


class DesconectarTest(GarminHostedTestCase):
    def test_desconectar_deletes_the_row(self):
        pid = self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})
        conn = db.connect()
        self.assertTrue(garmin_cuenta.conectado(conn, pid))
        conn.close()

        r = self.client.post("/garmin/desconectar")
        self.assertEqual(r.status_code, 303)
        conn = db.connect()
        self.assertFalse(garmin_cuenta.conectado(conn, pid))
        conn.close()


class IsolationTest(GarminHostedTestCase):
    def test_each_person_sees_and_changes_only_their_own_connection(self):
        pid_a = self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})

        other_client = TestClient(web.app, follow_redirects=False)
        other_sub = "11111111-1111-1111-1111-111111111111"
        auth = self._authorize_params(other_client.get("/login"))
        self.issuer.next_id_token = self.issuer.id_token(nonce=auth["nonce"], sub=other_sub)
        other_client.get("/login/cuenta/callback", params={"code": "c2", "state": auth["state"]})
        conn = db.connect()
        pid_b = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (other_sub,)).fetchone()["id"]
        privacidad.record_consent(conn, pid_b)
        conn.close()
        self.assertNotEqual(pid_a, pid_b)

        rb = other_client.get("/garmin")
        self.assertEqual(rb.status_code, 200)
        self.assertIn("No conectado", rb.text)  # B's own page, never A's "connected since" state

        conn = db.connect()
        self.assertFalse(garmin_cuenta.conectado(conn, pid_b))
        self.assertTrue(garmin_cuenta.conectado(conn, pid_a))
        conn.close()

        other_client.post("/garmin/desconectar")
        conn = db.connect()
        self.assertTrue(garmin_cuenta.conectado(conn, pid_a))  # B's desconectar never touches A's row
        conn.close()


class ReconnectInPlaceTest(GarminHostedTestCase):
    def test_connected_with_pull_failure_shows_reconnect_form_above_disconnect(self):
        pid = self._signed_in_person_id()
        conn = db.connect()
        garmin_cuenta.guardar(conn, pid, GOOD_TOKEN)
        garmin_cuenta.registrar_error(conn, pid, "ValueError")
        conn.close()

        r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("última sincronización con Garmin falló", r.text)
        self.assertIn('name="email"', r.text)
        self.assertLess(r.text.index('name="email"'), r.text.index("Desconectar"))

    def test_connected_with_not_signed_in_uses_the_existing_sentence(self):
        pid = self._signed_in_person_id()
        conn = db.connect()
        garmin_cuenta.guardar(conn, pid, GOOD_TOKEN)
        garmin_cuenta.registrar_error(conn, pid, "NotSignedIn")
        conn.close()

        r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Garmin rechazó el acceso guardado", r.text)
        self.assertIn('name="email"', r.text)

    def test_connected_without_error_shows_only_disconnect(self):
        self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})
        r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('name="email"', r.text)
        self.assertIn("Desconectar", r.text)


class RequestLogScrubTest(GarminHostedTestCase):
    def test_connect_request_log_has_no_email_or_password(self):
        self._signed_in_person_id()
        logger = logging.getLogger("body.request")
        original_level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, original_level)
        records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        secret_password = "super-secret-demo-password"
        self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": secret_password})

        combined = " ".join(records)
        self.assertIn("/garmin/conectar", combined)
        self.assertNotIn(GOOD_EMAIL, combined)
        self.assertNotIn(secret_password, combined)


# ---- hosted python -m body.pull: per-person loop ------------------------------------------------

class PullHostedTest(unittest.TestCase):
    def setUp(self):
        self.conn, orig = _fresh_db(self, "pull.db")
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        patcher_key = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher_key.start(); self.addCleanup(patcher_key.stop)
        patcher_garmin = mock.patch("garminconnect.Garmin", FakeGarmin)
        patcher_garmin.start(); self.addCleanup(patcher_garmin.stop)

    def test_one_person_failing_does_not_stop_the_other_and_records_the_error_class(self):
        garmin_cuenta.guardar(self.conn, 1, GOOD_TOKEN)
        garmin_cuenta.guardar(self.conn, 2, BAD_TOKEN)

        ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-01")
        self.assertEqual((ok, failed, saved), (1, 1, 1))
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 2), "NotSignedIn")
        self.assertIsNone(garmin_cuenta.ultimo_error(self.conn, 1))
        self.assertIsNotNone(db.last_sync(self.conn, 1))  # person 1's day was pulled and stored
        self.assertIsNone(db.last_sync(self.conn, 2))  # person 2 failed before anything was stored

    def test_refreshed_token_is_re_encrypted(self):
        garmin_cuenta.guardar(self.conn, 1, REFRESH_TOKEN)
        pull.pull_hosted(self.conn, fecha="2026-11-01")
        self.assertEqual(garmin_cuenta.leer(self.conn, 1), REFRESHED_TOKEN)

    def test_unchanged_token_is_not_rewritten(self):
        garmin_cuenta.guardar(self.conn, 1, GOOD_TOKEN)
        before = self.conn.execute("SELECT nonce FROM garmin_cuenta WHERE person_id = 1").fetchone()["nonce"]
        pull.pull_hosted(self.conn, fecha="2026-11-01")
        after = self.conn.execute("SELECT nonce FROM garmin_cuenta WHERE person_id = 1").fetchone()["nonce"]
        self.assertEqual(before, after)

    def test_person_with_no_garmin_row_is_skipped_quietly(self):
        ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-01")
        self.assertEqual((ok, failed, saved), (0, 0, 0))

    def test_generic_exception_mid_loop_keeps_the_refreshed_token_and_counts_days_stored(self):
        garmin_cuenta.guardar(self.conn, 1, GOOD_TOKEN)
        day_one = garmin_adapter.DayData(day="2026-11-01")
        patcher = mock.patch(
            "body.pull.garmin_adapter.fetch_day",
            side_effect=[(day_one, "tok-refreshed-mid"), ValueError("demo failure, not NotSignedIn")],
        )
        patcher.start(); self.addCleanup(patcher.stop)

        ok, failed, saved = pull.pull_hosted(self.conn, n_days=2)
        self.assertEqual((ok, failed, saved), (0, 1, 1))
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "ValueError")
        self.assertEqual(garmin_cuenta.leer(self.conn, 1), "tok-refreshed-mid")

    def test_success_clears_a_previous_error(self):
        garmin_cuenta.guardar(self.conn, 1, GOOD_TOKEN)
        garmin_cuenta.registrar_error(self.conn, 1, "NotSignedIn")
        ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-01")
        self.assertEqual((ok, failed, saved), (1, 0, 1))
        self.assertIsNone(garmin_cuenta.ultimo_error(self.conn, 1))

    def test_leer_returning_none_is_not_counted_as_ok(self):
        garmin_cuenta.guardar(self.conn, 1, GOOD_TOKEN)
        patcher = mock.patch("body.pull.garmin_cuenta.leer", return_value=None)
        patcher.start(); self.addCleanup(patcher.stop)
        ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-01")
        self.assertEqual((ok, failed, saved), (0, 0, 0))


class PullHostedPerPersonZoneTest(unittest.TestCase):
    """BODY-PULL-RELOJ-1: with no explicit `fecha`, each person's day list comes from their own
    local today (`fechas.hoy_local`), not the server's -- the hosted timer fires at one fixed
    server instant, so a person in a zone far from the server's would otherwise be asked for the
    wrong "today"."""

    def setUp(self):
        self.conn, orig = _fresh_db(self, "pull_zones.db")
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        patcher_key = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher_key.start(); self.addCleanup(patcher_key.stop)

    def test_each_person_gets_their_own_today_and_yesterday(self):
        self.conn.execute("UPDATE person SET timezone = 'America/Bogota' WHERE id = 1")
        self.conn.execute("UPDATE person SET timezone = 'Pacific/Auckland' WHERE id = 2")
        self.conn.commit()
        garmin_cuenta.guardar(self.conn, 1, "tok-bogota")
        garmin_cuenta.guardar(self.conn, 2, "tok-auckland")
        # One real instant: Nov 8 18:00 in Bogota (UTC-5) but already Nov 9 noon in Auckland
        # (UTC+13 in NZ daylight time) -- the server date and person 2's own date differ.
        instant = datetime(2026, 11, 8, 23, 0, tzinfo=timezone.utc)
        calls_by_token: dict[str, list[str]] = {"tok-bogota": [], "tok-auckland": []}

        def fetch(day, token_str=None):
            calls_by_token[token_str].append(day)
            return garmin_adapter.DayData(day=day, metrics={"resting_hr": 55.0}), token_str

        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=fetch), \
             mock.patch("body.pull.time.sleep"), \
             mock.patch("body.fechas.datetime", **{
                 "now.side_effect": lambda tz=None: instant.astimezone(tz) if tz else instant}):
            ok, failed, saved = pull.pull_hosted(self.conn, n_days=2)
        self.assertEqual((ok, failed, saved), (2, 0, 4))
        self.assertEqual(calls_by_token["tok-bogota"], ["2026-11-07", "2026-11-08"])
        self.assertEqual(calls_by_token["tok-auckland"], ["2026-11-08", "2026-11-09"])

    def test_backfill_without_hoy_uses_each_persons_own_local_date(self):
        self.conn.execute("UPDATE person SET timezone = 'America/Bogota' WHERE id = 1")
        self.conn.execute("UPDATE person SET timezone = 'Pacific/Auckland' WHERE id = 2")
        self.conn.commit()
        garmin_cuenta.guardar(self.conn, 1, "tok-bogota")
        garmin_cuenta.guardar(self.conn, 2, "tok-auckland")
        instant = datetime(2026, 11, 8, 23, 0, tzinfo=timezone.utc)
        calls_by_token: dict[str, list[str]] = {"tok-bogota": [], "tok-auckland": []}

        def fetch(day, token_str=None):
            calls_by_token[token_str].append(day)
            return garmin_adapter.DayData(day=day, metrics={"resting_hr": 55.0}), token_str

        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=fetch), \
             mock.patch("body.pull.time.sleep"), \
             mock.patch("body.fechas.datetime", **{
                 "now.side_effect": lambda tz=None: instant.astimezone(tz) if tz else instant}):
            ok, failed, saved = pull.pull_backfill_hosted(self.conn)
        self.assertEqual((ok, failed, saved), (2, 0, 30))
        # Bogota's own today is Nov 8: newest requested day is Nov 7 (yesterday).
        self.assertEqual(calls_by_token["tok-bogota"][0], "2026-11-07")
        # Auckland's own today is Nov 9: newest requested day is Nov 8 (yesterday).
        self.assertEqual(calls_by_token["tok-auckland"][0], "2026-11-08")


# ---- local mode: routes 404, the pull-day path is unchanged ---------------------------------------

class LocalModeTest(unittest.TestCase):
    def setUp(self):
        self.conn, orig = _fresh_db(self, "local.db")
        self.conn.close()
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.client = TestClient(web.app, follow_redirects=False)

    def test_garmin_routes_404_in_local_mode(self):
        self.assertEqual(self.client.get("/garmin").status_code, 404)
        self.assertEqual(self.client.post("/garmin/conectar").status_code, 404)
        self.assertEqual(self.client.post("/garmin/codigo").status_code, 404)
        self.assertEqual(self.client.post("/garmin/desconectar").status_code, 404)

    def test_fetch_day_without_a_local_token_still_raises_not_signed_in(self):
        with self.assertRaises(garmin_adapter.NotSignedIn) as ctx:
            garmin_adapter.fetch_day("2026-11-01")
        self.assertIn("python -m body.login", str(ctx.exception))

    def test_fetch_day_with_a_local_token_still_returns_bare_daydata(self):
        patcher = mock.patch("garminconnect.Garmin", FakeGarmin)
        patcher.start(); self.addCleanup(patcher.stop)
        config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_file = config.TOKEN_DIR / "garmin_tokens.json"
        token_file.write_text('{"demo": true}', encoding="utf-8")
        self.addCleanup(token_file.unlink, missing_ok=True)  # TOKEN_DIR is shared across this test run

        data = garmin_adapter.fetch_day("2026-11-01")
        self.assertIsInstance(data, garmin_adapter.DayData)


if __name__ == "__main__":
    unittest.main()
