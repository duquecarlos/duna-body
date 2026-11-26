"""BODY-AVATAR-3D-VISOR-1: the mock's 3D viewer, ported onto Hoy's body card behind an opt-in
2D/3D switch -- 2D stays the default until the real anatomy mesh exists.

BODY-AVATAR-3D-OCULTAR-1: Carlos rejected the stand-in figure, so the switch is hidden behind
`body.rutas.hoy.CUERPO3D_VISIBLE` (False by default). Tests that assert the 3D markup patch that
constant to True rather than being deleted -- the real-mesh slice flips it back on for good."""
import json
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import anatomia, config, db, garmin_adapter, identity, seguridad, web
from body.rutas import comun, hoy as hoy_rutas

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
            self.assertEqual(set(textos), {"entrenado", "pendiente", "limitada", "sin_registro",
                                            "sin_datos", "subentrenado", "en_rango", "sobrecargado",
                                            "recuperando"})
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
        with patch.object(hoy_rutas, "CUERPO3D_VISIBLE", True):
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
        with patch.object(hoy_rutas, "CUERPO3D_VISIBLE", True):
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


def _actividad_con_set(conn, person_id: int, source_id: str, day: str, categoria: str) -> None:
    """One strength activity on `day` with one active set of `categoria` -- the same shape
    tests/test_musculo.py's own _actividad_con_sets uses, reproduced here since neither test
    module imports the other."""
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', NULL, 'garmin')",
        (person_id, source_id, day, f"{day}T07:00:00"))
    conn.commit()
    db.save_exercise_sets(conn, person_id, "garmin", source_id, [garmin_adapter.ExerciseSet(
        set_type="active", category=categoria, exercise="press", reps=8.0, weight_value=50.0,
        weight_unit="kg", duration_s=30.0, start_local=f"{day}T07:00:00")])
    conn.commit()


def _plan_hoy(conn, person_id: int, day: str, categoria: str) -> None:
    conn.execute(
        "INSERT INTO plan (person_id, day, title, status, ejercicios_json) VALUES (?, ?, 'Plan', "
        "'accepted', ?)",
        (person_id, day, json.dumps([{"ejercicio": "x", "series": 3, "categoria": categoria}])))
    conn.commit()


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
        from body import fechas, musculo
        today_local = fechas.hoy_local(db.person(self.conn, pid))
        self.conn.execute(
            "INSERT INTO plan (person_id, day, title, status, ejercicios_json) VALUES (?, ?, 'Upper B', "
            "'accepted', ?)",
            (pid, today_local.isoformat(),
             json.dumps([{"ejercicio": "press militar", "series": 3, "categoria": "hombro"}])))
        self.conn.commit()
        _use(pid)
        with patch.object(hoy_rutas, "CUERPO3D_VISIBLE", True):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        regiones = _bloque_json(r.text, "cuerpo3d-regiones")
        objetivo = _bloque_json(r.text, "cuerpo3d-objetivo")
        nombres = _bloque_json(r.text, "cuerpo3d-nombres")
        estados_texto = _bloque_json(r.text, "cuerpo3d-estados-texto")
        estados_modelo = {clave: estado.estado for clave, estado in
                           musculo.compute(self.conn, today_local.isoformat(), pid).items()}
        self.assertEqual(regiones, anatomia.regiones_estado(estados_modelo=estados_modelo))
        self.assertIn("hombros", objetivo)
        self.assertEqual(set(nombres), {rg["clave"] for rg in anatomia.REGIONES})
        self.assertEqual(set(estados_texto), {"entrenado", "pendiente", "limitada", "sin_registro",
                                                "sin_datos", "subentrenado", "en_rango", "sobrecargado",
                                                "recuperando"})

    def test_no_training_no_plan_no_lesion_renders_regiones_as_sin_datos_for_every_muscle(self):
        # A day with nothing logged, planned or recovering -- before BODY-CUERPO-ESTADO-MUSCULO-1
        # regiones_estado() returned {} here, and the template emitted the literal "{}" (the exact
        # defect this slice introduced and fixed, caught by tests/test_estados_vacios.py's own
        # sweep). Now the model's own `estados_modelo` entry gives every MUSCLE region "sin_datos"
        # (no organ key, organs are not this model's) -- so what the template must never leak on
        # this same empty day is `objetivo`'s own emptiness, a bare "[]", never the regiones one.
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        with patch.object(hoy_rutas, "CUERPO3D_VISIBLE", True):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        regiones = _bloque_json(r.text, "cuerpo3d-regiones")
        muscle_claves = {rg["clave"] for rg in anatomia.REGIONES if rg["tipo"] == "musculo"}
        self.assertEqual(set(regiones), muscle_claves)
        for estado in regiones.values():
            self.assertEqual(estado, "sin_datos")
        self.assertIsNone(_bloque_json(r.text, "cuerpo3d-objetivo"))
        self.assertNotIn("{}", r.text)
        self.assertNotIn("}}", r.text)
        self.assertNotIn("[]", r.text)


class ObjetivoLayerTest(unittest.TestCase):
    """BODY-CUERPO-ESTADO-MUSCULO-1 ruling 5: `objetivo` (the plan's own target layer) and
    `regiones` (the model's own state layer) never collapse into one word -- each names the same
    muscle for its own reason, and both can be true of it at once."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_a_planned_muscle_that_is_also_recovering_shows_both_layers(self):
        from body import fechas
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        today = fechas.hoy_local(db.person(self.conn, pid))
        _actividad_con_set(self.conn, pid, "a1", (today - timedelta(1)).isoformat(), "hombro")
        _plan_hoy(self.conn, pid, today.isoformat(), "hombro")
        _use(pid)
        with patch.object(hoy_rutas, "CUERPO3D_VISIBLE", True):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        regiones = _bloque_json(r.text, "cuerpo3d-regiones")
        objetivo = _bloque_json(r.text, "cuerpo3d-objetivo")
        self.assertEqual(regiones.get("hombros"), "recuperando")
        self.assertIn("hombros", objetivo)

    def test_an_open_lesion_zona_stays_limitada_while_still_a_plan_target(self):
        from body import fechas
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        today = fechas.hoy_local(db.person(self.conn, pid))
        db.crear_periodo(self.conn, pid, "lesion", today.isoformat(), zonas=["hombro"])
        self.conn.commit()
        _plan_hoy(self.conn, pid, today.isoformat(), "hombro")
        _use(pid)
        with patch.object(hoy_rutas, "CUERPO3D_VISIBLE", True):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        regiones = _bloque_json(r.text, "cuerpo3d-regiones")
        objetivo = _bloque_json(r.text, "cuerpo3d-objetivo")
        self.assertEqual(regiones.get("hombros"), "limitada")
        self.assertIn("hombros", objetivo)


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
    """Ruling 8: the stand-in-figure line renders in the person's own language, while the 3D
    switch is forced on -- BODY-AVATAR-3D-OCULTAR-1 hides it by default (see
    CuerpoVisibleDefaultOffTest below)."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_spanish_honesty_line_present(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        with patch.object(hoy_rutas, "CUERPO3D_VISIBLE", True):
            r = client.get("/")
        self.assertIn("Figura provisional: la anatomía real llega pronto.", r.text)


class CuerpoVisibleDefaultOffTest(unittest.TestCase):
    """BODY-AVATAR-3D-OCULTAR-1: Carlos rejected the stand-in figure (relayed verbatim from the
    Personal desk, 25 Nov), so with the default `CUERPO3D_VISIBLE = False` Hoy carries none of
    the 3D markup -- no switch, no canvas, no stand-in line, no visor.js tag, no regiones blob --
    and a device that had chosen 3D in localStorage sees 2D, since visor.js (the only code that
    ever reads `body.cuerpo3d`) is never loaded (ruling 2 / ruling 3 of this slice)."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_default_is_false(self):
        self.assertFalse(hoy_rutas.CUERPO3D_VISIBLE)

    def test_hoy_carries_no_3d_markup_by_default(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        for marker in ("cuerpo3d-seg", "cuerpo3d-track", "cuerpo3d-lienzo",
                       "avatar/visor.js", "avatar/avatar3d.js", "cuerpo3d-regiones"):
            self.assertNotIn(marker, r.text, marker)
        self.assertNotIn("Figura provisional: la anatomía real llega pronto.", r.text)
        self.assertNotIn("Stand-in figure: the real anatomy is coming.", r.text)

    def test_stand_in_figure_stays_unreachable_while_hidden(self):
        """Ruling 2: the figure that would name `figura-v0.json` (the stand-in) can never surface
        by accident -- it is only ever referenced while `CUERPO3D_VISIBLE` is False."""
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("figura-v0.json", r.text)
        self.assertFalse(hoy_rutas.CUERPO3D_VISIBLE)


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
