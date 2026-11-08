"""BODY-LOGIN-MENSAJES-1: python -m body.login is Carlos's own first sign-in. The password must
never be printed on any path, and a failure must show only the exception's TYPE -- never its
message, which garminconnect sometimes builds out of whatever was typed.
"""
import contextlib
import io
import unittest
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import login

DEMO_EMAIL = "demo.carlos@example.invalid"
DEMO_PASSWORD = "demo-clave-NO-REAL-9f2"
DEMO_CODE = "445566"


class SignInOkTest(unittest.TestCase):
    def test_success_calls_sign_in_with_typed_values_and_prints_the_success_line(self):
        with mock.patch("builtins.input", return_value=DEMO_EMAIL), \
             mock.patch("body.login.getpass.getpass", return_value=DEMO_PASSWORD), \
             mock.patch("body.login.garmin_adapter.sign_in") as sign_in:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                codigo = login.main()

        self.assertEqual(codigo, 0)
        sign_in.assert_called_once()
        args, kwargs = sign_in.call_args
        self.assertEqual(args[0], DEMO_EMAIL)
        self.assertEqual(args[1], DEMO_PASSWORD)
        self.assertIn("Signed in", buf.getvalue())

    def test_password_is_never_printed_on_success(self):
        with mock.patch("builtins.input", return_value=DEMO_EMAIL), \
             mock.patch("body.login.getpass.getpass", return_value=DEMO_PASSWORD), \
             mock.patch("body.login.garmin_adapter.sign_in"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                login.main()
        self.assertNotIn(DEMO_PASSWORD, buf.getvalue())


class SignInFailureTest(unittest.TestCase):
    def test_failure_returns_1_and_never_prints_the_password_or_email(self):
        exc = RuntimeError(f"rejected: user={DEMO_EMAIL} pass={DEMO_PASSWORD}")
        with mock.patch("builtins.input", return_value=DEMO_EMAIL), \
             mock.patch("body.login.getpass.getpass", return_value=DEMO_PASSWORD), \
             mock.patch("body.login.garmin_adapter.sign_in", side_effect=exc):
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                codigo = login.main()

        self.assertEqual(codigo, 1)
        combinado = out.getvalue() + err.getvalue()
        self.assertNotIn(DEMO_PASSWORD, combinado)
        self.assertNotIn(DEMO_EMAIL, combinado)
        self.assertIn("RuntimeError", combinado)

    def test_mfa_prompt_callable_reads_input_and_strips_it(self):
        with mock.patch("builtins.input", return_value=DEMO_EMAIL), \
             mock.patch("body.login.getpass.getpass", return_value=DEMO_PASSWORD), \
             mock.patch("body.login.garmin_adapter.sign_in") as sign_in:
            with contextlib.redirect_stdout(io.StringIO()):
                login.main()

        prompt_mfa = sign_in.call_args.kwargs["prompt_mfa"]
        with mock.patch("builtins.input", return_value=f"  {DEMO_CODE}  "):
            self.assertEqual(prompt_mfa(), DEMO_CODE)


if __name__ == "__main__":
    unittest.main()
