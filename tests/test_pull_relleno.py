"""BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 1: the hosted daily pull heals its own gaps -- today and
yesterday always, plus every day in the last garmin_cuenta.VENTANA_RELLENO days with no stored
Garmin daily row (`daily_metric` where `source = 'garmin'`) -- instead of a fixed N-day window that
never looks further back once the 90-day backfill is done. Invented demo data only, no network;
tests/_isolate.py guards the real database.
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "77" * 32
HOY = date(2026, 11, 14)


class DiasPendientesRellenoTest(unittest.TestCase):
    """Unit-level coverage of pull._dias_pendientes_relleno's own gap logic, decoupled from the
    Garmin calls and from pull_hosted's per-person loop (tested below)."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "relleno.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)

    def _marcar_dia(self, day: str) -> None:
        db.set_metric(self.conn, day, "resting_hr", 55, "garmin", self.person_id)
        self.conn.commit()

    def test_a_full_fourteen_day_gap_is_filled_oldest_first(self):
        dias = pull._dias_pendientes_relleno(self.conn, self.person_id, HOY)
        esperado = sorted((HOY - timedelta(n)).isoformat() for n in range(garmin_cuenta.VENTANA_RELLENO))
        self.assertEqual(dias, esperado)

    def test_a_stored_day_inside_the_window_is_not_refetched(self):
        hace_cinco = (HOY - timedelta(5)).isoformat()
        self._marcar_dia(hace_cinco)
        dias = pull._dias_pendientes_relleno(self.conn, self.person_id, HOY)
        self.assertNotIn(hace_cinco, dias)
        # every other gap day in the window is still there
        self.assertEqual(len(dias), garmin_cuenta.VENTANA_RELLENO - 1)

    def test_today_and_yesterday_are_always_included_even_when_already_stored(self):
        hoy_iso, ayer_iso = HOY.isoformat(), (HOY - timedelta(1)).isoformat()
        self._marcar_dia(hoy_iso)
        self._marcar_dia(ayer_iso)
        dias = pull._dias_pendientes_relleno(self.conn, self.person_id, HOY)
        self.assertIn(hoy_iso, dias)
        self.assertIn(ayer_iso, dias)

    def test_a_day_outside_the_window_is_never_pulled_for_being_missing(self):
        fuera = (HOY - timedelta(garmin_cuenta.VENTANA_RELLENO)).isoformat()  # one day older than the window
        dias = pull._dias_pendientes_relleno(self.conn, self.person_id, HOY)
        self.assertNotIn(fuera, dias)

    def test_the_cap_keeps_today_and_yesterday_and_trims_the_oldest_gaps_first(self):
        with mock.patch.object(pull, "MAX_DIAS_POR_CORRIDA", 5):
            dias = pull._dias_pendientes_relleno(self.conn, self.person_id, HOY)
        self.assertEqual(len(dias), 5)
        hoy_iso, ayer_iso = HOY.isoformat(), (HOY - timedelta(1)).isoformat()
        self.assertIn(hoy_iso, dias)
        self.assertIn(ayer_iso, dias)
        # the newest three gap days before yesterday, not the oldest three
        self.assertEqual(dias, sorted({hoy_iso, ayer_iso, (HOY - timedelta(2)).isoformat(),
                                        (HOY - timedelta(3)).isoformat(), (HOY - timedelta(4)).isoformat()}))

    def test_sixteen_default_cap_never_trims_the_fourteen_day_window(self):
        dias = pull._dias_pendientes_relleno(self.conn, self.person_id, HOY)
        self.assertLessEqual(len(dias), pull.MAX_DIAS_POR_CORRIDA)
        self.assertEqual(len(dias), garmin_cuenta.VENTANA_RELLENO)

    def test_the_gap_is_found_in_one_query(self):
        statements = []
        self.conn.set_trace_callback(lambda sql: statements.append(sql))
        try:
            pull._dias_pendientes_relleno(self.conn, self.person_id, HOY)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1, statements)


def _stub_fetch_day(visitados: list, errores: frozenset = frozenset()):
    def fetch(day, token_str=None):
        visitados.append(day)
        if day in errores:
            raise ValueError("demo failure, not a real Garmin error")
        data = garmin_adapter.DayData(day=day, metrics={"resting_hr": 55.0})
        return data, token_str
    return fetch


class PullHostedHealsGapsTest(unittest.TestCase):
    """pull_hosted's own implicit "today" sweep (no --date, no --days): the healing set from
    _dias_pendientes_relleno, run through the real per-person loop."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "hosted_relleno.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        env_patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        env_patcher.start(); self.addCleanup(env_patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)
        instant = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)  # 10:00 in America/Bogota
        clock_patcher = mock.patch(
            "body.fechas.datetime", **{"now.side_effect": lambda tz=None: instant.astimezone(tz) if tz else instant})
        clock_patcher.start(); self.addCleanup(clock_patcher.stop)
        garmin_cuenta.guardar(self.conn, self.person_id, "tok-demo")
        self.conn.commit()

    def test_a_brand_new_connection_heals_the_whole_window_in_one_run(self):
        visitados: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(visitados)):
            ok, failed, saved = pull.pull_hosted(self.conn)
        self.assertEqual((ok, failed), (1, 0))
        self.assertEqual(saved, garmin_cuenta.VENTANA_RELLENO)
        self.assertEqual(visitados, sorted(visitados))  # oldest first
        self.assertEqual(len(db.days_with_data(self.conn, self.person_id)), garmin_cuenta.VENTANA_RELLENO)

    def test_an_already_stored_gap_day_is_not_refetched(self):
        hace_tres = (HOY - timedelta(3)).isoformat()
        db.set_metric(self.conn, hace_tres, "resting_hr", 55, "garmin", self.person_id)
        self.conn.commit()
        visitados: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(visitados)):
            pull.pull_hosted(self.conn)
        self.assertNotIn(hace_tres, visitados)

    def test_a_mid_list_failure_keeps_the_days_already_stored_and_records_the_error(self):
        hace_siete = (HOY - timedelta(7)).isoformat()
        visitados: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                         side_effect=_stub_fetch_day(visitados, errores=frozenset({hace_siete}))):
            ok, failed, saved = pull.pull_hosted(self.conn)
        self.assertEqual((ok, failed), (0, 1))
        self.assertGreater(saved, 0)  # the days before the failure (oldest-first) were kept
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, self.person_id), "ValueError")
        dias_guardados = set(db.days_with_data(self.conn, self.person_id))
        self.assertEqual(dias_guardados, {d for d in visitados if d != hace_siete})

    def test_an_explicit_days_call_keeps_the_old_plain_behaviour(self):
        """`--days N` (an explicit call) still pulls exactly today and the N-1 days before it,
        never the healing set -- ruling 1's own carve-out."""
        visitados: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_fetch_day(visitados)):
            ok, failed, saved = pull.pull_hosted(self.conn, n_days=2)
        self.assertEqual((ok, failed, saved), (1, 0, 2))
        self.assertEqual(sorted(visitados), [(HOY - timedelta(1)).isoformat(), HOY.isoformat()])


if __name__ == "__main__":
    unittest.main()
