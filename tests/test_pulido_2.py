"""BODY-PULIDO-2: Tendencias' and Metas' charts explain themselves on a phone -- legend, value on tap,
"Toca un día..." (DESIGN rules 9, 11), and the "aun no hay suficientes dias" page keeps its way back.
Invented demo data only, no real health figures.
"""
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, identity, web

FIGURE_RE = re.compile(r'<figure class="chart"([^>]*)>(.*?)</figure>', re.S)
_STATIC_DIR = Path(__file__).resolve().parent.parent / "body" / "static"


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _seed_days(conn, person_id: int, key: str, start: str, n: int, base: float, step: float = 0.5) -> None:
    year, month, day = (int(x) for x in start.split("-"))
    d0 = date(year, month, day)
    for i in range(n):
        db.set_metric(conn, (d0 + timedelta(i)).isoformat(), key, base + i * step, "garmin", person_id)


def _figures(page: str) -> list[tuple[str, str]]:
    """Every <figure class="chart" ...>...</figure> on the page, as (attrs, inner html)."""
    return FIGURE_RE.findall(page)


def _points_of(attrs: str) -> list[dict] | None:
    m = re.search(r'data-points="([^"]*)"', attrs)
    if not m:
        return None
    raw = m.group(1).replace("&quot;", '"').replace("&#x27;", "'").replace("&amp;", "&")
    return json.loads(raw)


def _fresh_db(tc: unittest.TestCase, name: str):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


class TrendsWindowChartTest(unittest.TestCase):
    """r=30 and r=90: every chart is an enhanced figure with one point per day, the per-day hit regions
    stay (the no-JS path), the hint renders, and charts.js is loaded."""

    def setUp(self):
        self.conn = _fresh_db(self, "pulido2_window.db")
        _seed_days(self.conn, 1, "resting_hr", "2026-08-01", 100, 58, -0.1)
        _seed_days(self.conn, 1, "sleep_minutes", "2026-08-01", 100, 410, 0.3)
        self.conn.commit()
        self.client = TestClient(web.app, follow_redirects=False)
        _as(1)

    def _check_window(self, r: int):
        page = self.client.get(f"/trends?r={r}").text
        figures = _figures(page)
        self.assertTrue(figures)
        saw_day_points = False
        for attrs, inner in figures:
            points = _points_of(attrs)
            self.assertIsNotNone(points)
            self.assertTrue(points)
            for p in points:
                self.assertIn("day", p)
                self.assertRegex(p["day"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertNotRegex(p["label"], r"^\d{4}-\d{2}-\d{2}")
                saw_day_points = True
            self.assertIn("<title>", inner)
            self.assertIn("<rect", inner)
        self.assertTrue(saw_day_points)
        self.assertIn("charts.js", page)
        self.assertIn(">Toca o desliza sobre la gráfica para ver cada día<", page)

    def test_r30(self):
        self._check_window(30)

    def test_r90(self):
        self._check_window(90)

    def test_en_labels_are_localised_not_iso(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        page = self.client.get("/trends?r=30").text
        figures = _figures(page)
        self.assertTrue(figures)
        for attrs, _ in figures:
            for p in _points_of(attrs):
                self.assertNotRegex(p["label"], r"^\d{4}-\d{2}-\d{2}")
        self.assertIn("Tap or drag on the chart to see each day", page)


class TrendsMonthlyChartTest(unittest.TestCase):
    """All-time view: month points carry a month key (never a day), and the figure names the metric key
    charts.js needs to build the same month link the existing hit region already opens."""

    def setUp(self):
        self.conn = _fresh_db(self, "pulido2_month.db")
        _seed_days(self.conn, 1, "resting_hr", "2026-05-01", 150, 60, -0.03)
        self.conn.commit()
        self.client = TestClient(web.app, follow_redirects=False)
        _as(1)

    def test_month_points_carry_month_not_day(self):
        page = self.client.get("/trends").text  # r=all (default) -> monthly view
        figures = _figures(page)
        self.assertTrue(figures)
        saw_month = False
        for attrs, _ in figures:
            points = _points_of(attrs)
            if not points or "m" not in points[0]:
                continue
            saw_month = True
            for p in points:
                self.assertRegex(p["m"], r"^\d{4}-\d{2}$")
                self.assertNotIn("day", p)
            self.assertIn('data-key="resting_hr"', attrs)
            self.assertIn("Abrir el mes", attrs)
        self.assertTrue(saw_month)
        self.assertIn("charts.js", page)


class GoalChartTest(unittest.TestCase):
    """The goal chart names the goal and both dates with no raw baseline/target figure, shows its legend,
    and its weekly points carry no day -- a week is not a day."""

    def setUp(self):
        self.conn = _fresh_db(self, "pulido2_goal.db")
        start = "2026-09-07"
        _seed_days(self.conn, 1, "resting_hr", start, 21, 60, -0.3)
        self.conn.commit()
        self.gid = goals.propose(self.conn, 1, "Bajar el pulso en reposo", start, metric_key="resting_hr",
                                 direction="lower", target_value=52.0, baseline_value=60.0,
                                 deadline="2026-11-30")
        goals.activate(self.conn, self.gid)
        goals.weekly_check(self.conn, self.gid, 1, start)
        self.conn.commit()
        self.client = TestClient(web.app, follow_redirects=False)
        _as(1)

    def test_accessible_name_legend_and_weekly_points(self):
        page = self.client.get(f"/goals/{self.gid}").text
        m = re.search(r'<svg[^>]*role="img"[^>]*aria-label="([^"]*)"', page)
        self.assertIsNotNone(m)
        aria = m.group(1)
        self.assertIn("Bajar el pulso en reposo", aria)
        self.assertIn("plan desde", aria)
        self.assertNotRegex(aria, r"\b60\b")
        self.assertNotRegex(aria, r"\b52\b")

        self.assertIn(">Plan<", page)
        self.assertIn(">Tu promedio semanal<", page)
        self.assertIn(">Hoy<", page)
        self.assertIn("Toca o desliza sobre la gráfica para ver cada semana", page)

        figures = _figures(page)
        self.assertTrue(figures)
        points = None
        for attrs, _ in figures:
            p = _points_of(attrs)
            if p:
                points = p
        self.assertIsNotNone(points)
        self.assertTrue(points)
        for p in points:
            self.assertNotIn("day", p)
            self.assertNotIn("m", p)


class SoonBackLinkTest(unittest.TestCase):
    """The "not enough days yet" page keeps the way back that `desde` promised (the gap BODY-PULIDO-1 left)."""

    def setUp(self):
        conn = _fresh_db(self, "pulido2_soon.db")
        _seed_days(conn, 1, "resting_hr", "2026-09-01", 5, 58, -0.1)
        conn.commit()
        self.client = TestClient(web.app, follow_redirects=False)
        _as(1)

    def test_back_link_shown_when_desde_dia_given(self):
        page = self.client.get("/trends?desde=dia&dia=2026-09-03").text
        self.assertIn("Volver al día", page)

    def test_no_back_link_without_desde(self):
        page = self.client.get("/trends").text
        self.assertNotIn("Volver al día", page)


class ChartsJsSourceTest(unittest.TestCase):
    def test_no_fetch_and_only_day_and_month_literal_urls(self):
        src = (_STATIC_DIR / "charts.js").read_text(encoding="utf-8")
        self.assertNotIn("fetch(", src)
        for u in re.findall(r'"(/[^"\s]*)"', src):
            self.assertTrue(u.startswith("/day/") or u.startswith("/trends?m="), u)


class PersonIsolationTest(unittest.TestCase):
    """Person B's chart data never reaches person A's page (BODY privacy rule, DESIGN rule 13)."""

    def setUp(self):
        self.conn = _fresh_db(self, "pulido2_isolation.db")
        _seed_days(self.conn, 1, "resting_hr", "2026-08-01", 40, 50, -0.1)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Other')")
        self.conn.commit()
        _seed_days(self.conn, 2, "resting_hr", "2026-08-01", 40, 95, 0.1)
        self.conn.commit()
        self.client = TestClient(web.app, follow_redirects=False)

    def test_a_does_not_see_b_values(self):
        _as(1)
        page_a = self.client.get("/trends?r=30").text
        points_a = None
        for attrs, _ in _figures(page_a):
            p = _points_of(attrs)
            if p:
                points_a = p
                break
        self.assertIsNotNone(points_a)
        label_a = points_a[0]["label"]
        common_day = points_a[0]["day"]

        _as(2)
        page_b = self.client.get("/trends?r=30").text
        points_b = None
        for attrs, _ in _figures(page_b):
            p = _points_of(attrs)
            if p:
                points_b = p
                break
        self.assertIsNotNone(points_b)
        label_b = next(p["label"] for p in points_b if p["day"] == common_day)

        self.assertNotEqual(label_a, label_b)
        self.assertNotIn(label_b, page_a)
        self.assertNotIn(label_a, page_b)


if __name__ == "__main__":
    unittest.main()
