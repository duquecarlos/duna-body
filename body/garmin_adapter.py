"""The ONLY module that knows about python-garminconnect (unofficial; may break).

It turns Garmin's payloads into a source-neutral DayData. To move to the official
Garmin Health API later, write another adapter that returns the same DayData.

Privacy: never log payloads, tokens or credentials from here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import config

SOURCE = "garmin"

# BODY-LOGIN-MENSAJES-1: garminconnect's own logger calls (api/_common.py, api/base.py and the
# five client/*.py modules, all getLogger(__name__) under "garminconnect") have no handler
# anywhere in Body's logging config, so Python's logging.lastResort would otherwise write their
# text straight to stderr. A NullHandler on the shared "garminconnect" namespace satisfies every
# descendant logger's search for a handler, and propagate=False stops the record climbing past
# here to a handler something else configures on the root logger.
_garminconnect_logger = logging.getLogger("garminconnect")
_garminconnect_logger.addHandler(logging.NullHandler())
_garminconnect_logger.propagate = False


@dataclass
class DayData:
    day: str
    source: str = SOURCE
    raw: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    sleep_start: str | None = None
    sleep_end: str | None = None
    activities: list[dict[str, Any]] = field(default_factory=list)
    # BODY-ENTRENO-SUENO-PARSE-1: the watch does not measure snoring, but these are a proxy
    # (low SpO2 / disturbed respiration can accompany it) -- kept off DayData.metrics, not
    # METRIC_DEFS rows yet, so a future storage slice decides the metric_def shape (keys, units,
    # direction) rather than inheriting one from here.
    sleep_spo2_avg: float | None = None
    sleep_respiration_avg: float | None = None
    sleep_respiration_low: float | None = None
    sleep_respiration_high: float | None = None


@dataclass
class IntradayData:
    """One local day's intraday series: heart rate, stress, Body Battery events.

    heart_rate and stress are tuples of (local time, value) in payload order, local time being the
    person's own zone (the zone is a parameter to parse_intraday/fetch_intraday -- this module
    never reads it from the database). A series Garmin returns empty or null, out of its ~130-140
    day retention window, stays an honest gap: absent (), never a zero. A null sample is dropped.
    Measured 2026-11-09 (BODY-INTRADIA-SPOTCHECK-1, body-app/research/intradia-spotcheck-
    2026-11-09.md): a stress sample of -1 or -2 is Garmin's "no reading" code (off wrist / too
    much motion), never a level, and is dropped the same way; a heart-rate sample of 0 or less is
    not a reading either (a living person's pulse is never 0) and is dropped too.
    *_interval_minutes is the sampling interval from a key the payload states directly (none does
    today, per the same spot-check), or else derived from the first two samples' own timestamps.
    """
    day: str
    source: str = SOURCE
    raw: dict[str, Any] = field(default_factory=dict)
    heart_rate: tuple[tuple[str, float], ...] = ()
    heart_rate_interval_minutes: float | None = None
    stress: tuple[tuple[str, float], ...] = ()
    stress_interval_minutes: float | None = None
    body_battery_events: tuple[dict[str, Any], ...] = ()


@dataclass
class ExerciseSet:
    """One set of a strength activity. BODY-ENTRENO-ADAPTER-1: every field here reads a key
    the installed garminconnect 0.3.20 package does not confirm for
    get_activity_exercise_sets -- see parse_exercise_sets for which key is assumed where. A
    value this module cannot find or cannot make sense of is None, an honest gap, never
    guessed and never a 0.
    """
    set_type: str | None = None  # 'active' | 'rest'
    category: str | None = None
    exercise: str | None = None
    display: str | None = None  # exercises.py catalog name for (category, exercise), when known
    reps: float | None = None
    weight_value: float | None = None
    weight_unit: str | None = None
    duration_s: float | None = None
    start_local: str | None = None


@dataclass
class ExerciseSetsData:
    """Per-set detail for one strength activity: exercise, reps, weight, duration, start, in
    the order Garmin returned them. BODY-ENTRENO-ADAPTER-1 -- see fetch_exercise_sets."""
    activity_id: str
    source: str = SOURCE
    sets: tuple[ExerciseSet, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


class NotSignedIn(RuntimeError):
    pass


def sign_in(email: str, password: str, prompt_mfa: Callable[[], str]) -> None:
    """Interactive first sign-in. Saves a token in TOKEN_DIR; the password is not kept."""
    from garminconnect import Garmin

    config.ensure_home()
    api = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    api.login(str(config.TOKEN_DIR))
    api.client.dump(str(config.TOKEN_DIR))  # make sure the token is on disk


def has_token() -> bool:
    return config.TOKEN_DIR.exists() and any(config.TOKEN_DIR.iterdir())


def start_sign_in(email: str, password: str) -> tuple[str, str]:
    """Hosted sign-in, step one: ("ok", token_str) on success, or ("needs_mfa", client_state_json)
    when Garmin asks for a code -- finish with finish_sign_in(). email and password are used once
    here and never stored, logged or returned."""
    from garminconnect import Garmin

    api = Garmin(email=email, password=password, return_on_mfa=True)
    status, state = api.login()
    if status == "needs_mfa":
        return "needs_mfa", json.dumps(state)
    return "ok", api.client.dumps()


def finish_sign_in(client_state_json: str, code: str) -> str:
    """Hosted sign-in, step two: finishes the MFA login start_sign_in() began and returns the
    token. code is used once here and never stored, logged or returned."""
    from garminconnect import Garmin

    api = Garmin(return_on_mfa=True)
    api.resume_login(json.loads(client_state_json), code)
    return api.client.dumps()


def _client(token_str: str | None = None):
    from garminconnect import Garmin

    if token_str is not None:
        api = Garmin(return_on_mfa=False)
        try:
            api.login(token_str)
        except Exception as e:  # rejected token -> the person must reconnect on /garmin
            raise NotSignedIn(f"Garmin token not accepted ({type(e).__name__}).") from None
        return api
    if not has_token():
        raise NotSignedIn("No Garmin token yet. Run: python -m body.login")
    api = Garmin()
    try:
        api.login(str(config.TOKEN_DIR))
    except Exception as e:  # stale/rejected token -> ask for a fresh sign-in
        raise NotSignedIn(f"Garmin token not accepted ({type(e).__name__}). Run: python -m body.login") from None
    return api


def fetch_day(day: str, token_str: str | None = None):
    """Pull one calendar day from Garmin Connect.

    token_str=None: today's local TOKEN_DIR flow, unchanged; returns DayData.
    token_str given (hosted, person-scoped): returns (DayData, token_after), where token_after is
    token_str's value, possibly refreshed by Garmin during this call -- compare the two to decide
    whether the caller must re-encrypt and store it.
    """
    api = _client(token_str)
    raw: dict[str, Any] = {}
    calls = {
        "user_summary": lambda: api.get_user_summary(day),
        "sleep": lambda: api.get_sleep_data(day),
        "hrv": lambda: api.get_hrv_data(day),
        "body_battery": lambda: api.get_body_battery(day),
        "activities": lambda: api.get_activities_by_date(day, day),
        "max_metrics": lambda: api.get_max_metrics(day),
    }
    errors = {}
    for kind, call in calls.items():
        try:
            raw[kind] = call()
        except Exception as e:  # one missing feed should not lose the rest of the day
            errors[kind] = type(e).__name__
    if errors:
        raw["_errors"] = errors
    data = parse_day(day, raw)
    if token_str is None:
        return data
    return data, api.client.dumps()


def fetch_intraday(day: str, zone: str, token_str: str | None = None):
    """Pull one calendar day's intraday series: heart rate, stress, Body Battery events.

    `zone` is the person's own IANA timezone name, given by the caller -- this module never reads
    it from the database. It is needed because Garmin's heart-rate/stress arrays carry each
    sample's timestamp as a UTC INSTANT, not already shifted to local wall time: measured
    2026-11-09 (BODY-INTRADIA-SPOTCHECK-1), the first sample of a day equals that day's own
    startTimestampGMT.

    One call per kind (get_heart_rates, get_stress_data -- not get_all_day_stress, which hits the
    same endpoint --, get_body_battery_events), same session/token pattern as fetch_day including
    the hosted per-person token path. Only heart rate and stress are parsed to the measured facts
    above; Body Battery events are passed through unparsed, as before (out of scope here).
    Called on demand only: nothing in pull.py's scheduled paths (pull_all, pull_hosted,
    pull_backfill_hosted) calls this (BODY-INTRADIA-ADAPTER-1, BODY-MOMENTOS-PULL-1).
    """
    api = _client(token_str)
    raw: dict[str, Any] = {}
    calls = {
        "heart_rate": lambda: api.get_heart_rates(day),
        "stress": lambda: api.get_stress_data(day),
        "body_battery_events": lambda: api.get_body_battery_events(day),
    }
    errors = {}
    for kind, call in calls.items():
        try:
            raw[kind] = call()
        except Exception as e:  # one missing series should not lose the other two
            errors[kind] = type(e).__name__
    if errors:
        raw["_errors"] = errors
    data = parse_intraday(day, raw, zone)
    if token_str is None:
        return data
    return data, api.client.dumps()


def fetch_exercise_sets(activity_id: int | str, token_str: str | None = None):
    """Pull one strength activity's per-set detail: exercise, reps, weight, duration, start.

    One call -- get_activity_exercise_sets(activity_id) (garminconnect api/activities.py:613)
    -- same session/token pattern as fetch_day and fetch_intraday, including the hosted
    per-person token path. BODY-ENTRENO-ADAPTER-1 (an investigation slice, BODY-ENTRENO-1-INV):
    the installed garminconnect 0.3.20 package types this call's return only as dict[str, Any]
    and its docstring says only "Return activity exercise sets." -- the response SHAPE IS
    UNCONFIRMED. The only confirmed facts about this endpoint come from the write call in the
    same module, set_activity_exercise_sets, whose docstring says its payload is "in the same
    shape as the response from get_activity_exercise_sets" and names exercises[].category
    (the parent) and exercises[].name (the sub-category) as validated fields; and from
    exercises.py's static category/name catalog. parse_exercise_sets below parses defensively
    from those two facts plus the common per-set keys a FIT-derived strength set would carry,
    and documents each assumed key where it is read.

    Called on demand only, when a person logs or corrects a session: nothing in pull.py, the
    backfill loop or the web app calls this yet. The write call set_activity_exercise_sets is
    NOT used here or anywhere in this module.
    """
    api = _client(token_str)
    raw: dict[str, Any] = {}
    errors: dict[str, str] = {}
    try:
        raw["exercise_sets"] = api.get_activity_exercise_sets(activity_id)
    except Exception as e:  # one failed call should give an honest gap, never raise out of a fetch
        errors["exercise_sets"] = type(e).__name__
    if errors:
        raw["_errors"] = errors
    sets = parse_exercise_sets(raw.get("exercise_sets"))
    data = ExerciseSetsData(activity_id=str(activity_id), sets=sets, raw=raw)
    if token_str is None:
        return data
    return data, api.client.dumps()


# ---------------------------------------------------------------- parsing (pure)

def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num_in_range(v: Any, lo: float, hi: float) -> float | None:
    """Garmin can send 0 or another sentinel when the sensor was off; a value outside the
    physiologically possible range is treated as absent rather than a real reading."""
    n = _num(v)
    if n is None or n < lo or n > hi:
        return None
    return n


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _first_num(d: Any, *keys: str) -> float | None:
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = _num(d.get(k))
        if v is not None:
            return v
    return None


def _max_metrics_entry(raw: Any) -> dict:
    """Garmin's maxmet endpoint is seen as either a list of per-day entries or a single dict
    (unconfirmed by the installed garminconnect package -- parsed defensively either way)."""
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                return entry
        return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _local_iso(ms: Any) -> str | None:
    """Garmin '...TimestampLocal' values are epoch-ms already shifted to local wall time."""
    n = _num(ms)
    if n is None:
        return None
    return datetime.fromtimestamp(n / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="minutes")


def parse_day(day: str, raw: dict[str, Any]) -> DayData:
    out = DayData(day=day, raw=raw)
    m: dict[str, float | None] = {}

    s = raw.get("user_summary") or {}
    m["resting_hr"] = _num(s.get("restingHeartRate"))
    m["steps"] = _num(s.get("totalSteps"))
    m["stress_avg"] = _num(s.get("averageStressLevel"))
    m["active_kcal"] = _num(s.get("activeKilocalories"))
    m["body_battery_high"] = _num(s.get("bodyBatteryHighestValue"))
    m["body_battery_low"] = _num(s.get("bodyBatteryLowestValue"))
    m["body_battery_wake"] = _num(s.get("bodyBatteryAtWakeTime"))
    m["respiration_awake"] = _num(s.get("avgWakingRespirationValue"))
    hs = _num(s.get("highStressDuration"))
    m["high_stress_min"] = round(hs / 60) if hs is not None else None
    mod, vig = _num(s.get("moderateIntensityMinutes")), _num(s.get("vigorousIntensityMinutes"))
    if mod is not None or vig is not None:
        m["intensity_minutes"] = (mod or 0) + 2 * (vig or 0)  # WHO-style: vigorous counts double
    if m["stress_avg"] is not None and m["stress_avg"] < 0:  # Garmin uses -1/-2 for "no data"
        m["stress_avg"] = None

    sl = _get(raw.get("sleep"), "dailySleepDTO") or {}
    secs = _num(sl.get("sleepTimeSeconds"))
    m["sleep_minutes"] = round(secs / 60) if secs is not None else None
    m["sleep_score"] = _num(_get(sl, "sleepScores", "overall", "value"))
    for key, field_ in (("sleep_deep_min", "deepSleepSeconds"), ("sleep_light_min", "lightSleepSeconds"),
                        ("sleep_rem_min", "remSleepSeconds"), ("sleep_awake_min", "awakeSleepSeconds")):
        v = _num(sl.get(field_))
        m[key] = round(v / 60) if v is not None else None
    m["sleep_need_min"] = _num(_get(sl, "sleepNeed", "actual"))
    m["body_battery_sleep_charge"] = _num((raw.get("sleep") or {}).get("bodyBatteryChange"))
    out.sleep_start = _local_iso(sl.get("sleepStartTimestampLocal"))
    out.sleep_end = _local_iso(sl.get("sleepEndTimestampLocal"))
    out.sleep_spo2_avg = _num_in_range(sl.get("avgSpO2"), 50, 100)
    out.sleep_respiration_avg = _num_in_range(sl.get("averageRespirationValue"), 1, 60)
    out.sleep_respiration_low = _num_in_range(sl.get("lowestRespirationValue"), 1, 60)
    out.sleep_respiration_high = _num_in_range(sl.get("highestRespirationValue"), 1, 60)

    hrv = _get(raw.get("hrv"), "hrvSummary") or {}
    m["hrv_last_night"] = _num(hrv.get("lastNightAvg"))
    m["hrv_weekly_avg"] = _num(hrv.get("weeklyAvg"))

    mm = _max_metrics_entry(raw.get("max_metrics"))
    generic = mm.get("generic") if isinstance(mm.get("generic"), dict) else {}
    cycling = mm.get("cycling") if isinstance(mm.get("cycling"), dict) else {}
    m["vo2max"] = _first_num(generic, "vo2MaxPreciseValue", "vo2MaxValue")
    m["vo2max_cycling"] = _first_num(cycling, "vo2MaxPreciseValue", "vo2MaxValue")
    fitness_age = _first_num(generic, "fitnessAge")
    m["fitness_age"] = fitness_age if fitness_age is not None else _num(mm.get("fitnessAge"))

    if m["body_battery_high"] is None:  # fall back to the body-battery feed
        bb = raw.get("body_battery")
        if isinstance(bb, list) and bb:
            vals = [_num(v[1]) for v in (bb[0].get("bodyBatteryValuesArray") or []) if isinstance(v, list) and len(v) > 1]
            vals = [v for v in vals if v is not None]
            if vals:
                m["body_battery_high"], m["body_battery_low"] = max(vals), min(vals)

    out.metrics = {k: v for k, v in m.items() if v is not None}

    for a in raw.get("activities") or []:
        if not isinstance(a, dict) or a.get("activityId") is None:
            continue
        out.activities.append({
            "source_id": str(a["activityId"]),
            "start_local": a.get("startTimeLocal"),
            "type": _get(a, "activityType", "typeKey"),
            "name": a.get("activityName"),
            "duration_s": _num(a.get("duration")),
            "distance_m": _num(a.get("distance")),
            "avg_hr": _num(a.get("averageHR")),
            "max_hr": _num(a.get("maxHR")),
            "calories": _num(a.get("calories")),
        })
    return out


# Garmin's stress feed uses these two negative values as "no reading" codes -- off-wrist or too
# much motion to compute a level -- never a real stress score (measured 2026-11-09,
# BODY-INTRADIA-SPOTCHECK-1). Kept as a named constant for what they mean; the actual filter below
# drops any negative value the same way, since a stress level can never be negative and no third
# code is confirmed.
GARMIN_STRESS_NO_READING_CODES = (-1, -2)


def _utc_to_local_iso(ms: Any, zone: str) -> str | None:
    """Garmin's intraday arrays (heartRateValues, stressValuesArray) carry each sample's
    timestamp as a UTC INSTANT -- measured 2026-11-09 (BODY-INTRADIA-SPOTCHECK-1): the first
    sample of a day equals that day's own startTimestampGMT. This is a DIFFERENT convention from
    the '...TimestampLocal' scalar fields _local_iso (above) handles, which Garmin already shifts
    to local wall time itself -- the two must not be confused. `zone` is the caller's IANA
    timezone name; this module never resolves it itself."""
    n = _num(ms)
    if n is None:
        return None
    try:
        tz = ZoneInfo(zone)
    except Exception:
        return None
    return (datetime.fromtimestamp(n / 1000, tz=timezone.utc)
            .astimezone(tz).replace(tzinfo=None).isoformat(timespec="minutes"))


def _descriptor_index(descriptors: Any, names: tuple[str, ...]) -> int | None:
    """The column index for a key in `names` (case-insensitive), read from a Garmin descriptor
    list -- each entry a dict naming its key and its index into the per-sample array, e.g.
    heartRateValueDescriptors: [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}]
    (measured 2026-11-09, BODY-INTRADIA-SPOTCHECK-1). Absent, non-list, or no matching entry ->
    None, so the caller falls back to a fixed position."""
    if not isinstance(descriptors, list):
        return None
    wanted = {n.lower() for n in names}
    for entry in descriptors:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        idx = entry.get("index")
        if isinstance(key, str) and key.lower() in wanted and isinstance(idx, int):
            return idx
    return None


def _derived_interval_minutes(values: Any, ts_idx: int) -> float | None:
    """The sampling interval computed from the first two samples' own timestamps -- used only when
    the payload states no interval key directly. None does today (the 2026-11-09 spot-check found
    none), so this derived path is what actually runs."""
    if not isinstance(values, list):
        return None
    times = []
    for sample in values:
        if isinstance(sample, list) and len(sample) > ts_idx:
            t = _num(sample[ts_idx])
            if t is not None:
                times.append(t)
        if len(times) == 2:
            break
    if len(times) < 2:
        return None
    diff = abs(times[1] - times[0])
    return diff / 60000 if diff else None


def _interval_minutes(d: Any, values: Any, ts_idx: int) -> float | None:
    """A key the payload states directly (any key naming "interval", case-insensitive, holding a
    number) takes priority; otherwise the interval is derived from the data (_derived_interval_
    minutes). The installed garminconnect 0.3.20 package does not type get_heart_rates/
    get_stress_data beyond dict[str, Any], so neither the stated-key name nor its presence is
    confirmed -- this is a defensive scan, not a guess at one specific key."""
    if isinstance(d, dict):
        for k, v in d.items():
            if "interval" in k.lower():
                n = _num(v)
                if n is not None:
                    return n
    return _derived_interval_minutes(values, ts_idx)


def _parse_intraday_series(
    d: Any, values_key: str, descriptors_key: str, value_names: tuple[str, ...],
    valid: Callable[[float], bool], zone: str,
) -> tuple[tuple[tuple[str, float], ...], float | None]:
    """One Garmin intraday series: values_key holding a list of samples, each a list whose
    timestamp and value columns are found BY NAME in descriptors_key's descriptor list (falling
    back to positions 0/1 when that list is absent -- measured 2026-11-09, BODY-INTRADIA-
    SPOTCHECK-1). A missing/non-list series, or one with no usable samples, is the empty tuple --
    an honest gap, never a zero. A null sample, or one `valid` rejects (Garmin's no-reading codes
    for stress, a non-positive value for heart rate), is dropped, not kept as 0."""
    if not isinstance(d, dict):
        return (), None
    values = d.get(values_key)
    if not isinstance(values, list):
        return (), None
    descriptors = d.get(descriptors_key)
    ts_idx = _descriptor_index(descriptors, ("timestamp",))
    val_idx = _descriptor_index(descriptors, value_names)
    ts_idx = 0 if ts_idx is None else ts_idx
    val_idx = 1 if val_idx is None else val_idx
    out = []
    for sample in values:
        if not isinstance(sample, list) or len(sample) <= max(ts_idx, val_idx):
            continue
        value = _num(sample[val_idx])
        if value is None or not valid(value):
            continue
        local_time = _utc_to_local_iso(sample[ts_idx], zone)
        if local_time is None:
            continue
        out.append((local_time, value))
    interval = _interval_minutes(d, values, ts_idx)
    return tuple(out), interval


def _heart_rate_valid(v: float) -> bool:
    return v > 0  # 0 bpm is not a reading either -- a living person's pulse is never 0


def _stress_valid(v: float) -> bool:
    return v >= 0  # drops GARMIN_STRESS_NO_READING_CODES and any other negative sentinel


def parse_heart_rate_series(payload: Any, zone: str) -> tuple[tuple[str, float], ...]:
    """One day's heart-rate series, parsed straight from a stored get_heart_rates(day) payload --
    the SAME parser parse_intraday uses, exposed so storage can re-parse a raw payload on read
    (db.intradia, BODY-MOMENTOS-PULL-1) without a network call."""
    series, _ = _parse_intraday_series(
        payload, "heartRateValues", "heartRateValueDescriptors", ("heartrate",), _heart_rate_valid, zone)
    return series


def parse_stress_series(payload: Any, zone: str) -> tuple[tuple[str, float], ...]:
    """One day's stress series, parsed straight from a stored get_stress_data(day) payload -- see
    parse_heart_rate_series."""
    series, _ = _parse_intraday_series(
        payload, "stressValuesArray", "stressValueDescriptorsDTOList", ("stresslevel",), _stress_valid, zone)
    return series


def parse_intraday(day: str, raw: dict[str, Any], zone: str) -> IntradayData:
    out = IntradayData(day=day, raw=raw)

    hr = raw.get("heart_rate")
    out.heart_rate, out.heart_rate_interval_minutes = _parse_intraday_series(
        hr, "heartRateValues", "heartRateValueDescriptors", ("heartrate",), _heart_rate_valid, zone)

    st = raw.get("stress")
    out.stress, out.stress_interval_minutes = _parse_intraday_series(
        st, "stressValuesArray", "stressValueDescriptorsDTOList", ("stresslevel",), _stress_valid, zone)

    events = raw.get("body_battery_events")
    out.body_battery_events = tuple(e for e in events if isinstance(e, dict)) if isinstance(events, list) else ()

    return out


def _exercise_set_rows(payload: Any) -> list[Any]:
    """The installed garminconnect 0.3.20 package names no shape for
    get_activity_exercise_sets. Assumed, from set_activity_exercise_sets's docstring (same
    module, same shape per that docstring): a dict holding an "exerciseSets" list. Also
    accepted: the list directly, in case the GET response is unwrapped relative to the PUT
    body. Anything else -- a missing key, a null, any other shape -- is the empty list, an
    honest gap, never a crash."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        rows = payload.get("exerciseSets")
        if isinstance(rows, list):
            return rows
    return []


_SET_TYPE_ACTIVE = {"active", "working", "work", "set"}
_SET_TYPE_REST = {"rest", "resting"}


def _set_type(d: dict) -> str | None:
    """Assumed key: "setType" (seen confirmed nowhere; a guess at common Garmin naming, tried
    alongside the plainer "type"). A value outside the known active/rest wording is an honest
    gap (None), never guessed toward one or the other."""
    for k in ("setType", "type"):
        v = d.get(k)
        if isinstance(v, str):
            low = v.strip().lower()
            if low in _SET_TYPE_ACTIVE:
                return "active"
            if low in _SET_TYPE_REST:
                return "rest"
    return None


def _category_and_exercise(d: dict) -> tuple[str | None, str | None]:
    """Confirmed by set_activity_exercise_sets's docstring: exercises[].category is the
    parent, exercises[].name is the sub-category. Only the first entry of "exercises" is
    read -- nothing confirms a set can name more than one."""
    exercises = d.get("exercises")
    if isinstance(exercises, list) and exercises and isinstance(exercises[0], dict):
        first = exercises[0]
        category = first.get("category")
        exercise = first.get("name")
        return (category if isinstance(category, str) else None,
                exercise if isinstance(exercise, str) else None)
    return None, None


_CATALOG_BY_PAIR: dict[tuple[str, str], str] | None = None


def _catalog_display(category: Any, exercise: Any) -> str | None:
    """Reads exercises.py's (category, exercise) -> display-name catalog -- never renames
    category/exercise themselves, only offers a display label when the catalog has one."""
    global _CATALOG_BY_PAIR
    if not isinstance(category, str) or not isinstance(exercise, str):
        return None
    if _CATALOG_BY_PAIR is None:
        from garminconnect import exercises as _ex
        _CATALOG_BY_PAIR = {(e["category"], e["exercise"]): e["name"] for e in _ex.EXERCISES}
    return _CATALOG_BY_PAIR.get((category, exercise))


def _weight(d: dict) -> tuple[float | None, str | None]:
    """Assumed key: "weight" (unconfirmed). Garmin is known to send body-composition weight in
    grams elsewhere, but nothing confirmed states this endpoint's unit, so a present value is
    kept as-is and its unit left absent rather than guessed grams/kilograms. No "weight" key,
    or an explicit null (a bodyweight set), leaves both value and unit absent together."""
    v = _num(d.get("weight"))
    if v is None:
        return None, None
    unit = d.get("weightUnit")
    return v, unit if isinstance(unit, str) else None


def _start_local(d: dict) -> str | None:
    """Assumed key: "startTime" (unconfirmed). Tried first as epoch-ms local -- the convention
    _local_iso already applies to this package's '...TimestampLocal' fields -- then kept as a
    plain string if it is one, since nothing confirms which this endpoint sends."""
    v = d.get("startTime")
    if v is None:
        return None
    iso = _local_iso(v)
    if iso is not None:
        return iso
    return v if isinstance(v, str) else None


def parse_exercise_sets(payload: Any) -> tuple[ExerciseSet, ...]:
    """Parse one get_activity_exercise_sets response (or None/any other shape a failed or
    unavailable call leaves behind) into an ordered tuple of ExerciseSet. See
    fetch_exercise_sets for why this shape is unconfirmed, and _exercise_set_rows and the
    per-field helpers below for exactly which key is assumed where."""
    sets = []
    for row in _exercise_set_rows(payload):
        if not isinstance(row, dict):
            continue
        category, exercise = _category_and_exercise(row)
        weight_value, weight_unit = _weight(row)
        sets.append(ExerciseSet(
            set_type=_set_type(row),
            category=category,
            exercise=exercise,
            display=_catalog_display(category, exercise),
            reps=_first_num(row, "reps", "repetitionCount", "repCount"),
            weight_value=weight_value,
            weight_unit=weight_unit,
            duration_s=_num(row.get("duration")),  # assumed key, unconfirmed; assumed seconds
            start_local=_start_local(row),
        ))
    return tuple(sets)
