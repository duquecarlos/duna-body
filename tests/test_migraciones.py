"""BODY-HOGAR-ESCRIBE-1 ruling 1: a database built before 'hogar' existed as an origen/aplicada_via
value gains the widened CHECKs, keeping every existing row -- same guarded table-rebuild pattern as
every other CHECK widening in body/db.py. Invented demo data only, never a real figure."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db
from body.agente import conversacion as agente_conversacion

_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_HOGAR = """
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito',
                         'proponer_experimento', 'marcar_experimento',
                         'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                         'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                         'registrar_comida', 'corregir_comida', 'registrar_checkin', 'descartar_series',
                         'separar_sesion', 'unir_sesiones', 'proponer_objetivo_nutriente',
                         'proponer_plan', 'corregir_meta_plan', 'ampliar_plan')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp'))
"""

_OLD_CONVERSACION_AGENTE_COLUMNS_SIN_HOGAR = """
    id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES person(id),
    creada_en TEXT NOT NULL, titulo TEXT,
    origen TEXT CHECK (origen IS NULL OR origen IN ('mcp'))
"""


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
    case.addCleanup(conn.close)
    return conn


class PropuestaAgenteHogarMigrationTest(unittest.TestCase):
    """A database whose propuesta_agente predates 'anotar_sesion'/'hogar' gains both widenings in
    one rebuild, keeping its existing row byte-for-byte."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = agente_conversacion.crear(self.conn, 1)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(f"CREATE TABLE propuesta_agente ({_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_HOGAR})")
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en, aplicada_via) VALUES (1, ?, 'proponer_plan', '{}', 'aplicada', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00', 'mcp')",
            (self.cid,),
        )
        self.old_id = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ?", (self.cid,)
        ).fetchone()[0]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_widens_accion_and_aplicada_via_keeping_the_old_row(self):
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT id, accion, aplicada_via FROM propuesta_agente WHERE conversacion_id = ?", (self.cid,)
        ).fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertEqual(row["accion"], "proponer_plan")
        self.assertEqual(row["aplicada_via"], "mcp")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 1)

        insert_anotar = self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'anotar_sesion', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (self.cid,),
        )
        self.conn.commit()
        self.assertEqual(insert_anotar.rowcount, 1)

        db.marcar_aplicada_via(self.conn, self.old_id, "hogar")
        updated = self.conn.execute(
            "SELECT aplicada_via FROM propuesta_agente WHERE id = ?", (self.old_id,)
        ).fetchone()
        self.assertEqual(updated["aplicada_via"], "hogar")

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ?", (self.cid,)
        ).fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 1)


_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_CORREGIR_ENTRENO = """
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito',
                         'proponer_experimento', 'marcar_experimento',
                         'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                         'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                         'registrar_comida', 'corregir_comida', 'registrar_checkin', 'descartar_series',
                         'separar_sesion', 'unir_sesiones', 'proponer_objetivo_nutriente',
                         'proponer_plan', 'corregir_meta_plan', 'ampliar_plan', 'anotar_sesion')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp', 'hogar'))
"""


class PropuestaAgenteCorregirEntrenoMigrationTest(unittest.TestCase):
    """BODY-CORREGIR-MEDIDA-ENTRENO-1-W ruling A: a database whose propuesta_agente predates
    'corregir_entreno' gains the widening in one rebuild, keeping its existing rows byte-for-byte,
    and then accepts a 'corregir_entreno' row."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = agente_conversacion.crear(self.conn, 1)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(
            f"CREATE TABLE propuesta_agente ({_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_CORREGIR_ENTRENO})"
        )
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'proponer_plan', '{}', 'aplicada', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'registrar_entreno', '{}', 'propuesta', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.old_ids = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
            ).fetchall()
        ]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_widens_accion_keeping_the_old_rows(self):
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], self.old_ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 2)

        insert_corregir = self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'corregir_entreno', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (self.cid,),
        )
        self.conn.commit()
        self.assertEqual(insert_corregir.rowcount, 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], self.old_ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 2)


class PropuestaAgenteCorregirEntrenoFreshDbTest(unittest.TestCase):
    """A fresh database (built with the current _PROPUESTA_AGENTE_COLUMNS) accepts a
    'corregir_entreno' row directly, with no rebuild needed."""

    def test_fresh_database_accepts_corregir_entreno(self):
        conn = _conn(self)
        db.init(conn)
        cid = agente_conversacion.crear(conn, 1)
        insert_corregir = conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'corregir_entreno', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (cid,),
        )
        conn.commit()
        self.assertEqual(insert_corregir.rowcount, 1)


_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_IMPORTAR_DESPLAZAMIENTO = """
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito',
                         'proponer_experimento', 'marcar_experimento',
                         'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                         'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                         'registrar_comida', 'corregir_comida', 'registrar_checkin', 'descartar_series',
                         'separar_sesion', 'unir_sesiones', 'proponer_objetivo_nutriente',
                         'proponer_plan', 'corregir_meta_plan', 'ampliar_plan', 'anotar_sesion',
                         'corregir_entreno')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp', 'hogar'))
"""


class PropuestaAgenteImportarDesplazamientoMigrationTest(unittest.TestCase):
    """BODY-DESPLAZAMIENTO-ASISTENTE-1-W ruling A: a database whose propuesta_agente predates
    'importar_desplazamiento' gains the widening in one rebuild, keeping its existing rows byte-for-byte,
    and then accepts an 'importar_desplazamiento' row."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = agente_conversacion.crear(self.conn, 1)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(
            f"CREATE TABLE propuesta_agente ({_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_IMPORTAR_DESPLAZAMIENTO})"
        )
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'proponer_plan', '{}', 'aplicada', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'registrar_entreno', '{}', 'propuesta', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.old_ids = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
            ).fetchall()
        ]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_widens_accion_keeping_the_old_rows(self):
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], self.old_ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 2)

        insert_importar = self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'importar_desplazamiento', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (self.cid,),
        )
        self.conn.commit()
        self.assertEqual(insert_importar.rowcount, 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], self.old_ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 2)


class PropuestaAgenteImportarDesplazamientoFreshDbTest(unittest.TestCase):
    """A fresh database (built with the current _PROPUESTA_AGENTE_COLUMNS) accepts an
    'importar_desplazamiento' row directly, with no rebuild needed."""

    def test_fresh_database_accepts_importar_desplazamiento(self):
        conn = _conn(self)
        db.init(conn)
        cid = agente_conversacion.crear(conn, 1)
        insert_importar = conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'importar_desplazamiento', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (cid,),
        )
        conn.commit()
        self.assertEqual(insert_importar.rowcount, 1)


# BODY-HISTORIA-VIDA-2 ruling 3: the fifteenth widening of propuesta_agente.accion (proponer_practica),
# through the SAME reusable rebuild (_rebuild_propuesta_agente_table) -- this is exactly today's
# shape, minus 'proponer_practica', the same "old == current, minus the newest value" convention
# every prior widening test in this file already follows.
_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_PROPONER_PRACTICA = """
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito',
                         'proponer_experimento', 'marcar_experimento',
                         'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                         'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                         'registrar_comida', 'corregir_comida', 'registrar_checkin', 'descartar_series',
                         'separar_sesion', 'unir_sesiones', 'proponer_objetivo_nutriente',
                         'proponer_plan', 'corregir_meta_plan', 'ampliar_plan', 'anotar_sesion',
                         'corregir_entreno', 'importar_desplazamiento')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp', 'hogar'))
"""


class PropuestaAgenteProponerPracticaMigrationTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-2 ruling 3: a database whose propuesta_agente predates 'proponer_practica'
    gains the widening in one rebuild, keeping its existing rows byte-for-byte, and then accepts a
    'proponer_practica' row."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = agente_conversacion.crear(self.conn, 1)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(
            f"CREATE TABLE propuesta_agente ({_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_PROPONER_PRACTICA})"
        )
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'proponer_plan', '{}', 'aplicada', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'importar_desplazamiento', '{}', 'propuesta', "
            "'2026-11-09T00:00:00+00:00', '2026-11-09T00:10:00+00:00')",
            (self.cid,),
        )
        self.old_ids = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
            ).fetchall()
        ]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_widens_accion_keeping_the_old_rows(self):
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], self.old_ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 2)

        insert_practica = self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'proponer_practica', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (self.cid,),
        )
        self.conn.commit()
        self.assertEqual(insert_practica.rowcount, 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], self.old_ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 2)


class PropuestaAgenteProponerPracticaFreshDbTest(unittest.TestCase):
    """A fresh database (built with the current _PROPUESTA_AGENTE_COLUMNS) accepts a
    'proponer_practica' row directly, with no rebuild needed."""

    def test_fresh_database_accepts_proponer_practica(self):
        conn = _conn(self)
        db.init(conn)
        cid = agente_conversacion.crear(conn, 1)
        insert_practica = conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'proponer_practica', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (cid,),
        )
        conn.commit()
        self.assertEqual(insert_practica.rowcount, 1)


class HabitPracticaIdMigrationTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-2 ruling 4: a database whose habit predates practica_id gains it,
    additive and nullable -- keeping the existing row byte-for-byte: an existing habit reads as
    practica_id NULL ("not linked to any practice")."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE habit")
        self.conn.execute(
            "CREATE TABLE habit (id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goal(id), "
            "title TEXT NOT NULL, cadence TEXT NOT NULL CHECK (json_valid(cadence)), "
            "cadence_text_legacy TEXT, assignee_person_id INTEGER REFERENCES person(id), "
            "active INTEGER NOT NULL DEFAULT 1)"
        )
        self.conn.execute(
            "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, "
            "target_value, baseline_value, start_day, status, visibility) VALUES "
            "(1, NULL, 'body', 'Vieja', 'metric', 'resting_hr', 'lower', 55, 60, "
            "'2026-11-09', 'active', 'private')"
        )
        self.goal_id = self.conn.execute("SELECT id FROM goal").fetchone()[0]
        self.conn.execute(
            "INSERT INTO habit (goal_id, title, cadence, active) VALUES (?, 'Caminar', "
            "'{\"per\": \"week\", \"times\": 3}', 1)", (self.goal_id,),
        )
        self.old_id = self.conn.execute("SELECT id FROM habit").fetchone()[0]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_adds_the_column_keeping_the_old_row(self):
        db.init(self.conn)
        row = self.conn.execute("SELECT id, title, practica_id FROM habit").fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertEqual(row["title"], "Caminar")
        self.assertIsNone(row["practica_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM habit").fetchone()[0], 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute("SELECT id, practica_id FROM habit").fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertIsNone(row["practica_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM habit").fetchone()[0], 1)

    def test_migrated_habit_accepts_a_practica_link(self):
        db.init(self.conn)
        self.conn.execute(
            "INSERT INTO practica (person_id, titulo, fuente, regla_json, meta_dias_semana, origen, "
            "creada_en) VALUES (1, 'ZZ', 'actividad', '{\"tipos\": [\"running\"]}', 5, 'persona', "
            "'2026-11-09T00:00:00+00:00')"
        )
        pid = self.conn.execute("SELECT id FROM practica").fetchone()[0]
        self.conn.execute("UPDATE habit SET practica_id = ? WHERE id = ?", (pid, self.old_id))
        self.conn.commit()
        row = self.conn.execute("SELECT practica_id FROM habit WHERE id = ?", (self.old_id,)).fetchone()
        self.assertEqual(row["practica_id"], pid)


class HabitPracticaIdFreshDbTest(unittest.TestCase):
    """A fresh database's habit table already carries practica_id -- no migration needed."""

    def test_fresh_database_has_the_column(self):
        conn = _conn(self)
        db.init(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(habit)")}
        self.assertIn("practica_id", cols)


class ConversacionAgenteHogarMigrationTest(unittest.TestCase):
    """A database whose conversacion_agente predates 'hogar' as an origen value gains the widened
    CHECK in one rebuild, keeping its existing row byte-for-byte."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE conversacion_agente")
        self.conn.execute(f"CREATE TABLE conversacion_agente ({_OLD_CONVERSACION_AGENTE_COLUMNS_SIN_HOGAR})")
        self.conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en, titulo, origen) "
            "VALUES (1, '2026-11-09T00:00:00+00:00', 'Vieja', 'mcp')"
        )
        self.old_id = self.conn.execute("SELECT id FROM conversacion_agente").fetchone()[0]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_widens_origen_keeping_the_old_row(self):
        db.init(self.conn)
        row = self.conn.execute("SELECT id, titulo, origen FROM conversacion_agente").fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertEqual(row["titulo"], "Vieja")
        self.assertEqual(row["origen"], "mcp")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversacion_agente").fetchone()[0], 1)

        nuevo_id = agente_conversacion.crear(self.conn, 1, origen="hogar")
        self.conn.commit()
        self.assertEqual(agente_conversacion.origen_de(self.conn, nuevo_id), "hogar")

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute("SELECT id FROM conversacion_agente").fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conversacion_agente").fetchone()[0], 1)


class ComidaMomentoMigrationTest(unittest.TestCase):
    """BODY-HOGAR-ESCRIBE-1-W ruling 3: a database whose comida predates the momento column gains
    it, additive and nullable -- keeping the existing row byte-for-byte."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE comida")
        self.conn.execute(
            "CREATE TABLE comida (id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES person(id), "
            "day TEXT NOT NULL, ts_local TEXT, descripcion TEXT, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "INSERT INTO comida (person_id, day, descripcion, created_at, updated_at) VALUES "
            "(1, '2026-11-09', 'Vieja', '2026-11-09T00:00:00+00:00', '2026-11-09T00:00:00+00:00')"
        )
        self.old_id = self.conn.execute("SELECT id FROM comida").fetchone()[0]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_adds_momento_keeping_the_old_row(self):
        db.init(self.conn)
        row = self.conn.execute("SELECT id, descripcion, momento FROM comida").fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertEqual(row["descripcion"], "Vieja")
        self.assertIsNone(row["momento"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute("SELECT id FROM comida").fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 1)


class MomentoComidaTableMigrationTest(unittest.TestCase):
    """BODY-HOGAR-ESCRIBE-1-W ruling 2: a database that predates momento_comida gains the table,
    empty, with no other table's row count disturbed, and is idempotent."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("DROP TABLE momento_comida")
        self.conn.execute(
            "INSERT INTO ajuste_agente (person_id, updated_at) VALUES (1, '2026-11-09T00:00:00+00:00')"
        )
        self.conn.commit()

    def test_migration_recreates_the_table_empty_without_touching_others(self):
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM momento_comida").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ajuste_agente").fetchone()[0], 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM momento_comida").fetchone()[0], 0)


class PlanNotaSesionOrigenMigrationTest(unittest.TestCase):
    """BODY-HOGAR-NOTA-SESION-GUARD-1 ruling 1: a database whose plan predates nota_sesion_origen
    gains it, additive and nullable -- keeping the existing row, and its existing note, byte for
    byte: an existing nota_sesion reads as nota_sesion_origen NULL ("written for the person")."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE plan")
        self.conn.execute(
            "CREATE TABLE plan (id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES person(id), "
            "hogar_id TEXT, goal_id INTEGER REFERENCES goal(id), day TEXT NOT NULL, time TEXT, "
            "title TEXT NOT NULL, booking TEXT, "
            "status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN "
            "('proposed', 'accepted', 'done', 'skipped')), "
            "ejercicios_json TEXT, nota_sesion TEXT, nota_sesion_en TEXT)"
        )
        self.conn.execute(
            "INSERT INTO plan (person_id, day, title, status, nota_sesion, nota_sesion_en) VALUES "
            "(1, '2026-11-09', 'Vieja', 'proposed', 'Nota previa', '2026-11-09T00:00:00+00:00')"
        )
        self.old_id = self.conn.execute("SELECT id FROM plan").fetchone()[0]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_adds_the_column_keeping_the_old_row_and_its_note(self):
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT id, title, nota_sesion, nota_sesion_en, nota_sesion_origen FROM plan"
        ).fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertEqual(row["title"], "Vieja")
        self.assertEqual(row["nota_sesion"], "Nota previa")
        self.assertEqual(row["nota_sesion_en"], "2026-11-09T00:00:00+00:00")
        self.assertIsNone(row["nota_sesion_origen"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plan").fetchone()[0], 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute("SELECT id, nota_sesion_origen FROM plan").fetchone()
        self.assertEqual(row["id"], self.old_id)
        self.assertIsNone(row["nota_sesion_origen"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plan").fetchone()[0], 1)


class EscrituraExternaTableMigrationTest(unittest.TestCase):
    """BODY-HOGAR-REGISTRAR-IDEMPOTENTE-1 ruling 1: a database that predates escritura_externa
    gains the table, empty, with no other table's row count disturbed, and is idempotent."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("DROP TABLE escritura_externa")
        self.conn.execute(
            "INSERT INTO ajuste_agente (person_id, updated_at) VALUES (1, '2026-11-09T00:00:00+00:00')"
        )
        self.conn.commit()

    def test_migration_recreates_the_table_empty_without_touching_others(self):
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM escritura_externa").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ajuste_agente").fetchone()[0], 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM escritura_externa").fetchone()[0], 0)


class PracticaTableMigrationTest(unittest.TestCase):
    """BODY-HISTORIA-VIDA-1 ruling 1: a database that predates `practica` gains the table (and its
    (person_id) index), empty, with no other table's row count disturbed; a fresh database gets
    the exact same shape (fresh == migrated); and running init() twice is a no-op either way."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("DROP TABLE practica")
        self.conn.execute(
            "INSERT INTO ajuste_agente (person_id, updated_at) VALUES (1, '2026-11-09T00:00:00+00:00')"
        )
        self.conn.commit()

    def test_migration_recreates_the_table_empty_without_touching_others(self):
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM practica").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ajuste_agente").fetchone()[0], 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM practica").fetchone()[0], 0)

    def test_migrated_database_accepts_a_row_and_keeps_its_index(self):
        db.init(self.conn)
        self.conn.execute(
            "INSERT INTO practica (person_id, titulo, fuente, regla_json, meta_dias_semana, origen, "
            "creada_en) VALUES (1, 'ZZ', 'actividad', '{}', 5, 'persona', '2026-11-09T00:00:00+00:00')"
        )
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM practica").fetchone()[0], 1)
        idx = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_practica_person'"
        ).fetchone()
        self.assertIsNotNone(idx)


class PracticaFreshDbTest(unittest.TestCase):
    """A fresh database already carries `practica` and its index -- no migration needed."""

    def test_fresh_database_has_the_table_and_index(self):
        conn = _conn(self)
        db.init(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM practica").fetchone()[0], 0)
        idx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_practica_person'"
        ).fetchone()
        self.assertIsNotNone(idx)


# BODY-DESPERTAR-REAL-4 ruling 2 (ORCH-CARLOS-DELEGATES-TIER-1-1): the sixteenth widening of
# propuesta_agente.accion (corregir_despertar), through the SAME reusable rebuild -- this is
# exactly today's shape, minus 'corregir_despertar', the same "old == current, minus the newest
# value" convention every prior widening test in this file already follows.
_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_CORREGIR_DESPERTAR = """
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL REFERENCES person(id),
    conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
    accion          TEXT NOT NULL CHECK (accion IN
                        ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos', 'proponer_idioma',
                         'planear_entreno', 'registrar_entreno', 'registrar_medida', 'marcar_habito',
                         'proponer_experimento', 'marcar_experimento',
                         'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                         'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                         'registrar_comida', 'corregir_comida', 'registrar_checkin', 'descartar_series',
                         'separar_sesion', 'unir_sesiones', 'proponer_objetivo_nutriente',
                         'proponer_plan', 'corregir_meta_plan', 'ampliar_plan', 'anotar_sesion',
                         'corregir_entreno', 'importar_desplazamiento', 'proponer_practica')),
    objetivo_id     INTEGER REFERENCES goal(id),
    antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
    despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
    estado          TEXT NOT NULL DEFAULT 'propuesta'
                        CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
    creada_en       TEXT NOT NULL,
    expira_en       TEXT NOT NULL,
    resuelta_en     TEXT,
    aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp', 'hogar'))
"""


class PropuestaAgenteCorregirDespertarMigrationTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-4 ruling 2: a database whose propuesta_agente predates
    'corregir_despertar' gains the widening in one rebuild, keeping its existing rows byte-for-byte,
    and then accepts a 'corregir_despertar' row."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.cid = agente_conversacion.crear(self.conn, 1)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(
            f"CREATE TABLE propuesta_agente ({_OLD_PROPUESTA_AGENTE_COLUMNS_SIN_CORREGIR_DESPERTAR})"
        )
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'proponer_plan', '{}', 'aplicada', "
            "'2026-11-18T00:00:00+00:00', '2026-11-18T00:10:00+00:00')",
            (self.cid,),
        )
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, estado, "
            "creada_en, expira_en) VALUES (1, ?, 'proponer_practica', '{}', 'propuesta', "
            "'2026-11-18T00:00:00+00:00', '2026-11-18T00:10:00+00:00')",
            (self.cid,),
        )
        self.old_ids = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
            ).fetchall()
        ]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.commit()

    def test_migration_widens_accion_keeping_the_old_rows(self):
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], self.old_ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 2)

        insert_despertar = self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'corregir_despertar', '{}', '2026-11-18T00:00:00+00:00', "
            "'2026-11-18T00:10:00+00:00')", (self.cid,),
        )
        self.conn.commit()
        self.assertEqual(insert_despertar.rowcount, 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM propuesta_agente WHERE conversacion_id = ? ORDER BY id", (self.cid,)
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], self.old_ids)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 2)


class PropuestaAgenteCorregirDespertarFreshDbTest(unittest.TestCase):
    """A fresh database (built with the current _PROPUESTA_AGENTE_COLUMNS) accepts a
    'corregir_despertar' row directly, with no rebuild needed."""

    def test_fresh_database_accepts_corregir_despertar(self):
        conn = _conn(self)
        db.init(conn)
        cid = agente_conversacion.crear(conn, 1)
        insert_despertar = conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'corregir_despertar', '{}', '2026-11-18T00:00:00+00:00', "
            "'2026-11-18T00:10:00+00:00')", (cid,),
        )
        conn.commit()
        self.assertEqual(insert_despertar.rowcount, 1)


class DespertarCorreccionTableMigrationTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-4 ruling 1: a database built before despertar_correccion existed gains
    the table on its next init(), additive, keeping every other table's row untouched; a fresh
    database gets the exact same shape; running init() twice is a no-op either way."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("DROP TABLE despertar_correccion")
        self.conn.execute(
            "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES "
            "(1, '2026-11-10', '2026-11-09T23:00:00', '2026-11-10T06:00:00')"
        )
        self.conn.commit()

    def test_migration_recreates_the_table_without_touching_others(self):
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 0)
        row = self.conn.execute("SELECT * FROM sleep_session WHERE person_id = 1").fetchone()
        self.assertEqual((row["day"], row["start_local"], row["end_local"]),
                         ("2026-11-10", "2026-11-09T23:00:00", "2026-11-10T06:00:00"))

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 0)

    def test_migrated_database_accepts_one_row_per_day_and_tipo(self):
        """BODY-DESPERTAR-REAL-6A-R2 ruling 1: a morning holds one sueno AND one levantado row --
        the UNIQUE is now (person_id, day, tipo), not (person_id, day)."""
        db.init(self.conn)
        self.conn.execute(
            "INSERT INTO despertar_correccion (person_id, day, tipo, hora_local, origen, creada_en) "
            "VALUES (1, '2026-11-10', 'levantado', '06:30', 'asistente', '2026-11-10T00:00:00+00:00')"
        )
        self.conn.execute(
            "INSERT INTO despertar_correccion (person_id, day, tipo, hora_local, origen, creada_en) "
            "VALUES (1, '2026-11-10', 'sueno', '06:15', 'persona', '2026-11-10T00:05:00+00:00')"
        )
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 2)
        with self.assertRaises(db.sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO despertar_correccion (person_id, day, tipo, hora_local, origen, creada_en) "
                "VALUES (1, '2026-11-10', 'sueno', '06:20', 'persona', '2026-11-10T00:10:00+00:00')"
            )


class DespertarCorreccionUniqueWideningRebuildTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-6A-R2 ruling 1 (ORCH-CARLOS-DELEGATES-TIER-1-1): a database at -4's own
    shape (UNIQUE(person_id, day)) rebuilds to UNIQUE(person_id, day, tipo), keeping every row --
    the existing rows are all one-per-day, so the copy is exact."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("DROP TABLE despertar_correccion")
        self.conn.execute(
            "CREATE TABLE despertar_correccion (id INTEGER PRIMARY KEY, "
            "person_id INTEGER NOT NULL REFERENCES person(id), day TEXT NOT NULL, "
            "tipo TEXT NOT NULL CHECK (tipo IN ('sueno', 'levantado')), hora_local TEXT NOT NULL, "
            "modelo_version TEXT, origen TEXT NOT NULL CHECK (origen IN ('persona', 'asistente')), "
            "creada_en TEXT NOT NULL, UNIQUE (person_id, day))"
        )
        self.conn.execute(
            "INSERT INTO despertar_correccion (person_id, day, tipo, hora_local, modelo_version, origen, "
            "creada_en) VALUES (1, '2026-11-10', 'levantado', '06:30', 'despertar-real-v1', 'asistente', "
            "'2026-11-10T00:00:00+00:00')"
        )
        self.conn.execute(
            "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES "
            "(1, '2026-11-10', '2026-11-09T23:00:00', '2026-11-10T06:00:00')"
        )
        self.conn.commit()

    def test_rebuild_widens_the_unique_and_keeps_the_row(self):
        antes = dict(self.conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = '2026-11-10'").fetchone())
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 1)
        fila = dict(self.conn.execute("SELECT * FROM despertar_correccion").fetchone())
        self.assertEqual((fila["person_id"], fila["day"], fila["tipo"], fila["hora_local"],
                           fila["modelo_version"], fila["origen"]),
                          (1, "2026-11-10", "levantado", "06:30", "despertar-real-v1", "asistente"))
        despues = dict(self.conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = '2026-11-10'").fetchone())
        self.assertEqual(antes, despues, "Garmin's own sleep_session row must stay byte-identical")
        # now accepts the OTHER tipo for the same day, the whole point of the widening.
        self.conn.execute(
            "INSERT INTO despertar_correccion (person_id, day, tipo, hora_local, modelo_version, origen, "
            "creada_en) VALUES (1, '2026-11-10', 'sueno', '06:15', 'despertar-real-v2', 'persona', "
            "'2026-11-10T00:05:00+00:00')"
        )
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 2)

    def test_rebuild_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 1)


class DespertarCorreccionFreshDbTest(unittest.TestCase):
    """A fresh database already carries despertar_correccion -- no migration needed."""

    def test_fresh_database_has_the_table(self):
        conn = _conn(self)
        db.init(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 0)


class VistaDailyMetricEfectivaMigrationTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-6B ruling 1: a database that predates `despertar_correccion` AND the
    view it later gains -- the "previous shape" this slice adds to -- migrates to both in one
    db.init() call, in the right order (the view is created last, after despertar_correccion
    exists), keeping Garmin's own rows byte-for-byte. tests/test_sueno_corregido.py's own
    MigracionVistaTest covers the narrower "just the view is missing" case."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP VIEW daily_metric_efectiva")
        self.conn.execute("DROP TABLE despertar_correccion")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES "
            "(1, '2026-11-10', '2026-11-09T22:00:00', '2026-11-10T03:41:00')"
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_metric (person_id, day, metric_key, value, source) "
            "VALUES (1, '2026-11-10', 'sleep_minutes', 322, 'garmin')"
        )
        self.conn.commit()
        self.antes_dm = dict(self.conn.execute(
            "SELECT * FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10'").fetchone())
        self.antes_ss = dict(self.conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = '2026-11-10'").fetchone())

    def test_one_init_gains_both_the_table_and_the_view(self):
        self.assertIsNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'despertar_correccion'").fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'daily_metric_efectiva'").fetchone())
        db.init(self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'despertar_correccion'").fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'daily_metric_efectiva'").fetchone())
        db.guardar_despertar_correccion(self.conn, 1, "2026-11-10", "sueno", "06:00",
                                         "despertar-real-v2", "persona")
        row = self.conn.execute(
            "SELECT value, corregido FROM daily_metric_efectiva WHERE person_id = 1 AND "
            "day = '2026-11-10' AND metric_key = 'sleep_minutes'"
        ).fetchone()
        self.assertEqual((row["value"], row["corregido"]), (322 + 139, 1))
        despues_dm = dict(self.conn.execute(
            "SELECT * FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10'").fetchone())
        despues_ss = dict(self.conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = '2026-11-10'").fetchone())
        self.assertEqual(self.antes_dm, despues_dm)
        self.assertEqual(self.antes_ss, despues_ss)


class PeriodKindColumnsMigrationTest(unittest.TestCase):
    """BODY-PLAN-FASES-4 ruling 1: a database whose period_kind predates excluye_del_rango/
    lleva_zona gains both columns, additive, keeping its three existing rows -- and the seed
    upsert (same ON CONFLICT DO UPDATE as sort_order already goes through) sets 'lesion' own
    lleva_zona=1 and adds the new 'molestia' row, byte for byte the same shape db.init() gives a
    fresh database."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE period_kind")
        self.conn.execute("CREATE TABLE period_kind (key TEXT PRIMARY KEY, sort_order INTEGER NOT NULL)")
        self.conn.executemany(
            "INSERT INTO period_kind (key, sort_order) VALUES (?, ?)",
            [("malestar", 0), ("viaje", 1), ("lesion", 2)],
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.pid = self.conn.execute(
            "INSERT INTO period (person_id, kind, start_day, created_at, updated_at) VALUES "
            "(1, 'lesion', '2026-11-01', '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00')"
        ).lastrowid
        self.conn.commit()

    def test_migration_adds_the_columns_keeping_the_old_rows(self):
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT key, sort_order, excluye_del_rango, lleva_zona FROM period_kind ORDER BY sort_order"
        ).fetchall()
        self.assertEqual(
            [(r["key"], r["sort_order"], r["excluye_del_rango"], r["lleva_zona"]) for r in rows],
            [("malestar", 0, 1, 0), ("viaje", 1, 1, 0), ("lesion", 2, 1, 1), ("molestia", 3, 0, 1)],
        )
        row = self.conn.execute("SELECT id, kind, start_day FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertEqual((row["id"], row["kind"], row["start_day"]), (self.pid, "lesion", "2026-11-01"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0], 1)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT key, excluye_del_rango, lleva_zona FROM period_kind ORDER BY sort_order"
        ).fetchall()
        self.assertEqual(
            [(r["key"], r["excluye_del_rango"], r["lleva_zona"]) for r in rows],
            [("malestar", 1, 0), ("viaje", 1, 0), ("lesion", 1, 1), ("molestia", 0, 1)],
        )

    def test_migrated_database_leaves_lesion_days_marked_but_not_molestia(self):
        db.init(self.conn)
        marcados = db.dias_marcados(self.conn, 1, "2026-10-01", "2026-11-05")
        self.assertIn("2026-11-01", marcados)
        db.actualizar_periodo(self.conn, 1, self.pid, kind="molestia")
        marcados = db.dias_marcados(self.conn, 1, "2026-10-01", "2026-11-05")
        self.assertNotIn("2026-11-01", marcados)


class PeriodKindColumnsFreshDbTest(unittest.TestCase):
    """A fresh database's period_kind already carries both columns, seeded for all four kinds --
    no migration needed."""

    def test_fresh_database_has_the_columns_and_seed(self):
        conn = _conn(self)
        db.init(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(period_kind)")}
        self.assertIn("excluye_del_rango", cols)
        self.assertIn("lleva_zona", cols)
        row = conn.execute("SELECT excluye_del_rango, lleva_zona FROM period_kind WHERE key = 'molestia'").fetchone()
        self.assertEqual((row["excluye_del_rango"], row["lleva_zona"]), (0, 1))


class EntrenoDolorNewTableMigrationTest(unittest.TestCase):
    """BODY-PLAN-FASES-5: a database built before entreno_dolor existed gains the table additively
    -- CREATE TABLE IF NOT EXISTS, no ALTER needed since nothing predates it -- and a second
    init() is a no-op that keeps the first run's own row."""

    def setUp(self):
        self.conn = _conn(self)
        db.init(self.conn)
        self.conn.execute("DROP TABLE entreno_dolor")
        self.conn.commit()

    def test_migration_creates_the_table(self):
        self.assertIsNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'entreno_dolor'").fetchone())
        db.init(self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'entreno_dolor'").fetchone())
        db.registrar_dolor_entreno(self.conn, 1, "2026-11-19", "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3, "dolor_despues": 2,
             "movimiento": None},
        ])
        self.assertEqual(len(db.dolor_de_sesion(self.conn, 1, "garmin", "s1")), 1)

    def test_migration_is_idempotent_and_keeps_existing_rows(self):
        db.init(self.conn)
        db.registrar_dolor_entreno(self.conn, 1, "2026-11-19", "garmin", "s1", [
            {"ejercicio": "press militar", "zona": "hombro", "dolor_durante": 3, "dolor_despues": 2,
             "movimiento": None},
        ])
        db.init(self.conn)
        self.assertEqual(len(db.dolor_de_sesion(self.conn, 1, "garmin", "s1")), 1)


if __name__ == "__main__":
    unittest.main()
