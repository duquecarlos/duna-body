"""BODY-SERIES-GRAMOS-1: `db.exercise_sets` converts a watch set's raw weight to kilograms for
display (ruling 1) -- unit "g" -> value / 1000, "lb"/"lbs" -> value x 0.45359237, "kg" as is,
rounded to 0.1 kg. The raw pair stays available under the row's `garmin` sub-dict, untouched. An
unknown unit, or a bodyweight set with no weight at all, shows no weight rather than a guessed
number. Invented demo data only."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


def _insert_activity(conn, person_id, source_id, day):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', NULL, 'garmin')",
        (person_id, source_id, day, f"{day}T07:00:00"),
    )


def _set(exercise, weight_value, weight_unit):
    return garmin_adapter.ExerciseSet(
        set_type="active", category="pecho", exercise=exercise, reps=8.0,
        weight_value=weight_value, weight_unit=weight_unit, duration_s=30.0,
        start_local="2026-11-09T07:00:00",
    )


class SeriesGramosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "a.db")
        db.init(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-09")
        self.conn.commit()

    def _guardar(self, sets):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", sets)
        self.conn.commit()
        return db.exercise_sets(self.conn, 1, "garmin", "a1")

    def test_grams_convert_to_kilograms(self):
        sets = self._guardar([_set("press", 11340.0, "g")])
        self.assertEqual(sets[0]["weight_value"], 11.3)
        self.assertEqual(sets[0]["weight_unit"], "kg")
        self.assertEqual(sets[0]["garmin"]["weight_value"], 11340.0)
        self.assertEqual(sets[0]["garmin"]["weight_unit"], "g")

    def test_pounds_convert_to_kilograms(self):
        sets = self._guardar([_set("press", 25.0, "lb")])
        self.assertEqual(sets[0]["weight_value"], 11.3)
        self.assertEqual(sets[0]["weight_unit"], "kg")

    def test_pounds_plural_unit_also_converts(self):
        sets = self._guardar([_set("press", 25.0, "lbs")])
        self.assertEqual(sets[0]["weight_value"], 11.3)
        self.assertEqual(sets[0]["weight_unit"], "kg")

    def test_kilograms_pass_through(self):
        sets = self._guardar([_set("press", 20.0, "kg")])
        self.assertEqual(sets[0]["weight_value"], 20.0)
        self.assertEqual(sets[0]["weight_unit"], "kg")

    def test_bodyweight_set_has_no_weight(self):
        sets = self._guardar([_set("dominadas", None, None)])
        self.assertIsNone(sets[0]["weight_value"])
        self.assertIsNone(sets[0]["weight_unit"])

    def test_unknown_unit_shows_no_weight_but_keeps_the_raw_pair(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("press", 60.0, "kg")])
        self.conn.commit()
        with self.conn:
            self.conn.execute(
                "UPDATE workout_set SET garmin_weight_unit = 'STONE' WHERE person_id = 1 "
                "AND activity_source = 'garmin' AND activity_source_id = 'a1' AND set_order = 1",
            )
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertIsNone(sets[0]["weight_value"])
        self.assertIsNone(sets[0]["weight_unit"])
        self.assertEqual(sets[0]["garmin"]["weight_value"], 60.0)
        self.assertEqual(sets[0]["garmin"]["weight_unit"], "STONE")

    def test_a_person_corrected_set_is_already_kilograms_and_is_untouched(self):
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_set("press", 11340.0, "g")])
        self.conn.commit()
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, weight_kg=12.5)
        self.conn.commit()
        sets = db.exercise_sets(self.conn, 1, "garmin", "a1")
        self.assertEqual(sets[0]["weight_value"], 12.5)
        self.assertEqual(sets[0]["weight_unit"], "kg")
        self.assertTrue(sets[0]["corrected_by_person"])


if __name__ == "__main__":
    unittest.main()
