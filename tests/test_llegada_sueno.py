"""BODY-SUENO-LLEGADA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1): Carlos, relayed by the desk 27 Nov,
before deciding whether to buy a bed-presence sensor -- "let's keep track on how much time it
takes body app to detect that I'm awake, for multiple days, and we'll see how to adjust and if we
could work only with the body app." Covers the new `llegada_sueno` table and its first-write-wins
write (body/db.py), its one capture point in body/pull.py's shared per-person pull (all four
vias), the report (body/llegada_sueno.py) and the read tool (`leer_llegada_sueno`,
body/agente/herramientas.py). Invented demo data only, no real health figures, no network --
garminconnect itself is never imported here; every Garmin call goes through a mocked
`garmin_adapter.fetch_day`/`fetch_device_last_sync`.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import config, cuenta, db, fechas, garmin_adapter, garmin_cuenta, llegada_sueno, mudanza, pull, privacidad
from body.agente import herramientas

TOKEN_KEY_HEX = "ab" * 32
_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8771",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only", garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}


def _fetch_day_con_sueno(sleep_end):
    def _fetch(day, token_str=None):
        data = garmin_adapter.DayData(day=day, raw={"resumen": {}}, metrics={"resting_hr": 55.0},
                                       sleep_end=sleep_end)
        return data if token_str is None else (data, token_str)
    return _fetch


def _fetch_day_sin_sueno(day, token_str=None):
    data = garmin_adapter.DayData(day=day, raw={"resumen": {}}, metrics={"resting_hr": 55.0})
    return data if token_str is None else (data, token_str)


def _no_reloj(token_str=None):
    return (None, token_str) if token_str is not None else None


def _reloj_fijo(cuando: datetime):
    def _fetch(token_str=None):
        return (cuando, token_str) if token_str is not None else cuando
    return _fetch


# --------------------------------------------------------------------------------- ruling 1: table

class TableTest(unittest.TestCase):
    def test_fresh_db_has_the_table_and_check_constraints(self):
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "fresh.db")
        db.init(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llegada_sueno)")}
        self.assertEqual(
            cols, {"person_id", "day", "noche_fin", "primera_llegada_en", "reloj_en_llegada", "via"})
        with self.assertRaises(Exception):
            conn.execute(
                "INSERT INTO llegada_sueno (person_id, day, primera_llegada_en, via) "
                "VALUES (1, '2026-11-01', '2026-11-01T00:00:00+00:00', 'invalida')")

    def test_init_is_idempotent(self):
        conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "idem.db")
        db.init(conn)
        db.init(conn)  # must not raise
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llegada_sueno)")}
        self.assertIn("primera_llegada_en", cols)


# -------------------------------------------------------------- ruling 1/2: the write, at db level

class RegistrarLlegadaSuenoTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "registrar.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)

    def test_first_write_wins_a_refetch_never_moves_it(self):
        db.registrar_llegada_sueno(self.conn, self.person_id, "2026-11-20", "2026-11-20T06:30",
                                    datetime(2026, 11, 20, 11, 45, tzinfo=timezone.utc), "diaria")
        self.conn.commit()
        primera = db.llegadas_sueno(self.conn, self.person_id, "2026-11-01", "2026-11-30")

        db.registrar_llegada_sueno(self.conn, self.person_id, "2026-11-20", "2026-11-20T07:15",
                                    datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc), "hoy")
        self.conn.commit()
        segunda = db.llegadas_sueno(self.conn, self.person_id, "2026-11-01", "2026-11-30")

        self.assertEqual(primera, segunda)
        self.assertEqual(segunda[0]["via"], "diaria")

    def test_a_failed_reloj_read_stores_null_not_an_older_value(self):
        db.registrar_llegada_sueno(self.conn, self.person_id, "2026-11-21", "2026-11-21T06:00", None, "diaria")
        self.conn.commit()
        fila = db.llegadas_sueno(self.conn, self.person_id, "2026-11-21", "2026-11-21")[0]
        self.assertIsNone(fila["reloj_en_llegada"])

    def test_retention_drops_rows_older_than_the_bound(self):
        import datetime as real_datetime
        instante_fijo = real_datetime.datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
        vieja = (instante_fijo - timedelta(days=db.LLEGADA_RETENCION_DIAS + 5)).date().isoformat()
        reciente = "2026-11-20"
        with mock.patch("body.db.datetime") as fake_dt:
            fake_dt.now.side_effect = lambda tz=None: instante_fijo.astimezone(tz) if tz is not None else instante_fijo
            db.registrar_llegada_sueno(self.conn, self.person_id, vieja, "2026-01-01T06:00", None, "diaria")
            self.conn.commit()
            db.registrar_llegada_sueno(self.conn, self.person_id, reciente, "2026-11-20T06:00", None, "diaria")
            self.conn.commit()
        filas = db.llegadas_sueno(self.conn, self.person_id, "2000-01-01", "2026-12-31")
        dias = {f["day"] for f in filas}
        self.assertNotIn(vieja, dias)
        self.assertIn(reciente, dias)

    def test_two_people_never_mix(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        self.conn.commit()
        db.registrar_llegada_sueno(self.conn, self.person_id, "2026-11-20", "2026-11-20T06:00", None, "diaria")
        db.registrar_llegada_sueno(self.conn, 2, "2026-11-20", "2026-11-20T06:00", None, "diaria")
        self.conn.commit()
        self.assertEqual(len(db.llegadas_sueno(self.conn, self.person_id, "2026-11-01", "2026-11-30")), 1)
        self.assertEqual(len(db.llegadas_sueno(self.conn, 2, "2026-11-01", "2026-11-30")), 1)


# -------------------------------------------------------- ruling 2: the one capture point, via pull

class _PullCaptureTestCase(unittest.TestCase):
    """Shared fixture: config.DB_PATH itself points at this test's own tmp file (the same pattern
    tests/test_sync_vivo.py's own `_fresh_db` uses), so a function under test that opens its OWN
    `db.connect()` (pull_persona_ahora, pull_hoy_ahora) reaches the SAME database file this test's
    own `self.conn` already holds open -- never a copy."""

    def setUp(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "captura.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.ensure_ready(self.conn)
        env_patcher = mock.patch.dict(os.environ, _HOSTED_ENV)
        env_patcher.start(); self.addCleanup(env_patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start(); self.addCleanup(sleep_patcher.stop)
        garmin_cuenta.guardar(self.conn, 1, "tok-demo")
        self.conn.commit()


class PullHostedCaptureTest(_PullCaptureTestCase):
    """via='diaria', through pull.pull_hosted."""

    def test_a_non_empty_sleep_end_claims_the_arrival_with_the_same_calls_own_reloj(self):
        reloj = datetime(2026, 11, 20, 11, 45, tzinfo=timezone.utc)
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                         side_effect=_fetch_day_con_sueno("2026-11-20T06:30")), \
             mock.patch("body.pull.garmin_adapter.fetch_device_last_sync", side_effect=_reloj_fijo(reloj)):
            pull.pull_hosted(self.conn, fecha="2026-11-20")
        filas = db.llegadas_sueno(self.conn, 1, "2026-11-20", "2026-11-20")
        self.assertEqual(len(filas), 1)
        fila = filas[0]
        self.assertEqual(fila["via"], "diaria")
        self.assertEqual(fila["noche_fin"], "2026-11-20T06:30")
        self.assertEqual(fila["reloj_en_llegada"], reloj.isoformat(timespec="seconds"))

    def test_an_empty_sleep_end_writes_nothing_a_later_refetch_claims_it(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_fetch_day_sin_sueno), \
             mock.patch("body.pull.garmin_adapter.fetch_device_last_sync", side_effect=_no_reloj):
            pull.pull_hosted(self.conn, fecha="2026-11-21")
        self.assertEqual(db.llegadas_sueno(self.conn, 1, "2026-11-21", "2026-11-21"), [])

        with mock.patch("body.pull.garmin_adapter.fetch_day",
                         side_effect=_fetch_day_con_sueno("2026-11-21T07:00")), \
             mock.patch("body.pull.garmin_adapter.fetch_device_last_sync", side_effect=_no_reloj):
            pull.pull_hosted(self.conn, fecha="2026-11-21")
        filas = db.llegadas_sueno(self.conn, 1, "2026-11-21", "2026-11-21")
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["via"], "diaria")


class PullConectarCaptureTest(_PullCaptureTestCase):
    """via='conectar', through pull.pull_persona_ahora -- which opens its own db.connect(), so
    reaches the same file via config.DB_PATH (the base fixture's own setUp)."""

    def setUp(self):
        super().setUp()
        reloj_patcher = mock.patch(
            "body.pull.garmin_adapter.fetch_device_last_sync", side_effect=_no_reloj)
        reloj_patcher.start(); self.addCleanup(reloj_patcher.stop)

    def test_connect_triggered_pull_captures_via_conectar(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                         side_effect=_fetch_day_con_sueno("2026-11-22T06:00")):
            pull.pull_persona_ahora(1)
        filas = db.llegadas_sueno(self.conn, 1, "2026-11-22", "2026-11-22")
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["via"], "conectar")


class PullHoyTodosCaptureTest(_PullCaptureTestCase):
    """via='hoy', through pull.pull_hoy_todos -- shares this test's own self.conn directly."""

    def setUp(self):
        super().setUp()
        quietas_patcher = mock.patch("body.pull._en_horas_quietas", return_value=False)
        quietas_patcher.start(); self.addCleanup(quietas_patcher.stop)
        reloj_patcher = mock.patch(
            "body.pull.garmin_adapter.fetch_device_last_sync", side_effect=_no_reloj)
        reloj_patcher.start(); self.addCleanup(reloj_patcher.stop)

    def test_the_sweep_captures_via_hoy(self):
        dia_hoy = fechas.hoy_local(db.person(self.conn, 1)).isoformat()
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                         side_effect=_fetch_day_con_sueno(f"{dia_hoy}T06:00")):
            pull.pull_hoy_todos(self.conn)
        filas = db.llegadas_sueno(self.conn, 1, dia_hoy, dia_hoy)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["via"], "hoy")


class PullHoyAhoraCaptureTest(_PullCaptureTestCase):
    """via='demanda', through pull.pull_hoy_ahora -- also opens its own db.connect()."""

    def setUp(self):
        super().setUp()
        self.addCleanup(pull._PULL_EN_CURSO.clear)
        quietas_patcher = mock.patch("body.pull._en_horas_quietas", return_value=False)
        quietas_patcher.start(); self.addCleanup(quietas_patcher.stop)
        reloj_patcher = mock.patch(
            "body.pull.garmin_adapter.fetch_device_last_sync", side_effect=_no_reloj)
        reloj_patcher.start(); self.addCleanup(reloj_patcher.stop)

    def test_the_on_demand_trigger_captures_via_demanda(self):
        dia_hoy = fechas.hoy_local(db.person(self.conn, 1)).isoformat()
        with mock.patch("body.pull.garmin_adapter.fetch_day",
                         side_effect=_fetch_day_con_sueno(f"{dia_hoy}T06:00")):
            estado = pull.pull_hoy_ahora(1)
        self.assertEqual(estado, "iniciado")
        filas = db.llegadas_sueno(self.conn, 1, dia_hoy, dia_hoy)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["via"], "demanda")


# ------------------------------------------------------------------------- ruling 3: jornada split

class JornadaTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "jornada.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)

    def test_an_off_shift_is_libre(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local) VALUES (?, ?, 'off', 'L', NULL)",
            (self.person_id, "2026-11-20"))
        self.conn.commit()
        self.assertEqual(llegada_sueno._jornada(self.conn, self.person_id, "2026-11-20"), "libre")

    def test_any_other_kind_is_trabajo(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local) "
            "VALUES (?, ?, 'night', 'N', '2026-11-20T22:00')",
            (self.person_id, "2026-11-20"))
        self.conn.commit()
        self.assertEqual(llegada_sueno._jornada(self.conn, self.person_id, "2026-11-20"), "trabajo")

    def test_no_shift_row_is_desconocido(self):
        self.assertEqual(llegada_sueno._jornada(self.conn, self.person_id, "2026-11-20"), "desconocido")


# ----------------------------------------------------------------------------- ruling 4: the report

class ResumenTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "resumen.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        self.conn.execute("UPDATE person SET timezone = 'America/Bogota' WHERE id = ?", (self.person_id,))
        self.conn.commit()

    def _poner(self, day, noche_fin_local, llegada_utc, reloj_utc=None):
        self.conn.execute(
            "INSERT INTO llegada_sueno (person_id, day, noche_fin, primera_llegada_en, reloj_en_llegada, via) "
            "VALUES (?, ?, ?, ?, ?, 'diaria')",
            (self.person_id, day, noche_fin_local, llegada_utc,
             reloj_utc.isoformat(timespec="seconds") if reloj_utc else None),
        )
        self.conn.commit()

    def test_minutes_across_a_non_utc_timezone(self):
        # Bogota is UTC-5: 06:30 local is 11:30 UTC; a 12:00 UTC arrival is a 30-minute gap.
        self._poner("2026-11-20", "2026-11-20T06:30", "2026-11-20T12:00:00+00:00")
        r = llegada_sueno.resumen(self.conn, self.person_id, "2026-11-01", "2026-11-30")
        self.assertEqual(r["total"]["desconocido"]["mediana_min"], 30)
        self.assertEqual(r["noches"], 1)
        self.assertEqual(r["descartadas"], 0)

    def test_a_negative_gap_lands_in_descartadas_never_averaged(self):
        # primera_llegada_en BEFORE noche_fin's own UTC instant -- a corrected night.
        self._poner("2026-11-21", "2026-11-21T06:30", "2026-11-21T10:00:00+00:00")
        r = llegada_sueno.resumen(self.conn, self.person_id, "2026-11-01", "2026-11-30")
        self.assertEqual(r["descartadas"], 1)
        self.assertEqual(r["total"]["desconocido"]["noches"], 0)

    def test_split_by_jornada(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local) VALUES (?, ?, 'off', 'L', NULL)",
            (self.person_id, "2026-11-22"))
        self.conn.commit()
        self._poner("2026-11-22", "2026-11-22T06:30", "2026-11-22T12:00:00+00:00")
        r = llegada_sueno.resumen(self.conn, self.person_id, "2026-11-01", "2026-11-30")
        self.assertEqual(r["total"]["libre"]["noches"], 1)
        self.assertEqual(r["total"]["trabajo"]["noches"], 0)
        self.assertEqual(r["total"]["desconocido"]["noches"], 0)

    def test_reloj_splits_into_two_sub_gaps(self):
        self._poner("2026-11-23", "2026-11-23T06:30", "2026-11-23T12:00:00+00:00",
                    reloj_utc=datetime(2026, 11, 23, 11, 45, tzinfo=timezone.utc))
        r = llegada_sueno.resumen(self.conn, self.person_id, "2026-11-01", "2026-11-30")
        self.assertEqual(r["reloj_a_nube"]["desconocido"]["mediana_min"], 15)
        self.assertEqual(r["nube_a_body"]["desconocido"]["mediana_min"], 15)

    def test_counts_by_via(self):
        self._poner("2026-11-24", "2026-11-24T06:30", "2026-11-24T12:00:00+00:00")
        self.conn.execute(
            "UPDATE llegada_sueno SET via = 'hoy' WHERE person_id = ? AND day = ?",
            (self.person_id, "2026-11-24"))
        self.conn.commit()
        r = llegada_sueno.resumen(self.conn, self.person_id, "2026-11-01", "2026-11-30")
        self.assertEqual(r["por_via"], {"hoy": 1})


class ReportCliTest(unittest.TestCase):
    def test_runs_clean_with_no_connected_person(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "cli.db"
        conn = db.connect()
        db.init(conn)
        conn.commit()
        conn.close()
        self.assertEqual(llegada_sueno.main(["--dias", "7"]), 0)


# ---------------------------------------------------------------------- ruling 5: the read tool

class LeerLlegadaSuenoToolTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "tool.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        self.conn.commit()
        db.registrar_llegada_sueno(self.conn, self.person_id, "2026-11-20", "2026-11-20T06:30",
                                    datetime(2026, 11, 20, 11, 45, tzinfo=timezone.utc), "diaria")
        db.registrar_llegada_sueno(self.conn, 2, "2026-11-21", "2026-11-21T06:30",
                                    datetime(2026, 11, 21, 11, 45, tzinfo=timezone.utc), "hoy")
        self.conn.commit()

    def test_is_registered_at_nivel_ninguno(self):
        entrada = herramientas._BY_NAME["leer_llegada_sueno"]
        self.assertEqual(entrada["nivel"], "ninguno")
        self.assertEqual(entrada["tipo"], "lectura")

    def test_returns_only_the_session_persons_rows_never_mixing(self):
        salida = herramientas.ejecutar(self.conn, self.person_id, "leer_llegada_sueno", {}, "ninguno")
        self.assertEqual(len(salida["noches"]), 1)
        self.assertEqual(salida["noches"][0]["dia"], "2026-11-20")
        self.assertEqual(salida["resumen"]["noches"], 1)

        salida_otra = herramientas.ejecutar(self.conn, 2, "leer_llegada_sueno", {}, "ninguno")
        self.assertEqual(len(salida_otra["noches"]), 1)
        self.assertEqual(salida_otra["noches"][0]["dia"], "2026-11-21")
        self.assertNotEqual(salida, salida_otra)

    def test_never_outputs_a_sleep_figure(self):
        salida = herramientas.ejecutar(self.conn, self.person_id, "leer_llegada_sueno", {}, "ninguno")
        noche = salida["noches"][0]
        self.assertEqual(set(noche), {"dia", "noche_fin", "primera_llegada_en", "via", "reloj_en_llegada",
                                       "jornada"})

    def test_hasta_cannot_be_future(self):
        persona = db.person(self.conn, self.person_id)
        futuro = (fechas.hoy_local(persona) + timedelta(days=5)).isoformat()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, self.person_id, "leer_llegada_sueno",
                                   {"hasta": futuro}, "ninguno")


# --------------------------------------------- BODY-SUENO-LLEGADA-1.W ruling 2: export/delete-all

class ExportDeleteTest(unittest.TestCase):
    """llegada_sueno joins FULLY_HANDLED_TABLES, treated exactly like despertar_correccion: fully
    exported under its own key, fully deleted with the rest of delete-all. Invented demo data
    only."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "export.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        self.conn.commit()
        db.registrar_llegada_sueno(self.conn, 1, "2026-11-20", "2026-11-20T06:30",
                                    datetime(2026, 11, 20, 11, 45, tzinfo=timezone.utc), "diaria")
        db.registrar_llegada_sueno(self.conn, 2, "2026-11-21", "2026-11-21T06:30",
                                    datetime(2026, 11, 21, 11, 45, tzinfo=timezone.utc), "hoy")
        self.conn.commit()

    def test_llegada_sueno_is_a_fully_handled_table(self):
        self.assertIn("llegada_sueno", privacidad.FULLY_HANDLED_TABLES)

    def test_export_carries_only_this_persons_rows(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["llegadas_sueno"]), 1)
        fila = data["llegadas_sueno"][0]
        self.assertEqual(
            set(fila), {"day", "noche_fin", "primera_llegada_en", "reloj_en_llegada", "via"})
        self.assertEqual(fila["day"], "2026-11-20")
        self.assertEqual(fila["via"], "diaria")

        data_otra = privacidad.export_person(self.conn, 2)
        self.assertEqual(len(data_otra["llegadas_sueno"]), 1)
        self.assertEqual(data_otra["llegadas_sueno"][0]["day"], "2026-11-21")

    def test_delete_all_removes_only_this_persons_rows(self):
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM llegada_sueno WHERE person_id = 1").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM llegada_sueno WHERE person_id = 2").fetchone()[0], 1)


# ------------------------------------------------- BODY-SUENO-LLEGADA-1.W ruling 2: a host move

class MudanzaRoundTripTest(unittest.TestCase):
    """llegada_sueno joins the mudanza registry beside despertar_correccion
    (TableSpec(match_cols=("person_id", "day"))) -- one row migrates from a PC install to its
    hosted destination, invented demo data only."""

    CUENTA_SUB = "sub-llegada-sueno-mudanza-test"

    def setUp(self):
        self._orig_home, self._orig_db = config.BODY_HOME, config.DB_PATH
        self.addCleanup(lambda: setattr(config, "BODY_HOME", self._orig_home))
        self.addCleanup(lambda: setattr(config, "DB_PATH", self._orig_db))
        self.tmp = Path(tempfile.mkdtemp(dir=TMP, prefix="llegada-mudanza-"))
        config.BODY_HOME = self.tmp
        config.DB_PATH = self.tmp / "host.db"
        self.source_path = self.tmp / "pc.db"

    def test_a_row_migrates_to_its_hosted_destination(self):
        source = db.connect(path=self.source_path)
        db.init(source)
        source_person_id = db.default_person_id()
        db.registrar_llegada_sueno(source, source_person_id, "2026-11-20", "2026-11-20T06:30",
                                    datetime(2026, 11, 20, 11, 45, tzinfo=timezone.utc), "diaria")
        source.commit()
        source.close()

        reports, ok = mudanza.importar(self.source_path, self.CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = {r.name: r for r in reports}
        self.assertEqual(by_name["llegada_sueno"].counts.insert, 1)

        dest = db.connect()
        self.addCleanup(dest.close)
        dest_person_id = dest.execute(
            "SELECT id FROM person WHERE cuenta_sub = ?", (self.CUENTA_SUB,)).fetchone()[0]
        filas = db.llegadas_sueno(dest, dest_person_id, "2026-11-01", "2026-11-30")
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["via"], "diaria")


if __name__ == "__main__":
    unittest.main()
