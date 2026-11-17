"""BODY-TEST-TMP-FUGA-1: tests/_isolate.py's own lifecycle -- TMP is removed when the process ends
(ruling 2), everything that calls tempfile.mkdtemp()/NamedTemporaryFile() after the guard lands inside
TMP (ruling 1), a hard kill's stale siblings are swept on the next import (ruling 3), and the guard's
own assertion still holds (ruling 4)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config

ROOT = Path(__file__).resolve().parent.parent


class TmpRemovedAtExitTest(unittest.TestCase):
    def test_child_process_home_is_gone_after_it_exits(self):
        code = (
            "import tempfile\n"
            "from pathlib import Path\n"
            "from tests._isolate import TMP\n"
            "(Path(TMP) / 'marker.txt').write_text('x', encoding='utf-8')\n"
            "tempfile.mkdtemp()\n"
            "print(TMP)\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                 text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        child_tmp = result.stdout.strip()
        self.assertTrue(child_tmp)
        self.assertFalse(Path(child_tmp).exists(), f"{child_tmp} still exists after the child exited")


class MkdtempLandsInsideTmpTest(unittest.TestCase):
    def test_mkdtemp_after_the_guard_lands_inside_tmp(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(d.parent, Path(TMP))

    def test_named_temp_file_lands_inside_tmp(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = Path(f.name)
        self.assertEqual(path.parent, Path(TMP))


class StaleSiblingSweepTest(unittest.TestCase):
    def test_only_stale_body_test_siblings_are_removed(self):
        scratch_root = tempfile.mkdtemp(prefix="body-test-sweep-scratch-")
        old_match = Path(scratch_root) / "body-test-old"
        new_match = Path(scratch_root) / "body-test-new"
        old_other = Path(scratch_root) / "other-old"
        for d in (old_match, new_match, old_other):
            d.mkdir()
        old_stamp = time.time() - 25 * 3600
        new_stamp = time.time() - 1 * 3600
        os.utime(old_match, (old_stamp, old_stamp))
        os.utime(old_other, (old_stamp, old_stamp))
        os.utime(new_match, (new_stamp, new_stamp))

        code = (
            "import tempfile\n"
            f"tempfile.tempdir = {scratch_root!r}\n"
            "import tests._isolate\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                 text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse(old_match.exists(), "a stale body-test-* sibling survived the sweep")
        self.assertTrue(new_match.exists(), "a fresh body-test-* sibling was swept")
        self.assertTrue(old_other.exists(), "a non-body-test folder was swept")


class GuardAssertionStillHoldsTest(unittest.TestCase):
    def test_db_path_is_inside_tmp(self):
        self.assertTrue(str(config.DB_PATH).startswith(TMP))


if __name__ == "__main__":
    unittest.main()
