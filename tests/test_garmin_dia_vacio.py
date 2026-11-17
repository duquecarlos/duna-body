"""BODY-GARMIN-DIA-VACIO-1: a Garmin day Body pulled but got no value for is a gap, not stored
data (rulings 1-3), and a day whose fetch failed outright -- every feed it attempted raised -- is
a failed attempt, never an honest empty day (rulings 5-6). Invented demo data only, no network:
every Garmin call here goes through a fake body.pull.garmin_adapter.fetch_day, and the watch's
own last-sync read is stubbed to a no-op so nothing here ever reaches garminconnect.
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
from body.agente import herramientas

TOKEN_KEY_HEX = "99" * 32
HOY = date(2026, 11, 17)
_INSTANTE = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)  # 10:00 in America/Bogota


def _conn(case: unittest.TestCase, name: str):
    """A fresh, isolated database file under TMP -- never a path relative to the repo root, and
    never shared with any other test (follows test_garmin_host.py's own `_fresh_db` pattern)."""
    orig_db_path = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    db.init(conn)
    return conn


def _dia(day: str, metrics: dict | None = None, errores: dict | None = None):
    """One fetch_day answer: `metrics` as the real parse_day would hand it -- only keys with a
    real value, never a None one (parse_day's own filter means that can never happen) -- so an
    empty Garmin day is `metrics={}`, exactly as production produces it. `errores` as
    garmin_adapter.fetch_day's own raw['_errors'] ({feed name: exception class name})."""
    raw = {"_errors": errores} if errores else {}
    return garmin_adapter.DayData(day=day, raw=raw, metrics=metrics or {})


def _fetch_from(respuestas: dict[str, "garmin_adapter.DayData"]):
    """Replaces body.pull.garmin_adapter.fetch_day: answers each day from `respuestas`, raising
    KeyError (a test bug, not a Garmin error) for any day not named."""
    def fetch(day, token_str=None):
        return respuestas[day], token_str
    return fetch


SEIS_AUTH = {k: "GarminConnectAuthenticationError" for k in
             ("user_summary", "sleep", "hrv", "body_battery", "activities", "max_metrics")}
SEIS_CONEXION = {k: "GarminConnectConnectionError" for k in
                 ("user_summary", "sleep", "hrv", "body_battery", "activities", "max_metrics")}


class _HostedPullCase(unittest.TestCase):
    """Shared setup for every test that drives a real pull_hosted/pull_backfill_hosted/
    pull_persona_ahora call: one connected person, no real sleep delay, no real watch-last-sync
    read, a fixed clock so 'today' is HOY in the person's own zone."""

    DB_NAME = "hosted.db"

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


# ---- garmin_cuenta.dias_garmin_con_valor / dias_sin_datos: ruling 1 ------------------------------

class DiasGarminConValorTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "valor.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()

    def test_an_all_null_day_is_not_counted(self):
        db.save_day(self.conn, _dia("2026-11-10", metrics={}), 1)
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 1), set())

    def test_a_partly_null_day_counts_as_stored(self):
        db.save_day(self.conn, _dia("2026-11-10", metrics={"steps": 1000}), 1)
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 1), {"2026-11-10"})

    def test_a_manual_row_never_counts(self):
        self.conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES "
            "(1, '2026-11-10', 'resting_hr', 55, 'manual')")
        self.conn.commit()
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 1), set())

    def test_another_persons_row_never_counts(self):
        db.save_day(self.conn, _dia("2026-11-10", metrics={"resting_hr": 55}), 2)
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 1), set())
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 2), {"2026-11-10"})

    def test_desde_narrows_the_window(self):
        db.save_day(self.conn, _dia("2026-11-01", metrics={"resting_hr": 50}), 1)
        db.save_day(self.conn, _dia("2026-11-10", metrics={"resting_hr": 55}), 1)
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 1, desde="2026-11-05"), {"2026-11-10"})
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 1), {"2026-11-01", "2026-11-10"})

    def test_one_query(self):
        statements = []
        self.conn.set_trace_callback(lambda sql: statements.append(sql))
        try:
            garmin_cuenta.dias_garmin_con_valor(self.conn, 1)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1, statements)


class DiasSinDatosTreatsEmptyAsGapTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "sin_datos.db")

    def test_an_all_null_day_stays_a_gap(self):
        db.save_day(self.conn, _dia(HOY.isoformat(), metrics={}), 1)
        gaps = garmin_cuenta.dias_sin_datos(self.conn, 1, HOY)
        self.assertIn(HOY.isoformat(), gaps)

    def test_a_real_value_leaves_the_gap_list(self):
        db.save_day(self.conn, _dia(HOY.isoformat(), metrics={"resting_hr": 55}), 1)
        gaps = garmin_cuenta.dias_sin_datos(self.conn, 1, HOY)
        self.assertNotIn(HOY.isoformat(), gaps)

    def test_still_one_query(self):
        statements = []
        self.conn.set_trace_callback(lambda sql: statements.append(sql))
        try:
            garmin_cuenta.dias_sin_datos(self.conn, 1, HOY)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 1, statements)


# ---- herramientas.leer_estado_reloj: ruling 3 -----------------------------------------------------

class UltimoDiaConDatosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "estado.db")
        patcher = mock.patch.dict("os.environ", {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_skips_an_all_null_newer_day(self):
        db.save_day(self.conn, _dia("2026-11-10", metrics={"resting_hr": 55}), 1)
        db.save_day(self.conn, _dia("2026-11-15", metrics={}), 1)  # the gap
        out = herramientas.ejecutar(self.conn, 1, "leer_estado_reloj", {}, "ninguno")
        self.assertEqual(out["ultimo_dia_con_datos"], "2026-11-10")

    def test_none_when_nobody_has_a_value(self):
        db.save_day(self.conn, _dia("2026-11-10", metrics={}), 1)
        out = herramientas.ejecutar(self.conn, 1, "leer_estado_reloj", {}, "ninguno")
        self.assertIsNone(out["ultimo_dia_con_datos"])


# ---- the daily/backfill/connect pulls: rulings 1-2 heal and count empty days --------------------

class PullHealsAnEmptyDayTest(_HostedPullCase):
    DB_NAME = "heals.db"

    def test_an_all_none_day_stays_in_the_gap_list_and_is_refetched_next_time(self):
        dias = [(HOY - timedelta(n)).isoformat() for n in range(garmin_cuenta.VENTANA_RELLENO)]
        respuestas = {d: _dia(d, metrics={}) for d in dias}
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)):
            pull.pull_hosted(self.conn)
        self.assertEqual(garmin_cuenta.dias_sin_datos(self.conn, 1, HOY), sorted(respuestas))

        visitados: list[str] = []

        def segunda_vez(day, token_str=None):
            visitados.append(day)
            return _dia(day, metrics={"resting_hr": 55.0}), token_str

        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=segunda_vez):
            pull.pull_hosted(self.conn)
        self.assertEqual(sorted(visitados), sorted(respuestas))  # every gap day refetched
        self.assertEqual(garmin_cuenta.dias_sin_datos(self.conn, 1, HOY), [])

    def test_a_partly_none_day_counts_as_stored(self):
        ayer = (HOY - timedelta(1)).isoformat()
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={"steps": 1000}),
                      ayer: _dia(ayer, metrics={"resting_hr": 55.0})}
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertEqual((ok, failed, saved), (1, 0, 1))

    def test_the_daily_line_names_the_empty_count_and_excludes_it_from_days_stored(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={})}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertIn("1 ok, 0 failed, 0 days stored, 1 empty", buf.getvalue())

    def test_a_fully_successful_day_line_is_unchanged_apart_from_the_empty_count(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={"resting_hr": 55.0})}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertIn("1 ok, 0 failed, 1 days stored, 0 empty", buf.getvalue())
        self.assertNotIn("feeds failed", buf.getvalue())


class BackfillEmptyDayTest(_HostedPullCase):
    DB_NAME = "backfill_vacio.db"

    def test_the_backfill_line_names_the_empty_count_and_the_marker_still_advances(self):
        def fetch(day, token_str=None):
            return _dia(day, metrics={}), token_str

        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=fetch), \
             contextlib.redirect_stdout(buf):
            pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertIn("backfill: 1 ok, 0 failed, 0 days stored, 15 empty", buf.getvalue())
        # the marker advanced through all 15 empty days -- none of them asked again this run
        self.assertEqual(garmin_cuenta.marcador_backfill(self.conn, 1), (HOY - timedelta(15)).isoformat())


class ConnectPullEmptyLineTest(_HostedPullCase):
    DB_NAME = "connect_vacio.db"

    def test_the_connect_line_names_the_empty_count(self):
        def fetch(day, token_str=None):
            return _dia(day, metrics={}), token_str

        buf = io.StringIO()
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=fetch), \
             contextlib.redirect_stdout(buf):
            pull.pull_persona_ahora(1)
        self.assertIn("days stored, 14 empty", buf.getvalue())


# ---- rulings 5-6: a full feed failure is a failure, a partial one is named -----------------------

class FeedsFallidosTest(_HostedPullCase):
    DB_NAME = "feeds_fallidos.db"

    def test_all_six_feeds_failing_is_not_saved_and_counts_as_failed(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), errores=SEIS_AUTH)}
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertEqual((ok, failed, saved), (0, 1, 0))
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "FeedsFallidos")
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 1), set())

    def test_the_failure_line_names_the_classes_and_counts(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), errores=SEIS_AUTH)}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertIn("pull attempt failed: FeedsFallidos (GarminConnectAuthenticationError x6)", buf.getvalue())

    def test_an_auth_class_sets_no_backoff(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), errores=SEIS_AUTH)}
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertIsNone(estado["reintentar_despues_de"])

    def test_a_connection_class_sets_the_transient_backoff(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), errores=SEIS_CONEXION)}
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertIsNotNone(estado["reintentar_despues_de"])
        esperado = (datetime.fromisoformat(estado["ultimo_intento_en"])
                    + timedelta(minutes=garmin_cuenta.BACKOFF_TRANSITORIO_MIN))
        self.assertEqual(estado["reintentar_despues_de"], esperado.isoformat())

    def test_two_of_six_feeds_failing_is_a_partial_failure_and_still_saved(self):
        errores = {"user_summary": "GarminConnectConnectionError", "sleep": "GarminConnectConnectionError"}
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={"steps": 1000}, errores=errores)}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertEqual((ok, failed, saved), (1, 0, 1))
        self.assertIn("2 feeds failed (GarminConnectConnectionError x2)", buf.getvalue())
        self.assertEqual(garmin_cuenta.dias_garmin_con_valor(self.conn, 1), {HOY.isoformat()})


if __name__ == "__main__":
    unittest.main()
