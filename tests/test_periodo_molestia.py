"""BODY-PLAN-FASES-4: 'molestia' (training through a mild issue) through the assistant's own
tools -- marcar_periodo/corregir_periodo accept it with a zona exactly as they accept 'lesion'
(ruling 3), its card and Hoy's own line say its days stay in the usual range instead of being left
out (ruling 4), and switching an open 'lesion' period to 'molestia' through corregir_periodo brings
the person's own 30-day range back with no data written except the period row (ruling 6). Invented
demo data only, no real health figures.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

DAY = "2026-11-10"
PERSON_A = 1


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


# ---- marcar_periodo/corregir_periodo accept 'molestia' with a zona, like 'lesion' ----------------

class MarcarPeriodoMolestiaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_apply_creates_a_molestia_period_with_zonas(self):
        out = herramientas.ejecutar(
            self.conn, 1, "marcar_periodo", {"kind": "molestia", "start_day": DAY, "zonas": ["hombro"]},
            "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        row = self.conn.execute("SELECT kind, zonas FROM period WHERE person_id = 1").fetchone()
        self.assertEqual(row["kind"], "molestia")
        self.assertEqual(json.loads(row["zonas"]), ["hombro"])

    def test_molestia_period_does_not_mark_its_days(self):
        out = herramientas.ejecutar(
            self.conn, 1, "marcar_periodo", {"kind": "molestia", "start_day": "2026-11-01", "end_day": "2026-11-03"},
            "ninguno", self.cid,
        )
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-05"), set())


# ---- the card and Hoy's own line (ruling 4): excluye_del_rango decides the wording ----------------

class CardTextMolestiaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, accion: str, params: dict) -> dict:
        out = herramientas.ejecutar(self.conn, 1, accion, params, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_marcar_periodo_molestia_card_es_says_days_still_count(self):
        t = self._tarjeta("es", "marcar_periodo", {"kind": "molestia", "start_day": DAY})
        self.assertIn("Molestia", t["despues"])
        self.assertIn("siguen contando en tu rango habitual", t["despues"])
        self.assertNotIn("dejará esos días fuera", t["despues"])
        self.assertNotIn("None", t["despues"])

    def test_marcar_periodo_molestia_card_en_says_days_still_count(self):
        t = self._tarjeta("en", "marcar_periodo", {"kind": "molestia", "start_day": DAY})
        self.assertIn("Training through it", t["despues"])
        self.assertIn("still count toward your usual range", t["despues"])
        self.assertNotIn("leave those days out", t["despues"])
        self.assertNotIn("None", t["despues"])

    def test_marcar_periodo_lesion_card_keeps_todays_wording(self):
        t = self._tarjeta("es", "marcar_periodo", {"kind": "lesion", "start_day": DAY})
        self.assertIn("dejará esos días fuera", t["despues"])
        self.assertNotIn("siguen contando", t["despues"])

    def test_corregir_periodo_from_lesion_to_molestia_switches_the_wording(self):
        pid = db.crear_periodo(self.conn, 1, "lesion", DAY)
        self.conn.commit()
        t = self._tarjeta("es", "corregir_periodo", {"period_id": pid, "kind": "molestia"})
        self.assertIn("dejará esos días fuera", t["antes"])
        self.assertIn("siguen contando", t["despues"])


# ---- ruling 6: switching an open period from 'lesion' to 'molestia' brings the range back ---------

class CorregirPeriodoBringsRangeBackTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_switching_kind_restores_dias_marcados_writing_only_the_period_row(self):
        hoy = date(2026, 11, 20)
        inicio = (hoy - timedelta(10)).isoformat()
        pid = db.crear_periodo(self.conn, 1, "lesion", inicio, None)
        self.conn.commit()
        other_tables = ("goal", "habit", "daily_metric", "activity", "checkin", "comida")
        antes_counts = {tbl: self.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] for tbl in other_tables}

        marcados_antes = db.dias_marcados(self.conn, 1, inicio, hoy.isoformat())
        self.assertIn(inicio, marcados_antes, "the open 'lesion' period should start out marked")

        out = herramientas.ejecutar(
            self.conn, 1, "corregir_periodo", {"period_id": pid, "kind": "molestia"}, "ninguno", self.cid,
        )
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")

        marcados_despues = db.dias_marcados(self.conn, 1, inicio, hoy.isoformat())
        self.assertEqual(marcados_despues, set(), "switching to 'molestia' should bring the range back at once")

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0], 1)
        row = self.conn.execute("SELECT kind, start_day, end_day FROM period WHERE id = ?", (pid,)).fetchone()
        self.assertEqual((row["kind"], row["start_day"], row["end_day"]), ("molestia", inicio, None))
        despues_counts = {tbl: self.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] for tbl in other_tables}
        self.assertEqual(antes_counts, despues_counts, "no table other than period's own row should change")


# ---- Hoy's own one-line note (ruling 4), through the real route -----------------------------------

class HoyPeriodoMolestiaTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_open_molestia_period_shows_the_stays_in_range_line(self):
        conn = self._reset_db("hoy_molestia.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.crear_periodo(conn, PERSON_A, "molestia", (hoy - timedelta(5)).isoformat(), None)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "periodo.kind.molestia"), r.text)
        self.assertIn("siguen contando en tu rango habitual", r.text)
        self.assertNotIn("deja esos días fuera", r.text)
        self.assertNotIn("None", r.text)

    def test_open_lesion_period_keeps_todays_line(self):
        conn = self._reset_db("hoy_lesion_sigue.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.crear_periodo(conn, PERSON_A, "lesion", (hoy - timedelta(5)).isoformat(), None)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("deja esos días fuera de tu rango habitual", r.text)
        self.assertNotIn("siguen contando", r.text)

    def test_renders_in_english(self):
        conn = self._reset_db("hoy_molestia_en.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.crear_periodo(conn, PERSON_A, "molestia", (hoy - timedelta(1)).isoformat(), None)
        db.set_language(conn, "en", PERSON_A)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("en", "periodo.kind.molestia"), r.text)
        self.assertIn("still count toward your usual range", r.text)
        self.assertNotIn("None", r.text)


if __name__ == "__main__":
    unittest.main()
