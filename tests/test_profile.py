"""Long-term profile: monthly means, normal range, typical week."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
import tempfile
from datetime import date, timedelta
from pathlib import Path

from tests._db import open_conn

from body import db, profile


class ProfileTest(unittest.TestCase):
    def test_profile(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "p.db")
        db.init(conn)
        start = date(2026, 1, 1)
        for i in range(90):
            d = start + timedelta(i)
            db.set_metric(conn, d.isoformat(), "sleep_minutes", 400 if d.weekday() == 2 else 420, "garmin", 1)
        conn.commit()
        p = profile.metric_profile(conn, "sleep_minutes", person_id=1)
        self.assertEqual(len(p.months), 3)
        self.assertEqual(p.weekday[2], 400)  # Wednesdays are the short nights
        self.assertIn("<svg", profile.month_svg(p))
        self.assertIsNone(profile.metric_profile(conn, "steps", person_id=1))  # no data -> honest None
        self.assertIn('href="/trends?m=2026-01#sleep_minutes"', profile.month_svg(p))  # month drills to its days

    def test_window(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "w.db")
        db.init(conn)
        for i in range(20):
            if i != 15:  # one night without the watch
                db.set_metric(conn, (date(2026, 3, 1) + timedelta(i)).isoformat(), "resting_hr", 50 + (i >= 10),
                             "garmin", 1)
        conn.commit()
        w = profile.window(conn, "resting_hr", date(2026, 3, 11), date(2026, 3, 20), 1)
        self.assertEqual(len(w.values), 9)
        self.assertEqual((w.mean, w.prev_mean), (51, 50))
        svg = profile.window_svg(w, 49, 52, nights={"2026-03-12"})
        self.assertEqual(svg.count("<polyline"), 2)        # the missing day breaks the line, no zero drawn
        self.assertIn('href="/day/2026-03-16"', svg)       # every day, even the gap, opens its day
        self.assertIn("c-night", svg)                      # night shift shaded
        self.assertNotIn("c-viaje", svg)                   # no viajes passed -- byte-identical otherwise

    def test_window_svg_shades_trip_days(self):
        """BODY-HISTORIA-VIDA-LUGARES-2-R ruling 2: `viajes` draws its own distinct mark, additive
        to nights/marcas -- the default (no callers before this slice pass it) stays unshaded."""
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "w2.db")
        db.init(conn)
        for i in range(20):
            db.set_metric(conn, (date(2026, 3, 1) + timedelta(i)).isoformat(), "resting_hr", 50, "garmin", 1)
        conn.commit()
        w = profile.window(conn, "resting_hr", date(2026, 3, 11), date(2026, 3, 20), 1)
        svg = profile.window_svg(w, 49, 52, viajes={"2026-03-13"})
        self.assertIn("c-viaje", svg)


class EndLabelOverlapGuardTest(unittest.TestCase):
    """BODY-REVISION-GRAFICOS-FIGURA-1 finding 10: window_svg/month_svg's end-of-range labels
    wrap instead of ever overlapping -- the same guard (graficos_eje.wrap_label, imported at
    profile.py's own top level, ORCH-CARLOS-DELEGATES-TIER-1-1) turnos.py's perfil chart already
    used, reused here rather than copied."""

    def test_month_svg_wraps_a_label_too_wide_for_its_half(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "wrap.db")
        db.init(conn)
        start = date(2026, 1, 1)
        for i in range(90):
            db.set_metric(conn, (start + timedelta(i)).isoformat(), "sleep_minutes", 420, "garmin", 1)
        conn.commit()
        p = profile.metric_profile(conn, "sleep_minutes", person_id=1)
        svg = profile.month_svg(p, etiqueta_mes=lambda m: "Un mes con un nombre muy largo")
        self.assertIn("<br>", svg)

    def test_month_svg_does_not_wrap_a_short_label(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "nowrap.db")
        db.init(conn)
        start = date(2026, 1, 1)
        for i in range(90):
            db.set_metric(conn, (start + timedelta(i)).isoformat(), "sleep_minutes", 420, "garmin", 1)
        conn.commit()
        p = profile.metric_profile(conn, "sleep_minutes", person_id=1)
        svg = profile.month_svg(p, etiqueta_mes=lambda m: m)
        self.assertNotIn("<br>", svg)

    def test_window_svg_wraps_a_label_too_wide_for_its_half(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "wwrap.db")
        db.init(conn)
        for i in range(20):
            db.set_metric(conn, (date(2026, 3, 1) + timedelta(i)).isoformat(), "resting_hr", 50, "garmin", 1)
        conn.commit()
        w = profile.window(conn, "resting_hr", date(2026, 3, 11), date(2026, 3, 20), 1)
        svg = profile.window_svg(w, 49, 52, etiqueta_dia=lambda d: "Un día con una etiqueta muy larga")
        self.assertIn("<br>", svg)


class MonthSvgByteIdenticalAfterGraficosEjeMoveTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4: profile.py reaching the wrap rule through
    body/graficos_eje.py (a top-level import) instead of turnos.py's own _perfil_wrap (a
    function-local, cross-package deferred import) must not change a single byte of the SVG.
    The expected string below was captured by running profile.py's own OLD month_svg (its own
    _wrap_label, calling turnos.py's old _label_px/_perfil_wrap math inlined) as it stood at
    c7d6e8d7179ecb923e3124e1368504e32408dee7 -- this slice's base commit -- against this exact
    fixed MetricProfile, built by hand so the fixture carries no real reading."""

    _P = profile.MetricProfile(
        key="sleep_minutes",
        months=[("2026-01", 410.0, 20), ("2026-02", 430.0, 22)],
        all_mean=420.0, low=380.0, high=460.0,
        last30=425.0, prev30=415.0, weekday=[None] * 7, days=42, first_day="2026-01-01",
    )

    _EXPECTED_SVG = (
        '<figure class="chart"><svg viewBox="0 0 320 106" width="100%" height="110" role="img" '
        'preserveAspectRatio="none" class="chart-svg">'
        '<rect x="0" y="15.1" width="320" height="75.8" class="c-inset"/>'
        '<polyline fill="none" class="stroke-body" stroke-width="2.2" vector-effect="non-scaling-stroke" '
        'stroke-linejoin="round" points="0.0,62.5 320.0,43.5"/>'
        '<a href="/trends?m=2026-01#sleep_minutes"><title>Un mes con un nombre muy largo</title>'
        '<rect x="0.0" y="0" width="320.0" height="106.0" class="fill-transparent"/></a>'
        '<a href="/trends?m=2026-02#sleep_minutes"><title>Un mes con un nombre muy largo</title>'
        '<rect x="160.0" y="0" width="320.0" height="106.0" class="fill-transparent"/></a></svg>'
        '<div class="row k fs-12"><span>Un<br>mes con un nombre muy largo</span>'
        '<span>Un<br>mes con un nombre muy largo</span></div></figure>'
    )

    def test_svg_byte_identical_to_main_before_the_move(self):
        svg = profile.month_svg(self._P, etiqueta_mes=lambda m: "Un mes con un nombre muy largo")
        self.assertEqual(svg, self._EXPECTED_SVG)


if __name__ == "__main__":
    unittest.main()
