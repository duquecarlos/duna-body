"""BODY-SYNC-VIVO-ESTADO-1: Carlos, relayed verbatim by the Personal desk 2026-11-15: "I would
like to see more like a live thing in the app or when I talk to you." BODY-SYNC-VIVO-1-INV found
that Body only ever recorded a successful write, never an attempt time or a backoff, and that
errores.texto_seguro's class-name-only text could not tell a 429 from a 500. This slice's three
rulings:

  1. Three nullable columns on garmin_cuenta (ultimo_intento_en, reintentar_despues_de,
     reloj_sincronizo_en) and garmin_cuenta.registrar_intento/puede_intentar/estado_sync.
  2. Backoff by what Garmin said (garmin_adapter.estado_http, garmin_cuenta's backoff table,
     errores.texto_seguro_con_estado) -- 429 waits 2h, a 5xx or a confirmed connection/timeout
     failure waits 15min, an auth failure sets no backoff (today's handling: reconnect).
  3. Every pull records one attempt; the daily pull and --backfill skip a person still inside a
     backoff, printing a counts-only 'omitido' line, and resume automatically once it expires.
  4. The watch's own last sync (garmin_adapter.fetch_device_last_sync), read once per person per
     pull, after the day fetches, never failing the pull.

No network: garminconnect.Garmin is replaced throughout by fakes built from the installed
garminconnect 0.3.20 package's own exception classes (.venv/Lib/site-packages/garminconnect/
exceptions.py, api/_common.py) -- never a real call. Invented demo data only, no real health
figures; every print checked here is counts/classes only.
"""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from garminconnect.exceptions import (
    GarminConnectAuthenticationError, GarminConnectConnectionError, GarminConnectTooManyRequestsError,
)

from body import db, errores, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "99" * 32


def _conn(case: unittest.TestCase, name: str):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


# ------------------------------------------------------------------- ruling 1: columns + functions

class SchemaTest(unittest.TestCase):
    def test_fresh_db_has_the_three_columns(self):
        conn = _conn(self, "fresh.db")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(garmin_cuenta)")}
        self.assertTrue({"ultimo_intento_en", "reintentar_despues_de", "reloj_sincronizo_en"} <= cols)

    @staticmethod
    def _old_shaped_garmin_cuenta(conn):
        """Every other table at its current shape (db.SCHEMA, executed directly); garmin_cuenta
        alone dropped and recreated at its pre-slice shape (backfill_hasta, no ruling-1
        columns) -- the realistic migration case, one additive table behind the rest."""
        conn.executescript(db.SCHEMA)
        conn.execute("DROP TABLE garmin_cuenta")
        conn.execute(
            "CREATE TABLE garmin_cuenta (person_id INTEGER PRIMARY KEY REFERENCES person(id), "
            "token_cifrado BLOB NOT NULL, nonce BLOB NOT NULL, conectado_en TEXT NOT NULL, "
            "ultimo_error TEXT, backfill_hasta TEXT)"
        )
        conn.execute("INSERT OR IGNORE INTO person (id, display_name) VALUES (1, 'Me')")

    def test_migration_adds_the_three_columns_to_an_old_table(self):
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "old.db")
        self._old_shaped_garmin_cuenta(conn)
        conn.commit()
        db.init(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(garmin_cuenta)")}
        self.assertTrue({"ultimo_intento_en", "reintentar_despues_de", "reloj_sincronizo_en"} <= cols)
        db.init(conn)  # idempotent
        cols2 = {r[1] for r in conn.execute("PRAGMA table_info(garmin_cuenta)")}
        self.assertEqual(cols, cols2)

    def test_existing_connected_person_reads_as_never_attempted(self):
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "old2.db")
        self._old_shaped_garmin_cuenta(conn)
        conn.execute(
            "INSERT INTO garmin_cuenta (person_id, token_cifrado, nonce, conectado_en) "
            "VALUES (1, X'00', X'00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        db.init(conn)
        row = conn.execute(
            "SELECT ultimo_intento_en, reintentar_despues_de, reloj_sincronizo_en "
            "FROM garmin_cuenta WHERE person_id = 1"
        ).fetchone()
        self.assertIsNone(row["ultimo_intento_en"])
        self.assertIsNone(row["reintentar_despues_de"])
        self.assertIsNone(row["reloj_sincronizo_en"])


class RegistrarIntentoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "intento.db")
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        self.conn.commit()

    def test_success_sets_ultimo_intento_en_and_clears_backoff(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora - timedelta(hours=1), ok=False, estado_http=429)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=True)
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertEqual(estado["ultimo_intento_en"], ahora.isoformat())
        self.assertIsNone(estado["reintentar_despues_de"])

    def test_429_backs_off_two_hours(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=429)
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertEqual(estado["reintentar_despues_de"], (ahora + timedelta(hours=2)).isoformat())

    def test_a_5xx_backs_off_fifteen_minutes(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=503)
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertEqual(estado["reintentar_despues_de"], (ahora + timedelta(minutes=15)).isoformat())

    def test_a_confirmed_connection_timeout_failure_also_backs_off_fifteen_minutes(self):
        """estado_http's own 0 sentinel: a transient failure, confirmed, but no numeric status."""
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=0)
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertEqual(estado["reintentar_despues_de"], (ahora + timedelta(minutes=15)).isoformat())

    def test_an_auth_failure_sets_no_backoff(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=None)
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertIsNone(estado["reintentar_despues_de"])
        self.assertEqual(estado["ultimo_intento_en"], ahora.isoformat())

    def test_a_non_429_4xx_sets_no_backoff_either(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=404)
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertIsNone(estado["reintentar_despues_de"])

    def test_a_new_failure_overwrites_a_stale_backoff(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=429)
        mas_tarde = ahora + timedelta(minutes=5)
        garmin_cuenta.registrar_intento(self.conn, 1, mas_tarde, ok=False, estado_http=None)
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertIsNone(estado["reintentar_despues_de"])


class PuedeIntentarTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "puede.db")
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        self.conn.commit()

    def test_true_when_never_attempted(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(garmin_cuenta.puede_intentar(self.conn, 1, ahora, 0))

    def test_false_while_the_backoff_has_not_expired(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=429)
        self.assertFalse(garmin_cuenta.puede_intentar(self.conn, 1, ahora + timedelta(hours=1), 0))

    def test_true_once_the_backoff_has_passed(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=429)
        self.assertTrue(garmin_cuenta.puede_intentar(self.conn, 1, ahora + timedelta(hours=2, seconds=1), 0))

    def test_false_within_espacio_min_of_the_last_attempt(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=True)
        self.assertFalse(garmin_cuenta.puede_intentar(self.conn, 1, ahora + timedelta(minutes=1), espacio_min=5))

    def test_espacio_min_zero_never_blocks_on_recency_alone(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=True)
        self.assertTrue(garmin_cuenta.puede_intentar(self.conn, 1, ahora + timedelta(seconds=1), espacio_min=0))

    def test_true_for_a_person_with_no_garmin_cuenta_row(self):
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(garmin_cuenta.puede_intentar(self.conn, 999, ahora, 0))


class EstadoSyncTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "estado.db")
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_shape_with_no_row(self):
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertEqual(set(estado), {
            "ultimo_intento_en", "ultimo_ok_en", "reintentar_despues_de", "reloj_sincronizo_en", "ultimo_error",
        })
        self.assertTrue(all(v is None for v in estado.values()))

    def test_ultimo_ok_en_reads_db_last_sync(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        self.conn.commit()
        db.save_day(self.conn, garmin_adapter.DayData(
            day="2026-11-10", metrics={"resting_hr": 55.0}, raw={"user_summary": {}}), 1)
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertEqual(estado["ultimo_ok_en"], db.last_sync(self.conn, 1))
        self.assertIsNotNone(estado["ultimo_ok_en"])

    def test_reads_every_written_field(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=429)
        garmin_cuenta.registrar_error(self.conn, 1, "GarminConnectTooManyRequestsError")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, ahora - timedelta(minutes=10))
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertEqual(estado["ultimo_intento_en"], ahora.isoformat())
        self.assertEqual(estado["reintentar_despues_de"], (ahora + timedelta(hours=2)).isoformat())
        self.assertEqual(estado["reloj_sincronizo_en"], (ahora - timedelta(minutes=10)).isoformat())
        self.assertEqual(estado["ultimo_error"], "GarminConnectTooManyRequestsError")


class ReconnectClearsBackoffTest(unittest.TestCase):
    def test_guardar_clears_a_stale_backoff(self):
        conn = _conn(self, "reconnect.db")
        with mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}):
            garmin_cuenta.guardar(conn, 1, "tok-old")
            ahora = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
            garmin_cuenta.registrar_intento(conn, 1, ahora, ok=False, estado_http=429)
            self.assertIsNotNone(garmin_cuenta.estado_sync(conn, 1)["reintentar_despues_de"])
            garmin_cuenta.guardar(conn, 1, "tok-new")
            self.assertIsNone(garmin_cuenta.estado_sync(conn, 1)["reintentar_despues_de"])


# ------------------------------------------------------------------- ruling 2: estado_http / texto_seguro_con_estado

class EstadoHttpTest(unittest.TestCase):
    def test_too_many_requests_is_429_even_with_no_response_attribute(self):
        exc = GarminConnectTooManyRequestsError("Rate limit exceeded")
        self.assertFalse(hasattr(exc, "response") and exc.response is not None)
        self.assertEqual(garmin_adapter.estado_http(exc), 429)

    def test_connection_error_with_a_response_reads_its_status_code(self):
        exc = GarminConnectConnectionError("client error (503)")
        exc.response = mock.Mock(status_code=503)
        self.assertEqual(garmin_adapter.estado_http(exc), 503)

    def test_connection_error_with_a_4xx_response_reads_it_too(self):
        exc = GarminConnectConnectionError("client error (404)")
        exc.response = mock.Mock(status_code=404)
        self.assertEqual(garmin_adapter.estado_http(exc), 404)

    def test_connection_error_with_no_response_is_the_zero_sentinel(self):
        exc = GarminConnectConnectionError("Connection error: timed out")
        self.assertIsNone(exc.response)
        self.assertEqual(garmin_adapter.estado_http(exc), 0)

    def test_authentication_error_is_none(self):
        self.assertIsNone(garmin_adapter.estado_http(GarminConnectAuthenticationError("Authentication failed")))

    def test_not_signed_in_is_none(self):
        self.assertIsNone(garmin_adapter.estado_http(garmin_adapter.NotSignedIn("token rejected")))

    def test_an_unrelated_exception_is_none(self):
        self.assertIsNone(garmin_adapter.estado_http(RuntimeError("boom")))

    def test_a_response_with_no_status_code_attribute_is_defensive_not_a_crash(self):
        exc = GarminConnectConnectionError("weird")
        exc.response = object()
        self.assertEqual(garmin_adapter.estado_http(exc), 0)


class TextoSeguroConEstadoTest(unittest.TestCase):
    def test_appends_the_status_when_known(self):
        exc = GarminConnectTooManyRequestsError("Rate limit exceeded")
        self.assertEqual(errores.texto_seguro_con_estado(exc, 429), "GarminConnectTooManyRequestsError 429")

    def test_plain_class_name_when_estado_is_none(self):
        exc = RuntimeError("boom")
        self.assertEqual(errores.texto_seguro_con_estado(exc, None), "RuntimeError")

    def test_never_the_message(self):
        exc = GarminConnectConnectionError("a secret-looking detail nobody should see")
        texto = errores.texto_seguro_con_estado(exc, 503)
        self.assertNotIn("secret-looking", texto)
        self.assertEqual(texto, "GarminConnectConnectionError 503")


class ClientPropagationTest(unittest.TestCase):
    """body/garmin_adapter._client: a transient Garmin-side failure propagates with its own type
    (so estado_http can read it); a genuinely rejected/unreadable token still becomes NotSignedIn,
    unchanged from before this slice."""

    def test_too_many_requests_propagates_as_is(self):
        with mock.patch("garminconnect.Garmin") as m:
            m.return_value.login.side_effect = GarminConnectTooManyRequestsError("Rate limit exceeded")
            with self.assertRaises(GarminConnectTooManyRequestsError):
                garmin_adapter._client("tok-x")

    def test_connection_error_propagates_as_is(self):
        with mock.patch("garminconnect.Garmin") as m:
            exc = GarminConnectConnectionError("client error (503)")
            exc.response = mock.Mock(status_code=503)
            m.return_value.login.side_effect = exc
            with self.assertRaises(GarminConnectConnectionError) as ctx:
                garmin_adapter._client("tok-x")
            self.assertIs(ctx.exception, exc)

    def test_a_plain_rejected_token_still_becomes_not_signed_in(self):
        with mock.patch("garminconnect.Garmin") as m:
            m.return_value.login.side_effect = RuntimeError("rejected demo token")
            with self.assertRaises(garmin_adapter.NotSignedIn):
                garmin_adapter._client("tok-x")

    def test_an_authentication_error_still_becomes_not_signed_in(self):
        with mock.patch("garminconnect.Garmin") as m:
            m.return_value.login.side_effect = GarminConnectAuthenticationError("Authentication failed")
            with self.assertRaises(garmin_adapter.NotSignedIn):
                garmin_adapter._client("tok-x")


# ------------------------------------------------------------------- ruling 4: fetch_device_last_sync

class ParseDeviceLastSyncTest(unittest.TestCase):
    def test_epoch_ms_key(self):
        raw = {"lastUsedDeviceUploadTime": 1731672000000}  # 2024-11-15T12:00:00Z
        self.assertEqual(garmin_adapter._parse_device_last_sync(raw),
                          datetime.fromtimestamp(1731672000000 / 1000, tz=timezone.utc))

    def test_epoch_seconds_fallback_key(self):
        raw = {"lastUsedDeviceUploadTimeInSeconds": 1731672000}
        self.assertEqual(garmin_adapter._parse_device_last_sync(raw),
                          datetime.fromtimestamp(1731672000, tz=timezone.utc))

    def test_iso_string_fallback_key_naive_is_assumed_utc(self):
        raw = {"deviceLastUsedDate": "2026-11-15T08:30:00"}
        got = garmin_adapter._parse_device_last_sync(raw)
        self.assertEqual(got, datetime(2026, 11, 15, 8, 30, tzinfo=timezone.utc))

    def test_iso_string_with_its_own_offset_is_kept(self):
        raw = {"deviceLastUsedDate": "2026-11-15T08:30:00-05:00"}
        got = garmin_adapter._parse_device_last_sync(raw)
        self.assertEqual(got, datetime(2026, 11, 15, 13, 30, tzinfo=timezone.utc))

    def test_not_a_dict_is_none(self):
        self.assertIsNone(garmin_adapter._parse_device_last_sync(None))
        self.assertIsNone(garmin_adapter._parse_device_last_sync([1, 2, 3]))

    def test_no_recognised_key_is_none(self):
        self.assertIsNone(garmin_adapter._parse_device_last_sync({"userDeviceId": 123}))

    def test_unparseable_iso_string_is_none(self):
        self.assertIsNone(garmin_adapter._parse_device_last_sync({"deviceLastUsedDate": "not-a-date"}))

    def test_non_numeric_upload_time_falls_through_to_other_keys(self):
        raw = {"lastUsedDeviceUploadTime": "not-a-number", "lastUsedDeviceUploadTimeInSeconds": 1731672000}
        self.assertEqual(garmin_adapter._parse_device_last_sync(raw),
                          datetime.fromtimestamp(1731672000, tz=timezone.utc))


class FetchDeviceLastSyncTest(unittest.TestCase):
    def test_successful_read_returns_the_parsed_instant_and_the_token(self):
        with mock.patch("garminconnect.Garmin") as m:
            api = m.return_value
            api.login.return_value = (None, None)
            api.get_device_last_used.return_value = {"lastUsedDeviceUploadTime": 1731672000000}
            api.client.dumps.return_value = "tok-after"
            reloj, token_after = garmin_adapter.fetch_device_last_sync(token_str="tok-before")
        self.assertEqual(reloj, datetime.fromtimestamp(1731672000000 / 1000, tz=timezone.utc))
        self.assertEqual(token_after, "tok-after")

    def test_a_raise_from_the_client_never_raises_here(self):
        with mock.patch("garminconnect.Garmin") as m:
            m.return_value.login.side_effect = GarminConnectTooManyRequestsError("Rate limit exceeded")
            reloj, token_after = garmin_adapter.fetch_device_last_sync(token_str="tok-before")
        self.assertIsNone(reloj)
        self.assertEqual(token_after, "tok-before")

    def test_a_raise_from_get_device_last_used_never_raises_here(self):
        with mock.patch("garminconnect.Garmin") as m:
            api = m.return_value
            api.login.return_value = (None, None)
            api.get_device_last_used.side_effect = GarminConnectConnectionError("down")
            reloj, token_after = garmin_adapter.fetch_device_last_sync(token_str="tok-before")
        self.assertIsNone(reloj)
        self.assertEqual(token_after, "tok-before")

    def test_an_unparseable_response_is_none_not_a_crash(self):
        with mock.patch("garminconnect.Garmin") as m:
            api = m.return_value
            api.login.return_value = (None, None)
            api.get_device_last_used.return_value = {"nothing": "useful"}
            api.client.dumps.return_value = "tok-before"
            reloj, token_after = garmin_adapter.fetch_device_last_sync(token_str="tok-before")
        self.assertIsNone(reloj)

    def test_local_mode_returns_the_bare_value(self):
        with mock.patch("garminconnect.Garmin") as m, \
             mock.patch.object(garmin_adapter, "has_token", return_value=True):
            api = m.return_value
            api.login.return_value = (None, None)
            api.get_device_last_used.return_value = {"lastUsedDeviceUploadTime": 1731672000000}
            reloj = garmin_adapter.fetch_device_last_sync()
        self.assertEqual(reloj, datetime.fromtimestamp(1731672000000 / 1000, tz=timezone.utc))


# ------------------------------------------------------------------- rulings 2-4 together, through pull.py

def _fake_garmin(token_exc: dict | None = None, last_used: dict | None = None):
    """Replaces garminconnect.Garmin for a hosted pull: login(tokenstore) raises token_exc[token]
    when the token names a configured failure, else succeeds; get_device_last_used() returns
    last_used[token] when the login succeeded. get_user_summary returns one real value (so a
    successful login never also trips BODY-GARMIN-DIA-VACIO-1's all-six-feeds-failed rule); every
    other feed call is left undefined -- fetch_day's own per-call try/except (AttributeError)
    makes those an honest partial failure, exactly as an unconfirmed feed would read for a real
    account."""
    token_exc = token_exc or {}
    last_used = last_used or {}

    class _FakeClient:
        def __init__(self):
            self.token = None

        def loads(self, token_str):
            self.token = token_str

        def dumps(self):
            return self.token

    class _Fake:
        def __init__(self, email=None, password=None, return_on_mfa=False, **kw):
            self.client = _FakeClient()

        def login(self, tokenstore=None):
            if tokenstore in token_exc:
                raise token_exc[tokenstore]
            self.client.token = tokenstore
            return None, None

        def get_device_last_used(self):
            return last_used.get(self.client.token)

        def get_user_summary(self, day):
            return {"restingHeartRate": 55}

    return _Fake


class HostedPullBackoffTest(unittest.TestCase):
    """Rulings 2-3 through pull.pull_hosted: a fake client's login failure (the only exception
    path that reaches _pull_persona, since fetch_day's own per-feed calls never raise) sets the
    matching backoff; a person inside it is skipped and counted, never retried early."""

    def setUp(self):
        self.conn = _conn(self, "backoff.db")
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)

    def test_a_429_sets_a_two_hour_backoff_and_records_the_class_and_status(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-429")
        self.conn.commit()
        exc = GarminConnectTooManyRequestsError("Rate limit exceeded")
        buf = io.StringIO()
        with mock.patch("garminconnect.Garmin", _fake_garmin({"tok-429": exc})), \
             contextlib.redirect_stdout(buf):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-10")
        self.assertEqual((ok, failed), (0, 1))
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "GarminConnectTooManyRequestsError")
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertIsNotNone(estado["ultimo_intento_en"])
        esperado = datetime.fromisoformat(estado["ultimo_intento_en"]) + timedelta(hours=2)
        self.assertEqual(estado["reintentar_despues_de"], esperado.isoformat())
        self.assertIn("GarminConnectTooManyRequestsError 429", buf.getvalue())

    def test_a_connection_error_with_a_5xx_sets_a_fifteen_minute_backoff(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-503")
        self.conn.commit()
        exc = GarminConnectConnectionError("client error (503)")
        exc.response = mock.Mock(status_code=503)
        with mock.patch("garminconnect.Garmin", _fake_garmin({"tok-503": exc})):
            pull.pull_hosted(self.conn, fecha="2026-11-10")
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        esperado = datetime.fromisoformat(estado["ultimo_intento_en"]) + timedelta(minutes=15)
        self.assertEqual(estado["reintentar_despues_de"], esperado.isoformat())

    def test_a_bare_connection_timeout_error_also_sets_a_fifteen_minute_backoff(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-timeout")
        self.conn.commit()
        exc = GarminConnectConnectionError("Connection error: timed out")
        with mock.patch("garminconnect.Garmin", _fake_garmin({"tok-timeout": exc})):
            pull.pull_hosted(self.conn, fecha="2026-11-10")
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        esperado = datetime.fromisoformat(estado["ultimo_intento_en"]) + timedelta(minutes=15)
        self.assertEqual(estado["reintentar_despues_de"], esperado.isoformat())

    def test_an_authentication_failure_sets_no_backoff(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-auth")
        self.conn.commit()
        exc = GarminConnectAuthenticationError("Authentication failed")
        with mock.patch("garminconnect.Garmin", _fake_garmin({"tok-auth": exc})):
            pull.pull_hosted(self.conn, fecha="2026-11-10")
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertIsNotNone(estado["ultimo_intento_en"])
        self.assertIsNone(estado["reintentar_despues_de"])

    def test_a_successful_pull_clears_a_previous_backoff_and_records_the_watchs_last_sync(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-ok")
        ahora = datetime.now(timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora - timedelta(hours=3), ok=False, estado_http=429)
        self.conn.commit()
        last_used = {"tok-ok": {"lastUsedDeviceUploadTime": 1731672000000}}
        with mock.patch("garminconnect.Garmin", _fake_garmin(last_used=last_used)):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-10")
        self.assertEqual((ok, failed), (1, 0))
        estado = garmin_cuenta.estado_sync(self.conn, 1)
        self.assertIsNone(estado["reintentar_despues_de"])
        self.assertEqual(estado["reloj_sincronizo_en"],
                          datetime.fromtimestamp(1731672000000 / 1000, tz=timezone.utc).isoformat())

    def test_a_failed_watch_last_sync_read_never_fails_an_otherwise_ok_pull(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-ok")
        self.conn.commit()

        class _RaisingOnLastUsed(_fake_garmin()):
            def get_device_last_used(self):
                raise GarminConnectConnectionError("down")

        with mock.patch("garminconnect.Garmin", _RaisingOnLastUsed):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-10")
        self.assertEqual((ok, failed), (1, 0))
        self.assertIsNone(garmin_cuenta.estado_sync(self.conn, 1)["reloj_sincronizo_en"])

    def test_a_person_still_inside_a_backoff_is_skipped_and_counted(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-ok")
        ahora = datetime.now(timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=429)
        self.conn.commit()
        buf = io.StringIO()
        with mock.patch("garminconnect.Garmin", _fake_garmin()), \
             contextlib.redirect_stdout(buf):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-10")
        self.assertEqual((ok, failed, saved), (0, 0, 0))  # never attempted -- not a failure
        self.assertIn("omitido: 1 personas en espera", buf.getvalue())

    def test_a_person_past_their_backoff_is_tried_again(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-ok")
        pasado = datetime.now(timezone.utc) - timedelta(hours=3)
        garmin_cuenta.registrar_intento(self.conn, 1, pasado, ok=False, estado_http=429)  # expired
        self.conn.commit()
        with mock.patch("garminconnect.Garmin", _fake_garmin()):
            ok, failed, saved = pull.pull_hosted(self.conn, fecha="2026-11-10")
        self.assertEqual((ok, failed), (1, 0))

    def test_omitido_line_absent_when_nobody_is_waiting(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-ok")
        self.conn.commit()
        buf = io.StringIO()
        with mock.patch("garminconnect.Garmin", _fake_garmin()), \
             contextlib.redirect_stdout(buf):
            pull.pull_hosted(self.conn, fecha="2026-11-10")
        self.assertNotIn("omitido", buf.getvalue())


class BackfillBackoffTest(unittest.TestCase):
    """Ruling 3's backfill half: an auth-type error (no backoff recorded) is skipped forever, as
    before this slice (test_primer_pull_1.py keeps covering that regression); a backoff-governed
    error resumes automatically once it expires -- the new behaviour this slice adds."""

    def setUp(self):
        self.conn = _conn(self, "backfill_backoff.db")
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        patcher.start(); self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)

    def test_a_429_backed_off_person_is_skipped_then_resumes_once_it_expires(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-ok")
        ahora = datetime.now(timezone.utc)
        garmin_cuenta.registrar_intento(self.conn, 1, ahora, ok=False, estado_http=429)
        garmin_cuenta.registrar_error(self.conn, 1, "GarminConnectTooManyRequestsError")
        self.conn.commit()
        buf = io.StringIO()
        with mock.patch("garminconnect.Garmin", _fake_garmin()), \
             contextlib.redirect_stdout(buf):
            ok, failed, saved = pull.pull_backfill_hosted(self.conn)
        self.assertEqual((ok, failed), (0, 0))
        self.assertIn("omitido: 1 personas en espera", buf.getvalue())

        # the backoff is in the past now -- the next run resumes without a reconnect
        garmin_cuenta.registrar_intento(self.conn, 1, ahora - timedelta(hours=3), ok=False, estado_http=429)
        self.conn.commit()
        with mock.patch("garminconnect.Garmin", _fake_garmin()):
            ok, failed, saved = pull.pull_backfill_hosted(self.conn)
        self.assertEqual(failed, 0)
        self.assertGreaterEqual(ok, 0)  # no AttributeError/crash -- the call was actually attempted

    def test_an_auth_type_error_with_no_backoff_is_skipped_forever_without_a_reconnect(self):
        garmin_cuenta.guardar(self.conn, 1, "tok-bad")
        garmin_cuenta.registrar_error(self.conn, 1, "NotSignedIn")  # no backoff ever set for this
        self.conn.commit()
        with mock.patch("garminconnect.Garmin", side_effect=AssertionError("Garmin must not be reached")):
            ok, failed, saved = pull.pull_backfill_hosted(self.conn)
        self.assertEqual((ok, failed, saved), (0, 0, 0))
        self.assertEqual(garmin_cuenta.ultimo_error(self.conn, 1), "NotSignedIn")  # unchanged


# ------------------------------------------------------------------- ruling 5: no WAL

class NoWalChangeTest(unittest.TestCase):
    def test_connect_still_sets_busy_timeout_and_no_wal(self):
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "wal.db")
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, 5000)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertNotEqual(mode.lower(), "wal")


if __name__ == "__main__":
    unittest.main()
