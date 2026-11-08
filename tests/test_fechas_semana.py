"""BODY-CALIDAD-1: fechas.lunes_de(d) -- Monday of d's ISO week, Monday first, no timezone."""
import unittest
from datetime import date

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import fechas


class LunesDeTest(unittest.TestCase):
    def test_a_monday_is_its_own_monday(self):
        self.assertEqual(fechas.lunes_de(date(2026, 11, 9)), date(2026, 11, 9))

    def test_a_sunday_goes_back_to_that_weeks_monday(self):
        self.assertEqual(fechas.lunes_de(date(2026, 11, 8)), date(2026, 11, 2))

    def test_a_year_boundary_stays_within_the_iso_week(self):
        # 2026-01-01 is a Thursday; its Monday is in the previous calendar year.
        self.assertEqual(fechas.lunes_de(date(2026, 1, 1)), date(2025, 12, 29))

    def test_a_leap_day_resolves_to_its_own_weeks_monday(self):
        # 2024-02-29 is a Thursday.
        self.assertEqual(fechas.lunes_de(date(2024, 2, 29)), date(2024, 2, 26))


if __name__ == "__main__":
    unittest.main()
