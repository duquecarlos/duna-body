"""Axis-label sizing shared by every server-rendered chart (ORCH-CARLOS-DELEGATES-TIER-1-1):
body/rutas/turnos.py's perfil chart, body/profile.py's month/window charts (BODY-REVISION-GRAFICOS-
FIGURA-1 finding 10, which reached this through turnos.py by a function-local import -- the defect
this module fixes), and body/rutas/tendencias.py's racha chart (which carried its own byte-for-byte
copy of the same approximation until now). A character-count estimate of a label's own pixel width
at the app's .tick font-size (real glyph metrics are not available server-side), and the wrap rule a
label too wide for its own slot breaks into two lines at.
"""
from __future__ import annotations

# BODY-TURNOS-PERFIL-LEGIBLE-1 ruling 1: the app's own small-text floor, read from body.css --
# .fs-12 (12px) is the smallest of the general small-text classes (.k is 13px), and the axis text
# (CSS class "tick") is set to this size, never below the ruling's own 11px floor.
TICK_FONT = 12.0
CHAR_W = 0.6 * TICK_FONT  # approx glyph width (px) at the tick font-size -- an approximation from
                          # character count, since real glyph metrics are not available server-side;
                          # generous enough that two labels' approximate boxes never touch.


def label_px(s: str) -> float:
    return len(s) * CHAR_W


def wrap_label(label: str, slot: float) -> list[str]:
    """One line unless `label`'s approximate width (`label_px`, the character-count estimate above)
    exceeds its own slot -- the point pitch minus the label gap -- in which case it breaks at its
    (single) space into two lines, each centred on the point: "day / before", "noche / 1". A label
    with no space to break at (the "+1".."+4" post-offsets) always stays one line."""
    if label_px(label) <= slot or " " not in label:
        return [label]
    i = label.index(" ")
    return [label[:i], label[i + 1:]]
