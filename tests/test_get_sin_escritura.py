"""Every GET route is read-only (BODY-SEGURIDAD-3). BODY-SEGURIDAD-CENSO-1's item 3 said "GET is
read-only"; the orchestrator's own reading of main found two GETs that were not -- `/goals` and
`/goals/{goal_id}` both called `_goal_check_row`, which upserted a goal_check row through
`goals.weekly_check` the first time anyone opened a goal in a week. This sweeps every GET route
the app defines, with a populated database, and asserts none of them changed a single row.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, web
from body.agente import conversacion as agente_conversacion
from tests._rutas import rutas_planas

client = TestClient(web.app, follow_redirects=False)

# GET routes that legitimately write. The pre-auth sign-in start stores the PKCE/state row
# (cuenta.start_login -> inicio_sesion) before any session cookie exists to carry a POST --
# BODY-SEGURIDAD-CENSO-1 named it. `/login` and `/login/cuenta/start` both decorate the same
# function (body/rutas/sesion.py's login_start). In LOCAL mode -- this sweep's mode, like every
# other web test in this repository -- cuenta.hosted() is False and both paths 404 before writing
# anything, so this entry is inert here; it is kept so the guard still holds if this test is ever
# run with a hosted environment.
_ALLOWED_WRITE_PATHS = {"/login", "/login/cuenta/start"}


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "sweep.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _seed(conn) -> dict:
    """A database wide enough to carry a real id into every parameterised GET route:
    body/demo.py's own seed (two people, three metric goals, a shared household milestone,
    habits, 30 days of metrics and activities, confirmed and proposed shift batches) plus one
    assistant conversation -- demo.py's seed carries no real figure, by its own contract."""
    demo._seed(conn, dias=30, idioma="es")
    goal_id = conn.execute("SELECT id FROM goal WHERE person_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
    day = conn.execute(
        "SELECT day FROM daily_metric WHERE person_id = 1 ORDER BY day DESC LIMIT 1").fetchone()[0]
    conversacion_id = agente_conversacion.crear(conn, 1)
    conn.commit()
    return {"goal_id": goal_id, "day": day, "conversacion_id": conversacion_id}


def _dump(conn) -> dict:
    """A deterministic snapshot of every table's actual rows, in a fixed column order -- content,
    not statement activity, so db.init()'s own unconditional upsert of metric_def/person (run by
    every request's identity.current_person_id and comun._ctx, changing nothing once the content
    already matches) never shows up as a difference. A sqlite3 authorizer would instead fire on
    that upsert's own INSERT statement every single request, whether or not it changes a row."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    out = {}
    for name in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        if not cols:
            continue
        order = ", ".join(f'"{c}"' for c in cols)
        out[name] = tuple(conn.execute(f"SELECT {order} FROM {name} ORDER BY {order}").fetchall())
    return out


def _fill(path: str, ids: dict) -> str:
    return path.format(**ids) if "{" in path else path


class GetRoutesWriteNothingTest(unittest.TestCase):
    def test_every_get_route_leaves_the_database_unchanged(self):
        conn = _reset_db(self)
        ids = _seed(conn)
        dump_conn = db.connect()
        self.addCleanup(dump_conn.close)

        get_routes = [r for r in rutas_planas(web.app.routes) if "GET" in getattr(r, "methods", ())]
        self.assertGreater(len(get_routes), 20)

        swept = 0
        offenders = []
        for route in get_routes:
            path = _fill(route.path, ids)
            before = _dump(dump_conn)
            client.get(path)
            after = _dump(dump_conn)
            swept += 1
            if before != after and route.path not in _ALLOWED_WRITE_PATHS:
                offenders.append(route.path)

        self.assertEqual(swept, len(get_routes))
        self.assertEqual(offenders, [], f"GET route(s) wrote to the database: {offenders}")


if __name__ == "__main__":
    unittest.main()
