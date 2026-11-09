"""BODY-SUENO-RESPIRACION-VISTA-1: Día's one honest line for the four night figures
BODY-ENTRENO-SUENO-STORE-1 already pulls (sleep_spo2_avg, sleep_respiration_avg/_low/_high), with
the caveat that this is a proxy, never a snoring or apnea reading (BODY-ENTRENO-1-INV Q6, DESIGN
rule 12) -- and Tendencias' new "night" group for the trend of two of them. Invented demo data only.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, web

CAVEAT_ES = "no oye el ronquido"
CAVEAT_EN = "can&#39;t hear snoring"


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _mkdb(name: str):
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    db.init(conn)
    return conn


class DiaLineaTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        self.conn = _mkdb("sueno_resp_dia.db")
        self.addCleanup(self.conn.close)
        self.day = date.today() - timedelta(10)
        _as(1)

    def _set(self, key: str, value: float) -> None:
        db.set_metric(self.conn, self.day.isoformat(), key, value, "garmin", 1)

    def test_all_four_render_one_line_and_caveat_outside_the_grid(self):
        self._set("sleep_minutes", 420.0)
        self._set("sleep_spo2_avg", 96.0)
        self._set("sleep_respiration_low", 11.0)
        self._set("sleep_respiration_high", 19.0)
        self._set("sleep_respiration_avg", 14.0)
        self.conn.commit()
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn("Oxígeno en sangre 96 %", page)
        self.assertIn("respiración 11–19 rpm (prom. 14)", page)
        self.assertIn(CAVEAT_ES, page)
        for key in ("sleep_spo2_avg", "sleep_respiration_avg", "sleep_respiration_low",
                    "sleep_respiration_high"):
            self.assertNotIn(f'id="{key}"', page)

    def test_only_spo2_shows_just_that_part(self):
        self._set("sleep_minutes", 420.0)
        self._set("sleep_spo2_avg", 95.0)
        self.conn.commit()
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn("Oxígeno en sangre 95 %", page)
        self.assertNotIn("respiración", page)
        self.assertIn(CAVEAT_ES, page)

    def test_none_of_the_four_shows_no_line_and_no_caveat(self):
        self._set("sleep_minutes", 420.0)
        self.conn.commit()
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertNotIn("Oxígeno en sangre", page)
        self.assertNotIn("respiración", page)
        self.assertNotIn(CAVEAT_ES, page)

    def test_figures_without_a_sleep_card_get_their_own_small_card(self):
        self._set("sleep_spo2_avg", 94.0)
        self.conn.commit()
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertNotIn('id="sleep_minutes"', page)
        self.assertIn("Oxígeno en sangre 94 %", page)
        self.assertIn(CAVEAT_ES, page)

    def test_en_renders(self):
        db.set_language(self.conn, "en", 1)
        self._set("sleep_minutes", 420.0)
        self._set("sleep_spo2_avg", 96.0)
        self._set("sleep_respiration_low", 11.0)
        self._set("sleep_respiration_high", 19.0)
        self._set("sleep_respiration_avg", 14.0)
        self.conn.commit()
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn("Blood oxygen 96 %", page)
        self.assertIn("breathing 11–19 brpm (avg 14)", page)
        self.assertIn(CAVEAT_EN, page)


def _seed_days(conn, person_id: int, key: str, start: str, n: int, base: float, step: float = 0.1) -> None:
    year, month, day = (int(x) for x in start.split("-"))
    d0 = date(year, month, day)
    for i in range(n):
        db.set_metric(conn, (d0 + timedelta(i)).isoformat(), key, base + i * step, "garmin", person_id)


class TendenciasNocheTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        self.conn = _mkdb("sueno_resp_trends.db")
        self.addCleanup(self.conn.close)
        # the default 9 need enough rows for the page to render cards at all
        _seed_days(self.conn, 1, "resting_hr", "2026-09-01", 20, 58, -0.2)
        _seed_days(self.conn, 1, "sleep_minutes", "2026-09-01", 20, 410, 1)
        _seed_days(self.conn, 1, "sleep_spo2_avg", "2026-09-01", 20, 95, 0.1)
        _seed_days(self.conn, 1, "sleep_respiration_avg", "2026-09-01", 20, 14, 0.05)
        self.conn.commit()
        _as(1)

    def test_night_group_lists_both_cards_with_the_caveat(self):
        page = self.client.get("/trends?todas=1").text
        details_at = page.index('<details class="files mt-10"')
        self.assertIn("De noche", page)
        for key in ("sleep_spo2_avg", "sleep_respiration_avg"):
            self.assertIn(f'id="{key}"', page)
            self.assertGreater(page.index(f'id="{key}"'), details_at)
        self.assertIn(CAVEAT_ES, page)
        self.assertEqual(page.count(CAVEAT_ES), 2)

    def test_en_renders(self):
        db.set_language(self.conn, "en", 1)
        page = self.client.get("/trends?todas=1").text
        self.assertIn("At night", page)
        self.assertIn("How is your blood oxygen at night?", page)
        self.assertIn("How do you breathe at night?", page)
        self.assertIn(CAVEAT_EN, page)


if __name__ == "__main__":
    unittest.main()
