"""BODY-CUERPO-NITIDEZ-1: the 24 over-compressed anatomy images (1080x1935, 36-83 KB) replaced by
sharp 800w/1440w pairs re-encoded from the original 1536x2752 source art (same framing, so every
glow position in body/anatomia.py stays valid), plus the responsive img markup (srcset+sizes) and
theme-switch wiring that serves them -- except vias-aereas, whose source crop was never found and
which keeps its single-file src, chosen by body/anatomia.py's own "nitida" field, never a filename
test in the template or JS.
"""
import json
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import anatomia
from body.rutas import comun

ROOT = Path(__file__).resolve().parent.parent
ART_DIR = ROOT / "body" / "static" / "anatomia"
JS_PATH = ROOT / "body" / "static" / "cuerpo-vista.js"
TEMPLATE_PATH = ROOT / "body" / "templates" / "_cuerpo_vista.html"


def _T():
    from body.i18n import T
    return T("es")


class FilesOnDiskMatchTheManifestTest(unittest.TestCase):
    """manifest.json (updated by this slice) is the one declared catalogue of anatomy files -- no
    file on disk outside it (an old single-width leftover, a stray copy) and nothing it names is
    missing."""

    def setUp(self):
        self.manifest = json.loads((ART_DIR / "manifest.json").read_text(encoding="utf-8"))

    def test_disk_has_exactly_the_manifests_files_no_more_no_less(self):
        declarados = {row["archivo"] for row in self.manifest}
        en_disco = {p.name for p in ART_DIR.glob("*.webp")}
        self.assertEqual(en_disco, declarados)

    def test_every_nitida_basename_has_both_themes_and_both_widths(self):
        ids = sorted({row["id"] for row in self.manifest})
        for basename in ids:
            if basename in anatomia._SIN_SRCSET:
                continue
            for tema in ("claro", "oscuro"):
                for ancho in ("800", "1440"):
                    nombre = f"{basename}-{tema}-{ancho}.webp"
                    self.assertTrue((ART_DIR / nombre).exists(), f"missing {nombre}")

    def test_vias_aereas_keeps_its_single_file_per_theme(self):
        for tema in ("claro", "oscuro"):
            nombre = f"vias-aereas-{tema}.webp"
            self.assertTrue((ART_DIR / nombre).exists(), f"missing {nombre}")
            self.assertFalse((ART_DIR / f"vias-aereas-{tema}-800.webp").exists())
            self.assertFalse((ART_DIR / f"vias-aereas-{tema}-1440.webp").exists())


class EveryScreenViewHasItsFilesTest(unittest.TestCase):
    """Ruling 4: every view Hoy, Tendencias or Turnos actually renders has its own image files on
    disk -- both widths for a nitida view, the single file otherwise."""

    def test_every_view_image_exists(self):
        for pantalla, vistas in anatomia.VISTAS_POR_PANTALLA.items():
            for v in vistas:
                nitida = v["imagen"] not in anatomia._SIN_SRCSET
                for tema in ("claro", "oscuro"):
                    if nitida:
                        for ancho in ("800", "1440"):
                            path = ART_DIR / f"{v['imagen']}-{tema}-{ancho}.webp"
                            self.assertTrue(path.exists(), f"{pantalla}/{v['id']}: missing {path.name}")
                    else:
                        path = ART_DIR / f"{v['imagen']}-{tema}.webp"
                        self.assertTrue(path.exists(), f"{pantalla}/{v['id']}: missing {path.name}")


class ContextoNitidaFieldTest(unittest.TestCase):
    """Ruling 2: the per-view choice lives in anatomia.py's own view data, one field."""

    def test_every_hoy_view_is_nitida(self):
        # None of Hoy/Tendencias/Turnos' own views renders vias-aereas today.
        for pantalla in ("hoy", "tendencias", "turnos"):
            ctx = anatomia.contexto(_T(), pantalla)
            for v in ctx:
                self.assertTrue(v["nitida"], f"{pantalla}/{v['id']} should be nitida")

    def test_vias_aereas_is_not_nitida(self):
        self.assertIn("vias-aereas", anatomia._SIN_SRCSET)


def _render_cuerpo_vista(vistas: list[dict], **kw) -> str:
    """Renders the macro standalone (not through a full screen route) -- the only way to exercise
    the vias-aereas branch, since no current screen wires that view in."""
    tmpl = comun.env.from_string(
        "{% from '_cuerpo_vista.html' import cuerpo_vista %}{{ cuerpo_vista(t, cuerpo, **kw) }}")
    return tmpl.render(t=_T(), cuerpo={"recorte": "cuerpo", "vistas": vistas, "caption": "",
                                       "caption_limitada": None}, kw=kw)


def _vista(imagen: str, nitida: bool) -> dict:
    return {"id": imagen, "imagen": imagen, "glow": [], "limitada": [], "pendiente": [],
            "nitida": nitida, "label": imagen, "alt": imagen}


class ResponsiveMarkupTest(unittest.TestCase):
    """Ruling 2: a nitida view's <img> carries srcset+sizes for both widths; a non-nitida one
    (vias-aereas) keeps the single-file src, same as before this slice."""

    def test_nitida_view_carries_srcset_and_sizes(self):
        html = _render_cuerpo_vista([_vista("musculos", True)])
        self.assertIn('data-claro-srcset="/static/anatomia/musculos-claro-800.webp', html)
        self.assertIn("800w", html)
        self.assertIn('/static/anatomia/musculos-claro-1440.webp', html)
        self.assertIn("1440w", html)
        self.assertIn('data-oscuro-srcset="/static/anatomia/musculos-oscuro-800.webp', html)
        self.assertIn('data-claro="/static/anatomia/musculos-claro-800.webp', html)
        self.assertIn("sizes=", html)

    def test_non_nitida_view_keeps_the_single_file_src(self):
        html = _render_cuerpo_vista([_vista("vias-aereas", False)])
        self.assertIn('data-claro="/static/anatomia/vias-aereas-claro.webp', html)
        self.assertIn('data-oscuro="/static/anatomia/vias-aereas-oscuro.webp', html)
        self.assertNotIn("vias-aereas-claro-800", html)
        self.assertNotIn("data-claro-srcset", html)
        self.assertNotIn("sizes=", html)

    def test_default_sizes_matches_the_full_width_card_layout(self):
        html = _render_cuerpo_vista([_vista("corazon", True)])
        self.assertIn('sizes="(min-width: 768px) 700px, calc(100vw - 64px)"', html)

    def test_hoy_figura_passes_its_own_narrower_sizes(self):
        html = _render_cuerpo_vista([_vista("musculos", True)], sizes="(min-width: 768px) 400px, 80vw")
        self.assertIn('sizes="(min-width: 768px) 400px, 80vw"', html)


class HoyRouteRendersSrcsetTest(unittest.TestCase):
    """The real Hoy template include (today.html) passes its own narrower `sizes`."""

    def test_today_html_passes_the_hoy_figura_sizes(self):
        text = (ROOT / "body" / "templates" / "today.html").read_text(encoding="utf-8")
        self.assertIn("sizes='(min-width: 768px) 400px, 80vw'", text)


class JsSetsSrcsetForTheThemeTest(unittest.TestCase):
    """The theme switch keeps working: cuerpo-vista.js sets `srcset` right where it sets `src`."""

    def test_js_sets_srcset_from_the_theme_dataset(self):
        text = JS_PATH.read_text(encoding="utf-8")
        self.assertIn("img.dataset[tema()+'Srcset']", text)
        self.assertIn("img.srcset=srcset", text)


class TemplateHasNoFilenameTestTest(unittest.TestCase):
    """Ruling 2: the per-view choice is anatomia.py's own field (`v.nitida`), never a filename
    test -- neither the template nor the JS branches on an image's own name."""

    def test_template_does_not_test_the_image_filename(self):
        text = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("vias-aereas", text)

    def test_js_code_does_not_test_the_image_filename(self):
        # The JS's own leading /* ... */ comment names vias-aereas as documentation (which view
        # it is a no-op for) -- the guard is on the executable code, so it drops that block first.
        text = JS_PATH.read_text(encoding="utf-8")
        codigo = text.split("*/", 1)[1]
        self.assertNotIn("vias-aereas", codigo)


if __name__ == "__main__":
    unittest.main()
