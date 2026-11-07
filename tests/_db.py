"""Helper for the next slice to adopt (BODY-CONEXIONES-CERRADAS-1): opens a connection the same way
existing test modules already do (db.connect()) and closes it automatically when the test tears
down, via case.addCleanup(), instead of each test module managing its own explicit conn.close() at
every return path. Not yet adopted by any existing test module -- this slice only adds it.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db


def open_conn(case: unittest.TestCase, path: Path | None = None) -> sqlite3.Connection:
    """db.connect(path), closed automatically when `case` tears down."""
    conn = db.connect(path)
    case.addCleanup(conn.close)
    return conn
