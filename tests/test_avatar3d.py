"""BODY-CUERPO-SPIN-HOY-1: the image spin on Hoy's body card, behind the SAME 2D/3D switch
BODY-AVATAR-3D-VISOR-1 first built -- Carlos approved this design (surface, quoted verbatim in
the ledger) after rejecting BODY-AVATAR-3D-OCULTAR-1's three.js stand-in, so the switch is visible
again (`body.rutas.hoy.CUERPO_GIRO_VISIBLE`, True by default -- one flag still hides it if a later
problem needs that). Every SUBJECT this module tested for the old stand-in is restored here for
the spin: the opt-in switch, nothing loaded until chosen, the Hoy-only blob -- never weakened.
"""
import json
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import anatomia, config, db, garmin_adapter, giro, identity, seguridad, web
from body.rutas import comun, hoy as hoy_rutas

client = TestClient(web.app, follow_redirects=False)

ROOT = Path(__file__).resolve().parent.parent
GIRO_DIR = ROOT / "body" / "static" / "giro"
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


def _bloque_json(html: str, clase: str):
    m = re.search(rf'<script type="application/json" class="{clase}">(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


def _actividad_con_set(conn, person_id: int, source_id: str, day: str, categoria: str) -> None:
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


class ArchivosCopiadosTest(unittest.TestCase):
    """Ruling 1/2: the removed stand-in's files are gone; the spin's own engine scripts and the
    112 art files (96 frames + 16 masks) are in body/static/giro/, served as their real type."""

    def test_the_old_stand_in_directory_is_gone(self):
        self.assertFalse(AVATAR_DIR.exists())

    def test_engine_scripts_exist_and_are_served_as_javascript(self):
        for rel in ("giro.js", "giro-interruptor.js"):
            path = ROOT / "body" / "static" / rel
            self.assertTrue(path.is_file(), rel)
            r = client.get(f"/static/{rel}")
            self.assertEqual(r.status_code, 200, rel)
            self.assertEqual(r.headers.get("content-type", "").split(";")[0], "text/javascript", rel)

    def test_every_frame_and_mask_is_served_with_its_type(self):
        checked = 0
        for capa in giro.CAPAS:
            for i in range(giro.N_VISTAS):
                r = client.get(f"/static/giro/{giro.nombre_cuadro(capa, i)}")
                self.assertEqual(r.status_code, 200, capa)
                self.assertEqual(r.headers.get("content-type", "").split(";")[0], "image/webp")
                checked += 1
        for i in range(giro.N_VISTAS):
            r = client.get(f"/static/giro/{giro.nombre_mascara(i)}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("content-type", "").split(";")[0], "image/png")
            checked += 1
        self.assertEqual(checked, 112)


class SwitchRendersOnHoyOnlyTest(unittest.TestCase):
    """Ruling 3: the 2D/3D switch (and the whole spin block) shows on Hoy's body card alone --
    Tendencias and Turnos build their own `contexto()` call and carry no `giro` key."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_hoy_carries_the_spin_block(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('cuerpo3d-seg', r.text)
        self.assertIn('cuerpo3d-track', r.text)
        self.assertIn('cuerpo3d-regiones', r.text)
        self.assertIn('giro-capas', r.text)
        self.assertIn('giro-escena', r.text)

    def test_trends_carries_no_spin_block(self):
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

    def test_shifts_carries_no_spin_block(self):
        pid = _mk_person(self.conn)
        db.set_trabaja_turnos(self.conn, True, pid)
        self.conn.commit()
        _use(pid)
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("cuerpo3d-seg", r.text)


class NadaCargaHasta3dTest(unittest.TestCase):
    """Ruling 3, restored for the spin: a 2D visit fetches no frame, no mask and no spin engine
    script -- only `giro-interruptor.js` (the small, always-loaded seam, same as the old visor.js
    before it) and the JSON blobs Hoy already emits."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_the_rendered_page_never_names_a_frame_or_mask_file_directly(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        # The frame/mask paths only ever appear INSIDE the two rutas-* JSON blobs, never as a
        # bare src="..." or data-*="..." attribute a 2D paint would actually fetch.
        sin_blobs = re.sub(
            r'<script type="application/json" class="cuerpo3d-rutas-(?:capas|mascaras)">.*?</script>',
            "", r.text, flags=re.S)
        self.assertNotIn("giro-00.webp", sin_blobs)
        self.assertNotIn("giro-regiones-00.png", sin_blobs)

    def test_only_the_tiny_seam_script_is_an_unconditional_src(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("giro-interruptor.js", r.text)
        self.assertNotIn('src="' + comun.static_url("giro.js"), r.text)

    def test_the_spin_track_names_the_engine_only_through_a_data_attribute(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'data-motor="{comun.static_url("giro.js")}"', r.text)


class DataAttributesTest(unittest.TestCase):
    """Ruling 3: URLs come from `static_url`, so the STATIC_HASH `?v=` applies -- never a
    hand-written path."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_engine_and_seam_scripts_carry_static_url_urls(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        hash_ = comun.STATIC_HASH
        self.assertIn(f'/static/giro-interruptor.js?v={hash_}', r.text)
        self.assertIn(f'/static/giro.js?v={hash_}', r.text)

    def test_every_frame_and_mask_path_in_the_blobs_carries_static_url_urls(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        hash_ = comun.STATIC_HASH
        capas = _bloque_json(r.text, "cuerpo3d-rutas-capas")
        mascaras = _bloque_json(r.text, "cuerpo3d-rutas-mascaras")
        self.assertEqual(set(capas), set(giro.CAPAS))
        for c in giro.CAPAS:
            self.assertEqual(len(capas[c]), giro.N_VISTAS)
            for url in capas[c]:
                self.assertTrue(url.startswith("/static/giro/"), url)
                self.assertTrue(url.endswith(f"?v={hash_}"), url)
        self.assertEqual(len(mascaras), giro.N_VISTAS)
        for url in mascaras:
            self.assertTrue(url.startswith("/static/giro/"), url)
            self.assertTrue(url.endswith(f"?v={hash_}"), url)


class Cuerpo3dJsonRoundtripsTest(unittest.TestCase):
    """Ruling 5/9: the inlined `estados` blob carries every GIRO_REGIONES name with a state --
    a model-tracked region carries musculo's own state, an untracked one `no_medido`, never
    `sin_datos` for the untracked ones. Six flat blobs, never one nested object (test_estados_
    vacios.py's own leak sweep flags a bare "{{"/"}}"/"{}"/"[]" anywhere in rendered HTML)."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_embedded_estados_matches_giro_construir_estados(self):
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
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        estados = _bloque_json(r.text, "cuerpo3d-regiones")
        colores = _bloque_json(r.text, "cuerpo3d-colores")
        nombres = _bloque_json(r.text, "cuerpo3d-nombres")
        estados_texto = _bloque_json(r.text, "cuerpo3d-estados-texto")
        objetivo = _bloque_json(r.text, "cuerpo3d-objetivo")
        vistas_texto = _bloque_json(r.text, "cuerpo3d-vistas")
        estados_modelo = {clave: estado.estado for clave, estado in
                           musculo.compute(self.conn, today_local.isoformat(), pid).items()}
        self.assertEqual(estados, giro.construir_estados(estados_modelo))
        self.assertEqual(colores, {k: (list(v) if v else None) for k, v in giro.COLORES.items()})
        self.assertEqual(set(nombres), {r for r in giro.GIRO_REGIONES if r is not None})
        self.assertEqual(set(estados_texto), {"en_rango", "recuperando", "subentrenado",
                                               "sobrecargado", "sin_datos", "no_medido"})
        self.assertIn("hombros", objetivo)
        self.assertEqual(len(vistas_texto), giro.N_VISTAS)

    def test_every_untracked_region_reads_no_medido_never_sin_datos(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        estados = _bloque_json(r.text, "cuerpo3d-regiones")
        for clave in ("triceps", "antebrazos", "oblicuos", "trapecio", "cuello", "lumbares",
                      "aductores", "gemelos", "tibiales"):
            self.assertEqual(estados[clave], "no_medido")

    def test_no_training_no_plan_no_lesion_still_renders_every_region_never_a_bare_literal(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        estados = _bloque_json(r.text, "cuerpo3d-regiones")
        self.assertEqual(set(estados), {rg for rg in giro.GIRO_REGIONES if rg is not None})
        for clave in ("pecho", "hombros", "biceps", "cuadriceps", "abdomen", "dorsales",
                      "espalda_alta", "deltoides_post", "gluteos", "isquios"):
            self.assertEqual(estados[clave], "sin_datos")
        self.assertIsNone(_bloque_json(r.text, "cuerpo3d-objetivo"))
        self.assertNotIn("{{", r.text)
        self.assertNotIn("}}", r.text)
        self.assertNotIn("{}", r.text)
        self.assertNotIn("[]", r.text)


class ObjetivoLayerTest(unittest.TestCase):
    """BODY-CUERPO-ESTADO-MUSCULO-1 ruling 5, restored: `objetivo` (the plan's own target layer)
    and `estados` (the model's own state layer) never collapse into one word."""

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
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        estados = _bloque_json(r.text, "cuerpo3d-regiones")
        objetivo = _bloque_json(r.text, "cuerpo3d-objetivo")
        self.assertEqual(estados.get("hombros"), "recuperando")
        self.assertIn("hombros", objetivo)

    def test_an_untracked_region_can_still_be_a_plan_target(self):
        """`objetivo` is independent of musculo's own model -- a categoria that maps to an
        untracked region (none do today, CATEGORIA_GRUPOS only ever names the 10 modelled ones)
        would still show as a target with state no_medido; proven here the other way round: a
        modelled region stays no_medido's own sibling state untouched by having a target too."""
        from body import fechas
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        today = fechas.hoy_local(db.person(self.conn, pid))
        _plan_hoy(self.conn, pid, today.isoformat(), "hombro")
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        estados = _bloque_json(r.text, "cuerpo3d-regiones")
        for clave in ("triceps", "antebrazos", "oblicuos"):
            self.assertEqual(estados[clave], "no_medido")


class I18nKeysExistTest(unittest.TestCase):
    """The new keys this slice adds exist in both languages, plus the reused switch keys."""

    _CLAVES = [
        "cuerpo.dibujo.aria", "cuerpo.dibujo.2d", "cuerpo.dibujo.3d", "cuerpo.detalle3d.inicial",
        "cuerpo.giro.capa.musculos", "cuerpo.giro.capa.esqueleto", "cuerpo.giro.capa.corazon",
        "cuerpo.giro.capa.pulmones", "cuerpo.giro.capa.nervios", "cuerpo.giro.capa.piel",
        "cuerpo.giro.vista.frente", "cuerpo.giro.vista.lado", "cuerpo.giro.vista.espalda",
        "cuerpo.giro.pista", "cuerpo.giro.cargando", "cuerpo.giro.aria", "cuerpo.giro.objetivo",
        "cuerpo.giro.estado.en_rango", "cuerpo.giro.estado.recuperando", "cuerpo.giro.estado.subentrenado",
        "cuerpo.giro.estado.sobrecargado", "cuerpo.giro.estado.sin_datos", "cuerpo.giro.estado.no_medido",
        "cuerpo.giro.region.triceps", "cuerpo.giro.region.antebrazos", "cuerpo.giro.region.oblicuos",
        "cuerpo.giro.region.trapecio", "cuerpo.giro.region.cuello", "cuerpo.giro.region.lumbares",
        "cuerpo.giro.region.aductores", "cuerpo.giro.region.gemelos", "cuerpo.giro.region.tibiales",
    ]

    def test_every_key_exists_in_both_languages(self):
        i18n_dir = ROOT / "body" / "i18n"
        es = json.loads((i18n_dir / "es.json").read_text(encoding="utf-8"))
        en = json.loads((i18n_dir / "en.json").read_text(encoding="utf-8"))
        for clave in self._CLAVES:
            self.assertIn(clave, es, clave)
            self.assertIn(clave, en, clave)


class CuerpoGiroVisibleDefaultOnTest(unittest.TestCase):
    """BODY-CUERPO-SPIN-HOY-1 ruling 1: the flag is renamed and now defaults True -- the switch
    this slice ships is visible by default, one byte away from hidden again if ever needed."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_default_is_true(self):
        self.assertTrue(hoy_rutas.CUERPO_GIRO_VISIBLE)

    def test_the_old_flag_name_is_gone(self):
        self.assertFalse(hasattr(hoy_rutas, "CUERPO3D_VISIBLE"))

    def test_flipping_the_flag_off_hides_every_spin_marker(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        with patch.object(hoy_rutas, "CUERPO_GIRO_VISIBLE", False):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        for marker in ("cuerpo3d-seg", "cuerpo3d-track", "giro-escena", "giro-interruptor.js",
                       "cuerpo3d-regiones"):
            self.assertNotIn(marker, r.text, marker)


class StandInGoneTest(unittest.TestCase):
    """Ruling 1: the three.js stand-in, its figure JSON and its honesty line are gone for good --
    never reachable, flag on or off."""

    def setUp(self):
        self.conn = _reset_db(self)

    def test_hoy_never_names_the_removed_figure_or_viewer_files(self):
        pid = _mk_person(self.conn)
        _algun_dato_hoy(self.conn, pid)
        _use(pid)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        for marker in ("figura-v0.json", "avatar3d.js", "avatar/visor.js",
                       "Figura provisional: la anatomía real llega pronto.",
                       "Stand-in figure: the real anatomy is coming."):
            self.assertNotIn(marker, r.text, marker)

    def test_etiquetas_regiones_and_estado3d_are_gone_from_anatomia(self):
        self.assertFalse(hasattr(anatomia, "etiquetas_regiones"))
        self.assertFalse(hasattr(anatomia, "etiquetas_estado3d"))


class CspUnchangedTest(unittest.TestCase):
    """Ruling 3: no CSP widening -- the exact header test_seguridad.py already pins."""

    def test_csp_string_is_unchanged(self):
        self.assertEqual(
            seguridad.content_security_policy(),
            "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )


if __name__ == "__main__":
    unittest.main()
