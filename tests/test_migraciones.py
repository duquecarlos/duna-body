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


if __name__ == "__main__":
    unittest.main()
