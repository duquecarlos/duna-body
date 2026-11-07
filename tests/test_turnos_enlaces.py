"""tests/test_turnos_enlaces.py: BODY-TURNOS-ENLACES-1 -- each row of Turnos' cost table links to its
own measure on Tendencias (90 days, night shifts shaded, anchored to its card) with a way back, and
Dia says where a day sits relative to a night block, read from confirmed shift rows and the turnos-v1
model, never stored. Invented demo data only, two persons."""
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, turnos_costo, web
from body.i18n import T

PERSON_B = 2
CONST = {"sleep_minutes": 420.0, "resting_hr": 55.0, "hrv_last_night": 60.0, "stress_avg": 25.0,
         "body_battery_wake": 75.0}


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _mkconn():
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


def _insert_shift(conn, person_id: int, day: str, kind: str = "night") -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _fill_range(conn, person_id: int, start: date, end: date, values: dict[str, float]) -> None:
    d = start
    while d <= end:
        for m, v in values.items():
            db.set_metric(conn, d.isoformat(), m, v, "garmin", person_id)
        d += timedelta(1)


def _seed_block(conn, person_id: int, inicio: date, dias: int) -> date:
    """A night block with values on every one of its own nights -- enough for every one of the 5 cost
    rows to get a figure. The exact cost numbers are BODY-TURNOS-COSTO-1's own tests' concern; these
    tests check the links the rows carry, not what they say."""
    fin = inicio + timedelta(dias - 1)
    d = inicio
    while d <= fin:
        _insert_shift(conn, person_id, d.isoformat())
        for m, v in CONST.items():
            db.set_metric(conn, d.isoformat(), m, v + 5, "garmin", person_id)
        d += timedelta(1)
    return fin


class CostRowLinksTest(unittest.TestCase):
    """Decision 1: each row with data links to /trends?r=90&desde=turnos#<key>, with todas=1 added
    for a metric that lives behind "Ver todas las medidas" (read from TREND_GROUPS, not duplicated)."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "enlaces_filas.db"
        conn = db.connect()
        db.init(conn)
        today = date.today()
        _fill_range(conn, 1, today - timedelta(140), today - timedelta(1), CONST)
        _seed_block(conn, 1, today - timedelta(20), 5)
        conn.commit()
        _as(1)

    def test_default_metric_row_links_with_no_todas_flag(self):
        # resting_hr is one of TREND_KEYS' default 9 -- not behind "Ver todas las medidas"
        page = self.client.get("/shifts").text
        self.assertIn('href="/trends?r=90&amp;desde=turnos#resting_hr"', page)

    def test_extra_group_metric_row_carries_todas_flag(self):
        # stress_avg lives in TREND_GROUPS' "stress" group
        page = self.client.get("/shifts").text
        self.assertIn('href="/trends?r=90&amp;desde=turnos&amp;todas=1#stress_avg"', page)

    def test_every_row_href_is_free_of_any_figure(self):
        page = self.client.get("/shifts").text
        hrefs = re.findall(r'<a class="costrow" href="([^"]+)">', page)
        self.assertEqual(len(hrefs), len(turnos_costo.MODELO["metricas"]))  # every metric got a figure
        for href in hrefs:
            self.assertRegex(href, r"^/trends\?r=90&amp;desde=turnos(&amp;todas=1)?#[a-z_]+$")

    def test_row_is_a_44px_target(self):
        self.assertIn(".costrow{", Path("body/static/body.css").read_text(encoding="utf-8"))
        self.assertIn("min-height:44px", Path("body/static/body.css").read_text(encoding="utf-8")
                      .split(".costrow{", 1)[1].split("}", 1)[0])


class CostRowNoDataTest(unittest.TestCase):
    """A row with no figure ('--') never links anywhere."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "enlaces_vacio.db"
        conn = db.connect()
        db.init(conn)
        conn.commit()
        _as(1)

    def test_no_link_and_the_em_dash_row_remains(self):
        page = self.client.get("/shifts").text
        self.assertNotIn('class="costrow"', page)
        self.assertIn(">—<div", page)
        self.assertIn(T("es")("shifts.sin_dato_corto"), page)


class TrendsBackLinkTest(unittest.TestCase):
    """Decision 2: desde=turnos shows the way back, kept on every range link; without it, nothing
    changes on Tendencias."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "enlaces_trends.db"
        conn = db.connect()
        db.init(conn)
        today = date.today()
        for i in range(20):
            d = (today - timedelta(i)).isoformat()
            db.set_metric(conn, d, "resting_hr", 58.0, "garmin", 1)
            db.set_metric(conn, d, "sleep_minutes", 410.0, "garmin", 1)
        conn.commit()
        _as(1)

    def test_back_link_shown_when_desde_turnos(self):
        page = self.client.get("/trends?r=90&desde=turnos").text
        self.assertIn('<a href="/shifts">', page)
        self.assertIn(T("es")("trends.back_turnos"), page)

    def test_range_links_keep_desde(self):
        page = self.client.get("/trends?r=90&desde=turnos").text
        self.assertIn('href="/trends?r=7&amp;desde=turnos"', page)
        self.assertIn('href="/trends?desde=turnos"', page)  # the "all" range button carries no r=

    def test_no_back_link_without_desde(self):
        page = self.client.get("/trends?r=90").text
        self.assertNotIn(T("es")("trends.back_turnos"), page)


class ContextoDiaTest(unittest.TestCase):
    """Decision 3: turnos_costo.contexto_dia, a pure read over confirmed shift rows."""

    def test_third_night_of_a_five_night_block(self):
        conn = _mkconn()
        inicio = date(2026, 3, 10)
        for i in range(5):
            _insert_shift(conn, 1, (inicio + timedelta(i)).isoformat())
        dia = inicio + timedelta(2)
        self.assertEqual(turnos_costo.contexto_dia(conn, 1, dia), {"tipo": "noche", "n": 3, "de": 5})

    def test_second_day_after_the_blocks_last_night(self):
        conn = _mkconn()
        inicio = date(2026, 3, 10)
        for i in range(5):
            _insert_shift(conn, 1, (inicio + timedelta(i)).isoformat())
        fin = inicio + timedelta(4)
        dia = fin + timedelta(2)
        _insert_shift(conn, 1, dia.isoformat(), kind="day")
        self.assertEqual(turnos_costo.contexto_dia(conn, 1, dia), {"tipo": "despues", "n": 2})

    def test_a_day_outside_the_recovery_window_is_none(self):
        conn = _mkconn()
        inicio = date(2026, 3, 10)
        for i in range(5):
            _insert_shift(conn, 1, (inicio + timedelta(i)).isoformat())
        fin = inicio + timedelta(4)
        dia = fin + timedelta(turnos_costo.MODELO["ventana_post"] + 1)
        self.assertIsNone(turnos_costo.contexto_dia(conn, 1, dia))

    def test_a_day_with_no_shifts_at_all_is_none(self):
        conn = _mkconn()
        self.assertIsNone(turnos_costo.contexto_dia(conn, 1, date(2026, 5, 1)))

    def test_person_b_shifts_never_change_person_as_context(self):
        conn = _mkconn()
        conn.execute("INSERT INTO person (id, display_name, timezone, language) VALUES (2, 'B', "
                     "'America/Bogota', 'es')")
        inicio = date(2026, 3, 10)
        for i in range(5):
            _insert_shift(conn, PERSON_B, (inicio + timedelta(i)).isoformat())
        dia = inicio + timedelta(2)
        self.assertIsNone(turnos_costo.contexto_dia(conn, 1, dia))
        self.assertEqual(turnos_costo.contexto_dia(conn, PERSON_B, dia), {"tipo": "noche", "n": 3, "de": 5})


class DiaRendersContextTest(unittest.TestCase):
    """Decision 3: Dia renders the block-position line under its shift line, in es and en, with the
    link to /shifts."""

    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "enlaces_dia.db"
        self.conn = db.connect()
        db.init(self.conn)
        self.inicio = date(2026, 3, 10)
        for i in range(5):
            _insert_shift(self.conn, 1, (self.inicio + timedelta(i)).isoformat())
        self.conn.commit()
        _as(1)

    def test_renders_noche_in_spanish_with_link(self):
        dia = self.inicio + timedelta(2)
        page = self.client.get(f"/day/{dia.isoformat()}").text
        self.assertIn("Noche 3 de 5 de este bloque", page)
        self.assertIn('<a href="/shifts"', page)
        self.assertIn(T("es")("day.shift_ctx_link"), page)

    def test_renders_despues_in_spanish_with_ordinal(self):
        fin = self.inicio + timedelta(4)
        dia = fin + timedelta(2)
        _insert_shift(self.conn, 1, dia.isoformat(), kind="day")
        self.conn.commit()
        page = self.client.get(f"/day/{dia.isoformat()}").text
        self.assertIn("2.º día después de tus noches", page)

    def test_renders_noche_in_english(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        dia = self.inicio + timedelta(2)
        page = self.client.get(f"/day/{dia.isoformat()}").text
        self.assertIn("Night 3 of 5 in this block", page)
        self.assertIn(T("en")("day.shift_ctx_link"), page)

    def test_renders_despues_in_english_without_an_ordinal(self):
        db.set_language(self.conn, "en", 1)
        fin = self.inicio + timedelta(4)
        dia = fin + timedelta(2)
        _insert_shift(self.conn, 1, dia.isoformat(), kind="day")
        self.conn.commit()
        page = self.client.get(f"/day/{dia.isoformat()}").text
        self.assertIn("Day 2 after your nights", page)

    def test_no_block_context_line_far_from_any_block(self):
        dia = self.inicio + timedelta(90)
        page = self.client.get(f"/day/{dia.isoformat()}").text
        self.assertNotIn(T("es")("day.shift_ctx_link"), page)


if __name__ == "__main__":
    unittest.main()
