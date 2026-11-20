"""ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: leer_serie's own paso='minuto' -- a bounded window of
full local timestamps over the minute streams (fc, estres, etapa, vfc, movimiento, body_battery),
for analysing a short stretch in detail instead of a whole leer_noche or several leer_intradia
calls. Invented demo data only, no real health figures, no network -- every date here is far
enough in the past that no on-demand refresh (leer_intradia/leer_noche's own) could ever fire,
so these tests need no Garmin mock at all.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter
from body.agente import herramientas

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_leer_noche.py convention)

PREV = "2026-10-04"
DIA = "2026-10-05"


def _conn(case: unittest.TestCase):
    c = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _guardar_fc(conn, person_id: int, day: str, muestras: list) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_fc",
                         {"heartRateValues": [[_ms(ts), v] for ts, v in muestras]})


def _guardar_estres(conn, person_id: int, day: str, muestras: list) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_estres",
                         {"stressValuesArray": [[_ms(ts), v] for ts, v in muestras]})


def _guardar_bb(conn, person_id: int, day: str, muestras: list) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, ?, 'body_battery', ?, ?, ?)",
        (person_id, day, garmin_adapter.SOURCE, "2026-01-01T00:00:00+00:00",
         json.dumps([{"bodyBatteryValuesArray": [[_ms(ts), v] for ts, v in muestras]}])),
    )


def _guardar_sueno(conn, person_id: int, day: str, hr=(), hrv=(), etapas=(), movimiento=()) -> None:
    payload = {}
    if hr:
        payload["sleepHeartRate"] = [{"startGMT": _ms(ts), "value": v} for ts, v in hr]
    if hrv:
        payload["hrvData"] = [{"startGMT": _ms(ts), "value": v} for ts, v in hrv]
    if etapas:
        payload["sleepLevels"] = [{"startGMT": _ms(ts), "activityLevel": c} for ts, c in etapas]
    if movimiento:
        payload["sleepMovement"] = [{"startGMT": _ms(ts), "activityLevel": v} for ts, v in movimiento]
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


def _ejecutar(conn, person_id: int, desde: str, hasta: str, nivel: str = "completo",
              metricas: list = None) -> dict:
    metricas = list(herramientas.LEER_SERIE_MINUTO_METRICAS) if metricas is None else metricas
    params = {"metricas": metricas, "desde": desde, "hasta": hasta, "paso": "minuto"}
    return herramientas.ejecutar(conn, person_id, "leer_serie", params, nivel)


class CrossMidnightShapeTest(unittest.TestCase):
    def test_one_row_per_minute_with_data_full_timestamps_both_dates(self):
        conn = _conn(self)
        _guardar_fc(conn, 1, PREV, [(f"{PREV}T23:55:00", 60), (f"{PREV}T23:40:00", 999)])
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T00:10:00", 58)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{PREV}T23:50:00", f"{DIA}T00:30:00", metricas=["fc"])
        self.assertEqual(out["paso"], "minuto")
        filas = {f["ts"]: f for f in out["filas"]}
        self.assertEqual(set(filas), {f"{PREV}T23:55", f"{DIA}T00:10"})
        self.assertEqual(filas[f"{PREV}T23:55"]["fc"], 60.0)
        self.assertEqual(filas[f"{DIA}T00:10"]["fc"], 58.0)
        # 23:40 is before the window (desde=23:50) and must never appear.
        self.assertNotIn(f"{PREV}T23:40", filas)

    def test_window_is_half_open_hasta_excluded(self):
        conn = _conn(self)
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T00:30:00", 70)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{DIA}T00:00:00", f"{DIA}T00:30:00", metricas=["fc"])
        self.assertEqual(out["filas"], [])


class EtapaSegmentTest(unittest.TestCase):
    def test_etapa_label_covers_each_minute_of_its_own_segment(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, etapas=[(f"{PREV}T23:50:00", 1), (f"{DIA}T00:10:00", 0)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{PREV}T23:50:00", f"{DIA}T00:30:00", metricas=["etapa"])
        filas = {f["ts"]: f["etapa"] for f in out["filas"]}
        self.assertEqual(filas[f"{PREV}T23:55"], "ligero")
        self.assertEqual(filas[f"{DIA}T00:09"], "ligero")
        self.assertEqual(filas[f"{DIA}T00:10"], "profundo")
        self.assertEqual(filas[f"{DIA}T00:29"], "profundo")

    def test_unknown_stage_code_is_desconocido_never_dropped(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, etapas=[(f"{PREV}T23:50:00", 99)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{PREV}T23:50:00", f"{PREV}T23:52:00", metricas=["etapa"])
        filas = {f["ts"]: f["etapa"] for f in out["filas"]}
        self.assertEqual(filas[f"{PREV}T23:50"], "desconocido")


class VfcForwardFillTest(unittest.TestCase):
    def test_vfc_forward_fills_within_tolerance_never_from_the_future(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, hrv=[(f"{PREV}T23:50:00", 50), (f"{DIA}T00:10:00", 55)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{PREV}T23:50:00", f"{DIA}T00:20:00", metricas=["vfc"])
        filas = {f["ts"]: f.get("vfc") for f in out["filas"]}
        self.assertEqual(filas.get(f"{PREV}T23:50"), 50.0)
        self.assertEqual(filas.get(f"{PREV}T23:54"), 50.0)  # 4 min after, within the 5-min tolerance
        self.assertNotIn(f"{PREV}T23:56", filas)  # 6 min after 23:50, and before the NEXT sample
        self.assertEqual(filas.get(f"{DIA}T00:10"), 55.0)
        self.assertEqual(filas.get(f"{DIA}T00:14"), 55.0)


class BodyBatteryNearestTest(unittest.TestCase):
    def test_body_battery_matches_the_nearest_sample_on_either_side(self):
        conn = _conn(self)
        _guardar_bb(conn, 1, PREV, [(f"{PREV}T23:50:00", 60)])
        _guardar_bb(conn, 1, DIA, [(f"{DIA}T00:20:00", 65)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{PREV}T23:55:00", f"{DIA}T00:10:00", metricas=["body_battery"])
        filas = {f["ts"]: f.get("body_battery") for f in out["filas"]}
        # 23:58 is 8 min from 23:50 and 22 min from 00:20 -- the nearer one wins.
        self.assertEqual(filas.get(f"{PREV}T23:58"), 60.0)
        # 00:07 is 17 min from 23:50 (over its own 15-min tolerance, moot) and 13 min from 00:20 --
        # the NEARER sample is picked even though it lies in the window's own future.
        self.assertEqual(filas.get(f"{DIA}T00:07"), 65.0)

    def test_body_battery_absent_past_the_tolerance_on_both_sides(self):
        conn = _conn(self)
        _guardar_bb(conn, 1, DIA, [(f"{DIA}T00:00:00", 60), (f"{DIA}T00:40:00", 70)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{DIA}T00:18:00", f"{DIA}T00:22:00", metricas=["body_battery"])
        self.assertEqual(out["filas"], [])


class StreamsAbsentTest(unittest.TestCase):
    def test_a_stream_with_no_data_never_appears_on_any_row(self):
        conn = _conn(self)
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T08:00:00", 58)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{DIA}T08:00:00", f"{DIA}T08:02:00")
        self.assertEqual(len(out["filas"]), 1)
        fila = out["filas"][0]
        self.assertEqual(fila["ts"], f"{DIA}T08:00")
        self.assertEqual(fila["fc"], 58.0)
        for clave in ("estres", "etapa", "vfc", "movimiento", "body_battery"):
            self.assertNotIn(clave, fila)

    def test_movimiento_is_a_minute_exact_join(self):
        conn = _conn(self)
        _sesion(conn, 1, DIA, f"{PREV}T23:00:00", f"{DIA}T06:00:00")
        _guardar_sueno(conn, 1, DIA, movimiento=[(f"{DIA}T02:00:00", 1.2)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{DIA}T01:59:00", f"{DIA}T02:02:00", metricas=["movimiento"])
        filas = {f["ts"]: f.get("movimiento") for f in out["filas"]}
        self.assertEqual(filas, {f"{DIA}T02:00": 1.2})


class BoundsTest(unittest.TestCase):
    def test_window_over_360_minutes_is_refused(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError) as ctx:
            _ejecutar(conn, 1, f"{DIA}T00:00:00", f"{DIA}T06:01:00")
        self.assertIn(str(herramientas._LEER_SERIE_MAX_MIN_PASO_MINUTO), str(ctx.exception))

    def test_exactly_360_minutes_is_allowed(self):
        conn = _conn(self)
        conn.commit()
        out = _ejecutar(conn, 1, f"{DIA}T00:00:00", f"{DIA}T06:00:00", metricas=["fc"])
        self.assertEqual(out["filas"], [])

    def test_hasta_before_or_equal_desde_is_refused(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, f"{DIA}T06:00:00", f"{DIA}T06:00:00")

    def test_a_future_window_is_refused(self):
        conn = _conn(self)
        conn.commit()
        futuro = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:00")
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, futuro, (datetime.fromisoformat(futuro) + timedelta(minutes=5)).isoformat())

    def test_malformed_timestamp_is_refused(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, "not-a-timestamp", f"{DIA}T00:10:00")

    def test_unknown_metric_for_paso_minuto_is_refused(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, f"{DIA}T00:00:00", f"{DIA}T00:10:00", metricas=["resting_hr"])


class NivelDatosTest(unittest.TestCase):
    def test_paso_minuto_refused_at_nivel_resumen(self):
        conn = _conn(self)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, 1, f"{DIA}T00:00:00", f"{DIA}T00:10:00", nivel="resumen")


class OtraPersonaInvisibleTest(unittest.TestCase):
    def test_another_persons_minutes_never_appear(self):
        conn = _conn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        _guardar_fc(conn, 1, DIA, [(f"{DIA}T08:00:00", 58)])
        _guardar_fc(conn, 2, DIA, [(f"{DIA}T08:00:00", 999)])
        conn.commit()
        out = _ejecutar(conn, 1, f"{DIA}T08:00:00", f"{DIA}T08:01:00", metricas=["fc"])
        self.assertEqual(len(out["filas"]), 1)
        self.assertEqual(out["filas"][0]["fc"], 58.0)


class StatementCountTest(unittest.TestCase):
    def _contar(self, conn, fn) -> int:
        vistos = []
        conn.set_trace_callback(lambda sql: vistos.append(sql))
        try:
            fn()
        finally:
            conn.set_trace_callback(None)
        return len(vistos)

    def test_statement_count_is_independent_of_window_length(self):
        conn = _conn(self)
        conn.commit()

        def corto():
            _ejecutar(conn, 1, f"{DIA}T08:00:00", f"{DIA}T08:10:00")

        def largo():
            _ejecutar(conn, 1, f"{DIA}T08:00:00", f"{DIA}T14:00:00")  # the full 360-minute cap

        n_corto = self._contar(conn, corto)
        n_largo = self._contar(conn, largo)
        self.assertEqual(n_corto, n_largo)
        # ejecutar()'s own query_only PRAGMA (2 statements, plus the fetchone that reads the first
        # one back) + person + fc + estres + body_battery + the sleep_session IN-clause lookup --
        # no night found, so no further sueno_minuto_a_minuto statement either way.
        self.assertEqual(n_corto, 8)


if __name__ == "__main__":
    unittest.main()
