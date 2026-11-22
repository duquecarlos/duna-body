"""body/db.py's daily_metric_efectiva view (BODY-NOCHE-SIN-REM-3, ORCH-CARLOS-DELEGATES-TIER-1-1,
recommendation (a) of BODY-NOCHE-SIN-REM-2-INV): a sleep_rem_min row the watch never captured --
stored exactly 0 with that night's own deep + light + 0 minutes reaching
captura_sueno.umbral_min() -- is OMITTED from the view, never turned into NULL and never written,
deleted or rewritten in `daily_metric` itself. This proves the view and captura_sueno.evaluar
agree on every invented night, that the generic readers (day_values/load_day/series) take the
honest-gap path while the raw readers (valores_etapas_garmin/series_garmin) still see the stored
0, that a marked night's baseline and Tendencias window simply exclude it like any other gap, and
that sleep_minutes is untouched. Invented nights only, no real figures, no network."""
from tests._isolate import TMP  # noqa: F401  (must come before any body import)

import statistics
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._db import open_conn

from body import baselines, captura_sueno, db, profile

PERSON_ID = 1


def _conn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


def _seed(conn, day: str, deep, light, rem, awake, person_id: int = PERSON_ID) -> None:
    """None skips that key entirely -- deep/light/rem each on its own, so a grid case can name
    'deep missing' or 'REM missing' without a placeholder value standing in for absence."""
    for key, value in (("sleep_deep_min", deep), ("sleep_light_min", light),
                        ("sleep_rem_min", rem), ("sleep_awake_min", awake)):
        if value is not None:
            db.set_metric(conn, day, key, value, "garmin", person_id)
    conn.commit()


def _view_row(conn, day: str, person_id: int = PERSON_ID):
    return conn.execute(
        "SELECT value FROM daily_metric_efectiva WHERE person_id = ? AND day = ? "
        "AND metric_key = 'sleep_rem_min'", (person_id, day)).fetchone()


def _stored(conn, day: str, person_id: int = PERSON_ID) -> bool:
    return conn.execute(
        "SELECT 1 FROM daily_metric WHERE person_id = ? AND day = ? AND metric_key = 'sleep_rem_min'",
        (person_id, day)).fetchone() is not None


class GridTest(unittest.TestCase):
    """evaluar(raw) is truthy IFF the view omits that night's own sleep_rem_min row -- the spec's
    own grid: REM 0 / 1 / missing; deep + light below, at and above the threshold; deep or light
    missing. A night whose REM was never stored at all has nothing for the view to omit (that is
    an ordinary missing-metric night, not this feature), so for those cases this only checks that
    neither side treats it as a capture gap."""

    CASES = [
        ("rem_zero_below_threshold", 50.0, 50.0, 0.0, 10.0),
        ("rem_zero_at_threshold", 100.0, 80.0, 0.0, 0.0),
        ("rem_zero_above_threshold", 120.0, 240.0, 0.0, 30.0),
        ("rem_one_sum_above_threshold", 120.0, 239.0, 1.0, 30.0),
        ("rem_one_sum_below_threshold", 50.0, 50.0, 1.0, 10.0),
        ("rem_missing_sum_would_clear", 120.0, 240.0, None, 30.0),
        ("rem_missing_sum_below", 50.0, 50.0, None, 10.0),
        ("deep_missing_light_alone_clears", None, 200.0, 0.0, 10.0),
        ("light_missing_deep_alone_clears", 200.0, None, 0.0, 10.0),
        ("deep_and_light_missing", None, None, 0.0, 10.0),
        ("deep_missing_light_insufficient", None, 100.0, 0.0, 10.0),
    ]

    def test_grid(self):
        conn = _conn(self)
        for i, (label, deep, light, rem, awake) in enumerate(self.CASES):
            day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
            _seed(conn, day, deep, light, rem, awake)
            raw = db.valores_etapas_garmin(conn, day, PERSON_ID)
            marcado = captura_sueno.evaluar(raw) is not None
            if not _stored(conn, day):
                self.assertFalse(marcado, msg=label)
                self.assertIsNone(_view_row(conn, day), msg=label)
                continue
            omitido = _view_row(conn, day) is None
            self.assertEqual(omitido, marcado, msg=label)


class ReaderShapeTest(unittest.TestCase):
    """A marked night's REM is absent from day_values / load_day / series and present (0.0)
    through valores_etapas_garmin and series_garmin."""

    DAY = "2026-11-10"

    def setUp(self):
        self.conn = _conn(self)
        _seed(self.conn, self.DAY, 120.0, 240.0, 0.0, 30.0)

    def test_day_values_omits_rem(self):
        vals = db.day_values(self.conn, self.DAY, PERSON_ID)
        self.assertNotIn("sleep_rem_min", vals)
        self.assertEqual(vals["sleep_deep_min"], 120.0)

    def test_load_day_has_no_rem_value(self):
        data = db.load_day(self.conn, self.DAY, PERSON_ID)
        fila = next(m for m in data["metrics"] if m["key"] == "sleep_rem_min")
        self.assertIsNone(fila["value"])

    def test_series_omits_the_day(self):
        serie = db.series(self.conn, "sleep_rem_min", self.DAY, self.DAY, PERSON_ID)
        self.assertEqual(serie, {})

    def test_valores_etapas_garmin_has_the_raw_zero(self):
        raw = db.valores_etapas_garmin(self.conn, self.DAY, PERSON_ID)
        self.assertEqual(raw["sleep_rem_min"], 0.0)
        self.assertEqual(raw["sleep_deep_min"], 120.0)

    def test_series_garmin_has_the_raw_zero(self):
        serie = db.series_garmin(self.conn, "sleep_rem_min", self.DAY, self.DAY, PERSON_ID)
        self.assertEqual(serie, {self.DAY: 0.0})


class SleepMinutesUnchangedTest(unittest.TestCase):
    """sleep_minutes (Garmin's own sleepTimeSeconds, garmin_adapter.py:544) is a separate
    daily_metric row from sleep_rem_min -- the view's WHERE clause tests dm.metric_key =
    'sleep_rem_min' only, so a marked night's own sleep_minutes is untouched."""

    def test_sleep_minutes_value_is_unaffected_by_a_marked_rem_row(self):
        conn = _conn(self)
        day = "2026-11-10"
        total = 120.0 + 240.0 + 0.0 + 30.0
        db.set_metric(conn, day, "sleep_minutes", total, "garmin", PERSON_ID)
        _seed(conn, day, 120.0, 240.0, 0.0, 30.0)
        self.assertEqual(db.series(conn, "sleep_minutes", day, day, PERSON_ID), {day: total})


class BaselineTest(unittest.TestCase):
    """The baseline of REM over a window with one marked night equals the baseline of the other
    nights -- baselines.baseline never sees the marked night's row at all, the same honest gap an
    ordinary missing day already produces."""

    def test_marked_night_is_excluded_like_any_other_gap(self):
        conn = _conn(self)
        start = date(2026, 9, 1)
        unmarked_vals = []
        for i in range(10):
            day = (start + timedelta(days=i)).isoformat()
            rem = 50.0 + i
            _seed(conn, day, 60.0, 60.0, rem, 20.0)
            unmarked_vals.append(rem)
        marcado_day = (start + timedelta(days=10)).isoformat()
        _seed(conn, marcado_day, 120.0, 240.0, 0.0, 30.0)
        hoy = (start + timedelta(days=11)).isoformat()
        base = baselines.baseline(conn, "sleep_rem_min", hoy, window=11, person_id=PERSON_ID)
        self.assertIsNotNone(base)
        self.assertEqual(base.n, len(unmarked_vals))
        self.assertAlmostEqual(base.mean, statistics.fmean(unmarked_vals))


class TendenciasGapTest(unittest.TestCase):
    """Tendencias' own REM window (body/profile.py's metric_profile, the same view) has a gap on
    the marked day, not a zero -- the marked night is not among the days counted."""

    def test_metric_profile_never_counts_the_marked_night(self):
        conn = _conn(self)
        start = date(2026, 8, 1)
        for i in range(14):
            day = (start + timedelta(days=i)).isoformat()
            _seed(conn, day, 60.0, 60.0, 50.0 + i, 20.0)
        marcado_day = (start + timedelta(days=14)).isoformat()
        _seed(conn, marcado_day, 120.0, 240.0, 0.0, 30.0)
        hoy = (start + timedelta(days=15)).isoformat()
        perfil = profile.metric_profile(conn, "sleep_rem_min", hoy, person_id=PERSON_ID, min_days=10)
        self.assertIsNotNone(perfil)
        self.assertEqual(perfil.days, 14)


if __name__ == "__main__":
    unittest.main()
