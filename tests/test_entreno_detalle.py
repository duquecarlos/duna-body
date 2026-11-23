"""BODY-ENTRENO-DETALLE-1: each activity's full heart-rate curve, laps and heart-rate-zone
breakdown (body/garmin_adapter.py's fetch_activity_detail and its three parsers, body/db.py's
activity_detail table), plus the night's own per-minute arrays parsed on read from the already-
stored sleep payload (parse_sleep_series, db.sueno_minuto_a_minuto). No network: every Garmin
call goes through body.pull.garmin_adapter / body.garmin_adapter, stubbed, and every payload
below is invented demo data shaped from the installed garminconnect package's own source
(.venv/Lib/site-packages/garminconnect/), never real health figures.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


def _insert_activity(conn, person_id, source_id, day, source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, 'running', NULL, ?)",
        (person_id, source_id, day, f"{day}T07:00:00", source),
    )


# ---------------------------------------------------------------------------------- db storage


class ActivityDetailStorageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "detalle.db")
        db.init(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-11-10")
        self.conn.commit()

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "no-such-kind", {})

    def test_not_stored_until_all_three_kinds_are_present(self):
        self.assertFalse(db.detalle_actividad_almacenado(self.conn, 1, "garmin", "a1"))
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "detalle", {"x": 1})
        self.assertFalse(db.detalle_actividad_almacenado(self.conn, 1, "garmin", "a1"))
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "vueltas", {"x": 1})
        self.assertFalse(db.detalle_actividad_almacenado(self.conn, 1, "garmin", "a1"))
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "zonas", [])
        self.assertTrue(db.detalle_actividad_almacenado(self.conn, 1, "garmin", "a1"))

    def test_an_explicit_empty_marker_counts_as_stored(self):
        for kind in db.DETALLE_KINDS:
            db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", kind, None)
        self.assertTrue(db.detalle_actividad_almacenado(self.conn, 1, "garmin", "a1"))

    def test_insert_or_replace_overwrites_the_same_kind(self):
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "detalle", {"v": 1})
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "detalle", {"v": 2})
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM activity_detail WHERE person_id = 1 AND "
            "activity_source_id = 'a1' AND kind = 'detalle'"
        ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_detalle_actividad_reads_through_the_adapters_own_parser(self):
        payload = {
            "metricDescriptors": [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directHeartRate", "metricsIndex": 1},
            ],
            "activityDetailMetrics": [
                {"metrics": [1762765200000, 120]},  # 2025-11-10T06:00:00Z
            ],
        }
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "detalle", payload)
        series = db.detalle_actividad(self.conn, 1, "garmin", "a1", "detalle")
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0][1], 120)

    def test_detalle_actividad_unknown_activity_is_the_empty_tuple(self):
        self.assertEqual(db.detalle_actividad(self.conn, 1, "garmin", "nunca", "detalle"), ())

    def test_detalle_actividad_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            db.detalle_actividad(self.conn, 1, "garmin", "a1", "no-such-kind")


class ActividadesSinDetalleTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "pendientes.db")
        db.init(self.conn)
        self.conn.commit()

    def test_newest_day_first(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-01")
        _insert_activity(self.conn, 1, "a2", "2026-11-05")
        _insert_activity(self.conn, 1, "a3", "2026-11-03")
        self.conn.commit()
        self.assertEqual(
            db.actividades_sin_detalle(self.conn, 1, "2026-11-01"), ["a2", "a3", "a1"]
        )

    def test_a_fully_stored_activity_is_left_out(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-01")
        self.conn.commit()
        for kind in db.DETALLE_KINDS:
            db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", kind, {})
        self.assertEqual(db.actividades_sin_detalle(self.conn, 1, "2026-11-01"), [])

    def test_a_partially_stored_activity_is_still_pending(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-01")
        self.conn.commit()
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "detalle", {})
        self.assertEqual(db.actividades_sin_detalle(self.conn, 1, "2026-11-01"), ["a1"])

    def test_days_before_desde_are_excluded(self):
        _insert_activity(self.conn, 1, "a1", "2026-10-01")
        self.conn.commit()
        self.assertEqual(db.actividades_sin_detalle(self.conn, 1, "2026-11-01"), [])

    def test_a_manual_session_is_never_listed(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-01", source="manual")
        self.conn.commit()
        self.assertEqual(db.actividades_sin_detalle(self.conn, 1, "2026-11-01"), [])

    def test_person_scoped(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        _insert_activity(self.conn, 1, "a1", "2026-11-01")
        _insert_activity(self.conn, 2, "b1", "2026-11-02")
        self.conn.commit()
        self.assertEqual(db.actividades_sin_detalle(self.conn, 1, "2026-11-01"), ["a1"])


# ---------------------------------------------------------------------------------- privacidad


class PrivacidadCoverageIncludesActivityDetailTest(unittest.TestCase):
    def test_activity_detail_is_fully_handled(self):
        from body import privacidad
        self.assertIn("activity_detail", privacidad.FULLY_HANDLED_TABLES)


# ---------------------------------------------------------------------------------- adapter: fetch


class FetchActivityDetailTest(unittest.TestCase):
    def test_one_call_each_and_the_right_kind_keys(self):
        calls = []

        class FakeApi:
            def get_activity_details(self, activity_id):
                calls.append(("detalle", activity_id))
                return {"metricDescriptors": [], "activityDetailMetrics": []}

            def get_activity_splits(self, activity_id):
                calls.append(("vueltas", activity_id))
                return {"lapDTOs": []}

            def get_activity_hr_in_timezones(self, activity_id):
                calls.append(("zonas", activity_id))
                return []

            class client:
                @staticmethod
                def dumps():
                    return "tok-after"

        with mock.patch("body.garmin_adapter._client", return_value=FakeApi()):
            data = garmin_adapter.fetch_activity_detail("a1")
        self.assertEqual(sorted(calls), [("detalle", "a1"), ("vueltas", "a1"), ("zonas", "a1")])
        self.assertEqual(set(data.raw.keys()), {"detalle", "vueltas", "zonas"})
        self.assertNotIn("_errors", data.raw)

    def test_one_failing_call_does_not_lose_the_other_two(self):
        class FakeApi:
            def get_activity_details(self, activity_id):
                raise RuntimeError("boom")

            def get_activity_splits(self, activity_id):
                return {"lapDTOs": []}

            def get_activity_hr_in_timezones(self, activity_id):
                return []

            class client:
                @staticmethod
                def dumps():
                    return "tok-after"

        with mock.patch("body.garmin_adapter._client", return_value=FakeApi()):
            data = garmin_adapter.fetch_activity_detail("a1")
        self.assertEqual(set(data.raw.keys()), {"vueltas", "zonas", "_errors"})
        self.assertEqual(data.raw["_errors"], {"detalle": "RuntimeError"})

    def test_hosted_token_path_returns_the_refreshed_token(self):
        class FakeApi:
            def get_activity_details(self, activity_id):
                return {}

            def get_activity_splits(self, activity_id):
                return {}

            def get_activity_hr_in_timezones(self, activity_id):
                return []

            class client:
                @staticmethod
                def dumps():
                    return "tok-after"

        with mock.patch("body.garmin_adapter._client", return_value=FakeApi()) as m_client:
            data, token_after = garmin_adapter.fetch_activity_detail("a1", token_str="tok-before")
        m_client.assert_called_once_with("tok-before")
        self.assertEqual(token_after, "tok-after")
        self.assertEqual(data.activity_id, "a1")


# ---------------------------------------------------------------------------------- adapter: parsers


class ParseActivityHrSeriesTest(unittest.TestCase):
    def test_finds_columns_by_key_not_position(self):
        payload = {
            "metricDescriptors": [
                {"key": "directHeartRate", "metricsIndex": 0},  # deliberately swapped order
                {"key": "directTimestamp", "metricsIndex": 1},
            ],
            "activityDetailMetrics": [
                {"metrics": [118, 1762765200000]},
                {"metrics": [121, 1762765260000]},
            ],
        }
        series = garmin_adapter.parse_activity_hr_series(payload, "UTC")
        self.assertEqual([v for _, v in series], [118, 121])

    def test_non_positive_heart_rate_is_dropped(self):
        payload = {
            "metricDescriptors": [
                {"key": "directTimestamp", "metricsIndex": 0},
                {"key": "directHeartRate", "metricsIndex": 1},
            ],
            "activityDetailMetrics": [{"metrics": [1762765200000, 0]}],
        }
        self.assertEqual(garmin_adapter.parse_activity_hr_series(payload, "UTC"), ())

    def test_no_matching_descriptor_is_the_empty_tuple_never_a_guessed_position(self):
        payload = {
            "metricDescriptors": [{"key": "somethingElse", "metricsIndex": 0}],
            "activityDetailMetrics": [{"metrics": [1762765200000, 120]}],
        }
        self.assertEqual(garmin_adapter.parse_activity_hr_series(payload, "UTC"), ())

    def test_malformed_payload_is_the_empty_tuple_never_a_crash(self):
        for bad in (None, "x", 5, {}, {"metricDescriptors": "x"}):
            self.assertEqual(garmin_adapter.parse_activity_hr_series(bad, "UTC"), ())


class ParseLapsTest(unittest.TestCase):
    def test_parses_each_assumed_field(self):
        payload = {"lapDTOs": [
            {"lapIndex": 1, "startTimeGMT": "2026-11-10 06:00:00.0", "duration": 300.0,
             "averageHR": 130, "maxHR": 145},
        ]}
        laps = garmin_adapter.parse_laps(payload, "America/Bogota")
        self.assertEqual(len(laps), 1)
        lap = laps[0]
        self.assertEqual((lap.index, lap.duration_s, lap.avg_hr, lap.max_hr), (1, 300.0, 130, 145))
        self.assertIsNotNone(lap.start_local)

    def test_missing_fields_are_none_never_guessed(self):
        payload = {"lapDTOs": [{}]}
        laps = garmin_adapter.parse_laps(payload, "UTC")
        self.assertEqual(len(laps), 1)
        lap = laps[0]
        self.assertIsNone(lap.index)
        self.assertIsNone(lap.start_local)
        self.assertIsNone(lap.duration_s)
        self.assertIsNone(lap.avg_hr)
        self.assertIsNone(lap.max_hr)

    def test_malformed_payload_is_the_empty_tuple_never_a_crash(self):
        for bad in (None, "x", 5, {}, {"lapDTOs": "x"}, {"lapDTOs": [None, 5]}):
            self.assertEqual(garmin_adapter.parse_laps(bad, "UTC"), ())


class ParseHrZonesTest(unittest.TestCase):
    def test_parses_a_bare_list(self):
        payload = [
            {"zoneNumber": 1, "secsInZone": 120.0, "zoneLowBoundary": 91},
            {"zoneNumber": 2, "secsInZone": 60.0, "zoneLowBoundary": 136},
        ]
        zones = garmin_adapter.parse_hr_zones(payload)
        self.assertEqual(zones, ((1, 120.0, 91.0), (2, 60.0, 136.0)))

    def test_accepts_a_wrapped_list(self):
        payload = {"hrTimeInZones": [{"zoneNumber": 1, "secsInZone": 10.0, "zoneLowBoundary": 90}]}
        self.assertEqual(garmin_adapter.parse_hr_zones(payload), ((1, 10.0, 90.0),))

    def test_a_row_missing_a_field_is_dropped(self):
        payload = [{"zoneNumber": 1, "secsInZone": 10.0}]  # no zoneLowBoundary
        self.assertEqual(garmin_adapter.parse_hr_zones(payload), ())

    def test_malformed_payload_is_the_empty_tuple_never_a_crash(self):
        for bad in (None, "x", 5, {}, {"hrTimeInZones": "x"}):
            self.assertEqual(garmin_adapter.parse_hr_zones(bad), ())


# ---------------------------------------------------------------------------------- ruling 6: sleep


class ParseSleepSeriesTest(unittest.TestCase):
    def test_parses_every_present_array(self):
        payload = {
            "sleepHeartRate": [{"startGMT": "2026-11-10 02:00:00.0", "value": 55}],
            "hrvData": [{"startGMT": "2026-11-10 02:05:00.0", "value": 42.0}],
            "sleepLevels": [{"startGMT": "2026-11-10 02:00:00.0", "activityLevel": 0.0}],
            "sleepMovement": [{"startGMT": "2026-11-10 02:00:00.0", "activityLevel": 1.2}],
            "wellnessEpochSPO2DataDTOList": [{"epochTimestamp": "2026-11-10 02:00:00.0", "spo2Reading": 96}],
        }
        out = garmin_adapter.parse_sleep_series(payload, "America/Bogota")
        self.assertEqual(set(out.keys()), {"heart_rate", "hrv", "stages", "movement", "spo2"})
        self.assertEqual(out["heart_rate"][0][1], 55)
        self.assertEqual(out["spo2"][0][1], 96)

    def test_an_absent_array_is_left_out_of_the_result(self):
        out = garmin_adapter.parse_sleep_series({"sleepHeartRate": [{"startGMT": "2026-11-10 02:00:00.0", "value": 55}]},
                                                  "UTC")
        self.assertEqual(set(out.keys()), {"heart_rate"})

    def test_malformed_payload_is_the_empty_dict_never_a_crash(self):
        for bad in (None, "x", 5, []):
            self.assertEqual(garmin_adapter.parse_sleep_series(bad, "UTC"), {})


class SuenoMinutoAMinutoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "sueno.db")
        db.init(self.conn)
        self.conn.commit()

    def test_reads_through_the_stored_sleep_payload(self):
        payload = {"sleepHeartRate": [{"startGMT": "2026-11-10 02:00:00.0", "value": 50}]}
        db.guardar_intradia(self.conn, 1, "2026-11-10", "intradia_fc", {})  # unrelated row, same table
        self.conn.execute(
            "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
            "VALUES (1, '2026-11-10', 'sleep', 'garmin', '2026-11-10T08:00:00+00:00', ?)",
            (json.dumps(payload),),
        )
        self.conn.commit()
        out = db.sueno_minuto_a_minuto(self.conn, 1, "2026-11-10", "America/Bogota")
        self.assertEqual(set(out.keys()), {"heart_rate"})

    def test_a_day_never_stored_is_the_empty_dict(self):
        self.assertEqual(db.sueno_minuto_a_minuto(self.conn, 1, "2026-11-10", "UTC"), {})


if __name__ == "__main__":
    unittest.main()
