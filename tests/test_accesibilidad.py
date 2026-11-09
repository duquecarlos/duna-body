"""BODY-ACCESIBILIDAD-1: a structural accessibility sweep over every HTML page this app renders.

DESIGN rule 9 gives charts their own accessible name and tap readout; rule 11 sets phone-first tap
targets. Nothing before this slice swept every page for the structural basics a screen reader or a
keyboard-only visitor needs: one `<h1>`, `<html lang>` matching the person's language, no duplicate
`id`, every link/button named, every form control labelled, every `<img>`/`<svg>` named or hidden,
and no positive `tabindex`. This reuses the existing route sweeps' own fixtures (tests/_rutas.py's
rutas_planas, tests/test_estados_vacios.py's sparse-state seeds, tests/test_cuenta_rp.py's hosted
session mechanism) instead of inventing a second way to render a page.

Parsing is done with the standard library's html.parser (no new dependency): `_TreeBuilder` turns
each response body into a small tree of `_Node`s (tag, attrs, parent, children -- children mixing
str text chunks and child `_Node`s in document order), which is all eight rules below need. Void
elements (img, input, br, ...) are never pushed onto the open-tag stack, since they take no end
tag; everything else is popped on its matching end tag, leniently (a stray unmatched end tag is a
no-op) since a hand-written-enough-but-not-XML template is still expected to nest correctly.

Scope, per the spec: the populated (demo) fixture plus sparse states A, D and E (not every state
test_estados_vacios.py seeds -- these three alone already reach every template this slice's
`touches:` lists), each in es and en; local mode; plus hosted mode for the three hosted-only pages
that can ever answer 200 (`/garmin`, `/ajustes/dispositivos`, `/privacidad/consentimiento`), reached
through the same fake-issuer session tests/test_cuenta_rp.py's CuentaRpTestCase and
tests/test_estados_vacios_hosted.py already build. A candidate path is only run through the eight
rules when it actually answers 200 with an HTML body -- a 404 or a redirect is not a page anyone
reads, and every one of those statuses is already covered by the existing sweeps.

Rules (f) and (h) are structurally vacuous in this codebase today: no template anywhere renders a
bare `<img>` tag (every picture-like thing is an inline, server-built `<svg>`) and no template sets
`tabindex` at all. The check functions below still run and would catch a violation the moment either
ever appears; the non-vacuous assertion for those two rules asserts the measured zero instead of a
fabricated nonzero, rather than planting a dead `<img>` purely to make a counter move. See this
slice's ledger entry for the measured count behind that call."""
from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, identity, privacidad, web
from tests._rutas import rutas_planas
from tests.test_cuenta_rp import CuentaRpTestCase
from tests.test_estados_vacios import STATES, _paths_for

_default_client = TestClient(web.app, follow_redirects=False)

# Per the spec: not every sparse state test_estados_vacios.py seeds -- A (brand new), D (turnos on,
# no shift yet) and E (an active goal with no data) alone reach every template this slice touches.
_LOCAL_STATES = {k: STATES[k] for k in ("A", "D", "E")}
_LANGS = ("es", "en")

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}


class _Node:
    __slots__ = ("tag", "attrs", "parent", "children")

    def __init__(self, tag: str, attrs, parent: "_Node | None"):
        self.tag = tag
        self.attrs = {k: (v if v is not None else "") for k, v in attrs}
        self.parent = parent
        self.children: list = []  # str (text) or _Node, in document order


class _TreeBuilder(HTMLParser):
    """Builds a `_Node` tree from one HTML response. Namespace-blind on purpose: an inline `<svg>`'s
    children (`<rect>`, `<title>`, a hit-area `<a href>`) parse exactly like HTML elements, which is
    all the eight rules below need -- none of them cares that SVG is a different namespace."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", [], None)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_Node(tag, attrs, self._stack[-1]))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def _iter_all(node: _Node):
    for c in node.children:
        if isinstance(c, _Node):
            yield c
            yield from _iter_all(c)


def _text_of(node: _Node) -> str:
    parts = []
    for c in node.children:
        if isinstance(c, str):
            parts.append(c)
        else:
            parts.append(_text_of(c))
    return "".join(parts)


def _element_children(node: _Node) -> list:
    return [c for c in node.children if isinstance(c, _Node)]


def _excerpt(node: _Node, maxlen: int = 140) -> str:
    attrs = " ".join(f'{k}="{v}"' for k, v in node.attrs.items())
    s = f"<{node.tag}" + (f" {attrs}" if attrs else "") + ">"
    return s if len(s) <= maxlen else s[:maxlen] + "…"


def _name_ok(node: _Node, id_set: set) -> bool:
    """Rule (d): non-whitespace text content, aria-label, aria-labelledby pointing at an id that
    exists, or title; an svg/img child alone counts only if THAT child carries its own name."""
    if _text_of(node).strip():
        return True
    if (node.attrs.get("aria-label") or "").strip():
        return True
    labelledby = node.attrs.get("aria-labelledby")
    if labelledby and labelledby in id_set:
        return True
    if (node.attrs.get("title") or "").strip():
        return True
    kids = _element_children(node)
    if len(kids) == 1 and kids[0].tag in ("svg", "img"):
        child = kids[0]
        if child.tag == "img":
            return bool((child.attrs.get("alt") or "").strip() or (child.attrs.get("aria-label") or "").strip())
        return bool((child.attrs.get("aria-label") or "").strip())
    return False


def _is_form_control(node: _Node) -> bool:
    if node.tag in ("select", "textarea"):
        return True
    if node.tag == "input":
        return (node.attrs.get("type") or "text").lower() not in ("hidden", "submit", "button")
    return False


def _label_ok(node: _Node, id_set: set, label_for: set) -> bool:
    """Rule (e): a `<label for>` matching this control's id, a wrapping `<label>` ancestor, or
    aria-label/aria-labelledby."""
    el_id = node.attrs.get("id")
    if el_id and el_id in label_for:
        return True
    if (node.attrs.get("aria-label") or "").strip():
        return True
    labelledby = node.attrs.get("aria-labelledby")
    if labelledby and labelledby in id_set:
        return True
    anc = node.parent
    while anc is not None:
        if anc.tag == "label":
            return True
        anc = anc.parent
    return False


def _svg_ok(node: _Node, id_set: set) -> bool:
    """Rule (g): role="img" plus an accessible name, or aria-hidden="true"."""
    if (node.attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if (node.attrs.get("role") or "").strip() == "img":
        if (node.attrs.get("aria-label") or "").strip():
            return True
        labelledby = node.attrs.get("aria-labelledby")
        if labelledby and labelledby in id_set:
            return True
    return False


class _Sweep:
    """Accumulates violations and per-rule evaluated-element counts across every page this test
    renders, then fails once with every offender listed (page x state x lang x rule x excerpt)."""

    def __init__(self):
        self.violations: list[tuple[str, str, str, str, str]] = []
        self.counts = Counter()
        self.pages_checked = 0

    def check(self, *, page: str, state: str, lang: str, client: TestClient | None = None) -> None:
        resp = (client or _default_client).get(page)
        if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
            return
        self.pages_checked += 1
        tree = _TreeBuilder()
        tree.feed(resp.text)
        nodes = list(_iter_all(tree.root))

        id_counts = Counter(n.attrs["id"] for n in nodes if n.attrs.get("id"))
        id_set = set(id_counts)
        label_for = {n.attrs["for"] for n in nodes if n.tag == "label" and n.attrs.get("for")}

        def record(rule: str, excerpt: str) -> None:
            self.violations.append((page, state, lang, rule, excerpt))

        # (a) <html lang> matches the person's language
        html_nodes = [n for n in nodes if n.tag == "html"]
        self.counts["a_html_lang"] += len(html_nodes)
        for n in html_nodes:
            got = (n.attrs.get("lang") or "").strip()
            if got != lang:
                record("a_html_lang", f"<html lang={got!r}> expected {lang!r}")

        # (b) exactly one <h1>
        h1_nodes = [n for n in nodes if n.tag == "h1"]
        self.counts["b_single_h1"] += len(h1_nodes)
        if len(h1_nodes) != 1:
            record("b_single_h1", f"found {len(h1_nodes)} <h1> element(s)")

        # (c) no duplicate id
        self.counts["c_duplicate_id"] += len(id_counts)
        for dup_id, n in id_counts.items():
            if n > 1:
                record("c_duplicate_id", f'id="{dup_id}" appears {n} times')

        # (d) every <a href> and <button> has an accessible name
        link_button_nodes = [n for n in nodes if n.tag == "button" or (n.tag == "a" and "href" in n.attrs)]
        self.counts["d_link_button_name"] += len(link_button_nodes)
        for n in link_button_nodes:
            if not _name_ok(n, id_set):
                record("d_link_button_name", _excerpt(n))

        # (e) every input (except hidden/submit/button)/select/textarea is labelled
        form_nodes = [n for n in nodes if _is_form_control(n)]
        self.counts["e_form_label"] += len(form_nodes)
        for n in form_nodes:
            if not _label_ok(n, id_set, label_for):
                record("e_form_label", _excerpt(n))

        # (f) every <img> has alt (empty allowed) -- structurally vacuous today, see module docstring
        img_nodes = [n for n in nodes if n.tag == "img"]
        self.counts["f_img_alt"] += len(img_nodes)
        for n in img_nodes:
            if "alt" not in n.attrs:
                record("f_img_alt", _excerpt(n))

        # (g) every inline <svg> is named or hidden
        svg_nodes = [n for n in nodes if n.tag == "svg"]
        self.counts["g_svg_name"] += len(svg_nodes)
        for n in svg_nodes:
            if not _svg_ok(n, id_set):
                record("g_svg_name", _excerpt(n))

        # (h) no positive tabindex -- structurally vacuous today, see module docstring
        tabindex_nodes = [n for n in nodes if "tabindex" in n.attrs]
        self.counts["h_positive_tabindex"] += len(tabindex_nodes)
        for n in tabindex_nodes:
            raw = (n.attrs.get("tabindex") or "").strip()
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0:
                record("h_positive_tabindex", _excerpt(n))

    def fail_message(self) -> str:
        lines = [f"  {page} state={state} lang={lang} rule={rule}: {excerpt}"
                 for page, state, lang, rule, excerpt in self.violations]
        return f"{len(self.violations)} accessibility violation(s):\n" + "\n".join(lines)


def _reset_db(tc: unittest.TestCase, name: str):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)
    return conn


def _seed_populated(conn, idioma: str) -> dict:
    demo._seed(conn, dias=30, idioma=idioma)
    goal_id = conn.execute("SELECT id FROM goal WHERE person_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
    day = conn.execute(
        "SELECT day FROM daily_metric WHERE person_id = 1 ORDER BY day DESC LIMIT 1").fetchone()[0]
    conn.commit()
    return {"days": [day], "goal_ids": [goal_id]}


class AccesibilidadLocalTest(unittest.TestCase):
    """Local mode: the populated fixture plus sparse states A, D and E, each in es and en."""

    def test_sweep_populated_and_sparse_states(self):
        sweep = _Sweep()
        get_routes = [r for r in rutas_planas(web.app.routes) if "GET" in getattr(r, "methods", ())]
        self.assertGreater(len(get_routes), 20)

        # ---- populated (demo) fixture, es and en -- each in its own fresh database -------------
        for lang in _LANGS:
            conn = _reset_db(self, f"populada-{lang}.db")
            ids = _seed_populated(conn, lang)
            for route in get_routes:
                for path in _paths_for(route.path, ids):
                    sweep.check(page=path, state="populada", lang=lang)

        # ---- sparse states A, D, E, es and en ----------------------------------------------------
        for state_key, seed_fn in _LOCAL_STATES.items():
            conn = _reset_db(self, f"vacio-{state_key}.db")
            for lang in _LANGS:
                ids = seed_fn(conn, lang)
                person_id = ids["person_id"]
                web.app.dependency_overrides[identity.current_person_id] = (lambda pid=person_id: pid)
                self.addCleanup(web.app.dependency_overrides.pop, identity.current_person_id, None)
                for route in get_routes:
                    for path in _paths_for(route.path, ids):
                        sweep.check(page=path, state=state_key, lang=lang)
                web.app.dependency_overrides.pop(identity.current_person_id, None)

        self.assertEqual(sweep.violations, [], sweep.fail_message())

        # Non-vacuous: a real sweep over real pages, and -- rules (f) and (h) aside, see this
        # module's own docstring -- every rule actually ran against more than 0 elements.
        self.assertGreater(sweep.pages_checked, 60,
                            "the local sweep rendered too few (page, state, language) combinations")
        for rule in ("a_html_lang", "b_single_h1", "c_duplicate_id", "d_link_button_name",
                     "e_form_label", "g_svg_name"):
            self.assertGreater(sweep.counts[rule], 0, f"rule {rule} was never evaluated on any element")
        # Measured, not assumed (see module docstring): no template anywhere renders a bare <img> or
        # sets tabindex, so these two stay at exactly 0 today rather than a fabricated nonzero.
        self.assertEqual(sweep.counts["f_img_alt"], 0,
                          "an <img> appeared -- rule (f) is no longer vacuous, update this assertion")
        self.assertEqual(sweep.counts["h_positive_tabindex"], 0,
                          "a tabindex appeared -- rule (h) is no longer vacuous, update this assertion")


class AccesibilidadHostedTest(CuentaRpTestCase):
    """Hosted mode, for the three hosted-only pages that can ever answer 200: `/garmin`,
    `/ajustes/dispositivos` and `/privacidad/consentimiento` (tests/test_estados_vacios_hosted.py's
    own census of which hosted-only routes ever render). Reuses CuentaRpTestCase's fake-issuer
    session exactly as tests/test_estados_vacios_hosted.py does -- never a mechanism of its own."""

    def setUp(self):
        super().setUp()
        self._sub_n = 0

    def _next_sub(self) -> str:
        self._sub_n += 1
        return f"88888888-0000-0000-0000-{self._sub_n:012d}"

    def _sign_in_fresh(self) -> tuple[TestClient, int]:
        tc = TestClient(web.app, follow_redirects=False)
        sub = self._next_sub()
        auth = self._authorize_params(tc.get("/login"))
        self.issuer.next_id_token = self.issuer.id_token(nonce=auth["nonce"], sub=sub)
        tc.get("/login/cuenta/callback", params={"code": f"c-{sub}", "state": auth["state"]})
        conn = db.connect()
        try:
            pid = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (sub,)).fetchone()["id"]
        finally:
            conn.close()
        return tc, pid

    def test_sweep_hosted_only_pages(self):
        sweep = _Sweep()
        for lang in _LANGS:
            tc, pid = self._sign_in_fresh()
            conn = db.connect()
            try:
                conn.execute("UPDATE person SET language = ? WHERE id = ?", (lang, pid))
                conn.commit()
            finally:
                conn.close()

            # H1: signed in, consent not yet accepted -- the consent-exempt page itself.
            sweep.check(page="/privacidad/consentimiento", state="hosted_sin_consentir", lang=lang, client=tc)
            conn = db.connect()
            try:
                privacidad.record_consent(conn, pid)
            finally:
                conn.close()
            sweep.check(page="/garmin", state="hosted_consentido", lang=lang, client=tc)
            sweep.check(page="/ajustes/dispositivos", state="hosted_consentido", lang=lang, client=tc)

        self.assertEqual(sweep.violations, [], sweep.fail_message())
        self.assertEqual(sweep.pages_checked, len(_LANGS) * 3,
                          "the hosted sweep did not render every hosted-only page for every language")


if __name__ == "__main__":
    unittest.main()
