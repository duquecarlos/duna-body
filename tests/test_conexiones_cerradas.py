"""Guard for BODY-CONEXIONES-CERRADAS-1: once every route closes the connection it opened, exercising
the main request paths through the test client raises no ResourceWarning for an unclosed
sqlite3.Connection. Deterministic because the fix closes explicitly (decorators / try-finally
around db.connect()), not by relying on garbage collection to eventually close a leaked connection --
gc.collect() here is just belt-and-suspenders, not what makes the assertion pass.
"""
import gc
import tempfile
import unittest
import warnings
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, web

client = TestClient(web.app, follow_redirects=False)

# A representative sample of the main request path: every one of these goes through web.py's
# _ctx() (the module's single biggest opening site before this slice), covering most of the
# decorated routes in one pass.
_ROUTES = ["/", "/goals", "/goals/new", "/trends", "/history", "/settings", "/shifts",
           "/shifts/como", "/ayuda", "/privacidad", "/ajustes/asistente"]


def _reset_db(tc: unittest.TestCase) -> None:
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "c.db"
    conn = db.connect()
    db.init(conn)
    conn.close()


class ConexionesCerradasTest(unittest.TestCase):
    def test_no_unclosed_sqlite_warning_across_main_routes(self):
        _reset_db(self)
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for route in _ROUTES:
                r = client.get(route)
                self.assertLess(r.status_code, 500, route)
            gc.collect()

        sqlite_leaks = [w for w in caught
                        if issubclass(w.category, ResourceWarning) and "sqlite3.Connection" in str(w.message)]
        self.assertEqual([], [str(w.message) for w in sqlite_leaks])


if __name__ == "__main__":
    unittest.main()
