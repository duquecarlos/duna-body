"""Which address the web server binds, and the line it prints for each choice."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import listen


class ListenTest(unittest.TestCase):
    def test_local(self):
        self.assertEqual(listen.listen_host(False), "127.0.0.1")

    def test_lan(self):
        self.assertEqual(listen.listen_host(True), "0.0.0.0")

    def test_describe_never_prints_bind_all(self):
        local = listen.describe(False, 8770)
        lan = listen.describe(True, 8770)
        self.assertIn("8770", local)
        self.assertIn("8770", lan)
        self.assertNotIn("0.0.0.0", local)
        self.assertNotIn("0.0.0.0", lan)

    def test_lan_line_differs_from_local(self):
        self.assertNotEqual(listen.describe(False, 8770), listen.describe(True, 8770))
