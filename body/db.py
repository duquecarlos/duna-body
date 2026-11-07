"""SQLite storage. Everything person-specific is data: goals, plans, shifts, metrics.

The schema is the template for a future multi-person product, so every row carries a
person_id. Nothing personal is seeded; only generic metric definitions are.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

_CADENCE_N_PER_WEEK = re.compile(r"^\s*(\d+)\s*x\s*/\s*week\s*$", re.IGNORECASE)

# Shared between SCHEMA (fresh installs) and the rebuild functions below (existing installs),
# so the two paths can never drift apart.
_GOAL_COLUMNS = """
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES person(id),
    hogar_id       TEXT,
    area           TEXT NOT NULL DEFAULT 'body' CHECK (area IN ('body', 'money', 'adventure', 'growth', 'work', 'home')),
    title          TEXT NOT NULL,
    why            TEXT,
    kind           TEXT NOT NULL DEFAULT 'metric' CHECK (kind IN ('metric', 'milestone')),
    metric_key     TEXT REFERENCES metric_def(key),
    direction      TEXT CHECK (direction IN ('raise', 'lower', 'keep') OR direction IS NULL),
    target_value   REAL,
    baseline_value REAL,
    start_day      TEXT NOT NULL,
    deadline       TEXT,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('proposed', 'active', 'paused', 'done', 'dropped')),
    visibility     TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'household')),
    CHECK (
        (kind = 'metric' AND metric_key IS NOT NULL AND direction IS NOT NULL)
        OR
        (kind = 'milestone' AND metric_key IS NULL AND deadline IS NOT NULL)
    )
"""

_HABIT_COLUMNS = """
    id                  INTEGER PRIMARY KEY,
    goal_id             INTEGER NOT NULL REFERENCES goal(id),
    title               TEXT NOT NULL,
    cadence             TEXT NOT NULL CHECK (json_valid(cadence)),
    cadence_text_legacy TEXT,
    assignee_person_id  INTEGER REFERENCES person(id),
    active              INTEGER NOT NULL DEFAULT 1
"""

_GOAL_CHECK_COLUMNS = """
    id           INTEGER PRIMARY KEY,
    goal_id      INTEGER NOT NULL REFERENCES goal(id),
    person_id    INTEGER NOT NULL REFERENCES person(id),
    week_start   TEXT NOT NULL,
    metric_avg   REAL,
    on_track     INTEGER,
    habits_done  INTEGER,
    habits_due   INTEGER,
    rule_version TEXT NOT NULL,
    note         TEXT,
    UNIQUE (goal_id, person_id, week_start)
"""

SCHEMA = f"""
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

-- Goals: metric or milestone, with a plan. vida-v1 shape (dev-protocol/contracts/vida-v1.md).
CREATE TABLE IF NOT EXISTS goal (
{_GOAL_COLUMNS}
);

-- A recurring habit that serves a goal; cadence is JSON, e.g. {{"per": "week", "times": 3}}.
CREATE TABLE IF NOT EXISTS habit (
{_HABIT_COLUMNS}
);

CREATE TABLE IF NOT EXISTS habit_check (
    habit_id  INTEGER NOT NULL REFERENCES habit(id),
    person_id INTEGER NOT NULL REFERENCES person(id),
    day       TEXT NOT NULL,
    done      INTEGER NOT NULL CHECK (done IN (0, 1)),
    note      TEXT,
    PRIMARY KEY (habit_id, person_id, day)
);

-- One row per member per goal per week: the weekly on-track rule's output (RULE_VERSION in body/goals.py).
CREATE TABLE IF NOT EXISTS goal_check (
{_GOAL_CHECK_COLUMNS}
);

-- A scheduled thing to do, maybe tied to a goal. vida-v1 shape.
CREATE TABLE IF NOT EXISTS plan (
    id        INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    hogar_id  TEXT,
    goal_id   INTEGER REFERENCES goal(id),
    day       TEXT NOT NULL,
    time      TEXT,
    title     TEXT NOT NULL,
    booking   TEXT CHECK (booking IS NULL OR json_valid(booking)),
    status    TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'accepted', 'done', 'skipped'))
);

-- How much health data the assistant's tools may read for this person (wave 7, stage one).
CREATE TABLE IF NOT EXISTS ajuste_agente (
    person_id  INTEGER PRIMARY KEY REFERENCES person(id),
    nivel_datos TEXT NOT NULL DEFAULT 'completo' CHECK (nivel_datos IN ('completo', 'resumen', 'ninguno')),
    updated_at TEXT NOT NULL
);

-- A signed-in session (Cuenta Duna, wave 8). Only a hash of the session token lives here -- the
-- token itself is never stored.
CREATE TABLE IF NOT EXISTS sesion (
    id_hash      TEXT PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES person(id),
    cuenta_sid   TEXT,                  -- Cuenta's ID token `sid`, so back-channel logout can find this row
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sesion_cuenta_sid ON sesion(cuenta_sid);
CREATE INDEX IF NOT EXISTS idx_sesion_person_id ON sesion(person_id);

-- One Cuenta sign-in attempt, minutes long and single use. Only a hash of `state` lives here --
-- like the session token above, the state itself is never stored.
CREATE TABLE IF NOT EXISTS inicio_sesion (
    state_hash    TEXT PRIMARY KEY,
    nonce         TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    return_to     TEXT,
    created_at    TEXT NOT NULL
);

-- One accepted consent text, by version, per person (sensitive-data amendment rule 6).
CREATE TABLE IF NOT EXISTS consentimiento (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES person(id),
    version     TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    UNIQUE (person_id, version)
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

DEFAULT_PERSON_ID = 1  # the local person's id; nowhere outside this module (local_person_id() is the seam)


def local_person_id(conn: sqlite3.Connection) -> int:
    """The person a local install (no Cuenta sign-in) resolves to -- the row init() ensures exists."""
    return DEFAULT_PERSON_ID


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
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(person)")}
    if pcols and "language" not in pcols:
        conn.execute("ALTER TABLE person ADD COLUMN language TEXT NOT NULL DEFAULT 'es'")
    if pcols and "cuenta_sub" not in pcols:
        conn.execute("ALTER TABLE person ADD COLUMN cuenta_sub TEXT")
    if pcols:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS person_cuenta_sub ON person(cuenta_sub) WHERE cuenta_sub IS NOT NULL"
        )
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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _goal_needs_rebuild(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'goal'").fetchone()
    return row is not None and "'proposed'" not in row[0]


def _map_legacy_cadence(cadence_text: str | None) -> dict:
    """plan_item.cadence was free text ('daily', '3x/week', ...); habit.cadence is JSON (vida-v1)."""
    if cadence_text:
        if cadence_text.strip().lower() == "daily":
            return {"per": "day", "times": 1}
        m = _CADENCE_N_PER_WEEK.match(cadence_text)
        if m:
            return {"per": "week", "times": int(m.group(1))}
    return {"per": "week", "times": 1}


def _copy_goal_row(conn: sqlite3.Connection, r: sqlite3.Row, old_cols: set, better_by_key: dict) -> None:
    metric_key = r["metric_key"] if "metric_key" in old_cols else None
    if metric_key is not None:
        kind = "metric"
        direction = {"higher": "raise", "lower": "lower", "neutral": "keep"}.get(better_by_key.get(metric_key),
                                                                                   "keep")
    else:
        kind, direction = "milestone", None
    conn.execute(
        "INSERT INTO goal_new (id, person_id, hogar_id, area, title, why, kind, metric_key, direction, "
        "target_value, baseline_value, start_day, deadline, status, visibility) "
        "VALUES (?, ?, ?, 'body', ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'private')",
        (r["id"], r["person_id"], r["hogar_id"] if "hogar_id" in old_cols else None, r["title"], kind, metric_key,
         direction, r["target_value"], r["baseline_value"], r["start_day"], r["deadline"], r["status"]),
    )


def _copy_habit_row(conn: sqlite3.Connection, r: sqlite3.Row, old_cols: set) -> None:
    cadence_text = r["cadence"] if "cadence" in old_cols else None
    shift_context = r["shift_context"] if "shift_context" in old_cols else None
    cadence = _map_legacy_cadence(cadence_text)
    if shift_context:
        cadence["context"] = shift_context
    conn.execute(
        "INSERT INTO habit (id, goal_id, title, cadence, cadence_text_legacy, assignee_person_id, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (r["id"], r["goal_id"], r["title"], json.dumps(cadence), cadence_text,
         r["assignee_person_id"] if "assignee_person_id" in old_cols else None, r["active"]),
    )


def _copy_goal_check_row(conn: sqlite3.Connection, r: sqlite3.Row, goal_person: dict) -> None:
    conn.execute(
        "INSERT INTO goal_check (id, goal_id, person_id, week_start, metric_avg, on_track, habits_done, "
        "habits_due, rule_version, note) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'legacy', ?)",
        (r["id"], r["goal_id"], goal_person.get(r["goal_id"]), r["week_start"], r["metric_avg"], r["on_track"],
         r["note"]),
    )


def _rebuild_with_fk_off(conn: sqlite3.Connection, step) -> None:
    """Run `step(conn)` as one transaction with foreign keys off (SQLite's 12-step ALTER procedure),
    then check for violations before committing; on any failure, roll back and raise so the old
    tables are left untouched."""
    fk_was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    try:
        step(conn)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"rebuild left foreign key violations: {violations}")
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_was_on else 'OFF'}")


def _rebuild_goal_table(conn: sqlite3.Connection) -> None:
    def step(conn):
        better_by_key = {r["key"]: r["better"] for r in conn.execute("SELECT key, better FROM metric_def")}
        old_cols = {r[1] for r in conn.execute("PRAGMA table_info(goal)")}
        old_rows = conn.execute("SELECT * FROM goal").fetchall()
        conn.execute(f"CREATE TABLE goal_new ({_GOAL_COLUMNS})")
        for r in old_rows:
            _copy_goal_row(conn, r, old_cols, better_by_key)
        new_count = conn.execute("SELECT COUNT(*) FROM goal_new").fetchone()[0]
        if new_count != len(old_rows):
            raise RuntimeError(f"goal rebuild count mismatch: {len(old_rows)} -> {new_count}")
        conn.execute("DROP TABLE goal")
        conn.execute("ALTER TABLE goal_new RENAME TO goal")
    _rebuild_with_fk_off(conn, step)


def _rebuild_habit_table(conn: sqlite3.Connection) -> None:
    def step(conn):
        old_cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_item)")}
        old_rows = conn.execute("SELECT * FROM plan_item").fetchall()
        conn.execute(f"CREATE TABLE habit ({_HABIT_COLUMNS})")
        for r in old_rows:
            _copy_habit_row(conn, r, old_cols)
        new_count = conn.execute("SELECT COUNT(*) FROM habit").fetchone()[0]
        if new_count != len(old_rows):
            raise RuntimeError(f"habit rebuild count mismatch: {len(old_rows)} -> {new_count}")
        conn.execute("DROP TABLE plan_item")
    _rebuild_with_fk_off(conn, step)


def _rebuild_goal_check_table(conn: sqlite3.Connection) -> None:
    def step(conn):
        old_rows = conn.execute("SELECT * FROM weekly_check").fetchall()
        goal_person = {r["id"]: r["person_id"] for r in conn.execute("SELECT id, person_id FROM goal")}
        conn.execute(f"CREATE TABLE goal_check ({_GOAL_CHECK_COLUMNS})")
        for r in old_rows:
            _copy_goal_check_row(conn, r, goal_person)
        new_count = conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0]
        if new_count != len(old_rows):
            raise RuntimeError(f"goal_check rebuild count mismatch: {len(old_rows)} -> {new_count}")
        conn.execute("DROP TABLE weekly_check")
    _rebuild_with_fk_off(conn, step)


def _migrate_metas(conn: sqlite3.Connection) -> None:
    """goal/plan_item/weekly_check -> vida-v1's goal/habit/goal_check shapes (BODY-METAS-1).

    Each rebuild runs guarded (detects the old shape) so it fires once; on a fresh install none of
    the old tables exist and SCHEMA below creates the new shapes directly.
    """
    if _goal_needs_rebuild(conn):
        _rebuild_goal_table(conn)
    if _table_exists(conn, "plan_item") and not _table_exists(conn, "habit"):
        _rebuild_habit_table(conn)
    if _table_exists(conn, "weekly_check") and not _table_exists(conn, "goal_check"):
        _rebuild_goal_check_table(conn)


def init(conn: sqlite3.Connection) -> None:
    _migrate_metas(conn)
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


def save_day(conn: sqlite3.Connection, day_data, person_id: int) -> None:
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


def load_day(conn: sqlite3.Connection, day: str, person_id: int) -> dict:
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


def set_metric(conn, day: str, key: str, value, source: str, person_id: int) -> None:
    if value is None:
        conn.execute("DELETE FROM daily_metric WHERE person_id=? AND day=? AND metric_key=?", (person_id, day, key))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO daily_metric (person_id, day, metric_key, value, source) VALUES (?, ?, ?, ?, ?)",
            (person_id, day, key, value, source),
        )


def series(conn, key: str, start: str, end: str, person_id: int) -> dict[str, float]:
    """Values of one metric for start <= day <= end."""
    rows = conn.execute(
        "SELECT day, value FROM daily_metric WHERE person_id=? AND metric_key=? AND day BETWEEN ? AND ?",
        (person_id, key, start, end),
    ).fetchall()
    return {r["day"]: r["value"] for r in rows}


def person(conn, person_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone())


def set_language(conn, lang: str, person_id: int) -> None:
    with conn:
        conn.execute("UPDATE person SET language = ? WHERE id = ?", (lang, person_id))


def day_values(conn, day: str, person_id: int) -> dict[str, float]:
    return {r["metric_key"]: r["value"] for r in conn.execute(
        "SELECT metric_key, value FROM daily_metric WHERE person_id=? AND day=?", (person_id, day))}


def last_sync(conn, person_id: int) -> str | None:
    r = conn.execute("SELECT MAX(fetched_at) AS t FROM raw_payload WHERE person_id=?", (person_id,)).fetchone()
    return r["t"] if r else None


def days_with_data(conn: sqlite3.Connection, person_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT day FROM daily_metric WHERE person_id = ? AND source = 'garmin' ORDER BY day DESC",
        (person_id,)
    ).fetchall()
    return [r["day"] for r in rows]
