"""BODY-DESPERTAR-REAL-4 ruling 1, widened by BODY-DESPERTAR-REAL-6A-R2 ruling 1
(ORCH-CARLOS-DELEGATES-TIER-1-1): the person's own correction to a night, read and written through
db.despertar_correcciones/db.despertar_correcciones_rango/db.guardar_despertar_correccion/
db.borrar_despertar_correccion, beside Garmin's own sleep_session row -- never a sibling column on
it, never a second source of truth for the sleep window itself. A morning can hold BOTH a 'sueno'
and a 'levantado' row (UNIQUE(person_id, day, tipo)). Invented demo data only, no real health
figures, no network.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import config, db, garmin_adapter
from body.agente import herramientas


def _conn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


DIA = "2026-10-05"


def _sesion(conn, person_id: int = 1, day: str = DIA, start_local: str = "2026-10-04T23:00:00",
            end_local: str = f"{DIA}T06:00:00") -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, start_local, end_local),
    )
    conn.commit()


class RoundTripTest(unittest.TestCase):
    """Save, read, replace, delete -- one row per (person_id, day, tipo)."""

    def test_no_correction_reads_empty(self):
        conn = _conn(self)
        self.assertEqual(db.despertar_correcciones(conn, 1, DIA), {})

    def test_save_then_read_round_trips(self):
        conn = _conn(self)
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        filas = db.despertar_correcciones(conn, 1, DIA)
        self.assertEqual(set(filas), {"levantado"})
        fila = filas["levantado"]
        self.assertEqual(fila["hora_local"], "06:45")
        self.assertEqual(fila["modelo_version"], "despertar-real-v2")
        self.assertEqual(fila["origen"], "asistente")
        self.assertTrue(fila["creada_en"])

    def test_saving_again_the_same_tipo_replaces_not_duplicates(self):
        conn = _conn(self)
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "07:00", "despertar-real-v2", "persona")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 1)
        fila = db.despertar_correcciones(conn, 1, DIA)["levantado"]
        self.assertEqual((fila["hora_local"], fila["origen"]), ("07:00", "persona"))

    def test_a_morning_holds_one_sueno_and_one_levantado_row(self):
        """BODY-DESPERTAR-REAL-6A-R2 ruling 1: saving the OTHER tipo never replaces this one --
        both survive, each read independently."""
        conn = _conn(self)
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:20", "despertar-real-v2", "persona")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 2)
        filas = db.despertar_correcciones(conn, 1, DIA)
        self.assertEqual(set(filas), {"sueno", "levantado"})
        self.assertEqual(filas["sueno"]["hora_local"], "06:20")
        self.assertEqual(filas["levantado"]["hora_local"], "06:45")

    def test_different_days_each_get_their_own_row(self):
        conn = _conn(self)
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        db.guardar_despertar_correccion(conn, 1, "2026-10-06", "sueno", "06:10", "despertar-real-v2", "asistente")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM despertar_correccion").fetchone()[0], 2)

    def test_delete_removes_only_its_own_tipo(self):
        conn = _conn(self)
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:20", "despertar-real-v2", "persona")
        db.borrar_despertar_correccion(conn, 1, DIA, "levantado")
        filas = db.despertar_correcciones(conn, 1, DIA)
        self.assertEqual(set(filas), {"sueno"})

    def test_delete_on_an_absent_row_is_a_quiet_no_op(self):
        conn = _conn(self)
        db.borrar_despertar_correccion(conn, 1, DIA, "levantado")  # no row -- must not raise
        self.assertEqual(db.despertar_correcciones(conn, 1, DIA), {})


class RangoTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-6A-R2 ruling 1: the range read, one statement for several days, both
    kinds per day -- the seam -6B needs."""

    def test_rango_devuelve_ambos_tipos_por_dia_en_una_sola_sentencia(self):
        conn = _conn(self)
        db.guardar_despertar_correccion(conn, 1, "2026-10-05", "sueno", "06:20", "despertar-real-v2", "persona")
        db.guardar_despertar_correccion(conn, 1, "2026-10-05", "levantado", "06:45", "despertar-real-v2", "persona")
        db.guardar_despertar_correccion(conn, 1, "2026-10-06", "levantado", "07:00", "despertar-real-v2", "persona")
        contador = {"n": 0}
        conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + 1))
        try:
            out = db.despertar_correcciones_rango(conn, 1, "2026-10-05", "2026-10-06")
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(contador["n"], 1, "the range read must cost exactly one statement")
        self.assertEqual(set(out), {"2026-10-05", "2026-10-06"})
        self.assertEqual(set(out["2026-10-05"]), {"sueno", "levantado"})
        self.assertEqual(out["2026-10-05"]["sueno"]["hora_local"], "06:20")
        self.assertEqual(set(out["2026-10-06"]), {"levantado"})

    def test_rango_vacio_sin_correcciones(self):
        conn = _conn(self)
        self.assertEqual(db.despertar_correcciones_rango(conn, 1, "2026-10-01", "2026-10-31"), {})


class SleepSessionUntouchedTest(unittest.TestCase):
    """Saving a correction never changes Garmin's own sleep_session row -- byte-identical before
    and after, since the two live in separate tables (never a sibling column)."""

    def test_sleep_session_row_is_byte_identical_after_a_correction_is_saved(self):
        conn = _conn(self)
        _sesion(conn)
        antes = dict(conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = ?", (DIA,)).fetchone())
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        despues = dict(conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = ?", (DIA,)).fetchone())
        self.assertEqual(antes, despues)

    def test_sleep_session_row_is_byte_identical_with_both_kinds_saved(self):
        conn = _conn(self)
        _sesion(conn)
        antes = dict(conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = ?", (DIA,)).fetchone())
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:20", "despertar-real-v2", "persona")
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        despues = dict(conn.execute(
            "SELECT * FROM sleep_session WHERE person_id = 1 AND day = ?", (DIA,)).fetchone())
        self.assertEqual(antes, despues)

    def test_a_resync_of_the_same_day_leaves_the_correction_in_place(self):
        conn = _conn(self)
        _sesion(conn)
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        # save_day's own INSERT OR REPLACE on sleep_session (4-column list) is exactly the re-sync
        # that motivated the separate table in the first place.
        datos = garmin_adapter.DayData(day=DIA, sleep_start="2026-10-04T23:00:00", sleep_end=f"{DIA}T06:00:00")
        db.save_day(conn, datos, 1)
        fila = db.despertar_correcciones(conn, 1, DIA)["levantado"]
        self.assertEqual(fila["hora_local"], "06:45")


class LeerDiaMuestraCorreccionTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-6A-R2 ruling 5: leer_dia includes BOTH of the day's own corrections
    next to Garmin's own sleep, each labelled by kind."""

    def test_leer_dia_shows_the_correction_next_to_garmins_sleep(self):
        conn = _conn(self)
        _sesion(conn)
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DIA}, "completo")
        self.assertEqual(out["sleep"]["end_local"], f"{DIA}T06:00:00")  # Garmin's own, untouched
        self.assertEqual(set(out["sleep"]["correcciones"]), {"levantado"})
        self.assertEqual(out["sleep"]["correcciones"]["levantado"]["hora_local"], "06:45")

    def test_leer_dia_shows_both_kinds_when_both_exist(self):
        conn = _conn(self)
        _sesion(conn)
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:20", "despertar-real-v2", "persona")
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "06:45", "despertar-real-v2", "asistente")
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DIA}, "completo")
        self.assertEqual(set(out["sleep"]["correcciones"]), {"sueno", "levantado"})

    def test_no_correction_leaves_the_key_absent(self):
        conn = _conn(self)
        _sesion(conn)
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DIA}, "completo")
        self.assertIsNotNone(out["sleep"])
        self.assertNotIn("correcciones", out["sleep"])

    def test_no_sleep_session_leaves_sleep_none_even_with_a_stray_correction_row(self):
        conn = _conn(self)
        # A correction can never be created for a day with no Garmin sleep (ruling 2's own refusal
        # at creation), but leer_dia's own guard is checked here directly regardless of how a row
        # might have gotten there.
        conn.execute(
            "INSERT INTO despertar_correccion (person_id, day, tipo, hora_local, origen, creada_en) "
            "VALUES (1, ?, 'levantado', '06:45', 'asistente', '2026-10-05T00:00:00+00:00')", (DIA,))
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DIA}, "completo")
        self.assertIsNone(out["sleep"])


if __name__ == "__main__":
    unittest.main()
