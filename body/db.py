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
    timezone    TEXT NOT NULL DEFAULT 'America/Bogota',
    language    TEXT NOT NULL DEFAULT 'es'
);

-- What can be measured. Generic definitions, shared by every person.
CREATE TABLE IF NOT EXISTS metric_def (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    unit    TEXT NOT NULL,
    better  TEXT NOT NULL CHECK (better IN ('higher', 'lower', 'neutral')),
    kind    TEXT NOT NULL DEFAULT 'measured' CHECK (kind IN ('measured', 'estimated'))
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

-- A set of shifts proposed from another app's extraction, confirmed or dismissed by the person.
CREATE TABLE IF NOT EXISTS shift_batch (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    file_name       TEXT NOT NULL,
    received        TEXT NOT NULL,
    extracted_at    TEXT NOT NULL,
    source_person   TEXT,
    source_timezone TEXT,
    status          TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'dismissed', 'superseded')),
    day_count       INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    UNIQUE (person_id, file_name, received, extracted_at)
);

-- One row per shift in a batch: the full record, including what `shift` has no column for.
CREATE TABLE IF NOT EXISTS shift_batch_item (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES shift_batch(id),
    day         TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('morning', 'afternoon', 'night', 'day', 'off', 'other')),
    code        TEXT,
    start_local TEXT,
    end_local   TEXT,
    holiday     INTEGER NOT NULL DEFAULT 0,
    sunday      INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    outcome     TEXT CHECK (outcome IN ('applied', 'manual_kept', 'newer_kept') OR outcome IS NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_batch_item_slot
    ON shift_batch_item (batch_id, day, COALESCE(start_local, ''));

-- Goals: metric, target, deadline. A plan is a list of items; a weekly check judges it.
CREATE TABLE IF NOT EXISTS goal (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES person(id),   -- who created it
    hogar_id       TEXT,                                     -- set = a household goal (hogar-v1), NULL = personal
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
    assignee_person_id INTEGER REFERENCES person(id),  -- household plans: whose item; NULL = everyone
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

# (key, label, unit, better, kind). 'estimated' = computed by Body, shown with «≈» and "how I calculate it".
METRIC_DEFS = [
    ("readiness", "Readiness", "pts", "higher", "estimated"),
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
    ("sleep_need_min", "Sleep need (Garmin)", "min", "neutral"),
    ("sleep_deep_min", "Deep sleep", "min", "higher"),
    ("sleep_light_min", "Light sleep", "min", "neutral"),
    ("sleep_rem_min", "REM sleep", "min", "higher"),
    ("sleep_awake_min", "Awake during sleep", "min", "lower"),
    ("body_battery_sleep_charge", "Body Battery charged overnight", "pts", "higher"),
    ("respiration_awake", "Breathing rate (awake)", "brpm", "neutral"),
    ("high_stress_min", "High stress time", "min", "lower"),
]
METRIC_DEFS = [d if len(d) == 5 else (*d, "measured") for d in METRIC_DEFS]

DEFAULT_PERSON_ID = 1


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_home()
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(metric_def)")}
    if cols and "kind" not in cols:
        conn.execute("ALTER TABLE metric_def ADD COLUMN kind TEXT NOT NULL DEFAULT 'measured'")
    for table, col, ddl in (("goal", "hogar_id", "TEXT"),
                            ("plan_item", "assignee_person_id", "INTEGER REFERENCES person(id)")):
        tcols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if tcols and col not in tcols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(person)")}
    if pcols and "language" not in pcols:
        conn.execute("ALTER TABLE person ADD COLUMN language TEXT NOT NULL DEFAULT 'es'")
    conn.execute("UPDATE person SET timezone = 'America/Bogota' WHERE timezone = 'Europe/Madrid'")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(shift)")}
    if scols and "batch_id" not in scols:
        conn.execute("ALTER TABLE shift ADD COLUMN batch_id INTEGER REFERENCES shift_batch(id)")
    # Closes the gap the PK leaves open: SQLite treats each NULL start_local as distinct, so two off-day
    # rows for the same person and day do not collide on the primary key alone (BODY-TURNOS-IMPORT-OBS-1).
    # Never a DELETE here: on IntegrityError the table is left untouched and the missing index is reported
    # by body/shift_import.py, which refuses to confirm until it exists.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_slot ON shift (person_id, day, COALESCE(start_local, ''))"
        )
    except sqlite3.IntegrityError:
        pass


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executemany(
        "INSERT INTO metric_def (key, label, unit, better, kind) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET label=excluded.label, unit=excluded.unit, better=excluded.better, "
        "kind=excluded.kind",
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
        "SELECT m.key, m.label, m.unit, m.kind, d.value FROM metric_def m "
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


def set_metric(conn, day: str, key: str, value, source: str, person_id: int = DEFAULT_PERSON_ID) -> None:
    if value is None:
        conn.execute("DELETE FROM daily_metric WHERE person_id=? AND day=? AND metric_key=?", (person_id, day, key))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO daily_metric (person_id, day, metric_key, value, source) VALUES (?, ?, ?, ?, ?)",
            (person_id, day, key, value, source),
        )


def series(conn, key: str, start: str, end: str, person_id: int = DEFAULT_PERSON_ID) -> dict[str, float]:
    """Values of one metric for start <= day <= end."""
    rows = conn.execute(
        "SELECT day, value FROM daily_metric WHERE person_id=? AND metric_key=? AND day BETWEEN ? AND ?",
        (person_id, key, start, end),
    ).fetchall()
    return {r["day"]: r["value"] for r in rows}


def person(conn, person_id: int = DEFAULT_PERSON_ID) -> dict:
    return dict(conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone())


def set_language(conn, lang: str, person_id: int = DEFAULT_PERSON_ID) -> None:
    with conn:
        conn.execute("UPDATE person SET language = ? WHERE id = ?", (lang, person_id))


def day_values(conn, day: str, person_id: int = DEFAULT_PERSON_ID) -> dict[str, float]:
    return {r["metric_key"]: r["value"] for r in conn.execute(
        "SELECT metric_key, value FROM daily_metric WHERE person_id=? AND day=?", (person_id, day))}


def last_sync(conn, person_id: int = DEFAULT_PERSON_ID) -> str | None:
    r = conn.execute("SELECT MAX(fetched_at) AS t FROM raw_payload WHERE person_id=?", (person_id,)).fetchone()
    return r["t"] if r else None


def days_with_data(conn: sqlite3.Connection, person_id: int = DEFAULT_PERSON_ID) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT day FROM daily_metric WHERE person_id = ? AND source = 'garmin' ORDER BY day DESC",
        (person_id,)
    ).fetchall()
    return [r["day"] for r in rows]
