"""BODY-SEGURIDAD-1: the same-origin check for unsafe methods, the fixed response-header set, and
the redirect-target guard (`cuenta.safe_return_to`).

BODY-SEGURIDAD-2 adds the script-src half of the Content-Security-Policy: every inline executable
<script> and inline event-handler attribute moved to /static (CSS01ScriptGuard / HandlerGuard
below), and the policy itself grew from `frame-ancestors 'none'` alone to the full header built by
`body.seguridad.content_security_policy()`.

Local mode only (no BODY_CUENTA_ISSUER): the Origin/Referer check runs ahead of
`identity.current_person_id`, so it refuses a cross-site POST/PUT/PATCH/DELETE before any route
handler, any auth check or any database write -- exactly the property these tests exercise.
"""
from __future__ import annotations

import ast
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._rutas import rutas_planas

from fastapi.testclient import TestClient

from body import config, cuenta, db, seguridad, web

ROOT = Path(__file__).resolve().parent.parent
BODY_DIR = ROOT / "body"
TEMPLATES_DIR = BODY_DIR / "templates"
STATIC_DIR = BODY_DIR / "static"

_PATH_PARAM_RE = re.compile(r"\{[^{}]+\}")


def _unsafe_pairs() -> list[tuple[str, str]]:
    """(path_template, method) for every route in the live app whose method is one
    seguridad.UNSAFE_METHODS covers -- read from the app itself, never typed by hand, so this
    test's own count tracks the route set instead of going stale against it."""
    pairs = []
    for r in rutas_planas(web.app.routes):
        for m in getattr(r, "methods", None) or ():
            if m in seguridad.UNSAFE_METHODS:
                pairs.append((r.path, m))
    return pairs


def _concrete_path(path_template: str) -> str:
    return _PATH_PARAM_RE.sub("1", path_template)


class SeguridadTestCase(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "seguridad.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        self.client = TestClient(web.app, follow_redirects=False)


class CrossSiteUnsafeMethodRefusedTest(SeguridadTestCase):
    """Guard (a): EVERY unsafe-method route refuses a cross-site Origin, before its handler runs."""

    def test_every_unsafe_route_refuses_a_foreign_origin(self):
        pairs = _unsafe_pairs()
        self.assertGreater(len(pairs), 0)
        checked = 0
        for path_template, method in pairs:
            concrete = _concrete_path(path_template)
            r = self.client.request(method, concrete, headers={"Origin": "https://evil.example"})
            self.assertEqual(r.status_code, 403, f"{method} {concrete}")
            self.assertIn("Esta acción vino de otra página y no se hizo.", r.text,
                          f"{method} {concrete} was not answered with origen_rechazado.html")
            checked += 1
        self.assertEqual(checked, len(pairs))
        self.assertGreater(checked, 0)


class OriginAndRefererAllowedTest(SeguridadTestCase):
    """Guard (b): same-origin Origin, Referer-only same-origin, and neither header, all reach the
    handler -- /settings/language always answers 303 regardless of its body, in local mode."""

    def test_same_origin_origin_header_reaches_handler(self):
        r = self.client.post("/settings/language", headers={"Origin": "http://testserver"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/settings")

    def test_same_origin_referer_only_reaches_handler(self):
        r = self.client.post("/settings/language",
                             headers={"Referer": "http://testserver/settings"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/settings")

    def test_no_origin_and_no_referer_reaches_handler(self):
        r = self.client.post("/settings/language")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/settings")


class OriginNullRefusedTest(SeguridadTestCase):
    """Guard (c): `Origin: null` -- what a browser sends for a cross-site POST, and what
    `no-referrer` used to make it send for our OWN forms (the trap this slice's templates fix)."""

    def test_origin_null_is_refused(self):
        r = self.client.post("/settings/language", headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403)


class RefusalPageLanguageTest(SeguridadTestCase):
    """BODY-SEGURIDAD-1-M: the refusal page runs before any person is resolved, so it must read
    the request's own Accept-Language instead of always answering in DEFAULT_LANG."""

    def _refuse(self, accept_language: str | None):
        headers = {"Origin": "https://evil.example"}
        if accept_language is not None:
            headers["Accept-Language"] = accept_language
        return self.client.post("/settings/language", headers=headers)

    def test_english_accept_language_gets_english_text(self):
        r = self._refuse("en-US,en;q=0.9")
        self.assertEqual(r.status_code, 403)
        self.assertIn("This action came from another page and was not done.", r.text)

    def test_spanish_accept_language_gets_spanish_text(self):
        r = self._refuse("es-CO")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Esta acción vino de otra página y no se hizo.", r.text)

    def test_absent_accept_language_gets_spanish_text(self):
        r = self._refuse(None)
        self.assertEqual(r.status_code, 403)
        self.assertIn("Esta acción vino de otra página y no se hizo.", r.text)

    def test_other_accept_language_gets_spanish_text(self):
        r = self._refuse("fr-FR,fr;q=0.9")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Esta acción vino de otra página y no se hizo.", r.text)


class SecurityHeadersTest(SeguridadTestCase):
    """Guard (c): the fixed header set, and the full CSP, on a 200 page, a 404, a 303 redirect,
    and a /static file -- in local mode, where form-action is 'self' alone."""

    def _assert_headers(self, r) -> None:
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("referrer-policy"), "same-origin")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        csp = r.headers.get("content-security-policy")
        self.assertEqual(csp, seguridad.content_security_policy())
        directives = {d.strip() for d in csp.split(";")}
        script_src = next(d for d in directives if d.startswith("script-src"))
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertNotIn("'unsafe-eval'", script_src)
        self.assertIn("form-action 'self'", directives)
        self.assertEqual(r.headers.get("cross-origin-opener-policy"), "same-origin")

    def test_200_page(self):
        self._assert_headers(self.client.get("/"))

    def test_404(self):
        r = self.client.get("/this-route-does-not-exist")
        self.assertEqual(r.status_code, 404)
        self._assert_headers(r)

    def test_303_redirect(self):
        r = self.client.post("/settings/language")
        self.assertEqual(r.status_code, 303)
        self._assert_headers(r)

    def test_static_file(self):
        r = self.client.get("/static/body.css")
        self.assertEqual(r.status_code, 200)
        self._assert_headers(r)


class NoReferrerMetaGoneTest(unittest.TestCase):
    """Guard (e): no template says `no-referrer` any more -- it would make the browser send
    Origin: null on Body's own forms, which the Origin check above refuses."""

    def test_no_template_sets_no_referrer(self):
        offenders = []
        for path in sorted(TEMPLATES_DIR.glob("*.html")):
            if re.search(r'<meta\s+name="referrer"\s+content="no-referrer"', path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


# ---- BODY-SEGURIDAD-2 guards -----------------------------------------------------------------

_SCRIPT_OPEN_RE = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_HANDLER_ATTR_RE = re.compile(r'\bon[a-z]+\s*=\s*"', re.IGNORECASE)
_STATIC_URL_JS_RE = re.compile(r"static_url\('([^']+\.js)'\)")


def _inline_script_offenders() -> list[str]:
    """Every `<script>` tag under body/templates with neither `src=` nor
    `type="application/json"` -- an executable inline script script-src 'self' would block.
    Proved to catch all 8 pre-slice offenders (base.html:58,156; settings.html:84;
    goal_new.html:97; the four standalone pages' theme-before-paint copy) before this slice's
    templates moved them to /static."""
    offenders = []
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _SCRIPT_OPEN_RE.finditer(text):
            attrs = m.group(1)
            if "src=" in attrs:
                continue
            if re.search(r'type\s*=\s*"application/json"', attrs):
                continue
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    return offenders


class InlineScriptGoneTest(unittest.TestCase):
    """Guard (a): no template under body/templates holds an executable inline <script> any more."""

    def test_no_executable_inline_script(self):
        self.assertEqual(_inline_script_offenders(), [])


def _inline_handler_offenders() -> list[str]:
    offenders = []
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _HANDLER_ATTR_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    return offenders


class InlineEventHandlerGoneTest(unittest.TestCase):
    """Guard (b): no template holds an inline event-handler attribute (onchange=, onclick=, ...)
    any more -- goal_new.html's onchange="bodyMetricChange()" was the one offender."""

    def test_no_inline_event_handler_attribute(self):
        self.assertEqual(_inline_handler_offenders(), [])


class StaticUrlScriptsExistTest(unittest.TestCase):
    """Guard (d): every static_url('...js') a template references names a file that exists under
    body/static -- including the four new ones this slice adds."""

    def test_every_referenced_js_file_exists(self):
        referenced = set()
        for path in sorted(TEMPLATES_DIR.glob("*.html")):
            referenced.update(_STATIC_URL_JS_RE.findall(path.read_text(encoding="utf-8")))
        self.assertIn("tema.js", referenced)
        self.assertIn("pagina.js", referenced)
        self.assertIn("ajustes_tema.js", referenced)
        self.assertIn("meta_nueva.js", referenced)
        missing = [name for name in referenced if not (STATIC_DIR / name).is_file()]
        self.assertEqual(missing, [])


class FormActionOriginTest(unittest.TestCase):
    """Guard (e): form-action is 'self' alone locally, and 'self' plus Cuenta's issuer origin
    once hosted -- the origin a Body form's POST (/logout, /privacidad/consentimiento,
    /privacidad/borrar) can 303-redirect to, via cuenta.logout_url(...)."""

    def test_local_mode_is_self_only(self):
        self.assertNotIn(cuenta.ISSUER_ENV, os.environ)
        self.assertEqual(seguridad._form_action_sources(), ["'self'"])

    def test_hosted_mode_adds_cuenta_issuer_origin(self):
        with mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"}):
            self.assertEqual(seguridad._form_action_sources(), ["'self'", "https://fake-cuenta.test"])

    def test_hosted_mode_strips_a_path_from_the_issuer(self):
        with mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test/realm"}):
            self.assertEqual(seguridad._form_action_sources(), ["'self'", "https://fake-cuenta.test"])


class HostedResponseCarriesFormActionTest(SeguridadTestCase):
    """The same thing, end to end: a real response served hosted carries Cuenta's origin in
    form-action, whatever status code the route itself answers with."""

    def test_hosted_response_header_includes_issuer_origin(self):
        with mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"}):
            r = self.client.get("/")
        csp = r.headers.get("content-security-policy")
        directives = {d.strip() for d in csp.split(";")}
        self.assertIn("form-action 'self' https://fake-cuenta.test", directives)


# ---- Guard (f): every RedirectResponse(...) target is either fixed, or provably sanitized -------

# Call sites the generic classifier below cannot see through, each with the reason it is safe
# anyway. Any RedirectResponse call this test finds that is NEITHER generically safe NOR listed
# here fails the test by name -- a new call site must either become generically safe (a fixed
# literal, an f-string with a fixed leading "/x" prefix, a direct cuenta.safe_return_to(...) /
# cuenta.logout_url(...) call, or a bare variable assigned from cuenta.safe_return_to(...) in the
# same function) or be added here with a reason.
ALLOWLIST = {
    ("body/rutas/sesion.py", 50):
        "url is cuenta.start_login()'s authorize_url -- built server-side from issuer() and random "
        "state/nonce/PKCE values, external to Body by design; never reflects request data",
    ("body/rutas/sesion.py", 76):
        "result.return_to was already passed through cuenta.safe_return_to in this same module's "
        "login_start, before being stored in the inicio_sesion row handle_callback reads it back from",
    ("body/rutas/ajustes.py", 72):
        "ajustes' own stricter allow-list: volver is narrowed to {'/', '/settings'} two lines above "
        "(body/rutas/ajustes.py's set_trabaja_turnos) -- left as is, per this slice's spec",
}


def _is_cuenta_call(node: ast.AST, attr: str) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "cuenta")


def _fixed_prefix_fstring(node: ast.AST) -> bool:
    """A JoinedStr (f-string) whose literal LEADING chunk already fixes the string as starting
    with "/" then a non-"/"-non-"\\" character -- nothing a later substitution can insert changes
    characters a browser already resolved from the fixed prefix, so whatever lands in the query
    or fragment behind it cannot turn the target into a protocol-relative redirect."""
    if not isinstance(node, ast.JoinedStr) or not node.values:
        return False
    first = node.values[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return False
    prefix = first.value
    return len(prefix) >= 2 and prefix[0] == "/" and prefix[1] not in ("/", "\\")


def _assigned_from_safe_return_to(func_node: ast.AST, name: str) -> bool:
    for n in ast.walk(func_node):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name
                and _is_cuenta_call(n.value, "safe_return_to")):
            return True
    return False


def _enclosing_function(tree: ast.Module, call_node: ast.AST):
    best = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(n, "end_lineno", None)
            if n.lineno <= call_node.lineno and (end is None or call_node.lineno <= end):
                if best is None or n.lineno > best.lineno:
                    best = n
    return best


class RedirectTargetsAreSafeTest(unittest.TestCase):
    def test_every_redirect_response_target_is_fixed_or_sanitized(self):
        unresolved = []
        allowlist_hits = set()
        for path in sorted(BODY_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "RedirectResponse" and node.args):
                    continue
                target = node.args[0]
                key = (rel, node.lineno)
                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    continue
                if _fixed_prefix_fstring(target):
                    continue
                if _is_cuenta_call(target, "safe_return_to") or _is_cuenta_call(target, "logout_url"):
                    continue
                if isinstance(target, ast.Name):
                    func = _enclosing_function(tree, node)
                    if func is not None and _assigned_from_safe_return_to(func, target.id):
                        continue
                if key in ALLOWLIST:
                    allowlist_hits.add(key)
                    continue
                unresolved.append(f"{rel}:{node.lineno}")
        self.assertEqual(unresolved, [],
                          f"RedirectResponse target not provably fixed or sanitized -- wrap it in "
                          f"cuenta.safe_return_to(...) or add a named, reasoned ALLOWLIST entry: "
                          f"{unresolved}")
        stale = set(ALLOWLIST) - allowlist_hits
        self.assertEqual(stale, set(), f"ALLOWLIST entry no longer matches any call site: {stale}")


# ---- BODY-ESTILOS-1 guards: style-src drops 'unsafe-inline' -----------------------------------

_TEMPLATE_STYLE_ATTR_RE = re.compile(r"style\s*=", re.IGNORECASE)


def _template_style_offenders() -> list[str]:
    """Every raw `style=` (any case, any spacing) left in a template's source text. Proved to catch
    201 occurrences across 22 templates on main 61cb029 -- the count this slice's own spec measured --
    before this slice moved each one to a class in body.css."""
    offenders = []
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _TEMPLATE_STYLE_ATTR_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    return offenders


class NoInlineStyleInTemplatesTest(unittest.TestCase):
    """Guard (a): no template under body/templates holds a `style="..."` attribute any more."""

    def test_no_style_attribute_in_any_template(self):
        self.assertEqual(_template_style_offenders(), [])


_PY_STYLE_SUBSTR_RE = re.compile(r"style\s*=", re.IGNORECASE)


def _python_markup_style_offenders() -> list[tuple[str, int]]:
    """An ast scan of every .py file under body/ for a string constant or an f-string's literal
    chunk containing `style=` -- the server-built SVG/HTML body/profile.py and body/rutas/metas.py
    used to assemble with f-strings. Proved to catch 11 lines in body/profile.py and 9 in
    body/rutas/metas.py on main 61cb029 before this slice moved each one to a class."""
    offenders = []
    scanned = 0
    for path in sorted(BODY_DIR.rglob("*.py")):
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _PY_STYLE_SUBSTR_RE.search(node.value):
                    offenders.append((rel, node.lineno))
            elif isinstance(node, ast.JoinedStr):
                for chunk in node.values:
                    if (isinstance(chunk, ast.Constant) and isinstance(chunk.value, str)
                            and _PY_STYLE_SUBSTR_RE.search(chunk.value)):
                        offenders.append((rel, node.lineno))
    assert scanned > 0, "no .py file found under body/ -- the scan itself is broken"
    return offenders


class NoInlineStyleInPythonMarkupTest(unittest.TestCase):
    """Guard (b): no string constant or f-string chunk under body/ assembles a `style=` attribute
    any more -- body/profile.py's and body/rutas/metas.py's server-built SVG now carry a class
    instead."""

    def test_no_style_attribute_built_in_python(self):
        self.assertEqual(_python_markup_style_offenders(), [])


_JS_SET_ATTR_STYLE_RE = re.compile(r"setAttribute\(\s*['\"]style['\"]", re.IGNORECASE)
_JS_LITERAL_STYLE_ATTR_RE = re.compile(r"(?<!\.)\bstyle\s*=", re.IGNORECASE)


def _js_style_attribute_offenders() -> list[str]:
    """Every body/static/*.js line that sets the `style` ATTRIBUTE -- `setAttribute('style', ...)`
    or a template string/innerHTML assembling `style="..."` -- never a CSSOM property set such as
    `el.style.left = ...` or `el.style.setProperty(...)`, which charts.js and assistant.js already
    use and which this guard deliberately leaves alone: the negative lookbehind on `style\\s*=`
    refuses to match when `style` is itself preceded by a `.` (a property access), so `el.style =`
    would still be flagged -- nothing in body/static does that -- while `el.style.left =` is not,
    because the character right before `=` there is `t` from `.left`, not `style`."""
    offenders = []
    for path in sorted((STATIC_DIR).glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for pattern in (_JS_SET_ATTR_STYLE_RE, _JS_LITERAL_STYLE_ATTR_RE):
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
    return offenders


class NoJsSetsStyleAttributeTest(unittest.TestCase):
    """Guard (c): no script under body/static sets the `style` attribute -- CSSOM property sets
    (`el.style.prop = ...`, `el.style.setProperty(...)`) are not CSP-relevant and stay."""

    def test_no_js_sets_the_style_attribute(self):
        self.assertEqual(_js_style_attribute_offenders(), [])


class StyleSrcHasNoUnsafeInlineTest(unittest.TestCase):
    """Guard (d): style-src carries no 'unsafe-inline' any more -- the gap BODY-SEGURIDAD-2 left and
    this slice closes, now that guards (a)-(c) prove nothing needs it."""

    def test_style_src_has_no_unsafe_inline(self):
        csp = seguridad.content_security_policy()
        directives = {d.strip() for d in csp.split(";")}
        style_src = next(d for d in directives if d.startswith("style-src"))
        self.assertNotIn("'unsafe-inline'", style_src)
        self.assertIn("style-src 'self'", directives)


class SafeReturnToTest(unittest.TestCase):
    """cuenta.safe_return_to's own refusal cases (BODY-SEGURIDAD-1 item 4)."""

    def test_refuses_protocol_relative_and_backslash_forms(self):
        from body import cuenta
        for raw in ("//x", "/\\x", "/\\\\x", "\\x", "https://x"):
            self.assertEqual(cuenta.safe_return_to(raw), "/", raw)

    def test_allows_percent_encoded_backslash(self):
        # Not a literal backslash -- a browser resolves "%5C" as two ordinary path characters, so
        # "/%5Cx" never becomes a protocol-relative target; it stays a same-origin path.
        from body import cuenta
        self.assertEqual(cuenta.safe_return_to("/%5Cx"), "/%5Cx")

    def test_allows_an_ordinary_path_with_query(self):
        from body import cuenta
        self.assertEqual(cuenta.safe_return_to("/ok/path?q=1"), "/ok/path?q=1")


if __name__ == "__main__":
    unittest.main()
