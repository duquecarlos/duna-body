"""ORCH-CARLOS-DELEGATES-TIER-1-1 (BODY-PARTIDO-1-ASIS): `leer_entreno` widens from strength/manual
sessions to every session of the person, each carrying the SAME effort summary
`body/esfuerzo_sesion.py`'s own `tarjeta` computes for the per-session page -- read only from that
function's return, never recomputed. Invented fixtures only, never a real figure.

1. Ruling 1: every session appears, any sport; strength-only fields (series/ultima_vez/dolor) stay
   real for a strength/manual session, present and honestly empty for any other.
2. Ruling 2: `esfuerzo`, figure for figure against `esfuerzo_sesion.tarjeta`'s own return, carried
   for a strength session too (the page's own deliberate difference).
3. Ruling 3: never the raw curve -- no list longer than the zone count, no timestamp besides the
   session's own `hora_inicio`.
6. Ruling 7: another person's sessions never appear; a linked pair still folds to one.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, entreno_sesion, esfuerzo_sesion, garmin_adapter
from body.agente import herramientas


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _activity(conn, person_id: int, source_id: str, day: str, start_local: str, type_: str | None,
              name: str | None, duration_s: float, source: str = "garmin") -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
        (person_id, source_id, day, start_local, type_, name, duration_s, source),
    )


def _detalle_payload(start_local: str, valores: list[float]) -> dict:
    # Same convention as tests/test_esfuerzo_sesion.py's own fixture: America/Bogota is UTC-5 year
    # round.
    t0 = datetime.fromisoformat(start_local)
    ms0 = int((t0 + timedelta(hours=5)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return {
        "metricDescriptors": [{"key": "directTimestamp", "metricsIndex": 0},
                               {"key": "directHeartRate", "metricsIndex": 1}],
        "activityDetailMetrics": [{"metrics": [ms0 + i * 60_000, v]} for i, v in enumerate(valores)],
    }


def _zonas_payload(minutos: dict[int, float]) -> list[dict]:
    return [{"zoneNumber": n, "secsInZone": m * 60.0, "zoneLowBoundary": 100.0 + 10 * n}
            for n, m in minutos.items()]


def _watch_day_data(day: str, source_id: str = "w1", avg_hr: float = 120.0, duration_s: float = 1800.0,
                     name: str | None = None) -> garmin_adapter.DayData:
    return garmin_adapter.DayData(
        day=day,
        activities=[{"source_id": source_id, "start_local": f"{day}T18:00:00", "type": "strength_training",
                     "name": name, "duration_s": duration_s, "distance_m": None, "avg_hr": avg_hr,
                     "max_hr": avg_hr + 20, "calories": 300.0}],
    )


# Minutes 0-13 (14 points, full coverage): three well-separated peaks, the same shape
# tests/test_esfuerzo_sesion.py's own EsfuerzosPicosTest fixture uses.
_TRES_PICOS = [100, 100, 100, 100, 100, 155, 100, 100, 180, 100, 100, 170, 100, 100]


def _assert_no_raw_curve(tc: unittest.TestCase, node, hora_inicio: str) -> None:
    """Ruling 3's own walk: no list longer than the zone count (5), no ISO timestamp besides the
    session's own `hora_inicio`."""
    if isinstance(node, dict):
        for v in node.values():
            _assert_no_raw_curve(tc, v, hora_inicio)
    elif isinstance(node, list):
        tc.assertLessEqual(len(node), 5, f"a list longer than the zone count leaked: {node!r}")
        for v in node:
            _assert_no_raw_curve(tc, v, hora_inicio)
    elif isinstance(node, str):
        try:
            datetime.fromisoformat(node)
        except ValueError:
            return
        tc.assertEqual(node, hora_inicio, f"a timestamp other than the session's own hora_inicio leaked: {node!r}")


class OtroDeporteConEsfuerzoTest(unittest.TestCase):
    """Ruling 1/2: a basketball ('other') session reads with the strength-only fields honestly
    empty, and with `esfuerzo` figure for figure against `esfuerzo_sesion.tarjeta`'s own return."""

    def test_basketball_session_matches_tarjeta_figure_for_figure(self):
        conn = _conn(self)
        _activity(conn, 1, "b1", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 13 * 60.0)
        db.guardar_detalle_actividad(conn, 1, "garmin", "b1", "detalle",
                                      _detalle_payload("2026-11-10T18:00:00", _TRES_PICOS))
        db.guardar_detalle_actividad(conn, 1, "garmin", "b1", "zonas",
                                      _zonas_payload({1: 2.0, 2: 2.0, 3: 4.0, 4: 3.0, 5: 2.0}))
        conn.commit()

        out = herramientas.ejecutar(conn, 1, "leer_entreno", {"day": "2026-11-10"}, "completo")
        self.assertEqual(len(out["sesiones"]), 1)
        sesion = out["sesiones"][0]
        self.assertEqual(sesion["tipo"], "other")
        self.assertAlmostEqual(sesion["duracion_min"], 13.0)
        self.assertEqual(sesion["series"], [])
        self.assertEqual(sesion["ultima_vez"], {})
        self.assertEqual(sesion["dolor"], [])

        a = entreno_sesion.actividad(conn, 1, "2026-11-10", "b1")
        tarjeta = esfuerzo_sesion.tarjeta(conn, 1, a)
        esf, zon, clasif = tarjeta["esfuerzos"], tarjeta["zonas"], tarjeta["clasificacion"]

        esfuerzo = sesion["esfuerzo"]
        self.assertIsNotNone(esfuerzo)
        self.assertEqual(esfuerzo["modelo"], esfuerzo_sesion.VERSION)
        self.assertEqual(esfuerzo["zonas_min"],
                          {n: zon["minutos"][n] for n in range(1, 6) if zon["minutos"][n] > 0})
        self.assertAlmostEqual(esfuerzo["zona_alta_pct"], zon["alta_share"] * 100)
        self.assertEqual(esfuerzo["esfuerzos_fuertes"], esf["n_picos"])
        self.assertEqual(esfuerzo["caida_1min_ppm"], esf["recuperacion"] if esf["suficiente"] else None)
        self.assertEqual(esfuerzo["datos_suficientes"], esf["suficiente"])
        self.assertEqual(esfuerzo["frente_a_tu_habitual"]["zona_alta"],
                          {"mas": "mas", "menos": "menos", "igual": "como"}.get(clasif["zona_alta"]))
        self.assertEqual(esfuerzo["frente_a_tu_habitual"]["recuperacion"],
                          {"mas": "mas", "menos": "menos", "igual": "como"}.get(clasif["caida"]))
        self.assertNotIn("sesiones_comparadas", esfuerzo)
        self.assertEqual(esfuerzo["comparadas"],
                          {"zona_alta": tarjeta["n"]["zona_alta"], "recuperacion": tarjeta["n"]["caida"]})
        self.assertEqual(esfuerzo["minimo_para_comparar"], esfuerzo_sesion.HISTORIAL_MIN)
        self.assertEqual(esfuerzo["detalle_guardado"], tarjeta["tiene_detalle"])
        _assert_no_raw_curve(self, esfuerzo, sesion["hora_inicio"])


class RunSinDetalleTest(unittest.TestCase):
    """Ruling 1/2: a run with no stored detail at all reads `esfuerzo: None`, never a guess."""

    def test_run_with_no_stored_detail_reads_esfuerzo_none(self):
        conn = _conn(self)
        _activity(conn, 1, "r1", "2026-11-11", "2026-11-11T06:00:00", "running", None, 1800.0)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_entreno", {"day": "2026-11-11"}, "completo")
        sesion = out["sesiones"][0]
        self.assertEqual(sesion["tipo"], "running")
        self.assertAlmostEqual(sesion["duracion_min"], 30.0)
        self.assertIsNone(sesion["esfuerzo"])
        self.assertEqual(sesion["series"], [])
        self.assertEqual(sesion["dolor"], [])


class FuerzaConEsfuerzoTest(unittest.TestCase):
    """Ruling 2's own deliberate difference from the page: a strength session's sets stay exactly
    what they were, and it still carries `esfuerzo` when detail is stored for it."""

    def test_strength_session_keeps_its_sets_and_gains_esfuerzo(self):
        conn = _conn(self)
        _activity(conn, 1, "s1", "2026-11-12", "2026-11-12T07:00:00", "strength_training", None, 12 * 60.0)
        db.save_exercise_sets(conn, 1, "garmin", "s1", [
            garmin_adapter.ExerciseSet(set_type="active", category="pecho", exercise="press banca",
                                        reps=8.0, weight_value=50.0, weight_unit="kg", duration_s=30.0,
                                        start_local="2026-11-12T07:00:00"),
        ])
        db.guardar_detalle_actividad(conn, 1, "garmin", "s1", "detalle",
                                      _detalle_payload("2026-11-12T07:00:00", [100.0] * 12))
        conn.commit()

        out = herramientas.ejecutar(conn, 1, "leer_entreno", {"day": "2026-11-12"}, "completo")
        sesion = out["sesiones"][0]
        self.assertEqual(sesion["tipo"], "strength_training")
        self.assertEqual(len(sesion["series"]), 1)
        self.assertEqual(sesion["series"][0]["ejercicio"], "press banca")
        self.assertIsNotNone(sesion["esfuerzo"])
        self.assertEqual(sesion["esfuerzo"]["modelo"], esfuerzo_sesion.VERSION)


class EsfuerzoBrechaHonestaTest(unittest.TestCase):
    """Ruling 2/3: too little stored detail is the honest gap, never a guess at the recovery
    figure."""

    def test_too_little_data_leaves_datos_suficientes_false_and_caida_none(self):
        conn = _conn(self)
        _activity(conn, 1, "g1", "2026-11-13", "2026-11-13T18:00:00", "other", "Baloncesto", 300.0)
        db.guardar_detalle_actividad(conn, 1, "garmin", "g1", "detalle",
                                      _detalle_payload("2026-11-13T18:00:00", [100.0] * 4))
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_entreno", {"day": "2026-11-13"}, "completo")
        esfuerzo = out["sesiones"][0]["esfuerzo"]
        self.assertIsNotNone(esfuerzo)
        self.assertFalse(esfuerzo["datos_suficientes"])
        self.assertIsNone(esfuerzo["caida_1min_ppm"])


class OtraPersonaTest(unittest.TestCase):
    """Ruling 7: another person's sessions never appear, even once the type filter is gone."""

    def test_another_persons_sessions_never_appear(self):
        conn = _conn(self)
        _activity(conn, 2, "p2", "2026-11-15", "2026-11-15T18:00:00", "other", "Baloncesto", 3000.0)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_entreno", {"day": "2026-11-15"}, "completo")
        self.assertEqual(out["sesiones"], [])


class ParEnlazadoTest(unittest.TestCase):
    """Ruling 1/7: a linked watch/manual pair still folds to one session, now also carrying
    `esfuerzo` from the watch half's own stored detail."""

    def test_linked_pair_still_folds_to_one_with_esfuerzo(self):
        conn = _conn(self)
        manual_id = db.create_manual_session(conn, 1, "2026-11-16", None, "Fuerza de la tarde")
        db.correct_exercise_set(conn, 1, "manual", manual_id, 1, exercise="sentadilla",
                                 category="pierna", reps=10, weight_kg=60.0)
        db.save_day(conn, _watch_day_data("2026-11-16", avg_hr=133.0, duration_s=2100.0, name="Fuerza"), 1)
        db.guardar_detalle_actividad(conn, 1, "garmin", "w1", "zonas",
                                      _zonas_payload({1: 5.0, 2: 10.0, 3: 10.0, 4: 5.0, 5: 5.0}))
        conn.commit()

        out = herramientas.ejecutar(conn, 1, "leer_entreno", {"day": "2026-11-16"}, "completo")
        self.assertEqual(len(out["sesiones"]), 1)
        sesion = out["sesiones"][0]
        self.assertEqual(sesion["titulo"], "Fuerza de la tarde")
        self.assertEqual([s["ejercicio"] for s in sesion["series"]], ["sentadilla"])
        self.assertIsNotNone(sesion["esfuerzo"])


if __name__ == "__main__":
    unittest.main()
