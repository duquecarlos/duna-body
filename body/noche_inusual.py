"""BODY-NOCHE-INUSUAL-1 (ORCH-CARLOS-DELEGATES-TIER-1-1, BODY-INFERENCIA-FC-1-INV slice 2): "this
night looked unusual" -- a passive FLAG, never a proposal and never a cause. Three signals, each
read against the person's OWN 30-day normal (body/baselines.py's Baseline.z, never a population
table, docs/DESIGN.md rules 4, 5, 12):

  - sleep_hr_avg  (body/sueno_fc.py)      -- HIGHER than normal reads as unusual.
  - hrv_last_night (the nightly HRV Garmin already gives) -- LOWER than normal reads as unusual.
  - sleep_score    (Garmin's own score)   -- LOWER than normal reads as unusual.

One signal alone never flags (ruling 2); a night missing a signal's value or baseline simply drops
that signal rather than failing the whole night (ruling 3's "no line, no normal claim either" holds
either way, since the caller only ever renders a line when `evaluar` returns something). This
module never names or guesses a cause (ruling 4) -- it returns which figures were off and in which
direction, nothing else; the words a screen shows around that are body/rutas/hoy.py's job, same
split body/sueno_fc.py already keeps from its own screens.

Computed on read, for the one or few nights a page shows (ruling 6) -- no derived table. The one
thing this module persists is the person's own dismissal ("Ya lo vi"), reusing body/db.py's
`moment` table (free-text `kind`) rather than a new one: already exported and deleted by
body/privacidad.py, so no new census entry is needed there.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import baselines, db

VERSION = "noche-inusual-v1"

MODELO = {
    "version": VERSION,
    # Each signal's OWN fixed unusual direction (never the night's, never a cause -- ruling 4):
    # 'alto' = higher than the person's own normal is what counts as off; 'bajo' = lower.
    "senales": {
        "sleep_hr_avg": "alto",
        "hrv_last_night": "bajo",
        "sleep_score": "bajo",
    },
    # Reuses experimentos.UMBRAL's own number rather than inventing a second magic constant
    # (ruling 2) -- that constant is a standard-error-of-a-DIFFERENCE-of-means bar, a different
    # unit than a single night's own z-score, but 2.0 also reads as a sensible "two standard
    # deviations from your own normal" bar for one night, so the same figure is reused here.
    "umbral_z": 2.0,
    # One signal alone never flags (ruling 2) -- at least two of the three must agree.
    "minimo_senales": 2,
}

# body/db.py's `moment` table is free-text `kind` -- this is this module's own, used only to check
# or record a dismissal, never matched against any other moment (e.g. body/experimentos.py's own
# anchor kinds).
DISMISS_KIND = "noche_inusual_vista"


@dataclass
class Noche:
    wake_day: str
    senales: list[tuple[str, str]]  # (metric_key, direction), each already off


def evaluar(conn, person_id: int, wake_day: str, vals: dict | None = None) -> Noche | None:
    """This one wake day's own unusual-night judgement, or None for a normal OR an unjudgeable
    night (ruling 3) -- the caller cannot tell which, and must not try to: both render nothing.
    `vals` lets a caller that already read this day's metrics (body/rutas/hoy.py's `today`/
    `day_view`, both via db.day_values/db.load_day) pass that dict in, so this adds no SQL beyond
    the one batched baselines() read; a bare call (tests, a single lookup) reads its own."""
    defs = MODELO["senales"]
    if vals is None:
        vals = db.day_values(conn, wake_day, person_id)
    bases = baselines.baselines(conn, list(defs.keys()), wake_day, person_id=person_id)
    umbral = MODELO["umbral_z"]
    fuera: list[tuple[str, str]] = []
    for key, direccion in defs.items():
        v = vals.get(key)
        b = bases.get(key)
        if v is None or b is None:
            continue
        z = b.z(v)
        if z is None:
            continue
        if direccion == "alto" and z > umbral:
            fuera.append((key, direccion))
        elif direccion == "bajo" and z < -umbral:
            fuera.append((key, direccion))
    if len(fuera) < MODELO["minimo_senales"]:
        return None
    return Noche(wake_day, fuera)


def vista(conn, person_id: int, wake_day: str) -> bool:
    """Whether THIS person already dismissed ("Ya lo vi") THIS one night -- per person, per night
    (ruling 5). Another person's dismissal of their own night (or of the same calendar date) never
    reads here, since the lookup is scoped by person_id like every other read in this app."""
    return conn.execute(
        "SELECT 1 FROM moment WHERE person_id = ? AND kind = ? AND ts_local = ?",
        (person_id, DISMISS_KIND, wake_day),
    ).fetchone() is not None


def marcar_vista(conn, person_id: int, wake_day: str) -> None:
    """Records the dismissal. Idempotent in effect -- a repeat call still reads as dismissed either
    way -- so it never inserts a second row for a night already marked."""
    if vista(conn, person_id, wake_day):
        return
    db.crear_momento(conn, person_id, wake_day, DISMISS_KIND)
