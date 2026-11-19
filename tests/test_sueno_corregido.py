"""BODY-DESPERTAR-REAL-6B ruling 1 (ORCH-CARLOS-DELEGATES-TIER-1-1): the one seam between Garmin's
own `daily_metric` (never edited) and the person's own correction (`despertar_correccion`) --
`daily_metric_efectiva`, a SQL VIEW dropped and recreated on every `db.init`. Every `daily_metric`
row as stored, `corregido` 0, except a 'sleep_minutes' row whose person and day have a 'sueno'
correction AND a `sleep_session` row with a non-NULL `end_local`: its `value` becomes Garmin's own
value plus the whole minutes from `end_local` to that day's date (`end_local`'s own date) at
`hora_local`, rounded, never negative. Ruling 2's own census: every reader of a daily_metric FIGURE
moves to this view; a NAMED allow-list (below) is every remaining bare-table read, each with its own
one-line reason. Invented demo data only, no real health figures, no network.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import baselines, db, garmin_adapter
from body.lector import Lector

DIA = "2026-11-10"


def _conn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


def _sesion(conn, person_id: int = 1, day: str = DIA, start_local: str = f"{DIA}T22:00:00",
            end_local: str = f"{DIA}T03:41:00") -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, start_local, end_local),
    )
    conn.commit()


def _metrica(conn, person_id: int, day: str, key: str, value: float, source: str = "garmin") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daily_metric (person_id, day, metric_key, value, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (person_id, day, key, value, source),
    )
    conn.commit()


def _vista(conn, person_id: int, day: str, metric_key: str = "sleep_minutes"):
    return conn.execute(
        "SELECT value, source, corregido FROM daily_metric_efectiva "
        "WHERE person_id = ? AND day = ? AND metric_key = ?",
        (person_id, day, metric_key),
    ).fetchone()


class ViewBehaviourTest(unittest.TestCase):
    """The view's own CASE, over every combination ruling 1 names."""

    def test_no_correction_at_all_is_unchanged_with_corregido_0(self):
        conn = _conn(self)
        _sesion(conn)
        _metrica(conn, 1, DIA, "sleep_minutes", 322)
        row = _vista(conn, 1, DIA)
        self.assertEqual((row["value"], row["corregido"]), (322, 0))

    def test_sueno_correction_adds_the_whole_minutes_to_garmins_end(self):
        """Garmin's own end 03:41, correction 06:00 -> +139 minutes, corregido 1."""
        conn = _conn(self)
        _sesion(conn, end_local=f"{DIA}T03:41:00")
        _metrica(conn, 1, DIA, "sleep_minutes", 322)
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:00", "despertar-real-v2", "persona")
        row = _vista(conn, 1, DIA)
        self.assertEqual(row["value"], 322 + 139)
        self.assertEqual(row["corregido"], 1)
        self.assertEqual(row["source"], "garmin")  # the row's own source column is untouched

    def test_levantado_only_correction_changes_nothing(self):
        conn = _conn(self)
        _sesion(conn, end_local=f"{DIA}T03:41:00")
        _metrica(conn, 1, DIA, "sleep_minutes", 322)
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "07:15", "despertar-real-v2", "persona")
        row = _vista(conn, 1, DIA)
        self.assertEqual((row["value"], row["corregido"]), (322, 0))

    def test_sueno_correction_with_no_sleep_session_end_local_changes_nothing(self):
        """A correction can never be CREATED for a day with no Garmin sleep (the tool's own
        refusal at creation), but the view's own guard is checked here directly, the same way
        tests/test_despertar_correccion.py's own stray-row test checks leer_dia's guard."""
        conn = _conn(self)
        _metrica(conn, 1, DIA, "sleep_minutes", 322)
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:00", "despertar-real-v2", "persona")
        row = _vista(conn, 1, DIA)
        self.assertEqual((row["value"], row["corregido"]), (322, 0))

    def test_a_correction_at_or_before_garmins_end_adds_zero_never_negative(self):
        conn = _conn(self)
        _sesion(conn, end_local=f"{DIA}T06:00:00")
        _metrica(conn, 1, DIA, "sleep_minutes", 400)
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "05:30", "despertar-real-v2", "persona")
        row = _vista(conn, 1, DIA)
        self.assertEqual(row["value"], 400)
        self.assertEqual(row["corregido"], 1)  # the row still counts as corrected -- it just adds 0

    def test_a_different_metric_key_on_the_same_day_is_never_touched(self):
        conn = _conn(self)
        _sesion(conn, end_local=f"{DIA}T03:41:00")
        _metrica(conn, 1, DIA, "resting_hr", 55)
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:00", "despertar-real-v2", "persona")
        row = _vista(conn, 1, DIA, metric_key="resting_hr")
        self.assertEqual((row["value"], row["corregido"]), (55, 0))

    def test_garmins_raw_rows_are_byte_identical_before_and_after(self):
        conn = _conn(self)
        _sesion(conn, end_local=f"{DIA}T03:41:00")
        _metrica(conn, 1, DIA, "sleep_minutes", 322)
        antes_dm = dict(conn.execute(
            "SELECT * FROM daily_metric WHERE person_id = 1 AND day = ?", (DIA,)).fetchone())
        antes_ss = dict(conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = ?", (DIA,)).fetchone())
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:00", "despertar-real-v2", "persona")
        _vista(conn, 1, DIA)  # reading the view touches nothing underneath it
        despues_dm = dict(conn.execute(
            "SELECT * FROM daily_metric WHERE person_id = 1 AND day = ?", (DIA,)).fetchone())
        despues_ss = dict(conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = ?", (DIA,)).fetchone())
        self.assertEqual(antes_dm, despues_dm)
        self.assertEqual(antes_ss, despues_ss)

    def test_resync_of_the_day_keeps_the_corrected_reading(self):
        """save_day's own INSERT OR REPLACE on daily_metric (BODY-DESPERTAR-REAL-6-INV) never
        erases a correction row living in a separate table -- the view picks the resynced raw
        value right back up and keeps adding the same correction to it."""
        conn = _conn(self)
        _sesion(conn, end_local=f"{DIA}T03:41:00")
        _metrica(conn, 1, DIA, "sleep_minutes", 322)
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:00", "despertar-real-v2", "persona")
        datos = garmin_adapter.DayData(day=DIA, metrics={"sleep_minutes": 330},
                                        sleep_start=f"{DIA[:8]}09T22:00:00", sleep_end=f"{DIA}T03:41:00")
        db.save_day(conn, datos, 1)
        row = _vista(conn, 1, DIA)
        self.assertEqual(row["value"], 330 + 139)
        self.assertEqual(row["corregido"], 1)


class MigracionVistaTest(unittest.TestCase):
    """tests/test_migraciones.py's own sibling: a database at the previous shape (no view at all)
    gains it on the next db.init(), additive, no row written by the migration itself."""

    def test_a_database_missing_the_view_gains_it_on_init(self):
        conn = _conn(self)  # already carries the view, from the FIRST db.init() above
        conn.execute("DROP VIEW daily_metric_efectiva")
        conn.commit()
        self.assertIsNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view' AND name = 'daily_metric_efectiva'"
        ).fetchone())
        db.init(conn)
        self.assertIsNotNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view' AND name = 'daily_metric_efectiva'"
        ).fetchone())
        _sesion(conn, end_local=f"{DIA}T03:41:00")
        _metrica(conn, 1, DIA, "sleep_minutes", 322)
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:00", "despertar-real-v2", "persona")
        row = _vista(conn, 1, DIA)
        self.assertEqual(row["value"], 322 + 139)

    def test_init_is_idempotent_on_an_already_migrated_database(self):
        conn = _conn(self)
        db.init(conn)  # a second call, as every route's ensure_ready does on a warm process
        self.assertIsNotNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view' AND name = 'daily_metric_efectiva'"
        ).fetchone())


class ReadersMoveTest(unittest.TestCase):
    """Ruling 2: db.series, db.day_values, Lector and baselines all read the corrected value."""

    def setUp(self):
        self.conn = _conn(self)
        _sesion(self.conn, end_local=f"{DIA}T03:41:00")
        _metrica(self.conn, 1, DIA, "sleep_minutes", 322)
        db.guardar_despertar_correccion(self.conn, 1, DIA, "sueno", "06:00", "despertar-real-v2", "persona")

    def test_db_series_reads_the_corrected_value(self):
        self.assertEqual(db.series(self.conn, "sleep_minutes", DIA, DIA, 1), {DIA: 322 + 139})

    def test_db_series_garmin_reads_garmins_own_value(self):
        self.assertEqual(db.series_garmin(self.conn, "sleep_minutes", DIA, DIA, 1), {DIA: 322})

    def test_db_day_values_reads_the_corrected_value(self):
        self.assertEqual(db.day_values(self.conn, DIA, 1)["sleep_minutes"], 322 + 139)

    def test_lector_reads_the_corrected_value(self):
        lector = Lector(self.conn, 1, DIA, DIA, ["sleep_minutes"])
        self.assertEqual(lector.day_values(DIA)["sleep_minutes"], 322 + 139)
        self.assertEqual(lector.series("sleep_minutes", DIA, DIA), {DIA: 322 + 139})

    def test_baselines_batched_reads_the_corrected_value(self):
        """body/baselines.py:61's own batched SELECT, over a 7-day window ending on a corrected
        night, carries the corrected figure into the mean -- not Garmin's own raw one."""
        for i in range(1, 7):
            _metrica(self.conn, 1, f"2026-11-{i:02d}", "sleep_minutes", 300)
        b = baselines.baseline(self.conn, "sleep_minutes", "2026-11-11", person_id=1)
        self.assertIsNotNone(b)
        self.assertAlmostEqual(b.mean, (300 * 6 + (322 + 139)) / 7)


# ---- ruling 2's own allow-list: every bare-table "daily_metric" (not "daily_metric_efectiva")
# occurrence left in body/, each with its own one-line reason ------------------------------------

ALLOW_LIST = {
    # The writer and its own write-adjacent bookkeeping -- these must round-trip with exactly what
    # they wrote, never the view's overlay, or an undo could bake a correction into Garmin's own
    # row (source='self') the next time it fires.
    ("body/db.py", "save_day's own INSERT OR REPLACE -- the one writer"),
    ("body/db.py", "set_metric's own INSERT OR REPLACE / DELETE -- the one writer"),
    ("body/db.py", "series_garmin -- a caller that names it wants Garmin's own record, uncorrected"),
    ("body/db.py", "days_with_data -- a Garmin-day existence check, reads no value"),
    ("body/agente/herramientas.py", "_registrar_medida's own antes-snapshot for registrar_medida's "
                                     "undo, which calls db.set_metric directly on the raw table"),
    ("body/agente/propuestas.py", "registrar_medida's own drift-on-apply/drift-on-undo, comparing "
                                   "against exactly what db.set_metric wrote or will restore"),
    # Garmin-day existence/count checks that read no value (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2).
    ("body/garmin_cuenta.py", "existence check, reads no value"),
    ("body/persona_garmin.py", "count/scrub specs, read no value"),
    ("body/tutoriales.py", "existence check, reads no value"),
    ("body/practicas.py", "_calcular_actividad/_primero_ultimo_dato/resumen's own 'has data' day "
                           "existence and MIN/MAX(day) range reads -- no value column"),
    ("body/sueno_fc.py", "recalcular's own idempotency check (NOT EXISTS, a different metric_key, "
                          "sleep_hr_avg) -- reads no value"),
    ("body/rutas/tendencias.py", "_trend_days_have's own COUNT(*) per metric_key -- a count, never "
                                  "a figure (the worker's own choice, ruling 2)"),
    # The record itself: export, deletes, and the migration tool's own row-for-row copy.
    ("body/privacidad.py", "export (the record as stored), deletes, and person_tables' own "
                            "type='table' schema scan (a view is never a table)"),
    ("body/mudanza.py", "row-for-row migration between databases -- the raw record, named in its "
                         "own TableSpec list, never the sqlite_master wildcard"),
    ("body/backup.py", "type='table' only -- a view is never backed up or counted as one"),
    # vida-v1, until BODY-DESPERTAR-REAL-6C labels the corrected figure.
    ("body/vida_dia.py", "keeps Garmin's own figure through db.series_garmin, named, until "
                          "BODY-DESPERTAR-REAL-6C"),
    ("body/vida_export.py", "keeps Garmin's own figure -- BODY-DESPERTAR-REAL-6C, not this slice"),
}


class AllowListCensusTest(unittest.TestCase):
    """A new reader of the bare `daily_metric` table (not the `daily_metric_efectiva` view) that
    is not in ALLOW_LIST above fails this test -- the same shape as tests/test_agente_leer_serie.py's
    own substring census, widened to every file in body/."""

    def test_every_bare_table_occurrence_is_named_in_the_allow_list(self):
        import re
        root = Path(__file__).resolve().parent.parent / "body"
        allowed_files = {f for f, _ in ALLOW_LIST}
        offenders = []
        pattern = re.compile(r"(FROM|JOIN)\s+daily_metric\b(?!_efectiva)")
        for path in sorted(root.rglob("*.py")):
            rel = "body/" + str(path.relative_to(root)).replace("\\", "/")
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    if rel not in allowed_files:
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "a bare daily_metric read outside the allow-list:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
