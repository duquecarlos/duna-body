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
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, demo, dispositivo, fechas, garmin_cuenta, goals, identity, mcp, practicas, web
from body.agente import conversacion as agente_conversacion
from body.rutas import comun as web_comun
from body.rutas import tendencias as web_tendencias
from tests._rutas import contar_sql, rutas_planas
from tests.test_cuenta_rp import FakeIssuer

client = TestClient(web.app, follow_redirects=False)

# route path -> statement ceiling. Measured on this module's own fixture (demo._seed(dias=30) +
# one assistant conversation) right after BODY-RENDIMIENTO-1's fix, with headroom so a small,
# legitimate change to a route does not make this test flaky. The measured value for each one, and
# the margin applied, are in BODY-RENDIMIENTO-1's ledger entry -- except /shifts and /shifts/como,
# remeasured after BODY-RENDIMIENTO-2's reader fix and again after BODY-TENDENCIAS-LEYENDA-
# PATRON-1-M folded body/lector.py's Lector down to one combined SELECT regardless of metric count
# (32 and 15 measured here, ceil(measured * 1.3) = 42 and 20), /goals and
# /experiments/{experiment_id}, remeasured after BODY-EXPERIMENTOS-PANTALLA-1 added the fixture's
# own experiment and its Metas-section/detail-page reads, and /goals/new and /trends, remeasured
# after BODY-AGUJETAS-1-B batched their per-metric-key reads (one query for the whole catalogue
# instead of one per key), and /day/{day} and /privacidad/exportar, remeasured after BODY-COMIDA-1
# added one meal-list read and one meal-export read respectively (ceil(measured * 1.3), the same
# ~30% margin rule), and /, remeasured after BODY-NUTRIENTE-OBJETIVO-1 added the "Metas de
# nutrientes" card's own read (one vigentes-targets query, plus -- only once a per_kg target is
# live -- a weight lookup and two batched totals/counts queries) --
# each in that slice's own ledger entry. BODY-HISTORIA-VIDA-1 ruling 8: /privacidad/exportar's
# ceiling, remeasured after this slice's own practica export query (one extra SELECT) -- 59
# measured, ceil(59 * 1.3) = 77.
# BODY-TENDENCIAS-RACHA-GRAFICO-1-M2: /trends' own ceiling, raised for the strength-session study
# card (BODY-TENDENCIAS-RACHA-GRAFICO-1-M's 3 statements, a fixed cost independent of history
# length -- tests/test_tendencias_racha.py's own guard). Measured 18 statements without the card
# present (this file's own fixture, which seeds no strength session) and 21 with it,
# ceil(21 * 1.3) = 28.
# BODY-PLAN-PANTALLA-1: /goals' own ceiling, raised after the demo fixture's horizon and its two
# child goals gave person 1 three more active goals to render (and /goals now nests a horizon's
# children under its own card). Measured 57 statements, ceil(57 * 1.3) = 75. /goals/{goal_id}'s own
# ceiling (33) did not move: the fixture's picked goal_id is still a plain metric goal (the sleep
# goal, seeded before the horizon), so the plan section's own extra queries never run for it --
# ruling 5's own bounded-cost proof for a horizon's detail view lives in this file's
# PlanPantallaCostIndependentOfSpanTest instead of here.
# BODY-HOY-ESCRITORIO-1 ruling 4: / and /day/{day}, remeasured after this file's own `_seed()`
# started logging one training session for today (above) -- the fixture change both routes share.
# / measured 85 statements (84 before, the new bounded workout_set read's own +1), ceil(85 * 1.3) =
# 111. /day/{day} measured 62 (60 before -- its existing per-activity zonas_limitadas read in
# body/db.py's load_day now also covers the new activity, inside the demo's own open lesion
# window), ceil(62 * 1.3) = 81.
# BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 2: /'s own ceiling, raised for the Garmin sync-status card
# (body/rutas/hoy.py's _garmin_sync_ctx), one unconditional garmin_cuenta.estado_conexion read.
# Measured 86 statements (85 before), ceil(86 * 1.3) = 112.
# BODY-ENTRENO-VISTA-SESION-1: /day/{day}/entreno/{source_id}'s own ceiling, new with this slice.
# Measured 12 statements against this file's own fixture (the manual "Fuerza" session _seed() logs
# for `day`, two corrected sets, no intraday series), ceil(12 * 1.3) = 16.
# BODY-PLAN-HOY-1: /'s own ceiling, raised for the "Tu plan esta semana" card (body/rutas/hoy.py's
# _plan_semana_ctx) -- one bounded horizonte.horizontes() listing plus up to PLANES_HOY_MAX
# horizonte.resumen() calls, ruling 3's own bound. A person with no plan pays exactly one extra
# statement, measured by calling _plan_semana_ctx directly against a minimal fixture (person 1, no
# horizon, no household): 1 statement, horizontes()'s own single query -- it folds the household
# scope in as a subquery rather than taking goals.goal_scope_sql's separate household.household_of
# call, which would have cost a second statement for nothing. With this file's own _seed() fixture
# (one active plan, the demo's own horizon), / measured 119 statements (118 before this file's own
# extra manual-session fixture addition above), ceil(119 * 1.3) = 155.
# BODY-HISTORIA-VIDA-2: /history's own ceiling, raised for the "Tus prácticas" section --
# practicas.listar plus practicas.estado() for each active practica (demo's own fixture seeds two)
# plus practicas.sugerencias(). Measured 44 statements (29 before), ceil(44 * 1.3) = 58.
# /history/practica/{practica_id}'s own ceiling, new with this slice: one practicas.estado() call
# against this file's own fixture (one of the demo's two seeded practicas, no linked habit yet).
# Measured 17 statements, ceil(17 * 1.3) = 23.
_CEILINGS = {
    "/": 155,
    "/ajustes/asistente": 16,
    "/ajustes/dispositivos": 9,
    "/api/asistente/mcp/herramientas": 3,
    # BODY-HOGAR-ESCRIBE-2: this sweep calls every GET route with no Authorization header (same as
    # every other device-key route above) -- `person_from_device` 404s in this file's LOCAL-mode
    # fixture before touching the database, measured 0 statements; 3 matches its siblings' own margin.
    "/api/hogar/herramientas": 3,
    "/api/hogar/manana": 3,
    "/api/hogar/serie": 3,
    "/api/hogar/turnos": 3,
    "/api/vida/semana": 3,
    "/asistente/conversacion/{conversacion_id}": 17,
    "/ayuda": 15,
    "/day/{day}": 81,
    "/day/{day}/entreno/{source_id}": 16,  # BODY-ENTRENO-VISTA-SESION-1: measured 12, ceil(12 * 1.3) = 16
    "/experiments/{experiment_id}": 27,
    "/garmin": 9,
    "/garmin/estado-vivo": 6,  # BODY-SYNC-VIVO-2: measured 4 (local mode's early 404, same path as
                               # /garmin's own), ceil(4 * 1.3) = 6
    "/goals": 75,
    "/goals/new": 20,
    "/goals/{goal_id}": 33,
    "/healthz": 3,
    "/history": 58,
    "/history/practica/{practica_id}": 23,  # BODY-HISTORIA-VIDA-2: measured 17, ceil(17 * 1.3) = 23
    "/how/{day}": 21,
    "/login": 3,
    "/login/cuenta/callback": 3,
    "/login/cuenta/start": 3,
    "/privacidad": 16,
    "/privacidad/consentimiento": 9,
    "/privacidad/exportar": 77,
    "/settings": 24,
    "/shifts": 42,
    "/shifts/como": 20,
    "/shifts/patron": 16,
    "/sw.js": 3,
    "/trends": 28,
}

# No route may open more than this many connections (BODY-RENDIMIENTO-1's own approval reason):
# this is the rule itself, not a measured-plus-margin figure.
_MAX_CONNECTIONS = 2

# BODY-RENDIMIENTO-4: the main sweep above calls every GET route with no Authorization header and
# no hosted env, so the 13 routes that gate on a device key or on `cuenta.hosted()` all answer
# 401/403/404 before touching more than `identity.current_person_id`'s own local-mode lookup --
# their ceiling in `_CEILINGS` measures that early refusal, never their own real cost. This table
# gives each of those 13 routes a SECOND ceiling, measured on its real path: an authorised device
# key (the same `dispositivo.crear` fixture every device-key route's own tests use) for the seven
# device-key routes, hosted mode plus `identity.current_person_id` overridden to person 1 (the same
# mechanism tests/test_garmin_conectar_vivo.py's and tests/test_dispositivo.py's own tests use) for
# the four routes gated only by sign-in, and a real OIDC round trip through tests/test_cuenta_rp.py's
# own `FakeIssuer` for the three sign-in routes, which take no `identity.current_person_id`
# dependency at all. `/login` and `/login/cuenta/start` never answer 200 by design (a successful
# visit redirects to the issuer, 303); `/login/cuenta/callback` never does either (a successful
# sign-in redirects to `return_to` or "/", 303) -- their real-path status is 303, not 200, and that
# is their own documented correct behaviour (tests/test_estados_vacios_hosted.py's own
# `_NO_IDENTITY_303`/no-200 set for these exact three routes). Every other entry's real path
# answers 200. Ceiling = ceil(measured * 1.3), the same margin rule as `_CEILINGS`; connections stay
# under `_MAX_CONNECTIONS`. Measured figures are each entry's own comment; this slice's own ledger
# entry carries the full table.
_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8771",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only", garmin_cuenta.KEY_ENV: "11" * 32}

_CEILINGS_REAL_PATH = {
    "/ajustes/dispositivos": 10,  # measured 7, ceil(7 * 1.3) = 10
    "/api/asistente/mcp/herramientas": 24,  # measured 18, ceil(18 * 1.3) = 24
    "/api/hogar/herramientas": 23,  # measured 17, ceil(17 * 1.3) = 23
    "/api/hogar/manana": 36,  # measured 27, ceil(27 * 1.3) = 36
    "/api/hogar/serie": 17,  # measured 13, ceil(13 * 1.3) = 17
    "/api/hogar/turnos": 16,  # measured 12, ceil(12 * 1.3) = 16
    "/api/vida/semana": 62,  # measured 47, ceil(47 * 1.3) = 62
    "/garmin": 16,  # measured 12, ceil(12 * 1.3) = 16 -- the hosted+connected path
                     # BODY-APERTURA-AUTOMATICA-1's own report named but never measured here
    "/garmin/estado-vivo": 11,  # measured 8, ceil(8 * 1.3) = 11
    "/login": 10,  # measured 7, ceil(7 * 1.3) = 10 -- real path answers 303, not 200 (see above)
    "/login/cuenta/callback": 16,  # measured 12, ceil(12 * 1.3) = 16 -- real path answers 303
    "/login/cuenta/start": 10,  # measured 7, ceil(7 * 1.3) = 10 -- real path answers 303
    "/privacidad/consentimiento": 7,  # measured 5, ceil(5 * 1.3) = 7
}

# Of the 13 routes above, these three never answer 200 on their real path -- a successful visit is
# a redirect (/login, /login/cuenta/start) or a successful sign-in redirect (/login/cuenta/callback).
_REAL_PATH_NO_200 = {"/login", "/login/cuenta/start", "/login/cuenta/callback"}


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
    GET route carries a real id -- demo.py's seed carries no real figure, by its own contract.
    BODY-HOY-ESCRITORIO-1 ruling 4: plus one logged (manual) training session for TODAY, so the
    route sweep below measures "/"'s own bounded workout_set read (body/rutas/hoy.py's
    _entrenado_hoy_categorias) against its real worst case -- an id list that is not empty --
    rather than the demo's own random activities, which rarely land a categorised set on today."""
    demo._seed(conn, dias=30, idioma="es")
    goal_id = conn.execute("SELECT id FROM goal WHERE person_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
    day = conn.execute(
        "SELECT day FROM daily_metric WHERE person_id = 1 ORDER BY day DESC LIMIT 1").fetchone()[0]
    conversacion_id = agente_conversacion.crear(conn, 1)
    experiment_id = db.crear_experimento(
        conn, 1, "ZZ-EXPERIMENTO", ["resting_hr"], {"tipo": "dia"},
        {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False}, fecha_inicio=day)
    db.registrar_dia_experimento(conn, experiment_id, 1, day, True)
    source_id = db.create_manual_session(conn, 1, day, None, "Fuerza")
    db.correct_exercise_set(conn, 1, "manual", source_id, 1, category="espalda", exercise="remo con barra")
    db.correct_exercise_set(conn, 1, "manual", source_id, 2, category="brazo", exercise="curl de biceps")
    # BODY-HISTORIA-VIDA-2: demo._seed() already seeds two practicas (body/demo.py's own
    # _seed_practicas, BODY-HISTORIA-VIDA-1 ruling 8) -- the first one picked here for
    # /history/practica/{practica_id}'s own real path.
    practica_id = conn.execute(
        "SELECT id FROM practica WHERE person_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
    conn.commit()
    return {"goal_id": goal_id, "day": day, "conversacion_id": conversacion_id, "experiment_id": experiment_id,
            "source_id": source_id, "practica_id": practica_id}


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


def _authorize_params(resp) -> dict:
    qp = parse_qs(urlparse(resp.headers["location"]).query)
    return {k: v[0] for k, v in qp.items()}


class RouteCostRealPathTest(unittest.TestCase):
    """BODY-RENDIMIENTO-4: a second ceiling, on each of the 13 routes `_CEILINGS` above can only
    measure as an early 404, on its own real path. Ruling 2's drift guard lives in
    `test_the_main_sweeps_404_set_matches_this_tables_keys` below: it is what makes a new
    device-key or hosted-only GET route fail this file until it gets a real-path ceiling too."""

    def test_the_main_sweeps_404_set_matches_this_tables_keys(self):
        conn = _reset_db(self)
        ids = _seed(conn)
        get_routes = [r for r in rutas_planas(web.app.routes) if "GET" in getattr(r, "methods", ())]
        non_200 = {route.path for route in get_routes
                   if client.get(_fill(route.path, ids)).status_code in (401, 403, 404)}
        self.assertEqual(non_200, set(_CEILINGS_REAL_PATH),
                          "the main sweep's 401/403/404 route set and this table's keys have drifted apart")

    def test_device_key_routes_on_their_real_path(self):
        conn = _reset_db(self)
        ids = _seed(conn)
        patcher = patch.dict(os.environ, _HOSTED_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        hclient = TestClient(web.app, follow_redirects=False)

        key_hogar_leer = dispositivo.crear(conn, 1, "PC hogar leer", {"hogar:leer"})
        key_hogar_escribir = dispositivo.crear(conn, 1, "PC hogar escribir", {"hogar:escribir"})
        key_vida = dispositivo.crear(conn, 1, "PC vida", {"vida:leer"})
        key_mcp = dispositivo.crear(conn, 1, "PC mcp", {"asistente:mcp"})
        conn.commit()
        day = ids["day"]

        checks = [
            ("/api/hogar/manana", f"/api/hogar/manana?dia={day}", key_hogar_leer),
            ("/api/hogar/serie", f"/api/hogar/serie?metric_key=resting_hr&desde={day}&hasta={day}", key_hogar_leer),
            ("/api/hogar/turnos", f"/api/hogar/turnos?desde={day}&hasta={day}", key_hogar_leer),
            ("/api/hogar/herramientas", "/api/hogar/herramientas", key_hogar_escribir),
            ("/api/vida/semana", "/api/vida/semana", key_vida),
            ("/api/asistente/mcp/herramientas", "/api/asistente/mcp/herramientas", key_mcp),
        ]
        over_statements = []
        over_connections = []
        for table_key, full_path, key in checks:
            with contar_sql() as stats:
                r = hclient.get(full_path, headers={"Authorization": f"Bearer {key}"})
            self.assertEqual(r.status_code, 200, f"{table_key}'s real path did not answer 200")
            ceiling = _CEILINGS_REAL_PATH[table_key]
            if stats["statements"] > ceiling:
                over_statements.append((table_key, stats["statements"], ceiling))
            if stats["connections"] > _MAX_CONNECTIONS:
                over_connections.append((table_key, stats["connections"]))
        self.assertEqual(over_statements, [], f"route(s) over their real-path ceiling: {over_statements}")
        self.assertEqual(over_connections, [],
                          f"route(s) opened more than {_MAX_CONNECTIONS} connections: {over_connections}")

    def test_hosted_only_routes_on_their_real_path(self):
        conn = _reset_db(self)
        _seed(conn)
        patcher = patch.dict(os.environ, _HOSTED_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        hclient = TestClient(web.app, follow_redirects=False)

        garmin_cuenta.guardar(conn, 1, "tok-demo")
        garmin_cuenta.registrar_error(conn, 1, "NotSignedIn")
        conn.commit()

        over_statements = []
        over_connections = []
        for path in ("/garmin", "/garmin/estado-vivo", "/ajustes/dispositivos", "/privacidad/consentimiento"):
            with contar_sql() as stats:
                r = hclient.get(path)
            self.assertEqual(r.status_code, 200, f"{path}'s real path did not answer 200")
            ceiling = _CEILINGS_REAL_PATH[path]
            if stats["statements"] > ceiling:
                over_statements.append((path, stats["statements"], ceiling))
            if stats["connections"] > _MAX_CONNECTIONS:
                over_connections.append((path, stats["connections"]))
        self.assertEqual(over_statements, [], f"route(s) over their real-path ceiling: {over_statements}")
        self.assertEqual(over_connections, [],
                          f"route(s) opened more than {_MAX_CONNECTIONS} connections: {over_connections}")

    def test_sign_in_routes_on_their_real_path(self):
        """`/login` and `/login/cuenta/start` take no `identity.current_person_id` dependency at
        all (body/rutas/sesion.py) -- a dependency override cannot reach their real cost, so this
        is a genuine OIDC round trip through tests/test_cuenta_rp.py's own `FakeIssuer`, the same
        mechanism its own CuentaRpTestCase builds on. Person 1 (this file's own fixture) is linked
        to the signing-in `sub` beforehand, so the callback's real path is a RETURNING person
        signing back in -- the common case, and the one that actually exercises this file's own
        seeded data -- never a brand-new sign-up, which would cost an extra person INSERT no
        existing user ever pays."""
        conn = _reset_db(self)
        _seed(conn)
        patcher = patch.dict(os.environ, _HOSTED_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        hclient = TestClient(web.app, follow_redirects=False)

        issuer = FakeIssuer()
        patcher_get = patch.object(cuenta, "_get_json", issuer._get_json)
        patcher_post = patch.object(cuenta, "_post_form", issuer._post_form)
        patcher_get.start()
        self.addCleanup(patcher_get.stop)
        patcher_post.start()
        self.addCleanup(patcher_post.stop)
        cuenta._reset_caches_for_tests()
        self.addCleanup(cuenta._reset_caches_for_tests)

        sub = "zz-returning-sub-1"
        conn.execute("UPDATE person SET cuenta_sub = ? WHERE id = 1", (sub,))
        conn.commit()

        over_statements = []
        over_connections = []

        def _check(table_key, stats, status):
            self.assertEqual(status, 303, f"{table_key}'s real path did not answer 303")
            ceiling = _CEILINGS_REAL_PATH[table_key]
            if stats["statements"] > ceiling:
                over_statements.append((table_key, stats["statements"], ceiling))
            if stats["connections"] > _MAX_CONNECTIONS:
                over_connections.append((table_key, stats["connections"]))

        with contar_sql() as stats_login:
            r_login = hclient.get("/login")
        _check("/login", stats_login, r_login.status_code)

        auth = _authorize_params(r_login)
        issuer.next_id_token = issuer.id_token(nonce=auth["nonce"], sub=sub)
        with contar_sql() as stats_callback:
            r_callback = hclient.get("/login/cuenta/callback", params={"code": "c-1", "state": auth["state"]})
        _check("/login/cuenta/callback", stats_callback, r_callback.status_code)

        with contar_sql() as stats_start:
            r_start = hclient.get("/login/cuenta/start")
        _check("/login/cuenta/start", stats_start, r_start.status_code)

        self.assertEqual(over_statements, [], f"route(s) over their real-path ceiling: {over_statements}")
        self.assertEqual(over_connections, [],
                          f"route(s) opened more than {_MAX_CONNECTIONS} connections: {over_connections}")


class HogarMananaCostIndependentOfTargetCountTest(unittest.TestCase):
    """BODY-RENDIMIENTO-4 ruling 3: tests/test_hogar_feed.py's own
    `MetasTest.test_cost_does_not_grow_with_the_number_of_targets` proves this at the
    `hogar_feed.manana` function level, never through the HTTP route -- this is that same proof,
    through GET /api/hogar/manana itself."""

    def test_one_target_vs_three_same_statement_count_through_the_route(self):
        conn = _reset_db(self)
        ids = _seed(conn)
        patcher = patch.dict(os.environ, _HOSTED_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        hclient = TestClient(web.app, follow_redirects=False)
        key = dispositivo.crear(conn, 1, "PC", {"hogar:leer"})
        day = ids["day"]
        conn.execute(
            "INSERT INTO nutrient_target (person_id, metric_key, start_day, kind, amount, sentido, "
            "created_at) VALUES (1, 'protein_g', '2026-01-01', 'absolute', 130.0, 'al_menos', "
            "'2026-01-01T00:00:00')")
        conn.commit()
        with contar_sql() as uno:
            r1 = hclient.get(f"/api/hogar/manana?dia={day}", headers={"Authorization": f"Bearer {key}"})
        for metric_key in ("caffeine_mg", "water_ml"):
            conn.execute(
                "INSERT INTO nutrient_target (person_id, metric_key, start_day, kind, amount, sentido, "
                "created_at) VALUES (1, ?, '2026-01-01', 'absolute', 100.0, 'al_menos', "
                "'2026-01-01T00:00:00')", (metric_key,))
        conn.commit()
        with contar_sql() as tres:
            r2 = hclient.get(f"/api/hogar/manana?dia={day}", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(uno["statements"], tres["statements"],
                          f"one nutrient target vs three cost different statement counts through the "
                          f"route: {uno['statements']} vs {tres['statements']}")


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


def _horizonte_milestone(conn, person_id: int, start_day: str, deadline: str, titulo: str) -> int:
    return goals.propose(conn, person_id, titulo, start_day, kind="milestone", deadline=deadline)


class PlanPantallaCostIndependentOfSpanTest(unittest.TestCase):
    """BODY-PLAN-PANTALLA-1 ruling 5: /goals/{goal_id}'s own cost for a horizon, viewing its
    current week, stays the same whether the horizon spans 1 week or 12, and whether that week
    carries 1 weekly target line or 8 -- horizonte.semana()'s own queries are scoped to the one
    week being viewed, never to the horizon's whole span or every line it has ever carried."""

    HOY = date(2026, 11, 5)
    MONDAY = fechas.lunes_de(HOY)

    def test_one_week_vs_twelve_weeks_same_statement_count(self):
        conn = _reset_db(self)
        gid_short = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=6)).isoformat(), "ZZ horizonte corto")
        db.crear_meta_semanal(conn, 1, gid_short, self.MONDAY.isoformat(), "sesiones_fuerza", "al_menos", 3.0)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as corto:
                r1 = client.get(f"/goals/{gid_short}")

        gid_long = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(weeks=11, days=6)).isoformat(),
            "ZZ horizonte largo")
        semana = self.MONDAY
        for _ in range(12):
            db.crear_meta_semanal(conn, 1, gid_long, semana.isoformat(), "sesiones_fuerza", "al_menos", 3.0)
            semana += timedelta(days=7)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as largo:
                r2 = client.get(f"/goals/{gid_long}")

        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(
            corto["statements"], largo["statements"],
            f"a 12-week horizon cost more statements than a 1-week one viewing the same current "
            f"week: {corto['statements']} vs {largo['statements']}")

    def test_one_line_vs_eight_lines_same_statement_count(self):
        conn = _reset_db(self)
        gid_one = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=6)).isoformat(), "ZZ una linea")
        db.crear_meta_semanal(conn, 1, gid_one, self.MONDAY.isoformat(), "sesiones_fuerza", "al_menos", 3.0)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as una:
                r1 = client.get(f"/goals/{gid_one}")

        gid_eight = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=6)).isoformat(), "ZZ ocho lineas")
        medidas = ["sueno_minutos", "hora_acostarse", "hora_levantarse", "sesiones_fuerza",
                   "sesiones_cardio", "minutos_intensidad", "resting_hr", "steps"]
        for medida in medidas:
            db.crear_meta_semanal(conn, 1, gid_eight, self.MONDAY.isoformat(), medida, "al_menos", 1.0)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as ocho:
                r2 = client.get(f"/goals/{gid_eight}")

        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(
            una["statements"], ocho["statements"],
            f"8 weekly target lines cost more statements than 1 in the same week: "
            f"{una['statements']} vs {ocho['statements']}")


class PlanHoyCostIndependentOfSpanTest(unittest.TestCase):
    """BODY-PLAN-HOY-1 ruling 3: "/"'s own "Tu plan esta semana" card costs the same number of
    statements whether the active plan's current week carries 1 weekly target line or 8, and
    whether the horizon spans 1 week or 12 -- horizonte.resumen's own semana/checkpoints scoping
    (BODY-PLAN-MODULO-1) stays bounded regardless, and this card never recomputes anything beyond
    its own PLANES_HOY_MAX resumen calls."""

    HOY = date(2026, 11, 5)
    MONDAY = fechas.lunes_de(HOY)

    def _minimal_fixture(self):
        conn = _reset_db(self)
        db.set_metric(conn, self.HOY.isoformat(), "sleep_minutes", 400, "garmin", 1)
        conn.commit()
        return conn

    def test_one_week_vs_twelve_weeks_same_statement_count(self):
        conn = self._minimal_fixture()
        gid_short = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=6)).isoformat(), "ZZ plan corto")
        db.crear_meta_semanal(conn, 1, gid_short, self.MONDAY.isoformat(), "sesiones_fuerza", "al_menos", 3.0)
        goals.activate(conn, gid_short)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as corto:
                r1 = client.get("/")

        conn2 = self._minimal_fixture()
        gid_long = _horizonte_milestone(
            conn2, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(weeks=11, days=6)).isoformat(),
            "ZZ plan largo")
        semana = self.MONDAY
        for _ in range(12):
            db.crear_meta_semanal(conn2, 1, gid_long, semana.isoformat(), "sesiones_fuerza", "al_menos", 3.0)
            semana += timedelta(days=7)
        goals.activate(conn2, gid_long)
        conn2.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as largo:
                r2 = client.get("/")

        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(
            corto["statements"], largo["statements"],
            f"a 12-week horizon cost more statements than a 1-week one on \"/\": "
            f"{corto['statements']} vs {largo['statements']}")

    def test_one_line_vs_eight_lines_same_statement_count(self):
        conn = self._minimal_fixture()
        gid_one = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=6)).isoformat(), "ZZ una linea")
        db.crear_meta_semanal(conn, 1, gid_one, self.MONDAY.isoformat(), "sesiones_fuerza", "al_menos", 3.0)
        goals.activate(conn, gid_one)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as una:
                r1 = client.get("/")

        conn2 = self._minimal_fixture()
        gid_eight = _horizonte_milestone(
            conn2, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=6)).isoformat(), "ZZ ocho lineas")
        medidas = ["sueno_minutos", "hora_acostarse", "hora_levantarse", "sesiones_fuerza",
                   "sesiones_cardio", "minutos_intensidad", "resting_hr", "steps"]
        for medida in medidas:
            db.crear_meta_semanal(conn2, 1, gid_eight, self.MONDAY.isoformat(), medida, "al_menos", 1.0)
        goals.activate(conn2, gid_eight)
        conn2.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as ocho:
                r2 = client.get("/")

        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(
            una["statements"], ocho["statements"],
            f"8 weekly target lines cost more statements than 1 on \"/\": "
            f"{una['statements']} vs {ocho['statements']}")


class PracticasDiasEstadoCostTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-1 ruling 3: `practicas.dias_estado`'s own cost is ONE SELECT per source
    family for the whole range (the `entreno_racha.sesiones_fuerza` shape) -- never one per day or
    per week, so it costs the SAME whether the range spans 30 days or 300."""

    def _contar(self, conn, llamada) -> int:
        """`contar_sql` (tests/_rutas.py) only traces a connection opened AFTER it starts -- this
        file's own fixture connection is already open, so the trace callback is applied directly
        to it, the same way tests/test_hogar_feed.py's `_contar_statements` does."""
        contador = {"n": 0}
        conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + 1))
        try:
            llamada()
        finally:
            conn.set_trace_callback(None)
        return contador["n"]

    def test_cost_independent_of_range_length(self):
        conn = _reset_db(self)
        base = date(2026, 1, 5)
        for i in range(0, 300, 3):
            conn.execute(
                "INSERT INTO activity (person_id, source_id, day, type, duration_s, source) "
                "VALUES (1, ?, ?, 'running', 1800, 'garmin')",
                (f"zz-a{i}", (base + timedelta(i)).isoformat()),
            )
        conn.commit()
        regla = {"tipos": ["running"]}
        corto = self._contar(conn, lambda: practicas.dias_estado(
            conn, 1, "actividad", regla, base, base + timedelta(29)))
        largo = self._contar(conn, lambda: practicas.dias_estado(
            conn, 1, "actividad", regla, base, base + timedelta(299)))
        self.assertEqual(
            corto, largo,
            f"a 300-day range cost more statements than a 30-day one: {corto} vs {largo}")
        # The actividad family's own ceiling: one daily_metric SELECT plus one activity SELECT,
        # ceil(2 * 1.3) = 3 -- the same margin rule every other ceiling in this file carries.
        self.assertLessEqual(largo, 3, f"dias_estado cost {largo} statements, over its ceiling of 3")


class McpCopiaPcDateCeilingTest(unittest.TestCase):
    """BODY-MCP-COPIA-PC-1 ruling 1: the person's last Garmin day, read for every LOCAL body/mcp.py
    tools/call result's own freshness line, costs at most ONE query -- never a second one that
    would grow with this person's history length. `conn.set_trace_callback` applied directly, the
    same way PracticasDiasEstadoCostTest._contar above does, since this file's own fixture
    connection is already open before `contar_sql()` could trace it."""

    def _contar(self, conn, llamada) -> int:
        contador = {"n": 0}
        conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + 1))
        try:
            llamada()
        finally:
            conn.set_trace_callback(None)
        return contador["n"]

    def test_one_query_at_most_with_garmin_history(self):
        conn = _reset_db(self)
        person_id = db.local_person_id(conn)
        for i in range(30):
            db.set_metric(conn, (date(2026, 10, 1) + timedelta(i)).isoformat(), "resting_hr", 58,
                          "garmin", person_id)
        conn.commit()
        n = self._contar(conn, lambda: mcp._dia_mas_reciente_garmin(conn, person_id))
        self.assertLessEqual(n, 1, f"the last Garmin day cost {n} statements, over its ceiling of 1")

    def test_one_query_at_most_with_no_data(self):
        conn = _reset_db(self)
        person_id = db.local_person_id(conn)
        n = self._contar(conn, lambda: mcp._dia_mas_reciente_garmin(conn, person_id))
        self.assertLessEqual(n, 1, f"the last Garmin day cost {n} statements, over its ceiling of 1")


if __name__ == "__main__":
    unittest.main()
