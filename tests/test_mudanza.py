"""body/mudanza.py: move one person's data from a PC database into this process's own database
(BODY-MUDANZA-HERRAMIENTA-1). Invented demo data only -- never a real health figure."""
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import backup, config, cuenta, db, mudanza, privacidad, productos as productos_mod

CUENTA_SUB = "sub-mudanza-test-1"
NOW = "2026-11-01T08:00:00+00:00"


def _populate_full(conn: sqlite3.Connection, person_id: int) -> dict:
    """Invented demo data touching every category the registry has to handle: a normal goal, a
    household-shared goal (ruling 5), a habit assigned to another person (ruling 5), surrogate-id
    chains (goal -> habit -> experimento -> experimento_registro, conversacion -> mensaje/
    propuesta, comida -> ingesta, checkin -> checkin_nota), and a figura_compartida row (always
    other-person)."""
    ids: dict = {}

    def last_id() -> int:
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
        "target_value, baseline_value, start_day, status, visibility) VALUES "
        "(?, NULL, 'body', 'Lower resting HR', 'metric', 'resting_hr', 'lower', 55, 60, "
        "'2026-10-01', 'active', 'private')",
        (person_id,),
    )
    ids["goal"] = last_id()

    conn.execute(
        "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
        "target_value, baseline_value, start_day, status, visibility) VALUES "
        "(?, 'HOGAR-1', 'body', 'Shared goal', 'metric', 'resting_hr', 'lower', 55, 60, "
        "'2026-10-01', 'active', 'household')",
        (person_id,),
    )
    ids["goal_hogar"] = last_id()

    conn.execute(
        "INSERT INTO habit (goal_id, title, cadence, assignee_person_id, active) "
        "VALUES (?, 'Walk', '{\"per\": \"week\", \"times\": 3}', NULL, 1)",
        (ids["goal"],),
    )
    ids["habit"] = last_id()

    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Someone else')")
    conn.execute(
        "INSERT INTO habit (goal_id, title, cadence, assignee_person_id, active) "
        "VALUES (?, 'Spouse habit', '{\"per\": \"week\", \"times\": 1}', 2, 1)",
        (ids["goal"],),
    )
    ids["habit_other_person"] = last_id()

    conn.execute(
        "INSERT INTO habit (goal_id, title, cadence, assignee_person_id, active) "
        "VALUES (?, 'Household habit', '{\"per\": \"week\", \"times\": 2}', NULL, 1)",
        (ids["goal_hogar"],),
    )
    ids["habit_under_hogar_goal"] = last_id()

    conn.execute(
        "INSERT INTO habit_check (habit_id, person_id, day, done) VALUES (?, ?, '2026-10-05', 1)",
        (ids["habit"], person_id),
    )
    conn.execute(
        "INSERT INTO goal_check (goal_id, person_id, week_start, metric_avg, on_track, "
        "habits_done, habits_due, rule_version) VALUES (?, ?, '2026-10-05', 58.0, 1, 2, 3, 'v1')",
        (ids["goal"], person_id),
    )
    conn.execute(
        "INSERT INTO goal_enlace (goal_id, app, ref, creado_at) VALUES (?, 'finanzas', 'ref-1', ?)",
        (ids["goal"], NOW),
    )
    conn.execute(
        "INSERT INTO goal_enlace (goal_id, app, ref, creado_at) VALUES (?, 'finanzas', 'ref-2', ?)",
        (ids["goal_hogar"], NOW),
    )
    conn.execute(
        "INSERT INTO plan (person_id, hogar_id, goal_id, day, title, status) "
        "VALUES (?, NULL, ?, '2026-10-06', 'Run', 'proposed')",
        (person_id, ids["goal"]),
    )
    conn.execute(
        "INSERT INTO plan (person_id, hogar_id, goal_id, day, title, status) "
        "VALUES (?, 'HOGAR-1', ?, '2026-10-06', 'Shared plan', 'proposed')",
        (person_id, ids["goal"]),
    )

    conn.execute(
        "INSERT INTO conversacion_agente (person_id, creada_en, titulo) VALUES (?, ?, 'Chat 1')",
        (person_id, NOW),
    )
    ids["conv"] = last_id()
    conn.execute(
        "INSERT INTO mensaje_agente (conversacion_id, person_id, rol, contenido_json, creado_en) "
        "VALUES (?, ?, 'usuario', '{}', ?)",
        (ids["conv"], person_id, NOW),
    )
    conn.execute(
        "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, objetivo_id, "
        "despues_json, estado, creada_en, expira_en) "
        "VALUES (?, ?, 'proponer_meta', ?, '{}', 'propuesta', ?, ?)",
        (person_id, ids["conv"], ids["goal"], NOW, NOW),
    )

    conn.execute(
        "INSERT INTO uso_agente (person_id, creado_en, proveedor, modelo) VALUES (?, ?, 'anthropic', 'demo')",
        (person_id, NOW),
    )
    conn.execute(
        "INSERT INTO uso_herramienta_externa (person_id, herramienta, llamada_en, caller) "
        "VALUES (?, 'leer_hoy', ?, 'mcp')",
        (person_id, NOW),
    )

    conn.execute(
        "INSERT INTO daily_metric (person_id, day, metric_key, value, source) "
        "VALUES (?, '2026-10-05', 'resting_hr', 58.0, 'garmin')",
        (person_id,),
    )
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) "
        "VALUES (?, '2026-10-05', '2026-10-04T23:00:00', '2026-10-05T06:30:00')",
        (person_id,),
    )
    # BODY-DESPERTAR-REAL-6A-R2 ruling 1: match_cols now includes `tipo`, so these two rows for the
    # same day are distinct records, never a collision.
    conn.execute(
        "INSERT INTO despertar_correccion (person_id, day, tipo, hora_local, modelo_version, origen, "
        "creada_en) VALUES (?, '2026-10-05', 'sueno', '06:15', 'despertar-real-v2', 'persona', ?)",
        (person_id, NOW),
    )
    conn.execute(
        "INSERT INTO despertar_correccion (person_id, day, tipo, hora_local, modelo_version, origen, "
        "creada_en) VALUES (?, '2026-10-05', 'levantado', '07:00', 'despertar-real-v2', 'persona', ?)",
        (person_id, NOW),
    )
    conn.execute(
        "INSERT INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, '2026-10-05', 'user_summary', 'garmin', ?, '{}')",
        (person_id, NOW),
    )
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
        "VALUES (?, 'act-1', '2026-10-05', '2026-10-05T07:00:00', 'strength', 'Gym', 1800, 'garmin')",
        (person_id,),
    )
    conn.execute(
        "INSERT INTO workout_set (person_id, activity_source, activity_source_id, set_order, "
        "set_type, corrected_by_person, created_at, updated_at) "
        "VALUES (?, 'garmin', 'act-1', 1, 'active', 0, ?, ?)",
        (person_id, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, "
        "day_count, created_at, kind) VALUES (?, 'turnos.pdf', ?, ?, 'confirmed', 1, ?, 'import')",
        (person_id, NOW, NOW, NOW),
    )
    ids["shift_batch"] = last_id()
    conn.execute(
        "INSERT INTO shift_batch_item (batch_id, day, kind, start_local, end_local) "
        "VALUES (?, '2026-10-06', 'night', '2026-10-06T22:00:00', '2026-10-07T06:00:00')",
        (ids["shift_batch"],),
    )
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, end_local, source, batch_id) "
        "VALUES (?, '2026-10-06', 'night', '2026-10-06T22:00:00', '2026-10-07T06:00:00', "
        "'import', ?)",
        (person_id, ids["shift_batch"]),
    )
    conn.execute(
        "INSERT INTO shift_pattern (person_id, start_day, cycle, updated_at) "
        "VALUES (?, '2026-10-01', '[]', ?)",
        (person_id, NOW),
    )
    conn.execute("INSERT INTO ajuste_agente (person_id, updated_at) VALUES (?, ?)", (person_id, NOW))
    conn.execute("INSERT INTO tope_agente (person_id) VALUES (?)", (person_id,))
    conn.execute(
        "INSERT INTO consentimiento (person_id, version, accepted_at) "
        "VALUES (?, ?, ?)",
        (person_id, privacidad.CONSENT_VERSION, NOW),
    )
    conn.execute(
        "INSERT INTO moment (person_id, ts_local, kind, created_at) VALUES (?, ?, 'nota', ?)",
        (person_id, NOW, NOW),
    )
    ids["moment"] = last_id()
    conn.execute(
        "INSERT INTO experimento (person_id, titulo, habito_id, metricas, ventana, filtro_dias, "
        "fecha_inicio, estado, creado_en) VALUES (?, 'Prueba', ?, '[]', '{}', '{}', "
        "'2026-10-01', 'activo', ?)",
        (person_id, ids["habit"], NOW),
    )
    ids["experimento"] = last_id()
    conn.execute(
        "INSERT INTO experimento_registro (experimento_id, person_id, day, ocurrio, momento_id, "
        "creado_en) VALUES (?, ?, '2026-10-05', 1, ?, ?)",
        (ids["experimento"], person_id, ids["moment"], NOW),
    )
    conn.execute(
        "INSERT INTO comida (person_id, day, created_at, updated_at) VALUES (?, '2026-10-05', ?, ?)",
        (person_id, NOW, NOW),
    )
    ids["comida"] = last_id()
    conn.execute(
        "INSERT INTO ingesta (person_id, day, metric_key, amount, comida_id, created_at, updated_at) "
        "VALUES (?, '2026-10-05', 'kcal', 500, ?, ?, ?)",
        (person_id, ids["comida"], NOW, NOW),
    )
    conn.execute(
        "INSERT INTO period (person_id, kind, start_day, created_at, updated_at) "
        "VALUES (?, 'viaje', '2026-10-01', ?, ?)",
        (person_id, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, "
        "modelo_version, minutos, computed_at) VALUES (?, 'garmin', 'act-1', 'v1', 30.0, ?)",
        (person_id, NOW),
    )
    conn.execute(
        "INSERT INTO turno_fc (person_id, day, kind, modelo_version, computed_at) "
        "VALUES (?, '2026-10-06', 'night', 'v1', ?)",
        (person_id, NOW),
    )
    conn.execute(
        "INSERT INTO checkin (person_id, day, momento, mood, created_at) "
        "VALUES (?, '2026-10-05', 'wake', 4, ?)",
        (person_id, NOW),
    )
    ids["checkin"] = last_id()
    conn.execute(
        "INSERT INTO checkin_nota (id, person_id, day, nota, created_at) "
        "VALUES (?, ?, '2026-10-05', 'felt ok', ?)",
        (ids["checkin"], person_id, NOW),
    )
    conn.execute(
        "INSERT INTO figura_compartida (person_id, metric_key, hogar_id, shared_at) "
        "VALUES (?, 'resting_hr', 'HOGAR-1', ?)",
        (person_id, NOW),
    )
    conn.commit()
    return ids


# BODY-MUDANZA-ORIGEN-ANTIGUO-1-R ruling 2: a GENUINELY older schema, not a hand-trimmed one --
# body/db.py exactly as it was at commit 3ef9e8e (the build the host ran until 2026-11-14), read
# from git history and run through its OWN init(), never retyped and never the current init().
_OLD_SCHEMA_COMMIT = "3ef9e8e"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_old_db_module():
    result = subprocess.run(
        ["git", "show", f"{_OLD_SCHEMA_COMMIT}:body/db.py"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    tmp_dir = Path(tempfile.mkdtemp(prefix="body-mudanza-old-schema-"))
    tmp_file = tmp_dir / "db_3ef9e8e.py"
    tmp_file.write_text(result.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("body._old_db_3ef9e8e", tmp_file)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "body"  # so its own `from . import config, garmin_adapter` resolves
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _old_propuesta_actions(conn: sqlite3.Connection) -> list[str]:
    """Read back, from the live schema this seeded source actually has, every `accion` its own
    CHECK allowed -- not retyped from the source file."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'propuesta_agente'"
    ).fetchone()[0]
    match = re.search(r"accion IN\s*\(([^)]*)\)", sql, re.DOTALL)
    return [v.strip().strip("'") for v in match.group(1).split(",")]


def _populate_old_schema(conn: sqlite3.Connection, person_id: int, actions: list[str]) -> dict:
    """Seeds every person-scoped table a GENUINELY older install (commit 3ef9e8e) actually has --
    comida, ingesta, period, checkin, checkin_nota, nutrient_target, fuente_importacion,
    uso_herramienta_externa and the four horizonte_* tables do not exist at that schema yet, so
    none of them are seeded here. A propuesta_agente row for EVERY accion the older CHECK allowed
    (ruling 2), so the widening rebuild below runs against a non-empty table."""
    ids: dict = {}

    def last_id() -> int:
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
        "target_value, baseline_value, start_day, status, visibility) VALUES "
        "(?, NULL, 'body', 'Lower resting HR', 'metric', 'resting_hr', 'lower', 55, 60, "
        "'2026-10-01', 'active', 'private')",
        (person_id,),
    )
    ids["goal"] = last_id()

    conn.execute(
        "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
        "target_value, baseline_value, start_day, status, visibility) VALUES "
        "(?, 'HOGAR-1', 'body', 'Shared goal', 'metric', 'resting_hr', 'lower', 55, 60, "
        "'2026-10-01', 'active', 'household')",
        (person_id,),
    )
    ids["goal_hogar"] = last_id()

    conn.execute(
        "INSERT INTO habit (goal_id, title, cadence, assignee_person_id, active) "
        "VALUES (?, 'Walk', '{\"per\": \"week\", \"times\": 3}', NULL, 1)",
        (ids["goal"],),
    )
    ids["habit"] = last_id()

    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Someone else')")
    conn.execute(
        "INSERT INTO habit (goal_id, title, cadence, assignee_person_id, active) "
        "VALUES (?, 'Spouse habit', '{\"per\": \"week\", \"times\": 1}', 2, 1)",
        (ids["goal"],),
    )
    ids["habit_other_person"] = last_id()

    conn.execute(
        "INSERT INTO habit (goal_id, title, cadence, assignee_person_id, active) "
        "VALUES (?, 'Household habit', '{\"per\": \"week\", \"times\": 2}', NULL, 1)",
        (ids["goal_hogar"],),
    )
    ids["habit_under_hogar_goal"] = last_id()

    conn.execute(
        "INSERT INTO habit_check (habit_id, person_id, day, done) VALUES (?, ?, '2026-10-05', 1)",
        (ids["habit"], person_id),
    )
    conn.execute(
        "INSERT INTO goal_check (goal_id, person_id, week_start, metric_avg, on_track, "
        "habits_done, habits_due, rule_version) VALUES (?, ?, '2026-10-05', 58.0, 1, 2, 3, 'v1')",
        (ids["goal"], person_id),
    )
    conn.execute(
        "INSERT INTO goal_enlace (goal_id, app, ref, creado_at) VALUES (?, 'finanzas', 'ref-1', ?)",
        (ids["goal"], NOW),
    )
    conn.execute(
        "INSERT INTO goal_enlace (goal_id, app, ref, creado_at) VALUES (?, 'finanzas', 'ref-2', ?)",
        (ids["goal_hogar"], NOW),
    )
    conn.execute(
        "INSERT INTO plan (person_id, hogar_id, goal_id, day, title, status) "
        "VALUES (?, NULL, ?, '2026-10-06', 'Run', 'proposed')",
        (person_id, ids["goal"]),
    )
    conn.execute(
        "INSERT INTO plan (person_id, hogar_id, goal_id, day, title, status) "
        "VALUES (?, 'HOGAR-1', ?, '2026-10-06', 'Shared plan', 'proposed')",
        (person_id, ids["goal"]),
    )

    conn.execute(
        "INSERT INTO conversacion_agente (person_id, creada_en, titulo) VALUES (?, ?, 'Chat 1')",
        (person_id, NOW),
    )
    ids["conv"] = last_id()
    conn.execute(
        "INSERT INTO mensaje_agente (conversacion_id, person_id, rol, contenido_json, creado_en) "
        "VALUES (?, ?, 'usuario', '{}', ?)",
        (ids["conv"], person_id, NOW),
    )

    ids["propuestas"] = []
    for accion in actions:
        conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, objetivo_id, "
            "despues_json, estado, creada_en, expira_en) "
            "VALUES (?, ?, ?, ?, '{}', 'propuesta', ?, ?)",
            (person_id, ids["conv"], accion, ids["goal"], NOW, NOW),
        )
        ids["propuestas"].append(last_id())

    conn.execute(
        "INSERT INTO uso_agente (person_id, creado_en, proveedor, modelo) VALUES (?, ?, 'anthropic', 'demo')",
        (person_id, NOW),
    )
    conn.execute(
        "INSERT INTO daily_metric (person_id, day, metric_key, value, source) "
        "VALUES (?, '2026-10-05', 'resting_hr', 58.0, 'garmin')",
        (person_id,),
    )
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) "
        "VALUES (?, '2026-10-05', '2026-10-04T23:00:00', '2026-10-05T06:30:00')",
        (person_id,),
    )
    conn.execute(
        "INSERT INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, '2026-10-05', 'user_summary', 'garmin', ?, '{}')",
        (person_id, NOW),
    )
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
        "VALUES (?, 'act-1', '2026-10-05', '2026-10-05T07:00:00', 'strength', 'Gym', 1800, 'garmin')",
        (person_id,),
    )
    conn.execute(
        "INSERT INTO workout_set (person_id, activity_source, activity_source_id, set_order, "
        "set_type, corrected_by_person, created_at, updated_at) "
        "VALUES (?, 'garmin', 'act-1', 1, 'active', 0, ?, ?)",
        (person_id, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, "
        "day_count, created_at, kind) VALUES (?, 'turnos.pdf', ?, ?, 'confirmed', 1, ?, 'import')",
        (person_id, NOW, NOW, NOW),
    )
    ids["shift_batch"] = last_id()
    conn.execute(
        "INSERT INTO shift_batch_item (batch_id, day, kind, start_local, end_local) "
        "VALUES (?, '2026-10-06', 'night', '2026-10-06T22:00:00', '2026-10-07T06:00:00')",
        (ids["shift_batch"],),
    )
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, end_local, source, batch_id) "
        "VALUES (?, '2026-10-06', 'night', '2026-10-06T22:00:00', '2026-10-07T06:00:00', "
        "'import', ?)",
        (person_id, ids["shift_batch"]),
    )
    conn.execute(
        "INSERT INTO shift_pattern (person_id, start_day, cycle, updated_at) "
        "VALUES (?, '2026-10-01', '[]', ?)",
        (person_id, NOW),
    )
    conn.execute("INSERT INTO ajuste_agente (person_id, updated_at) VALUES (?, ?)", (person_id, NOW))
    conn.execute("INSERT INTO tope_agente (person_id) VALUES (?)", (person_id,))
    conn.execute(
        "INSERT INTO consentimiento (person_id, version, accepted_at) "
        "VALUES (?, ?, ?)",
        (person_id, privacidad.CONSENT_VERSION, NOW),
    )
    conn.execute(
        "INSERT INTO moment (person_id, ts_local, kind, created_at) VALUES (?, ?, 'nota', ?)",
        (person_id, NOW, NOW),
    )
    ids["moment"] = last_id()
    conn.execute(
        "INSERT INTO experimento (person_id, titulo, habito_id, metricas, ventana, filtro_dias, "
        "fecha_inicio, estado, creado_en) VALUES (?, 'Prueba', ?, '[]', '{}', '{}', "
        "'2026-10-01', 'activo', ?)",
        (person_id, ids["habit"], NOW),
    )
    ids["experimento"] = last_id()
    conn.execute(
        "INSERT INTO experimento_registro (experimento_id, person_id, day, ocurrio, momento_id, "
        "creado_en) VALUES (?, ?, '2026-10-05', 1, ?, ?)",
        (ids["experimento"], person_id, ids["moment"], NOW),
    )
    conn.execute(
        "INSERT INTO figura_compartida (person_id, metric_key, hogar_id, shared_at) "
        "VALUES (?, 'resting_hr', 'HOGAR-1', ?)",
        (person_id, NOW),
    )
    conn.commit()
    return ids


class MudanzaTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_home, self._orig_db = config.BODY_HOME, config.DB_PATH
        self.tmp = Path(tempfile.mkdtemp(prefix="body-mudanza-test-"))
        config.BODY_HOME = self.tmp
        config.DB_PATH = self.tmp / "host.db"
        self.source_path = self.tmp / "pc.db"

    def tearDown(self):
        config.BODY_HOME, config.DB_PATH = self._orig_home, self._orig_db

    def _new_source(self) -> sqlite3.Connection:
        conn = db.connect(path=self.source_path)
        db.init(conn)
        return conn

    def _dest_conn(self) -> sqlite3.Connection:
        conn = db.connect()
        db.init(conn)
        return conn

    def _reports_by_name(self, reports):
        return {r.name: r for r in reports}


class RegistryTest(MudanzaTestCase):
    def test_every_live_table_is_in_the_registry_or_skipped(self):
        conn = self._dest_conn()
        live = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        covered = set(mudanza.REGISTRY_BY_NAME) | set(mudanza.SKIPPED_TABLES) | {"person"}
        self.assertEqual(live - covered, set(), "a table exists that the registry never heard of")

    def test_every_table_with_a_person_id_column_is_covered(self):
        conn = self._dest_conn()
        person_tables = privacidad.person_tables(conn)
        covered = set(mudanza.REGISTRY_BY_NAME) | set(mudanza.SKIPPED_TABLES)
        self.assertEqual(person_tables - covered, set())

    def test_scope_matches_whether_the_table_has_a_person_id_column(self):
        conn = self._dest_conn()
        for spec in mudanza.REGISTRY:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({spec.name})")}
            if spec.scope == "person":
                self.assertIn("person_id", cols, f"{spec.name} is scope=person but has no person_id")
            else:
                self.assertNotIn(
                    "person_id", cols, f"{spec.name} is scope=parent but DOES have person_id"
                )

    def test_tables_without_person_id_are_the_documented_exception(self):
        no_person_id = {s.name for s in mudanza.REGISTRY if s.scope == "parent"}
        self.assertEqual(no_person_id, mudanza._NO_PERSON_ID_COLUMN)


class PersonResolutionTest(MudanzaTestCase):
    def test_source_person_is_the_pc_installs_one_person(self):
        source = self._new_source()
        source.close()
        source_ro = backup.open_readonly(self.source_path)
        self.assertEqual(mudanza.resolve_source_person(source_ro), db.DEFAULT_PERSON_ID)
        source_ro.close()

    def test_dest_person_is_created_through_the_sign_in_path_when_absent(self):
        dest = self._dest_conn()
        before = {r["id"] for r in dest.execute("SELECT id FROM person")}
        person_id = mudanza.resolve_dest_person(dest, CUENTA_SUB)
        self.assertNotIn(person_id, before)
        row = dest.execute("SELECT cuenta_sub FROM person WHERE id = ?", (person_id,)).fetchone()
        self.assertEqual(row["cuenta_sub"], CUENTA_SUB)
        # Exactly what cuenta.resolve_person would have done -- not a hand-written INSERT.
        again = cuenta.resolve_person(dest, {"sub": CUENTA_SUB, "duna_productos": ["body"]})
        self.assertEqual(again, person_id)

    def test_dest_person_is_found_when_already_linked(self):
        dest = self._dest_conn()
        existing_id = cuenta.resolve_person(dest, {"sub": CUENTA_SUB, "duna_productos": ["body"]})
        found = mudanza.resolve_dest_person(dest, CUENTA_SUB)
        self.assertEqual(found, existing_id)


class DryRunTest(MudanzaTestCase):
    def setUp(self):
        super().setUp()
        source = self._new_source()
        self.ids = _populate_full(source, db.DEFAULT_PERSON_ID)
        source.close()

    def test_dry_run_on_empty_destination_counts_but_writes_nothing(self):
        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)

        goal = by_name["goal"].counts
        self.assertEqual((goal.rows_source, goal.already_present, goal.insert, goal.other_person),
                         (2, 0, 1, 1))

        habit = by_name["habit"].counts
        self.assertEqual(habit.rows_source, 3)
        self.assertEqual(habit.insert, 1)
        self.assertEqual(habit.other_person, 2)  # assignee is someone else, and under the hogar goal

        plan = by_name["plan"].counts
        self.assertEqual((plan.rows_source, plan.insert, plan.other_person), (2, 1, 1))

        figura = by_name["figura_compartida"].counts
        self.assertEqual((figura.rows_source, figura.insert, figura.other_person), (1, 0, 1))

        goal_enlace = by_name["goal_enlace"].counts
        self.assertEqual((goal_enlace.rows_source, goal_enlace.insert, goal_enlace.other_person),
                          (2, 1, 1))

        daily_metric = by_name["daily_metric"].counts
        self.assertEqual((daily_metric.rows_source, daily_metric.insert, daily_metric.update,
                           daily_metric.skip), (1, 1, 0, 0))

        # Destination received no actual writes.
        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM goal").fetchone()[0], 0)

    def test_dry_run_reports_skipped_catalog_session_and_device_tables(self):
        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        by_name = self._reports_by_name(reports)
        for name in ("metric_def", "sesion", "dispositivo", "garmin_cuenta", "hogar_miembro"):
            self.assertTrue(by_name[name].skipped_table)
            self.assertIsNone(by_name[name].counts)

    def test_dry_run_against_a_destination_already_holding_rows(self):
        dest = self._dest_conn()
        dest_person_id = cuenta.resolve_person(dest, {"sub": CUENTA_SUB, "duna_productos": ["body"]})
        with dest:
            dest.execute(
                "INSERT INTO daily_metric (person_id, day, metric_key, value, source) "
                "VALUES (?, '2026-10-05', 'resting_hr', 90.0, 'garmin')",
                (dest_person_id,),
            )
            dest.execute(
                "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
                "target_value, baseline_value, start_day, status, visibility) VALUES "
                "(?, NULL, 'body', 'Pre-existing', 'metric', 'resting_hr', 'lower', 50, 70, "
                "'2026-09-01', 'active', 'private')",
                (dest_person_id,),
            )
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        by_name = self._reports_by_name(reports)

        goal = by_name["goal"].counts
        self.assertEqual(goal.already_present, 1)
        self.assertEqual(goal.insert, 1)  # the PC's own goal is still a fresh insert

        daily_metric = by_name["daily_metric"].counts
        self.assertEqual(daily_metric.already_present, 1)
        self.assertEqual(daily_metric.update, 1)  # same (person, day, metric) key, PC value differs
        self.assertEqual(daily_metric.insert, 0)


class ApplyTest(MudanzaTestCase):
    def setUp(self):
        super().setUp()
        source = self._new_source()
        self.ids = _populate_full(source, db.DEFAULT_PERSON_ID)
        source.close()

    def test_apply_resolves_remapped_fks_and_counts_match_source(self):
        # A decoy goal for an unrelated person, already on the host, so the destination's own
        # autoincrement id diverges from the source id -- proving the FK below was really REMAPPED,
        # not merely copied through by coincidence.
        decoy = self._dest_conn()
        with decoy:
            decoy.execute("INSERT INTO person (id, display_name) VALUES (777, 'Other host person')")
            decoy.execute(
                "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
                "target_value, baseline_value, start_day, status, visibility) VALUES "
                "(777, NULL, 'body', 'Decoy', 'metric', 'resting_hr', 'lower', 50, 70, "
                "'2026-09-01', 'active', 'private')"
            )
        decoy.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["goal"].counts.insert, 1)
        self.assertEqual(by_name["habit"].counts.insert, 1)
        self.assertEqual(by_name["experimento"].counts.insert, 1)
        self.assertEqual(by_name["experimento_registro"].counts.insert, 1)
        self.assertEqual(by_name["comida"].counts.insert, 1)
        self.assertEqual(by_name["ingesta"].counts.insert, 1)
        self.assertEqual(by_name["checkin"].counts.insert, 1)
        self.assertEqual(by_name["checkin_nota"].counts.insert, 1)
        # BODY-DESPERTAR-REAL-6A-R2 ruling 1: the sueno row and the levantado row for the same day
        # both migrate -- the widened match_cols never collides them.
        self.assertEqual(by_name["despertar_correccion"].counts.insert, 2)

        dest = db.connect()
        person_id = dest.execute("SELECT id FROM person WHERE cuenta_sub = ?", (CUENTA_SUB,)).fetchone()[0]

        dest_goal = dest.execute("SELECT id FROM goal WHERE person_id = ?", (person_id,)).fetchone()
        self.assertIsNotNone(dest_goal)
        self.assertNotEqual(dest_goal["id"], self.ids["goal"])  # re-keyed, not copied verbatim

        dest_habit = dest.execute("SELECT goal_id FROM habit WHERE goal_id = ?", (dest_goal["id"],)).fetchone()
        self.assertIsNotNone(dest_habit)  # the habit's FK now points at the NEW goal id

        dest_checkin = dest.execute("SELECT id FROM checkin WHERE person_id = ?", (person_id,)).fetchone()
        dest_nota = dest.execute("SELECT id FROM checkin_nota WHERE person_id = ?", (person_id,)).fetchone()
        self.assertEqual(dest_nota["id"], dest_checkin["id"])  # checkin_nota.id == its sibling checkin.id

        dest_comida = dest.execute("SELECT id FROM comida WHERE person_id = ?", (person_id,)).fetchone()
        dest_ingesta = dest.execute(
            "SELECT comida_id FROM ingesta WHERE person_id = ?", (person_id,)
        ).fetchone()
        self.assertEqual(dest_ingesta["comida_id"], dest_comida["id"])

        # Nothing from another person, or from a household link, ever reached the destination.
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM figura_compartida").fetchone()[0], 0)
        self.assertEqual(
            dest.execute("SELECT COUNT(*) FROM goal WHERE hogar_id IS NOT NULL").fetchone()[0], 0
        )
        self.assertEqual(
            dest.execute(
                "SELECT COUNT(*) FROM habit WHERE assignee_person_id IS NOT NULL AND "
                "assignee_person_id != ?",
                (person_id,),
            ).fetchone()[0],
            0,
        )

    def test_apply_passes_integrity_and_foreign_key_check(self):
        _, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        dest = db.connect()
        self.assertEqual(dest.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(dest.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_second_apply_is_a_no_op(self):
        mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        reports2, ok2 = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok2)
        for r in reports2:
            if r.skipped_table:
                continue
            self.assertEqual(r.counts.insert, 0, f"{r.name} inserted again on the second run")
            self.assertEqual(r.counts.update, 0, f"{r.name} updated again on the second run")

        dest = db.connect()
        person_id = dest.execute("SELECT id FROM person WHERE cuenta_sub = ?", (CUENTA_SUB,)).fetchone()[0]
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM goal WHERE person_id = ?", (person_id,)).fetchone()[0], 1)
        self.assertEqual(
            dest.execute("SELECT COUNT(*) FROM daily_metric WHERE person_id = ?", (person_id,)).fetchone()[0], 1
        )

    def test_pc_row_wins_on_conflict(self):
        dest = self._dest_conn()
        dest_person_id = cuenta.resolve_person(dest, {"sub": CUENTA_SUB, "duna_productos": ["body"]})
        with dest:
            dest.execute(
                "INSERT INTO daily_metric (person_id, day, metric_key, value, source) "
                "VALUES (?, '2026-10-05', 'resting_hr', 90.0, 'garmin')",
                (dest_person_id,),
            )
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["daily_metric"].counts.update, 1)

        dest = db.connect()
        value = dest.execute(
            "SELECT value FROM daily_metric WHERE person_id = ? AND day = '2026-10-05' AND metric_key = 'resting_hr'",
            (dest_person_id,),
        ).fetchone()[0]
        self.assertEqual(value, 58.0)  # the PC's own figure, not the host's 90.0

    def test_source_file_bytes_are_unchanged_after_the_run(self):
        before = self.source_path.read_bytes()
        mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        after = self.source_path.read_bytes()
        self.assertEqual(before, after)


class PeriodZonasMudanzaTest(MudanzaTestCase):
    """BODY-LESION-ZONA-1 ruling 5: period.zonas survives a move -- mudanza.py never lists period's
    columns explicitly (match_cols=None copies a row's own columns), so this is purely additive."""

    def test_zonas_survives_a_move(self):
        source = self._new_source()
        db.crear_periodo(source, db.DEFAULT_PERSON_ID, "lesion", "2026-10-01", None, zonas=["hombro"])
        source.close()

        _, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)

        dest = db.connect()
        row = dest.execute("SELECT zonas FROM period").fetchone()
        self.assertEqual(json.loads(row["zonas"]), ["hombro"])


class EscrituraExternaMudanzaTest(MudanzaTestCase):
    """BODY-HOGAR-REGISTRAR-IDEMPOTENTE-1 ruling 3: an idempotency-key row moves with the person,
    matched by its own natural key (person_id, origen, ref) -- its propuesta_id remapped to the
    destination's own propuesta_agente row, after that table's own id map, like any other FK --
    and a second import of the same source never duplicates it."""

    def test_row_moves_with_its_propuesta_id_remapped_and_a_second_import_does_not_duplicate_it(self):
        source = self._new_source()
        with source:
            source.execute(
                "INSERT INTO conversacion_agente (person_id, creada_en, titulo, origen) VALUES "
                "(?, ?, 'Chat Hogar', 'hogar')", (db.DEFAULT_PERSON_ID, NOW),
            )
            conv_id = source.execute("SELECT last_insert_rowid()").fetchone()[0]
            source.execute(
                "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
                "estado, creada_en, expira_en, aplicada_via) VALUES "
                "(?, ?, 'registrar_comida', '{}', 'aplicada', ?, ?, 'hogar')",
                (db.DEFAULT_PERSON_ID, conv_id, NOW, NOW),
            )
            propuesta_id = source.execute("SELECT last_insert_rowid()").fetchone()[0]
            source.execute(
                "INSERT INTO escritura_externa (person_id, origen, ref, herramienta, propuesta_id, "
                "creada_en) VALUES (?, 'hogar', 'zz-mudanza-ref', 'registrar_comida', ?, ?)",
                (db.DEFAULT_PERSON_ID, propuesta_id, NOW),
            )
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["escritura_externa"].counts.insert, 1)

        dest = db.connect()
        person_id = dest.execute("SELECT id FROM person WHERE cuenta_sub = ?", (CUENTA_SUB,)).fetchone()[0]
        dest_propuesta = dest.execute(
            "SELECT id FROM propuesta_agente WHERE person_id = ?", (person_id,)
        ).fetchone()
        dest_row = dest.execute(
            "SELECT propuesta_id, herramienta FROM escritura_externa WHERE person_id = ? AND ref = ?",
            (person_id, "zz-mudanza-ref"),
        ).fetchone()
        self.assertEqual(dest_row["propuesta_id"], dest_propuesta["id"])
        self.assertEqual(dest_row["herramienta"], "registrar_comida")
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["escritura_externa"].counts.insert, 0)


class EntrenoDolorMudanzaTest(MudanzaTestCase):
    """BODY-PLAN-FASES-5: a pain-by-exercise row moves with the person, matched by its own
    natural key (person_id, sets_source, sets_source_id, ejercicio) -- a second import of the
    same source never duplicates it."""

    def test_row_moves_and_a_second_import_does_not_duplicate_it(self):
        source = self._new_source()
        with source:
            db.registrar_dolor_entreno(source, db.DEFAULT_PERSON_ID, "2026-11-17", "garmin", "s1", [
                {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3,
                 "dolor_despues": 2, "movimiento": "al bajar la barra"},
            ])
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["entreno_dolor"].counts.insert, 1)

        dest = db.connect()
        row = dest.execute(
            "SELECT ejercicio, zona, dolor_durante, dolor_despues, movimiento FROM entreno_dolor"
        ).fetchone()
        self.assertEqual((row["ejercicio"], row["zona"], row["dolor_durante"], row["dolor_despues"],
                           row["movimiento"]),
                          ("press militar", "hombro", 3, 2, "al bajar la barra"))
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["entreno_dolor"].counts.insert, 0)
        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM entreno_dolor").fetchone()[0], 1)


class CheckinSesionMudanzaTest(MudanzaTestCase):
    """BODY-PARTIDO-2: a person's own check-in set (set_pregunta + its items, reached through
    set_pregunta_id) and their own answers (set_pregunta_respuesta, value-keyed) both move with the
    person -- a second import of the same source never duplicates either."""

    def test_set_and_item_and_respuesta_move_and_a_second_import_does_not_duplicate_them(self):
        source = self._new_source()
        with source:
            db.guardar_set_pregunta(source, db.DEFAULT_PERSON_ID, "other", "zz-baloncesto", [
                {"clave": "formato", "tipo_pregunta": "opcion_unica",
                 "texto": "checkin_sesion.pregunta.formato",
                 "opciones": [{"clave": "uno_uno", "texto": "checkin_sesion.opcion.formato.uno_uno"}]},
                {"clave": "sensacion", "tipo_pregunta": "escala",
                 "texto": "checkin_sesion.pregunta.sensacion", "opciones": None},
            ])
            db.registrar_respuestas_sesion(source, db.DEFAULT_PERSON_ID, "garmin", "s1",
                                            {"formato": "uno_uno", "sensacion": "4"})
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["set_pregunta"].counts.insert, 1)
        self.assertEqual(by_name["set_pregunta_item"].counts.insert, 2)
        self.assertEqual(by_name["set_pregunta_respuesta"].counts.insert, 2)

        dest = db.connect()
        person_id = dest.execute("SELECT id FROM person WHERE cuenta_sub = ?", (CUENTA_SUB,)).fetchone()[0]
        self.assertEqual(db.set_de_persona(dest, person_id, "other", "zz-baloncesto"),
                          [{"clave": "formato", "tipo_pregunta": "opcion_unica",
                            "texto": "checkin_sesion.pregunta.formato", "origen": "defecto",
                            "opciones": [{"clave": "uno_uno",
                                          "texto": "checkin_sesion.opcion.formato.uno_uno"}]},
                           {"clave": "sensacion", "tipo_pregunta": "escala",
                            "texto": "checkin_sesion.pregunta.sensacion", "origen": "defecto",
                            "opciones": None}])
        self.assertEqual(db.respuestas_de_sesion(dest, person_id, "garmin", "s1"),
                          {"formato": "uno_uno", "sensacion": "4"})
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["set_pregunta"].counts.insert, 0)
        self.assertEqual(by_name["set_pregunta_item"].counts.insert, 0)
        self.assertEqual(by_name["set_pregunta_respuesta"].counts.insert, 0)
        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM set_pregunta").fetchone()[0], 1)
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM set_pregunta_item").fetchone()[0], 2)
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM set_pregunta_respuesta").fetchone()[0], 2)


class NutrientTargetMudanzaTest(MudanzaTestCase):
    """BODY-NUTRIENTE-OBJETIVO-1: a daily nutrient target moves with the rest of the person's data,
    matched by its own natural key (person_id, metric_key, start_day) -- a second import of the
    same source never duplicates it."""

    def test_target_moves_and_a_second_import_does_not_duplicate_it(self):
        source = self._new_source()
        with source:
            db.set_nutrient_target(source, db.DEFAULT_PERSON_ID, "protein_g", "2026-10-01", "per_kg", 1.6,
                                    "al_menos")
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["nutrient_target"].counts.insert, 1)

        dest = db.connect()
        row = dest.execute("SELECT metric_key, start_day, kind, amount, sentido FROM nutrient_target").fetchone()
        self.assertEqual((row["metric_key"], row["start_day"], row["kind"], row["amount"], row["sentido"]),
                         ("protein_g", "2026-10-01", "per_kg", 1.6, "al_menos"))
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["nutrient_target"].counts.insert, 0)
        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM nutrient_target").fetchone()[0], 1)


class PlanSesionNotaMudanzaTest(MudanzaTestCase):
    """BODY-HOGAR-ESCRIBE-1 ruling 4: plan's nota_sesion/nota_sesion_en are ordinary columns on an
    already-registered table, so the generic SELECT *-based copy carries them with no registry
    change."""

    def test_the_note_and_its_timestamp_move_with_the_plan(self):
        source = self._new_source()
        with source:
            source.execute(
                "INSERT INTO plan (person_id, hogar_id, goal_id, day, title, status, "
                "nota_sesion, nota_sesion_en) VALUES (?, NULL, NULL, '2026-10-06', 'Run', "
                "'done', 'fue dura', '2026-10-06T07:00:00')",
                (db.DEFAULT_PERSON_ID,),
            )
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["plan"].counts.insert, 1)

        dest = db.connect()
        row = dest.execute("SELECT nota_sesion, nota_sesion_en FROM plan WHERE title = 'Run'").fetchone()
        self.assertEqual((row["nota_sesion"], row["nota_sesion_en"]), ("fue dura", "2026-10-06T07:00:00"))
        dest.close()


class MomentoComidaMudanzaTest(MudanzaTestCase):
    """BODY-HOGAR-ESCRIBE-1-W ruling 2/6: momento_comida moves with the person, matched by its own
    natural key (person_id, clave) -- a second import of the same source never duplicates it."""

    def test_row_moves_and_a_second_import_does_not_duplicate_it(self):
        source = self._new_source()
        with source:
            source.execute(
                "INSERT INTO momento_comida (person_id, clave, desde_hora, hasta_hora, esperado, orden) "
                "VALUES (?, 'brunch', 9, 12, 1, 1)", (db.DEFAULT_PERSON_ID,))
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["momento_comida"].counts.insert, 1)

        dest = db.connect()
        row = dest.execute(
            "SELECT desde_hora, hasta_hora, esperado, orden FROM momento_comida WHERE clave = 'brunch'"
        ).fetchone()
        self.assertEqual((row["desde_hora"], row["hasta_hora"], row["esperado"], row["orden"]), (9, 12, 1, 1))
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["momento_comida"].counts.insert, 0)


class PracticaMudanzaTest(MudanzaTestCase):
    """BODY-HISTORIA-VIDA-1 ruling 8: a practice moves with the rest of the person's data, matched
    by its own full row content (no natural key) -- a second import of the same source never
    duplicates it."""

    def test_row_moves_and_a_second_import_does_not_duplicate_it(self):
        source = self._new_source()
        with source:
            source.execute(
                "INSERT INTO practica (person_id, titulo, fuente, regla_json, meta_dias_semana, "
                "origen, creada_en) VALUES (?, 'Correr', 'actividad', '{\"tipos\": [\"running\"]}', "
                "5, 'persona', '2026-10-01T00:00:00+00:00')", (db.DEFAULT_PERSON_ID,))
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["practica"].counts.insert, 1)

        dest = db.connect()
        row = dest.execute(
            "SELECT titulo, fuente, regla_json, meta_dias_semana, origen, archivada_en FROM practica"
        ).fetchone()
        self.assertEqual(
            (row["titulo"], row["fuente"], row["regla_json"], row["meta_dias_semana"], row["origen"],
             row["archivada_en"]),
            ("Correr", "actividad", '{"tipos": ["running"]}', 5, "persona", None),
        )
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["practica"].counts.insert, 0)
        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM practica").fetchone()[0], 1)


class ProductoMudanzaTest(MudanzaTestCase):
    """BODY-COMIDA-PRODUCTO-1-D: a person's own manual product, its nutrition row and a meal
    naming it all move with them, every surrogate id remapped, a second import never duplicating
    any of the three new tables."""

    def test_product_nutrition_and_meal_line_move_with_remapped_ids(self):
        # A decoy product and meal for an unrelated person, already on the host, so the
        # destination's own autoincrement ids diverge from the source ids -- proving the FKs below
        # were really REMAPPED, not merely copied through by coincidence (the same technique
        # ApplyTest's own goal/habit test above uses).
        decoy = self._dest_conn()
        with decoy:
            decoy.execute("INSERT INTO person (id, display_name) VALUES (777, 'Other host person')")
            decoy.execute(
                "INSERT INTO producto (person_id, origen, nombre, created_at) VALUES "
                "(777, 'manual', 'Decoy', ?)", (NOW,))
            decoy.execute(
                "INSERT INTO comida (person_id, day, created_at, updated_at) VALUES "
                "(777, '2026-10-01', ?, ?)", (NOW, NOW))
        decoy.close()

        source = self._new_source()
        producto_id = productos_mod.crear_producto_manual(
            source, db.DEFAULT_PERSON_ID, "Yogur griego", marca="ZZ-Marca",
            tamano_neto=150, tamano_unidad="g")
        productos_mod.establecer_nutricion(source, producto_id, "kcal", 97, "100g", "etiqueta")
        comida_id = db.crear_comida(
            source, db.DEFAULT_PERSON_ID, "2026-10-05", [{"metric_key": "kcal", "amount": 145.5,
                                                            "estimado": False}],
            "yogur", "2026-10-05T08:00:00",
            productos=[{"producto_id": producto_id, "cantidad": 150, "unidad": "g"}])
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["producto"].counts.insert, 1)
        self.assertEqual(by_name["producto_nutricion"].counts.insert, 1)
        self.assertEqual(by_name["comida_producto"].counts.insert, 1)

        dest = db.connect()
        person_id = dest.execute("SELECT id FROM person WHERE cuenta_sub = ?", (CUENTA_SUB,)).fetchone()[0]
        dest_producto = dest.execute(
            "SELECT id, nombre, marca FROM producto WHERE person_id = ?", (person_id,)
        ).fetchone()
        self.assertIsNotNone(dest_producto)
        self.assertNotEqual(dest_producto["id"], producto_id)  # re-keyed, not copied verbatim
        self.assertEqual((dest_producto["nombre"], dest_producto["marca"]), ("Yogur griego", "ZZ-Marca"))

        dest_nutricion = dest.execute(
            "SELECT producto_id, metric_key, amount_per, basis, fuente FROM producto_nutricion"
        ).fetchone()
        self.assertEqual(dest_nutricion["producto_id"], dest_producto["id"])
        self.assertEqual((dest_nutricion["metric_key"], dest_nutricion["amount_per"], dest_nutricion["basis"],
                           dest_nutricion["fuente"]), ("kcal", 97, "100g", "etiqueta"))

        dest_comida = dest.execute("SELECT id FROM comida WHERE person_id = ?", (person_id,)).fetchone()
        self.assertNotEqual(dest_comida["id"], comida_id)  # re-keyed, not copied verbatim
        dest_linea = dest.execute(
            "SELECT comida_id, producto_id, cantidad, unidad FROM comida_producto"
        ).fetchone()
        self.assertEqual(dest_linea["comida_id"], dest_comida["id"])
        self.assertEqual(dest_linea["producto_id"], dest_producto["id"])
        self.assertEqual((dest_linea["cantidad"], dest_linea["unidad"]), (150, "g"))
        dest.close()

        reports2, ok2 = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok2)
        by_name2 = self._reports_by_name(reports2)
        self.assertEqual(by_name2["producto"].counts.insert, 0)
        self.assertEqual(by_name2["producto_nutricion"].counts.insert, 0)
        self.assertEqual(by_name2["comida_producto"].counts.insert, 0)
        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM producto WHERE person_id = ?",
                                       (person_id,)).fetchone()[0], 1)
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM producto_nutricion").fetchone()[0], 1)
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM comida_producto").fetchone()[0], 1)


class HabitPracticaIdMudanzaTest(MudanzaTestCase):
    """BODY-HISTORIA-VIDA-2 ruling 4: a habit linked to a practice remaps practica_id to the
    practice's NEW id at the destination -- practica is processed before habit in REGISTRY exactly
    so its id map is ready when habit's own fk_remap needs it."""

    def test_practica_id_remaps_to_the_destination_row(self):
        source = self._new_source()
        with source:
            source.execute(
                "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
                "target_value, baseline_value, start_day, status, visibility) VALUES "
                "(?, NULL, 'body', 'Lower resting HR', 'metric', 'resting_hr', 'lower', 55, 60, "
                "'2026-10-01', 'active', 'private')", (db.DEFAULT_PERSON_ID,),
            )
            goal_id = source.execute("SELECT id FROM goal").fetchone()[0]
            source.execute(
                "INSERT INTO practica (person_id, titulo, fuente, regla_json, meta_dias_semana, "
                "origen, creada_en) VALUES (?, 'Correr', 'actividad', '{\"tipos\": [\"running\"]}', "
                "5, 'persona', '2026-10-01T00:00:00+00:00')", (db.DEFAULT_PERSON_ID,),
            )
            practica_id_source = source.execute("SELECT id FROM practica").fetchone()[0]
            source.execute(
                "INSERT INTO habit (goal_id, title, cadence, assignee_person_id, active, practica_id) "
                "VALUES (?, 'Retomar correr', '{\"per\": \"week\", \"times\": 3}', NULL, 1, ?)",
                (goal_id, practica_id_source),
            )
        source.close()

        # A pre-existing practica already at the destination (another table's row, or Cuenta's own
        # resolve_person creating the person first) pushes the newly-migrated one to a DIFFERENT id
        # than the source had -- so the assertion below proves the remap actually ran, not a
        # coincidence of both starting empty.
        dest = self._dest_conn()
        dest.execute("INSERT INTO person (id, display_name) VALUES (2, 'Other')")
        dest.execute(
            "INSERT INTO practica (person_id, titulo, fuente, regla_json, meta_dias_semana, origen, "
            "creada_en) VALUES (2, 'Ya existía', 'actividad', '{\"tipos\": [\"cycling\"]}', 5, "
            "'persona', '2026-10-01T00:00:00+00:00')"
        )
        dest.commit()
        dest.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["practica"].counts.insert, 1)
        self.assertEqual(by_name["habit"].counts.insert, 1)

        dest = db.connect()
        practica_id_dest = dest.execute("SELECT id FROM practica WHERE titulo = 'Correr'").fetchone()["id"]
        habit_row = dest.execute("SELECT practica_id FROM habit WHERE title = 'Retomar correr'").fetchone()
        self.assertEqual(habit_row["practica_id"], practica_id_dest)
        self.assertNotEqual(practica_id_dest, practica_id_source)
        dest.close()
        dest.close()


class SelfRefRoundTripTest(MudanzaTestCase):
    """BODY-PLAN-ESQUEMA-1-R: the plan's own parts move with the person -- goal's self-reference
    (horizonte_goal_id) and horizonte_meta_semanal's append-only correction chain (reemplaza_id)
    both resolve to DESTINATION ids, in a second pass, regardless of source id order."""

    def setUp(self):
        super().setUp()
        source = self._new_source()
        pid = db.DEFAULT_PERSON_ID

        # The child metric goal is created FIRST (lower id); its horizon parent is created AFTER
        # (higher id) -- proving the second pass, not id order, resolves the self-reference.
        source.execute(
            "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
            "target_value, baseline_value, start_day, status, visibility) VALUES "
            "(?, NULL, 'body', 'Lower resting HR', 'metric', 'resting_hr', 'lower', 55, 60, "
            "'2026-10-01', 'active', 'private')",
            (pid,),
        )
        self.child_goal_id = source.execute("SELECT last_insert_rowid()").fetchone()[0]

        source.execute(
            "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
            "target_value, baseline_value, start_day, deadline, status, visibility) VALUES "
            "(?, NULL, 'body', 'The plan horizon', 'milestone', NULL, NULL, NULL, NULL, "
            "'2026-10-01', '2027-01-01', 'active', 'private')",
            (pid,),
        )
        self.horizon_goal_id = source.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.assertGreater(self.horizon_goal_id, self.child_goal_id)

        source.execute(
            "UPDATE goal SET horizonte_goal_id = ? WHERE id = ?",
            (self.horizon_goal_id, self.child_goal_id),
        )

        source.execute(
            "INSERT INTO experimento (person_id, titulo, goal_id, metricas, ventana, filtro_dias, "
            "fecha_inicio, estado, creado_en) VALUES (?, 'Prueba de plan', ?, '[]', '{}', '{}', "
            "'2026-10-01', 'activo', ?)",
            (pid, self.horizon_goal_id, NOW),
        )

        db.crear_regla_horizonte(source, pid, self.horizon_goal_id, "siempre", None, "Dormir 8 horas")
        db.crear_semana_horizonte(source, pid, self.horizon_goal_id, "2026-11-02", titulo="Semana 1")
        self.old_meta_id = db.crear_meta_semanal(source, pid, self.horizon_goal_id, "2026-11-02",
                                                   "resting_hr", "al_menos", 55.0)
        self.new_meta_id = db.corregir_meta_semanal(source, pid, self.old_meta_id, 53.0)
        db.crear_checkpoint_horizonte(source, pid, self.horizon_goal_id, "2026-11-02",
                                        "Primer checkpoint", ["durmio_8_horas"])

        source.commit()
        source.close()

    def test_apply_resolves_self_refs_and_remaps_to_destination_ids(self):
        # A decoy goal for an unrelated person already on the host, so the destination's own
        # autoincrement id diverges from the source id -- proving the self-ref was really REMAPPED,
        # not merely copied through by coincidence (the same technique ApplyTest uses above).
        decoy = self._dest_conn()
        with decoy:
            decoy.execute("INSERT INTO person (id, display_name) VALUES (777, 'Other host person')")
            decoy.execute(
                "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
                "target_value, baseline_value, start_day, status, visibility) VALUES "
                "(777, NULL, 'body', 'Decoy', 'metric', 'resting_hr', 'lower', 50, 70, "
                "'2026-09-01', 'active', 'private')"
            )
        decoy.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)

        self.assertEqual(by_name["goal"].counts.self_ref_unresolved, 0)
        self.assertEqual(by_name["horizonte_meta_semanal"].counts.self_ref_unresolved, 0)
        self.assertEqual(by_name["goal"].counts.insert, 2)
        self.assertEqual(by_name["experimento"].counts.insert, 1)
        self.assertEqual(by_name["horizonte_regla"].counts.insert, 1)
        self.assertEqual(by_name["horizonte_semana"].counts.insert, 1)
        self.assertEqual(by_name["horizonte_meta_semanal"].counts.insert, 2)
        self.assertEqual(by_name["horizonte_checkpoint"].counts.insert, 1)

        dest = db.connect()
        person_id = dest.execute(
            "SELECT id FROM person WHERE cuenta_sub = ?", (CUENTA_SUB,)
        ).fetchone()[0]

        dest_horizon = dest.execute(
            "SELECT id FROM goal WHERE person_id = ? AND kind = 'milestone'", (person_id,)
        ).fetchone()["id"]
        self.assertNotEqual(dest_horizon, self.horizon_goal_id)  # re-keyed, not copied verbatim

        dest_child = dest.execute(
            "SELECT horizonte_goal_id FROM goal WHERE person_id = ? AND kind = 'metric'", (person_id,)
        ).fetchone()
        self.assertEqual(dest_child["horizonte_goal_id"], dest_horizon)

        dest_exp = dest.execute(
            "SELECT goal_id FROM experimento WHERE person_id = ?", (person_id,)
        ).fetchone()
        self.assertEqual(dest_exp["goal_id"], dest_horizon)

        for table in ("horizonte_regla", "horizonte_semana", "horizonte_checkpoint"):
            row = dest.execute(
                f"SELECT horizonte_goal_id FROM {table} WHERE person_id = ?", (person_id,)
            ).fetchone()
            self.assertEqual(row["horizonte_goal_id"], dest_horizon, table)

        meta_rows = dest.execute(
            "SELECT id, vigente, reemplaza_id, valor FROM horizonte_meta_semanal "
            "WHERE person_id = ? ORDER BY id", (person_id,),
        ).fetchall()
        self.assertEqual(len(meta_rows), 2)
        old_row = next(r for r in meta_rows if r["vigente"] == 0)
        new_row = next(r for r in meta_rows if r["vigente"] == 1)
        self.assertIsNone(old_row["reemplaza_id"])
        self.assertEqual(new_row["reemplaza_id"], old_row["id"])  # resolved to the DESTINATION id
        self.assertEqual(old_row["valor"], 55.0)
        self.assertEqual(new_row["valor"], 53.0)

        vigente_count = dest.execute(
            "SELECT COUNT(*) FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? AND "
            "week_start = '2026-11-02' AND medida = 'resting_hr' AND vigente = 1", (dest_horizon,),
        ).fetchone()[0]
        self.assertEqual(vigente_count, 1)

    def test_dry_run_reports_the_same_insert_counts_and_writes_nothing(self):
        dry_reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        self.assertTrue(ok)
        apply_reports, ok2 = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok2)
        dry_by_name = self._reports_by_name(dry_reports)
        apply_by_name = self._reports_by_name(apply_reports)
        for name in ("goal", "experimento", "horizonte_regla", "horizonte_semana",
                     "horizonte_meta_semanal", "horizonte_checkpoint"):
            self.assertEqual(dry_by_name[name].counts.insert, apply_by_name[name].counts.insert, name)
            self.assertEqual(dry_by_name[name].counts.self_ref_unresolved,
                              apply_by_name[name].counts.self_ref_unresolved, name)

        # The dry run above wrote nothing: the apply call right after it still saw an empty
        # destination and inserted everything fresh.
        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM horizonte_meta_semanal").fetchone()[0], 2)

    def test_second_apply_is_a_no_op(self):
        mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        reports2, ok2 = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok2)
        for name in ("goal", "experimento", "horizonte_regla", "horizonte_semana",
                     "horizonte_meta_semanal", "horizonte_checkpoint"):
            counts = self._reports_by_name(reports2)[name].counts
            self.assertEqual(counts.insert, 0, name)
            self.assertEqual(counts.update, 0, name)

        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM horizonte_meta_semanal").fetchone()[0], 2)


class SelfRefUnresolvedTest(MudanzaTestCase):
    """A self-reference naming a row that was never migrated (another person's) stays NULL in the
    destination and is counted, never silently dropped or mis-linked."""

    def test_self_ref_to_a_non_migrated_goal_stays_null_and_is_counted(self):
        source = self._new_source()
        pid = db.DEFAULT_PERSON_ID
        source.execute("INSERT INTO person (id, display_name) VALUES (2, 'Someone else')")
        source.execute(
            "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
            "target_value, baseline_value, start_day, deadline, status, visibility) VALUES "
            "(2, NULL, 'body', 'Someone elses horizon', 'milestone', NULL, NULL, NULL, NULL, "
            "'2026-10-01', '2027-01-01', 'active', 'private')"
        )
        other_horizon_id = source.execute("SELECT last_insert_rowid()").fetchone()[0]
        source.execute(
            "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
            "target_value, baseline_value, start_day, horizonte_goal_id, status, visibility) VALUES "
            "(?, NULL, 'body', 'Dangling horizon ref', 'metric', 'resting_hr', 'lower', 55, 60, "
            "'2026-10-01', ?, 'active', 'private')",
            (pid, other_horizon_id),
        )
        source.commit()
        source.close()

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["goal"].counts.self_ref_unresolved, 1)
        self.assertEqual(by_name["goal"].counts.insert, 1)  # the dangling goal, pid's only own row

        dest = db.connect()
        row = dest.execute(
            "SELECT horizonte_goal_id FROM goal WHERE person_id = "
            "(SELECT id FROM person WHERE cuenta_sub = ?)", (CUENTA_SUB,),
        ).fetchone()
        self.assertIsNone(row["horizonte_goal_id"])


class PersonasTest(MudanzaTestCase):
    def test_lists_only_persons_with_a_cuenta_sub(self):
        dest = self._dest_conn()
        cuenta.resolve_person(dest, {"sub": CUENTA_SUB, "duna_productos": ["body"]})
        with dest:
            dest.execute("INSERT INTO person (id, display_name) VALUES (999, 'No sub')")
        rows = mudanza.personas(dest)
        subs = [r["cuenta_sub"] for r in rows]
        self.assertIn(CUENTA_SUB, subs)
        self.assertNotIn(None, subs)
        self.assertEqual(len(rows), 1)

    def test_creado_is_the_earliest_consent_or_none(self):
        dest = self._dest_conn()
        person_id = cuenta.resolve_person(dest, {"sub": CUENTA_SUB, "duna_productos": ["body"]})
        rows = mudanza.personas(dest)
        self.assertEqual(rows[0]["creado"], None)
        with dest:
            dest.execute(
                "INSERT INTO consentimiento (person_id, version, accepted_at) VALUES (?, 'v1', ?)",
                (person_id, NOW),
            )
        rows = mudanza.personas(dest)
        self.assertEqual(rows[0]["creado"], NOW)

    def test_prints_only_the_three_named_fields(self):
        dest = self._dest_conn()
        person_id = cuenta.resolve_person(dest, {"sub": CUENTA_SUB, "duna_productos": ["body"]})
        with dest:
            dest.execute(
                "INSERT INTO consentimiento (person_id, version, accepted_at) VALUES (?, 'v1', ?)",
                (person_id, NOW),
            )
            dest.execute(
                "INSERT INTO daily_metric (person_id, day, metric_key, value, source) "
                "VALUES (?, '2026-10-05', 'resting_hr', 999.0, 'garmin')",
                (person_id,),
            )
        rows = mudanza.personas(dest)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"cuenta_sub", "display_name", "creado"})
        for value in rows[0].values():
            self.assertNotEqual(value, 999.0)

    def test_cli_personas_prints_the_three_fields_and_no_other(self):
        import io
        import contextlib

        dest = self._dest_conn()
        cuenta.resolve_person(dest, {"sub": CUENTA_SUB, "duna_productos": ["body"]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mudanza.main(["personas"])
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn(CUENTA_SUB, printed)
        self.assertIn("cuenta_sub=", printed)
        self.assertIn("display_name=", printed)
        self.assertIn("creado=", printed)
        self.assertNotIn("trabaja_turnos", printed)
        self.assertNotIn("language", printed)


class OrigenAntiguoMudanzaTest(MudanzaTestCase):
    """BODY-MUDANZA-ORIGEN-ANTIGUO-1: the uploaded source can be at an OLDER schema than this
    process's own code -- any person's PC can be behind the host's code. `importar` must bring a
    COPY of it to today's schema before reading a single row, never the upload itself. The
    fixture here is the current schema minus exactly what BODY-PLAN-ESQUEMA-1 and BODY-PLAN-
    FASES-2 added: the five horizonte_* tables (the fifth, horizonte_fase, BODY-PLAN-FASES-2's
    own) and the two additive columns goal.horizonte_goal_id and experimento.goal_id."""

    def _new_older_source(self) -> sqlite3.Connection:
        conn = db.connect(path=self.source_path)
        db.init(conn)
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in ("horizonte_checkpoint", "horizonte_meta_semanal", "horizonte_semana",
                      "horizonte_regla", "horizonte_fase"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute(
            "CREATE TABLE goal_old ("
            "id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES person(id), "
            "hogar_id TEXT, "
            "area TEXT NOT NULL DEFAULT 'body' CHECK (area IN ('body', 'money', 'adventure', "
            "'growth', 'work', 'home')), "
            "title TEXT NOT NULL, why TEXT, "
            "kind TEXT NOT NULL DEFAULT 'metric' CHECK (kind IN ('metric', 'milestone')), "
            "metric_key TEXT REFERENCES metric_def(key), "
            "direction TEXT CHECK (direction IN ('raise', 'lower', 'keep') OR direction IS NULL), "
            "target_value REAL, baseline_value REAL, start_day TEXT NOT NULL, deadline TEXT, "
            "status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('proposed', 'active', "
            "'paused', 'done', 'dropped')), "
            "visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', "
            "'household')), "
            "CHECK ((kind = 'metric' AND metric_key IS NOT NULL AND direction IS NOT NULL) OR "
            "(kind = 'milestone' AND metric_key IS NULL AND deadline IS NOT NULL)))"
        )
        conn.execute(
            "INSERT INTO goal_old SELECT id, person_id, hogar_id, area, title, why, kind, "
            "metric_key, direction, target_value, baseline_value, start_day, deadline, status, "
            "visibility FROM goal"
        )
        conn.execute("DROP TABLE goal")
        conn.execute("ALTER TABLE goal_old RENAME TO goal")

        conn.execute(
            "CREATE TABLE experimento_old ("
            "id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES person(id), "
            "titulo TEXT NOT NULL, habito_id INTEGER REFERENCES habit(id), "
            "metricas TEXT NOT NULL CHECK (json_valid(metricas)), "
            "ventana TEXT NOT NULL CHECK (json_valid(ventana)), "
            "filtro_dias TEXT NOT NULL CHECK (json_valid(filtro_dias)), "
            "dias_prueba_objetivo INTEGER, fecha_inicio TEXT NOT NULL, fecha_fin TEXT, "
            "estado TEXT NOT NULL CHECK (estado IN ('activo', 'cerrado')), "
            "creado_en TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO experimento_old SELECT id, person_id, titulo, habito_id, metricas, "
            "ventana, filtro_dias, dias_prueba_objetivo, fecha_inicio, fecha_fin, estado, "
            "creado_en FROM experimento"
        )
        conn.execute("DROP TABLE experimento")
        conn.execute("ALTER TABLE experimento_old RENAME TO experimento")
        conn.commit()

        live_tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        self.assertFalse(any(t.startswith("horizonte_") for t in live_tables))
        goal_cols = {r[1] for r in conn.execute("PRAGMA table_info(goal)")}
        self.assertNotIn("horizonte_goal_id", goal_cols)
        exp_cols = {r[1] for r in conn.execute("PRAGMA table_info(experimento)")}
        self.assertNotIn("goal_id", exp_cols)
        return conn

    def setUp(self):
        super().setUp()
        source = self._new_older_source()
        self.ids = _populate_full(source, db.DEFAULT_PERSON_ID)
        source.close()

    def test_dry_run_completes_and_reports_the_tables_the_copy_gained(self):
        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["origen"].origen_tablas_nuevas, 5)

        goal = by_name["goal"].counts
        self.assertEqual((goal.rows_source, goal.insert, goal.other_person), (2, 1, 1))
        habit = by_name["habit"].counts
        self.assertEqual(habit.insert, 1)
        plan = by_name["plan"].counts
        self.assertEqual((plan.rows_source, plan.insert, plan.other_person), (2, 1, 1))

        # Still a dry run: nothing written.
        dest = db.connect()
        self.assertEqual(dest.execute("SELECT COUNT(*) FROM goal").fetchone()[0], 0)

    def test_apply_imports_the_rows(self):
        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["goal"].counts.insert, 1)
        self.assertEqual(by_name["habit"].counts.insert, 1)
        self.assertEqual(by_name["checkin"].counts.insert, 1)
        self.assertEqual(by_name["checkin_nota"].counts.insert, 1)

        dest = db.connect()
        self.assertEqual(dest.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(dest.execute("PRAGMA foreign_key_check").fetchall(), [])
        person_id = dest.execute(
            "SELECT id FROM person WHERE cuenta_sub = ?", (CUENTA_SUB,)
        ).fetchone()[0]
        self.assertEqual(
            dest.execute(
                "SELECT COUNT(*) FROM goal WHERE person_id = ?", (person_id,)
            ).fetchone()[0], 1,
        )

    def test_report_line_names_the_tables_the_migration_added(self):
        reports, _ = mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        out = []
        with mock.patch("builtins.print", out.append):
            mudanza._print_report(reports, apply=False)
        self.assertEqual(out[0], "origen: esquema actualizado en la copia (5 tablas nuevas)")

    def test_uploaded_file_bytes_are_unchanged_after_apply(self):
        before = self.source_path.read_bytes()
        mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        after = self.source_path.read_bytes()
        self.assertEqual(before, after)

    def test_uploaded_file_bytes_are_unchanged_after_a_dry_run(self):
        before = self.source_path.read_bytes()
        mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        after = self.source_path.read_bytes()
        self.assertEqual(before, after)

    def test_no_temp_copy_is_left_behind_after_a_dry_run(self):
        mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        leftover = [p.name for p in self.tmp.iterdir() if p.name.startswith(".mudanza-origen-")]
        self.assertEqual(leftover, [])

    def test_no_temp_copy_is_left_behind_after_an_apply(self):
        mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        leftover = [p.name for p in self.tmp.iterdir() if p.name.startswith(".mudanza-origen-")]
        self.assertEqual(leftover, [])

    def test_no_temp_copy_is_left_behind_when_the_import_raises_mid_way(self):
        with mock.patch.object(mudanza, "_run", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        leftover = [p.name for p in self.tmp.iterdir() if p.name.startswith(".mudanza-origen-")]
        self.assertEqual(leftover, [])


class OrigenActualMudanzaTest(MudanzaTestCase):
    """A source already AT today's schema gains no table from the copy's own migration step --
    proving the migration is a genuine no-op for the common case, not something that always adds
    something."""

    def test_current_schema_source_reports_zero_tables_added(self):
        source = self._new_source()
        source.close()
        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["origen"].origen_tablas_nuevas, 0)


class OrigenGenuinamenteAntiguoMudanzaTest(MudanzaTestCase):
    """BODY-MUDANZA-ORIGEN-ANTIGUO-1-R: OrigenAntiguoMudanzaTest above trims four tables and two
    columns off TODAY's schema by hand -- it never held a row in a table a current migration
    REBUILDS (propuesta_agente's CHECK widening), so it never caught the live crash
    (`TypeError: tuple indices must be integers or slices, not str` at the rebuild's `r["id"]`,
    body/db.py:1437, via `_migrated_source`'s bare `sqlite3.connect`, no `row_factory`). This
    fixture is the schema an older Body install GENUINELY had -- commit 3ef9e8e's own body/db.py,
    read from git history and run through its own `init()` -- then seeded with rows in every
    person-scoped table that schema has, including a propuesta_agente row for every `accion` its
    own CHECK allowed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.old_db = _load_old_db_module()

    def setUp(self):
        super().setUp()
        source = sqlite3.connect(self.source_path)
        self.old_db.init(source)
        self.pid = self.old_db.DEFAULT_PERSON_ID
        self.actions = _old_propuesta_actions(source)
        self.ids = _populate_old_schema(source, self.pid, self.actions)
        source.close()

    def test_dry_run_and_apply_complete_without_the_tuple_index_crash(self):
        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=False)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertGreater(by_name["origen"].origen_tablas_nuevas, 0)

        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)

    def test_every_old_propuesta_action_arrives(self):
        self.assertGreaterEqual(len(self.actions), 10)  # the older CHECK's own action vocabulary
        _, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)

        dest = db.connect()
        person_id = dest.execute(
            "SELECT id FROM person WHERE cuenta_sub = ?", (CUENTA_SUB,)
        ).fetchone()[0]
        rows = dest.execute(
            "SELECT accion FROM propuesta_agente WHERE person_id = ?", (person_id,)
        ).fetchall()
        self.assertEqual(sorted(r["accion"] for r in rows), sorted(self.actions))

    def test_the_tables_a_current_migration_rebuilds_survive_with_integrity_ok(self):
        reports, ok = mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        self.assertTrue(ok)
        by_name = self._reports_by_name(reports)
        self.assertEqual(by_name["goal"].counts.insert, 1)
        self.assertEqual(by_name["goal"].counts.other_person, 1)
        self.assertEqual(by_name["habit"].counts.insert, 1)
        self.assertEqual(by_name["habit"].counts.other_person, 2)
        self.assertEqual(by_name["goal_check"].counts.insert, 1)

        dest = db.connect()
        self.assertEqual(dest.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(dest.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_uploaded_file_bytes_are_unchanged_after_apply(self):
        before = self.source_path.read_bytes()
        mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        after = self.source_path.read_bytes()
        self.assertEqual(before, after)

    def test_no_temp_copy_is_left_behind_after_apply(self):
        mudanza.importar(self.source_path, CUENTA_SUB, apply=True)
        leftover = [p.name for p in self.tmp.iterdir() if p.name.startswith(".mudanza-origen-")]
        self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
