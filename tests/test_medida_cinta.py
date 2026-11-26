"""BODY-MEDIDA-CINTA-1: eleven tape-measured circumference metric_def rows, on the same
propose/apply/undo path `registrar_medida` already gives body_weight_kg/waist_cm
(BODY-MEDIDA-PESO-1) -- each with its own plausible-range refusal (`db.RANGOS_MEDIDA_PERSONAL`),
one decimal (`db.DECIMALES_MEDIDA`), a label and a how-to-measure line in both languages, and one
dedicated Tendencias group ("measures") holding waist_cm alongside them. Invented demo data only,
no real health figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

CINTA_KEYS = (
    "arm_relaxed_left_cm", "arm_relaxed_right_cm", "arm_flexed_left_cm", "arm_flexed_right_cm",
    "chest_cm", "hips_cm", "thigh_left_cm", "thigh_right_cm", "calf_left_cm", "calf_right_cm",
    "neck_cm",
)


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


class RowsExistTest(unittest.TestCase):
    def test_every_key_is_a_generic_self_measured_cm_row(self):
        conn = _conn(self)
        db.init(conn)
        rows = {
            r["key"]: (r["unit"], r["better"], r["kind"], r["origen"])
            for r in conn.execute(
                f"SELECT key, unit, better, kind, origen FROM metric_def "
                f"WHERE key IN ({','.join('?' * len(CINTA_KEYS))})", CINTA_KEYS,
            )
        }
        self.assertEqual(set(rows), set(CINTA_KEYS))
        for key, row in rows.items():
            with self.subTest(key=key):
                self.assertEqual(row, ("cm", "neutral", "measured", "self"))

    def test_rows_arrive_on_an_existing_database(self):
        conn = _conn(self)
        db.init(conn)
        conn.execute(f"DELETE FROM metric_def WHERE key IN ({','.join('?' * len(CINTA_KEYS))})", CINTA_KEYS)
        conn.commit()
        still_there = conn.execute(
            f"SELECT COUNT(*) FROM metric_def WHERE key IN ({','.join('?' * len(CINTA_KEYS))})", CINTA_KEYS,
        ).fetchone()[0]
        self.assertEqual(still_there, 0)
        db.init(conn)  # the next init() call on an existing install
        present = {r["key"] for r in conn.execute(
            f"SELECT key FROM metric_def WHERE key IN ({','.join('?' * len(CINTA_KEYS))})", CINTA_KEYS,
        )}
        self.assertEqual(present, set(CINTA_KEYS))

    def test_each_key_has_one_decimal(self):
        for key in CINTA_KEYS:
            with self.subTest(key=key):
                self.assertEqual(db.DECIMALES_MEDIDA.get(key), 1)


class ProposeApplyUndoTest(unittest.TestCase):
    """The same propose/apply/undo shape body_weight_kg/waist_cm already have, for each new key."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = _cid(self.conn)

    def test_propose_apply_undo_every_key(self):
        for key in CINTA_KEYS:
            with self.subTest(key=key):
                lo, hi = db.RANGOS_MEDIDA_PERSONAL[key]
                value = round((lo + hi) / 2, 1)
                out = herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                            {"metric_key": key, "value": value, "day": "2026-11-10"},
                                            "ninguno", self.cid)
                self.assertEqual(out["estado"], "propuesta")
                resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
                self.assertEqual(resultado["estado"], "aplicada")
                row = self.conn.execute(
                    "SELECT value, source FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
                    "AND metric_key = ?", (key,)).fetchone()
                self.assertEqual((row["value"], row["source"]), (value, "self"))
                deshecho = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
                self.assertEqual(deshecho["estado"], "deshecha")
                row = self.conn.execute(
                    "SELECT value FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
                    "AND metric_key = ?", (key,)).fetchone()
                self.assertIsNone(row)


class RangeRefusalTest(unittest.TestCase):
    """A plausible-range refusal for each key (db.RANGOS_MEDIDA_PERSONAL): a typo-sized figure is a
    ToolError, never a stored point. The range edges themselves are accepted."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = _cid(self.conn)

    def _propone(self, metric_key: str, value: float):
        return herramientas.ejecutar(self.conn, 1, "registrar_medida",
                                     {"metric_key": metric_key, "value": value, "day": "2026-11-10"},
                                     "ninguno", self.cid)

    def test_below_range_is_refused_for_every_key(self):
        for key in CINTA_KEYS:
            with self.subTest(key=key):
                lo, _hi = db.RANGOS_MEDIDA_PERSONAL[key]
                with self.assertRaises(herramientas.ToolError):
                    self._propone(key, lo - 0.1)

    def test_above_range_is_refused_for_every_key(self):
        for key in CINTA_KEYS:
            with self.subTest(key=key):
                _lo, hi = db.RANGOS_MEDIDA_PERSONAL[key]
                with self.assertRaises(herramientas.ToolError):
                    self._propone(key, hi + 0.1)

    def test_range_edges_are_accepted_for_every_key(self):
        for key in CINTA_KEYS:
            with self.subTest(key=key):
                lo, hi = db.RANGOS_MEDIDA_PERSONAL[key]
                self.assertEqual(self._propone(key, float(lo))["estado"], "propuesta")
                self.assertEqual(self._propone(key, float(hi))["estado"], "propuesta")


class CardTextTest(unittest.TestCase):
    """The assistant's card, in both languages, for each new key."""

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

    def test_every_key_has_a_card_in_spanish(self):
        t = T("es")
        for key in CINTA_KEYS:
            with self.subTest(key=key):
                lo, hi = db.RANGOS_MEDIDA_PERSONAL[key]
                value = round((lo + hi) / 2, 1)
                tarjeta = asistente._tarjeta_propuesta(t, self.defs, self._registro(key, value))
                self.assertIn(t("metric." + key), tarjeta["despues"])
                self.assertIn("cm", tarjeta["despues"])

    def test_every_key_has_a_card_in_english(self):
        t = T("en")
        for key in CINTA_KEYS:
            with self.subTest(key=key):
                lo, hi = db.RANGOS_MEDIDA_PERSONAL[key]
                value = round((lo + hi) / 2, 1)
                tarjeta = asistente._tarjeta_propuesta(t, self.defs, self._registro(key, value))
                self.assertIn(t("metric." + key), tarjeta["despues"])
                self.assertIn("cm", tarjeta["despues"])


class TendenciasGroupTest(unittest.TestCase):
    """BODY-MEDIDA-CINTA-1 ruling 3: one group, waist_cm plus the eleven, read as one set; weight
    (body_weight_kg) stays in the generic "self" sweep."""

    def test_measures_group_holds_waist_and_the_eleven(self):
        from body import catalogo_tendencias
        groups = dict(catalogo_tendencias.TREND_GROUPS)
        self.assertIn("measures", groups)
        self.assertEqual(set(groups["measures"]), {"waist_cm", *CINTA_KEYS})

    def test_self_group_no_longer_carries_waist_or_the_eleven(self):
        from body import catalogo_tendencias
        groups = dict(catalogo_tendencias.TREND_GROUPS)
        self_group = set(groups["self"])
        self.assertNotIn("waist_cm", self_group)
        for key in CINTA_KEYS:
            self.assertNotIn(key, self_group)
        self.assertIn("body_weight_kg", self_group)

    def test_every_key_has_an_i18n_label_question_and_plain_text_in_both_languages(self):
        for lang in ("es", "en"):
            t = T(lang)
            for key in ("waist_cm", *CINTA_KEYS):
                with self.subTest(lang=lang, key=key):
                    self.assertNotEqual(t("metric." + key), "metric." + key)
                    self.assertNotEqual(t("trends.q." + key), "trends.q." + key)
                    self.assertNotEqual(t("trends.plain." + key), "trends.plain." + key)
            self.assertNotEqual(t("trends.group.measures"), "trends.group.measures")


if __name__ == "__main__":
    unittest.main()
