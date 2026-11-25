"""BODY-HOY-ESCRITORIO-1: Hoy's own top block from tablet width up (rulings 1-2 -- the score hero
and "El día por delante" beside the body view, never the figure alone filling the screen) and the
trained/still-to-come glow split (ruling 3 -- solid for what today's logged training actually
worked, dashed for whatever today's plan still owes). BODY-HOY-ESCRITORIO-2 adds the block BELOW
.hoy-top: one row of tiles, then "Body noticed" beside "Night week ahead", then the eating window,
then the "Tell Body" row, from 768px -- the mock's desktop order, placed with `order`/`flex` on the
phone's own unchanged DOM (`.hoy-abajo`, which replaces the now-dead `.cols-hoy`)."""
import json
import os
import pathlib
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, demo, fechas, identity, readiness, web
from body.rutas import comun

client = TestClient(web.app, follow_redirects=False)
ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = ROOT / "body" / "static" / "body.css"

# BODY-DEMO-HOY-FIJO-1: _seed_full_coverage below seeds the demo with no `today=` and
# HoyAbajoBlockMarkupTest reads "/" through the real clock (comun._today_local) -- pinned so the
# suite never depends on which real day it runs on.
_ANCHOR = date(2026, 11, 20)


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "hoy_escritorio.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)
    return conn


def _mk_person(conn, lang: str = "es") -> int:
    cur = conn.execute(
        "INSERT INTO person (display_name, timezone, language) VALUES (?, 'America/Bogota', ?)", ("P", lang))
    conn.commit()
    return cur.lastrowid


def _use(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = (lambda pid=person_id: pid)


class EntrenadoPendienteGlowTest(unittest.TestCase):
    """Ruling 3: the body lights what today actually holds, as the mock drew it."""

    def setUp(self):
        self.conn = _reset_db(self)

    def _hoy_iso(self, pid: int) -> str:
        return fechas.hoy_local(db.person(self.conn, pid)).isoformat()

    def _algun_dato_hoy(self, pid: int) -> None:
        """So /'s own `_latest_day` gate finds a day at all -- otherwise it renders soon.html."""
        db.set_metric(self.conn, self._hoy_iso(pid), "resting_hr", 55.0, "garmin", pid)
        self.conn.commit()

    def _plan_hoy(self, pid: int, ejercicios: list, status: str = "accepted") -> None:
        self.conn.execute(
            "INSERT INTO plan (person_id, day, title, status, ejercicios_json) VALUES (?, ?, 'Upper B', "
            "?, ?)", (pid, self._hoy_iso(pid), status, json.dumps(ejercicios, ensure_ascii=False)))
        self.conn.commit()

    def _registrar_sesion(self, pid: int, sets: list) -> None:
        """A LOGGED session for today, each (categoria, ejercicio) pair its own set -- through
        db.create_manual_session + db.correct_exercise_set, the same pair body/agente/
        herramientas.py's registrar_entreno writes a person's own entry through (never
        save_exercise_sets, which upserts a watch read)."""
        source_id = db.create_manual_session(self.conn, pid, self._hoy_iso(pid), None, "Fuerza")
        for i, (categoria, ejercicio) in enumerate(sets, start=1):
            db.correct_exercise_set(self.conn, pid, "manual", source_id, i, category=categoria, exercise=ejercicio)
        self.conn.commit()

    def test_logged_espalda_y_brazo_lights_solid_and_captions_trained(self):
        pid = _mk_person(self.conn)
        self._algun_dato_hoy(pid)
        self._registrar_sesion(pid, [("espalda", "remo con barra"), ("brazo", "curl de bíceps")])
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Entrenado hoy: bíceps, dorsales, espalda alta y deltoides posteriores.", r.text)
        self.assertNotIn("Por venir", r.text)
        self.assertNotIn("cuerpo-vista-glow pendiente", r.text)

    def test_plan_pierna_not_trained_shows_dashed_and_planned_only_caption(self):
        pid = _mk_person(self.conn)
        self._algun_dato_hoy(pid)
        self._plan_hoy(pid, [{"ejercicio": "sentadilla", "series": 4, "categoria": "pierna"}])
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("cuerpo-vista-glow pendiente", r.text)
        self.assertIn("Iluminado: lo que entrena tu sesión de hoy (cuádriceps y isquiotibiales)", r.text)
        self.assertNotIn("Entrenado hoy", r.text)

    def test_planned_group_already_trained_is_solid_only(self):
        pid = _mk_person(self.conn)
        self._algun_dato_hoy(pid)
        self._plan_hoy(pid, [{"ejercicio": "remo con barra", "series": 4, "categoria": "espalda"}])
        self._registrar_sesion(pid, [("espalda", "remo con barra")])
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Entrenado hoy: dorsales, espalda alta y deltoides posteriores.", r.text)
        self.assertNotIn("Por venir", r.text)
        self.assertNotIn("cuerpo-vista-glow pendiente", r.text)

    def test_trained_plus_pending_caption_both_languages(self):
        pid_es = _mk_person(self.conn, lang="es")
        self._algun_dato_hoy(pid_es)
        self._plan_hoy(pid_es, [{"ejercicio": "remo con barra", "series": 4, "categoria": "espalda"},
                                 {"ejercicio": "sentadilla", "series": 4, "categoria": "pierna"}])
        self._registrar_sesion(pid_es, [("espalda", "remo con barra")])
        _use(pid_es)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Entrenado hoy: dorsales, espalda alta y deltoides posteriores. "
                       "Por venir: cuádriceps y isquiotibiales.", r.text)

        conn2 = _reset_db(self)
        pid_en = _mk_person(conn2, lang="en")
        db.set_metric(conn2, fechas.hoy_local(db.person(conn2, pid_en)).isoformat(),
                      "resting_hr", 55.0, "garmin", pid_en)
        conn2.commit()
        conn2.execute(
            "INSERT INTO plan (person_id, day, title, status, ejercicios_json) VALUES (?, ?, 'Upper B', "
            "'accepted', ?)",
            (pid_en, fechas.hoy_local(db.person(conn2, pid_en)).isoformat(),
             json.dumps([{"ejercicio": "row", "series": 4, "categoria": "espalda"},
                         {"ejercicio": "squat", "series": 4, "categoria": "pierna"}])))
        conn2.commit()
        source_id = db.create_manual_session(
            conn2, pid_en, fechas.hoy_local(db.person(conn2, pid_en)).isoformat(), None, "Strength")
        db.correct_exercise_set(conn2, pid_en, "manual", source_id, 1, category="espalda", exercise="row")
        conn2.commit()
        _use(pid_en)
        r2 = client.get("/")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("Trained today: lats, upper back, and rear delts. "
                      "Still to come: quads and hamstrings.", r2.text)

    def test_done_plan_contributes_no_pending_group(self):
        pid = _mk_person(self.conn)
        self._algun_dato_hoy(pid)
        self._plan_hoy(pid, [{"ejercicio": "sentadilla", "series": 4, "categoria": "pierna"}], status="done")
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Hoy no hay sesión planeada.", r.text)
        self.assertNotIn("cuerpo-vista-glow pendiente", r.text)

    def test_nothing_logged_or_planned_keeps_the_existing_empty_caption(self):
        pid = _mk_person(self.conn)
        self._algun_dato_hoy(pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Hoy no hay sesión planeada.", r.text)

    def test_lesion_hatch_still_drawn_above_a_trained_glow(self):
        pid = _mk_person(self.conn)
        self._algun_dato_hoy(pid)
        self._registrar_sesion(pid, [("hombro", "press militar")])
        hoy = self._hoy_iso(pid)
        db.crear_periodo(self.conn, pid, "lesion", hoy, zonas=["hombro"])
        self.conn.commit()
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("En recuperación: hombro", r.text)
        self.assertIn("cuerpo-vista-glow limitada", r.text)
        self.assertIn("Entrenado hoy: hombros.", r.text)


class HoyTopBlockMarkupTest(unittest.TestCase):
    """Rulings 1-2: one grid, two columns from 768px, the figure rendered once, never cropped."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_hoy_top_grid_classes_present(self):
        pid = _mk_person(self.conn)
        db.set_metric(self.conn, fechas.hoy_local(db.person(self.conn, pid)).isoformat(),
                      "resting_hr", 55.0, "garmin", pid)
        self.conn.commit()
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="hoy-top"', r.text)
        self.assertIn('class="hoy-top-left"', r.text)
        self.assertIn("hoy-figura", r.text)

    def test_figure_rendered_exactly_once(self):
        pid = _mk_person(self.conn)
        db.set_metric(self.conn, fechas.hoy_local(db.person(self.conn, pid)).isoformat(),
                      "resting_hr", 55.0, "garmin", pid)
        self.conn.commit()
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text.count("cuerpo-vista-track"), 1)


class HoyTopBlockCssTest(unittest.TestCase):
    """Rulings 1-2: the grid, its named custom properties, and the figure's own cap -- checked as
    CSS text, the same way tests/test_cuerpo_vista.py's LimitadaStyleInBodyCssTest does."""

    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")
        self.compact = self.css.replace(" ", "").replace("\n", "")

    def test_custom_properties_defined(self):
        self.assertIn("--hoy-figura-min:280px", self.compact)
        self.assertIn("--hoy-figura-max:400px", self.compact)
        self.assertIn("--hoy-figura-parte:36%", self.compact)

    def test_hoy_top_grid_uses_the_named_properties_not_magic_numbers(self):
        self.assertIn(
            ".hoy-top{display:grid;grid-template-columns:minmax(0,1fr)"
            "clamp(var(--hoy-figura-min),var(--hoy-figura-parte),var(--hoy-figura-max))", self.compact)

    def test_grid_sits_inside_the_768_query(self):
        idx = self.compact.index(".hoy-top{display:grid")
        media = self.compact.rindex("@media(min-width:768px){", 0, idx)
        self.assertGreater(idx, media)

    def test_image_is_never_cropped_on_hoy(self):
        self.assertIn(".hoy-figura.cuerpo-vista-img{object-fit:contain}", self.compact)

    def test_desktop_height_cap_present(self):
        self.assertIn(".hoy-figura.cuerpo-vista-track{max-height:min(78vh,720px)}", self.compact)

    def test_phone_cap_is_scoped_to_a_max_width_query(self):
        """BODY-HOY-ESCRITORIO-1-C: the 60svh rule must sit inside a max-width query (below 768px),
        never unscoped after the >=768px block, or it wins at every width by source order alone."""
        marker = ("@media(max-width:767.98px){"
                  ".hoy-figura.cuerpo-vista-track{max-height:60vh;max-height:60svh}}")
        self.assertIn(marker, self.compact)
        self.assertEqual(
            self.compact.count(".hoy-figura.cuerpo-vista-track{max-height:60vh;max-height:60svh}"), 1)

    def test_pending_glow_class_present(self):
        self.assertIn(".cuerpo-vista-glow.pendiente{", self.css)

    def test_lectura_does_not_collapse_hoy_top(self):
        """Ruling (BODY-HOY-ESCRITORIO-1-B): Lectura narrows the page to 820px, where the top row's
        ~400px left column plus the 280-400px body column still fit side by side -- so `.hoy-top`
        must NOT appear in the `[data-ancho="lectura"]` list that collapses `.cols-*` to one column
        (those collapse because their LISTS get cramped at 820px; the top row is not a list). This
        only pins today's choice as deliberate -- a later edit that adds `.hoy-top` there is a
        decision, not an accident, and must change this test on purpose."""
        self.assertNotIn('[data-ancho="lectura"].hoy-top', self.compact)
        self.assertNotIn(',[data-ancho="lectura"].hoy-top', self.compact)


def _seed_full_coverage(tc: unittest.TestCase) -> "db.sqlite3.Connection":
    """A demo-seeded person (id 1): has an unusual night (`Body noticed`), an upcoming night
    block (`Night week ahead`) and a live nutrient target (`objetivos_nutriente`), so every
    optional card BODY-HOY-ESCRITORIO-2 ruling 1 places renders at once."""
    conn = _reset_db(tc)
    demo._seed(conn, dias=30, idioma="es", today=_ANCHOR)
    conn.commit()
    _use(db.local_person_id(conn))
    return conn


class HoyAbajoBlockMarkupTest(unittest.TestCase):
    """Ruling 1/3: every card below `.hoy-top` carries its own class so CSS `order`/`flex` can
    place it from 768px without the DOM itself changing -- `.cols-hoy` (dead since the figure
    moved into `.hoy-top`, BODY-HOY-ESCRITORIO-1) is gone."""

    def setUp(self):
        patcher = mock.patch.object(comun, "_today_local", return_value=_ANCHOR)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = _seed_full_coverage(self)

    def test_cols_hoy_is_gone_hoy_abajo_present(self):
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("cols-hoy", r.text)
        self.assertIn('class="hoy-abajo"', r.text)

    def test_every_optional_card_carries_its_own_class(self):
        r = client.get("/")
        self.assertIn('class="card hoy-noticed"', r.text)
        self.assertIn('class="card hoy-comida"', r.text)
        self.assertIn('class="card hoy-nutriente"', r.text)
        self.assertIn('class="card hoy-semana"', r.text)
        self.assertIn('class="grid2 hoy-tiles mb-10"', r.text)
        self.assertIn('class="more hoy-tiles-link"', r.text)

    def test_every_card_rendered_exactly_once(self):
        r = client.get("/")
        for marker in ("hoy-noticed", "hoy-comida", "hoy-nutriente", "hoy-semana", "hoy-tiles-link"):
            self.assertEqual(r.text.count(marker), 1, f"{marker} rendered more than once")
        self.assertEqual(r.text.count('class="hoy-abajo"'), 1)

    def test_tellbody_markup_unchanged(self):
        """The talk row needs no new class -- `.tellbody` is already unique to it -- so
        BODY-MOCK-R2-HOY-1's own exact-string tests on this tag stay valid untouched."""
        r = client.get("/")
        self.assertIn('<a class="tellbody taplink" href="#asis-btn">', r.text)

    def test_phone_order_unchanged_dom_order(self):
        """Ruling 2: below 768px today's order is unchanged -- checked here as the raw DOM/HTML
        order, same marks BoardOrderTest (tests/test_mock_r2_hoy.py) already asserts, now including
        the two extra optional cards this fixture also renders."""
        r = client.get("/")
        marks = ['class="tellbody', "Body lo notó", 'class="grid2', "Ventana de comida",
                 "Metas de nutrientes", "Semana nocturna por delante"]
        positions = [r.text.index(m) for m in marks]
        self.assertEqual(positions, sorted(positions), f"phone DOM order broken: {marks}")


class HoyAbajoBlockCssTest(unittest.TestCase):
    """Ruling 1/4: the tile row's own column counts per breakpoint, the desktop order, and the
    noticed/week pairing -- checked as CSS text, the same way HoyTopBlockCssTest above does."""

    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")
        self.compact = self.css.replace(" ", "").replace("\n", "")

    def test_cols_hoy_rule_removed(self):
        self.assertNotIn(".cols-hoy{", self.compact)

    def test_hoy_abajo_is_a_wrapping_flex_row_from_768(self):
        idx = self.compact.index(".hoy-abajo{display:flex;flex-direction:row;flex-wrap:wrap")
        media = self.compact.rindex("@media(min-width:768px){", 0, idx)
        self.assertGreater(idx, media)

    _BARE_HOY_TILES_COLS = re.compile(
        r"(?<!\])\.hoy-tiles\{grid-template-columns:repeat\((\d+),minmax\(0,1fr\)\)\}")

    def test_tiles_three_across_scoped_below_1280(self):
        """BODY-HOY-ESCRITORIO-2-D: the 3-across rule must carry its own upper bound -- find the
        @media header immediately wrapping the declaration and require a max-width under 1280px
        in it, so the rule cannot outlive tablet/narrow-desktop width (the measured defect this
        slice fixes: six across at 1024px, with the >=1024px left rail, left each tile only
        ~110px, overflowing the steps tile's own figure). The negative lookbehind excludes the
        Lectura compound `[data-ancho="lectura"] .hoy-tiles` rule, which is a separate,
        higher-specificity selector, not this one."""
        m = next(mm for mm in self._BARE_HOY_TILES_COLS.finditer(self.compact) if mm.group(1) == "3")
        media_start = self.compact.rindex("@media(", 0, m.start())
        header_end = self.compact.index("){", media_start)
        header = self.compact[media_start:header_end]
        self.assertIn("max-width:1279.98px", header)

    def test_tiles_six_across_from_1280(self):
        idx = self.compact.index(".hoy-tiles{grid-template-columns:repeat(6,minmax(0,1fr))}")
        media = self.compact.rindex("@media(min-width:1280px){", 0, idx)
        self.assertGreater(idx, media)

    def test_tiles_six_across_is_the_effective_rule_from_1280(self):
        """The EFFECTIVE rule per range, not only that both declarations exist: the 6-across
        declaration must be the LAST unbounded (no max-width) bare `.hoy-tiles` column-count
        declaration in source order -- an equal-specificity rule later in the file with no upper
        bound would win at >=1280px by source order alone (the defect class BODY-HOY-ESCRITORIO-1-C
        fixed for `.hoy-figura .cuerpo-vista-track`). The Lectura compound selector is excluded
        (see above) -- it is a separate, more specific rule, not a competing bare declaration."""
        unbounded = []
        for m in self._BARE_HOY_TILES_COLS.finditer(self.compact):
            media_start = self.compact.rindex("@media(", 0, m.start())
            header_end = self.compact.index("){", media_start)
            header = self.compact[media_start:header_end]
            if "max-width" not in header:
                unbounded.append(m.group(1))
        self.assertEqual(unbounded, ["6"], unbounded)

    def test_tiles_three_across_under_lectura_even_past_1280(self):
        self.assertIn(
            '[data-ancho="lectura"].hoy-tiles{grid-template-columns:repeat(3,minmax(0,1fr))}',
            self.compact)

    def test_desktop_order_tiles_then_noticed_week_then_comida_then_talk(self):
        self.assertIn(".hoy-abajo>.hoy-tiles,.hoy-abajo>.hoy-tiles-link{order:1}", self.compact)
        self.assertIn(".hoy-abajo>.hoy-noticed,.hoy-abajo>.hoy-semana{order:2}", self.compact)
        self.assertIn(".hoy-abajo>.hoy-comida{order:3}", self.compact)
        self.assertIn(".hoy-abajo>.hoy-nutriente{order:4}", self.compact)
        self.assertIn(".hoy-abajo>.tellbody{order:5}", self.compact)

    def test_noticed_and_week_share_the_same_two_column_row(self):
        """A lone noticed or week card still spans the row -- each is a 280px-basis, flex-grow:1
        item in a wrapping flex row, so with no sibling sharing its `order` value it is the only
        item on its line and grows to fill it; no separate "solo" rule is needed."""
        self.assertIn(".hoy-abajo>.hoy-noticed,.hoy-abajo>.hoy-semana{flex:11280px}", self.compact)

    def test_full_width_rows_use_100_percent_basis(self):
        self.assertIn(
            ".hoy-abajo>.hoy-tiles,.hoy-abajo>.hoy-tiles-link,.hoy-abajo>.hoy-comida,"
            ".hoy-abajo>.hoy-nutriente,.hoy-abajo>.tellbody{flex:11100%}", self.compact)

    def test_lectura_wrap_cap_still_fits_two_280px_columns(self):
        """Ruling 4: Lectura's own 820px wrap cap is untouched -- two 280px-basis flex-grow items
        plus the 16px gap (576px) comfortably fit inside it, so noticed/week keep two columns with
        no extra CSS needed under `[data-ancho="lectura"]` for that pair."""
        self.assertIn('[data-ancho="lectura"].wrap{max-width:820px', self.compact)


class ReadinessAgreesAcrossTodayDiaStoredTest(unittest.TestCase):
    """BODY-REVISION-SYNC-READINESS-1 ruling 2: for one invented day, Today's own score, Día's
    own score and the stored value are all equal -- in hosted mode too, where the bug actually
    lived (a hosted pull never stored readiness at all, BODY-GARMIN-SYNC-CONSISTENTE-1's own
    gap -- ruling 1 fixes the storage, this test pins that the two screens read it the same way
    `readiness.update` itself stores it). Invented demo data only, no real health figures."""

    def setUp(self):
        self.conn = _reset_db(self)
        env_patcher = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta-escritorio.test"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _seed_historia(self, pid: int, day: str) -> None:
        start = date.fromisoformat(day) - timedelta(30)
        for i in range(31):
            d = (start + timedelta(i)).isoformat()
            wobble = (-1) ** i  # alternating +/-1 so the baseline's own spread is never zero
            for k, v in (("hrv_last_night", 50 + 2 * wobble), ("resting_hr", 55 + wobble),
                         ("stress_avg", 30 + 3 * wobble), ("sleep_minutes", 450),
                         ("sleep_need_min", 480), ("body_battery_wake", 75)):
                db.set_metric(self.conn, d, k, v, "garmin", pid)
        self.conn.commit()

    def test_today_dia_and_stored_value_agree_in_hosted_mode(self):
        self.assertTrue(cuenta.hosted())
        pid = _mk_person(self.conn)
        day = "2026-11-20"
        self._seed_historia(pid, day)
        readiness.update(self.conn, day, pid)  # the exact call ruling 1 now wires into every
        # hosted pull path -- simulated directly here since this test's own subject is ruling 2
        # (the read side), not ruling 1 (already pinned in tests/test_pull.py).
        stored = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id=? AND day=? AND metric_key='readiness'",
            (pid, day),
        ).fetchone()["value"]
        self.assertIsNotNone(stored)
        _use(pid)

        today_page = client.get("/").text
        self.assertIn(f'class="big">{int(stored)}<', today_page)

        day_page = client.get(f"/day/{day}").text
        start = day_page.index('id="readiness"')
        end = day_page.index("</a>", start)
        tile = day_page[start:end]
        self.assertIn(f'{int(stored)}<span class="u">', tile)


if __name__ == "__main__":
    unittest.main()
