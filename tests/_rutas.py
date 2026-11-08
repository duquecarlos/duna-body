"""Shared route flattener for tests (BODY-WEB-ESTRUCTURA-1-M). Not a test module (no `test_`
prefix): tests/test_guardia_parches.py's guard asserts this is the only place under tests/ that
reads an app's or router's `.routes` list directly.

FastAPI 0.162 keeps `app.include_router(...)` as one lazy `_IncludedRouter` wrapper in
`app.routes` rather than flattening it eagerly -- a filter that only checks `isinstance(r,
APIRoute)` or `hasattr(r, "path")` over `app.routes` itself therefore misses every route an
`include_router()` call added (BODY-WEB-ESTRUCTURA-ROUTE-FLATTEN-AUDIT-1). `rutas_planas`
descends into one the same way every reader needs.

`modulos_con_rutas` (BODY-WEB-ESTRUCTURA-3) is the one place that reads the *source* of every
module that can hold a route: body/web.py plus every body/rutas/*.py, discovered by glob so a new
router module is read without editing this helper. A guard that parsed only body/web.py by path
(tests/test_conexiones_cerradas.py's RouteClosesItsConnectionTest, before this slice) checked zero
of the routes that had already moved into body/rutas/ -- a green test that tested nothing.
"""
from __future__ import annotations

import ast
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BODY_DIR = _REPO_ROOT / "body"

# Verbs that change a database's content (BODY-RENDIMIENTO-1's two statement-level guards:
# tests/test_get_sin_escritura.py and tests/test_rendimiento.py).
_WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP"}


def modulos_con_rutas() -> list[tuple[Path, ast.Module]]:
    """(path, parsed module) for body/web.py and every body/rutas/*.py file."""
    paths = [_BODY_DIR / "web.py"] + sorted((_BODY_DIR / "rutas").glob("*.py"))
    return [(p, ast.parse(p.read_text(encoding="utf-8"), filename=str(p))) for p in paths]


def rutas_planas(routes):
    """Yields every leaf route reachable from `routes`. A plain Route/APIRoute or a Mount is
    yielded as itself -- both carry their own `.path`, and a Mount's mounted app (e.g.
    StaticFiles) is never descended into, so the mount entry itself stays where today's callers
    expect it. An `_IncludedRouter` has no `.path` of its own; it is unwrapped through
    `.original_router.routes`. Anything else exposing a `.routes` list (a plain sub-router) is
    descended into the same way."""
    for r in routes:
        if hasattr(r, "path"):
            yield r
        elif hasattr(r, "original_router"):
            yield from rutas_planas(r.original_router.routes)
        elif hasattr(r, "routes"):
            yield from rutas_planas(r.routes)


@contextmanager
def contar_sql():
    """Context manager, one per measurement: wraps `sqlite3.connect` for its duration so EVERY
    connection the app opens -- every call reaches sqlite3.connect() only through body.db.connect
    -- is traced. Yields a dict with `connections` (how many were opened), `statements` (every SQL
    statement run on any of them) and `writes` (statements whose verb is in _WRITE_VERBS, plus a
    COMMIT that followed one -- a read-only transaction's COMMIT, if any, is never counted).

    Used by tests/test_get_sin_escritura.py (asserts `writes == 0` per GET route) and
    tests/test_rendimiento.py (asserts ceilings on `statements` and `connections` per route).
    Restores the real sqlite3.connect in `finally`, so a failure inside the `with` block never
    leaves every later sqlite3.connect call patched.
    """
    stats = {"connections": 0, "statements": 0, "writes": 0}
    real_connect = sqlite3.connect
    dirty: set[int] = set()

    def _trace(conn_id: int, sql: str):
        stats["statements"] += 1
        verb = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if verb in _WRITE_VERBS:
            stats["writes"] += 1
            dirty.add(conn_id)
        elif verb == "COMMIT" and conn_id in dirty:
            stats["writes"] += 1
            dirty.discard(conn_id)

    def _traced_connect(*a, **kw):
        conn = real_connect(*a, **kw)
        stats["connections"] += 1
        conn.set_trace_callback(lambda sql, _cid=id(conn): _trace(_cid, sql))
        return conn

    sqlite3.connect = _traced_connect
    try:
        yield stats
    finally:
        sqlite3.connect = real_connect
