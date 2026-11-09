"""BODY-ENTRENO-PULL-1: db.py's set-storage helpers (save_exercise_sets, correct_exercise_set,
create_manual_session, exercise_sets) and pull.py's on-demand traer_series. No network: every
Garmin call goes through body.pull.garmin_adapter / body.garmin_adapter, stubbed. Invented demo
data only, no real health figures.
"""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "44" * 32


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


def _insert_activity(conn, person_id, source_id, day, source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', NULL, ?)",
        (person_id, source_id, day, f"{day}T07:00:00", source),
    )


def _set(set_type="active", category="piernas", exercise="sentadilla", reps=8.0,
         weight_value=50.0, weight_unit="kg", duration_s=30.0,
         start_local="2026-11-09T07:00:00"):
    return garmin_adapter.ExerciseSet(
        set_type=set_type, category=category, exercise=exercise, reps=reps,
        weight_value=weight_value, weight_unit=weight_unit, duration_s=duration_s,
        start_local=start_local,
    )


class SaveExerciseSetsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "save.db")
        db.init(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()

    def test_first_save_stores_the_watchs_sets_in_order(self):
        sets = [
            _set(exercise="press"),
            _set(set_type="rest", category=None, exercise=None, reps=None,
                 weight_value=None, weight_unit=None),
            _set(exercise="remo"),
        ]
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", sets)
        rows = self.conn.execute(
            "SELECT set_order, set_type, garmin_exercise FROM workout_set "
            "WHERE person_id = 1 AND activity_source_id = 'a1' ORDER BY set_order"
        ).fetchall()
        self.assertEqual(
            [(r["set_order"], r["set_type"], r["garmin_exercise"]) for r in rows],
            [(1, "active", "press"), (2, "rest", None), (3, "active", "remo")],
        )

    def test_absent_weight_stays_null_with_its_unit(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1",
                               [_set(weight_value=None, weight_unit=None)])
        row = self.conn.execute(
            "SELECT garmin_weight_value, garmin_weight_unit FROM workout_set WHERE person_id = 1"
        ).fetchone()
        self.assertIsNone(row["garmin_weight_value"])
        self.assertIsNone(row["garmin_weight_unit"])

    def test_requires_the_activity_row_to_exist(self):
        with self.assertRaises(Exception):
            db.save_exercise_sets(self.conn, 1, "garmin", "no-existe", [_set()])

    def test_a_correction_survives_a_second_save_with_different_watch_values(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set(exercise="press", reps=8.0)])
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, exercise="press banca", reps=10)
        db.save_exercise_sets(self.conn, 1, "garmin", "a1",
                               [_set(exercise="press distinto", reps=5.0)])
        row = self.conn.execute(
            "SELECT garmin_exercise, garmin_reps, person_exercise, person_reps, "
            "corrected_by_person FROM workout_set WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(row["garmin_exercise"], "press distinto")
        self.assertEqual(row["garmin_reps"], 5.0)
        self.assertEqual(row["person_exercise"], "press banca")
        self.assertEqual(row["person_reps"], 10)
        self.assertEqual(row["corrected_by_person"], 1)

    def test_uncorrected_stale_slot_is_removed_a_corrected_one_stays(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1",
                               [_set(exercise="uno"), _set(exercise="dos")])
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 2, note="me quedo con esta")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set(exercise="uno-nuevo")])
        rows = self.conn.execute(
            "SELECT set_order, garmin_exercise, corrected_by_person FROM workout_set "
            "WHERE person_id = 1 ORDER BY set_order"
        ).fetchall()
        self.assertEqual([r["set_order"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["garmin_exercise"], "uno-nuevo")
        self.assertEqual(rows[1]["garmin_exercise"], "dos")  # untouched, not deleted
        self.assertEqual(rows[1]["corrected_by_person"], 1)


class CorrectExerciseSetTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "correct.db")
        db.init(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()

    def test_creates_the_slot_when_the_watch_missed_it(self):
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 5, exercise="burpee", reps=12)
        row = self.conn.execute(
            "SELECT set_type, person_exercise, person_reps, corrected_by_person, garmin_exercise "
            "FROM workout_set WHERE person_id = 1 AND set_order = 5"
        ).fetchone()
        self.assertEqual(row["set_type"], "active")
        self.assertEqual(row["person_exercise"], "burpee")
        self.assertEqual(row["person_reps"], 12)
        self.assertEqual(row["corrected_by_person"], 1)
        self.assertIsNone(row["garmin_exercise"])

    def test_a_field_left_out_keeps_the_prior_correction(self):
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, exercise="press banca",
                                 reps=10, weight_kg=40.0, note="hombro bien")
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, reps=12)  # only reps given
        row = self.conn.execute(
            "SELECT person_exercise, person_reps, person_weight_kg, person_note "
            "FROM workout_set WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(row["person_exercise"], "press banca")
        self.assertEqual(row["person_reps"], 12)
        self.assertEqual(row["person_weight_kg"], 40.0)
        self.assertEqual(row["person_note"], "hombro bien")

    def test_never_touches_garmin_or_set_type_on_an_existing_slot(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1",
                               [_set(set_type="rest", exercise="pausa")])
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, exercise="en realidad activa")
        row = self.conn.execute(
            "SELECT set_type, garmin_exercise, person_exercise FROM workout_set "
            "WHERE person_id = 1 AND set_order = 1"
        ).fetchone()
        self.assertEqual(row["set_type"], "rest")  # unchanged
        self.assertEqual(row["garmin_exercise"], "pausa")  # unchanged
        self.assertEqual(row["person_exercise"], "en realidad activa")


class ManualSessionTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "manual.db")
        db.init(self.conn)

    def test_manual_session_with_spoken_sets(self):
        source_id = db.create_manual_session(
            self.conn, 1, "2026-11-09", "2026-11-09T18:00:00", "Pierna en casa", duration_s=1200.0
        )
        activity = self.conn.execute(
            "SELECT source, day, start_local, name, duration_s FROM activity "
            "WHERE person_id = 1 AND source_id = ?", (source_id,)
        ).fetchone()
        self.assertEqual(activity["source"], "manual")
        self.assertEqual(activity["day"], "2026-11-09")
        self.assertEqual(activity["name"], "Pierna en casa")
        self.assertEqual(activity["duration_s"], 1200.0)

        db.correct_exercise_set(self.conn, 1, "manual", source_id, 1, exercise="sentadilla",
                                 reps=10, weight_kg=40.0)
        db.correct_exercise_set(self.conn, 1, "manual", source_id, 2, exercise="zancada", reps=12)
        sets = db.exercise_sets(self.conn, 1, "manual", source_id)
        self.assertEqual([s["exercise"] for s in sets], ["sentadilla", "zancada"])
        self.assertEqual(sets[0]["weight_value"], 40.0)
        self.assertEqual(sets[0]["weight_unit"], "kg")
        self.assertTrue(sets[0]["corrected_by_person"])
        self.assertTrue(sets[1]["corrected_by_person"])


class ExerciseSetsReadTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "read.db")
        db.init(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()

    def test_uncorrected_set_displays_the_watchs_values_and_both_originals(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1",
                               [_set(exercise="press", reps=8.0, weight_value=50.0, weight_unit="kg")])
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual(len(sets), 1)
        s = sets[0]
        self.assertFalse(s["corrected_by_person"])
        self.assertEqual(s["exercise"], "press")
        self.assertEqual(s["weight_value"], 50.0)
        self.assertEqual(s["weight_unit"], "kg")
        self.assertEqual(s["garmin"]["exercise"], "press")
        self.assertIsNone(s["person"]["exercise"])

    def test_corrected_set_displays_the_persons_values_and_both_originals(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1",
                               [_set(exercise="press", reps=8.0, weight_value=50.0, weight_unit="kg")])
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, exercise="press banca",
                                 reps=10, weight_kg=55.0)
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        s = sets[0]
        self.assertTrue(s["corrected_by_person"])
        self.assertEqual(s["exercise"], "press banca")
        self.assertEqual(s["weight_value"], 55.0)
        self.assertEqual(s["weight_unit"], "kg")
        self.assertEqual(s["garmin"]["exercise"], "press")  # the watch's original, kept
        self.assertEqual(s["person"]["exercise"], "press banca")


class PersonScopeTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "scope.db")
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        _insert_activity(self.conn, 2, "a1", "2026-11-09")
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set(exercise="press-A")])
        db.save_exercise_sets(self.conn, 2, "garmin", "a1", [_set(exercise="press-B")])

    def test_person_a_cannot_read_person_bs_sets(self):
        sets_a = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual([s["exercise"] for s in sets_a], ["press-A"])
        sets_b = db.exercise_sets(self.conn, 2, "garmin", "a1")
        self.assertEqual([s["exercise"] for s in sets_b], ["press-B"])

    def test_person_a_correcting_never_touches_person_bs_row(self):
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, exercise="press-A-corregido")
        row_b = self.conn.execute(
            "SELECT garmin_exercise, corrected_by_person FROM workout_set WHERE person_id = 2"
        ).fetchone()
        self.assertEqual(row_b["garmin_exercise"], "press-B")
        self.assertEqual(row_b["corrected_by_person"], 0)


class TraerSeriesTest(unittest.TestCase):
    """pull.py's on-demand traer_series: calls garmin_adapter.fetch_exercise_sets and saves with
    db.save_exercise_sets."""

    def setUp(self):
        self.conn = _db(self, "traer.db")
        db.init(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()

    def test_local_mode_saves_and_returns_the_count(self):
        data = garmin_adapter.ExerciseSetsData(activity_id="a1", sets=(_set(exercise="press"),))
        with mock.patch("body.pull.garmin_adapter.fetch_exercise_sets", return_value=data) as m:
            n = pull.traer_series(self.conn, 1, "a1")
        m.assert_called_once_with("a1")
        self.assertEqual(n, 1)
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual([s["exercise"] for s in sets], ["press"])

    def test_hosted_token_path_returns_the_count_and_the_refreshed_token(self):
        data = garmin_adapter.ExerciseSetsData(activity_id="a1", sets=(_set(exercise="press"),))
        with mock.patch("body.pull.garmin_adapter.fetch_exercise_sets",
                         return_value=(data, "tok-after")) as m:
            n, token_after = pull.traer_series(self.conn, 1, "a1", token_str="tok-before")
        m.assert_called_once_with("a1", token_str="tok-before")
        self.assertEqual((n, token_after), (1, "tok-after"))


class TraerSeriesNotCalledByAnyJobTest(unittest.TestCase):
    """traer_series / garmin_adapter.fetch_exercise_sets must not be reachable from pull_all,
    pull_hosted or pull_backfill_hosted -- BODY-ENTRENO-PULL-1 adds no new Garmin traffic to the
    daily loop or the backfill. Stubs the adapter to prove it, via a Mock that must stay
    unvisited."""

    def setUp(self):
        self.conn = _db(self, "jobs.db")
        db.init(self.conn)
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)

    @staticmethod
    def _empty_fetch_day(day, token_str=None):
        data = garmin_adapter.DayData(day=day)
        return data if token_str is None else (data, token_str)

    def test_pull_all_never_calls_fetch_exercise_sets(self):
        person_id = db.local_person_id(self.conn)
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=self._empty_fetch_day), \
             mock.patch("body.pull.garmin_adapter.fetch_exercise_sets") as m_sets:
            pull.pull_all(self.conn, person_id)
        m_sets.assert_not_called()

    def test_pull_hosted_never_calls_fetch_exercise_sets(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=self._empty_fetch_day), \
             mock.patch("body.pull.garmin_adapter.fetch_exercise_sets") as m_sets:
            pull.pull_hosted(self.conn, fecha="2026-11-09")
        m_sets.assert_not_called()

    def test_pull_backfill_hosted_never_calls_fetch_exercise_sets(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=self._empty_fetch_day), \
             mock.patch("body.pull.garmin_adapter.fetch_exercise_sets") as m_sets:
            pull.pull_backfill_hosted(self.conn, hoy=date(2026, 11, 9))
        m_sets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
