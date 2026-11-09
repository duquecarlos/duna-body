"""BODY-CUENTA-RP-1: the OIDC relying-party flow -- authorization code + PKCE, callback, sessions,
sign-out, back-channel logout, «Sin acceso», `vincular_cuenta`, local-mode parity, `/healthz`.

A local fake issuer only: an RSA key generated in this test, its own JWKS, signed ID and logout
tokens; `body/cuenta.py`'s only two network functions (`_get_json`, `_post_form`) are patched so
nothing ever reaches a real network. Invented demo data only, no real health figures.
"""
import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, cuenta, db, privacidad, vincular_cuenta, web

_DEFAULT_AUTH_TIME = object()  # sentinel: "not given" (use now) vs. explicit None (omit the claim)

ISSUER = "https://fake-cuenta.test"
PUBLIC_URL = "http://127.0.0.1:8770"
CLIENT_SECRET = "s3cr3t-test-only"
KID = "k1"
SUB = "6f1c9a52-3b7e-4d1a-9c55-2a8e0f4b7d31"


def _b64url_uint(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") or b"\x00"
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeIssuer:
    """Serves discovery, JWKS and the token endpoint through body/cuenta.py's two network seams."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = self.key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID,
                    "n": _b64url_uint(pub.n), "e": _b64url_uint(pub.e)}
        self.next_id_token: str | None = None
        self.token_error = False

    def sign(self, claims: dict, typ: str = "JWT") -> str:
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": KID, "typ": typ})

    def id_token(self, *, nonce: str, sub: str = SUB, duna_productos=("body",), sid: str = "sid-demo-1",
                 acr: str = "pwd", auth_time=_DEFAULT_AUTH_TIME, extra: dict | None = None) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "sub": sub, "aud": cuenta.CLIENT_ID, "exp": now + 300, "iat": now,
                  "nonce": nonce, "sid": sid, "acr": acr, "duna_productos": list(duna_productos),
                  "name": "Persona de Prueba"}
        resolved_auth_time = now if auth_time is _DEFAULT_AUTH_TIME else auth_time
        if resolved_auth_time is not None:
            claims["auth_time"] = resolved_auth_time
        if extra:
            claims.update(extra)
        return self.sign(claims)

    def logout_token(self, *, sub: str | None = SUB, sid: str | None = "sid-demo-1", jti: str = "jti-1",
                      extra: dict | None = None) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": cuenta.CLIENT_ID, "iat": now, "exp": now + 120, "jti": jti,
                  "events": {cuenta._EVENTO_LOGOUT: {}}}
        if sub is not None:
            claims["sub"] = sub
        if sid is not None:
            claims["sid"] = sid
        if extra:
            claims.update(extra)
        return self.sign(claims, typ="logout+jwt")

    def _get_json(self, url: str) -> dict:
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return {"jwks_uri": f"{ISSUER}/jwks"}
        if url == f"{ISSUER}/jwks":
            return {"keys": [self.jwk]}
        raise AssertionError(f"unexpected GET {url}")

    def _post_form(self, url: str, data: dict, auth: tuple[str, str]) -> httpx.Response:
        assert url == f"{ISSUER}/oauth/token"
        assert auth == (cuenta.CLIENT_ID, CLIENT_SECRET)
        if self.token_error:
            return httpx.Response(400, json={"error": "server_error"})
        return httpx.Response(200, json={"id_token": self.next_id_token})


class CuentaRpTestCase(unittest.TestCase):
    """Common hosted-mode setup: a fresh temp db, a fake issuer, env vars, a fresh client."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "rp.db"
        conn = db.connect()
        db.init(conn)
        conn.close()

        self.issuer = FakeIssuer()
        patcher_get = mock.patch.object(cuenta, "_get_json", self.issuer._get_json)
        patcher_post = mock.patch.object(cuenta, "_post_form", self.issuer._post_form)
        patcher_get.start(); self.addCleanup(patcher_get.stop)
        patcher_post.start(); self.addCleanup(patcher_post.stop)
        cuenta._reset_caches_for_tests()
        self.addCleanup(cuenta._reset_caches_for_tests)

        env = {cuenta.ISSUER_ENV: ISSUER, cuenta.PUBLIC_URL_ENV: PUBLIC_URL,
               cuenta.CLIENT_SECRET_ENV: CLIENT_SECRET}
        patcher_env = mock.patch.dict(os.environ, env)
        patcher_env.start(); self.addCleanup(patcher_env.stop)

        self.client = TestClient(web.app, follow_redirects=False)

    def _authorize_params(self, resp) -> dict:
        self.assertEqual(resp.status_code, 303)
        qp = parse_qs(urlparse(resp.headers["location"]).query)
        return {k: v[0] for k, v in qp.items()}

    def _start_login(self, return_to: str | None = None) -> dict:
        params = {"return_to": return_to} if return_to else {}
        r = self.client.get("/login", params=params)
        return self._authorize_params(r)

    def _db_count(self, sql: str, *args) -> int:
        conn = db.connect()
        try:
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()

    def _sign_in(self, *, duna_productos=("body",), return_to: str | None = None, sub: str = SUB,
                sid: str = "sid-demo-1", auth_time=_DEFAULT_AUTH_TIME, extra: dict | None = None):
        auth = self._start_login(return_to)
        self.issuer.next_id_token = self.issuer.id_token(nonce=auth["nonce"], sub=sub,
                                                          duna_productos=duna_productos, sid=sid,
                                                          auth_time=auth_time, extra=extra)
        return self.client.get(
            "/login/cuenta/callback", params={"code": "auth-code-1", "state": auth["state"]}
        )


class LocalModeTest(unittest.TestCase):
    """Unset BODY_CUENTA_ISSUER: unchanged from before this slice."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        self.client = TestClient(web.app, follow_redirects=False)
        self.assertNotIn(cuenta.ISSUER_ENV, os.environ)

    def test_sign_in_routes_404_in_local_mode(self):
        for path in ("/login", "/login/cuenta/start", "/login/cuenta/callback"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/logout").status_code, 404)
        self.assertEqual(self.client.post("/login/cuenta/logout-token").status_code, 404)

    def test_pages_render_as_today(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("nav.logout", r.text)

    def test_healthz_ok(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, "ok")


class StartupConfigTest(unittest.TestCase):
    def test_hosted_without_secret_refuses_to_start_and_names_the_variable_not_a_value(self):
        env = {cuenta.ISSUER_ENV: ISSUER, cuenta.PUBLIC_URL_ENV: PUBLIC_URL}
        with mock.patch.dict(os.environ, env):
            os.environ.pop(cuenta.CLIENT_SECRET_ENV, None)
            with self.assertRaises(RuntimeError) as cm:
                with TestClient(web.app):
                    pass
        message = str(cm.exception)
        self.assertIn(cuenta.CLIENT_SECRET_ENV, message)
        self.assertNotIn(PUBLIC_URL, message)
        self.assertNotIn(ISSUER, message)


class FullSignInTest(CuentaRpTestCase):
    def test_new_sub_creates_a_person_and_sets_the_session_cookie(self):
        before = self._db_count("SELECT COUNT(*) FROM person")
        r = self._sign_in(return_to="/goals")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/goals")
        cookie = r.cookies.get(cuenta.SESSION_COOKIE)
        self.assertIsNotNone(cookie)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM person"), before + 1)
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("samesite=lax", set_cookie.lower())
        self.assertNotIn("Domain", set_cookie)

    def test_already_linked_sub_returns_that_same_person(self):
        self._sign_in()
        self.client.cookies.clear()
        before = self._db_count("SELECT COUNT(*) FROM person")
        r = self._sign_in()
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM person"), before)

    def test_signed_in_person_sees_their_own_today_page(self):
        r = self._sign_in(return_to="/")
        self.assertEqual(r.status_code, 303)
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id)
        conn.close()
        r2 = self.client.get("/")
        self.assertEqual(r2.status_code, 200)

    def test_signed_in_person_without_consent_is_redirected_to_consentimiento(self):
        r = self._sign_in(return_to="/")
        self.assertEqual(r.status_code, 303)
        r2 = self.client.get("/")
        self.assertEqual(r2.status_code, 303)
        location = r2.headers["location"]
        self.assertTrue(location.startswith("/privacidad/consentimiento?"))
        self.assertIn("return_to=%2F", location)

    def test_body_missing_from_duna_productos_shows_sin_acceso(self):
        before = self._db_count("SELECT COUNT(*) FROM person")
        r = self._sign_in(duna_productos=("lotes",))
        self.assertEqual(r.status_code, 200)
        self.assertIn(cuenta.SWITCHER_URL, r.text)
        self.assertIsNone(r.cookies.get(cuenta.SESSION_COOKIE))
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM person"), before)


class CallbackRefusalTest(CuentaRpTestCase):
    def test_state_mismatch_refused(self):
        auth = self._start_login()
        self.issuer.next_id_token = self.issuer.id_token(nonce=auth["nonce"])
        r = self.client.get("/login/cuenta/callback", params={"code": "c", "state": "wrong-state"})
        self.assertEqual(r.status_code, 400)

    def test_missing_flujo_cookie_refused(self):
        self._start_login()
        self.client.cookies.clear()
        r = self.client.get("/login/cuenta/callback", params={"code": "c", "state": "whatever"})
        self.assertEqual(r.status_code, 400)

    def test_foreign_flujo_cookie_refused(self):
        self._start_login()
        self.client.cookies.set(cuenta.FLUJO_COOKIE, "not-a-real-flujo-id")
        r = self.client.get("/login/cuenta/callback", params={"code": "c", "state": "whatever"})
        self.assertEqual(r.status_code, 400)

    def test_reused_attempt_refused(self):
        auth = self._start_login()
        self.issuer.next_id_token = self.issuer.id_token(nonce=auth["nonce"])
        r1 = self.client.get("/login/cuenta/callback", params={"code": "c", "state": auth["state"]})
        self.assertEqual(r1.status_code, 303)
        r2 = self.client.get("/login/cuenta/callback", params={"code": "c", "state": auth["state"]})
        self.assertEqual(r2.status_code, 400)

    def test_wrong_nonce_refused(self):
        auth = self._start_login()
        self.issuer.next_id_token = self.issuer.id_token(nonce="not-the-real-nonce")
        r = self.client.get("/login/cuenta/callback", params={"code": "c", "state": auth["state"]})
        self.assertEqual(r.status_code, 400)

    def test_token_endpoint_error_shows_cuenta_down_sentence(self):
        auth = self._start_login()
        self.issuer.token_error = True
        r = self.client.get("/login/cuenta/callback", params={"code": "c", "state": auth["state"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Cuenta Duna", r.text)

    def test_login_required_starts_a_visible_sign_in(self):
        auth = self._start_login(return_to="/goals")
        r = self.client.get(
            "/login/cuenta/callback", params={"error": "login_required", "state": auth["state"]}
        )
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/login?"))

    def test_access_denied_shows_sin_acceso(self):
        before = self._db_count("SELECT COUNT(*) FROM person")
        auth = self._start_login()
        r = self.client.get(
            "/login/cuenta/callback", params={"error": "access_denied", "state": auth["state"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(cuenta.SWITCHER_URL, r.text)
        self.assertIsNone(r.cookies.get(cuenta.SESSION_COOKIE))
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM person"), before)

    def test_outage_codes_show_cuenta_down_sentence(self):
        for codigo in ("invalid_request", "unauthorized_client", "server_error", "temporarily_unavailable"):
            auth = self._start_login()
            r = self.client.get(
                "/login/cuenta/callback", params={"error": codigo, "state": auth["state"]}
            )
            self.assertEqual(r.status_code, 200, codigo)
            self.assertIn("Cuenta Duna", r.text, codigo)
            self.assertNotIn(codigo, r.text)

    def test_unknown_error_code_keeps_acceso_rechazado(self):
        auth = self._start_login()
        r = self.client.get(
            "/login/cuenta/callback", params={"error": "some_unknown_code", "state": auth["state"]}
        )
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("some_unknown_code", r.text)

    def test_forged_access_denied_with_bad_state_is_still_refused_as_state_mismatch(self):
        self._start_login()
        r = self.client.get(
            "/login/cuenta/callback", params={"error": "access_denied", "state": "wrong-state"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(cuenta.SWITCHER_URL, r.text)


class LogoutTest(CuentaRpTestCase):
    def test_logout_ends_session_and_redirects_with_sid_and_no_id_token_hint(self):
        self._sign_in(sid="the-stored-sid")
        r = self.client.post("/logout")
        self.assertEqual(r.status_code, 303)
        location = r.headers["location"]
        self.assertTrue(location.startswith(f"{ISSUER}/oauth/logout?"))
        qp = parse_qs(urlparse(location).query)
        self.assertEqual(qp["logout_hint"][0], "the-stored-sid")
        self.assertNotIn("id_token_hint", qp)
        self.assertEqual(qp["client_id"][0], cuenta.CLIENT_ID)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion"), 0)

    def test_expired_session_redirects_to_prompt_none(self):
        self._sign_in()
        conn = db.connect()
        past = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE sesion SET expires_at = ?", (past,))
        conn.commit()
        conn.close()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/login?"))
        self.assertIn("prompt=none", r.headers["location"])

    def test_no_cookie_at_all_redirects_without_prompt(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("prompt=", r.headers["location"])

    def test_return_to_double_slash_is_refused(self):
        r = self.client.get("/", headers={"Cookie": f"{cuenta.SESSION_COOKIE}=bogus"})
        self.assertEqual(r.status_code, 303)
        # //evil would mean an external redirect; current path "/" is always safe already, so
        # exercise the guard directly against cuenta.safe_return_to.
        self.assertEqual(cuenta.safe_return_to("//evil.example.com"), "/")
        self.assertEqual(cuenta.safe_return_to("/goals"), "/goals")

    def test_post_without_session_is_401(self):
        r = self.client.post("/goals/1/pause")
        self.assertEqual(r.status_code, 401)


class BackchannelLogoutTest(CuentaRpTestCase):
    def _loopback_client(self) -> TestClient:
        return TestClient(web.app, follow_redirects=False, client=("127.0.0.1", 12345))

    def test_non_loopback_caller_refused(self):
        r = self.client.post("/login/cuenta/logout-token", data={"logout_token": "whatever"})
        self.assertEqual(r.status_code, 403)

    def test_logout_by_sid_ends_matching_session_only(self):
        self._sign_in(sid="sid-a")
        self.client.cookies.clear()
        self._sign_in(sub="other-sub-demo", sid="sid-b")
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion"), 2)
        token = self.issuer.logout_token(sub=None, sid="sid-a", jti="jti-by-sid")
        lb = self._loopback_client()
        r = lb.post("/login/cuenta/logout-token", data={"logout_token": token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion WHERE cuenta_sid = ?", "sid-a"), 0)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion WHERE cuenta_sid = ?", "sid-b"), 1)

    def test_logout_by_sub_ends_every_session_of_that_person(self):
        self._sign_in(sub=SUB, sid="sid-x")
        token = self.issuer.logout_token(sub=SUB, sid=None, jti="jti-by-sub")
        lb = self._loopback_client()
        r = lb.post("/login/cuenta/logout-token", data={"logout_token": token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion"), 0)

    def test_replayed_jti_is_400(self):
        self._sign_in(sid="sid-y")
        token = self.issuer.logout_token(sub=None, sid="sid-y", jti="jti-replay")
        lb = self._loopback_client()
        self.assertEqual(lb.post("/login/cuenta/logout-token", data={"logout_token": token}).status_code, 200)
        self.assertEqual(lb.post("/login/cuenta/logout-token", data={"logout_token": token}).status_code, 400)

    def test_wrong_typ_is_400(self):
        bad = self.issuer.id_token(nonce="n")  # typ=JWT, not logout+jwt
        lb = self._loopback_client()
        self.assertEqual(lb.post("/login/cuenta/logout-token", data={"logout_token": bad}).status_code, 400)

    def test_missing_events_is_400(self):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": cuenta.CLIENT_ID, "iat": now, "exp": now + 120, "jti": "jti-no-events",
                  "sid": "sid-z"}
        token = self.issuer.sign(claims, typ="logout+jwt")
        lb = self._loopback_client()
        self.assertEqual(lb.post("/login/cuenta/logout-token", data={"logout_token": token}).status_code, 400)

    def test_nonce_present_is_400(self):
        token = self.issuer.logout_token(extra={"nonce": "x"})
        lb = self._loopback_client()
        self.assertEqual(lb.post("/login/cuenta/logout-token", data={"logout_token": token}).status_code, 400)

    def test_missing_sub_and_sid_is_400(self):
        token = self.issuer.logout_token(sub=None, sid=None)
        lb = self._loopback_client()
        self.assertEqual(lb.post("/login/cuenta/logout-token", data={"logout_token": token}).status_code, 400)

    def test_loopback_with_x_forwarded_for_refused_and_no_session_ended(self):
        self._sign_in(sid="sid-proxy")
        token = self.issuer.logout_token(sub=None, sid="sid-proxy", jti="jti-proxy")
        lb = self._loopback_client()
        r = lb.post("/login/cuenta/logout-token", data={"logout_token": token},
                    headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion WHERE cuenta_sid = ?", "sid-proxy"), 1)

    def test_loopback_without_proxy_headers_and_valid_token_still_200(self):
        self._sign_in(sid="sid-clean")
        token = self.issuer.logout_token(sub=None, sid="sid-clean", jti="jti-clean")
        lb = self._loopback_client()
        r = lb.post("/login/cuenta/logout-token", data={"logout_token": token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion WHERE cuenta_sid = ?", "sid-clean"), 0)


class VincularCuentaTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "vincular.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Other')")
        self.conn.commit()

    def _csv(self, rows: list[tuple[str, str]]) -> str:
        d = tempfile.mkdtemp(dir=TMP)
        path = Path(d) / "vinculos-body.csv"
        path.write_text("id_local,sub\n" + "\n".join(f"{a},{b}" for a, b in rows), encoding="utf-8")
        return str(path)

    def test_dry_run_changes_nothing(self):
        path = self._csv([("2", "sub-demo-aaa")])
        rows = vincular_cuenta.read_rows(path)
        result = vincular_cuenta.plan(self.conn, rows)
        self.assertEqual(len(result["to_link"]), 1)
        self.assertIsNone(
            self.conn.execute("SELECT cuenta_sub FROM person WHERE id = 2").fetchone()["cuenta_sub"]
        )

    def test_apply_links(self):
        path = self._csv([("2", "sub-demo-aaa")])
        rows = vincular_cuenta.read_rows(path)
        result = vincular_cuenta.plan(self.conn, rows)
        vincular_cuenta.apply(self.conn, result["to_link"])
        self.assertEqual(
            self.conn.execute("SELECT cuenta_sub FROM person WHERE id = 2").fetchone()["cuenta_sub"],
            "sub-demo-aaa",
        )

    def test_reapply_is_a_noop(self):
        path = self._csv([("2", "sub-demo-aaa")])
        rows = vincular_cuenta.read_rows(path)
        vincular_cuenta.apply(self.conn, vincular_cuenta.plan(self.conn, rows)["to_link"])
        result2 = vincular_cuenta.plan(self.conn, rows)
        self.assertEqual(len(result2["to_link"]), 0)
        self.assertEqual(len(result2["already_linked"]), 1)

    def test_changed_sub_refused(self):
        path = self._csv([("2", "sub-demo-aaa")])
        rows = vincular_cuenta.read_rows(path)
        vincular_cuenta.apply(self.conn, vincular_cuenta.plan(self.conn, rows)["to_link"])
        path2 = self._csv([("2", "sub-demo-bbb")])
        result2 = vincular_cuenta.plan(self.conn, vincular_cuenta.read_rows(path2))
        self.assertEqual(len(result2["refused"]), 1)
        self.assertEqual(
            self.conn.execute("SELECT cuenta_sub FROM person WHERE id = 2").fetchone()["cuenta_sub"],
            "sub-demo-aaa",
        )


class InicioSesionRebuildTest(unittest.TestCase):
    """BODY-CUENTA-SCHEMA-1's shape (`state_hash`) migrates to this slice's (`flujo_hash`, with
    `state` carried) without ever needing a DELETE, since the table has never been written."""

    def test_old_shape_rebuilds_to_the_new_one(self):
        path = Path(tempfile.mkdtemp(dir=TMP)) / "rebuild.db"
        conn = db.connect(path)
        conn.executescript(db.SCHEMA.replace(
            f"CREATE TABLE IF NOT EXISTS inicio_sesion (\n{db._INICIO_SESION_COLUMNS}\n);",
            "CREATE TABLE IF NOT EXISTS inicio_sesion (\n"
            "    state_hash    TEXT PRIMARY KEY,\n"
            "    nonce         TEXT NOT NULL,\n"
            "    code_verifier TEXT NOT NULL,\n"
            "    return_to     TEXT,\n"
            "    created_at    TEXT NOT NULL\n"
            ");",
        ))
        conn.commit()
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(inicio_sesion)")}
        self.assertIn("state_hash", cols_before)
        db.init(conn)
        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(inicio_sesion)")}
        self.assertEqual(cols_after, {"flujo_hash", "state", "nonce", "code_verifier", "return_to", "created_at"})
        conn.close()


if __name__ == "__main__":
    unittest.main()
