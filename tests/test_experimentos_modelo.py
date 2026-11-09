"""body/experimentos.py: the whole-day verdict (BODY-EXPERIMENTOS-MODELO-1) -- which days count
(`dias_que_cuentan`), the day offset per metric, the person's own matched baseline, and the
four-outcome verdict (`veredicto`), judged against the standard error of the difference of two
means, never a single day's noise. Invented demo data only, no real health figures."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, experimentos, goals, turnos_costo


def _mkconn(case: unittest.TestCase, name: str = "e.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _insert_shift(conn, person_id: int, day: str, kind: str = "afternoon") -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _fill_afternoon(conn, person_id: int, start: date, end: date, metric: str, pattern: list[float]) -> None:
    """Every day in [start, end] gets an 'afternoon' shift and `metric` cycled through `pattern`."""
    d, i = start, 0
    while d <= end:
        _insert_shift(conn, person_id, d.isoformat(), kind="afternoon")
        db.set_metric(conn, d.isoformat(), metric, pattern[i % len(pattern)], "garmin", person_id)
        d += timedelta(1)
        i += 1


def _registrar_todos(conn, person_id: int, experimento_id: int, dias: list[str]) -> None:
    for day in dias:
        db.registrar_dia_experimento(conn, experimento_id, person_id, day, True)


FILTRO_TARDE = {"turno_en": ["afternoon"], "turno_fuera": None, "excluir_tras_noche": False}
BASE_INICIO = date(2026, 11, 9)  # Carlos's own cold-shower week start (invented figures only)


def _ducha_fria(metricas: list[str], fecha_inicio: str = "2026-11-09",
                 filtro_dias: dict | None = None) -> dict:
    return dict(
        titulo="Ducha fria corta en turno de tarde",
        metricas=metricas,
        ventana={"tipo": "dia"},
        filtro_dias=filtro_dias or dict(FILTRO_TARDE),
        fecha_inicio=fecha_inicio,
    )


class DiasQueCuentanTest(unittest.TestCase):
    def test_turno_en_keeps_only_the_matching_kind(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2026-03-01", kind="afternoon")
        _insert_shift(conn, 1, "2026-03-02", kind="morning")
        dias = experimentos.dias_que_cuentan(
            conn, 1, {"turno_en": ["afternoon"], "turno_fuera": None, "excluir_tras_noche": False},
            "2026-03-01", "2026-03-03",
        )
        self.assertEqual(dias, ["2026-03-01"])

    def test_turno_fuera_drops_the_listed_kind(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2026-03-01", kind="night")
        _insert_shift(conn, 1, "2026-03-02", kind="morning")
        dias = experimentos.dias_que_cuentan(
            conn, 1, {"turno_en": None, "turno_fuera": ["night"], "excluir_tras_noche": False},
            "2026-03-01", "2026-03-02",
        )
        self.assertEqual(dias, ["2026-03-02"])

    def test_a_day_with_no_shift_row_is_not_treated_as_off(self):
        """turno_fuera=['night'] never matches a day with no shift row (it has no kind at all) --
        the same way turnos_costo's bloques_noche only ever looks at rows with kind='night'."""
        conn = _mkconn(self)
        dias = experimentos.dias_que_cuentan(
            conn, 1, {"turno_en": None, "turno_fuera": ["night"], "excluir_tras_noche": False},
            "2026-03-01", "2026-03-01",
        )
        self.assertEqual(dias, ["2026-03-01"])
        dias_en = experimentos.dias_que_cuentan(
            conn, 1, {"turno_en": ["afternoon"], "turno_fuera": None, "excluir_tras_noche": False},
            "2026-03-01", "2026-03-01",
        )
        self.assertEqual(dias_en, [])  # turno_en never matches a missing kind either

    def test_excluir_tras_noche_drops_the_post_night_window(self):
        conn = _mkconn(self)
        for day in ("2026-03-02", "2026-03-03", "2026-03-04"):  # a 3-night block
            _insert_shift(conn, 1, day, kind="night")
        desde, hasta = "2026-03-01", "2026-03-20"
        dias = experimentos.dias_que_cuentan(
            conn, 1, {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": True}, desde, hasta,
        )
        post = turnos_costo.dias_post_noche(conn, 1, desde, hasta)
        self.assertTrue(post)
        self.assertFalse(post & set(dias))  # none of the post-night days made it into dias_que_cuentan

    def test_cold_shower_filter_keeps_afternoon_not_after_a_night(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2026-03-01", kind="night")
        _insert_shift(conn, 1, "2026-03-02", kind="afternoon")  # the day right after the night
        _insert_shift(conn, 1, "2026-03-20", kind="afternoon")  # well clear of the recovery window
        dias = experimentos.dias_que_cuentan(conn, 1, FILTRO_TARDE | {"excluir_tras_noche": True},
                                              "2026-03-01", "2026-03-20")
        self.assertNotIn("2026-03-02", dias)
        self.assertIn("2026-03-20", dias)


class VeredictoDiaTest(unittest.TestCase):
    def _seed_baseline(self, conn, person_id: int, metric: str, pattern: list[float], dias: int = 120):
        hasta = BASE_INICIO - timedelta(1)
        desde = BASE_INICIO - timedelta(dias)
        _fill_afternoon(conn, person_id, desde, hasta, metric, pattern)

    def _seed_test_days(self, conn, person_id: int, eid: int, metric: str, values: list[float],
                         ocurrio: list[bool] | None = None) -> list[str]:
        dias = [(BASE_INICIO + timedelta(i)).isoformat() for i in range(len(values))]
        ocurrio = ocurrio or [True] * len(values)
        for day, v, occ in zip(dias, values, ocurrio):
            _insert_shift(conn, person_id, day, kind="afternoon")
            db.set_metric(conn, day, metric, v, "garmin", person_id)
            if occ:
                db.registrar_dia_experimento(conn, eid, person_id, day, True)
        return dias

    def test_a_clear_drop_against_a_noisy_baseline_reads_mas_bajo(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        self._seed_baseline(conn, 1, "stress_avg", [45.0, 55.0])  # mean 50, pstdev 5
        self._seed_test_days(conn, 1, eid, "stress_avg", [20.0] * 5)
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        m = v["metricas"]["stress_avg"]
        self.assertEqual(m["resultado"], "mas_bajo")
        self.assertEqual(m["n_prueba"], 5)
        self.assertEqual(m["n_base"], 120)
        self.assertAlmostEqual(m["media_base"], 50.0)
        self.assertAlmostEqual(m["media_prueba"], 20.0)

    def test_a_small_change_inside_the_noise_reads_sin_cambio(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        self._seed_baseline(conn, 1, "stress_avg", [45.0, 55.0])  # mean 50, pstdev 5
        self._seed_test_days(conn, 1, eid, "stress_avg", [49.0, 51.0, 50.0, 50.0, 50.0])
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        self.assertEqual(v["metricas"]["stress_avg"]["resultado"], "sin_cambio")

    def test_too_few_test_days_reads_pocos_dias(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        self._seed_baseline(conn, 1, "stress_avg", [45.0, 55.0])
        # Only 2 of 5 days occurred -- below MIN_PRUEBA (3).
        self._seed_test_days(conn, 1, eid, "stress_avg", [20.0] * 5,
                              ocurrio=[True, True, False, False, False])
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        m = v["metricas"]["stress_avg"]
        self.assertEqual(m["resultado"], "pocos_dias")
        self.assertEqual(m["n_prueba"], 2)

    def test_too_short_a_baseline_reads_pocos_dias(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        self._seed_baseline(conn, 1, "stress_avg", [45.0, 55.0], dias=3)  # below baselines.MIN_DAYS (7)
        self._seed_test_days(conn, 1, eid, "stress_avg", [20.0] * 5)
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        m = v["metricas"]["stress_avg"]
        self.assertEqual(m["resultado"], "pocos_dias")
        self.assertLess(m["n_base"], experimentos.MIN_BASE)

    def test_a_zero_variance_baseline_reads_pocos_dias_never_a_division(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        self._seed_baseline(conn, 1, "stress_avg", [50.0])  # every baseline day identical -> sd 0
        self._seed_test_days(conn, 1, eid, "stress_avg", [20.0, 21.0, 19.0, 22.0, 18.0])
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        self.assertEqual(v["metricas"]["stress_avg"]["resultado"], "pocos_dias")

    def test_a_skipped_day_is_excluded_from_both_sides(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        self._seed_baseline(conn, 1, "stress_avg", [45.0, 55.0])
        # The 3rd day gets an outlier value but is never registered as occurred.
        dias = self._seed_test_days(conn, 1, eid, "stress_avg", [20.0, 20.0, 999.0, 20.0, 20.0],
                                     ocurrio=[True, True, False, True, True])
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        self.assertEqual(v["dias_excluidos_no_ocurrio"], [dias[2]])
        self.assertNotIn(dias[2], v["dias_prueba"])
        m = v["metricas"]["stress_avg"]
        self.assertEqual(m["n_prueba"], 4)
        self.assertAlmostEqual(m["media_prueba"], 20.0)  # the 999 outlier never entered the mean

    def test_a_night_metric_is_read_from_the_day_after(self):
        """No value is ever written on a test day's OWN date -- only on the day after (offset +1,
        ORCH-CARLOS-DELEGATES-TIER-1-1 decision (b)). If the lookup wrongly used the practice day
        itself it would find nothing there at all and read 'pocos_dias', not a verdict built on
        40.0."""
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["hrv_last_night"]))
        self._seed_baseline(conn, 1, "hrv_last_night", [57.0, 63.0])  # mean 60, pstdev 3
        dias = [(BASE_INICIO + timedelta(i)).isoformat() for i in range(5)]
        for day in dias:
            _insert_shift(conn, 1, day, kind="afternoon")
            db.registrar_dia_experimento(conn, eid, 1, day, True)
            siguiente = (date.fromisoformat(day) + timedelta(1)).isoformat()
            db.set_metric(conn, siguiente, "hrv_last_night", 40.0, "garmin", 1)  # the night after
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(5))
        m = v["metricas"]["hrv_last_night"]
        self.assertEqual(m["n_prueba"], 5)
        self.assertAlmostEqual(m["media_prueba"], 40.0)
        self.assertEqual(m["resultado"], "mas_bajo")

    def test_another_experiments_test_days_are_kept_out_of_the_baseline(self):
        conn = _mkconn(self)
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        self._seed_baseline(conn, 1, "stress_avg", [45.0, 55.0])  # mean 50, pstdev 5 over 120 days
        otros_dias = [(date(2026, 9, 1) + timedelta(i)).isoformat() for i in range(5)]
        for day in otros_dias:
            db.set_metric(conn, day, "stress_avg", 999.0, "garmin", 1)  # would skew the mean badly
        eid_otro = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"], fecha_inicio="2026-09-01"))
        conn.execute("UPDATE experimento SET fecha_fin = ? WHERE id = ?", ("2026-09-05", eid_otro))
        self._seed_test_days(conn, 1, eid, "stress_avg", [20.0] * 5)
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        m = v["metricas"]["stress_avg"]
        self.assertEqual(m["n_base"], 115)  # 120 - the other experiment's 5 test days
        self.assertLess(m["media_base"], 100.0)  # the 999s never entered this experiment's baseline

    def test_habit_linked_experiment_reads_occurrence_from_habit_check(self):
        conn = _mkconn(self)
        gid = goals.propose(conn, 1, "Meta de sueno", "2026-01-01", kind="milestone", deadline="2027-01-01")
        hid = goals.add_habit(conn, gid, "Ducha fria", {"per": "day", "times": 1})
        eid = db.crear_experimento(conn, 1, habito_id=hid, **_ducha_fria(["stress_avg"]))
        self._seed_baseline(conn, 1, "stress_avg", [45.0, 55.0])
        dias = [(BASE_INICIO + timedelta(i)).isoformat() for i in range(5)]
        for i, day in enumerate(dias):
            _insert_shift(conn, 1, day, kind="afternoon")
            db.set_metric(conn, day, "stress_avg", 20.0, "garmin", 1)
            conn.execute("INSERT INTO habit_check (habit_id, person_id, day, done) VALUES (?, ?, ?, ?)",
                         (hid, 1, day, 1 if i < 4 else 0))  # the 5th day was never done
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO + timedelta(4))
        self.assertEqual(len(v["dias_prueba"]), 4)
        self.assertEqual(v["dias_excluidos_no_ocurrio"], [dias[4]])

    def test_intradia_window_with_no_registered_days_reads_pocos_dias(self):
        """BODY-EXPERIMENTOS-INTRADIA-MODELO-1: 'aun_no' is no longer produced for an intraday
        window -- a day never marked occurred falls out the same way a whole-day experiment's own
        unmarked day does (see test_too_few_test_days_reads_pocos_dias above)."""
        conn = _mkconn(self)
        ventana = {"tipo": "intradia", "ancla": "despertar", "antes_min": None, "despues_min": 60}
        filtro = {"turno_en": None, "turno_fuera": ["night"], "excluir_tras_noche": True}
        eid = db.crear_experimento(conn, 1, titulo="Enraizamiento", metricas=["heart_rate", "stress"],
                                    ventana=ventana, filtro_dias=filtro)
        v = experimentos.veredicto(conn, 1, eid, hoy=BASE_INICIO)
        self.assertEqual(v["metricas"]["heart_rate"]["resultado"], "pocos_dias")
        self.assertEqual(v["metricas"]["stress"]["resultado"], "pocos_dias")
        self.assertEqual(v["dias_prueba"], [])
        self.assertEqual(v["dias_sin_dato"]["prueba"], [])  # no anchor at all -> never even tried

    def test_person_isolation(self):
        conn = _mkconn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        eid = db.crear_experimento(conn, 1, **_ducha_fria(["stress_avg"]))
        with self.assertRaises(ValueError):
            experimentos.veredicto(conn, 2, eid, hoy=BASE_INICIO)


if __name__ == "__main__":
    unittest.main()
