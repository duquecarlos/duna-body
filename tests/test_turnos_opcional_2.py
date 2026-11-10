"""BODY-TURNOS-OPCIONAL-2: the one-time «¿Trabajas por turnos?» question, answered in place on
Primeros pasos (Hoy) or any time on Ajustes, slice 2 of BODY-TURNOS-OPCIONAL-OBS-1's plan. This only
records the answer through db.get_trabaja_turnos / db.set_trabaja_turnos -- hiding Turnos and the
PC-link rule are later slices. Invented demo data only, no real health figures.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, identity, tutoriales, web


def _fresh_client(case: unittest.TestCase) -> tuple[TestClient, "db.sqlite3.Connection"]:
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return TestClient(web.app, follow_redirects=False), conn


class _LocalModeTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(tutoriales._manifest_cache.clear)
        self.client, self.conn = _fresh_client(self)
        self.assertNotIn(cuenta.ISSUER_ENV, os.environ)


class _HostedModeTestCase(unittest.TestCase):
    """Person resolved by overriding the dependency directly (BODY-DISPOSITIVO-1's own test
    convention), so consent is not in the way of a route that only cares about the person id."""

    def setUp(self):
        self.addCleanup(tutoriales._manifest_cache.clear)
        self.client, self.conn = _fresh_client(self)
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1


# ---- POST /settings/trabaja_turnos ------------------------------------------------------------------

class SetTrabajaTurnosRouteTest(_LocalModeTestCase):
    def test_si_stores_true_and_redirects_to_root(self):
        r = self.client.post("/settings/trabaja_turnos", data={"valor": "si", "volver": "/"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        self.assertTrue(db.get_trabaja_turnos(self.conn, 1))

    def test_no_stores_false_and_redirects_to_settings(self):
        r = self.client.post("/settings/trabaja_turnos", data={"valor": "no", "volver": "/settings"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/settings")
        self.assertFalse(db.get_trabaja_turnos(self.conn, 1))

    def test_default_volver_is_settings(self):
        r = self.client.post("/settings/trabaja_turnos", data={"valor": "si"})
        self.assertEqual(r.headers["location"], "/settings")

    def test_foreign_volver_falls_back_to_settings(self):
        for foreign in ("https://example.com", "//evil"):
            r = self.client.post("/settings/trabaja_turnos", data={"valor": "si", "volver": foreign})
            self.assertEqual(r.headers["location"], "/settings", foreign)

    def test_bad_valor_changes_nothing(self):
        self.assertIsNone(db.get_trabaja_turnos(self.conn, 1))
        r = self.client.post("/settings/trabaja_turnos", data={"valor": "quizas", "volver": "/settings"})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(db.get_trabaja_turnos(self.conn, 1))

    def test_missing_valor_changes_nothing(self):
        r = self.client.post("/settings/trabaja_turnos", data={"volver": "/settings"})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(db.get_trabaja_turnos(self.conn, 1))


# ---- Ajustes: Sí / No like the language card --------------------------------------------------------

def _trabaja_turnos_card(html: str) -> str:
    """Slices the page between the trabaja_turnos card's own form action and the next card, so a
    sibling Sí/No card (BODY-MCP-APLICAR-1's `aplicar_mcp` card uses the identical `btn on"
    name="valor"` markup) can never satisfy or break what this test asserts about THIS card."""
    inicio = html.index('action="/settings/trabaja_turnos"')
    fin = html.index('<div class="card">', inicio)
    return html[inicio:fin]


class AjustesCardTest(_LocalModeTestCase):
    def test_null_neither_button_on(self):
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        card = _trabaja_turnos_card(r.text)
        self.assertNotIn('btn on" name="valor" value="si"', card)
        self.assertNotIn('btn on" name="valor" value="no"', card)

    def test_si_marks_on(self):
        db.set_trabaja_turnos(self.conn, True, 1)
        r = self.client.get("/settings")
        card = _trabaja_turnos_card(r.text)
        self.assertIn('btn on" name="valor" value="si"', card)
        self.assertNotIn('btn on" name="valor" value="no"', card)

    def test_no_marks_on(self):
        db.set_trabaja_turnos(self.conn, False, 1)
        r = self.client.get("/settings")
        self.assertIn('btn on" name="valor" value="no"', r.text)
        self.assertNotIn('btn on" name="valor" value="si"', r.text)

    def test_description_line_present(self):
        r = self.client.get("/settings")
        from body.i18n import T
        self.assertIn(T("es")("settings.turnos.desc"), r.text)


# ---- Primeros pasos: position, unticked with two buttons, ticked once answered ---------------------

class OnboardingOrderTest(unittest.TestCase):
    def test_item_sits_after_conectar_garmin_and_before_vincular_este_pc(self):
        self.assertEqual(
            tutoriales.ONBOARDING_ORDEN.index("trabajas-por-turnos"),
            tutoriales.ONBOARDING_ORDEN.index("conectar-garmin") + 1,
        )
        self.assertEqual(
            tutoriales.ONBOARDING_ORDEN.index("vincular-este-pc"),
            tutoriales.ONBOARDING_ORDEN.index("trabajas-por-turnos") + 1,
        )

    def test_catalog_orden_matches(self):
        tareas = {t["id"]: t for t in tutoriales.catalogo()["tareas"]}
        self.assertLess(tareas["conectar-garmin"]["orden"], tareas["trabajas-por-turnos"]["orden"])
        self.assertLess(tareas["trabajas-por-turnos"]["orden"], tareas["vincular-este-pc"]["orden"])

    def test_shows_third_in_its_chapter_despite_array_position(self):
        """BODY-TURNOS-OPCIONAL-2-M: the task was moved to the end of the catalog's `tareas` array
        (fix 1, to restore the /settings help door) -- display order comes from `orden`, not array
        position, so it must still render third in Primeros pasos and in its chapter."""
        capitulo = next(c for c in tutoriales.capitulos_con_tareas() if c["id"] == "primeros-pasos")
        self.assertEqual(
            [t["id"] for t in capitulo["tareas"]],
            [
                "iniciar-sesion-y-aceptar",
                "conectar-garmin",
                "trabajas-por-turnos",
                "vincular-este-pc",
                "primera-mirada-a-hoy",
            ],
        )


class AyudaHelpDoorTest(unittest.TestCase):
    """BODY-TURNOS-OPCIONAL-2-M fix 1: `trabajas-por-turnos` shares its `pantalla` (/settings) with
    the pre-existing `pagarle-a-duna` task. The in-context "¿Cómo se hace?" door must keep resolving
    to the task that held it before this slice, not to the newer one."""

    def test_settings_help_door_resolves_to_pagarle_a_duna(self):
        tarea = tutoriales.tarea_en_ruta("/settings")
        self.assertIsNotNone(tarea)
        self.assertEqual(tarea["id"], "pagarle-a-duna")


class PrimerosPasosHechoTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def test_null_is_not_hecho(self):
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertFalse(items["trabajas-por-turnos"])

    def test_no_is_hecho(self):
        db.set_trabaja_turnos(self.conn, False, 1)
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertTrue(items["trabajas-por-turnos"])

    def test_si_is_hecho(self):
        db.set_trabaja_turnos(self.conn, True, 1)
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertTrue(items["trabajas-por-turnos"])

    def test_shown_in_local_mode_too(self):
        ids = {i["id"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=False)}
        self.assertIn("trabajas-por-turnos", ids)


class PrimerosPasosCardRenderTest(_HostedModeTestCase):
    """Hoy's Primeros pasos card: the question with two buttons while unanswered, ticked like every
    other item once answered."""

    def _seed_other_items_unticked(self):
        pass  # nothing seeded: card shows with everything unticked, including this item

    def test_unanswered_renders_two_buttons_not_the_plain_link(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Primeros pasos", r.text)
        self.assertNotIn('href="/ayuda#trabajas-por-turnos"', r.text)
        self.assertIn('action="/settings/trabaja_turnos"', r.text)
        self.assertIn('name="valor" value="si"', r.text)
        self.assertIn('name="valor" value="no"', r.text)
        self.assertIn('name="volver" value="/"', r.text)

    def test_answered_no_renders_ticked_link_like_other_items(self):
        db.set_trabaja_turnos(self.conn, False, 1)
        r = self.client.get("/")
        self.assertIn('href="/ayuda#trabajas-por-turnos"', r.text)
        self.assertNotIn('action="/settings/trabaja_turnos"', r.text)

    def test_answered_si_renders_ticked_link_like_other_items(self):
        db.set_trabaja_turnos(self.conn, True, 1)
        r = self.client.get("/")
        self.assertIn('href="/ayuda#trabajas-por-turnos"', r.text)
        self.assertNotIn('action="/settings/trabaja_turnos"', r.text)



class PrimerosPasosCardLocalModeTest(_LocalModeTestCase):
    def test_renders_in_local_mode_too(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('action="/settings/trabaja_turnos"', r.text)


# ---- both languages render ---------------------------------------------------------------------------

class BothLanguagesTest(_LocalModeTestCase):
    def test_spanish_settings_card(self):
        r = self.client.get("/settings")
        self.assertIn("¿Trabajas por turnos?", r.text)

    def test_english_settings_card(self):
        self.client.post("/settings/language", data={"lang": "en"})
        r = self.client.get("/settings")
        self.assertIn("Do you work shifts?", r.text)

    def test_spanish_primeros_pasos_question(self):
        r = self.client.get("/")
        self.assertIn("¿Trabajas por turnos?", r.text)

    def test_english_primeros_pasos_question(self):
        self.client.post("/settings/language", data={"lang": "en"})
        r = self.client.get("/")
        self.assertIn("Do you work shifts?", r.text)

    def test_english_description_reads_how_long_it_takes_you(self):
        """BODY-TURNOS-OPCIONAL-2-M fix 2."""
        self.client.post("/settings/language", data={"lang": "en"})
        r = self.client.get("/settings")
        self.assertIn("how long it takes you to recover", r.text)
        self.assertNotIn("how long you take to recover", r.text)


if __name__ == "__main__":
    unittest.main()
