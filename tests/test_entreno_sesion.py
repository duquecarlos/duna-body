"""BODY-ENTRENO-VISTA-SESION-1: `GET /day/{day}/entreno/{source_id}` -- Día's own per-activity
drill-down. Rulings:

1. Person-scoped like every Día route: another person's source_id, or a day/source_id this person
   has no activity under, 404s.
2. Header (name, type, day, start-end, duration, distance), summary figures (avg HR, max HR,
   calories) each beside the person's own usual for that activity type when computable (median of
   the same type's last 10 OTHER sessions, fewer than 3 -> no comparison, stated plainly).
3. Strength sessions: sets in order (exercise, reps, weight -- the person's correction when
   present, else Garmin's; corrected marked; excluded struck+labelled), each with its own start/
   duration and the REST before the next set (negative/missing -> never guessed).
4. A heart-rate curve only where the day's per-minute series is already stored; otherwise one
   honest line, never an empty chart.
6. The demo's own strength (today, a curve) and basketball (yesterday, no curve) sessions show
   both of ruling 4's branches.

Invented fixtures only, never a real figure.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, entreno_sesion, garmin_adapter, identity, web
from body.i18n import T

ROOT = Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / "body" / "i18n"

client = TestClient(web.app, follow_redirects=False)


def _as(tc: unittest.TestCase, person_id: int) -> None:
    tc.addCleanup(web.app.dependency_overrides.clear)
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _reset_db(tc: unittest.TestCase, name: str = "sesion.db"):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    db.local_person_id(conn)
    return conn


def _activity(conn, person_id: int, source_id: str, day: str, start_local: str | None, type_: str | None,
              name: str | None, duration_s: float | None, avg_hr: float | None = None,
              max_hr: float | None = None, calories: float | None = None, distance_m: float | None = None,
              source: str = "garmin") -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, start_local, type_, name, duration_s, distance_m, avg_hr, max_hr,
         calories, source),
    )
    conn.commit()


def _es(key: str) -> str:
    return json.loads((I18N_DIR / "es.json").read_text(encoding="utf-8"))[key]


def _en(key: str) -> str:
    return json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))[key]


class NotFoundTest(unittest.TestCase):
    """Ruling 1."""

    def setUp(self):
        self.conn = _reset_db(self, "404.db")
        _as(self, 1)
        _activity(self.conn, 1, "a1", "2026-11-10", "2026-11-10T18:00:00", "running", "Carrera", 1800.0,
                  avg_hr=140, max_hr=160, calories=300)

    def test_unknown_source_id_on_a_day_with_an_activity_404s(self):
        r = client.get("/day/2026-11-10/entreno/nope")
        self.assertEqual(r.status_code, 404)

    def test_right_source_id_on_the_wrong_day_404s(self):
        r = client.get("/day/2026-11-09/entreno/a1")
        self.assertEqual(r.status_code, 404)

    def test_malformed_day_404s(self):
        r = client.get("/day/not-a-date/entreno/a1")
        self.assertEqual(r.status_code, 404)

    def test_another_persons_activity_404s(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Other')")
        _activity(self.conn, 2, "a2", "2026-11-10", "2026-11-10T18:00:00", "running", "Carrera", 1800.0)
        r = client.get("/day/2026-11-10/entreno/a2")  # still signed in as person 1
        self.assertEqual(r.status_code, 404)

    def test_right_source_id_on_the_right_day_200s(self):
        r = client.get("/day/2026-11-10/entreno/a1")
        self.assertEqual(r.status_code, 200)


class HeaderAndFiguresTest(unittest.TestCase):
    """Ruling 2."""

    def setUp(self):
        self.conn = _reset_db(self, "figuras.db")
        _as(self, 1)
        # Five OTHER running sessions (avg_hr 140..148, median 144), so the comparison clears
        # HISTORIAL_MIN (3) with a known median.
        for i, hr in enumerate((140, 142, 144, 146, 148)):
            day = (date(2026, 11, 1) + timedelta(i)).isoformat()
            _activity(self.conn, 1, f"hist-{i}", day, f"{day}T07:00:00", "running", None, 1500.0,
                      avg_hr=hr, max_hr=hr + 20, calories=250 + i)
        _activity(self.conn, 1, "today", "2026-11-10", "2026-11-10T18:05:00", "running", None, 1830.0,
                  avg_hr=150, max_hr=170, calories=310, distance_m=5200)

    def test_header_shows_name_type_window_duration_distance(self):
        r = client.get("/day/2026-11-10/entreno/today")
        self.assertEqual(r.status_code, 200)
        t_es = T("es")
        self.assertIn(t_es("activity.type.running"), r.text)
        self.assertIn("18:05", r.text)
        self.assertIn(t_es.minutes(1830 / 60), r.text)
        self.assertIn(f"{t_es.num(5.2, 1)} km", r.text)

    def test_figures_compare_against_the_median_of_the_other_sessions(self):
        r = client.get("/day/2026-11-10/entreno/today")
        t_es = T("es")
        bpm = t_es("unit.bpm")
        esperado = _es("entreno_sesion.comparacion").format(
            valor=f"{t_es.num(150)} {bpm}", tipo=t_es("activity.type.running"),
            habitual=f"{t_es.num(144)} {bpm}")
        self.assertIn(esperado, r.text)

    def test_english_uses_the_english_comparison_sentence(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        r = client.get("/day/2026-11-10/entreno/today")
        t_en = T("en")
        bpm = t_en("unit.bpm")
        esperado = _en("entreno_sesion.comparacion").format(
            valor=f"{t_en.num(150)} {bpm}", tipo=t_en("activity.type.running"),
            habitual=f"{t_en.num(144)} {bpm}")
        self.assertIn(esperado, r.text)

    def test_calories_with_no_comparable_figure_in_history_shows_the_bare_value(self):
        # The history above never carries distance -- avg_hr/max_hr/calories all do, so instead
        # check a figure absent on THIS session never renders a row at all.
        self.conn.execute("UPDATE activity SET calories = NULL WHERE person_id = 1 AND source_id = 'today'")
        self.conn.commit()
        r = client.get("/day/2026-11-10/entreno/today")
        t_es = T("es")
        self.assertNotIn(t_es("entreno_sesion.calorias"), r.text)


class SinHistorialTest(unittest.TestCase):
    """Ruling 2: fewer than 3 other sessions of the same type -> no comparison, stated plainly."""

    def setUp(self):
        self.conn = _reset_db(self, "sinhistorial.db")
        _as(self, 1)
        # Only two prior cycling sessions -- below HISTORIAL_MIN (3).
        for i, day in enumerate(("2026-11-01", "2026-11-03")):
            _activity(self.conn, 1, f"c{i}", day, f"{day}T07:00:00", "cycling", None, 2400.0, avg_hr=120)
        _activity(self.conn, 1, "today", "2026-11-10", "2026-11-10T18:00:00", "cycling", None, 2400.0,
                  avg_hr=125, max_hr=150, calories=400)

    def test_sin_historial_line_shown_instead_of_a_comparison(self):
        r = client.get("/day/2026-11-10/entreno/today")
        t_es = T("es")
        self.assertIn(_es("entreno_sesion.sin_historial").format(tipo=t_es("activity.type.cycling")), r.text)
        self.assertNotIn("tu habitual en", r.text)

    def test_a_manual_session_with_no_type_shows_no_comparison_and_no_sin_historial(self):
        source_id = db.create_manual_session(self.conn, 1, "2026-11-11", "2026-11-11T08:00:00", "A mano",
                                              duration_s=600.0)
        r = client.get("/day/2026-11-11/entreno/" + source_id)
        self.assertEqual(r.status_code, 200)
        t_es = T("es")
        self.assertNotIn(_es("entreno_sesion.sin_historial").split("{tipo}")[0], r.text)


def _watch_set(exercise: str | None, reps: float | None, weight_value: float | None, start_local: str,
               duration_s: float, set_type: str = "active") -> garmin_adapter.ExerciseSet:
    return garmin_adapter.ExerciseSet(set_type=set_type, category="pecho" if exercise else None,
                                       exercise=exercise, reps=reps, weight_value=weight_value,
                                       weight_unit="kg" if weight_value is not None else None,
                                       duration_s=duration_s, start_local=start_local)


class StrengthSetsTest(unittest.TestCase):
    """Ruling 3."""

    DAY = "2026-11-12"

    def setUp(self):
        self.conn = _reset_db(self, "sets.db")
        _as(self, 1)
        _activity(self.conn, 1, "s1", self.DAY, f"{self.DAY}T18:00:00", "strength_training", None, 600.0)
        # Active/rest/active/rest/active, Garmin's own alternating shape -- a 'rest' slot's own
        # duration (30s) is deliberately WRONG against the true gap (60s) between the active
        # slots' own start times, so the test can tell "computed from real start times" apart from
        # "copied from the watch's own rest slot".
        sets = [
            _watch_set("press banca", 8, 40.0, f"{self.DAY}T18:00:00", 40.0),
            _watch_set(None, None, None, f"{self.DAY}T18:00:40", 30.0, set_type="rest"),
            _watch_set("press banca", 8, 40.0, f"{self.DAY}T18:02:00", 40.0),
            _watch_set(None, None, None, f"{self.DAY}T18:02:40", 30.0, set_type="rest"),
            _watch_set("sentadilla", 10, 60.0, f"{self.DAY}T18:04:00", 45.0),
        ]
        db.save_exercise_sets(self.conn, 1, "garmin", "s1", sets)
        # Set 1 corrected (reps 8 -> 6); set 3 excluded.
        db.correct_exercise_set(self.conn, 1, "garmin", "s1", 1, exercise="press banca", category="pecho",
                                reps=6, weight_kg=40.0)
        db.set_series_excluida(self.conn, 1, "garmin", "s1", 3, True)

    def test_sets_listed_in_order_exercise_reps_weight(self):
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("press banca", r.text)
        self.assertIn("sentadilla", r.text)

    def test_corrected_set_shows_the_persons_value_and_is_marked(self):
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        t_es = T("es")
        self.assertIn(">6", r.text)  # the person's corrected reps, not the watch's 8
        self.assertIn(t_es("entreno_sesion.set_corregida"), r.text)

    def test_excluded_set_is_struck_and_labelled_but_still_listed(self):
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        t_es = T("es")
        self.assertIn(t_es("entreno_sesion.set_excluida"), r.text)
        self.assertIn("set-excluida", r.text)

    def test_rest_is_the_real_gap_between_active_sets_not_the_watch_rest_slots_own_duration(self):
        sets = entreno_sesion.series(self.conn, 1, "garmin", "s1")
        self.assertEqual([s["set_order"] for s in sets], [1, 3, 5])  # only the active slots
        self.assertEqual(sets[0]["rest_after_s"], 80.0)  # 18:02:00 - 18:00:40
        self.assertEqual(sets[1]["rest_after_s"], 80.0)  # 18:04:00 - 18:03:10
        self.assertIsNone(sets[2]["rest_after_s"])  # the last set: no rest after

    def test_a_negative_gap_shows_as_missing_never_guessed(self):
        # The watch re-sent set 5 starting BEFORE set 3 ends -- a bad sync, never shown as a
        # negative rest.
        db.save_exercise_sets(self.conn, 1, "garmin", "s1", [
            _watch_set("press banca", 8, 40.0, f"{self.DAY}T18:00:00", 40.0),
            _watch_set("press banca", 8, 40.0, f"{self.DAY}T18:02:00", 40.0),
            _watch_set("sentadilla", 10, 60.0, f"{self.DAY}T18:02:30", 45.0),
        ])
        sets = entreno_sesion.series(self.conn, 1, "garmin", "s1")
        self.assertIsNone(sets[1]["rest_after_s"])

    def test_a_non_strength_activity_shows_no_sets_section(self):
        _activity(self.conn, 1, "r1", self.DAY, f"{self.DAY}T06:00:00", "running", None, 1200.0)
        r = client.get(f"/day/{self.DAY}/entreno/r1")
        t_es = T("es")
        self.assertNotIn(t_es("entreno_sesion.sets_titulo"), r.text)


class CurvaTest(unittest.TestCase):
    """Ruling 4."""

    DAY = "2026-11-13"

    def setUp(self):
        self.conn = _reset_db(self, "curva.db")
        _as(self, 1)

    def test_no_stored_series_shows_the_honest_pending_line_never_an_empty_chart(self):
        _activity(self.conn, 1, "sin-serie", self.DAY, f"{self.DAY}T18:00:00", "strength_training", None, 600.0)
        r = client.get(f"/day/{self.DAY}/entreno/sin-serie")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_es("entreno_sesion.curva_pendiente"), r.text)
        self.assertNotIn('class="chart-svg"', r.text)

    def test_a_stored_series_covering_the_window_draws_the_curve_with_peak_and_drop(self):
        # BODY-ENTRENO-VISTA-SESION-1: garmin_adapter._utc_to_local_iso truncates every sample's
        # own local timestamp to the minute on READ, whatever precision it was written at -- so a
        # set/rest window edge placed exactly on a minute boundary makes a minute-aligned sample
        # ambiguous between the two windows it touches. Set 1 at :30s past the minute keeps every
        # window edge off a whole minute, so each sample below falls inside exactly one window.
        _activity(self.conn, 1, "con-serie", self.DAY, f"{self.DAY}T18:00:00", "strength_training", None, 330.0)
        sets = [
            _watch_set("press banca", 8, 40.0, f"{self.DAY}T18:00:30", 60.0),  # ends 18:01:30
            _watch_set("press banca", 8, 40.0, f"{self.DAY}T18:04:30", 60.0),  # ends 18:05:30
        ]
        db.save_exercise_sets(self.conn, 1, "garmin", "con-serie", sets)
        # Per-minute series: 18:01 inside set 1's own window (peak 130); 18:02 and 18:03 strictly
        # inside the rest (120, then 100 -- a real two-sample drop of 20); 18:05 inside set 2's
        # own window (peak 140).
        muestras = [
            (f"{self.DAY}T18:01", 130), (f"{self.DAY}T18:02", 120), (f"{self.DAY}T18:03", 100),
            (f"{self.DAY}T18:05", 140),
        ]
        from datetime import datetime, timezone as tz
        payload = {"heartRateValues": [
            [int((datetime.fromisoformat(t) + timedelta(hours=5)).replace(tzinfo=tz.utc).timestamp() * 1000), v]
            for t, v in muestras
        ]}
        db.guardar_intradia(self.conn, 1, self.DAY, "intradia_fc", payload)
        r = client.get(f"/day/{self.DAY}/entreno/con-serie")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="chart-svg"', r.text)
        self.assertNotIn(_es("entreno_sesion.curva_pendiente"), r.text)
        t_es = T("es")
        bpm = t_es("unit.bpm")
        self.assertIn(t_es("entreno_sesion.pico", valor=f"{t_es.num(130)} {bpm}"), r.text)
        self.assertIn(t_es("entreno_sesion.caida", valor=f"{t_es.num(20)} {bpm}"), r.text)

    def test_series_stored_but_outside_the_sessions_own_window_still_shows_the_pending_line(self):
        # BODY-ENTRENO-VISTA-SESION-1 ruling 4: the day has SOME per-minute data, just none of it
        # inside THIS session's own window -- still the honest line, never a flat/empty chart.
        _activity(self.conn, 1, "tarde", self.DAY, f"{self.DAY}T18:00:00", "strength_training", None, 300.0)
        from datetime import datetime, timezone as tz
        t = f"{self.DAY}T06:00"
        ms = int((datetime.fromisoformat(t) + timedelta(hours=5)).replace(tzinfo=tz.utc).timestamp() * 1000)
        db.guardar_intradia(self.conn, 1, self.DAY, "intradia_fc", {"heartRateValues": [[ms, 60]]})
        r = client.get(f"/day/{self.DAY}/entreno/tarde")
        self.assertIn(_es("entreno_sesion.curva_pendiente"), r.text)
        self.assertNotIn('class="chart-svg"', r.text)


class PinnedCopyTest(unittest.TestCase):
    """Ruling 5: the exact given copy for the comparison and not-enough-history sentences."""

    def test_spanish_copy(self):
        self.assertEqual(_es("entreno_sesion.comparacion"), "{valor} — tu habitual en {tipo}: {habitual}")
        self.assertEqual(_es("entreno_sesion.sin_historial"),
                         "Aún no hay suficientes sesiones de {tipo} para comparar.")

    def test_english_copy(self):
        self.assertEqual(_en("entreno_sesion.comparacion"), "{valor} — your usual for {tipo}: {habitual}")
        self.assertEqual(_en("entreno_sesion.sin_historial"), "Not enough {tipo} sessions yet to compare.")


class DemoBranchesTest(unittest.TestCase):
    """Ruling 6: the demo's own strength (today, a curve) and basketball (yesterday, no curve)
    sessions, both reachable the same way Día's own row links to them (ruling 1)."""

    ANCHOR = date(2026, 11, 15)

    class _FakeToday(date):
        @classmethod
        def today(cls):
            return DemoBranchesTest.ANCHOR

    def setUp(self):
        self.conn = _reset_db(self, "demo_ramas.db")
        with mock.patch.object(demo, "date", self._FakeToday):
            demo._seed(self.conn, dias=60, idioma="es")
        _as(self, 1)

    def test_days_activity_row_links_to_the_session_page(self):
        r = client.get(f"/day/{self.ANCHOR.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/day/{self.ANCHOR.isoformat()}/entreno/demo-{self.ANCHOR.isoformat()}-1", r.text)

    def test_todays_strength_session_shows_a_curve(self):
        r = client.get(f"/day/{self.ANCHOR.isoformat()}/entreno/demo-{self.ANCHOR.isoformat()}-1")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="chart-svg"', r.text)
        self.assertNotIn(_es("entreno_sesion.curva_pendiente"), r.text)
        t_es = T("es")
        self.assertIn(t_es("entreno_sesion.sets_titulo"), r.text)
        self.assertIn(t_es("entreno_sesion.set_corregida"), r.text)
        self.assertIn(t_es("entreno_sesion.set_excluida"), r.text)

    def test_yesterdays_basketball_session_shows_the_honest_line(self):
        yesterday = (self.ANCHOR - timedelta(1)).isoformat()
        r = client.get(f"/day/{yesterday}/entreno/demo-{yesterday}-1")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_es("entreno_sesion.curva_pendiente"), r.text)
        self.assertNotIn('class="chart-svg"', r.text)


if __name__ == "__main__":
    unittest.main()
