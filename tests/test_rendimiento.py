"""Cost ceiling per GET route (BODY-RENDIMIENTO-1, stage one of BODY-RENDIMIENTO-OBS-1's three
slices). Before this slice, `db.init(conn)` ran on every request -- the five guarded migrations,
`executescript(SCHEMA)` (48 `CREATE ... IF NOT EXISTS`), `_migrate`, a 48-row METRIC_DEFS upsert
and a commit -- on top of whatever a route itself needed, so a route's own cost was invisible
inside ~180 fixed statements. `db.ensure_ready` now runs that once per process per DB file
(body/db.py), so the statement count a route runs on a database already made ready is close to
its own real cost. This pins that cost with a ceiling per route, measured on this file's own fixed,
populated fixture, plus headroom -- the exact measured values and the margin are in this slice's
ledger entry (DECISIONS.md).

`/shifts` stays far above every other route (its own per-day DayData cost, not touched by this
slice) and keeps its own, much higher ceiling with a comment naming the slice that will bring it
down -- BODY-RENDIMIENTO-2.

Non-vacuous: the route set checked below must equal the app's whole GET route set, not a sample --
checked by comparing the two sets directly, not just their counts.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, web
from body.agente import conversacion as agente_conversacion
from tests._rutas import contar_sql, rutas_planas

client = TestClient(web.app, follow_redirects=False)

# route path -> statement ceiling. Measured on this module's own fixture (demo._seed(dias=30) +
# one assistant conversation) right after BODY-RENDIMIENTO-1's fix, with headroom so a small,
# legitimate change to a route does not make this test flaky. The measured value for each one, and
# the margin applied, are in BODY-RENDIMIENTO-1's ledger entry.
_CEILINGS = {
    "/": 75,
    "/ajustes/asistente": 16,
    "/ajustes/dispositivos": 9,
    "/api/vida/semana": 3,
    "/asistente/conversacion/{conversacion_id}": 17,
    "/ayuda": 15,
    "/day/{day}": 45,
    "/garmin": 9,
    "/goals": 40,
    "/goals/new": 48,
    "/goals/{goal_id}": 33,
    "/healthz": 3,
    "/history": 29,
    "/how/{day}": 21,
    "/login": 3,
    "/login/cuenta/callback": 3,
    "/login/cuenta/start": 3,
    "/privacidad": 16,
    "/privacidad/consentimiento": 9,
    "/privacidad/exportar": 43,
    "/settings": 24,
    # Stays high until BODY-RENDIMIENTO-2: /shifts builds a DayData-shaped row per day in its
    # window, a per-day cost this slice did not touch (BODY-RENDIMIENTO-OBS-1's own scope note).
    "/shifts": 400,
    "/shifts/como": 46,
    "/shifts/patron": 16,
    "/sw.js": 3,
    "/trends": 51,
}

# No route may open more than this many connections (BODY-RENDIMIENTO-1's own approval reason):
# this is the rule itself, not a measured-plus-margin figure.
_MAX_CONNECTIONS = 2


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "rendimiento.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)  # made ready BEFORE any route is measured
    return conn


def _seed(conn) -> dict:
    """Same populated fixture as tests/test_get_sin_escritura.py's sweep, so every parameterised
    GET route carries a real id -- demo.py's seed carries no real figure, by its own contract."""
    demo._seed(conn, dias=30, idioma="es")
    goal_id = conn.execute("SELECT id FROM goal WHERE person_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
    day = conn.execute(
        "SELECT day FROM daily_metric WHERE person_id = 1 ORDER BY day DESC LIMIT 1").fetchone()[0]
    conversacion_id = agente_conversacion.crear(conn, 1)
    conn.commit()
    return {"goal_id": goal_id, "day": day, "conversacion_id": conversacion_id}


def _fill(path: str, ids: dict) -> str:
    return path.format(**ids) if "{" in path else path


class RouteCostCeilingTest(unittest.TestCase):
    def test_every_get_route_stays_within_its_ceiling(self):
        conn = _reset_db(self)
        ids = _seed(conn)

        get_routes = [r for r in rutas_planas(web.app.routes) if "GET" in getattr(r, "methods", ())]
        self.assertEqual({r.path for r in get_routes}, set(_CEILINGS),
                          "the app's GET routes and this test's ceiling table have drifted apart")

        over_statements = []
        over_connections = []
        for route in get_routes:
            path = _fill(route.path, ids)
            with contar_sql() as stats:
                client.get(path)
            ceiling = _CEILINGS[route.path]
            if stats["statements"] > ceiling:
                over_statements.append((route.path, stats["statements"], ceiling))
            if stats["connections"] > _MAX_CONNECTIONS:
                over_connections.append((route.path, stats["connections"]))

        self.assertEqual(over_statements, [], f"route(s) over their statement ceiling: {over_statements}")
        self.assertEqual(over_connections, [],
                          f"route(s) opened more than {_MAX_CONNECTIONS} connections: {over_connections}")


if __name__ == "__main__":
    unittest.main()
