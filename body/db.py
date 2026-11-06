"""SQLite storage. Everything person-specific is data: goals, plans, shifts, metrics.

The schema is the template for a future multi-person product, so every row carries a
person_id. Nothing personal is seeded; only generic metric definitions are.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS person (
    id          INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    timezone    TEXT NOT NULL DEFAULT 'Europe/Madrid'
);

-- What can be measured. Generic definitions, shared by every person.
CREATE TABLE IF NOT EXISTS metric_def (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    unit    TEXT NOT NULL,
    better  TEXT NOT NULL CHECK (better IN ('higher', 'lower', 'neutral'))
);

-- One normalised number per person, day and metric.
CREATE TABLE IF NOT EXISTS daily_metric (
    person_id  INTEGER NOT NULL REFERENCES person(id),
    day        TEXT NOT NULL,              -- YYYY-MM-DD (calendar day of the watch)
    metric_key TEXT NOT NULL REFERENCES metric_def(key),
    value      REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT 'garmin',
    PRIMARY KEY (person_id, day, metric_key)
);

-- Sleep as a session, because timing matters for shift work, not only duration.
CREATE TABLE IF NOT EXISTS sleep_session (
    person_id   INTEGER NOT NULL REFERENCES person(id),
    day         TEXT NOT NULL,             -- the day Garmin files the sleep under (wake day)
    start_local TEXT,                      -- ISO local time
    end_local   TEXT,
    PRIMARY KEY (person_id, day)
);

-- Raw source payloads, kept while the model settles so nothing is lost.
CREATE TABLE IF NOT EXISTS raw_payload (
    person_id  INTEGER NOT NULL REFERENCES person(id),
    day        TEXT NOT NULL,
    kind       TEXT NOT NULL,              -- e.g. 'user_summary', 'sleep', 'hrv'
    source     TEXT NOT NULL DEFAULT 'garmin',
    fetched_at TEXT NOT NULL,
    payload    TEXT NOT NULL,              -- JSON
    PRIMARY KEY (person_id, day, kind, source)
);

CREATE TABLE IF NOT EXISTS activity (
    person_id   INTEGER NOT NULL REFERENCES person(id),
    source_id   TEXT NOT NULL,
    day         TEXT NOT NULL,
    start_local TEXT,
    type        TEXT,
    name        TEXT,
    duration_s  REAL,
    distance_m  REAL,
    avg_hr      REAL,
    max_hr      REAL,
    calories    REAL,
    source      TEXT NOT NULL DEFAULT 'garmin',
    PRIMARY KEY (person_id, source, source_id)
);

-- Work shifts: a first-class input (the shift-vs-recovery idea).
-- Arrives later from the Personal desk; codes are whatever the employer uses.
CREATE TABLE IF NOT EXISTS shift (
    person_id   INTEGER NOT NULL REFERENCES person(id),
    day         TEXT NOT NULL,             -- the day the shift starts
    kind        TEXT NOT NULL CHECK (kind IN ('morning', 'afternoon', 'night', 'day', 'off', 'other')),
    code        TEXT,                      -- original code from the schedule, e.g. 'N'
    start_local TEXT,                      -- ISO local time; a night shift ends the next day
    end_local   TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (person_id, day, start_local)
);

-- Goals: metric, target, deadline. A plan is a list of items; a weekly check judges it.
CREATE TABLE IF NOT EXISTS goal (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES person(id),
    title          TEXT NOT NULL,
    metric_key     TEXT REFERENCES metric_def(key),
    target_value   REAL,
    baseline_value REAL,
    start_day      TEXT NOT NULL,
    deadline       TEXT,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done', 'dropped'))
);

CREATE TABLE IF NOT EXISTS plan_item (
    id            INTEGER PRIMARY KEY,
    goal_id       INTEGER NOT NULL REFERENCES goal(id),
    title         TEXT NOT NULL,
    cadence       TEXT,                    -- e.g. 'daily', '3x/week'
    shift_context TEXT,                    -- e.g. 'before_night', 'after_night', 'off_days', NULL = always
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS weekly_check (
    id          INTEGER PRIMARY KEY,
    goal_id     INTEGER NOT NULL REFERENCES goal(id),
    week_start  TEXT NOT NULL,             -- Monday, YYYY-MM-DD
    metric_avg  REAL,
    on_track    INTEGER,                   -- 1 / 0 / NULL (not enough data)
    note        TEXT,
    UNIQUE (goal_id, week_start)
);
"""

METRIC_DEFS = [
    ("resting_hr", "Resting heart rate", "bpm", "lower"),
    ("hrv_last_night", "HRV (last night avg)", "ms", "higher"),
    ("hrv_weekly_avg", "HRV (7-day avg)", "ms", "higher"),
    ("sleep_minutes", "Sleep", "min", "higher"),
    ("sleep_score", "Sleep score", "pts", "higher"),
    ("stress_avg", "Average stress", "pts", "lower"),
    ("body_battery_high", "Body Battery high", "pts", "higher"),
    ("body_battery_low", "Body Battery low", "pts", "higher"),
    ("body_battery_wake", "Body Battery at wake", "pts", "higher"),
    ("steps", "Steps", "steps", "higher"),
    ("active_kcal", "Active calories", "kcal", "neutral"),
    ("intensity_minutes", "Intensity minutes", "min", "higher"),
]

DEFAULT_PERSON_ID = 1


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_home()
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO metric_def (key, label, unit, better) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET label=excluded.label, unit=excluded.unit, better=excluded.better",
        METRIC_DEFS,
    )
    # One owner row so a single-person install works; no personal details.
    conn.execute(
        "INSERT OR IGNORE INTO person (id, display_name) VALUES (?, 'Me')", (DEFAULT_PERSON_ID,)
    )
    conn.commit()


def save_day(conn: sqlite3.Connection, day_data, person_id: int = DEFAULT_PERSON_ID) -> None:
    """Store one DayData from an adapter (raw payloads, metrics, sleep, activities)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    d = day_data.day
    src = day_data.source
    with conn:
        for kind, payload in day_data.raw.items():
            conn.execute(
                "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (person_id, d, kind, src, now, json.dumps(payload)),
            )
        for key, value in day_data.metrics.items():
            conn.execute(
                "INSERT OR REPLACE INTO daily_metric (person_id, day, metric_key, value, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (person_id, d, key, value, src),
            )
        if day_data.sleep_start or day_data.sleep_end:
            conn.execute(
                "INSERT OR REPLACE INTO sleep_session (person_id, day, start_local, end_local) "
                "VALUES (?, ?, ?, ?)",
                (person_id, d, day_data.sleep_start, day_data.sleep_end),
            )
        for a in day_data.activities:
            conn.execute(
                "INSERT OR REPLACE INTO activity (person_id, source_id, day, start_local, type, name, "
                "duration_s, distance_m, avg_hr, max_hr, calories, source) "
                "VALUES (:person_id, :source_id, :day, :start_local, :type, :name, :duration_s, "
                ":distance_m, :avg_hr, :max_hr, :calories, :source)",
                {**a, "person_id": person_id, "day": d, "source": src},
            )


def load_day(conn: sqlite3.Connection, day: str, person_id: int = DEFAULT_PERSON_ID) -> dict:
    metrics = conn.execute(
        "SELECT m.key, m.label, m.unit, d.value FROM metric_def m "
        "LEFT JOIN daily_metric d ON d.metric_key = m.key AND d.person_id = ? AND d.day = ? "
        "ORDER BY m.rowid",
        (person_id, day),
    ).fetchall()
    sleep = conn.execute(
        "SELECT start_local, end_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, day),
    ).fetchone()
    activities = conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day = ? ORDER BY start_local",
        (person_id, day),
    ).fetchall()
    shifts = conn.execute(
        "SELECT * FROM shift WHERE person_id = ? AND day = ? ORDER BY start_local",
        (person_id, day),
    ).fetchall()
    return {
        "day": day,
        "metrics": [dict(r) for r in metrics],
        "sleep": dict(sleep) if sleep else None,
        "activities": [dict(r) for r in activities],
        "shifts": [dict(r) for r in shifts],
    }


def days_with_data(conn: sqlite3.Connection, person_id: int = DEFAULT_PERSON_ID) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT day FROM daily_metric WHERE person_id = ? ORDER BY day DESC", (person_id,)
    ).fetchall()
    return [r["day"] for r in rows]
