"""body/mudanza.py: move one person's data from a PC database into this process's own database
(BODY-MUDANZA-HERRAMIENTA-1). Invented demo data only -- never a real health figure."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import backup, config, cuenta, db, mudanza, privacidad

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
        "VALUES (?, 'consentimiento-v3', ?)",
        (person_id, NOW),
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


if __name__ == "__main__":
    unittest.main()
