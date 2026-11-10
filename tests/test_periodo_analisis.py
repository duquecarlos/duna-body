"""BODY-PERIODO-1 ruling 3: a marked day (illness, travel, injury) is left OUT of a baseline's
own range and of an experiment's counted days -- before and test alike -- never re-weighted, and
a verdict with too few clean days reads through the existing 'pocos_dias' path, no fourth
filtro_dias key needed. Invented demo data only, no real health figures.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import baselines, db, experimentos


def _mkconn(case: unittest.TestCase, name: str = "pa.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


# ---- baselines.baseline excludes marked days -------------------------------------------------

class BaselineExcludesMarkedDaysTest(unittest.TestCase):
    def test_marked_days_are_dropped_from_the_usual_range(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(baselines.WINDOW_DAYS)
        d = start
        while d < hoy:
            db.set_metric(conn, d.isoformat(), "hrv_last_night", 50.0, "garmin", 1)
            d += timedelta(1)
        marcado_ini = start + timedelta(5)
        marcado_fin = marcado_ini + timedelta(4)  # 5 marked days, each an outlier
        d = marcado_ini
        while d <= marcado_fin:
            db.set_metric(conn, d.isoformat(), "hrv_last_night", 200.0, "garmin", 1)
            d += timedelta(1)
        db.crear_periodo(conn, 1, "malestar", marcado_ini.isoformat(), marcado_fin.isoformat())
        conn.commit()
        b = baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), person_id=1)
        self.assertIsNotNone(b)
        self.assertEqual(b.n, baselines.WINDOW_DAYS - 5)
        self.assertAlmostEqual(b.mean, 50.0)  # the 200.0 outliers never entered the mean

    def test_too_few_clean_days_after_exclusion_reads_as_no_baseline(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        window = 9
        start = hoy - timedelta(window)
        d = start
        while d < hoy:
            db.set_metric(conn, d.isoformat(), "hrv_last_night", 50.0, "garmin", 1)
            d += timedelta(1)
        marcado_fin = start + timedelta(4)  # 5 of the 9 days marked, leaving 4 -- below MIN_DAYS (7)
        pid = db.crear_periodo(conn, 1, "malestar", start.isoformat(), marcado_fin.isoformat())
        conn.commit()
        self.assertIsNone(baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), window=window, person_id=1))
        # Sanity: without the period the same 9 days are enough -- the None above is the exclusion,
        # not some other cause.
        db.borrar_periodo(conn, 1, pid)
        conn.commit()
        self.assertIsNotNone(
            baselines.baseline(conn, "hrv_last_night", hoy.isoformat(), window=window, person_id=1)
        )


# ---- experimentos.dias_que_cuentan excludes marked days ---------------------------------------

class DiasQueCuentanExcludesMarkedDaysTest(unittest.TestCase):
    def test_a_marked_day_is_dropped(self):
        conn = _mkconn(self)
        db.crear_periodo(conn, 1, "malestar", "2026-03-02", "2026-03-02")
        conn.commit()
        dias = experimentos.dias_que_cuentan(
            conn, 1, {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False},
            "2026-03-01", "2026-03-03",
        )
        self.assertEqual(dias, ["2026-03-01", "2026-03-03"])

    def test_a_multi_day_period_drops_every_day_it_covers(self):
        conn = _mkconn(self)
        db.crear_periodo(conn, 1, "viaje", "2026-03-10", "2026-03-12")
        conn.commit()
        dias = experimentos.dias_que_cuentan(
            conn, 1, {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False},
            "2026-03-09", "2026-03-13",
        )
        self.assertEqual(dias, ["2026-03-09", "2026-03-13"])


# ---- experimentos.veredicto excludes marked days from before and test alike -------------------

FILTRO_TODOS = {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False}
BASE_INICIO = date(2026, 11, 9)  # invented figures only, matches body/experimentos.py's own fixture style


def _ducha_fria(metricas: list[str]) -> dict:
    return dict(
        titulo="Ducha fria corta",
        metricas=metricas,
        ventana={"tipo": "dia"},
        filtro_dias=dict(FILTRO_TODOS),
        fecha_inicio=BASE_INICIO.isoformat(),
    )


def _fill_baseline(conn, person_id: int, metric: str, pattern: list[float], dias: int = 120) -> None:
    hasta = BASE_INICIO - timedelta(1)
    desde = BASE_INICIO - timedelta(dias)
    d, i = desde, 0
    while d <= hasta:
        db.set_metric(conn, d.isoformat(), metric, pattern[i % len(pattern)], "garmin", person_id)
        d += timedelta(1)
        i += 1


def _seed_test_days(conn, person_id: int, eid: int, metric: str, values: list[float]) -> list[str]:
    dias = [(BASE_INICIO + timedelta(i)).isoformat() for i in range(len(values))]
    for day, v in zip(dias, values):
        db.set_metric(conn, day, metric, v, "garmin", person_id)
        db.registrar_dia_experimento(conn, eid, person_id, day, True)
    return dias


class VeredictoExcludesMarkedDaysTest(unittest.TestCase):
    def test_a_marked_test_day_never_enters_the_test_mean(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        _fill_baseline(conn, 1, "stress_avg", [45.0, 55.0])  # mean 50, pstdev 5
        dias = _seed_test_days(conn, 1, eid, "stress_avg", [20.0, 20.0, 999.0, 20.0, 20.0])
        marcado = dias[2]
        db.crear_periodo(conn, 1, "lesion", marcado, marcado)
        conn.commit()
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        self.assertNotIn(marcado, v["dias_prueba"])
        self.assertNotIn(marcado, v["dias_excluidos_no_ocurrio"])  # excluded as marked, not "didn't happen"
        m = v["metricas"]["stress_avg"]
        self.assertEqual(m["n_prueba"], 4)
        self.assertAlmostEqual(m["media_prueba"], 20.0)  # the 999 outlier on the marked day never counted

    def test_marked_baseline_days_are_dropped_from_n_base(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        _fill_baseline(conn, 1, "stress_avg", [45.0, 55.0])  # 120 baseline days
        db.crear_periodo(conn, 1, "malestar", (BASE_INICIO - timedelta(10)).isoformat(),
                          (BASE_INICIO - timedelta(6)).isoformat())  # 5 of them marked
        _seed_test_days(conn, 1, eid, "stress_avg", [20.0] * 5)
        conn.commit()
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        self.assertEqual(v["metricas"]["stress_avg"]["n_base"], 115)

    def test_too_few_clean_test_days_reads_pocos_dias(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        _fill_baseline(conn, 1, "stress_avg", [45.0, 55.0])
        dias = _seed_test_days(conn, 1, eid, "stress_avg", [20.0] * 5)
        # 3 of the 5 test days marked -- only 2 clean days remain, below MIN_PRUEBA (3).
        for day in dias[:3]:
            db.crear_periodo(conn, 1, "lesion", day, day)
        conn.commit()
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        m = v["metricas"]["stress_avg"]
        self.assertEqual(m["resultado"], "pocos_dias")
        self.assertEqual(m["n_prueba"], 2)


if __name__ == "__main__":
    unittest.main()
