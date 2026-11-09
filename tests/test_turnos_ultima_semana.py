"""body/turnos_costo.py's `ultima_semana` (BODY-TURNOS-REFERENCIA-NOCHE-1): the last complete night
block against the person's own earlier night blocks -- like for like, never the day-shift reference
(BODY-TURNOS-SUENO-DIURNO-1-INV, BODY-TURNOS-PALABRA-DIRECCION-1). Also /shifts rendering the new
section in both languages, on invented demo data."""
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, turnos_costo, web
from body.i18n import T

Bloque = turnos_costo.Bloque
MIN = turnos_costo.MIN_BLOQUES_PREVIOS


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


def _bloques_out(bloques: list, recuperaciones: list) -> list[dict]:
    return [{"inicio": b.inicio, "fin": b.fin, "dias": b.dias, "recuperacion_dias": r, "costo": {}}
            for b, r in zip(bloques, recuperaciones)]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


class UltimaSemanaTest(unittest.TestCase):
    """Direct unit tests of the pure `_ultima_semana` helper -- no conn, no Lector, same style as
    CostoBloqueTest/RecuperacionBloqueTest in tests/test_turnos_costo.py."""

    HOY = date(2026, 6, 1)

    def _bloques(self):
        return [
            Bloque(inicio="2026-03-01", fin="2026-03-03", dias=3),
            Bloque(inicio="2026-03-21", fin="2026-03-23", dias=3),
            Bloque(inicio="2026-04-10", fin="2026-04-12", dias=3),
            Bloque(inicio="2026-04-30", fin="2026-05-02", dias=3),  # last: fin+2 <= HOY
        ]

    def test_worse_metric_reads_peor_with_mas_and_better_reads_mejor_with_mas(self):
        bloques = self._bloques()
        medias = [
            {"resting_hr": 55.0, "sleep_minutes": 420.0, "hrv_last_night": 60.0,
             "body_battery_wake": 75.0, "stress_avg": None},
            {"resting_hr": 55.0, "sleep_minutes": 420.0, "hrv_last_night": 60.0,
             "body_battery_wake": 75.0, "stress_avg": None},
            {"resting_hr": 55.0, "sleep_minutes": 420.0, "hrv_last_night": 60.0,
             "body_battery_wake": None, "stress_avg": None},  # body_battery_wake missing here: n=2
            {"resting_hr": 70.0, "sleep_minutes": 480.0, "hrv_last_night": 60.5,
             "body_battery_wake": 80.0, "stress_avg": None},
        ]
        bloques_out = _bloques_out(bloques, [5, 3, None, 2])
        u = turnos_costo._ultima_semana(bloques, bloques_out, medias, self.HOY)
        self.assertIsNotNone(u)
        self.assertEqual(u["inicio"], "2026-04-30")
        self.assertEqual(u["fin"], "2026-05-02")
        self.assertIsNone(u["motivo"])
        self.assertEqual(u["n"], 3)
        por_metrica = {m["metric"]: m for m in u["metricas"]}
        self.assertEqual(por_metrica["resting_hr"]["direccion"], "mas")
        self.assertEqual(por_metrica["resting_hr"]["veredicto"], "peor")
        self.assertEqual(por_metrica["sleep_minutes"]["direccion"], "mas")
        self.assertEqual(por_metrica["sleep_minutes"]["veredicto"], "mejor")
        self.assertEqual(por_metrica["hrv_last_night"]["direccion"], "igual")
        self.assertEqual(por_metrica["hrv_last_night"]["veredicto"], "igual")
        # body_battery_wake had only two earlier blocks with a value (< MIN_BLOQUES_PREVIOS): left out
        self.assertNotIn("body_battery_wake", por_metrica)
        self.assertNotIn("stress_avg", por_metrica)  # no data at all for this metric

    def test_recovery_with_a_figure_uses_last_block_and_median_of_earlier(self):
        bloques = self._bloques()
        medias = [{"resting_hr": 55.0} for _ in range(3)] + [{"resting_hr": 70.0}]
        bloques_out = _bloques_out(bloques, [5, 3, None, 2])
        u = turnos_costo._ultima_semana(bloques, bloques_out, medias, self.HOY)
        self.assertEqual(u["recuperacion_dias"], 2)
        self.assertEqual(u["recuperacion_mediana"], 4)  # median of [5, 3], the None dropped

    def test_recovery_without_a_figure_yet(self):
        bloques = self._bloques()
        medias = [{"resting_hr": 55.0} for _ in range(3)] + [{"resting_hr": 70.0}]
        bloques_out = _bloques_out(bloques, [5, 3, 4, None])  # last block: no recovery figure yet
        u = turnos_costo._ultima_semana(bloques, bloques_out, medias, self.HOY)
        self.assertIsNone(u["recuperacion_dias"])
        self.assertEqual(u["recuperacion_mediana"], 4)  # still reported from the earlier blocks

    def test_zero_metrics_reaching_the_minimum_is_a_gap(self):
        bloques = self._bloques()[1:]  # only two earlier blocks + the last one
        medias = [{"resting_hr": 55.0}, {"resting_hr": 55.0}, {"resting_hr": 70.0}]
        bloques_out = _bloques_out(bloques, [3, None, 2])
        u = turnos_costo._ultima_semana(bloques, bloques_out, medias, self.HOY)
        self.assertIsNotNone(u)
        self.assertEqual(u["metricas"], [])
        self.assertEqual(u["motivo"], "pocos_bloques_previos")

    def test_no_completed_block_at_all_is_none(self):
        bloque = Bloque(inicio="2026-05-30", fin="2026-06-01", dias=3)  # fin + 2 > HOY: not complete
        u = turnos_costo._ultima_semana([bloque], _bloques_out([bloque], [None]), [{"resting_hr": 55.0}], self.HOY)
        self.assertIsNone(u)
        u_empty = turnos_costo._ultima_semana([], [], [], self.HOY)
        self.assertIsNone(u_empty)


class _SeedMixin:
    def _insert_shift(self, conn, person_id: int, day: str) -> None:
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, 'night', 'manual')",
                     (person_id, day))

    def _set_block(self, conn, person_id: int, inicio: date, dias: int, values: dict[str, float]) -> date:
        fin = inicio + timedelta(dias - 1)
        end = fin + timedelta(turnos_costo.MODELO["dias_estables"])
        d = inicio
        while d <= fin:
            self._insert_shift(conn, person_id, d.isoformat())
            d += timedelta(1)
        d = inicio
        while d <= end:
            for m, v in values.items():
                db.set_metric(conn, d.isoformat(), m, v, "garmin", person_id)
            d += timedelta(1)
        return fin


class ResumenUltimaSemanaTest(_SeedMixin, unittest.TestCase):
    """turnos_costo.resumen() wires `ultima_semana` from figures it already reads -- no second DB read."""

    def test_resumen_carries_ultima_semana_from_its_own_blocks(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        normal = {"resting_hr": 55.0, "sleep_minutes": 420.0, "hrv_last_night": 60.0}
        worse = {"resting_hr": 70.0, "sleep_minutes": 420.0, "hrv_last_night": 60.0}
        self._set_block(conn, 1, date(2026, 3, 1), 3, normal)
        self._set_block(conn, 1, date(2026, 3, 21), 3, normal)
        self._set_block(conn, 1, date(2026, 4, 10), 3, normal)
        self._set_block(conn, 1, date(2026, 4, 30), 3, worse)
        r = turnos_costo.resumen(conn, 1, hoy=hoy)
        u = r["ultima_semana"]
        self.assertIsNotNone(u)
        por_metrica = {m["metric"]: m for m in u["metricas"]}
        self.assertEqual(por_metrica["resting_hr"]["veredicto"], "peor")
        self.assertEqual(u["n"], 3)


class WebUltimaSemanaTest(_SeedMixin, unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def _seed_three_and_last(self, conn):
        today = date.today()
        normal = {"resting_hr": 55.0, "sleep_minutes": 420.0, "hrv_last_night": 60.0}
        worse = {"resting_hr": 70.0, "sleep_minutes": 420.0, "hrv_last_night": 60.0}
        self._set_block(conn, 1, today - timedelta(85), 3, normal)
        self._set_block(conn, 1, today - timedelta(60), 3, normal)
        self._set_block(conn, 1, today - timedelta(35), 3, normal)
        self._set_block(conn, 1, today - timedelta(10), 3, worse)
        conn.commit()

    def test_shifts_shows_the_section_in_spanish(self):
        conn = self._reset_db("ultima_es.db")
        self._seed_three_and_last(conn)
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "shifts.ultima.title"), r.text)
        self.assertIn(_t("es", "metric.resting_hr"), r.text)
        self.assertIn(_t("es", "shifts.ultima.word.peor"), r.text)
        self.assertIn(_t("es", "shifts.word.mas"), r.text)

    def test_shifts_shows_the_section_in_english(self):
        conn = self._reset_db("ultima_en.db")
        self._seed_three_and_last(conn)
        self.client.post("/settings/language", data={"lang": "en"})
        try:
            r = self.client.get("/shifts")
            self.assertEqual(r.status_code, 200)
            self.assertIn(_t("en", "shifts.ultima.title"), r.text)
            self.assertIn(_t("en", "shifts.ultima.word.peor"), r.text)
        finally:
            self.client.post("/settings/language", data={"lang": "es"})

    def test_shifts_shows_the_gap_text_with_too_few_earlier_blocks(self):
        conn = self._reset_db("ultima_gap.db")
        today = date.today()
        normal = {"resting_hr": 55.0}
        self._set_block(conn, 1, today - timedelta(60), 3, normal)
        self._set_block(conn, 1, today - timedelta(10), 3, {"resting_hr": 70.0})
        conn.commit()
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "shifts.ultima.faltan", n=MIN), r.text)

    def test_no_section_without_any_completed_block(self):
        conn = self._reset_db("ultima_none.db")
        today = date.today()
        self._set_block(conn, 1, today - timedelta(1), 2, {"resting_hr": 55.0})  # window not complete yet
        conn.commit()
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(_t("es", "shifts.ultima.title"), r.text)


if __name__ == "__main__":
    unittest.main()
