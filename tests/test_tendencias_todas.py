"""BODY-TENDENCIAS-TODAS-1: the rest of the measure catalogue behind "Ver todas las medidas", grouped by
plain-word topic (DESIGN rule 8). Invented demo data only, no real health figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, identity, profile, readiness, web
from body.agente import herramientas
from body.i18n import T
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
        self.assertEqual(set(groups["night"]),
                          {"sleep_spo2_avg", "sleep_respiration_avg", "sleep_hr_avg", "sleep_hr_min"})
        self.assertEqual(set(groups["energy"]),
                          {"body_battery_high", "body_battery_low", "body_battery_sleep_charge"})
        self.assertEqual(set(groups["stress"]), {"stress_avg", "high_stress_min"})
        self.assertEqual(set(groups["activity"]), {"intensity_minutes", "active_kcal"})
        self.assertEqual(set(groups["breathing"]), {"respiration_awake"})
        self.assertEqual(set(groups["commute"]),
                          {"vehicle_minutes", "vehicle_trips", "walking_minutes", "cycling_minutes"})


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

    def test_since_caption_carries_a_year(self):
        """BODY-REVISION-GRAFICOS-FIGURA-1 finding 11a: the "since" caption used t.long_date,
        which has no year -- comun._short_date does ("1 sep 2026" style), and the earliest day
        with any data here is 2026-09-01."""
        _as(1)
        page = self.client.get("/trends").text
        self.assertIn("1 sep 2026", page)

    def test_body_view_carries_hoy_figura_cap(self):
        """BODY-REVISION-GRAFICOS-FIGURA-1 finding 15/ruling 1: Trends' body view used no height
        cap at all, unlike Today's -- `hoy-figura` is Today's own class, reused here rather than
        a second rule."""
        _as(1)
        page = self.client.get("/trends").text
        self.assertIn("hoy-figura", page)

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
        # BODY-DESPLAZAMIENTO-3: the commute group's own "never imported" freshness line is the
        # one extra thing every person sees, even with zero measured extras otherwise.
        self.assertIn("Ver todas las medidas", page_b)

    def test_no_control_when_no_extra_metric_has_data(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        _as(PERSON_B)
        _seed_days(conn, PERSON_B, "resting_hr", "2026-09-01", 20, 60, -0.1)
        _seed_days(conn, PERSON_B, "sleep_minutes", "2026-09-01", 20, 400, 1)
        conn.commit()
        page_b = self.client.get("/trends").text
        # BODY-DESPLAZAMIENTO-3: the details control itself now appears for the commute group's
        # own freshness line, but with nothing else inside it.
        self.assertIn('<details class="files mt-10">', page_b)
        self.assertIn("Aún no hay datos de desplazamiento", page_b)
        self.assertNotIn('id="sleep_score"', page_b)

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


class MinutesByUnitTest(unittest.TestCase):
    """BODY-DESPLAZAMIENTO-3-M fix 3: a card's unit/minute formatting is read from the metric's
    own declared unit (db.METRIC_DEFS), never from the key's spelling -- sleep_hr_min is bpm
    (the key ends in "_min" but is not minutes); vehicle_minutes/walking_minutes/cycling_minutes
    are minutes (the key ends in "_minutes", not "_min", but the unit is "min")."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "minutos.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        _seed_days(conn, 1, "resting_hr", "2026-09-01", 20, 58, -0.2)
        _seed_days(conn, 1, "sleep_minutes", "2026-09-01", 20, 410, 1)
        _seed_days(conn, 1, "sleep_hr_min", "2026-09-01", 20, 50, 0.1)
        _seed_days(conn, 1, "sleep_awake_min", "2026-09-01", 20, 15, 0.2)
        _seed_days(conn, 1, "vehicle_minutes", "2026-09-01", 20, 18, 0.3)
        db.set_language(conn, "en", 1)
        conn.commit()
        _as(1)
        self.client = TestClient(web.app, follow_redirects=False)

    @staticmethod
    def _chunk(page: str, key: str) -> str:
        start = page.index(f'id="{key}"')
        return page[start:page.index("</div>", page.index('class="legend mt-4"', start))]

    def test_sleep_hr_min_renders_bpm_not_minutes(self):
        page = self.client.get("/trends").text
        chunk = self._chunk(page, "sleep_hr_min")
        self.assertIn("bpm", chunk)
        self.assertNotRegex(chunk, r"\d+\s*min\b")

    def test_vehicle_minutes_normal_range_matches_sleep_awake_min_s_minute_formatting(self):
        page = self.client.get("/trends").text
        chunk_v = self._chunk(page, "vehicle_minutes")
        chunk_s = self._chunk(page, "sleep_awake_min")
        self.assertRegex(chunk_v, r"\d+ min–\d+ min")
        self.assertRegex(chunk_s, r"\d+ min–\d+ min")

    def test_vehicle_minutes_delta_shows_minutes_not_a_bare_number(self):
        page = self.client.get("/trends").text
        chunk = self._chunk(page, "vehicle_minutes")
        if "delta good" in chunk or "delta bad" in chunk:
            self.assertRegex(chunk, r"[↑↓] \d+ min ")


class ReadinessChartReachesTheSameEndAsOtherSeriesTest(unittest.TestCase):
    """BODY-REVISION-SYNC-READINESS-1 ruling 4: once readiness is stored for the stored history
    (ruling 1's own fix), its own Tendencias monthly line reaches the SAME last month as any
    other series -- never ending months earlier, the gap finding 11c named. Synthetic data only,
    no real health figures."""

    def test_readiness_last_month_matches_another_series_last_month(self):
        conn = _conn(self)
        db.init(conn)
        start, n = "2026-08-01", 60
        for key, base in (("hrv_last_night", 50.0), ("resting_hr", 55.0), ("stress_avg", 30.0),
                           ("sleep_minutes", 450.0), ("body_battery_wake", 75.0)):
            _seed_days(conn, 1, key, start, n, base)
        conn.commit()
        readiness.recompute_all(conn, 1)
        conn.commit()
        p_readiness = profile.metric_profile(conn, "readiness", person_id=1)
        p_resting_hr = profile.metric_profile(conn, "resting_hr", person_id=1)
        self.assertIsNotNone(p_readiness)
        self.assertIsNotNone(p_resting_hr)
        self.assertEqual(p_readiness.months[-1][0], p_resting_hr.months[-1][0])


def _activity(conn, person_id: int, source_id: str, day: str, start_local: str, type_: str = "running",
              avg_hr: float | None = None) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, avg_hr, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'garmin')",
        (person_id, source_id, day, start_local, type_, avg_hr),
    )


class TrendsAyunoCardTest(unittest.TestCase):
    """BODY-AYUNO-ENTRENO-1 ruling 3: Trends' own aggregate card, "Your fasted sessions vs. fed" --
    omitted outright with no training session at all, and its three states otherwise (both
    buckets, the fast-window figure alone, or its honest empty line)."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "ayuno_trends.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        # Trends shows the "not synced yet" page with no metric data at all -- enough of the
        # default cards for the page itself to render past that early return.
        _seed_days(self.conn, 1, "resting_hr", "2026-01-01", 20, 58, -0.2)
        _seed_days(self.conn, 1, "sleep_minutes", "2026-01-01", 20, 410, 1)
        self.conn.commit()
        _as(1)
        self.client = TestClient(web.app, follow_redirects=False)

    def _seed_pool(self, n_ayunas: int, n_alimentado: int) -> None:
        from datetime import date, timedelta
        cursor = date(2026, 1, 2)
        for i in range(n_ayunas):
            day = cursor.isoformat()
            _activity(self.conn, 1, f"ayunas-{i}", day, f"{day}T07:00:00", avg_hr=150 + i)
            dia_ant = (cursor - timedelta(1)).isoformat()
            db.registrar_ingesta(self.conn, 1, dia_ant, "protein_g", 20.0, False, ts_local=f"{dia_ant}T22:00:00")
            cursor += timedelta(1)
        for i in range(n_alimentado):
            day = cursor.isoformat()
            _activity(self.conn, 1, f"alim-{i}", day, f"{day}T07:00:00", avg_hr=130 + i)
            db.registrar_ingesta(self.conn, 1, day, "protein_g", 20.0, False, ts_local=f"{day}T05:00:00")
            cursor += timedelta(1)
        self.conn.commit()

    def test_card_omitted_with_no_training_session_at_all(self):
        page = self.client.get("/trends").text
        self.assertNotIn('id="ayuno"', page)

    def test_both_buckets_show_rows(self):
        self._seed_pool(n_ayunas=3, n_alimentado=3)
        page = self.client.get("/trends").text
        t_es = T("es")
        self.assertIn('id="ayuno"', page)
        self.assertIn(t_es("trends.ayuno.titulo"), page)
        bpm = t_es("unit.bpm")
        self.assertIn(t_es("trends.ayuno.fila_en_ayunas", valor=f"{t_es.num(151)} {bpm}"), page)
        self.assertIn(t_es("trends.ayuno.fila_alimentado", valor=f"{t_es.num(131)} {bpm}"), page)

    def test_only_the_fast_window_when_no_type_qualifies_on_both_buckets(self):
        self._seed_pool(n_ayunas=3, n_alimentado=0)  # all three fasted exactly 9h -- median 9.0
        page = self.client.get("/trends").text
        t_es = T("es")
        self.assertIn('id="ayuno"', page)
        self.assertIn(t_es("trends.ayuno.ventana", horas=t_es.minutes(9 * 60)), page)
        self.assertIn(t_es("trends.ayuno.faltan"), page)

    def test_honest_empty_line_when_nothing_qualifies_at_all(self):
        _activity(self.conn, 1, "solo", "2026-01-05", "2026-01-05T07:00:00", avg_hr=140)
        self.conn.commit()
        page = self.client.get("/trends").text
        t_es = T("es")
        self.assertIn('id="ayuno"', page)
        self.assertIn(t_es("trends.ayuno.faltan"), page)


if __name__ == "__main__":
    unittest.main()
