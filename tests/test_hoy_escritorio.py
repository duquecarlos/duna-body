"""BODY-HOY-ESCRITORIO-1: Hoy's own top block from tablet width up (rulings 1-2 -- the score hero
and "El día por delante" beside the body view, never the figure alone filling the screen) and the
trained/still-to-come glow split (ruling 3 -- solid for what today's logged training actually
worked, dashed for whatever today's plan still owes)."""
import json
import pathlib
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, fechas, identity, web

client = TestClient(web.app, follow_redirects=False)
ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = ROOT / "body" / "static" / "body.css"


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

    def test_hoy_top_grid_uses_the_named_properties_not_magic_numbers(self):
        self.assertIn(
            ".hoy-top{display:grid;grid-template-columns:minmax(0,1fr)"
            "minmax(var(--hoy-figura-min),var(--hoy-figura-max))", self.compact)

    def test_grid_sits_inside_the_768_query(self):
        idx = self.compact.index(".hoy-top{display:grid")
        media = self.compact.rindex("@media(min-width:768px){", 0, idx)
        self.assertGreater(idx, media)

    def test_image_is_never_cropped_on_hoy(self):
        self.assertIn(".hoy-figura.cuerpo-vista-img{object-fit:contain}", self.compact)

    def test_phone_and_desktop_height_caps_present(self):
        self.assertIn(".hoy-figura.cuerpo-vista-track{max-height:60vh;max-height:60svh}", self.compact)
        self.assertIn(".hoy-figura.cuerpo-vista-track{max-height:min(78vh,720px)}", self.compact)

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


if __name__ == "__main__":
    unittest.main()
