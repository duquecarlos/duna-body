"""BODY-DISPOSITIVO-1: device keys (one per linked PC), the Ajustes page that mints/revokes them,
and the two hosted-only API routes they authorize. Invented demo data only, no real health figures.
"""
import contextlib
import io
import json
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, dispositivo, identity, vida_export, web

FIXTURES = Path(__file__).parent / "fixtures"


class _CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


# ---- the module itself, no HTTP ------------------------------------------------------------------

class DispositivoModuleTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()

    def test_crear_returns_bdy_key_and_stores_only_the_hash(self):
        key = dispositivo.crear(self.conn, 1, "PC de prueba", {"turnos:subir"})
        self.assertTrue(key.startswith("bdy_"))
        rows = self.conn.execute("SELECT * FROM dispositivo").fetchall()
        self.assertEqual(len(rows), 1)
        for row in rows:
            for value in tuple(row):
                self.assertNotEqual(value, key)

    def test_resolver_ok(self):
        key = dispositivo.crear(self.conn, 1, "PC", {"turnos:subir", "vida:leer"})
        self.assertEqual(dispositivo.resolver(self.conn, key), (1, {"turnos:subir", "vida:leer"}))

    def test_resolver_unknown_key(self):
        self.assertIsNone(dispositivo.resolver(self.conn, "bdy_does-not-exist"))

    def test_resolver_revoked_key(self):
        key = dispositivo.crear(self.conn, 1, "PC", {"turnos:subir"})
        row = self.conn.execute("SELECT id FROM dispositivo WHERE person_id = 1").fetchone()
        self.assertTrue(dispositivo.revocar(self.conn, 1, row["id"]))
        self.assertIsNone(dispositivo.resolver(self.conn, key))

    def test_listar_never_returns_the_hash(self):
        dispositivo.crear(self.conn, 1, "PC", {"turnos:subir"})
        items = dispositivo.listar(self.conn, 1)
        self.assertEqual(len(items), 1)
        self.assertNotIn("token_hash", items[0])

    def test_listar_excludes_revoked_rows(self):
        dispositivo.crear(self.conn, 1, "PC1", {"turnos:subir"})
        dispositivo.crear(self.conn, 1, "PC2", {"vida:leer"})
        row1 = self.conn.execute("SELECT id FROM dispositivo WHERE nombre = 'PC1'").fetchone()
        dispositivo.revocar(self.conn, 1, row1["id"])
        self.assertEqual([i["nombre"] for i in dispositivo.listar(self.conn, 1)], ["PC2"])

    def test_revocar_on_another_persons_id_changes_nothing(self):
        dispositivo.crear(self.conn, 2, "B's PC", {"turnos:subir"})
        row = self.conn.execute("SELECT id FROM dispositivo WHERE person_id = 2").fetchone()
        self.assertFalse(dispositivo.revocar(self.conn, 1, row["id"]))
        still_live = self.conn.execute(
            "SELECT revocado_en FROM dispositivo WHERE id = ?", (row["id"],)
        ).fetchone()["revocado_en"]
        self.assertIsNone(still_live)


# ---- hosted HTTP base: person 1 ("Me", from db.init) and person 2 ("B") --------------------------

class DispositivoHostedTestCase(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "dispositivo.db"
        self.conn = db.connect()
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()

        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)

        self.addCleanup(web.app.dependency_overrides.clear)
        self.client = TestClient(web.app, follow_redirects=False)


class LocalModeDeviceRoutesTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        self.client = TestClient(web.app, follow_redirects=False)

    def test_ajustes_dispositivos_404(self):
        self.assertEqual(self.client.get("/ajustes/dispositivos").status_code, 404)

    def test_api_turnos_404(self):
        self.assertEqual(self.client.post("/api/turnos", content=b"{}").status_code, 404)

    def test_api_vida_semana_404(self):
        self.assertEqual(self.client.get("/api/vida/semana").status_code, 404)


class AjustesPageTest(DispositivoHostedTestCase):
    def test_key_renders_once_then_not_on_a_later_get(self):
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        r1 = self.client.post(
            "/ajustes/dispositivos",
            data={"nombre": "Mi PC", "alcance_turnos": "1", "alcance_vida": "1"},
        )
        self.assertEqual(r1.status_code, 200)
        m = re.search(r"bdy_[A-Za-z0-9_-]+", r1.text)
        self.assertIsNotNone(m, "the new key never appeared in the creation response")
        key = m.group(0)
        self.assertEqual(dispositivo.resolver(self.conn, key), (1, {"turnos:subir", "vida:leer"}))

        r2 = self.client.get("/ajustes/dispositivos")
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn(key, r2.text)

    def test_revocar_only_affects_the_callers_own_row(self):
        web.app.dependency_overrides[identity.current_person_id] = lambda: 2
        key_b = dispositivo.crear(self.conn, 2, "B's PC", {"turnos:subir"})
        row_b = self.conn.execute("SELECT id FROM dispositivo WHERE person_id = 2").fetchone()

        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        r = self.client.post(f"/ajustes/dispositivos/{row_b['id']}/revocar")
        self.assertEqual(r.status_code, 303)
        self.assertIsNotNone(dispositivo.resolver(self.conn, key_b))


# ---- POST /api/turnos ------------------------------------------------------------------------------

class ApiTurnosTest(DispositivoHostedTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC de Carlos", {"turnos:subir", "vida:leer"})

    def _post(self, body: bytes, *, auth: str | None = None, file_name: str = "turnos-demo-a.json"):
        headers = {"X-Body-Archivo": file_name}
        if auth is not None:
            headers["Authorization"] = auth
        return self.client.post("/api/turnos", content=body, headers=headers)

    def test_no_header_401(self):
        r = self._post(b"{}")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"error": "no_autorizado"})
        self.assertEqual(r.headers.get("www-authenticate"), "Bearer")

    def test_bad_key_401(self):
        r = self._post(b"{}", auth="Bearer bdy_does-not-exist")
        self.assertEqual(r.status_code, 401)

    def test_revoked_key_401(self):
        row = self.conn.execute("SELECT id FROM dispositivo WHERE person_id = 1").fetchone()
        dispositivo.revocar(self.conn, 1, row["id"])
        r = self._post(b"{}", auth=f"Bearer {self.key}")
        self.assertEqual(r.status_code, 401)

    def test_key_without_scope_403(self):
        key_sin_alcance = dispositivo.crear(self.conn, 1, "sin alcance", {"vida:leer"})
        r = self._post(b"{}", auth=f"Bearer {key_sin_alcance}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json(), {"error": "alcance"})

    def test_wrong_file_name_header_400(self):
        r = self._post(b"{}", auth=f"Bearer {self.key}", file_name="codigos.json")
        self.assertEqual(r.status_code, 400)

    def test_over_256kb_413(self):
        r = self._post(b"x" * (257 * 1024), auth=f"Bearer {self.key}")
        self.assertEqual(r.status_code, 413)

    def test_valid_file_proposes_a_batch_for_the_keys_person_only(self):
        body = (FIXTURES / "turnos-demo-a.json").read_bytes()
        r = self._post(body, auth=f"Bearer {self.key}")
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["turnos"], 4)
        self.assertTrue(out["nuevo"])
        self.assertIn("lote", out)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM shift_batch WHERE person_id = 1").fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM shift_batch WHERE person_id = 2").fetchone()[0], 0
        )

        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.assertEqual(self.client.get("/shifts").status_code, 200)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 2
        self.assertEqual(self.client.get("/shifts").status_code, 200)

    def test_the_same_file_again_is_not_new(self):
        body = (FIXTURES / "turnos-demo-a.json").read_bytes()
        r1 = self._post(body, auth=f"Bearer {self.key}")
        r2 = self._post(body, auth=f"Bearer {self.key}")
        self.assertTrue(r1.json()["nuevo"])
        self.assertFalse(r2.json()["nuevo"])
        self.assertEqual(r1.json()["lote"], r2.json()["lote"])

    def test_malformed_file_422_with_no_date_in_the_body(self):
        data = json.loads((FIXTURES / "turnos-demo-a.json").read_text())
        data["shifts"][0]["kind"] = "nope"
        r = self._post(json.dumps(data).encode(), auth=f"Bearer {self.key}")
        self.assertEqual(r.status_code, 422)
        out = r.json()
        self.assertEqual(out["error"], "archivo_rechazado")
        self.assertIn(out["motivo"], {"dia_malformado", "turno_duplicado", "esquema"})
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", r.text))


# ---- GET /api/vida/semana -------------------------------------------------------------------------

class ApiVidaSemanaTest(DispositivoHostedTestCase):
    def setUp(self):
        super().setUp()
        self.key_a = dispositivo.crear(self.conn, 1, "PC A", {"vida:leer"})
        self.key_b = dispositivo.crear(self.conn, 2, "PC B", {"vida:leer"})
        monday = vida_export.current_week_monday()
        db.set_metric(self.conn, monday.isoformat(), "resting_hr", 58.0, "garmin", 1)
        db.set_metric(self.conn, monday.isoformat(), "resting_hr", 70.0, "garmin", 2)
        self.conn.commit()

    def _metric(self, resp, key: str):
        return next(m for m in resp.json()["metrics"] if m["key"] == key)

    def test_scope_enforced(self):
        key_sin_alcance = dispositivo.crear(self.conn, 1, "sin alcance", {"turnos:subir"})
        r = self.client.get("/api/vida/semana", headers={"Authorization": f"Bearer {key_sin_alcance}"})
        self.assertEqual(r.status_code, 403)

    def test_returns_only_the_keys_own_person_section(self):
        r_a = self.client.get("/api/vida/semana", headers={"Authorization": f"Bearer {self.key_a}"})
        self.assertEqual(r_a.status_code, 200)
        self.assertEqual(self._metric(r_a, "body.resting_hr")["value"], 58.0)

        r_b = self.client.get("/api/vida/semana", headers={"Authorization": f"Bearer {self.key_b}"})
        self.assertEqual(r_b.status_code, 200)
        self.assertEqual(self._metric(r_b, "body.resting_hr")["value"], 70.0)

    def test_no_store_header(self):
        r = self.client.get("/api/vida/semana", headers={"Authorization": f"Bearer {self.key_a}"})
        self.assertEqual(r.headers.get("cache-control"), "no-store")


# ---- vida_export's hosted refusal -----------------------------------------------------------------

class VidaExportHostedRefusalTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "hosted.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)

    def test_main_refuses_and_writes_nothing(self):
        out_dir = config.BODY_HOME / "vida"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vida_export.main([])
        self.assertEqual(rc, 1)
        self.assertIn("hosted:", buf.getvalue())
        self.assertFalse(out_dir.exists())


# ---- request log privacy ---------------------------------------------------------------------------

class RequestLogPrivacyTest(DispositivoHostedTestCase):
    def test_no_authorization_value_or_body_in_the_log(self):
        key = dispositivo.crear(self.conn, 1, "PC de prueba", {"turnos:subir"})
        logger = logging.getLogger("body.request")
        handler = _CollectingHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old_level)

        self.client.post(
            "/api/turnos", content=b"not json, just a probe body",
            headers={"Authorization": f"Bearer {key}", "X-Body-Archivo": "turnos-x.json"},
        )

        self.assertTrue(handler.records)
        for line in handler.records:
            self.assertNotIn(key, line)
            self.assertNotIn("Bearer", line)
            self.assertNotIn("probe body", line)
        self.assertIn("/api/turnos", handler.records[-1])


if __name__ == "__main__":
    unittest.main()
