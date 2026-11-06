"""Import FIRST in every test module: points Body at a throwaway home so tests can never touch real health data."""
import os
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="body-test-")
os.environ["BODY_HOME"] = TMP

from body import config  # noqa: E402

# Patch the module too, in case it was imported before this file ran.
config.BODY_HOME = Path(TMP)
config.DB_PATH = config.BODY_HOME / "body.db"
config.TOKEN_DIR = config.BODY_HOME / "garmin_tokens"
assert str(config.DB_PATH).startswith(TMP)
