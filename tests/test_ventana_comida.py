"""body/ventana_comida.py (BODY-AYUNO-1): pure reads of `ingesta.ts_local` -- the day's eating
window, the overnight fast before it, the gap from a day's last meal to that night's sleep, and
the running "fasting now" count. No inference, no write, no new table. Invented data only."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, ventana_comida

PERSON_A = 1
PERSON_B = 2


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


def _meal(conn, person_id: int, day: str, ts_local: str | None, amount: float = 20.0) -> int:
    return db.registrar_ingesta(conn, person_id, day, "protein_g", amount, False, ts_local=ts_local)


def _sleep(conn, person_id: int, day: str, start_local: str, end_local: str) -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, start_local, end_local),
    )


# ---- ventana() ---------------------------------------------------------------------------------

class VentanaTest(unittest.TestCase):
    def test_no_entries_is_none(self):
        conn = _mkconn(self)
        self.assertIsNone(ventana_comida.ventana(conn, PERSON_A, "2026-03-10"))

    def test_window_across_several_timed_entries(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T07:40:00")
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T12:30:00")
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T17:50:00")
        conn.commit()
        v = ventana_comida.ventana(conn, PERSON_A, "2026-03-10")
        self.assertEqual(v.primera, "2026-03-10T07:40:00")
        self.assertEqual(v.ultima, "2026-03-10T17:50:00")
        self.assertAlmostEqual(v.duracion_min, 10 * 60 + 10)
        self.assertEqual(v.entradas, 3)
        self.assertEqual(v.sin_hora, 0)

    def test_null_ts_local_counts_as_eaten_but_never_sets_the_window(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T07:40:00")
        _meal(conn, PERSON_A, "2026-03-10", None)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T17:50:00")
        conn.commit()
        v = ventana_comida.ventana(conn, PERSON_A, "2026-03-10")
        self.assertEqual(v.primera, "2026-03-10T07:40:00")
        self.assertEqual(v.ultima, "2026-03-10T17:50:00")
        self.assertEqual(v.entradas, 3)
        self.assertEqual(v.sin_hora, 1)

    def test_only_null_ts_local_entries_leaves_no_window_but_says_so(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", None)
        _meal(conn, PERSON_A, "2026-03-10", None)
        conn.commit()
        v = ventana_comida.ventana(conn, PERSON_A, "2026-03-10")
        self.assertIsNotNone(v)
        self.assertIsNone(v.primera)
        self.assertIsNone(v.ultima)
        self.assertIsNone(v.duracion_min)
        self.assertEqual(v.entradas, 2)
        self.assertEqual(v.sin_hora, 2)

    def test_one_timed_entry_is_a_window_of_one_meal_not_a_missing_value(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T07:40:00")
        conn.commit()
        v = ventana_comida.ventana(conn, PERSON_A, "2026-03-10")
        self.assertEqual(v.primera, "2026-03-10T07:40:00")
        self.assertEqual(v.ultima, "2026-03-10T07:40:00")
        self.assertEqual(v.duracion_min, 0.0)
        self.assertEqual(v.entradas, 1)

    def test_a_multi_row_meal_counts_as_one_entry_not_one_per_nutrient(self):
        """BODY-COMIDA-1 ruling 5: a `registrar_comida` meal writes one `ingesta` row per
        nutrient, all sharing one `comida_id` -- the window must count the MEAL, never inflate to
        one per row."""
        conn = _mkconn(self)
        db.crear_comida(conn, PERSON_A, "2026-03-10",
                        [{"metric_key": "protein_g", "amount": 30.0, "estimado": True},
                         {"metric_key": "kcal", "amount": 500.0, "estimado": True},
                         {"metric_key": "carbs_g", "amount": 40.0, "estimado": True}],
                        ts_local="2026-03-10T13:00:00")
        conn.commit()
        v = ventana_comida.ventana(conn, PERSON_A, "2026-03-10")
        self.assertEqual(v.entradas, 1)
        self.assertEqual(v.primera, "2026-03-10T13:00:00")
        self.assertEqual(v.ultima, "2026-03-10T13:00:00")

    def test_a_multi_row_meal_plus_a_legacy_single_row_count_two(self):
        conn = _mkconn(self)
        db.crear_comida(conn, PERSON_A, "2026-03-10",
                        [{"metric_key": "protein_g", "amount": 30.0, "estimado": True},
                         {"metric_key": "fat_g", "amount": 10.0, "estimado": True}],
                        ts_local="2026-03-10T08:00:00")
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T19:00:00")
        conn.commit()
        v = ventana_comida.ventana(conn, PERSON_A, "2026-03-10")
        self.assertEqual(v.entradas, 2)

    def test_another_person_never_leaks_in(self):
        conn = _mkconn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        _meal(conn, PERSON_B, "2026-03-10", "2026-03-10T07:40:00")
        conn.commit()
        self.assertIsNone(ventana_comida.ventana(conn, PERSON_A, "2026-03-10"))


# ---- ayuno_previo() -----------------------------------------------------------------------------

class AyunoPrevioTest(unittest.TestCase):
    def test_overnight_gap_across_midnight(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T19:00:00")
        _meal(conn, PERSON_A, "2026-03-11", "2026-03-11T08:00:00")
        conn.commit()
        a = ventana_comida.ayuno_previo(conn, PERSON_A, "2026-03-11")
        self.assertAlmostEqual(a.horas, 13.0)
        self.assertEqual(a.desde, "2026-03-10T19:00:00")
        self.assertEqual(a.hasta, "2026-03-11T08:00:00")

    def test_overnight_gap_across_midnight_regardless_of_the_persons_zone(self):
        """ts_local is already local wall time (BODY-PROTEINA-1) -- the module reads it the same
        way whatever zone it belongs to, since no actual UTC conversion ever happens here."""
        conn = _mkconn(self)
        conn.execute("UPDATE person SET timezone = 'Asia/Tokyo' WHERE id = ?", (PERSON_A,))
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T23:50:00")
        _meal(conn, PERSON_A, "2026-03-11", "2026-03-11T00:10:00")
        conn.commit()
        a = ventana_comida.ayuno_previo(conn, PERSON_A, "2026-03-11")
        self.assertAlmostEqual(a.horas, 20 / 60)

    def test_none_when_the_previous_day_has_no_timed_entry(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-11", "2026-03-11T08:00:00")
        conn.commit()
        self.assertIsNone(ventana_comida.ayuno_previo(conn, PERSON_A, "2026-03-11"))

    def test_none_when_todays_entry_has_no_time(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T19:00:00")
        _meal(conn, PERSON_A, "2026-03-11", None)
        conn.commit()
        self.assertIsNone(ventana_comida.ayuno_previo(conn, PERSON_A, "2026-03-11"))

    def test_none_with_no_entries_at_all(self):
        conn = _mkconn(self)
        self.assertIsNone(ventana_comida.ayuno_previo(conn, PERSON_A, "2026-03-11"))


# ---- comida_a_sueno() ---------------------------------------------------------------------------

class ComidaASuenoTest(unittest.TestCase):
    def test_gap_to_that_nights_sleep(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T18:00:00")
        _sleep(conn, PERSON_A, "2026-03-11", "2026-03-10T22:00:00", "2026-03-11T06:00:00")
        conn.commit()
        a = ventana_comida.comida_a_sueno(conn, PERSON_A, "2026-03-10")
        self.assertAlmostEqual(a.horas, 4.0)
        self.assertEqual(a.desde, "2026-03-10T18:00:00")
        self.assertEqual(a.hasta, "2026-03-10T22:00:00")

    def test_none_without_a_sleep_session(self):
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T18:00:00")
        conn.commit()
        self.assertIsNone(ventana_comida.comida_a_sueno(conn, PERSON_A, "2026-03-10"))

    def test_none_without_a_timed_meal(self):
        conn = _mkconn(self)
        _sleep(conn, PERSON_A, "2026-03-11", "2026-03-10T22:00:00", "2026-03-11T06:00:00")
        conn.commit()
        self.assertIsNone(ventana_comida.comida_a_sueno(conn, PERSON_A, "2026-03-10"))

    def test_sleep_session_read_from_the_wake_day_not_the_meal_day(self):
        """sleep_session.day is the WAKE day (body/db.py's save_day) -- a row stored under this
        same calendar day never answers comida_a_sueno for it."""
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T18:00:00")
        _sleep(conn, PERSON_A, "2026-03-10", "2026-03-09T22:00:00", "2026-03-10T06:00:00")
        conn.commit()
        self.assertIsNone(ventana_comida.comida_a_sueno(conn, PERSON_A, "2026-03-10"))


# ---- ayuno_ahora() ------------------------------------------------------------------------------

class AyunoAhoraTest(unittest.TestCase):
    def test_hours_since_the_last_timed_meal(self):
        from datetime import datetime
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T18:00:00")
        conn.commit()
        horas = ventana_comida.ayuno_ahora(conn, PERSON_A, datetime(2026, 3, 11, 8, 0, 0))
        self.assertAlmostEqual(horas, 14.0)

    def test_picks_the_most_recent_meal_across_days(self):
        from datetime import datetime
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-09", "2026-03-09T18:00:00")
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T08:00:00")
        conn.commit()
        horas = ventana_comida.ayuno_ahora(conn, PERSON_A, datetime(2026, 3, 10, 10, 0, 0))
        self.assertAlmostEqual(horas, 2.0)

    def test_ignores_untimed_entries(self):
        from datetime import datetime
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", None)
        conn.commit()
        self.assertIsNone(ventana_comida.ayuno_ahora(conn, PERSON_A, datetime(2026, 3, 10, 10, 0, 0)))

    def test_none_with_no_meal_ever(self):
        from datetime import datetime
        conn = _mkconn(self)
        self.assertIsNone(ventana_comida.ayuno_ahora(conn, PERSON_A, datetime(2026, 3, 10, 10, 0, 0)))


# ---- never logged -------------------------------------------------------------------------------

class NeverLoggedTest(unittest.TestCase):
    """Every function here is a pure read with no print/log of its own -- a health figure never
    reaches stdout just by calling them (BODY CLAUDE.md: no figure in a log)."""

    def test_no_output_from_any_read(self):
        from datetime import datetime
        conn = _mkconn(self)
        _meal(conn, PERSON_A, "2026-03-10", "2026-03-10T18:00:00")
        _sleep(conn, PERSON_A, "2026-03-11", "2026-03-10T22:00:00", "2026-03-11T06:00:00")
        conn.commit()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ventana_comida.ventana(conn, PERSON_A, "2026-03-10")
            ventana_comida.ayuno_previo(conn, PERSON_A, "2026-03-11")
            ventana_comida.comida_a_sueno(conn, PERSON_A, "2026-03-10")
            ventana_comida.ayuno_ahora(conn, PERSON_A, datetime(2026, 3, 11, 8, 0, 0))
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
