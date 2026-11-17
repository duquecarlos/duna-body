"""BODY-GARMIN-DIAGNOSTICO-1: Garmin answered without an exception while Body stored nothing for
three days, and the journal could not tell "Garmin sent nothing" from "Garmin sent a shape Body
does not read" from "the watch has not reached Garmin's cloud." Three rulings: (1) `pull._forma_
feeds` -- one shape per feed, never a value, never a key name; (2) `pull._sufijo_reloj` -- the
watch's own last-known contact instant, or "desconocido"; (3) the --hoy sweep's own line counts
empty days too. Invented demo data only, no network: every Garmin call here goes through a fake
body.pull.garmin_adapter.fetch_day/fetch_device_last_sync.
"""
import contextlib
import io
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, cuenta, db, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "55" * 32
HOY = date(2026, 11, 17)
_INSTANTE = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)  # 10:00 in America/Bogota
RELOJ_INSTANTE = datetime(2026, 11, 17, 14, 32, tzinfo=timezone.utc)
_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8771",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only", garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}

_FORMA_TODO_AUSENTE = ("user_summary=ausente sleep=ausente hrv=ausente body_battery=ausente "
                       "activities=ausente max_metrics=ausente")


def _conn(case: unittest.TestCase, name: str):
    """A fresh, isolated database file under TMP -- follows test_garmin_dia_vacio.py's own
    `_conn` pattern, never a path relative to the repo root, never shared across tests."""
    orig_db_path = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    db.init(conn)
    return conn


def _dia(day: str, metrics: dict | None = None, raw: dict | None = None, errores: dict | None = None):
    """One fetch_day answer: `metrics` as the real parse_day would hand it (an empty day is
    metrics={}), `raw` as whatever feed shapes a test wants to name (never a real key or value),
    `errores` merged into raw['_errors'] ({feed name: exception class name})."""
    raw = dict(raw or {})
    if errores:
        raw["_errors"] = errores
    return garmin_adapter.DayData(day=day, raw=raw, metrics=metrics or {})


def _fetch_from(respuestas: dict[str, "garmin_adapter.DayData"]):
    """Replaces body.pull.garmin_adapter.fetch_day: answers each day from `respuestas`, raising
    KeyError (a test bug, not a Garmin error) for any day not named."""
    def fetch(day, token_str=None):
        return respuestas[day], token_str
    return fetch


def _fetch_vacio(day, token_str=None):
    """Answers ANY day with an honest, all-ausente empty day -- for the callers (connect,
    backfill, the --hoy sweep) that fetch a whole window of days rather than one explicit date."""
    return _dia(day, metrics={}), token_str


# ---- ruling 1: _forma_feeds / _contar_numeros, pure functions ------------------------------------

class FormaFeedsTest(unittest.TestCase):
    def test_all_six_shapes_in_fixed_order(self):
        raw = {
            "sleep": None,
            "hrv": {"valor": 1},
            "body_battery": [10, 20, 30],
            "activities": "no-deberia-pasar",
            "_errors": {"max_metrics": "GarminConnectConnectionError"},
        }
        self.assertEqual(
            pull._forma_feeds(raw),
            "user_summary=ausente sleep=None hrv=dict:1 claves/1 num "
            "body_battery=list:3/3 num activities=str max_metrics=error:GarminConnectConnectionError",
        )

    def test_empty_raw_is_all_ausente(self):
        self.assertEqual(pull._forma_feeds({}), _FORMA_TODO_AUSENTE)

    def test_dict_with_zero_numeric_leaves(self):
        raw = {"user_summary": {"estado": "ok", "zona": "America/Bogota"}}
        self.assertIn("user_summary=dict:2 claves/0 num", pull._forma_feeds(raw))

    def test_numeric_leaves_counted_recursively(self):
        raw = {"hrv": {"resumen": {"ultimoValor": 42, "etiqueta": "alto"}, "promedio": 7}}
        self.assertIn("hrv=dict:2 claves/2 num", pull._forma_feeds(raw))

    def test_depth_cap_stops_counting_leaves_past_it(self):
        en_el_limite = {"a": {"b": {"c": {"d": 1}}}}  # d is at depth 4 -- still counted
        self.assertEqual(pull._contar_numeros(en_el_limite), 1)
        mas_alla = {"a": {"b": {"c": {"d": {"e": 1}}}}}  # e is at depth 5 -- past the cap
        self.assertEqual(pull._contar_numeros(mas_alla), 0)

    def test_bools_are_never_counted_as_numbers(self):
        self.assertEqual(pull._contar_numeros({"activo": True, "cuenta": 3}), 1)

    def test_list_shape_and_its_numeric_count(self):
        self.assertIn("activities=list:3/3 num", pull._forma_feeds({"activities": [1, 2.5, 3]}))

    def test_error_shape_names_only_the_class(self):
        raw = {"_errors": {"user_summary": "GarminConnectAuthenticationError"}}
        self.assertIn("user_summary=error:GarminConnectAuthenticationError", pull._forma_feeds(raw))

    def test_no_invented_name_or_id_string_ever_appears(self):
        raw = {"user_summary": {"deviceName": "Forerunner-955-DEMO", "userProfileId": 999999999}}
        forma = pull._forma_feeds(raw)
        self.assertNotIn("Forerunner", forma)
        self.assertNotIn("999999999", forma)
        self.assertIn("user_summary=dict:2 claves/1 num", forma)


# ---- rulings 1-2, in a real hosted pull (daily/connect/backfill/today-sweep) ---------------------

class _HostedCase(unittest.TestCase):
    """One connected person, no real sleep delay, the watch's own last-sync read stubbed to
    'unknown' by default (a specific test overrides it to a known instant), a fixed clock."""

    DB_NAME = "diagnostico.db"

    def setUp(self):
        self.conn = _conn(self, self.DB_NAME)
        patcher = mock.patch.dict("os.environ", _HOSTED_ENV)
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


class DiaVacioShapeLineTest(_HostedCase):
    def test_daily_pull_prints_the_shape_line_for_an_empty_day(self):
        raw = {"user_summary": {"cualquiera": "x"}}
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={}, raw=raw)}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertIn(
            f"dia vacio {HOY.isoformat()}: user_summary=dict:1 claves/0 num sleep=ausente "
            "hrv=ausente body_battery=ausente activities=ausente max_metrics=ausente",
            buf.getvalue(),
        )

    def test_a_day_with_data_prints_no_shape_line(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={"resting_hr": 55.0})}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertNotIn("dia vacio", buf.getvalue())

    def test_a_raised_feed_prints_the_error_shape(self):
        raw = {"_errors": {"hrv": "GarminConnectConnectionError"}}
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={}, raw=raw)}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertIn("hrv=error:GarminConnectConnectionError", buf.getvalue())

    def test_connect_pull_prints_the_shape_line_too(self):
        buf = io.StringIO()
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_vacio), \
             contextlib.redirect_stdout(buf):
            pull.pull_persona_ahora(1)
        self.assertIn("dia vacio ", buf.getvalue())
        self.assertIn(_FORMA_TODO_AUSENTE, buf.getvalue())

    def test_backfill_prints_the_shape_line_too(self):
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_vacio), \
             contextlib.redirect_stdout(buf):
            pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertIn("dia vacio ", buf.getvalue())
        self.assertIn(_FORMA_TODO_AUSENTE, buf.getvalue())

    def test_today_sweep_prints_the_shape_line_too(self):
        buf = io.StringIO()
        with mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_vacio), \
             contextlib.redirect_stdout(buf):
            ok, espera, fuera, fallidas, vacios, ultima = pull.pull_hoy_todos(self.conn)
        self.assertEqual((ok, espera, fuera, fallidas, vacios, ultima), (1, 0, 0, 0, 1, 1))
        self.assertIn("dia vacio ", buf.getvalue())
        self.assertIn(_FORMA_TODO_AUSENTE, buf.getvalue())

    def test_partial_failure_with_an_otherwise_empty_day_prints_both_lines(self):
        errores = {k: "GarminConnectConnectionError" for k in
                   ("sleep", "hrv", "body_battery", "activities", "max_metrics")}
        raw = {"user_summary": {"zona": "America/Bogota"}, "_errors": errores}
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={}, raw=raw)}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertEqual((ok, failed, saved), (1, 0, 0))  # 5 of 6 failed: partial, still "ok"
        salida = buf.getvalue()
        self.assertIn("5 feeds failed (GarminConnectConnectionError x5)", salida)
        self.assertIn("dia vacio", salida)
        self.assertIn("user_summary=dict:1 claves/0 num", salida)

    def test_no_invented_name_or_id_leaks_through_a_real_pull(self):
        raw = {
            "user_summary": {"deviceName": "DEMO-WATCH-001", "userProfileId": 123123123},
            "_errors": {"hrv": "GarminConnectConnectionError"},
        }
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={}, raw=raw)}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        salida = buf.getvalue()
        self.assertNotIn("DEMO-WATCH-001", salida)
        self.assertNotIn("123123123", salida)
        self.assertIn("user_summary=dict:2 claves/1 num", salida)
        self.assertIn("hrv=error:GarminConnectConnectionError", salida)


# ---- ruling 2: the reloj suffix, known and unknown ------------------------------------------------

class ReloSuffixTest(_HostedCase):
    def test_daily_line_names_desconocido_by_default(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={"resting_hr": 55.0})}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertIn(", reloj desconocido", buf.getvalue())

    def test_daily_line_names_a_known_reloj_timestamp(self):
        respuestas = {HOY.isoformat(): _dia(HOY.isoformat(), metrics={"resting_hr": 55.0})}
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_from(respuestas)), \
             mock.patch("body.pull.garmin_adapter.fetch_device_last_sync",
                         side_effect=lambda token_str=None: (RELOJ_INSTANTE, token_str)), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha=HOY.isoformat())
        self.assertIn(", reloj 2026-11-17 14:32 UTC", buf.getvalue())

    def test_connect_line_names_a_known_reloj_timestamp(self):
        buf = io.StringIO()
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_vacio), \
             mock.patch("body.pull.garmin_adapter.fetch_device_last_sync",
                         side_effect=lambda token_str=None: (RELOJ_INSTANTE, token_str)), \
             contextlib.redirect_stdout(buf):
            pull.pull_persona_ahora(1)
        self.assertIn(", reloj 2026-11-17 14:32 UTC", buf.getvalue())

    def test_backfill_line_names_desconocido_by_default(self):
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_vacio), \
             contextlib.redirect_stdout(buf):
            pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertIn(", reloj desconocido", buf.getvalue())

    def test_backfill_line_names_a_known_reloj_timestamp(self):
        buf = io.StringIO()
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_vacio), \
             mock.patch("body.pull.garmin_adapter.fetch_device_last_sync",
                         side_effect=lambda token_str=None: (RELOJ_INSTANTE, token_str)), \
             contextlib.redirect_stdout(buf):
            pull.pull_backfill_hosted(self.conn, hoy=HOY)
        self.assertIn(", reloj 2026-11-17 14:32 UTC", buf.getvalue())

    def test_sufijo_reloj_is_desconocido_when_no_person_was_pulled(self):
        self.assertEqual(pull._sufijo_reloj(self.conn, None), ", reloj desconocido")


# ---- ruling 3: the --hoy sweep's own line counts empty days too -----------------------------------

class SweepCuentaVaciosTest(_HostedCase):
    def test_sweep_line_includes_the_empty_count_and_the_reloj_suffix(self):
        buf = io.StringIO()
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_vacio), \
             contextlib.redirect_stdout(buf):
            codigo = pull.main(["--hoy"])
        self.assertEqual(codigo, 0)
        self.assertIn(
            "hoy: 1 ok, 0 en espera, 0 fuera de horario, 0 fallidas, 1 vacios, reloj desconocido",
            buf.getvalue(),
        )

    def test_sweep_line_names_a_known_reloj_timestamp(self):
        buf = io.StringIO()
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_vacio), \
             mock.patch("body.pull.garmin_adapter.fetch_device_last_sync",
                         side_effect=lambda token_str=None: (RELOJ_INSTANTE, token_str)), \
             contextlib.redirect_stdout(buf):
            pull.main(["--hoy"])
        self.assertIn(
            "hoy: 1 ok, 0 en espera, 0 fuera de horario, 0 fallidas, 1 vacios, "
            "reloj 2026-11-17 14:32 UTC",
            buf.getvalue(),
        )

    def test_a_day_with_data_is_not_counted_as_vacio_in_the_sweep_line(self):
        def fetch_con_datos(day, token_str=None):
            return _dia(day, metrics={"resting_hr": 55.0}), token_str

        buf = io.StringIO()
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=fetch_con_datos), \
             contextlib.redirect_stdout(buf):
            pull.main(["--hoy"])
        self.assertIn("hoy: 1 ok, 0 en espera, 0 fuera de horario, 0 fallidas, 0 vacios",
                      buf.getvalue())

    def test_a_garmin_failure_in_the_sweep_carries_its_own_vacios_from_the_exception(self):
        """`_pull_one_person` attaches `.vacios` to whatever it raises (the same contract every
        other caller here reads) -- this proves `_pull_hoy_persona` reads it too, instead of
        assuming 0 on any failure."""
        with mock.patch("body.pull._en_horas_quietas", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=RuntimeError("demo failure")):
            ok, espera, fuera, fallidas, vacios, ultima = pull.pull_hoy_todos(self.conn)
        self.assertEqual((ok, fallidas, vacios), (0, 1, 0))
        self.assertEqual(ultima, 1)


if __name__ == "__main__":
    unittest.main()
