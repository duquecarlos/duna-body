"""BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1: body/pull.py's own wiring of the on-demand
intraday fetch (pull.traer_intradia) into the scheduled pull -- pull._pull_intradia_pendientes,
and its call sites in the local default/--days path (pull._run, via pull.main) and the hosted
daily sweep (pull.pull_hosted). experimentos.dias_intradia_necesarios' own logic (which days,
newest first, Garmin's reach, already-stored skip) is tested in
tests/test_pull_intradia_experimentos.py; here it is mocked where the test only cares about the
wiring, and left real where a test's whole point is "no active experiment, no call". No network;
no real health figures, invented demo data only."""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "44" * 32


def _empty_fetch_day(day, token_str=None):
    data = garmin_adapter.DayData(day=day)
    return data if token_str is None else (data, token_str)


class PullIntradiaPendientesTest(unittest.TestCase):
    """Unit-level coverage of the helper itself, decoupled from experimentos.dias_intradia_
    necesarios' own logic (mocked here) and from the local/hosted call sites (tested below)."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "helper.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.person_id = db.local_person_id(self.conn)

    def test_no_pending_days_makes_no_call_and_returns_zero(self):
        with mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=[]), \
             mock.patch("body.pull.traer_intradia") as m_traer:
            fetched = pull._pull_intradia_pendientes(self.conn, self.person_id, date(2026, 11, 9))
        self.assertEqual(fetched, 0)
        m_traer.assert_not_called()

    def test_fetches_up_to_the_cap_in_the_given_newest_first_order(self):
        dias = [f"2026-11-{d:02d}" for d in range(20, 1, -1)]  # 19 candidate days, newest first
        visitados = []
        with mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=dias), \
             mock.patch("body.pull.traer_intradia", side_effect=lambda c, p, d: visitados.append(d)):
            fetched = pull._pull_intradia_pendientes(self.conn, self.person_id, date(2026, 11, 20))
        self.assertEqual(fetched, pull.INTRADIA_MAX_POR_CORRIDA)
        self.assertEqual(visitados, dias[:pull.INTRADIA_MAX_POR_CORRIDA])
        self.assertLess(pull.INTRADIA_MAX_POR_CORRIDA, len(dias))  # the cap actually bound this run

    def test_a_fetch_error_on_one_day_leaves_the_others_fetched_and_the_run_successful(self):
        dias = ["2026-11-03", "2026-11-02", "2026-11-01"]

        def _traer(c, p, d):
            if d == "2026-11-02":
                raise RuntimeError("boom")

        with mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=dias), \
             mock.patch("body.pull.traer_intradia", side_effect=_traer):
            fetched = pull._pull_intradia_pendientes(self.conn, self.person_id, date(2026, 11, 3))
        self.assertEqual(fetched, 2)

    def test_hosted_token_path_persists_a_refreshed_token(self):
        with mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=["2026-11-05"]), \
             mock.patch("body.pull.traer_intradia", return_value=("fetched", "tok-after")) as m_traer:
            fetched = pull._pull_intradia_pendientes(
                self.conn, self.person_id, date(2026, 11, 5), token_str="tok-before")
            self.assertEqual(fetched, 1)
            m_traer.assert_called_once_with(self.conn, self.person_id, "2026-11-05", token_str="tok-before")
            self.assertEqual(garmin_cuenta.leer(self.conn, self.person_id), "tok-after")

    def test_local_path_never_touches_garmin_cuenta(self):
        with mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=["2026-11-05"]), \
             mock.patch("body.pull.traer_intradia", return_value="fetched"), \
             mock.patch("body.pull.garmin_cuenta.guardar") as m_guardar:
            pull._pull_intradia_pendientes(self.conn, self.person_id, date(2026, 11, 5))
        m_guardar.assert_not_called()


class LocalScheduledPullWiringTest(unittest.TestCase):
    """pull.main / pull._run -- the local default/--days path (manana's own --days 1, and the
    plain scheduled daily sync's default --days 2)."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "local.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_default_pull_fetches_the_days_experimentos_says_are_needed(self):
        person_id = db.local_person_id(self.conn)
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=["2026-11-08"]), \
             mock.patch("body.pull.traer_intradia") as m_traer:
            codigo = pull.main([])
        self.assertEqual(codigo, 0)
        m_traer.assert_called_once_with(self.conn, person_id, "2026-11-08")

    def test_explicit_date_never_triggers_an_intraday_fetch(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios") as m_needed, \
             mock.patch("body.pull.traer_intradia") as m_traer:
            codigo = pull.main(["--date", "2026-11-05"])
        self.assertEqual(codigo, 0)
        m_needed.assert_not_called()
        m_traer.assert_not_called()

    def test_no_active_experiment_makes_no_intraday_garmin_call(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.garmin_adapter.fetch_intraday") as m_intraday:
            codigo = pull.main([])
        self.assertEqual(codigo, 0)
        m_intraday.assert_not_called()


class HostedScheduledPullWiringTest(unittest.TestCase):
    """pull.pull_hosted -- the daily hosted sweep, per connected person."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "hosted.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        env_patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        garmin_cuenta.guardar(self.conn, 1, "tok-before")
        self.conn.commit()

    def test_implicit_today_sweep_fetches_pending_intraday_and_persists_the_refreshed_token(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=["2026-11-08"]), \
             mock.patch("body.pull.traer_intradia", return_value=("fetched", "tok-after")) as m_traer:
            ok, failed, saved = pull.pull_hosted(self.conn)
        self.assertEqual((ok, failed), (1, 0))
        m_traer.assert_called_once_with(self.conn, 1, "2026-11-08", token_str="tok-before")
        self.assertEqual(garmin_cuenta.leer(self.conn, 1), "tok-after")

    def test_explicit_fecha_never_triggers_an_intraday_fetch(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios") as m_needed:
            pull.pull_hosted(self.conn, fecha="2026-11-09")
        m_needed.assert_not_called()

    def test_no_active_experiment_makes_no_intraday_garmin_call(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.garmin_adapter.fetch_intraday") as m_intraday:
            pull.pull_hosted(self.conn)
        m_intraday.assert_not_called()


if __name__ == "__main__":
    unittest.main()
