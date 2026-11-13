"""Route-level tests for BODY-MOCK-R2-HOY-1 -- Carlos's mock round 2 Today board: the ruling-1
board order, the "day ahead" merged list (ruling 2), the inline "Tell Body" row (ruling 3), the two
new tiles' band word and partial-day caption (ruling 4), the "Body noticed" card's own note
(ruling 5), the night-week-ahead card (ruling 6), 375px/focus statics (ruling 7), and the demo
person's own full coverage (ruling 8). Invented demo data and synthetic fixtures only, never a
real figure.
"""
import json
import pathlib
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, identity, noche_inusual, web
from body.rutas import hoy
from body.i18n import T

ROOT = pathlib.Path(__file__).resolve().parent.parent
_ES = json.loads((ROOT / "body" / "i18n" / "es.json").read_text(encoding="utf-8"))
_EN = json.loads((ROOT / "body" / "i18n" / "en.json").read_text(encoding="utf-8"))


def _web_conn(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """A connection the web app's own routes read (tests/test_noche_inusual.py's own convention)."""
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    case.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    web.app.dependency_overrides[identity.current_person_id] = lambda: 1
    return conn


def _seed_demo(case: unittest.TestCase, name: str, idioma: str = "es", today: date | None = None):
    conn = _web_conn(case, name)
    demo._seed(conn, dias=30, idioma=idioma, today=today)
    conn.commit()
    return conn


class BoardOrderTest(unittest.TestCase):
    """Ruling 1: body view, score hero (synced time inline), The day ahead, inline "Tell Body"
    row, Body noticed, tiles, eating window, Night week ahead -- in that order, on a person whose
    own data makes every optional section present (the demo person)."""

    def test_order_es(self):
        _seed_demo(self, "orden_es.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        marks = ["cuerpo-vista", 'class="card est"', "Reloj sincronizado",
                 "El día por delante", 'class="tellbody', "Body lo notó",
                 'class="grid2', "Ventana de comida", "Semana nocturna por delante"]
        positions = [page.index(m) for m in marks]
        self.assertEqual(positions, sorted(positions), f"board order broken: {marks}")

    def test_order_en(self):
        _seed_demo(self, "orden_en.db", "en")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        marks = ["cuerpo-vista", 'class="card est"', "Watch synced",
                 "The day ahead", 'class="tellbody', "Body noticed",
                 'class="grid2', "Eating window", "Night week ahead"]
        positions = [page.index(m) for m in marks]
        self.assertEqual(positions, sorted(positions), f"board order broken: {marks}")

    def test_synced_time_is_inside_score_hero_not_a_separate_meta_block(self):
        """The old top `.meta` block is gone -- the synced line now lives inside the score hero
        `<a class="card est">`, after the reading paragraph."""
        _seed_demo(self, "orden_meta.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertNotIn('<div class="meta">', page)
        hero_start = page.index('class="card est"')
        hero_end = page.index("</a>", hero_start)
        self.assertIn("Reloj sincronizado", page[hero_start:hero_end])


class DiaPorDelanteRenderTest(unittest.TestCase):
    """Ruling 2: one time-ordered list merging today's shift, planned session, and due habit --
    rendered in that time order, never an autonomous-sounding claim."""

    def test_shift_session_habit_in_time_order(self):
        _seed_demo(self, "dia_orden.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        start = page.index('<div class="q">El día por delante</div>')
        end = page.index('class="tellbody', start)
        section = page[start:end]
        self.assertIn("08:00", section)
        self.assertIn("hasta las 16:00", section)
        self.assertIn("15:30", section)
        self.assertIn("Upper B", section)
        self.assertIn("3 ejercicios", section)
        self.assertIn("18:00", section)
        self.assertIn("Entrenar piernas", section)
        self.assertLess(section.index("08:00"), section.index("15:30"))
        self.assertLess(section.index("15:30"), section.index("18:00"))

    def test_empty_day_shows_honest_gap(self):
        conn = _web_conn(self, "dia_vacio.db")
        db.set_metric(conn, date.today().isoformat(), "resting_hr", 55.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn(T("es")("today.dia.vacio"), page)

    def test_never_autonomous_sounding_copy(self):
        """DESIGN rule 14 / BODY-MOCK-R2-CENSO-1-INV conflict #3: the rendered page itself never
        claims Body moved or scheduled anything on its own."""
        _seed_demo(self, "dia_sin_autonomia.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text.lower()
        for prohibido in ("body movió", "body cambió tu plan", "body movió tu plan"):
            self.assertNotIn(prohibido, page)

    def test_shift_row_title_reads_as_a_shift_not_the_bare_kind_es(self):
        """BODY-MOCK-R2-HOY-1-M fix 3: the shift row's title reads «Turno de día», never the
        bare kind word «Día» the earlier mock used."""
        _seed_demo(self, "dia_turno_titulo_es.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        start = page.index('<div class="q">El día por delante</div>')
        end = page.index('class="tellbody', start)
        section = page[start:end]
        self.assertIn(_ES["shift.kind.title.day"], section)

    def test_shift_row_title_reads_as_a_shift_not_the_bare_kind_en(self):
        _seed_demo(self, "dia_turno_titulo_en.db", "en")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        start = page.index('<div class="q">The day ahead</div>')
        end = page.index('class="tellbody', start)
        section = page[start:end]
        self.assertIn(_EN["shift.kind.title.day"], section)

    def test_habit_row_same_shape_as_other_rows(self):
        """BODY-MOCK-R2-HOY-1-M fix 2: the habit's own time sits on its own line like every other
        row (never inline before the title), and its check control comes after the title, at the
        row's end (never leading the row)."""
        _seed_demo(self, "dia_habito_forma.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        start = page.index('<div class="q">El día por delante</div>')
        end = page.index('class="tellbody', start)
        section = page[start:end]
        self.assertNotIn('<span class="k">18:00</span> Entrenar piernas', section)
        self.assertIn('<div class="k">18:00</div>', section)
        titulo_pos = section.index("Entrenar piernas")
        boxbtn_pos = section.index("boxbtn", titulo_pos)
        self.assertGreater(boxbtn_pos, titulo_pos, "the check control must come after the title")


class TellBodyRowTest(unittest.TestCase):
    """Ruling 3: an inline row that opens the EXISTING assistant panel, focused -- no second chat
    implementation, so it must point at the same #asis-btn anchor base.html's own panel toggle
    uses."""

    def test_row_present_es(self):
        _seed_demo(self, "tellbody_es.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn('<a class="tellbody taplink" href="#asis-btn">', page)
        self.assertIn(_ES["today.tell_body"], page)

    def test_row_present_en(self):
        _seed_demo(self, "tellbody_en.db", "en")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn('<a class="tellbody taplink" href="#asis-btn">', page)
        self.assertIn(_EN["today.tell_body"], page)

    def test_asis_btn_anchor_matches_the_existing_convention(self):
        """The row's own href is the SAME "#asis-btn" fragment another, already-existing Hoy
        feature (the period aviso's own assistant link) already uses to open the panel -- no
        second chat implementation, a new anchor pointed at the one base.html already defines."""
        _seed_demo(self, "tellbody_anchor.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn('href="#asis-btn">', page)
        self.assertEqual(page.count('href="#asis-btn"'), 2, "the aviso's own link plus the new row")


class TellBodyOpensPanelJsTest(unittest.TestCase):
    """BODY-MOCK-R2-HOY-1-M2 fix 1: the row opens the EXISTING assistant panel through its own
    abrir() path (no second chat implementation, no inline script -- the CSP forbids one)."""

    @classmethod
    def setUpClass(cls):
        js = ROOT / "body" / "static" / "assistant.js"
        cls.text = js.read_text(encoding="utf-8")

    def test_delegated_listener_targets_the_tellbody_row(self):
        self.assertIn('e.target.closest(".tellbody")', self.text)

    def test_it_prevents_the_default_navigation_and_opens_through_abrir(self):
        listener_start = self.text.index('document.addEventListener("click"')
        listener_end = self.text.index("});", listener_start)
        block = self.text[listener_start:listener_end]
        self.assertIn("e.preventDefault()", block)
        self.assertIn("abrir()", block)

    def test_no_inline_script_was_added_for_this(self):
        html = (ROOT / "body" / "templates" / "today.html").read_text(encoding="utf-8")
        self.assertIn('<a class="tellbody taplink" href="#asis-btn">', html)
        self.assertNotIn("<script>", html)


class TileBandTest(unittest.TestCase):
    """Ruling 4: Stress-so-far and Steps tiles, each with a low/usual/high band word against the
    person's own range (never a generic table) and a "by HH:MM" caption when today is partial."""

    @staticmethod
    def _seed_baseline(conn, person_id: int, day: date, key: str, base: float) -> None:
        for i in range(30):
            dia = (day - timedelta(30 - i)).isoformat()
            db.set_metric(conn, dia, key, base + (i % 5 - 2), "garmin", person_id)

    @staticmethod
    def _seed_sync(conn, person_id: int, day: date, utc_hhmm: str) -> None:
        conn.execute(
            "INSERT INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
            "VALUES (?, ?, 'day', 'garmin', ?, '{}')",
            (person_id, day.isoformat(), f"{day.isoformat()}T{utc_hhmm}:00+00:00"))

    def test_low_band_and_partial_caption_es(self):
        conn = _web_conn(self, "banda_baja_es.db")
        today = date.today()
        self._seed_baseline(conn, 1, today, "stress_avg", 30.0)
        db.set_metric(conn, today.isoformat(), "stress_avg", 5.0, "garmin", 1)
        self._seed_sync(conn, 1, today, "19:30")  # UTC 19:30 -> America/Bogota 14:30
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        tile_start = page.index("#stress_avg")
        tile_end = page.index("</a>", tile_start)
        tile = page[tile_start:tile_end]
        self.assertIn(_ES["today.tile.stress_so_far"], tile)
        self.assertIn(_ES["today.tile.banda.low"], tile)
        self.assertIn("hasta las 14:30", tile)

    def test_usual_band_steps_en(self):
        conn = _web_conn(self, "banda_usual_en.db")
        db.set_language(conn, "en", 1)
        today = date.today()
        self._seed_baseline(conn, 1, today, "steps", 6000.0)
        db.set_metric(conn, today.isoformat(), "steps", 6000.0, "garmin", 1)
        self._seed_sync(conn, 1, today, "19:30")
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        tile_start = page.index("#steps")
        tile_end = page.index("</a>", tile_start)
        tile = page[tile_start:tile_end]
        self.assertIn(_EN["today.tile.banda.usual"], tile)
        self.assertIn("by 14:30", tile)

    def test_high_band(self):
        conn = _web_conn(self, "banda_alta.db")
        today = date.today()
        self._seed_baseline(conn, 1, today, "stress_avg", 30.0)
        db.set_metric(conn, today.isoformat(), "stress_avg", 90.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        tile_start = page.index("#stress_avg")
        tile_end = page.index("</a>", tile_start)
        self.assertIn(_ES["today.tile.banda.high"], page[tile_start:tile_end])

    def test_no_baseline_no_band_honest_gap(self):
        """Fewer than baselines.MIN_DAYS of history -- the honest gap, never a guessed band."""
        conn = _web_conn(self, "banda_sin_historia.db")
        today = date.today()
        db.set_metric(conn, today.isoformat(), "stress_avg", 50.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        tile_start = page.index("#stress_avg")
        tile_end = page.index("</a>", tile_start)
        tile = page[tile_start:tile_end]
        for banda in (_ES["today.tile.banda.low"], _ES["today.tile.banda.usual"], _ES["today.tile.banda.high"]):
            self.assertNotIn(banda, tile)

    def test_no_caption_without_today_sync(self):
        """A day other than today, or no sync yet, never shows the "by HH:MM" partial caption --
        that caption only makes sense for today's own in-progress figures."""
        conn = _web_conn(self, "banda_sin_sync.db")
        today = date.today()
        self._seed_baseline(conn, 1, today, "stress_avg", 30.0)
        db.set_metric(conn, today.isoformat(), "stress_avg", 30.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        tile_start = page.index("#stress_avg")
        tile_end = page.index("</a>", tile_start)
        self.assertNotIn("hasta las", page[tile_start:tile_end])


class NocheInusualCardTest(unittest.TestCase):
    """Ruling 5: "Body noticed" is a card (not a one-line aviso), with "Add a note" writing to an
    EXISTING private store (the `moment` table, under noche_inusual.NOTA_KIND) and an inline
    dismiss -- both in es and en."""

    HR_KEY, HRV_KEY, SCORE_KEY = "sleep_hr_avg", "hrv_last_night", "sleep_score"

    def _seed_unusual_night(self, conn, day: date) -> None:
        for i in range(30):
            dia = (day - timedelta(30 - i)).isoformat()
            db.set_metric(conn, dia, self.HRV_KEY, 60.0 + (i % 5 - 2), "garmin", 1)
            db.set_metric(conn, dia, self.SCORE_KEY, 70.0 + (i % 5 - 2), "garmin", 1)
        db.set_metric(conn, day.isoformat(), self.HRV_KEY, 20.0, "garmin", 1)
        db.set_metric(conn, day.isoformat(), self.SCORE_KEY, 15.0, "garmin", 1)

    def test_card_present_with_note_form(self):
        conn = _web_conn(self, "noticed_card.db")
        today = date.today()
        self._seed_unusual_night(conn, today)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn(_ES["today.noche_inusual_titulo"], page)
        self.assertIn(f'action="/day/{today.isoformat()}/noche-inusual/nota"', page)
        self.assertIn(_ES["today.noche_inusual.nota_btn"], page)
        self.assertIn(f'action="/day/{today.isoformat()}/noche-inusual/visto"', page)
        self.assertIn(_ES["today.noche_inusual.descartar_btn"], page)

    def test_save_note_then_shows_saved_text_not_the_form(self):
        conn = _web_conn(self, "noticed_note.db")
        today = date.today()
        self._seed_unusual_night(conn, today)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        r = client.post(f"/day/{today.isoformat()}/noche-inusual/nota",
                        data={"nota": "Trabajo hasta tarde", "back": "/"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        page = client.get("/").text
        self.assertIn("Trabajo hasta tarde", page)
        self.assertNotIn(f'action="/day/{today.isoformat()}/noche-inusual/nota"', page)
        self.assertEqual(noche_inusual.nota(conn, 1, today.isoformat()), "Trabajo hasta tarde")

    def test_blank_note_is_never_stored(self):
        conn = _web_conn(self, "noticed_blank.db")
        today = date.today()
        self._seed_unusual_night(conn, today)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        client.post(f"/day/{today.isoformat()}/noche-inusual/nota", data={"nota": "   "})
        self.assertIsNone(noche_inusual.nota(conn, 1, today.isoformat()))
        page = client.get("/").text
        self.assertIn(f'action="/day/{today.isoformat()}/noche-inusual/nota"', page, "form still there, nothing saved")

    def test_dismiss_removes_the_card(self):
        conn = _web_conn(self, "noticed_dismiss.db")
        today = date.today()
        self._seed_unusual_night(conn, today)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        self.assertIn(_ES["today.noche_inusual_titulo"], client.get("/").text)
        r = client.post(f"/day/{today.isoformat()}/noche-inusual/visto")
        self.assertEqual(r.status_code, 303)
        self.assertNotIn(_ES["today.noche_inusual_titulo"], client.get("/").text)

    def test_note_private_like_checkin_nota(self):
        """Ruling 5's own store choice: the generic `moment` table under its own kind, same as
        noche_inusual.py's dismissal -- never a new table."""
        conn = _web_conn(self, "noticed_privacy.db")
        today = date.today()
        self._seed_unusual_night(conn, today)
        conn.commit()
        noche_inusual.guardar_nota(conn, 1, today.isoformat(), "una nota privada")
        row = conn.execute("SELECT kind FROM moment WHERE note = ?", ("una nota privada",)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], noche_inusual.NOTA_KIND)

    def test_card_absent_on_a_normal_night(self):
        conn = _web_conn(self, "noticed_absent.db")
        today = date.today()
        for i in range(30):
            dia = (today - timedelta(30 - i)).isoformat()
            db.set_metric(conn, dia, self.HRV_KEY, 60.0, "garmin", 1)
        db.set_metric(conn, today.isoformat(), self.HRV_KEY, 61.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertNotIn(_ES["today.noche_inusual_titulo"], page)


class SemanaNocturnaCardTest(unittest.TestCase):
    """Ruling 6: a "Night week ahead" card when the next night block starts within 7 days -- its
    own dates and the person's own recovery estimate, linking to Turnos; absent otherwise."""

    def _seed_future_night(self, conn, start: date, length: int = 2) -> None:
        for i in range(length):
            conn.execute(
                "INSERT INTO shift (person_id, day, kind, start_local, end_local, source) VALUES "
                "(1, ?, 'night', ?, ?, 'manual')",
                ((start + timedelta(i)).isoformat(),
                 f"{(start + timedelta(i)).isoformat()}T22:00:00",
                 f"{(start + timedelta(i + 1)).isoformat()}T06:00:00"))

    def test_card_present_with_honest_gap_recovery(self):
        conn = _web_conn(self, "semana_presente.db")
        today = date.today()
        self._seed_future_night(conn, today + timedelta(3))
        db.set_metric(conn, today.isoformat(), "resting_hr", 55.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn(_ES["today.semana_nocturna.titulo"], page)
        self.assertIn(_ES["today.semana_nocturna.sin_dato"], page)
        self.assertIn('href="/shifts"', page)

    def test_card_absent_with_no_upcoming_block(self):
        conn = _web_conn(self, "semana_ausente.db")
        db.set_metric(conn, date.today().isoformat(), "resting_hr", 55.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertNotIn(_ES["today.semana_nocturna.titulo"], page)

    def test_card_absent_when_block_is_more_than_7_days_out(self):
        conn = _web_conn(self, "semana_lejos.db")
        today = date.today()
        self._seed_future_night(conn, today + timedelta(9))
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertNotIn(_ES["today.semana_nocturna.titulo"], page)

    def test_never_a_body_will_remind_claim(self):
        """DESIGN rule 14 / BODY-MOCK-R2-CENSO-1-INV conflicts #1/#3: the card states the block's
        own dates and the person's own recovery estimate, never a promise that Body will act."""
        conn = _web_conn(self, "semana_sin_promesa.db")
        today = date.today()
        self._seed_future_night(conn, today + timedelta(3))
        db.set_metric(conn, today.isoformat(), "resting_hr", 55.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text.lower()
        card_start = page.index(_ES["today.semana_nocturna.titulo"].lower())
        card_end = page.index("</div>", card_start)
        for prohibido in ("te voy a recordar", "i'll remind you", "voy a avisarte"):
            self.assertNotIn(prohibido, page[card_start:card_end])


class EstaticosAccesibilidadTest(unittest.TestCase):
    """Ruling 7: phone first (375 px, no horizontal scroll), light/dark, keyboard-reachable with
    visible focus, for every new card -- the static checks that do not need a live render."""

    def test_tellbody_css_has_no_fixed_width_over_375(self):
        css = (ROOT / "body" / "static" / "body.css").read_text(encoding="utf-8")
        start = css.index(".tellbody{")
        end = css.index("}", start)
        rule = css[start:end]
        for m in re.finditer(r"width:\s*(\d+)px", rule):
            self.assertLessEqual(int(m.group(1)), 375, f"fixed width over 375px in .tellbody: {rule}")
        self.assertIn("width:100%", rule)
        self.assertIn("min-height:44px", rule, "tap target stays >= 44px (DESIGN rule)")

    def test_focus_visible_rule_covers_new_controls(self):
        """The global :focus-visible outline rule (DESIGN rule) applies to every new focusable
        element for free -- no new CSS needed per control, so this only checks the rule still
        exists and is not scoped away from the new elements' own selectors."""
        css = (ROOT / "body" / "static" / "body.css").read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)

    def test_new_template_markup_has_no_fixed_px_width_either(self):
        html = (ROOT / "body" / "templates" / "today.html").read_text(encoding="utf-8")
        for m in re.finditer(r'style="[^"]*width:\s*(\d+)px', html):
            self.assertLessEqual(int(m.group(1)), 375)


class DemoCoverageTest(unittest.TestCase):
    """Ruling 8: the demo person shows every new card -- shift today, a planned session with a
    time, a timed habit, a night block starting in 3 days, and partial-day stress/steps."""

    def test_every_new_card_present_es(self):
        conn = _seed_demo(self, "demo_completo_es.db", "es")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn("08:00", page)
        self.assertIn("hasta las 16:00", page)
        self.assertIn("Upper B", page)
        self.assertIn("15:30", page)
        self.assertIn("18:00", page)
        self.assertIn("Entrenar piernas", page)
        self.assertIn(_ES["today.semana_nocturna.titulo"], page)
        fechas_esperadas = hoy._semana_nocturna_ctx(T("es"), conn, 1, date.today())["fechas"]
        self.assertIn(fechas_esperadas, page)
        stress_tile = page[page.index("#stress_avg"):page.index("</a>", page.index("#stress_avg"))]
        self.assertIn(_ES["today.tile.banda.usual"], stress_tile)
        self.assertIn("hasta las", stress_tile)
        steps_tile = page[page.index("#steps"):page.index("</a>", page.index("#steps"))]
        self.assertIn("hasta las", steps_tile)

    def test_every_new_card_present_en(self):
        _seed_demo(self, "demo_completo_en.db", "en")
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn("Day", page)
        self.assertIn("until 16:00", page)
        self.assertIn("Train legs", page)
        self.assertIn(_EN["today.semana_nocturna.titulo"], page)
        stress_tile = page[page.index("#stress_avg"):page.index("</a>", page.index("#stress_avg"))]
        self.assertIn("by ", stress_tile)


class DemoStressBandAcrossDatesTest(unittest.TestCase):
    """BODY-PLAN-MODULO-1-R: the partial-day stress tile's band must read "usual" whichever real
    date the demo happens to run on -- 21 consecutive anchors starting the Monday before a night
    week (body/demo.py's own 4-week cycle, confirmed by _week_index), so the sweep covers every
    weekday at least three times and a night block before, on (the week's five night days) and
    after (the taper _recovery_factor gives the days right after it) `today`."""

    ANCHOR0 = date(2024, 1, 8)  # a Monday; 2024-01-15 is that cycle's own night week (BODY-DEMO-TURNOS-1)

    def test_usual_band_every_day_of_the_sweep(self):
        for offset in range(21):
            hoy = self.ANCHOR0 + timedelta(offset)
            with self.subTest(hoy=hoy.isoformat()):
                _seed_demo(self, f"banda_barrido_{offset}.db", "es", today=hoy)
                page = TestClient(web.app, follow_redirects=False).get("/").text
                stress_tile = page[page.index("#stress_avg"):page.index("</a>", page.index("#stress_avg"))]
                self.assertIn(_ES["today.tile.banda.usual"], stress_tile,
                              f"stress band not usual for hoy={hoy.isoformat()}: {stress_tile}")


if __name__ == "__main__":
    unittest.main()
