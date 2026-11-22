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
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, entreno_sesion, esfuerzo_sesion, garmin_adapter, identity, web
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


def _load_lang(lang: str) -> dict:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def _es(key: str) -> str:
    return _load_lang("es")[key]


def _en(key: str) -> str:
    return _load_lang("en")[key]


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
        titulo = _es("entreno_sesion.comparacion_titulo").format(tipo=t_es("activity.type.running").lower())
        self.assertIn(titulo, r.text)
        habitual = _es("entreno_sesion.habitual").format(habitual=f"{t_es.num(144)} {bpm}")
        self.assertIn(habitual, r.text)

    def test_the_comparison_caption_is_said_once_not_once_per_tile(self):
        r = client.get("/day/2026-11-10/entreno/today")
        t_es = T("es")
        titulo = _es("entreno_sesion.comparacion_titulo").format(tipo=t_es("activity.type.running").lower())
        self.assertEqual(r.text.count(titulo), 1)

    def test_english_uses_the_english_comparison_sentence(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        r = client.get("/day/2026-11-10/entreno/today")
        t_en = T("en")
        bpm = t_en("unit.bpm")
        titulo = _en("entreno_sesion.comparacion_titulo").format(tipo=t_en("activity.type.running"))
        self.assertIn(titulo, r.text)
        habitual = _en("entreno_sesion.habitual").format(habitual=f"{t_en.num(144)} {bpm}")
        self.assertIn(habitual, r.text)

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
        self.assertNotIn("habitual", r.text)

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
        # BODY-ENTRENO-VISTA-SESION-1-C ruling 6: capital first letter, display only.
        self.assertIn("Press banca", r.text)
        self.assertIn("Sentadilla", r.text)
        self.assertNotIn("press banca", r.text)

    def test_a_set_reads_reps_times_weight_with_no_trailing_zero_on_a_whole_value(self):
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        t_es = T("es")
        # Set 3 (uncorrected, unexcluded): 8 reps, 40 kg -- a whole value, never "40,0 kg".
        self.assertIn(f"{t_es.num(8)} × {t_es.num(40)} kg", r.text)
        self.assertNotIn("40,0", r.text)
        # Set 5: 10 reps, 60 kg.
        self.assertIn(f"{t_es.num(10)} × {t_es.num(60)} kg", r.text)
        self.assertNotIn("60,0", r.text)

    def test_a_fractional_weight_keeps_its_one_decimal(self):
        _activity(self.conn, 1, "s3", self.DAY, f"{self.DAY}T20:00:00", "strength_training", None, 60.0)
        db.save_exercise_sets(self.conn, 1, "garmin", "s3", [
            _watch_set("curl", 10, 12.5, f"{self.DAY}T20:00:00", 40.0),
        ])
        r = client.get(f"/day/{self.DAY}/entreno/s3")
        t_es = T("es")
        self.assertIn(f"{t_es.num(10)} × {t_es.num(12.5, 1)} kg", r.text)

    def test_a_bodyweight_set_with_no_weight_reads_reps_only(self):
        # A fresh activity/source_id -- s1's own slot 1 was corrected in setUp, and a correction's
        # fields survive a later save_exercise_sets() re-read (BODY-ENTRENO-1-INV Q2), so reusing
        # s1 here would still show the PERSON's earlier correction, not this watch read.
        _activity(self.conn, 1, "s2", self.DAY, f"{self.DAY}T19:00:00", "strength_training", None, 60.0)
        db.save_exercise_sets(self.conn, 1, "garmin", "s2", [
            _watch_set("plancha", 12, None, f"{self.DAY}T19:00:00", 40.0),
        ])
        r = client.get(f"/day/{self.DAY}/entreno/s2")
        t_es = T("es")
        self.assertIn(_es("entreno_sesion.reps_solo").format(n=t_es.num(12)), r.text)
        self.assertNotIn("×", r.text)

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


class LegendCssTest(unittest.TestCase):
    """BODY-ENTRENO-VISTA-SESION-1-C ruling 1, 2: the legend chips get a background of their own
    (the base `.legend i` rule at body.css:196 sets none), and the set/rest bands are raised
    enough to read against the card in both themes."""

    CSS = (ROOT / "body" / "static" / "body.css").read_text(encoding="utf-8")

    def test_legend_chips_have_a_compound_rule_setting_a_background(self):
        self.assertRegex(self.CSS, r"\.legend\s+i\.hr-set\s*\{[^}]*background\s*:")
        self.assertRegex(self.CSS, r"\.legend\s+i\.hr-rest\s*\{[^}]*background\s*:")

    def test_band_opacities_were_raised_and_the_set_band_stays_stronger(self):
        set_op = float(re.search(r"(?<!i)\.hr-set\{[^}]*opacity\s*:\s*([\d.]+)", self.CSS).group(1))
        rest_op = float(re.search(r"(?<!i)\.hr-rest\{[^}]*opacity\s*:\s*([\d.]+)", self.CSS).group(1))
        self.assertGreater(set_op, 0.14)   # above BODY-ENTRENO-VISTA-SESION-1's original .14
        self.assertGreater(rest_op, 0.12)  # above BODY-ENTRENO-VISTA-SESION-1's original .12
        self.assertGreater(set_op, rest_op)  # the set band stays the stronger of the two


class GenericTypeTest(unittest.TestCase):
    """BODY-ENTRENO-VISTA-SESION-1-C ruling 4: Garmin's generic "other" bucket uses the session's
    own name instead of the "Otros"/"Other" label."""

    def setUp(self):
        self.conn = _reset_db(self, "generico.db")
        _as(self, 1)

    def test_subtitle_shows_the_date_only_never_the_generic_label(self):
        _activity(self.conn, 1, "basket", "2026-11-14", "2026-11-14T18:00:00", "other", "Baloncesto", 3000.0,
                  avg_hr=128, max_hr=162, calories=410)
        r = client.get("/day/2026-11-14/entreno/basket")
        t_es = T("es")
        self.assertNotIn(t_es("activity.type.other"), r.text)

    def test_a_named_known_type_keeps_todays_subtitle_behaviour(self):
        _activity(self.conn, 1, "run1", "2026-11-14", "2026-11-14T18:00:00", "running", None, 1800.0)
        r = client.get("/day/2026-11-14/entreno/run1")
        t_es = T("es")
        self.assertIn(t_es("activity.type.running"), r.text)

    def test_comparison_group_is_the_same_generic_type_with_the_same_name_case_and_accent_insensitive(self):
        # Three prior "Baloncesto"/"BALONCESTO " sessions (case/accent-insensitive match) and one
        # prior "Yoga"-named "other" session that must NOT be pulled into the median.
        for i, (day, nombre, hr) in enumerate((
                ("2026-11-01", "baloncesto", 120), ("2026-11-03", "BALONCESTO", 124),
                ("2026-11-05", "Balóncesto", 128), ("2026-11-07", "Yoga", 90))):
            _activity(self.conn, 1, f"h{i}", day, f"{day}T18:00:00", "other", nombre, 3000.0, avg_hr=hr)
        _activity(self.conn, 1, "basket", "2026-11-14", "2026-11-14T18:00:00", "other", "Baloncesto", 3000.0,
                  avg_hr=130, max_hr=162, calories=410)
        r = client.get("/day/2026-11-14/entreno/basket")
        t_es = T("es")
        bpm = t_es("unit.bpm")
        titulo = _es("entreno_sesion.comparacion_titulo").format(tipo="baloncesto")
        self.assertIn(titulo, r.text)
        habitual = _es("entreno_sesion.habitual").format(habitual=f"{t_es.num(124)} {bpm}")  # median 120/124/128
        self.assertIn(habitual, r.text)

    def test_not_enough_same_named_sessions_shows_the_honest_line_with_the_name_not_otros(self):
        _activity(self.conn, 1, "basket", "2026-11-14", "2026-11-14T18:00:00", "other", "Baloncesto", 3000.0,
                  avg_hr=130, max_hr=162, calories=410)
        r = client.get("/day/2026-11-14/entreno/basket")
        t_es = T("es")
        self.assertIn(_es("entreno_sesion.sin_historial").format(tipo="baloncesto"), r.text)
        self.assertNotIn(t_es("activity.type.other"), r.text)


class PinnedCopyTest(unittest.TestCase):
    """BODY-ENTRENO-VISTA-SESION-1-C ruling 3, 5: the exact given copy for the comparison caption,
    the per-tile "habitual" line, the not-enough-history sentence and the curve's honest line."""

    def test_spanish_copy(self):
        self.assertEqual(_es("entreno_sesion.comparacion_titulo"), "Frente a tus sesiones habituales de {tipo}")
        self.assertEqual(_es("entreno_sesion.habitual"), "habitual: {habitual}")
        self.assertEqual(_es("entreno_sesion.sin_historial"),
                         "Aún no hay suficientes sesiones de {tipo} para comparar.")
        self.assertEqual(_es("entreno_sesion.curva_pendiente"),
                         "Body aún no guarda la curva de pulso de esta sesión.")
        self.assertNotIn("permiso", _es("entreno_sesion.curva_pendiente"))

    def test_english_copy(self):
        self.assertEqual(_en("entreno_sesion.comparacion_titulo"), "Compared with your usual {tipo} sessions")
        self.assertEqual(_en("entreno_sesion.habitual"), "usual: {habitual}")
        self.assertEqual(_en("entreno_sesion.sin_historial"), "Not enough {tipo} sessions yet to compare.")
        self.assertEqual(_en("entreno_sesion.curva_pendiente"),
                         "Body doesn't have this session's heart-rate curve yet.")
        self.assertNotIn("permission", _en("entreno_sesion.curva_pendiente"))

    def test_the_retired_key_is_gone_from_both_languages(self):
        self.assertNotIn("entreno_sesion.comparacion", _load_lang("es"))
        self.assertNotIn("entreno_sesion.comparacion", _load_lang("en"))

    def test_the_second_peak_keys_are_gone_from_both_languages(self):
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the card's own "peak" contradicted the page's
        # own max-HR comparison -- these three keys are retired, in both languages.
        es, en = _load_lang("es"), _load_lang("en")
        for key in ("entreno_sesion.esfuerzo.pico_mas", "entreno_sesion.esfuerzo.pico_como",
                    "entreno_sesion.esfuerzo.pico_menos"):
            self.assertNotIn(key, es)
            self.assertNotIn(key, en)


def _detalle_payload(start_local: str, valores: list[float]) -> dict:
    t0 = datetime.fromisoformat(start_local)
    # America/Bogota is UTC-5 year round -- demo.py's own _muestra_ms convention.
    ms0 = int((t0 + timedelta(hours=5)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return {
        "metricDescriptors": [{"key": "directTimestamp", "index": 0}, {"key": "directHeartRate", "index": 1}],
        "activityDetailMetrics": [{"metrics": [ms0 + i * 60_000, v]} for i, v in enumerate(valores)],
    }


def _zonas_payload(minutos: dict[int, float]) -> list[dict]:
    return [{"zoneNumber": n, "secsInZone": m * 60.0, "zoneLowBoundary": 100.0 + 10 * n}
            for n, m in minutos.items()]


# Minutes 0-13 (14 points, full coverage): three peaks exactly MODELO['separacion_min'] apart (so
# none collapse), each with a one-minute drop -- pico 180, median drop 70 (median(55, 80, 70)).
_TRES_PICOS = [100, 100, 100, 100, 100, 155, 100, 100, 180, 100, 100, 170, 100, 100]


class EsfuerzoCardAbsentTest(unittest.TestCase):
    """Ruling 6: the card's own presence gate -- no stored 'detalle' or 'zonas' at all, no card,
    the page unchanged; a strength session keeps its own set/rest view even when it DOES carry
    stored detail."""

    def setUp(self):
        self.conn = _reset_db(self, "esfuerzo_ausente.db")
        _as(self, 1)

    def test_non_strength_session_with_no_detail_shows_no_card(self):
        _activity(self.conn, 1, "r1", "2026-11-10", "2026-11-10T18:00:00", "running", None, 1800.0,
                  avg_hr=140, max_hr=160, calories=300)
        r = client.get("/day/2026-11-10/entreno/r1")
        self.assertEqual(r.status_code, 200)
        t_es = T("es")
        self.assertNotIn(t_es("entreno_sesion.esfuerzo.titulo"), r.text)

    def test_strength_session_with_detail_stored_still_shows_no_card(self):
        _activity(self.conn, 1, "s1", "2026-11-10", "2026-11-10T18:00:00", "strength_training", None, 600.0)
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "s1", "zonas",
                                      _zonas_payload({1: 5.0, 2: 5.0, 3: 5.0, 4: 5.0, 5: 5.0}))
        self.conn.commit()
        r = client.get("/day/2026-11-10/entreno/s1")
        self.assertEqual(r.status_code, 200)
        t_es = T("es")
        self.assertNotIn(t_es("entreno_sesion.esfuerzo.titulo"), r.text)


class EsfuerzoCardTest(unittest.TestCase):
    """Ruling 6: the effort card's own rows."""

    DAY = "2026-11-18"

    def setUp(self):
        self.conn = _reset_db(self, "esfuerzo.db")
        _as(self, 1)
        _activity(self.conn, 1, "partido", self.DAY, f"{self.DAY}T18:00:00", "other", "Baloncesto", 14 * 60.0,
                  avg_hr=128, max_hr=180, calories=400)
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "partido", "detalle",
                                      _detalle_payload(f"{self.DAY}T18:00:00", _TRES_PICOS))
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "partido", "zonas",
                                      _zonas_payload({1: 2.0, 2: 2.0, 3: 4.0, 4: 3.0, 5: 3.0}))
        self.conn.commit()

    def test_titulo_shown(self):
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        self.assertEqual(r.status_code, 200)
        t_es = T("es")
        self.assertIn(t_es("entreno_sesion.esfuerzo.titulo"), r.text)

    def test_zone_rows_and_high_share(self):
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        t_es = T("es")
        self.assertIn(t_es("entreno_sesion.esfuerzo.zona", n=3, dur=t_es.minutes(4.0)), r.text)
        self.assertIn(t_es("entreno_sesion.esfuerzo.zona", n=4, dur=t_es.minutes(3.0)), r.text)
        pct = round(6.0 / 14.0 * 100)
        self.assertIn(t_es("entreno_sesion.esfuerzo.zona_alta", pct=t_es.num(pct)), r.text)
        self.assertIn('class="stage"', r.text)

    def test_efforts_and_recovery_lines(self):
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        t_es = T("es")
        bpm = t_es("unit.bpm")
        self.assertIn(t_es("entreno_sesion.esfuerzo.esfuerzos", n=t_es.num(3)), r.text)
        self.assertIn(t_es("entreno_sesion.esfuerzo.recuperacion", valor=f"{t_es.num(70)} {bpm}"), r.text)
        self.assertNotIn(t_es("entreno_sesion.esfuerzo.pocos_datos"), r.text)

    def test_no_second_peak_figure_shown(self):
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the efforts line carries no peak value ("3
        # esfuerzos fuertes", never "... · pico ..."), and none of the three retired "pico"
        # phrases render, es or en.
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        t_es = T("es")
        self.assertNotIn("pico", t_es("entreno_sesion.esfuerzo.esfuerzos", n=t_es.num(3)))
        self.assertNotIn(" · pico ", r.text)
        for texto in ("Pico más alto que lo habitual", "Pico como lo habitual", "Pico más bajo que lo habitual"):
            self.assertNotIn(texto, r.text)
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        for texto in ("Higher peak than usual", "Peak as usual", "Lower peak than usual"):
            self.assertNotIn(texto, r.text)

    def test_zone_legend_has_one_swatch_per_zone_and_each_rect_carries_a_title(self):
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: the sleep-stage-bar precedent -- a <title> per
        # <rect>, then one swatch per non-empty zone, the SAME class tying bar and swatch.
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        self.assertIn('<div class="legend">', r.text)
        self.assertEqual(r.text.count("<title>Zona"), 5)  # all five zones carry minutes in this fixture
        for cls in ("c-inset", "c-other", "c-info", "c-body", "c-amber"):
            self.assertIn(f'<i class="{cls}">', r.text)

    def test_english(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        t_en = T("en")
        self.assertIn(t_en("entreno_sesion.esfuerzo.titulo"), r.text)
        self.assertIn(t_en("entreno_sesion.esfuerzo.esfuerzos", n=t_en.num(3)), r.text)


class EsfuerzoHonestGapCardTest(unittest.TestCase):
    """Ruling 6: 'detalle' stored but too thin to measure recovery between efforts -- the honest
    gap line, never a guess."""

    DAY = "2026-11-19"

    def test_pocos_datos_line_shown(self):
        conn = _reset_db(self, "esfuerzo_pocos.db")
        _as(self, 1)
        _activity(conn, 1, "corto", self.DAY, f"{self.DAY}T18:00:00", "other", "Pachanga", 300.0)
        db.guardar_detalle_actividad(conn, 1, "garmin", "corto", "detalle",
                                      _detalle_payload(f"{self.DAY}T18:00:00", [100.0, 110.0, 105.0, 95.0]))
        conn.commit()
        r = client.get(f"/day/{self.DAY}/entreno/corto")
        self.assertEqual(r.status_code, 200)
        t_es = T("es")
        self.assertIn(t_es("entreno_sesion.esfuerzo.titulo"), r.text)
        self.assertIn(t_es("entreno_sesion.esfuerzo.pocos_datos"), r.text)


class EsfuerzoComparacionTest(unittest.TestCase):
    """Ruling 5/6: the three-way comparison words, one real example of each ("mas"/"igual"/
    "menos") against a crafted history."""

    DAY = "2026-11-20"

    # Three peaks exactly 170, settling to 141 after each (a drop of 29 every time) -- used
    # identically for every one of the four history games, so each figure's median is exact.
    _CURVA_HISTORIAL = [100, 100, 100, 100, 100, 170, 141, 100, 170, 141, 100, 170, 141, 100]
    # Lower peaks (120, 140, 130) than the history's own 170 -- today's own "pico" reads 140.
    _CURVA_HOY = [100, 100, 100, 100, 100, 120, 100, 100, 140, 100, 100, 130, 100, 100]

    def setUp(self):
        self.conn = _reset_db(self, "esfuerzo_comp.db")
        _as(self, 1)
        for i in range(4):
            day = (date(2026, 11, 1) + timedelta(i)).isoformat()
            source_id = f"hist-{i}"
            # avg_hr=150 on every game (history and today alike) so the PAGE's own figure/habitual
            # comparison also has something to say here -- ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2's
            # own fixture for "the page's heading, said once, is never repeated inside the card".
            _activity(self.conn, 1, source_id, day, f"{day}T18:00:00", "other", "Baloncesto", 14 * 60.0,
                      avg_hr=150)
            db.guardar_detalle_actividad(self.conn, 1, "garmin", source_id, "detalle",
                                          _detalle_payload(f"{day}T18:00:00", self._CURVA_HISTORIAL))
            # alta_share = 4/14 ~= 28.6% for every history game.
            db.guardar_detalle_actividad(self.conn, 1, "garmin", source_id, "zonas",
                                          _zonas_payload({1: 2.0, 2: 2.0, 3: 6.0, 4: 2.0, 5: 2.0}))
        _activity(self.conn, 1, "hoy", self.DAY, f"{self.DAY}T18:00:00", "other", "Baloncesto", 14 * 60.0,
                  avg_hr=150)
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "hoy", "detalle",
                                      _detalle_payload(f"{self.DAY}T18:00:00", self._CURVA_HOY))
        # alta_share = 7/14 = 50% today -- well above the history's own ~28.6% (+21.4 pts, over
        # MODELO['banda_zona_alta_pts']).
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "hoy", "zonas",
                                      _zonas_payload({1: 2.0, 2: 2.0, 3: 3.0, 4: 4.0, 5: 3.0}))
        self.conn.commit()

    def test_more_time_in_the_high_zones_and_same_recovery(self):
        r = client.get(f"/day/{self.DAY}/entreno/hoy")
        self.assertEqual(r.status_code, 200)
        t_es = T("es")
        self.assertIn(t_es("entreno_sesion.esfuerzo.zona_alta_mas"), r.text)
        self.assertIn(t_es("entreno_sesion.esfuerzo.recuperacion_como"), r.text)
        self.assertNotIn(t_es("entreno_sesion.esfuerzo.zona_alta_como"), r.text)

    def test_comparison_heading_reuses_the_existing_key(self):
        r = client.get(f"/day/{self.DAY}/entreno/hoy")
        t_es = T("es")
        titulo = _es("entreno_sesion.comparacion_titulo").format(tipo="baloncesto")
        self.assertIn(titulo, r.text)

    def test_comparison_heading_is_not_repeated_inside_the_card(self):
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: the page's own heading says it once, the card's
        # comparison rows follow the figures directly with no second heading of their own.
        r = client.get(f"/day/{self.DAY}/entreno/hoy")
        titulo = _es("entreno_sesion.comparacion_titulo").format(tipo="baloncesto")
        self.assertEqual(r.text.count(titulo), 1)


class CurvaDesdeDetalleTest(unittest.TestCase):
    """Ruling 7: the pulse curve drawn from the activity's own stored detail when the day-level
    series is absent."""

    DAY = "2026-11-21"

    def test_curve_drawn_from_activity_detail_when_day_series_is_absent(self):
        conn = _reset_db(self, "curva_detalle.db")
        _as(self, 1)
        _activity(conn, 1, "partido", self.DAY, f"{self.DAY}T18:00:00", "other", "Baloncesto", 13 * 60.0)
        db.guardar_detalle_actividad(conn, 1, "garmin", "partido", "detalle",
                                      _detalle_payload(f"{self.DAY}T18:00:00", _TRES_PICOS))
        conn.commit()
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="chart-svg"', r.text)
        self.assertNotIn(_es("entreno_sesion.curva_pendiente"), r.text)

    def test_day_level_series_still_wins_when_present(self):
        # Both a day-level series AND activity-level detail are stored -- the day-level one
        # (ruling 4's own, already-existing behaviour) is used, never the fallback.
        conn = _reset_db(self, "curva_prioridad.db")
        _as(self, 1)
        _activity(conn, 1, "partido", self.DAY, f"{self.DAY}T18:00:00", "other", "Baloncesto", 300.0)
        muestras = [(f"{self.DAY}T18:0{i}", 100.0 + i) for i in range(5)]
        payload = {"heartRateValues": [
            [int((datetime.fromisoformat(t) + timedelta(hours=5)).replace(tzinfo=timezone.utc).timestamp() * 1000), v]
            for t, v in muestras
        ]}
        db.guardar_intradia(conn, 1, self.DAY, "intradia_fc", payload)
        db.guardar_detalle_actividad(conn, 1, "garmin", "partido", "detalle",
                                      _detalle_payload(f"{self.DAY}T18:00:00", [999.0] * 5))
        conn.commit()
        r = client.get(f"/day/{self.DAY}/entreno/partido")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="chart-svg"', r.text)
        self.assertNotIn("999", r.text)  # the activity-level fallback curve never rendered


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
            demo._seed(self.conn, dias=60, idioma="es", today=self.ANCHOR)
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

    def test_yesterdays_basketball_session_shows_the_curve_from_its_own_detail_and_the_effort_card(self):
        # BODY-PARTIDO-1 ruling 9: the demo now seeds stored 'detalle'/'zonas' for this session (no
        # day-level series either way) -- ruling 7's own fallback draws the curve from the
        # activity's own detail instead of the honest "pending" line, and ruling 6's new card shows.
        yesterday = (self.ANCHOR - timedelta(1)).isoformat()
        r = client.get(f"/day/{yesterday}/entreno/demo-{yesterday}-1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(_es("entreno_sesion.curva_pendiente"), r.text)
        self.assertIn('class="chart-svg"', r.text)
        t_es = T("es")
        self.assertIn(t_es("entreno_sesion.esfuerzo.titulo"), r.text)
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4/5: this anchor basketball game's own curve now
        # agrees with its seeded avg/max, and the 4 earlier games it seeds give it a full history --
        # a recovery figure and both comparison rows (zona_alta + recuperacion) show.
        self.assertIn(t_es("unit.bpm"), r.text)
        self.assertRegex(r.text, r"bajó unos [\d.,]+")
        comparacion_keys = ("entreno_sesion.esfuerzo.zona_alta_mas", "entreno_sesion.esfuerzo.zona_alta_como",
                            "entreno_sesion.esfuerzo.zona_alta_menos")
        self.assertTrue(any(t_es(k) in r.text for k in comparacion_keys))
        recuperacion_keys = ("entreno_sesion.esfuerzo.recuperacion_mas", "entreno_sesion.esfuerzo.recuperacion_como",
                             "entreno_sesion.esfuerzo.recuperacion_menos")
        self.assertTrue(any(t_es(k) in r.text for k in recuperacion_keys))


class DolorCardTest(unittest.TestCase):
    """BODY-PLAN-FASES-5 rulings 2/3: the session screen's own pain card -- only for an exercise
    whose category falls inside an open zona period, and only then."""

    DAY = "2026-11-17"

    def setUp(self):
        self.conn = _reset_db(self, "dolor.db")
        _as(self, 1)
        _activity(self.conn, 1, "s1", self.DAY, f"{self.DAY}T18:00:00", "strength_training", None, 600.0)
        db.save_exercise_sets(self.conn, 1, "garmin", "s1", [
            garmin_adapter.ExerciseSet(set_type="active", category="hombro", exercise="press militar",
                                        reps=8, weight_value=20.0, weight_unit="kg", duration_s=40.0,
                                        start_local=f"{self.DAY}T18:00:00"),
            garmin_adapter.ExerciseSet(set_type="active", category="pierna", exercise="sentadilla",
                                        reps=10, weight_value=60.0, weight_unit="kg", duration_s=45.0,
                                        start_local=f"{self.DAY}T18:02:00"),
        ])

    def test_no_card_without_an_open_zona_period(self):
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('name="ejercicio_0"', r.text)

    def test_card_shows_only_the_exercise_whose_category_is_in_the_open_zona(self):
        db.crear_periodo(self.conn, 1, "lesion", self.DAY, None, zonas=["hombro"])
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        t_es = T("es")
        self.assertIn(t_es("entreno_sesion.dolor_titulo", zona=t_es("entreno.categoria.hombro")), r.text)
        self.assertIn("press militar", r.text)
        self.assertNotIn("sentadilla", r.text)

    def test_closed_period_before_today_shows_no_card(self):
        db.crear_periodo(self.conn, 1, "lesion", "2026-10-01", "2026-10-05", zonas=["hombro"])
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        self.assertNotIn('name="ejercicio_0"', r.text)

    def test_excluded_set_is_never_asked_about(self):
        # The excluded set is still LISTED (struck) in the sets section, so "press militar" still
        # appears on the page -- what must be absent is the dolor card itself, since this session
        # then has no active, non-excluded exercise left in the open zona.
        db.crear_periodo(self.conn, 1, "lesion", self.DAY, None, zonas=["hombro"])
        db.set_series_excluida(self.conn, 1, "garmin", "s1", 1, True)
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        t_es = T("es")
        self.assertNotIn(t_es("entreno_sesion.dolor_titulo", zona=t_es("entreno.categoria.hombro")), r.text)
        self.assertNotIn('name="ejercicio_0"', r.text)

    def test_card_exercise_text_matches_series_display_case(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: the card's own exercise text comes from the
        same display form Series already uses on this screen (ruling 6's _exercise_display: a
        capital first letter, display only) -- never a second, independent case rule. "press
        militar" is stored lower-case; both sections must show the identical "Press militar"."""
        db.crear_periodo(self.conn, 1, "lesion", self.DAY, None, zonas=["hombro"])
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        self.assertEqual(r.text.count(">Press militar<"), 2, "Series' own row and the dolor card's row")
        self.assertNotIn(">press militar<", r.text)
        # The hidden input the form posts back still carries the RAW stored key, unchanged.
        self.assertIn('name="ejercicio_0" value="press militar"', r.text)

    def test_each_rows_fields_get_an_aria_label_naming_its_own_exercise(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: with two exercises sharing the same open zona,
        the visible label text alone ("Durante"/"Después") is identical for every row, so a screen
        reader needs each select/input's own aria-label naming ITS exercise to tell them apart."""
        db.crear_periodo(self.conn, 1, "lesion", self.DAY, None, zonas=["hombro", "pierna"])
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        t_es = T("es")

        def aria(campo_key: str, ejercicio: str) -> str:
            return t_es("entreno_sesion.dolor_campo_aria", campo=t_es(campo_key), ejercicio=ejercicio)

        self.assertIn(f'aria-label="{aria("entreno_sesion.dolor_durante_label", "Press militar")}"', r.text)
        self.assertIn(f'aria-label="{aria("entreno_sesion.dolor_despues_label", "Press militar")}"', r.text)
        self.assertIn(f'aria-label="{aria("entreno_sesion.dolor_movimiento_label", "Press militar")}"', r.text)
        self.assertIn(f'aria-label="{aria("entreno_sesion.dolor_durante_label", "Sentadilla")}"', r.text)
        self.assertIn(f'aria-label="{aria("entreno_sesion.dolor_despues_label", "Sentadilla")}"', r.text)
        self.assertIn(f'aria-label="{aria("entreno_sesion.dolor_movimiento_label", "Sentadilla")}"', r.text)

    def test_aria_labels_in_english(self):
        """Ruling 1, English: the same per-exercise aria-label template reads through en.json."""
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        db.crear_periodo(self.conn, 1, "lesion", self.DAY, None, zonas=["hombro"])
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        t_en = T("en")
        aria = t_en("entreno_sesion.dolor_campo_aria", campo=t_en("entreno_sesion.dolor_durante_label"),
                    ejercicio="Press militar")
        self.assertIn(f'aria-label="{aria}"', r.text)
        self.assertIn(">Press militar<", r.text)

    def test_post_saves_and_redirects_with_a_result_line(self):
        db.crear_periodo(self.conn, 1, "lesion", self.DAY, None, zonas=["hombro"])
        r = client.post(f"/day/{self.DAY}/entreno/s1/dolor", data={
            "n": "1", "ejercicio_0": "press militar", "zona_0": "hombro",
            "durante_0": "3", "despues_0": "2", "movimiento_0": "al bajar la barra",
        })
        self.assertEqual(r.status_code, 303)
        self.assertIn("dolor_guardado=1", r.headers["location"])
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: the redirect lands back on the card itself
        # (`#dolor`, comun._ancla), not at the top of the page.
        self.assertTrue(r.headers["location"].endswith("#dolor"))
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s1")
        self.assertEqual(len(filas), 1)
        self.assertEqual((filas[0]["dolor_durante"], filas[0]["dolor_despues"], filas[0]["movimiento"]),
                          (3, 2, "al bajar la barra"))
        r2 = client.get(r.headers["location"])
        self.assertIn(_es("entreno_sesion.dolor_guardado"), r2.text)
        self.assertIn('id="dolor"', r2.text)

    def test_editing_again_prefills_the_saved_values(self):
        db.crear_periodo(self.conn, 1, "lesion", self.DAY, None, zonas=["hombro"])
        db.registrar_dolor_entreno(self.conn, 1, self.DAY, "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 4, "dolor_despues": 1,
             "movimiento": "al final del rango"},
        ])
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        self.assertIn('value="al final del rango"', r.text)
        self.assertIn('value="4" selected', r.text)
        self.assertIn('value="1" selected', r.text)

    def test_post_rejects_out_of_range_as_no_answer_never_a_500(self):
        db.crear_periodo(self.conn, 1, "lesion", self.DAY, None, zonas=["hombro"])
        r = client.post(f"/day/{self.DAY}/entreno/s1/dolor", data={
            "n": "1", "ejercicio_0": "press militar", "zona_0": "hombro", "durante_0": "99",
        })
        self.assertEqual(r.status_code, 303)
        filas = db.dolor_de_sesion(self.conn, 1, "garmin", "s1")
        self.assertIsNone(filas[0]["dolor_durante"])

    def test_another_persons_session_404s_on_post(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        _as(self, 2)
        r = client.post(f"/day/{self.DAY}/entreno/s1/dolor", data={"n": "0"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
