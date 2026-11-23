"""BODY-HISTORIA-MINUTO-2: body.cobertura, a read-only, counts-and-dates-only report of what is
actually stored for one person's minute-by-minute history. No network: every payload below is
invented demo data, never a real figure. Every sentinel value chosen below (173, 999, 55.5, ...)
stands in for a real health figure and must never appear in the command's own output -- only the
sample COUNT it produces may.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import backup, cobertura, config, cuenta, db, fechas, garmin_adapter, pull


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _raw(conn, person_id, day, kind, source, payload_text, fetched_at="2026-01-01T00:00:00+00:00"):
    """Writes a raw_payload row with the EXACT text given -- used both for ordinary JSON payloads
    (json.dumps(...)) and for deliberately malformed text that will not parse as JSON at all."""
    conn.execute(
        "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (person_id, day, kind, source, fetched_at, payload_text),
    )
    conn.commit()


def _hr_payload(ts_ms: int, value) -> dict:
    return {
        "heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
        "heartRateValues": [[ts_ms, value]],
    }


def _stress_payload(ts_ms: int, value) -> dict:
    return {
        "stressValueDescriptorsDTOList": [{"key": "timestamp", "index": 0}, {"key": "stresslevel", "index": 1}],
        "stressValuesArray": [[ts_ms, value]],
    }


def _sleep_payload(sentinel_hr=173, sentinel_hrv=55.5, sentinel_stage=0.0, sentinel_mov=1.2,
                    sentinel_spo2=96) -> dict:
    return {
        "sleepHeartRate": [{"startGMT": "2026-11-10 02:00:00.0", "value": sentinel_hr},
                           {"startGMT": "2026-11-10 02:05:00.0", "value": sentinel_hr}],
        "hrvData": [{"startGMT": "2026-11-10 02:00:00.0", "value": sentinel_hrv}],
        "sleepLevels": [{"startGMT": "2026-11-10 02:00:00.0", "activityLevel": sentinel_stage}],
        "sleepMovement": [{"startGMT": "2026-11-10 02:00:00.0", "activityLevel": sentinel_mov}],
        "wellnessEpochSPO2DataDTOList": [{"epochTimestamp": "2026-11-10 02:00:00.0", "spo2Reading": sentinel_spo2}],
    }


def _bb_payload(values: list) -> list:
    return [{"bodyBatteryValuesArray": values}]


def _actividad(conn, person_id, source_id, day, start_local, duration_s, type_="running",
                source=None, name="Secret Activity Name"):
    """A row in `activity` -- `name` is a sentinel that must never reach the block's own output
    (BODY-ESFUERZO-COBERTURA-1 ruling 2 bans it explicitly)."""
    conn.execute(
        "INSERT OR REPLACE INTO activity (person_id, source, source_id, day, start_local, type, "
        "name, duration_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, source or garmin_adapter.SOURCE, source_id, day, start_local, type_, name, duration_s),
    )
    conn.commit()


def _detalle_payload(ts_values: list, hr_values: list, key_ts="directTimestamp", key_hr="directHeartRate") -> dict:
    """The real activity-details shape, confirmed from the installed garminconnect 0.3.20
    package's own source (activity_details.py:8-44, BODY-ESFUERZO-REAL-1-INV): each descriptor
    names its column with "metricsIndex", not the day-level intraday descriptors' "index" --
    or, with other key names passed in, a payload whose descriptor keys do not match it at all."""
    return {
        "metricDescriptors": [{"key": key_ts, "metricsIndex": 0}, {"key": key_hr, "metricsIndex": 1}],
        "activityDetailMetrics": [{"metrics": [t, h]} for t, h in zip(ts_values, hr_values)],
    }


def _epoch_ms(iso_local: str) -> int:
    """`iso_local` read as a UTC wall-clock instant -- paired with a person whose own timezone is
    'UTC' in these tests, so a sample's epoch-ms timestamp and its local ISO string agree exactly,
    with no timezone arithmetic for the test itself to get wrong."""
    return int(datetime.fromisoformat(iso_local).replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------- intraday fc/estres


class IntradiaCoverageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "intradia.db")

    def test_counts_by_source_separately_and_valid_days_separately(self):
        p = db.local_person_id(self.conn)
        # garmin: one valid day, one malformed-JSON day, one well-formed-but-shapeless day.
        _raw(self.conn, p, "2026-07-01", "intradia_fc", garmin_adapter.SOURCE,
             json.dumps(_hr_payload(1762300800000, 173)))
        _raw(self.conn, p, "2026-07-02", "intradia_fc", garmin_adapter.SOURCE, "not-json-at-all")
        _raw(self.conn, p, "2026-07-03", "intradia_fc", garmin_adapter.SOURCE, json.dumps({"nada": []}))
        # garmin_export: one valid day only.
        _raw(self.conn, p, "2026-06-15", "intradia_fc", garmin_adapter.SOURCE_EXPORT,
             json.dumps(_hr_payload(1762300800000, 173)))

        out = cobertura.recopilar(self.conn)
        garmin = out["intradia_fc"][garmin_adapter.SOURCE]
        self.assertEqual(garmin["dias"], 3)
        self.assertEqual(garmin["primero"], "2026-07-01")
        self.assertEqual(garmin["ultimo"], "2026-07-03")
        self.assertEqual(garmin["dias_validos"], 1)  # the malformed and shapeless days do not count

        export = out["intradia_fc"][garmin_adapter.SOURCE_EXPORT]
        self.assertEqual(export["dias"], 1)
        self.assertEqual(export["dias_validos"], 1)

        # The sentinel heart-rate figure (173) never appears in the report; only counts do.
        dumped = json.dumps(out)
        self.assertNotIn("173", dumped)

    def test_estres_counted_independently_of_fc(self):
        p = db.local_person_id(self.conn)
        _raw(self.conn, p, "2026-07-01", "intradia_estres", garmin_adapter.SOURCE,
             json.dumps(_stress_payload(1762300800000, 25)))
        out = cobertura.recopilar(self.conn)
        self.assertEqual(out["intradia_estres"][garmin_adapter.SOURCE]["dias"], 1)
        self.assertEqual(out["intradia_fc"][garmin_adapter.SOURCE]["dias"], 0)


# -------------------------------------------------------------------------------------- sleep keys


class SleepCoverageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "sleep.db")

    def test_each_key_counted_present_or_missing_and_median_samples(self):
        p = db.local_person_id(self.conn)
        _raw(self.conn, p, "2026-11-10", "sleep", garmin_adapter.SOURCE,
             json.dumps(_sleep_payload()))
        # A night with only heart rate, missing all four other keys.
        _raw(self.conn, p, "2026-11-11", "sleep", garmin_adapter.SOURCE,
             json.dumps({"sleepHeartRate": [{"startGMT": "2026-11-11 02:00:00.0", "value": 173}]}))
        # A night whose payload is not even a dict -- stored, but unparseable as sleep data.
        _raw(self.conn, p, "2026-11-12", "sleep", garmin_adapter.SOURCE, json.dumps("not-a-dict"))

        out = cobertura.recopilar(self.conn)
        sleep = out["sleep"]
        self.assertEqual(sleep["noches"], 3)
        self.assertEqual(sleep["primero"], "2026-11-10")
        self.assertEqual(sleep["ultimo"], "2026-11-12")

        claves = sleep["claves"]
        self.assertEqual(claves["sleepHeartRate"]["noches"], 2)
        self.assertEqual(claves["sleepHeartRate"]["mediana_muestras"], 1.5)  # 2 samples, then 1
        self.assertEqual(claves["hrvData"]["noches"], 1)
        self.assertEqual(claves["sleepLevels"]["noches"], 1)
        self.assertEqual(claves["sleepMovement"]["noches"], 1)
        self.assertEqual(claves["wellnessEpochSPO2DataDTOList"]["noches"], 1)

        dumped = json.dumps(out)
        self.assertNotIn("173", dumped)
        self.assertNotIn("55.5", dumped)
        self.assertNotIn("96", dumped)

    def test_no_sleep_stored_is_all_zero_and_none(self):
        db.local_person_id(self.conn)
        out = cobertura.recopilar(self.conn)
        sleep = out["sleep"]
        self.assertEqual(sleep["noches"], 0)
        self.assertIsNone(sleep["primero"])
        for datos in sleep["claves"].values():
            self.assertEqual(datos["noches"], 0)
            self.assertIsNone(datos["mediana_muestras"])
        self.assertEqual(sleep["claves_crudas"], {})


# -------------------------------------------------------------- ruling 3: raw top-level key census


class ClavesCrudasTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: every top-level key name a stored sleep payload
    carries, with nights present/non-empty -- names and counts only, so an assumed key that is
    simply wrong (BODY-COBERTURA-HOST-1's SpO2 census) can be told apart from a key that is
    genuinely absent, by naming whatever key IS there instead."""

    def setUp(self):
        self.conn = _db(self, "claves_crudas.db")

    def test_counts_every_top_level_key_including_an_unassumed_one(self):
        p = db.local_person_id(self.conn)
        # An invented key no assumed spec names -- the real-world case this ruling answers: the
        # watch may record SpO2 under a name nobody assumed, and this census must still find it.
        _raw(self.conn, p, "2026-11-10", "sleep", garmin_adapter.SOURCE, json.dumps({
            **_sleep_payload(), "unknownSpo2KeyXYZ": [{"epochTimestamp": "t", "spo2Reading": 999}],
        }))
        _raw(self.conn, p, "2026-11-11", "sleep", garmin_adapter.SOURCE, json.dumps({
            "sleepHeartRate": [{"startGMT": "2026-11-11 02:00:00.0", "value": 173}],
            "unknownSpo2KeyXYZ": [{"epochTimestamp": "t", "spo2Reading": 999}],
        }))
        # A third night where the unassumed key is present but EMPTY -- never counted.
        _raw(self.conn, p, "2026-11-12", "sleep", garmin_adapter.SOURCE, json.dumps({
            "sleepHeartRate": [{"startGMT": "2026-11-12 02:00:00.0", "value": 173}],
            "unknownSpo2KeyXYZ": [],
        }))

        out = cobertura.recopilar(self.conn)
        crudas = out["sleep"]["claves_crudas"]
        self.assertEqual(crudas["unknownSpo2KeyXYZ"], 2)  # the third night's empty array never counts
        self.assertEqual(crudas["sleepHeartRate"], 3)
        self.assertEqual(crudas["wellnessEpochSPO2DataDTOList"], 1)  # from _sleep_payload()'s own night
        self.assertEqual(crudas["hrvData"], 1)
        self.assertEqual(crudas["sleepLevels"], 1)
        self.assertEqual(crudas["sleepMovement"], 1)

        # Never a value, invented or real: only the key NAME and a count ever reach the output.
        dumped = json.dumps(out)
        self.assertNotIn("999", dumped)
        self.assertNotIn("173", dumped)

    def test_a_payload_that_is_not_a_dict_contributes_no_key(self):
        p = db.local_person_id(self.conn)
        _raw(self.conn, p, "2026-11-10", "sleep", garmin_adapter.SOURCE, json.dumps("not-a-dict"))
        out = cobertura.recopilar(self.conn)
        self.assertEqual(out["sleep"]["claves_crudas"], {})


# ---------------------------------------------------------------------------------- body battery


class BodyBatteryCoverageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "bb.db")

    def test_days_stored_vs_days_with_data_and_median(self):
        p = db.local_person_id(self.conn)
        _raw(self.conn, p, "2026-11-01", "body_battery", garmin_adapter.SOURCE,
             json.dumps(_bb_payload([[1762300800000, 55], [1762300860000, 54], [1762300920000, 53]])))
        _raw(self.conn, p, "2026-11-02", "body_battery", garmin_adapter.SOURCE,
             json.dumps(_bb_payload([])))  # stored, but empty array
        _raw(self.conn, p, "2026-11-03", "body_battery", garmin_adapter.SOURCE,
             json.dumps({"not": "a list"}))  # malformed shape

        out = cobertura.recopilar(self.conn)
        bb = out["body_battery"]
        self.assertEqual(bb["dias"], 3)
        self.assertEqual(bb["dias_con_datos"], 1)
        self.assertEqual(bb["mediana_muestras"], 3)

        dumped = json.dumps(out)
        self.assertNotIn("55", dumped)  # the invented body-battery score never leaks
        self.assertNotIn("54", dumped)
        self.assertNotIn("53", dumped)


# -------------------------------------------------------------------------------------- the keeper


class KeeperGapTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "keeper.db")
        self.person_id = db.local_person_id(self.conn)
        self.hoy = fechas.hoy_local(db.person(self.conn, self.person_id))

    def _dia(self, back: int) -> str:
        from datetime import timedelta
        return (self.hoy - timedelta(days=back)).isoformat()

    def test_counts_days_with_a_daily_pull_and_the_oldest_gap_per_kind(self):
        p = self.person_id
        # Four days had a daily pull (the 'sleep' feed, source=garmin), all inside the window.
        for back in (1, 2, 3, 50):
            _raw(self.conn, p, self._dia(back), "sleep", garmin_adapter.SOURCE, json.dumps({}))
        # intradia_fc present for every day except back=3 (the gap).
        for back in (1, 2, 50):
            _raw(self.conn, p, self._dia(back), "intradia_fc", garmin_adapter.SOURCE,
                 json.dumps(_hr_payload(1762300800000, 60)))
        # intradia_estres present for every day except back=50 (a DIFFERENT gap).
        for back in (1, 2, 3):
            _raw(self.conn, p, self._dia(back), "intradia_estres", garmin_adapter.SOURCE,
                 json.dumps(_stress_payload(1762300800000, 10)))

        out = cobertura.recopilar(self.conn)
        keeper = out["keeper"]
        self.assertEqual(keeper["intradia_fc"]["dias_con_pull_diario"], 4)
        self.assertEqual(keeper["intradia_fc"]["dias_sin_intradia"], 1)
        self.assertEqual(keeper["intradia_fc"]["dia_mas_antiguo_faltante"], self._dia(3))

        self.assertEqual(keeper["intradia_estres"]["dias_con_pull_diario"], 4)
        self.assertEqual(keeper["intradia_estres"]["dias_sin_intradia"], 1)
        self.assertEqual(keeper["intradia_estres"]["dia_mas_antiguo_faltante"], self._dia(50))

    def test_no_daily_pulls_is_zero_gap(self):
        out = cobertura.recopilar(self.conn)
        for kind in db.INTRADIA_KINDS:
            self.assertEqual(out["keeper"][kind]["dias_con_pull_diario"], 0)
            self.assertEqual(out["keeper"][kind]["dias_sin_intradia"], 0)
            self.assertIsNone(out["keeper"][kind]["dia_mas_antiguo_faltante"])


# --------------------------------------------------------------------- person scope / cuenta-sub


class PersonScopeTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "scope.db")

    def test_another_persons_rows_are_never_counted(self):
        p1 = db.local_person_id(self.conn)
        p2 = cuenta.resolve_person(self.conn, {"sub": "sub-2", "duna_productos": ["body"]})
        self.conn.commit()
        _raw(self.conn, p1, "2026-07-01", "intradia_fc", garmin_adapter.SOURCE,
             json.dumps(_hr_payload(1762300800000, 60)))
        _raw(self.conn, p2, "2026-08-01", "intradia_fc", garmin_adapter.SOURCE,
             json.dumps(_hr_payload(1762300800000, 999)))

        out1 = cobertura.recopilar(self.conn)
        self.assertEqual(out1["intradia_fc"][garmin_adapter.SOURCE]["dias"], 1)
        self.assertEqual(out1["intradia_fc"][garmin_adapter.SOURCE]["primero"], "2026-07-01")

        out2 = cobertura.recopilar(self.conn, cuenta_sub="sub-2")
        self.assertEqual(out2["intradia_fc"][garmin_adapter.SOURCE]["dias"], 1)
        self.assertEqual(out2["intradia_fc"][garmin_adapter.SOURCE]["primero"], "2026-08-01")

        self.assertNotIn("999", json.dumps(out1))

    def test_unknown_cuenta_sub_is_refused(self):
        db.local_person_id(self.conn)
        with self.assertRaises(ValueError):
            cobertura.recopilar(self.conn, cuenta_sub="no-such-sub")

    def test_hosted_requires_cuenta_sub(self):
        db.local_person_id(self.conn)
        with mock.patch.object(cuenta, "hosted", return_value=True):
            with self.assertRaises(ValueError):
                cobertura.recopilar(self.conn, cuenta_sub=None)

    def test_hosted_with_cuenta_sub_resolves_without_creating_anyone(self):
        p2 = cuenta.resolve_person(self.conn, {"sub": "sub-3", "duna_productos": ["body"]})
        self.conn.commit()
        before = self.conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        with mock.patch.object(cuenta, "hosted", return_value=True):
            out = cobertura.recopilar(self.conn, cuenta_sub="sub-3")
        after = self.conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        self.assertEqual(before, after)  # a read-only resolution never creates a person
        self.assertIn("keeper", out)


# ----------------------------------------------- ruling 3: the standing-go census (--conteo)


class ConteoPersonasTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "conteo.db")

    def test_consentidos_counts_only_hosted_persons_with_a_consent(self):
        p_hosted = cuenta.resolve_person(self.conn, {"sub": "sub-a", "duna_productos": ["body"]})
        p_hosted_sin_consentir = cuenta.resolve_person(
            self.conn, {"sub": "sub-b", "duna_productos": ["body"]})
        p_local = db.local_person_id(self.conn)
        for p in (p_hosted, p_local):
            self.conn.execute(
                "INSERT INTO consentimiento (person_id, version, accepted_at) VALUES (?, ?, ?)",
                (p, "consentimiento-v4", "2026-01-01T00:00:00+00:00"),
            )
        self.conn.commit()
        self.assertIsNotNone(p_hosted_sin_consentir)

        out = cobertura.conteo_personas(self.conn)
        # Only the hosted, consented person counts -- the local install's own person is never
        # hosted (no cuenta_sub), and the hosted-but-not-consented person has no consent row.
        self.assertEqual(out["consentidos"], 1)

    def test_activos_30d_counts_recent_sessions_only(self):
        p_reciente = cuenta.resolve_person(self.conn, {"sub": "sub-c", "duna_productos": ["body"]})
        p_viejo = cuenta.resolve_person(self.conn, {"sub": "sub-d", "duna_productos": ["body"]})
        cuenta.create_session(self.conn, p_reciente, None)
        cuenta.create_session(self.conn, p_viejo, None)
        reciente = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        viejo = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE sesion SET last_seen_at = ? WHERE person_id = ?", (reciente, p_reciente))
        self.conn.execute("UPDATE sesion SET last_seen_at = ? WHERE person_id = ?", (viejo, p_viejo))
        self.conn.commit()

        out = cobertura.conteo_personas(self.conn)
        self.assertEqual(out["activos_30d"], 1)

    def test_no_hosted_persons_is_zero_and_zero(self):
        db.local_person_id(self.conn)
        out = cobertura.conteo_personas(self.conn)
        self.assertEqual(out, {"consentidos": 0, "activos_30d": 0})


class ConteoPersonasCliTest(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "conteo-cli.db"
        conn = open_conn(self, config.DB_PATH)
        db.init(conn)
        p = cuenta.resolve_person(conn, {"sub": "sub-secret-xyz", "duna_productos": ["body"]})
        conn.execute(
            "INSERT INTO consentimiento (person_id, version, accepted_at) VALUES (?, ?, ?)",
            (p, "consentimiento-v4", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

    def test_cli_conteo_prints_only_two_counts_never_a_sub(self):
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a: buf.append(" ".join(str(x) for x in a))):
            rc = cobertura.main(["--conteo"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(buf), 1)
        self.assertIn("consentidos=1", buf[0])
        self.assertIn("activos_30d=0", buf[0])
        self.assertNotIn("sub-secret-xyz", buf[0])

    def test_cli_conteo_leaves_the_database_unchanged(self):
        path = config.DB_PATH
        before_bytes = path.read_bytes()
        before_mtime = os.stat(path).st_mtime_ns
        with mock.patch("builtins.print"):
            cobertura.main(["--conteo"])
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(os.stat(path).st_mtime_ns, before_mtime)


# --------------------------------------------------------------------------------------- the CLI


class MainCliTest(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "cli.db"
        conn = open_conn(self, config.DB_PATH)
        db.init(conn)
        db.local_person_id(conn)
        conn.commit()
        conn.close()

    def test_json_flag_prints_valid_json(self):
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a: buf.append(" ".join(str(x) for x in a))):
            rc = cobertura.main(["--json"])
        self.assertEqual(rc, 0)
        parsed = json.loads(buf[0])
        self.assertIn("keeper", parsed)

    def test_text_mode_prints_lines_and_never_crashes_on_an_empty_home(self):
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a: buf.append(" ".join(str(x) for x in a))):
            rc = cobertura.main([])
        self.assertEqual(rc, 0)
        self.assertGreater(len(buf), 5)

    def test_unknown_cuenta_sub_on_the_cli_exits_1(self):
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a: buf.append(" ".join(str(x) for x in a))):
            rc = cobertura.main(["--cuenta-sub", "no-such-sub"])
        self.assertEqual(rc, 1)
        self.assertTrue(any("refused" in line for line in buf))

    def test_the_database_file_is_byte_for_byte_unchanged_after_a_run(self):
        path = config.DB_PATH
        before_bytes = path.read_bytes()
        before_mtime = os.stat(path).st_mtime_ns
        with mock.patch("builtins.print"):
            rc = cobertura.main(["--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(os.stat(path).st_mtime_ns, before_mtime)


# --------------------------------------- BODY-ESFUERZO-COBERTURA-1: detalle_actividad


class DetalleActividadCoverageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "detalle.db")
        self.person_id = db.local_person_id(self.conn)
        self.conn.execute("UPDATE person SET timezone = 'UTC' WHERE id = ?", (self.person_id,))
        self.conn.commit()
        self.hoy = fechas.hoy_local(db.person(self.conn, self.person_id))

    def _dia(self, back: int) -> str:
        return (self.hoy - timedelta(days=back)).isoformat()

    def test_real_shape_payload_parses_in_window_and_is_sufficient(self):
        day = self._dia(5)
        start_local = f"{day}T08:00:00"
        inicio_ms = _epoch_ms(start_local)
        ts_values = [inicio_ms + i * 60000 for i in range(20)]
        hr_values = [120 + (i % 5) for i in range(20)]
        _actividad(self.conn, self.person_id, "act-1", day, start_local, 1200.0)
        db.guardar_detalle_actividad(self.conn, self.person_id, garmin_adapter.SOURCE, "act-1",
                                      "detalle", _detalle_payload(ts_values, hr_values))

        out = cobertura.recopilar(self.conn)
        da = out["detalle_actividad"]
        self.assertEqual(da["actividades_en_ventana"], 1)
        self.assertEqual(da["con_fila_detalle"], 1)
        self.assertEqual(da["con_fila_vueltas"], 0)
        self.assertEqual(da["con_fila_zonas"], 0)
        self.assertEqual(da["con_muestra_detalle"], 1)
        self.assertEqual(da["claves_descriptor"], {"directTimestamp": 1, "directHeartRate": 1})
        self.assertEqual(da["tipos_index_descriptor"], {"int": 0, "other": 2})
        self.assertEqual(da["tipos_metricsindex_descriptor"], {"int": 2, "other": 0})
        self.assertEqual(da["claves_nivel_superior_detalle"],
                          {"metricDescriptors": 1, "activityDetailMetrics": 1})
        self.assertEqual(da["tipo_timestamp_crudo"], {"int": 1, "float": 0, "str": 0, "missing": 0})

        a = da["actividades"][0]
        self.assertEqual(a["orden"], 1)
        self.assertEqual(a["type"], "running")
        self.assertEqual(a["day"], day)
        self.assertEqual(a["minutos_sesion"], 20)
        self.assertEqual(a["filas_crudas"], 20)
        self.assertEqual(a["muestras_parseadas"], 20)
        self.assertEqual(a["muestras_en_ventana"], 20)
        self.assertEqual(a["minutos_distintos_en_ventana"], 20)
        self.assertEqual(a["cobertura_pct"], 100.0)
        self.assertEqual(a["desfase_min"], {"inicio": 0, "fin": -1})
        self.assertEqual(a["intervalo_mediano_s"], 60.0)
        self.assertTrue(a["suficiente"])

        dumped = json.dumps(out)
        self.assertNotIn("Secret Activity Name", dumped)
        self.assertNotIn("act-1", dumped)

    def test_unrecognised_descriptor_keys_parse_to_zero_and_name_themselves(self):
        day = self._dia(6)
        start_local = f"{day}T08:00:00"
        inicio_ms = _epoch_ms(start_local)
        ts_values = [inicio_ms + i * 60000 for i in range(5)]
        hr_values = [100] * 5
        _actividad(self.conn, self.person_id, "act-2", day, start_local, 300.0)
        db.guardar_detalle_actividad(
            self.conn, self.person_id, garmin_adapter.SOURCE, "act-2", "detalle",
            _detalle_payload(ts_values, hr_values, key_ts="ts", key_hr="hr"))

        out = cobertura.recopilar(self.conn)
        da = out["detalle_actividad"]
        self.assertEqual(da["claves_descriptor"], {"ts": 1, "hr": 1})
        self.assertEqual(da["tipo_timestamp_crudo"], {"int": 0, "float": 0, "str": 0, "missing": 1})

        a = da["actividades"][0]
        self.assertEqual(a["muestras_parseadas"], 0)
        self.assertEqual(a["muestras_en_ventana"], 0)
        self.assertEqual(a["filas_crudas"], 5)
        self.assertIsNone(a["intervalo_mediano_s"])
        self.assertFalse(a["suficiente"])
        self.assertEqual(a["desfase_min"], {"inicio": None, "fin": None})

    def test_timestamps_five_hours_off_the_start_show_the_offset_and_zero_coverage(self):
        day = self._dia(7)
        start_local = f"{day}T08:00:00"
        inicio_ms = _epoch_ms(start_local) + 5 * 3600 * 1000  # five hours after the activity's own start
        ts_values = [inicio_ms + i * 60000 for i in range(20)]
        hr_values = [130] * 20
        _actividad(self.conn, self.person_id, "act-3", day, start_local, 1200.0)
        db.guardar_detalle_actividad(self.conn, self.person_id, garmin_adapter.SOURCE, "act-3",
                                      "detalle", _detalle_payload(ts_values, hr_values))

        out = cobertura.recopilar(self.conn)
        a = out["detalle_actividad"]["actividades"][0]
        self.assertEqual(a["muestras_parseadas"], 20)
        self.assertEqual(a["muestras_en_ventana"], 0)
        self.assertEqual(a["minutos_distintos_en_ventana"], 0)
        self.assertEqual(a["desfase_min"]["inicio"], 300)  # five hours, signed
        self.assertFalse(a["suficiente"])

    def test_seconds_not_milliseconds_timestamps_parse_but_land_nowhere_near_the_window(self):
        day = self._dia(8)
        start_local = f"{day}T08:00:00"
        inicio_ms = _epoch_ms(start_local)
        # The same instants, but stored as SECONDS -- the parser (which always divides by 1000,
        # the confirmed convention for every OTHER Garmin intraday array) reads them as if they
        # were milliseconds, landing near the 1970 epoch.
        ts_values = [(inicio_ms + i * 60000) // 1000 for i in range(20)]
        hr_values = [140] * 20
        _actividad(self.conn, self.person_id, "act-4", day, start_local, 1200.0)
        db.guardar_detalle_actividad(self.conn, self.person_id, garmin_adapter.SOURCE, "act-4",
                                      "detalle", _detalle_payload(ts_values, hr_values))

        out = cobertura.recopilar(self.conn)
        da = out["detalle_actividad"]
        self.assertEqual(da["tipo_timestamp_crudo"], {"int": 1, "float": 0, "str": 0, "missing": 0})
        a = da["actividades"][0]
        self.assertEqual(a["muestras_parseadas"], 20)  # the parser still produces samples -- just wrong ones
        self.assertEqual(a["muestras_en_ventana"], 0)
        self.assertFalse(a["suficiente"])
        self.assertIsNotNone(a["desfase_min"]["inicio"])
        self.assertGreater(abs(a["desfase_min"]["inicio"]), 1000)  # years off, not minutes

    def test_existing_cobertura_blocks_are_unaffected(self):
        out = cobertura.recopilar(self.conn)
        self.assertEqual(out["detalle_actividad"]["actividades_en_ventana"], 0)
        self.assertIn("keeper", out)
        self.assertIn("sleep", out)


class DetalleActividadCliLeakTest(unittest.TestCase):
    """Ruling 4's own walk of the whole block, text and --json: neither carries the seeded
    heart-rate value, the activity's own name, or its source_id."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "detalle-cli.db"
        conn = open_conn(self, config.DB_PATH)
        db.init(conn)
        person_id = db.local_person_id(conn)
        conn.execute("UPDATE person SET timezone = 'UTC' WHERE id = ?", (person_id,))
        conn.commit()
        hoy = fechas.hoy_local(db.person(conn, person_id))
        day = (hoy - timedelta(days=9)).isoformat()
        start_local = f"{day}T08:00:00"
        inicio_ms = _epoch_ms(start_local)
        ts_values = [inicio_ms + i * 60000 for i in range(15)]
        self.sentinel_hr = 173
        hr_values = [self.sentinel_hr] * 15
        _actividad(conn, person_id, "SECRET-SOURCE-ID-9", day, start_local, 900.0,
                   name="Secret Activity Name Nine")
        db.guardar_detalle_actividad(conn, person_id, garmin_adapter.SOURCE, "SECRET-SOURCE-ID-9",
                                      "detalle", _detalle_payload(ts_values, hr_values))
        conn.close()

    def test_json_mode_never_leaks_a_name_source_id_or_heart_rate_value(self):
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a: buf.append(" ".join(str(x) for x in a))):
            rc = cobertura.main(["--json"])
        self.assertEqual(rc, 0)
        self.assertNotIn(str(self.sentinel_hr), buf[0])
        self.assertNotIn("SECRET-SOURCE-ID-9", buf[0])
        self.assertNotIn("Secret Activity Name Nine", buf[0])
        parsed = json.loads(buf[0])
        self.assertEqual(parsed["detalle_actividad"]["actividades_en_ventana"], 1)

    def test_text_mode_never_leaks_a_name_source_id_or_heart_rate_value(self):
        buf = []
        with mock.patch("builtins.print", side_effect=lambda *a: buf.append(" ".join(str(x) for x in a))):
            rc = cobertura.main([])
        self.assertEqual(rc, 0)
        texto = "\n".join(buf)
        self.assertNotIn(str(self.sentinel_hr), texto)
        self.assertNotIn("SECRET-SOURCE-ID-9", texto)
        self.assertNotIn("Secret Activity Name Nine", texto)
        self.assertIn("detalle_actividad: actividades_en_ventana=1", texto)


if __name__ == "__main__":
    unittest.main()
