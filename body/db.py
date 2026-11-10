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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, garmin_adapter

_CADENCE_N_PER_WEEK = re.compile(r"^\s*(\d+)\s*x\s*/\s*week\s*$", re.IGNORECASE)

# The shift-kind vocabulary: shared by the shift and shift_batch_item CHECK constraints in SCHEMA
# below, by validar_experimento's filtro_dias check, and by shift_import.VALID_KINDS (which reads
# it from here) -- one spelling, not three.
SHIFT_KINDS = ("morning", "afternoon", "night", "day", "off", "other")

# BODY-RECUPERACION-1: the honest-gap reason codes recuperacion.py's Resultado carries when it has
# no minutes figure -- shared here (not defined in recuperacion.py) the same way SHIFT_KINDS above
# is, since recuperacion.py imports db but db must not import recuperacion.py (it would be
# circular). 'sin_cobertura' = too little minute-HR coverage after the activity's end; 'sin_rango'
# = no resting_hr baseline yet; 'fuera_de_horizonte' = never back to resting within MODELO's
# horizon; 'marcado' = the activity's own day is inside a marked period (db.dias_marcados).
RECUPERACION_MOTIVOS = ("sin_cobertura", "sin_rango", "fuera_de_horizonte", "marcado")

# BODY-TURNO-FC-1: the honest-gap reason codes body/turno_fc.py's Resultado carries when a shift
# gets no figure -- same reasoning as RECUPERACION_MOTIVOS above. 'sin_cobertura' = too little
# minute-HR coverage inside the shift's own window; 'sin_rango' = no days-off usual yet for an hour
# the window touches; 'marcado' = the shift's own day is inside a marked period.
TURNO_FC_MOTIVOS = ("sin_cobertura", "sin_rango", "marcado")

# BODY-SUENO-FC-1: the honest-gap reason codes body/sueno_fc.py's `noche()` returns when a night
# gets no figure. 'sin_sesion' = no sleep_session row (or no start_local/end_local) for that wake
# day; 'sin_cobertura' = too little minute-HR coverage inside the sleep window.
SUENO_FC_MOTIVOS = ("sin_sesion", "sin_cobertura")

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
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito',
                         'proponer_experimento', 'marcar_experimento',
                         'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                         'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                         'registrar_comida')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    -- Set to 'mcp' only when body/mcp.py's own `aplicar_propuesta` tool applied this row
    -- (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4): the panel's own "applied by
    -- your personal assistant" label, never set for an apply through Body's own web route, even
    -- for a proposal this same outside caller made.
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp'))
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

# BODY-RECUPERACION-1: one row per (person, activity, model version) -- a version bump recomputes
# rather than mixes (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1), the same idea goal_check's own
# rule_version carries. `minutos`/`motivo` are mutually exclusive honest-gap pairs, like
# experimento_registro's own shape -- never both set, never both NULL.
_RECUPERACION_ACTIVIDAD_COLUMNS = f"""
    person_id          INTEGER NOT NULL REFERENCES person(id),
    activity_source    TEXT NOT NULL,
    activity_source_id TEXT NOT NULL,
    modelo_version     TEXT NOT NULL,
    minutos            REAL,
    motivo             TEXT CHECK (motivo IN {RECUPERACION_MOTIVOS!r} OR motivo IS NULL),
    computed_at        TEXT NOT NULL,
    PRIMARY KEY (person_id, activity_source, activity_source_id, modelo_version),
    FOREIGN KEY (person_id, activity_source, activity_source_id)
        REFERENCES activity(person_id, source, source_id)
"""

# BODY-TURNO-FC-1: one row per (person, shift day, model version) -- a version bump recomputes
# rather than mixes (same convention as _RECUPERACION_ACTIVIDAD_COLUMNS above). `kind` is a plain
# copy of the shift's own kind at the time this was computed, so body/turno_fc.py's estudio() never
# needs a join back to `shift` just to group by it. `fc_mediana`/`estres_mediana`/`bajo_usual_pct`/
# `cobertura` are all NULL together when `motivo` is set -- never a mix of some figures and a
# reason.
_TURNO_FC_COLUMNS = f"""
    person_id      INTEGER NOT NULL REFERENCES person(id),
    day            TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN {SHIFT_KINDS!r}),
    modelo_version TEXT NOT NULL,
    fc_mediana     REAL,
    estres_mediana REAL,
    bajo_usual_pct REAL,
    -- The days-off reference (ruling 2): the mean of this shift's own touched hours' own usual,
    -- computed once here at recalcular() time and read back as-is by estudio()'s kind grouping --
    -- never recomputed live on a page render (BODY-RENDIMIENTO-1's own ceiling on /shifts).
    referencia_fc  REAL,
    cobertura      REAL,
    motivo         TEXT CHECK (motivo IN {TURNO_FC_MOTIVOS!r} OR motivo IS NULL),
    computed_at    TEXT NOT NULL,
    PRIMARY KEY (person_id, day, modelo_version)
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
    kind        TEXT NOT NULL CHECK (kind IN {SHIFT_KINDS!r}),
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
    kind        TEXT NOT NULL CHECK (kind IN {SHIFT_KINDS!r}),
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
-- (SHIFT_KINDS above); `start`/`end` are "HH:MM" strings, null on an `off` day.
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
-- `aplicar_mcp` (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1): OFF (0) by default,
-- changed only through Body's own /settings/aplicar_mcp POST -- never by an MCP tool, never by the
-- in-app assistant.
CREATE TABLE IF NOT EXISTS ajuste_agente (
    person_id  INTEGER PRIMARY KEY REFERENCES person(id),
    nivel_datos TEXT NOT NULL DEFAULT 'completo' CHECK (nivel_datos IN ('completo', 'resumen', 'ninguno')),
    proveedor  TEXT,
    aplicar_mcp INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Conversations with Body's assistant (wave 7, stage two: BODY-ASISTENTE-PROVEEDOR-1).
-- `origen` (BODY-MCP-LOCAL-1): NULL for one started in Body's own chat panel, 'mcp' for one the
-- local MCP server (body/mcp.py) started -- so the panel can label its proposal cards "from your
-- personal assistant" (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3).
CREATE TABLE IF NOT EXISTS conversacion_agente (
    id        INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    creada_en TEXT NOT NULL,
    titulo    TEXT,
    origen    TEXT CHECK (origen IS NULL OR origen IN ('mcp'))
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

-- One row per call an OUTSIDE caller (today, only the local MCP server, body/mcp.py) made against
-- the tool registry (BODY-MCP-LOCAL-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5): the tool NAME, the
-- UTC time and the caller id only -- never its params or its result, so this row can never carry a
-- health figure or anything the person typed.
CREATE TABLE IF NOT EXISTS uso_herramienta_externa (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES person(id),
    herramienta TEXT NOT NULL,
    llamada_en  TEXT NOT NULL,
    caller      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uso_herramienta_externa_person ON uso_herramienta_externa(person_id);

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

-- BODY-COMIDA-1: one meal, grouping its own `ingesta` rows (one per nutrient) under one
-- description and time. Additive, reversible (ruling 1): ts_local/descripcion are nullable --
-- "sometime that day", or no words at all -- and nothing here is backfilled from existing
-- `ingesta` rows, which keep comida_id NULL and stay valid on their own.
CREATE TABLE IF NOT EXISTS comida (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES person(id),
    day         TEXT NOT NULL,
    ts_local    TEXT,
    descripcion TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comida_person_dia ON comida(person_id, day);

-- BODY-PROTEINA-1: one row per thing eaten or drunk, keyed by a metric_def key marked `agregado =
-- 'suma_ingesta'` (protein_g today; calories or water later are rows here too, never new tables).
-- The day's total is DERIVED by the one writer (db.registrar_ingesta/corregir_ingesta/
-- borrar_ingesta) into daily_metric, source 'self' -- never written directly by anything else.
-- ts_local is the moment eaten, or NULL for "sometime that day"; descripcion is the person's own
-- words, kept like moment.note, never printed or logged. comida_id (BODY-COMIDA-1, nullable, no
-- CASCADE -- db.borrar_comida deletes its children itself) links one of several amounts of the
-- SAME meal together; a row with comida_id NULL is a standalone entry, its own one-item "meal".
CREATE TABLE IF NOT EXISTS ingesta (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES person(id),
    day         TEXT NOT NULL,
    ts_local    TEXT,
    metric_key  TEXT NOT NULL REFERENCES metric_def(key),
    amount      REAL NOT NULL CHECK (amount > 0),
    estimado    INTEGER NOT NULL DEFAULT 0 CHECK (estimado IN (0, 1)),
    descripcion TEXT,
    comida_id   INTEGER REFERENCES comida(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingesta_person_dia ON ingesta(person_id, day);

CREATE TABLE IF NOT EXISTS experimento (
{_EXPERIMENTO_COLUMNS}
);
CREATE INDEX IF NOT EXISTS idx_experimento_person ON experimento(person_id);

CREATE TABLE IF NOT EXISTS experimento_registro (
{_EXPERIMENTO_REGISTRO_COLUMNS}
);
CREATE INDEX IF NOT EXISTS idx_experimento_registro_person ON experimento_registro(person_id);

-- BODY-PERIODO-1: a kind the person can mark across days (illness, travel, injury) so a baseline's
-- usual range and an experiment's counted days leave those days out, never re-weight them. Seeded
-- with 'malestar', 'viaje', 'lesion' below -- a kind added later is a row here, never a code
-- change; its label is shown through "periodo.kind." + key in body/i18n/*.json, the same
-- convention metric_def's own key already uses for "metric." + key.
CREATE TABLE IF NOT EXISTS period_kind (
    key        TEXT PRIMARY KEY,
    sort_order INTEGER NOT NULL
);

-- One row per marked stretch of days. end_day NULL = still open -- an open period counts through
-- today in the person's own zone (db.dias_marcados). note is the person's own words, kept like
-- moment.note and ingesta.descripcion -- never printed, logged or exported off the PC.
CREATE TABLE IF NOT EXISTS period (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES person(id),
    kind       TEXT NOT NULL REFERENCES period_kind(key),
    start_day  TEXT NOT NULL,
    end_day    TEXT,
    note       TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (end_day IS NULL OR end_day >= start_day)
);
CREATE INDEX IF NOT EXISTS idx_period_person ON period(person_id);

-- BODY-RECUPERACION-1: see _RECUPERACION_ACTIVIDAD_COLUMNS above for why.
CREATE TABLE IF NOT EXISTS recuperacion_actividad (
{_RECUPERACION_ACTIVIDAD_COLUMNS}
);
CREATE INDEX IF NOT EXISTS idx_recuperacion_actividad_person ON recuperacion_actividad(person_id);

-- BODY-TURNO-FC-1: see _TURNO_FC_COLUMNS above for why.
CREATE TABLE IF NOT EXISTS turno_fc (
{_TURNO_FC_COLUMNS}
);
CREATE INDEX IF NOT EXISTS idx_turno_fc_person ON turno_fc(person_id);
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
    # BODY-SUENO-FC-1 (ORCH-CARLOS-DELEGATES-TIER-1-1 rulings 1, 6): the sleep-window heart rate
    # itself, derived from minute data -- 'estimated' like readiness, since Body computes both,
    # Garmin's API writes neither. 'lower' for both: a lower sleeping heart rate reads as more
    # rested, the same word this app already uses for resting_hr.
    ("sleep_hr_avg", "Sleeping heart rate", "bpm", "lower", "estimated"),
    ("sleep_hr_min", "Lowest sleeping heart rate", "bpm", "lower", "estimated"),
]
METRIC_DEFS = [d if len(d) == 5 else (*d, "measured") for d in METRIC_DEFS]
METRIC_DEFS = [d if len(d) == 6 else (*d, "garmin") for d in METRIC_DEFS]

# Generic self-measured metric_def rows (BODY-ENTRENO-SCHEMA-1): no person's values, just the
# definitions a future tool slice can write to. Labels are added to body/i18n/*.json by that
# tool slice -- i18n is not in this slice's touches.
METRIC_DEFS += [
    ("flex_alcance_cm", "Flexibility reach", "cm", "lower", "measured", "self"),
    ("snoring_min", "Snoring", "min", "lower", "measured", "self"),
    # BODY-MEDIDA-PESO-1: weight and waist, both self-measured. 'neutral' -- whether weight should
    # go up or down is the person's own goal (a goal row), never this metric's own direction.
    ("body_weight_kg", "Weight", "kg", "neutral", "measured", "self"),
    ("waist_cm", "Waist", "cm", "neutral", "measured", "self"),
    # BODY-PROTEINA-1: the first intake metric -- 'neutral' like weight, since whether to raise or
    # lower protein is the person's own goal, never assumed here. `agregado = 'suma_ingesta'` marks
    # it as derived from `ingesta` rows, not a single self-reported figure.
    ("protein_g", "Protein", "g", "neutral", "measured", "self", "suma_ingesta"),
    # BODY-COMIDA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2): six more intake keys beside
    # protein_g, same `suma_ingesta` aggregation and `self` origin, 'neutral' direction (whether to
    # raise or lower any of these is the person's own goal, never assumed here). alcohol_g is grams
    # of PURE alcohol, not drinks or "units" -- those differ by country, grams do not.
    ("kcal", "Calories", "kcal", "neutral", "measured", "self", "suma_ingesta"),
    ("carbs_g", "Carbs", "g", "neutral", "measured", "self", "suma_ingesta"),
    ("fat_g", "Fat", "g", "neutral", "measured", "self", "suma_ingesta"),
    ("fiber_g", "Fiber", "g", "neutral", "measured", "self", "suma_ingesta"),
    ("water_ml", "Water", "ml", "neutral", "measured", "self", "suma_ingesta"),
    ("caffeine_mg", "Caffeine", "mg", "neutral", "measured", "self", "suma_ingesta"),
    ("alcohol_g", "Alcohol", "g", "neutral", "measured", "self", "suma_ingesta"),
    # BODY-AGUJETAS-1 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1): next-day muscle soreness, one key
    # per muscle GROUP, not a finer list -- 'neutral' (ruling 2): soreness is neither good nor bad,
    # it is the person's own read of the work a session gave that muscle.
    ("soreness_legs", "Soreness: legs", "of5", "neutral", "measured", "self"),
    ("soreness_glutes", "Soreness: glutes", "of5", "neutral", "measured", "self"),
    ("soreness_back", "Soreness: back", "of5", "neutral", "measured", "self"),
    ("soreness_chest", "Soreness: chest", "of5", "neutral", "measured", "self"),
    ("soreness_shoulders", "Soreness: shoulders", "of5", "neutral", "measured", "self"),
    ("soreness_arms", "Soreness: arms", "of5", "neutral", "measured", "self"),
    ("soreness_core", "Soreness: core", "of5", "neutral", "measured", "self"),
]
# BODY-PROTEINA-1: a 7th field, `agregado` -- 'suma_ingesta' marks a key whose daily_metric value is
# the SUM of that day's `ingesta` rows, derived by db.registrar_ingesta/corregir_ingesta/
# borrar_ingesta, never set directly; None (padded here) for every other key, today's behaviour.
METRIC_DEFS = [d if len(d) == 7 else (*d, None) for d in METRIC_DEFS]

# BODY-COMIDA-1 ruling 2: every intake key, in catalogue order (the order METRIC_DEFS declares
# them) -- read off the live table instead of a second hand-typed list, the same template pattern
# as RANGOS_MEDIDA_PERSONAL below. body/agente/herramientas.py (the enum offered to the model) and
# body/rutas/hoy.py (a meal card's own amount order) both read this one constant.
INGESTA_METRIC_KEYS = tuple(d[0] for d in METRIC_DEFS if d[6] == "suma_ingesta")

# BODY-MEDIDA-PESO-1: a plausible-range refusal for a self-measured figure prone to a typo (780
# instead of 78.0) -- keyed by metric key, not every self key: flex_alcance_cm and snoring_min have
# no such check. (min, max) inclusive; body/agente/herramientas.py's _registrar_medida reads this
# instead of an if-chain naming the two keys itself.
RANGOS_MEDIDA_PERSONAL = {
    "body_weight_kg": (20, 300),
    "waist_cm": (40, 200),
    # BODY-AGUJETAS-1 ruling 2: 0-5 inclusive for every soreness key.
    "soreness_legs": (0, 5),
    "soreness_glutes": (0, 5),
    "soreness_back": (0, 5),
    "soreness_chest": (0, 5),
    "soreness_shoulders": (0, 5),
    "soreness_arms": (0, 5),
    "soreness_core": (0, 5),
}

# BODY-AGUJETAS-1 ruling 2: a soreness reading is whole numbers only (0, 1, 2, 3, 4 or 5) -- keyed
# set, not every self key with a range: body_weight_kg/waist_cm keep a decimal.
# body/agente/herramientas.py's _registrar_medida reads this instead of naming the seven keys itself.
ENTEROS_MEDIDA_PERSONAL = frozenset({
    "soreness_legs", "soreness_glutes", "soreness_back", "soreness_chest",
    "soreness_shoulders", "soreness_arms", "soreness_core",
})

# BODY-PERIODO-1 ruling 1: kinds are DATA in period_kind, never code -- a kind added later is just
# a new row (and an i18n "periodo.kind.<key>" entry for its label), (key, sort_order) in that order.
PERIOD_KINDS = [
    ("malestar", 0),
    ("viaje", 1),
    ("lesion", 2),
]

# BODY-MEDIDA-PESO-1-M2: how many decimals a metric's own figure, its "vs. tu historia"/"vs.
# anterior" difference and its normal-range legend show -- keyed by metric key, every key not
# listed keeps 0 (today's behaviour, unchanged). A whole number hides exactly the signal a
# self-measured key like weight or waist exists to show (muscle gain moves it a few tenths a week),
# so this is data next to the metric a new key can opt into, never an `if` naming the key in
# body/rutas/comun.py or body/rutas/tendencias.py.
DECIMALES_MEDIDA = {
    "body_weight_kg": 1,
    "waist_cm": 1,
}

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


def registrar_uso_herramienta_externa(conn: sqlite3.Connection, person_id: int, herramienta: str,
                                       caller: str) -> None:
    """One audit row for one call an outside caller made against the tool registry
    (BODY-MCP-LOCAL-1): the tool's own name, the UTC time, and who called -- never the call's
    params or its result, so this can never carry a health figure (ORCH-CARLOS-DELEGATES-TIER-1-1
    ruling 5)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO uso_herramienta_externa (person_id, herramienta, llamada_en, caller) "
            "VALUES (?, ?, ?, ?)",
            (person_id, herramienta, now, caller),
        )


def marcar_aplicada_via_mcp(conn: sqlite3.Connection, propuesta_id: int) -> None:
    """Ties a proposal's row to its own outside apply, called ONLY after `agente_propuestas.aplicar`
    has already marked it 'aplicada' (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4):
    the panel's own "applied by your personal assistant" label. Never set for an apply through
    Body's own web route."""
    with conn:
        conn.execute("UPDATE propuesta_agente SET aplicada_via = 'mcp' WHERE id = ?", (propuesta_id,))


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
    # BODY-PERIODO-1: id(conn) is only ever reused once the earlier Connection it named has been
    # closed and garbage-collected, so purging _PERIODOS_CACHE's entries for this (possibly
    # reused) id here, before anything can read through this brand-new connection, means a cache
    # hit can only ever be this same connection's own earlier read -- never a ghost from one that
    # died. No SQL runs for this -- a plain dict scan.
    for k in [k for k in _PERIODOS_CACHE if k[0] == id(conn)]:
        del _PERIODOS_CACHE[k]
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
    if cols and "agregado" not in cols:
        conn.execute(
            "ALTER TABLE metric_def ADD COLUMN agregado TEXT CHECK (agregado IN ('suma_ingesta') OR agregado IS NULL)"
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
    if acols and "aplicar_mcp" not in acols:
        conn.execute("ALTER TABLE ajuste_agente ADD COLUMN aplicar_mcp INTEGER NOT NULL DEFAULT 0")
    gcols = {r[1] for r in conn.execute("PRAGMA table_info(garmin_cuenta)")}
    if gcols and "backfill_hasta" not in gcols:
        conn.execute("ALTER TABLE garmin_cuenta ADD COLUMN backfill_hasta TEXT")
    plcols = {r[1] for r in conn.execute("PRAGMA table_info(plan)")}
    if plcols and "ejercicios_json" not in plcols:
        conn.execute(
            "ALTER TABLE plan ADD COLUMN ejercicios_json TEXT "
            "CHECK (ejercicios_json IS NULL OR json_valid(ejercicios_json))"
        )
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(conversacion_agente)")}
    if ccols and "origen" not in ccols:
        conn.execute("ALTER TABLE conversacion_agente ADD COLUMN origen TEXT CHECK (origen IS NULL OR origen IN ('mcp'))")
    sbcols = {r[1] for r in conn.execute("PRAGMA table_info(shift_batch)")}
    if sbcols and "kind" not in sbcols:
        conn.execute(
            "ALTER TABLE shift_batch ADD COLUMN kind TEXT NOT NULL DEFAULT 'import' "
            "CHECK (kind IN ('import', 'pattern'))"
        )
    # BODY-COMIDA-1 ruling 1: a plain nullable ADD COLUMN, run after SCHEMA has already created
    # `comida` (the column's own REFERENCES target) on this same init() call -- never backfilled,
    # so every existing ingesta row keeps comida_id NULL and stays valid.
    icols = {r[1] for r in conn.execute("PRAGMA table_info(ingesta)")}
    if icols and "comida_id" not in icols:
        conn.execute("ALTER TABLE ingesta ADD COLUMN comida_id INTEGER REFERENCES comida(id)")
    # Never inside SCHEMA's own executescript (BODY-COMIDA-1): that script runs BEFORE this
    # function on every init() call, so an index on a column only THIS migration might still be
    # about to add would fail against an old `ingesta` table that has not been migrated yet.
    # Unconditional and idempotent: by this point the column exists either way (fresh SCHEMA or
    # the ALTER right above).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ingesta_comida ON ingesta(comida_id)")
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


def _propuesta_agente_needs_experimentos_rebuild(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'propuesta_agente'"
    ).fetchone()
    return row is not None and "marcar_experimento" not in row[0]


def _propuesta_agente_needs_ingesta_rebuild(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'propuesta_agente'"
    ).fetchone()
    return row is not None and "registrar_ingesta" not in row[0]


def _propuesta_agente_needs_periodo_rebuild(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'propuesta_agente'"
    ).fetchone()
    return row is not None and "marcar_periodo" not in row[0]


def _propuesta_agente_needs_comida_rebuild(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'propuesta_agente'"
    ).fetchone()
    return row is not None and "registrar_comida" not in row[0]


def _migrate_propuestas(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "propuesta_agente") and _propuesta_agente_needs_rebuild(conn):
        _rebuild_propuesta_agente_table(conn)
    # BODY-EXPERIMENTOS-REGISTRO-1: a second, later widening of the same CHECK (proponer_experimento,
    # marcar_experimento) -- reuses _rebuild_propuesta_agente_table unchanged, since that rebuild
    # already copies column-for-column from the CURRENT _PROPUESTA_AGENTE_COLUMNS rather than a
    # hardcoded old shape.
    if _table_exists(conn, "propuesta_agente") and _propuesta_agente_needs_experimentos_rebuild(conn):
        _rebuild_propuesta_agente_table(conn)
    # BODY-PROTEINA-1: a third widening (registrar_ingesta, corregir_ingesta, borrar_ingesta), same
    # reusable rebuild.
    if _table_exists(conn, "propuesta_agente") and _propuesta_agente_needs_ingesta_rebuild(conn):
        _rebuild_propuesta_agente_table(conn)
    # BODY-PERIODO-1: a fourth widening (marcar_periodo, cerrar_periodo, corregir_periodo,
    # borrar_periodo), same reusable rebuild.
    if _table_exists(conn, "propuesta_agente") and _propuesta_agente_needs_periodo_rebuild(conn):
        _rebuild_propuesta_agente_table(conn)
    # BODY-COMIDA-1: a fifth widening (registrar_comida), same reusable rebuild.
    if _table_exists(conn, "propuesta_agente") and _propuesta_agente_needs_comida_rebuild(conn):
        _rebuild_propuesta_agente_table(conn)
    # BODY-MCP-APLICAR-1: a plain nullable column, never a CHECK widening on `accion` -- a plain
    # ALTER TABLE ADD COLUMN, not the rebuild dance above.
    ppcols = {r[1] for r in conn.execute("PRAGMA table_info(propuesta_agente)")} if _table_exists(
        conn, "propuesta_agente") else set()
    if ppcols and "aplicada_via" not in ppcols:
        conn.execute(
            "ALTER TABLE propuesta_agente ADD COLUMN aplicada_via TEXT "
            "CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp'))"
        )


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
        "INSERT INTO metric_def (key, label, unit, better, kind, origen, agregado) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET label=excluded.label, unit=excluded.unit, better=excluded.better, "
        "kind=excluded.kind, agregado=excluded.agregado",
        METRIC_DEFS,
    )
    conn.executemany(
        "INSERT INTO period_kind (key, sort_order) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET sort_order=excluded.sort_order",
        PERIOD_KINDS,
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
    ingesta = conn.execute(
        "SELECT id, ts_local, metric_key, amount, estimado, descripcion FROM ingesta "
        "WHERE person_id = ? AND day = ? ORDER BY COALESCE(ts_local, ''), id",
        (person_id, day),
    ).fetchall()
    periodos_dia = periodos(conn, person_id, day, day)
    return {
        "day": day,
        "metrics": [dict(r) for r in metrics],
        "sleep": dict(sleep) if sleep else None,
        "activities": [dict(r) for r in activities],
        "shifts": [dict(r) for r in shifts],
        "ingesta": [dict(r) for r in ingesta],
        "periodos": [dict(r) for r in periodos_dia],
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


def get_aplicar_mcp(conn, person_id: int) -> bool:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: OFF (False) by default, for a person with no
    ajuste_agente row yet, same as every other column of that table."""
    row = conn.execute("SELECT aplicar_mcp FROM ajuste_agente WHERE person_id = ?", (person_id,)).fetchone()
    return bool(row["aplicar_mcp"]) if row else False


def set_aplicar_mcp(conn, person_id: int, value: bool) -> None:
    """Changed only by body/rutas/ajustes.py's own /settings/aplicar_mcp POST, from the person's
    own session (ruling 1) -- never by body/mcp.py, never by an in-app assistant tool, neither of
    which imports this function."""
    if not isinstance(value, bool):
        raise TypeError(f"aplicar_mcp must be a bool, got {type(value).__name__}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO ajuste_agente (person_id, aplicar_mcp, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET aplicar_mcp = excluded.aplicar_mcp, "
            "updated_at = excluded.updated_at",
            (person_id, int(value), now),
        )


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


# BODY-MOMENTOS-PULL-1: intraday heart-rate/stress payloads, stored under raw_payload's existing
# (person_id, day, kind, source) primary key -- no schema change. Garmin's own `heart_rate`/
# `stress` sub-dicts (garmin_adapter.fetch_intraday's `raw`) are stored exactly as fetched,
# including None/{} for a day out of Garmin's ~130-140 day retention window: an EXPLICIT empty
# marker (chosen over storing nothing at all) so that day is never re-fetched -- pull.traer_
# intradia's "already stored" check reads the same row. Parsing happens on READ
# (garmin_adapter.parse_heart_rate_series/parse_stress_series), so a parser fix never needs a
# re-pull.
INTRADIA_KINDS = ("intradia_fc", "intradia_estres")

# BODY-MOMENTOS-DIA-COMPLETO-1: the watch syncs to the phone in batches, so the last hours of a
# day can reach Garmin's servers hours after that day's local midnight. A fetch taken inside this
# margin after the day's close may be missing those last hours, so it does not count as the day
# being fully stored -- measured from BODY-MOMENTOS-PULL-1 shipping without this margin
# (BODY-EXPERIMENTOS-1-INV: a day fetched mid-evening, or fetched before the watch had synced the
# night, was marked stored forever and never completed).
INTRADIA_MARGEN_CIERRE_H = 6

_INTRADIA_PARSER_NAMES = {
    "intradia_fc": "parse_heart_rate_series",
    "intradia_estres": "parse_stress_series",
}


def guardar_intradia(conn: sqlite3.Connection, person_id: int, day: str, kind: str, payload,
                      source: str = garmin_adapter.SOURCE) -> None:
    """INSERT OR REPLACE under raw_payload's (person_id, day, kind, source) key -- a different
    `source` is a different row, never a replacement of another source's row for the same day and
    kind. `source` defaults to the live API's own tag; body.importar_garmin passes garmin_adapter.
    SOURCE_EXPORT (BODY-GARMIN-IMPORT-1) so an import can never overwrite or delete an API row."""
    if kind not in INTRADIA_KINDS:
        raise ValueError(f"unknown intradia kind: {kind!r}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, day, kind, source, now, json.dumps(payload)),
        )


def intradia_almacenado(conn: sqlite3.Connection, person_id: int, day: str) -> bool:
    """Whether this day's intraday payloads are stored AND COMPLETE through the live API --
    fetched at least INTRADIA_MARGEN_CIERRE_H hours after `day`'s end in the person's own zone
    (the next local midnight, converted to UTC), including an out-of-retention day's empty marker
    fetched that late. False for a day never fetched, AND for a day fetched too early to be
    trusted yet -- pull.traer_intradia re-fetches that case, overwriting the earlier, partial row.
    Both kinds are always written together by pull.traer_intradia at the same fetched_at, so
    checking one (intradia_fc) is enough. Filtered to source=garmin_adapter.SOURCE on purpose
    (BODY-GARMIN-IMPORT-1): an export-only day must read as "not yet pulled through the API" so
    the missing-days job (pull._pull_intradia_pendientes) still fetches it when it is inside
    Garmin's API reach, and never skips a day the API has not actually covered."""
    row = conn.execute(
        "SELECT fetched_at FROM raw_payload WHERE person_id = ? AND day = ? AND kind = 'intradia_fc' "
        "AND source = ?",
        (person_id, day, garmin_adapter.SOURCE),
    ).fetchone()
    if row is None:
        return False
    zona = person(conn, person_id)["timezone"]
    cierre_local = datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(),
                                     tzinfo=ZoneInfo(zona))
    umbral_utc = cierre_local.astimezone(timezone.utc) + timedelta(hours=INTRADIA_MARGEN_CIERRE_H)
    fetched_at = datetime.fromisoformat(row["fetched_at"])
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return fetched_at >= umbral_utc


def intradia(conn: sqlite3.Connection, person_id: int, day: str, kind: str, zona: str) -> list[tuple[str, float]]:
    """The stored (local_iso, value) series for one day and kind, parsed on read with the
    adapter's own parser. [] for a day never stored, or stored as an empty/null marker.
    Precedence (BODY-GARMIN-IMPORT-1): when both the live API and an import
    (garmin_adapter.SOURCE_EXPORT) hold a row for the same day and kind, the API's own row always
    wins -- ORDER BY picks it first when both exist, without needing a second query."""
    if kind not in INTRADIA_KINDS:
        raise ValueError(f"unknown intradia kind: {kind!r}")
    row = conn.execute(
        "SELECT payload FROM raw_payload WHERE person_id = ? AND day = ? AND kind = ? "
        "ORDER BY (source != ?) LIMIT 1",
        (person_id, day, kind, garmin_adapter.SOURCE),
    ).fetchone()
    if row is None:
        return []
    payload = json.loads(row["payload"])
    parser = getattr(garmin_adapter, _INTRADIA_PARSER_NAMES[kind])
    return list(parser(payload, zona))


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


class ExperimentoInvalido(ValueError):
    """One of `validar_experimento`'s three shapes was wrong -- a ValueError subclass (so existing
    `except ValueError`/`assertRaises(ValueError)` callers are untouched), but a Body-owned class
    (body/errores.py's `texto_propio` accepts it) since its message never echoes anything but
    Body's own field names and fixed phrases, never caller-supplied text (BODY-EXPERIMENTOS-
    REGISTRO-1: `_proponer_experimento` surfaces it to the person through this, not a bare
    ValueError, which `tests/test_errores_texto.py`'s AST guard refuses)."""


def _campo_invalido(campo: str, detalle: str) -> None:
    raise ExperimentoInvalido(f"{campo}: {detalle}")


def validar_experimento(metricas, ventana, filtro_dias) -> None:
    """The one place crear_experimento's three JSON shapes are checked. Pure (no connection), so a
    caller can validate before ever touching the database. Raises ExperimentoInvalido (a
    ValueError) naming the invalid
    field first -- ventana, filtro_dias or metricas.

    ventana: {"tipo": "dia"} or {"tipo": "intradia", "ancla": "despertar" | "momento",
    "antes_min": int >= 0 | null, "despues_min": int > 0}.
    filtro_dias: {"turno_en": [kinds] | null, "turno_fuera": [kinds] | null,
    "excluir_tras_noche": bool} -- kinds from SHIFT_KINDS (this module's own vocabulary;
    shift_import.VALID_KINDS reads it from here, not the other way round).
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
    for campo in ("turno_en", "turno_fuera"):
        valor = filtro_dias[campo]
        if valor is not None and (
            not isinstance(valor, list) or not valor or any(k not in SHIFT_KINDS for k in valor)
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
                       fecha_inicio: str | None = None, fecha_fin: str | None = None, estado: str = "activo") -> int:
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
            "dias_prueba_objetivo, fecha_inicio, fecha_fin, estado, creado_en) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (person_id, titulo, habito_id, json.dumps(metricas), json.dumps(ventana), json.dumps(filtro_dias),
             dias_prueba_objetivo, fecha_inicio, fecha_fin, estado, now),
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


def cerrar_experimento(conn: sqlite3.Connection, person_id: int, experimento_id: int,
                        fecha_fin: str | None = None) -> None:
    """Closes one of this person's own experiments (BODY-EXPERIMENTOS-PANTALLA-1): sets
    estado='cerrado' and fecha_fin to the given day, or today (UTC date) if none is given. Refuses
    (ValueError) an experimento_id that is not this person's own, or one already closed -- the
    screen's /experiments/{id}/close route turns either into the same 404 an unknown id gets.
    `experimentos.veredicto` already caps its test window at fecha_fin, so a closed experiment
    keeps the verdict it had the moment it closed."""
    row = conn.execute(
        "SELECT estado FROM experimento WHERE id = ? AND person_id = ?", (experimento_id, person_id)
    ).fetchone()
    if row is None:
        raise ValueError("experimento_id: not this person's own experiment")
    if row["estado"] == "cerrado":
        raise ValueError("experimento_id: already closed")
    fecha_fin = fecha_fin or datetime.now(timezone.utc).isoformat(timespec="seconds")[:10]
    with conn:
        conn.execute(
            "UPDATE experimento SET estado = 'cerrado', fecha_fin = ? WHERE id = ? AND person_id = ?",
            (fecha_fin, experimento_id, person_id),
        )


def borrar_experimento(conn: sqlite3.Connection, person_id: int, experimento_id: int) -> None:
    """Deletes one of this person's own experiments -- refused (ValueError) if it is not theirs, or
    if a day has already been marked against it (`experimento_registro`, the only row that points
    back at `experimento.id`). BODY-EXPERIMENTOS-REGISTRO-1: `proponer_experimento`'s undo checks
    the same condition first as its drift check, so this is defense-in-depth, not the only guard."""
    propio = conn.execute(
        "SELECT 1 FROM experimento WHERE id = ? AND person_id = ?", (experimento_id, person_id)
    ).fetchone()
    if propio is None:
        raise ValueError("experimento_id: not this person's own experiment")
    tiene_registro = conn.execute(
        "SELECT 1 FROM experimento_registro WHERE experimento_id = ?", (experimento_id,)
    ).fetchone()
    if tiene_registro is not None:
        raise ValueError("experimento_id: has registered days, cannot be deleted")
    with conn:
        conn.execute("DELETE FROM experimento WHERE id = ? AND person_id = ?", (experimento_id, person_id))


def crear_momento(conn: sqlite3.Connection, person_id: int, ts_local: str, kind: str,
                   note: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO moment (person_id, ts_local, kind, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (person_id, ts_local, kind, note, now),
        )
    return cur.lastrowid


# ---- ingesta (BODY-PROTEINA-1): registrar_ingesta/corregir_ingesta/borrar_ingesta are the ONE
# writer of a (person, day, metric_key) day total -- each recomputes the sum of that day's rows and
# writes it to daily_metric with source='self' through set_metric, removing the row entirely once
# the day has no entries left (set_metric(value=None) already does that).

def _recalcular_ingesta_dia(conn: sqlite3.Connection, person_id: int, day: str, metric_key: str) -> None:
    total = conn.execute(
        "SELECT SUM(amount) FROM ingesta WHERE person_id = ? AND day = ? AND metric_key = ?",
        (person_id, day, metric_key),
    ).fetchone()[0]
    set_metric(conn, day, metric_key, total, "self", person_id)


def registrar_ingesta(conn: sqlite3.Connection, person_id: int, day: str, metric_key: str, amount: float,
                       estimado: bool, descripcion: str | None = None, ts_local: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO ingesta (person_id, day, ts_local, metric_key, amount, estimado, descripcion, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (person_id, day, ts_local, metric_key, amount, int(bool(estimado)), descripcion, now, now),
        )
        _recalcular_ingesta_dia(conn, person_id, day, metric_key)
    return cur.lastrowid


def corregir_ingesta(conn: sqlite3.Connection, person_id: int, ingesta_id: int, amount: float) -> None:
    fila = conn.execute(
        "SELECT person_id, day, metric_key FROM ingesta WHERE id = ?", (ingesta_id,)
    ).fetchone()
    if fila is None or fila["person_id"] != person_id:
        raise ValueError("ingesta_id: not this person's own entry")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute("UPDATE ingesta SET amount = ?, updated_at = ? WHERE id = ?", (amount, now, ingesta_id))
        _recalcular_ingesta_dia(conn, person_id, fila["day"], fila["metric_key"])


def borrar_ingesta(conn: sqlite3.Connection, person_id: int, ingesta_id: int) -> None:
    fila = conn.execute(
        "SELECT person_id, day, metric_key FROM ingesta WHERE id = ?", (ingesta_id,)
    ).fetchone()
    if fila is None or fila["person_id"] != person_id:
        raise ValueError("ingesta_id: not this person's own entry")
    with conn:
        conn.execute("DELETE FROM ingesta WHERE id = ?", (ingesta_id,))
        _recalcular_ingesta_dia(conn, person_id, fila["day"], fila["metric_key"])


# ---- comida (BODY-COMIDA-1): one spoken meal = one `comida` row + N `ingesta` children, one per
# nutrient (ruling 3). crear_comida/borrar_comida are the ONE writer of `comida` -- each recomputes
# every touched metric_key's daily total through _recalcular_ingesta_dia, the same writer
# registrar_ingesta/corregir_ingesta/borrar_ingesta already use, so a meal's amounts sum into
# daily_metric exactly like a standalone intake entry does.

def crear_comida(conn: sqlite3.Connection, person_id: int, day: str, cantidades: list[dict],
                  descripcion: str | None = None, ts_local: str | None = None) -> int:
    """`cantidades`: a list of {metric_key, amount, estimado}, already validated by the caller
    (herramientas._registrar_comida) -- one per key, amount > 0. Writes the `comida` row and every
    child `ingesta` row in one transaction, recalculating each touched key's day total."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO comida (person_id, day, ts_local, descripcion, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, day, ts_local, descripcion, now, now),
        )
        comida_id = cur.lastrowid
        for c in cantidades:
            conn.execute(
                "INSERT INTO ingesta (person_id, day, ts_local, metric_key, amount, estimado, comida_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (person_id, day, ts_local, c["metric_key"], c["amount"], int(bool(c.get("estimado", True))),
                 comida_id, now, now),
            )
            _recalcular_ingesta_dia(conn, person_id, day, c["metric_key"])
    return comida_id


def borrar_comida(conn: sqlite3.Connection, person_id: int, comida_id: int) -> None:
    """Deletes a meal and every one of its own `ingesta` children together (ruling 3's "Deshacer
    removes them together"), recalculating each touched key's day total afterwards."""
    fila = conn.execute(
        "SELECT day FROM comida WHERE id = ? AND person_id = ?", (comida_id, person_id)
    ).fetchone()
    if fila is None:
        raise ValueError("comida_id: not this person's own meal")
    day = fila["day"]
    claves = [r["metric_key"] for r in conn.execute(
        "SELECT DISTINCT metric_key FROM ingesta WHERE comida_id = ?", (comida_id,))]
    with conn:
        conn.execute("DELETE FROM ingesta WHERE comida_id = ?", (comida_id,))
        conn.execute("DELETE FROM comida WHERE id = ?", (comida_id,))
        for metric_key in claves:
            _recalcular_ingesta_dia(conn, person_id, day, metric_key)


def comidas_del_dia(conn: sqlite3.Connection, person_id: int, day: str) -> list[dict]:
    """One entry per MEAL that day (ruling 5): a `comida` row's own children grouped under its own
    description/time, and each legacy comida_id-NULL `ingesta` row standing alone as its own
    one-item meal, keeping its own descripcion/ts_local exactly as before -- ordered the same way
    `load_day`'s own ingesta list is (COALESCE(ts_local, ''), id), so an untimed meal sorts last."""
    rows = conn.execute(
        "SELECT i.id, i.ts_local, i.descripcion AS ingesta_descripcion, i.metric_key, i.amount, "
        "i.estimado, i.comida_id, c.descripcion AS comida_descripcion, c.ts_local AS comida_ts_local "
        "FROM ingesta i LEFT JOIN comida c ON c.id = i.comida_id "
        "WHERE i.person_id = ? AND i.day = ? ORDER BY COALESCE(i.ts_local, ''), i.id",
        (person_id, day),
    ).fetchall()
    grupos: dict = {}
    orden: list = []
    for r in rows:
        es_comida = r["comida_id"] is not None
        clave = ("comida", r["comida_id"]) if es_comida else ("ingesta", r["id"])
        if clave not in grupos:
            grupos[clave] = {
                "ts_local": r["comida_ts_local"] if es_comida else r["ts_local"],
                "descripcion": r["comida_descripcion"] if es_comida else r["ingesta_descripcion"],
                "cantidades": [],
            }
            orden.append(clave)
        grupos[clave]["cantidades"].append(
            {"metric_key": r["metric_key"], "amount": r["amount"], "estimado": bool(r["estimado"])})
    return [grupos[c] for c in orden]


# ---- periodos (BODY-PERIODO-1): a marked stretch of days (illness, travel, injury) that a
# baseline's usual range and an experiment's counted days leave out -- never re-weighted. The
# person's own note is kept like moment.note and ingesta.descripcion, never printed or logged.

# A baseline/experiment sweep calls periodos()/dias_marcados() once per day THROUGH THE SAME
# CONNECTION (readiness.compute's own 56-strip-day loop behind /shifts, BODY-RENDIMIENTO-1/2) --
# periods are rare (an illness, a trip), so one real query per connection beats one per call.
# Keyed by id(conn), never by the Connection object itself (sqlite3.Connection accepts no extra
# attribute and is not weak-referenceable); connect() purges any entry left under a reused id
# before this cache could ever be read through the new connection it names (see connect()'s own
# comment), so a cache hit here can only be this same connection's own earlier read.
_PERIODOS_LOCK = threading.Lock()
_PERIODOS_CACHE: dict[tuple[int, int], tuple[list[sqlite3.Row], str | None]] = {}


def _periodos_de_persona(conn: sqlite3.Connection, person_id: int) -> tuple[list[sqlite3.Row], str | None]:
    """This person's own period rows, ORDER BY start_day, paired with their timezone -- read in
    the SAME statement (a JOIN), not a separate person() call, so dias_marcados (below) no longer
    re-reads the timezone on every call while an open period overlaps the range (BODY-PERIODO-
    PANTALLAS-1-B: that per-call read turned /day/{d}'s 38 statements into 55 against a ceiling of
    45). zona is None when this person has no periods at all -- dias_marcados never needs it then,
    so a person with no periods still pays no timezone lookup, same as before."""
    key = (id(conn), person_id)
    with _PERIODOS_LOCK:
        cached = _PERIODOS_CACHE.get(key)
    if cached is None:
        rows = conn.execute(
            "SELECT period.*, person.timezone AS _zona FROM period "
            "JOIN person ON person.id = period.person_id "
            "WHERE period.person_id = ? ORDER BY period.start_day",
            (person_id,),
        ).fetchall()
        zona = rows[0]["_zona"] if rows else None
        cached = (rows, zona)
        with _PERIODOS_LOCK:
            _PERIODOS_CACHE[key] = cached
    return cached


def invalidar_periodos_cache(conn: sqlite3.Connection, person_id: int) -> None:
    with _PERIODOS_LOCK:
        _PERIODOS_CACHE.pop((id(conn), person_id), None)


def _solapan(rows: list[sqlite3.Row], desde: str, hasta: str) -> list[sqlite3.Row]:
    return [r for r in rows if r["start_day"] <= hasta and (r["end_day"] is None or r["end_day"] >= desde)]


def periodos(conn: sqlite3.Connection, person_id: int, desde: str, hasta: str) -> list[sqlite3.Row]:
    """This person's own periods overlapping [desde, hasta] -- an open period (end_day NULL)
    always overlaps anything that is not entirely before its start_day. Filtered in memory from
    _periodos_de_persona's own cached full list, same rows and order a fresh ranged SELECT would
    give (that list is already ORDER BY start_day, and filtering never reorders)."""
    rows, _ = _periodos_de_persona(conn, person_id)
    return _solapan(rows, desde, hasta)


def dias_marcados(conn: sqlite3.Connection, person_id: int, desde: str, hasta: str) -> set[str]:
    """ISO days in [desde, hasta] covered by one of this person's own periods -- an open period
    (end_day IS NULL) counts through TODAY in the person's own timezone, never past it. The zone
    comes from _periodos_de_persona's own cache (one read per connection, at most), never a fresh
    person() call here -- the common case (no periods at all, or only closed ones) still costs no
    timezone lookup, since zona is only read above when this person has a period at all. "today"
    itself is always computed fresh from that cached zone at call time, so a midnight crossing
    still moves it -- only the ZONE is cached, never the date."""
    rows, zona = _periodos_de_persona(conn, person_id)
    overlapping = _solapan(rows, desde, hasta)
    if not overlapping:
        return set()
    hoy: str | None = None
    out: set[str] = set()
    for r in overlapping:
        fin_val = r["end_day"]
        if fin_val is None:
            if hoy is None:
                hoy = datetime.now(ZoneInfo(zona)).date().isoformat()
            fin_val = hoy
        ini = max(r["start_day"], desde)
        fin = min(fin_val, hasta)
        if fin < ini:
            continue
        d, limite = date.fromisoformat(ini), date.fromisoformat(fin)
        while d <= limite:
            out.add(d.isoformat())
            d += timedelta(1)
    return out


def crear_periodo(conn: sqlite3.Connection, person_id: int, kind: str, start_day: str,
                   end_day: str | None = None, note: str | None = None) -> int:
    if conn.execute("SELECT 1 FROM period_kind WHERE key = ?", (kind,)).fetchone() is None:
        raise ValueError(f"unknown period kind: {kind!r}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO period (person_id, kind, start_day, end_day, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (person_id, kind, start_day, end_day, note, now, now),
        )
    invalidar_periodos_cache(conn, person_id)
    return cur.lastrowid


_SIN_DAR = object()  # sentinel: "this field was not given", distinct from an intentional None


def actualizar_periodo(conn: sqlite3.Connection, person_id: int, period_id: int, *, kind: str | None = None,
                        start_day: str | None = None, end_day=_SIN_DAR, note=_SIN_DAR) -> None:
    """Updates one of this person's own periods -- only the fields actually given change. end_day
    and note each use the _SIN_DAR sentinel rather than a None default, since both are legitimately
    settable to None (end_day -> reopened, note -> cleared), unlike kind/start_day, which never
    are. Refuses a period_id that is not this person's own."""
    propio = conn.execute("SELECT 1 FROM period WHERE id = ? AND person_id = ?", (period_id, person_id)).fetchone()
    if propio is None:
        raise ValueError("period_id: not this person's own period")
    if kind is not None and conn.execute("SELECT 1 FROM period_kind WHERE key = ?", (kind,)).fetchone() is None:
        raise ValueError(f"unknown period kind: {kind!r}")
    sets, vals = [], []
    if kind is not None:
        sets.append("kind = ?")
        vals.append(kind)
    if start_day is not None:
        sets.append("start_day = ?")
        vals.append(start_day)
    if end_day is not _SIN_DAR:
        sets.append("end_day = ?")
        vals.append(end_day)
    if note is not _SIN_DAR:
        sets.append("note = ?")
        vals.append(note)
    if not sets:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sets.append("updated_at = ?")
    vals.append(now)
    vals += [period_id, person_id]
    with conn:
        conn.execute(f"UPDATE period SET {', '.join(sets)} WHERE id = ? AND person_id = ?", vals)
    invalidar_periodos_cache(conn, person_id)


def borrar_periodo(conn: sqlite3.Connection, person_id: int, period_id: int) -> None:
    fila = conn.execute("SELECT 1 FROM period WHERE id = ? AND person_id = ?", (period_id, person_id)).fetchone()
    if fila is None:
        raise ValueError("period_id: not this person's own period")
    with conn:
        conn.execute("DELETE FROM period WHERE id = ? AND person_id = ?", (period_id, person_id))
    invalidar_periodos_cache(conn, person_id)
