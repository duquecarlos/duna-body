"""BODY-TAP-44-1: four under-44px tap targets (DESIGN rule 11) grow their hit area, not their
drawing -- the `.boxbtn` habit check button (Hoy and a goal's habit list), the `.foot a` footer
links ("Ayuda", "Ajustes"), and Historia's month rows (the new `.month-row` class). Each gets a
borderless, see-through ::before, inset to reach 44x44, pinned so no width/height/padding/margin/
display on the original selector moves. Invented demo data only, no real health figures.
"""
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, identity, web

_STATIC_DIR = Path(__file__).resolve().parent.parent / "body" / "static"
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "body" / "templates"

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


def _block(css: str, selector: str) -> str:
    # anchored at the start of a line, so a selector that is also a comma-list member elsewhere
    # (e.g. ".month-row" inside ".hit::before,...,.month-row::before{...}") is not matched there.
    m = re.search(r"(?:\A|\n)" + re.escape(selector) + r"\{([^}]*)\}", css)
    assert m, f"no rule for {selector!r}"
    return m.group(1)


class HitRuleCssTest(unittest.TestCase):
    """The shared `.hit` pattern and its three applications, read straight from body.css."""

    def setUp(self):
        self.css = (_STATIC_DIR / "body.css").read_text(encoding="utf-8")

    def test_hit_class_is_position_relative_with_a_borderless_before(self):
        self.assertRegex(self.css, r'\.hit,[^{]*\{[^}]*position:relative')
        before = _block(self.css, ".hit::before,.boxbtn::before,.foot a::before,.month-row::before")
        self.assertIn('content:""', before)
        self.assertIn("position:absolute", before)
        self.assertNotIn("background", before)
        self.assertNotIn("border", before)
        self.assertNotIn("pointer-events:none", before)

    def test_boxbtn_before_extends_8px_each_side_only(self):
        before = _block(self.css, ".boxbtn::before")
        self.assertIn("left:-8px", before)
        self.assertIn("right:-8px", before)
        self.assertNotIn("top:-", before)
        self.assertNotIn("bottom:-", before)

    def test_foot_a_before_extends_5px_each_side(self):
        before = _block(self.css, ".foot a::before")
        self.assertIn("left:-5px", before)
        self.assertIn("right:-5px", before)

    def test_month_row_before_extends_3px_top_and_bottom_only(self):
        before = _block(self.css, ".month-row::before")
        self.assertIn("top:-3px", before)
        self.assertIn("bottom:-3px", before)
        self.assertNotIn("left:-", before)
        self.assertNotIn("right:-", before)

    def test_boxbtn_rule_itself_keeps_its_original_box_untouched(self):
        boxbtn = _block(self.css, ".boxbtn")
        self.assertIn("width:28px", boxbtn)
        self.assertIn("min-height:44px", boxbtn)
        self.assertIn("display:inline-flex", boxbtn)
        self.assertIn("padding:0", boxbtn)
        self.assertIn("margin:0", boxbtn)
        self.assertNotIn("position:relative", boxbtn)  # added by the separate .hit,.boxbtn,... rule

    def test_foot_a_rule_itself_keeps_its_original_box_untouched(self):
        foot_a = _block(self.css, ".foot a")
        self.assertIn("min-height:44px", foot_a)
        self.assertIn("display:inline-flex", foot_a)
        self.assertNotIn("width:", foot_a)
        self.assertNotIn("padding", foot_a)
        self.assertNotIn("margin", foot_a)
        self.assertNotIn("position:relative", foot_a)

    def test_box_done_after_is_on_the_inner_span_not_on_boxbtn(self):
        # verifies .boxbtn's own ::before is genuinely free before this slice claims it
        self.assertIn(".box.done::after", self.css)
        self.assertNotIn(".boxbtn::after", self.css)
        self.assertNotIn(".boxbtn::before{", self.css.split(".hit::before")[0])

    def test_rule_is_non_vacuous_three_selectors_extended_plus_the_shared_hit_class(self):
        base = _block(self.css, ".hit,.boxbtn,.foot a,.month-row")
        self.assertEqual(base.strip(), "position:relative")
        inset_rules = [".boxbtn::before", ".foot a::before", ".month-row::before"]
        self.assertEqual(len(inset_rules), 3)
        for selector in inset_rules:
            inset = _block(self.css, selector)
            self.assertTrue(any(axis in inset for axis in ("top:-", "bottom:-", "left:-", "right:-")))


class HitRuleRenderedTest(unittest.TestCase):
    """Each page that owns an under-44px target renders the selector the CSS rule relies on."""

    def setUp(self):
        self.conn = _reset_db(self, "tap44.db")
        _as(self, 1)

    def test_hoy_renders_boxbtn_for_a_due_habit(self):
        today = date(2026, 11, 12)
        gid = goals.propose(self.conn, 1, "Dormir mejor", today.isoformat(), kind="milestone",
                             deadline="2027-01-01")
        goals.activate(self.conn, gid)
        goals.add_habit(self.conn, gid, "Estirar", {"per": "day", "times": 1})
        self.conn.commit()
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="boxbtn"', r.text)

    def test_a_goal_page_renders_boxbtn_for_its_habit_list(self):
        today = date(2026, 11, 12)
        gid = goals.propose(self.conn, 1, "Dormir mejor", today.isoformat(), kind="milestone",
                             deadline="2027-01-01")
        goals.activate(self.conn, gid)
        goals.add_habit(self.conn, gid, "Estirar", {"per": "day", "times": 1})
        self.conn.commit()
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="boxbtn"', r.text)

    def test_history_month_row_renders_month_row_class(self):
        db.set_metric(self.conn, "2026-09-10", "resting_hr", 55.0, "garmin", 1)
        self._insert_activity("act-1", "2026-09-10", "running")
        self.conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.text, r'class="block mt-10 no-underline month-row"')

    def test_footer_links_render_inside_foot(self):
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.text, r'<p class="foot">.*<a href="/ayuda">.*</p>')

    def _insert_activity(self, source_id, day, act_type):
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (?, ?, ?, ?, ?, NULL, 'garmin')",
            (1, source_id, day, f"{day}T07:00:00", act_type))


class MonthRowTemplateSourceTest(unittest.TestCase):
    """history.html's own macro carries the class, since its rows have no selector of their own."""

    def test_month_row_macro_has_the_new_class(self):
        src = (_TEMPLATES_DIR / "history.html").read_text(encoding="utf-8")
        self.assertIn('class="block mt-10 no-underline month-row"', src)


if __name__ == "__main__":
    unittest.main()
