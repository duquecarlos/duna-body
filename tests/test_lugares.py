"""BODY-HISTORIA-VIDA-LUGARES-1: body/lugares.py, the only module that talks to Lugares' read-only
trips API. No network, ever -- every test patches `lugares._get_json`. No real sub, no real
token: every value here is an invented string that looks nothing like a real one.
"""
import io
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import config, db, enlaces, lugares, mudanza, privacidad, pull


SUB_A = "zz-sub-a-invented"
TOKEN = "zz-token-invented"
HOY = date(2026, 11, 18)


def _conn(case: unittest.TestCase):
    """A fresh database per test -- never the shared default config.DB_PATH every other test
    module in this run could also be touching."""
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "lugares.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name, cuenta_sub) VALUES (2, 'B', ?)", (SUB_A,))
    conn.commit()
    return conn


def _evento(ref, starts_at, ends_at, kind="trip"):
    return {"id": 1, "person": SUB_A, "hogar_id": None, "kind": kind, "starts_at": starts_at,
            "ends_at": ends_at, "all_day": True, "timezone": "America/Bogota", "source": "lugares",
            "ref": ref, "data": {}}


@contextmanager
def _patch_token(value: str | None = TOKEN):
    """mock.patch.dict snapshots and restores os.environ on exit regardless of what happens
    inside -- so popping the key when `value` is None (never set) is restored correctly too,
    the same pattern tests/test_hogar_enlace_1.py's own UrlDeTest uses for DUNA_LUGARES_ORIGEN."""
    with mock.patch.dict(os.environ, {}, clear=False):
        if value is None:
            os.environ.pop(config.LUGARES_TOKEN_ENV, None)
        else:
            os.environ[config.LUGARES_TOKEN_ENV] = value
        yield


class ConfigTokenTest(unittest.TestCase):
    def test_unset_is_none(self):
        with _patch_token(None):
            self.assertIsNone(config.lugares_token())

    def test_set_reads_through(self):
        with _patch_token("zz-abc"):
            self.assertEqual(config.lugares_token(), "zz-abc")

    def test_empty_string_reads_as_none(self):
        with _patch_token(""):
            self.assertIsNone(config.lugares_token())


class EnlacesOrigenTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-LUGARES-1 ruling 2: `_origin` is gone, `origen` is public, and
    body/lugares.py reads the SAME override every goal-link URL already does."""

    def test_origen_is_public_and_origin_name_is_gone(self):
        self.assertFalse(hasattr(enlaces, "_origin"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DUNA_LUGARES_ORIGEN", None)
            self.assertEqual(enlaces.origen("lugares"), "https://lugares.duna.solutions")

    def test_env_override_reaches_origen(self):
        with mock.patch.dict("os.environ", {"DUNA_LUGARES_ORIGEN": "https://lugares.local:4000"}):
            self.assertEqual(enlaces.origen("lugares"), "https://lugares.local:4000")


class NoSubNoTokenTest(unittest.TestCase):
    """Ruling 1-2: no sub, or no token, means NO REQUEST at all."""

    def test_no_sub_never_calls_the_network(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = NULL WHERE id = 2")
        conn.commit()
        with _patch_token(), mock.patch("body.lugares._get_json") as m:
            resultado = lugares.sincronizar(conn, 2, HOY)
        m.assert_not_called()
        self.assertEqual(resultado, {"estado": "sin_conectar", "viajes": 0})

    def test_no_token_never_calls_the_network(self):
        conn = _conn(self)
        with _patch_token(None), mock.patch("body.lugares._get_json") as m:
            resultado = lugares.sincronizar(conn, 2, HOY)
        m.assert_not_called()
        self.assertEqual(resultado, {"estado": "sin_conectar", "viajes": 0})

    def test_no_sub_writes_no_sync_row(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = NULL WHERE id = 2")
        conn.commit()
        with _patch_token():
            lugares.sincronizar(conn, 2, HOY)
        self.assertIsNone(conn.execute("SELECT 1 FROM lugares_sync WHERE person_id = 2").fetchone())


class TokenNeverLeaksTest(unittest.TestCase):
    """The token never appears in a URL, a log line, the DB or an exception message."""

    def test_token_absent_from_request_url(self):
        conn = _conn(self)
        captured = {}

        def _fake(url, token):
            captured["url"] = url
            captured["token"] = token
            return 200, {"eventos": []}

        with _patch_token(), mock.patch("body.lugares._get_json", side_effect=_fake):
            lugares.sincronizar(conn, 2, HOY)
        self.assertNotIn(TOKEN, captured["url"])
        self.assertEqual(captured["token"], TOKEN)  # passed as its own argument, never interpolated

    def test_token_never_stored_in_any_table(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, {"eventos": []})):
            lugares.sincronizar(conn, 2, HOY)
        dump = []
        for t in ("viaje_lugares", "lugares_sync"):
            for row in conn.execute(f"SELECT * FROM {t}"):
                dump.append(repr(dict(row)))
        self.assertNotIn(TOKEN, "\n".join(dump))

    def test_token_never_printed_by_the_safe_wrapper(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(0, {})):
            buf = io.StringIO()
            with redirect_stdout(buf):
                pull._sincronizar_lugares_seguro(conn, 2)
        self.assertNotIn(TOKEN, buf.getvalue())

    def test_a_raised_exception_never_carries_the_token(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares._get_json", side_effect=RuntimeError("boom")):
            try:
                lugares.sincronizar(conn, 2, HOY)
            except RuntimeError as e:
                self.assertNotIn(TOKEN, str(e))
            else:
                self.fail("expected RuntimeError to propagate out of sincronizar (the safe wrapper catches it)")


class RequestShapeTest(unittest.TestCase):
    def test_request_carries_exactly_person_desde_hasta_and_bearer_header(self):
        conn = _conn(self)
        captured = {}

        def _fake(url, token):
            captured["url"] = url
            captured["token"] = token
            return 200, {"eventos": []}

        with _patch_token(), mock.patch("body.lugares._get_json", side_effect=_fake):
            lugares.sincronizar(conn, 2, HOY)
        url = captured["url"]
        self.assertTrue(url.startswith("https://lugares.duna.solutions/api/vida/eventos?"))
        self.assertIn(f"person={SUB_A}", url)
        desde = (HOY - timedelta(days=lugares.VENTANA_PASADO_DIAS)).isoformat()
        hasta = (HOY + timedelta(days=lugares.VENTANA_FUTURO_DIAS)).isoformat()
        self.assertIn(f"desde={desde}", url)
        self.assertIn(f"hasta={hasta}", url)
        self.assertEqual(captured["token"], TOKEN)


class SyncSuccessTest(unittest.TestCase):
    def test_200_stores_the_trips(self):
        conn = _conn(self)
        eventos = {"eventos": [_evento(101, "2026-10-01", "2026-10-05"), _evento(102, "2026-11-01", "2026-11-01")]}
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, eventos)):
            resultado = lugares.sincronizar(conn, 2, HOY)
        self.assertEqual(resultado, {"estado": "ok", "viajes": 2, "invalidos": 0})
        refs = {r["ref"] for r in conn.execute("SELECT ref FROM viaje_lugares WHERE person_id = 2")}
        self.assertEqual(refs, {"101", "102"})

    def test_a_later_200_replaces_the_previous_set(self):
        conn = _conn(self)
        primero = {"eventos": [_evento(101, "2026-10-01", "2026-10-05")]}
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, primero)):
            lugares.sincronizar(conn, 2, HOY)
        segundo = {"eventos": [_evento(202, "2026-11-10", "2026-11-12")]}
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, segundo)):
            resultado = lugares.sincronizar(conn, 2, HOY)
        self.assertEqual(resultado["viajes"], 1)
        refs = {r["ref"] for r in conn.execute("SELECT ref FROM viaje_lugares WHERE person_id = 2")}
        self.assertEqual(refs, {"202"}, "the trip missing from the later answer must be gone")

    def test_an_invalid_event_is_skipped_and_counted(self):
        conn = _conn(self)
        eventos = {"eventos": [
            _evento(101, "2026-10-01", "2026-10-05"),
            _evento(None, "2026-10-01", "2026-10-05"),       # no ref
            _evento(103, "2026-10-05", "2026-10-01"),         # starts after ends
            {"kind": "trip", "ref": 104},                     # no dates at all
            _evento(105, "2026-10-01", "2026-10-05", kind="stay"),  # wrong kind
        ]}
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, eventos)):
            resultado = lugares.sincronizar(conn, 2, HOY)
        self.assertEqual(resultado, {"estado": "ok", "viajes": 1, "invalidos": 4})

    def test_success_records_ok_at_and_clears_any_earlier_motivo(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(0, {})):
            lugares.sincronizar(conn, 2, HOY)
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, {"eventos": []})):
            lugares.sincronizar(conn, 2, HOY)
        row = conn.execute("SELECT ok_at, motivo FROM lugares_sync WHERE person_id = 2").fetchone()
        self.assertIsNotNone(row["ok_at"])
        self.assertIsNone(row["motivo"])


class SyncFailureKeepsCacheTest(unittest.TestCase):
    def _seed_one_trip(self, conn):
        with _patch_token(), mock.patch("body.lugares._get_json",
                                         return_value=(200, {"eventos": [_evento(101, "2026-10-01", "2026-10-05")]})):
            lugares.sincronizar(conn, 2, HOY)

    def _assert_kept(self, conn, motivo_esperado):
        refs = {r["ref"] for r in conn.execute("SELECT ref FROM viaje_lugares WHERE person_id = 2")}
        self.assertEqual(refs, {"101"})
        row = conn.execute("SELECT motivo FROM lugares_sync WHERE person_id = 2").fetchone()
        self.assertEqual(row["motivo"], motivo_esperado)

    def test_401_keeps_cache_and_records_token_invalido(self):
        conn = _conn(self)
        self._seed_one_trip(conn)
        with _patch_token(), mock.patch("body.lugares._get_json",
                                         return_value=(401, {"motivo": "token_invalido"})):
            resultado = lugares.sincronizar(conn, 2, HOY)
        self.assertEqual(resultado, {"estado": "error", "viajes": 0, "motivo": "token_invalido"})
        self._assert_kept(conn, "token_invalido")

    def test_422_keeps_cache_and_records_rango_invalido(self):
        conn = _conn(self)
        self._seed_one_trip(conn)
        with _patch_token(), mock.patch("body.lugares._get_json",
                                         return_value=(422, {"motivo": "rango_invalido"})):
            lugares.sincronizar(conn, 2, HOY)
        self._assert_kept(conn, "rango_invalido")

    def test_503_keeps_cache_and_records_vida_no_configurado(self):
        conn = _conn(self)
        self._seed_one_trip(conn)
        with _patch_token(), mock.patch("body.lugares._get_json",
                                         return_value=(503, {"motivo": "vida_no_configurado"})):
            lugares.sincronizar(conn, 2, HOY)
        self._assert_kept(conn, "vida_no_configurado")

    def test_network_error_keeps_cache_and_records_red(self):
        conn = _conn(self)
        self._seed_one_trip(conn)
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(0, {})):
            lugares.sincronizar(conn, 2, HOY)
        self._assert_kept(conn, "red")

    def test_malformed_body_keeps_cache_and_records_formato(self):
        conn = _conn(self)
        self._seed_one_trip(conn)
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, {"no_eventos_key": []})):
            lugares.sincronizar(conn, 2, HOY)
        self._assert_kept(conn, "formato")


class DbReadersTest(unittest.TestCase):
    def test_viajes_lugares_returns_intersecting_trips_in_one_statement(self):
        conn = _conn(self)
        eventos = {"eventos": [_evento(101, "2026-10-01", "2026-10-05"), _evento(102, "2026-11-01", "2026-11-01"),
                                _evento(103, "2027-01-01", "2027-01-03")]}
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, eventos)):
            lugares.sincronizar(conn, 2, HOY)
        filas = db.viajes_lugares(conn, 2, "2026-10-03", "2026-11-05")
        refs = [f["ref"] for f in filas]
        self.assertEqual(refs, ["101", "102"])

    def test_lugares_estado_defaults_when_never_attempted(self):
        conn = _conn(self)
        estado = db.lugares_estado(conn, 2)
        self.assertEqual(estado, {"intento_at": None, "ok_at": None, "motivo": None, "n_viajes": 0})

    def test_lugares_estado_counts_cached_trips(self):
        conn = _conn(self)
        eventos = {"eventos": [_evento(101, "2026-10-01", "2026-10-05")]}
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(200, eventos)):
            lugares.sincronizar(conn, 2, HOY)
        estado = db.lugares_estado(conn, 2)
        self.assertEqual(estado["n_viajes"], 1)
        self.assertIsNotNone(estado["ok_at"])
        self.assertIsNone(estado["motivo"])


class MudanzaCensusTest(unittest.TestCase):
    def test_both_tables_are_skipped_never_hand_copied(self):
        self.assertIn("viaje_lugares", mudanza.SKIPPED_TABLES)
        self.assertIn("lugares_sync", mudanza.SKIPPED_TABLES)


class PrivacidadCensusTest(unittest.TestCase):
    def test_viaje_lugares_is_fully_handled_lugares_sync_is_never_exported(self):
        self.assertIn("viaje_lugares", privacidad.FULLY_HANDLED_TABLES)
        self.assertIn("lugares_sync", privacidad.NEVER_EXPORTED_TABLES)

    def test_export_includes_the_trip_delete_all_removes_both_tables(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares._get_json",
                                         return_value=(200, {"eventos": [_evento(101, "2026-10-01", "2026-10-05")]})):
            lugares.sincronizar(conn, 2, HOY)
        data = privacidad.export_person(conn, 2)
        self.assertEqual(len(data["viajes_lugares"]), 1)
        self.assertEqual(data["viajes_lugares"][0]["ref"], "101")
        self.assertNotIn("lugares_sync", data)
        privacidad.delete_person(conn, 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM viaje_lugares").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM lugares_sync").fetchone()[0], 0)


class SafeWrapperTest(unittest.TestCase):
    """body/pull.py's `_sincronizar_lugares_seguro`: once per person per calendar day, never raises,
    prints counts only."""

    def test_sin_conectar_prints_and_never_raises(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = NULL WHERE id = 2")
        conn.commit()
        with _patch_token():
            buf = io.StringIO()
            with redirect_stdout(buf):
                pull._sincronizar_lugares_seguro(conn, 2)
        self.assertIn("lugares: sin conectar", buf.getvalue())

    def test_success_prints_the_trip_count(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares._get_json",
                                         return_value=(200, {"eventos": [_evento(101, "2026-10-01", "2026-10-05")]})):
            buf = io.StringIO()
            with redirect_stdout(buf):
                pull._sincronizar_lugares_seguro(conn, 2)
        self.assertIn("lugares: 1 viajes", buf.getvalue())

    def test_failure_prints_the_motivo_and_never_raises(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares._get_json", return_value=(401, {})):
            buf = io.StringIO()
            with redirect_stdout(buf):
                pull._sincronizar_lugares_seguro(conn, 2)
        self.assertIn("lugares: token_invalido", buf.getvalue())

    def test_an_exception_from_the_client_is_caught_and_printed_as_a_type_only_line(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares.sincronizar", side_effect=RuntimeError("boom")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                pull._sincronizar_lugares_seguro(conn, 2)  # must not raise
        self.assertIn("lugares failed", buf.getvalue())

    def test_second_call_the_same_day_makes_no_second_request(self):
        conn = _conn(self)
        with _patch_token(), mock.patch("body.lugares._get_json",
                                         return_value=(200, {"eventos": []})) as m:
            pull._sincronizar_lugares_seguro(conn, 2)
            pull._sincronizar_lugares_seguro(conn, 2)
        self.assertEqual(m.call_count, 1)

    def test_backfill_never_calls_it(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: --backfill does not call it."""
        conn = _conn(self)
        with mock.patch("body.pull._sincronizar_lugares_seguro") as m, \
             mock.patch("body.pull.garmin_cuenta.every_person_id", return_value=[]):
            pull.pull_backfill_hosted(conn)
        m.assert_not_called()


class PullHostedCallsItTest(unittest.TestCase):
    """body/pull.py's own daily hosted pull calls the safe wrapper once per connected person."""

    def test_pull_hosted_calls_the_safe_wrapper_per_person(self):
        conn = _conn(self)
        with mock.patch("body.pull.garmin_cuenta.every_person_id", return_value=[2]), \
             mock.patch("body.pull.garmin_cuenta.puede_intentar", return_value=True), \
             mock.patch("body.pull._pull_persona", return_value={"estado": "sin_token", "saved": 0,
                                                                   "vacios": 0, "fallos_parciales": {}}), \
             mock.patch("body.pull._sincronizar_lugares_seguro") as m, \
             mock.patch("body.pull._cerrar_semanas_todas"):
            pull.pull_hosted(conn, fecha="2026-11-18")
        m.assert_called_once_with(conn, 2)


class LocalRunCallsItTest(unittest.TestCase):
    """The local CLI path (`_run`) calls the same safe wrapper after its own recalcs."""

    def test_recompute_path_calls_the_safe_wrapper(self):
        conn = _conn(self)
        conn.execute("INSERT OR IGNORE INTO person (id, display_name) VALUES (1, 'Me')")
        conn.commit()

        class _Args:
            intradia = False
            backfill = False
            hoy = False
            all = False
            recompute = True
            date = None
            days = None

        with mock.patch("body.pull.cuenta.hosted", return_value=False), \
             mock.patch("body.pull._sincronizar_lugares_seguro") as m:
            pull._run(conn, _Args())
        m.assert_called_once()


class AjustesEstadoTest(unittest.TestCase):
    """body/rutas/ajustes.py's `_lugares_estado_texto`: the three states, in both languages."""

    def test_sin_conectar_when_no_sub(self):
        from body.i18n import T
        from body.rutas.ajustes import _lugares_estado_texto
        conn = _conn(self)
        person = {"cuenta_sub": None}
        with _patch_token():
            self.assertEqual(_lugares_estado_texto(T("es"), conn, person, 2), "Lugares: sin conectar")
            self.assertEqual(_lugares_estado_texto(T("en"), conn, person, 2), "Lugares: not connected")

    def test_sin_conectar_when_no_token(self):
        from body.i18n import T
        from body.rutas.ajustes import _lugares_estado_texto
        conn = _conn(self)
        person = {"cuenta_sub": SUB_A}
        with _patch_token(None):
            self.assertEqual(_lugares_estado_texto(T("es"), conn, person, 2), "Lugares: sin conectar")

    def test_connected_shows_the_trip_count(self):
        from body.i18n import T
        from body.rutas.ajustes import _lugares_estado_texto
        conn = _conn(self)
        person = {"cuenta_sub": SUB_A}
        eventos = {"eventos": [_evento(101, "2026-10-01", "2026-10-05"), _evento(102, "2026-11-01", "2026-11-01")]}
        with _patch_token():
            with mock.patch("body.lugares._get_json", return_value=(200, eventos)):
                lugares.sincronizar(conn, 2, HOY)
            self.assertEqual(_lugares_estado_texto(T("es"), conn, person, 2), "Lugares: conectado · 2 viajes")
            self.assertEqual(_lugares_estado_texto(T("en"), conn, person, 2), "Lugares: connected · 2 trips")

    def test_connected_singular_trip(self):
        from body.i18n import T
        from body.rutas.ajustes import _lugares_estado_texto
        conn = _conn(self)
        person = {"cuenta_sub": SUB_A}
        eventos = {"eventos": [_evento(101, "2026-10-01", "2026-10-05")]}
        with _patch_token():
            with mock.patch("body.lugares._get_json", return_value=(200, eventos)):
                lugares.sincronizar(conn, 2, HOY)
            self.assertEqual(_lugares_estado_texto(T("es"), conn, person, 2), "Lugares: conectado · 1 viaje")
            self.assertEqual(_lugares_estado_texto(T("en"), conn, person, 2), "Lugares: connected · 1 trip")

    def test_sin_datos_desde_never_syncd_shows_nunca(self):
        from body.i18n import T
        from body.rutas.ajustes import _lugares_estado_texto
        conn = _conn(self)
        person = {"cuenta_sub": SUB_A}
        with _patch_token():
            with mock.patch("body.lugares._get_json", return_value=(401, {})):
                lugares.sincronizar(conn, 2, HOY)
            self.assertEqual(_lugares_estado_texto(T("es"), conn, person, 2), "Lugares: sin datos desde nunca")
            self.assertEqual(_lugares_estado_texto(T("en"), conn, person, 2), "Lugares: no data since never")

    def test_sin_datos_desde_shows_the_last_ok_date_after_a_later_failure(self):
        from body.i18n import T
        from body.rutas.ajustes import _lugares_estado_texto
        from body.rutas import comun
        conn = _conn(self)
        person = {"cuenta_sub": SUB_A}
        with _patch_token():
            with mock.patch("body.lugares._get_json", return_value=(200, {"eventos": []})):
                lugares.sincronizar(conn, 2, HOY)
            ok_at = conn.execute("SELECT ok_at FROM lugares_sync WHERE person_id = 2").fetchone()["ok_at"]
            with mock.patch("body.lugares._get_json", return_value=(401, {})):
                lugares.sincronizar(conn, 2, HOY)
            t_es = T("es")
            fecha = comun._short_date(t_es, date.fromisoformat(ok_at[:10]))
            self.assertEqual(_lugares_estado_texto(t_es, conn, person, 2), f"Lugares: sin datos desde {fecha}")

    def test_settings_get_renders_the_row_and_writes_nothing(self):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        from body import identity, web

        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "settings_lugares.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        self.addCleanup(web.app.dependency_overrides.clear)
        identity_person_id = 1
        web.app.dependency_overrides[identity.current_person_id] = lambda: identity_person_id

        client = TestClient(web.app, follow_redirects=False)
        before = conn.execute("SELECT COUNT(*) FROM lugares_sync").fetchone()[0]
        r = client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Lugares: sin conectar", r.text)
        after = conn.execute("SELECT COUNT(*) FROM lugares_sync").fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
