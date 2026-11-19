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


if __name__ == "__main__":
    unittest.main()
