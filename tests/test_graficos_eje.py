"""body/graficos_eje.py: the character-count label-width estimate and wrap rule shared by
body/rutas/turnos.py's perfil chart, body/profile.py's month/window charts and body/rutas/
tendencias.py's racha chart (ORCH-CARLOS-DELEGATES-TIER-1-1, moved out of turnos.py so profile.py
no longer needs a function-local import through body.rutas to reach it)."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import graficos_eje


class ConstantsTest(unittest.TestCase):
    def test_tick_font_meets_the_small_text_floor(self):
        """BODY-TURNOS-PERFIL-LEGIBLE-1 ruling 1: never below the app's own 11px floor."""
        self.assertGreaterEqual(graficos_eje.TICK_FONT, 11.0)

    def test_char_w_is_derived_from_tick_font(self):
        self.assertEqual(graficos_eje.CHAR_W, 0.6 * graficos_eje.TICK_FONT)


class LabelPxTest(unittest.TestCase):
    def test_label_px_is_character_count_times_char_w(self):
        self.assertEqual(graficos_eje.label_px("noche 1"), len("noche 1") * graficos_eje.CHAR_W)

    def test_empty_label_is_zero_width(self):
        self.assertEqual(graficos_eje.label_px(""), 0.0)


class WrapLabelTest(unittest.TestCase):
    def test_short_label_stays_one_line(self):
        self.assertEqual(graficos_eje.wrap_label("+1", 5.0), ["+1"])

    def test_label_with_no_space_never_wraps_even_if_too_wide(self):
        label = "x" * 50
        self.assertEqual(graficos_eje.wrap_label(label, 1.0), [label])

    def test_label_narrower_than_its_slot_stays_one_line(self):
        label = "noche 1"
        wide_slot = graficos_eje.label_px(label) + 1
        self.assertEqual(graficos_eje.wrap_label(label, wide_slot), [label])

    def test_label_wider_than_its_slot_breaks_at_the_space(self):
        label = "noche 1"
        narrow_slot = graficos_eje.label_px(label) - 1
        self.assertEqual(graficos_eje.wrap_label(label, narrow_slot), ["noche", "1"])

    def test_label_at_exactly_the_slot_width_stays_one_line(self):
        label = "noche 1"
        self.assertEqual(graficos_eje.wrap_label(label, graficos_eje.label_px(label)), [label])

    def test_breaks_at_the_first_space_only(self):
        label = "a bc de"
        narrow_slot = graficos_eje.label_px(label) - 1
        self.assertEqual(graficos_eje.wrap_label(label, narrow_slot), ["a", "bc de"])


if __name__ == "__main__":
    unittest.main()
