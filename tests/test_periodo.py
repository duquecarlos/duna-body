"""BODY-PERIODO-1: the `period`/`period_kind` schema and db.py's own read/write functions --
periodos, dias_marcados, crear_periodo, actualizar_periodo, borrar_periodo. Invented demo data
only, no real health figures.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import conversacion as agente_conversacion

DAY = "2026-11-09"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


# ---- schema -----------------------------------------------------------------------------------

class FreshDbTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_period_kind_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(period_kind)")}
        self.assertEqual(cols, {"key", "sort_order"})

    def test_period_kind_is_seeded_with_the_three_kinds(self):
        rows = self.conn.execute("SELECT key, sort_order FROM period_kind ORDER BY sort_order").fetchall()
        self.assertEqual([(r["key"], r["sort_order"]) for r in rows],
                          [("malestar", 0), ("viaje", 1), ("lesion", 2)])

    def test_period_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(period)")}
        expected = {"id", "person_id", "kind", "start_day", "end_day", "note", "created_at", "updated_at"}
        self.assertEqual(cols, expected)

    def test_end_day_must_not_be_before_start_day(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO period (person_id, kind, start_day, end_day, created_at, updated_at) "
                "VALUES (1, 'malestar', '2026-11-09', '2026-11-08', '2026-11-09T00:00:00+00:00', "
                "'2026-11-09T00:00:00+00:00')"
            )

    def test_unknown_kind_is_refused_by_the_fk(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO period (person_id, kind, start_day, created_at, updated_at) "
                "VALUES (1, 'gripe', '2026-11-09', '2026-11-09T00:00:00+00:00', '2026-11-09T00:00:00+00:00')"
            )

    def test_propuesta_agente_accion_accepts_the_four_periodo_actions(self):
        cid = agente_conversacion.crear(self.conn, 1)
        for accion in ("marcar_periodo", "cerrar_periodo", "corregir_periodo", "borrar_periodo"):
            self.conn.execute(
                "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
                "creada_en, expira_en) VALUES (1, ?, ?, '{}', '2026-11-09T00:00:00+00:00', "
                "'2026-11-09T00:10:00+00:00')",
                (cid, accion),
            )


class MigrationOnAnExistingDbTest(unittest.TestCase):
    """A database built from the current SCHEMA with period/period_kind dropped (the same technique
    tests/test_experimentos_esquema.py uses) -- purely additive, so dropping is enough to simulate
    main's current schema."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE period")
        self.conn.execute("DROP TABLE period_kind")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        self.conn.commit()

    def test_migration_creates_and_seeds_the_tables(self):
        db.init(self.conn)
        rows = self.conn.execute("SELECT key FROM period_kind ORDER BY sort_order").fetchall()
        self.assertEqual([r["key"] for r in rows], ["malestar", "viaje", "lesion"])

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        pid = db.crear_periodo(self.conn, 1, "viaje", "2026-11-01", "2026-11-03")
        db.init(self.conn)
        row = self.conn.execute("SELECT kind FROM period WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["kind"], "viaje")


# ---- crear_periodo / actualizar_periodo / borrar_periodo --------------------------------------

class CrearPeriodoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_creates_an_open_period(self):
        pid = db.crear_periodo(self.conn, 1, "malestar", DAY)
        row = self.conn.execute("SELECT kind, start_day, end_day FROM period WHERE id = ?", (pid,)).fetchone()
        self.assertEqual((row["kind"], row["start_day"], row["end_day"]), ("malestar", DAY, None))

    def test_creates_a_closed_period_with_a_note(self):
        pid = db.crear_periodo(self.conn, 1, "viaje", "2026-11-01", "2026-11-05", "a Medellín")
        row = self.conn.execute("SELECT end_day, note FROM period WHERE id = ?", (pid,)).fetchone()
        self.assertEqual((row["end_day"], row["note"]), ("2026-11-05", "a Medellín"))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            db.crear_periodo(self.conn, 1, "gripe", DAY)


class ActualizarPeriodoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.pid = db.crear_periodo(self.conn, 1, "malestar", DAY, note="algo")

    def test_closing_sets_end_day(self):
        db.actualizar_periodo(self.conn, 1, self.pid, end_day="2026-11-11")
        row = self.conn.execute("SELECT end_day FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertEqual(row["end_day"], "2026-11-11")

    def test_reopening_sets_end_day_back_to_null(self):
        db.actualizar_periodo(self.conn, 1, self.pid, end_day="2026-11-11")
        db.actualizar_periodo(self.conn, 1, self.pid, end_day=None)
        row = self.conn.execute("SELECT end_day FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertIsNone(row["end_day"])

    def test_clearing_the_note(self):
        db.actualizar_periodo(self.conn, 1, self.pid, note=None)
        row = self.conn.execute("SELECT note FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertIsNone(row["note"])

    def test_changing_kind_and_start_day(self):
        db.actualizar_periodo(self.conn, 1, self.pid, kind="viaje", start_day="2026-11-01")
        row = self.conn.execute("SELECT kind, start_day FROM period WHERE id = ?", (self.pid,)).fetchone()
        self.assertEqual((row["kind"], row["start_day"]), ("viaje", "2026-11-01"))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            db.actualizar_periodo(self.conn, 1, self.pid, kind="gripe")

    def test_no_fields_given_is_a_no_op(self):
        before = self.conn.execute("SELECT updated_at FROM period WHERE id = ?", (self.pid,)).fetchone()["updated_at"]
        db.actualizar_periodo(self.conn, 1, self.pid)
        after = self.conn.execute("SELECT updated_at FROM period WHERE id = ?", (self.pid,)).fetchone()["updated_at"]
        self.assertEqual(before, after)

    def test_another_persons_period_is_refused(self):
        with self.assertRaises(ValueError):
            db.actualizar_periodo(self.conn, 2, self.pid, end_day="2026-11-11")


class BorrarPeriodoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.pid = db.crear_periodo(self.conn, 1, "lesion", DAY)

    def test_deletes_the_row(self):
        db.borrar_periodo(self.conn, 1, self.pid)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM period WHERE id = ?", (self.pid,)).fetchone())

    def test_another_persons_period_is_refused(self):
        with self.assertRaises(ValueError):
            db.borrar_periodo(self.conn, 2, self.pid)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM period WHERE id = ?", (self.pid,)).fetchone())

    def test_unknown_period_is_refused(self):
        with self.assertRaises(ValueError):
            db.borrar_periodo(self.conn, 1, 999999)


# ---- periodos / dias_marcados -------------------------------------------------------------------

class PeriodosReadTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_periodos_only_lists_the_owners_rows_overlapping_the_range(self):
        db.crear_periodo(self.conn, 1, "malestar", "2026-11-01", "2026-11-03")
        db.crear_periodo(self.conn, 1, "viaje", "2026-12-01", "2026-12-03")
        db.crear_periodo(self.conn, 2, "lesion", "2026-11-01", "2026-11-03")
        rows = db.periodos(self.conn, 1, "2026-11-01", "2026-11-30")
        self.assertEqual([r["kind"] for r in rows], ["malestar"])

    def test_dias_marcados_covers_a_closed_range(self):
        db.crear_periodo(self.conn, 1, "malestar", "2026-11-05", "2026-11-07")
        marcados = db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-10")
        self.assertEqual(marcados, {"2026-11-05", "2026-11-06", "2026-11-07"})

    def test_dias_marcados_clips_to_the_queried_range(self):
        db.crear_periodo(self.conn, 1, "viaje", "2026-11-05", "2026-11-20")
        marcados = db.dias_marcados(self.conn, 1, "2026-11-08", "2026-11-10")
        self.assertEqual(marcados, {"2026-11-08", "2026-11-09", "2026-11-10"})

    def test_dias_marcados_only_counts_the_owners_own_periods(self):
        db.crear_periodo(self.conn, 2, "lesion", "2026-11-05", "2026-11-07")
        marcados = db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-10")
        self.assertEqual(marcados, set())

    def test_open_period_counts_through_today_not_past_it(self):
        self.conn.execute("UPDATE person SET timezone = 'America/Bogota' WHERE id = 1")
        self.conn.commit()
        import datetime as real_datetime
        instante_fijo = real_datetime.datetime(2026, 11, 9, 23, 0, tzinfo=ZoneInfo("UTC"))
        with mock.patch("body.db.datetime") as fake_dt:
            fake_dt.now.side_effect = lambda tz=None: instante_fijo.astimezone(tz) if tz is not None else instante_fijo
            db.crear_periodo(self.conn, 1, "malestar", "2026-11-05")
            marcados = db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-15")
        self.assertIn("2026-11-09", marcados)
        self.assertNotIn("2026-11-10", marcados)

    def test_open_period_across_midnight_in_a_non_utc_zone(self):
        """23:00 UTC on 2026-11-09 is already 2026-11-10 in a zone ahead of UTC (e.g. Auckland,
        UTC+13) -- the open period must count through that LOCAL today, not UTC's."""
        self.conn.execute("UPDATE person SET timezone = 'Pacific/Auckland' WHERE id = 1")
        self.conn.commit()
        import datetime as real_datetime
        instante_fijo = real_datetime.datetime(2026, 11, 9, 23, 0, tzinfo=ZoneInfo("UTC"))
        with mock.patch("body.db.datetime") as fake_dt:
            fake_dt.now.side_effect = lambda tz=None: instante_fijo.astimezone(tz) if tz is not None else instante_fijo
            db.crear_periodo(self.conn, 1, "malestar", "2026-11-05")
            marcados = db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-15")
        self.assertIn("2026-11-10", marcados)
        self.assertNotIn("2026-11-11", marcados)


# ---- BODY-PERIODO-PANTALLAS-1-B: the zone is read once per connection, not once per call --------

class DiasMarcadosNoPerCallReadTest(unittest.TestCase):
    """BODY-PERIODO-1-INV measured dias_marcados re-reading person() for the timezone on EVERY
    call while an open period overlapped the range -- a real per-call SELECT, not just a function
    call. Fixed by folding the zone into the SAME cached statement as the period rows
    (db.py _periodos_de_persona), so it costs at most one read per connection."""

    def setUp(self):
        self.conn = _conn(self)

    def test_n_calls_on_one_connection_issue_no_more_than_one_statement(self):
        db.crear_periodo(self.conn, 1, "malestar", "2026-11-05")
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            for _ in range(5):
                db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-20")
        finally:
            self.conn.set_trace_callback(None)
        self.assertLessEqual(len(statements), 1,
                             f"warming the cache should cost at most one statement; got {statements}")

    def test_midnight_crossing_still_moves_today_even_once_the_zone_is_cached(self):
        """The cache holds the ZONE, never the DATE -- two calls on the same connection, straddling
        local midnight, must disagree about whether the new day is marked."""
        self.conn.execute("UPDATE person SET timezone = 'America/Bogota' WHERE id = 1")  # UTC-5
        self.conn.commit()
        db.crear_periodo(self.conn, 1, "malestar", "2026-11-05")
        import datetime as real_datetime
        antes = real_datetime.datetime(2026, 11, 9, 23, 0, tzinfo=ZoneInfo("UTC"))  # 18:00 Bogota, still the 9th
        despues = real_datetime.datetime(2026, 11, 10, 6, 0, tzinfo=ZoneInfo("UTC"))  # 01:00 Bogota, now the 10th
        with mock.patch("body.db.datetime") as fake_dt:
            fake_dt.now.side_effect = lambda tz=None: antes.astimezone(tz) if tz is not None else antes
            marcados_antes = db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-15")
        with mock.patch("body.db.datetime") as fake_dt:
            fake_dt.now.side_effect = lambda tz=None: despues.astimezone(tz) if tz is not None else despues
            marcados_despues = db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-15")
        self.assertNotIn("2026-11-10", marcados_antes)
        self.assertIn("2026-11-10", marcados_despues)

    def test_a_timezone_change_is_seen_once_the_cache_is_invalidated(self):
        """No product write path changes a person's timezone today, but the mechanism that would
        have to see it already exists: any period write invalidates the SAME cache entry that now
        holds the zone (not a separate one), so the next read after one picks up the current
        timezone rather than the one in effect when the cache was first warmed."""
        self.conn.execute("UPDATE person SET timezone = 'America/Bogota' WHERE id = 1")  # UTC-5
        self.conn.commit()
        pid = db.crear_periodo(self.conn, 1, "malestar", "2026-11-05")
        import datetime as real_datetime
        instante = real_datetime.datetime(2026, 11, 9, 23, 0, tzinfo=ZoneInfo("UTC"))
        with mock.patch("body.db.datetime") as fake_dt:
            fake_dt.now.side_effect = lambda tz=None: instante.astimezone(tz) if tz is not None else instante
            antes = db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-15")
        self.assertNotIn("2026-11-10", antes, "still the 9th in Bogota (UTC-5) at 23:00 UTC")

        self.conn.execute("UPDATE person SET timezone = 'Pacific/Auckland' WHERE id = 1")  # UTC+13
        self.conn.commit()
        db.actualizar_periodo(self.conn, 1, pid, note="revisado")  # any period write invalidates the cache

        with mock.patch("body.db.datetime") as fake_dt:
            fake_dt.now.side_effect = lambda tz=None: instante.astimezone(tz) if tz is not None else instante
            despues = db.dias_marcados(self.conn, 1, "2026-11-01", "2026-11-15")
        self.assertIn("2026-11-10", despues, "already the 10th in Auckland (UTC+13) at 23:00 UTC")


if __name__ == "__main__":
    unittest.main()
