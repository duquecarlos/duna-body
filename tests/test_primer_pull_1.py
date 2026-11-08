"""BODY-PRIMER-PULL-1: a bounded hosted backfill (the last 90 days), run by a second short-
interval timer, serialised against the daily pull with a file lock. No network: every Garmin call
here is stubbed through body.pull.garmin_adapter.fetch_day, exactly as tests/test_garmin_host.py
stubs it. Invented demo data only, no real health figures.
"""
import contextlib
import io
import os
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, cuenta, db, garmin_adapter, garmin_cuenta, goals, pull

TOKEN_KEY_HEX = "33" * 32
HOY = date(2026, 11, 7)


def _fresh_db(case: unittest.TestCase, name: str):
    orig_db_path = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    return conn


def _stub_fetch_day(calls: list | None = None, empty_days: set | None = None,
                     error_token: str | None = None, refresh_to: str | None = None):
    """Replaces body.pull.garmin_adapter.fetch_day. `calls` (if given) collects every day asked
    for, in the order asked, so a test can check newest-first order and exact call counts."""
    empty_days = empty_days or set()

    def fetch(day, token_str=None):
        if calls is not None:
            calls.append(day)
        if error_token is not None and token_str == error_token:
            raise ValueError("demo adapter failure, not a real Garmin error")
        metrics = {} if day in empty_days else {"resting_hr": 55.0}
        data = garmin_adapter.DayData(day=day, metrics=metrics)
        return data, (refresh_to if refresh_to is not None else token_str)

    return fetch


def _offsets(start: int, n: int) -> list[str]:
    """The n days ending `start` days before HOY, newest first -- the same newest-first order
    --backfill is supposed to ask Garmin for."""
    return [(HOY - timedelta(start + i)).isoformat() for i in range(n)]


class BackfillWindowTest(unittest.TestCase):
    """body/pull.py's pull_backfill_hosted: the per-person batching, ordering and marker."""

    def setUp(self):
        self.conn = _fresh_db(self, "backfill.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")  # PAUSE_S would otherwise slow this file a lot
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)

    def test_one_run_fetches_15_days_newest_first_and_moves_the_marker(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        calls: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(calls=calls)):
            ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual((ok, failed, saved), (1, 0, 15))
        self.assertEqual(calls, _offsets(1, 15))
        self.assertEqual(garmin_cuenta.marcador_backfill(self.conn, 1), calls[-1])

    def test_repeated_runs_reach_the_window_start_then_make_zero_adapter_calls(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        for run in range(6):  # 6 * 15 = 90 days: the whole BACKFILL_DIAS window
            calls: list[str] = []
            with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(calls=calls)):
                ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
            self.assertEqual(calls, _offsets(1 + run * 15, 15), f"run {run}")
            self.assertEqual((ok, failed, saved), (1, 0, 15), f"run {run}")

        calls = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(calls=calls)) as m:
            ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual(calls, [])
        self.assertEqual(m.call_count, 0)
        self.assertEqual((ok, failed, saved), (1, 0, 0))

    def test_a_day_with_no_data_is_not_fetched_again(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        first_batch = _offsets(1, 15)
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                        side_effect=_stub_fetch_day(empty_days=set(first_batch))):
            pull.pull_backfill_hosted(self.conn, hoy=HOY)
        second_calls: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(calls=second_calls)):
            pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual(set(second_calls) & set(first_batch), set())
        self.assertEqual(second_calls, _offsets(16, 15))

    def test_adapter_error_on_one_person_does_not_stop_the_other(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-bad")
        garmin_cuenta.guardar(self.conn, 2, "tok-good")
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(error_token="tok-bad")):
            ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual((ok, failed, saved), (1, 1, 15))  # only person 2's 15 days
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "ValueError")
        self.assertIsNone(garmin_cuenta.ultimo_error(self.conn, 2))
        self.assertIsNone(garmin_cuenta.marcador_backfill(self.conn, 1))  # no day fully attempted
        self.assertEqual(garmin_cuenta.marcador_backfill(self.conn, 2), _offsets(1, 15)[-1])

    def test_refreshed_token_is_re_saved(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-refresh")
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                        side_effect=_stub_fetch_day(refresh_to="tok-refreshed")):
            pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual(garmin_cuenta.leer(self.conn, 1), "tok-refreshed")

    def test_person_not_connected_is_skipped_with_no_adapter_call(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual((ok, failed, saved), (0, 0, 0))
        m.assert_not_called()

    def test_person_with_a_recorded_error_is_skipped_without_an_adapter_call(self):
        """A rejected or expired token must not be retried every 10 minutes (risks Garmin
        rate-limiting). Another connected person in the same run is unaffected."""
        garmin_cuenta.guardar(self.conn, 1, "tok-bad")
        garmin_cuenta.registrar_error(self.conn, 1, "GarminAuthError")
        garmin_cuenta.guardar(self.conn, 2, "tok-good")
        calls: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(calls=calls)):
            ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual((ok, failed, saved), (1, 0, 15))  # only person 2
        self.assertEqual(calls, _offsets(1, 15))  # person 1 never asked
        self.assertIsNone(garmin_cuenta.marcador_backfill(self.conn, 1))
        self.assertEqual(garmin_cuenta.marcador_backfill(self.conn, 2), calls[-1])
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "GarminAuthError")  # unchanged

    def test_backfill_resumes_once_a_reconnect_clears_the_recorded_error(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-bad")
        garmin_cuenta.registrar_error(self.conn, 1, "GarminAuthError")
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual((ok, failed, saved), (0, 0, 0))
        m.assert_not_called()

        garmin_cuenta.guardar(self.conn, 1, "tok-new")  # a reconnect clears ultimo_error
        self.assertIsNone(garmin_cuenta.ultimo_error(self.conn, 1))
        calls: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(calls=calls)):
            ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual((ok, failed, saved), (1, 0, 15))
        self.assertEqual(calls, _offsets(1, 15))


class LockTest(unittest.TestCase):
    """The non-blocking file lock body/pull.py's daily hosted pull and --backfill both take."""

    def setUp(self):
        self.conn = _fresh_db(self, "lock.db")
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        env_patcher = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        env_patcher.start(); self.addCleanup(env_patcher.stop)

    def test_round_trip_allows_sequential_runs(self):
        f = pull._try_lock()
        self.assertIsNotNone(f)
        pull._unlock(f)
        f2 = pull._try_lock()
        self.assertIsNotNone(f2)
        pull._unlock(f2)

    def test_held_lock_makes_backfill_exit_0_with_zero_adapter_calls(self):
        held = pull._try_lock()
        self.addCleanup(pull._unlock, held)
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = pull.main(["--backfill"])
        self.assertEqual(rc, 0)
        m.assert_not_called()

    def test_held_lock_makes_the_daily_hosted_pull_wait_then_exit_0_once_the_bound_elapses(self):
        """The daily pull runs once a day, so a held lock must not make it skip immediately --
        it waits up to LOCK_ESPERA_S (patched short here) before giving up the same way."""
        held = pull._try_lock()
        self.addCleanup(pull._unlock, held)
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        buf = io.StringIO()
        with mock.patch("body.pull.LOCK_ESPERA_S", 0.2), \
             mock.patch("body.pull.LOCK_SONDEO_S", 0.05), \
             mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            with contextlib.redirect_stdout(buf):
                rc = pull.main([])
        self.assertEqual(rc, 0)
        m.assert_not_called()
        self.assertIn("another pull is already running", buf.getvalue())

    def test_daily_hosted_pull_waits_for_the_lock_then_completes_once_it_is_released(self):
        held = pull._try_lock()
        timer = threading.Timer(0.2, pull._unlock, args=(held,))
        timer.start()
        self.addCleanup(timer.cancel)
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        with mock.patch("body.pull.LOCK_ESPERA_S", 5), \
             mock.patch("body.pull.LOCK_SONDEO_S", 0.05), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day()) as m:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = pull.main(["--days", "1"])
        self.assertEqual(rc, 0)
        m.assert_called()

    def test_daily_hosted_pull_also_takes_the_lock(self):
        """Proven from the inside: while the daily pull's own adapter call is in flight, a second
        attempt to take the same lock (a fresh handle, same non-blocking primitive) must fail --
        showing the daily pull itself is holding it, not skipping it. It must also let go again
        once it finishes."""
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        observed_during_run = []

        def fetch(day, token_str=None):
            observed_during_run.append(pull._try_lock())  # None <=> already held by this run
            return garmin_adapter.DayData(day=day, metrics={"resting_hr": 55.0}), token_str

        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=fetch):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = pull.main(["--days", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(observed_during_run, [None])

        f = pull._try_lock()  # released now that the run has finished
        self.assertIsNotNone(f, "the daily hosted pull left the lock held after it finished")
        pull._unlock(f)


class LocalModeRefusesBackfillTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db(self, "local.db")

    def test_local_mode_refuses_backfill_without_any_adapter_call(self):
        self.assertNotIn(cuenta.ISSUER_ENV, os.environ)
        with mock.patch("body.pull.garmin_adapter.fetch_day") as m:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = pull.main(["--backfill"])
        self.assertEqual(rc, 2)
        m.assert_not_called()


def _add_active_habit_goal(conn, person_id: int) -> int:
    gid = goals.propose(conn, person_id, "Steps", HOY.isoformat(), kind="milestone", deadline="2027-01-01")
    goals.activate(conn, gid)
    return gid


class WeeklyChecksHostedTest(unittest.TestCase):
    """BODY-SEGURIDAD-3-M: the hosted pull closes weeks, once each, for every person who can see
    an active goal -- connected to Garmin or not, pulled OK or failed today -- through the
    every-person sweep (`goals.personas_con_meta_activa` / `_cerrar_semanas_todas`), for the daily
    pull and for --backfill alike, and never letting a failure there count as a Garmin failure or
    stop the next person."""

    def setUp(self):
        self.conn = _fresh_db(self, "weekly_hosted.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)

    def test_daily_pull_closes_weeks_for_every_person_with_a_goal_not_only_connected_ones(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")  # person 1: connected
        _add_active_habit_goal(self.conn, 1)
        _add_active_habit_goal(self.conn, 2)  # person 2: an active goal, NO garmin_cuenta row
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day()), \
             mock.patch("body.pull.goals.cerrar_semanas", return_value=0) as m_cerrar:
            ok, failed, saved = pull.pull_hosted(self.conn, [HOY.isoformat()])
        self.assertEqual((ok, failed), (1, 0))  # only person 1 had a token to pull with
        self.assertEqual(m_cerrar.call_count, 2)  # both are swept regardless
        called_persons = sorted(c.args[1] for c in m_cerrar.call_args_list)
        self.assertEqual(called_persons, [1, 2])

    def test_person_whose_pull_raised_still_gets_their_weekly_checks(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        _add_active_habit_goal(self.conn, 1)
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                        side_effect=_stub_fetch_day(error_token="tok-a")), \
             mock.patch("body.pull.goals.cerrar_semanas", return_value=0) as m_cerrar:
            ok, failed, saved = pull.pull_hosted(self.conn, [HOY.isoformat()])
        self.assertEqual((ok, failed), (0, 1))  # the Garmin pull itself failed
        m_cerrar.assert_called_once_with(self.conn, 1, hoy=mock.ANY)  # the sweep still ran for them

    def test_nobody_is_closed_twice(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        garmin_cuenta.guardar(self.conn, 2, "tok-b")
        _add_active_habit_goal(self.conn, 1)
        _add_active_habit_goal(self.conn, 2)
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day()), \
             mock.patch("body.pull.goals.cerrar_semanas", return_value=0) as m_cerrar:
            pull.pull_hosted(self.conn, [HOY.isoformat()])
        self.assertEqual(m_cerrar.call_count, 2)  # not 3+: the per-person call inside the Garmin
        called_persons = [c.args[1] for c in m_cerrar.call_args_list]  # loop was removed, not kept
        self.assertEqual(sorted(called_persons), [1, 2])
        self.assertEqual(len(called_persons), len(set(called_persons)))

    def test_backfill_also_closes_weeks_for_every_person_with_a_goal(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        _add_active_habit_goal(self.conn, 1)
        _add_active_habit_goal(self.conn, 2)  # no garmin_cuenta row at all
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day()), \
             mock.patch("body.pull.goals.cerrar_semanas", return_value=0) as m_cerrar:
            ok, failed, saved = pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertEqual(ok, 1)
        self.assertEqual(m_cerrar.call_count, 2)
        called_persons = sorted(c.args[1] for c in m_cerrar.call_args_list)
        self.assertEqual(called_persons, [1, 2])

    def test_cerrar_semanas_failure_for_one_person_does_not_stop_the_next(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        garmin_cuenta.guardar(self.conn, 2, "tok-b")
        _add_active_habit_goal(self.conn, 1)
        _add_active_habit_goal(self.conn, 2)
        self.conn.commit()
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day()), \
             mock.patch("body.pull.goals.cerrar_semanas", side_effect=[RuntimeError("boom"), 1]), \
             contextlib.redirect_stdout(buf):
            ok, failed, saved = pull.pull_hosted(self.conn, [HOY.isoformat()])
        self.assertEqual((ok, failed), (2, 0))  # neither person counted as a Garmin failure
        self.assertIsNone(garmin_cuenta.ultimo_error(self.conn, 1))
        self.assertIsNone(garmin_cuenta.ultimo_error(self.conn, 2))
        self.assertIn("weekly checks failed: RuntimeError", buf.getvalue())
        self.assertIn("weekly checks: 1 rows", buf.getvalue())  # the second person still got theirs
        self.assertNotIn("boom", buf.getvalue())  # the exception TYPE only, never its message


class WeeklyChecksLocalTest(unittest.TestCase):
    """BODY-SEGURIDAD-3: the local daily sync is the one writer of goal_check in local mode too,
    calling goals.cerrar_semanas once after its day-pull, with the local person's own today."""

    def test_local_pull_calls_cerrar_semanas_with_the_local_persons_today(self):
        conn = _fresh_db(self, "weekly_local.db")
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                        return_value=garmin_adapter.DayData(day="2026-11-07", metrics={"resting_hr": 55.0})), \
             mock.patch("body.pull.goals.cerrar_semanas", return_value=2) as m_cerrar, \
             contextlib.redirect_stdout(buf):
            rc = pull.main(["--date", "2026-11-07"])
        self.assertEqual(rc, 0)
        m_cerrar.assert_called_once()
        call_args, call_kwargs = m_cerrar.call_args
        self.assertEqual(call_args[1], db.local_person_id(conn))
        self.assertIn("hoy", call_kwargs)
        self.assertIn("weekly checks: 2 rows", buf.getvalue())


class NoHealthFigureInOutputTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db(self, "print.db")
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        env_patcher = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        env_patcher.start(); self.addCleanup(env_patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")  # PAUSE_S would otherwise slow this file a lot
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)

    def test_backfill_prints_only_counts(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day()):
            with contextlib.redirect_stdout(buf):
                rc = pull.main(["--backfill"])
        self.assertEqual(rc, 0)
        # Nobody has a goal in this test's database, so the weekly-check sweep closes nobody and
        # prints nothing (BODY-SEGURIDAD-3-M: the sweep's person list comes from the goal table).
        self.assertEqual(buf.getvalue().strip(), "backfill: 1 ok, 0 failed, 15 days stored")


if __name__ == "__main__":
    unittest.main()
