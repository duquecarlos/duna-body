"""BODY-NOCHE-LECTURA-1 ruling 2: leer_noche, one call for the whole night Garmin files under a
wake day -- the window around Garmin's own sleep session, crossing midnight on purpose, plus the
derived facts Body already knows how to compute for that night. Invented demo data only, no real
health figures, no network -- every date here is either long in the past or computed from a
dynamic reference, never a Garmin call.
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import baselines, db, desplazamiento_import, despertar_real, garmin_adapter, pull, sueno_fc
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
                    etapas: list[tuple[str, float]] = ()) -> None:
    payload = {"sleepHeartRate": [{"startGMT": _ms(ts), "value": v} for ts, v in hr]}
    if etapas:
        payload["sleepLevels"] = [{"startGMT": _ms(ts), "activityLevel": c} for ts, c in etapas]
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


def _seed_baseline_metric(conn, person_id: int, key: str, antes_de: str, n: int = 10, valor: float = 50.0) -> None:
    d = date.fromisoformat(antes_de)
    for i in range(1, n + 1):
        day = (d - timedelta(days=i)).isoformat()
        db.set_metric(conn, day, key, valor, "garmin", person_id)


# A night crossing midnight far enough in the past that it is never today/yesterday for real --
# ruling 2's own on-demand refresh never fires here, so these tests need no Garmin mock at all.
PREV = "2026-10-04"
DIA = "2026-10-05"


def _ejecutar(conn, person_id: int, nivel: str = "completo", **kwargs):
    params = {"fecha": DIA}
    params.update(kwargs)
    return herramientas.ejecutar(conn, person_id, "leer_noche", params, nivel)


class WholeNightOneCallTest(unittest.TestCase):
    def test_window_spans_before_sleep_start_to_after_sleep_end_across_midnight(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["dia"], DIA)
        self.assertEqual(out["desde"], f"{PREV}T22:00:00")  # 60 min before sleep start
        self.assertEqual(out["hasta"], f"{DIA}T08:00:00")   # 120 min after sleep end

    def test_one_call_returns_both_calendar_dates_fc_and_estres(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_fc(conn, 1, PREV, [(f"{PREV}T21:00:00", 65), (f"{PREV}T22:30:00", 62)])
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T07:00:00", 58), (f"{DIA}T09:00:00", 70)])
        _guardar_estres(conn, 1, DIA, [(f"{DIA}T07:30:00", 15)])
        conn.commit()
        out = _ejecutar(conn, 1)
        # 21:00 (before the window) and 09:00 (after it) are both dropped; the ones inside carry
        # their own FULL local timestamp (date and time), never just HH:MM (ruling 1).
        self.assertEqual(out["frecuencia_cardiaca"], [[f"{PREV}T22:30", 62.0], [f"{DIA}T07:00", 58.0]])
        self.assertEqual(out["estres"], [[f"{DIA}T07:30", 15.0]])

    def test_sleep_minute_arrays_and_garmins_own_start_end_are_included(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T02:00:00", 55), (f"{DIA}T04:30:00", 47)])
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["sueno"]["inicio"], f"{PREV}T23:00:00")
        self.assertEqual(out["sueno"]["fin"], f"{DIA}T06:00:00")
        self.assertEqual(out["sueno"]["heart_rate"], [[f"{DIA}T02:00", 55.0], [f"{DIA}T04:30", 47.0]])

    def test_corrections_are_shown_next_to_garmins_own_sleep(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        db.guardar_despertar_correccion(conn, 1, DIA, "levantado", "07:00", "despertar-real-v2", "asistente")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(set(out["sueno"]["correcciones"]), {"levantado"})
        self.assertEqual(out["sueno"]["correcciones"]["levantado"]["hora_local"], "07:00")

    def test_no_correction_leaves_the_key_absent(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertNotIn("correcciones", out["sueno"])


class WindowCapTest(unittest.TestCase):
    def test_a_long_sleep_session_window_is_capped_at_sixteen_hours(self):
        conn = _conn(self)
        # 23:00 to 14:00 the next day is 15 hours asleep; padded (+60/-120... err -60/+120) that
        # would be 17 hours -- capped back to the 16-hour ceiling, counted from the window's own
        # start (60 min before sleep start), never from sleep start itself.
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T14:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        inicio = datetime.fromisoformat(out["desde"])
        fin = datetime.fromisoformat(out["hasta"])
        self.assertEqual((fin - inicio).total_seconds() / 60, herramientas._LEER_NOCHE_MAX_MIN)
        self.assertEqual(out["desde"], f"{PREV}T22:00:00")

    def test_a_short_sleep_session_is_never_padded_past_the_cap(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        inicio = datetime.fromisoformat(out["desde"])
        fin = datetime.fromisoformat(out["hasta"])
        self.assertLess((fin - inicio).total_seconds() / 60, herramientas._LEER_NOCHE_MAX_MIN)


class SinDatosTest(unittest.TestCase):
    def test_no_sleep_session_at_all_says_watch_off_when_not_connected(self):
        conn = _conn(self)
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["sin_datos"]["motivo"], "reloj_apagado")
        self.assertTrue(out["sin_datos"]["mensaje"])
        self.assertNotIn("frecuencia_cardiaca", out)
        self.assertNotIn("sueno", out)

    def test_no_sleep_session_says_not_fetched_yet_when_connected(self):
        conn = _conn(self)
        conn.commit()
        with mock.patch.object(herramientas.garmin_adapter, "has_token", return_value=True):
            out = _ejecutar(conn, 1)
        self.assertEqual(out["sin_datos"]["motivo"], "no_traido")

    def test_no_sleep_session_says_out_of_garmin_reach_for_an_old_day(self):
        conn = _conn(self)
        conn.commit()
        vieja = (date.today() - timedelta(days=pull.INTRADIA_ALCANCE_DIAS + 10)).isoformat()
        out = _ejecutar(conn, 1, fecha=vieja)
        self.assertEqual(out["sin_datos"]["motivo"], "fuera_de_alcance")

    def test_a_sleep_session_with_only_one_side_missing_still_sin_datos(self):
        conn = _conn(self)
        conn.execute(
            "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (1, ?, ?, NULL)",
            (DIA, f"{PREV}T23:00:00"),
        )
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertIn("sin_datos", out)


class FutureDateTest(unittest.TestCase):
    def test_future_fecha_is_refused(self):
        conn = _conn(self)
        conn.commit()
        futuro = (date.today() + timedelta(days=5)).isoformat()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, fecha=futuro)

    def test_malformed_fecha_is_refused(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, fecha="not-a-date")


class DerivedFactsTest(unittest.TestCase):
    def test_fc_minima_is_the_actual_lowest_minute_with_its_own_hour(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T02:00:00", 55), (f"{DIA}T04:30:00", 47),
                                          (f"{DIA}T05:45:00", 52)])
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["fc_minima"], {"valor": 47.0, "hora": f"{DIA}T04:30"})
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: the single minute carries no range of its own --
        # the old `fc_minima_vs_rango` key is gone, never silently renamed back onto `fc_minima`.
        self.assertNotIn("fc_minima_vs_rango", out)

    def test_fc_baja_robusta_vs_rango_reuses_baselines_baseline_on_sleep_hr_min(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: fc_baja_robusta reads the wake day's own stored
        # ROBUST low (daily_metric_efectiva), never the single minute -- so this test stores the
        # metric itself, the same way body/sueno_fc.py's own post-pull step would.
        db.set_metric(conn, DIA, sueno_fc.CLAVE_MIN, 40.0, "body-sueno-fc-v1", 1)
        _seed_baseline_metric(conn, 1, sueno_fc.CLAVE_MIN, DIA, n=10, valor=55.0)
        conn.commit()
        esperado = baselines.baseline(conn, sueno_fc.CLAVE_MIN, DIA, person_id=1)
        out = _ejecutar(conn, 1)
        self.assertIsNotNone(esperado)
        self.assertEqual(out["fc_baja_robusta"], 40.0)
        self.assertEqual(out["fc_baja_robusta_vs_rango"]["rango_30_dias"],
                          {"bajo": esperado.low, "alto": esperado.high})
        self.assertAlmostEqual(out["fc_baja_robusta_vs_rango"]["z"], esperado.z(40.0))
        self.assertEqual(out["fc_baja_robusta_vs_rango"]["z_base"], "rango_30_dias")
        self.assertNotIn("motivo_sin_rango_30_dias", out["fc_baja_robusta_vs_rango"])

    def test_vfc_noche_reads_the_days_own_hrv_and_its_usual_range(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        db.set_metric(conn, DIA, "hrv_last_night", 35.0, "garmin", 1)
        _seed_baseline_metric(conn, 1, "hrv_last_night", DIA, n=10, valor=50.0)
        conn.commit()
        esperado = baselines.baseline(conn, "hrv_last_night", DIA, person_id=1)
        out = _ejecutar(conn, 1)
        self.assertIsNotNone(esperado)
        self.assertEqual(out["vfc_noche"]["valor"], 35.0)
        self.assertEqual(out["vfc_noche"]["rango_30_dias"], {"bajo": esperado.low, "alto": esperado.high})
        self.assertEqual(out["vfc_noche"]["z_base"], "rango_30_dias")

    def test_despertar_modelo_is_despertar_reals_own_unedited_result(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T05:35:00", 52), (f"{DIA}T05:40:00", 52),
                                          (f"{DIA}T05:50:00", 52), (f"{DIA}T05:55:00", 52)])
        conn.commit()
        esperado = despertar_real.estimar(conn, 1, DIA)
        out = _ejecutar(conn, 1)
        self.assertEqual(out["despertar_modelo"], esperado)

    def test_minutos_despierto_tras_fin_matches_the_dozing_stretch_after_garmins_own_end(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        # Baseline right before Garmin's own end (>= cobertura_min_base samples).
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T05:35:00", 52), (f"{DIA}T05:40:00", 52),
                                          (f"{DIA}T05:50:00", 52), (f"{DIA}T05:55:00", 52)])
        # Still dozing right after Garmin's own end, then a sustained rise.
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T06:05:00", 54), (f"{DIA}T06:30:00", 55),
                                     (f"{DIA}T07:00:00", 75), (f"{DIA}T07:15:00", 76)])
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DIA)
        primero = resultado["segmentos"][0]
        self.assertEqual(primero["tipo"], "dormitando")  # the scenario this test means to build
        esperado = round((datetime.fromisoformat(primero["fin"]) - datetime.fromisoformat(primero["inicio"]))
                          .total_seconds() / 60, 1)
        self.assertGreater(esperado, 0)
        out = _ejecutar(conn, 1)
        self.assertEqual(out["minutos_despierto_tras_fin"], esperado)

    def test_minutos_despierto_tras_fin_is_zero_on_an_immediate_confirmed_wake(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T05:35:00", 52), (f"{DIA}T05:40:00", 52),
                                          (f"{DIA}T05:50:00", 52), (f"{DIA}T05:55:00", 52)])
        # A sustained, immediate rise right at Garmin's own end -- Garmin was already right.
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T06:01:00", 80), (f"{DIA}T06:15:00", 81), (f"{DIA}T06:30:00", 82)])
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DIA)
        self.assertEqual(resultado["segmentos"][0]["tipo"], "levantado")  # the scenario this test means to build
        out = _ejecutar(conn, 1)
        self.assertEqual(out["minutos_despierto_tras_fin"], 0.0)


class DerivedFactsNullTest(unittest.TestCase):
    """Each derived fact is null exactly when ITS OWN input is missing -- never gated on a
    sibling fact, and never invented."""

    def test_fc_minima_is_null_with_no_heart_rate_array(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertIsNone(out["fc_minima"])

    def test_fc_baja_robusta_and_its_rango_are_null_with_no_stored_metric(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertIsNone(out["fc_baja_robusta"])
        self.assertIsNone(out["fc_baja_robusta_vs_rango"])

    def test_fc_baja_robusta_vs_rango_is_null_with_too_few_baseline_days(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        db.set_metric(conn, DIA, sueno_fc.CLAVE_MIN, 40.0, "body-sueno-fc-v1", 1)
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["fc_baja_robusta"], 40.0)
        # BODY-NOCHE-LECTURA-2 ruling 1: no baseline days at all is 'pocos_dias', not an entirely
        # None fact -- both ranges stay independently null and z has nothing to come from either.
        self.assertEqual(out["fc_baja_robusta_vs_rango"]["rango_30_dias"], None)
        self.assertEqual(out["fc_baja_robusta_vs_rango"]["motivo_sin_rango_30_dias"], "pocos_dias")
        self.assertIsNone(out["fc_baja_robusta_vs_rango"]["rango_historia"])
        self.assertIsNone(out["fc_baja_robusta_vs_rango"]["z"])
        self.assertIsNone(out["fc_baja_robusta_vs_rango"]["z_base"])

    def test_vfc_noche_is_null_with_no_hrv_stored_for_the_day(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertIsNone(out["vfc_noche"])

    def test_minutos_despierto_tras_fin_is_null_with_no_model_baseline(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["despertar_modelo"]["evidencia"]["motivo"], "sin_linea_base")
        self.assertIsNone(out["minutos_despierto_tras_fin"])


class ActualizacionNotaTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-2 ruling 2's own wiring, reused as-is: `ejecutar()` passes
    `actualizacion_fallo` through to `_leer_noche`, which turns it into the same localized note
    leer_intradia already uses."""

    def test_a_failed_refresh_adds_the_localized_note(self):
        conn = _conn(self)
        conn.commit()
        out = herramientas._leer_noche(conn, 1, DIA, actualizacion_fallo=True)
        self.assertEqual(out["actualizacion"], "No pude traer los datos más recientes justo ahora; "
                                                 "aquí tienes lo último que ya tengo guardado.")

    def test_no_failure_carries_no_actualizacion_key(self):
        conn = _conn(self)
        conn.commit()
        out = herramientas._leer_noche(conn, 1, DIA)
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
        self.assertIn("sin_datos", out)  # no sleep session seeded; still a valid, allowed call


class RegistryShapeTest(unittest.TestCase):
    def test_is_a_lectura_tool_at_nivel_completo(self):
        self.assertEqual(herramientas.tipo_de("leer_noche"), "lectura")
        entrada = herramientas._BY_NAME["leer_noche"]
        self.assertEqual(entrada["nivel"], "completo")

    def test_offered_wherever_a_lectura_key_is_offered(self):
        self.assertIn("leer_noche", {h["nombre"] for h in herramientas.disponibles("completo")})


class CrossPersonTest(unittest.TestCase):
    def test_another_persons_night_is_never_returned(self):
        conn = _conn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, hr=[(f"{DIA}T04:30:00", 47)])
        db.set_metric(conn, DIA, "hrv_last_night", 35.0, "garmin", 1)
        # Person 2 has no sleep session at all for this day -- their own call must read as
        # sin_datos, never borrowing person 1's own night.
        conn.commit()
        out_a = _ejecutar(conn, 1)
        out_b = _ejecutar(conn, 2)
        self.assertEqual(out_a["fc_minima"], {"valor": 47.0, "hora": f"{DIA}T04:30"})
        self.assertIn("sin_datos", out_b)
        self.assertNotIn("fc_minima", out_b)


class ContextoTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-5-CONTEXTO ruling 3: leer_noche's own `contexto` -- the EVENING's
    (the day before `fecha`) derived shift/load/alcohol, plus any trip or period covering either
    calendar date (body/contexto_dia.py's own module docstring)."""

    def test_contexto_carries_the_evenings_shift(self):
        conn = _conn(self)
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, ?, 'night', 'manual')",
                     (PREV,))
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["contexto"]["turno"]["tipo"], "night")

    def test_sin_datos_night_has_no_contexto_key(self):
        conn = _conn(self)
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertIn("sin_datos", out)
        self.assertNotIn("contexto", out)

    def test_contexto_carries_the_evenings_commute(self):
        """BODY-MINUTO-ANALISIS-7."""
        conn = _conn(self)
        db.set_metric(conn, PREV, "vehicle_minutes", 18.0, desplazamiento_import.FUENTE, 1)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["contexto"]["desplazamiento"], {"vehicle_minutes": 18.0})


class StatementCountTest(unittest.TestCase):
    """BODY-NOCHE-LECTURA-1 ruling 3: the SQL statement count stays the same whether the night's
    own window holds a handful of samples or hundreds -- never one statement per sample, and
    never one per calendar day beyond the two the window can ever touch (ruling 3's own comment
    in body/agente/herramientas.py)."""

    def _contar_raw_payload(self, conn, fn) -> int:
        vistos = []
        conn.set_trace_callback(lambda sql: vistos.append(sql))
        try:
            fn()
        finally:
            conn.set_trace_callback(None)
        return sum(1 for sql in vistos if "raw_payload" in sql)

    def test_same_statement_count_sparse_vs_dense_night(self):
        # Both seed the SAME baseline-window samples (so despertar_real.estimar's own baseline
        # takes the same path either way, never a confound this test does not mean to introduce)
        # and differ only in how many OTHER minute samples the night carries.
        baseline_hr = [(f"{DIA}T05:30:00", 50), (f"{DIA}T05:40:00", 50), (f"{DIA}T05:50:00", 50)]

        conn_pocos = _conn(self)
        _sesion(conn_pocos, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_fc(conn_pocos, 1, PREV, [(f"{PREV}T22:30:00", 62)])
        _guardar_fc(conn_pocos, 1, DIA, [(f"{DIA}T07:00:00", 58)])
        _guardar_sueno(conn_pocos, 1, DIA, hr=baseline_hr + [(f"{DIA}T04:30:00", 47)])
        conn_pocos.commit()

        conn_muchos = _conn(self)
        _sesion(conn_muchos, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_fc(conn_muchos, 1, PREV, [(f"{PREV}T2{h}:{m:02d}:00", 60 + h)
                                            for h in (0, 1, 2, 3) for m in range(0, 60, 5)])
        _guardar_fc(conn_muchos, 1, DIA, [(f"{DIA}T0{h}:{m:02d}:00", 55 + h)
                                           for h in range(0, 8) for m in range(0, 60, 5)])
        _guardar_sueno(conn_muchos, 1, DIA, hr=baseline_hr + [(f"{DIA}T0{h}:{m:02d}:00", 45 + h)
                                                               for h in range(0, 6) for m in range(0, 60, 5)])
        conn_muchos.commit()

        n_pocos = self._contar_raw_payload(conn_pocos, lambda: _ejecutar(conn_pocos, 1))
        n_muchos = self._contar_raw_payload(conn_muchos, lambda: _ejecutar(conn_muchos, 1))
        self.assertEqual(n_pocos, n_muchos)

    def test_same_statement_count_short_vs_near_cap_night(self):
        conn_corta = _conn(self)
        _sesion(conn_corta, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T00:30:00")
        conn_corta.commit()

        conn_larga = _conn(self)
        _sesion(conn_larga, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T13:00:00")
        conn_larga.commit()

        n_corta = self._contar_raw_payload(conn_corta, lambda: _ejecutar(conn_corta, 1))
        n_larga = self._contar_raw_payload(conn_larga, lambda: _ejecutar(conn_larga, 1))
        self.assertEqual(n_corta, n_larga)


class RangoDobleTest(unittest.TestCase):
    """BODY-NOCHE-LECTURA-2 ruling 1: rango_30_dias and rango_historia, each independently null,
    with the honest reason when the 30-day one is."""

    def test_an_open_marked_period_covering_the_window_gives_dias_marcados_with_history_present(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        db.set_metric(conn, DIA, sueno_fc.CLAVE_MIN, 40.0, "body-sueno-fc-v1", 1)
        # Twenty values inside the 30-day window -- enough to clear MIN_DAYS before exclusion
        # (so the honest reason is 'dias_marcados', never 'pocos_dias') and enough for
        # profile.metric_profile's own 14-day floor, so rango_historia is present.
        _seed_baseline_metric(conn, 1, sueno_fc.CLAVE_MIN, DIA, n=20, valor=55.0)
        # An open ('lesion', end_day IS NULL) period starting long before the window -- covers
        # every one of those 20 days through today (BODY-PERIODO-1 ruling 3's own exclusion),
        # the desk's own case (a leer_noche surface, relayed 2026-11-19).
        conn.execute(
            "INSERT INTO period (person_id, kind, start_day, created_at, updated_at) "
            "VALUES (1, 'lesion', '2026-01-01', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        out = _ejecutar(conn, 1)
        rango = out["fc_baja_robusta_vs_rango"]
        self.assertIsNone(rango["rango_30_dias"])
        self.assertEqual(rango["motivo_sin_rango_30_dias"], "dias_marcados")
        # 21, not 20: `fc_baja_robusta` is this night's own stored sleep_hr_min (set above, the
        # same precondition production's sueno_fc.py post-pull step leaves behind), which
        # profile.metric_profile counts as one more day of this person's own history, same as
        # any other past night's own value -- never excluded just because it is the night read.
        self.assertEqual(rango["rango_historia"], {"bajo": 55.0, "alto": 55.0, "dias": 21})

    def test_a_new_person_with_few_days_gives_pocos_dias(self):
        conn = _conn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'C')")
        _sesion(conn, 3, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        db.set_metric(conn, DIA, sueno_fc.CLAVE_MIN, 40.0, "body-sueno-fc-v1", 3)
        _seed_baseline_metric(conn, 3, sueno_fc.CLAVE_MIN, DIA, n=3, valor=55.0)
        conn.commit()
        out = _ejecutar(conn, 3)
        rango = out["fc_baja_robusta_vs_rango"]
        self.assertIsNone(rango["rango_30_dias"])
        self.assertEqual(rango["motivo_sin_rango_30_dias"], "pocos_dias")
        self.assertIsNone(rango["rango_historia"])

    def test_another_persons_history_profile_is_never_used(self):
        conn = _conn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        db.set_metric(conn, DIA, sueno_fc.CLAVE_MIN, 40.0, "body-sueno-fc-v1", 1)
        # Person 2 alone has plenty of history for this same key; person 1 has none at all.
        _seed_baseline_metric(conn, 2, sueno_fc.CLAVE_MIN, DIA, n=30, valor=55.0)
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertIsNone(out["fc_baja_robusta_vs_rango"]["rango_historia"])
        self.assertEqual(out["fc_baja_robusta_vs_rango"]["motivo_sin_rango_30_dias"], "pocos_dias")


class EtapaEtiquetaTest(unittest.TestCase):
    """BODY-NOCHE-LECTURA-2 ruling 2: every stage point carries [hora, codigo, etiqueta]; a code
    outside ETAPAS_SUENO_GARMIN gets 'desconocido', never a guess."""

    def test_known_codes_get_their_label_and_an_unknown_code_gets_desconocido(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, etapas=[(f"{DIA}T00:00:00", 0), (f"{DIA}T01:00:00", 1),
                                              (f"{DIA}T02:00:00", 2), (f"{DIA}T03:00:00", 3),
                                              (f"{DIA}T04:00:00", 9)])
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["sueno"]["stages"], [
            [f"{DIA}T00:00", 0.0, "profundo"], [f"{DIA}T01:00", 1.0, "ligero"],
            [f"{DIA}T02:00", 2.0, "rem"], [f"{DIA}T03:00", 3.0, "despierto"],
            [f"{DIA}T04:00", 9.0, "desconocido"],
        ])


class EtapaVerificacionTest(unittest.TestCase):
    """BODY-NOCHE-LECTURA-2 ruling 2: etapas_verificacion checks the labelled stages array against
    the night's own stored totals (the day page's own STAGES keys)."""

    _ETAPAS = [(f"{DIA}T00:00:00", 0), (f"{DIA}T02:00:00", 1), (f"{DIA}T04:00:00", 2),
               (f"{DIA}T05:30:00", 3)]
    # The array alone spans 00:00->06:00 (fin_dt): profundo 2h, ligero 2h, rem 1.5h, despierto 0.5h.

    def _sembrar(self, conn, person_id: int, deep=120.0, light=120.0, rem=90.0, awake=30.0) -> None:
        _sesion(conn, person_id, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, person_id, DIA, etapas=self._ETAPAS)
        db.set_metric(conn, DIA, "sleep_deep_min", deep, "garmin", person_id)
        db.set_metric(conn, DIA, "sleep_light_min", light, "garmin", person_id)
        db.set_metric(conn, DIA, "sleep_rem_min", rem, "garmin", person_id)
        db.set_metric(conn, DIA, "sleep_awake_min", awake, "garmin", person_id)
        conn.commit()

    def test_coincide_true_when_the_stored_totals_match_the_labelled_array(self):
        conn = _conn(self)
        self._sembrar(conn, 1)
        out = _ejecutar(conn, 1)
        self.assertEqual(out["etapas_verificacion"], {"coincide": True, "diferencia_min_max": 0.0})

    def test_coincide_false_when_a_stored_total_does_not_match(self):
        conn = _conn(self)
        self._sembrar(conn, 1, deep=10.0)  # the array says 120 minutes of 'profundo', not 10
        out = _ejecutar(conn, 1)
        self.assertFalse(out["etapas_verificacion"]["coincide"])
        self.assertGreater(out["etapas_verificacion"]["diferencia_min_max"], 5)

    def test_coincide_is_null_without_stored_totals(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, etapas=self._ETAPAS)
        conn.commit()
        out = _ejecutar(conn, 1)
        self.assertEqual(out["etapas_verificacion"], {"coincide": None, "diferencia_min_max": None})


class RangoDobleStatementCountTest(unittest.TestCase):
    """BODY-NOCHE-LECTURA-2 ruling 1: reading rango_30_dias, rango_historia and z for BOTH
    fc_baja_robusta_vs_rango and vfc_noche costs a fixed, named number of statements on top of
    StatementCountTest's own invariant above -- never one that grows with the night's own sample
    count. Measured on this fixture at the time this test was written: 22 (both ranges present)
    and 26 (both null, which also pays the wide-window z fallback, once per metric) -- the
    ceiling below gives both a comfortable, named margin.

    BODY-MINUTO-ANALISIS-5-CONTEXTO ruling 3: re-measured with leer_noche's own new `contexto`
    (contexto_dia.contexto_noche, a fixed cost independent of this night's own data -- tests/
    test_contexto_dia.py's own guard): 29 (both ranges present) and 33 (both null). Ceiling
    raised from 30 to 40 to keep the same comfortable margin."""

    _CEILING = 40

    def _contar(self, conn, fn) -> int:
        vistos = []
        conn.set_trace_callback(lambda sql: vistos.append(sql))
        try:
            fn()
        finally:
            conn.set_trace_callback(None)
        return len(vistos)

    def test_both_ranges_present_stays_within_the_named_ceiling(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        db.set_metric(conn, DIA, sueno_fc.CLAVE_MIN, 40.0, "body-sueno-fc-v1", 1)
        db.set_metric(conn, DIA, "hrv_last_night", 35.0, "garmin", 1)
        _seed_baseline_metric(conn, 1, sueno_fc.CLAVE_MIN, DIA, n=20, valor=55.0)
        _seed_baseline_metric(conn, 1, "hrv_last_night", DIA, n=20, valor=50.0)
        conn.commit()
        n = self._contar(conn, lambda: _ejecutar(conn, 1))
        self.assertLessEqual(n, self._CEILING)

    def test_both_ranges_null_stays_within_the_named_ceiling(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        db.set_metric(conn, DIA, sueno_fc.CLAVE_MIN, 40.0, "body-sueno-fc-v1", 1)
        db.set_metric(conn, DIA, "hrv_last_night", 35.0, "garmin", 1)
        conn.commit()
        n = self._contar(conn, lambda: _ejecutar(conn, 1))
        self.assertLessEqual(n, self._CEILING)


if __name__ == "__main__":
    unittest.main()
