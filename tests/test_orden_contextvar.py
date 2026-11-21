"""BODY-TURNOS-ACTUAL-CONTEXTVAR-LEAK-1: tests/test_mcp.py's ToolsListTest used to call
herramientas.fijar_turnos_actual(...) without restoring it afterwards (unlike every other test
that sets it, each of which registers an addCleanup -- tests/test_mcp_remoto.py,
tests/test_sync_vivo.py, tests/test_turno_dia_plan.py). herramientas._TURNOS_ACTUAL is a module
ContextVar, so a leaked value survives past the test that set it, within the same process, and
reaches whichever test runs next -- here, a later module in a NON-alphabetical run order (unittest
discover's default order is alphabetical by module name, and "test_agente_registro" already sorts
before "test_mcp", so the full gate run never hit this; a chunked or reordered run could).

This reproduces the exact order that exposed it -- test_mcp's own leaking test, then
test_agente_registro's own DisponiblesTest, in ONE process -- and proves the fix (the same
addCleanup every sibling already uses) actually stops the leak, rather than merely asserting the
cleanup call exists. Scoped to the one named test method, not the whole of ToolsListTest: a
sibling method in that same class also happens to touch the contextvar as a side effect of the
real request path it exercises (body/mcp.py's own _nivel(), which every request sets afresh) --
a separate, broader test-isolation question this slice's spec does not reach."""
from __future__ import annotations

import io
import unittest

from tests._isolate import TMP  # noqa: F401,E402  (must come before any body import)

from tests import test_agente_registro, test_mcp


class OrderIndependenceTest(unittest.TestCase):
    def test_agente_registro_disponibles_passes_when_run_after_test_mcp_in_one_process(self):
        loader = unittest.TestLoader()
        suite = unittest.TestSuite([
            test_mcp.ToolsListTest("test_list_matches_the_in_app_level_and_has_no_apply_undo_delete"),
            loader.loadTestsFromTestCase(test_agente_registro.DisponiblesTest),
        ])
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertTrue(
            result.wasSuccessful(),
            f"{len(result.failures)} failure(s), {len(result.errors)} error(s): "
            f"{result.failures + result.errors}")
        # Non-vacuous: the leaking test plus DisponiblesTest's own three methods actually ran.
        self.assertEqual(result.testsRun, 4)


if __name__ == "__main__":
    unittest.main()
