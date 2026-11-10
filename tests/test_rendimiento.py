"""Cost ceiling per GET route (BODY-RENDIMIENTO-1, stage one of BODY-RENDIMIENTO-OBS-1's three
slices). Before this slice, `db.init(conn)` ran on every request -- the five guarded migrations,
`executescript(SCHEMA)` (48 `CREATE ... IF NOT EXISTS`), `_migrate`, a 48-row METRIC_DEFS upsert
and a commit -- on top of whatever a route itself needed, so a route's own cost was invisible
inside ~180 fixed statements. `db.ensure_ready` now runs that once per process per DB file
(body/db.py), so the statement count a route runs on a database already made ready is close to
its own real cost. This pins that cost with a ceiling per route, measured on this file's own fixed,
populated fixture, plus headroom -- the exact measured values and the margin are in this slice's
ledger entry (DECISIONS.md).

`/shifts` carried a much higher ceiling than every other route until BODY-RENDIMIENTO-2: the
screen's own per-day readiness and cost math queried the database fresh for every one of its 56
strip days and every night block, instead of reading its metric window once (`body/lector.py`'s
`Lector`, now threaded through `readiness.compute`, `baselines.baseline` and `turnos_costo`'s block
math). It now carries the same margin rule as every other route.

Non-vacuous: the route set checked below must equal the app's whole GET route set, not a sample --
checked by comparing the two sets directly, not just their counts.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, web
from body.agente import conversacion as agente_conversacion
from body.rutas import comun as web_comun
from body.rutas import tendencias as web_tendencias
from tests._rutas import contar_sql, rutas_planas

client = TestClient(web.app, follow_redirects=False)

# route path -> statement ceiling. Measured on this module's own fixture (demo._seed(dias=30) +
# one assistant conversation) right after BODY-RENDIMIENTO-1's fix, with headroom so a small,
# legitimate change to a route does not make this test flaky. The measured value for each one, and
# the margin applied, are in BODY-RENDIMIENTO-1's ledger entry -- except /shifts and /shifts/como,
# remeasured after BODY-RENDIMIENTO-2's reader fix, /goals and /experiments/{experiment_id},
# remeasured after BODY-EXPERIMENTOS-PANTALLA-1 added the fixture's own experiment and its
# Metas-section/detail-page reads, and /goals/new and /trends, remeasured after BODY-AGUJETAS-1-B
# batched their per-metric-key reads (one query for the whole catalogue instead of one per key) --
# each in that slice's own ledger entry.
_CEILINGS = {
    "/": 75,
    "/ajustes/asistente": 16,
    "/ajustes/dispositivos": 9,
    "/api/vida/semana": 3,
    "/asistente/conversacion/{conversacion_id}": 17,
    "/ayuda": 15,
    "/day/{day}": 45,
    "/experiments/{experiment_id}": 27,
    "/garmin": 9,
    "/goals": 50,
    "/goals/new": 20,
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
    "/shifts": 50,
    "/shifts/como": 25,
    "/shifts/patron": 16,
    "/sw.js": 3,
    "/trends": 19,
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
    experiment_id = db.crear_experimento(
        conn, 1, "ZZ-EXPERIMENTO", ["resting_hr"], {"tipo": "dia"},
        {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False}, fecha_inicio=day)
    db.registrar_dia_experimento(conn, experiment_id, 1, day, True)
    conn.commit()
    return {"goal_id": goal_id, "day": day, "conversacion_id": conversacion_id, "experiment_id": experiment_id}


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


def _add_metric_def(conn, key: str) -> None:
    conn.execute(
        "INSERT INTO metric_def (key, label, unit, better, kind, origen) "
        "VALUES (?, 'ZZ', 'u', 'higher', 'measured', 'self')", (key,))
    conn.commit()


class RouteCostIndependentOfCatalogSizeTest(unittest.TestCase):
    """BODY-AGUJETAS-1-B: the fix for GATE_RED was to batch /goals/new's and /trends' per-metric-key
    reads into one query each (body/baselines.py's baselines(), body/profile.py's metric_profiles()
    and windows()), not to widen their ceiling -- so a metric_def row added to the catalogue (as
    BODY-MEDIDA-PESO-1 and BODY-AGUJETAS-1 each did) must cost the route NO extra statement."""

    def test_goal_new_form_unaffected_by_an_extra_self_measured_key(self):
        conn = _reset_db(self)
        _seed(conn)
        with contar_sql() as before:
            r1 = client.get("/goals/new")
        _add_metric_def(conn, "zz_extra_self")
        with contar_sql() as after:
            r2 = client.get("/goals/new")
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(before["statements"], after["statements"])

    def test_trends_unaffected_by_an_extra_self_measured_key(self):
        conn = _reset_db(self)
        _seed(conn)
        with contar_sql() as before:
            r1 = client.get("/trends")
        _add_metric_def(conn, "zz_extra_self")
        nuevos_groups = [g if g[0] != "self" else (g[0], [*g[1], "zz_extra_self"]) for g in web_comun.TREND_GROUPS]
        with patch.object(web_comun, "TREND_GROUPS", nuevos_groups), \
             patch.object(web_tendencias, "TODAS", (*web_tendencias.TODAS, "zz_extra_self")):
            with contar_sql() as after:
                r2 = client.get("/trends")
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(before["statements"], after["statements"])


if __name__ == "__main__":
    unittest.main()
