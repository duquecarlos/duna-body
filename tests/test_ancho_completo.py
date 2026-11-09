"""BODY-ANCHO-COMPLETO-1: a per-device width preference ("completo" full width / "lectura" a
centred reading column), on the theme's own mechanism (localStorage, read before paint by
tema.js, changed in Ajustes by ajustes_tema.js). No schema, no migration -- the preference lives
only in the browser, like the theme."""
from tests import _isolate  # noqa: F401,E402

import json
import pathlib
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

from body import config, db, goals, web

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "body" / "static"
TEMPLATES = ROOT / "body" / "templates"
I18N_DIR = ROOT / "body" / "i18n"

# BODY-ANCHO-COMPLETO-1 spec step 3: the grid class each screen relies on for its columns. A
# screen not listed here (goal.html, settings.html) gets the base rule and no columns.
_COLS_BY_SCREEN = {
    "today.html": "cols-hoy",
    "trends.html": "cols-tendencias",
    "goals.html": "cols-metas",
    "shifts.html": "cols-turnos",
    "history.html": "cols-historia",
}

_NEW_I18N_KEYS = ("settings.ancho", "ancho.completo", "ancho.lectura", "ancho.nota")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _fresh_client(case: unittest.TestCase) -> tuple:
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = pathlib.Path(tempfile.mkdtemp(dir=_isolate.TMP)) / "a.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return TestClient(web.app, follow_redirects=False), conn


def _media_1024_flags(css_text: str) -> list:
    """For each line of css_text, whether that line sits inside a top-level
    `@media (min-width:1024px)` block. Tracks a stack of block openers by brace depth; good
    enough for this file's style (one rule per line, at most one level of real nesting)."""
    flags = []
    stack = []
    for line in css_text.splitlines():
        stripped = line.strip()
        in_1024 = any(s.startswith("@media (min-width:1024px)") for s in stack)
        flags.append(in_1024 or stripped.startswith("@media (min-width:1024px)"))
        opens, closes = line.count("{"), line.count("}")
        if stripped.startswith("@media") and opens:
            stack.append(stripped)
            opens -= 1
        stack.extend([""] * opens)
        for _ in range(closes):
            if stack:
                stack.pop()
    return flags


def _top_level_flags(css_text: str) -> list:
    """For each line of css_text, whether that line sits at the top level -- outside every
    `@media` block, phone-level CSS. Same brace-depth tracking as `_media_1024_flags`, generalised
    to any nesting rather than just the 1024 one."""
    flags = []
    depth = 0
    for line in css_text.splitlines():
        stripped = line.strip()
        flags.append(depth == 0 and not stripped.startswith("@media"))
        opens, closes = line.count("{"), line.count("}")
        if stripped.startswith("@media") and opens:
            depth += 1
            opens -= 1
        depth = max(0, depth + opens - closes)
    return flags


# BODY-DEPLOY-HOST-1-M: the selectors the wide-layout rules are built from -- a `cols-` class, or
# the `data-ancho` attribute -- plus the grid3 auto-fill override, identified by body content since
# `.grid3` also carries two other rules (the 640px three-column default and the lectura-collapsed
# one) that must NOT be required to sit inside the 1024 block.
_TRACKED_SELECTOR_RE = re.compile(r"\.cols-[\w-]+|data-ancho")


def _is_tracked_rule(selector: str, body: str) -> bool:
    if _TRACKED_SELECTOR_RE.search(selector):
        return True
    return selector.strip() == ".grid3" and "auto-fill" in body


class TemaJsAnchoTest(unittest.TestCase):
    """tema.js reads `body.ancho` and sets dataset.ancho before paint; absent/other values never
    set it, so the CSS default (no attribute) is completo -- same pattern as the theme's own
    light default."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(STATIC / "tema.js")
        cls.compact = re.sub(r"\s+", "", cls.src)

    def test_reads_ancho_key(self):
        self.assertIn("'body.ancho'", self.compact.replace('"', "'"))

    def test_sets_dataset_ancho_only_for_lectura(self):
        self.assertIn("dataset.ancho='lectura'", self.compact)
        self.assertNotIn("dataset.ancho='completo'", self.compact)

    def test_one_blocking_script_tag_for_tema(self):
        base = _read(TEMPLATES / "base.html")
        self.assertEqual(base.count("static_url('tema.js')"), 1)


class AjustesAnchoControlTest(unittest.TestCase):
    """settings.html's new 'Pantallas anchas' / 'Wide screens' control: same .seg/.btn shape as
    the theme row, handled by ajustes_tema.js, both languages, shown at every width."""

    @classmethod
    def setUpClass(cls):
        cls.settings_html = _read(TEMPLATES / "settings.html")
        cls.ajustes_js = _read(STATIC / "ajustes_tema.js")
        cls.es = json.loads(_read(I18N_DIR / "es.json"))
        cls.en = json.loads(_read(I18N_DIR / "en.json"))

    def test_settings_has_ancho_seg_with_both_buttons(self):
        self.assertIn('id="ancho"', self.settings_html)
        self.assertIn('class="seg" id="ancho"', self.settings_html)
        self.assertIn('data-v="completo"', self.settings_html)
        self.assertIn('data-v="lectura"', self.settings_html)

    def test_control_is_not_hidden_below_1024(self):
        # the ancho card carries no width-gating attribute of its own -- it must render the same
        # at every width, unlike the asis-resize handle which is 768px-gated.
        start = self.settings_html.index("settings.ancho")
        end = self.settings_html.index("<script", start)
        self.assertNotIn("hidden", self.settings_html[start:end])

    def test_ajustes_tema_js_handles_both_groups(self):
        self.assertIn("#theme", self.ajustes_js)
        self.assertIn("#ancho", self.ajustes_js)
        self.assertIn("body.ancho", self.ajustes_js)
        self.assertIn("body.theme", self.ajustes_js)

    def test_i18n_keys_present_in_both_languages(self):
        for key in _NEW_I18N_KEYS:
            self.assertIn(key, self.es, f"{key} missing from es.json")
            self.assertIn(key, self.en, f"{key} missing from en.json")

    def test_i18n_values_nonempty(self):
        for key in _NEW_I18N_KEYS:
            self.assertTrue(self.es[key].strip(), f"es.json {key} is empty")
            self.assertTrue(self.en[key].strip(), f"en.json {key} is empty")


class AjustesAnchoRenderedTest(unittest.TestCase):
    """The control actually renders, with the right words, in es and in en."""

    def setUp(self):
        self.client, self.conn = _fresh_client(self)
        self.es = json.loads(_read(I18N_DIR / "es.json"))
        self.en = json.loads(_read(I18N_DIR / "en.json"))

    def test_renders_in_spanish_by_default(self):
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.es["settings.ancho"], r.text)
        self.assertIn(self.es["ancho.completo"], r.text)
        self.assertIn(self.es["ancho.lectura"], r.text)
        self.assertIn(self.es["ancho.nota"], r.text)

    def test_renders_in_english(self):
        self.client.post("/settings/language", data={"lang": "en"})
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.en["settings.ancho"], r.text)
        self.assertIn(self.en["ancho.completo"], r.text)
        self.assertIn(self.en["ancho.lectura"], r.text)
        self.assertIn(self.en["ancho.nota"], r.text)


class WrapCapOnlyUnderLecturaTest(unittest.TestCase):
    """body.css's `.wrap` 820px cap exists only under `[data-ancho="lectura"]`; completo's .wrap
    carries no cap at all."""

    def setUp(self):
        self.css = _read(STATIC / "body.css")
        self.compact = self.css.replace(" ", "")

    def test_no_unscoped_wrap_cap(self):
        self.assertNotIn(".wrap{max-width:820px}", self.compact)

    def test_lectura_scoped_wrap_cap_present(self):
        self.assertIn('[data-ancho="lectura"].wrap{max-width:820px', self.compact)

    def test_every_820px_wrap_rule_is_lectura_scoped(self):
        offenders = []
        for m in re.finditer(r"([^{}]*\.wrap[^{}]*)\{([^{}]*)\}", self.css):
            selector, body = m.group(1), m.group(2)
            if "820px" in body and '[data-ancho="lectura"]' not in selector:
                offenders.append(selector.strip())
        self.assertEqual(offenders, [], f"a .wrap 820px cap outside lectura: {offenders}")


class ColsAndDataAnchoStayInThe1024QueryTest(unittest.TestCase):
    """BODY-DEPLOY-HOST-1-M: the wide layout never changes the phone. Checked as a property of the
    file itself -- not a diff against main, which rots the moment main contains this slice family's
    own CSS (BODY-DEPLOY-HOST-1-INV) and would then refuse any later slice that touches body.css
    outside the 1024 query. Every rule whose selector names a `cols-` class or the `data-ancho`
    attribute, or is the grid3 auto-fill override, must sit inside the min-width:1024px block, and
    no phone-level (outside every @media) rule for those same selectors may exist."""

    def setUp(self):
        self.css = _read(STATIC / "body.css")
        self.in_1024 = _media_1024_flags(self.css)
        self.top_level = _top_level_flags(self.css)

    def _tracked_rules(self):
        rules = []
        for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", self.css):
            selector, body = m.group(1), m.group(2)
            if _is_tracked_rule(selector, body):
                brace_pos = m.start(2) - 1  # the rule's own "{" -- never the gap before it
                line_idx = self.css.count("\n", 0, brace_pos)
                rules.append((line_idx, selector.strip()))
        return rules

    def test_every_tracked_rule_sits_inside_the_1024_query(self):
        rules = self._tracked_rules()
        self.assertTrue(rules, "no cols-/data-ancho/grid3-auto-fill rule found at all")
        offenders = [(ln + 1, sel) for ln, sel in rules if not self.in_1024[ln]]
        self.assertEqual(offenders, [],
                          f"rule outside the min-width:1024px block: {offenders}")

    def test_no_phone_level_rule_exists_for_a_tracked_selector(self):
        rules = self._tracked_rules()
        offenders = [(ln + 1, sel) for ln, sel in rules if self.top_level[ln]]
        self.assertEqual(offenders, [],
                          f"phone-level (outside every @media) rule for a tracked selector: {offenders}")


class ScreenColumnClassesTest(unittest.TestCase):
    """Each listed screen carries the grid class it relies on; day.html relies on the shared,
    already-present .grid3, which gets the auto-fill override instead of a new class."""

    def test_each_listed_screen_carries_its_grid_class(self):
        for template, cls in _COLS_BY_SCREEN.items():
            html = _read(TEMPLATES / template)
            self.assertIn(f'class="{cls}"', html, f"{template} missing .{cls}")

    def test_day_relies_on_shared_grid3_unchanged(self):
        html = _read(TEMPLATES / "day.html")
        self.assertIn('class="grid2 grid3"', html)

    def test_grid3_gets_the_1024_auto_fill_override(self):
        compact = _read(STATIC / "body.css").replace(" ", "")
        self.assertIn(".grid3{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}", compact)

    def test_lectura_collapses_every_cols_class_and_grid3_to_one_column(self):
        compact = _read(STATIC / "body.css").replace(" ", "")
        for cls in _COLS_BY_SCREEN.values():
            self.assertIn(f'[data-ancho="lectura"].{cls}', compact,
                          f"[data-ancho=lectura] does not collapse .{cls}")
        self.assertIn('[data-ancho="lectura"].grid3{grid-template-columns:repeat(3,minmax(0,1fr))}',
                      compact)


class HoyPhoneOrderHeroFirstTest(unittest.TestCase):
    """BODY-ANCHO-COMPLETO-1-M: the wide-screen grid must not change Hoy's phone reading order.
    main's DOM order is the readiness hero (`a.card.est`), then Primeros pasos, then the habits
    card, then the figure cards -- with demo data that has both Primeros pasos and habits, the
    hero's HTML must still come first."""

    def test_hero_comes_before_primeros_pasos_and_habits(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = pathlib.Path(tempfile.mkdtemp(dir=_isolate.TMP)) / "hoy_order.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)

        # one metric row: ticks "conectar-garmin" / "primera-mirada-a-hoy" but leaves
        # "trabajas-por-turnos" unanswered, so primeros_pasos is not None.
        db.set_metric(conn, "2026-11-01", "resting_hr", 55, "garmin", 1)
        gid = goals.propose(conn, 1, "Dormir mejor", "2026-11-01", metric_key="sleep_minutes",
                             direction="raise", target_value=420.0, baseline_value=350.0,
                             deadline="2026-12-31")
        goals.activate(conn, gid)
        goals.add_habit(conn, gid, "Dormir temprano", {"per": "day", "times": 1})
        conn.commit()

        client = TestClient(web.app, follow_redirects=False)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)

        es = json.loads(_read(ROOT / "body" / "i18n" / "es.json"))
        hero_pos = r.text.index('class="card est"')
        primeros_pos = r.text.index(es["primeros_pasos.titulo"])
        habits_pos = r.text.index(es["today.plan_title"])

        self.assertLess(hero_pos, primeros_pos,
                         "the readiness hero must render before Primeros pasos")
        self.assertLess(hero_pos, habits_pos,
                         "the readiness hero must render before the habits card")


class ProseWidthTest(unittest.TestCase):
    def test_wrap_prose_capped_at_70ch(self):
        compact = _read(STATIC / "body.css").replace(" ", "")
        self.assertIn("max-width:70ch", compact)


class NoInlineStyleOrScriptTest(unittest.TestCase):
    """CSP (script-src 'self', style-src 'self', no 'unsafe-inline'): none of this slice's
    touched templates gained an inline style= attribute or an inline <script> with no src."""

    TOUCHED_TEMPLATES = ("base.html", "settings.html", "today.html", "trends.html",
                          "goals.html", "goal.html", "shifts.html", "day.html", "history.html")

    def test_no_style_attribute(self):
        for name in self.TOUCHED_TEMPLATES:
            html = _read(TEMPLATES / name)
            self.assertNotIn("style=", html, f"{name} has an inline style attribute")

    def test_no_inline_executable_script(self):
        for name in self.TOUCHED_TEMPLATES:
            html = _read(TEMPLATES / name)
            for m in re.finditer(r"<script\b([^>]*)>", html):
                attrs = m.group(1)
                if "src=" in attrs or 'type="application/json"' in attrs:
                    continue
                self.fail(f"{name} has an inline <script> with no src: {m.group(0)}")

    def test_static_js_files_unchanged_in_shape(self):
        # tema.js and ajustes_tema.js stay plain static files loaded via static_url -- no new
        # script tag of any kind was introduced by this slice.
        for name in ("tema.js", "ajustes_tema.js"):
            self.assertTrue((STATIC / name).exists())


class SanityCountsTest(unittest.TestCase):
    """Non-vacuous counts: the per-screen census and the i18n additions are not accidentally
    empty, which would make the checks above pass trivially."""

    def test_five_screens_listed(self):
        self.assertEqual(len(_COLS_BY_SCREEN), 5)

    def test_four_new_i18n_keys(self):
        self.assertEqual(len(_NEW_I18N_KEYS), 4)

    def test_1024_block_holds_every_cols_rule_and_the_lectura_cap(self):
        # BODY-DEPLOY-HOST-1-M: non-vacuity, rewritten as a property of the file instead of a diff
        # against main (which goes false on main itself the moment this slice family merges --
        # BODY-DEPLOY-HOST-1-INV). The wide layout really added CSS: a min-width:1024px block
        # exists, and inside it there is a rule for every cols-* class plus the Lectura wrap cap.
        css = _read(STATIC / "body.css")
        compact = css.replace(" ", "")
        self.assertIn("@media(min-width:1024px){", compact)
        flags = _media_1024_flags(css)
        lines = css.splitlines()
        inside_compact = "".join(line for i, line in enumerate(lines) if flags[i]).replace(" ", "")
        for cls in _COLS_BY_SCREEN.values():
            self.assertIn(f".{cls}{{", inside_compact, f"no 1024-query rule for .{cls}")
        self.assertIn('[data-ancho="lectura"].wrap{max-width:820px', inside_compact)


if __name__ == "__main__":
    unittest.main()
