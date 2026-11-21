"""BODY-PLAN-ESQUEMA-1: the plan's pure schema -- two additive columns (goal.horizonte_goal_id,
experimento.goal_id) and four new tables (horizonte_regla, horizonte_semana,
horizonte_meta_semanal, horizonte_checkpoint), each a child row under a milestone goal (the
plan's own horizon, ORCH-CARLOS-DELEGATES-TIER-1-1). Invented demo data only, no real health
figures.

BODY-PLAN-FASES-2 ruling 1/2: a plan moves through phases -- a fifth table (horizonte_fase) and a
nullable additive column on horizonte_regla (fase_id), both added here too.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, goals


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


NEW_TABLES = ("horizonte_regla", "horizonte_semana", "horizonte_meta_semanal", "horizonte_checkpoint")
FASES_TABLE = ("horizonte_fase",)


def _table_info(conn: sqlite3.Connection, name: str) -> list:
    return [tuple(r) for r in conn.execute(f"PRAGMA table_info({name})")]


def _index_list(conn: sqlite3.Connection, name: str) -> list:
    return sorted(r["name"] for r in conn.execute(f"PRAGMA index_list({name})"))


class FreshDbTest(unittest.TestCase):
    """A fresh install gets every new shape directly from SCHEMA."""

    def setUp(self):
        self.conn = _db(self, "fresh.db")
        db.init(self.conn)

    def test_goal_has_horizonte_goal_id(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(goal)")}
        self.assertIn("horizonte_goal_id", cols)

    def test_experimento_has_goal_id(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(experimento)")}
        self.assertIn("goal_id", cols)

    def test_horizonte_regla_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(horizonte_regla)")}
        self.assertEqual(cols, {
            "id", "horizonte_goal_id", "person_id", "contexto", "contexto_valor", "texto", "chequeo",
            "orden", "activa", "creado_en", "fase_id",
        })

    def test_horizonte_fase_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(horizonte_fase)")}
        self.assertEqual(cols, {
            "id", "horizonte_goal_id", "person_id", "titulo", "start_day", "end_day", "nota", "orden",
            "creado_en",
        })

    def test_horizonte_semana_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(horizonte_semana)")}
        self.assertEqual(cols, {
            "id", "horizonte_goal_id", "person_id", "week_start", "titulo", "nota", "creado_en",
        })

    def test_horizonte_meta_semanal_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(horizonte_meta_semanal)")}
        self.assertEqual(cols, {
            "id", "horizonte_goal_id", "person_id", "week_start", "medida", "sentido", "valor", "nota",
            "vigente", "reemplaza_id", "origen", "creado_en",
        })

    def test_horizonte_checkpoint_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(horizonte_checkpoint)")}
        self.assertEqual(cols, {
            "id", "horizonte_goal_id", "person_id", "day", "titulo", "criterios", "nota", "creado_en",
        })

    def test_new_tables_start_empty(self):
        for table in NEW_TABLES + FASES_TABLE:
            n = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(n, 0, table)


def _pre_slice_schema() -> str:
    """db.SCHEMA with this slice's own additions spliced back out, by locating its own
    ledger-tagged comments -- rebuilding the exact shape main had before BODY-PLAN-ESQUEMA-1 (same
    CHECKs, PK, FKs on goal/experimento, just missing what this slice adds), rather than a rebuild
    that would silently drop constraints SQLite's own ALTER ... DROP COLUMN cannot apply next to a
    SQL comment."""
    s = db.SCHEMA
    goal_start = s.index("    -- BODY-PLAN-ESQUEMA-1: when this goal belongs")
    goal_end = s.index("    CHECK (\n        (kind = 'metric'")
    s = s[:goal_start] + s[goal_end:]

    exp_start = s.index(",\n    -- BODY-PLAN-ESQUEMA-1: when this experiment is part of a plan")
    exp_marker = "    goal_id              INTEGER REFERENCES goal(id)\n"
    exp_end = s.index(exp_marker) + len(exp_marker)
    s = s[:exp_start] + "\n" + s[exp_end:]

    tables_start = s.index("-- BODY-PLAN-ESQUEMA-1: the plan's own parts")
    tables_end = s.index("-- BODY-PERIODO-1:")
    s = s[:tables_start] + s[tables_end:]
    return s


def _old_db(case: unittest.TestCase, name: str) -> sqlite3.Connection:
    """A DB built from main's pre-slice schema (_pre_slice_schema above) -- the two additive
    columns and four new tables absent, every other constraint on goal/experimento untouched."""
    conn = _db(case, name)
    conn.executescript(_pre_slice_schema())
    return conn


def _pre_fases_schema() -> str:
    """db.SCHEMA with ONLY BODY-PLAN-FASES-2's own additions spliced back out -- the shape main
    had right before this slice: the four BODY-PLAN-ESQUEMA-1 tables present, horizonte_fase and
    horizonte_regla.fase_id absent. Same technique as _pre_slice_schema above, on this slice's own
    ledger-tagged markers."""
    s = db.SCHEMA
    fase_start = s.index("-- BODY-PLAN-FASES-2: horizonte_fase is created FIRST")
    fase_end = s.index("CREATE TABLE IF NOT EXISTS horizonte_regla")
    s = s[:fase_start] + s[fase_end:]

    col_start = s.index(",\n    -- BODY-PLAN-FASES-2 ruling 2: additive and nullable")
    col_marker = "    fase_id           INTEGER REFERENCES horizonte_fase(id)\n"
    col_end = s.index(col_marker) + len(col_marker)
    s = s[:col_start] + "\n" + s[col_end:]
    return s


def _old_fases_db(case: unittest.TestCase, name: str) -> sqlite3.Connection:
    """A DB built from main's pre-BODY-PLAN-FASES-2 schema (_pre_fases_schema above) -- the
    original four horizonte_* tables present and populated exactly as BODY-PLAN-ESQUEMA-1 left
    them, horizonte_fase and fase_id absent."""
    conn = _db(case, name)
    conn.executescript(_pre_fases_schema())
    return conn


class MigrationOnAnExistingDbTest(unittest.TestCase):
    def setUp(self):
        self.conn = _old_db(self, "vieja.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.conn.commit()

    def test_migration_adds_the_two_columns(self):
        db.init(self.conn)
        gcols = {r[1] for r in self.conn.execute("PRAGMA table_info(goal)")}
        self.assertIn("horizonte_goal_id", gcols)
        ecols = {r[1] for r in self.conn.execute("PRAGMA table_info(experimento)")}
        self.assertIn("goal_id", ecols)

    def test_migration_creates_the_four_empty_tables(self):
        db.init(self.conn)
        for table in NEW_TABLES:
            n = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(n, 0, table)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        db.crear_regla_horizonte(self.conn, 1, gid, "siempre", None, "texto")
        db.init(self.conn)
        rows = self.conn.execute("SELECT COUNT(*) FROM horizonte_regla").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_fresh_and_migrated_schema_end_up_identical(self):
        db.init(self.conn)
        fresh = _db(self, "fresh_compare.db")
        db.init(fresh)
        for table in ("goal", "experimento") + NEW_TABLES:
            self.assertEqual(_table_info(self.conn, table), _table_info(fresh, table), table)
            self.assertEqual(_index_list(self.conn, table), _index_list(fresh, table), table)


class GoalColumnCheckTest(unittest.TestCase):
    """goal's own CHECK (kind/metric_key/direction/deadline) is unmodified by the additive
    horizonte_goal_id column -- still enforced exactly as before this slice."""

    def setUp(self):
        self.conn = _db(self, "goal_check.db")
        db.init(self.conn)

    def test_a_milestone_goal_can_set_horizonte_goal_id_to_null(self):
        gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        row = self.conn.execute("SELECT horizonte_goal_id FROM goal WHERE id = ?", (gid,)).fetchone()
        self.assertIsNone(row["horizonte_goal_id"])

    def test_a_sub_goal_can_point_at_a_horizon_goal(self):
        horizon = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        sub = goals.propose(self.conn, 1, "Sub-meta", "2026-11-01", metric_key="resting_hr", direction="lower",
                             target_value=50.0, baseline_value=60.0)
        self.conn.execute("UPDATE goal SET horizonte_goal_id = ? WHERE id = ?", (horizon, sub))
        self.conn.commit()
        row = self.conn.execute("SELECT horizonte_goal_id FROM goal WHERE id = ?", (sub,)).fetchone()
        self.assertEqual(row["horizonte_goal_id"], horizon)

    def test_metric_goal_still_refuses_missing_direction(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO goal (person_id, area, title, kind, metric_key, direction, start_day, status, "
                "visibility) VALUES (1, 'body', 'Malo', 'metric', 'resting_hr', NULL, '2026-11-01', 'proposed', "
                "'private')"
            )


class HorizonteReglaCheckTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "regla_check.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()

    def test_contexto_check_refuses_an_unknown_value(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_regla (horizonte_goal_id, person_id, contexto, contexto_valor, texto, "
                "orden, activa, creado_en) VALUES (?, 1, 'nunca', NULL, 'x', 0, 1, '2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )

    def test_activa_check_refuses_a_value_outside_0_1(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_regla (horizonte_goal_id, person_id, contexto, contexto_valor, texto, "
                "orden, activa, creado_en) VALUES (?, 1, 'siempre', NULL, 'x', 0, 2, '2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )

    def test_chequeo_check_refuses_invalid_json(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_regla (horizonte_goal_id, person_id, contexto, contexto_valor, texto, "
                "chequeo, orden, activa, creado_en) VALUES (?, 1, 'siempre', NULL, 'x', '{not json}', 0, 1, "
                "'2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )

    def test_chequeo_accepts_valid_json(self):
        self.conn.execute(
            "INSERT INTO horizonte_regla (horizonte_goal_id, person_id, contexto, contexto_valor, texto, "
            "chequeo, orden, activa, creado_en) VALUES (?, 1, 'siempre', NULL, 'x', '{\"a\": 1}', 0, 1, "
            "'2026-11-01T00:00:00+00:00')",
            (self.gid,),
        )

    def test_unknown_horizonte_goal_id_is_refused_by_the_fk(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_regla (horizonte_goal_id, person_id, contexto, contexto_valor, texto, "
                "orden, activa, creado_en) VALUES (999999, 1, 'siempre', NULL, 'x', 0, 1, "
                "'2026-11-01T00:00:00+00:00')"
            )


class ValidarReglaHorizonteTest(unittest.TestCase):
    """db.validar_regla_horizonte checks contexto_valor against SHIFT_KINDS for 'turno' and the
    period_kind table for 'periodo' -- the part a CHECK cannot express."""

    def setUp(self):
        self.conn = _db(self, "validar_regla.db")
        db.init(self.conn)

    def test_siempre_accepts_null(self):
        db.validar_regla_horizonte(self.conn, "siempre", None)

    def test_descanso_accepts_null(self):
        db.validar_regla_horizonte(self.conn, "descanso", None)

    def test_siempre_refuses_a_non_null_value(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_regla_horizonte(self.conn, "siempre", "night")
        self.assertIn("contexto_valor", str(ctx.exception))

    def test_turno_accepts_every_shift_kind(self):
        for kind in db.SHIFT_KINDS:
            db.validar_regla_horizonte(self.conn, "turno", kind)

    def test_turno_refuses_an_unknown_kind(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_regla_horizonte(self.conn, "turno", "luna")
        self.assertIn("contexto_valor", str(ctx.exception))

    def test_periodo_accepts_a_seeded_period_kind(self):
        db.validar_regla_horizonte(self.conn, "periodo", "malestar")

    def test_periodo_refuses_an_unknown_kind(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_regla_horizonte(self.conn, "periodo", "no_existe")
        self.assertIn("contexto_valor", str(ctx.exception))

    def test_tras_noche_accepts_a_day_count_as_text(self):
        db.validar_regla_horizonte(self.conn, "tras_noche", "2")

    def test_tras_noche_refuses_a_non_digit_string(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_regla_horizonte(self.conn, "tras_noche", "dos")
        self.assertIn("contexto_valor", str(ctx.exception))

    def test_unknown_contexto_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_regla_horizonte(self.conn, "nunca", None)
        self.assertIn("contexto", str(ctx.exception))


class CrearReglaHorizonteTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "crear_regla.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()

    def test_creates_a_row(self):
        rid = db.crear_regla_horizonte(self.conn, 1, self.gid, "turno", "night", "Dormir antes del turno")
        row = self.conn.execute("SELECT * FROM horizonte_regla WHERE id = ?", (rid,)).fetchone()
        self.assertEqual(row["contexto"], "turno")
        self.assertEqual(row["contexto_valor"], "night")
        self.assertEqual(row["activa"], 1)

    def test_invalid_contexto_valor_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            db.crear_regla_horizonte(self.conn, 1, self.gid, "turno", "luna", "x")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM horizonte_regla").fetchone()[0], 0)

    def test_empty_texto_is_refused(self):
        with self.assertRaises(ValueError):
            db.crear_regla_horizonte(self.conn, 1, self.gid, "siempre", None, "")


class CrearSemanaHorizonteTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "crear_semana.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()

    def test_a_monday_is_accepted(self):
        sid = db.crear_semana_horizonte(self.conn, 1, self.gid, "2026-11-09", titulo="Semana 1")
        row = self.conn.execute("SELECT * FROM horizonte_semana WHERE id = ?", (sid,)).fetchone()
        self.assertEqual(row["week_start"], "2026-11-09")

    def test_a_non_monday_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            db.crear_semana_horizonte(self.conn, 1, self.gid, "2026-11-10")
        self.assertIn("week_start", str(ctx.exception))

    def test_the_same_week_twice_for_one_goal_is_refused_by_the_unique_index(self):
        db.crear_semana_horizonte(self.conn, 1, self.gid, "2026-11-09")
        with self.assertRaises(sqlite3.IntegrityError):
            db.crear_semana_horizonte(self.conn, 1, self.gid, "2026-11-09")

    def test_the_same_week_for_two_different_goals_is_allowed(self):
        gid2 = goals.propose(self.conn, 1, "Otro horizonte", "2026-11-01", kind="milestone", deadline="2027-06-01")
        db.crear_semana_horizonte(self.conn, 1, self.gid, "2026-11-09")
        db.crear_semana_horizonte(self.conn, 1, gid2, "2026-11-09")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM horizonte_semana").fetchone()[0], 2)


class MetaSemanalPartialUniqueIndexTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "meta_unico.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()

    def test_two_vigente_rows_for_the_same_triple_are_refused(self):
        db.crear_meta_semanal(self.conn, 1, self.gid, "2026-11-09", "resting_hr", "al_menos", 50.0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_meta_semanal (horizonte_goal_id, person_id, week_start, medida, sentido, "
                "valor, vigente, origen, creado_en) VALUES (?, 1, '2026-11-09', 'resting_hr', 'al_menos', 52.0, 1, "
                "'persona', '2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )

    def test_a_vigente_row_plus_a_replaced_one_is_allowed(self):
        mid = db.crear_meta_semanal(self.conn, 1, self.gid, "2026-11-09", "resting_hr", "al_menos", 50.0)
        db.corregir_meta_semanal(self.conn, 1, mid, 52.0)
        rows = self.conn.execute(
            "SELECT valor, vigente FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? ORDER BY id", (self.gid,)
        ).fetchall()
        self.assertEqual([(r["valor"], r["vigente"]) for r in rows], [(50.0, 0), (52.0, 1)])

    def test_a_different_medida_in_the_same_week_is_allowed(self):
        db.crear_meta_semanal(self.conn, 1, self.gid, "2026-11-09", "resting_hr", "al_menos", 50.0)
        db.crear_meta_semanal(self.conn, 1, self.gid, "2026-11-09", "hrv", "al_menos", 60.0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM horizonte_meta_semanal").fetchone()[0], 2)

    def test_sentido_check_refuses_an_unknown_value(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_meta_semanal (horizonte_goal_id, person_id, week_start, medida, sentido, "
                "valor, vigente, origen, creado_en) VALUES (?, 1, '2026-11-09', 'resting_hr', 'nunca', 50.0, 1, "
                "'persona', '2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )

    def test_origen_check_refuses_an_unknown_value(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_meta_semanal (horizonte_goal_id, person_id, week_start, medida, sentido, "
                "valor, vigente, origen, creado_en) VALUES (?, 1, '2026-11-09', 'resting_hr', 'al_menos', 50.0, 1, "
                "'robot', '2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )

    def test_vigente_check_refuses_a_value_outside_0_1(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_meta_semanal (horizonte_goal_id, person_id, week_start, medida, sentido, "
                "valor, vigente, origen, creado_en) VALUES (?, 1, '2026-11-09', 'resting_hr', 'al_menos', 50.0, 2, "
                "'persona', '2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )


class CorregirMetaSemanalTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "corregir.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()
        self.mid = db.crear_meta_semanal(self.conn, 1, self.gid, "2026-11-09", "resting_hr", "al_menos", 50.0)

    def test_leaves_exactly_one_vigente_row(self):
        db.corregir_meta_semanal(self.conn, 1, self.mid, 52.0)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? AND vigente = 1", (self.gid,)
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_the_old_row_is_intact_with_vigente_0(self):
        db.corregir_meta_semanal(self.conn, 1, self.mid, 52.0)
        old = self.conn.execute("SELECT valor, vigente FROM horizonte_meta_semanal WHERE id = ?",
                                 (self.mid,)).fetchone()
        self.assertEqual((old["valor"], old["vigente"]), (50.0, 0))

    def test_the_new_row_points_back_via_reemplaza_id(self):
        new_id = db.corregir_meta_semanal(self.conn, 1, self.mid, 52.0)
        new = self.conn.execute("SELECT reemplaza_id, valor FROM horizonte_meta_semanal WHERE id = ?",
                                 (new_id,)).fetchone()
        self.assertEqual((new["reemplaza_id"], new["valor"]), (self.mid, 52.0))

    def test_rolls_back_whole_on_failure(self):
        with self.assertRaises(ValueError):
            db.corregir_meta_semanal(self.conn, 1, self.mid, 52.0, sentido="nunca")
        row = self.conn.execute("SELECT vigente FROM horizonte_meta_semanal WHERE id = ?", (self.mid,)).fetchone()
        self.assertEqual(row["vigente"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM horizonte_meta_semanal").fetchone()[0], 1)

    def test_refuses_a_meta_id_that_is_not_this_persons_own(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        with self.assertRaises(ValueError) as ctx:
            db.corregir_meta_semanal(self.conn, 2, self.mid, 52.0)
        self.assertIn("meta_id", str(ctx.exception))

    def test_refuses_a_meta_id_already_replaced(self):
        db.corregir_meta_semanal(self.conn, 1, self.mid, 52.0)
        with self.assertRaises(ValueError) as ctx:
            db.corregir_meta_semanal(self.conn, 1, self.mid, 54.0)
        self.assertIn("already replaced", str(ctx.exception))


class CrearCheckpointHorizonteTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "checkpoint.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()

    def test_creates_a_row_with_criterios_as_json(self):
        import json
        cid = db.crear_checkpoint_horizonte(self.conn, 1, self.gid, "2026-11-15", "Chequeo 1", ["a", "b"])
        row = self.conn.execute("SELECT * FROM horizonte_checkpoint WHERE id = ?", (cid,)).fetchone()
        self.assertEqual(json.loads(row["criterios"]), ["a", "b"])

    def test_empty_criterios_is_refused(self):
        with self.assertRaises(ValueError):
            db.crear_checkpoint_horizonte(self.conn, 1, self.gid, "2026-11-15", "Chequeo 1", [])

    def test_non_list_criterios_is_refused(self):
        with self.assertRaises(ValueError):
            db.crear_checkpoint_horizonte(self.conn, 1, self.gid, "2026-11-15", "Chequeo 1", "no es lista")

    def test_criterios_check_refuses_invalid_json_at_the_db_level(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_checkpoint (horizonte_goal_id, person_id, day, titulo, criterios, "
                "creado_en) VALUES (?, 1, '2026-11-15', 'x', '{not json}', '2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )


class FasesMigrationOnAnExistingDbTest(unittest.TestCase):
    """BODY-PLAN-FASES-2: migrating a database that already has BODY-PLAN-ESQUEMA-1's four
    horizonte_* tables (main's own shape right before this slice) but neither horizonte_fase nor
    horizonte_regla.fase_id."""

    def setUp(self):
        self.conn = _old_fases_db(self, "vieja_fases.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.conn.commit()
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.execute(
            "INSERT INTO horizonte_regla (horizonte_goal_id, person_id, contexto, contexto_valor, texto, "
            "orden, activa, creado_en) VALUES (?, 1, 'siempre', NULL, 'texto viejo', 0, 1, "
            "'2026-11-01T00:00:00+00:00')", (self.gid,),
        )
        self.conn.commit()

    def test_migration_adds_the_fase_id_column_as_null_on_existing_rows(self):
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT fase_id FROM horizonte_regla WHERE texto = 'texto viejo'").fetchone()
        self.assertIsNone(row["fase_id"])

    def test_migration_creates_the_empty_horizonte_fase_table(self):
        db.init(self.conn)
        n = self.conn.execute("SELECT COUNT(*) FROM horizonte_fase").fetchone()[0]
        self.assertEqual(n, 0)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.crear_fase_horizonte(self.conn, 1, self.gid, "Re-entry", "2026-11-01", "2026-11-14")
        db.init(self.conn)
        rows = self.conn.execute("SELECT COUNT(*) FROM horizonte_fase").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_fresh_and_migrated_schema_end_up_identical(self):
        db.init(self.conn)
        fresh = _db(self, "fresh_fases_compare.db")
        db.init(fresh)
        for table in ("horizonte_regla", "horizonte_fase"):
            self.assertEqual(_table_info(self.conn, table), _table_info(fresh, table), table)
            self.assertEqual(_index_list(self.conn, table), _index_list(fresh, table), table)


class HorizonteFaseCheckTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "fase_check.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()

    def test_start_after_end_is_refused_by_the_check(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_fase (horizonte_goal_id, person_id, titulo, start_day, end_day, orden, "
                "creado_en) VALUES (?, 1, 'x', '2026-11-14', '2026-11-01', 0, '2026-11-01T00:00:00+00:00')",
                (self.gid,),
            )

    def test_start_equal_to_end_is_accepted(self):
        self.conn.execute(
            "INSERT INTO horizonte_fase (horizonte_goal_id, person_id, titulo, start_day, end_day, orden, "
            "creado_en) VALUES (?, 1, 'x', '2026-11-14', '2026-11-14', 0, '2026-11-01T00:00:00+00:00')",
            (self.gid,),
        )

    def test_unknown_horizonte_goal_id_is_refused_by_the_fk(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_fase (horizonte_goal_id, person_id, titulo, start_day, end_day, orden, "
                "creado_en) VALUES (999999, 1, 'x', '2026-11-01', '2026-11-14', 0, "
                "'2026-11-01T00:00:00+00:00')"
            )

    def test_an_unknown_fase_id_on_horizonte_regla_is_refused_by_the_fk(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO horizonte_regla (horizonte_goal_id, person_id, contexto, contexto_valor, texto, "
                "orden, activa, creado_en, fase_id) VALUES (?, 1, 'siempre', NULL, 'x', 0, 1, "
                "'2026-11-01T00:00:00+00:00', 999999)", (self.gid,),
            )


class ValidarFasesHorizonteTest(unittest.TestCase):
    """db.validar_fases_horizonte: the one validator for a plan's phases -- never four hardcoded
    phases, never a phase-name constant (BODY-PLAN-FASES-2 ruling 1)."""

    def test_a_single_well_formed_phase_is_accepted(self):
        db.validar_fases_horizonte(
            [{"titulo": "Re-entry", "start_day": "2026-11-01", "end_day": "2026-11-14"}],
            "2026-11-01", "2027-01-01")

    def test_two_non_overlapping_phases_are_accepted(self):
        db.validar_fases_horizonte(
            [{"titulo": "Re-entry", "start_day": "2026-11-01", "end_day": "2026-11-14"},
             {"titulo": "Base", "start_day": "2026-11-15", "end_day": "2026-12-01"}],
            "2026-11-01", "2027-01-01")

    def test_empty_titulo_is_refused_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_fases_horizonte(
                [{"titulo": "", "start_day": "2026-11-01", "end_day": "2026-11-14"}],
                "2026-11-01", "2027-01-01")
        self.assertIn("titulo", str(ctx.exception))

    def test_start_after_end_is_refused_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_fases_horizonte(
                [{"titulo": "Re-entry", "start_day": "2026-11-14", "end_day": "2026-11-01"}],
                "2026-11-01", "2027-01-01")
        self.assertIn("fases", str(ctx.exception))

    def test_a_phase_outside_the_plans_own_dates_is_refused_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_fases_horizonte(
                [{"titulo": "Re-entry", "start_day": "2026-10-20", "end_day": "2026-11-14"}],
                "2026-11-01", "2027-01-01")
        self.assertIn("outside the plan's own dates", str(ctx.exception))

    def test_a_phase_reaching_past_the_deadline_is_refused_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_fases_horizonte(
                [{"titulo": "Taper", "start_day": "2026-12-20", "end_day": "2027-01-10"}],
                "2026-11-01", "2027-01-01")
        self.assertIn("outside the plan's own dates", str(ctx.exception))

    def test_overlapping_phases_are_refused_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_fases_horizonte(
                [{"titulo": "Re-entry", "start_day": "2026-11-01", "end_day": "2026-11-14"},
                 {"titulo": "Base", "start_day": "2026-11-10", "end_day": "2026-12-01"}],
                "2026-11-01", "2027-01-01")
        self.assertIn("overlaps", str(ctx.exception))

    def test_adjacent_phases_with_no_gap_are_accepted(self):
        """The day right after one phase ends can start the next -- 'never overlap' is not 'must
        leave a gap'."""
        db.validar_fases_horizonte(
            [{"titulo": "Re-entry", "start_day": "2026-11-01", "end_day": "2026-11-14"},
             {"titulo": "Base", "start_day": "2026-11-15", "end_day": "2026-12-01"}],
            "2026-11-01", "2027-01-01")


class CrearFaseHorizonteTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "crear_fase.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()

    def test_creates_a_row(self):
        fid = db.crear_fase_horizonte(self.conn, 1, self.gid, "Re-entry", "2026-11-01", "2026-11-14")
        row = self.conn.execute("SELECT * FROM horizonte_fase WHERE id = ?", (fid,)).fetchone()
        self.assertEqual(row["titulo"], "Re-entry")
        self.assertEqual((row["start_day"], row["end_day"]), ("2026-11-01", "2026-11-14"))

    def test_empty_titulo_is_refused(self):
        with self.assertRaises(ValueError):
            db.crear_fase_horizonte(self.conn, 1, self.gid, "", "2026-11-01", "2026-11-14")

    def test_start_after_end_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            db.crear_fase_horizonte(self.conn, 1, self.gid, "x", "2026-11-14", "2026-11-01")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM horizonte_fase").fetchone()[0], 0)

    def test_a_rule_can_name_its_phase(self):
        fid = db.crear_fase_horizonte(self.conn, 1, self.gid, "Re-entry", "2026-11-01", "2026-11-14")
        rid = db.crear_regla_horizonte(self.conn, 1, self.gid, "siempre", None, "Duerme 7h", fase_id=fid)
        row = self.conn.execute("SELECT fase_id FROM horizonte_regla WHERE id = ?", (rid,)).fetchone()
        self.assertEqual(row["fase_id"], fid)

    def test_a_rule_with_no_phase_stays_null(self):
        rid = db.crear_regla_horizonte(self.conn, 1, self.gid, "siempre", None, "Duerme 7h")
        row = self.conn.execute("SELECT fase_id FROM horizonte_regla WHERE id = ?", (rid,)).fetchone()
        self.assertIsNone(row["fase_id"])


class ActualizarQuitarRestaurarFaseHorizonteTest(unittest.TestCase):
    """db.actualizar_fase_horizonte / quitar_fase_horizonte / restaurar_fase_horizonte -- the
    update-shaped writer and the remove-detaches-never-deletes pair corregir_fase (a later layer)
    calls (BODY-PLAN-FASES-2 ruling 3)."""

    def setUp(self):
        self.conn = _db(self, "fase_escritores.db")
        db.init(self.conn)
        self.gid = goals.propose(self.conn, 1, "Horizonte", "2026-11-01", kind="milestone", deadline="2027-01-01")
        self.conn.commit()
        self.fid = db.crear_fase_horizonte(self.conn, 1, self.gid, "Re-entry", "2026-11-01", "2026-11-14")

    def test_actualizar_updates_the_fields_in_place(self):
        db.actualizar_fase_horizonte(self.conn, 1, self.fid, "Re-entry (ajustada)", "2026-11-02",
                                      "2026-11-16", "ajuste")
        row = self.conn.execute("SELECT * FROM horizonte_fase WHERE id = ?", (self.fid,)).fetchone()
        self.assertEqual(row["titulo"], "Re-entry (ajustada)")
        self.assertEqual((row["start_day"], row["end_day"], row["nota"]), ("2026-11-02", "2026-11-16", "ajuste"))

    def test_actualizar_never_touches_another_persons_phase(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        db.actualizar_fase_horizonte(self.conn, 2, self.fid, "Hackeada", "2026-11-02", "2026-11-16", None)
        row = self.conn.execute("SELECT titulo FROM horizonte_fase WHERE id = ?", (self.fid,)).fetchone()
        self.assertEqual(row["titulo"], "Re-entry")

    def test_quitar_deletes_the_phase_and_detaches_its_rules(self):
        rid = db.crear_regla_horizonte(self.conn, 1, self.gid, "siempre", None, "Duerme 7h", fase_id=self.fid)
        detached = db.quitar_fase_horizonte(self.conn, 1, self.fid)
        self.assertEqual(detached, [rid])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM horizonte_fase").fetchone()[0], 0)
        row = self.conn.execute("SELECT fase_id, texto FROM horizonte_regla WHERE id = ?", (rid,)).fetchone()
        self.assertIsNone(row["fase_id"])
        self.assertEqual(row["texto"], "Duerme 7h")  # the rule itself survives, never deleted

    def test_restaurar_brings_the_phase_back_with_its_own_id_and_reattaches_the_rules(self):
        rid = db.crear_regla_horizonte(self.conn, 1, self.gid, "siempre", None, "Duerme 7h", fase_id=self.fid)
        before = dict(self.conn.execute("SELECT * FROM horizonte_fase WHERE id = ?", (self.fid,)).fetchone())
        detached = db.quitar_fase_horizonte(self.conn, 1, self.fid)
        db.restaurar_fase_horizonte(self.conn, 1, before, detached)
        row = self.conn.execute("SELECT * FROM horizonte_fase WHERE id = ?", (self.fid,)).fetchone()
        self.assertEqual(row["id"], self.fid)
        self.assertEqual(row["titulo"], "Re-entry")
        regla = self.conn.execute("SELECT fase_id FROM horizonte_regla WHERE id = ?", (rid,)).fetchone()
        self.assertEqual(regla["fase_id"], self.fid)


class ForeignKeysTest(unittest.TestCase):
    """FKs hold with PRAGMA foreign_keys as the app sets it (db.connect's own setting)."""

    def setUp(self):
        self.conn = _db(self, "fk.db")
        db.init(self.conn)

    def test_foreign_keys_are_on(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_an_unknown_goal_id_on_experimento_is_refused(self):
        self.conn.execute(
            "INSERT INTO experimento (person_id, titulo, metricas, ventana, filtro_dias, fecha_inicio, estado, "
            "creado_en) VALUES (1, 'x', '[]', '{}', '{}', '2026-11-01', 'activo', '2026-11-01T00:00:00+00:00')"
        )
        eid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE experimento SET goal_id = 999999 WHERE id = ?", (eid,))


if __name__ == "__main__":
    unittest.main()
