"""BODY-HOY-ESCRITORIO-2-B: a guard against one defect class, found twice in two days
(BODY-HOY-ESCRITORIO-1-C fixed it for `.hoy-figura .cuerpo-vista-track`; this slice's own
spec found it again for `.hoy-tiles`'s column count) -- a later `@media (min-width:...)`
block with a SMALLER min-width than an earlier one, carrying no `max-width` bound, declares
the same property on the same selector as that earlier block. Because the later rule has no
upper bound, it also applies inside the earlier (narrower, larger-min) block's own range, and
at equal specificity the later declaration wins by source order alone -- so the earlier,
more-specific-looking block's declaration never actually takes effect at its own, wider
breakpoint. The fix is always the same: give the later/narrower-range block its own
`max-width` bound (as `.hoy-tiles`'s 3-across rule now has, scoped to
`(min-width:768px) and (max-width:1023.98px)`), or reorder so the wider-min block comes last.

This module parses body/static/body.css with the stdlib only (a small brace-matching reader,
not a real CSS engine) to find every such pair. It does NOT understand full CSS cascade
semantics (specificity beyond exact-selector-text equality, `@supports`, custom media, CSS
nesting, `:is()`/`:where()` expansion, or inherited conditions from an enclosing non-min-width
`@media` such as `(hover:hover)`) -- it only checks what the spec asked for: pairs of
`min-width` media blocks, by their own stated condition, compared by source order and exact
selector text.
"""
import re
import pathlib
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS_PATH = ROOT / "body" / "static" / "body.css"

_MIN_WIDTH_RE = re.compile(r"min-width\s*:\s*([\d.]+)\s*px")
_MAX_WIDTH_RE = re.compile(r"max-width\s*:\s*([\d.]+)\s*px")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _strip_comments_keep_positions(css: str) -> str:
    """Blanks out every `/* ... */` comment, character for character (newlines kept), so every
    later position and line number still matches the ORIGINAL file -- a comment that happens to
    quote a CSS snippet containing `{`/`}` (as this very file's own cuerpo-vista comment does)
    would otherwise desync the brace-matching below."""
    def repl(m: "re.Match[str]") -> str:
        return "".join(ch if ch == "\n" else " " for ch in m.group(0))
    return re.sub(r"/\*.*?\*/", repl, css, flags=re.S)


def _split_top_level_blocks(text: str, base_offset: int = 0):
    """Splits `text` into sibling `<head>{<body>}` blocks by brace depth, at whatever nesting
    level `text` itself sits at. Returns (head, body, head_start, body_start, body_end), every
    position relative to `base_offset` so a caller recursing into a block's own body can pass
    that block's absolute body_start back in and keep every position file-absolute."""
    blocks = []
    i = 0
    n = len(text)
    head_start = 0
    while i < n:
        if text[i] == "{":
            head = text[head_start:i]
            depth = 1
            j = i + 1
            while j < n and depth:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            body_start = i + 1
            body_end = j - 1
            blocks.append((head.strip(), text[body_start:body_end],
                            base_offset + head_start, base_offset + body_start, base_offset + body_end))
            i = j
            head_start = i
        else:
            i += 1
    return blocks


def _normalize_selector(head: str) -> str:
    return re.sub(r"\s+", " ", head).strip()


def _iter_declarations(body: str, body_offset: int, css: str):
    """Yields (property_name, line_number) for each `prop:value` part of a declaration block,
    split on `;` -- line_number points at that declaration's own position in `css`."""
    pos = 0
    for part in body.split(";"):
        part_start = pos
        pos += len(part) + 1
        if ":" not in part:
            continue
        prop = part.split(":", 1)[0].strip()
        if not prop:
            continue
        yield prop, _line_of(css, body_offset + part_start)


def _parse_min_width_blocks(css: str):
    """Returns every `@media` block (at any nesting depth) whose own condition names a
    `min-width`, each as a dict: condition text, its min-width/max-width in px (max_width is
    None when absent), `decls` ({(selector, property): line}, the block's OWN direct
    declarations only -- a nested `@media`'s declarations belong to that nested block, not this
    one), and `start_pos` (for source-order comparisons)."""
    css = _strip_comments_keep_positions(css)
    found = []

    def walk(blocks):
        for head, body, head_start, body_start, body_end in blocks:
            if not head.startswith("@media"):
                continue
            condition = head[len("@media"):].strip()
            inner = _split_top_level_blocks(body, body_start)
            nested_media = [b for b in inner if b[0].startswith("@media")]
            leaf_rules = [b for b in inner if not b[0].startswith("@media")]
            decls = {}
            for sel_head, sel_body, _sel_head_start, sel_body_start, _sel_body_end in leaf_rules:
                selector = _normalize_selector(sel_head)
                for prop, line in _iter_declarations(sel_body, sel_body_start, css):
                    decls.setdefault((selector, prop), line)
            m = _MIN_WIDTH_RE.search(condition)
            if m:
                mx = _MAX_WIDTH_RE.search(condition)
                found.append({
                    "condition": condition,
                    "min_width": float(m.group(1)),
                    "max_width": float(mx.group(1)) if mx else None,
                    "decls": decls,
                    "start_pos": head_start,
                })
            walk(nested_media)

    walk(_split_top_level_blocks(css, 0))
    return found


def find_cascade_inversions(css: str):
    """For every pair of min-width blocks A (larger min) and B (smaller min) where B comes
    after A in the file, and B carries no max-width bound keeping it below A's min (so B's
    range reaches into A's), reports every (selector, property) the two declare in common --
    B wins there by source order alone, so A's declaration for that selector/property never
    takes effect in A's own range. Returns a list of dicts: selector, property, a_condition,
    a_line, b_condition, b_line (the two declarations' own line numbers)."""
    blocks = _parse_min_width_blocks(css)
    hits = []
    for a in blocks:
        for b in blocks:
            if b is a or b["start_pos"] <= a["start_pos"]:
                continue
            if not (a["min_width"] > b["min_width"]):
                continue
            if b["max_width"] is not None and b["max_width"] < a["min_width"]:
                continue
            for key, b_line in b["decls"].items():
                if key in a["decls"]:
                    selector, prop = key
                    hits.append({
                        "selector": selector, "property": prop,
                        "a_condition": a["condition"], "a_line": a["decls"][key],
                        "b_condition": b["condition"], "b_line": b_line,
                    })
    return hits


# Every hit `find_cascade_inversions` reports against the real file that is NOT the defect this
# slice fixed, reviewed one by one (BODY-HOY-ESCRITORIO-2-B):
#
# .grid3 / grid-template-columns -- NOT on Hoy (`.grid3` is used by day.html, history.html and
# trends.html; today.html, Hoy's own template, never uses it). The >=1024px block (line ~71)
# means `.grid3` to flow as `repeat(auto-fill,minmax(150px,1fr))` on desktop, but the plain
# `@media (min-width:640px){.grid3{...}}` rule (line ~109, 3 fixed columns, meant for
# small-tablet width) carries no upper bound and sits later in the file, so it wins at every
# width from 640px up INCLUDING desktop -- the auto-fill desktop rule never actually applies.
# This is the SAME defect class, pre-existing and unrelated to Hoy, so it is marked BUG and
# left to BODY-GRID3-CASCADA-1 (see open_followups) rather than fixed here.
ALLOWLIST = {
    (".grid3", "grid-template-columns"): (
        "BUG, not on Hoy -- tracked as BODY-GRID3-CASCADA-1, out of this slice's scope"),
}


class CascadeInversionGuardTest(unittest.TestCase):
    """Runs the guard on the real body.css and requires every hit to be a reviewed, named
    exception -- an unlisted hit fails the gate, naming the selector, the property and both
    declarations' line numbers, exactly as BODY-HOY-ESCRITORIO-2-B's spec asked."""

    def setUp(self):
        self.css = CSS_PATH.read_text(encoding="utf-8")

    def test_every_cascade_inversion_is_a_named_reviewed_exception(self):
        hits = find_cascade_inversions(self.css)
        unlisted = [h for h in hits if (h["selector"], h["property"]) not in ALLOWLIST]
        self.assertEqual(unlisted, [], "\n".join(
            f"{h['selector']} {{{h['property']}}}: @media{h['a_condition']} (line {h['a_line']}) "
            f"is overridden by the later, narrower-min @media{h['b_condition']} (line {h['b_line']}), "
            "which carries no matching max-width bound" for h in unlisted))

    def test_allowlist_has_no_stale_entries(self):
        """Every allow-listed pair must still be a real, currently-measured hit -- an entry that
        stops reproducing (because the underlying rule was fixed or removed) is dead weight that
        would hide a REAL new regression at the same selector/property if one were ever added."""
        hits = {(h["selector"], h["property"]) for h in find_cascade_inversions(self.css)}
        stale = set(ALLOWLIST) - hits
        self.assertEqual(stale, set(), f"allow-listed but no longer a real hit: {stale}")


class CascadeInversionDetectorUnitTest(unittest.TestCase):
    """Proves the guard itself, on tiny synthetic CSS strings, independent of the real file."""

    def test_detects_a_later_unbounded_smaller_min_width_overriding_an_earlier_larger_one(self):
        css = (
            "@media (min-width:1024px){.tile{grid-template-columns:repeat(6,1fr)}}\n"
            "@media (min-width:768px){.tile{grid-template-columns:repeat(3,1fr)}}\n"
        )
        hits = find_cascade_inversions(css)
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["selector"], ".tile")
        self.assertEqual(hits[0]["property"], "grid-template-columns")
        self.assertEqual(hits[0]["a_line"], 1)
        self.assertEqual(hits[0]["b_line"], 2)

    def test_a_max_width_bound_below_the_earlier_blocks_min_clears_it(self):
        """The exact fix this slice applied: once the smaller-min block is capped below the
        larger-min block's own min-width, the two ranges no longer overlap and there is no
        inversion to report."""
        css = (
            "@media (min-width:1024px){.tile{grid-template-columns:repeat(6,1fr)}}\n"
            "@media (min-width:768px) and (max-width:1023.98px){"
            ".tile{grid-template-columns:repeat(3,1fr)}}\n"
        )
        self.assertEqual(find_cascade_inversions(css), [])

    def test_a_different_property_on_the_same_selector_is_not_a_conflict(self):
        css = (
            "@media (min-width:1024px){.tile{grid-template-columns:repeat(6,1fr)}}\n"
            "@media (min-width:768px){.tile{gap:8px}}\n"
        )
        self.assertEqual(find_cascade_inversions(css), [])

    def test_order_matters_the_narrower_min_must_come_after(self):
        """If the smaller-min, unbounded block comes FIRST and the larger-min block comes after,
        the larger-min block is the later declaration and wins honestly -- no inversion."""
        css = (
            "@media (min-width:768px){.tile{grid-template-columns:repeat(3,1fr)}}\n"
            "@media (min-width:1024px){.tile{grid-template-columns:repeat(6,1fr)}}\n"
        )
        self.assertEqual(find_cascade_inversions(css), [])

    def test_a_comment_quoting_braces_does_not_desync_the_parser(self):
        """The real file's own cuerpo-vista comment quotes a CSS snippet with braces in it
        (BODY-HOY-ESCRITORIO-2-B measured this breaking the naive version of this parser) --
        a comment anywhere must not change what is found after it."""
        css = (
            "/* some note quoting `@media (x){*{y:z}}` for illustration */\n"
            "@media (min-width:1024px){.tile{grid-template-columns:repeat(6,1fr)}}\n"
            "@media (min-width:768px){.tile{grid-template-columns:repeat(3,1fr)}}\n"
        )
        hits = find_cascade_inversions(css)
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["a_line"], 2)
        self.assertEqual(hits[0]["b_line"], 3)


# ---------------------------------------------------------------------------
# BODY-HOY-ESCRITORIO-2-C: a second guard, for the OTHER half of this defect class.
# `find_cascade_inversions` above only catches two media blocks declaring the SAME selector --
# it cannot see the actual BODY-HOY-ESCRITORIO-2-B gap, where TWO DIFFERENT classes (`.grid2`
# and `.hoy-tiles`) sit on ONE element (`<div class="grid2 hoy-tiles mb-10">`,
# body/templates/today.html:139) and collide at equal specificity (one class each). This
# "co-class" guard finds every element in today.html that carries two or more literal classes,
# every property both of a pair declare somewhere in body.css, and which of the two wins -- the
# LAST declaration in file order among the rules that apply at a given pixel width -- at each of
# four sample widths.
# ---------------------------------------------------------------------------
SAMPLE_WIDTHS = (375, 800, 1024, 1568)

_SIMPLE_CLASS_SELECTOR_RE = re.compile(r"^\.[\w-]+$")
_CLASS_ATTR_RE = re.compile(r'class="([^"]*)"')


def _media_width_bounds(condition: str):
    """(min_width, max_width) in px if `condition` is only min-width/max-width terms joined by
    " and ", else None -- a condition this checker cannot evaluate purely by pixel width
    (`(hover:hover)`, `(prefers-reduced-motion: reduce)`, ...). The selectors that sit inside
    such a block on the real file are never bare single-class selectors (they are compound,
    like `.tabs a:hover`), so excluding the whole block loses nothing this guard needs."""
    min_w = max_w = None
    for term in condition.split(" and "):
        term = term.strip()
        m = re.fullmatch(r"\(min-width\s*:\s*([\d.]+)px\)", term)
        if m:
            min_w = float(m.group(1))
            continue
        m = re.fullmatch(r"\(max-width\s*:\s*([\d.]+)px\)", term)
        if m:
            max_w = float(m.group(1))
            continue
        return None
    return (min_w, max_w)


def parse_simple_class_rules(css: str):
    """Every declaration of a bare, single-class selector (`.foo`, never `.foo.bar` or
    `.foo .baz`) -- the only shape where two classes on one element are equal specificity -- at
    the top level or nested only inside pure min/max-width @media conditions. Returns a list of
    dicts: selector, property, min_width, max_width (None = unbounded on that side, both None =
    unconditional), start_pos (source order), line."""
    css = _strip_comments_keep_positions(css)
    results = []

    def walk(blocks, min_w, max_w):
        for head, body, head_start, body_start, body_end in blocks:
            if head.startswith("@media"):
                bounds = _media_width_bounds(head[len("@media"):].strip())
                if bounds is None:
                    continue
                nmin, nmax = bounds
                walk(_split_top_level_blocks(body, body_start),
                     nmin if nmin is not None else min_w,
                     nmax if nmax is not None else max_w)
                continue
            selector = _normalize_selector(head)
            if not _SIMPLE_CLASS_SELECTOR_RE.match(selector):
                continue
            for prop, line in _iter_declarations(body, body_start, css):
                results.append({"selector": selector, "property": prop,
                                 "min_width": min_w, "max_width": max_w,
                                 "start_pos": head_start, "line": line})

    walk(_split_top_level_blocks(css, 0), None, None)
    return results


def _effective_rule(rules_by_key, selector, prop, width):
    """Among every rule for (selector, prop) whose min/max-width bounds include `width`, the one
    that wins the cascade: equal specificity, so the LAST in source order."""
    candidates = [r for r in rules_by_key.get((selector, prop), [])
                  if (r["min_width"] is None or width >= r["min_width"])
                  and (r["max_width"] is None or width <= r["max_width"])]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r["start_pos"])


def multiclass_elements(html: str):
    """Every literal `class="..."` attribute in `html` naming two or more classes, skipping any
    attribute whose value is templated (`{{`/`{%`) -- Jinja can change the class list entirely,
    so a static co-class check cannot say anything about it. Returns a list of
    (line, [classes])."""
    found = []
    for m in _CLASS_ATTR_RE.finditer(html):
        value = m.group(1)
        if "{{" in value or "{%" in value:
            continue
        classes = value.split()
        if len(classes) >= 2:
            found.append((_line_of(html, m.start()), classes))
    return found


def find_coclass_collisions(css: str, html: str, widths=SAMPLE_WIDTHS):
    """For every pair of classes sharing an element in `html`, and every property both declare
    somewhere in `css` (as a bare single-class selector), the winning class at each width in
    `widths`. Returns a dict {(class_a, class_b, property): {width: winning_class_or_None}}, one
    entry per (pair, property) actually declared by both -- a pair that shares no element
    anywhere, or shares an element but no common property, is not reported at all."""
    rules = parse_simple_class_rules(css)
    rules_by_key = {}
    props_by_selector = {}
    for r in rules:
        rules_by_key.setdefault((r["selector"], r["property"]), []).append(r)
        props_by_selector.setdefault(r["selector"], set()).add(r["property"])

    hits = {}
    for _line, classes in multiclass_elements(html):
        selectors = ["." + c for c in classes]
        for i in range(len(selectors)):
            for j in range(i + 1, len(selectors)):
                a, b = selectors[i], selectors[j]
                common = props_by_selector.get(a, set()) & props_by_selector.get(b, set())
                for prop in common:
                    key = (a, b, prop)
                    if key in hits:
                        continue
                    per_width = {}
                    for w in widths:
                        ra = _effective_rule(rules_by_key, a, prop, w)
                        rb = _effective_rule(rules_by_key, b, prop, w)
                        if ra is None and rb is None:
                            per_width[w] = None
                        elif ra is None:
                            per_width[w] = b
                        elif rb is None:
                            per_width[w] = a
                        else:
                            per_width[w] = a if ra["start_pos"] > rb["start_pos"] else b
                    hits[key] = per_width
    return hits


# Every (class_a, class_b, property) hit `find_coclass_collisions` reports against the real
# file, other than `.grid2`/`.hoy-tiles` (the defect this slice fixes, checked against the exact
# expected table below), reviewed one by one -- every one of these is a utility class
# (BODY-ESTILOS-1, body.css:309-320: utilities sit LAST in the file ON PURPOSE, so they always
# beat a component class at equal specificity, by design) or `.tellbody` deliberately overriding
# `.taplink`'s generic inline-flex with its own block layout (body/templates/today.html:113) --
# in both cases the SAME class wins at all four sample widths, so there is no width-dependent
# cascade risk, only an intentional, stable override:
COCLASS_ALLOWLIST = {
    (".approx", ".big", "font-weight"): "utility/component, BODY-ESTILOS-1 -- stable at every width",
    (".row", ".items-end", "align-items"): "utility/component, BODY-ESTILOS-1 -- stable at every width",
    (".row", ".gap-8", "gap"): "utility/component, BODY-ESTILOS-1 -- stable at every width",
    (".row", ".justify-start", "justify-content"): "utility/component, BODY-ESTILOS-1 -- stable at every width",
    (".tellbody", ".taplink", "display"): "intended -- .tellbody's own block layout overrides .taplink",
    (".tellbody", ".taplink", "min-height"): "intended -- .tellbody's own block layout overrides .taplink",
    (".tellbody", ".taplink", "text-decoration"): "intended -- .tellbody's own block layout overrides .taplink",
}

HOY_TILES_EXPECTED_WINNER = {375: ".grid2", 800: ".hoy-tiles", 1024: ".hoy-tiles", 1568: ".hoy-tiles"}


class CoClassCollisionGuardTest(unittest.TestCase):
    """BODY-HOY-ESCRITORIO-2-C: the defect `find_cascade_inversions` cannot see -- two DIFFERENT
    classes on one Hoy element, colliding at equal specificity."""

    def setUp(self):
        self.css = CSS_PATH.read_text(encoding="utf-8")
        self.html = (ROOT / "body" / "templates" / "today.html").read_text(encoding="utf-8")

    def test_grid2_hoy_tiles_winner_matches_the_expected_table(self):
        hits = find_coclass_collisions(self.css, self.html)
        key = (".grid2", ".hoy-tiles", "grid-template-columns")
        self.assertIn(key, hits, "the .grid2/.hoy-tiles column-count collision has disappeared")
        self.assertEqual(hits[key], HOY_TILES_EXPECTED_WINNER)

    def test_every_other_coclass_collision_is_a_named_reviewed_exception(self):
        hits = find_coclass_collisions(self.css, self.html)
        target = (".grid2", ".hoy-tiles", "grid-template-columns")
        unlisted = [k for k in hits if k != target and k not in COCLASS_ALLOWLIST]
        self.assertEqual(unlisted, [], f"unreviewed co-class collision(s) on Hoy: {unlisted}")

    def test_allowlist_has_no_stale_entries(self):
        hits = find_coclass_collisions(self.css, self.html)
        stale = set(COCLASS_ALLOWLIST) - set(hits)
        self.assertEqual(stale, set(), f"allow-listed but no longer a real hit: {stale}")


class CoClassCheckerCatchesTheMeasuredDefectTest(unittest.TestCase):
    """Proves the checker on a MINIMAL synthetic reproduction of the exact shape measured at
    d658b79 (body.css there: the unconditional `.grid2` rule at line 107, and `.hoy-tiles`'
    6-across rule inside the >=1024px block near the TOP of the file, line 78 -- BEFORE .grid2)
    -- the checker must report `.grid2` as the (wrong) winner at 1024/1568px, not `.hoy-tiles`,
    exactly reproducing BODY-HOY-ESCRITORIO-2-B's measured gap."""

    DEFECT_SHAPE_CSS = (
        "@media (min-width:1024px){\n"
        "  .hoy-tiles{grid-template-columns:repeat(6,minmax(0,1fr))}\n"
        "}\n"
        ".grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}\n"
        "@media (min-width:768px) and (max-width:1023.98px){\n"
        "  .hoy-tiles{grid-template-columns:repeat(3,minmax(0,1fr))}\n"
        "}\n"
    )
    DEFECT_SHAPE_HTML = '<div class="grid2 hoy-tiles mb-10"></div>'

    def test_grid2_wins_at_1024_and_1568_before_the_fix(self):
        hits = find_coclass_collisions(self.DEFECT_SHAPE_CSS, self.DEFECT_SHAPE_HTML)
        key = (".grid2", ".hoy-tiles", "grid-template-columns")
        self.assertEqual(hits[key][1024], ".grid2")
        self.assertEqual(hits[key][1568], ".grid2")
        self.assertNotEqual(hits[key], HOY_TILES_EXPECTED_WINNER)

    def test_the_fix_shape_gives_hoy_tiles_the_win(self):
        fixed_css = (
            ".grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}\n"
            "@media (min-width:768px) and (max-width:1023.98px){\n"
            "  .hoy-tiles{grid-template-columns:repeat(3,minmax(0,1fr))}\n"
            "}\n"
            "@media (min-width:1024px){\n"
            "  .hoy-tiles{grid-template-columns:repeat(6,minmax(0,1fr))}\n"
            "}\n"
        )
        hits = find_coclass_collisions(fixed_css, self.DEFECT_SHAPE_HTML)
        key = (".grid2", ".hoy-tiles", "grid-template-columns")
        self.assertEqual(hits[key], HOY_TILES_EXPECTED_WINNER)


if __name__ == "__main__":
    unittest.main()
