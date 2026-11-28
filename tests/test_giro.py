"""BODY-CUERPO-SPIN-HOY-1: body/giro.py's own data and pure functions -- the mask encoding
(GIRO_REGIONES), the muscle-state blob built from body.musculo's own result, the region/state/
layer labels (one literal t() call each) and the per-frame aria-valuetext array. No request, no
database -- body/rutas/hoy.py is the one caller, tested separately in tests/test_avatar3d.py.
"""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import anatomia, giro
from body.i18n import T

ROOT_STATIC_GIRO = None


def _T(lang="es"):
    return T(lang)


class GiroRegionesTest(unittest.TestCase):
    """Ruling 5: GIRO_REGIONES is the mock's own REGION order, index 0 kept as None."""

    def test_has_twenty_entries_index_zero_is_none(self):
        self.assertEqual(len(giro.GIRO_REGIONES), 20)
        self.assertIsNone(giro.GIRO_REGIONES[0])

    def test_nineteen_named_regions_no_duplicates(self):
        nombres = [r for r in giro.GIRO_REGIONES if r is not None]
        self.assertEqual(len(nombres), 19)
        self.assertEqual(len(set(nombres)), 19)

    def test_matches_the_mocks_own_order(self):
        self.assertEqual(giro.GIRO_REGIONES, (
            None, "pecho", "hombros", "deltoides_post", "biceps", "triceps", "antebrazos", "abdomen",
            "oblicuos", "trapecio", "cuello", "espalda_alta", "dorsales", "lumbares", "gluteos",
            "cuadriceps", "aductores", "isquios", "gemelos", "tibiales",
        ))

    def test_ten_of_the_nineteen_are_modelled_by_musculo(self):
        modeladas = frozenset(r["clave"] for r in anatomia.REGIONES if r["tipo"] == "musculo")
        self.assertEqual(len(modeladas), 10)
        en_giro = frozenset(r for r in giro.GIRO_REGIONES if r is not None)
        self.assertTrue(modeladas.issubset(en_giro))
        no_modeladas = en_giro - modeladas
        self.assertEqual(len(no_modeladas), 9)


class ConstruirEstadosTest(unittest.TestCase):
    """Ruling 5: every tracked region reads musculo's own word; every other one reads
    "no_medido" -- never "sin_datos" (that would claim Body measured it and found nothing)."""

    def test_every_tracked_region_reads_the_models_word(self):
        modelo = {"pecho": "en_rango", "hombros": "subentrenado", "biceps": "sobrecargado",
                  "cuadriceps": "recuperando", "abdomen": "sin_datos", "dorsales": "en_rango",
                  "espalda_alta": "en_rango", "deltoides_post": "en_rango", "gluteos": "en_rango",
                  "isquios": "en_rango"}
        estados = giro.construir_estados(modelo)
        for clave, esperado in modelo.items():
            self.assertEqual(estados[clave], esperado)

    def test_every_untracked_region_reads_no_medido(self):
        estados = giro.construir_estados({})
        for clave in ("triceps", "antebrazos", "oblicuos", "trapecio", "cuello", "lumbares",
                      "aductores", "gemelos", "tibiales"):
            self.assertEqual(estados[clave], "no_medido")

    def test_result_carries_every_named_region_once(self):
        estados = giro.construir_estados({})
        esperadas = frozenset(r for r in giro.GIRO_REGIONES if r is not None)
        self.assertEqual(frozenset(estados), esperadas)

    def test_never_empty(self):
        self.assertTrue(giro.construir_estados({}))


class ColoresTest(unittest.TestCase):
    """Ruling 5: the mock's own rgb values; sin_datos and no_medido both untinted."""

    def test_four_tinted_states_two_untinted(self):
        self.assertIsNone(giro.COLORES["sin_datos"])
        self.assertIsNone(giro.COLORES["no_medido"])
        for estado in ("en_rango", "recuperando", "subentrenado", "sobrecargado"):
            self.assertIsInstance(giro.COLORES[estado], tuple)
            self.assertEqual(len(giro.COLORES[estado]), 3)


class EtiquetasTest(unittest.TestCase):
    """Ruling 7: region labels reuse `cuerpo.grupo.*` for the 10 modelled muscles, and this
    slice's own `cuerpo.giro.region.*` for the other 9 -- in both languages."""

    def test_every_region_has_a_label_in_both_languages(self):
        for lang in ("es", "en"):
            etiquetas = giro.etiquetas_regiones(_T(lang))
            esperadas = frozenset(r for r in giro.GIRO_REGIONES if r is not None)
            self.assertEqual(frozenset(etiquetas), esperadas)
            for clave, texto in etiquetas.items():
                self.assertTrue(texto.strip(), clave)

    def test_every_state_has_a_label_in_both_languages(self):
        for lang in ("es", "en"):
            textos = giro.etiquetas_estado(_T(lang))
            self.assertEqual(set(textos), {"en_rango", "recuperando", "subentrenado", "sobrecargado",
                                            "sin_datos", "no_medido"})
            for estado, texto in textos.items():
                self.assertTrue(texto.strip(), estado)

    def test_every_layer_has_a_label_in_both_languages(self):
        for lang in ("es", "en"):
            textos = giro.etiquetas_capas(_T(lang))
            self.assertEqual(set(textos), set(giro.CAPAS))
            for capa, texto in textos.items():
                self.assertTrue(texto.strip(), capa)


class VistasTextoTest(unittest.TestCase):
    """Ruling 4/7: one aria-valuetext per of the 16 frames, each one of the three view words --
    never one of the mock's own 16 invented English sentences (not approved)."""

    def test_sixteen_entries(self):
        self.assertEqual(len(giro.vistas_texto(_T())), giro.N_VISTAS)

    def test_every_entry_is_one_of_the_three_view_words(self):
        t = _T()
        palabras = {t("cuerpo.giro.vista.frente"), t("cuerpo.giro.vista.lado"), t("cuerpo.giro.vista.espalda")}
        for texto in giro.vistas_texto(t):
            self.assertIn(texto, palabras)

    def test_the_three_jump_indices_name_their_own_word(self):
        t = _T()
        textos = giro.vistas_texto(t)
        self.assertEqual(textos[0], t("cuerpo.giro.vista.frente"))
        self.assertEqual(textos[4], t("cuerpo.giro.vista.lado"))
        self.assertEqual(textos[8], t("cuerpo.giro.vista.espalda"))

    def test_both_languages_differ(self):
        self.assertNotEqual(giro.vistas_texto(_T("es")), giro.vistas_texto(_T("en")))


class NombresArchivoTest(unittest.TestCase):
    def test_nombre_cuadro_shape(self):
        self.assertEqual(giro.nombre_cuadro("giro", 0), "giro-00.webp")
        self.assertEqual(giro.nombre_cuadro("piel", 15), "piel-15.webp")

    def test_nombre_mascara_shape(self):
        self.assertEqual(giro.nombre_mascara(0), "giro-regiones-00.png")
        self.assertEqual(giro.nombre_mascara(15), "giro-regiones-15.png")


class ArteEnElRepoTest(unittest.TestCase):
    """Ruling 2: every frame and mask body/giro.py's own data can name exists under
    body/static/giro/ -- 96 frames (6 layers x 16) + 16 masks = 112 files."""

    def test_every_frame_and_mask_file_exists(self):
        import pathlib
        base = pathlib.Path(__file__).resolve().parent.parent / "body" / "static" / "giro"
        faltan = []
        for capa in giro.CAPAS:
            for i in range(giro.N_VISTAS):
                if not (base / giro.nombre_cuadro(capa, i)).is_file():
                    faltan.append(giro.nombre_cuadro(capa, i))
        for i in range(giro.N_VISTAS):
            if not (base / giro.nombre_mascara(i)).is_file():
                faltan.append(giro.nombre_mascara(i))
        self.assertEqual(faltan, [])

    def test_exactly_112_files_no_extras(self):
        import pathlib
        base = pathlib.Path(__file__).resolve().parent.parent / "body" / "static" / "giro"
        archivos = sorted(p.name for p in base.iterdir() if p.is_file())
        self.assertEqual(len(archivos), 112)


if __name__ == "__main__":
    unittest.main()
