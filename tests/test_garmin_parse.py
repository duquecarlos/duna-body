"""BODY-VO2MAX-1: parsing of Garmin's max-metrics feed (VO2 max estimate, fitness age).
BODY-INTRADIA-ADAPTER-1: parsing of Garmin's intraday feeds (heart rate, stress, Body Battery
events) for fetch_intraday/parse_intraday.

The installed garminconnect 0.3.20 package names no confirmed field shape for get_max_metrics,
get_heart_rates, get_stress_data or get_body_battery_events beyond their dict[str, Any] /
list[dict[str, Any]] type hints, so parsing is defensive throughout. Fixtures below are invented
shapes only, never real health figures.
"""
import pathlib
import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, garmin_adapter

LIST_RAW = [{
    "generic": {"vo2MaxPreciseValue": 47.3, "vo2MaxValue": 47},
    "cycling": {"vo2MaxPreciseValue": 45.8, "vo2MaxValue": 46},
    "fitnessAge": 33,
}]

DICT_RAW = {
    "generic": {"vo2MaxValue": 48, "fitnessAge": 31},
    "cycling": {"vo2MaxValue": 44},
}


class ShapeParsingTest(unittest.TestCase):
    def test_list_shaped_response_parses(self):
        d = garmin_adapter.parse_day("2026-11-05", {"max_metrics": LIST_RAW})
        self.assertEqual(d.metrics["vo2max"], 47.3)
        self.assertEqual(d.metrics["vo2max_cycling"], 45.8)
        self.assertEqual(d.metrics["fitness_age"], 33)

    def test_dict_shaped_response_parses(self):
        d = garmin_adapter.parse_day("2026-11-05", {"max_metrics": DICT_RAW})
        self.assertEqual(d.metrics["vo2max"], 48)
        self.assertEqual(d.metrics["vo2max_cycling"], 44)
        self.assertEqual(d.metrics["fitness_age"], 31)

    def test_precise_value_preferred_over_rounded(self):
        d = garmin_adapter.parse_day("2026-11-05", {"max_metrics": LIST_RAW})
        self.assertEqual(d.metrics["vo2max"], 47.3)
        self.assertNotEqual(d.metrics["vo2max"], 47)

    def test_missing_max_metrics_key_is_none(self):
        d = garmin_adapter.parse_day("2026-11-05", {})
        self.assertNotIn("vo2max", d.metrics)
        self.assertNotIn("vo2max_cycling", d.metrics)
        self.assertNotIn("fitness_age", d.metrics)

    def test_empty_list_and_unrecognised_shapes_are_none(self):
        for raw in ([], "unexpected-shape", None, {}):
            d = garmin_adapter.parse_day("2026-11-05", {"max_metrics": raw})
            self.assertNotIn("vo2max", d.metrics)
            self.assertNotIn("vo2max_cycling", d.metrics)
            self.assertNotIn("fitness_age", d.metrics)


SLEEP_RESPIRATION_RAW = {
    "dailySleepDTO": {
        "sleepTimeSeconds": 25200,
        "avgSpO2": 95.5,
        "averageRespirationValue": 14.2,
        "lowestRespirationValue": 10.0,
        "highestRespirationValue": 19.0,
    },
}


class SleepRespirationParsingTest(unittest.TestCase):
    def test_present_values_parse(self):
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": SLEEP_RESPIRATION_RAW})
        self.assertEqual(d.sleep_spo2_avg, 95.5)
        self.assertEqual(d.sleep_respiration_avg, 14.2)
        self.assertEqual(d.sleep_respiration_low, 10.0)
        self.assertEqual(d.sleep_respiration_high, 19.0)
        self.assertEqual(d.metrics["sleep_minutes"], 420)  # the rest of the day still parses

    def test_missing_sleep_key_stays_absent(self):
        d = garmin_adapter.parse_day("2026-11-05", {})
        self.assertIsNone(d.sleep_spo2_avg)
        self.assertIsNone(d.sleep_respiration_avg)
        self.assertIsNone(d.sleep_respiration_low)
        self.assertIsNone(d.sleep_respiration_high)

    def test_null_values_stay_absent(self):
        raw = {"dailySleepDTO": {
            "avgSpO2": None, "averageRespirationValue": None,
            "lowestRespirationValue": None, "highestRespirationValue": None,
        }}
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": raw})
        self.assertIsNone(d.sleep_spo2_avg)
        self.assertIsNone(d.sleep_respiration_avg)
        self.assertIsNone(d.sleep_respiration_low)
        self.assertIsNone(d.sleep_respiration_high)

    def test_zero_stays_absent_not_a_reading(self):
        raw = {"dailySleepDTO": {
            "avgSpO2": 0, "averageRespirationValue": 0,
            "lowestRespirationValue": 0, "highestRespirationValue": 0,
        }}
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": raw})
        self.assertIsNone(d.sleep_spo2_avg)
        self.assertIsNone(d.sleep_respiration_avg)
        self.assertIsNone(d.sleep_respiration_low)
        self.assertIsNone(d.sleep_respiration_high)

    def test_out_of_range_values_stay_absent(self):
        raw = {"dailySleepDTO": {
            "avgSpO2": 49,  # below the 50..100 SpO2 window
            "averageRespirationValue": 61,  # above the 1..60 breaths/min window
            "lowestRespirationValue": -3,
            "highestRespirationValue": 200,
        }}
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": raw})
        self.assertIsNone(d.sleep_spo2_avg)
        self.assertIsNone(d.sleep_respiration_avg)
        self.assertIsNone(d.sleep_respiration_low)
        self.assertIsNone(d.sleep_respiration_high)

    def test_in_range_boundaries_parse(self):
        raw = {"dailySleepDTO": {
            "avgSpO2": 50, "averageRespirationValue": 1,
            "lowestRespirationValue": 60, "highestRespirationValue": 100,
        }}
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": raw})
        self.assertEqual(d.sleep_spo2_avg, 50)
        self.assertEqual(d.sleep_respiration_avg, 1)
        self.assertEqual(d.sleep_respiration_low, 60)
        self.assertIsNone(d.sleep_respiration_high)  # 100 is outside the 1..60 breaths/min window

    def test_non_numeric_stays_absent(self):
        raw = {"dailySleepDTO": {"avgSpO2": "n/a", "averageRespirationValue": "unknown"}}
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": raw})
        self.assertIsNone(d.sleep_spo2_avg)
        self.assertIsNone(d.sleep_respiration_avg)

    def test_older_payload_without_the_keys_still_parses_the_rest_of_the_day(self):
        raw = {"dailySleepDTO": {"sleepTimeSeconds": 25200, "sleepScores": {"overall": {"value": 80}}}}
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": raw, "user_summary": {"restingHeartRate": 55}})
        self.assertIsNone(d.sleep_spo2_avg)
        self.assertIsNone(d.sleep_respiration_avg)
        self.assertIsNone(d.sleep_respiration_low)
        self.assertIsNone(d.sleep_respiration_high)
        self.assertEqual(d.metrics["sleep_minutes"], 420)
        self.assertEqual(d.metrics["sleep_score"], 80)
        self.assertEqual(d.metrics["resting_hr"], 55)

    def test_not_stored_in_metrics_dict_yet(self):
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": SLEEP_RESPIRATION_RAW})
        for key in ("sleep_spo2_avg", "sleep_respiration_avg", "sleep_respiration_low", "sleep_respiration_high"):
            self.assertNotIn(key, d.metrics)


class FakeGarminMaxMetricsFails:
    """Replaces garminconnect.Garmin: every call but get_max_metrics succeeds. Invented data only."""

    def __init__(self, **kw):
        self.client = mock.Mock()
        self.client.dumps.return_value = "tok-demo"

    def login(self, tokenstore=None):
        return None, None

    def get_user_summary(self, day):
        return {"restingHeartRate": 55}

    def get_sleep_data(self, day):
        return {}

    def get_hrv_data(self, day):
        return {}

    def get_body_battery(self, day):
        return []

    def get_activities_by_date(self, start, end):
        return []

    def get_max_metrics(self, day):
        raise RuntimeError("demo max-metrics failure")


class FetchDayMaxMetricsFailureTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("garminconnect.Garmin", FakeGarminMaxMetricsFails)
        patcher.start()
        self.addCleanup(patcher.stop)
        config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_file = config.TOKEN_DIR / "garmin_tokens.json"
        token_file.write_text('{"demo": true}', encoding="utf-8")
        self.addCleanup(token_file.unlink, missing_ok=True)  # TOKEN_DIR is shared across this test run

    def test_other_metrics_survive_and_the_error_class_is_recorded(self):
        data = garmin_adapter.fetch_day("2026-11-05")
        self.assertEqual(data.metrics["resting_hr"], 55)
        self.assertNotIn("vo2max", data.metrics)
        self.assertEqual(data.raw["_errors"]["max_metrics"], "RuntimeError")


HEART_RATE_RAW = {
    "userProfilePK": 1,
    "calendarDate": "2026-11-05",
    "heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
    "heartRateValues": [
        [1762300800000, 58],
        [1762300920000, None],  # a null sample -- must be dropped, not kept as 0
        [1762301040000, 61],
    ],
}

STRESS_RAW = {
    "calendarDate": "2026-11-05",
    "maxStressLevel": 72,
    "avgStressLevel": 24,
    "stressValuesArray": [
        [1762300800000, 18],
        [1762300980000, -1],  # Garmin's "no data" sentinel value, not a real reading
        [1762301160000, 30],
    ],
}

STRESS_WITH_STATED_INTERVAL_RAW = {
    "calendarDate": "2026-11-05",
    "stressValueIntervalMinutes": 3,
    "stressValuesArray": [[1762300800000, 20]],
}

BODY_BATTERY_EVENTS_RAW = [
    {"eventType": "sleep", "eventStartTimeGmt": "2026-11-04T23:00:00.0", "durationInMs": 7200000,
     "bodyBatteryImpact": 12},
    {"eventType": "activity", "eventStartTimeGmt": "2026-11-05T06:00:00.0", "durationInMs": 1800000,
     "bodyBatteryImpact": -5},
]


def _expect_local(ms, zone="America/Bogota"):
    """Independently computed (not delegating to garmin_adapter._utc_to_local_iso): the sample's
    timestamp is a UTC instant, converted to `zone`'s own wall time."""
    return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .astimezone(ZoneInfo(zone)).replace(tzinfo=None).isoformat(timespec="minutes"))


class IntradayFullDayTest(unittest.TestCase):
    def test_full_day_parses_in_order(self):
        raw = {
            "heart_rate": HEART_RATE_RAW,
            "stress": STRESS_RAW,
            "body_battery_events": BODY_BATTERY_EVENTS_RAW,
        }
        d = garmin_adapter.parse_intraday("2026-11-05", raw, "America/Bogota")
        self.assertEqual(d.day, "2026-11-05")
        self.assertEqual(d.heart_rate, (
            (_expect_local(1762300800000), 58.0),
            (_expect_local(1762301040000), 61.0),
        ))
        # the -1 sample is Garmin's "no reading" sentinel -- dropped, not kept as a real level
        self.assertEqual(d.stress, (
            (_expect_local(1762300800000), 18.0),
            (_expect_local(1762301160000), 30.0),
        ))
        self.assertEqual(d.body_battery_events, tuple(BODY_BATTERY_EVENTS_RAW))
        self.assertEqual(d.heart_rate_interval_minutes, 2.0)  # derived: no stated key in this fixture
        self.assertEqual(d.stress_interval_minutes, 3.0)
        self.assertIs(d.raw, raw)


class IntradayIntervalTest(unittest.TestCase):
    def test_interval_minutes_read_when_stated(self):
        d = garmin_adapter.parse_intraday(
            "2026-11-05", {"stress": STRESS_WITH_STATED_INTERVAL_RAW}, "America/Bogota")
        self.assertEqual(d.stress_interval_minutes, 3)

    def test_interval_minutes_derived_when_not_stated(self):
        d = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": HEART_RATE_RAW}, "America/Bogota")
        self.assertEqual(d.heart_rate_interval_minutes, 2.0)  # 1762300920000 - 1762300800000 = 120000ms

    def test_interval_minutes_none_with_fewer_than_two_samples(self):
        raw = {"heartRateValues": [[1762300800000, 58]]}
        d = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": raw}, "America/Bogota")
        self.assertIsNone(d.heart_rate_interval_minutes)


class IntradayTimezoneTest(unittest.TestCase):
    """BODY-INTRADIA-SPOTCHECK-1 (2026-11-09): the timestamp is a UTC instant (the first sample
    equals the day's own startTimestampGMT), not already local -- checked against a zone 5h
    behind UTC and one ahead."""

    def test_zone_five_hours_behind_utc(self):
        d = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": HEART_RATE_RAW}, "America/Bogota")
        self.assertEqual(d.heart_rate[0][0], _expect_local(1762300800000, "America/Bogota"))

    def test_zone_ahead_of_utc(self):
        d = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": HEART_RATE_RAW}, "Europe/Madrid")
        self.assertEqual(d.heart_rate[0][0], _expect_local(1762300800000, "Europe/Madrid"))

    def test_different_zones_land_on_different_local_minutes(self):
        behind = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": HEART_RATE_RAW}, "America/Bogota")
        ahead = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": HEART_RATE_RAW}, "Europe/Madrid")
        self.assertNotEqual(behind.heart_rate[0][0], ahead.heart_rate[0][0])


HEART_RATE_SWAPPED_COLUMNS_RAW = {
    # the value column is index 0 and the timestamp column is index 1 -- the REVERSE of the usual
    # order -- so a parser reading by fixed position rather than by descriptor key would get this
    # wrong
    "heartRateValueDescriptors": [{"key": "heartrate", "index": 0}, {"key": "timestamp", "index": 1}],
    "heartRateValues": [[58, 1762300800000]],
}


class IntradayDescriptorOrderTest(unittest.TestCase):
    def test_descriptors_in_a_different_order_still_map_correctly(self):
        d = garmin_adapter.parse_intraday(
            "2026-11-05", {"heart_rate": HEART_RATE_SWAPPED_COLUMNS_RAW}, "America/Bogota")
        self.assertEqual(d.heart_rate, ((_expect_local(1762300800000), 58.0),))

    def test_stress_falls_back_to_positions_0_1_when_descriptors_absent(self):
        # STRESS_RAW carries no stressValueDescriptorsDTOList key at all
        d = garmin_adapter.parse_intraday("2026-11-05", {"stress": STRESS_RAW}, "America/Bogota")
        self.assertEqual(d.stress[0], (_expect_local(1762300800000), 18.0))


class IntradaySentinelDropTest(unittest.TestCase):
    def test_stress_minus_one_and_minus_two_dropped(self):
        self.assertEqual(garmin_adapter.GARMIN_STRESS_NO_READING_CODES, (-1, -2))
        raw = {"stressValuesArray": [[1762300800000, -1], [1762300980000, -2], [1762301160000, 45]]}
        d = garmin_adapter.parse_intraday("2026-11-05", {"stress": raw}, "America/Bogota")
        self.assertEqual(len(d.stress), 1)
        self.assertEqual(d.stress[0][1], 45.0)

    def test_stress_zero_is_a_real_reading_kept(self):
        raw = {"stressValuesArray": [[1762300800000, 0]]}
        d = garmin_adapter.parse_intraday("2026-11-05", {"stress": raw}, "America/Bogota")
        self.assertEqual(d.stress, ((_expect_local(1762300800000), 0.0),))

    def test_heart_rate_zero_or_negative_dropped_not_a_reading(self):
        # a living person's pulse is never 0 -- treated the same honest-gap way as a negative
        # stress sentinel, never kept as a real bpm value
        raw = {"heartRateValues": [[1762300800000, 0], [1762300920000, -5], [1762301040000, 60]]}
        d = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": raw}, "America/Bogota")
        self.assertEqual(len(d.heart_rate), 1)
        self.assertEqual(d.heart_rate[0][1], 60.0)


class IntradayHonestGapsTest(unittest.TestCase):
    def test_missing_series_are_absent_not_zero(self):
        d = garmin_adapter.parse_intraday("2026-11-05", {}, "America/Bogota")
        self.assertEqual(d.heart_rate, ())
        self.assertEqual(d.stress, ())
        self.assertEqual(d.body_battery_events, ())

    def test_empty_and_null_series_stay_absent(self):
        for raw in ({"heartRateValues": []}, {"heartRateValues": None}, {}, "unexpected-shape", None):
            d = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": raw}, "America/Bogota")
            self.assertEqual(d.heart_rate, ())
        for raw in ({"stressValuesArray": []}, {"stressValuesArray": None}, {}, None):
            d = garmin_adapter.parse_intraday("2026-11-05", {"stress": raw}, "America/Bogota")
            self.assertEqual(d.stress, ())
        for raw in ([], None, "unexpected-shape"):
            d = garmin_adapter.parse_intraday("2026-11-05", {"body_battery_events": raw}, "America/Bogota")
            self.assertEqual(d.body_battery_events, ())

    def test_null_samples_are_dropped_not_zero(self):
        d = garmin_adapter.parse_intraday("2026-11-05", {"heart_rate": HEART_RATE_RAW}, "America/Bogota")
        self.assertEqual(len(d.heart_rate), 2)  # the 3rd sample is null -> dropped, not a 0.0
        for _, value in d.heart_rate:
            self.assertNotEqual(value, 0)


class FakeGarminStressFails:
    """Replaces garminconnect.Garmin: heart rate and Body Battery events succeed, stress raises.
    Invented data only."""

    def __init__(self, **kw):
        self.client = mock.Mock()
        self.client.dumps.return_value = "tok-demo"

    def login(self, tokenstore=None):
        return None, None

    def get_heart_rates(self, cdate):
        return HEART_RATE_RAW

    def get_stress_data(self, cdate):
        raise RuntimeError("demo stress failure")

    def get_body_battery_events(self, cdate):
        return BODY_BATTERY_EVENTS_RAW


class FetchIntradayStressFailureTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("garminconnect.Garmin", FakeGarminStressFails)
        patcher.start()
        self.addCleanup(patcher.stop)
        config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_file = config.TOKEN_DIR / "garmin_tokens.json"
        token_file.write_text('{"demo": true}', encoding="utf-8")
        self.addCleanup(token_file.unlink, missing_ok=True)  # TOKEN_DIR is shared across this test run

    def test_other_two_series_survive_and_the_missing_kind_is_named(self):
        data = garmin_adapter.fetch_intraday("2026-11-05", "America/Bogota")
        self.assertEqual(len(data.heart_rate), 2)
        self.assertEqual(data.body_battery_events, tuple(BODY_BATTERY_EVENTS_RAW))
        self.assertEqual(data.stress, ())
        self.assertEqual(data.raw["_errors"], {"stress": "RuntimeError"})


class OnlyGarminAdapterImportsGarminconnectTest(unittest.TestCase):
    """No guard for this exists elsewhere in tests/ (checked by grep before writing this one)."""

    def test_no_other_body_module_imports_garminconnect(self):
        body_dir = pathlib.Path(__file__).resolve().parent.parent / "body"
        offenders = []
        for path in body_dir.rglob("*.py"):
            if path.name == "garmin_adapter.py":
                continue
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("import garminconnect") or stripped.startswith("from garminconnect"):
                    offenders.append(str(path.relative_to(body_dir.parent)))
        self.assertEqual(offenders, [])


EXERCISE_SETS_RAW = {
    "exerciseSets": [
        {
            "setType": "ACTIVE",
            "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"}],
            "reps": 8,
            "weight": 60000,  # grams, almost certainly -- but the unit itself is unconfirmed
            "duration": 42,
            "startTime": 1762300800000,
        },
        {
            "setType": "REST",
            "duration": 90,
            "startTime": 1762300900000,
        },
        {
            "setType": "ACTIVE",
            "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"}],
            "reps": 6,
            "duration": 38,
            "startTime": 1762301000000,
            # no "weight" key at all -- a bodyweight-style set
        },
    ]
}


class ExerciseSetsParsingTest(unittest.TestCase):
    def test_strength_session_active_and_rest_sets_parse_in_order(self):
        sets = garmin_adapter.parse_exercise_sets(EXERCISE_SETS_RAW)
        self.assertEqual(len(sets), 3)

        active1, rest, active2 = sets
        self.assertEqual(active1.set_type, "active")
        self.assertEqual(active1.category, "BENCH_PRESS")
        self.assertEqual(active1.exercise, "BARBELL_BENCH_PRESS")
        self.assertEqual(active1.display, "Barbell Bench Press")  # read from exercises.py, not renamed
        self.assertEqual(active1.reps, 8)
        self.assertEqual(active1.duration_s, 42)
        self.assertIsNotNone(active1.start_local)

        self.assertEqual(rest.set_type, "rest")
        self.assertIsNone(rest.category)
        self.assertIsNone(rest.exercise)
        self.assertIsNone(rest.reps)
        self.assertEqual(rest.duration_s, 90)

        self.assertEqual(active2.set_type, "active")
        self.assertEqual(active2.reps, 6)

    def test_set_without_weight_keeps_weight_and_unit_absent(self):
        sets = garmin_adapter.parse_exercise_sets(EXERCISE_SETS_RAW)
        for s in (sets[1], sets[2]):  # rest set, and the bodyweight-style active set
            self.assertIsNone(s.weight_value)
            self.assertIsNone(s.weight_unit)

    def test_weight_value_kept_when_unit_is_not_stated(self):
        sets = garmin_adapter.parse_exercise_sets(EXERCISE_SETS_RAW)
        active1 = sets[0]
        self.assertEqual(active1.weight_value, 60000)
        self.assertIsNone(active1.weight_unit)  # kept, never guessed as grams or kilograms

    def test_weight_unit_passed_through_when_the_payload_states_one(self):
        payload = {"exerciseSets": [{"setType": "ACTIVE", "weight": 60, "weightUnit": "KILOGRAM"}]}
        sets = garmin_adapter.parse_exercise_sets(payload)
        self.assertEqual(sets[0].weight_value, 60)
        self.assertEqual(sets[0].weight_unit, "KILOGRAM")

    def test_unknown_keys_and_empty_payload_give_empty_result_without_raising(self):
        for payload in (None, {}, [], "unexpected-shape", {"exerciseSets": None}, {"exerciseSets": "nope"}):
            self.assertEqual(garmin_adapter.parse_exercise_sets(payload), ())

        sets = garmin_adapter.parse_exercise_sets({"exerciseSets": [{"mystery": 1}, "not-a-dict"]})
        self.assertEqual(len(sets), 1)  # the non-dict row is skipped, not raised on
        s = sets[0]
        self.assertIsNone(s.set_type)
        self.assertIsNone(s.category)
        self.assertIsNone(s.exercise)
        self.assertIsNone(s.display)
        self.assertIsNone(s.reps)
        self.assertIsNone(s.weight_value)
        self.assertIsNone(s.weight_unit)
        self.assertIsNone(s.duration_s)
        self.assertIsNone(s.start_local)

    def test_unknown_category_exercise_pair_has_no_display(self):
        payload = {"exerciseSets": [{"exercises": [{"category": "NOT_A_REAL_CATEGORY", "name": "NOT_A_REAL_EXERCISE"}]}]}
        sets = garmin_adapter.parse_exercise_sets(payload)
        self.assertIsNone(sets[0].display)

    def test_payload_given_as_a_bare_list_is_also_accepted(self):
        sets = garmin_adapter.parse_exercise_sets(EXERCISE_SETS_RAW["exerciseSets"])
        self.assertEqual(len(sets), 3)


class FakeGarminExerciseSetsSucceed:
    """Replaces garminconnect.Garmin: get_activity_exercise_sets returns a fixture. Invented
    data only."""

    def __init__(self, **kw):
        self.client = mock.Mock()
        self.client.dumps.return_value = "tok-demo"

    def login(self, tokenstore=None):
        return None, None

    def get_activity_exercise_sets(self, activity_id):
        return EXERCISE_SETS_RAW


class FetchExerciseSetsSuccessTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("garminconnect.Garmin", FakeGarminExerciseSetsSucceed)
        patcher.start()
        self.addCleanup(patcher.stop)
        config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_file = config.TOKEN_DIR / "garmin_tokens.json"
        token_file.write_text('{"demo": true}', encoding="utf-8")
        self.addCleanup(token_file.unlink, missing_ok=True)  # TOKEN_DIR is shared across this test run

    def test_activity_id_kept_as_a_string_and_sets_parse(self):
        data = garmin_adapter.fetch_exercise_sets(12345)
        self.assertEqual(data.activity_id, "12345")
        self.assertEqual(len(data.sets), 3)
        self.assertNotIn("_errors", data.raw)

    def test_hosted_token_path_returns_the_refreshed_token(self):
        data, token_after = garmin_adapter.fetch_exercise_sets(12345, token_str="tok-before")
        self.assertEqual(len(data.sets), 3)
        self.assertEqual(token_after, "tok-demo")


class FakeGarminExerciseSetsFails:
    """Replaces garminconnect.Garmin: get_activity_exercise_sets raises. Invented data only."""

    def __init__(self, **kw):
        self.client = mock.Mock()
        self.client.dumps.return_value = "tok-demo"

    def login(self, tokenstore=None):
        return None, None

    def get_activity_exercise_sets(self, activity_id):
        raise RuntimeError("demo exercise-sets failure")


class FetchExerciseSetsFailureTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("garminconnect.Garmin", FakeGarminExerciseSetsFails)
        patcher.start()
        self.addCleanup(patcher.stop)
        config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_file = config.TOKEN_DIR / "garmin_tokens.json"
        token_file.write_text('{"demo": true}', encoding="utf-8")
        self.addCleanup(token_file.unlink, missing_ok=True)  # TOKEN_DIR is shared across this test run

    def test_error_kind_is_recorded_and_sets_stay_empty(self):
        data = garmin_adapter.fetch_exercise_sets("12345")
        self.assertEqual(data.activity_id, "12345")
        self.assertEqual(data.sets, ())
        self.assertEqual(data.raw["_errors"], {"exercise_sets": "RuntimeError"})


SLEEP_RAW_SYNCED = {"dailySleepDTO": {
    "sleepStartTimestampLocal": 1793833200000, "sleepEndTimestampLocal": 1793859300000,
}}


class ParseSleepEndTest(unittest.TestCase):
    """BODY-DESPERTAR-1: _parse_sleep_end is the one helper both parse_day and fetch_sleep use --
    exercised directly so a future change to either caller cannot silently fork the logic."""

    def test_synced_night_parses_the_same_as_parse_day(self):
        wake = garmin_adapter._parse_sleep_end(SLEEP_RAW_SYNCED)
        d = garmin_adapter.parse_day("2026-11-05", {"sleep": SLEEP_RAW_SYNCED})
        self.assertEqual(wake, d.sleep_end)
        self.assertEqual(wake, "2026-11-05T06:15")

    def test_not_synced_yet_is_none(self):
        self.assertIsNone(garmin_adapter._parse_sleep_end({}))
        self.assertIsNone(garmin_adapter._parse_sleep_end({"dailySleepDTO": {}}))
        self.assertIsNone(garmin_adapter._parse_sleep_end(None))


class FakeGarminSleepSucceeds:
    """Replaces garminconnect.Garmin: get_sleep_data returns a fixture. Invented data only."""

    def __init__(self, **kw):
        self.client = mock.Mock()
        self.client.dumps.return_value = "tok-demo"

    def login(self, tokenstore=None):
        return None, None

    def get_sleep_data(self, day):
        return SLEEP_RAW_SYNCED


class FetchSleepSuccessTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("garminconnect.Garmin", FakeGarminSleepSucceeds)
        patcher.start()
        self.addCleanup(patcher.stop)
        config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_file = config.TOKEN_DIR / "garmin_tokens.json"
        token_file.write_text('{"demo": true}', encoding="utf-8")
        self.addCleanup(token_file.unlink, missing_ok=True)  # TOKEN_DIR is shared across this test run

    def test_wake_time_parses(self):
        wake = garmin_adapter.fetch_sleep("2026-11-05")
        self.assertEqual(wake, "2026-11-05T06:15")

    def test_hosted_token_path_returns_the_refreshed_token(self):
        wake, token_after = garmin_adapter.fetch_sleep("2026-11-05", token_str="tok-before")
        self.assertEqual(wake, "2026-11-05T06:15")
        self.assertEqual(token_after, "tok-demo")


class FakeGarminSleepNotSyncedYet:
    """Replaces garminconnect.Garmin: get_sleep_data returns a payload with no sleep end yet."""

    def __init__(self, **kw):
        self.client = mock.Mock()
        self.client.dumps.return_value = "tok-demo"

    def login(self, tokenstore=None):
        return None, None

    def get_sleep_data(self, day):
        return {"dailySleepDTO": {}}


class FetchSleepNotSyncedYetTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("garminconnect.Garmin", FakeGarminSleepNotSyncedYet)
        patcher.start()
        self.addCleanup(patcher.stop)
        config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_file = config.TOKEN_DIR / "garmin_tokens.json"
        token_file.write_text('{"demo": true}', encoding="utf-8")
        self.addCleanup(token_file.unlink, missing_ok=True)

    def test_none_when_not_synced(self):
        self.assertIsNone(garmin_adapter.fetch_sleep("2026-11-05"))


class FakeGarminSleepFails:
    """Replaces garminconnect.Garmin: get_sleep_data raises. Invented data only."""

    def __init__(self, **kw):
        self.client = mock.Mock()
        self.client.dumps.return_value = "tok-demo"

    def login(self, tokenstore=None):
        return None, None

    def get_sleep_data(self, day):
        raise RuntimeError("demo sleep failure")


class FetchSleepFailureTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("garminconnect.Garmin", FakeGarminSleepFails)
        patcher.start()
        self.addCleanup(patcher.stop)
        config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_file = config.TOKEN_DIR / "garmin_tokens.json"
        token_file.write_text('{"demo": true}', encoding="utf-8")
        self.addCleanup(token_file.unlink, missing_ok=True)

    def test_error_propagates_no_payload_swallowed(self):
        with self.assertRaises(RuntimeError):
            garmin_adapter.fetch_sleep("2026-11-05")


if __name__ == "__main__":
    unittest.main()
