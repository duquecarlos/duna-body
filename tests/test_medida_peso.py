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
from body.rutas import asistente, comun


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


if __name__ == "__main__":
    unittest.main()
