"""BODY-ENTRENO-VINCULO-1: a strength activity the watch syncs LATE matches the session the person
already logged by hand, instead of doubling it (BODY-ENTRENO-DUPLICADO-1-INV). Rulings:

1. Additive `activity.vinculo_manual`/`vinculo_separado` columns, migrated onto an old database.
2. `db.save_day` links a freshly-synced watch strength session to the day's one unlinked manual
   session -- never on two-plus or zero candidates, never wiping an existing link on a re-sync.
3. A linked pair reads as ONE session everywhere that counts or lists sessions -- the manual half's
   title and sets, the watch half's timing/vitals, never both halves' sets shown at once.
6. The watch-first path (`_registrar_entreno` writing onto the watch's own slots) is unchanged.
4. `separar_sesion` undoes a link, automatic or chat-raised; undo re-links.
5. `unir_sesiones` is the batch fix for a day stored before this slice, or one already split.
8. The demo person has one manual-first day already linked.

Invented demo data and synthetic fixtures only, never a real figure.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, history
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _watch_day_data(day: str, source_id: str = "w1", avg_hr: float = 120.0, duration_s: float = 1800.0,
                     name: str | None = None, start_local: str | None = None) -> garmin_adapter.DayData:
    return garmin_adapter.DayData(
        day=day,
        activities=[{"source_id": source_id, "start_local": start_local or f"{day}T18:00:00",
                     "type": "strength_training", "name": name, "duration_s": duration_s,
                     "distance_m": None, "avg_hr": avg_hr, "max_hr": avg_hr + 20, "calories": 300.0}],
    )


class MigrationTest(unittest.TestCase):
    """Ruling 1: an old `activity` table (no link columns) migrates cleanly, the existing row kept."""

    _OLD_ACTIVITY_SQL = """
        CREATE TABLE activity (
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
    """

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja_activity.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE activity")
        self.conn.execute(self._OLD_ACTIVITY_SQL)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'a1', '2026-11-09', '2026-11-09T07:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()

    def test_migration_adds_columns_and_preserves_the_row(self):
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT type, vinculo_manual, vinculo_separado FROM activity WHERE person_id = 1 AND source_id = 'a1'"
        ).fetchone()
        self.assertEqual(row["type"], "strength_training")
        self.assertIsNone(row["vinculo_manual"])
        self.assertEqual(row["vinculo_separado"], 0)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        row = self.conn.execute(
            "SELECT vinculo_separado FROM activity WHERE person_id = 1 AND source_id = 'a1'"
        ).fetchone()
        self.assertEqual(row["vinculo_separado"], 0)


class PropuestaAgenteEntrenoVinculoMigrationTest(unittest.TestCase):
    """A ninth widening of propuesta_agente.accion (separar_sesion, unir_sesiones) -- the existing
    row survives, the new actions are accepted."""

    _OLD_SQL = """
        CREATE TABLE propuesta_agente (
            id              INTEGER PRIMARY KEY,
            person_id       INTEGER NOT NULL REFERENCES person(id),
            conversacion_id INTEGER NOT NULL REFERENCES conversacion_agente(id),
            accion          TEXT NOT NULL CHECK (accion IN
                                ('proponer_meta', 'proponer_cambio_meta', 'proponer_confirmar_turnos',
                                 'proponer_idioma', 'planear_entreno', 'registrar_entreno', 'registrar_medida',
                                 'marcar_habito', 'proponer_experimento', 'marcar_experimento',
                                 'registrar_ingesta', 'corregir_ingesta', 'borrar_ingesta',
                                 'marcar_periodo', 'cerrar_periodo', 'corregir_periodo', 'borrar_periodo',
                                 'registrar_comida', 'registrar_checkin', 'descartar_series')),
            objetivo_id     INTEGER REFERENCES goal(id),
            antes_json      TEXT CHECK (antes_json IS NULL OR json_valid(antes_json)),
            despues_json    TEXT NOT NULL CHECK (json_valid(despues_json)),
            estado          TEXT NOT NULL DEFAULT 'propuesta'
                                CHECK (estado IN ('propuesta', 'aplicada', 'descartada', 'expirada', 'deshecha')),
            creada_en       TEXT NOT NULL,
            expira_en       TEXT NOT NULL,
            resuelta_en     TEXT,
            aplicada_via    TEXT CHECK (aplicada_via IS NULL OR aplicada_via IN ('mcp'))
        );
    """

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja_propuesta.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute(self._OLD_SQL)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.cid = self.conn.execute(
            "INSERT INTO conversacion_agente (person_id, creada_en) VALUES (1, '2026-11-09T00:00:00+00:00')"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'descartar_series', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (self.cid,),
        )
        self.conn.commit()

    def test_migration_preserves_the_old_row_and_accepts_separar_sesion(self):
        db.init(self.conn)
        old = self.conn.execute(
            "SELECT accion FROM propuesta_agente WHERE conversacion_id = ?", (self.cid,)
        ).fetchone()
        self.assertEqual(old["accion"], "descartar_series")
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'unir_sesiones', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (self.cid,),
        )
        self.conn.commit()


class SaveDayLinkingTest(unittest.TestCase):
    """Ruling 2: db.save_day links a freshly-synced watch strength session to the day's one
    unlinked manual session -- never on two-plus or zero candidates, never wiping an existing link
    on a re-sync, never re-linking a pair the person split."""

    def setUp(self):
        self.conn = _conn(self)

    def test_manual_then_sync_links(self):
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], manual_id)

    def test_sync_twice_still_linked_one_row_each(self):
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        db.save_day(self.conn, _watch_day_data("2026-11-10", avg_hr=130.0), 1)
        rows = self.conn.execute("SELECT * FROM activity WHERE person_id = 1 AND day = '2026-11-10'").fetchall()
        self.assertEqual(len(rows), 2)
        reloj = self.conn.execute(
            "SELECT vinculo_manual, avg_hr FROM activity WHERE person_id = 1 AND source = 'garmin' "
            "AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], manual_id)
        self.assertEqual(reloj["avg_hr"], 130.0)

    def test_two_manual_sessions_no_link(self):
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza 1")
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza 2")
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])

    def test_no_manual_session_no_link(self):
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])

    def test_split_pair_resynced_stays_split(self):
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        db.set_vinculo_manual(self.conn, 1, "garmin", "w1", None, True)
        db.save_day(self.conn, _watch_day_data("2026-11-10", avg_hr=140.0), 1)
        reloj = self.conn.execute(
            "SELECT vinculo_manual, vinculo_separado, avg_hr FROM activity WHERE person_id = 1 AND "
            "source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])
        self.assertEqual(reloj["vinculo_separado"], 1)
        self.assertEqual(reloj["avg_hr"], 140.0)

    def test_links_at_180_minutes_apart(self):
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", "2026-11-10T15:00:00", "Fuerza")
        db.save_day(self.conn, _watch_day_data("2026-11-10", start_local="2026-11-10T18:00:00"), 1)
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], manual_id)

    def test_does_not_link_at_181_minutes_apart(self):
        db.create_manual_session(self.conn, 1, "2026-11-10", "2026-11-10T14:59:00", "Fuerza")
        db.save_day(self.conn, _watch_day_data("2026-11-10", start_local="2026-11-10T18:00:00"), 1)
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])


class ManualAfterWatchLinkingTest(unittest.TestCase):
    """BODY-ENTRENO-VINCULO-2 ruling 1: the mirror direction -- a manual session logged AFTER the
    watch's own strength row already synced links through `create_manual_session`, symmetric to
    `SaveDayLinkingTest` above, including the same time window (ruling 2)."""

    def setUp(self):
        self.conn = _conn(self)

    def test_watch_then_manual_links(self):
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], manual_id)

    def test_no_start_time_on_the_manual_session_still_links_by_day(self):
        db.save_day(self.conn, _watch_day_data("2026-11-10", start_local="2026-11-10T18:00:00"), 1)
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], manual_id)

    def test_links_at_180_minutes_apart(self):
        db.save_day(self.conn, _watch_day_data("2026-11-10", start_local="2026-11-10T18:00:00"), 1)
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", "2026-11-10T15:00:00", "Fuerza")
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], manual_id)

    def test_does_not_link_at_181_minutes_apart(self):
        db.save_day(self.conn, _watch_day_data("2026-11-10", start_local="2026-11-10T18:00:00"), 1)
        db.create_manual_session(self.conn, 1, "2026-11-10", "2026-11-10T14:59:00", "Fuerza")
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source = 'garmin' AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])

    def test_two_watch_candidates_no_link(self):
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w1', '2026-11-10', '2026-11-10T07:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w2', '2026-11-10', '2026-11-10T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        rows = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND day = '2026-11-10' AND "
            "type = 'strength_training'"
        ).fetchall()
        self.assertTrue(all(r["vinculo_manual"] is None for r in rows))

    def test_two_manual_candidates_no_link(self):
        # Both manual sessions logged BEFORE any watch row exists, so neither can have claimed it
        # yet; the watch row is then inserted directly (bypassing save_day's own auto-link, like
        # UnirSesionesTest does) so a third manual session finds two already-unlinked candidates.
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza 1")
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza 2")
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w1', '2026-11-10', '2026-11-10T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza 3")
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])

    def test_already_linked_watch_row_no_link(self):
        old_manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza vieja")
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza nueva")
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], old_manual_id)

    def test_split_watch_row_no_link(self):
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza vieja")
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        db.set_vinculo_manual(self.conn, 1, "garmin", "w1", None, True)
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza nueva")
        reloj = self.conn.execute(
            "SELECT vinculo_manual, vinculo_separado FROM activity WHERE person_id = 1 AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])
        self.assertEqual(reloj["vinculo_separado"], 1)

    def test_different_persons_rows_no_link(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra persona')")
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (2, 'w1', '2026-11-10', '2026-11-10T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        reloj = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 2 AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])


class WatchFirstUnchangedTest(unittest.TestCase):
    """Ruling 6: the watch-first path (`_registrar_entreno` mapping spoken series onto the watch's
    own active slots) is untouched by this slice."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w1', '2026-11-10', '2026-11-10T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "w1", [
            garmin_adapter.ExerciseSet(set_type="active", category="pecho", exercise="press banca", reps=8.0,
                                        weight_value=50.0, weight_unit="kg", duration_s=30.0,
                                        start_local="2026-11-10T18:00:00"),
        ])

    def test_registers_onto_the_watchs_own_slot_no_manual_row_created(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
            "day": "2026-11-10", "series": [{"ejercicio": "press banca", "reps": 8, "peso_kg": 52.5}],
        }, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        rows = self.conn.execute("SELECT * FROM activity WHERE person_id = 1 AND day = '2026-11-10'").fetchall()
        self.assertEqual(len(rows), 1)
        fila = self.conn.execute(
            "SELECT person_weight_kg, corrected_by_person FROM workout_set WHERE person_id = 1 AND "
            "activity_source = 'garmin' AND activity_source_id = 'w1' AND set_order = 1"
        ).fetchone()
        self.assertEqual(fila["person_weight_kg"], 52.5)
        self.assertEqual(fila["corrected_by_person"], 1)


class MergedReadsTest(unittest.TestCase):
    """Ruling 3: a linked pair reads as ONE session everywhere that counts or lists sessions -- the
    manual half's title and sets win, the watch half's timing/vitals win, and the watch's own
    (possibly fetched) sets are never listed beside the person's."""

    def setUp(self):
        self.conn = _conn(self)
        self.manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza de la tarde")
        db.correct_exercise_set(self.conn, 1, "manual", self.manual_id, 1, exercise="sentadilla",
                                 category="pierna", reps=10, weight_kg=60.0)
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 55.0, "garmin", 1)
        db.save_day(self.conn, _watch_day_data("2026-11-10", avg_hr=133.0, duration_s=2100.0, name="Fuerza"), 1)
        # The watch's own (unrelated) fetched set -- must never be listed beside the person's own.
        db.save_exercise_sets(self.conn, 1, "garmin", "w1", [
            garmin_adapter.ExerciseSet(set_type="active", category="pierna", exercise="prensa", reps=12.0,
                                        weight_value=80.0, weight_unit="kg", duration_s=30.0,
                                        start_local="2026-11-10T18:00:00"),
        ])

    def test_load_day_merges_into_one_entry(self):
        data = db.load_day(self.conn, "2026-11-10", 1)
        self.assertEqual(len(data["activities"]), 1)
        a = data["activities"][0]
        self.assertEqual(a["name"], "Fuerza de la tarde")
        self.assertEqual(a["avg_hr"], 133.0)
        self.assertEqual(a["duration_s"], 2100.0)
        self.assertTrue(a["vinculado"])
        self.assertEqual(a["vinculo_manual_source_id"], self.manual_id)
        self.assertEqual(a["source"], "garmin")
        self.assertEqual(a["source_id"], "w1")

    def test_load_day_zonas_limitadas_from_the_persons_own_sets(self):
        db.crear_periodo(self.conn, 1, "lesion", "2026-11-01", None, zonas=["pierna"])
        data = db.load_day(self.conn, "2026-11-10", 1)
        self.assertEqual(data["activities"][0]["zonas_limitadas"], ["pierna"])

    def test_leer_dia_shows_one_session_with_the_persons_sets(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": "2026-11-10"}, "completo")
        self.assertEqual(len(out["activities"]), 1)
        self.assertEqual(out["activities"][0]["name"], "Fuerza de la tarde")

    def test_leer_entreno_shows_one_session_with_the_persons_sets_not_the_watchs(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-10"}, "completo")
        self.assertEqual(len(out["sesiones"]), 1)
        sesion = out["sesiones"][0]
        self.assertEqual(sesion["titulo"], "Fuerza de la tarde")
        ejercicios = [s["ejercicio"] for s in sesion["series"]]
        self.assertEqual(ejercicios, ["sentadilla"])
        self.assertNotIn("prensa", ejercicios)

    def test_history_training_counts_one_session(self):
        out = history.training(self.conn, 1)
        fila = next(ti for ti in out["types"] if ti["key"] == "strength_training")
        self.assertEqual(fila["sessions"], 1)

    def test_history_coverage_counts_one_session(self):
        cov = history.coverage(self.conn, 1)
        self.assertEqual(cov["activity_count"], 1)

    def test_unlinked_pair_counts_two(self):
        db.create_manual_session(self.conn, 1, "2026-11-11", None, "Otra sesión")
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w2', '2026-11-11', '2026-11-11T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        db.set_metric(self.conn, "2026-11-11", "resting_hr", 55.0, "garmin", 1)
        self.conn.commit()
        cov = history.coverage(self.conn, 1)
        self.assertEqual(cov["activity_count"], 3)  # the linked pair (1) + the unlinked pair (2)


class SepararSesionTest(unittest.TestCase):
    """Ruling 4: the person's own undo of a link, automatic or chat-raised. `source_id` resolves
    either half; apply splits, undo re-links."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza de la tarde")
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)

    def test_apply_splits_the_pair(self):
        out = herramientas.ejecutar(self.conn, 1, "separar_sesion",
                                    {"day": "2026-11-10", "source_id": "w1"}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        reloj = self.conn.execute(
            "SELECT vinculo_manual, vinculo_separado FROM activity WHERE person_id = 1 AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])
        self.assertEqual(reloj["vinculo_separado"], 1)
        data = db.load_day(self.conn, "2026-11-10", 1)
        self.assertEqual(len(data["activities"]), 2)

    def test_source_id_can_name_the_manual_half(self):
        out = herramientas.ejecutar(self.conn, 1, "separar_sesion",
                                    {"day": "2026-11-10", "source_id": self.manual_id}, "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")

    def test_undo_re_links(self):
        out = herramientas.ejecutar(self.conn, 1, "separar_sesion",
                                    {"day": "2026-11-10", "source_id": "w1"}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        reloj = self.conn.execute(
            "SELECT vinculo_manual, vinculo_separado FROM activity WHERE person_id = 1 AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], self.manual_id)
        self.assertEqual(reloj["vinculo_separado"], 0)

    def test_refuses_when_the_day_has_no_link(self):
        db.create_manual_session(self.conn, 1, "2026-11-12", None, "Suelta")
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w2', '2026-11-12', '2026-11-12T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "separar_sesion",
                                   {"day": "2026-11-12", "source_id": "w2"}, "ninguno", self.cid)

    def test_person_b_cannot_apply_person_as_proposal(self):
        out = herramientas.ejecutar(self.conn, 1, "separar_sesion",
                                    {"day": "2026-11-10", "source_id": "w1"}, "ninguno", self.cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])


class UnirSesionesTest(unittest.TestCase):
    """Ruling 5: the batch fix for a day stored before this slice, or one `separar_sesion` already
    split -- only ever when the day holds exactly one unlinked manual and one unlinked watch
    strength session."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_links_the_days_one_manual_and_one_watch_session(self):
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w1', '2026-11-10', '2026-11-10T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "unir_sesiones", {"day": "2026-11-10"}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        reloj = self.conn.execute(
            "SELECT vinculo_manual, vinculo_separado FROM activity WHERE person_id = 1 AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], manual_id)
        self.assertEqual(reloj["vinculo_separado"], 0)

    def test_undo_unlinks_without_marking_separated(self):
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w1', '2026-11-10', '2026-11-10T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "unir_sesiones", {"day": "2026-11-10"}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        reloj = self.conn.execute(
            "SELECT vinculo_manual, vinculo_separado FROM activity WHERE person_id = 1 AND source_id = 'w1'"
        ).fetchone()
        self.assertIsNone(reloj["vinculo_manual"])
        self.assertEqual(reloj["vinculo_separado"], 0)

    def test_refuses_with_two_manual_sessions(self):
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza 1")
        db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza 2")
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'w1', '2026-11-10', '2026-11-10T18:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "unir_sesiones", {"day": "2026-11-10"}, "ninguno", self.cid)

    def test_refuses_with_no_candidates(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "unir_sesiones", {"day": "2026-11-10"}, "ninguno", self.cid)

    def test_re_joins_a_pair_separar_sesion_already_split(self):
        manual_id = db.create_manual_session(self.conn, 1, "2026-11-10", None, "Fuerza")
        db.save_day(self.conn, _watch_day_data("2026-11-10"), 1)
        db.set_vinculo_manual(self.conn, 1, "garmin", "w1", None, True)
        out = herramientas.ejecutar(self.conn, 1, "unir_sesiones", {"day": "2026-11-10"}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        reloj = self.conn.execute(
            "SELECT vinculo_manual, vinculo_separado FROM activity WHERE person_id = 1 AND source_id = 'w1'"
        ).fetchone()
        self.assertEqual(reloj["vinculo_manual"], manual_id)
        self.assertEqual(reloj["vinculo_separado"], 0)


if __name__ == "__main__":
    unittest.main()
