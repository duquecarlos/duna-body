"""The ONLY module that knows about python-garminconnect (unofficial; may break).

It turns Garmin's payloads into a source-neutral DayData. To move to the official
Garmin Health API later, write another adapter that returns the same DayData.

Privacy: never log payloads, tokens or credentials from here.
"""
from __future__ import annotations

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


def _client():
    from garminconnect import Garmin

    if not has_token():
        raise NotSignedIn("No Garmin token yet. Run: python -m body.login")
    api = Garmin()
    try:
        api.login(str(config.TOKEN_DIR))
    except Exception as e:  # stale/rejected token -> ask for a fresh sign-in
        raise NotSignedIn(f"Garmin token not accepted ({type(e).__name__}). Run: python -m body.login") from None
    return api


def fetch_day(day: str) -> DayData:
    """Pull one calendar day from Garmin Connect."""
    api = _client()
    raw: dict[str, Any] = {}
    calls = {
        "user_summary": lambda: api.get_user_summary(day),
        "sleep": lambda: api.get_sleep_data(day),
        "hrv": lambda: api.get_hrv_data(day),
        "body_battery": lambda: api.get_body_battery(day),
        "activities": lambda: api.get_activities_by_date(day, day),
    }
    errors = {}
    for kind, call in calls.items():
        try:
            raw[kind] = call()
        except Exception as e:  # one missing feed should not lose the rest of the day
            errors[kind] = type(e).__name__
    if errors:
        raw["_errors"] = errors
    return parse_day(day, raw)


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
    mod, vig = _num(s.get("moderateIntensityMinutes")), _num(s.get("vigorousIntensityMinutes"))
    if mod is not None or vig is not None:
        m["intensity_minutes"] = (mod or 0) + 2 * (vig or 0)  # WHO-style: vigorous counts double
    if m["stress_avg"] is not None and m["stress_avg"] < 0:  # Garmin uses -1/-2 for "no data"
        m["stress_avg"] = None

    sl = _get(raw.get("sleep"), "dailySleepDTO") or {}
    secs = _num(sl.get("sleepTimeSeconds"))
    m["sleep_minutes"] = round(secs / 60) if secs is not None else None
    m["sleep_score"] = _num(_get(sl, "sleepScores", "overall", "value"))
    out.sleep_start = _local_iso(sl.get("sleepStartTimestampLocal"))
    out.sleep_end = _local_iso(sl.get("sleepEndTimestampLocal"))

    hrv = _get(raw.get("hrv"), "hrvSummary") or {}
    m["hrv_last_night"] = _num(hrv.get("lastNightAvg"))
    m["hrv_weekly_avg"] = _num(hrv.get("weeklyAvg"))

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
