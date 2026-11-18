"""BODY-DESPERTAR-REAL-2 ruling 1: leer_intradia, a bounded window (up to 4 hours) of one day's
heart-rate/stress points plus the sleep minute arrays for the part of that window inside Garmin's
own sleep session. Invented demo data only, no real health figures, no network -- every date here
is either long in the past or computed from a dynamic reference, never a Garmin call.
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, pull
from body.agente import herramientas

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_momentos_pull.py convention)


def _conn(case: unittest.TestCase):
    c = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _fc_payload(muestras: list[tuple[str, float]]) -> dict:
    return {"heartRateValues": [[_ms(ts), v] for ts, v in muestras]}


def _estres_payload(muestras: list[tuple[str, float]]) -> dict:
    return {"stressValuesArray": [[_ms(ts), v] for ts, v in muestras]}


def _guardar_fc(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_fc", _fc_payload(muestras))


def _guardar_estres(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_estres", _estres_payload(muestras))


def _guardar_sueno(conn, person_id: int, day: str, hr: list[tuple[str, float]] = (),
                    movement: list[tuple[str, float]] = ()) -> None:
    payload = {
        "sleepHeartRate": [{"startGMT": _ms(ts), "value": v} for ts, v in hr],
        "sleepMovement": [{"startGMT": _ms(ts), "activityLevel": v} for ts, v in movement],
    }
    conn.execute(
        "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, ?, 'sleep', ?, ?, ?)",
        (person_id, day, garmin_adapter.SOURCE, "2026-01-01T00:00:00+00:00", json.dumps(payload)),
    )


def _sesion(conn, person_id: int, day: str, start_local: str, end_local: str) -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, start_local, end_local),
    )


# A day far enough in the past that it is never today/yesterday for real -- ruling 2's own
# on-demand refresh never fires here, so these tests need no Garmin mock at all.
DIA = "2026-10-05"


def _ejecutar(conn, person_id: int, nivel: str = "completo", **kwargs):
    params = {"fecha": DIA, "desde": "06:00", "hasta": "08:00"}
    params.update(kwargs)
    return herramientas.ejecutar(conn, person_id, "leer_intradia", params, nivel)


class WindowShapeTest(unittest.TestCase):
    def test_only_points_inside_the_window_in_local_time(self):
        conn = _conn(self)
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T05:58:00", 60), (f"{DIA}T06:00:00", 61),
                                    (f"{DIA}T07:30:00", 70), (f"{DIA}T08:00:00", 80),
                                    (f"{DIA}T08:02:00", 81)])
        conn.commit()
        out = _ejecutar(conn, 1, desde="06:00", hasta="08:00")
        self.assertEqual(out["frecuencia_cardiaca"], [["06:00", 61.0], ["07:30", 70.0]])
        self.assertEqual(out["dia"], DIA)
        self.assertEqual(out["desde"], "06:00")
        self.assertEqual(out["hasta"], "08:00")
        self.assertEqual(out["zona"], "America/Bogota")

    def test_minutes_without_data_are_simply_absent(self):
        conn = _conn(self)
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T06:00:00", 61), (f"{DIA}T06:10:00", 63)])
        conn.commit()
        out = _ejecutar(conn, 1, desde="06:00", hasta="06:20")
        self.assertEqual(out["frecuencia_cardiaca"], [["06:00", 61.0], ["06:10", 63.0]])
        self.assertEqual(out["estres"], [])

    def test_fc_and_estres_both_read(self):
        conn = _conn(self)
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T06:00:00", 61)])
        _guardar_estres(conn, 1, DIA, [(f"{DIA}T06:05:00", 20)])
        conn.commit()
        out = _ejecutar(conn, 1, desde="06:00", hasta="06:30")
        self.assertEqual(out["frecuencia_cardiaca"], [["06:00", 61.0]])
        self.assertEqual(out["estres"], [["06:05", 20.0]])


class CapTest(unittest.TestCase):
    def test_four_hour_window_is_allowed(self):
        conn = _conn(self)
        conn.commit()
        out = _ejecutar(conn, 1, desde="04:00", hasta="08:00")
        self.assertEqual(out["desde"], "04:00")

    def test_five_hour_window_is_refused_with_the_cap_in_the_message(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError) as ctx:
            _ejecutar(conn, 1, desde="04:00", hasta="09:00")
        self.assertIn("4 horas", str(ctx.exception))

    def test_hasta_before_desde_is_refused(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, desde="08:00", hasta="06:00")

    def test_malformed_hour_is_refused(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, desde="6am", hasta="08:00")

    def test_future_date_is_refused(self):
        conn = _conn(self)
        conn.commit()
        futuro = (date.today() + timedelta(days=5)).isoformat()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, fecha=futuro)


class SleepWindowTest(unittest.TestCase):
    def test_sleep_arrays_only_cover_the_overlap_with_the_window(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, "2026-10-04T23:00:00", f"{DIA}T06:30:00")
        _guardar_sueno(conn, 1, DIA,
                        hr=[(f"{DIA}T05:00:00", 55), (f"{DIA}T06:00:00", 58), (f"{DIA}T06:15:00", 59)],
                        movement=[(f"{DIA}T06:00:00", 1.0)])
        conn.commit()
        # Window [05:30, 06:10): keeps the 06:00 hr/movement samples, drops 05:00 (before) and
        # 06:15 (at/after hasta).
        out = _ejecutar(conn, 1, desde="05:30", hasta="06:10")
        self.assertIsNotNone(out["sueno"])
        self.assertEqual(out["sueno"]["inicio"], "2026-10-04T23:00:00")
        self.assertEqual(out["sueno"]["fin"], f"{DIA}T06:30:00")
        self.assertEqual(out["sueno"]["heart_rate"], [["06:00", 58.0]])
        self.assertEqual(out["sueno"]["movement"], [["06:00", 1.0]])

    def test_no_sleep_session_row_leaves_sueno_none(self):
        conn = _conn(self)
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T06:00:00", 61)])
        conn.commit()
        out = _ejecutar(conn, 1, desde="06:00", hasta="07:00")
        self.assertIsNone(out["sueno"])

    def test_window_outside_the_sleep_session_leaves_sueno_none(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, "2026-10-04T23:00:00", f"{DIA}T06:30:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T05:00:00", 55)])
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T10:00:00", 70)])
        conn.commit()
        out = _ejecutar(conn, 1, desde="09:00", hasta="11:00")
        self.assertIsNone(out["sueno"])


class CorreccionTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-4 ruling 3 / BODY-DESPERTAR-REAL-6A-R2 ruling 5: the day's own
    correction(s), shown next to Garmin's own sleep, each labelled as which is which -- never a
    second source of truth for the sleep window. A morning can carry both 'sueno' and 'levantado'
    at once."""

    def test_correction_shown_next_to_garmin_sleep(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, "2026-10-04T23:00:00", f"{DIA}T06:30:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T06:00:00", 58)])
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "07:00", "despertar-real-v1", "asistente")
        conn.commit()
        out = _ejecutar(conn, 1, desde="05:30", hasta="07:00")
        self.assertIsNotNone(out["sueno"])
        self.assertEqual(out["sueno"]["fin"], f"{DIA}T06:30:00")  # Garmin's own record, untouched
        self.assertEqual(set(out["sueno"]["correcciones"]), {"levantado"})
        self.assertEqual(out["sueno"]["correcciones"]["levantado"]["hora_local"], "07:00")

    def test_both_kinds_of_correction_are_shown_together(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, "2026-10-04T23:00:00", f"{DIA}T06:30:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T06:00:00", 58)])
        db.guardar_despertar_correccion(conn, 1, DIA, "sueno", "06:45", "despertar-real-v2", "asistente")
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "07:00", "despertar-real-v2", "asistente")
        conn.commit()
        out = _ejecutar(conn, 1, desde="05:30", hasta="07:00")
        self.assertIsNotNone(out["sueno"])
        self.assertEqual(set(out["sueno"]["correcciones"]), {"sueno", "levantado"})
        self.assertEqual(out["sueno"]["correcciones"]["sueno"]["hora_local"], "06:45")
        self.assertEqual(out["sueno"]["correcciones"]["levantado"]["hora_local"], "07:00")

    def test_no_correction_leaves_the_key_absent(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, "2026-10-04T23:00:00", f"{DIA}T06:30:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T06:00:00", 58)])
        conn.commit()
        out = _ejecutar(conn, 1, desde="05:30", hasta="07:00")
        self.assertIsNotNone(out["sueno"])
        self.assertNotIn("correcciones", out["sueno"])


class SinDatosTest(unittest.TestCase):
    def test_empty_window_says_watch_off_when_not_connected(self):
        conn = _conn(self)
        conn.commit()
        out = _ejecutar(conn, 1, desde="06:00", hasta="07:00")
        self.assertEqual(out["sin_datos"]["motivo"], "reloj_apagado")
        self.assertTrue(out["sin_datos"]["mensaje"])

    def test_empty_window_says_not_fetched_yet_when_connected(self):
        conn = _conn(self)
        conn.commit()
        with mock.patch.object(herramientas.garmin_adapter, "has_token", return_value=True):
            out = _ejecutar(conn, 1, desde="06:00", hasta="07:00")
        self.assertEqual(out["sin_datos"]["motivo"], "no_traido")

    def test_empty_window_says_out_of_garmin_reach_for_an_old_day(self):
        conn = _conn(self)
        conn.commit()
        vieja = (date.today() - timedelta(days=pull.INTRADIA_ALCANCE_DIAS + 10)).isoformat()
        out = _ejecutar(conn, 1, fecha=vieja, desde="06:00", hasta="07:00")
        self.assertEqual(out["sin_datos"]["motivo"], "fuera_de_alcance")

    def test_non_empty_window_carries_no_sin_datos_key(self):
        conn = _conn(self)
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T06:00:00", 61)])
        conn.commit()
        out = _ejecutar(conn, 1, desde="06:00", hasta="07:00")
        self.assertNotIn("sin_datos", out)


class ActualizacionNotaTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-2 ruling 2: `ejecutar()` passes `actualizacion_fallo` through to
    `_leer_intradia`, which turns it into the localized note -- the wiring, tested directly here;
    `pull.actualizar_intradia_si_corresponde`'s own decision logic is tests/test_pull.py's."""

    def test_a_failed_refresh_adds_the_localized_note(self):
        conn = _conn(self)
        conn.commit()
        out = herramientas._leer_intradia(conn, 1, DIA, "06:00", "08:00", actualizacion_fallo=True)
        self.assertEqual(out["actualizacion"], "No pude traer los datos más recientes justo ahora; "
                                                 "aquí tienes lo último que ya tengo guardado.")

    def test_no_failure_carries_no_actualizacion_key(self):
        conn = _conn(self)
        conn.commit()
        out = herramientas._leer_intradia(conn, 1, DIA, "06:00", "08:00")
        self.assertNotIn("actualizacion", out)


class NivelDatosGuardTest(unittest.TestCase):
    def test_refused_below_completo(self):
        conn = _conn(self)
        conn.commit()
        for nivel in ("resumen", "ninguno"):
            with self.assertRaises(herramientas.ToolError):
                _ejecutar(conn, 1, nivel)

    def test_allowed_at_completo(self):
        conn = _conn(self)
        conn.commit()
        out = _ejecutar(conn, 1, "completo")
        self.assertIn("frecuencia_cardiaca", out)


class RegistryShapeTest(unittest.TestCase):
    def test_is_a_lectura_tool_at_nivel_completo(self):
        self.assertEqual(herramientas.tipo_de("leer_intradia"), "lectura")
        entrada = herramientas._BY_NAME["leer_intradia"]
        self.assertEqual(entrada["nivel"], "completo")

    def test_offered_wherever_a_lectura_key_is_offered(self):
        self.assertIn("leer_intradia", {h["nombre"] for h in herramientas.disponibles("completo")})


class CrossPersonTest(unittest.TestCase):
    def test_another_persons_window_is_never_returned(self):
        conn = _conn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T06:00:00", 61)])
        _guardar_fc(conn, 2, DIA, [(f"{DIA}T06:00:00", 999)])
        conn.commit()
        out_a = _ejecutar(conn, 1, desde="06:00", hasta="07:00")
        out_b = _ejecutar(conn, 2, desde="06:00", hasta="07:00")
        self.assertEqual(out_a["frecuencia_cardiaca"], [["06:00", 61.0]])
        self.assertEqual(out_b["frecuencia_cardiaca"], [["06:00", 999.0]])


if __name__ == "__main__":
    unittest.main()
