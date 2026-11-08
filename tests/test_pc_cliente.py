"""BODY-PC-CLIENTE-1: the PC-side client for a hosted Body -- the link file, conectar_pc,
subir_turnos, traer_semana, and the installer's hosted mode. No network: every call to
pc_cliente's transport is a fake. Invented demo data only, no real health figures or dates.
"""
from __future__ import annotations

import contextlib
import email.message
import io
import json
import os
import re
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, conectar_pc, pc_cliente, subir_turnos, traer_semana, vida_export

TEST_KEY = "bdy_test-key-0123456789"
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-daily-sync.ps1"


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


class _FakeHTTPResponse:
    """Minimal stand-in for http.client.HTTPResponse, enough for OpenerDirector's redirect path."""

    def __init__(self, status: int, headers: email.message.Message, body: bytes = b""):
        self.status = status
        self.code = status
        self.msg = ""
        self.headers = headers
        self._body = body

    def info(self):
        return self.headers

    def read(self, *a, **k):
        return self._body

    def geturl(self):
        return "https://fake.test/api/vida/semana"

    def close(self):
        pass


def _http_error(code: int, payload: dict | None = None) -> urllib.error.HTTPError:
    body = json.dumps(payload).encode() if payload is not None else b""
    return urllib.error.HTTPError("https://fake.test/x", code, "", None, io.BytesIO(body))


class _TempHomeTestCase(unittest.TestCase):
    def setUp(self):
        orig_home = config.BODY_HOME
        self.addCleanup(lambda: setattr(config, "BODY_HOME", orig_home))
        config.BODY_HOME = Path(tempfile.mkdtemp(dir=TMP))

    def _patch_abrir(self, fn):
        patcher = mock.patch.object(pc_cliente, "_abrir", fn)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---- the link file ----------------------------------------------------------------------------

class EnlaceFileTest(_TempHomeTestCase):
    def test_holds_the_key_and_is_0600_where_supported(self):
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)
        path = config.BODY_HOME / pc_cliente.ENLACE_NAME
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["clave"], TEST_KEY)
        self.assertEqual(data["url"], "https://fake.test")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_leer_enlace_missing_names_the_fix(self):
        with self.assertRaises(pc_cliente.EnlaceFaltante) as ctx:
            pc_cliente.leer_enlace()
        self.assertIn("conectar_pc", str(ctx.exception))

    def test_borrar_enlace_removes_the_file(self):
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)
        pc_cliente.borrar_enlace()
        self.assertFalse((config.BODY_HOME / pc_cliente.ENLACE_NAME).exists())

    def test_http_to_non_local_host_refused(self):
        with self.assertRaises(pc_cliente.UrlRefusada):
            pc_cliente.escribir_enlace("http://example.com", TEST_KEY)
        self.assertFalse((config.BODY_HOME / pc_cliente.ENLACE_NAME).exists())

    def test_http_to_localhost_allowed(self):
        pc_cliente.escribir_enlace("http://127.0.0.1:8770", TEST_KEY)
        self.assertTrue((config.BODY_HOME / pc_cliente.ENLACE_NAME).exists())

    def test_conectar_pc_has_no_cli_flag_for_the_key(self):
        text = Path(conectar_pc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--clave", text)
        self.assertNotIn("--key", text)


# ---- redirects are refused, never followed -----------------------------------------------------

class RedirectRefusedTest(_TempHomeTestCase):
    def setUp(self):
        super().setUp()
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)

    def test_redirect_to_another_host_is_never_followed(self):
        calls = []

        def fake_do_open(self, http_class, req, **kwargs):
            calls.append(req.full_url)
            headers = email.message.Message()
            headers["Location"] = "https://evil.example/elsewhere"
            return _FakeHTTPResponse(302, headers)

        with mock.patch.object(urllib.request.AbstractHTTPHandler, "do_open", fake_do_open):
            status, _data = pc_cliente.llamar("GET", "/api/vida/semana")

        self.assertEqual(status, 302)
        self.assertEqual(len(calls), 1, "a redirect target must never be requested")
        self.assertNotIn("evil.example", calls[0])

    def test_opener_has_the_no_redirect_handler_not_the_default(self):
        handlers = pc_cliente._opener.handle_error.get("http", {}).get(302, [])
        self.assertTrue(any(type(h) is pc_cliente._SinRedirigir for h in handlers))
        self.assertFalse(any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers))


# ---- python -m body.conectar_pc ----------------------------------------------------------------

class ConectarPcTest(_TempHomeTestCase):
    def _run(self, argv, fake_key, abrir=None):
        if abrir is not None:
            self._patch_abrir(abrir)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch("getpass.getpass", return_value=fake_key), \
                contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = conectar_pc.main(argv)
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_key_never_printed_or_sent_in_the_url(self):
        def abrir(req):
            self.assertNotIn(TEST_KEY, req.full_url)
            return _FakeResponse(200, b"{}")
        rc, out, err = self._run([], TEST_KEY, abrir)
        self.assertEqual(rc, 0)
        self.assertNotIn(TEST_KEY, out)
        self.assertNotIn(TEST_KEY, err)

    def test_200_saves_link_and_prints_conectado(self):
        rc, out, _ = self._run([], TEST_KEY, lambda req: _FakeResponse(200, b"{}"))
        self.assertEqual(rc, 0)
        self.assertIn("conectado", out.lower())
        self.assertTrue((config.BODY_HOME / pc_cliente.ENLACE_NAME).exists())

    def test_401_removes_the_link_and_says_so(self):
        def abrir(req):
            raise _http_error(401, {"error": "no_autorizado"})
        rc, out, _ = self._run([], TEST_KEY, abrir)
        self.assertEqual(rc, 1)
        self.assertFalse((config.BODY_HOME / pc_cliente.ENLACE_NAME).exists())
        self.assertNotIn(TEST_KEY, out)

    def test_403_names_the_missing_permission(self):
        def abrir(req):
            raise _http_error(403, {"error": "alcance"})
        rc, out, _ = self._run([], TEST_KEY, abrir)
        self.assertEqual(rc, 1)
        self.assertIn("vida:leer", out)

    def test_bad_key_prefix_rejected_before_any_call(self):
        called = []
        rc, out, _ = self._run([], "not-a-real-key", lambda req: called.append(req) or _FakeResponse(200, b"{}"))
        self.assertEqual(rc, 1)
        self.assertFalse(called)

    def test_http_to_non_local_host_refused_without_leaking_the_key(self):
        rc, out, _ = self._run(["--url", "http://example.com"], TEST_KEY, lambda req: _FakeResponse(200, b"{}"))
        self.assertEqual(rc, 1)
        self.assertNotIn(TEST_KEY, out)
        self.assertFalse((config.BODY_HOME / pc_cliente.ENLACE_NAME).exists())

    def test_key_whitespace_trimmed_before_check_and_save(self):
        rc, out, _ = self._run([], TEST_KEY + " \n", lambda req: _FakeResponse(200, b"{}"))
        self.assertEqual(rc, 0)
        path = config.BODY_HOME / pc_cliente.ENLACE_NAME
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["clave"], TEST_KEY)

    def test_olvidar_removes_the_link(self):
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)
        rc, out, _ = self._run(["--olvidar"], "")
        self.assertEqual(rc, 0)
        self.assertFalse((config.BODY_HOME / pc_cliente.ENLACE_NAME).exists())


# ---- python -m body.subir_turnos ---------------------------------------------------------------

class SubirTurnosTest(_TempHomeTestCase):
    def setUp(self):
        super().setUp()
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)
        self.shifts_dir = Path(tempfile.mkdtemp(dir=TMP))

    def _write(self, name: str, data: dict) -> Path:
        path = self.shifts_dir / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _run(self, abrir):
        self._patch_abrir(abrir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = subir_turnos.main(["--dir", str(self.shifts_dir)])
        return rc, buf.getvalue()

    def test_uploads_only_new_files(self):
        self._write("turnos-a.json", {"source": {"attachment": "a.xlsx"}})
        self._write("turnos-b.json", {"source": {"attachment": "b.xlsx"}})
        calls = []

        def abrir(req):
            calls.append(req)
            return _FakeResponse(200, json.dumps({"lote": 1, "turnos": 4, "nuevo": True}).encode())

        rc, out = self._run(abrir)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("2 subidos, 0 ya estaban, 0 rechazados", out)

        rc2, out2 = self._run(abrir)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(calls), 2, "already-uploaded files must not be re-sent")
        self.assertIn("0 subidos, 0 ya estaban, 0 rechazados", out2)

    def test_200_with_nuevo_false_is_still_recorded(self):
        self._write("turnos-a.json", {"source": {}})

        def abrir(req):
            return _FakeResponse(200, json.dumps({"lote": 1, "turnos": 4, "nuevo": False}).encode())

        rc, out = self._run(abrir)
        self.assertEqual(rc, 0)
        self.assertIn("0 subidos, 1 ya estaban, 0 rechazados", out)

        calls = []

        def abrir2(req):
            calls.append(req)
            return _FakeResponse(200, b"{}")

        self._patch_abrir(abrir2)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            subir_turnos.main(["--dir", str(self.shifts_dir)])
        self.assertEqual(calls, [], "a file recorded with nuevo=false must not be re-sent")

    def test_422_is_not_recorded_so_a_fixed_file_retries(self):
        self._write("turnos-bad.json", {"source": {}})
        calls = []

        def abrir(req):
            calls.append(req)
            raise _http_error(422, {"error": "archivo_rechazado", "motivo": "esquema"})

        rc, out = self._run(abrir)
        self.assertEqual(rc, 0)
        self.assertIn("0 subidos, 0 ya estaban, 1 rechazados", out)

        rc2, out2 = self._run(abrir)
        self.assertEqual(len(calls), 2, "a 422'd file must retry on the next run")
        self.assertIn("1 rechazados", out2)

    def test_401_stops_and_says_to_reconnect(self):
        self._write("turnos-a.json", {"source": {}})
        self._write("turnos-b.json", {"source": {}})
        calls = []

        def abrir(req):
            calls.append(req)
            raise _http_error(401, {"error": "no_autorizado"})

        rc, out = self._run(abrir)
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1, "must stop at the first 401, not try every file")
        self.assertIn("conectar_pc", out)

    def test_403_stops_and_says_to_reconnect(self):
        self._write("turnos-a.json", {"source": {}})

        def abrir(req):
            raise _http_error(403, {"error": "alcance"})

        rc, out = self._run(abrir)
        self.assertEqual(rc, 1)
        self.assertIn("conectar_pc", out)

    def test_output_has_no_date_and_no_shift_content(self):
        self._write("turnos-a.json", {"source": {}, "shifts": [{"day": "2026-11-09", "kind": "night"}]})

        def abrir(req):
            return _FakeResponse(200, json.dumps({"lote": 1, "turnos": 1, "nuevo": True}).encode())

        rc, out = self._run(abrir)
        self.assertEqual(rc, 0)
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", out))
        self.assertNotIn("night", out)

    def test_missing_link_file_stops_before_any_call(self):
        pc_cliente.borrar_enlace()
        self._write("turnos-a.json", {"source": {}})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = subir_turnos.main(["--dir", str(self.shifts_dir)])
        self.assertEqual(rc, 1)
        self.assertIn("conectar_pc", buf.getvalue())


# ---- python -m body.traer_semana ---------------------------------------------------------------

class TraerSemanaTest(_TempHomeTestCase):
    def setUp(self):
        super().setUp()
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)

    def _run(self, argv, abrir):
        self._patch_abrir(abrir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = traer_semana.main(argv)
        return rc, buf.getvalue()

    def test_writes_the_exact_local_path_atomically_and_prints_only_it(self):
        payload = {"area": "body", "metrics": [{"key": "body.resting_hr", "value": 58.0}]}

        def abrir(req):
            return _FakeResponse(200, json.dumps(payload).encode())

        rc, out = self._run([], abrir)
        self.assertEqual(rc, 0)
        monday = vida_export.current_week_monday()
        expected = config.BODY_HOME / "vida" / f"body-week-{monday.isoformat()}.json"
        self.assertEqual(out.strip(), str(expected))
        self.assertNotIn("58.0", out)
        self.assertTrue(expected.exists())
        self.assertEqual(json.loads(expected.read_text(encoding="utf-8")), payload)
        self.assertFalse(expected.with_name(expected.name + ".tmp").exists())

    def test_explicit_week_uses_that_monday(self):
        monday = vida_export.current_week_monday() - timedelta(days=7)

        def abrir(req):
            return _FakeResponse(200, b"{}")

        rc, out = self._run(["--semana", monday.isoformat()], abrir)
        self.assertEqual(rc, 0)
        expected = config.BODY_HOME / "vida" / f"body-week-{monday.isoformat()}.json"
        self.assertEqual(out.strip(), str(expected))

    def test_401_stops_without_writing(self):
        def abrir(req):
            raise _http_error(401, {"error": "no_autorizado"})

        rc, out = self._run([], abrir)
        self.assertEqual(rc, 1)
        self.assertFalse((config.BODY_HOME / "vida").exists())
        self.assertIn("conectar_pc", out)

    def test_403_stops_without_writing(self):
        def abrir(req):
            raise _http_error(403, {"error": "alcance"})

        rc, out = self._run([], abrir)
        self.assertEqual(rc, 1)
        self.assertFalse((config.BODY_HOME / "vida").exists())

    def test_missing_link_file_stops_before_any_call(self):
        pc_cliente.borrar_enlace()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = traer_semana.main([])
        self.assertEqual(rc, 1)
        self.assertIn("conectar_pc", buf.getvalue())


# ---- scripts/install-daily-sync.ps1 (text only -- never executed) -----------------------------

class InstallerHostedTest(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def _hosted_block(self) -> str:
        start = self.text.index("if ($Hosted)")
        close = self.text.index("\n}\n", start)
        return self.text[start:close]

    def _after_hosted_block(self) -> str:
        start = self.text.index("if ($Hosted)")
        close = self.text.index("\n}\n", start)
        return self.text[close:]

    def test_hosted_switch_declared(self):
        m = re.search(r"^param\((.+)\)$", self.text, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertIn("[switch]$Hosted", m.group(1))

    def test_hosted_registers_shift_upload_and_weekly_fetch(self):
        block = self._hosted_block()
        self.assertIn('Register-ScheduledTask -TaskName "Body app shift upload"', block)
        self.assertIn('Register-ScheduledTask -TaskName "Body app weekly fetch"', block)
        self.assertIn("body.vivo subir_turnos", block)
        self.assertIn("body.vivo traer_semana", block)
        self.assertIn("-ShiftsDir", self.text)

    def test_hosted_unregisters_the_three_old_tasks(self):
        block = self._hosted_block()
        self.assertIn("Unregister-ScheduledTask", block)
        for name in ("Body app daily sync", "Body app weekly file", "Body app web"):
            self.assertIn(name, block)

    def test_hosted_refuses_without_the_link_file(self):
        block = self._hosted_block()
        self.assertIn("dispositivo.json", block)
        self.assertIn("conectar_pc", block)
        self.assertIn("exit 1", block)

    def test_hosted_requires_shiftsdir(self):
        block = self._hosted_block()
        self.assertIn("$ShiftsDir", block)
        self.assertIn("exit 1", block)

    def test_no_key_bearing_text_in_hosted_block(self):
        block = self._hosted_block()
        self.assertNotIn("clave", block.lower())
        self.assertNotIn("bdy_", block)

    def test_non_hosted_task_names_unchanged(self):
        after = self._after_hosted_block()
        for name in ("Body app daily sync", "Body app weekly file", "Body app web"):
            self.assertIn(f'Register-ScheduledTask -TaskName "{name}"', after)
        self.assertNotIn("Body app shift upload", after)
        self.assertNotIn("Body app weekly fetch", after)

    def test_non_hosted_still_defaults_to_0930_and_1800(self):
        after = self._after_hosted_block()
        self.assertIn('"09:30"', after)


if __name__ == "__main__":
    unittest.main()
