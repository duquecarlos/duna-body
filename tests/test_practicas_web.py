"""BODY-HISTORIA-VIDA-2 ruling 2: the "Tus prácticas" section on /history and its
/history/practica/{id} drill-down -- list, suggestion one-tap creation, detail (rule text,
streaks, the day grid honest about gaps, temporadas), 404 on another person's id, archive.
Invented demo data only, never a real figure."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, practicas, web

client = TestClient(web.app, follow_redirects=False)


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "p.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _activity(conn, person_id, source_id, day, *, type):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, type, duration_s, source) VALUES (?, ?, ?, ?, 1800, 'garmin')",
        (person_id, source_id, day, type),
    )


def _con_cobertura(conn, person_id: int = 1, day: str = "2026-11-10"):
    """history.coverage() needs >= 1 day of garmin daily_metric data, or /history renders
    soon.html instead of the real template -- every test below that reads the real page seeds
    this first."""
    db.set_metric(conn, day, "resting_hr", 60.0, "garmin", person_id)


class HistorySectionTest(unittest.TestCase):
    def test_empty_state_when_no_practices_or_suggestions(self):
        conn = _reset_db(self)
        _con_cobertura(conn)
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Todavía no tienes ninguna práctica.", r.text)

    def test_active_practice_is_listed_with_its_link(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        _con_cobertura(conn)
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Correr", r.text)
        self.assertIn(f"/history/practica/{pid}", r.text)

    def test_archived_practice_is_not_listed(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        practicas.archivar(conn, 1, pid)
        _con_cobertura(conn)
        conn.commit()
        r = client.get("/history")
        self.assertNotIn(f"/history/practica/{pid}", r.text)

    def test_suggestion_appears_after_enough_weeks_and_can_be_created_one_tap(self):
        conn = _reset_db(self)
        hoy = date(2026, 11, 10)
        for i in range(9):
            _activity(conn, 1, f"c{i}", (hoy - timedelta(days=i * 7)).isoformat(), type="cycling")
        _con_cobertura(conn)
        conn.commit()
        r = client.get("/history")
        self.assertIn('action="/history/practicas"', r.text)

        before = conn.execute("SELECT COUNT(*) FROM practica").fetchone()[0]
        resp = client.post("/history/practicas", data={
            "fuente": "actividad", "regla": json.dumps({"tipos": ["cycling"]}), "titulo": "Ciclismo",
        })
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/history")
        after = conn.execute("SELECT COUNT(*) FROM practica").fetchone()[0]
        self.assertEqual(after, before + 1)
        row = conn.execute("SELECT titulo, origen FROM practica WHERE titulo = 'Ciclismo'").fetchone()
        self.assertEqual(row["origen"], "persona")

    def test_create_with_an_invalid_rule_writes_nothing(self):
        conn = _reset_db(self)
        before = conn.execute("SELECT COUNT(*) FROM practica").fetchone()[0]
        resp = client.post("/history/practicas", data={
            "fuente": "actividad", "regla": json.dumps({"tipos": []}), "titulo": "Nada",
        })
        self.assertEqual(resp.status_code, 303)
        after = conn.execute("SELECT COUNT(*) FROM practica").fetchone()[0]
        self.assertEqual(after, before)

    def test_create_with_malformed_json_redirects_without_raising(self):
        _reset_db(self)
        resp = client.post("/history/practicas", data={
            "fuente": "actividad", "regla": "not-json", "titulo": "Nada",
        })
        self.assertEqual(resp.status_code, 303)


class DetailTest(unittest.TestCase):
    def test_unknown_id_is_404(self):
        _reset_db(self)
        r = client.get("/history/practica/9999")
        self.assertEqual(r.status_code, 404)

    def test_another_persons_practica_is_404(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 2, "Ajena", "actividad", {"tipos": ["running"]})
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertEqual(r.status_code, 404)

    def test_brand_new_practice_shows_the_no_data_empty_state(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Todavía no hay datos de tu reloj para esta práctica.", r.text)

    def test_detail_shows_rule_streak_and_week_grid_with_honest_gaps(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        hoy = date(2026, 11, 10)
        _activity(conn, 1, "r1", hoy.isoformat(), type="running")
        _activity(conn, 1, "r2", (hoy - timedelta(days=1)).isoformat(), type="running")
        # a day with watch data but no run -- a genuine no_hecho, not a gap
        conn.execute("INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES "
                      "(1, ?, '2026-11-07T23:00', '2026-11-08T06:00')", ((hoy - timedelta(days=3)).isoformat(),))
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Correr", r.text)
        self.assertIn("Actividad: ", r.text)
        self.assertIn("Meta: 3 días por semana", r.text)
        self.assertIn("p-hecho", r.text)
        self.assertIn("p-sin_dato", r.text)
        self.assertIn(f'href="/day/{hoy.isoformat()}"', r.text)

    def test_detail_bounds_the_day_grid_to_90_days(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        old_day = "2025-01-01"
        _activity(conn, 1, "old", old_day, type="running")
        _activity(conn, 1, "recent", "2026-11-10", type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertNotIn(f'href="/day/{old_day}"', r.text)
        self.assertIn('href="/day/2026-11-10"', r.text)

    def test_archived_practica_detail_shows_badge_and_no_archive_button(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        practicas.archivar(conn, 1, pid)
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Archivada", r.text)
        self.assertNotIn(f'/history/practica/{pid}/archive', r.text)

    def test_seasons_section_lists_a_season_with_context_and_day_links(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        monday = date(2026, 8, 3)
        for week in range(4):
            wk_monday = monday + timedelta(weeks=week)
            for i in range(3):
                _activity(conn, 1, f"r{week}-{i}", (wk_monday + timedelta(days=i)).isoformat(), type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Temporadas", r.text)
        self.assertIn(f"/day/{monday.isoformat()}", r.text)


class ArchiveActionTest(unittest.TestCase):
    def test_archiving_removes_it_from_the_history_list(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        conn.commit()
        resp = client.post(f"/history/practica/{pid}/archive")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/history")
        row = conn.execute("SELECT archivada_en FROM practica WHERE id = ?", (pid,)).fetchone()
        self.assertIsNotNone(row["archivada_en"])
        r = client.get("/history")
        self.assertNotIn(f"/history/practica/{pid}", r.text)

    def test_archiving_another_persons_practica_is_404(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 2, "Ajena", "actividad", {"tipos": ["running"]})
        conn.commit()
        resp = client.post(f"/history/practica/{pid}/archive")
        self.assertEqual(resp.status_code, 404)
        row = conn.execute("SELECT archivada_en FROM practica WHERE id = ?", (pid,)).fetchone()
        self.assertIsNone(row["archivada_en"])

    def test_archiving_twice_is_404_the_second_time(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        conn.commit()
        self.assertEqual(client.post(f"/history/practica/{pid}/archive").status_code, 303)
        self.assertEqual(client.post(f"/history/practica/{pid}/archive").status_code, 404)


class LanguageTest(unittest.TestCase):
    def test_english_history_and_detail(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        # metrica_umbral, not actividad: _con_cobertura's own garmin day only touches resting_hr,
        # so this practica's own [first, last] (daily_metric WHERE metric_key = 'steps') stays
        # empty -- unlike an 'actividad' practica, which would read that same garmin day as its
        # own "day with data" (practicas._primero_ultimo_dato's own UNION).
        pid = practicas.crear(conn, 1, "Steps", "metrica_umbral",
                               {"metric_key": "steps", "op": ">=", "valor": 8000})
        _con_cobertura(conn)
        conn.commit()
        r = client.get("/history")
        self.assertIn("Your practices", r.text)
        r2 = client.get(f"/history/practica/{pid}")
        self.assertIn("No watch data for this practice yet.", r2.text)


if __name__ == "__main__":
    unittest.main()
