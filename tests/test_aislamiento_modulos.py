"""BODY-TESTS-VELOCIDAD-2: regression guard for the sys.modules leak fixed in
tests/test_env_local_1.py::ImportarWebTest (root cause: a `del sys.modules["body.web"]` plus
re-import repoints the `body` package's own `web` attribute at the throwaway module, and the
old restore wrote back only the sys.modules entry). Runs that test in-process through a
TextTestRunner and asserts afterwards that sys.modules["body.web"] and the `body` package's
`web` attribute are the same object again, both when `body.web` was already imported before
the test ran and when it was not.
"""
import io
import sys
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

import body
from tests.test_env_local_1 import ImportarWebTest


def _run_importar_web_test():
    suite = unittest.TestLoader().loadTestsFromTestCase(ImportarWebTest)
    return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)


class AislamientoModulosTest(unittest.TestCase):
    def test_present_before_is_restored_to_the_same_object_in_both_slots(self):
        self.assertIn("body.web", sys.modules)
        antes = sys.modules["body.web"]

        resultado = _run_importar_web_test()

        self.assertTrue(resultado.wasSuccessful())
        self.assertIs(sys.modules["body.web"], antes)
        self.assertIs(body.web, antes)
        import body.web as modulo_importado
        self.assertIs(modulo_importado, antes)

    def test_absent_before_is_restored_to_absent_in_both_slots(self):
        modulo_original = sys.modules.pop("body.web")
        atributo_original = body.web
        del body.web
        try:
            resultado = _run_importar_web_test()

            self.assertTrue(resultado.wasSuccessful())
            self.assertNotIn("body.web", sys.modules)
            self.assertFalse(hasattr(body, "web"))
        finally:
            sys.modules["body.web"] = modulo_original
            body.web = atributo_original
        self.assertIs(sys.modules["body.web"], modulo_original)
        self.assertIs(body.web, atributo_original)


if __name__ == "__main__":
    unittest.main()
