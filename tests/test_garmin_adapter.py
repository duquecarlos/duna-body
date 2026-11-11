"""BODY-SERIES-UNIDAD-1 ruling 1: `garmin_adapter._weight` always returns value and unit as a
pair -- never a value without a unit, never a unit without a value. Invented payloads only."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import garmin_adapter


class WeightPairTest(unittest.TestCase):
    def test_no_weight_key_is_a_bodyweight_set(self):
        self.assertEqual(garmin_adapter._weight({}), (None, None))

    def test_explicit_null_weight_is_a_bodyweight_set(self):
        self.assertEqual(garmin_adapter._weight({"weight": None}), (None, None))

    def test_weight_zero_is_a_bodyweight_set(self):
        self.assertEqual(garmin_adapter._weight({"weight": 0}), (None, None))

    def test_positive_weight_with_no_unit_key_gets_unit_grams(self):
        # the failing payload from the 9 Nov strength session (BODY-ENTRENO-1-INV): a positive
        # weight, no weightUnit at all -- a 25 lb dumbbell arrives as about 11340 grams.
        self.assertEqual(garmin_adapter._weight({"weight": 11340}), (11340.0, "g"))

    def test_positive_weight_with_a_stated_unit_is_kept_as_sent(self):
        self.assertEqual(garmin_adapter._weight({"weight": 60, "weightUnit": "KILOGRAM"}),
                          (60.0, "KILOGRAM"))

    def test_never_a_value_without_a_unit_or_a_unit_without_a_value(self):
        for payload in ({}, {"weight": None}, {"weight": 0}, {"weight": 11340},
                         {"weight": 60, "weightUnit": "KILOGRAM"}):
            value, unit = garmin_adapter._weight(payload)
            self.assertEqual(value is None, unit is None)


if __name__ == "__main__":
    unittest.main()
