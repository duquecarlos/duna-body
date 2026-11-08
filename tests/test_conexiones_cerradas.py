"""Guard for BODY-CONEXIONES-CERRADAS-1: once every route closes the connection it opened, exercising
the main request paths through the test client raises no ResourceWarning for an unclosed
sqlite3.Connection. Deterministic because the fix closes explicitly (decorators / try-finally
around db.connect()), not by relying on garbage collection to eventually close a leaked connection --
gc.collect() here is just belt-and-suspenders, not what makes the assertion pass.
"""
import ast
import contextlib
import gc
import io
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._rutas import modulos_con_rutas

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

_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}

# BODY-WEB-ESTRUCTURA-8: pwa.py's /sw.js serves a static asset by content (the cache name built
# from comun.STATIC_HASH) -- it is not about any person's own data, opens no connection, and so
# carries no route that calls _ctx. sesion.py's routes open their own connection with plain
# db.connect()/db.init() (as they did in web.py before this slice, unchanged by the move) rather
# than through comun._ctx -- there is no person_id to scope by before sign-in has happened. A
# module named here may have routes without a single one of them calling _ctx; every OTHER
# router module must still have at least one that does.
_MODULES_WITHOUT_A_CTX_ROUTE = {"pwa.py", "sesion.py"}


def _decorator_name(node) -> str | None:
    """The decorator's own name -- a bare `@_cierra_conexion` or the final attribute of
    `@comun._cierra_conexion` (BODY-WEB-ESTRUCTURA-3: a route in body/rutas/ carries the
    attribute form, since the close decorators live in body/rutas/comun.py, not in the route's
    own module)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _calls_ctx(func_node) -> bool:
    """True if `func_node`'s body calls `_ctx(` bare or as an attribute of any name
    (`comun._ctx(`) -- the same two forms a route's close decorator can take."""
    for node in ast.walk(func_node):
        if node is func_node:
            continue
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == "_ctx":
            return True
        if isinstance(f, ast.Attribute) and f.attr == "_ctx":
            return True
    return False


def _is_route_decorator(node) -> bool:
    """True for `@router.get(...)` / `@app.post(...)` and the other HTTP-method decorators --
    what makes a function a route handler, independent of whether it calls _ctx."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _HTTP_METHODS)


def _func_text(source_lines: list[str], node) -> str:
    end = getattr(node, "end_lineno", node.lineno)
    return "\n".join(source_lines[node.lineno - 1:end])


class RouteClosesItsConnectionTest(unittest.TestCase):
    """Guard for BODY-CONEXIONES-CERRADAS-2 / BODY-WEB-ESTRUCTURA-3: every top-level function in
    body/web.py or a body/rutas/*.py module that calls _ctx( -- bare or as comun._ctx( -- carries
    _cierra_conexion or _cierra_conexion_async (bare or as comun.X), except the one documented
    exception in _ALLOWED_WITHOUT_DECORATOR.

    Non-vacuous by construction: this guard used to read only `inspect.getsource(web)` and match
    only a bare `_ctx(` call, so it checked zero of the routes already moved into body/rutas/ --
    a green test that tested nothing (BODY-WEB-ESTRUCTURA-3). `checked` (what the AST walk this
    guard runs actually found) is cross-checked here against `expected_min`, a plain substring
    count of "_ctx(" inside each route-decorated function's own source slice -- a method sharing
    no code with `_calls_ctx`, so if the walk regresses to matching nothing, `checked` drops to
    zero while `expected_min` stays positive and the assertion fails instead of passing quietly."""

    def test_every_route_calling_ctx_carries_a_close_decorator(self):
        missing = []
        checked = 0
        expected_min = 0
        modules_with_routes = set()
        modules_checked = set()
        for path, tree in modulos_con_rutas():
            source_lines = path.read_text(encoding="utf-8").splitlines()
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(_is_route_decorator(d) for d in node.decorator_list):
                    modules_with_routes.add(path)
                    if "_ctx(" in _func_text(source_lines, node):
                        expected_min += 1
                if not _calls_ctx(node):
                    continue
                checked += 1
                modules_checked.add(path)
                if node.name in _ALLOWED_WITHOUT_DECORATOR:
                    continue
                names = {_decorator_name(d) for d in node.decorator_list}
                if not (names & _CLOSE_DECORATORS):
                    missing.append(f"{path.name}:{node.name}")
        self.assertEqual(missing, [])
        self.assertGreaterEqual(checked, expected_min)
        self.assertTrue(modules_with_routes, "no router module carries a route -- the scan is broken")
        allowed_unchecked = {p for p in modules_with_routes if p.name in _MODULES_WITHOUT_A_CTX_ROUTE}
        self.assertEqual(modules_checked | allowed_unchecked, modules_with_routes,
                          f"a router module has routes but none call _ctx -- checked {modules_checked} "
                          f"vs routed {modules_with_routes}")


# BODY-SESION-CONEXION-1: body/rutas/privacidad.py:40 (consentimiento_decide's "decline" branch)
# opens its own connection the same unguarded way sesion.py's four routes did before this slice --
# found while building the guard below, left unfixed because it is outside this slice's `touches:`
# (DECISIONS.md's BODY-SESION-CONEXION-1 entry lists it for a later slice).
_KNOWN_DIRECT_CONNECT_OFFENDERS = {("privacidad.py", "consentimiento_decide")}


def _stmt_lists(func_node):
    """Every list of statements reachable from `func_node`: its own body, plus the body/orelse/
    finalbody of each nested if/for/while/try, and each except handler's body."""
    lists = [func_node.body]
    for child in ast.walk(func_node):
        if child is func_node:
            continue
        for attr in ("body", "orelse", "finalbody"):
            lst = getattr(child, attr, None)
            if isinstance(lst, list):
                lists.append(lst)
        for h in getattr(child, "handlers", []) or []:
            lists.append(h.body)
    return lists


def _connect_assigns(stmts):
    """(stmts, index, var_name) for each `var = db.connect(...)` / `var = X.connect(...)` found at
    the top of `stmts`."""
    found = []
    for i, stmt in enumerate(stmts):
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute)
                and stmt.value.func.attr == "connect"):
            found.append((stmts, i, stmt.targets[0].id))
    return found


def _closes_right_after(stmts, i, var_name) -> bool:
    """True when the statement right after `stmts[i]` (the `var = ...connect()` assignment) is a
    `try` whose `finally` closes `var_name`, or a `with contextlib.closing(var_name)`."""
    if i + 1 >= len(stmts):
        return False
    nxt = stmts[i + 1]
    if isinstance(nxt, ast.Try):
        for stmt in nxt.finalbody:
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "close" and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == var_name):
                    return True
        return False
    if isinstance(nxt, ast.With):
        for item in nxt.items:
            ctx = item.context_expr
            if (isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute)
                    and ctx.func.attr == "closing" and ctx.args
                    and isinstance(ctx.args[0], ast.Name) and ctx.args[0].id == var_name):
                return True
        return False
    return False


class DirectConnectClosesInFinallyTest(unittest.TestCase):
    """Guard for BODY-SESION-CONEXION-1: a route-decorated function may open its own connection
    with a bare db.connect() rather than through comun._ctx -- true before sign-in, when there is
    no person_id for _ctx to scope by. Such a connection must close in the `finally` of a `try`
    that starts on the very next statement after the assignment (ast: the statement right after
    `X = db.connect()` is a `Try` whose `finalbody` calls `X.close()`), or a
    `with contextlib.closing(...)` in its place. `_KNOWN_DIRECT_CONNECT_OFFENDERS` carries the one
    pre-existing offender found while building this guard, outside this slice's `touches:`."""

    def test_every_route_level_connect_closes_right_after(self):
        offenders = []
        checked = 0
        for path, tree in modulos_con_rutas():
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(_is_route_decorator(d) for d in node.decorator_list):
                    continue
                for stmts in _stmt_lists(node):
                    for stmts2, i, var_name in _connect_assigns(stmts):
                        checked += 1
                        if _closes_right_after(stmts2, i, var_name):
                            continue
                        if (path.name, node.name) in _KNOWN_DIRECT_CONNECT_OFFENDERS:
                            continue
                        offenders.append(f"{path.name}:{node.name}")
        self.assertGreater(checked, 0,
                            "no route function calls db.connect() directly -- guard checks nothing")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
