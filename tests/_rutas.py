"""Shared route flattener for tests (BODY-WEB-ESTRUCTURA-1-M). Not a test module (no `test_`
prefix): tests/test_guardia_parches.py's guard asserts this is the only place under tests/ that
reads an app's or router's `.routes` list directly.

FastAPI 0.162 keeps `app.include_router(...)` as one lazy `_IncludedRouter` wrapper in
`app.routes` rather than flattening it eagerly -- a filter that only checks `isinstance(r,
APIRoute)` or `hasattr(r, "path")` over `app.routes` itself therefore misses every route an
`include_router()` call added (BODY-WEB-ESTRUCTURA-ROUTE-FLATTEN-AUDIT-1). `rutas_planas`
descends into one the same way every reader needs.
"""
from __future__ import annotations


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
