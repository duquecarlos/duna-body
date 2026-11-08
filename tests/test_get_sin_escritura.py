"""Every GET route is read-only (BODY-SEGURIDAD-3). BODY-SEGURIDAD-CENSO-1's item 3 said "GET is
read-only"; the orchestrator's own reading of main found two GETs that were not -- `/goals` and
`/goals/{goal_id}` both called `_goal_check_row`, which upserted a goal_check row through
`goals.weekly_check` the first time anyone opened a goal in a week. This sweeps every GET route
the app defines, with a populated database, and asserts none of them changed a single row.

BODY-RENDIMIENTO-1 widens the guard to the STATEMENT level: before this slice, `db.init(conn)` ran
on every request and executed real INSERT/UPSERT statements whose rows happened to already match
(so the content-dump comparison below saw no difference even though writes were happening) --
a write that leaves identical rows is invisible to `_dump`, by its own docstring. The database is
now made ready once, before the sweep starts (`db.ensure_ready` in `_reset_db`), and
`tests._rutas.contar_sql` traces every connection the app opens during each route call, so a write
statement is caught even when it changes nothing.
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
    db.ensure_ready(conn)  # made ready BEFORE the sweep, so first-touch setup is not counted below
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
    """A deterministic snapshot of every table's actual rows, in a fixed column order. Kept
    alongside the statement-level guard below, not in place of it: this alone would miss a write
    that leaves identical rows (the gap BODY-RENDIMIENTO-1's module docstring names)."""
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
        write_offenders = []
        for route in get_routes:
            path = _fill(route.path, ids)
            before = _dump(dump_conn)
            with contar_sql() as stats:
                client.get(path)
            after = _dump(dump_conn)
            swept += 1
            if before != after and route.path not in _ALLOWED_WRITE_PATHS:
                offenders.append(route.path)
            if stats["writes"] and route.path not in _ALLOWED_WRITE_PATHS:
                write_offenders.append((route.path, stats["writes"]))

        self.assertEqual(swept, len(get_routes))
        self.assertEqual(offenders, [], f"GET route(s) wrote to the database: {offenders}")
        self.assertEqual(write_offenders, [],
                          f"GET route(s) ran a write statement (even if it changed no row): {write_offenders}")


if __name__ == "__main__":
    unittest.main()
