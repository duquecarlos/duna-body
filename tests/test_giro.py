"""BODY-CUERPO-SPIN-HOY-1: body/giro.py's own data and pure functions -- the mask encoding
(GIRO_REGIONES), the muscle-state blob built from body.musculo's own result, the region/state/
layer labels (one literal t() call each) and the per-frame aria-valuetext array. No request, no
database -- body/rutas/hoy.py is the one caller, tested separately in tests/test_avatar3d.py.
"""
import pathlib
import re
import tempfile
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import anatomia, config, db, fechas, giro, identity, web
from body.i18n import T

ROOT = pathlib.Path(__file__).resolve().parent.parent

ROOT_STATIC_GIRO = None

client = TestClient(web.app, follow_redirects=False)


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


class HiddenGlobalRuleTest(unittest.TestCase):
    """Ruling 1: body.css carries one global rule so the browser's own `[hidden]{display:none}`
    -- a low-specificity UA rule any author `display` beats regardless of source order -- always
    wins. Without it the spin's own loading overlay (`.giro-cargando{display:grid}`) and the
    muscle legend (`.giro-leyenda{display:flex}`) never clear once `hidden` is set."""

    def test_body_css_carries_the_global_hidden_rule(self):
        css = (ROOT / "body" / "static" / "body.css").read_text(encoding="utf-8")
        self.assertIn("[hidden]{display:none !important}", css)


class SpinMarkupHiddenTest(unittest.TestCase):
    """Ruling 1/4: the loading overlay and the muscle-only legend/caption hide through the native
    `hidden` attribute alone -- never an inline `style="display:..."` that the global rule would
    then have to fight (or that would silently win over it without `!important`)."""

    def test_no_inline_display_style_on_spin_elements(self):
        html = (ROOT / "body" / "templates" / "_cuerpo_vista.html").read_text(encoding="utf-8")
        for clase in ("giro-cargando", "giro-leyenda", "giro-objetivo-caption", "cuerpo3d-detalle",
                      "cuerpo3d-track"):
            for m in re.finditer(r'<[a-z]+\b[^>]*\bclass="[^"]*\b' + re.escape(clase) + r'\b[^"]*"[^>]*>', html):
                self.assertNotIn("style=", m.group(0), f".{clase}'s own tag carries an inline style")

    def test_giro_js_toggles_the_hidden_attribute_not_an_inline_style(self):
        js = (ROOT / "body" / "static" / "giro.js").read_text(encoding="utf-8")
        for nombre in ("cargando", "leyenda", "objetivoCaption", "detalle"):
            self.assertIn(nombre + ".hidden", js, f"giro.js never toggles {nombre}.hidden")
            self.assertNotIn(nombre + ".style.display", js,
                              f"giro.js sets an inline display style on {nombre} instead of .hidden")

    def test_legend_and_objetivo_caption_classes_exist_in_the_markup(self):
        html = (ROOT / "body" / "templates" / "_cuerpo_vista.html").read_text(encoding="utf-8")
        self.assertIn('class="giro-leyenda"', html)
        self.assertIn("giro-objetivo-caption", html)


class TwoDSelectorHookClassTest(unittest.TestCase):
    """BODY-CUERPO-SPIN-HOY-PULIDO-1 ruling 1/2: the 2D view selector row (`.cuerpo-vista-btn`
    buttons + `.cuerpo-vista-swipe`) carries an extra hook class, `cuerpo2d-btns`, so the switch
    can hide it while 3D is on -- exactly one such row, outside `.cuerpo3d-track`, never inside it
    (moving it there would feed cuerpo-vista.js's swipe handling on the wrong track)."""

    def setUp(self):
        self.html = (ROOT / "body" / "templates" / "_cuerpo_vista.html").read_text(encoding="utf-8")

    def test_exactly_one_cuerpo2d_btns_row(self):
        self.assertEqual(self.html.count("cuerpo2d-btns"), 1)

    def test_the_row_still_carries_its_own_class_and_the_vista_buttons(self):
        m = re.search(r'<div class="([^"]*\bcuerpo2d-btns\b[^"]*)">', self.html)
        self.assertIsNotNone(m, "no div carries the cuerpo2d-btns hook class")
        clases = m.group(1).split()
        self.assertIn("cuerpo-vista-btns", clases)
        resto = self.html[m.end():]
        cierre = resto.index("</div>")
        bloque = resto[:cierre]
        self.assertIn("cuerpo-vista-btn", bloque)
        self.assertIn("cuerpo-vista-swipe", bloque)

    def test_the_row_is_not_inside_the_3d_track(self):
        inicio_3d = self.html.index('class="cuerpo3d-track"')
        fin_3d = self.html.index("{% endif %}", inicio_3d)
        bloque_3d = self.html[inicio_3d:fin_3d]
        self.assertNotIn("cuerpo2d-btns", bloque_3d)

    def test_giro_interruptor_js_hides_the_row_with_the_same_mode_test_as_the_tracks(self):
        js = (ROOT / "body" / "static" / "giro-interruptor.js").read_text(encoding="utf-8")
        self.assertIn('querySelector(".cuerpo2d-btns")', js)
        self.assertIn("track2d.hidden = modo !== \"2d\";", js)
        self.assertIn("track3d.hidden = modo !== \"3d\";", js)
        self.assertIn("btns2d.hidden = modo !== \"2d\";", js)


class RenderedHoyCardTwoDSelectorTest(unittest.TestCase):
    """Ruling 4: the actual Hoy card render -- 2D is the server-side default, so the row carries
    no `hidden` there; the switch hides it client-side only once giro-interruptor.js runs."""

    def setUp(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = pathlib.Path(tempfile.mkdtemp(dir=TMP)) / "giro_render.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.ensure_ready(self.conn)

    def _hoy_person(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO person (display_name, timezone, language) VALUES (?, 'America/Bogota', 'es')", ("P",))
        self.conn.commit()
        pid = cur.lastrowid
        hoy = fechas.hoy_local(db.person(self.conn, pid)).isoformat()
        db.set_metric(self.conn, hoy, "resting_hr", 55.0, "garmin", pid)
        self.conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = (lambda pid=pid: pid)
        return pid

    def test_exactly_one_unhidden_row_holding_the_vista_buttons(self):
        self._hoy_person()
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text.count("cuerpo2d-btns"), 1)
        m = re.search(r'<div class="cuerpo-vista-btns cuerpo2d-btns">(.*?)</div>', r.text, re.S)
        self.assertIsNotNone(m, "no unhidden cuerpo2d-btns row in the render")
        self.assertIn("cuerpo-vista-btn", m.group(1))
        self.assertIn("cuerpo-vista-swipe", m.group(1))


class ValueTextAnguloTest(unittest.TestCase):
    """Ruling 3: a new literal key, `{vista}, girado {grados}°` / `{vista}, turned {grados}°`,
    both placeholders in both languages -- the raw, unformatted string giro.js fills in at every
    angle between the four named views (`t(...)` with no kwargs returns it unformatted, body/i18n/
    __init__.py's own T.__call__)."""

    def test_key_exists_with_both_placeholders_in_both_languages(self):
        for lang in ("es", "en"):
            t = _T(lang)
            valor = t("cuerpo.giro.vista.angulo")
            self.assertNotEqual(valor, "cuerpo.giro.vista.angulo", f"{lang}: key missing")
            self.assertIn("{vista}", valor)
            self.assertIn("{grados}", valor)

    def test_template_passes_the_key_literally(self):
        html = (ROOT / "body" / "templates" / "_cuerpo_vista.html").read_text(encoding="utf-8")
        self.assertIn("t('cuerpo.giro.vista.angulo')", html)

    def test_giro_js_reads_and_formats_the_template(self):
        js = (ROOT / "body" / "static" / "giro.js").read_text(encoding="utf-8")
        self.assertIn("cuerpo3d-angulo-texto", js)
        self.assertIn('replace("{vista}"', js)
        self.assertIn('replace("{grados}"', js)


class TinteYLienzoOscuroTest(unittest.TestCase):
    """BODY-CUERPO-SPIN-TINTE-OSCURO-1 ruling 1/2: the tint multiplies in both themes (no dark-theme
    `screen` override left in body.css), and the spin's own stage colour (`--giro-lienzo`) is
    declared with the mock's two paper values and used by `.cuerpo3d-track`, never `.cuerpo-vista-
    track` (the 2D viewer keeps `var(--wash)`, its art has its own light/dark variants)."""

    def setUp(self):
        self.css = (ROOT / "body" / "static" / "body.css").read_text(encoding="utf-8")

    def test_giro_tinte_has_no_blend_other_than_multiply(self):
        reglas = re.findall(r'([^{}]*\.giro-tinte[^{}]*)\{([^}]*)\}', self.css)
        self.assertTrue(reglas, "no rule targets .giro-tinte")
        con_blend = [cuerpo for _, cuerpo in reglas if "mix-blend-mode" in cuerpo]
        self.assertTrue(con_blend, "no rule sets .giro-tinte's mix-blend-mode")
        for cuerpo in con_blend:
            self.assertIn("mix-blend-mode:multiply", cuerpo)

    def test_no_theme_scoped_rule_overrides_giro_tinte(self):
        self.assertNotRegex(self.css, r'data-theme="dark"\]\s*\.giro-tinte')

    def test_stage_variable_declared_with_both_paper_values(self):
        m_light = re.search(r':root\{[^}]*--giro-lienzo:\s*(#[0-9A-Fa-f]{6})', self.css)
        m_dark = re.search(r':root\[data-theme="dark"\]\{[^}]*--giro-lienzo:\s*(#[0-9A-Fa-f]{6})', self.css)
        self.assertIsNotNone(m_light, "--giro-lienzo not declared in the light :root block")
        self.assertIsNotNone(m_dark, "--giro-lienzo not declared in the dark :root[data-theme=dark] block")
        self.assertEqual(m_light.group(1).upper(), "#FBF3EA")
        self.assertEqual(m_dark.group(1).upper(), "#ECE4D9")
        self.assertNotEqual(m_light.group(1).upper(), m_dark.group(1).upper())

    def test_cuerpo3d_track_uses_the_stage_variable(self):
        m = re.search(r'(?<![,\w-])\.cuerpo3d-track\{([^}]*)\}', self.css)
        self.assertIsNotNone(m, "no standalone .cuerpo3d-track rule found")
        self.assertIn("var(--giro-lienzo)", m.group(1))

    def test_cuerpo_vista_track_still_uses_wash(self):
        m = re.search(r'(?<![,\w-])\.cuerpo-vista-track\{([^}]*)\}', self.css)
        self.assertIsNotNone(m, "no standalone .cuerpo-vista-track rule found")
        self.assertIn("var(--wash)", m.group(1))
        self.assertNotIn("--giro-lienzo", m.group(1))


if __name__ == "__main__":
    unittest.main()
