"""Where things live. Health data and tokens stay outside the repo, in BODY_HOME."""
import os
from pathlib import Path

BODY_HOME = Path(os.environ.get("BODY_HOME", Path.home() / ".body-app")).expanduser()
DB_PATH = BODY_HOME / "body.db"
TOKEN_DIR = BODY_HOME / "garmin_tokens"


def ensure_home() -> None:
    BODY_HOME.mkdir(parents=True, exist_ok=True)
