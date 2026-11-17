"""Import FIRST in every test module: points Body at a throwaway home so tests can never touch real health data.

BODY-TEST-TMP-FUGA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1, test infrastructure only, isolation guarantee unchanged):
ruling 1 -- tempfile.tempdir = TMP right after TMP is created, so every later mkdtemp/TemporaryDirectory/mkstemp/
NamedTemporaryFile in the run lands inside this run's own home;
ruling 2 -- atexit removes the whole home when the process ends, one mechanism, owned here;
ruling 3 -- a hard kill skips atexit, so on import this module also sweeps SIBLING body-test-* folders in the
system temp root last modified more than 24 hours ago, errors ignored, only its own prefix touched;
ruling 4 -- the guard's order and assertion are unchanged: BODY_HOME and config paths point inside TMP before
any body module touches a database.
"""
import atexit
import os
import shutil
import tempfile
import time
from pathlib import Path

_SWEEP_AGE_SECONDS = 24 * 3600


def _sweep_stale_siblings(root: str) -> None:
    cutoff = time.time() - _SWEEP_AGE_SECONDS
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith("body-test-"):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            pass


_sweep_stale_siblings(tempfile.gettempdir())

TMP = tempfile.mkdtemp(prefix="body-test-")
tempfile.tempdir = TMP
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
os.environ["BODY_HOME"] = TMP

from body import config  # noqa: E402

# Patch the module too, in case it was imported before this file ran.
config.BODY_HOME = Path(TMP)
config.DB_PATH = config.BODY_HOME / "body.db"
config.TOKEN_DIR = config.BODY_HOME / "garmin_tokens"
assert str(config.DB_PATH).startswith(TMP)
