"""BODY-ETIQUETA-FOTO-1 (ORCH-CARLOS-DELEGATES-TIER-1-1, Carlos's own yes to proposal 25): a photo
of a food label fills "New product" on /comida/productos. Demo provider and fakes only -- no
network, no real key, no real figure.
"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas
from body.rutas import comida

client = TestClient(web.app, follow_redirects=False)

_PROVIDER_ENV_VARS = ("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_MODELO", "BODY_AGENTE_ANTHROPIC_API_KEY",
                      "BODY_AGENTE_OPENAI_API_KEY", "BODY_AGENTE_GEMINI_API_KEY")


def _borrar_env(*nombres):
    guardado = {n: os.environ.pop(n, None) for n in nombres}

    def _restaurar():
        for n, v in guardado.items():
            if v is not None:
                os.environ[n] = v
            else:
                os.environ.pop(n, None)
    return _restaurar


def _reset_web_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    tc.addCleanup(_borrar_env(*_PROVIDER_ENV_VARS))
    for n in _PROVIDER_ENV_VARS:
        os.environ.pop(n, None)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "etiqueta-web.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    conn.commit()
    web.app.dependency_overrides[identity.current_person_id] = lambda: 1
    return conn


def _conteos(conn) -> tuple[int, int]:
    conv = conn.execute("SELECT COUNT(*) AS n FROM conversacion_agente").fetchone()["n"]
    prop = conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente").fetchone()["n"]
    return conv, prop


_JPEG_HEADERS = {"Content-Type": "image/jpeg"}


class ByteCeilingTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_web_db(self)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"

    def test_exactly_at_the_ceiling_is_accepted(self):
        r = client.post("/comida/productos/etiqueta", content=b"x" * comida.MAX_ETIQUETA_BYTES,
                         headers=_JPEG_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_one_over_the_ceiling_is_refused_and_writes_nothing(self):
        before = _conteos(self.conn)
        r = client.post("/comida/productos/etiqueta", content=b"x" * (comida.MAX_ETIQUETA_BYTES + 1),
                         headers=_JPEG_HEADERS)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json(), {"ok": False, "error": "muy_grande"})
        self.assertEqual(_conteos(self.conn), before)


class ContentTypeTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_web_db(self)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"

    def test_a_non_jpeg_content_type_is_refused(self):
        before = _conteos(self.conn)
        r = client.post("/comida/productos/etiqueta", content=b"not a jpeg",
                         headers={"Content-Type": "image/png"})
        self.assertEqual(r.status_code, 415)
        self.assertEqual(r.json(), {"ok": False, "error": "no_imagen"})
        self.assertEqual(_conteos(self.conn), before)


class NadaEscritoEnDiscoTest(unittest.TestCase):
    """Ruling 3/8: the photo's own bytes never touch disk -- neither on a refusal nor on a
    successful read, measured against a directory this test owns (nothing in this route ever
    writes there, so any new file would mean a leak) and against the proposal's own stored
    despues_json (structured fields only, never the image bytes)."""

    def setUp(self):
        self.conn = _reset_web_db(self)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"
        self.vigilado = Path(tempfile.mkdtemp(dir=TMP))

    def _listado(self) -> set:
        return set(self.vigilado.iterdir())

    def test_refused_upload_leaves_the_watched_directory_untouched(self):
        antes = self._listado()
        client.post("/comida/productos/etiqueta", content=b"x" * (comida.MAX_ETIQUETA_BYTES + 1),
                     headers=_JPEG_HEADERS)
        self.assertEqual(self._listado(), antes)

    def test_successful_upload_stores_only_structured_fields_never_image_bytes(self):
        antes = self._listado()
        body = b"\xff\xd8\xff" + b"x" * 1000
        r = client.post("/comida/productos/etiqueta", content=body, headers=_JPEG_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._listado(), antes)
        propuesta_id = r.json()["propuesta_id"]
        row = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()
        self.assertNotIn(base64.b64encode(body).decode("ascii"), row["despues_json"])


class AsistenteApagadoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_web_db(self)  # no provider configured at all

    def test_upload_refuses_409_and_writes_nothing(self):
        before = _conteos(self.conn)
        r = client.post("/comida/productos/etiqueta", content=b"x" * 1000, headers=_JPEG_HEADERS)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json(), {"ok": False, "error": "sin_asistente"})
        self.assertEqual(_conteos(self.conn), before)

    def test_picker_explains_instead_of_offering_the_button(self):
        r = client.get("/comida/productos")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('id="etiqueta-input"', r.text)
        self.assertIn("no se puede leer una foto de etiqueta", r.text)
        self.assertIn("Ajustes", r.text)


class AvisoYTarjetaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_web_db(self)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"

    def test_the_privacy_notice_shows_every_time_the_assistant_is_available(self):
        r = client.get("/comida/productos")
        self.assertIn("Body guarda solo las cifras que confirmes, nunca la foto", r.text)
        self.assertIn('id="etiqueta-input"', r.text)

    def test_confirm_card_shows_every_field_and_leaves_an_unread_one_empty(self):
        cid = agente_conversacion.crear(self.conn, 1)
        out = herramientas.ejecutar(
            self.conn, 1, "crear_producto",
            {"nombre": "Galleta leída", "tamano_unidad": "g", "nutrientes": {"kcal": 120}},
            "ninguno", cid)
        propuesta_id = out["propuesta_id"]
        r = client.get("/comida/productos", params={"propuesta_id": propuesta_id})
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="Galleta leída"', r.text)
        self.assertIn('name="marca" class="field mb-12" aria-label="Marca (opcional)" value=""', r.text)
        self.assertIn('name="valor_kcal" class="field mb-12" aria-label="Calorías" value="120"', r.text)
        self.assertIn('name="valor_protein_g" class="field mb-12" aria-label="Proteína" value=""', r.text)


class FlujoCompletoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_web_db(self)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"

    def _subir(self) -> int:
        r = client.post("/comida/productos/etiqueta", content=b"x" * 1000, headers=_JPEG_HEADERS)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["campos"]["nombre"], "Galleta de avena (demo)")
        return data["propuesta_id"]

    def test_upload_creates_a_pending_proposal_from_the_demo_fixture(self):
        propuesta_id = self._subir()
        row = self.conn.execute("SELECT accion, estado FROM propuesta_agente WHERE id = ?",
                                 (propuesta_id,)).fetchone()
        self.assertEqual((row["accion"], row["estado"]), ("crear_producto", "propuesta"))

    def test_confirmar_applies_the_persons_own_edited_fields(self):
        propuesta_id = self._subir()
        r = client.post(f"/comida/productos/etiqueta/{propuesta_id}/confirmar",
                         data={"nombre": "Nombre editado a mano"})
        self.assertEqual(r.status_code, 303)
        self.assertIn(f"aplicado_id={propuesta_id}", r.headers["location"])
        fila = self.conn.execute("SELECT id FROM producto WHERE nombre = 'Nombre editado a mano'").fetchone()
        self.assertIsNotNone(fila)
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM producto WHERE nombre = 'Galleta de avena (demo)'").fetchone())
        estado = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                    (propuesta_id,)).fetchone()["estado"]
        self.assertEqual(estado, "aplicada")

    def test_descartar_leaves_no_product_and_marks_the_proposal_discarded(self):
        propuesta_id = self._subir()
        r = client.post(f"/comida/productos/etiqueta/{propuesta_id}/descartar", data={})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM producto").fetchone()[0], 0)
        estado = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                    (propuesta_id,)).fetchone()["estado"]
        self.assertEqual(estado, "descartada")

    def test_deshacer_after_confirmar_removes_the_product(self):
        propuesta_id = self._subir()
        client.post(f"/comida/productos/etiqueta/{propuesta_id}/confirmar", data={"nombre": "Para deshacer"})
        self.assertIsNotNone(
            self.conn.execute("SELECT id FROM producto WHERE nombre = 'Para deshacer'").fetchone())
        r = client.post(f"/comida/productos/etiqueta/{propuesta_id}/deshacer", data={})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(
            self.conn.execute("SELECT id FROM producto WHERE nombre = 'Para deshacer'").fetchone())
        estado = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                    (propuesta_id,)).fetchone()["estado"]
        self.assertEqual(estado, "deshecha")

    def test_applied_banner_offers_undo_on_the_picker(self):
        propuesta_id = self._subir()
        client.post(f"/comida/productos/etiqueta/{propuesta_id}/confirmar",
                     data={"nombre": "Con bandera", "linea": [], "q": ""})
        r = client.get("/comida/productos", params={"aplicado_id": propuesta_id})
        self.assertIn("Creamos el producto a partir de la foto", r.text)
        self.assertIn(f"/comida/productos/etiqueta/{propuesta_id}/deshacer", r.text)


class CsrfTest(unittest.TestCase):
    """Ruling 3: CSRF-protected exactly as the app's other POST routes are -- the SAME same-origin
    check every unsafe-method route gets (body/seguridad.py's SeguridadMiddleware), proven directly
    here rather than only through tests/test_seguridad.py's own whole-app sweep."""

    def setUp(self):
        self.conn = _reset_web_db(self)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"

    def test_a_foreign_origin_is_refused_before_the_route_runs(self):
        before = _conteos(self.conn)
        r = client.post("/comida/productos/etiqueta", content=b"x" * 1000, headers={
            **_JPEG_HEADERS, "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(_conteos(self.conn), before)


if __name__ == "__main__":
    unittest.main()
