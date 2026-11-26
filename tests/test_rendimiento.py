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
import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, demo, dispositivo, fechas, garmin_cuenta, goals, identity, mcp, \
    noche_rasgos, practicas, registro_cobertura, shift_import, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas
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
# BODY-HISTORIA-VIDA-LUGARES-2-R ruling 7: practicas.estado()'s own one-read-per-call trip set
# (ruling 0's dias_de_viaje, read once for every temporada AND the baseline together) adds exactly
# one statement -- measured 18 (17 before), still under the ceiling of 23, unchanged.
# BODY-DESPERTAR-REAL-7: / and /day/{day} each gained Día/Hoy's own inline despertar card (ruling
# 1-4) -- both ceilings stay UNCHANGED, measured comfortably under them. On this file's own fixture
# (demo._seed(dias=30), whose most recent sleep night sits a day before the most recent metric day,
# so `_despertar_hoy_ctx`'s own correction check finds none and `despertar_real.estimar` runs but
# exits at its own first query, no sleep_session that later day): / measured 124 (ceiling 155),
# /day/{day} measured 59 (ceiling 81) -- `_despertar_dia_ctx` costs 0 there (no sleep that day, its
# own first check).
# BODY-HISTORIA-VIDA-LUGARES-2-R ruling 7: /day/{day}'s own unconditional one-day trip check
# (ruling 3's dias_de_viaje call, always one statement, never skipped) measured 68 on this file's
# own fixture (67 without it) -- still under the ceiling of 81, unchanged. /trends' own trip read
# (ruling 2) runs only when a window is picked (r=7/30/90 or a month, never "all"): measured
# /trends?r=7 at 25 statements (24 before), still under the ceiling of 28, unchanged.
# On the richer fixture tests/test_despertar_pantalla.py's own
# `_seed_levantado_media` builds (a real propuesta, no correction yet): `despertar_real.estimar`
# alone costs 8 statements; `_despertar_hoy_ctx`'s own full run (the correction check plus that
# estimar call) costs 9; `_despertar_dia_ctx`'s own full run (the correction check, that same
# estimar call, the curve's own intraday query and the pending-propuesta check) costs 11 -- both
# within ceil(8 * 1.3) = 11, the SAME margin rule this file's own ceilings use, read here as
# "estimar's own measured ceiling" (ruling 5).
# BODY-DESPERTAR-REAL-6A-R2 ruling 3: re-measured on the SAME file's own `_seed_ambos_alta` (a
# morning offering BOTH kinds, neither corrected yet) -- unchanged at 8 statements for
# `despertar_real.estimar` itself (ruling 1 added no new query) and 9 for `_despertar_hoy_ctx`
# (it still reads only the first propuesta in the list, sueno-first ordering). `_despertar_dia_
# ctx` now costs 12 (was 11 with one kind): ruling 4's per-kind loop runs one extra pending-
# propuesta check for the second kind, so its own ceiling moves to ceil(12 * 1.3) = 16, named
# here rather than carried over from the single-kind figure above.
# BODY-TURNOS-DESHACER-LOTE-3-R: /shifts' own history section (ORCH-CARLOS-DELEGATES-TIER-1-1
# ruling 1, one combined SELECT) added its one statement only on the no-pending-proposals path --
# this file's own fixture always carries a pending proposed batch (demo._seed_shifts), so the
# route returned before that path ran and this shared ceiling stayed UNCHANGED at 42.
# tests/test_turnos_deshacer.py's own HistorialCostoTest measures that path's +1 directly.
# BODY-PLAN-FASES-2 ruling 6: horizonte.resumen() now always reads horizonte_fase too (its own
# `fases()` call, folded into the one dict `_plan_semana_ctx`'s PLANES_HOY_MAX resumen() calls
# already read) -- "/"'s own ceiling stays UNCHANGED at 155, measured 128 on this file's own
# fixture (one active plan, now the demo's own horizon with three seeded phases). /goals/{goal_id}
# itself never moves (ceiling 33): the fixture's picked goal_id is still the plain sleep goal, so
# `_plan_fases_ctx` never runs for it. The horizon's OWN /goals/{goal_id} page (metas.py's new
# `_plan_fases_ctx`) is bounded, not swept here -- tests/test_rendimiento.py's own
# PlanFasesCostIndependentTest proves it: one statement with no phase at all, two total with one
# phase or with three phases each carrying one scoped rule, never more.
# BODY-TURNOS-DESHACER-LOTE-4 ruling 1: the route now calls _historial_filas ONCE per request
# regardless of branch, so this file's own fixture's one-pending-proposal state reaches it too --
# measured 33 statements (32 before), ceil(33 * 1.3) = 43. The many-proposals state is never
# reached by this shared fixture (it seeds exactly one pending batch); it is named separately as
# tests/test_rendimiento.py's own ManyProposalShiftsCostTest below, on the SAME demo fixture plus
# one extra pending batch -- measured 38 statements, ceil(38 * 1.3) = 50.
# ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6) ruling 3: /'s own ceiling, re-measured after
# `_plan_hoy_item` (body/rutas/hoy.py) started calling `reentrada.senales` for the day ahead's own
# planned-session row. Measured 131 statements (128 before), still comfortably under 155 --
# ceiling unchanged. `planear_entreno`'s own cost (rulings 1-2, the same slice) is named
# separately in this file's own PlanearEntrenoReentradaCostTest below, since it is a tool call,
# never a GET route, and so cannot sit in this table.
# BODY-COMIDA-PRODUCTO-1-D: /comida/productos and /comida/productos/agregar, new with this slice
# (BODY-COMIDA-PRODUCTO-1's own picker). Measured on this file's own fixture (demo._seed(dias=30),
# which now also seeds a few manual products and one meal naming two of them, BODY-COMIDA-
# PRODUCTO-1 ruling 7): /comida/productos measured 22 statements (productos_visibles plus
# basis_de_producto per product, the picker's own search-free listing), ceil(22 * 1.3) = 29.
# /comida/productos/agregar measured 13 (the sweep's own no-params call: the early-return path,
# productos_visibles's own one-read visibility check, no product actually added), ceil(13 * 1.3) =
# 17. Both well under _MAX_CONNECTIONS (2 measured for each).
# BODY-REVISION-AJUSTES-NAV-PICKER-1 finding 13: /comida/productos now also threads
# db.trabaja_turnos_si into its own render call (the nav's Shifts tab was silently dropping for a
# person who works shifts). Measured 24 statements (22 before), still comfortably under 29 --
# ceiling unchanged.
# BODY-REVISION-COPY-GAPS-1 finding 19: /'s own ceiling, raised for the yesterday-unmarked
# experiment nudge (body/rutas/experimentos.py's nudge_ayer_ctx) -- one unconditional dismissal
# check plus one batched active-experiments read, never a per-experiment query on this file's own
# fixture (its seeded experiment never qualifies as an unmarked-yesterday candidate). Measured 157
# statements (131 before), ceil(157 * 1.3) = 205.
_CEILINGS = {
    "/": 205,
    "/comida/productos": 29,
    "/comida/productos/agregar": 17,
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
    # BODY-ENTRENO-VISTA-SESION-1: measured 12, ceil(12 * 1.3) = 16. BODY-PLAN-FASES-5
    # re-measured with the dolor card's own two new reads (zonas_en_recuperacion, dolor_de_sesion)
    # ON THE REAL PATH that shows the card (an open 'lesion' period over a watch strength set's own
    # categoria): 13, still comfortably under 16 -- ceiling unchanged.
    "/day/{day}/entreno/{source_id}": 16,
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
    "/shifts": 43,
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
    "/api/hogar/manana": 37,  # BODY-HOGAR-PLAN-RANGO-1: measured 28 (plan_bloque's one extra SELECT
                              # for its own filas), ceil(28 * 1.3) = 37 -- was measured 27, ceil = 36
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


# BODY-DEMO-HOY-FIJO-1: _seed below seeds the demo with no `today=` and most of its callers read
# routes through the real clock (comun._today_local) -- pinned so the module's own statement-count
# ceilings never depend on which real day the suite runs on (a different anchor can take a route
# down a different branch, e.g. turnos.py's empty-cost-table state, with its own statement count).
_ANCHOR = date(2026, 11, 20)


def _seed(conn) -> dict:
    """Same populated fixture as tests/test_get_sin_escritura.py's sweep, so every parameterised
    GET route carries a real id -- demo.py's seed carries no real figure, by its own contract.
    BODY-HOY-ESCRITORIO-1 ruling 4: plus one logged (manual) training session for TODAY, so the
    route sweep below measures "/"'s own bounded workout_set read (body/rutas/hoy.py's
    _entrenado_hoy_categorias) against its real worst case -- an id list that is not empty --
    rather than the demo's own random activities, which rarely land a categorised set on today."""
    demo._seed(conn, dias=30, idioma="es", today=_ANCHOR)
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
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
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


class NavShiftsLinkCensusTest(unittest.TestCase):
    """BODY-REVISION-AJUSTES-NAV-PICKER-1 ruling 2: every screen that renders the shared nav must
    carry its Shifts tab for a person who works shifts -- walks the SAME non-vacuous GET route set
    `RouteCostCeilingTest` proves equal to `_CEILINGS` above, rather than a hand-picked sample, so a
    future screen that forgets to thread `turnos` through its own render call is caught here instead
    of being found by a person later (finding 13: body/rutas/comida.py's own picker was exactly
    this)."""

    def test_every_nav_bearing_route_carries_the_shifts_tab(self):
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = _reset_db(self)
        ids = _seed(conn)
        db.set_trabaja_turnos(conn, True, 1)
        conn.commit()

        get_routes = [r for r in rutas_planas(web.app.routes) if "GET" in getattr(r, "methods", ())]
        missing = []
        checked = 0
        for route in get_routes:
            path = _fill(route.path, ids)
            r = client.get(path)
            if '<nav class="tabs"' in r.text:
                checked += 1
                if 'href="/shifts"' not in r.text:
                    missing.append(route.path)

        self.assertGreater(checked, 0, "no nav-bearing route was reached -- the sweep itself is broken")
        self.assertEqual(missing, [], f"route(s) rendering the nav without the Shifts tab: {missing}")


class ManyProposalShiftsCostTest(unittest.TestCase):
    """BODY-TURNOS-DESHACER-LOTE-4 ruling 3: /shifts' many-pending-proposals state, never reached
    by `_CEILINGS`'s own shared sweep (its fixture seeds exactly one pending batch) -- its own
    named ceiling, on the SAME `_seed` fixture plus one extra pending batch. Measured 38
    statements, ceil(38 * 1.3) = 50."""

    _CEILING = 50

    def test_stays_within_its_own_ceiling(self):
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = _reset_db(self)
        _seed(conn)
        data = {"source": {"received": "2026-01-01T12:00:00-05:00", "extracted_at": "2026-01-01T12:00:00-05:00"},
                "shifts": [{"day": "2026-01-01", "kind": "night", "start_local": "2026-01-01T22:00:00"}]}
        shift_import.propose_data(conn, data, "turnos-segunda-propuesta.json", 1)
        conn.commit()

        with contar_sql() as stats:
            r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(stats["statements"], self._CEILING)
        self.assertLessEqual(stats["connections"], _MAX_CONNECTIONS)


def _authorize_params(resp) -> dict:
    qp = parse_qs(urlparse(resp.headers["location"]).query)
    return {k: v[0] for k, v in qp.items()}


class RouteCostRealPathTest(unittest.TestCase):
    """BODY-RENDIMIENTO-4: a second ceiling, on each of the 13 routes `_CEILINGS` above can only
    measure as an early 404, on its own real path. Ruling 2's drift guard lives in
    `test_the_main_sweeps_404_set_matches_this_tables_keys` below: it is what makes a new
    device-key or hosted-only GET route fail this file until it gets a real-path ceiling too."""

    def test_the_main_sweeps_404_set_matches_this_tables_keys(self):
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
        conn = _reset_db(self)
        ids = _seed(conn)
        get_routes = [r for r in rutas_planas(web.app.routes) if "GET" in getattr(r, "methods", ())]
        non_200 = {route.path for route in get_routes
                   if client.get(_fill(route.path, ids)).status_code in (401, 403, 404)}
        self.assertEqual(non_200, set(_CEILINGS_REAL_PATH),
                          "the main sweep's 401/403/404 route set and this table's keys have drifted apart")

    def test_device_key_routes_on_their_real_path(self):
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
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
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
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
        clock_patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        clock_patcher.start()
        self.addCleanup(clock_patcher.stop)
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
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
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


class PracticasResumenCostIndependentOfCountTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-2-R2 ruling 2: /history's "Tus prácticas" section reads each source
    family once for ALL active practices together (practicas.resumen), so its statement count
    stays the same whether the person tracks 1 practice or 6."""

    def test_one_practica_vs_six_same_statement_count(self):
        conn = _reset_db(self)
        db.set_metric(conn, "2026-11-01", "resting_hr", 55.0, "garmin", 1)  # so /history leaves soon.html
        practicas.crear(conn, 1, "ZZ Correr", "actividad", {"tipos": ["running"]})
        conn.commit()
        with contar_sql() as uno:
            r1 = client.get("/history")
        for tipo in ("cycling", "swimming", "walking", "hiking", "yoga"):
            practicas.crear(conn, 1, f"ZZ {tipo}", "actividad", {"tipos": [tipo]})
        conn.commit()
        with contar_sql() as seis:
            r2 = client.get("/history")
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(uno["statements"], seis["statements"],
                          f"1 practica vs 6 cost different statement counts through /history: "
                          f"{uno['statements']} vs {seis['statements']}")


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
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
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
        patcher = patch.object(web_comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
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


class PlanFasesCostIndependentTest(unittest.TestCase):
    """BODY-PLAN-FASES-2 ruling 6: /goals/{goal_id}'s own phases block (body/rutas/metas.py's
    `_plan_fases_ctx`) costs one statement (`horizonte.fases()`) when the plan has no phase at
    all, and the SAME two statements total -- that same `fases()` call plus one grouped rule read
    -- whether the plan has one phase and no scoped rule or three phases each with one scoped
    rule."""

    HOY = date(2026, 11, 5)
    MONDAY = fechas.lunes_de(HOY)

    def test_no_phase_vs_one_phase_statement_delta_is_exactly_one(self):
        """Both plans already carry a plain, unscoped rule, so both are a horizon and both render
        the full plan screen -- the only difference between the two requests is `gid_con`'s one
        phase. `_plan_fases_ctx` always pays one statement for `horizonte.fases()`; `gid_sin`'s
        empty result stops there (its own early return), while `gid_con`'s one phase pays a
        second statement for the grouped rule read -- a delta of exactly 1, the same 2 total
        `_plan_fases_ctx` pays for any number of phases or rules once it has at least one phase."""
        conn = _reset_db(self)
        gid_sin = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=20)).isoformat(), "ZZ sin fase")
        db.crear_regla_horizonte(conn, 1, gid_sin, "siempre", None, "Regla sin fase")
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as sin:
                r1 = client.get(f"/goals/{gid_sin}")

        gid_con = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=20)).isoformat(), "ZZ una fase")
        db.crear_regla_horizonte(conn, 1, gid_con, "siempre", None, "Regla sin fase")
        db.crear_fase_horizonte(conn, 1, gid_con, "Fase", self.MONDAY.isoformat(),
                                 (self.MONDAY + timedelta(days=20)).isoformat())
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as con:
                r2 = client.get(f"/goals/{gid_con}")

        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(
            con["statements"] - sin["statements"], 1,
            f"a plan with one phase should cost exactly 1 statement more than one with none: "
            f"{sin['statements']} vs {con['statements']}")

    def test_one_phase_vs_three_phases_with_scoped_rules_same_statement_count(self):
        conn = _reset_db(self)
        gid_una = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=62)).isoformat(), "ZZ una fase larga")
        fid = db.crear_fase_horizonte(conn, 1, gid_una, "Fase unica", self.MONDAY.isoformat(),
                                       (self.MONDAY + timedelta(days=62)).isoformat())
        db.crear_regla_horizonte(conn, 1, gid_una, "siempre", None, "Regla de la fase unica", fase_id=fid)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as una:
                r1 = client.get(f"/goals/{gid_una}")

        gid_tres = _horizonte_milestone(
            conn, 1, self.MONDAY.isoformat(), (self.MONDAY + timedelta(days=62)).isoformat(), "ZZ tres fases")
        cortes = [0, 21, 42, 62]
        for i in range(3):
            fid_i = db.crear_fase_horizonte(
                conn, 1, gid_tres, f"Fase {i}", (self.MONDAY + timedelta(days=cortes[i])).isoformat(),
                (self.MONDAY + timedelta(days=cortes[i + 1])).isoformat(), orden=i)
            db.crear_regla_horizonte(conn, 1, gid_tres, "siempre", None, f"Regla de la fase {i}", fase_id=fid_i)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=self.HOY):
            with contar_sql() as tres:
                r2 = client.get(f"/goals/{gid_tres}")

        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(
            una["statements"], tres["statements"],
            f"three phases with their own scoped rule cost more statements than one: "
            f"{una['statements']} vs {tres['statements']}")


class RegistroCoberturaCostIndependentOfSpanTest(unittest.TestCase):
    """BODY-REGISTRO-COBERTURA-1 ruling (bounded cost): `cobertura`'s own statement count stays the
    same whether the window it is asked to cover is 7 days or 90 -- each stream is read with one
    bounded ranged query over the whole window, never one query per day. `conn.set_trace_callback`
    applied directly on the fixture's own already-open connection, the same pattern
    PlanearEntrenoReentradaCostTest._contar above uses -- `cobertura` reuses the connection it is
    given rather than opening its own, so `contar_sql()`'s own sqlite3.connect patch would see
    nothing."""

    HOY = date(2026, 11, 5)

    def _contar(self, conn, llamada):
        contador = {"n": 0}
        conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + 1))
        try:
            return llamada(), contador["n"]
        finally:
            conn.set_trace_callback(None)

    def test_seven_days_vs_ninety_days_same_statement_count(self):
        conn = _reset_db(self)
        gid = _horizonte_milestone(
            conn, 1, self.HOY.isoformat(), (self.HOY + timedelta(days=120)).isoformat(), "ZZ cobertura")
        conn.commit()
        _, corto = self._contar(
            conn, lambda: registro_cobertura.cobertura(conn, 1, gid, hasta=self.HOY + timedelta(days=7)))
        _, largo = self._contar(
            conn, lambda: registro_cobertura.cobertura(conn, 1, gid, hasta=self.HOY + timedelta(days=90)))
        self.assertEqual(corto, largo,
                          f"a 90-day cobertura() cost more statements than a 7-day one: {corto} vs {largo}")


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


class DespertarRealCostTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-3: body/despertar_real.py's `estimar` reads a day's own intraday series
    through body/intradia_horario.py's per-(day, kind) cache, so its own statement count stays
    the same whether a day holds a handful of samples or a dense one -- never one query per
    sample, never one per point evaluated."""

    def _contar(self, conn, llamada) -> int:
        contador = {"n": 0}
        conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + 1))
        try:
            llamada()
        finally:
            conn.set_trace_callback(None)
        return contador["n"]

    def test_cost_independent_of_sample_density(self):
        from tests.test_despertar_real import BASE_MUESTRAS, DAY, END_LOCAL, START_LOCAL, _comida, _guardar_estres, _guardar_fc, _sesion
        from body import despertar_real

        conn = _reset_db(self)
        _sesion(conn, 1, DAY, START_LOCAL, END_LOCAL)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + [
            (f"{DAY}T05:02:00", 58.0), (f"{DAY}T05:04:00", 58.0), (f"{DAY}T05:06:00", 58.0)])
        conn.commit()
        escaso = self._contar(conn, lambda: despertar_real.estimar(conn, 1, DAY))

        conn2 = _reset_db(self)
        _sesion(conn2, 1, DAY, START_LOCAL, END_LOCAL)
        base = datetime.fromisoformat(f"{DAY}T05:00:00")
        densa_fc = BASE_MUESTRAS + [((base + timedelta(minutes=2 * i)).isoformat(), 58.0) for i in range(120)]
        densa_estres = [((base + timedelta(minutes=3 * i)).isoformat(), 20.0) for i in range(80)]
        _guardar_fc(conn2, 1, DAY, densa_fc)
        _guardar_estres(conn2, 1, DAY, densa_estres)
        _comida(conn2, 1, DAY, f"{DAY}T07:00:00")
        conn2.commit()
        densa = self._contar(conn2, lambda: despertar_real.estimar(conn2, 1, DAY))

        self.assertEqual(escaso, densa,
                          f"a sparse day vs a dense one cost different statement counts: {escaso} vs {densa}")
        # Measured 8 statements on this file's own dense fixture; ceil(8 * 1.3) = 11, the same
        # margin rule every other ceiling in this file carries.
        self.assertLessEqual(densa, 11, f"estimar cost {densa} statements, over its ceiling of 11")

    def test_dia_and_hoy_ctx_cost_with_both_kinds_offered(self):
        """BODY-DESPERTAR-REAL-6A-R2 ruling 3: `_despertar_dia_ctx`'s and `_despertar_hoy_ctx`'s
        own ceilings, re-measured on a morning offering BOTH kinds (neither corrected yet) -- see
        this file's own comment block above for the figures."""
        from datetime import date as _date

        from body.i18n import T
        from body.rutas import hoy
        from tests.test_despertar_pantalla import _seed_ambos_alta
        from tests.test_despertar_real import DAY

        conn = _reset_db(self)
        _seed_ambos_alta(conn)
        t = T("es")
        sleep_end_local = conn.execute(
            "SELECT end_local FROM sleep_session WHERE person_id = 1 AND day = ?", (DAY,)).fetchone()["end_local"]

        n_hoy = self._contar(conn, lambda: hoy._despertar_hoy_ctx(t, conn, 1, DAY, _date.fromisoformat(DAY)))
        self.assertLessEqual(n_hoy, 11, f"_despertar_hoy_ctx cost {n_hoy} statements, over its ceiling of 11")

        n_dia = self._contar(
            conn, lambda: hoy._despertar_dia_ctx(t, conn, 1, "America/Bogota", DAY, sleep_end_local))
        self.assertLessEqual(n_dia, 16, f"_despertar_dia_ctx cost {n_dia} statements, over its ceiling of 16")


class CompararNocheCostTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-2 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3): `noche_rasgos.comparar`'s
    own statement count is independent of how many nights fall inside [desde, fecha] -- one bulk
    statement per source table, never one per night. Measured on 400 invented consecutive nights,
    seeded once: a 30-night call and a 365-night call over that SAME data cost the SAME number of
    statements, and the 365-night call answers within a named wall-clock ceiling on this test box."""

    _N_NOCHES = 400

    def _contar(self, conn, llamada) -> int:
        contador = {"n": 0}
        conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + 1))
        try:
            llamada()
        finally:
            conn.set_trace_callback(None)
        return contador["n"]

    def _seed(self, conn) -> str:
        """400 consecutive nights, each with a stored figure, a sleep_session and a tiny 'sleep'
        raw_payload (two heart-rate samples, two stage points) -- enough to exercise every bulk
        reader `comparar` calls, never enough to make seeding itself the slow part."""
        base = date(2025, 1, 1)
        payload = json.dumps({
            "sleepHeartRate": [{"startGMT": 0, "value": 60.0}, {"startGMT": 1800000, "value": 55.0}],
            "sleepLevels": [{"startGMT": 0, "activityLevel": 1}, {"startGMT": 1800000, "activityLevel": 0}],
        })
        dia = base.isoformat()
        for i in range(self._N_NOCHES):
            dia = (base + timedelta(i)).isoformat()
            db.set_metric(conn, dia, "hrv_last_night", 40.0 + i % 20, "garmin", 1)
            conn.execute(
                "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES "
                "(1, ?, ?, ?)", (dia, f"{dia}T23:00:00", f"{dia}T06:00:00"))
            conn.execute(
                "INSERT INTO raw_payload (person_id, day, kind, source, fetched_at, payload) VALUES "
                "(1, ?, 'sleep', 'garmin', '2026-01-01T00:00:00+00:00', ?)", (dia, payload))
        conn.commit()
        return dia  # the last night seeded, this test's own `fecha`

    def test_cost_independent_of_night_count(self):
        # Two separate connections, exactly DespertarRealCostTest's own pattern just above --
        # db.periodos's own per-connection cache (body/db.py's `_periodos_de_persona`, "one read
        # per connection, at most") would otherwise let whichever call runs FIRST on a SHARED
        # connection pay for that one-time fill and the second one run free, making the two
        # counts differ for a reason that has nothing to do with night count.
        conn_30 = _reset_db(self)
        fecha = self._seed(conn_30)
        desde_30 = (date.fromisoformat(fecha) - timedelta(29)).isoformat()
        treinta = self._contar(conn_30, lambda: noche_rasgos.comparar(conn_30, 1, fecha, desde=desde_30))

        conn_365 = _reset_db(self)
        fecha = self._seed(conn_365)
        desde_365 = (date.fromisoformat(fecha) - timedelta(364)).isoformat()
        trescientos_65 = self._contar(conn_365, lambda: noche_rasgos.comparar(conn_365, 1, fecha, desde=desde_365))

        self.assertEqual(treinta, trescientos_65,
                          f"a 365-night range cost more statements than a 30-night one: "
                          f"{trescientos_65} vs {treinta}")

    def test_within_time_ceiling_for_a_365_night_history(self):
        conn = _reset_db(self)
        fecha = self._seed(conn)
        desde_365 = (date.fromisoformat(fecha) - timedelta(364)).isoformat()
        inicio = time.perf_counter()
        noche_rasgos.comparar(conn, 1, fecha, desde=desde_365)
        transcurrido = time.perf_counter() - inicio
        # Measured well under 0.5s on this test box for 365 nights of invented data; a 2s ceiling
        # leaves ample headroom for a slower CI box without hiding a real regression.
        self.assertLess(transcurrido, 2.0,
                         f"a 365-night comparar() took {transcurrido:.3f}s, over its 2.0s ceiling")


class BuscarNochesCostTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-3 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2): `noche_rasgos.buscar`'s
    own statement count is independent of how many nights fall inside [desde, hasta] -- it is
    built on the SAME `rasgos_rango` table CompararNocheCostTest above already measures, filtered
    and ordered in pure Python. Same 400-night fixture and connection-per-call pattern as
    CompararNocheCostTest: a 30-night call and a 365-night call over that SAME data cost the SAME
    number of statements, and the 365-night call answers within a named wall-clock ceiling."""

    _N_NOCHES = CompararNocheCostTest._N_NOCHES
    _contar = CompararNocheCostTest._contar
    _seed = CompararNocheCostTest._seed

    def test_cost_independent_of_night_count(self):
        conn_30 = _reset_db(self)
        fecha = self._seed(conn_30)
        desde_30 = (date.fromisoformat(fecha) - timedelta(29)).isoformat()
        filtros = [{"rasgo": "hrv_last_night", "op": ">=", "valor": 40.0}]
        treinta = self._contar(
            conn_30, lambda: noche_rasgos.buscar(conn_30, 1, desde_30, fecha, filtros=filtros))

        conn_365 = _reset_db(self)
        fecha = self._seed(conn_365)
        desde_365 = (date.fromisoformat(fecha) - timedelta(364)).isoformat()
        trescientos_65 = self._contar(
            conn_365, lambda: noche_rasgos.buscar(conn_365, 1, desde_365, fecha, filtros=filtros))

        self.assertEqual(treinta, trescientos_65,
                          f"a 365-night range cost more statements than a 30-night one: "
                          f"{trescientos_65} vs {treinta}")

    def test_within_time_ceiling_for_a_365_night_history(self):
        conn = _reset_db(self)
        fecha = self._seed(conn)
        desde_365 = (date.fromisoformat(fecha) - timedelta(364)).isoformat()
        filtros = [{"rasgo": "hrv_last_night", "op": ">=", "valor": 40.0}]
        inicio = time.perf_counter()
        noche_rasgos.buscar(conn, 1, desde_365, fecha, filtros=filtros, limite=10)
        transcurrido = time.perf_counter() - inicio
        # Same 2.0s ceiling as CompararNocheCostTest's own 365-night call, on the same box.
        self.assertLess(transcurrido, 2.0,
                         f"a 365-night buscar() took {transcurrido:.3f}s, over its 2.0s ceiling")


class PlanearEntrenoReentradaCostTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PLAN-FASES-6): `planear_entreno` now calls
    `reentrada.senales` unconditionally (rulings 1-2) -- this names that cost rather than leaving
    it invisible inside the tool's total. `tras_carga` is the costlier of the two signals (one
    extra activity-table read plus `recuperacion.estudio`'s own query); `reentrada` alone
    (`entreno_racha.sesiones_fuerza`'s one query) costs the same as calling the tool with neither
    signal present. Measured 10 statements with no signal or with reentrada alone, 15 with
    tras_carga (the worst case measured here), ceil(15 * 1.3) = 20 -- the same margin rule every
    other ceiling in this file uses. `conn.set_trace_callback` applied directly on the fixture's
    own already-open connection, the same pattern McpCopiaPcDateCeilingTest._contar above uses --
    the tool reuses the connection it is given rather than opening its own, so `contar_sql()`'s
    own sqlite3.connect patch would see nothing."""

    _CEILING = 20

    def _contar(self, conn, llamada):
        contador = {"n": 0}
        conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + 1))
        try:
            return llamada(), contador["n"]
        finally:
            conn.set_trace_callback(None)

    def test_no_signal_stays_within_the_ceiling(self):
        from tests.test_agente_entreno import _cid, _conn
        conn = _conn(self)
        cid = _cid(conn)
        out, n = self._contar(conn, lambda: herramientas.ejecutar(conn, 1, "planear_entreno", {
            "day": "2026-10-10", "titulo": "Full body",
            "ejercicios": [{"ejercicio": "press banca", "series": 4, "categoria": "pecho"}],
        }, "ninguno", cid))
        self.assertEqual(out["estado"], "propuesta")
        self.assertLessEqual(n, self._CEILING,
                              f"planear_entreno cost {n} statements, over its ceiling of {self._CEILING}")

    def test_tras_carga_the_costlier_path_stays_within_the_same_ceiling(self):
        from tests.test_agente_entreno import _cid, _conn, _seed_dia_pesado
        conn = _conn(self)
        cid = _cid(conn)
        _seed_dia_pesado(conn, 1, "2026-10-09")
        out, n = self._contar(conn, lambda: herramientas.ejecutar(conn, 1, "planear_entreno", {
            "day": "2026-10-10", "titulo": "Full body",
            "ejercicios": [{"ejercicio": "sentadilla", "series": 4, "categoria": "pierna"},
                           {"ejercicio": "press banca", "series": 4, "categoria": "pecho"}],
        }, "ninguno", cid))
        self.assertEqual(out["estado"], "propuesta")
        self.assertLessEqual(n, self._CEILING,
                              f"planear_entreno cost {n} statements, over its ceiling of {self._CEILING}")


if __name__ == "__main__":
    unittest.main()
