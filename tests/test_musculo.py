"""BODY-CUERPO-ESTADO-MUSCULO-1: body.musculo.compute()'s versioned, as-of-day per-region state --
effective sets (ruling 2), activities-without-sets (ruling 3), the acute/baseline windows and the
five-state precedence (ruling 4-5). Invented demo data only.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import anatomia, db, garmin_adapter, musculo

# An arbitrary anchor day, far from any real calendar concern -- only the deltas below matter.
DAY = date(2027, 3, 15)
ACUTE_START = DAY - timedelta(6)
BASE_END = ACUTE_START - timedelta(1)
BASE_START = BASE_END - timedelta(27)

MUSCLE_CLAVES = frozenset(r["clave"] for r in anatomia.REGIONES if r["tipo"] == "musculo")


def _mkconn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


def _insert_activity(conn, person_id, source_id, day, type_="strength_training", start_local=None):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, ?, NULL, 'garmin')",
        (person_id, source_id, day, start_local or f"{day}T07:00:00", type_))


def _set(set_type="active", category="pecho", exercise="press", reps=8.0,
         weight_value=50.0, weight_unit="kg", duration_s=30.0, start_local="2027-01-01T07:00:00"):
    return garmin_adapter.ExerciseSet(
        set_type=set_type, category=category, exercise=exercise, reps=reps,
        weight_value=weight_value, weight_unit=weight_unit, duration_s=duration_s, start_local=start_local)


def _actividad_con_sets(conn, person_id, source_id, day, categorias, type_="strength_training"):
    """One activity on `day` with one active set per entry in `categorias`."""
    _insert_activity(conn, person_id, source_id, day, type_=type_)
    conn.commit()
    db.save_exercise_sets(conn, person_id, "garmin", source_id, [_set(category=c) for c in categorias])
    conn.commit()


class SinDatosTest(unittest.TestCase):
    def test_no_training_anywhere_gives_sin_datos_for_every_muscle(self):
        conn = _mkconn(self)
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(set(estados), MUSCLE_CLAVES)
        for clave, e in estados.items():
            self.assertEqual(e.estado, "sin_datos", clave)
            self.assertEqual(e.version, musculo.VERSION)

    def test_a_past_day_ignores_training_logged_after_it(self):
        conn = _mkconn(self)
        _actividad_con_sets(conn, 1, "future1", (ACUTE_START + timedelta(3)).isoformat(), ["pecho"] * 3)
        estados = musculo.compute(conn, ACUTE_START.isoformat(), 1)
        self.assertEqual(estados["pecho"].estado, "sin_datos")

    def test_another_persons_sets_never_leak(self):
        conn = _mkconn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra persona')")
        conn.commit()
        _actividad_con_sets(conn, 2, "a1", (DAY - timedelta(1)).isoformat(), ["pecho"] * 5)
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(estados["pecho"].estado, "sin_datos")


class UnaSolaRegionEntrenadaTest(unittest.TestCase):
    def test_training_only_for_pecho_leaves_everything_else_subentrenado(self):
        conn = _mkconn(self)
        _actividad_con_sets(conn, 1, "a1", (DAY - timedelta(1)).isoformat(), ["pecho"] * 3)
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertIn(estados["pecho"].estado, ("en_rango", "recuperando"))
        for clave, e in estados.items():
            if clave != "pecho":
                self.assertEqual(e.estado, "subentrenado", clave)


class SobrecargaTest(unittest.TestCase):
    def test_steady_baseline_then_a_7day_spike_is_sobrecargado(self):
        conn = _mkconn(self)
        for i in range(4):
            d = BASE_START + timedelta(7 * i)
            _actividad_con_sets(conn, 1, f"base{i}", d.isoformat(), ["pecho", "pecho"])
        for i, d in enumerate([ACUTE_START, ACUTE_START + timedelta(1)]):
            _actividad_con_sets(conn, 1, f"spike{i}", d.isoformat(), ["pecho"] * 5)
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(estados["pecho"].base_semanal, 2.0)
        self.assertEqual(estados["pecho"].series_7d, 10)
        self.assertEqual(estados["pecho"].estado, "sobrecargado")

    def test_fewer_than_two_baseline_weeks_gives_no_baseline_and_never_sobrecargado(self):
        conn = _mkconn(self)
        _actividad_con_sets(conn, 1, "base1", BASE_START.isoformat(), ["pecho"] * 10)
        _actividad_con_sets(conn, 1, "acute1", ACUTE_START.isoformat(), ["pecho"] * 10)
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertIsNone(estados["pecho"].base_semanal)
        self.assertNotEqual(estados["pecho"].estado, "sobrecargado")


class RecuperandoTest(unittest.TestCase):
    def test_a_set_yesterday_is_recuperando(self):
        conn = _mkconn(self)
        _actividad_con_sets(conn, 1, "a1", (DAY - timedelta(1)).isoformat(), ["pecho"])
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(estados["pecho"].estado, "recuperando")
        self.assertEqual(estados["pecho"].dias_desde_ultima, 1)

    def test_a_set_today_is_also_recuperando(self):
        conn = _mkconn(self)
        _actividad_con_sets(conn, 1, "a1", DAY.isoformat(), ["pecho"])
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(estados["pecho"].estado, "recuperando")
        self.assertEqual(estados["pecho"].dias_desde_ultima, 0)


class SubentrenadoTest(unittest.TestCase):
    def test_drop_under_half_the_baseline_is_subentrenado(self):
        conn = _mkconn(self)
        for i in range(4):
            d = BASE_START + timedelta(7 * i)
            _actividad_con_sets(conn, 1, f"base{i}", d.isoformat(), ["pecho"] * 4)
        _actividad_con_sets(conn, 1, "acute1", ACUTE_START.isoformat(), ["pecho"])
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(estados["pecho"].base_semanal, 4.0)
        self.assertEqual(estados["pecho"].series_7d, 1)
        self.assertEqual(estados["pecho"].estado, "subentrenado")


class ActividadSinSeriesTest(unittest.TestCase):
    def test_a_run_with_no_sets_lights_the_legs(self):
        conn = _mkconn(self)
        _insert_activity(conn, 1, "run1", ACUTE_START.isoformat(), type_="running")
        conn.commit()
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        for clave in ("cuadriceps", "isquios", "gluteos"):
            self.assertEqual(estados[clave].series_7d, musculo.MODELO["series_equivalentes_actividad"])

    def test_a_run_with_its_own_sets_counts_only_the_sets(self):
        conn = _mkconn(self)
        _actividad_con_sets(conn, 1, "run1", ACUTE_START.isoformat(), ["pecho", "pecho"], type_="running")
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(estados["pecho"].series_7d, 2)
        for clave in ("cuadriceps", "isquios", "gluteos"):
            self.assertEqual(estados[clave].series_7d, 0)

    def test_cardio_type_activity_with_no_sets_lights_nothing(self):
        conn = _mkconn(self)
        _insert_activity(conn, 1, "c1", ACUTE_START.isoformat(), type_="cardio")
        conn.commit()
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        for e in estados.values():
            self.assertEqual(e.series_7d, 0)


class ExcluidasYCorregidasTest(unittest.TestCase):
    def test_excluded_and_non_active_sets_count_for_nothing(self):
        conn = _mkconn(self)
        _insert_activity(conn, 1, "a1", ACUTE_START.isoformat())
        conn.commit()
        sets = [_set(category="pecho"),
                _set(set_type="rest", category=None, exercise=None, reps=None, weight_value=None, weight_unit=None)]
        db.save_exercise_sets(conn, 1, "garmin", "a1", sets)
        db.set_series_excluida(conn, 1, "garmin", "a1", 1, True)
        conn.commit()
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(estados["pecho"].series_7d, 0)

    def test_a_corrected_set_uses_the_persons_categoria(self):
        conn = _mkconn(self)
        _insert_activity(conn, 1, "a1", ACUTE_START.isoformat())
        conn.commit()
        db.save_exercise_sets(conn, 1, "garmin", "a1", [_set(category="pierna")])
        db.correct_exercise_set(conn, 1, "garmin", "a1", 1, category="pecho")
        conn.commit()
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertEqual(estados["pecho"].series_7d, 1)
        for clave in ("cuadriceps", "isquios"):
            self.assertEqual(estados[clave].series_7d, 0)


class BaselineMarcadoTest(unittest.TestCase):
    def test_a_marked_illness_day_is_left_out_of_the_baseline(self):
        conn = _mkconn(self)
        _actividad_con_sets(conn, 1, "base1", BASE_START.isoformat(), ["pecho", "pecho"])
        _actividad_con_sets(conn, 1, "base2", (BASE_START + timedelta(7)).isoformat(), ["pecho", "pecho"])
        db.crear_periodo(conn, 1, "malestar", BASE_START.isoformat(), BASE_START.isoformat())
        estados = musculo.compute(conn, DAY.isoformat(), 1)
        self.assertIsNone(estados["pecho"].base_semanal)


if __name__ == "__main__":
    unittest.main()
