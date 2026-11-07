"""BODY-EJES-FECHAS-1: Tendencias' chart axes and tap targets read in the person's language, never raw ISO
(DESIGN rules 9, 10). URLs still carry ISO dates -- they carry only dates, never values. Invented demo data only,
no real health figures.
"""
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, profile, web

ATTR_RE = re.compile(r'\s+[\w-]+="[^"]*"')
ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ISO_MONTH_RE = re.compile(r"\d{4}-\d{2}\b")


def _strip_attrs(html_text: str) -> str:
    """Drop every attr="..." so only element text (titles, spans, labels) remains to check."""
    return ATTR_RE.sub("", html_text)


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _seed_days(conn, person_id: int, key: str, start: str, n: int, base: float, step: float = 0.5) -> None:
    year, month, day = (int(x) for x in start.split("-"))
    d0 = date(year, month, day)
    for i in range(n):
        db.set_metric(conn, (d0 + timedelta(i)).isoformat(), key, base + i * step, "garmin", person_id)


class TrendsAxisLabelsTest(unittest.TestCase):
    """Person 1, 68 days of two metrics spanning three months: enough for both the 30-day window view
    and the all-history monthly view to render a chart."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "ejes.db"
        conn = db.connect()
        db.init(conn)
        _seed_days(conn, 1, "resting_hr", "2026-09-01", 68, 58, -0.1)
        _seed_days(conn, 1, "sleep_minutes", "2026-09-01", 68, 410, 0.3)
        conn.commit()
        self.client = TestClient(web.app, follow_redirects=False)
        _as(1)

    def test_window_view_has_no_bare_iso_day_outside_hrefs(self):
        page = self.client.get("/trends?r=30").text
        self.assertNotRegex(_strip_attrs(page), ISO_DAY_RE)

    def test_window_view_axis_ends_read_in_spanish(self):
        stripped = _strip_attrs(self.client.get("/trends?r=30").text)
        self.assertIn("oct", stripped.lower())  # abbreviated month name, readable

    def test_window_hrefs_still_carry_iso_days(self):
        page = self.client.get("/trends?r=30").text
        self.assertRegex(page, r'href="/day/\d{4}-\d{2}-\d{2}"')

    def test_monthly_all_view_has_no_bare_iso_month_outside_hrefs(self):
        page = self.client.get("/trends").text  # r=all (default) -> monthly view
        self.assertNotRegex(_strip_attrs(page), ISO_MONTH_RE)

    def test_monthly_hrefs_still_carry_iso_months(self):
        page = self.client.get("/trends").text
        self.assertRegex(page, r'href="/trends\?m=\d{4}-\d{2}#')

    def test_tap_titles_are_readable_not_bare_iso(self):
        page = self.client.get("/trends?r=30").text
        titles = re.findall(r"<title>(.*?)</title>", page)
        self.assertTrue(titles)
        for title in titles:
            self.assertNotRegex(title, r"^\d{4}-\d{2}-\d{2}$")

    def test_every_svg_has_an_aria_label_naming_its_metric(self):
        page = self.client.get("/trends?r=30").text
        chart_svgs = [tag for tag in re.findall(r"<svg[^>]*>", page) if 'role="img"' in tag]
        self.assertTrue(chart_svgs)
        for tag in chart_svgs:
            self.assertIn('aria-label="', tag)
        self.assertTrue(any("Pulso" in tag or "Sueño" in tag for tag in chart_svgs))

    def test_en_renders_english_labels(self):
        conn = db.connect()
        db.set_language(conn, "en", 1)
        conn.commit()
        page = self.client.get("/trends?r=30").text
        stripped = _strip_attrs(page)
        self.assertNotRegex(stripped, ISO_DAY_RE)
        self.assertIn("Oct", stripped)
        chart_svgs = [tag for tag in re.findall(r"<svg[^>]*>", page) if 'role="img"' in tag]
        self.assertTrue(any("Resting heart rate" in tag or "Sleep" in tag for tag in chart_svgs))


class BuildersBackwardCompatTest(unittest.TestCase):
    """Called without the new keyword arguments, the builders still produce ISO -- no other caller breaks."""

    def test_month_svg_without_new_args_is_iso(self):
        conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "m.db")
        db.init(conn)
        start = date(2026, 1, 1)
        for i in range(60):
            db.set_metric(conn, (start + timedelta(i)).isoformat(), "sleep_minutes", 420, "garmin", 1)
        conn.commit()
        p = profile.metric_profile(conn, "sleep_minutes", person_id=1)
        svg = profile.month_svg(p)
        self.assertIn("2026-01", svg)
        self.assertNotIn("aria-label", svg)

    def test_window_svg_without_new_args_is_iso(self):
        conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "w.db")
        db.init(conn)
        for i in range(10):
            db.set_metric(conn, (date(2026, 3, 1) + timedelta(i)).isoformat(), "resting_hr", 50, "garmin", 1)
        conn.commit()
        w = profile.window(conn, "resting_hr", date(2026, 3, 1), date(2026, 3, 10), 1)
        svg = profile.window_svg(w, 49, 52)
        self.assertIn("2026-03-01", svg)
        self.assertNotIn("aria-label", svg)


if __name__ == "__main__":
    unittest.main()
