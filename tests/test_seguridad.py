"""BODY-SEGURIDAD-1: the same-origin check for unsafe methods, the fixed response-header set, and
the redirect-target guard (`cuenta.safe_return_to`).

Local mode only (no BODY_CUENTA_ISSUER): the Origin/Referer check runs ahead of
`identity.current_person_id`, so it refuses a cross-site POST/PUT/PATCH/DELETE before any route
handler, any auth check or any database write -- exactly the property these tests exercise.
"""
from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._rutas import rutas_planas

from fastapi.testclient import TestClient

from body import config, db, seguridad, web

ROOT = Path(__file__).resolve().parent.parent
BODY_DIR = ROOT / "body"
TEMPLATES_DIR = BODY_DIR / "templates"

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
    """Guard (d): the fixed header set on a 200 page, a 404, a 303 redirect, and a /static file."""

    def _assert_headers(self, r) -> None:
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("referrer-policy"), "same-origin")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertEqual(r.headers.get("content-security-policy"), "frame-ancestors 'none'")
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
