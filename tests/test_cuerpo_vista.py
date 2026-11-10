"""BODY-CUERPO-VISTA-1: mock round 2's one body view per main screen -- the copied-in anatomy art
and its manifest (ruling 1), the categoria -> glow-group map and the per-screen view/glow data
(ruling 2), and the component each of Hoy, Tendencias and Turnos renders from it (rulings 3-4).
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import anatomia, config, db, fechas, identity, web

client = TestClient(web.app, follow_redirects=False)

ART_DIR = Path(__file__).resolve().parent.parent / "body" / "static" / "anatomia"


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "cuerpo.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)
    return conn


def _mk_person(conn, lang: str = "es") -> int:
    cur = conn.execute(
        "INSERT INTO person (display_name, timezone, language) VALUES (?, 'America/Bogota', ?)", ("P", lang))
    return cur.lastrowid


def _use(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = (lambda pid=person_id: pid)


class ManifestTest(unittest.TestCase):
    """Ruling 1: every manifest file is in the repo, byte for byte."""

    def test_every_manifest_file_matches_its_sha256(self):
        manifest = json.loads((ART_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 26)
        for row in manifest:
            path = ART_DIR / row["archivo"]
            self.assertTrue(path.exists(), f"missing {row['archivo']}")
            got = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(got, row["sha256"], f"sha256 mismatch for {row['archivo']}")

    def test_anclajes_json_is_in_the_repo(self):
        self.assertTrue((ART_DIR / "anclajes.json").exists())


class AnatomiaDataTest(unittest.TestCase):
    """Ruling 2: anatomia.py's own data, never hand-placed per template."""

    def test_every_view_image_exists_in_both_themes(self):
        for pantalla, vistas in anatomia.VISTAS_POR_PANTALLA.items():
            for v in vistas:
                for tema in ("claro", "oscuro"):
                    path = ART_DIR / f"{v['imagen']}-{tema}.webp"
                    self.assertTrue(path.exists(), f"{pantalla}/{v['id']}: missing {path.name}")

    def test_categoria_grupos_is_data(self):
        self.assertEqual(anatomia.CATEGORIA_GRUPOS["pierna"], frozenset({"cuadriceps", "gluteos", "isquios"}))
        self.assertEqual(anatomia.CATEGORIA_GRUPOS["espalda"],
                          frozenset({"dorsales", "espalda_alta", "deltoides_post"}))
        self.assertEqual(anatomia.CATEGORIA_GRUPOS["pecho"], frozenset({"pecho"}))
        self.assertEqual(anatomia.CATEGORIA_GRUPOS["hombro"], frozenset({"hombros"}))
        self.assertEqual(anatomia.CATEGORIA_GRUPOS["brazo"], frozenset({"biceps"}))
        self.assertEqual(anatomia.CATEGORIA_GRUPOS["core"], frozenset({"abdomen"}))

    def test_grupos_de_categorias_unions_and_normalises_case(self):
        got = anatomia.grupos_de_categorias(["PECHO", "hombro"])
        self.assertEqual(got, frozenset({"pecho", "hombros"}))

    def test_unknown_category_lights_nothing(self):
        self.assertEqual(anatomia.grupos_de_categorias(["day off", None, ""]), frozenset())

    def test_grupos_ordenados_is_stable(self):
        got = anatomia.grupos_ordenados(frozenset({"isquios", "pecho", "gluteos"}))
        self.assertEqual(got, ["pecho", "gluteos", "isquios"])

    def test_pierna_lights_front_quads_and_back_glutes_hamstrings(self):
        grupos = anatomia.CATEGORIA_GRUPOS["pierna"]
        ctx = anatomia.contexto(_T(), "hoy", grupos)
        musculos = next(v for v in ctx if v["id"] == "musculos")
        espalda = next(v for v in ctx if v["id"] == "espalda")
        self.assertEqual(len(musculos["glow"]), 2)  # cuadriceps: two ellipses
        self.assertEqual(len(espalda["glow"]), 2 + 1)  # gluteos (1) + isquios (2)
        corazon = next(v for v in ctx if v["id"] == "corazon")
        self.assertEqual(corazon["glow"], [])

    def test_unrecognised_category_lights_no_view(self):
        ctx = anatomia.contexto(_T(), "hoy", frozenset())
        for v in ctx:
            self.assertEqual(v["glow"], [])


def _T():
    from body.i18n import T
    return T("es")


class CuerpoVistaHoyTest(unittest.TestCase):
    """Rulings 3-4: Hoy's own component -- views, aria-pressed, glow follows a planned session."""

    def setUp(self):
        self.conn = _reset_db(self)

    def _plan_hoy(self, pid: int, ejercicios: list) -> None:
        person = db.person(self.conn, pid)
        today = fechas.hoy_local(person).isoformat()
        self.conn.execute(
            "INSERT INTO plan (person_id, day, title, status, ejercicios_json) VALUES (?, ?, 'Upper B', "
            "'accepted', ?)", (pid, today, json.dumps(ejercicios, ensure_ascii=False)))
        self.conn.commit()

    def test_no_plan_shows_no_session_caption_and_no_glow(self):
        pid = _mk_person(self.conn)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Hoy no hay sesión planeada.", r.text)

    def test_planned_session_lights_its_categories(self):
        pid = _mk_person(self.conn)
        self._plan_hoy(pid, [{"ejercicio": "remo con barra", "series": 4, "categoria": "espalda"},
                              {"ejercicio": "press militar", "series": 3, "categoria": "hombro"}])
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Iluminado: lo que entrena tu sesión de hoy", r.text)
        self.assertIn("Hombros", r.text)
        self.assertIn("Dorsales", r.text)
        self.assertNotIn("Hoy no hay sesión planeada.", r.text)

    def test_planned_session_with_no_recognised_category_reads_as_no_session(self):
        pid = _mk_person(self.conn)
        self._plan_hoy(pid, [{"ejercicio": "algo raro", "series": 4, "categoria": "misterio"}])
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Hoy no hay sesión planeada.", r.text)

    def test_view_buttons_are_aria_pressed_and_first_is_on(self):
        pid = _mk_person(self.conn)
        _use(pid)
        r = client.get("/")
        self.assertIn('aria-pressed="true"', r.text)
        self.assertIn("Músculos", r.text)
        self.assertIn("Espalda", r.text)
        self.assertIn("Corazón", r.text)

    def test_english_shows_english_strings(self):
        pid = _mk_person(self.conn, lang="en")
        _use(pid)
        r = client.get("/")
        self.assertIn("Muscles", r.text)
        self.assertIn("No session planned today.", r.text)

    def test_swipe_to_turn_label_present(self):
        pid = _mk_person(self.conn)
        _use(pid)
        r = client.get("/")
        self.assertIn("Desliza para girar", r.text)


class CuerpoVistaTendenciasTest(unittest.TestCase):
    """Ruling 4: Tendencias always lights the heart, on a day with enough history for a card."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_heart_always_lit_regardless_of_data(self):
        pid = _mk_person(self.conn)
        today = fechas.hoy_local(db.person(self.conn, pid))
        for i in range(20):
            d = (today - __import__("datetime").timedelta(i)).isoformat()
            db.set_metric(self.conn, d, "resting_hr", 55.0, "garmin", pid)
        self.conn.commit()
        _use(pid)
        r = client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tendencias gira el cuerpo hacia el corazón y los pulmones", r.text)
        self.assertIn("Corazón", r.text)
        self.assertIn("Pulmones", r.text)
        self.assertIn("Cuerpo entero", r.text)


class CuerpoVistaTurnosTest(unittest.TestCase):
    """Ruling 4: Turnos always lights the brain, whenever the shifts screen itself is reachable."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_brain_always_lit(self):
        pid = _mk_person(self.conn)
        db.set_trabaja_turnos(self.conn, True, pid)
        self.conn.commit()
        _use(pid)
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Turnos gira el cuerpo hacia el cerebro y los nervios", r.text)
        self.assertIn("Cerebro y sueño", r.text)
        self.assertIn("Nervios", r.text)


if __name__ == "__main__":
    unittest.main()
