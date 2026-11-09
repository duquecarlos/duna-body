"""SQLite storage. Everything person-specific is data: goals, plans, shifts, metrics.

The schema is the template for a future multi-person product, so every row carries a
person_id. Nothing personal is seeded; only generic metric definitions are.
"""
import json
import os
import re
import sqlite3
import threading
import uuid
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

_PROPUESTA_AGENTE_COLUMNS = """
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT
"""

# workout_set keys to activity's composite key (person_id, source, source_id): the watch's values
# and the person's corrections live in separate columns so neither overwrites the other
# (BODY-ENTRENO-1-INV). Weight value and unit are NULL together, never defaulted to 0.
_WORKOUT_SET_COLUMNS = """
    id                  INTEGER PRIMARY KEY,
    person_id           INTEGER NOT NULL REFERENCES person(id),
    activity_source     TEXT NOT NULL,
    activity_source_id  TEXT NOT NULL,
    set_order           INTEGER NOT NULL,
    set_type            TEXT NOT NULL CHECK (set_type IN ('active', 'rest')),
    garmin_category     TEXT,
    garmin_exercise     TEXT,
    garmin_reps         INTEGER,
    garmin_weight_value REAL,
    garmin_weight_unit  TEXT,
    garmin_duration_s   REAL,
    garmin_start_local  TEXT,
    person_category     TEXT,
    person_exercise     TEXT,
    person_reps         INTEGER,
    person_weight_kg    REAL,
    person_note         TEXT,
    corrected_by_person INTEGER NOT NULL DEFAULT 0 CHECK (corrected_by_person IN (0, 1)),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CHECK ((garmin_weight_value IS NULL) = (garmin_weight_unit IS NULL)),
    FOREIGN KEY (person_id, activity_source, activity_source_id)
        REFERENCES activity(person_id, source, source_id),
    UNIQUE (person_id, activity_source, activity_source_id, set_order)
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

# cuenta-duna-v1 (BODY-CUENTA-RP-1): keyed by a hash of the `flujo` cookie value, not of OAuth
# `state` -- the cookie is the single-use handle the browser carries; `state` is just one of the
# values stored under it.
_INICIO_SESION_COLUMNS = """
    flujo_hash    TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    nonce         TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    return_to     TEXT,
    created_at    TEXT NOT NULL
"""

# Wave 9 (BODY-EXPERIMENTOS-ESQUEMA-1, BODY-EXPERIMENTOS-1-INV slice 3): an experiment is its own
# concept, only optionally linked to a habit (habito_id) -- zero change to goal/habit. ventana and
# filtro_dias are JSON (the day filter as data, generalising turnos_costo._dias_excluidos),
# validated once by db.validar_experimento before a row is ever written.
_EXPERIMENTO_COLUMNS = """
    id                   INTEGER PRIMARY KEY,
    person_id            INTEGER NOT NULL REFERENCES person(id),
    titulo               TEXT NOT NULL,
    habito_id            INTEGER REFERENCES habit(id),
    metricas             TEXT NOT NULL CHECK (json_valid(metricas)),
    ventana              TEXT NOT NULL CHECK (json_valid(ventana)),
    filtro_dias          TEXT NOT NULL CHECK (json_valid(filtro_dias)),
    dias_prueba_objetivo INTEGER,
    fecha_inicio         TEXT NOT NULL,
    fecha_fin            TEXT,
    estado               TEXT NOT NULL CHECK (estado IN ('activo', 'cerrado')),
    creado_en            TEXT NOT NULL
"""

# The "it happened" log for an experiment with no habito_id -- with one, habit_check is the log
# instead. One row per (experimento_id, day): registrar_dia_experimento upserts on that pair.
_EXPERIMENTO_REGISTRO_COLUMNS = """
    id             INTEGER PRIMARY KEY,
    experimento_id INTEGER NOT NULL REFERENCES experimento(id),
    person_id      INTEGER NOT NULL REFERENCES person(id),
    day            TEXT NOT NULL,
    ocurrio        INTEGER NOT NULL CHECK (ocurrio IN (0, 1)),
    nota           TEXT,
    momento_id     INTEGER REFERENCES moment(id),
    creado_en      TEXT NOT NULL,
    UNIQUE (experimento_id, day)
"""

# A timestamped thing the person did. `kind` is open text on purpose (no CHECK) -- a code-level
# known set comes later, once more than the two hand-run experiments exist to generalise from.
_MOMENT_COLUMNS = """
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES person(id),
    ts_local   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS person (
    id          INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    timezone    TEXT NOT NULL DEFAULT 'America/Bogota',
    language    TEXT NOT NULL DEFAULT 'es',
    -- NULL = not answered yet, 0 = no, 1 = yes (BODY-TURNOS-ESQUEMA-1 ruling (a)): a screen can tell
    -- "never asked" from "said no", and Turnos shows only when this is 1.
    trabaja_turnos INTEGER CHECK (trabaja_turnos IN (0, 1))
);

-- What can be measured. Generic definitions, shared by every person. `origen` (BODY-ENTRENO-
-- SCHEMA-1) is 'garmin' for anything the watch supplies and 'self' for a figure the person
-- measures themself (e.g. a flexibility reach, a snoring minutes-per-night from another app).
CREATE TABLE IF NOT EXISTS metric_def (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    unit    TEXT NOT NULL,
    better  TEXT NOT NULL CHECK (better IN ('higher', 'lower', 'neutral')),
    kind    TEXT NOT NULL DEFAULT 'measured' CHECK (kind IN ('measured', 'estimated')),
    origen  TEXT NOT NULL DEFAULT 'garmin' CHECK (origen IN ('garmin', 'self'))
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

-- One row per strength-training set within an activity (BODY-ENTRENO-1-INV, BODY-ENTRENO-
-- SCHEMA-1): keyed to `activity`'s composite key, since a set belongs to one recorded or manual
-- session. A manual session (the watch recorded nothing) is an `activity` row with source =
-- 'manual' -- allowed, since `activity` has no CHECK on `source`.
CREATE TABLE IF NOT EXISTS workout_set (
{_WORKOUT_SET_COLUMNS}
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
    -- 'import': extracted from another app's file (the only kind before BODY-TURNOS-ESQUEMA-1).
    -- 'pattern': expanded from the person's own hand-entered shift_pattern (slice 5).
    kind            TEXT NOT NULL DEFAULT 'import' CHECK (kind IN ('import', 'pattern')),
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

-- One row per person: the hand-entered repeating cycle (slice 5 expands it into proposed
-- shift_batch rows, the same propose-then-confirm flow as a file import). `cycle` is a JSON array
-- of {"kind", "start", "end"} entries, one per day of the cycle; `kind` is from the
-- same vocabulary as shift.kind -- 'morning', 'afternoon', 'night', 'day', 'off', 'other'
-- (body/shift_import.py's VALID_KINDS); `start`/`end` are "HH:MM" strings, null on an `off` day.
-- Validated and expanded by body/patron_turnos.py.
CREATE TABLE IF NOT EXISTS shift_pattern (
    person_id     INTEGER PRIMARY KEY REFERENCES person(id),
    start_day     TEXT NOT NULL,
    cycle         TEXT NOT NULL CHECK (json_valid(cycle)),
    horizon_weeks INTEGER NOT NULL DEFAULT 8 CHECK (horizon_weeks BETWEEN 1 AND 26),
    updated_at    TEXT NOT NULL
);

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

-- A goal can point at where the rest of its plan lives in another Duna app, instead of copying it
-- (DESIGN rule 17; BODY-HOGAR-ENLACE-1). `app` is a key into the registry in body/enlaces.py, the
-- only module that reads or writes this table -- NOT CHECK-constrained, because the registry in
-- code is the authority: an app key no longer known there is simply not rendered. One link per app
-- per goal (UNIQUE), never a title or URL copied from the other app -- only its opaque `ref`.
CREATE TABLE IF NOT EXISTS goal_enlace (
    id         INTEGER PRIMARY KEY,
    goal_id    INTEGER NOT NULL REFERENCES goal(id),
    app        TEXT NOT NULL,
    ref        TEXT NOT NULL,
    creado_at  TEXT NOT NULL,
    UNIQUE (goal_id, app)
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
    status    TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'accepted', 'done', 'skipped')),
    -- A planned training session's exercise list (BODY-ENTRENO-SCHEMA-1); JSON, shape owned by
    -- the tool that writes it -- nothing here yet.
    ejercicios_json TEXT CHECK (ejercicios_json IS NULL OR json_valid(ejercicios_json))
);

-- How much health data the assistant's tools may read for this person (wave 7, stage one).
CREATE TABLE IF NOT EXISTS ajuste_agente (
    person_id  INTEGER PRIMARY KEY REFERENCES person(id),
    nivel_datos TEXT NOT NULL DEFAULT 'completo' CHECK (nivel_datos IN ('completo', 'resumen', 'ninguno')),
    proveedor  TEXT,
    updated_at TEXT NOT NULL
);

-- Conversations with Body's assistant (wave 7, stage two: BODY-ASISTENTE-PROVEEDOR-1).
CREATE TABLE IF NOT EXISTS conversacion_agente (
    id        INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    creada_en TEXT NOT NULL,
    titulo    TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversacion_agente_person ON conversacion_agente(person_id);

-- One row per turn (question or answer, including tool calls and their results -- the person's
-- own data, kept so the history sent back to the provider can be rebuilt server-side).
CREATE TABLE IF NOT EXISTS mensaje_agente (
    id              INTEGER PRIMARY KEY,
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    person_id       INTEGER NOT NULL REFERENCES person(id),
    rol             TEXT NOT NULL CHECK (rol IN ('usuario', 'asistente')),
    contenido_json  TEXT NOT NULL CHECK (json_valid(contenido_json)),
    creado_en       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mensaje_agente_conv ON mensaje_agente(conversacion_id);

-- One row per assistant question, including failed ones (agent-capabilities-v1 section 6,
-- amendment 2: entrada/cache_lectura/cache_escritura are DISJOINT token counts).
CREATE TABLE IF NOT EXISTS uso_agente (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    creado_en       TEXT NOT NULL,
    proveedor       TEXT NOT NULL,
    modelo          TEXT NOT NULL,
    entrada         INTEGER NOT NULL DEFAULT 0,
    salida          INTEGER NOT NULL DEFAULT 0,
    cache_lectura   INTEGER NOT NULL DEFAULT 0,
    cache_escritura INTEGER NOT NULL DEFAULT 0,
    rondas          INTEGER NOT NULL DEFAULT 0,
    motivo_fin      TEXT,
    codigo_error    TEXT,
    es_pregunta     INTEGER NOT NULL DEFAULT 1 CHECK (es_pregunta IN (0, 1)),
    superficie      TEXT NOT NULL DEFAULT 'asistente'
);
CREATE INDEX IF NOT EXISTS idx_uso_agente_person_dia ON uso_agente(person_id, creado_en);

-- Prices are DATA, seeded empty -- never invented (amendment 5: nothing is costed at 0 because
-- its price is missing; an unpriced model falls back to the count breaker instead).
-- `desde` (BODY-MODELOS-ESTANDAR-1 decision 2) makes a price DATED: a row applies from its date
-- until the next row's date for the same (proveedor, modelo, tipo) -- one column, so it can never
-- leave a gap or an overlap the way a desde/hasta pair could.
CREATE TABLE IF NOT EXISTS precio_modelo (
    proveedor            TEXT NOT NULL,
    modelo               TEXT NOT NULL,
    tipo                 TEXT NOT NULL CHECK (tipo IN ('entrada', 'salida', 'cache_lectura', 'cache_escritura')),
    desde                TEXT NOT NULL DEFAULT '2000-01-01',
    micro_usd_por_millon INTEGER NOT NULL,
    PRIMARY KEY (proveedor, modelo, tipo, desde)
);

-- Per-person overrides of the daily ceilings, both nullable (NULL = use the env default).
CREATE TABLE IF NOT EXISTS tope_agente (
    person_id        INTEGER PRIMARY KEY REFERENCES person(id),
    diario_micro_usd INTEGER,
    conteo_diario    INTEGER
);

-- A signed-in session (Cuenta Duna, wave 8). Only a hash of the session token lives here -- the
-- token itself is never stored.
CREATE TABLE IF NOT EXISTS sesion (
    id_hash      TEXT PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES person(id),
    cuenta_sid   TEXT,                  -- Cuenta's ID token `sid`, so back-channel logout can find this row
    auth_time    TEXT,                  -- ISO UTC, from the ID token's `auth_time`; NULL = stale (BODY-BORRAR-REAUTH-1)
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sesion_cuenta_sid ON sesion(cuenta_sid);
CREATE INDEX IF NOT EXISTS idx_sesion_person_id ON sesion(person_id);

-- One Cuenta sign-in attempt, minutes long and single use. Keyed by a hash of the `flujo` cookie
-- value the browser carries -- like the session token above, neither the cookie value nor the
-- OAuth `state` it stores is kept in the clear.
CREATE TABLE IF NOT EXISTS inicio_sesion (
{_INICIO_SESION_COLUMNS}
);

-- Every back-channel logout `jti` ever accepted, kept until its own `exp` so a replay is refused
-- even across a restart (cuenta-duna-v1's back-channel logout check `jti`).
CREATE TABLE IF NOT EXISTS logout_jti (
    jti TEXT PRIMARY KEY,
    exp INTEGER NOT NULL
);

-- One accepted consent text, by version, per person (sensitive-data amendment rule 6).
CREATE TABLE IF NOT EXISTS consentimiento (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES person(id),
    version     TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    UNIQUE (person_id, version)
);

-- Each hosted person's Garmin token, encrypted at rest (BODY-GARMIN-HOST-1). ultimo_error holds
-- an error CLASS name only (e.g. 'NotSignedIn'), never a message. backfill_hasta (BODY-PRIMER-
-- PULL-1) is the oldest day --backfill has already attempted for this person; NULL = not started.
CREATE TABLE IF NOT EXISTS garmin_cuenta (
    person_id      INTEGER PRIMARY KEY REFERENCES person(id),
    token_cifrado  BLOB NOT NULL,
    nonce          BLOB NOT NULL,
    conectado_en   TEXT NOT NULL,
    ultimo_error   TEXT,
    backfill_hasta TEXT
);

-- A proposal the assistant made: nothing on the target changes until the person taps Aplicar in
-- the chat (agent-capabilities-v1 section 2 + amendment 1, BODY-ASISTENTE-PROPUESTAS-1).
CREATE TABLE IF NOT EXISTS propuesta_agente (
{_PROPUESTA_AGENTE_COLUMNS}
);
CREATE INDEX IF NOT EXISTS idx_propuesta_agente_conv ON propuesta_agente(conversacion_id);
CREATE INDEX IF NOT EXISTS idx_propuesta_agente_person ON propuesta_agente(person_id);

-- One device key per linked PC, carrying explicit scopes (BODY-DISPOSITIVO-1). Only the key's
-- sha256 hex ever reaches this table -- the plain key is shown once, at creation, and never again.
CREATE TABLE IF NOT EXISTS dispositivo (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES person(id),
    nombre      TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    alcances    TEXT NOT NULL,
    creado_en   TEXT NOT NULL,
    usado_en    TEXT,
    revocado_en TEXT
);
CREATE INDEX IF NOT EXISTS idx_dispositivo_person ON dispositivo(person_id);

-- One figure a person has chosen to share with one household, with its own history (hogar-v1:
-- "sharing is per figure, chosen by its owner, and can be withdrawn"; BODY-HOGAR-COMPARTIR-1).
-- Withdrawing sets withdrawn_at rather than deleting the row, so the person can see what they
-- shared and when; re-sharing inserts a NEW row. hogar_id is TEXT, matching goal.hogar_id and
-- hogar_miembro.hogar_id (body/household.py's Household.id is also typed str).
CREATE TABLE IF NOT EXISTS figura_compartida (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES person(id),
    metric_key   TEXT NOT NULL REFERENCES metric_def(key),
    hogar_id     TEXT NOT NULL,
    shared_at    TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_figura_compartida_activa
    ON figura_compartida (person_id, metric_key, hogar_id) WHERE withdrawn_at IS NULL;

-- Wave 9 (BODY-EXPERIMENTOS-ESQUEMA-1): experimento, moment and experimento_registro. See the
-- _EXPERIMENTO_COLUMNS/_MOMENT_COLUMNS/_EXPERIMENTO_REGISTRO_COLUMNS comments above for why.
CREATE TABLE IF NOT EXISTS moment (
{_MOMENT_COLUMNS}
);
CREATE INDEX IF NOT EXISTS idx_moment_person ON moment(person_id);

CREATE TABLE IF NOT EXISTS experimento (
{_EXPERIMENTO_COLUMNS}
);
CREATE INDEX IF NOT EXISTS idx_experimento_person ON experimento(person_id);

CREATE TABLE IF NOT EXISTS experimento_registro (
{_EXPERIMENTO_REGISTRO_COLUMNS}
);
CREATE INDEX IF NOT EXISTS idx_experimento_registro_person ON experimento_registro(person_id);
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
    ("vo2max", "VO2 max", "ml/kg/min", "higher"),
    ("vo2max_cycling", "VO2 max (cycling)", "ml/kg/min", "higher"),
    ("fitness_age", "Fitness age", "years", "lower"),
    # BODY-ENTRENO-SUENO-STORE-1: sleep SpO2/respiration, parsed by BODY-ENTRENO-SUENO-PARSE-1 into
    # DayData fields of the same name (not DayData.metrics -- save_day reads them separately below).
    # No single "better" direction for a breathing rate, only a normal band -- "neutral", like
    # active_kcal. No screen shows these yet.
    ("sleep_spo2_avg", "Blood oxygen at night (avg)", "%", "higher"),
    ("sleep_respiration_avg", "Breathing at night (avg)", "brpm", "neutral"),
    ("sleep_respiration_low", "Breathing at night (low)", "brpm", "neutral"),
    ("sleep_respiration_high", "Breathing at night (high)", "brpm", "neutral"),
]
METRIC_DEFS = [d if len(d) == 5 else (*d, "measured") for d in METRIC_DEFS]
METRIC_DEFS = [d if len(d) == 6 else (*d, "garmin") for d in METRIC_DEFS]

# Generic self-measured metric_def rows (BODY-ENTRENO-SCHEMA-1): no person's values, just the
# definitions a future tool slice can write to. Labels are added to body/i18n/*.json by that
# tool slice -- i18n is not in this slice's touches.
METRIC_DEFS += [
    ("flex_alcance_cm", "Flexibility reach", "cm", "lower", "measured", "self"),
    ("snoring_min", "Snoring", "min", "lower", "measured", "self"),
]

DEFAULT_PERSON_ID = 1  # the local person's id; nowhere outside this module (local_person_id() is the seam)


def local_person_id(conn: sqlite3.Connection) -> int:
    """The person a local install (no Cuenta sign-in) resolves to -- the row init() used to ensure
    exists on every request. Since ensure_ready() (below) stopped re-running init() on every
    request (BODY-RENDIMIENTO-1), a local account deletion (privacidad.delete_person) can remove
    this row and nothing would otherwise recreate it before the next request needs it. SELECT
    first, INSERT only if absent, so the normal path (the row already exists) writes nothing."""
    row = conn.execute("SELECT 1 FROM person WHERE id = ?", (DEFAULT_PERSON_ID,)).fetchone()
    if row is None:
        with conn:
            conn.execute("INSERT OR IGNORE INTO person (id, display_name) VALUES (?, 'Me')", (DEFAULT_PERSON_ID,))
    return DEFAULT_PERSON_ID


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_home()
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A reader that collides with the pull's or the assistant's write used to fail immediately
    # (SQLite's default busy behaviour): wait up to 5s for the writer to finish instead
    # (BODY-RENDIMIENTO-1, BODY-RENDIMIENTO-OBS-1). Stores nothing in the file; not a journal-mode
    # change.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(metric_def)")}
    if cols and "kind" not in cols:
        conn.execute("ALTER TABLE metric_def ADD COLUMN kind TEXT NOT NULL DEFAULT 'measured'")
    if cols and "origen" not in cols:
        conn.execute(
            "ALTER TABLE metric_def ADD COLUMN origen TEXT NOT NULL DEFAULT 'garmin' "
            "CHECK (origen IN ('garmin', 'self'))"
        )
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(person)")}
    if pcols and "language" not in pcols:
        conn.execute("ALTER TABLE person ADD COLUMN language TEXT NOT NULL DEFAULT 'es'")
    if pcols and "cuenta_sub" not in pcols:
        conn.execute("ALTER TABLE person ADD COLUMN cuenta_sub TEXT")
    if pcols and "trabaja_turnos" not in pcols:
        conn.execute("ALTER TABLE person ADD COLUMN trabaja_turnos INTEGER CHECK (trabaja_turnos IN (0, 1))")
        # Backfill ONCE, inside this same branch (ruling (b)): it runs exactly when the column is
        # first added, never again, so a person who later answers "no" is never flipped back to 1
        # by a restart. Every person with a row in shift or shift_batch already has shift data,
        # which is Carlos's own Turnos exactly as today.
        conn.execute(
            "UPDATE person SET trabaja_turnos = 1 WHERE id IN ("
            "SELECT person_id FROM shift UNION SELECT person_id FROM shift_batch)"
        )
    if pcols:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS person_cuenta_sub ON person(cuenta_sub) WHERE cuenta_sub IS NOT NULL"
        )
    conn.execute("UPDATE person SET timezone = 'America/Bogota' WHERE timezone = 'Europe/Madrid'")
    sescols = {r[1] for r in conn.execute("PRAGMA table_info(sesion)")}
    if sescols and "auth_time" not in sescols:
        conn.execute("ALTER TABLE sesion ADD COLUMN auth_time TEXT")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(shift)")}
    if scols and "batch_id" not in scols:
        conn.execute("ALTER TABLE shift ADD COLUMN batch_id INTEGER REFERENCES shift_batch(id)")
    acols = {r[1] for r in conn.execute("PRAGMA table_info(ajuste_agente)")}
    if acols and "proveedor" not in acols:
        conn.execute("ALTER TABLE ajuste_agente ADD COLUMN proveedor TEXT")
    gcols = {r[1] for r in conn.execute("PRAGMA table_info(garmin_cuenta)")}
    if gcols and "backfill_hasta" not in gcols:
        conn.execute("ALTER TABLE garmin_cuenta ADD COLUMN backfill_hasta TEXT")
    plcols = {r[1] for r in conn.execute("PRAGMA table_info(plan)")}
    if plcols and "ejercicios_json" not in plcols:
        conn.execute(
            "ALTER TABLE plan ADD COLUMN ejercicios_json TEXT "
            "CHECK (ejercicios_json IS NULL OR json_valid(ejercicios_json))"
        )
    sbcols = {r[1] for r in conn.execute("PRAGMA table_info(shift_batch)")}
    if sbcols and "kind" not in sbcols:
        conn.execute(
            "ALTER TABLE shift_batch ADD COLUMN kind TEXT NOT NULL DEFAULT 'import' "
            "CHECK (kind IN ('import', 'pattern'))"
        )
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


def _precio_modelo_needs_desde(conn: sqlite3.Connection) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(precio_modelo)")}
    return bool(cols) and "desde" not in cols


def _rebuild_precio_modelo_table(conn: sqlite3.Connection) -> None:
    """precio_modelo gains `desde` as part of its primary key (BODY-MODELOS-ESTANDAR-1 decision 2):
    SQLite cannot ALTER a primary key, so this rebuilds the table in one transaction, the same
    guarded shape as the goal/habit/goal_check and inicio_sesion rebuilds above. Every existing row
    -- including an operator's own hand-set price -- gets desde '2000-01-01', never lost."""
    def step(conn):
        old_rows = conn.execute("SELECT * FROM precio_modelo").fetchall()
        conn.execute(
            "CREATE TABLE precio_modelo_nuevo ("
            "proveedor TEXT NOT NULL, modelo TEXT NOT NULL, "
            "tipo TEXT NOT NULL CHECK (tipo IN ('entrada', 'salida', 'cache_lectura', 'cache_escritura')), "
            "desde TEXT NOT NULL DEFAULT '2000-01-01', micro_usd_por_millon INTEGER NOT NULL, "
            "PRIMARY KEY (proveedor, modelo, tipo, desde))"
        )
        for r in old_rows:
            conn.execute(
                "INSERT INTO precio_modelo_nuevo (proveedor, modelo, tipo, desde, micro_usd_por_millon) "
                "VALUES (?, ?, ?, '2000-01-01', ?)",
                (r["proveedor"], r["modelo"], r["tipo"], r["micro_usd_por_millon"]),
            )
        new_count = conn.execute("SELECT COUNT(*) FROM precio_modelo_nuevo").fetchone()[0]
        if new_count != len(old_rows):
            raise RuntimeError(f"precio_modelo rebuild count mismatch: {len(old_rows)} -> {new_count}")
        conn.execute("DROP TABLE precio_modelo")
        conn.execute("ALTER TABLE precio_modelo_nuevo RENAME TO precio_modelo")
    _rebuild_with_fk_off(conn, step)


def _propuesta_agente_needs_rebuild(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'propuesta_agente'"
    ).fetchone()
    return row is not None and "planear_entreno" not in row[0]


def _rebuild_propuesta_agente_table(conn: sqlite3.Connection) -> None:
    """propuesta_agente's `accion` CHECK gains the training/measure/habit tools' action names
    (BODY-ENTRENO-SCHEMA-1); every other column is unchanged, so this is a straight column-for-
    column copy, the same guarded rebuild shape as precio_modelo/goal above. Every existing row
    survives byte-for-byte."""
    def step(conn):
        old_rows = conn.execute("SELECT * FROM propuesta_agente").fetchall()
        conn.execute(f"CREATE TABLE propuesta_agente_nuevo ({_PROPUESTA_AGENTE_COLUMNS})")
        for r in old_rows:
            conn.execute(
                "INSERT INTO propuesta_agente_nuevo (id, person_id, conversacion_id, accion, objetivo_id, "
                "antes_json, despues_json, estado, creada_en, expira_en, resuelta_en) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r["person_id"], r["conversacion_id"], r["accion"], r["objetivo_id"], r["antes_json"],
                 r["despues_json"], r["estado"], r["creada_en"], r["expira_en"], r["resuelta_en"]),
            )
        new_count = conn.execute("SELECT COUNT(*) FROM propuesta_agente_nuevo").fetchone()[0]
        if new_count != len(old_rows):
            raise RuntimeError(f"propuesta_agente rebuild count mismatch: {len(old_rows)} -> {new_count}")
        conn.execute("DROP TABLE propuesta_agente")
        conn.execute("ALTER TABLE propuesta_agente_nuevo RENAME TO propuesta_agente")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_propuesta_agente_conv ON propuesta_agente(conversacion_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_propuesta_agente_person ON propuesta_agente(person_id)")
    _rebuild_with_fk_off(conn, step)


def _migrate_propuestas(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "propuesta_agente") and _propuesta_agente_needs_rebuild(conn):
        _rebuild_propuesta_agente_table(conn)


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


def _inicio_sesion_needs_rebuild(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'inicio_sesion'"
    ).fetchone()
    return row is not None and "flujo_hash" not in row[0]


def _rebuild_inicio_sesion_table(conn: sqlite3.Connection) -> None:
    """BODY-CUENTA-SCHEMA-1's `inicio_sesion` (keyed by `state_hash`) -> BODY-CUENTA-RP-1's shape
    (keyed by `flujo_hash`, carrying `state` itself). This table has never been written by any
    shipped code, so the count compare below is expected to find 0 rows -- never a DELETE, the
    same guard the goal/habit/goal_check rebuilds above use."""
    def step(conn):
        old_rows = conn.execute("SELECT * FROM inicio_sesion").fetchall()
        if old_rows:
            raise RuntimeError(f"inicio_sesion rebuild expected 0 rows, found {len(old_rows)}")
        conn.execute("DROP TABLE inicio_sesion")
        conn.execute(f"CREATE TABLE inicio_sesion ({_INICIO_SESION_COLUMNS})")
    _rebuild_with_fk_off(conn, step)


def _migrate_cuenta(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "inicio_sesion") and _inicio_sesion_needs_rebuild(conn):
        _rebuild_inicio_sesion_table(conn)


def _ensure_hogar_miembro(conn: sqlite3.Connection) -> None:
    """Creates `hogar_miembro` on every database, fresh or existing -- from
    `household._COLUMNS` (body/household.py's one definition of the table's shape), so the
    request-path functions that used to create it lazily (household.household_of,
    household.sincronizar_desde_claim, cuenta._debe_refrescar_hogar -- BODY-HOGAR-CUENTA-DB-INIT-1)
    no longer need to. Runs before `_migrate_hogar`, which then only copies rows and drops
    `hogar_local`."""
    from . import household
    conn.execute(f"CREATE TABLE IF NOT EXISTS hogar_miembro ({household._COLUMNS})")


def _migrate_hogar(conn: sqlite3.Connection) -> None:
    """hogar_local (pre BODY-HOGAR-CUENTA-1) -> hogar_miembro (created unconditionally by
    `_ensure_hogar_miembro`, called just before this), once and idempotently: a database that
    never had hogar_local -- fresh, or already migrated -- is untouched here."""
    if not _table_exists(conn, "hogar_local"):
        return

    def step(conn):
        old_rows = conn.execute("SELECT person_id, hogar_id, member_sub FROM hogar_local").fetchall()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in old_rows:
            conn.execute(
                "INSERT INTO hogar_miembro (person_id, hogar_id, member_sub, fuente, actualizado_at) "
                "VALUES (?, ?, ?, 'local', ?)",
                (r["person_id"], r["hogar_id"], r["member_sub"], now),
            )
        new_count = conn.execute("SELECT COUNT(*) FROM hogar_miembro WHERE fuente = 'local'").fetchone()[0]
        if new_count != len(old_rows):
            raise RuntimeError(f"hogar_miembro migration count mismatch: {len(old_rows)} -> {new_count}")
        conn.execute("DROP TABLE hogar_local")
    _rebuild_with_fk_off(conn, step)


def _migrate_precios(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "precio_modelo") and _precio_modelo_needs_desde(conn):
        _rebuild_precio_modelo_table(conn)


def init(conn: sqlite3.Connection) -> None:
    _migrate_metas(conn)
    _migrate_cuenta(conn)
    _migrate_precios(conn)
    _migrate_propuestas(conn)
    _ensure_hogar_miembro(conn)
    _migrate_hogar(conn)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executemany(
        "INSERT INTO metric_def (key, label, unit, better, kind, origen) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET label=excluded.label, unit=excluded.unit, better=excluded.better, "
        "kind=excluded.kind",
        METRIC_DEFS,
    )
    # One owner row so a single-person install works; no personal details.
    conn.execute(
        "INSERT OR IGNORE INTO person (id, display_name) VALUES (?, 'Me')", (DEFAULT_PERSON_ID,)
    )
    conn.commit()


# Per-process memo of which DB files have already been made ready (BODY-RENDIMIENTO-OBS-1 measured
# init() running on every page view: the five guarded migrations, 48 CREATE ... IF NOT EXISTS
# statements, a 48-row METRIC_DEFS upsert and a commit -- on two connections, on every request).
# Keyed on the file's resolved path AND its inode (os.stat().st_ino -- the NTFS file index on
# Windows), not on the Connection object or the path string alone: a test that points
# config.DB_PATH at a fresh temp dir, or deletes and recreates the file, gets a new key and is
# made ready again, even from the same process.
_READY_LOCK = threading.Lock()
_READY_FILES: set[tuple[str, int]] = set()


def _file_key(conn: sqlite3.Connection) -> tuple[str, int]:
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    return (path, os.stat(path).st_ino)


def ensure_ready(conn: sqlite3.Connection) -> None:
    """Runs init(conn) the first time this process meets `conn`'s underlying DB file, and is a
    no-op after that. Per-request code (body/identity.py, body/rutas/comun.py, body/rutas/
    sesion.py) calls this, never init() directly -- init() keeps doing the real work and keeps its
    CLI callers (precios, demo, pull, shift_import, vida_export, vincular_cuenta), each of which
    calls it once per process already. Guarded by a lock because routes run in a threadpool."""
    key = _file_key(conn)
    with _READY_LOCK:
        if key in _READY_FILES:
            return
        init(conn)
        _READY_FILES.add(key)


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
        # BODY-ENTRENO-SUENO-STORE-1: these four live on DayData itself, not DayData.metrics (see
        # BODY-ENTRENO-SUENO-PARSE-1) -- absent (None) stays absent here too, never a 0 row.
        for key in ("sleep_spo2_avg", "sleep_respiration_avg", "sleep_respiration_low",
                    "sleep_respiration_high"):
            value = getattr(day_data, key, None)
            if value is not None:
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


def get_trabaja_turnos(conn, person_id: int) -> bool | None:
    """NULL = not answered yet; otherwise True (1) or False (0)."""
    row = conn.execute("SELECT trabaja_turnos FROM person WHERE id = ?", (person_id,)).fetchone()
    value = row["trabaja_turnos"] if row else None
    return None if value is None else bool(value)


def set_trabaja_turnos(conn, value: bool, person_id: int) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"trabaja_turnos must be a bool, got {type(value).__name__}")
    with conn:
        conn.execute("UPDATE person SET trabaja_turnos = ? WHERE id = ?", (int(value), person_id))


def trabaja_turnos_si(conn, person_id: int) -> bool:
    """The one predicate every screen slice uses to show or hide Turnos.

    True when the stored value is 1. Also True when it is NULL (never answered) AND the person
    already has at least one row in shift or shift_batch -- read-time inference only, never a
    write, so an explicit "no" (0) is never overridden. This matters because the migration's
    backfill-to-1 (see _migrate) runs only when the trabaja_turnos column is ADDED to an existing
    database; a database CREATED fresh -- every test, and the hosted Body, which starts empty and
    receives shift files from the PC uploader -- never runs that backfill, so a person with shift
    data would otherwise sit at NULL and Turnos would hide from the very person whose shifts are
    arriving (BODY-TURNOS-INFERIR-1). False when the stored value is 0, or NULL with no shift data.
    get_trabaja_turnos is unchanged: it reports only what is stored, so Primeros pasos keeps asking
    while it is NULL."""
    stored = get_trabaja_turnos(conn, person_id)
    if stored is not None:
        return stored
    return conn.execute(
        "SELECT EXISTS("
        "SELECT 1 FROM shift WHERE person_id = ? "
        "UNION SELECT 1 FROM shift_batch WHERE person_id = ?"
        ")",
        (person_id, person_id),
    ).fetchone()[0] == 1


def update_goal_target(conn, goal_id: int, person_id: int, *, target_value: float | None = None,
                        deadline: str | None = None) -> bool:
    """Updates an existing, owned goal's target_value and/or deadline. Returns whether a row
    changed. No screen calls this yet -- body/goals.py has no editor for an active goal's target --
    so this is the assistant proposal's own write path (BODY-ASISTENTE-PROPUESTAS-1), built the same
    way body/goals.py's own setters are: a plain per-field UPDATE, scoped to the owner."""
    sets, vals = [], []
    if target_value is not None:
        sets.append("target_value = ?")
        vals.append(target_value)
    if deadline is not None:
        sets.append("deadline = ?")
        vals.append(deadline)
    if not sets:
        return False
    vals += [goal_id, person_id]
    with conn:
        cur = conn.execute(f"UPDATE goal SET {', '.join(sets)} WHERE id = ? AND person_id = ?", vals)
    return cur.rowcount == 1


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


def save_exercise_sets(conn: sqlite3.Connection, person_id: int, source: str, source_id: str, sets) -> None:
    """Upserts one activity's per-set detail from a Garmin re-read (BODY-ENTRENO-PULL-1): writes
    only the watch's own columns (`garmin_*`, `set_type`, `updated_at`) on the UNIQUE slot
    (person_id, activity_source, activity_source_id, set_order) -- a person's correction
    (`person_*`, `corrected_by_person`) is never touched here, so it survives every later sync
    (BODY-ENTRENO-1-INV Q2). `sets` is stored 1-indexed in the order given (each item an
    `ExerciseSet`-shaped object: `.set_type`, `.category`, `.exercise`, `.reps`, `.weight_value`,
    `.weight_unit`, `.duration_s`, `.start_local`). A slot beyond `len(sets)` -- the watch no
    longer reports it -- is removed unless the person corrected it. Requires the activity row
    (person_id, source, source_id) to already exist -- the table's own FK."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        for i, s in enumerate(sets, start=1):
            conn.execute(
                "INSERT INTO workout_set (person_id, activity_source, activity_source_id, set_order, "
                "set_type, garmin_category, garmin_exercise, garmin_reps, garmin_weight_value, "
                "garmin_weight_unit, garmin_duration_s, garmin_start_local, person_category, "
                "person_exercise, person_reps, person_weight_kg, person_note, corrected_by_person, "
                "created_at, updated_at) "
                "VALUES (:person_id, :activity_source, :activity_source_id, :set_order, :set_type, "
                ":garmin_category, :garmin_exercise, :garmin_reps, :garmin_weight_value, "
                ":garmin_weight_unit, :garmin_duration_s, :garmin_start_local, NULL, NULL, NULL, "
                "NULL, NULL, 0, :created_at, :updated_at) "
                "ON CONFLICT (person_id, activity_source, activity_source_id, set_order) DO UPDATE SET "
                "set_type = excluded.set_type, garmin_category = excluded.garmin_category, "
                "garmin_exercise = excluded.garmin_exercise, garmin_reps = excluded.garmin_reps, "
                "garmin_weight_value = excluded.garmin_weight_value, "
                "garmin_weight_unit = excluded.garmin_weight_unit, "
                "garmin_duration_s = excluded.garmin_duration_s, "
                "garmin_start_local = excluded.garmin_start_local, updated_at = excluded.updated_at",
                {
                    "person_id": person_id, "activity_source": source, "activity_source_id": source_id,
                    "set_order": i, "set_type": s.set_type, "garmin_category": s.category,
                    "garmin_exercise": s.exercise, "garmin_reps": s.reps,
                    "garmin_weight_value": s.weight_value, "garmin_weight_unit": s.weight_unit,
                    "garmin_duration_s": s.duration_s, "garmin_start_local": s.start_local,
                    "created_at": now, "updated_at": now,
                },
            )
        conn.execute(
            "DELETE FROM workout_set WHERE person_id = ? AND activity_source = ? AND "
            "activity_source_id = ? AND set_order > ? AND corrected_by_person = 0",
            (person_id, source, source_id, len(sets)),
        )


def correct_exercise_set(conn: sqlite3.Connection, person_id: int, source: str, source_id: str,
                          set_order: int, *, exercise: str | None = None, category: str | None = None,
                          reps: float | None = None, weight_kg: float | None = None,
                          note: str | None = None) -> None:
    """Writes a person's correction to one set slot. Person-scoped by construction: the UNIQUE
    slot a conflict can land on always includes this `person_id`, so this can never touch another
    person's row. Creates the slot if the watch never reported it (`set_type` defaults to
    'active', since a correction describes a worked set) -- a set the watch missed. On an
    existing slot, only the fields actually given (not None) replace the stored `person_*` value;
    a field left out keeps whatever correction was already there. Never touches `garmin_*` or
    `set_type` on an existing slot (BODY-ENTRENO-1-INV Q2)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO workout_set (person_id, activity_source, activity_source_id, set_order, "
            "set_type, person_category, person_exercise, person_reps, person_weight_kg, person_note, "
            "corrected_by_person, created_at, updated_at) "
            "VALUES (:person_id, :activity_source, :activity_source_id, :set_order, 'active', "
            ":category, :exercise, :reps, :weight_kg, :note, 1, :created_at, :updated_at) "
            "ON CONFLICT (person_id, activity_source, activity_source_id, set_order) DO UPDATE SET "
            "person_category = COALESCE(:category, workout_set.person_category), "
            "person_exercise = COALESCE(:exercise, workout_set.person_exercise), "
            "person_reps = COALESCE(:reps, workout_set.person_reps), "
            "person_weight_kg = COALESCE(:weight_kg, workout_set.person_weight_kg), "
            "person_note = COALESCE(:note, workout_set.person_note), "
            "corrected_by_person = 1, updated_at = :updated_at",
            {
                "person_id": person_id, "activity_source": source, "activity_source_id": source_id,
                "set_order": set_order, "category": category, "exercise": exercise, "reps": reps,
                "weight_kg": weight_kg, "note": note, "created_at": now, "updated_at": now,
            },
        )


def create_manual_session(conn: sqlite3.Connection, person_id: int, day: str, start_local: str | None,
                           title: str, duration_s: float | None = None) -> str:
    """Creates an `activity` row for a session the watch never recorded (`source = 'manual'`),
    filling only columns the `activity` table already has -- `title` maps to `name`; there is no
    manual `type`. Returns the generated `source_id`, for `save_exercise_sets`/
    `correct_exercise_set` to key that session's sets to."""
    source_id = f"manual-{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
            "distance_m, avg_hr, max_hr, calories, source) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, NULL, NULL, NULL, 'manual')",
            (person_id, source_id, day, start_local, title, duration_s),
        )
    return source_id


def exercise_sets(conn: sqlite3.Connection, person_id: int, source: str, source_id: str) -> list[dict]:
    """One activity's sets in order, each with the display value (the person's correction when
    `corrected_by_person`, else the watch's reading) and both originals -- for the assistant to
    read. Person-scoped by the WHERE clause: a row can never surface for another person's id."""
    rows = conn.execute(
        "SELECT * FROM workout_set WHERE person_id = ? AND activity_source = ? AND "
        "activity_source_id = ? ORDER BY set_order",
        (person_id, source, source_id),
    ).fetchall()
    out = []
    for r in rows:
        corrected = bool(r["corrected_by_person"])
        if corrected:
            weight_value = r["person_weight_kg"]
            weight_unit = "kg" if weight_value is not None else None
        else:
            weight_value, weight_unit = r["garmin_weight_value"], r["garmin_weight_unit"]
        out.append({
            "set_order": r["set_order"],
            "set_type": r["set_type"],
            "corrected_by_person": corrected,
            "exercise": r["person_exercise"] if corrected else r["garmin_exercise"],
            "category": r["person_category"] if corrected else r["garmin_category"],
            "reps": r["person_reps"] if corrected else r["garmin_reps"],
            "weight_value": weight_value,
            "weight_unit": weight_unit,
            "note": r["person_note"],
            "duration_s": r["garmin_duration_s"],
            "start_local": r["garmin_start_local"],
            "garmin": {
                "category": r["garmin_category"], "exercise": r["garmin_exercise"],
                "reps": r["garmin_reps"], "weight_value": r["garmin_weight_value"],
                "weight_unit": r["garmin_weight_unit"], "duration_s": r["garmin_duration_s"],
                "start_local": r["garmin_start_local"],
            },
            "person": {
                "category": r["person_category"], "exercise": r["person_exercise"],
                "reps": r["person_reps"], "weight_kg": r["person_weight_kg"], "note": r["person_note"],
            },
        })
    return out


# ---- experimentos (wave 9, BODY-EXPERIMENTOS-ESQUEMA-1) --------------------------------------------

# intradia's own metric vocabulary -- unlike tipo 'dia', never checked against metric_def, since an
# intraday window reads the clock's own series (body/lector.py), not a daily aggregate.
METRICAS_INTRADIA = ("heart_rate", "stress")


def _campo_invalido(campo: str, detalle: str) -> None:
    raise ValueError(f"{campo}: {detalle}")


def validar_experimento(metricas, ventana, filtro_dias) -> None:
    """The one place crear_experimento's three JSON shapes are checked. Pure (no connection), so a
    caller can validate before ever touching the database. Raises ValueError naming the invalid
    field first -- ventana, filtro_dias or metricas.

    ventana: {"tipo": "dia"} or {"tipo": "intradia", "ancla": "despertar" | "momento",
    "antes_min": int >= 0 | null, "despues_min": int > 0}.
    filtro_dias: {"turno_en": [kinds] | null, "turno_fuera": [kinds] | null,
    "excluir_tras_noche": bool} -- kinds from shift_import.VALID_KINDS (imported lazily here:
    shift_import imports this module, so a module-level import would be circular).
    metricas: a non-empty list; for tipo 'dia' every key must be a known metric_def key, for
    'intradia' a key from METRICAS_INTRADIA.
    """
    if not isinstance(ventana, dict) or ventana.get("tipo") not in ("dia", "intradia"):
        _campo_invalido("ventana", "tipo must be 'dia' or 'intradia'")
    tipo = ventana["tipo"]
    if tipo == "dia":
        if set(ventana) != {"tipo"}:
            _campo_invalido("ventana", "tipo 'dia' takes no other key")
    else:
        if set(ventana) != {"tipo", "ancla", "antes_min", "despues_min"}:
            _campo_invalido("ventana", "tipo 'intradia' needs ancla, antes_min and despues_min")
        if ventana["ancla"] not in ("despertar", "momento"):
            _campo_invalido("ventana", "ancla must be 'despertar' or 'momento'")
        antes = ventana["antes_min"]
        if antes is not None and (isinstance(antes, bool) or not isinstance(antes, int) or antes < 0):
            _campo_invalido("ventana", "antes_min must be a non-negative int or null")
        despues = ventana["despues_min"]
        if isinstance(despues, bool) or not isinstance(despues, int) or despues <= 0:
            _campo_invalido("ventana", "despues_min must be a positive int")

    if not isinstance(filtro_dias, dict) or set(filtro_dias) != {"turno_en", "turno_fuera", "excluir_tras_noche"}:
        _campo_invalido("filtro_dias", "needs exactly turno_en, turno_fuera and excluir_tras_noche")
    from . import shift_import  # local: shift_import imports this module, so this stays lazy
    for campo in ("turno_en", "turno_fuera"):
        valor = filtro_dias[campo]
        if valor is not None and (
            not isinstance(valor, list) or not valor or any(k not in shift_import.VALID_KINDS for k in valor)
        ):
            _campo_invalido("filtro_dias", f"{campo} must be null or a non-empty list of known shift kinds")
    if not isinstance(filtro_dias["excluir_tras_noche"], bool):
        _campo_invalido("filtro_dias", "excluir_tras_noche must be a bool")

    if not isinstance(metricas, list) or not metricas:
        _campo_invalido("metricas", "must be a non-empty list")
    if tipo == "dia":
        conocidas = {d[0] for d in METRIC_DEFS}
        if any(m not in conocidas for m in metricas):
            _campo_invalido("metricas", "every key must be a known metric_def key for tipo 'dia'")
    else:
        if any(m not in METRICAS_INTRADIA for m in metricas):
            _campo_invalido("metricas", f"intradia keys must be one of {METRICAS_INTRADIA}")


def crear_experimento(conn: sqlite3.Connection, person_id: int, titulo: str, metricas, ventana, filtro_dias,
                       *, habito_id: int | None = None, dias_prueba_objetivo: int | None = None,
                       fecha_inicio: str | None = None, estado: str = "activo") -> int:
    """Validates the three JSON shapes, then inserts. If habito_id is given it must already be this
    person's own habit (via habit.goal_id -> goal.person_id) -- checked in SQL, scoped by
    person_id, same as every other read and write in this section."""
    validar_experimento(metricas, ventana, filtro_dias)
    if habito_id is not None:
        propio = conn.execute(
            "SELECT 1 FROM habit h JOIN goal g ON g.id = h.goal_id WHERE h.id = ? AND g.person_id = ?",
            (habito_id, person_id),
        ).fetchone()
        if propio is None:
            raise ValueError("habito_id: not this person's own habit")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fecha_inicio = fecha_inicio or now[:10]
    with conn:
        cur = conn.execute(
            "INSERT INTO experimento (person_id, titulo, habito_id, metricas, ventana, filtro_dias, "
            "dias_prueba_objetivo, fecha_inicio, estado, creado_en) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (person_id, titulo, habito_id, json.dumps(metricas), json.dumps(ventana), json.dumps(filtro_dias),
             dias_prueba_objetivo, fecha_inicio, estado, now),
        )
    return cur.lastrowid


def experimentos(conn: sqlite3.Connection, person_id: int, estado: str | None = None) -> list[sqlite3.Row]:
    if estado is None:
        return conn.execute(
            "SELECT * FROM experimento WHERE person_id = ? ORDER BY id", (person_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM experimento WHERE person_id = ? AND estado = ? ORDER BY id", (person_id, estado)
    ).fetchall()


def registrar_dia_experimento(conn: sqlite3.Connection, experimento_id: int, person_id: int, day: str,
                               ocurrio: bool, nota: str | None = None, momento_id: int | None = None) -> None:
    """Upserts on (experimento_id, day) -- used only when the experiment has no habito_id; with one,
    habit_check is the log instead. Refuses an experimento_id that is not this person's own,
    checked in SQL scoped by person_id, before anything is written."""
    propio = conn.execute(
        "SELECT 1 FROM experimento WHERE id = ? AND person_id = ?", (experimento_id, person_id)
    ).fetchone()
    if propio is None:
        raise ValueError("experimento_id: not this person's own experiment")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO experimento_registro (experimento_id, person_id, day, ocurrio, nota, momento_id, "
            "creado_en) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(experimento_id, day) DO UPDATE SET "
            "ocurrio = excluded.ocurrio, nota = excluded.nota, momento_id = excluded.momento_id",
            (experimento_id, person_id, day, 1 if ocurrio else 0, nota, momento_id, now),
        )


def crear_momento(conn: sqlite3.Connection, person_id: int, ts_local: str, kind: str,
                   note: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO moment (person_id, ts_local, kind, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (person_id, ts_local, kind, note, now),
        )
    return cur.lastrowid
