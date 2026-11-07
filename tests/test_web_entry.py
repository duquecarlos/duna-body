"""body.web._ensure_streams and main(): pythonw has no console, so sys.stdout/sys.stderr are None there."""
import sys
import unittest
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import web


class EnsureStreamsTest(unittest.TestCase):
    def test_replaces_missing_streams(self):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = None
        sys.stderr = None
        try:
            web._ensure_streams()
            self.assertIsNotNone(sys.stdout)
            self.assertIsNotNone(sys.stderr)
            sys.stdout.write("")
            sys.stderr.write("")
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def test_leaves_present_streams_untouched(self):
        marker_out, marker_err = sys.stdout, sys.stderr
        web._ensure_streams()
        self.assertIs(sys.stdout, marker_out)
        self.assertIs(sys.stderr, marker_err)


class MainEntryTest(unittest.TestCase):
    def test_main_reaches_uvicorn_run_with_no_streams(self):
        old_out, old_err, old_argv = sys.stdout, sys.stderr, sys.argv
        sys.stdout = None
        sys.stderr = None
        sys.argv = ["body.web"]
        try:
            with patch("uvicorn.run") as run:
                web.main()
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs.get("access_log"), False)
        finally:
            sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv


if __name__ == "__main__":
    unittest.main()
