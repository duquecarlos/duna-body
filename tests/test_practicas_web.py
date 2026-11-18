"""BODY-HISTORIA-VIDA-2 ruling 2: the "Tus prácticas" section on /history and its
/history/practica/{id} drill-down -- list, suggestion one-tap creation, detail (rule text,
streaks, the day grid honest about gaps, temporadas), 404 on another person's id, archive.
BODY-HISTORIA-VIDA-3: the drill-down's own main visual is the whole history in weeks (ruling 1),
an edit form for titulo/meta_dias_semana (ruling 2), the weekly-target label and the "En toda tu
historia" sentence with shift counts (rulings 3-4). Invented demo data only, never a real figure."""
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, practicas, web

ROOT = Path(__file__).resolve().parent.parent

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
        self.assertIn("Objetivo semanal: 3 días", r.text)
        self.assertIn("p-hecho", r.text)
        self.assertIn("p-sin_dato", r.text)
        self.assertIn(f'href="/day/{hoy.isoformat()}"', r.text)

    def test_detail_bounds_the_recent_day_grid_to_the_last_4_weeks(self):
        """BODY-HISTORIA-VIDA-3 ruling 1: "Últimas 4 semanas" replaces the old 90-day grid --
        only the last 28 days get a `/day/{day}` cell; the weekly strip above it still covers the
        whole history (BODY-HISTORIA-VIDA-2's own 90-day bound is gone)."""
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        old_day = "2025-01-01"
        _activity(conn, 1, "old", old_day, type="running")
        _activity(conn, 1, "recent", "2026-11-10", type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("Últimas 4 semanas", r.text)
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
        self.assertNotIn("/goals/new?practica_id=", r.text)

    def test_an_active_practica_shows_a_retomar_link(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'href="/goals/new?practica_id={pid}"', r.text)

    def test_seasons_section_lists_a_season_with_context_and_day_links(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        monday = date(2026, 8, 3)
        for week in range(4):
            wk_monday = monday + timedelta(weeks=week)
            # 4 running days: >= meta_dias_semana (3) and >= MIN_DIAS_CON_DATO (4), so the week
            # reads as "cumple", never "semana_sin_dato" -- a real temporada, not an artifact of
            # too little watch coverage that week.
            for i in range(4):
                _activity(conn, 1, f"r{week}-{i}", (wk_monday + timedelta(days=i)).isoformat(), type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Temporadas", r.text)
        self.assertIn(f"/day/{monday.isoformat()}", r.text)
        self.assertIn("w-season", r.text)  # BODY-HISTORIA-VIDA-3 ruling 1: outlined on the weekly strip


class WeeklyStripTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-3 ruling 1: the drill-down's own main visual -- the whole history in
    weeks, a cell's fill proportional to hecho/meta_dias_semana, a week with too little watch
    coverage drawn as a dashed gap, never a colour of failure."""

    def test_met_partial_empty_and_gap_weeks_each_render_their_class(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        base = date(2026, 11, 2)
        monday0 = base - timedelta(days=base.weekday())
        # week 0: met -- 4 running days (>= MIN_DIAS_CON_DATO so it is not a gap), >= the weekly target
        for i in range(4):
            _activity(conn, 1, f"met{i}", (monday0 + timedelta(days=i)).isoformat(), type="running")
        # week 1: gap -- deliberately no watch data at all this week (no rows seeded for it)
        # week 2: partial -- 1 running day plus enough OTHER watch data to avoid a gap (>= 4 data days)
        monday2 = monday0 + timedelta(weeks=2)
        _activity(conn, 1, "partial-run", monday2.isoformat(), type="running")
        for i in range(1, 4):
            _activity(conn, 1, f"partial-walk{i}", (monday2 + timedelta(days=i)).isoformat(), type="walking")
        # week 3: empty -- 0 running days, but >= 4 data days (so it is not a gap either)
        monday3 = monday0 + timedelta(weeks=3)
        for i in range(4):
            _activity(conn, 1, f"empty-walk{i}", (monday3 + timedelta(days=i)).isoformat(), type="walking")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tu historia, semana a semana", r.text)
        self.assertIn("w-met", r.text)
        self.assertIn("w-partial", r.text)
        self.assertIn("w-empty", r.text)
        self.assertIn("w-gap", r.text)


class NoFailureColorInPracticeStylesTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-3 ruling 1: "a missing day is not a failed day" -- neither the day grid
    nor the weekly strip may use --danger anywhere in their own styles."""

    def test_no_danger_variable_in_practice_or_weekly_styles(self):
        css = (ROOT / "body" / "static" / "body.css").read_text(encoding="utf-8")
        offenders = [line for line in css.splitlines()
                     if "{" in line and "--danger" in line
                     and (".p-no_hecho" in line or ".p-sin_dato" in line or ".p-hecho" in line
                          or ".week-cell" in line or ".season-row" in line)]
        self.assertEqual(offenders, [], f"a failure colour leaked into a practice style: {offenders}")


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


class EditActionTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-3 ruling 2: an edit form for titulo and the weekly target, validated the
    same way `crear` validates them, 404 on another person's practica."""

    def test_editing_changes_title_and_weekly_target(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        conn.commit()
        resp = client.post(f"/history/practica/{pid}", data={"titulo": "Correr fuerte", "meta_dias_semana": "5"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], f"/history/practica/{pid}")
        row = conn.execute("SELECT titulo, meta_dias_semana FROM practica WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["titulo"], "Correr fuerte")
        self.assertEqual(row["meta_dias_semana"], 5)
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("Correr fuerte", r.text)
        self.assertIn("Objetivo semanal: 5 días", r.text)

    def test_editing_with_an_out_of_range_target_is_refused(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        conn.commit()
        resp = client.post(f"/history/practica/{pid}", data={"titulo": "Correr", "meta_dias_semana": "8"})
        self.assertEqual(resp.status_code, 303)
        row = conn.execute("SELECT meta_dias_semana FROM practica WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["meta_dias_semana"], 3)

    def test_editing_with_an_empty_title_is_refused(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        conn.commit()
        client.post(f"/history/practica/{pid}", data={"titulo": "   ", "meta_dias_semana": "4"})
        row = conn.execute("SELECT titulo FROM practica WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["titulo"], "Correr")

    def test_editing_another_persons_practica_is_404(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 2, "Ajena", "actividad", {"tipos": ["running"]})
        conn.commit()
        resp = client.post(f"/history/practica/{pid}", data={"titulo": "Robada", "meta_dias_semana": "4"})
        self.assertEqual(resp.status_code, 404)
        row = conn.execute("SELECT titulo FROM practica WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["titulo"], "Ajena")

    def test_editing_an_archived_practica_is_404(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        practicas.archivar(conn, 1, pid)
        conn.commit()
        resp = client.post(f"/history/practica/{pid}", data={"titulo": "Robada", "meta_dias_semana": "4"})
        self.assertEqual(resp.status_code, 404)

    def test_the_edit_form_is_present_only_for_an_active_practica(self):
        conn = _reset_db(self)
        active_id = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        archived_id = practicas.crear(conn, 1, "Nadar", "actividad", {"tipos": ["swimming"]})
        practicas.archivar(conn, 1, archived_id)
        conn.commit()
        r_active = client.get(f"/history/practica/{active_id}")
        self.assertIn(f'action="/history/practica/{active_id}"', r_active.text)
        r_archived = client.get(f"/history/practica/{archived_id}")
        self.assertNotIn(f'action="/history/practica/{archived_id}"', r_archived.text)


class BaselineSentenceTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-3 ruling 4: "En general" becomes a sentence with its own subject and
    units, and the shift-kind counts carry proper plural agreement -- never a unit-less dump of
    numbers."""

    def test_baseline_sentence_lists_shift_counts_with_plural_agreement(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Steps", "metrica_umbral", {"metric_key": "steps", "op": ">=", "valor": 8000})
        for day in ("2026-11-01", "2026-11-02"):
            db.set_metric(conn, day, "steps", 9000.0, "garmin", 1)
            conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, ?, 'off', 'manual')", (day,))
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("En toda tu historia:", r.text)
        self.assertIn("2 días libres", r.text)

    def test_baseline_sentence_uses_the_singular_noun_for_one_shift(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Steps", "metrica_umbral", {"metric_key": "steps", "op": ">=", "valor": 8000})
        db.set_metric(conn, "2026-11-01", "steps", 9000.0, "garmin", 1)
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-01', 'off', 'manual')")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("1 día libre", r.text)
        self.assertNotIn("1 días libres", r.text)

    def test_baseline_sentence_omits_the_shift_clause_when_there_are_none(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Steps", "metrica_umbral", {"metric_key": "steps", "op": ">=", "valor": 8000})
        db.set_metric(conn, "2026-11-01", "steps", 9000.0, "garmin", 1)
        db.set_metric(conn, "2026-11-01", "sleep_minutes", 400.0, "garmin", 1)
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("En toda tu historia:", r.text)
        self.assertIn("sueño medio", r.text)

    def test_english_baseline_sentence(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        pid = practicas.crear(conn, 1, "Steps", "metrica_umbral", {"metric_key": "steps", "op": ">=", "valor": 8000})
        for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
            db.set_metric(conn, day, "steps", 9000.0, "garmin", 1)
            conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, ?, 'off', 'manual')", (day,))
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("Across your whole history:", r.text)
        self.assertIn("3 days off", r.text)

    def test_baseline_sentence_uses_turno_de_dia_for_day_shift_counts(self):
        """BODY-HISTORIA-VIDA-4 ruling 1: the day-shift noun reads as a shift, not a calendar day
        ('turno de día' / 'turnos de día'), unlike the other es nouns, unchanged."""
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Steps", "metrica_umbral", {"metric_key": "steps", "op": ">=", "valor": 8000})
        for day in ("2026-11-01", "2026-11-02"):
            db.set_metric(conn, day, "steps", 9000.0, "garmin", 1)
            conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, ?, 'day', 'manual')", (day,))
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("2 turnos de día", r.text)

    def test_english_baseline_sentence_uses_shift_nouns_for_day_and_night(self):
        """BODY-HISTORIA-VIDA-4 ruling 1: every working kind gets 'shift' in English, including
        the night-shift count the surface's gap (a) named explicitly."""
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        pid = practicas.crear(conn, 1, "Steps", "metrica_umbral", {"metric_key": "steps", "op": ">=", "valor": 8000})
        db.set_metric(conn, "2026-11-01", "steps", 9000.0, "garmin", 1)
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-01', 'day', 'manual')")
        db.set_metric(conn, "2026-11-02", "steps", 9000.0, "garmin", 1)
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-02', 'night', 'manual')")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("1 day shift", r.text)
        self.assertIn("1 night shift", r.text)


class DayGridHeaderTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-4 ruling 2: a weekday-initial header row, Monday first, above the day
    grid, and the leading pad cell drawn as no cell at all (no visible border class)."""

    def test_header_has_seven_letters_monday_first_es(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        _activity(conn, 1, "r1", "2026-11-10", type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        header = re.findall(r'<div class="strip-header" aria-hidden="true">(.*?)</div>', r.text, re.S)
        self.assertEqual(len(header), 1)
        self.assertEqual(re.findall(r'<span>([^<]*)</span>', header[0]), ["L", "M", "M", "J", "V", "S", "D"])

    def test_header_has_seven_letters_monday_first_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        _activity(conn, 1, "r1", "2026-11-10", type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        header = re.findall(r'<div class="strip-header" aria-hidden="true">(.*?)</div>', r.text, re.S)
        self.assertEqual(len(header), 1)
        self.assertEqual(re.findall(r'<span>([^<]*)</span>', header[0]), ["M", "T", "W", "T", "F", "S", "S"])

    def test_pad_cell_has_no_visible_border_class(self):
        # "2026-11-10" is a Tuesday (weekday()==1), so the day grid starts with one pad cell.
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        _activity(conn, 1, "r1", "2026-11-10", type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn('<span class="strip-cell pad" aria-hidden="true"></span>', r.text)


class WeeklyStripLegendTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-4 ruling 3: a legend under the weekly strip, four swatches without a
    season and a fifth, outlined one when the practice has at least one."""

    def test_legend_has_four_swatches_with_no_season(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        base = date(2026, 11, 2)
        monday0 = base - timedelta(days=base.weekday())
        for i in range(4):
            _activity(conn, 1, f"met{i}", (monday0 + timedelta(days=i)).isoformat(), type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("Objetivo cumplido", r.text)
        self.assertIn("Parte del objetivo", r.text)
        self.assertIn("Ninguno", r.text)
        self.assertIn("Pocos datos", r.text)
        self.assertNotIn("Temporada</span>", r.text)
        self.assertNotIn('class="dot w-season"', r.text)

    def test_legend_has_a_fifth_season_swatch_when_a_season_exists(self):
        conn = _reset_db(self)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        monday = date(2026, 8, 3)
        for week in range(4):
            wk_monday = monday + timedelta(weeks=week)
            for i in range(4):
                _activity(conn, 1, f"r{week}-{i}", (wk_monday + timedelta(days=i)).isoformat(), type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("Temporada</span>", r.text)
        self.assertIn('class="dot w-season"', r.text)

    def test_english_legend_labels(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        pid = practicas.crear(conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        base = date(2026, 11, 2)
        monday0 = base - timedelta(days=base.weekday())
        for i in range(4):
            _activity(conn, 1, f"met{i}", (monday0 + timedelta(days=i)).isoformat(), type="running")
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("Target met", r.text)
        self.assertIn("Part of the target", r.text)
        self.assertIn("None", r.text)
        self.assertIn("Little data", r.text)


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

    def test_english_weekly_target_label(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        pid = practicas.crear(conn, 1, "Steps", "metrica_umbral",
                               {"metric_key": "steps", "op": ">=", "valor": 8000}, meta_dias_semana=1)
        db.set_metric(conn, "2026-11-01", "steps", 9000.0, "garmin", 1)
        conn.commit()
        r = client.get(f"/history/practica/{pid}")
        self.assertIn("Weekly target: 1 day", r.text)
        self.assertIn("Last 4 weeks", r.text)
        self.assertIn("Your history, week by week", r.text)


if __name__ == "__main__":
    unittest.main()
