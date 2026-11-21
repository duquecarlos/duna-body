"""BODY-NOCHE-SIN-REM-1 (ORCH-CARLOS-DELEGATES-TIER-1-1): whether a night's own zero REM reading is
a CAPTURE GAP -- the watch's fit or signal, not the person's sleep -- rather than a real physiological
reading. Field note from the Personal desk, 2026-11-21: a synced night with light/deep/awake and NO
REM at all, with a long early "awake" block during which heart rate and stress stayed at sleep
levels; the night's own `etapas_verificacion` (body/agente/herramientas.py) came back `coincide:
true`, so this is not Body's stage-code map misreading Garmin -- Garmin itself stored a REM total of
zero (body/garmin_adapter.py stores `sleep_rem_min` from `remSleepSeconds`, unedited, Tier 1).

REM recurs in every sleep cycle of roughly 90 minutes, so a full night long enough for at least two
complete cycles (`MODELO['rem_minimo_noche_min']`) that still carries zero REM reads, in practice, as
the watch not capturing it -- never as a judgement on the night and never medical language (CLAUDE.md:
"an honest gap shown plainly, never as medical advice"). A short, nap-length night with zero REM is
simply a night too short to expect a REM cycle at all, not a gap.

Pure read over a dict the caller already holds (`vals`, `db.day_values`'s own shape) -- no SQL of its
own, the same "versioned model, computed on read" shape body/noche_inusual.py and body/reentrada.py
already keep for their own flags. The stored figure itself is never touched (db.py, garmin_adapter.py
are not edited by this module or its callers) -- this only NAMES the gap for a reader that already
has the value.
"""
from __future__ import annotations

VERSION = "captura-sueno-v1"

MODELO = {
    "version": VERSION,
    # Two full ~90-minute sleep cycles, each of which normally ends in REM -- a night this long (or
    # longer) that still reads zero REM is read as a capture gap. A shorter night genuinely may not
    # reach a complete cycle, so zero REM there is not, by itself, evidence of anything.
    "rem_minimo_noche_min": 180,
}


def evaluar(vals: dict) -> dict | None:
    """`vals` is the night's own values dict (db.day_values's shape, or any caller-held dict with the
    same keys) -- never a second read. Returns `{"rem_no_capturado": True, "version": VERSION}` when
    `sleep_rem_min` is exactly 0 AND the night's own deep + light + rem minutes (awake excluded) reach
    `MODELO['rem_minimo_noche_min']`; `None` otherwise -- REM missing entirely, REM above zero, or a
    night too short to expect a complete cycle all read as `None` alike, and this never says which."""
    rem = vals.get("sleep_rem_min")
    if rem != 0:
        return None
    noche_min = (vals.get("sleep_deep_min") or 0) + (vals.get("sleep_light_min") or 0) + rem
    if noche_min < MODELO["rem_minimo_noche_min"]:
        return None
    return {"rem_no_capturado": True, "version": VERSION}
