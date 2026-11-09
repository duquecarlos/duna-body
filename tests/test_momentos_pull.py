"""BODY-MOMENTOS-PULL-1: intraday heart-rate/stress storage (db.guardar_intradia/intradia_
almacenado/intradia) and the on-demand pull (pull.traer_intradia/traer_intradia_rango) and CLI
task (body.vivo's `intradia`, tested in tests/test_vivo.py) built on BODY-EXPERIMENTOS-1-INV slice
7 with BODY-INTRADIA-SPOTCHECK-1's measured facts. No network: every Garmin call goes through
garmin_adapter.fetch_intraday, stubbed. Invented demo data only, no real health figures.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "44" * 32

HR_PAYLOAD = {
    "heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
    "heartRateValues": [[1762300800000, 58], [1762300920000, 61]],
}
STRESS_PAYLOAD = {
    "stressValuesArray": [[1762300800000, 20], [1762300980000, -1]],  # -1 is Garmin's no-reading code
}


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


def _intraday_data(day, hr_payload=HR_PAYLOAD, stress_payload=STRESS_PAYLOAD, zone="America/Bogota"):
    return garmin_adapter.parse_intraday(day, {"heart_rate": hr_payload, "stress": stress_payload}, zone)


class StorageRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "storage.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        self.conn.commit()

    def test_round_trip_parses_with_the_adapters_own_parser(self):
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", HR_PAYLOAD)
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_estres", STRESS_PAYLOAD)
        hr = db.intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", "America/Bogota")
        stress = db.intradia(self.conn, self.person_id, "2026-11-05", "intradia_estres", "America/Bogota")
        self.assertEqual(len(hr), 2)
        self.assertEqual([v for _, v in hr], [58.0, 61.0])
        self.assertEqual(len(stress), 1)  # the -1 sample is dropped on read, not stored differently
        self.assertEqual(stress[0][1], 20.0)

    def test_a_parser_fix_changes_what_a_stored_payload_reads_back_as(self):
        """The payload is stored once; patching the adapter's parser (simulating a later fix)
        changes db.intradia's answer with no new write -- the contract this slice is built on."""
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", HR_PAYLOAD)
        before = db.intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", "America/Bogota")
        self.assertEqual(len(before), 2)
        with mock.patch("body.garmin_adapter.parse_heart_rate_series", return_value=(("fixed", 99.0),)):
            after = db.intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", "America/Bogota")
        self.assertEqual(after, [("fixed", 99.0)])

    def test_unstored_day_reads_as_empty(self):
        self.assertEqual(db.intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", "America/Bogota"), [])

    def test_out_of_retention_marker_reads_as_empty_not_an_error(self):
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", None)
        self.assertEqual(db.intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", "America/Bogota"), [])

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "bogus", {})
        with self.assertRaises(ValueError):
            db.intradia(self.conn, self.person_id, "2026-11-05", "bogus", "America/Bogota")


class AlmacenadoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "almacenado.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        self.conn.commit()

    def test_false_before_true_after(self):
        self.assertFalse(db.intradia_almacenado(self.conn, self.person_id, "2026-11-05"))
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", HR_PAYLOAD)
        self.assertTrue(db.intradia_almacenado(self.conn, self.person_id, "2026-11-05"))

    def test_an_empty_marker_counts_as_stored(self):
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", None)
        self.assertTrue(db.intradia_almacenado(self.conn, self.person_id, "2026-11-05"))


class PersonIsolationTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "scope.db")
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        db.guardar_intradia(self.conn, 1, "2026-11-05", "intradia_fc", HR_PAYLOAD)

    def test_person_b_sees_nothing_person_a_stored(self):
        self.assertFalse(db.intradia_almacenado(self.conn, 2, "2026-11-05"))
        self.assertEqual(db.intradia(self.conn, 2, "2026-11-05", "intradia_fc", "America/Bogota"), [])
        self.assertTrue(db.intradia_almacenado(self.conn, 1, "2026-11-05"))


class TraerIntradiaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "traer.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        self.conn.commit()

    def test_local_mode_fetches_stores_both_kinds_and_returns_fetched(self):
        data = _intraday_data("2026-11-05")
        with mock.patch("body.pull.garmin_adapter.fetch_intraday", return_value=data) as m:
            estado = pull.traer_intradia(self.conn, self.person_id, "2026-11-05")
        m.assert_called_once_with("2026-11-05", "America/Bogota")
        self.assertEqual(estado, "fetched")
        self.assertTrue(db.intradia_almacenado(self.conn, self.person_id, "2026-11-05"))
        self.assertEqual(len(db.intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", "America/Bogota")), 2)

    def test_out_of_retention_day_is_reported_empty_and_stores_the_marker(self):
        data = garmin_adapter.IntradayData(day="2026-07-01", raw={"heart_rate": None, "stress": None})
        with mock.patch("body.pull.garmin_adapter.fetch_intraday", return_value=data):
            estado = pull.traer_intradia(self.conn, self.person_id, "2026-07-01")
        self.assertEqual(estado, "empty")
        self.assertTrue(db.intradia_almacenado(self.conn, self.person_id, "2026-07-01"))

    def test_already_stored_day_skips_the_garmin_call(self):
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", HR_PAYLOAD)
        with mock.patch("body.pull.garmin_adapter.fetch_intraday") as m:
            estado = pull.traer_intradia(self.conn, self.person_id, "2026-11-05")
        m.assert_not_called()
        self.assertEqual(estado, "already_stored")

    def test_forzar_refetches_an_already_stored_day(self):
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", HR_PAYLOAD)
        data = _intraday_data("2026-11-05")
        with mock.patch("body.pull.garmin_adapter.fetch_intraday", return_value=data) as m:
            estado = pull.traer_intradia(self.conn, self.person_id, "2026-11-05", forzar=True)
        m.assert_called_once()
        self.assertEqual(estado, "fetched")

    def test_hosted_token_path_returns_the_status_and_the_refreshed_token(self):
        data = _intraday_data("2026-11-05")
        with mock.patch("body.pull.garmin_adapter.fetch_intraday", return_value=(data, "tok-after")) as m:
            estado, token_after = pull.traer_intradia(self.conn, self.person_id, "2026-11-05", token_str="tok-before")
        m.assert_called_once_with("2026-11-05", "America/Bogota", token_str="tok-before")
        self.assertEqual((estado, token_after), ("fetched", "tok-after"))

    def test_already_stored_with_token_still_returns_the_pair(self):
        db.guardar_intradia(self.conn, self.person_id, "2026-11-05", "intradia_fc", HR_PAYLOAD)
        estado, token_after = pull.traer_intradia(self.conn, self.person_id, "2026-11-05", token_str="tok-before")
        self.assertEqual((estado, token_after), ("already_stored", "tok-before"))

    def test_uses_the_persons_own_timezone(self):
        self.conn.execute("UPDATE person SET timezone = 'Europe/Madrid' WHERE id = ?", (self.person_id,))
        self.conn.commit()
        data = _intraday_data("2026-11-05")
        with mock.patch("body.pull.garmin_adapter.fetch_intraday", return_value=data) as m:
            pull.traer_intradia(self.conn, self.person_id, "2026-11-05")
        m.assert_called_once_with("2026-11-05", "Europe/Madrid")


class TraerIntradiaRangoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "rango.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        self.conn.commit()
        sleep_patcher = mock.patch("body.pull.time.sleep")
        self.mock_sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_pulls_oldest_first_with_a_pause_between_calls(self):
        visitados = []

        def _fake_traer(conn, person_id, day, token_str=None, forzar=False):
            visitados.append(day)
            return "fetched"

        with mock.patch("body.pull.traer_intradia", side_effect=_fake_traer):
            fetched, stored, empty = pull.traer_intradia_rango(
                self.conn, self.person_id, date(2026, 11, 1), date(2026, 11, 4), pausa_s=2.0)

        self.assertEqual(visitados, ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"])
        self.assertEqual((fetched, stored, empty), (4, 0, 0))
        self.assertEqual(self.mock_sleep.call_count, 3)  # one fewer than the number of days
        self.mock_sleep.assert_called_with(2.0)

    def test_counts_fetched_already_stored_and_empty_separately(self):
        estados = iter(["fetched", "already_stored", "empty"])

        def _fake_traer(conn, person_id, day, token_str=None, forzar=False):
            return next(estados)

        with mock.patch("body.pull.traer_intradia", side_effect=_fake_traer):
            fetched, stored, empty = pull.traer_intradia_rango(
                self.conn, self.person_id, date(2026, 11, 1), date(2026, 11, 3))

        self.assertEqual((fetched, stored, empty), (1, 1, 1))

    def test_stops_cleanly_on_a_sign_in_error_and_keeps_the_counts_so_far(self):
        def _fake_traer(conn, person_id, day, token_str=None, forzar=False):
            if day == "2026-11-02":
                raise garmin_adapter.NotSignedIn("token rejected")
            return "fetched"

        with mock.patch("body.pull.traer_intradia", side_effect=_fake_traer):
            fetched, stored, empty = pull.traer_intradia_rango(
                self.conn, self.person_id, date(2026, 11, 1), date(2026, 11, 5))

        self.assertEqual((fetched, stored, empty), (1, 0, 0))

    def test_refuses_a_desde_older_than_the_measured_retention(self):
        desde = date.today() - timedelta(days=pull.INTRADIA_ALCANCE_DIAS + 1)
        with self.assertRaises(ValueError):
            pull.traer_intradia_rango(self.conn, self.person_id, desde, date.today())

    def test_accepts_a_desde_exactly_at_the_retention_boundary(self):
        desde = date.today() - timedelta(days=pull.INTRADIA_ALCANCE_DIAS)
        with mock.patch("body.pull.traer_intradia", return_value="already_stored"):
            fetched, stored, empty = pull.traer_intradia_rango(self.conn, self.person_id, desde, desde)
        self.assertEqual((fetched, stored, empty), (0, 1, 0))


class IntradiaNotCalledByAnyJobTest(unittest.TestCase):
    """Nothing scheduled calls traer_intradia / traer_intradia_rango / fetch_intraday in this
    slice: pull_all, pull_hosted and pull_backfill_hosted are unchanged. Stubs fetch_intraday with
    a Mock that must stay unvisited."""

    def setUp(self):
        self.conn = _db(self, "jobs.db")
        db.init(self.conn)
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)

    @staticmethod
    def _empty_fetch_day(day, token_str=None):
        data = garmin_adapter.DayData(day=day)
        return data if token_str is None else (data, token_str)

    def test_pull_all_never_calls_fetch_intraday(self):
        person_id = db.local_person_id(self.conn)
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=self._empty_fetch_day), \
             mock.patch("body.pull.garmin_adapter.fetch_intraday") as m_intraday:
            pull.pull_all(self.conn, person_id)
        m_intraday.assert_not_called()

    def test_pull_hosted_never_calls_fetch_intraday(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=self._empty_fetch_day), \
             mock.patch("body.pull.garmin_adapter.fetch_intraday") as m_intraday:
            pull.pull_hosted(self.conn, fecha="2026-11-09")
        m_intraday.assert_not_called()

    def test_pull_backfill_hosted_never_calls_fetch_intraday(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-a")
        self.conn.commit()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=self._empty_fetch_day), \
             mock.patch("body.pull.garmin_adapter.fetch_intraday") as m_intraday:
            pull.pull_backfill_hosted(self.conn, hoy=date(2026, 11, 9))
        m_intraday.assert_not_called()


class CliIntradiaFlagTest(unittest.TestCase):
    """body.pull's own --intradia/--dias flags (the task body.vivo's `intradia` runs through)."""

    def setUp(self):
        self.conn = _db(self, "cli.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def test_intradia_flag_runs_the_range_for_the_local_person_over_dias_days(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.traer_intradia_rango", return_value=(1, 2, 3)) as m:
            codigo = pull.main(["--intradia", "--dias", "5"])
        self.assertEqual(codigo, 0)
        m.assert_called_once()
        args = m.call_args[0]
        self.assertEqual(args[0], self.conn)
        desde, hasta = args[2], args[3]
        self.assertEqual((hasta - desde).days, 4)  # 5 days inclusive of today
        self.assertEqual(hasta, date.today())

    def test_dias_is_clamped_to_the_measured_reach(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.traer_intradia_rango", return_value=(0, 0, 0)) as m:
            pull.main(["--intradia", "--dias", str(pull.INTRADIA_ALCANCE_DIAS + 50)])
        desde, hasta = m.call_args[0][2], m.call_args[0][3]
        self.assertEqual((hasta - desde).days, pull.INTRADIA_ALCANCE_DIAS - 1)

    def test_hosted_refuses_intradia(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.cuenta.hosted", return_value=True), \
             mock.patch("body.pull.traer_intradia_rango") as m:
            codigo = pull.main(["--intradia"])
        self.assertEqual(codigo, 1)
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
