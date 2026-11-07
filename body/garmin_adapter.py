"""The ONLY module that knows about python-garminconnect (unofficial; may break).

It turns Garmin's payloads into a source-neutral DayData. To move to the official
Garmin Health API later, write another adapter that returns the same DayData.

Privacy: never log payloads, tokens or credentials from here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import config

SOURCE = "garmin"


@dataclass
class DayData:
    day: str
    source: str = SOURCE
    raw: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    sleep_start: str | None = None
    sleep_end: str | None = None
    activities: list[dict[str, Any]] = field(default_factory=list)


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


# ---------------------------------------------------------------- parsing (pure)

def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
