"""BODY-MEDIDA-PESO-1: two generic self-measured metric_def rows, `body_weight_kg` and `waist_cm`,
on the same propose/apply/undo path `registrar_medida` already gives `flex_alcance_cm` and
`snoring_min` (BODY-ENTRENO-MEDIDA-1) -- plus a plausible-range refusal for these two keys only
(`db.RANGOS_MEDIDA_PERSONAL`), since a typo in a weight or waist figure is otherwise a stored
point, not a refusal. Invented demo data only, no real health figures.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, identity, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun, tendencias


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


class RowsExistTest(unittest.TestCase):
    def test_both_rows_exist_on_a_fresh_db(self):
        conn = _conn(self)
        db.init(conn)
        rows = {
            r["key"]: (r["label"], r["unit"], r["better"], r["kind"], r["origen"])
            for r in conn.execute(
                "SELECT key, label, unit, better, kind, origen FROM metric_def "
                "WHERE key IN ('body_weight_kg', 'waist_cm')"
            )
        }
        self.assertEqual(rows["body_weight_kg"][1:], ("kg", "neutral", "measured", "self"))
        self.assertEqual(rows["waist_cm"][1:], ("cm", "neutral", "measured", "self"))

    def test_both_rows_arrive_on_an_existing_database(self):
        conn = _conn(self)
        db.init(conn)
        conn.execute("DELETE FROM metric_def WHERE key IN ('body_weight_kg', 'waist_cm')")
        conn.commit()
        still_there = conn.execute(
            "SELECT COUNT(*) FROM metric_def WHERE key IN ('body_weight_kg', 'waist_cm')"
        ).fetchone()[0]
        self.assertEqual(still_there, 0)
        db.init(conn)  # the next init() call on an existing install
        rows = {
            r["key"]: r["origen"] for r in conn.execute(
                "SELECT key, origen FROM metric_def WHERE key IN ('body_weight_kg', 'waist_cm')"
            )
        }
        self.assertEqual(rows, {"body_weight_kg": "self", "waist_cm": "self"})


class ProposeApplyUndoTest(unittest.TestCase):
    """The same propose/apply/undo shape `flex_alcance_cm` already has, for each new key."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = _cid(self.conn)

    def test_propose_apply_undo_body_weight_kg(self):
        before = self.conn.execute("SELECT COUNT(*) FROM daily_metric").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "body_weight_kg", "value": 78.4, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_metric").fetchone()[0], before)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT value, source FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'body_weight_kg'").fetchone()
        self.assertEqual((row["value"], row["source"]), (78.4, "self"))
        deshecho = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(deshecho["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'body_weight_kg'").fetchone()
        self.assertIsNone(row)

    def test_propose_apply_undo_waist_cm(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": "waist_cm", "value": 88.0, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT value, source FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'waist_cm'").fetchone()
        self.assertEqual((row["value"], row["source"]), (88.0, "self"))
        deshecho = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(deshecho["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'waist_cm'").fetchone()
        self.assertIsNone(row)


class RangeRefusalTest(unittest.TestCase):
    """A plausible-range refusal for these two keys only (db.RANGOS_MEDIDA_PERSONAL): a typo-sized
    figure is a ToolError, never a stored point. flex_alcance_cm keeps no such check -- only the
    existing negative refusal (BODY-ENTRENO-MEDIDA-1)."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = _cid(self.conn)

    def _propone(self, metric_key: str, value: float):
        return herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                     {"metric_key": metric_key, "value": value, "day": "2026-11-10"},
                                     "ninguno", self.cid)

    def test_weight_below_range_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._propone("body_weight_kg", 19.9)

    def test_weight_typo_above_range_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._propone("body_weight_kg", 780.0)

    def test_weight_at_the_range_edges_is_accepted(self):
        self.assertEqual(self._propone("body_weight_kg", 20.0)["estado"], "propuesta")
        self.assertEqual(self._propone("body_weight_kg", 300.0)["estado"], "propuesta")

    def test_waist_below_range_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._propone("waist_cm", 39.9)

    def test_waist_above_range_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._propone("waist_cm", 200.1)

    def test_waist_at_the_range_edges_is_accepted(self):
        self.assertEqual(self._propone("waist_cm", 40.0)["estado"], "propuesta")
        self.assertEqual(self._propone("waist_cm", 200.0)["estado"], "propuesta")

    def test_flex_alcance_cm_has_no_range_check_only_the_negative_one(self):
        self.assertEqual(self._propone("flex_alcance_cm", 999.0)["estado"], "propuesta")
        with self.assertRaises(herramientas.ToolError):
            self._propone("flex_alcance_cm", -1.0)


class CardTextTest(unittest.TestCase):
    """The assistant's card, in both languages, for each new key (body/rutas/asistente.py's
    _tarjeta_registrar_medida, exercised through the shared _tarjeta_propuesta frame)."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)

    def _registro(self, metric_key: str, value: float):
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                    {"metric_key": metric_key, "value": value, "day": "2026-11-10"},
                                    "ninguno", self.cid)
        return out["_registro"]

    def test_body_weight_kg_card_in_spanish(self):
        tarjeta = asistente._tarjeta_propuesta(T("es"), self.defs, self._registro("body_weight_kg", 78.4))
        self.assertIn("Peso", tarjeta["despues"])
        self.assertIn("78", tarjeta["despues"])
        self.assertIn("kg", tarjeta["despues"])

    def test_body_weight_kg_card_in_english(self):
        tarjeta = asistente._tarjeta_propuesta(T("en"), self.defs, self._registro("body_weight_kg", 78.4))
        self.assertIn("Weight", tarjeta["despues"])
        self.assertIn("78", tarjeta["despues"])
        self.assertIn("kg", tarjeta["despues"])

    def test_waist_cm_card_in_spanish(self):
        tarjeta = asistente._tarjeta_propuesta(T("es"), self.defs, self._registro("waist_cm", 88.0))
        self.assertIn("Cintura", tarjeta["despues"])
        self.assertIn("cm", tarjeta["despues"])

    def test_waist_cm_card_in_english(self):
        tarjeta = asistente._tarjeta_propuesta(T("en"), self.defs, self._registro("waist_cm", 88.0))
        self.assertIn("Waist", tarjeta["despues"])
        self.assertIn("cm", tarjeta["despues"])


class TrendsNoGradingColourTest(unittest.TestCase):
    """direction 'neutral' -- same as active_kcal's precedent: no good/bad colour, just 'flat'
    (body/rutas/tendencias.py's _compare)."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "trends_peso.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        d0 = date(2026, 9, 1)
        for i in range(20):
            day = (d0 + timedelta(i)).isoformat()
            db.set_metric(self.conn, day, "resting_hr", 58 - i * 0.1, "garmin", 1)
            db.set_metric(self.conn, day, "body_weight_kg", 80.0 - i * 0.05, "self", 1)
            db.set_metric(self.conn, day, "waist_cm", 90.0 - i * 0.05, "self", 1)
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _card_chunk(self, page: str, key: str) -> str:
        start = page.index(f'id="{key}"')
        end = page.index("</div>", page.index('class="legend mt-4"', start))
        return page[start:end]

    def test_weight_card_renders_with_flat_delta_never_good_or_bad(self):
        chunk = self._card_chunk(self.client.get("/trends?todas=1").text, "body_weight_kg")
        self.assertIn('class="delta flat', chunk)
        self.assertNotIn('class="delta good', chunk)
        self.assertNotIn('class="delta bad', chunk)

    def test_waist_card_renders_with_flat_delta_never_good_or_bad(self):
        chunk = self._card_chunk(self.client.get("/trends?todas=1").text, "waist_cm")
        self.assertIn('class="delta flat', chunk)
        self.assertNotIn('class="delta good', chunk)
        self.assertNotIn('class="delta bad', chunk)

    def test_both_keys_are_in_the_self_group_on_the_page(self):
        page = self.client.get("/trends?todas=1").text
        self.assertIn('id="body_weight_kg"', page)
        self.assertIn('id="waist_cm"', page)


class PrecisionAsDataTest(unittest.TestCase):
    """BODY-MEDIDA-PESO-1-M2 fix 1: weight and waist show one decimal, in the person's own locale
    (comma in es, point in en), through db.DECIMALES_MEDIDA -- read by comun._value, the one place
    that formats a metric's own figure. Every other key (resting_hr here) keeps today's whole
    number: db.DECIMALES_MEDIDA carries no entry for it, so comun._decimales falls back to 0."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "precision.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        d0 = date(2026, 8, 1)
        for i in range(90):
            day = (d0 + timedelta(i)).isoformat()
            db.set_metric(self.conn, day, "resting_hr", 58 - i * 0.01, "garmin", 1)
            if i % 7 == 0:
                db.set_metric(self.conn, day, "body_weight_kg", 70.0 + i * 0.01, "self", 1)
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _fig(self, page: str, key: str) -> str:
        start = page.index(f'id="{key}"')
        i = page.index('class="fig">', start) + len('class="fig">')
        return page[i:page.index("</span>", i)]

    def test_weight_shows_one_decimal_in_spanish(self):
        page = self.client.get("/trends?todas=1").text
        self.assertRegex(self._fig(page, "body_weight_kg"), r"^\d+,\d$")

    def test_weight_shows_one_decimal_in_english(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        page = self.client.get("/trends?todas=1").text
        self.assertRegex(self._fig(page, "body_weight_kg"), r"^\d+\.\d$")

    def test_resting_hr_keeps_a_whole_number(self):
        page = self.client.get("/trends?todas=1").text
        self.assertRegex(self._fig(page, "resting_hr"), r"^\d+$")


class CompareZeroArrowTest(unittest.TestCase):
    """BODY-MEDIDA-PESO-1-M2 fix 3: body/rutas/tendencias.py's _compare never shows an arrow whose
    own amount rounds to "0" at the metric's precision -- it reuses "delta.same" instead, the exact
    wording the spread-based flatness check already uses. A real difference still gets its arrow."""

    def test_diff_rounding_to_zero_at_one_decimal_is_same_as_history_not_an_arrow(self):
        t = T("es")
        text, cls = tendencias._compare(t, "body_weight_kg", {"better": "neutral"},
                                        value=70.04, ref=70.00, spread=0.2, vs_key="trends.vs_hist")
        self.assertEqual(text, t("delta.same"))
        self.assertEqual(cls, "flat")

    def test_diff_rounding_to_zero_at_one_decimal_is_same_as_history_in_english(self):
        t = T("en")
        text, cls = tendencias._compare(t, "body_weight_kg", {"better": "neutral"},
                                        value=70.04, ref=70.00, spread=0.2, vs_key="trends.vs_hist")
        self.assertEqual(text, t("delta.same"))

    def test_diff_rounding_to_zero_at_zero_decimals_is_same_as_history(self):
        """The same fix for a whole-number metric (resting_hr, 0 decimals): a 0.4 diff used to
        print "↑ 0 vs. tu historia" whenever it cleared the spread-based flatness check."""
        t = T("es")
        text, cls = tendencias._compare(t, "resting_hr", {"better": "lower"},
                                        value=58.4, ref=58.0, spread=2, vs_key="trends.vs_hist")
        self.assertEqual(text, t("delta.same"))
        self.assertEqual(cls, "flat")

    def test_a_real_difference_still_gets_its_arrow(self):
        t = T("es")
        text, cls = tendencias._compare(t, "body_weight_kg", {"better": "neutral"},
                                        value=71.0, ref=70.0, spread=0.2, vs_key="trends.vs_hist")
        self.assertIn("↑", text)
        self.assertIn("1,0", text)


class SelfKeyMonthlyChartTest(unittest.TestCase):
    """BODY-MEDIDA-PESO-1-M2 fix 2+4: a weekly self-measured key's monthly chart (the default
    view's "últimos 30 días" card, body/profile.py's month_svg) draws from 2 points in a month --
    SELF_MIN_MONTH_POINTS -- not the near-daily-cadence 7 every Garmin metric still needs; the
    "Toca o desliza..." hint renders only when that chart actually drew. flex_alcance_cm, already
    on main, gets the same rule."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "self_chart.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        d0 = date(2026, 9, 7)
        for i in range(6):  # six weekly readings: three land in September, three in October
            day = (d0 + timedelta(7 * i)).isoformat()
            db.set_metric(self.conn, day, "body_weight_kg", 75.0 - i * 0.2, "self", 1)
            db.set_metric(self.conn, day, "flex_alcance_cm", 10.0 - i * 0.2, "self", 1)
        # resting_hr, one of the default 9: without at least one default card the whole page falls
        # back to soon.html before the self-group cards this test is about ever render.
        for i in range(20):
            db.set_metric(self.conn, (d0 + timedelta(i)).isoformat(), "resting_hr", 58 - i * 0.1, "garmin", 1)
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _chunk(self, page: str, key: str) -> str:
        start = page.index(f'id="{key}"')
        end = page.index("</div>", page.index('class="legend mt-4"', start))
        return page[start:end]

    def test_weekly_self_key_draws_a_chart_with_the_hint_on_the_default_view(self):
        page = self.client.get("/trends?todas=1").text
        hint = T("es")("trends.chart_hint")
        for key in ("body_weight_kg", "flex_alcance_cm"):
            chunk = self._chunk(page, key)
            self.assertIn("<svg", chunk, f"{key} should draw a chart")
            self.assertIn(hint, chunk, f"{key} should show the chart hint")

    def test_weekly_self_key_draws_a_chart_with_the_hint_at_30_days(self):
        page = self.client.get("/trends?r=30&todas=1").text
        hint = T("es")("trends.chart_hint")
        chunk = self._chunk(page, "body_weight_kg")
        self.assertIn("<svg", chunk)
        self.assertIn(hint, chunk)


class SelfKeySinglePointNoChartTest(unittest.TestCase):
    """fix 4's other half: one point is still too few for a chart at any precision -- no chart,
    no hint, for either range."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "self_one_point.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        db.set_metric(self.conn, "2026-11-02", "body_weight_kg", 74.0, "self", 1)
        # resting_hr, one of the default 9, for the same reason as SelfKeyMonthlyChartTest above.
        d0 = date(2026, 10, 1)
        for i in range(20):
            db.set_metric(self.conn, (d0 + timedelta(i)).isoformat(), "resting_hr", 58 - i * 0.1, "garmin", 1)
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _chunk(self, page: str, key: str) -> str:
        start = page.index(f'id="{key}"')
        end = page.index("</div>", page.index('class="legend mt-4"', start))
        return page[start:end]

    def test_single_point_draws_neither_chart_nor_hint(self):
        page = self.client.get("/trends?todas=1").text
        chunk = self._chunk(page, "body_weight_kg")
        hint = T("es")("trends.chart_hint")
        self.assertNotIn("<svg", chunk)
        self.assertNotIn(hint, chunk)


if __name__ == "__main__":
    unittest.main()
