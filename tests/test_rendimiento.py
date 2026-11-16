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
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, fechas, goals, web
from body.agente import conversacion as agente_conversacion
from body.rutas import comun as web_comun
from body.rutas import tendencias as web_tendencias
from tests._rutas import contar_sql, rutas_planas

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
# each in that slice's own ledger entry.
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
_CEILINGS = {
    "/": 112,
    "/ajustes/asistente": 16,
    "/ajustes/dispositivos": 9,
    "/api/asistente/mcp/herramientas": 3,
    "/api/hogar/manana": 3,
    "/api/hogar/serie": 3,
    "/api/vida/semana": 3,
    "/asistente/conversacion/{conversacion_id}": 17,
    "/ayuda": 15,
    "/day/{day}": 81,
    "/day/{day}/entreno/{source_id}": 16,  # BODY-ENTRENO-VISTA-SESION-1: measured 12, ceil(12 * 1.3) = 16
    "/experiments/{experiment_id}": 27,
    "/garmin": 9,
    "/goals": 75,
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
    "/privacidad/exportar": 58,
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
    conn.commit()
    return {"goal_id": goal_id, "day": day, "conversacion_id": conversacion_id, "experiment_id": experiment_id,
            "source_id": source_id}


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


if __name__ == "__main__":
    unittest.main()
