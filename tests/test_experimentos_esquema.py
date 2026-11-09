"""BODY-EXPERIMENTOS-ESQUEMA-1: wave 9's pure schema -- `experimento`, `experimento_registro` and
`moment` (BODY-EXPERIMENTOS-1-INV slice 3, ranked first of three shapes). An experiment is its own
concept, only optionally linked to a habit: zero change to goal/habit, their CHECKs,
goals.evaluar_semana, goal_access or any reader of `goal`. Invented demo data only, no real health
figures.
"""
import re
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

import tempfile

from tests._db import open_conn

from body import db, goals, privacidad


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


# The two experiments already running by hand (BODY-EXPERIMENTOS-1-INV), invented titles, no figures.
DUCHA_FRIA = dict(
    titulo="Ducha fria corta en turno de tarde",
    metricas=["stress_avg", "resting_hr"],
    ventana={"tipo": "dia"},
    filtro_dias={"turno_en": ["afternoon"], "turno_fuera": None, "excluir_tras_noche": True},
)
ENRAIZAMIENTO = dict(
    titulo="Rutina de enraizamiento al despertar",
    metricas=["heart_rate", "stress"],
    ventana={"tipo": "intradia", "ancla": "despertar", "antes_min": None, "despues_min": 60},
    filtro_dias={"turno_en": None, "turno_fuera": ["night"], "excluir_tras_noche": True},
)


class FreshDbTest(unittest.TestCase):
    """A fresh install gets every new shape directly from SCHEMA."""

    def setUp(self):
        self.conn = _db(self, "fresh.db")
        db.init(self.conn)

    def test_experimento_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(experimento)")}
        expected = {
            "id", "person_id", "titulo", "habito_id", "metricas", "ventana", "filtro_dias",
            "dias_prueba_objetivo", "fecha_inicio", "fecha_fin", "estado", "creado_en",
        }
        self.assertEqual(cols, expected)

    def test_experimento_registro_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(experimento_registro)")}
        expected = {
            "id", "experimento_id", "person_id", "day", "ocurrio", "nota", "momento_id", "creado_en",
        }
        self.assertEqual(cols, expected)

    def test_moment_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(moment)")}
        expected = {"id", "person_id", "ts_local", "kind", "note", "created_at"}
        self.assertEqual(cols, expected)

    def test_tables_start_empty(self):
        for table in ("experimento", "experimento_registro", "moment"):
            n = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(n, 0, table)


class ShiftKindsVocabularyTest(unittest.TestCase):
    """BODY-EXPERIMENTOS-ESQUEMA-1-M: one spelling of the shift-kind vocabulary. The shift and
    shift_batch_item CHECK constraints are parsed back out of the schema SQL and must equal
    db.SHIFT_KINDS exactly, so a fourth spelling (the two CHECKs, validar_experimento,
    shift_import.VALID_KINDS) can never drift from the others unnoticed."""

    def test_schema_check_constraints_match_shift_kinds(self):
        conn = _db(self, "vocabulario.db")
        db.init(conn)
        for table in ("shift", "shift_batch_item"):
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            match = re.search(r"kind\s+TEXT NOT NULL CHECK \(kind IN \(([^)]*)\)\)", sql)
            self.assertIsNotNone(match, f"{table}: no 'kind IN (...)' CHECK constraint found to parse")
            kinds = tuple(k.strip().strip("'") for k in match.group(1).split(","))
            self.assertEqual(kinds, db.SHIFT_KINDS, f"{table}'s CHECK constraint kinds drifted from db.SHIFT_KINDS")


class ValidarExperimentoTest(unittest.TestCase):
    """db.validar_experimento is the one place the three JSON shapes are checked."""

    def test_both_running_experiments_validate(self):
        db.validar_experimento(DUCHA_FRIA["metricas"], DUCHA_FRIA["ventana"], DUCHA_FRIA["filtro_dias"])
        db.validar_experimento(ENRAIZAMIENTO["metricas"], ENRAIZAMIENTO["ventana"], ENRAIZAMIENTO["filtro_dias"])

    def test_ventana_tipo_must_be_dia_or_intradia(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["stress_avg"], {"tipo": "semana"}, DUCHA_FRIA["filtro_dias"])
        self.assertIn("ventana", str(ctx.exception))

    def test_ventana_dia_rejects_extra_keys(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["stress_avg"], {"tipo": "dia", "ancla": "despertar"},
                                    DUCHA_FRIA["filtro_dias"])
        self.assertIn("ventana", str(ctx.exception))

    def test_ventana_intradia_requires_ancla_antes_min_and_despues_min(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["heart_rate"], {"tipo": "intradia", "ancla": "despertar"},
                                    DUCHA_FRIA["filtro_dias"])
        self.assertIn("ventana", str(ctx.exception))

    def test_ventana_intradia_ancla_must_be_known(self):
        ventana = {"tipo": "intradia", "ancla": "mediodia", "antes_min": None, "despues_min": 60}
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["heart_rate"], ventana, DUCHA_FRIA["filtro_dias"])
        self.assertIn("ventana", str(ctx.exception))

    def test_ventana_intradia_despues_min_must_be_positive(self):
        ventana = {"tipo": "intradia", "ancla": "despertar", "antes_min": None, "despues_min": 0}
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["heart_rate"], ventana, DUCHA_FRIA["filtro_dias"])
        self.assertIn("ventana", str(ctx.exception))

    def test_ventana_intradia_antes_min_rejects_negative(self):
        ventana = {"tipo": "intradia", "ancla": "despertar", "antes_min": -1, "despues_min": 60}
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["heart_rate"], ventana, DUCHA_FRIA["filtro_dias"])
        self.assertIn("ventana", str(ctx.exception))

    def test_ventana_intradia_antes_min_rejects_bool(self):
        ventana = {"tipo": "intradia", "ancla": "despertar", "antes_min": True, "despues_min": 60}
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["heart_rate"], ventana, DUCHA_FRIA["filtro_dias"])
        self.assertIn("ventana", str(ctx.exception))

    def test_filtro_dias_requires_exactly_the_three_keys(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["stress_avg"], DUCHA_FRIA["ventana"], {"excluir_tras_noche": True})
        self.assertIn("filtro_dias", str(ctx.exception))

    def test_filtro_dias_turno_en_must_be_a_known_kind(self):
        filtro = {"turno_en": ["luna"], "turno_fuera": None, "excluir_tras_noche": False}
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["stress_avg"], DUCHA_FRIA["ventana"], filtro)
        self.assertIn("filtro_dias", str(ctx.exception))

    def test_filtro_dias_turno_en_rejects_an_empty_list(self):
        filtro = {"turno_en": [], "turno_fuera": None, "excluir_tras_noche": False}
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["stress_avg"], DUCHA_FRIA["ventana"], filtro)
        self.assertIn("filtro_dias", str(ctx.exception))

    def test_filtro_dias_excluir_tras_noche_must_be_a_bool(self):
        filtro = {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": 1}
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["stress_avg"], DUCHA_FRIA["ventana"], filtro)
        self.assertIn("filtro_dias", str(ctx.exception))

    def test_metricas_must_be_a_non_empty_list(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento([], DUCHA_FRIA["ventana"], DUCHA_FRIA["filtro_dias"])
        self.assertIn("metricas", str(ctx.exception))

    def test_metricas_for_tipo_dia_must_be_known_metric_def_keys(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["no_existe"], DUCHA_FRIA["ventana"], DUCHA_FRIA["filtro_dias"])
        self.assertIn("metricas", str(ctx.exception))

    def test_metricas_for_intradia_rejects_a_daily_only_key(self):
        with self.assertRaises(ValueError) as ctx:
            db.validar_experimento(["resting_hr"], ENRAIZAMIENTO["ventana"], ENRAIZAMIENTO["filtro_dias"])
        self.assertIn("metricas", str(ctx.exception))


class CrearExperimentoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "crear.db")
        db.init(self.conn)

    def test_both_running_experiments_are_expressible_as_rows(self):
        eid_a = db.crear_experimento(self.conn, 1, **DUCHA_FRIA)
        eid_b = db.crear_experimento(self.conn, 1, **ENRAIZAMIENTO)
        rows = {r["id"]: r for r in db.experimentos(self.conn, 1)}
        self.assertEqual(rows[eid_a]["titulo"], DUCHA_FRIA["titulo"])
        self.assertEqual(rows[eid_a]["estado"], "activo")
        self.assertIsNone(rows[eid_a]["habito_id"])
        self.assertEqual(rows[eid_b]["titulo"], ENRAIZAMIENTO["titulo"])

    def test_invalid_shape_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            db.crear_experimento(self.conn, 1, "Malo", ["no_existe"], {"tipo": "dia"},
                                  {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM experimento").fetchone()[0], 0)

    def test_habito_id_must_be_this_persons_own_habit(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        gid = goals.propose(self.conn, 2, "Meta de B", "2026-11-01", kind="milestone", deadline="2027-01-01")
        hid = goals.add_habit(self.conn, gid, "Habito de B", {"per": "day", "times": 1})
        self.conn.commit()
        with self.assertRaises(ValueError) as ctx:
            db.crear_experimento(self.conn, 1, habito_id=hid, **DUCHA_FRIA)
        self.assertIn("habito_id", str(ctx.exception))

    def test_habito_id_links_to_an_own_habit(self):
        gid = goals.propose(self.conn, 1, "Mi meta", "2026-11-01", kind="milestone", deadline="2027-01-01")
        hid = goals.add_habit(self.conn, gid, "Mi habito", {"per": "day", "times": 1})
        self.conn.commit()
        eid = db.crear_experimento(self.conn, 1, habito_id=hid, **DUCHA_FRIA)
        row = self.conn.execute("SELECT habito_id FROM experimento WHERE id = ?", (eid,)).fetchone()
        self.assertEqual(row["habito_id"], hid)


class PersonIsolationTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "aislamiento.db")
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        self.eid = db.crear_experimento(self.conn, 1, **DUCHA_FRIA)

    def test_experimentos_only_lists_the_owners_rows(self):
        db.crear_experimento(self.conn, 2, **ENRAIZAMIENTO)
        ids_1 = {r["id"] for r in db.experimentos(self.conn, 1)}
        ids_2 = {r["id"] for r in db.experimentos(self.conn, 2)}
        self.assertEqual(ids_1, {self.eid})
        self.assertNotIn(self.eid, ids_2)

    def test_another_person_cannot_register_on_this_experiment(self):
        with self.assertRaises(ValueError) as ctx:
            db.registrar_dia_experimento(self.conn, self.eid, 2, "2026-11-09", True)
        self.assertIn("experimento_id", str(ctx.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM experimento_registro").fetchone()[0], 0)

    def test_experimentos_filtered_by_estado_stays_person_scoped(self):
        rows = db.experimentos(self.conn, 2, estado="activo")
        self.assertEqual(list(rows), [])


class RegistrarDiaExperimentoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "registro.db")
        db.init(self.conn)
        self.eid = db.crear_experimento(self.conn, 1, **DUCHA_FRIA)

    def test_registering_a_day_is_upserted_on_experimento_and_day(self):
        db.registrar_dia_experimento(self.conn, self.eid, 1, "2026-11-09", True, nota="se sintio bien")
        db.registrar_dia_experimento(self.conn, self.eid, 1, "2026-11-09", False, nota="no hubo turno")
        rows = self.conn.execute(
            "SELECT ocurrio, nota FROM experimento_registro WHERE experimento_id = ? AND day = ?",
            (self.eid, "2026-11-09"),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ocurrio"], 0)
        self.assertEqual(rows[0]["nota"], "no hubo turno")

    def test_registering_a_moment_links_momento_id(self):
        mid = db.crear_momento(self.conn, 1, "2026-11-09T06:30:00-05:00", "ducha_fria")
        db.registrar_dia_experimento(self.conn, self.eid, 1, "2026-11-09", True, momento_id=mid)
        row = self.conn.execute(
            "SELECT momento_id FROM experimento_registro WHERE experimento_id = ? AND day = ?",
            (self.eid, "2026-11-09"),
        ).fetchone()
        self.assertEqual(row["momento_id"], mid)

    def test_unknown_experimento_id_is_refused(self):
        with self.assertRaises(ValueError):
            db.registrar_dia_experimento(self.conn, 999999, 1, "2026-11-09", True)


def _old_db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """A DB built from the current SCHEMA (so every other table is already right) with the three
    wave-9 tables dropped -- the same technique tests/test_entreno_schema.py uses, except these
    tables are purely additive, so there is no old shape to recreate: dropping them is enough to
    simulate main's current schema."""
    conn = _db(case, name)
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE experimento_registro")
    conn.execute("DROP TABLE experimento")
    conn.execute("DROP TABLE moment")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class MigrationOnAnExistingDbTest(unittest.TestCase):
    def setUp(self):
        self.conn = _old_db(self, "vieja.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.conn.commit()

    def _assert_tables_exist_and_are_empty(self):
        for table in ("experimento", "experimento_registro", "moment"):
            n = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(n, 0, table)

    def test_migration_creates_the_three_empty_tables(self):
        db.init(self.conn)
        self._assert_tables_exist_and_are_empty()

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.crear_experimento(self.conn, 1, **DUCHA_FRIA)
        db.init(self.conn)
        rows = db.experimentos(self.conn, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["titulo"], DUCHA_FRIA["titulo"])


class PrivacyCensusTest(unittest.TestCase):
    """experimento, experimento_registro and moment are the person's own data: privacidad.py's
    census puts all three in FULLY_HANDLED_TABLES, so an invented row is in the person's export and
    gone after delete-all, same as every other person-owned table."""

    def setUp(self):
        self.conn = _db(self, "privacidad.db")
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        self.eid = db.crear_experimento(self.conn, 1, **DUCHA_FRIA)
        self.mid = db.crear_momento(self.conn, 1, "2026-11-09T06:30:00-05:00", "ducha_fria")
        db.registrar_dia_experimento(self.conn, self.eid, 1, "2026-11-09", True, momento_id=self.mid)
        self.eid_b = db.crear_experimento(self.conn, 2, **ENRAIZAMIENTO)

    def test_export_has_only_this_persons_rows(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["experimentos"]["experimentos"]), 1)
        self.assertEqual(data["experimentos"]["experimentos"][0]["titulo"], DUCHA_FRIA["titulo"])
        self.assertEqual(len(data["experimentos"]["registros"]), 1)
        self.assertEqual(len(data["experimentos"]["momentos"]), 1)

    def test_delete_all_removes_only_this_persons_rows(self):
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM experimento WHERE person_id = 1").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM experimento_registro WHERE person_id = 1").fetchone()[0], 0
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM moment WHERE person_id = 1").fetchone()[0], 0)
        # B's own experiment is untouched.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM experimento WHERE id = ?", (self.eid_b,)).fetchone()[0], 1)

    def test_every_person_table_is_exported_or_excepted(self):
        found = privacidad.person_tables(self.conn)
        accounted = (
            privacidad.FULLY_HANDLED_TABLES | privacidad.PARTIAL_EXPORT_TABLES | privacidad.NEVER_EXPORTED_TABLES
        )
        self.assertEqual(found, accounted)


if __name__ == "__main__":
    unittest.main()
