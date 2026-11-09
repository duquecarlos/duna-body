"""BODY-TENDENCIAS-TODAS-1: the rest of the measure catalogue behind "Ver todas las medidas", grouped by
plain-word topic (DESIGN rule 8). Invented demo data only, no real health figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, identity, web
from body.agente import herramientas
from body.rutas import comun

PERSON_B = 2


def _conn(case: unittest.TestCase):
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "tendencias_todas.db")


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _seed_days(conn, person_id: int, key: str, start: str, n: int, base: float, step: float = 0.5) -> None:
    year, month, day = (int(x) for x in start.split("-"))
    from datetime import date, timedelta
    d0 = date(year, month, day)
    for i in range(n):
        db.set_metric(conn, (d0 + timedelta(i)).isoformat(), key, base + i * step, "garmin", person_id)


class GroupsCatalogueTest(unittest.TestCase):
    def test_every_group_key_exists_in_metric_defs(self):
        defined = {d[0] for d in db.METRIC_DEFS}
        for _, keys in comun.TREND_GROUPS:
            for k in keys:
                self.assertIn(k, defined, f"{k} is not a METRIC_DEFS key")

    def test_sleep_need_min_is_never_in_a_group(self):
        all_keys = {k for _, keys in comun.TREND_GROUPS for k in keys}
        self.assertNotIn("sleep_need_min", all_keys)

    def test_expected_group_keys(self):
        groups = dict(comun.TREND_GROUPS)
        self.assertEqual(set(groups["sleep"]),
                          {"sleep_score", "sleep_deep_min", "sleep_rem_min", "sleep_light_min", "sleep_awake_min"})
        self.assertEqual(set(groups["night"]), {"sleep_spo2_avg", "sleep_respiration_avg"})
        self.assertEqual(set(groups["energy"]),
                          {"body_battery_high", "body_battery_low", "body_battery_sleep_charge"})
        self.assertEqual(set(groups["stress"]), {"stress_avg", "high_stress_min"})
        self.assertEqual(set(groups["activity"]), {"intensity_minutes", "active_kcal"})
        self.assertEqual(set(groups["breathing"]), {"respiration_awake"})


class TrendsPageWithExtrasTest(unittest.TestCase):
    """Person A has the 9 default metrics plus a mix of extras: some groups full, one group entirely
    without data (breathing), and one extra metric with fewer than TREND_MIN_DAYS rows (sleep_awake_min)."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "trends.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        # the default 9 -- only enough of them for the page to render cards at all
        _seed_days(conn, 1, "resting_hr", "2026-09-01", 20, 58, -0.2)
        _seed_days(conn, 1, "sleep_minutes", "2026-09-01", 20, 410, 1)
        # extras WITH data, spread across groups
        _seed_days(conn, 1, "sleep_score", "2026-09-01", 20, 72, 0.3)
        _seed_days(conn, 1, "sleep_deep_min", "2026-09-01", 20, 80, 0.5)
        _seed_days(conn, 1, "sleep_rem_min", "2026-09-01", 20, 90, 0.4)
        _seed_days(conn, 1, "sleep_light_min", "2026-09-01", 20, 200, 0.6)   # neutral
        _seed_days(conn, 1, "body_battery_high", "2026-09-01", 20, 92, 0.2)
        _seed_days(conn, 1, "body_battery_low", "2026-09-01", 20, 28, 0.2)
        _seed_days(conn, 1, "stress_avg", "2026-09-01", 20, 24, -0.1)
        _seed_days(conn, 1, "intensity_minutes", "2026-09-01", 20, 40, 0.5)
        _seed_days(conn, 1, "active_kcal", "2026-09-01", 20, 350, 2)         # neutral
        # extras WITHOUT enough data, or none at all
        _seed_days(conn, 1, "sleep_awake_min", "2026-09-01", 5, 18, 0.1)     # fewer than TREND_MIN_DAYS
        # body_battery_sleep_charge, high_stress_min, respiration_awake: no rows for person 1 at all
        conn.commit()
        self.client = TestClient(web.app, follow_redirects=False)

    def test_default_cards_outside_details_extra_cards_inside_grouped_with_correct_count(self):
        _as(1)
        page = self.client.get("/trends").text
        details_at = page.index('<details class="files mt-10"')
        for default_id in ('id="resting_hr"', 'id="sleep_minutes"'):
            self.assertIn(default_id, page)
            self.assertLess(page.index(default_id), details_at, f"{default_id} should be outside the details")
        for extra_id in ('id="sleep_score"', 'id="sleep_deep_min"', 'id="sleep_rem_min"', 'id="sleep_light_min"',
                         'id="body_battery_high"', 'id="body_battery_low"', 'id="stress_avg"',
                         'id="intensity_minutes"', 'id="active_kcal"'):
            self.assertIn(extra_id, page)
            self.assertGreater(page.index(extra_id), details_at, f"{extra_id} should be inside the details")
        # 9 extra cards had data: 4 sleep + 2 energy + 1 stress + 2 activity + 0 breathing
        self.assertIn("Ver todas las medidas (9 más)", page)

    def test_group_without_any_data_is_omitted(self):
        _as(1)
        page = self.client.get("/trends").text
        self.assertNotIn("Respiración", page)
        self.assertNotIn('id="respiration_awake"', page)

    def test_fewer_than_min_days_gets_no_card(self):
        _as(1)
        page = self.client.get("/trends").text
        self.assertNotIn('id="sleep_awake_min"', page)

    def test_neutral_metric_card_has_no_better_worse_class(self):
        _as(1)
        page = self.client.get("/trends").text
        for key in ("sleep_light_min", "active_kcal"):
            start = page.index(f'id="{key}"')
            end = page.index("</div>", page.index('class="legend mt-4"', start))
            chunk = page[start:end]
            self.assertIn("delta flat", chunk)
            self.assertNotIn("delta good", chunk)
            self.assertNotIn("delta bad", chunk)

    def test_person_b_never_sees_person_a_extras(self):
        _as(PERSON_B)
        conn = db.connect()
        self.addCleanup(conn.close)
        _seed_days(conn, PERSON_B, "resting_hr", "2026-09-01", 20, 60, -0.1)
        _seed_days(conn, PERSON_B, "sleep_minutes", "2026-09-01", 20, 400, 1)
        conn.commit()
        page_b = self.client.get("/trends").text
        for extra_id in ('id="sleep_score"', 'id="sleep_deep_min"', 'id="sleep_rem_min"', 'id="body_battery_high"',
                         'id="stress_avg"', 'id="intensity_minutes"', 'id="active_kcal"'):
            self.assertNotIn(extra_id, page_b)
        self.assertNotIn("Ver todas las medidas", page_b)

    def test_no_control_when_no_extra_metric_has_data(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        _as(PERSON_B)
        _seed_days(conn, PERSON_B, "resting_hr", "2026-09-01", 20, 60, -0.1)
        _seed_days(conn, PERSON_B, "sleep_minutes", "2026-09-01", 20, 400, 1)
        conn.commit()
        page_b = self.client.get("/trends").text
        self.assertNotIn('<details class="files"', page_b)

    def test_todas_1_renders_open_and_range_links_carry_the_flag(self):
        _as(1)
        page = self.client.get("/trends?todas=1").text
        self.assertIn('<details class="files mt-10" open>', page)
        self.assertIn('href="/trends?todas=1"', page)          # the "all" range button
        self.assertIn('href="/trends?r=7&amp;todas=1"', page)  # the "7 days" range button

    def test_todas_absent_renders_closed(self):
        _as(1)
        page = self.client.get("/trends").text
        self.assertIn('<details class="files mt-10">', page)

    def test_en_renders(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        db.set_language(conn, "en", 1)
        conn.commit()
        _as(1)
        page = self.client.get("/trends").text
        self.assertIn("See all measures (9 more)", page)
        self.assertIn("Sleep", page)
        self.assertIn("Energy", page)


class AssistantTrendsToolTest(unittest.TestCase):
    def test_leer_tendencias_accepts_sleep_rem_min(self):
        conn = _conn(self)
        db.init(conn)
        _seed_days(conn, 1, "sleep_rem_min", "2026-10-01", 14, 85, 0.3)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_tendencias", {"metric": "sleep_rem_min"}, "completo")
        self.assertIn("sleep_rem_min", out["metricas"])
        self.assertEqual(out["metricas"]["sleep_rem_min"]["dias_con_datos"], 14)


if __name__ == "__main__":
    unittest.main()
