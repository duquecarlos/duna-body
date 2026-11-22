"""BODY-DEMO-HOY-FIJO-1 ruling 3: a demo._seed()/demo.generar() call with no explicit `today=`
reads the real calendar date through whichever route it later exercises
(body/rutas/comun.py's `_today_local`) -- the suite's own rule (tests/test_demo_fechas.py's own
precedent) is that a demo seed must not depend on which real day the suite happens to run on.
BODY-REGISTRO-COBERTURA-2's gate report caught exactly this on 2026-11-22, a Sunday
(tests/test_estados_vacios_2.py:244, fixed by BODY-DEMO-HOY-FIJO-1 itself). This guard census
fails the day a new unpinned seed is WRITTEN, naming file:line, instead of waiting for its own
unlucky day.

Uses `ast`, not a text/regex scan, so a `demo._seed(` mention inside a comment or docstring (this
module's own module-level test fixtures reference the call in prose, e.g.
tests/test_rendimiento.py's own header comments) is never mistaken for a real call.
"""
import ast
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

# ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2 found no test here whose own SUBJECT is the real
# calendar clock -- every demo._seed()/demo.generar() call must pin `today=`. Left empty on
# purpose; a name is added here only when a future test is deliberately exercising the real
# clock itself, with a comment saying so.
_ALLOWLIST: frozenset[str] = frozenset()

_SEED_ATTRS = ("_seed", "generar")


def _unpinned_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _SEED_ATTRS:
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "demo"):
            continue
        if any(kw.arg == "today" for kw in node.keywords):
            continue
        offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: demo.{func.attr}(...) with no today=")
    return offenders


class DemoSeedPinsTheCalendarTest(unittest.TestCase):
    def test_every_demo_seed_call_pins_today(self):
        test_files = sorted(p for p in TESTS_DIR.glob("*.py") if p.name != "test_demo_hoy_fijo.py")
        self.assertGreater(len(test_files), 50, "the census scanned too few test files")

        offenders = []
        for path in test_files:
            for offender in _unpinned_calls(path):
                if offender not in _ALLOWLIST:
                    offenders.append(offender)

        self.assertEqual(
            offenders, [],
            "demo seed(s) with no today= -- pin the calendar (ORCH-CARLOS-DELEGATES-TIER-1-1 "
            "ruling 2) or add to _ALLOWLIST with a comment saying why the test's own subject is "
            "the real clock:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
