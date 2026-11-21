"""body/captura_sueno.py: BODY-NOCHE-SIN-REM-1 -- zero REM reads as a capture gap only on a night
long enough for at least two full sleep cycles; a short night, a missing REM figure, or REM above
zero all read as None alike. Invented values only, no real figures."""
from tests._isolate import TMP  # noqa: F401  (must come before any body import)

import unittest

from body import captura_sueno


class TestEvaluar(unittest.TestCase):
    def test_zero_rem_six_hour_night_is_marked(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 240, "sleep_rem_min": 0, "sleep_awake_min": 30}
        out = captura_sueno.evaluar(vals)
        self.assertEqual(out, {"rem_no_capturado": True, "version": captura_sueno.VERSION})

    def test_zero_rem_two_hour_night_is_none(self):
        vals = {"sleep_deep_min": 60, "sleep_light_min": 60, "sleep_rem_min": 0, "sleep_awake_min": 10}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_rem_missing_key_is_none(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 240, "sleep_awake_min": 30}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_rem_none_value_is_none(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 240, "sleep_rem_min": None, "sleep_awake_min": 30}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_rem_one_minute_is_none(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 239, "sleep_rem_min": 1, "sleep_awake_min": 30}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_awake_minutes_do_not_count_toward_minimum(self):
        vals = {"sleep_deep_min": 50, "sleep_light_min": 50, "sleep_rem_min": 0, "sleep_awake_min": 400}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_rem_above_zero_is_none_even_on_a_long_night(self):
        vals = {"sleep_deep_min": 120, "sleep_light_min": 240, "sleep_rem_min": 5, "sleep_awake_min": 30}
        self.assertIsNone(captura_sueno.evaluar(vals))

    def test_exact_minimum_is_marked(self):
        vals = {"sleep_deep_min": 100, "sleep_light_min": 80, "sleep_rem_min": 0, "sleep_awake_min": 0}
        out = captura_sueno.evaluar(vals)
        self.assertEqual(out, {"rem_no_capturado": True, "version": captura_sueno.VERSION})

    def test_one_minute_under_minimum_is_none(self):
        vals = {"sleep_deep_min": 100, "sleep_light_min": 79, "sleep_rem_min": 0, "sleep_awake_min": 0}
        self.assertIsNone(captura_sueno.evaluar(vals))


class TestModelo(unittest.TestCase):
    def test_modelo_is_versioned_and_keys_are_named(self):
        self.assertEqual(captura_sueno.MODELO["version"], captura_sueno.VERSION)
        self.assertEqual(set(captura_sueno.MODELO), {"version", "rem_minimo_noche_min"})
        self.assertEqual(captura_sueno.MODELO["rem_minimo_noche_min"], 180)


if __name__ == "__main__":
    unittest.main()
