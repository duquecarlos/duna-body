"""BODY-GARMIN-RELLENO-ANTIGUO-1: BODY-GARMIN-DIA-VACIO-2-INV (OBSERVED) found a dead end -- a
Garmin day that came back empty is only ever refetched while it sits inside
garmin_cuenta.VENTANA_RELLENO (14 days), and the daily pull's own backfill marker advances past an
empty day and never revisits it, so a day Garmin did not have yet when Body asked becomes a
permanent hole on its 15th day.

The orchestrator's rulings: the DAILY pull (pull_hosted's own implicit-today branch -- never the
--hoy sweep, never connect, never backfill) also retries gap days OLDER than VENTANA_RELLENO and
inside the backfill's own 90-day horizon (BACKFILL_DIAS), newest first, at most
garmin_cuenta.RELLENO_ANTIGUO_POR_CORRIDA (5) per person per run, AFTER today/yesterday and the
14-day gaps and only in the room they leave under MAX_DIAS_POR_CORRIDA. A day older than
BACKFILL_DIAS is never retried. The daily job line gains ", {k} older retried" after its empty
count.

Invented demo data only, no network: every Garmin call here goes through a fake
body.pull.garmin_adapter.fetch_day/fetch_device_last_sync.
"""
import contextlib
import io
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "33" * 32
HOY = date(2026, 11, 17)
_INSTANTE = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)  # 10:00 in America/Bogota


def _conn(case: unittest.TestCase, name: str):
    """A fresh, isolated database file under TMP -- follows test_garmin_dia_vacio.py's own `_conn`
    pattern, never a path relative to the repo root, never shared across tests."""
    orig_db_path = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    db.init(conn)
    return conn


def _marcar(conn, day: str, person_id: int = 1) -> None:
    """Writes one real (non-empty) Garmin daily_metric row directly -- the "has data" fixture
    every scenario here seeds with, bypassing a real pull."""
    db.set_metric(conn, day, "resting_hr", 55, "garmin", person_id)


def _stub_siempre_con_dato(visitados: list[str] | None = None):
    """Answers ANY requested day with a real (non-empty) value, recording the day visited when a
    list is given -- the fetch stub for every end-to-end scenario below, which cares about WHICH
    days get requested, never about an empty-day branch (that is BODY-GARMIN-DIA-VACIO-1's own
    concern, not this slice's)."""
    def fetch(day, token_str=None):
        if visitados is not None:
            visitados.append(day)
        return garmin_adapter.DayData(day=day, metrics={"resting_hr": 55.0}), token_str
    return fetch


class _HostedPullCase(unittest.TestCase):
    """Shared setup for every test that drives a real pull_hosted/pull_hoy_todos/
    pull_backfill_hosted/pull_persona_ahora call: one connected person, no real sleep delay, no
    real watch-last-sync read, a fixed clock so 'today' is HOY in the person's own (default)
    zone."""

    DB_NAME = "relleno_antiguo.db"

    def setUp(self):
        self.conn = _conn(self, self.DB_NAME)
        patcher = mock.patch.dict("os.environ", {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)
        reloj_patcher = mock.patch(
            "body.pull.garmin_adapter.fetch_device_last_sync",
            side_effect=lambda token_str=None: (None, token_str))
        reloj_patcher.start(); self.addCleanup(reloj_patcher.stop)
        clock_patcher = mock.patch("body.fechas.datetime", **{
            "now.side_effect": lambda tz=None: _INSTANTE.astimezone(tz) if tz else _INSTANTE})
        clock_patcher.start(); self.addCleanup(clock_patcher.stop)
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        self.conn.commit()


# ---- ruling 1: garmin_cuenta.dias_sin_datos_antiguos, unit-level ---------------------------------

class DiasSinDatosAntiguosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "antiguos.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()

    def test_a_day_just_past_the_fourteen_day_window_is_a_candidate(self):
        justo_fuera = (HOY - timedelta(garmin_cuenta.VENTANA_RELLENO)).isoformat()
        dias = garmin_cuenta.dias_sin_datos_antiguos(self.conn, 1, HOY, 90)
        self.assertIn(justo_fuera, dias)

    def test_newest_first_ordering(self):
        dias = garmin_cuenta.dias_sin_datos_antiguos(self.conn, 1, HOY, 20)
        self.assertEqual(dias, sorted(dias, reverse=True))
        self.assertEqual(len(dias), len(set(dias)))

    def test_a_day_outside_limite_dias_is_never_a_candidate(self):
        fuera = (HOY - timedelta(95)).isoformat()
        dias = garmin_cuenta.dias_sin_datos_antiguos(self.conn, 1, HOY, 90)
        self.assertNotIn(fuera, dias)

    def test_a_stored_day_is_excluded(self):
        dia = (HOY - timedelta(20)).isoformat()
        _marcar(self.conn, dia)
        self.conn.commit()
        dias = garmin_cuenta.dias_sin_datos_antiguos(self.conn, 1, HOY, 90)
        self.assertNotIn(dia, dias)

    def test_another_persons_row_never_counts(self):
        dia = (HOY - timedelta(20)).isoformat()
        _marcar(self.conn, dia, person_id=2)
        self.conn.commit()
        self.assertIn(dia, garmin_cuenta.dias_sin_datos_antiguos(self.conn, 1, HOY, 90))
        self.assertNotIn(dia, garmin_cuenta.dias_sin_datos_antiguos(self.conn, 2, HOY, 90))

    def test_a_manual_row_never_counts(self):
        dia = (HOY - timedelta(20)).isoformat()
        self.conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES "
            "(1, ?, 'resting_hr', 55, 'manual')", (dia,))
        self.conn.commit()
        self.assertIn(dia, garmin_cuenta.dias_sin_datos_antiguos(self.conn, 1, HOY, 90))

    def test_a_limite_dias_not_past_the_window_returns_nothing(self):
        self.assertEqual(
            garmin_cuenta.dias_sin_datos_antiguos(self.conn, 1, HOY, garmin_cuenta.VENTANA_RELLENO), [])

    def test_one_query(self):
        statements = []
        self.conn.set_trace_callback(lambda sql: statements.append(sql))
        try:
            garmin_cuenta.dias_sin_datos_antiguos(self.conn, 1, HOY, 90)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1, statements)


# ---- rulings 1 & 3: pull._dias_pendientes_relleno(incluir_antiguos=True), unit-level -------------

class DiasPendientesRellenoAntiguosTest(_HostedPullCase):
    def test_incluir_antiguos_false_returns_a_plain_list_unchanged(self):
        dias = pull._dias_pendientes_relleno(self.conn, 1, HOY)
        self.assertIsInstance(dias, list)
        self.assertEqual(len(dias), garmin_cuenta.VENTANA_RELLENO)

    def test_a_full_fourteen_day_gap_leaves_only_the_remaining_room_for_older_retries(self):
        dias, antiguos = pull._dias_pendientes_relleno(self.conn, 1, HOY, incluir_antiguos=True)
        esperado = min(pull.MAX_DIAS_POR_CORRIDA - garmin_cuenta.VENTANA_RELLENO,
                        garmin_cuenta.RELLENO_ANTIGUO_POR_CORRIDA)
        self.assertEqual(len(antiguos), esperado)
        self.assertTrue(antiguos.issubset(set(dias)))

    def test_with_every_fourteen_day_already_stored_up_to_five_older_gaps_fill_in(self):
        for n in range(garmin_cuenta.VENTANA_RELLENO):
            _marcar(self.conn, (HOY - timedelta(n)).isoformat())
        self.conn.commit()
        dias, antiguos = pull._dias_pendientes_relleno(self.conn, 1, HOY, incluir_antiguos=True)
        self.assertEqual(len(antiguos), garmin_cuenta.RELLENO_ANTIGUO_POR_CORRIDA)
        # every 14-day day already has a value, so `dias` holds only today/yesterday (n=2
        # obligatorios, never refetched for already having data) plus the older retries.
        self.assertEqual(len(dias), 2 + garmin_cuenta.RELLENO_ANTIGUO_POR_CORRIDA)


# ---- ruling 1, through the real daily pull (pull_hosted) -----------------------------------------

class GapAntiguoSanadoYNoRefetchedTest(_HostedPullCase):
    """A gap older than VENTANA_RELLENO is fetched by the daily pull; once Garmin returns a real
    value for it, the next run finds it already stored and does not ask again."""

    def test_a_twenty_day_old_gap_is_fetched_once_then_left_alone(self):
        for n in range(garmin_cuenta.VENTANA_RELLENO):
            _marcar(self.conn, (HOY - timedelta(n)).isoformat())
        hace_20 = (HOY - timedelta(20)).isoformat()
        with mock.patch.object(pull, "BACKFILL_DIAS", 25):
            for n in range(garmin_cuenta.VENTANA_RELLENO, 25):
                dia = (HOY - timedelta(n)).isoformat()
                if dia != hace_20:
                    _marcar(self.conn, dia)
            self.conn.commit()

            visitados: list[str] = []
            with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato(visitados)):
                pull.pull_hosted(self.conn)
            self.assertIn(hace_20, visitados)

            visitados.clear()
            with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato(visitados)):
                pull.pull_hosted(self.conn)
        self.assertNotIn(hace_20, visitados)


class OchoHuecosAntiguosTest(_HostedPullCase):
    """Ruling 1: at most RELLENO_ANTIGUO_POR_CORRIDA (5) older gaps per run, newest first --
    eight gaps take the five newest this run, the remaining three the next."""

    def test_eight_older_gaps_take_five_this_run_and_three_the_next(self):
        for n in range(garmin_cuenta.VENTANA_RELLENO):
            _marcar(self.conn, (HOY - timedelta(n)).isoformat())
        huecos = [(HOY - timedelta(n)).isoformat() for n in range(14, 22)]  # 8 gaps
        with mock.patch.object(pull, "BACKFILL_DIAS", 30):
            for n in range(14, 30):
                dia = (HOY - timedelta(n)).isoformat()
                if dia not in huecos:
                    _marcar(self.conn, dia)
            self.conn.commit()

            visitados: list[str] = []
            with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato(visitados)):
                pull.pull_hosted(self.conn)
            cinco_mas_nuevos = set(sorted(huecos, reverse=True)[:5])
            self.assertEqual(set(visitados) & set(huecos), cinco_mas_nuevos)

            visitados.clear()
            with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato(visitados)):
                pull.pull_hosted(self.conn)
        tres_restantes = set(huecos) - cinco_mas_nuevos
        self.assertEqual(set(visitados) & set(huecos), tres_restantes)
        self.assertEqual(len(tres_restantes), 3)


class GapFueraDeHorizonteTest(_HostedPullCase):
    """Ruling 1: a day older than the backfill's own 90-day horizon is never retried, even with
    the real (unpatched) BACKFILL_DIAS."""

    def test_a_ninety_five_day_old_gap_is_never_fetched(self):
        for n in range(garmin_cuenta.VENTANA_RELLENO):
            _marcar(self.conn, (HOY - timedelta(n)).isoformat())
        self.conn.commit()
        hace_95 = (HOY - timedelta(95)).isoformat()
        visitados: list[str] = []
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato(visitados)):
            pull.pull_hosted(self.conn)
        self.assertNotIn(hace_95, visitados)


class PrioridadVentanaSobreAntiguosTest(_HostedPullCase):
    """Ruling 3: a gap inside the 14-day window is still fetched before any older one -- under a
    tight cap, the recent gap takes the only room left and no older retry is attempted at all."""

    def test_a_recent_gap_wins_the_room_over_an_older_one(self):
        hace_5 = (HOY - timedelta(5)).isoformat()
        for n in range(2, garmin_cuenta.VENTANA_RELLENO):
            dia = (HOY - timedelta(n)).isoformat()
            if dia != hace_5:
                _marcar(self.conn, dia)
        self.conn.commit()
        hace_20 = (HOY - timedelta(20)).isoformat()  # an older gap, left unseeded on purpose
        visitados: list[str] = []
        with mock.patch.object(pull, "MAX_DIAS_POR_CORRIDA", 3), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato(visitados)):
            pull.pull_hosted(self.conn)
        self.assertIn(hace_5, visitados)
        self.assertNotIn(hace_20, visitados)


# ---- ruling 2: only the daily pull gets older retries ---------------------------------------------

class SoloElPullDiarioTest(_HostedPullCase):
    """Measured callers of `pull._dias_pendientes_relleno`: `pull_hosted` (the daily/--days path)
    and `pull_persona_ahora` (the connect path, body/pull.py:593 at measurement time). The
    today-sweep (`_pull_hoy_persona`/`pull_hoy_todos`) fetches its one day straight through
    `_pull_one_person` and never calls it at all; `_backfill_one_person` never calls it either
    (its own marker-based `_dias_pendientes_backfill` is a separate mechanism). Only
    `pull_hosted`'s own implicit-today branch passes `incluir_antiguos=True`."""

    def test_today_sweep_never_calls_dias_pendientes_relleno(self):
        with mock.patch.object(pull, "_dias_pendientes_relleno", wraps=pull._dias_pendientes_relleno) as m, \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato()):
            pull.pull_hoy_todos(self.conn)
        m.assert_not_called()

    def test_backfill_never_calls_dias_pendientes_relleno(self):
        with mock.patch.object(pull, "_dias_pendientes_relleno", wraps=pull._dias_pendientes_relleno) as m, \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato()):
            pull.pull_backfill_hosted(self.conn, hoy=HOY)
        m.assert_not_called()

    def test_connect_pull_calls_it_without_older_retries(self):
        with mock.patch.object(pull, "_dias_pendientes_relleno", wraps=pull._dias_pendientes_relleno) as m, \
             mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato()):
            pull.pull_persona_ahora(1)
        m.assert_called_once()
        self.assertFalse(m.call_args.kwargs.get("incluir_antiguos", False))

    def test_daily_pull_is_the_only_caller_with_older_retries(self):
        with mock.patch.object(pull, "_dias_pendientes_relleno", wraps=pull._dias_pendientes_relleno) as m, \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato()):
            pull.pull_hosted(self.conn)
        m.assert_called_once()
        self.assertTrue(m.call_args.kwargs.get("incluir_antiguos", False))


# ---- ruling 4: the daily job line names the older-retried count ----------------------------------

class LineaOlderRetriedTest(_HostedPullCase):
    def test_the_daily_line_names_the_older_retried_count(self):
        for n in range(garmin_cuenta.VENTANA_RELLENO):
            _marcar(self.conn, (HOY - timedelta(n)).isoformat())
        hace_20 = (HOY - timedelta(20)).isoformat()
        buf = io.StringIO()
        with mock.patch.object(pull, "BACKFILL_DIAS", 25):
            for n in range(garmin_cuenta.VENTANA_RELLENO, 25):
                dia = (HOY - timedelta(n)).isoformat()
                if dia != hace_20:
                    _marcar(self.conn, dia)
            self.conn.commit()
            with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato()), \
                 contextlib.redirect_stdout(buf):
                pull.pull_hosted(self.conn)
        self.assertIn(", 1 older retried", buf.getvalue())

    def test_zero_older_retried_is_printed_too(self):
        buf = io.StringIO()
        with mock.patch.object(pull, "BACKFILL_DIAS", garmin_cuenta.VENTANA_RELLENO), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_stub_siempre_con_dato()), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn)
        self.assertIn(", 0 older retried", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
