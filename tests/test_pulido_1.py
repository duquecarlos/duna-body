"""tests/test_pulido_1.py: BODY-PULIDO-1 -- 44px tap targets on Hoy/soon's shift-card links, the
assistant's icon buttons, Ayuda's recorrido links and sin_acceso.html's links; Dia's cards and Como lo
calculo's part rows open their own measure on Tendencias with a way back (DESIGN rule 3), through the
same one way-back mechanism Turnos already uses. Invented demo data only, two persons."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, web
from body.i18n import T

PERSON_B = 2


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _mkdb(name: str):
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    db.init(conn)
    return conn


class DiaCardLinksTest(unittest.TestCase):
    """Decision 2: a Dia card whose measure has a Tendencias card and a value that day links there,
    with a way back to the same day; a card with no value, or no card on Tendencias, stays a plain div."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        self.conn = _mkdb("pulido_dia.db")
        self.addCleanup(self.conn.close)
        self.day = date(2026, 4, 1)
        db.set_metric(self.conn, self.day.isoformat(), "resting_hr", 55.0, "garmin", 1)
        db.set_metric(self.conn, self.day.isoformat(), "stress_avg", 20.0, "garmin", 1)
        db.set_metric(self.conn, self.day.isoformat(), "sleep_need_min", 460.0, "garmin", 1)
        self.conn.commit()
        _as(1)

    def test_default_metric_card_links_with_no_todas_flag(self):
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn(f'<a class="card" id="resting_hr" '
                      f'href="/trends?r=30&amp;desde=dia&amp;dia={self.day.isoformat()}#resting_hr">', page)

    def test_extra_group_metric_card_carries_todas_flag(self):
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn(f'<a class="card" id="stress_avg" href="/trends?r=30&amp;desde=dia&amp;'
                      f'dia={self.day.isoformat()}&amp;todas=1#stress_avg">', page)

    def test_metric_with_a_value_but_no_tendencias_card_stays_a_div(self):
        # sleep_need_min is deliberately never shown on Tendencias (web.py's TREND_GROUPS comment)
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn('<div class="card" id="sleep_need_min">', page)
        self.assertNotIn('id="sleep_need_min" href', page)

    def test_dash_card_has_no_link(self):
        # steps is one of TREND_KEYS' defaults but never given a value this day
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn('<div class="card empty" id="steps">', page)


class ComoRowLinksTest(unittest.TestCase):
    """Decision 3: a readiness part with a value, mapped to a metric with a Tendencias card, links its
    label to that measure with desde=como; a part with no value is never a link."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        self.conn = _mkdb("pulido_como.db")
        self.addCleanup(self.conn.close)
        self.day = date(2026, 4, 3)
        db.set_metric(self.conn, self.day.isoformat(), "body_battery_wake", 72.0, "garmin", 1)
        self.conn.commit()
        _as(1)

    def test_part_with_a_value_links_with_desde_como(self):
        page = self.client.get(f"/how/{self.day.isoformat()}").text
        self.assertIn('<a class="taplink" href="/trends?r=30&amp;desde=como&amp;'
                      f'dia={self.day.isoformat()}#body_battery_wake">', page)

    def test_part_without_a_value_is_not_a_link(self):
        # hrv was never given a value this day -- it is a missing-part row, never a link
        page = self.client.get(f"/how/{self.day.isoformat()}").text
        self.assertNotIn("desde=como", page.split("body_battery_wake", 1)[1])

    def test_english(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        page = self.client.get(f"/how/{self.day.isoformat()}").text
        self.assertIn('<a class="taplink" href="/trends?r=30&amp;desde=como&amp;'
                      f'dia={self.day.isoformat()}#body_battery_wake">', page)


class TrendsBackLinkTest(unittest.TestCase):
    """Decision 1: one way-back mechanism serves Turnos, Dia and Como; an unparseable dia or an
    unknown desde shows no back link; range links keep desde and dia together."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        self.conn = _mkdb("pulido_trends.db")
        self.addCleanup(self.conn.close)
        today = date.today()
        for i in range(20):
            d = (today - timedelta(i)).isoformat()
            db.set_metric(self.conn, d, "resting_hr", 58.0, "garmin", 1)
        self.conn.commit()
        _as(1)
        self.day = (today - timedelta(5)).isoformat()

    def test_back_link_for_desde_turnos(self):
        page = self.client.get("/trends?r=90&desde=turnos").text
        self.assertIn('<a class="taplink" href="/shifts">', page)
        self.assertIn(T("es")("trends.back_turnos"), page)

    def test_back_link_for_desde_dia(self):
        page = self.client.get(f"/trends?r=90&desde=dia&dia={self.day}").text
        self.assertIn(f'<a class="taplink" href="/day/{self.day}">', page)
        self.assertIn(T("es")("trends.back_dia"), page)

    def test_back_link_for_desde_como(self):
        page = self.client.get(f"/trends?r=90&desde=como&dia={self.day}").text
        self.assertIn(f'<a class="taplink" href="/how/{self.day}">', page)
        self.assertIn(T("es")("trends.back_como"), page)

    def test_back_link_in_english(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        page = self.client.get(f"/trends?r=90&desde=dia&dia={self.day}").text
        self.assertIn(T("en")("trends.back_dia"), page)

    def test_invalid_dia_shows_no_back_link(self):
        page = self.client.get("/trends?r=90&desde=dia&dia=not-a-date").text
        self.assertNotIn('taplink" href="/day/', page)
        self.assertNotIn(T("es")("trends.back_dia"), page)

    def test_desde_dia_without_a_dia_shows_no_back_link(self):
        page = self.client.get("/trends?r=90&desde=dia").text
        self.assertNotIn('taplink" href="/day/', page)

    def test_unknown_desde_shows_no_back_link(self):
        page = self.client.get(f"/trends?r=90&desde=mystery&dia={self.day}").text
        self.assertNotIn('taplink" href="/day/', page)
        self.assertNotIn('taplink" href="/how/', page)

    def test_range_links_keep_desde_and_dia(self):
        page = self.client.get(f"/trends?r=90&desde=dia&dia={self.day}").text
        self.assertIn(f'href="/trends?r=7&amp;desde=dia&amp;dia={self.day}"', page)
        self.assertIn(f'href="/trends?desde=dia&amp;dia={self.day}"', page)  # "all" carries no r=


class PersonScopeTest(unittest.TestCase):
    """Person B's data never appears on Person A's Dia or Como lo calculo."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        self.conn = _mkdb("pulido_scope.db")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO person (id, display_name, timezone, language) VALUES (2, 'B', "
                          "'America/Bogota', 'es')")
        self.day = date(2026, 4, 2)
        db.set_metric(self.conn, self.day.isoformat(), "resting_hr", 70.0, "garmin", PERSON_B)
        db.set_metric(self.conn, self.day.isoformat(), "body_battery_wake", 65.0, "garmin", PERSON_B)
        self.conn.commit()

    def test_person_a_sees_no_link_for_a_day_only_b_has_data_on(self):
        _as(1)
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn('<div class="card empty" id="resting_hr">', page)

    def test_person_b_sees_her_own_link(self):
        _as(PERSON_B)
        page = self.client.get(f"/day/{self.day.isoformat()}").text
        self.assertIn(f'href="/trends?r=30&amp;desde=dia&amp;dia={self.day.isoformat()}#resting_hr"', page)

    def test_person_a_sees_no_como_link_for_person_bs_part(self):
        _as(1)
        page = self.client.get(f"/how/{self.day.isoformat()}").text
        self.assertNotIn("desde=como", page)


class TapTargetsTest(unittest.TestCase):
    """Decision 4: .asis-icon-btn is a 44px target; the three hoy_turno_card links, Ayuda's recorrido
    links and sin_acceso.html's links carry the shared taplink class."""

    def test_asis_icon_btn_declares_44px_min_sizes(self):
        css = Path("body/static/body.css").read_text(encoding="utf-8")
        block = css.split(".asis-icon-btn{", 1)[1].split("}", 1)[0]
        self.assertIn("min-width:44px", block)
        self.assertIn("min-height:44px", block)

    def test_hoy_turno_card_links_carry_taplink(self):
        base_html = Path("body/templates/base.html").read_text(encoding="utf-8")
        macro = base_html.split("{% macro hoy_turno_card", 1)[1].split("{% endmacro %}", 1)[0]
        self.assertEqual(macro.count('class="note taplink"'), 3)

    def test_ayuda_recorrido_links_carry_taplink(self):
        html = Path("body/templates/ayuda.html").read_text(encoding="utf-8")
        self.assertIn('<a class="taplink" href="#{{ tarea.id }}">', html)

    def test_sin_acceso_links_carry_taplink(self):
        html = Path("body/templates/sin_acceso.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('class="taplink"'), 2)


if __name__ == "__main__":
    unittest.main()
