"""Guard for BODY-CONEXIONES-CERRADAS-1: once every route closes the connection it opened, exercising
the main request paths through the test client raises no ResourceWarning for an unclosed
sqlite3.Connection. Deterministic because the fix closes explicitly (decorators / try-finally
around db.connect()), not by relying on garbage collection to eventually close a leaked connection --
gc.collect() here is just belt-and-suspenders, not what makes the assertion pass.
"""
import ast
import contextlib
import gc
import inspect
import io
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, garmin_adapter, pull, shift_import, vida_export, web

client = TestClient(web.app, follow_redirects=False)

FIXTURES = Path(__file__).parent / "fixtures"

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


class _CloseRecorder:
    """Wraps a real sqlite3.Connection, recording whether close() was ever called on it."""

    def __init__(self, real):
        self._real = real
        self.closed = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        self.closed = True
        self._real.close()


def _no_sqlite_leak(caught) -> list[str]:
    return [str(w.message) for w in caught
            if issubclass(w.category, ResourceWarning) and "sqlite3.Connection" in str(w.message)]


class MainClosesConnectionTest(unittest.TestCase):
    """BODY-CONEXIONES-T1-1: the command-line main() of shift_import, vida_export and pull each
    close the connection they open, on the happy path and when the work inside raises."""

    def _recording_connect(self):
        real_connect = db.connect
        recorders: list[_CloseRecorder] = []

        def _connect(*a, **kw):
            rec = _CloseRecorder(real_connect(*a, **kw))
            recorders.append(rec)
            return rec

        return _connect, recorders

    def test_shift_import_main_closes_on_success(self):
        _reset_db(self)
        folder = Path(tempfile.mkdtemp(dir=TMP))
        (folder / "turnos-demo-a.json").write_text(
            (FIXTURES / "turnos-demo-a.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                shift_import.main(["--dir", str(folder), "--person-id", "1"])
            gc.collect()
        self.assertEqual([], _no_sqlite_leak(caught))

    def test_shift_import_main_closes_when_work_raises(self):
        _reset_db(self)
        folder = Path(tempfile.mkdtemp(dir=TMP))
        (folder / "turnos-demo-a.json").write_text(
            (FIXTURES / "turnos-demo-a.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        connect, recorders = self._recording_connect()
        with mock.patch("body.shift_import.db.connect", side_effect=connect):
            with mock.patch("body.shift_import.db.init", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    shift_import.main(["--dir", str(folder), "--person-id", "1"])
        self.assertTrue(recorders and recorders[0].closed)

    def test_vida_export_main_closes_on_success(self):
        _reset_db(self)
        orig_home = config.BODY_HOME
        self.addCleanup(lambda: setattr(config, "BODY_HOME", orig_home))
        config.BODY_HOME = Path(tempfile.mkdtemp(dir=TMP))
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                vida_export.main(["--week", "2026-11-02"])
            gc.collect()
        self.assertEqual([], _no_sqlite_leak(caught))

    def test_vida_export_main_closes_when_work_raises(self):
        _reset_db(self)
        connect, recorders = self._recording_connect()
        with mock.patch("body.vida_export.db.connect", side_effect=connect):
            with mock.patch("body.vida_export.db.init", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    vida_export.main(["--week", "2026-11-02"])
        self.assertTrue(recorders and recorders[0].closed)

    def test_pull_main_closes_on_success(self):
        _reset_db(self)
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            buf = io.StringIO()
            with mock.patch("body.pull.garmin_adapter.fetch_day",
                             return_value=garmin_adapter.DayData(day="2026-11-01")):
                with contextlib.redirect_stdout(buf):
                    pull.main(["--date", "2026-11-01"])
            gc.collect()
        self.assertEqual([], _no_sqlite_leak(caught))

    def test_pull_main_closes_when_work_raises(self):
        _reset_db(self)
        connect, recorders = self._recording_connect()
        with mock.patch("body.pull.db.connect", side_effect=connect):
            with mock.patch("body.pull.db.init", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    pull.main(["--date", "2026-11-01"])
        self.assertTrue(recorders and recorders[0].closed)


_CLOSE_DECORATORS = {"_cierra_conexion", "_cierra_conexion_async"}

# asistente_preguntar returns a StreamingResponse whose generator keeps using the connection
# after the route function itself returns (BODY-CONEXIONES-CERRADAS-1 decision 1) -- it already
# closes that connection correctly in the generator's own `finally`, so it carries neither decorator.
_ALLOWED_WITHOUT_DECORATOR = {"asistente_preguntar"}


def _decorator_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return None


def _calls_ctx(func_node) -> bool:
    for node in ast.walk(func_node):
        if node is func_node:
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_ctx":
            return True
    return False


class RouteClosesItsConnectionTest(unittest.TestCase):
    """Guard for BODY-CONEXIONES-CERRADAS-2: every top-level function in body/web.py that calls
    _ctx( -- the exact name, not a helper like _hoy_turno_ctx -- carries _cierra_conexion or
    _cierra_conexion_async, except the one documented exception in _ALLOWED_WITHOUT_DECORATOR."""

    def test_every_route_calling_ctx_carries_a_close_decorator(self):
        source = inspect.getsource(web)
        tree = ast.parse(source)
        missing = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _calls_ctx(node):
                continue
            if node.name in _ALLOWED_WITHOUT_DECORATOR:
                continue
            names = {_decorator_name(d) for d in node.decorator_list}
            if not (names & _CLOSE_DECORATORS):
                missing.append(node.name)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
