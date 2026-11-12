"""BODY-ENTRENO-RACHA-SEMANAL-1: `body.entreno_racha.racha_semanal`, the weekly strength-session
count against the plan's floor, tagged by night-shift week. Invented demo data only."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, entreno_racha, garmin_adapter, goals


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)  # already seeds person id 1 ('Me', DEFAULT_PERSON_ID)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _activity(conn, person_id, source_id, day, *, type="strength_training", source="garmin",
              vinculo_manual=None):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, type, source, vinculo_manual) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, type, source, vinculo_manual),
    )


def _set(conn, person_id, source, source_id, *, excluded=False, set_type="active"):
    s = garmin_adapter.ExerciseSet(set_type=set_type, category="pecho", exercise="press banca",
                                    reps=8.0, weight_value=40.0, weight_unit="kg")
    db.save_exercise_sets(conn, person_id, source, source_id, [s])
    if excluded:
        db.set_series_excluida(conn, person_id, source, source_id, 1, True)


def _shift_night(conn, person_id, day):
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, 'night', 'manual')",
                 (person_id, day))


class _ContadorConexion:
    def __init__(self, conn):
        self._conn = conn
        self.llamadas = 0

    def execute(self, *a, **kw):
        self.llamadas += 1
        return self._conn.execute(*a, **kw)

    def __getattr__(self, nombre):
        return getattr(self._conn, nombre)


class RachaSemanalTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_none_when_no_strength_session_at_all(self):
        _activity(self.conn, 1, "a1", "2026-11-02", type="running")
        self.conn.commit()
        self.assertIsNone(entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 9)))

    def test_none_when_no_activity_at_all(self):
        self.assertIsNone(entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 9)))

    def test_type_based_session_counts(self):
        _activity(self.conn, 1, "a1", "2026-11-02", type="strength_training")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2))
        self.assertEqual(out["semanas"][-1]["sesiones"], 1)

    def test_set_based_session_counts_even_without_the_type(self):
        _activity(self.conn, 1, "a1", "2026-11-02", type="other")
        _set(self.conn, 1, "garmin", "a1")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2))
        self.assertEqual(out["semanas"][-1]["sesiones"], 1)

    def test_rest_and_excluded_sets_do_not_make_a_session(self):
        _activity(self.conn, 1, "a1", "2026-11-02", type="other")
        _set(self.conn, 1, "garmin", "a1", excluded=True)
        self.conn.commit()
        self.assertIsNone(entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2)))

    def test_a_linked_watch_manual_pair_counts_once(self):
        # The manual half carries the sets; the watch half names the link. One session, not two.
        _activity(self.conn, 1, "m1", "2026-11-02", type="other", source="manual")
        _set(self.conn, 1, "manual", "m1")
        _activity(self.conn, 1, "w1", "2026-11-02", type="other", source="garmin",
                  vinculo_manual="m1")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2))
        self.assertEqual(out["semanas"][-1]["sesiones"], 1)

    def test_weeks_start_at_the_first_activity_of_any_kind_not_the_first_strength_session(self):
        _activity(self.conn, 1, "a0", "2026-10-19", type="running")  # Monday 2026-10-19
        _activity(self.conn, 1, "a1", "2026-11-02", type="strength_training")  # Monday 2026-11-02
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2))
        self.assertEqual(out["desde"], "2026-10-19")
        self.assertEqual(len(out["semanas"]), 3)  # 10-19, 10-26, 11-02

    def test_weeks_with_zero_sessions_are_present(self):
        _activity(self.conn, 1, "a1", "2026-10-19", type="strength_training")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2))
        sesiones = [s["sesiones"] for s in out["semanas"]]
        self.assertEqual(sesiones, [1, 0, 0])

    def test_dias_historia(self):
        _activity(self.conn, 1, "a1", "2026-10-19", type="strength_training")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2))
        self.assertEqual(out["dias_historia"], 14)

    def test_noche_flag_true_when_the_week_has_a_night_block(self):
        _activity(self.conn, 1, "a1", "2026-10-19", type="strength_training")
        _shift_night(self.conn, 1, "2026-10-21")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 10, 19))
        self.assertTrue(out["semanas"][0]["noche"])

    def test_noche_flag_false_when_the_week_has_no_night_block(self):
        _activity(self.conn, 1, "a1", "2026-10-19", type="strength_training")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 10, 19))
        self.assertFalse(out["semanas"][0]["noche"])

    def test_piso_none_means_no_rachas(self):
        _activity(self.conn, 1, "a1", "2026-11-02", type="strength_training")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2))
        self.assertIsNone(out["semanas"][-1]["alcanza"])
        self.assertNotIn("rachas", out)
        self.assertNotIn("mas_larga", out)
        self.assertNotIn("actual", out)
        self.assertNotIn("terminadas_en_noche", out)

    def test_alcanza_is_sessions_against_piso(self):
        for i in range(3):
            _activity(self.conn, 1, f"a{i}", "2026-11-02", type="strength_training")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, piso=3, hoy=date(2026, 11, 2))
        self.assertTrue(out["semanas"][-1]["alcanza"])
        out2 = entreno_racha.racha_semanal(self.conn, 1, piso=4, hoy=date(2026, 11, 2))
        self.assertFalse(out2["semanas"][-1]["alcanza"])

    def _tres_sesiones(self, person_id, day):
        for i in range(3):
            _activity(self.conn, person_id, f"{day}-{i}", day, type="strength_training")

    def test_mas_larga_and_tie_break_to_the_most_recent(self):
        # Week 1 (10-19): run of 1. Week 2 (10-26): gap. Weeks 3-4 (11-02, 11-09): run of 2.
        self._tres_sesiones(1, "2026-10-19")
        self._tres_sesiones(1, "2026-11-02")
        self._tres_sesiones(1, "2026-11-09")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, piso=3, hoy=date(2026, 11, 16))
        self.assertEqual(out["mas_larga"]["semanas"], 2)
        self.assertEqual(out["mas_larga"]["inicio"], "2026-11-02")

    def test_mas_larga_tie_prefers_the_more_recent_run(self):
        # Two runs of length 1 each, far apart: the later one wins the tie.
        self._tres_sesiones(1, "2026-10-19")
        self._tres_sesiones(1, "2026-11-09")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, piso=3, hoy=date(2026, 11, 16))
        self.assertEqual(out["mas_larga"]["inicio"], "2026-11-09")

    def test_actual_run_tolerates_the_in_progress_current_week(self):
        # The run ends at the week before "hoy"; the current week (hoy's own, mid-week, 0
        # sessions so far) does not break it.
        self._tres_sesiones(1, "2026-11-02")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, piso=3, hoy=date(2026, 11, 10))
        self.assertIsNotNone(out["actual"])
        self.assertEqual(out["actual"]["inicio"], "2026-11-02")

    def test_actual_is_none_when_neither_the_current_nor_the_previous_week_reaches(self):
        self._tres_sesiones(1, "2026-10-19")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, piso=3, hoy=date(2026, 11, 16))
        self.assertIsNone(out["actual"])

    def test_termina_en_noche_and_the_n_de_count(self):
        # Run 1 (10-19): ends into a night week (10-26 has a night) -> termina_en_noche True.
        # Run 2 (11-09): ends into a non-night week -> False. Both are finished (far from "hoy").
        self._tres_sesiones(1, "2026-10-19")
        _shift_night(self.conn, 1, "2026-10-27")
        self._tres_sesiones(1, "2026-11-09")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, piso=3, hoy=date(2026, 11, 23))
        self.assertEqual(len(out["rachas"]), 2)
        primera, segunda = out["rachas"]
        self.assertTrue(primera["termina_en_noche"])
        self.assertFalse(segunda["termina_en_noche"])
        self.assertEqual(out["terminadas_en_noche"], {"n": 1, "de": 2})

    def test_terminadas_en_noche_excludes_the_current_run(self):
        # The current run's own "week after" does not exist yet -- never counted toward
        # terminadas_en_noche, which counts finished runs only.
        self._tres_sesiones(1, "2026-11-09")
        self.conn.commit()
        out = entreno_racha.racha_semanal(self.conn, 1, piso=3, hoy=date(2026, 11, 9))
        self.assertIsNotNone(out["actual"])
        self.assertEqual(out["terminadas_en_noche"], {"n": 0, "de": 0})

    def test_another_persons_activities_are_invisible(self):
        _activity(self.conn, 2, "b1", "2026-11-02", type="strength_training")
        self.conn.commit()
        self.assertIsNone(entreno_racha.racha_semanal(self.conn, 1, hoy=date(2026, 11, 2)))

    def test_statement_count_is_bounded_independent_of_history_length(self):
        conteos = []
        for semanas in (2, 6, 12):
            self.conn.execute("DELETE FROM activity")
            self.conn.execute("DELETE FROM workout_set")
            self.conn.execute("DELETE FROM shift")
            dia = date(2026, 1, 5)
            for i in range(semanas):
                d = (dia + timedelta(weeks=i)).isoformat()
                _activity(self.conn, 1, f"s{i}", d, type="strength_training")
            self.conn.commit()
            contador = _ContadorConexion(self.conn)
            hoy = dia + timedelta(weeks=semanas - 1)
            entreno_racha.racha_semanal(contador, 1, piso=3, hoy=hoy)
            conteos.append(contador.llamadas)
        self.assertEqual(conteos, [conteos[0]] * len(conteos),
                          f"statement count grew with history length: {conteos}")


class RachaPisoTableTest(unittest.TestCase):
    """BODY-TENDENCIAS-RACHA-GRAFICO-1 ruling 1: body/db.py's racha_piso table and its set/get
    functions -- created on a fresh db and on a db built before this slice."""

    def test_fresh_db_has_the_contract_columns(self):
        conn = _conn(self)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(racha_piso)")}
        self.assertEqual(cols, {"person_id", "habit_id", "updated_at"})

    def test_migration_creates_the_table_on_a_db_built_before_this_slice(self):
        # racha_piso has never been part of db.SCHEMA's own executescript, only added by
        # _migrate() -- executing SCHEMA alone already simulates a database built before this
        # slice, with no table to drop first (unlike a column added to an existing table).
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        conn.commit()
        self.assertEqual(
            conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'racha_piso'").fetchall(),
            [])
        db.init(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(racha_piso)")}
        self.assertEqual(cols, {"person_id", "habit_id", "updated_at"})

    def test_migration_is_idempotent(self):
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        conn.commit()
        db.init(conn)
        db.set_racha_piso(conn, 1, None)
        db.init(conn)
        row = conn.execute("SELECT person_id FROM racha_piso WHERE person_id = 1").fetchone()
        self.assertIsNotNone(row)

    def test_set_and_get_round_trip(self):
        conn = _conn(self)
        self.assertIsNone(db.racha_piso_habit_id(conn, 1))
        gid = goals.propose(conn, 1, "G", "2026-10-01", kind="metric", metric_key="resting_hr",
                            direction="lower", target_value=55.0, baseline_value=60.0)
        habit_id = goals.add_habit(conn, gid, "Fuerza", {"per": "week", "times": 3})
        conn.commit()
        db.set_racha_piso(conn, 1, habit_id)
        self.assertEqual(db.racha_piso_habit_id(conn, 1), habit_id)
        db.set_racha_piso(conn, 1, None)
        self.assertIsNone(db.racha_piso_habit_id(conn, 1))

    def test_set_is_scoped_to_one_person(self):
        conn = _conn(self)
        gid = goals.propose(conn, 1, "G", "2026-10-01", kind="metric", metric_key="resting_hr",
                            direction="lower", target_value=55.0, baseline_value=60.0)
        habit_id = goals.add_habit(conn, gid, "Fuerza", {"per": "week", "times": 3})
        conn.commit()
        db.set_racha_piso(conn, 1, habit_id)
        self.assertIsNone(db.racha_piso_habit_id(conn, 2))


if __name__ == "__main__":
    unittest.main()
