"""BODY-FUENTES-PROPIAS-1: Hanken Grotesk is self-hosted from body/static/fonts/hanken-grotesk/,
the same files Duna's console commits (ADMIN-FUENTES-LOCALES-1) -- no page loads anything from
fonts.googleapis.com or fonts.gstatic.com any more, and the served policy carries no https: origin
in style-src or font-src.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, seguridad, web

ROOT = Path(__file__).resolve().parent.parent
BODY_DIR = ROOT / "body"
TEMPLATES_DIR = BODY_DIR / "templates"
STATIC_DIR = BODY_DIR / "static"
FONTS_DIR = STATIC_DIR / "fonts" / "hanken-grotesk"

# sha256 of the files as copied from C:\Duna\duna-admin-ui\src\fonts\hanken-grotesk\ (ADMIN-
# FUENTES-LOCALES-1, commit fcd45ad1), re-measured by this slice -- see DECISIONS.md.
_FONT_FILES = {
    "hanken-grotesk-latin-wght-normal.woff2":
        "e9201eddf1d41d0b62253295d869ce3cf65768f7102b797f02c7f8c876b4a9d5",
    "hanken-grotesk-latin-ext-wght-normal.woff2":
        "768af2923e0ab1549f1dfba0a5c8ea749c4c01f01d8e77ffaf7fcd12f57a0a24",
}

# BODY-FUENTE-CACHE-1: every file under body/static/fonts/ (relative to that directory), the same
# three hashes already pinned above and in LicenseShipsBesideFontsTest -- gathered here once so
# EveryFontFileIsPinnedTest can prove the set on disk is exactly this set, no more and no fewer.
_FUENTES_DIR_FILES = {
    "hanken-grotesk/hanken-grotesk-latin-wght-normal.woff2":
        "e9201eddf1d41d0b62253295d869ce3cf65768f7102b797f02c7f8c876b4a9d5",
    "hanken-grotesk/hanken-grotesk-latin-ext-wght-normal.woff2":
        "768af2923e0ab1549f1dfba0a5c8ea749c4c01f01d8e77ffaf7fcd12f57a0a24",
    "hanken-grotesk/LICENSE":
        "ef8780a5a0864714268dd7b6e00de9b39605a1dd61650ed2fa0af9b79b857967",
}

_THIRD_PARTY_RE = re.compile(r"fonts\.googleapis\.com|fonts\.gstatic\.com")

# BODY-FUENTES-PROPIAS-1-M: these are the three files git must store byte-exact. autocrlf on
# this machine plus the old `* text eol=lf` rule rewrote binary bytes on `git add` (2 bytes lost
# from each .woff2, 93 from the CRLF LICENSE) until .gitattributes marked them non-text -- see
# DECISIONS.md. This guards the TRACKED blob, so a future .gitattributes change that lets git
# rewrite them again goes red.
_TRACKED_FILES = {
    "body/static/fonts/hanken-grotesk/hanken-grotesk-latin-wght-normal.woff2":
        "e9201eddf1d41d0b62253295d869ce3cf65768f7102b797f02c7f8c876b4a9d5",
    "body/static/fonts/hanken-grotesk/hanken-grotesk-latin-ext-wght-normal.woff2":
        "768af2923e0ab1549f1dfba0a5c8ea749c4c01f01d8e77ffaf7fcd12f57a0a24",
    "body/static/fonts/hanken-grotesk/LICENSE":
        "ef8780a5a0864714268dd7b6e00de9b39605a1dd61650ed2fa0af9b79b857967",
}


class _FuentesTestCase(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "fuentes.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        self.client = TestClient(web.app, follow_redirects=False)


class FontFilesServedTest(_FuentesTestCase):
    """Guard (a): each font file is served as font/woff2, byte-identical to the copy on disk."""

    def test_each_font_file_served_correctly(self):
        self.assertGreater(len(_FONT_FILES), 0)
        checked = 0
        for name, expected_sha256 in _FONT_FILES.items():
            r = self.client.get(f"/static/fonts/hanken-grotesk/{name}")
            self.assertEqual(r.status_code, 200, name)
            self.assertEqual(r.headers.get("content-type"), "font/woff2", name)
            self.assertEqual(hashlib.sha256(r.content).hexdigest(), expected_sha256, name)
            checked += 1
        self.assertEqual(checked, len(_FONT_FILES))


class NoThirdPartyFontOriginTest(unittest.TestCase):
    """Guard (b): no template and no static file names fonts.googleapis.com or fonts.gstatic.com --
    non-vacuous, so this guard counts what it scanned (proved to bite on main 4d7a8cc: 5 templates,
    10 occurrences, see DECISIONS.md)."""

    def test_no_template_or_static_file_names_google_fonts(self):
        offenders = []
        templates_scanned = sorted(TEMPLATES_DIR.glob("*.html"))
        self.assertGreater(len(templates_scanned), 0)
        for path in templates_scanned:
            text = path.read_text(encoding="utf-8")
            for m in _THIRD_PARTY_RE.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}")

        static_scanned = [p for p in STATIC_DIR.rglob("*")
                           if p.is_file() and p.suffix in (".css", ".js", ".html")]
        self.assertGreater(len(static_scanned), 0)
        for path in static_scanned:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in _THIRD_PARTY_RE.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}")

        self.assertEqual(offenders, [])
        self.assertGreater(len(templates_scanned) + len(static_scanned), 0)


class PolicyHasNoHttpsOriginTest(unittest.TestCase):
    """Guard (c): the served policy's style-src and font-src are 'self' alone -- no https: origin."""

    def test_style_src_and_font_src_have_no_https_origin(self):
        csp = seguridad.content_security_policy()
        directives = {d.strip() for d in csp.split(";")}
        style_src = next(d for d in directives if d.startswith("style-src"))
        font_src = next(d for d in directives if d.startswith("font-src"))
        self.assertEqual(style_src, "style-src 'self'")
        self.assertEqual(font_src, "font-src 'self'")
        self.assertNotIn("https:", style_src)
        self.assertNotIn("https:", font_src)


class BasePagePreloadsLatinFontTest(_FuentesTestCase):
    """Guard (d): the rendered base page carries the preload link for the latin file, so it starts
    loading with the HTML as Google's CSS used to."""

    def test_rendered_page_carries_preload_link(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(
            '<link rel="preload" '
            'href="/static/fonts/hanken-grotesk/hanken-grotesk-latin-wght-normal.woff2" '
            'as="font" type="font/woff2" crossorigin>',
            r.text,
        )


class LicenseShipsBesideFontsTest(unittest.TestCase):
    """Guard (e): the OFL licence ships beside the font files it covers."""

    def test_license_file_present(self):
        license_path = FONTS_DIR / "LICENSE"
        self.assertTrue(license_path.is_file())
        self.assertEqual(
            hashlib.sha256(license_path.read_bytes()).hexdigest(),
            "ef8780a5a0864714268dd7b6e00de9b39605a1dd61650ed2fa0af9b79b857967",
        )


class EveryFontFileIsPinnedTest(unittest.TestCase):
    """Guard (g) (BODY-FUENTE-CACHE-1): a font file is immutable by its path alone -- so every file
    under body/static/fonts/ must have its sha256 pinned here, no more and no fewer, or an in-place
    change (or a new file that nobody pinned) ships quietly. Non-vacuous: the set of pinned paths is
    compared to the set actually on disk, and the count checked must equal both."""

    def test_every_file_on_disk_is_pinned_and_matches(self):
        on_disk = sorted(
            p.relative_to(FONTS_DIR.parent).as_posix()
            for p in (FONTS_DIR.parent).rglob("*")
            if p.is_file()
        )
        self.assertGreater(len(on_disk), 0)
        self.assertEqual(on_disk, sorted(_FUENTES_DIR_FILES))

        checked = 0
        for rel_path, expected_sha256 in _FUENTES_DIR_FILES.items():
            actual = hashlib.sha256((FONTS_DIR.parent / rel_path).read_bytes()).hexdigest()
            self.assertEqual(actual, expected_sha256, rel_path)
            checked += 1
        self.assertEqual(checked, len(on_disk))


class TrackedBlobByteExactTest(unittest.TestCase):
    """Guard (f): the blob git has COMMITTED for each font/licence file hashes to the expected
    value -- not just the file on disk. Catches a .gitattributes regression that lets `git add`
    rewrite these bytes again (BODY-FUENTES-PROPIAS-1-M)."""

    def test_tracked_blob_matches_expected_hash(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available on PATH")
        if not (ROOT / ".git").exists():
            self.skipTest("not running inside a git checkout")

        checked = 0
        for rel_path, expected_sha256 in _TRACKED_FILES.items():
            result = subprocess.run(
                ["git", "cat-file", "-p", f"HEAD:{rel_path}"],
                cwd=ROOT,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            actual = hashlib.sha256(result.stdout).hexdigest()
            self.assertEqual(actual, expected_sha256, rel_path)
            checked += 1
        self.assertEqual(checked, len(_TRACKED_FILES))


if __name__ == "__main__":
    unittest.main()
