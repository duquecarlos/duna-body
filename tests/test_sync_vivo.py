"""BODY-SYNC-VIVO-2: Carlos, relayed by the Personal desk 2026-11-15: "I would like to see more
like a live thing in the app or when I talk to you." Rulings 1-2 (pull.pull_hoy_ahora, the two
non-blocking triggers) and rulings 3-4 (the "Tu reloj" freshness read-out, the ceiling). A fake
Garmin adapter throughout -- no network, invented demo data only, never a real figure.
"""
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, garmin_adapter, garmin_cuenta, identity, pull, web
from body.agente import herramientas
from body.i18n import T
from body.rutas import garmin as garmin_rutas
from tests._rutas import contar_sql

TOKEN_KEY_HEX = "11" * 32
_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only", garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}


def _fresh_db(case: unittest.TestCase, name: str):
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.ensure_ready(conn)  # also seeds person id=1 ("Me"); marks this DB file ready so a later
    # connection the app opens to the SAME path (any GET route under test) never re-runs init()
    # (tests/test_get_sin_escritura.py's own pattern -- init()'s CREATE TABLE IF NOT EXISTS
    # statements would otherwise count as writes on a route that writes nothing of its own).
    return conn


def _fetch_day_ok(day, token_str=None):
    return garmin_adapter.DayData(day=day, raw={"resumen": {}}, metrics={"resting_hr": 55.0}), token_str


class _PullHoyAhoraTestCase(unittest.TestCase):
    """Shared fixture: hosted, person 1, the watch's own last-sync read stubbed to a no-op
    (BODY-SYNC-VIVO-ESTADO-1's own pattern in tests/test_garmin_host.py), time.sleep stubbed."""

    def setUp(self):
        self.conn = _fresh_db(self, "sync_vivo.db")
        patcher_env = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher_env.start(); self.addCleanup(patcher_env.stop)
        self.addCleanup(pull._PULL_EN_CURSO.clear)
        reloj_patcher = mock.patch(
            "body.pull.garmin_adapter.fetch_device_last_sync", side_effect=lambda token_str=None: (None, token_str))
        reloj_patcher.start(); self.addCleanup(reloj_patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)
        # Ruling 4's quiet hours are exercised on their own in EnHorasQuietasTest below (direct
        # calls to _en_horas_quietas); these tests are about the spacing/backoff/lock rulings and
        # must not go flaky depending on what the real wall clock happens to read in Bogota when
        # the suite runs.
        quietas_patcher = mock.patch("body.pull._en_horas_quietas", return_value=False)
        quietas_patcher.start(); self.addCleanup(quietas_patcher.stop)


# ---- ruling 1: pull.pull_hoy_ahora ---------------------------------------------------------------

class PullHoyAhoraTest(_PullHoyAhoraTestCase):
    def test_local_mode_is_a_no_op(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(cuenta.ISSUER_ENV, None)
            with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
                estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "sin_cuenta")
        m.assert_not_called()

    def test_no_garmin_account_is_sin_cuenta(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "sin_cuenta")
        m.assert_not_called()

    def test_connected_and_free_pulls_today_and_returns_iniciado(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_day_ok) as m:
            estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "iniciado")
        m.assert_called_once()
        self.assertIsNotNone(db.last_sync(self.conn, 1))

    def test_a_pull_already_in_flight_for_this_person_is_en_curso(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        pull._PULL_EN_CURSO.add(1)
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "en_curso")
        m.assert_not_called()

    def test_too_soon_since_the_last_attempt_is_espera(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_intento(self.conn, 1, datetime.now(timezone.utc), ok=True)
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "espera")
        m.assert_not_called()

    def test_inside_a_recorded_backoff_is_espera(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_intento(self.conn, 1, datetime.now(timezone.utc), ok=False, estado_http=429)
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "espera")
        m.assert_not_called()

    def test_a_garmin_failure_never_raises_and_records_the_error(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=RuntimeError("demo failure")):
            estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "iniciado")
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "RuntimeError")

    def test_the_guard_clears_even_after_a_failure(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=RuntimeError("demo failure")):
            pull.pull_hoy_ahora(1)
        self.assertNotIn(1, pull._PULL_EN_CURSO)

    def test_success_recomputes_only_sueno_fc_not_recuperacion_or_turno_fc(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_day_ok), \
             mock.patch("body.pull._recalcular_sueno_fc_seguro") as m_sueno, \
             mock.patch("body.pull._recalcular_recuperacion_seguro") as m_rec, \
             mock.patch("body.pull._recalcular_turno_fc_seguro") as m_turno:
            estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "iniciado")
        m_sueno.assert_called_once_with(mock.ANY, 1)
        m_rec.assert_not_called()
        m_turno.assert_not_called()

    def test_two_concurrent_triggers_for_the_same_person_reach_garmin_exactly_once(self):
        """Ruling 1's shared lock, proven with a real second thread: the first call blocks
        mid-fetch (an Event holds it there) while a second call for the SAME person arrives --
        it must see the lock already held and never reach Garmin itself."""
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        dentro = threading.Event()
        continuar = threading.Event()
        llamadas = []

        def fetch(day, token_str=None):
            llamadas.append(day)
            dentro.set()
            continuar.wait(2)
            return garmin_adapter.DayData(day=day, metrics={"resting_hr": 55.0}), token_str

        resultados = {}

        def disparar():
            resultados["primero"] = pull.pull_hoy_ahora(1)

        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=fetch):
            hilo = threading.Thread(target=disparar)
            hilo.start()
            self.assertTrue(dentro.wait(2), "the first call never reached Garmin")
            segundo = pull.pull_hoy_ahora(1)
            continuar.set()
            hilo.join(2)
        self.assertEqual(len(llamadas), 1)
        self.assertEqual(segundo, "en_curso")
        self.assertEqual(resultados["primero"], "iniciado")

    def test_the_lock_is_shared_with_the_connect_triggered_pull(self):
        """Ruling 1: pull_hoy_ahora and _lanzar_pull_conexion (body/rutas/garmin.py) take the SAME
        `pull._PULL_EN_CURSO` set -- a live pull in flight blocks a connect-triggered one for the
        same person, and vice versa."""
        pull._PULL_EN_CURSO.add(1)
        with mock.patch("body.rutas.garmin.pull.pull_persona_ahora") as m:
            garmin_rutas._lanzar_pull_conexion(1)
        m.assert_not_called()


# ---- ruling 2: the two non-blocking triggers -----------------------------------------------------

class HoyBackgroundTriggerTest(unittest.TestCase):
    """Hoy's own `GET /` -- schedules pull.pull_hoy_ahora as a BackgroundTask exactly when ruling
    2 says to, and never lets a real failure inside it break the response."""

    def setUp(self):
        self.conn = _fresh_db(self, "hoy_bg.db")
        patcher_env = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher_env.start(); self.addCleanup(patcher_env.stop)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.addCleanup(web.app.dependency_overrides.clear)
        self.addCleanup(pull._PULL_EN_CURSO.clear)
        self.client = TestClient(web.app, follow_redirects=False)

    def test_connected_and_free_schedules_the_pull(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        with mock.patch("body.rutas.hoy.pull.pull_hoy_ahora") as m:
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        m.assert_called_once_with(1)

    def test_not_connected_schedules_nothing(self):
        with mock.patch("body.rutas.hoy.pull.pull_hoy_ahora") as m:
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        m.assert_not_called()

    def test_too_soon_since_the_last_attempt_schedules_nothing(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_intento(self.conn, 1, datetime.now(timezone.utc), ok=True)
        self.conn.commit()
        with mock.patch("body.rutas.hoy.pull.pull_hoy_ahora") as m:
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        m.assert_not_called()

    def test_local_mode_schedules_nothing(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(cuenta.ISSUER_ENV, None)
            with mock.patch("body.rutas.hoy.pull.pull_hoy_ahora") as m:
                r = self.client.get("/")
            self.assertEqual(r.status_code, 200)
            m.assert_not_called()

    def test_a_real_garmin_failure_in_the_background_pull_never_breaks_the_response(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=RuntimeError("demo failure")), \
             mock.patch("body.pull.garmin_adapter.fetch_device_last_sync",
                        side_effect=lambda token_str=None: (None, token_str)), \
             mock.patch("body.pull.time.sleep"), \
             mock.patch("body.pull._en_horas_quietas", return_value=False):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)


class _HiloInmediato:
    """A fully synchronous stand-in for threading.Thread, for the tests below that need to
    observe the call before `ejecutar` returns -- `.start()` runs the target in the SAME thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class EjecutarLivePullTriggerTest(unittest.TestCase):
    """herramientas.ejecutar's own trigger (ruling 2): fires before leer_hoy/leer_estado_reloj's
    own read, through a real thread it never awaits -- proven with threading.Event below, never a
    sleep. Also reaches the hosted MCP door, since mcp.py's `_llamar_herramienta` calls this same
    `ejecutar` for a `_SesionRemota` exactly as it does for the local stdio door."""

    def setUp(self):
        self.conn = _fresh_db(self, "ejecutar_sync.db")
        patcher_env = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher_env.start(); self.addCleanup(patcher_env.stop)
        # test_hosted_mcp_door_reaches_the_same_trigger below goes through body.mcp's own
        # _llamar_herramienta, which sets this contextvar for the call's own context -- restored
        # to its default here so a later test MODULE in the same unittest-discover process (this
        # var is not request-scoped outside a real ASGI task boundary) never inherits "no shifts"
        # from this fixture's own fresh, turnos-unset person 1.
        self.addCleanup(herramientas.fijar_turnos_actual, True)

    def test_leer_hoy_triggers_the_live_pull(self):
        evento = threading.Event()
        llamadas = []

        def capturar(pid):
            llamadas.append(pid)
            evento.set()

        with mock.patch("body.agente.herramientas.pull.pull_hoy_ahora", side_effect=capturar):
            herramientas.ejecutar(self.conn, 1, "leer_hoy", {}, "completo")
            self.assertTrue(evento.wait(2))
        self.assertEqual(llamadas, [1])

    def test_leer_estado_reloj_triggers_the_live_pull(self):
        evento = threading.Event()
        llamadas = []

        def capturar(pid):
            llamadas.append(pid)
            evento.set()

        with mock.patch("body.agente.herramientas.pull.pull_hoy_ahora", side_effect=capturar):
            herramientas.ejecutar(self.conn, 1, "leer_estado_reloj", {}, "ninguno")
            self.assertTrue(evento.wait(2))
        self.assertEqual(llamadas, [1])

    def test_an_unrelated_tool_never_triggers_it(self):
        with mock.patch("body.agente.herramientas.threading.Thread") as m_hilo:
            herramientas.ejecutar(self.conn, 1, "leer_ajustes", {}, "ninguno")
        m_hilo.assert_not_called()

    def test_local_mode_spawns_no_thread_at_all(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(cuenta.ISSUER_ENV, None)
            with mock.patch("body.agente.herramientas.threading.Thread") as m_hilo:
                herramientas.ejecutar(self.conn, 1, "leer_hoy", {}, "completo")
            m_hilo.assert_not_called()

    def test_a_failing_live_pull_never_breaks_the_tool_answer(self):
        evento = threading.Event()

        def fallar(pid):
            evento.set()
            raise RuntimeError("boom")

        with mock.patch("body.agente.herramientas.pull.pull_hoy_ahora", side_effect=fallar):
            out = herramientas.ejecutar(self.conn, 1, "leer_hoy", {}, "completo")
        self.assertIn("dia", out)
        self.assertTrue(evento.wait(2))

    def test_hosted_mcp_door_reaches_the_same_trigger(self):
        """The hosted MCP door (body/rutas/mcp_remoto.py) calls body.mcp's own
        `_llamar_herramienta`, which for a `_SesionRemota` dispatches straight to
        `herramientas.ejecutar` -- the exact function this whole test class already exercises, so
        no separate thread-spawn test is needed for that door; this proves the call chain itself
        reaches it, through the synchronous stub, without a real Garmin call."""
        from body import mcp
        conn = self.conn
        sesion = mcp._SesionRemota(conn, 1, None)
        with mock.patch("body.agente.herramientas.threading.Thread", _HiloInmediato), \
             mock.patch("body.agente.herramientas.pull.pull_hoy_ahora") as m:
            mcp._llamar_herramienta(sesion, "leer_hoy", {})
        m.assert_called_once_with(1)


# ---- rulings 3-4: the freshness read-out and the ceiling -----------------------------------------

class TextoEstadoVivoTest(_PullHoyAhoraTestCase):
    """pull.texto_estado_vivo: the three states, in both languages, and the "none apply" gap."""

    def test_no_connection_is_none(self):
        self.assertIsNone(pull.texto_estado_vivo(T("es"), self.conn, 1, ZoneInfo("America/Bogota"),
                                                   datetime.now(timezone.utc).date()))

    def test_syncing_now(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        pull._PULL_EN_CURSO.add(1)
        for lang, esperado in (("es", "Sincronizando con Garmin…"), ("en", "Syncing with Garmin…")):
            self.assertEqual(
                pull.texto_estado_vivo(T(lang), self.conn, 1, ZoneInfo("America/Bogota"),
                                        datetime.now(timezone.utc).date()),
                esperado)

    def test_failed_with_a_scheduled_retry(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        tz = ZoneInfo("America/Bogota")
        ahora = datetime(2026, 11, 10, 14, 5, tzinfo=timezone.utc)  # 09:05 Bogota
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=429)
        hora_local = ahora.astimezone(tz).strftime("%H:%M")
        reintento_local = garmin_cuenta.estado_sync(self.conn, 1)["reintentar_despues_de"]
        reintento_txt = datetime.fromisoformat(reintento_local).astimezone(tz).strftime("%H:%M")
        hoy = ahora.astimezone(tz).date()
        self.assertEqual(
            pull.texto_estado_vivo(T("es"), self.conn, 1, tz, hoy),
            f"Garmin no respondió a las {hora_local}. Lo intentamos de nuevo a las {reintento_txt}.")
        self.assertEqual(
            pull.texto_estado_vivo(T("en"), self.conn, 1, tz, hoy),
            f"Garmin didn't answer at {hora_local}. We'll try again at {reintento_txt}.")

    def test_watch_last_synced_today(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        tz = ZoneInfo("America/Bogota")
        cuando = datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc)  # 08:30 Bogota
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, cuando)
        hoy = cuando.astimezone(tz).date()
        self.assertEqual(pull.texto_estado_vivo(T("es"), self.conn, 1, tz, hoy),
                          "Tu reloj se sincronizó con Garmin a las 08:30.")
        self.assertEqual(pull.texto_estado_vivo(T("en"), self.conn, 1, tz, hoy),
                          "Your watch last synced with Garmin at 08:30.")

    def test_watch_last_synced_a_previous_day_names_the_day(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        tz = ZoneInfo("America/Bogota")
        cuando = datetime(2026, 11, 8, 13, 30, tzinfo=timezone.utc)
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, cuando)
        hoy = cuando.astimezone(tz).date() + timedelta(days=2)
        self.assertEqual(pull.texto_estado_vivo(T("es"), self.conn, 1, tz, hoy),
                          "Tu reloj se sincronizó con Garmin a las 8 nov, 08:30.")

    def test_syncing_wins_over_a_scheduled_retry(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_intento(self.conn, 1, datetime.now(timezone.utc), ok=False, estado_http=429)
        pull._PULL_EN_CURSO.add(1)
        self.assertEqual(
            pull.texto_estado_vivo(T("es"), self.conn, 1, ZoneInfo("America/Bogota"),
                                    datetime.now(timezone.utc).date()),
            "Sincronizando con Garmin…")


class EnHorasQuietasTest(unittest.TestCase):
    """Ruling 4's ceiling: no on-demand pull between 23:00 and 06:00 in the person's own zone."""

    def test_boundaries(self):
        tz = ZoneInfo("America/Bogota")
        self.assertTrue(pull._en_horas_quietas(datetime(2026, 1, 1, 23, 0, tzinfo=tz)))
        self.assertTrue(pull._en_horas_quietas(datetime(2026, 1, 1, 5, 59, tzinfo=tz)))
        self.assertFalse(pull._en_horas_quietas(datetime(2026, 1, 1, 6, 0, tzinfo=tz)))
        self.assertFalse(pull._en_horas_quietas(datetime(2026, 1, 1, 22, 59, tzinfo=tz)))
        self.assertTrue(pull._en_horas_quietas(datetime(2026, 1, 1, 0, 0, tzinfo=tz)))


class ReglaVivaHoyCardTest(unittest.TestCase):
    """The «Tu reloj» card on today.html: each state renders its own sentence, in both languages,
    with the poll script present only while syncing, and the card absent when nothing applies."""

    def setUp(self):
        self.conn = _fresh_db(self, "reloj_card.db")
        patcher_env = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher_env.start(); self.addCleanup(patcher_env.stop)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.addCleanup(web.app.dependency_overrides.clear)
        self.addCleanup(pull._PULL_EN_CURSO.clear)
        self.client = TestClient(web.app, follow_redirects=False)
        self.conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES "
            "(1, ?, 'resting_hr', 55, 'garmin')", (datetime.now(timezone.utc).date().isoformat(),))
        self.conn.commit()

    def _get(self):
        with mock.patch("body.rutas.hoy.pull.pull_hoy_ahora"):
            return self.client.get("/")

    def test_no_card_with_no_garmin_connection(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(T("es")("reloj_vivo.titulo"), r.text)
        self.assertNotIn("reloj-sync.js", r.text)

    def test_syncing_shows_the_card_the_attribute_and_the_script(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        pull._PULL_EN_CURSO.add(1)
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertIn(T("es")("reloj_vivo.sincronizando"), r.text)
        self.assertIn('data-sincronizando="1"', r.text)
        self.assertIn("reloj-sync.js", r.text)
        pull._PULL_EN_CURSO.discard(1)

    def test_last_synced_shows_the_card_without_the_attribute_or_the_script(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, datetime.now(timezone.utc))
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Garmin", r.text)
        self.assertNotIn('data-sincronizando="1"', r.text)
        self.assertNotIn("reloj-sync.js", r.text)

    def test_english_renders_in_english(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, datetime.now(timezone.utc))
        self.client.post("/settings/language", data={"lang": "en"})
        r = self._get()
        self.assertIn("Your watch last synced with Garmin at", r.text)


class MergedRelojCardTest(unittest.TestCase):
    """BODY-SYNC-VIVO-2-B ruling 1: the gap half (`garmin_sync`) and the freshness half
    (`reloj_vivo`) share ONE «Tu reloj» card -- never two headings for the same subject, in either
    language, in every combination of the two states."""

    def setUp(self):
        self.conn = _fresh_db(self, "reloj_merge.db")
        patcher_env = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher_env.start(); self.addCleanup(patcher_env.stop)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.addCleanup(web.app.dependency_overrides.clear)
        self.addCleanup(pull._PULL_EN_CURSO.clear)
        self.client = TestClient(web.app, follow_redirects=False)
        self.conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES "
            "(1, ?, 'resting_hr', 55, 'garmin')", (datetime.now(timezone.utc).date().isoformat(),))
        self.conn.commit()

    def _get(self):
        with mock.patch("body.rutas.hoy.pull.pull_hoy_ahora"):
            return self.client.get("/")

    def _titulo_count(self, text: str, lang: str = "es") -> int:
        """Counts the card's own HEADING only -- `t('reloj_vivo.titulo')`'s bare value ("Tu
        reloj"/"Your watch") is also a PREFIX of reloj_vivo.ultimo_sync's own sentence ("Tu reloj
        se sincronizó..."/"Your watch last synced..."), so a plain substring count double-counts
        the freshness state; anchored to the heading's own markup instead."""
        return text.count('<div class="q">' + T(lang)("reloj_vivo.titulo"))

    def test_gap_only_renders_one_title_the_chip_the_link_and_no_script(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_error(self.conn, 1, "NotSignedIn")
        self.conn.commit()
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._titulo_count(r.text), 1)
        self.assertIn(T("es")("today.garmin_sync.chip"), r.text)
        self.assertIn(T("es")("today.garmin_sync.not_signed_in"), r.text)
        self.assertIn('href="/garmin"', r.text)
        self.assertNotIn("reloj-sync.js", r.text)

    def test_freshness_only_renders_one_title_no_chip_and_no_garmin_link(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, datetime.now(timezone.utc))
        self.conn.commit()
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._titulo_count(r.text), 1)
        self.assertNotIn(T("es")("today.garmin_sync.chip"), r.text)
        self.assertNotIn('href="/garmin"', r.text)

    def test_both_apply_render_exactly_one_title_gap_then_freshness_then_link(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_error(self.conn, 1, "NotSignedIn")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, datetime.now(timezone.utc))
        self.conn.commit()
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._titulo_count(r.text), 1)
        gap = r.text.index(T("es")("today.garmin_sync.not_signed_in"))
        fresco = r.text.index(T("es")("reloj_vivo.ultimo_sync").split("{")[0])
        enlace = r.text.index('href="/garmin"')
        self.assertTrue(gap < fresco < enlace, (gap, fresco, enlace))

    def test_neither_applies_renders_no_title_at_all(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._titulo_count(r.text), 0)

    def test_english_both_apply_renders_exactly_one_title(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_error(self.conn, 1, "NotSignedIn")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, datetime.now(timezone.utc))
        self.conn.commit()
        self.client.post("/settings/language", data={"lang": "en"})
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._titulo_count(r.text, "en"), 1)


class GarminEstadoVivoRouteTest(unittest.TestCase):
    """body/rutas/garmin.py's small poll endpoint (ruling 3; BODY-SYNC-VIVO-2-B ruling 1 adds the
    gap half to its own reload, so a mid-poll swap never drops it)."""

    def setUp(self):
        self.conn = _fresh_db(self, "estado_vivo_route.db")
        patcher_env = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher_env.start(); self.addCleanup(patcher_env.stop)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.addCleanup(web.app.dependency_overrides.clear)
        self.addCleanup(pull._PULL_EN_CURSO.clear)
        self.client = TestClient(web.app, follow_redirects=False)

    def test_404_in_local_mode(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(cuenta.ISSUER_ENV, None)
            r = self.client.get("/garmin/estado-vivo")
        self.assertEqual(r.status_code, 404)

    def test_renders_the_current_state(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        pull._PULL_EN_CURSO.add(1)
        r = self.client.get("/garmin/estado-vivo")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-sincronizando="1"', r.text)

    def test_stops_carrying_the_attribute_once_the_pull_finishes(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, datetime.now(timezone.utc))
        r = self.client.get("/garmin/estado-vivo")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('data-sincronizando="1"', r.text)

    def test_this_route_never_writes(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        self.conn.commit()
        with contar_sql() as stats:
            r = self.client.get("/garmin/estado-vivo")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(stats["writes"], 0)

    def test_renders_the_gap_half_too_so_a_mid_poll_swap_never_drops_it(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        garmin_cuenta.registrar_error(self.conn, 1, "NotSignedIn")
        self.conn.commit()
        r = self.client.get("/garmin/estado-vivo")
        self.assertEqual(r.status_code, 200)
        self.assertIn(T("es")("today.garmin_sync.not_signed_in"), r.text)
        self.assertIn('class="chip warn"', r.text)
        self.assertEqual(r.text.count(T("es")("reloj_vivo.titulo")), 1)


if __name__ == "__main__":
    unittest.main()
