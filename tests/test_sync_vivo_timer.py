"""BODY-SYNC-VIVO-TIMER-1: Carlos, relayed verbatim by the Personal desk 2026-11-15: "I would
like to see more like a live thing in the app or when I talk to you." Ruling 1's own bulk sweep --
pull.pull_hoy_todos (python -m body.pull --hoy), the lock it shares with the daily pull and
--backfill, and the counts line it prints. No network, invented demo data only, never a real
health figure.
"""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import cuenta, db, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "77" * 32
_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only", garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}


def _fetch_day_ok(day, token_str=None):
    return garmin_adapter.DayData(day=day, raw={"resumen": {}}, metrics={"resting_hr": 55.0}), token_str


class _PullHoyTodosTestCase(unittest.TestCase):
    """Shared fixture: hosted, person 1 connected -- the watch's own last-sync read stubbed to a
    no-op (the same pattern test_sync_vivo.py uses for pull_hoy_ahora), time.sleep stubbed."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "sync_vivo_timer.db")
        db.init(self.conn)
        env_patcher = mock.patch.dict(os.environ, _HOSTED_ENV)
        env_patcher.start(); self.addCleanup(env_patcher.stop)
        reloj_patcher = mock.patch(
            "body.pull.garmin_adapter.fetch_device_last_sync", side_effect=lambda token_str=None: (None, token_str))
        reloj_patcher.start(); self.addCleanup(reloj_patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        self.m_sleep = sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        self.conn.commit()


# ---- ruling 1: pull.pull_hoy_todos (the --hoy sweep's own per-person loop) ------------------------

class PullHoyTodosTest(_PullHoyTodosTestCase):
    def test_outside_daytime_window_is_fuera_horario_with_no_garmin_call(self):
        with mock.patch("body.pull._en_horas_quietas", return_value=True), \
             mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            ok, espera, fuera, fallidas = pull.pull_hoy_todos(self.conn)
        self.assertEqual((ok, espera, fuera, fallidas), (0, 0, 1, 0))
        m.assert_not_called()

    def test_too_soon_since_the_last_attempt_is_en_espera(self):
        """Proves pull_hoy_todos shares the ESPACIO_MIN_HOY floor with pull_hoy_ahora (never the
        scheduled pulls' own espacio_min=0) -- the combined ceiling the README's ruling 4 relies
        on. With espacio_min=0 this attempt would NOT be blocked."""
        garmin_cuenta.registrar_intento(self.conn, 1, datetime.now(timezone.utc), ok=True)
        with mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            ok, espera, fuera, fallidas = pull.pull_hoy_todos(self.conn)
        self.assertEqual((ok, espera, fuera, fallidas), (0, 1, 0, 0))
        m.assert_not_called()

    def test_inside_a_recorded_backoff_is_en_espera(self):
        garmin_cuenta.registrar_intento(self.conn, 1, datetime.now(timezone.utc), ok=False, estado_http=429)
        with mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            ok, espera, fuera, fallidas = pull.pull_hoy_todos(self.conn)
        self.assertEqual((ok, espera, fuera, fallidas), (0, 1, 0, 0))
        m.assert_not_called()

    def test_free_and_in_window_pulls_today_and_counts_ok(self):
        with mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_day_ok) as m:
            ok, espera, fuera, fallidas = pull.pull_hoy_todos(self.conn)
        self.assertEqual((ok, espera, fuera, fallidas), (1, 0, 0, 0))
        m.assert_called_once()
        self.assertIsNotNone(db.last_sync(self.conn, 1))

    def test_a_garmin_failure_counts_as_fallidas_and_records_the_error(self):
        with mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=RuntimeError("demo failure")):
            ok, espera, fuera, fallidas = pull.pull_hoy_todos(self.conn)
        self.assertEqual((ok, espera, fuera, fallidas), (0, 0, 0, 1))
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "RuntimeError")

    def test_each_person_is_checked_in_their_own_zone(self):
        self.conn.execute(
            "INSERT INTO person (id, display_name, timezone) VALUES (2, 'Otra', 'Pacific/Auckland')")
        garmin_cuenta.guardar(self.conn, 2, "tok-demo-2")
        self.conn.commit()
        with mock.patch("body.pull._en_horas_quietas", return_value=True) as m:
            pull.pull_hoy_todos(self.conn)
        zonas = [c.args[0].tzinfo for c in m.call_args_list]
        self.assertIn(ZoneInfo("America/Bogota"), zonas)
        self.assertIn(ZoneInfo("Pacific/Auckland"), zonas)

    def test_sequential_with_a_pause_between_people(self):
        self.conn.execute(
            "INSERT INTO person (id, display_name, timezone) VALUES (2, 'Otra', 'Pacific/Auckland')")
        garmin_cuenta.guardar(self.conn, 2, "tok-demo-2")
        self.conn.commit()
        with mock.patch("body.pull._en_horas_quietas", return_value=True):
            pull.pull_hoy_todos(self.conn)
        self.m_sleep.assert_called_once_with(pull.PAUSE_S)

    def test_no_connected_person_is_a_no_op(self):
        self.conn.execute("DELETE FROM garmin_cuenta")
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            result = pull.pull_hoy_todos(self.conn)
        self.assertEqual(result, (0, 0, 0, 0))
        m.assert_not_called()
        self.m_sleep.assert_not_called()


# ---- the --hoy CLI flag: the lock, the quiet skip, the counts line --------------------------------

class HoyCliTest(_PullHoyTodosTestCase):
    def test_hoy_flag_prints_the_counts_line_and_returns_zero(self):
        buf = io.StringIO()
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_day_ok), \
             contextlib.redirect_stdout(buf):
            codigo = pull.main(["--hoy"])
        self.assertEqual(codigo, 0)
        self.assertIn("hoy: 1 ok, 0 en espera, 0 fuera de horario, 0 fallidas", buf.getvalue())

    def test_hoy_skips_quietly_when_another_pull_holds_the_lock(self):
        """Ruling 1: --hoy takes pull.lock non-blocking, exactly as --backfill does -- a daily
        pull or a backfill already holding it means a quiet, successful skip."""
        lock = pull._try_lock()
        self.addCleanup(pull._unlock, lock)
        buf = io.StringIO()
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day") as m, \
             contextlib.redirect_stdout(buf):
            codigo = pull.main(["--hoy"])
        self.assertEqual(codigo, 0)
        self.assertIn("another pull is already running; skipping this run.", buf.getvalue())
        m.assert_not_called()

    def test_local_mode_has_no_hoy(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(cuenta.ISSUER_ENV, None)
            with mock.patch("body.pull.db.connect", return_value=self.conn), \
                 mock.patch("body.pull.garmin_adapter.fetch_day") as m:
                codigo = pull.main(["--hoy"])
            self.assertEqual(codigo, 2)
            m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
