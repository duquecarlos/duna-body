"""BODY-MCP-HTTP-1: body/llave_asistente.py's own introspection against Cuenta Duna -- a stubbed
transport (`llave_asistente._post_introspect`), never a real network call. Invented demo data
only, no real health figures.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

import httpx

from body import config, cuenta, db, llave_asistente

SUB = "sub-zz-demo-1"
ISSUER = "https://fake-cuenta.test"
CLIENT_SECRET = "s3cr3t-test-only"


def _activa(*, producto: str = "body", persona_id: str = SUB, permiso: str = "lectura",
            llave_id: str = "llave-1") -> httpx.Response:
    return httpx.Response(200, json={
        "active": True, "producto": producto, "persona_id": persona_id, "permiso": permiso,
        "llave_id": llave_id,
    })


class _LlaveAsistenteTestCase(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "llave.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute("UPDATE person SET cuenta_sub = ? WHERE id = 1", (SUB,))
        self.conn.commit()

        env = {cuenta.ISSUER_ENV: ISSUER, cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: CLIENT_SECRET}
        patcher_env = mock.patch.dict(os.environ, env)
        patcher_env.start()
        self.addCleanup(patcher_env.stop)

        llave_asistente.reset_cache()
        self.addCleanup(llave_asistente.reset_cache)

    def _patch_introspect(self, fn):
        patcher = mock.patch.object(llave_asistente, "_post_introspect", fn)
        patcher.start()
        self.addCleanup(patcher.stop)


class RequestShapeTest(_LlaveAsistenteTestCase):
    def test_request_carries_form_token_and_basic_auth_only(self):
        captured = {}

        def fake_post(self_client, url, data=None, auth=None, **kw):
            captured["url"] = url
            captured["data"] = data
            captured["auth"] = auth
            captured["kw"] = kw
            return httpx.Response(200, json={"active": False})

        patcher = mock.patch.object(httpx.Client, "post", fake_post)
        patcher.start()
        self.addCleanup(patcher.stop)

        with self.assertRaises(llave_asistente.LlaveInvalida):
            llave_asistente.verificar(self.conn, "the-assistant-key")

        self.assertEqual(captured["url"], f"{ISSUER}/oauth/introspect")
        self.assertEqual(captured["data"], {"token": "the-assistant-key"})
        self.assertEqual(captured["auth"], (cuenta.CLIENT_ID, CLIENT_SECRET))
        self.assertEqual(captured["kw"], {})


class OutcomeTest(_LlaveAsistenteTestCase):
    def test_active_resolves_to_the_body_person(self):
        self._patch_introspect(lambda key: _activa(permiso="escritura", llave_id="llave-xyz"))
        autorizacion = llave_asistente.verificar(self.conn, "k")
        self.assertEqual(autorizacion.person_id, 1)
        self.assertEqual(autorizacion.permiso, "escritura")
        self.assertEqual(autorizacion.llave_id, "llave-xyz")

    def test_inactive_is_llave_invalida(self):
        self._patch_introspect(lambda key: httpx.Response(200, json={"active": False}))
        with self.assertRaises(llave_asistente.LlaveInvalida):
            llave_asistente.verificar(self.conn, "k")

    def test_wrong_producto_is_llave_invalida(self):
        self._patch_introspect(lambda key: _activa(producto="finanzas"))
        with self.assertRaises(llave_asistente.LlaveInvalida):
            llave_asistente.verificar(self.conn, "k")

    def test_unknown_permiso_is_llave_invalida(self):
        self._patch_introspect(lambda key: _activa(permiso="admin"))
        with self.assertRaises(llave_asistente.LlaveInvalida):
            llave_asistente.verificar(self.conn, "k")

    def test_persona_id_not_a_string_is_llave_invalida(self):
        self._patch_introspect(lambda key: httpx.Response(200, json={
            "active": True, "producto": "body", "persona_id": 123, "permiso": "lectura"}))
        with self.assertRaises(llave_asistente.LlaveInvalida):
            llave_asistente.verificar(self.conn, "k")

    def test_unparseable_json_is_llave_invalida(self):
        self._patch_introspect(lambda key: httpx.Response(200, content=b"not json"))
        with self.assertRaises(llave_asistente.LlaveInvalida):
            llave_asistente.verificar(self.conn, "k")

    def test_no_body_person_for_that_persona_is_persona_sin_body(self):
        self._patch_introspect(lambda key: _activa(persona_id="sub-nobody-signed-in-yet"))
        with self.assertRaises(llave_asistente.PersonaSinBody):
            llave_asistente.verificar(self.conn, "k")


class CuentaFaultTest(_LlaveAsistenteTestCase):
    def test_timeout_is_cuenta_no_disponible(self):
        def fake(key):
            raise httpx.ConnectTimeout("boom")
        self._patch_introspect(fake)
        with self.assertRaises(llave_asistente.CuentaNoDisponible):
            llave_asistente.verificar(self.conn, "k")

    def test_500_is_cuenta_no_disponible(self):
        self._patch_introspect(lambda key: httpx.Response(500))
        with self.assertRaises(llave_asistente.CuentaNoDisponible):
            llave_asistente.verificar(self.conn, "k")

    def test_401_on_the_introspection_call_itself_is_cuenta_no_disponible_not_unauthorized(self):
        self._patch_introspect(lambda key: httpx.Response(401))
        with self.assertRaises(llave_asistente.CuentaNoDisponible):
            llave_asistente.verificar(self.conn, "k")

    def test_429_passes_retry_after_through(self):
        self._patch_introspect(lambda key: httpx.Response(429, headers={"retry-after": "30"}))
        with self.assertRaises(llave_asistente.CuentaNoDisponible) as ctx:
            llave_asistente.verificar(self.conn, "k")
        self.assertEqual(ctx.exception.retry_after, "30")

    def test_cuenta_faults_are_never_cached(self):
        calls = []

        def fake(key):
            calls.append(key)
            raise httpx.ConnectTimeout("boom")

        self._patch_introspect(fake)
        for _ in range(3):
            with self.assertRaises(llave_asistente.CuentaNoDisponible):
                llave_asistente.verificar(self.conn, "k")
        self.assertEqual(len(calls), 3)

    def test_inactive_answers_are_never_cached(self):
        calls = []

        def fake(key):
            calls.append(key)
            return httpx.Response(200, json={"active": False})

        self._patch_introspect(fake)
        for _ in range(3):
            with self.assertRaises(llave_asistente.LlaveInvalida):
                llave_asistente.verificar(self.conn, "k")
        self.assertEqual(len(calls), 3)


class CacheTest(_LlaveAsistenteTestCase):
    def test_active_answer_is_cached_60s_then_re_asked_and_a_revoked_key_is_then_refused(self):
        calls = []

        def fake(key):
            calls.append(key)
            activo = len(calls) == 1
            return httpx.Response(200, json={
                "active": activo, "producto": "body", "persona_id": SUB, "permiso": "lectura",
                "llave_id": "llave-1"})

        self._patch_introspect(fake)
        clock = {"t": 0.0}
        reloj = lambda: clock["t"]

        autorizacion = llave_asistente.verificar(self.conn, "k", reloj=reloj)
        self.assertEqual(autorizacion.person_id, 1)
        self.assertEqual(len(calls), 1)

        clock["t"] = 59.0
        llave_asistente.verificar(self.conn, "k", reloj=reloj)
        self.assertEqual(len(calls), 1, "a call inside the 60s window must not re-ask Cuenta")

        clock["t"] = 61.0
        with self.assertRaises(llave_asistente.LlaveInvalida):
            llave_asistente.verificar(self.conn, "k", reloj=reloj)
        self.assertEqual(len(calls), 2, "past 60s the revoked key must be re-asked and refused")

    def test_a_freshly_granted_key_works_on_its_first_call(self):
        self._patch_introspect(lambda key: _activa(persona_id=SUB))
        autorizacion = llave_asistente.verificar(self.conn, "brand-new-key")
        self.assertEqual(autorizacion.person_id, 1)


if __name__ == "__main__":
    unittest.main()
