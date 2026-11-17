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


if __name__ == "__main__":
    unittest.main()
