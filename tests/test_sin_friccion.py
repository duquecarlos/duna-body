"""BODY-SIN-FRICCION-1: census of every empty state and two "no dead end" pickers, on invented demo
data. Every entry renders its sentence (and link, or "fills by itself" line) in both languages; the
new first-habit block in goal creation; no health figure ever lands in a redirect URL."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, web
from body.i18n import T

client = TestClient(web.app, follow_redirects=False)

SENTINEL_VALUE = 987.654321  # cannot occur in a count, date, size or a static-asset hash


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "f.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _set_lang(lang: str):
    client.post("/settings/language", data={"lang": lang})


def _esc(s: str) -> str:
    """Jinja autoescapes i18n text into HTML entities; match what the page actually renders."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


class HoyEmptyCensusTest(unittest.TestCase):
    """Hoy with no data ever synced: the sync runs on its own, no sync date to show yet."""

    def test_no_sync_yet_both_languages(self):
        _reset_db(self)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "sync.runs"), r.text)
        self.assertIn(_t("es", "sync.none_yet"), r.text)
        _set_lang("en")
        r = client.get("/")
        self.assertIn(_t("en", "sync.runs"), r.text)
        self.assertIn(_t("en", "sync.none_yet"), r.text)

    def test_shows_last_sync_date_when_recorded(self):
        conn = _reset_db(self)
        conn.execute(
            "INSERT INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
            "VALUES (1, '2026-11-01', 'user_summary', 'garmin', '2026-11-01T12:00:00+00:00', '{}')"
        )
        conn.commit()
        last = web._short_date(T("es"), date(2026, 11, 1))
        r = client.get("/")
        self.assertIn(_t("es", "sync.last", date=last), r.text)
        self.assertNotIn(_t("es", "sync.none_yet"), r.text)


class DiaEmptyCensusTest(unittest.TestCase):
    """Día with no data for that day: the same sync explanation, next to the existing no-data line."""

    def test_no_data_day_both_languages(self):
        _reset_db(self)
        r = client.get("/day/2026-01-01")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "day.no_data"), r.text)
        self.assertIn(_t("es", "sync.runs"), r.text)
        _set_lang("en")
        r = client.get("/day/2026-01-01")
        self.assertIn(_t("en", "day.no_data"), r.text)
        self.assertIn(_t("en", "sync.runs"), r.text)


class TrendsZeroDataCensusTest(unittest.TestCase):
    """Tendencias with zero days ever synced: same sync explanation as Hoy/Día, not a generic 'soon'."""

    def test_zero_data_both_languages(self):
        _reset_db(self)
        r = client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "sync.runs"), r.text)
        self.assertIn(_t("es", "sync.none_yet"), r.text)
        _set_lang("en")
        r = client.get("/trends")
        self.assertIn(_t("en", "sync.runs"), r.text)


class TrendsTooLittleCensusTest(unittest.TestCase):
    """Tendencias with 1-13 days: the honest gap and a day count, never a figure."""

    def test_too_little_data_both_languages(self):
        conn = _reset_db(self)
        for i in range(5):  # 5 days: under profile.metric_profile's 14-day floor
            conn.execute(
                "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES "
                "(1, ?, 'resting_hr', ?, 'garmin')", (f"2026-10-{i + 1:02d}", SENTINEL_VALUE))
        conn.commit()
        r = client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "trends.too_little", days=9), r.text)
        self.assertNotIn(str(SENTINEL_VALUE), r.text)  # the count is shown, never the metric's own figure
        _set_lang("en")
        r = client.get("/trends")
        self.assertIn(_t("en", "trends.too_little", days=9), r.text)


class HistoryZeroDataCensusTest(unittest.TestCase):
    """Tu historia with zero days ever synced: same sync explanation, not the mismatched trends text."""

    def test_zero_data_both_languages(self):
        _reset_db(self)
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "sync.runs"), r.text)
        _set_lang("en")
        r = client.get("/history")
        self.assertIn(_t("en", "sync.runs"), r.text)


class HistorySectionsAlreadyGoodCensusTest(unittest.TestCase):
    """Tu historia's patterns/shift sections already link Turnos -- keep, census it."""

    def test_patterns_and_shift_empty_link_turnos(self):
        conn = _reset_db(self)
        conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES "
            "(1, '2026-10-01', 'resting_hr', 55, 'garmin')")
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "history.patterns_none"), r.text)
        self.assertIn(_t("es", "history.shift_empty"), r.text)
        self.assertIn('href="/shifts"', r.text)
        _set_lang("en")
        r = client.get("/history")
        self.assertIn(_t("en", "history.patterns_none"), r.text)
        self.assertIn(_t("en", "history.shift_empty"), r.text)


class TurnosEmptyCensusTest(unittest.TestCase):
    """Turnos with nothing proposed or confirmed, and no watch data at all, is an honest empty state
    (BODY-TURNOS-COSTO-1-M) -- the hero reads a reason code instead of a generic 'soon' placeholder."""

    def test_empty_both_languages(self):
        _reset_db(self)
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "shifts.reason.sin_datos_reloj"), r.text)
        _set_lang("en")
        r = client.get("/shifts")
        self.assertIn(_t("en", "shifts.reason.sin_datos_reloj"), r.text)


class MetasEmptyCensusTest(unittest.TestCase):
    """Metas already offers «+ Nueva» -- keep, census it."""

    def test_empty_offers_new_goal_link(self):
        _reset_db(self)
        r = client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "goals.empty_text"), r.text)
        self.assertIn('href="/goals/new"', r.text)
        _set_lang("en")
        r = client.get("/goals")
        self.assertIn(_t("en", "goals.empty_text"), r.text)


class MetricPickerEmptyCatalogueTest(unittest.TestCase):
    """A picker with an empty catalogue keeps its label and a one-line reason (BODY-SIN-FRICCION-1)."""

    def test_metric_row_keeps_label_with_empty_catalogue(self):
        _reset_db(self)
        # db.init() reseeds metric_def on every request, so an empty catalogue can only happen if
        # it is somehow queried before that seeding -- exactly the defensive case this guards.
        with mock.patch.object(web, "_defs", return_value={}):
            r = client.get("/goals/new")
            self.assertEqual(r.status_code, 200)
            self.assertIn(_t("es", "goals.field_metric"), r.text)
            self.assertIn(_t("es", "goals.metric_empty"), r.text)
            self.assertNotIn('id="metric-select"', r.text)
            _set_lang("en")
            r = client.get("/goals/new")
            self.assertIn(_t("en", "goals.field_metric"), r.text)
            self.assertIn(_t("en", "goals.metric_empty"), r.text)


class HouseholdPickerNoHouseholdTest(unittest.TestCase):
    """The household choice keeps its label with a short explainer when there is no household."""

    def test_visibility_row_keeps_label_without_household(self):
        _reset_db(self)
        r = client.get("/goals/new")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "goals.field_visibility"), r.text)
        self.assertIn(_t("es", "goals.household_empty"), r.text)
        self.assertNotIn('name="visibility"', r.text)
        _set_lang("en")
        r = client.get("/goals/new")
        self.assertIn(_t("en", "goals.field_visibility"), r.text)
        self.assertIn(_t("en", "goals.household_empty"), r.text)


class FirstHabitInCreationFlowTest(unittest.TestCase):
    """Create in place, come back ready: a filled first-habit block adds a habit in the same POST."""

    def test_filled_habit_block_creates_habit_and_lands_on_detail(self):
        _reset_db(self)
        resp = client.post("/goals/new", data={
            "kind": "milestone", "title": "Subir el Cocuy", "area": "adventure",
            "deadline": "2027-06-01",
            "habit_title": "Caminar 30 min", "habit_times": "3", "habit_per": "week",
        })
        self.assertEqual(resp.status_code, 303)
        location = resp.headers["location"]
        self.assertRegex(location, r"^/goals/\d+$")
        detail = client.get(location)
        self.assertIn("Caminar 30 min", detail.text)

    def test_empty_habit_block_creates_no_habit(self):
        conn = _reset_db(self)
        resp = client.post("/goals/new", data={
            "kind": "milestone", "title": "Subir el Cocuy sin hábito", "area": "adventure",
            "deadline": "2027-06-01",
        })
        self.assertEqual(resp.status_code, 303)
        gid = int(resp.headers["location"].rsplit("/", 1)[1])
        n = conn.execute("SELECT COUNT(*) FROM habit WHERE goal_id = ?", (gid,)).fetchone()[0]
        self.assertEqual(n, 0)


class NoFigureInRedirectTest(unittest.TestCase):
    """No health figure ever lands in a redirect URL -- only ids, dates and counts."""

    def test_goal_creation_redirect_carries_only_an_id(self):
        _reset_db(self)
        resp = client.post("/goals/new", data={
            "kind": "metric", "title": "Bajar el pulso", "area": "body",
            "metric_key": "resting_hr", "direction": "lower", "target_value": "52",
            "baseline_value": "60", "deadline": "2027-03-31",
            "habit_title": "Dormir temprano", "habit_times": "1", "habit_per": "day",
        })
        self.assertEqual(resp.status_code, 303)
        self.assertRegex(resp.headers["location"], r"^/goals/\d+$")


if __name__ == "__main__":
    unittest.main()
