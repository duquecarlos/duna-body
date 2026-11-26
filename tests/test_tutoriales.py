"""BODY-TUTORIALES-EN-1: tutorial titles follow the person's own language (ruling 1), and every
chapter and task id in tutoriales/catalogo.json has both an es.json and an en.json title key
(ruling 2), so a new catalog entry without an English title fails here. Invented demo data only.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, tutoriales
from body.i18n import T

ROOT = Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / "body" / "i18n"
CATALOGO_PATH = ROOT / "tutoriales" / "catalogo.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class CatalogTranslationCoverageTest(unittest.TestCase):
    """Ruling 2, pinned directly from the catalog -- independent of tests/test_i18n_paridad.py's own
    dynamic-prefix census, which guards the same truth from the code's side."""

    @classmethod
    def setUpClass(cls):
        cls.catalogo = _load_json(CATALOGO_PATH)
        cls.es = _load_json(I18N_DIR / "es.json")
        cls.en = _load_json(I18N_DIR / "en.json")

    def test_every_chapter_has_a_title_key_in_both_languages(self):
        for c in self.catalogo["capitulos"]:
            key = "tutoriales.capitulo." + c["id"]
            self.assertIn(key, self.es, f"chapter {c['id']!r} has no Spanish title key")
            self.assertIn(key, self.en, f"chapter {c['id']!r} has no English title key")

    def test_every_task_has_a_title_key_in_both_languages(self):
        for t in self.catalogo["tareas"]:
            key = "tutoriales.tarea." + t["id"]
            self.assertIn(key, self.es, f"task {t['id']!r} has no Spanish title key")
            self.assertIn(key, self.en, f"task {t['id']!r} has no English title key")

    def test_every_task_has_an_answer_key_in_both_languages(self):
        """BODY-REVISION-COPY-GAPS-1 finding 21: a one-line text answer for every task, in place
        of the bare "video coming soon"."""
        for t in self.catalogo["tareas"]:
            key = "tutoriales.respuesta." + t["id"]
            self.assertIn(key, self.es, f"task {t['id']!r} has no Spanish answer key")
            self.assertIn(key, self.en, f"task {t['id']!r} has no English answer key")


class CapitulosConTareasLanguageTest(unittest.TestCase):
    def test_no_t_keeps_the_catalog_titulo(self):
        """Callers that predate this slice and never named a language (none left in this repo, but
        the default must still hold) keep reading the catalog's own (Spanish) titulo."""
        capitulo = next(c for c in tutoriales.capitulos_con_tareas() if c["id"] == "primeros-pasos")
        self.assertEqual(capitulo["titulo"], "Primeros pasos")

    def test_resolves_spanish(self):
        capitulo = next(c for c in tutoriales.capitulos_con_tareas(T("es")) if c["id"] == "primeros-pasos")
        self.assertEqual(capitulo["titulo"], "Primeros pasos")
        tarea = next(x for x in capitulo["tareas"] if x["id"] == "iniciar-sesion-y-aceptar")
        self.assertEqual(tarea["titulo"], "Iniciar sesión y aceptar")

    def test_resolves_english(self):
        capitulo = next(c for c in tutoriales.capitulos_con_tareas(T("en")) if c["id"] == "primeros-pasos")
        self.assertEqual(capitulo["titulo"], "First steps")
        tarea = next(x for x in capitulo["tareas"] if x["id"] == "iniciar-sesion-y-aceptar")
        self.assertEqual(tarea["titulo"], "Sign in and accept")


class TituloFallbackTest(unittest.TestCase):
    """Ruling 1's last rung: a key missing in both languages falls back to the catalog's own
    `titulo`, never the raw key -- body/i18n/__init__.py's own T.__call__ already falls Spanish-ward
    for a key missing only in English; this is the one case it cannot cover on its own."""

    def test_capitulo_falls_back_when_the_key_exists_nowhere(self):
        capitulo = {"id": "no-existe-en-ningun-idioma", "titulo": "Título del catálogo"}
        self.assertEqual(tutoriales._titulo_capitulo(T("en"), capitulo), "Título del catálogo")
        self.assertEqual(tutoriales._titulo_capitulo(T("es"), capitulo), "Título del catálogo")

    def test_tarea_falls_back_when_the_key_exists_nowhere(self):
        tarea = {"id": "no-existe-en-ningun-idioma", "titulo": "Tarea del catálogo"}
        self.assertEqual(tutoriales._titulo_tarea(T("en"), tarea), "Tarea del catálogo")
        self.assertEqual(tutoriales._titulo_tarea(T("es"), tarea), "Tarea del catálogo")


class PrimerosPasosLanguageTest(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def test_no_t_keeps_the_catalog_titulo(self):
        items = {i["id"]: i["titulo"] for i in tutoriales.primeros_pasos(self.conn, 1, False)}
        self.assertEqual(items["iniciar-sesion-y-aceptar"], "Iniciar sesión y aceptar")

    def test_resolves_english(self):
        items = {i["id"]: i["titulo"] for i in tutoriales.primeros_pasos(self.conn, 1, False, T("en"))}
        self.assertEqual(items["iniciar-sesion-y-aceptar"], "Sign in and accept")
        self.assertEqual(items["conectar-garmin"], "Connect your Garmin")


if __name__ == "__main__":
    unittest.main()
