"""body/ayuno_entreno.py (BODY-AYUNO-ENTRENO-1): how a training session's own start compares to
the person's last meal -- the single-session read `horas_desde_comida`, and the session's own
fasted-vs-fed comparison `comparar`, read against the SAME pool `entreno_sesion.sesiones_anteriores`
already selects. `comparar_tendencias` is Trends' own aggregate, over a window. Invented data only.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import ayuno_entreno, db, entreno_sesion

PERSON_A = 1
PERSON_B = 2


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
    return conn


def _meal(conn, person_id: int, day: str, ts_local: str) -> None:
    db.registrar_ingesta(conn, person_id, day, "protein_g", 20.0, False, ts_local=ts_local)


def _activity(conn, person_id: int, source_id: str, day: str, start_local: str, type_: str = "running",
              name: str | None = None, avg_hr: float | None = None, max_hr: float | None = None,
              calories: float | None = None, source: str = "garmin") -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, avg_hr, max_hr, calories, "
        "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, start_local, type_, name, avg_hr, max_hr, calories, source),
    )


def _actividad(conn, person_id: int, day: str, source_id: str) -> dict:
    a = entreno_sesion.actividad(conn, person_id, day, source_id)
    assert a is not None
    return a


def _contar(conn, patron: str, llamada):
    contador = {"n": 0}
    conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + (patron in sql)))
    try:
        return llamada(), contador["n"]
    finally:
        conn.set_trace_callback(None)


# ---- horas_desde_comida() -----------------------------------------------------------------------

class HorasDesdeComidaTest(unittest.TestCase):
    def test_meal_exactly_at_the_threshold(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-09", "2026-03-09T23:00:00")
        conn.commit()
        horas = ayuno_entreno.horas_desde_comida(conn, PERSON_A, "2026-03-10T07:00:00")
        self.assertAlmostEqual(horas, 8.0)

    def test_meal_after_the_session_start_is_ignored(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T08:00:00")
        conn.commit()
        self.assertIsNone(ayuno_entreno.horas_desde_comida(conn, PERSON_A, "2026-03-10T07:00:00"))

    def test_meal_beyond_the_lookback_window_is_none(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-08", "2026-03-08T06:00:00")  # 37h before start
        conn.commit()
        self.assertIsNone(ayuno_entreno.horas_desde_comida(conn, PERSON_A, "2026-03-10T07:00:00"))

    def test_meal_just_inside_the_lookback_window(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-08", "2026-03-08T19:00:00")  # exactly 36h before start
        conn.commit()
        horas = ayuno_entreno.horas_desde_comida(conn, PERSON_A, "2026-03-10T07:00:00")
        self.assertAlmostEqual(horas, 36.0)

    def test_midnight_crossing(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-09", "2026-03-09T22:00:00")
        conn.commit()
        horas = ayuno_entreno.horas_desde_comida(conn, PERSON_A, "2026-03-10T05:00:00")
        self.assertAlmostEqual(horas, 7.0)

    def test_two_people_never_mix(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_B, "2026-03-09", "2026-03-09T20:00:00")
        conn.commit()
        self.assertIsNone(ayuno_entreno.horas_desde_comida(conn, PERSON_A, "2026-03-10T07:00:00"))

    def test_none_with_no_meal_ever(self):
        conn = _mkconn(self)
        self.assertIsNone(ayuno_entreno.horas_desde_comida(conn, PERSON_A, "2026-03-10T07:00:00"))

    def test_untimed_meals_never_count(self):
        conn = _mkconn(self)
        db.registrar_ingesta(conn, PERSON_A, "2026-03-09", "protein_g", 20.0, False, ts_local=None)
        conn.commit()
        self.assertIsNone(ayuno_entreno.horas_desde_comida(conn, PERSON_A, "2026-03-10T07:00:00"))


# ---- comparar() -----------------------------------------------------------------------------------

def _seed_pool(conn, person_id: int, n_ayunas: int, n_alimentado: int, n_desconocido: int = 0,
               avg_hr_ayunas: float = 150.0, avg_hr_alimentado: float = 130.0, tipo: str = "running",
               nombre: str | None = None) -> None:
    """Earlier sessions of `tipo`, each on its own day starting 2026-01-02, before the "current"
    session this module always places on 2026-02-01: `n_ayunas` with a meal 9h before start (the
    evening before), `n_alimentado` with one 2h before, `n_desconocido` with no meal within the
    lookback window at all."""
    cursor = date(2026, 1, 2)

    def _dia() -> str:
        nonlocal cursor
        d = cursor.isoformat()
        cursor += timedelta(days=1)
        return d

    for i in range(n_ayunas):
        day = _dia()
        _activity(conn, person_id, f"ayunas-{i}", day, f"{day}T07:00:00", type_=tipo, name=nombre,
                  avg_hr=avg_hr_ayunas + i)
        dia_anterior = (date.fromisoformat(day) - timedelta(1)).isoformat()
        _meal(conn, person_id, dia_anterior, f"{dia_anterior}T22:00:00")
    for i in range(n_alimentado):
        day = _dia()
        _activity(conn, person_id, f"alimentado-{i}", day, f"{day}T07:00:00", type_=tipo, name=nombre,
                  avg_hr=avg_hr_alimentado + i)
        _meal(conn, person_id, day, f"{day}T05:00:00")
    if n_desconocido:
        cursor += timedelta(days=3)  # a gap past MODELO['ventana_busqueda_horas'] (36h) from the
                                      # last known meal above, so these truly carry no known hours
    for i in range(n_desconocido):
        day = _dia()
        _activity(conn, person_id, f"desconocido-{i}", day, f"{day}T07:00:00", type_=tipo, name=nombre,
                  avg_hr=140.0)
    conn.commit()


class CompararTest(unittest.TestCase):
    def test_none_when_the_pool_is_empty(self):
        conn = _mkconn(self)
        _activity(conn, PERSON_A, "hoy", "2026-02-01", "2026-02-01T07:00:00")
        conn.commit()
        a = _actividad(conn, PERSON_A, "2026-02-01", "hoy")
        self.assertIsNone(ayuno_entreno.comparar(conn, PERSON_A, a))

    def test_both_buckets_qualify(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=3, n_alimentado=3)
        _activity(conn, PERSON_A, "hoy", "2026-02-01", "2026-02-01T07:00:00")
        conn.commit()
        a = _actividad(conn, PERSON_A, "2026-02-01", "hoy")
        comp = ayuno_entreno.comparar(conn, PERSON_A, a)
        self.assertEqual(comp["version"], ayuno_entreno.MODELO["version"])
        self.assertEqual(comp["n"], {"en_ayunas": 3, "alimentado": 3})
        self.assertIsNotNone(comp["en_ayunas"])
        self.assertIsNotNone(comp["alimentado"])
        self.assertAlmostEqual(comp["en_ayunas"]["avg_hr"], 151.0)       # median of 150, 151, 152
        self.assertAlmostEqual(comp["alimentado"]["avg_hr"], 131.0)     # median of 130, 131, 132

    def test_only_one_bucket_qualifies(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=3, n_alimentado=1)
        _activity(conn, PERSON_A, "hoy", "2026-02-01", "2026-02-01T07:00:00")
        conn.commit()
        a = _actividad(conn, PERSON_A, "2026-02-01", "hoy")
        comp = ayuno_entreno.comparar(conn, PERSON_A, a)
        self.assertEqual(comp["n"], {"en_ayunas": 3, "alimentado": 1})
        self.assertIsNotNone(comp["en_ayunas"])
        self.assertIsNone(comp["alimentado"])

    def test_neither_bucket_qualifies(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=1, n_alimentado=1)
        _activity(conn, PERSON_A, "hoy", "2026-02-01", "2026-02-01T07:00:00")
        conn.commit()
        a = _actividad(conn, PERSON_A, "2026-02-01", "hoy")
        comp = ayuno_entreno.comparar(conn, PERSON_A, a)
        self.assertIsNotNone(comp)
        self.assertIsNone(comp["en_ayunas"])
        self.assertIsNone(comp["alimentado"])

    def test_unknown_hours_sessions_sit_in_neither_bucket(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=3, n_alimentado=3, n_desconocido=4)
        _activity(conn, PERSON_A, "hoy", "2026-02-01", "2026-02-01T07:00:00")
        conn.commit()
        a = _actividad(conn, PERSON_A, "2026-02-01", "hoy")
        comp = ayuno_entreno.comparar(conn, PERSON_A, a)
        self.assertEqual(comp["n"], {"en_ayunas": 3, "alimentado": 3})

    def test_another_persons_pool_never_leaks_in(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_B, n_ayunas=3, n_alimentado=3)
        _activity(conn, PERSON_A, "hoy", "2026-02-01", "2026-02-01T07:00:00")
        conn.commit()
        a = _actividad(conn, PERSON_A, "2026-02-01", "hoy")
        self.assertIsNone(ayuno_entreno.comparar(conn, PERSON_A, a))

    def test_one_meal_read_for_the_whole_pool(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1's own trap: the meal data behind an 8-session pool must
        still cost exactly one `ingesta` read, never one per historical session."""
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=4, n_alimentado=4)
        _activity(conn, PERSON_A, "hoy", "2026-02-01", "2026-02-01T07:00:00")
        conn.commit()
        a = _actividad(conn, PERSON_A, "2026-02-01", "hoy")
        _, n_ingesta = _contar(conn, "FROM ingesta", lambda: ayuno_entreno.comparar(conn, PERSON_A, a))
        self.assertEqual(n_ingesta, 1)


# ---- comparar_tendencias() -------------------------------------------------------------------------

class CompararTendenciasTest(unittest.TestCase):
    def test_none_with_no_sessions_at_all(self):
        conn = _mkconn(self)
        self.assertIsNone(ayuno_entreno.comparar_tendencias(conn, PERSON_A, None, None))

    def test_a_type_with_both_buckets_qualifying_is_reported(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=3, n_alimentado=3)
        conn.commit()
        comp = ayuno_entreno.comparar_tendencias(conn, PERSON_A, None, None)
        self.assertIsNotNone(comp)
        self.assertEqual(len(comp["tipos"]), 1)
        grupo = comp["tipos"][0]
        self.assertEqual(grupo["tipo"], "running")
        self.assertEqual(grupo["n"], {"en_ayunas": 3, "alimentado": 3})

    def test_a_type_under_the_minimum_in_one_bucket_is_left_out(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=3, n_alimentado=1)
        conn.commit()
        comp = ayuno_entreno.comparar_tendencias(conn, PERSON_A, None, None)
        self.assertIsNotNone(comp)
        self.assertEqual(comp["tipos"], [])

    def test_ventana_horas_is_the_median_over_every_known_hour_session(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=3, n_alimentado=3)
        conn.commit()
        comp = ayuno_entreno.comparar_tendencias(conn, PERSON_A, None, None)
        self.assertIsNotNone(comp["ventana_horas"])

    def test_window_bounds_which_sessions_are_read(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=3, n_alimentado=3)
        conn.commit()
        comp = ayuno_entreno.comparar_tendencias(conn, PERSON_A, "2026-06-01", "2026-06-30")
        self.assertIsNone(comp)

    def test_generic_other_bucket_groups_by_its_own_folded_name(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=3, n_alimentado=3, tipo="other", nombre="Baloncesto")
        # a DIFFERENT "other"-typed activity (unrelated name) must never join this group
        _activity(conn, PERSON_A, "o-yoga", "2026-02-20", "2026-02-20T07:00:00", type_="other", name="Yoga")
        conn.commit()
        comp = ayuno_entreno.comparar_tendencias(conn, PERSON_A, None, None)
        self.assertEqual(len(comp["tipos"]), 1)
        self.assertEqual(comp["tipos"][0]["n"], {"en_ayunas": 3, "alimentado": 3})
        self.assertEqual(comp["tipos"][0]["nombre"], "Baloncesto")

    def test_fixed_cost_regardless_of_how_many_sessions_the_window_holds(self):
        conn = _mkconn(self)
        _seed_pool(conn, PERSON_A, n_ayunas=6, n_alimentado=6)
        conn.commit()
        _, n_ingesta = _contar(conn, "FROM ingesta",
                               lambda: ayuno_entreno.comparar_tendencias(conn, PERSON_A, None, None))
        self.assertEqual(n_ingesta, 1)


if __name__ == "__main__":
    unittest.main()
