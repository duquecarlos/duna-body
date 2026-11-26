"""Baselines and readiness v1 on synthetic days (no Garmin, temp database)."""
import tempfile
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from datetime import date, timedelta
from pathlib import Path

from tests._db import open_conn

from body import baselines, db, readiness
from body.lector import Lector


def make_db(case: unittest.TestCase, days=31, hrv=50.0, rhr=55.0, stress=30.0):
    conn = open_conn(case, Path(tempfile.mkdtemp()) / "t.db")
    db.init(conn)
    start = date(2026, 10, 1)
    for i in range(days):
        d = (start + timedelta(i)).isoformat()
        wobble = (-1) ** i  # alternating +/-1 so the spread is not zero
        for k, v in (("hrv_last_night", hrv + 2 * wobble), ("resting_hr", rhr + wobble),
                     ("stress_avg", stress + 3 * wobble), ("sleep_minutes", 450), ("sleep_need_min", 480),
                     ("body_battery_wake", 75)):
            db.set_metric(conn, d, k, v, "garmin", 1)
    conn.commit()
    return conn, (start + timedelta(days - 1)).isoformat()


class ReadinessTest(unittest.TestCase):
    def test_baseline_excludes_the_day_and_needs_seven_days(self):
        conn, last = make_db(self, days=5)
        self.assertIsNone(baselines.baseline(conn, "hrv_last_night", last, person_id=1))
        conn, last = make_db(self)
        b = baselines.baseline(conn, "hrv_last_night", last, person_id=1)
        self.assertEqual(b.n, 30)
        self.assertAlmostEqual(b.mean, 50, delta=0.2)

    def test_normal_day_scores_near_seventy_plus(self):
        conn, last = make_db(self)
        r = readiness.compute(conn, last, 1)
        self.assertIsNotNone(r.score)
        self.assertTrue(60 <= r.score <= 85, r.score)
        self.assertEqual(len(r.parts), 5)

    def test_bad_night_scores_lower(self):
        conn, last = make_db(self)
        normal = readiness.compute(conn, last, 1).score
        for k, v in (("hrv_last_night", 38), ("sleep_minutes", 300), ("body_battery_wake", 40), ("resting_hr", 60)):
            db.set_metric(conn, last, k, v, "garmin", 1)
        bad = readiness.compute(conn, last, 1)
        self.assertLess(bad.score, normal - 20)
        self.assertEqual(bad.band, "low")

    def test_too_little_data_gives_no_score(self):
        conn, last = make_db(self, days=3)
        r = readiness.compute(conn, last, 1)
        self.assertIsNone(r.score)  # only sleep + battery available; honest "not yet"

    def test_part_day_is_yesterday_for_stress_else_the_score_day(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: each part says which day its `value` came
        from -- the stress part reads yesterday's (full-day) stress_avg on purpose, so its `day`
        is the day before; every other part reads the score's own day."""
        conn, last = make_db(self)
        yesterday = (date.fromisoformat(last) - timedelta(1)).isoformat()
        r = readiness.compute(conn, last, 1)
        by_key = {p.key: p.day for p in r.parts}
        self.assertEqual(by_key["stress"], yesterday)
        for key in ("hrv", "sleep", "battery", "resting_hr"):
            self.assertEqual(by_key[key], last)

    def test_update_stores_estimate_with_version(self):
        conn, last = make_db(self)
        readiness.update(conn, last, 1)
        row = conn.execute("SELECT source FROM daily_metric WHERE metric_key='readiness' AND day=?", (last,)).fetchone()
        self.assertEqual(row["source"], readiness.VERSION)
        kind = conn.execute("SELECT kind FROM metric_def WHERE key='readiness'").fetchone()["kind"]
        self.assertEqual(kind, "estimated")


def _make_sin_necesidad(case: unittest.TestCase, days: int):
    """Every day's own `sleep_need_min` is absent on purpose, so the "sleep" part's own `need`
    always falls back to `baseline(conn, "sleep_minutes", day, ...)` (BODY-DESPERTAR-REAL-6B
    ruling 3's own dependency: a day's readiness leans on OTHER days' sleep_minutes through this
    baseline, which is exactly how a correction on one night can move a LATER day's own score)."""
    conn = open_conn(case, Path(tempfile.mkdtemp()) / "t.db")
    db.init(conn)
    start = date(2026, 10, 1)
    for i in range(days):
        d = (start + timedelta(i)).isoformat()
        wobble = (-1) ** i
        for k, v in (("hrv_last_night", 50 + 2 * wobble), ("resting_hr", 55 + wobble),
                     ("stress_avg", 30 + 3 * wobble), ("sleep_minutes", 450),
                     ("body_battery_wake", 75)):
            db.set_metric(conn, d, k, v, "garmin", 1)
    conn.commit()
    return conn, start


class RecomputeDesdeTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-6B ruling 3: applying or undoing a 'sueno' correction re-stores
    readiness for that day and every later day with data within baselines.WINDOW_DAYS after it."""

    def _score(self, conn, day):
        row = conn.execute(
            "SELECT value FROM daily_metric WHERE person_id=1 AND day=? AND metric_key='readiness'", (day,)
        ).fetchone()
        return row["value"] if row else None

    def _bajar_sueno(self, conn, corrected_day: str, boundary_day: str):
        """`corrected_day`'s and `boundary_day`'s own sleep_minutes, lowered BEFORE
        `readiness.recompute_all` runs -- well BELOW the usual 450-minute baseline (ratio < 1),
        off the sleep part's own ceiling, where a correction would otherwise show no change at
        all (a ratio already >= 1 just clamps)."""
        db.set_metric(conn, corrected_day, "sleep_minutes", 200, "garmin", 1)
        db.set_metric(conn, boundary_day, "sleep_minutes", 250, "garmin", 1)

    def _aplicar_correccion(self, conn, corrected_day: str):
        """A big correction (07:30 -> 20:30, 780 minutes -- invented data, never checked against
        despertar_real's own horizon here since this writes the row directly) pushes
        `corrected_day`'s own ratio back up, and the same swing moves a LATER day's own 30-day
        trailing baseline mean (780 / 30 = 26 min) enough to move ITS rounded score too."""
        conn.execute(
            "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (1, ?, ?, ?)",
            (corrected_day, f"{corrected_day}T22:00:00", f"{corrected_day}T07:30:00"))
        conn.commit()
        db.guardar_despertar_correccion(conn, 1, corrected_day, "sueno", "20:30", "despertar-real-v2", "persona")

    def test_recomputes_the_day_and_the_window_after_it_not_beyond(self):
        days = 42
        conn, start = _make_sin_necesidad(self, days)
        corrected_day = (start + timedelta(10)).isoformat()
        boundary_day = (start + timedelta(10 + baselines.WINDOW_DAYS)).isoformat()   # 10 + 30 = 40, inside
        outside_day = (start + timedelta(10 + baselines.WINDOW_DAYS + 1)).isoformat()  # 41, outside
        self._bajar_sueno(conn, corrected_day, boundary_day)
        readiness.recompute_all(conn, 1)
        stale_corrected = self._score(conn, corrected_day)
        stale_boundary = self._score(conn, boundary_day)
        stale_outside = self._score(conn, outside_day)

        self._aplicar_correccion(conn, corrected_day)
        # The TRUE figures right now, computed live (never written until recompute_desde runs) --
        # the fixture must actually move these, or the assertions below would be vacuous.
        live_corrected = readiness.compute(conn, corrected_day, 1).score
        live_boundary = readiness.compute(conn, boundary_day, 1).score
        live_outside = readiness.compute(conn, outside_day, 1).score
        self.assertNotEqual(stale_corrected, live_corrected, "fixture must move the corrected day")
        self.assertNotEqual(stale_boundary, live_boundary, "fixture must move the boundary day")
        self.assertEqual(stale_outside, live_outside, "the day outside the window must be untouched either way")

        # Still stale right before recompute_desde runs -- the write only happens there.
        self.assertEqual(self._score(conn, corrected_day), stale_corrected)
        self.assertEqual(self._score(conn, boundary_day), stale_boundary)

        n = readiness.recompute_desde(conn, 1, corrected_day)
        self.assertEqual(n, baselines.WINDOW_DAYS + 1)  # idx 10..40 inclusive, every day has data

        self.assertEqual(self._score(conn, corrected_day), live_corrected)
        self.assertEqual(self._score(conn, boundary_day), live_boundary)
        self.assertEqual(self._score(conn, outside_day), stale_outside)  # recompute_desde never reached it

    def test_undo_restores_the_pre_correction_scores(self):
        days = 42
        conn, start = _make_sin_necesidad(self, days)
        corrected_day = (start + timedelta(10)).isoformat()
        boundary_day = (start + timedelta(10 + baselines.WINDOW_DAYS)).isoformat()
        self._bajar_sueno(conn, corrected_day, boundary_day)
        readiness.recompute_all(conn, 1)
        before_corrected = self._score(conn, corrected_day)
        before_boundary = self._score(conn, boundary_day)

        self._aplicar_correccion(conn, corrected_day)
        readiness.recompute_desde(conn, 1, corrected_day)
        self.assertNotEqual(before_corrected, self._score(conn, corrected_day))

        db.borrar_despertar_correccion(conn, 1, corrected_day, "sueno")
        readiness.recompute_desde(conn, 1, corrected_day)
        self.assertEqual(before_corrected, self._score(conn, corrected_day))
        self.assertEqual(before_boundary, self._score(conn, boundary_day))

    def test_statement_count_is_bounded_by_the_window(self):
        """Ruling 6: the recompute on apply is bounded by WINDOW_DAYS + 1 days -- one update()
        call per day in range, never a query whose cost tracks the WHOLE database's history."""
        days = 42
        conn, start = _make_sin_necesidad(self, days)
        readiness.recompute_all(conn, 1)
        corrected_day = (start + timedelta(10)).isoformat()
        self._aplicar_correccion(conn, corrected_day)
        n = readiness.recompute_desde(conn, 1, corrected_day)
        self.assertLessEqual(n, baselines.WINDOW_DAYS + 1)


class ReadinessLectorEqualityTest(unittest.TestCase):
    """`readiness.compute` with a `body/lector.py` Lector must give EXACTLY the same `Readiness`
    as the original per-call query path (BODY-RENDIMIENTO-2)."""

    def _parts(self, r: readiness.Readiness):
        return [(p.key, p.value, p.reference, p.subscore, p.weight, p.day) for p in r.parts]

    def test_matches_on_a_normal_day(self):
        conn, last = make_db(self)
        # +1: the stress part's baseline is computed on yesterday, one day further back than
        # WINDOW_DAYS alone reaches (body/rutas/turnos.py carries the same margin).
        start = date.fromisoformat(last) - timedelta(baselines.WINDOW_DAYS + 1)
        lector = Lector(conn, 1, start.isoformat(), last, readiness.METRICS)
        without = readiness.compute(conn, last, 1)
        with_lector = readiness.compute(conn, last, 1, lector=lector)
        self.assertEqual(with_lector.score, without.score)
        self.assertEqual(with_lector.version, without.version)
        self.assertEqual(with_lector.missing, without.missing)
        self.assertEqual(self._parts(with_lector), self._parts(without))

    def test_matches_with_too_little_data(self):
        conn, last = make_db(self, days=3)
        start = date.fromisoformat(last) - timedelta(baselines.WINDOW_DAYS + 1)
        lector = Lector(conn, 1, start.isoformat(), last, readiness.METRICS)
        without = readiness.compute(conn, last, 1)
        with_lector = readiness.compute(conn, last, 1, lector=lector)
        self.assertIsNone(without.score)
        self.assertIsNone(with_lector.score)
        self.assertEqual(with_lector.missing, without.missing)


class AccionPorParteTest(unittest.TestCase):
    """BODY-REVISION-COPY-GAPS-1 finding 22: a plain, non-medical action word per part -- HRV,
    stress and resting HR share one ("liviano": keep today easy), sleep and Body Battery get their
    own."""

    def test_every_part_has_an_action_word(self):
        for key in readiness.WEIGHTS:
            self.assertIsInstance(readiness.accion(key), str)

    def test_hrv_stress_resting_hr_share_the_same_word(self):
        self.assertEqual(readiness.accion("hrv"), readiness.accion("stress"))
        self.assertEqual(readiness.accion("hrv"), readiness.accion("resting_hr"))

    def test_sleep_and_battery_get_their_own_word(self):
        self.assertNotEqual(readiness.accion("sleep"), readiness.accion("hrv"))
        self.assertNotEqual(readiness.accion("battery"), readiness.accion("hrv"))
        self.assertNotEqual(readiness.accion("sleep"), readiness.accion("battery"))


if __name__ == "__main__":
    unittest.main()
