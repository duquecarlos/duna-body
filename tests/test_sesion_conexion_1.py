"""BODY-SESION-CONEXION-1: body/rutas/sesion.py's four routes each open their own connection with
a bare db.connect() (no person_id for comun._ctx to scope by before sign-in) -- this asserts each
one closes it, on the happy path and on every exceptional path, and that no response changed.
Invented demo data only, no real health figures.
"""
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import cuenta, db, web
from tests.test_cuenta_rp import CuentaRpTestCase, ISSUER


class _CloseRecorder:
    """Wraps a real sqlite3.Connection, recording whether close() was ever called on it."""

    def __init__(self, real):
        self._real = real
        self.closed = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        self.closed = True
        self._real.close()


def _recording_connect():
    real_connect = db.connect
    recorders: list[_CloseRecorder] = []

    def _connect(*a, **kw):
        rec = _CloseRecorder(real_connect(*a, **kw))
        recorders.append(rec)
        return rec

    return _connect, recorders


class LoginStartAndCallbackCloseTest(CuentaRpTestCase):
    def test_successful_callback_closes_every_connection_and_response_is_unchanged(self):
        connect, recorders = _recording_connect()
        with mock.patch.object(db, "connect", side_effect=connect):
            r = self._sign_in(return_to="/goals")
        self.assertTrue(recorders)
        self.assertTrue(all(rec.closed for rec in recorders))
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/goals")
        self.assertIsNotNone(r.cookies.get(cuenta.SESSION_COOKIE))

    def test_flow_refused_closes_the_connection_and_response_is_unchanged(self):
        self._start_login()
        self.client.cookies.clear()
        connect, recorders = _recording_connect()
        with mock.patch.object(db, "connect", side_effect=connect):
            r = self.client.get("/login/cuenta/callback", params={"code": "c", "state": "whatever"})
        self.assertTrue(recorders)
        self.assertTrue(all(rec.closed for rec in recorders))
        self.assertEqual(r.status_code, 400)

    def test_cuenta_unavailable_closes_the_connection_and_response_is_unchanged(self):
        auth = self._start_login()
        self.issuer.token_error = True
        connect, recorders = _recording_connect()
        with mock.patch.object(db, "connect", side_effect=connect):
            r = self.client.get("/login/cuenta/callback", params={"code": "c", "state": auth["state"]})
        self.assertTrue(recorders)
        self.assertTrue(all(rec.closed for rec in recorders))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Cuenta Duna", r.text)

    def test_unexpected_exception_from_handle_callback_still_closes_the_connection(self):
        auth = self._start_login()
        client = TestClient(web.app, follow_redirects=False, raise_server_exceptions=False)
        client.cookies.update(self.client.cookies)
        connect, recorders = _recording_connect()
        with mock.patch.object(db, "connect", side_effect=connect):
            with mock.patch.object(cuenta, "handle_callback", side_effect=RuntimeError("boom")):
                r = client.get("/login/cuenta/callback", params={"code": "c", "state": auth["state"]})
        self.assertEqual(r.status_code, 500)
        self.assertTrue(recorders)
        self.assertTrue(all(rec.closed for rec in recorders))


class LogoutClosesConnectionTest(CuentaRpTestCase):
    def test_logout_closes_the_connection_and_response_is_unchanged(self):
        self._sign_in(sid="the-stored-sid")
        connect, recorders = _recording_connect()
        with mock.patch.object(db, "connect", side_effect=connect):
            r = self.client.post("/logout")
        self.assertTrue(recorders)
        self.assertTrue(all(rec.closed for rec in recorders))
        self.assertEqual(r.status_code, 303)
        location = r.headers["location"]
        self.assertTrue(location.startswith(f"{ISSUER}/oauth/logout?"))
        qp = parse_qs(urlparse(location).query)
        self.assertEqual(qp["logout_hint"][0], "the-stored-sid")
        self.assertNotIn("id_token_hint", qp)


class BackchannelLogoutClosesConnectionTest(CuentaRpTestCase):
    def _loopback_client(self) -> TestClient:
        return TestClient(web.app, follow_redirects=False, client=("127.0.0.1", 12345))

    def test_backchannel_logout_closes_the_connection_and_response_is_unchanged(self):
        self._sign_in(sid="sid-conexion-1")
        token = self.issuer.logout_token(sub=None, sid="sid-conexion-1", jti="jti-conexion-1")
        lb = self._loopback_client()
        connect, recorders = _recording_connect()
        with mock.patch.object(db, "connect", side_effect=connect):
            r = lb.post("/login/cuenta/logout-token", data={"logout_token": token})
        self.assertTrue(recorders)
        self.assertTrue(all(rec.closed for rec in recorders))
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
