"""BODY-AGUJETAS-1: next-day muscle soreness, one `metric_def` row per muscle group
(ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1), on the same propose/apply/undo path `registrar_medida`
already gives `flex_alcance_cm`/`body_weight_kg` -- plus a 0-5 whole-number-only range (ruling 2).
Invented demo data only, no real health figures.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, identity, readiness, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

SORENESS_KEYS = ("soreness_legs", "soreness_glutes", "soreness_back", "soreness_chest",
                 "soreness_shoulders", "soreness_arms", "soreness_core")


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


class RowsExistTest(unittest.TestCase):
    def test_all_seven_rows_exist_with_the_rulings_shape(self):
        conn = _conn(self)
        db.init(conn)
        rows = {
            r["key"]: (r["unit"], r["better"], r["kind"], r["origen"])
            for r in conn.execute(
                "SELECT key, unit, better, kind, origen FROM metric_def WHERE key IN "
                "('soreness_legs', 'soreness_glutes', 'soreness_back', 'soreness_chest', "
                "'soreness_shoulders', 'soreness_arms', 'soreness_core')"
            )
        }
        self.assertEqual(set(rows), set(SORENESS_KEYS))
        for key, shape in rows.items():
            self.assertEqual(shape, ("pts", "neutral", "measured", "self"), key)

    def test_rows_arrive_on_an_existing_database(self):
        conn = _conn(self)
        db.init(conn)
        conn.execute("DELETE FROM metric_def WHERE key IN (%s)" % ",".join("?" * len(SORENESS_KEYS)),
                     SORENESS_KEYS)
        conn.commit()
        db.init(conn)
        still_there = {r["key"] for r in conn.execute(
            "SELECT key FROM metric_def WHERE key IN (%s)" % ",".join("?" * len(SORENESS_KEYS)),
            SORENESS_KEYS)}
        self.assertEqual(still_there, set(SORENESS_KEYS))

    def test_each_key_has_a_zero_to_five_inclusive_range(self):
        for key in SORENESS_KEYS:
            self.assertEqual(db.RANGOS_MEDIDA_PERSONAL[key], (0, 5))

    def test_each_key_requires_a_whole_number(self):
        for key in SORENESS_KEYS:
            self.assertIn(key, db.ENTEROS_MEDIDA_PERSONAL)

    def test_every_key_is_offered_by_registrar_medida(self):
        for key in SORENESS_KEYS:
            self.assertIn(key, herramientas.SELF_METRIC_KEYS)


class ProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = _cid(self.conn)

    def test_propose_apply_undo_soreness_legs(self):
        before = self.conn.execute("SELECT COUNT(*) FROM daily_metric").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "soreness_legs", "value": 3, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_metric").fetchone()[0], before)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT value, source FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'soreness_legs'").fetchone()
        self.assertEqual((row["value"], row["source"]), (3.0, "self"))
        deshecho = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(deshecho["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'soreness_legs'").fetchone()
        self.assertIsNone(row)

    def test_each_group_key_can_be_proposed_on_the_day_felt(self):
        for key in SORENESS_KEYS:
            out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                        {"metric_key": key, "value": 2, "day": "2026-11-09"},
                                        "ninguno", self.cid)
            self.assertEqual(out["estado"], "propuesta")


class RangeAndWholeNumberRefusalTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = _cid(self.conn)

    def _propone(self, value):
        return herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                     {"metric_key": "soreness_legs", "value": value, "day": "2026-11-10"},
                                     "ninguno", self.cid)

    def test_zero_and_five_are_the_accepted_edges(self):
        self.assertEqual(self._propone(0)["estado"], "propuesta")
        self.assertEqual(self._propone(5)["estado"], "propuesta")

    def test_above_five_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._propone(6)

    def test_negative_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._propone(-1)

    def test_a_non_whole_number_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._propone(3.5)

    def test_a_whole_number_given_as_a_float_is_accepted(self):
        self.assertEqual(self._propone(3.0)["estado"], "propuesta")


class ReadinessIgnoresSorenessTest(unittest.TestCase):
    """A soreness key never enters readiness or any of its baselines (ORCH-CARLOS-DELEGATES-
    TIER-1-1): readiness.METRICS never names one, and a day with a soreness value plus the usual
    readiness inputs scores exactly as it would without it."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)

    def test_metrics_tuple_names_no_soreness_key(self):
        for key in SORENESS_KEYS:
            self.assertNotIn(key, readiness.METRICS)

    def test_a_soreness_value_never_changes_the_score(self):
        d0 = date(2026, 9, 1)
        today = date(2026, 10, 5)
        for i in range((today - d0).days + 1):
            day = (d0 + timedelta(i)).isoformat()
            db.set_metric(self.conn, day, "hrv_last_night", 55.0 + (i % 5) * 0.3, "garmin", 1)
            db.set_metric(self.conn, day, "resting_hr", 55.0 - (i % 5) * 0.2, "garmin", 1)
            db.set_metric(self.conn, day, "sleep_minutes", 420.0 + (i % 5), "garmin", 1)
            db.set_metric(self.conn, day, "sleep_need_min", 420.0, "garmin", 1)
            db.set_metric(self.conn, day, "body_battery_wake", 80.0 + (i % 5), "garmin", 1)
            db.set_metric(self.conn, day, "stress_avg", 25.0 - (i % 5) * 0.4, "garmin", 1)
        self.conn.commit()
        before = readiness.compute(self.conn, today.isoformat(), 1)
        self.assertIsNotNone(before.score)
        db.set_metric(self.conn, today.isoformat(), "soreness_legs", 5.0, "self", 1)
        self.conn.commit()
        after = readiness.compute(self.conn, today.isoformat(), 1)
        self.assertEqual(before.score, after.score)
        self.assertTrue(all("soreness" not in p.key for p in after.parts))


class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)

    def _registro(self, key: str, value: float):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": key, "value": value, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        return out["_registro"]

    def test_soreness_legs_card_in_spanish(self):
        tarjeta = asistente._tarjeta_propuesta(T("es"), self.defs, self._registro("soreness_legs", 3))
        self.assertIn("Agujetas: piernas", tarjeta["despues"])
        self.assertIn("3", tarjeta["despues"])

    def test_soreness_legs_card_in_english(self):
        tarjeta = asistente._tarjeta_propuesta(T("en"), self.defs, self._registro("soreness_legs", 3))
        self.assertIn("Soreness: legs", tarjeta["despues"])
        self.assertIn("3", tarjeta["despues"])


class TrendsRenderTest(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "trends_agujetas.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        d0 = date(2026, 9, 1)
        for i in range(20):
            db.set_metric(self.conn, (d0 + timedelta(i)).isoformat(), "resting_hr", 58 - i * 0.1, "garmin", 1)
        for i in range(5):
            db.set_metric(self.conn, (d0 + timedelta(7 * i)).isoformat(), "soreness_legs", float(i % 6),
                          "self", 1)
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _chunk(self, page: str, key: str) -> str:
        start = page.index(f'id="{key}"')
        end = page.index("</div>", page.index('class="legend mt-4"', start))
        return page[start:end]

    def test_renders_in_spanish(self):
        page = self.client.get("/trends?todas=1").text
        self.assertIn('id="soreness_legs"', page)
        chunk = self._chunk(page, "soreness_legs")
        self.assertIn(T("es")("trends.q.soreness_legs"), chunk)
        self.assertIn(T("es")("trends.plain.soreness_legs"), chunk)
        self.assertIn("pts", chunk)

    def test_renders_in_english(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        page = self.client.get("/trends?todas=1").text
        self.assertIn('id="soreness_legs"', page)
        chunk = self._chunk(page, "soreness_legs")
        self.assertIn(T("en")("trends.q.soreness_legs"), chunk)
        self.assertIn(T("en")("trends.plain.soreness_legs"), chunk)
        self.assertIn("pts", chunk)

    def test_self_group_includes_every_soreness_key(self):
        groups = dict(comun.TREND_GROUPS)
        for key in SORENESS_KEYS:
            self.assertIn(key, groups["self"])


if __name__ == "__main__":
    unittest.main()
