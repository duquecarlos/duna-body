"""BODY-TUTORIALES-2: tutoriales-v1 step 4's four doors -- Ayuda, the in-context "¿Cómo se hace?"
door, Primeros pasos on Hoy, and the assistant's leer_tutoriales. No network: every manifest fetch
below is patched. Invented demo data only, no real health figures.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, dispositivo, identity, privacidad, tutoriales, web
from body.agente import herramientas

MANIFEST_BASE = "https://media.example.test"


def _fresh_client() -> tuple[TestClient, "sqlite3.Connection"]:
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
    conn = db.connect()
    db.init(conn)
    return TestClient(web.app, follow_redirects=False), conn


class _LocalModeTestCase(unittest.TestCase):
    """Local mode (no Cuenta sign-in configured): the default person, no gates."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(tutoriales._manifest_cache.clear)
        self.client, self.conn = _fresh_client()
        self.assertNotIn(cuenta.ISSUER_ENV, os.environ)


class _HostedModeTestCase(unittest.TestCase):
    """Hosted mode, person resolved by overriding the dependency directly (BODY-DISPOSITIVO-1's own
    test convention) -- no OIDC dance needed for routes that only care about the person id."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(tutoriales._manifest_cache.clear)
        self.client, self.conn = _fresh_client()
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1


# ---- manifest(): env unset, bad shape, fetch error, cache, the URL it fetches --------------------

class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(tutoriales._manifest_cache.clear)

    def test_env_unset_is_empty(self):
        self.assertNotIn("BODY_TUTORIALES_BASE_URL", os.environ)
        self.assertEqual(tutoriales.manifest(), {})

    def test_fetch_raising_is_empty(self):
        with mock.patch.dict(os.environ, {"BODY_TUTORIALES_BASE_URL": MANIFEST_BASE}):
            with mock.patch.object(tutoriales, "_fetch_remota", side_effect=RuntimeError("down")):
                self.assertEqual(tutoriales.manifest(), {})

    def test_bad_shape_is_empty(self):
        with mock.patch.dict(os.environ, {"BODY_TUTORIALES_BASE_URL": MANIFEST_BASE}):
            with mock.patch.object(tutoriales, "_fetch_remota", return_value=["not", "a", "dict"]):
                self.assertEqual(tutoriales.manifest(), {})

    def test_url_is_base_plus_fixed_suffix_never_person_data(self):
        seen = []

        def _fake(url):
            seen.append(url)
            return {"version": 1, "producto": "body", "tareas": {}}

        with mock.patch.dict(os.environ, {"BODY_TUTORIALES_BASE_URL": MANIFEST_BASE}):
            with mock.patch.object(tutoriales, "_fetch_remota", side_effect=_fake):
                tutoriales.manifest()
        self.assertEqual(seen, [f"{MANIFEST_BASE}/body/manifest.json"])

    def test_cached_within_ten_minutes(self):
        calls = {"n": 0}

        def _fake(url):
            calls["n"] += 1
            return {"version": 1, "producto": "body", "tareas": {}}

        with mock.patch.dict(os.environ, {"BODY_TUTORIALES_BASE_URL": MANIFEST_BASE}):
            with mock.patch.object(tutoriales, "_fetch_remota", side_effect=_fake):
                tutoriales.manifest()
                tutoriales.manifest()
        self.assertEqual(calls["n"], 1)


# ---- ir_a_pantalla(): a dynamic pantalla with no pantalla_lista gets no link ----------------------

class IrAPantallaTest(unittest.TestCase):
    def test_dynamic_without_lista_is_none(self):
        self.assertIsNone(tutoriales.ir_a_pantalla({"pantalla": "/day/{day}"}))

    def test_dynamic_with_lista_uses_it(self):
        self.assertEqual(tutoriales.ir_a_pantalla({"pantalla": "/day/{day}", "pantalla_lista": "/"}), "/")

    def test_static_uses_itself(self):
        self.assertEqual(tutoriales.ir_a_pantalla({"pantalla": "/trends"}), "/trends")

    def test_neither_is_none(self):
        self.assertIsNone(tutoriales.ir_a_pantalla({}))


# ---- /ayuda: every chapter and task, video vs "en preparación" -----------------------------------

class AyudaRendersTest(_LocalModeTestCase):
    def test_every_chapter_and_task_present(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BODY_TUTORIALES_BASE_URL", None)
            r = self.client.get("/ayuda")
        self.assertEqual(r.status_code, 200)
        data = tutoriales.catalogo()
        for c in data["capitulos"]:
            self.assertIn(c["titulo"], r.text)
        for t in data["tareas"]:
            self.assertIn(t["titulo"], r.text)
        self.assertIn("Video en preparación", r.text)

    def test_task_in_manifest_shows_video_with_track(self):
        manifest = {
            "version": 1, "producto": "body",
            "tareas": {
                "crear-una-meta": {
                    "duracion_s": 42, "horizontal": "https://media.example.test/crear-una-meta.mp4",
                    "vertical": "https://media.example.test/crear-una-meta-v.mp4",
                    "subtitulos": "https://media.example.test/crear-una-meta.vtt",
                    "poster": "https://media.example.test/crear-una-meta.jpg",
                    "grabado_en": "deadbeef", "voz": "ninguna",
                },
            },
        }
        with mock.patch.dict(os.environ, {"BODY_TUTORIALES_BASE_URL": MANIFEST_BASE}):
            with mock.patch.object(tutoriales, "_fetch_remota", return_value=manifest):
                r = self.client.get("/ayuda")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<video controls preload="none"', r.text)
        self.assertIn("crear-una-meta.mp4", r.text)
        self.assertIn("crear-una-meta-v.mp4", r.text)
        self.assertIn('<track kind="subtitles"', r.text)
        self.assertIn("crear-una-meta.vtt", r.text)
        # a task with no manifest entry still shows "Video en preparación" on the very same page
        self.assertIn("Video en preparación", r.text)

    def test_env_unset_or_fetch_raising_still_200_with_every_task(self):
        data = tutoriales.catalogo()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BODY_TUTORIALES_BASE_URL", None)
            r1 = self.client.get("/ayuda")
        self.assertEqual(r1.status_code, 200)
        for t in data["tareas"]:
            self.assertIn(t["titulo"], r1.text)

        with mock.patch.dict(os.environ, {"BODY_TUTORIALES_BASE_URL": MANIFEST_BASE}):
            with mock.patch.object(tutoriales, "_fetch_remota", side_effect=RuntimeError("down")):
                tutoriales._manifest_cache.clear()
                r2 = self.client.get("/ayuda")
        self.assertEqual(r2.status_code, 200)
        for t in data["tareas"]:
            self.assertIn(t["titulo"], r2.text)


# ---- in-context "¿Cómo se hace?" door --------------------------------------------------------------

class InContextDoorTest(_LocalModeTestCase):
    def test_trends_links_its_first_task(self):
        r = self.client.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/ayuda#comparar-una-metrica-con-tu-base"', r.text)

    def test_goals_new_links_its_task(self):
        r = self.client.get("/goals/new")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/ayuda#crear-una-meta"', r.text)

    def test_goals_list_has_no_door(self):
        r = self.client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("ayuda-ctx", r.text)


# ---- Primeros pasos: the tick is read, never stored -----------------------------------------------

class PrimerosPasosFunctionTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
        self.conn = db.connect()
        db.init(self.conn)

    def test_hosted_with_nothing_is_all_unticked(self):
        items = tutoriales.primeros_pasos(self.conn, 1, hosted=True)
        self.assertEqual({i["id"] for i in items},
                         {"iniciar-sesion-y-aceptar", "conectar-garmin", "vincular-este-pc", "primera-mirada-a-hoy"})
        self.assertFalse(any(i["hecho"] for i in items))

    def test_hosted_with_consent_metric_and_device_is_all_done(self):
        privacidad.record_consent(self.conn, 1)
        db.set_metric(self.conn, "2026-01-01", "resting_hr", 58, "garmin", 1)
        dispositivo.crear(self.conn, 1, "Mi PC", {"turnos:subir"})
        items = tutoriales.primeros_pasos(self.conn, 1, hosted=True)
        self.assertTrue(all(i["hecho"] for i in items))

    def test_local_mode_hides_the_pc_item(self):
        items = tutoriales.primeros_pasos(self.conn, 1, hosted=False)
        self.assertEqual({i["id"] for i in items},
                         {"iniciar-sesion-y-aceptar", "conectar-garmin", "primera-mirada-a-hoy"})

    def test_local_mode_iniciar_sesion_is_always_done(self):
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=False)}
        self.assertTrue(items["iniciar-sesion-y-aceptar"])

    def test_local_mode_primera_mirada_follows_conectar_garmin(self):
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=False)}
        self.assertFalse(items["primera-mirada-a-hoy"])
        db.set_metric(self.conn, "2026-01-01", "resting_hr", 58, "garmin", 1)
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=False)}
        self.assertTrue(items["primera-mirada-a-hoy"])


class PrimerosPasosOnHoyTest(_HostedModeTestCase):
    def _seed_one_day(self):
        for key in ("hrv_last_night", "sleep_minutes", "resting_hr", "body_battery_wake"):
            db.set_metric(self.conn, "2026-01-01", key, 60, "garmin", 1)
        self.conn.commit()

    def test_card_shows_with_unticked_items_when_something_is_missing(self):
        self._seed_one_day()  # conectar-garmin done, but no consent and no device yet
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Primeros pasos", r.text)
        self.assertIn('href="/ayuda#iniciar-sesion-y-aceptar"', r.text)

    def test_card_disappears_once_everything_is_done(self):
        self._seed_one_day()
        privacidad.record_consent(self.conn, 1)
        dispositivo.crear(self.conn, 1, "Mi PC", {"turnos:subir"})
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Primeros pasos", r.text)


class PrimerosPasosOnNoDataHoyTest(_HostedModeTestCase):
    """A brand-new person's first visit: today() returns soon.html (no day yet). The checklist must
    show there too, not only once there is a day to show (measured: a scratch Body with no watch
    data showed no card)."""

    def test_card_shows_on_the_no_data_hoy(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Primeros pasos", r.text)
        self.assertIn('href="/ayuda#iniciar-sesion-y-aceptar"', r.text)


# ---- SOLO_EN_LINEA: routes that only exist hosted get no "Ir a la pantalla" link locally -----------

class SoloEnLineaRoutesTest(_LocalModeTestCase):
    def test_each_route_404s_locally(self):
        for route in tutoriales.SOLO_EN_LINEA:
            r = self.client.get(route)
            self.assertEqual(r.status_code, 404, route)

    def test_ayuda_shows_the_note_not_the_link(self):
        r = self.client.get("/ayuda")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Solo en Body en línea", r.text)
        for route in tutoriales.SOLO_EN_LINEA:
            self.assertNotIn(f'href="{route}"', r.text)


class SoloEnLineaRoutesHostedTest(_HostedModeTestCase):
    def test_each_route_does_not_404_hosted(self):
        for route in tutoriales.SOLO_EN_LINEA:
            r = self.client.get(route)
            self.assertNotEqual(r.status_code, 404, route)

    def test_ayuda_shows_the_link_not_the_note(self):
        r = self.client.get("/ayuda")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Solo en Body en línea", r.text)
        for route in tutoriales.SOLO_EN_LINEA:
            self.assertIn(f'href="{route}"', r.text)


# ---- the assistant's leer_tutoriales ---------------------------------------------------------------

class LeerTutorialesToolTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
        self.conn = db.connect()
        db.init(self.conn)
        self.addCleanup(tutoriales._manifest_cache.clear)

    def test_unfiltered_is_capped_at_five(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_tutoriales", {}, "completo")
        self.assertGreater(len(tutoriales.catalogo()["tareas"]), 5)
        self.assertEqual(len(out["tareas"]), 5)

    def test_consulta_filters_by_title_or_chapter_case_insensitively(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_tutoriales", {"consulta": "TURNO"}, "completo")
        ids = {t["id"] for t in out["tareas"]}
        self.assertEqual(ids, {"confirmar-los-turnos-propuestos", "leer-lo-que-cuesta-una-semana-nocturna"})
        self.assertLessEqual(len(out["tareas"]), 5)

    def test_entry_shape_and_url(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_tutoriales", {"consulta": "meta"}, "completo")
        self.assertTrue(out["tareas"])
        entrada = next(t for t in out["tareas"] if t["id"] == "crear-una-meta")
        self.assertEqual(entrada["pantalla"], "/goals/new")
        self.assertEqual(entrada["agente"], "si")
        self.assertEqual(entrada["url"], "/ayuda#crear-una-meta")
        self.assertFalse(entrada["has_video"])

    def test_available_at_nivel_ninguno(self):
        names = {h["nombre"] for h in herramientas.disponibles("ninguno")}
        self.assertIn("leer_tutoriales", names)


if __name__ == "__main__":
    unittest.main()
