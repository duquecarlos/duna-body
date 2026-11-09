"""BODY-ENTRENO-MEDIDA-1: `registrar_medida` on the existing propose/apply/undo pattern, the
self-measured keys in Trends' metric picker, and a goal evaluated from self-measured values like
any other (body/goals.py is source-agnostic, BODY-ENTRENO-1-INV Q4). Invented demo data only.
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import baselines, config, db, goals, identity, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.rutas import comun


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


class RegistrarMedidaProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_propose_writes_nothing_to_daily_metric(self):
        before = self.conn.execute("SELECT COUNT(*) FROM daily_metric").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_metric").fetchone()[0], before)

    def test_apply_writes_the_value_with_source_self_only_for_that_person(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT value, source FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'flex_alcance_cm'").fetchone()
        self.assertEqual(row["value"], 32.0)
        self.assertEqual(row["source"], "self")

    def test_undo_removes_the_value_when_there_was_none_before(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'flex_alcance_cm'").fetchone()
        self.assertIsNone(row)

    def test_undo_restores_the_prior_value(self):
        db.set_metric(self.conn, "2026-11-10", "flex_alcance_cm", 30.0, "self", 1)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'flex_alcance_cm'").fetchone()
        self.assertEqual(row["value"], 30.0)

    def test_drift_refuses_when_the_value_changed_after_propose(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        db.set_metric(self.conn, "2026-11-10", "flex_alcance_cm", 29.0, "self", 1)  # drifts after propose
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'flex_alcance_cm'").fetchone()
        self.assertEqual(row["value"], 29.0)

    def test_undo_refuses_when_the_value_was_changed_again_after_applying(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        db.set_metric(self.conn, "2026-11-10", "flex_alcance_cm", 31.0, "self", 1)
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_person_b_cannot_apply_person_as_proposal(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])

    def test_day_defaults_to_today_in_the_persons_zone(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "snoring_min", "value": 12.0}, "ninguno", self.cid)
        registro = self.conn.execute("SELECT despues_json FROM propuesta_agente WHERE id = ?",
                                     (out["propuesta_id"],)).fetchone()
        self.assertIn("day", json.loads(registro["despues_json"]))


class FutureDayTest(unittest.TestCase):
    """BODY-AGENTE-DIA-FUTURO-1: `registrar_medida` is a number the person measured themself -- an
    actual record -- so it refuses a day its own person has not reached yet, checked in THEIR zone,
    not the server's. `Pacific/Kiritimati` (UTC+14) and a UTC instant late in its day are picked so
    the person's local date and the UTC date disagree, proving the check reads the person's own
    today."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.conn.execute("UPDATE person SET timezone = 'Pacific/Kiritimati' WHERE id = 1")
        self.conn.commit()

    def _patched(self, instant: datetime):
        return mock.patch("body.fechas.datetime", **{
            "now.side_effect": lambda tz=None: instant.astimezone(tz) if tz else instant,
        })

    def test_today_in_the_persons_zone_is_accepted(self):
        instant = datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc)  # already Nov 10 in Kiritimati
        with self._patched(instant):
            out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                        {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                        "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")

    def test_a_day_beyond_tolerance_in_the_persons_zone_is_refused(self):
        instant = datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc)  # local today is Nov 10
        with self._patched(instant):
            with self.assertRaises(herramientas.ToolError):
                herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                      {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-12"},
                                      "ninguno", self.cid)


class RefusalsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_a_garmin_key_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                  {"metric_key": "resting_hr", "value": 55.0, "day": "2026-11-10"},
                                  "ninguno", self.cid)

    def test_unknown_key_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                  {"metric_key": "no_existe", "value": 1.0, "day": "2026-11-10"},
                                  "ninguno", self.cid)

    def test_negative_value_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                  {"metric_key": "flex_alcance_cm", "value": -1.0, "day": "2026-11-10"},
                                  "ninguno", self.cid)

    def test_a_garmin_key_is_refused_by_the_function_itself_too(self):
        # Defense in depth, past the static enum (BODY-ENTRENO-MEDIDA-1): the function's own check
        # reads the live metric_def row, not a hardcoded pair of keys.
        with self.assertRaises(herramientas.ToolError):
            herramientas._registrar_medida(self.conn, 1, self.cid, "resting_hr", 55.0, day="2026-11-10")


class PersonIsolationTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_person_bs_value_never_reaches_person_a(self):
        cid_a = _cid(self.conn, 1)
        cid_b = _cid(self.conn, 2)
        out_a = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                      {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                      "ninguno", cid_a)
        out_b = herramientas.ejecutar(self.conn, 2, "registrar_medida",
                                      {"metric_key": "flex_alcance_cm", "value": 40.0, "day": "2026-11-10"},
                                      "ninguno", cid_b)
        propuestas.aplicar(self.conn, 1, out_a["propuesta_id"])
        propuestas.aplicar(self.conn, 2, out_b["propuesta_id"])
        row_a = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'flex_alcance_cm'").fetchone()
        row_b = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 2 AND day = '2026-11-10' "
            "AND metric_key = 'flex_alcance_cm'").fetchone()
        self.assertEqual(row_a["value"], 32.0)
        self.assertEqual(row_b["value"], 40.0)

    def test_undo_of_person_as_proposal_never_touches_person_b(self):
        cid_a = _cid(self.conn, 1)
        db.set_metric(self.conn, "2026-11-10", "flex_alcance_cm", 40.0, "self", 2)
        self.conn.commit()
        out_a = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                      {"metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10"},
                                      "ninguno", cid_a)
        propuestas.aplicar(self.conn, 1, out_a["propuesta_id"])
        propuestas.deshacer(self.conn, 1, out_a["propuesta_id"])
        row_b = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 2 AND day = '2026-11-10' "
            "AND metric_key = 'flex_alcance_cm'").fetchone()
        self.assertEqual(row_b["value"], 40.0)


class GoalEvaluationFromSelfMeasuredTest(unittest.TestCase):
    """A goal on a self-measured key, direction 'lower', is evaluated weekly the same way as any
    Garmin-sourced metric: body/goals.py reads daily_metric through db.series without filtering by
    source."""

    def setUp(self):
        self.conn = _conn(self)

    def test_goal_on_flex_alcance_cm_lower_is_evaluated_from_self_measured_values(self):
        gid = goals.propose(self.conn, 1, "Mejorar flexibilidad", "2026-10-01",
                            metric_key="flex_alcance_cm", direction="lower",
                            target_value=10.0, baseline_value=30.0, deadline="2026-12-30")
        goals.activate(self.conn, gid)
        monday = date(2026, 11, 2)
        for i in range(5):
            db.set_metric(self.conn, (monday + timedelta(i)).isoformat(), "flex_alcance_cm",
                          20.0 - i, "self", 1)
        self.conn.commit()
        resultado = goals.evaluar_semana(self.conn, gid, 1, monday.isoformat())
        self.assertIsNotNone(resultado)
        self.assertEqual(resultado["metric_avg"], 18.0)
        self.assertIsNotNone(resultado["on_track"])


class TrendsSelfMeasuredGroupTest(unittest.TestCase):
    """The self-measured keys join Trends' "Ver todas las medidas" picker as their own group, with
    an honest card even from a single point (BODY-ENTRENO-MEDIDA-1)."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "trends_self.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        d0 = date(2026, 9, 1)
        for i in range(20):
            day = (d0 + timedelta(i)).isoformat()
            db.set_metric(self.conn, day, "resting_hr", 58 - i * 0.1, "garmin", 1)
            db.set_metric(self.conn, day, "sleep_minutes", 410 + i, "garmin", 1)
        db.set_metric(self.conn, (d0 + timedelta(19)).isoformat(), "flex_alcance_cm", 32.0, "self", 1)
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def test_trend_groups_has_a_self_group_built_from_metric_def(self):
        groups = dict(comun.TREND_GROUPS)
        self.assertIn("self", groups)
        self.assertEqual(set(groups["self"]), {"flex_alcance_cm", "snoring_min"})

    def test_trends_page_shows_the_self_measured_card_from_a_single_point(self):
        page = self.client.get("/trends?todas=1").text
        self.assertIn('id="flex_alcance_cm"', page)
        self.assertIn("Medido por ti", page)  # trends.group.self
        self.assertIn("Tú registras este dato", page)  # trends.self_note


class TrendsSelfMeasuredThresholdTest(unittest.TestCase):
    """BODY-ENTRENO-TOOLS-1-M fix 3: below baselines.MIN_DAYS of self-measured values, the card has
    no delta, no normal-range legend and no band -- a count line instead; from MIN_DAYS on it
    behaves like any other card."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "trends_threshold.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        d0 = date(2026, 9, 1)
        for i in range(20):
            db.set_metric(self.conn, (d0 + timedelta(i)).isoformat(), "resting_hr", 58 - i * 0.1, "garmin", 1)
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _card_chunk(self, page: str) -> str:
        start = page.index('id="flex_alcance_cm"')
        end = page.index("</div>", page.index('class="legend mt-4"', start))
        return page[start:end]

    def test_below_min_days_has_a_count_line_and_no_normal_legend(self):
        self.assertLess(3, baselines.MIN_DAYS)
        for i in range(3):
            db.set_metric(self.conn, (date(2026, 10, 1) + timedelta(i)).isoformat(), "flex_alcance_cm",
                          30.0 - i, "self", 1)
        self.conn.commit()
        chunk = self._card_chunk(self.client.get("/trends?todas=1").text)
        self.assertIn("3 mediciones hasta ahora", chunk)
        self.assertNotIn("Tu rango normal", chunk)
        self.assertNotIn("como siempre", chunk)

    def test_at_min_days_behaves_like_any_other_card(self):
        for i in range(baselines.MIN_DAYS):
            db.set_metric(self.conn, (date(2026, 10, 1) + timedelta(i)).isoformat(), "flex_alcance_cm",
                          30.0 - i, "self", 1)
        self.conn.commit()
        chunk = self._card_chunk(self.client.get("/trends?todas=1").text)
        self.assertIn("Tu rango normal", chunk)
        self.assertNotIn("mediciones hasta ahora", chunk)


if __name__ == "__main__":
    unittest.main()
