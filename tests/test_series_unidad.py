"""BODY-SERIES-UNIDAD-1 ruling 2: one bad set never loses the others. `pull.traer_series` drops,
before saving, any set the adapter still returns with a mismatched weight value/unit pair (the
exact shape `db.save_exercise_sets`'s CHECK constraint refuses), counts them, and logs the count
(never a figure) to stderr. No network: every Garmin call goes through
body.pull.garmin_adapter.fetch_exercise_sets, stubbed. Invented demo data only.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, pull


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


def _insert_activity(conn, person_id, source_id, day, source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', NULL, ?)",
        (person_id, source_id, day, f"{day}T07:00:00", source),
    )


def _set(exercise="press", weight_value=50.0, weight_unit="kg"):
    return garmin_adapter.ExerciseSet(
        set_type="active", category="pecho", exercise=exercise, reps=8.0,
        weight_value=weight_value, weight_unit=weight_unit, duration_s=30.0,
        start_local="2026-11-09T07:00:00",
    )


class TraerSeriesDropsMismatchedPairsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "a.db")
        db.init(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()

    def test_a_set_with_a_mismatched_pair_is_dropped_the_others_are_saved(self):
        # a value with no unit -- exactly the shape body/db.py's CHECK refuses -- can no longer
        # come from garmin_adapter._weight itself (ruling 1), but a future/other adapter path
        # could still hand traer_series one; it must never crash the save.
        bueno = _set(exercise="press")
        malo = garmin_adapter.ExerciseSet(set_type="active", exercise="malo",
                                           weight_value=11340.0, weight_unit=None)
        data = garmin_adapter.ExerciseSetsData(activity_id="a1", sets=(bueno, malo))
        with mock.patch("body.pull.garmin_adapter.fetch_exercise_sets", return_value=data):
            buf = io.StringIO()
            with redirect_stderr(buf):
                n = pull.traer_series(self.conn, 1, "a1")
        self.assertEqual(n, 1)
        self.assertIn("series descartadas: 1", buf.getvalue())
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual([s["exercise"] for s in sets], ["press"])

    def test_a_unit_with_no_value_is_also_dropped(self):
        malo = garmin_adapter.ExerciseSet(set_type="active", exercise="malo",
                                           weight_value=None, weight_unit="kg")
        data = garmin_adapter.ExerciseSetsData(activity_id="a1", sets=(malo,))
        with mock.patch("body.pull.garmin_adapter.fetch_exercise_sets", return_value=data):
            buf = io.StringIO()
            with redirect_stderr(buf):
                n = pull.traer_series(self.conn, 1, "a1")
        self.assertEqual(n, 0)
        self.assertIn("series descartadas: 1", buf.getvalue())
        self.assertEqual(db.exercise_sets(self.conn, 1, "garmin", "a1"), [])

    def test_no_mismatched_pair_logs_nothing_and_drops_nothing(self):
        data = garmin_adapter.ExerciseSetsData(activity_id="a1", sets=(_set(exercise="press"),
                                                                        _set(exercise="remo")))
        with mock.patch("body.pull.garmin_adapter.fetch_exercise_sets", return_value=data):
            buf = io.StringIO()
            with redirect_stderr(buf):
                n = pull.traer_series(self.conn, 1, "a1")
        self.assertEqual(n, 2)
        self.assertEqual(buf.getvalue(), "")
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual([s["exercise"] for s in sets], ["press", "remo"])

    def test_hosted_token_path_counts_only_the_saved_sets(self):
        bueno = _set(exercise="press")
        malo = garmin_adapter.ExerciseSet(set_type="active", exercise="malo",
                                           weight_value=11340.0, weight_unit=None)
        data = garmin_adapter.ExerciseSetsData(activity_id="a1", sets=(bueno, malo))
        with mock.patch("body.pull.garmin_adapter.fetch_exercise_sets",
                         return_value=(data, "tok-after")):
            n, token_after = pull.traer_series(self.conn, 1, "a1", token_str="tok-before")
        self.assertEqual((n, token_after), (1, "tok-after"))


if __name__ == "__main__":
    unittest.main()
