"""BODY-GARMIN-SYNC-CONSISTENTE-1 rulings 4 and 4b: /garmin's connect and MFA-code forms carry
progress hooks (a disabled button plus an in-progress label, through body/static/pagina.js), and a
successful connect schedules exactly one background pull of that person's own healing set (ruling
1), through pull.pull_persona_ahora -- the same per-person path pull_hosted's own loop uses, never
a copy. No network: garminconnect.Garmin is replaced by FakeGarmin throughout.
"""
import os
import unittest
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, garmin_adapter, garmin_cuenta, pull
from body.rutas import garmin as garmin_rutas
from tests.test_garmin_host import (BAD_EMAIL, GOOD_CODE, GOOD_EMAIL, MFA_EMAIL, TOKEN_KEY_HEX,
                                     GarminHostedTestCase, _fresh_db)


class ConnectProgressHooksTest(GarminHostedTestCase):
    """Ruling 4: the connect and MFA-code buttons carry the progress hooks (a data-cargando
    attribute body/static/pagina.js reads) -- progressive enhancement, so the markup carries them
    whether or not JS runs; with JS off the forms still just submit."""

    def test_not_connected_connect_button_carries_the_progress_label(self):
        self._signed_in_person_id()
        r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-cargando="Conectando con Garmin…"', r.text)

    def test_mfa_code_button_carries_its_own_progress_label(self):
        self._signed_in_person_id()
        r = self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-cargando="Verificando código…"', r.text)

    def test_reconnect_form_after_an_error_also_carries_the_progress_label(self):
        pid = self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})
        conn = db.connect()
        garmin_cuenta.registrar_error(conn, pid, "NotSignedIn")
        conn.close()
        r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-cargando="Conectando con Garmin…"', r.text)

    def test_english_progress_labels_render_in_english(self):
        self._signed_in_person_id()
        self.client.post("/settings/language", data={"lang": "en"})
        r = self.client.get("/garmin")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-cargando="Connecting to Garmin…"', r.text)


class ConnectTriggersPullTest(GarminHostedTestCase):
    """Ruling 4b: a successful connect (plain sign-in or MFA) schedules exactly one background
    pull for that person; a failed attempt schedules none."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("body.rutas.garmin.pull.pull_persona_ahora")
        self.m_pull = patcher.start()
        self.addCleanup(patcher.stop)

    def test_successful_connect_schedules_exactly_one_pull_for_that_person(self):
        pid = self._signed_in_person_id()
        r = self.client.post("/garmin/conectar", data={"email": GOOD_EMAIL, "password": "whatever-demo"})
        self.assertEqual(r.status_code, 303)
        self.m_pull.assert_called_once_with(pid)

    def test_a_failed_connect_schedules_no_pull(self):
        self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": BAD_EMAIL, "password": "whatever-demo"})
        self.m_pull.assert_not_called()

    def test_mfa_finish_schedules_exactly_one_pull(self):
        pid = self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        r = self.client.post("/garmin/codigo", data={"code": GOOD_CODE})
        self.assertEqual(r.status_code, 303)
        self.m_pull.assert_called_once_with(pid)

    def test_needs_mfa_alone_schedules_no_pull_yet(self):
        self._signed_in_person_id()
        self.client.post("/garmin/conectar", data={"email": MFA_EMAIL, "password": "whatever-demo"})
        self.m_pull.assert_not_called()


class PullGuardTest(unittest.TestCase):
    """Ruling 4b: a second connect while one is already running for that person does not start
    another (an in-process guard, enough for one web worker). BODY-SYNC-VIVO-2 ruling 1: the
    guard set itself now lives in body/pull.py (`pull._PULL_EN_CURSO`), shared with the on-demand
    live pull -- these tests read and mutate it there, never through `garmin_rutas`."""

    def setUp(self):
        pull._PULL_EN_CURSO.clear()
        self.addCleanup(pull._PULL_EN_CURSO.clear)

    def test_a_second_call_for_the_same_person_while_one_runs_is_skipped(self):
        pull._PULL_EN_CURSO.add(7)
        with mock.patch("body.rutas.garmin.pull.pull_persona_ahora") as m:
            garmin_rutas._lanzar_pull_conexion(7)
        m.assert_not_called()

    def test_a_different_person_is_unaffected(self):
        pull._PULL_EN_CURSO.add(7)
        with mock.patch("body.rutas.garmin.pull.pull_persona_ahora") as m:
            garmin_rutas._lanzar_pull_conexion(8)
        m.assert_called_once_with(8)

    def test_the_guard_clears_once_the_pull_finishes(self):
        with mock.patch("body.rutas.garmin.pull.pull_persona_ahora"):
            garmin_rutas._lanzar_pull_conexion(9)
        self.assertNotIn(9, pull._PULL_EN_CURSO)

    def test_the_guard_clears_even_if_the_pull_raises(self):
        with mock.patch("body.rutas.garmin.pull.pull_persona_ahora", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                garmin_rutas._lanzar_pull_conexion(10)
        self.assertNotIn(10, pull._PULL_EN_CURSO)


class PullPersonaAhoraTest(unittest.TestCase):
    """pull.pull_persona_ahora itself -- the function the background task calls: runs ruling 1's
    healing set for that one person and records an error exactly as the daily timer does."""

    def setUp(self):
        self.conn, orig = _fresh_db(self, "pull_persona_ahora.db")
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        env_patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        env_patcher.start(); self.addCleanup(env_patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        self.conn.commit()

    def test_runs_the_healing_set_oldest_first(self):
        visitados: list[str] = []

        def fetch(day, token_str=None):
            visitados.append(day)
            return garmin_adapter.DayData(day=day, metrics={"resting_hr": 55.0}), token_str

        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=fetch):
            pull.pull_persona_ahora(1)
        self.assertEqual(len(visitados), garmin_cuenta.VENTANA_RELLENO)
        self.assertEqual(visitados, sorted(visitados))

    def test_records_an_error_on_failure(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=ValueError("demo failure")):
            pull.pull_persona_ahora(1)
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "ValueError")


if __name__ == "__main__":
    unittest.main()
