"""Where things live. Health data and tokens stay outside the repo, in BODY_HOME."""
import os
from pathlib import Path

BODY_HOME = Path(os.environ.get("BODY_HOME", Path.home() / ".body-app")).expanduser()
DB_PATH = BODY_HOME / "body.db"
TOKEN_DIR = BODY_HOME / "garmin_tokens"


def ensure_home() -> None:
    BODY_HOME.mkdir(parents=True, exist_ok=True)


def commute_file() -> Path | None:
    """BODY_COMMUTE_FILE (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5): the Personal desk's commute
    CSV, read here rather than a second `os.environ.get` elsewhere. No default path -- never a
    literal naming any person's own folder."""
    value = os.environ.get("BODY_COMMUTE_FILE")
    return Path(value).expanduser() if value else None
