"""BODY-RENDIMIENTO-3: pages are compressed in transit (GZipMiddleware), the assistant's streamed
answer keeps streaming instead of being buffered or recompressed, and a /static file is cached
forever only when its `v` matches the current content hash. Invented demo data only.
"""
from __future__ import annotations

import gzip
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, web
from body.rutas import comun

PERSON = 1


def _conn(case: unittest.TestCase):
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "rendimiento.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


def _seed_trend_data(conn, days: int = 150) -> None:
    """Enough rows, across every key Tendencias reads (the default cards plus every "Ver todas
    las medidas" extra), that /trends has real repetition to compress -- the property this slice
    is about, not any one figure."""
    keys = list(comun.TREND_KEYS) + [k for _, ks in comun.TREND_GROUPS for k in ks]
    d0 = date(2026, 1, 1)
    for i in range(days):
        day = (d0 + timedelta(days=i)).isoformat()
        for j, key in enumerate(keys):
            db.set_metric(conn, day, key, 40 + (i + j) % 30, "garmin", PERSON)
    conn.commit()


class _RendimientoTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _seed_trend_data(self.conn)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: PERSON
        self.client = TestClient(web.app, follow_redirects=False)


class TrendsGzipRoundTripTest(_RendimientoTestCase):
    """(b)/(c): a gzip-accepting request gets Content-Encoding: gzip and Vary: Accept-Encoding,
    and decompresses to exactly the plain body; a request that declines it gets that same plain
    body straight, with no Content-Encoding at all."""

    def test_accept_encoding_gzip_is_compressed_and_round_trips(self):
        with self.client.stream("GET", "/trends?r=90", headers={"Accept-Encoding": "gzip"}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("content-encoding"), "gzip")
            self.assertIn("accept-encoding", r.headers.get("vary", "").lower())
            compressed = b"".join(r.iter_raw())
        with self.client.stream("GET", "/trends?r=90", headers={"Accept-Encoding": ""}) as r2:
            self.assertEqual(r2.status_code, 200)
            self.assertIsNone(r2.headers.get("content-encoding"))
            plain = b"".join(r2.iter_raw())
        self.assertEqual(gzip.decompress(compressed), plain)
        self.assertLess(len(compressed), len(plain))

    def test_no_accept_encoding_gets_the_plain_body_untouched(self):
        with self.client.stream("GET", "/trends", headers={"Accept-Encoding": ""}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.headers.get("content-encoding"))
            plain = b"".join(r.iter_raw())
        self.assertIn(b"<html", plain.lower())


class SecurityHeadersSurviveGzipTest(_RendimientoTestCase):
    def test_security_headers_present_on_a_compressed_response(self):
        r = self.client.get("/trends?r=90", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("content-encoding"), "gzip")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertEqual(r.headers.get("referrer-policy"), "same-origin")
        self.assertIn("default-src 'self'", r.headers.get("content-security-policy", ""))
        self.assertEqual(r.headers.get("cross-origin-opener-policy"), "same-origin")


class AssistantStreamKeepsStreamingTest(_RendimientoTestCase):
    """(a): the SSE route (body/rutas/asistente.py, media_type="text/event-stream") is excluded
    from GZip by Starlette's own DEFAULT_EXCLUDED_CONTENT_TYPES -- it is neither compressed nor
    buffered into one chunk, with the demo provider's two-round, three-event exchange (herramienta,
    texto, fin) arriving as separate increments, not one chunk at the end."""

    def setUp(self):
        super().setUp()
        import os
        self._provider_env = {k: os.environ.get(k) for k in ("BODY_AGENTE_PROVEEDOR",)}
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        import os
        for k, v in self._provider_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_sse_response_is_not_gzip_encoded(self):
        with self.client.stream("POST", "/asistente/preguntar", headers={"Accept-Encoding": "gzip"},
                                json={"pregunta": "¿cómo voy?", "conversacion_id": None}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.headers.get("content-encoding"))
            self.assertEqual(r.headers.get("content-type", "").split(";")[0], "text/event-stream")
            chunks = list(r.iter_raw())
        texto_completo = b"".join(chunks).decode()
        self.assertIn("event: herramienta", texto_completo)
        self.assertIn("event: texto", texto_completo)
        self.assertIn("event: fin", texto_completo)

    def test_the_three_sse_events_arrive_as_separate_increments(self):
        with self.client.stream("POST", "/asistente/preguntar", headers={"Accept-Encoding": "gzip"},
                                json={"pregunta": "¿cómo voy?", "conversacion_id": None}) as r:
            chunks = [c for c in r.iter_raw() if c]
        eventos_por_chunk = sum(1 for c in chunks if c.count(b"event: ") >= 1)
        self.assertGreaterEqual(len(chunks), 3, chunks)
        self.assertGreaterEqual(eventos_por_chunk, 3)


class StaticCachingTest(_RendimientoTestCase):
    """(2): a /static request whose `v` equals the current hash is cached forever; any other `v`
    (absent, stale, or foreign) revalidates instead, for body.css and a script. A font under
    /static/fonts/ (BODY-FUENTE-CACHE-1) is immutable by path alone -- cached forever with or
    without `v`, since it has none to carry."""

    VERSIONED_PATHS = (
        "body.css",
        "charts.js",
    )
    FONT_PATH = "fonts/hanken-grotesk/hanken-grotesk-latin-wght-normal.woff2"

    def test_current_v_is_cached_forever(self):
        for path in self.VERSIONED_PATHS + (self.FONT_PATH,):
            r = self.client.get(f"/static/{path}?v={comun.STATIC_HASH}")
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers.get("cache-control"), "public, max-age=31536000, immutable", path)

    def test_missing_v_revalidates_except_the_font(self):
        for path in self.VERSIONED_PATHS:
            r = self.client.get(f"/static/{path}")
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers.get("cache-control"), "no-cache", path)
        r = self.client.get(f"/static/{self.FONT_PATH}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "public, max-age=31536000, immutable")

    def test_stale_v_revalidates_except_the_font(self):
        for path in self.VERSIONED_PATHS:
            r = self.client.get(f"/static/{path}?v=0000000000")
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers.get("cache-control"), "no-cache", path)
        r = self.client.get(f"/static/{self.FONT_PATH}?v=0000000000")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "public, max-age=31536000, immutable")

    def test_sw_js_keeps_no_cache(self):
        r = self.client.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-cache")


if __name__ == "__main__":
    unittest.main()
