"""BODY-AVATAR-3D-VISOR-1: the mock's 3D viewer, ported onto Hoy's body card behind an opt-in
2D/3D switch -- 2D stays the default until the real anatomy mesh exists."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import anatomia, config, db, identity, seguridad, web
from body.rutas import comun

client = TestClient(web.app, follow_redirects=False)

ROOT = Path(__file__).resolve().parent.parent
AVATAR_DIR = ROOT / "body" / "static" / "avatar"


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "avatar3d.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)
    return conn


def _mk_person(conn) -> int:
    cur = conn.execute(
        "INSERT INTO person (display_name, timezone, language) VALUES (?, 'America/Bogota', 'es')", ("P",))
    conn.commit()
    return cur.lastrowid


def _use(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = (lambda pid=person_id: pid)


def _algun_dato_hoy(conn, pid: int) -> None:
    from body import fechas
    hoy = fechas.hoy_local(db.person(conn, pid)).isoformat()
    db.set_metric(conn, hoy, "resting_hr", 55.0, "garmin", pid)
    conn.commit()


class ArchivosCopiadosTest(unittest.TestCase):
    """Ruling 1: the mock's own files, copied in unchanged, served with the right type."""

    _ARCHIVOS = {
        "avatar3d.js": "text/javascript",
        "figura-v0.json": "application/json",
        "img/matcap-musculo-claro-512.webp": "image/webp",
        "img/matcap-piel-claro-512.webp": "image/webp",
        "img/matcap-musculo-oscuro-512.webp": "image/webp",
        "img/matcap-piel-oscuro-512.webp": "image/webp",
        "img/fibras-512.webp": "image/webp",
    }

    def test_every_file_exists_on_disk(self):
        for rel in self._ARCHIVOS:
            self.assertTrue((AVATAR_DIR / rel).is_file(), f"missing {rel}")

    def test_every_file_is_served_with_its_type(self):
        checked = 0
        for rel, media_type in self._ARCHIVOS.items():
            r = client.get(f"/static/avatar/{rel}")
            self.assertEqual(r.status_code, 200, rel)
            self.assertEqual(r.headers.get("content-type", "").split(";")[0], media_type, rel)
            checked += 1
        self.assertEqual(checked, len(self._ARCHIVOS))

    def test_visor_js_exists_and_is_served_as_javascript(self):
        self.assertTrue((AVATAR_DIR / "visor.js").is_file())
        r = client.get("/static/avatar/visor.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("content-type", "").split(";")[0], "text/javascript")


class FiguraRegionesTest(unittest.TestCase):
    """Ruling 5 / the spec's own test 2: one vocabulary, enforced -- figura-v0.json's own
    `regiones` list is exactly `anatomia.REGIONES`'s claves, with `otro` at index 0."""

    def test_regiones_list_matches_anatomia_regiones_plus_otro(self):
        fig = json.loads((AVATAR_DIR / "figura-v0.json").read_text(encoding="utf-8"))
        self.assertEqual(fig["regiones"], ["otro"] + [r["clave"] for r in anatomia.REGIONES])

    def test_figura_json_parses_and_has_a_grid_and_a_box(self):
        fig = json.loads((AVATAR_DIR / "figura-v0.json").read_text(encoding="utf-8"))
        self.assertIn("rejilla_m", fig)
        self.assertIn("caja", fig)
        self.assertIn("musculos", fig)
        self.assertIn("organos", fig)


class EtiquetasTest(unittest.TestCase):
    """Ruling 5: region names come from the existing `cuerpo.grupo.<clave>` keys; corazon/cerebro
    get their own two entries since no 2D caption ever needed them before."""

    def test_every_region_has_a_label_in_spanish(self):
        from body.i18n import T
        t = T("es")
        etiquetas = anatomia.etiquetas_regiones(t)
        self.assertEqual(set(etiquetas), {r["clave"] for r in anatomia.REGIONES})
        for clave, texto in etiquetas.items():
            self.assertTrue(texto, clave)

    def test_every_state_has_a_word_in_both_languages(self):
        from body.i18n import T
        for lang in ("es", "en"):
            t = T(lang)
            textos = anatomia.etiquetas_estado3d(t)
            self.assertEqual(set(textos), {"entrenado", "pendiente", "limitada", "sin_registro"})
            for estado, texto in textos.items():
                self.assertTrue(texto, estado)


class SwitchRendersOnHoyOnlyTest(unittest.TestCase):
    """Ruling 3: the 2D/3D switch (and the whole 3D block) shows on Hoy's body card alone --
    Tendencias and Turnos build their own `contexto()` call and carry no `cuerpo3d` key."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_hoy_carries_the_3d_block(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('cuerpo3d-seg', r.text)
        self.assertIn('cuerpo3d-track', r.text)
        self.assertIn('cuerpo3d-regiones', r.text)
        self.assertIn('cuerpo3d-nombres', r.text)
        self.assertIn('cuerpo3d-estados-texto', r.text)

    def test_trends_carries_no_3d_block(self):
        pid = _mk_person(self.conn)
        from body import fechas
        today = fechas.hoy_local(db.person(self.conn, pid))
        for i in range(20):
            d = (today - __import__("datetime").timedelta(i)).isoformat()
            db.set_metric(self.conn, d, "resting_hr", 55.0, "garmin", pid)
        self.conn.commit()
        _use(pid)
        r = client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("cuerpo3d-seg", r.text)

    def test_shifts_carries_no_3d_block(self):
        pid = _mk_person(self.conn)
        db.set_trabaja_turnos(self.conn, True, pid)
        self.conn.commit()
        _use(pid)
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("cuerpo3d-seg", r.text)


class DataAttributesTest(unittest.TestCase):
    """Ruling 3: URLs come from `static_url`, so the STATIC_HASH `?v=` applies -- never a
    hand-written path."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_canvas_data_attributes_carry_static_url_urls(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        hash_ = comun.STATIC_HASH
        for rel in ("avatar/avatar3d.js", "avatar/figura-v0.json",
                    "avatar/img/matcap-musculo-claro-512.webp", "avatar/img/matcap-piel-claro-512.webp",
                    "avatar/img/matcap-musculo-oscuro-512.webp", "avatar/img/matcap-piel-oscuro-512.webp",
                    "avatar/img/fibras-512.webp"):
            self.assertIn(f"/static/{rel}?v={hash_}", r.text, rel)
        self.assertIn(f'/static/avatar/visor.js?v={hash_}', r.text)


def _bloque_json(html: str, clase: str):
    m = re.search(rf'<script type="application/json" class="{clase}">(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


class Cuerpo3dJsonRoundtripsTest(unittest.TestCase):
    """Ruling 5: the inlined `regiones` blob round-trips -- what the template embedded is exactly
    what `anatomia.regiones_estado` computed, never a second, divergent read. Three flat blobs,
    never one nested object (test_estados_vacios.py's own leak sweep flags a bare "{{"/"}}"/"{}"/
    "[]" anywhere in rendered HTML, which a nested object's own closing braces would trip)."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_embedded_json_matches_the_screens_own_regiones_map(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        from body import fechas
        self.conn.execute(
            "INSERT INTO plan (person_id, day, title, status, ejercicios_json) VALUES (?, ?, 'Upper B', "
            "'accepted', ?)",
            (pid, fechas.hoy_local(db.person(self.conn, pid)).isoformat(),
             json.dumps([{"ejercicio": "press militar", "series": 3, "categoria": "hombro"}])))
        self.conn.commit()
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        regiones = _bloque_json(r.text, "cuerpo3d-regiones")
        nombres = _bloque_json(r.text, "cuerpo3d-nombres")
        estados_texto = _bloque_json(r.text, "cuerpo3d-estados-texto")
        self.assertIsNotNone(regiones)
        self.assertEqual(regiones.get("hombros"), "pendiente")
        self.assertEqual(set(nombres), {rg["clave"] for rg in anatomia.REGIONES})
        self.assertEqual(set(estados_texto), {"entrenado", "pendiente", "limitada", "sin_registro"})

    def test_no_training_no_plan_no_lesion_renders_regiones_as_null_not_an_empty_object(self):
        # A day with nothing logged, planned or recovering -- regiones_estado() returns {}, and
        # the template must never emit the literal "{}" (or "}}") into the page (the exact defect
        # this slice introduced and fixed, caught by tests/test_estados_vacios.py's own sweep).
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(_bloque_json(r.text, "cuerpo3d-regiones"))
        self.assertNotIn("{}", r.text)
        self.assertNotIn("}}", r.text)
        self.assertNotIn("[]", r.text)


class I18nKeysExistTest(unittest.TestCase):
    """The new keys this slice adds exist in both languages."""

    _CLAVES = [
        "cuerpo.grupo.corazon", "cuerpo.grupo.cerebro", "cuerpo.dibujo.aria", "cuerpo.dibujo.2d",
        "cuerpo.dibujo.3d", "cuerpo.dibujo.pista", "cuerpo.aria3d.lienzo", "cuerpo.detalle3d.inicial",
        "cuerpo.estado3d.entrenado", "cuerpo.estado3d.pendiente", "cuerpo.estado3d.limitada",
        "cuerpo.estado3d.sin_registro", "cuerpo.aviso3d.figura_provisional",
    ]

    def test_every_new_key_exists_in_both_languages(self):
        i18n_dir = ROOT / "body" / "i18n"
        es = json.loads((i18n_dir / "es.json").read_text(encoding="utf-8"))
        en = json.loads((i18n_dir / "en.json").read_text(encoding="utf-8"))
        for clave in self._CLAVES:
            self.assertIn(clave, es, clave)
            self.assertIn(clave, en, clave)


class HonestyLineTest(unittest.TestCase):
    """Ruling 8: the stand-in-figure line renders in the person's own language."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_spanish_honesty_line_present(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertIn("Figura provisional: la anatomía real llega pronto.", r.text)


class CspUnchangedTest(unittest.TestCase):
    """Ruling 2: no CSP widening -- the exact header test_seguridad.py already pins."""

    def test_csp_string_is_unchanged(self):
        self.assertEqual(
            seguridad.content_security_policy(),
            "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )


if __name__ == "__main__":
    unittest.main()
