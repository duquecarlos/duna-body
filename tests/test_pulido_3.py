"""BODY-PULIDO-3: the round BODY-DISENO-CENSO-2 found, carried out together since each item is
small and they share templates/CSS -- 44px tap targets on five nav/footer links (DESIGN rule 11),
the Turnos strip's readiness score marked as an estimate (rule 4), Dia's activity fallback never
printing Garmin's raw type code (rule 10), Hoy's shared-figure row keeping its one documented
exception to rule 3 (a provenance line instead of a link), a habit's off-days context chip no
longer borrowing the night tint (rule 7), a household goal's member verdicts keeping their
coloured chip class, and Ajustes' link rows becoming fully tappable cards (rule 11). Invented demo
data only, no real health figures.
"""
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import compartir, config, db, goals, identity, web

_STATIC_DIR = Path(__file__).resolve().parent.parent / "body" / "static"
_TODAY = date(2026, 11, 12)
_MONDAY = _TODAY - timedelta(_TODAY.weekday())
_WEEK_DAYS = [(_MONDAY + timedelta(i)).isoformat() for i in range(7)]

client = TestClient(web.app, follow_redirects=False)


def _as(tc: unittest.TestCase, person_id: int) -> None:
    tc.addCleanup(web.app.dependency_overrides.clear)
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _reset_db(tc: unittest.TestCase, name: str):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _add_person(conn, pid: int, name: str) -> None:
    conn.execute("INSERT INTO person (id, display_name) VALUES (?, ?)", (pid, name))
    conn.commit()


def _link_household(conn, *person_ids, hogar_id="h-1") -> None:
    conn.executemany(
        "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
        [(pid, hogar_id) for pid in person_ids])
    conn.commit()


def _insert_shift(conn, person_id: int, day: str, kind: str) -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _seed_days(conn, person_id: int, key: str, start: str, n: int, base: float, step: float = 0.5) -> None:
    year, month, day = (int(x) for x in start.split("-"))
    d0 = date(year, month, day)
    for i in range(n):
        db.set_metric(conn, (d0 + timedelta(i)).isoformat(), key, base + i * step, "garmin", person_id)


def _insert_activity(conn, person_id: int, source_id: str, day: str, act_type: str | None) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, ?, NULL, 'garmin')",
        (person_id, source_id, day, f"{day}T07:00:00", act_type))


def _seed_readiness_history(conn, person_id: int, end: date, days: int = 40) -> None:
    """40 consecutive days of the five readiness parts ending on `end`, with a small wobble so
    every baseline has a non-zero spread (the same shape test_readiness.py's make_db uses) --
    enough for `end` itself to score, but short of the 56-day strip's earliest cells, which need a
    7-day (baselines.MIN_DAYS) window starting 30 days further back than this history reaches."""
    start = end - timedelta(days - 1)
    d, i = start, 0
    while d <= end:
        wobble = (-1) ** i
        for k, v in (("hrv_last_night", 50 + 2 * wobble), ("resting_hr", 55 + wobble),
                     ("stress_avg", 30 + 3 * wobble), ("sleep_minutes", 450),
                     ("sleep_need_min", 480), ("body_battery_wake", 75)):
            db.set_metric(conn, d.isoformat(), k, v, "garmin", person_id)
        d += timedelta(1)
        i += 1
    conn.commit()


# ---- BODY-PULIDO-NAV-GLOBAL-2: 44px tap targets ----------------------------------------------

class NavGlobalCssTest(unittest.TestCase):
    """The shared CSS rule each site relies on, read straight from body.css."""

    def test_ayuda_ctx_is_a_full_width_44px_right_aligned_row(self):
        css = (_STATIC_DIR / "body.css").read_text(encoding="utf-8")
        self.assertRegex(css, r'\.ayuda-ctx\{[^}]*min-height:44px')
        self.assertRegex(css, r'\.ayuda-ctx\{[^}]*justify-content:flex-end')

    def test_foot_links_get_a_44px_inline_target(self):
        css = (_STATIC_DIR / "body.css").read_text(encoding="utf-8")
        self.assertRegex(css, r'\.foot a\{[^}]*min-height:44px')

    def test_taplink_rule_used_by_trends_and_history_is_still_44px(self):
        css = (_STATIC_DIR / "body.css").read_text(encoding="utf-8")
        self.assertRegex(css, r'\.taplink\{[^}]*min-height:44px')


class NavGlobalRenderedTest(unittest.TestCase):
    def setUp(self):
        _reset_db(self, "nav_global.db")
        _as(self, 1)

    def test_in_context_help_and_footer_links_render_on_an_ordinary_page(self):
        r = client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<a class="ayuda-ctx" href="/ayuda#comparar-una-metrica-con-tu-base">', r.text)
        self.assertIn('<a href="/ayuda">', r.text)
        self.assertIn('<a href="/settings">', r.text)
        self.assertNotIn('<a href="/ayuda" style="text-decoration:underline">', r.text)

    def test_trends_back_to_history_link_reuses_taplink(self):
        conn = db.connect()
        _seed_days(conn, 1, "resting_hr", "2026-08-01", 20, 58)
        conn.commit()
        conn.close()
        r = client.get("/trends?m=2026-08")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<a class="taplink" href="/trends">', r.text)

    def test_history_shift_empty_link_reuses_taplink(self):
        conn = db.connect()
        db.set_metric(conn, "2026-10-01", "resting_hr", 55.0, "garmin", 1)
        conn.commit()
        db.set_trabaja_turnos(conn, True, 1)
        conn.close()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<a class="taplink" href="/shifts">', r.text)


# ---- BODY-PULIDO-TURNOS-SCORE-1: the strip's score is an estimate -----------------------------

class TurnosStripApproxTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "turnos_score.db")
        db.set_trabaja_turnos(self.conn, True, 1)
        _seed_readiness_history(self.conn, 1, _TODAY)
        _as(self, 1)

    def test_scored_cell_carries_the_same_approx_marker_as_hoy(self):
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<span class="approx">≈</span>', r.text)

    def test_a_day_with_no_score_still_shows_a_plain_dash(self):
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/shifts")
        cells = re.findall(r'<a class="strip-cell[^"]*"[^>]*>(.*?)</a>', r.text)
        self.assertTrue(any(c == "—" for c in cells))


# ---- BODY-PULIDO-DIA-ACTIVIDAD-1: no raw Garmin type code on Dia -------------------------------

class DayActivityFallbackTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "dia_actividad.db")
        _as(self, 1)

    def test_unnamed_known_type_shows_its_translated_kind(self):
        _insert_activity(self.conn, 1, "act-known", "2026-09-10", "strength_training")
        self.conn.commit()
        r = client.get("/day/2026-09-10")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Entrenamiento de fuerza", r.text)

    def test_unnamed_unknown_type_shows_a_plain_generic_label_never_the_code(self):
        _insert_activity(self.conn, 1, "act-unknown", "2026-09-11", "padel_raqueta")
        self.conn.commit()
        r = client.get("/day/2026-09-11")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Actividad", r.text)
        self.assertNotIn("padel_raqueta", r.text)

    def test_english_unknown_type_label(self):
        db.set_language(self.conn, "en", 1)
        _insert_activity(self.conn, 1, "act-unknown-en", "2026-09-12", "padel_raqueta")
        self.conn.commit()
        r = client.get("/day/2026-09-12")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Activity", r.text)
        self.assertNotIn("padel_raqueta", r.text)


# ---- BODY-PULIDO-HOGAR-ENLACE-HOY-1: a shared figure's row stays a non-link, with provenance ---

class HogarProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "hogar_enlace.db")
        _add_person(self.conn, 2, "Ana")
        _link_household(self.conn, 1, 2)
        db.set_metric(self.conn, _TODAY.isoformat(), "resting_hr", 55.0, "garmin", 2)
        db.set_metric(self.conn, _TODAY.isoformat(), "resting_hr", 58.0, "garmin", 1)
        self.conn.commit()
        compartir.compartir(self.conn, 2, "resting_hr")
        _as(self, 1)

    def test_row_states_provenance_in_words_in_spanish(self):
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ana compartió esto · su reloj · hoy", r.text)

    def test_row_states_provenance_in_words_in_english(self):
        db.set_language(self.conn, "en", 1)
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Shared by Ana · their watch · today", r.text)

    def test_the_row_itself_is_never_a_link(self):
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        hogar_start = r.text.index("Tu hogar")
        hogar_card = r.text[hogar_start:r.text.index("</ul>", hogar_start)]
        self.assertNotIn("<a ", hogar_card)
        self.assertNotIn("<a>", hogar_card)


# ---- BODY-PULIDO-HABITO-CONTEXTO-1: off_days must not carry the night tint ---------------------

class HabitContextChipTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "habito_contexto.db")
        _as(self, 1)

    def test_off_days_chip_gets_the_off_family_not_the_night_tint(self):
        db.set_metric(self.conn, _TODAY.isoformat(), "resting_hr", 58.0, "garmin", 1)
        _insert_shift(self.conn, 1, _TODAY.isoformat(), "off")
        gid = goals.propose(self.conn, 1, "Cuidar la espalda", _TODAY.isoformat(), kind="milestone",
                            deadline="2027-01-01")
        goals.activate(self.conn, gid)
        goals.add_habit(self.conn, gid, "Estirar", {"per": "day", "times": 1, "context": "off_days"})
        self.conn.commit()

        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<span class="chip off">días libres</span>', r.text)
        self.assertNotIn('<span class="chip night">días libres</span>', r.text)

    def test_before_night_chip_keeps_the_night_family(self):
        db.set_metric(self.conn, _TODAY.isoformat(), "resting_hr", 58.0, "garmin", 1)
        _insert_shift(self.conn, 1, (_TODAY + timedelta(1)).isoformat(), "night")
        gid = goals.propose(self.conn, 1, "Descansar antes de noche", _TODAY.isoformat(), kind="milestone",
                            deadline="2027-01-01")
        goals.activate(self.conn, gid)
        goals.add_habit(self.conn, gid, "Siesta corta", {"per": "day", "times": 1, "context": "before_night"})
        self.conn.commit()

        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<span class="chip night">antes de noche</span>', r.text)


# ---- BODY-PULIDO-METAS-MIEMBROS-1: a member's verdict keeps its coloured chip class ------------

class MembersVerdictChipTest(unittest.TestCase):
    def test_other_members_verdict_renders_as_the_same_chip_the_viewers_own_verdict_uses(self):
        conn = _reset_db(self, "metas_miembros.db")
        _add_person(conn, 2, "Ana")
        _link_household(conn, 1, 2)
        gid = goals.propose(conn, 1, "Dormir mejor en pareja", _WEEK_DAYS[0], kind="milestone",
                            deadline="2027-01-01", hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        hid = goals.add_habit(conn, gid, "Dormir temprano", {"per": "week", "times": 1})
        for day in _WEEK_DAYS:
            goals.check_habit(conn, hid, 2, day, True, hoy=date.fromisoformat(_WEEK_DAYS[6]))
        goals.weekly_check(conn, gid, 2, _WEEK_DAYS[0])
        conn.commit()

        _as(self, 1)
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<span class="chip ok">En camino</span>', r.text)
        self.assertNotIn("Ana<span>", r.text)  # the discarded-class shape (BODY-PULIDO-3 fix 6)


# ---- BODY-PULIDO-AJUSTES-1: the link rows become fully tappable cards --------------------------

class AjustesTapTargetTest(unittest.TestCase):
    def setUp(self):
        _reset_db(self, "ajustes_tap.db")
        _as(self, 1)

    def test_ayuda_privacidad_and_asistente_rows_are_each_one_44px_anchor(self):
        r = client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.text, r'<a href="/ayuda" class="card" style="[^"]*min-height:44px[^"]*">')
        self.assertRegex(r.text, r'<a href="/privacidad" class="card" style="[^"]*min-height:44px[^"]*">')
        self.assertRegex(r.text, r'<a href="/ajustes/asistente" class="card" style="[^"]*min-height:44px[^"]*">')
        self.assertNotIn('<a href="/ayuda" class="q"', r.text)


if __name__ == "__main__":
    unittest.main()
