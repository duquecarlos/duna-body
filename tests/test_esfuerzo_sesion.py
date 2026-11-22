"""BODY-PARTIDO-1: the session card's pure model -- body/esfuerzo_sesion.py. Invented fixtures
only, never a real figure.

1. Ruling 2: per-minute folding, sort order, the activity's own window.
2. Ruling 3: peaks (threshold, local max, the 3-minute separation), drops (contiguous next minute
   only), the median recovery over at least 3 drops, the honest gap under minutos_min/cobertura_min.
3. Ruling 4: zone minutes and the zones-4/5 share; an empty/never-stored marker is no zone row.
4. Ruling 5: the comparable-session selection matches entreno_sesion.comparaciones exactly; the
   historical median per figure, gated on HISTORIAL_MIN; the three-way classification bands.
5. Ruling 7: the activity-level curve fallback shape.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, entreno_sesion, esfuerzo_sesion

import tempfile
from pathlib import Path


def _serie_continua(start: str, valores: list[float]) -> tuple[tuple[str, float], ...]:
    """A per-minute series with no gaps, one value per consecutive minute from `start`."""
    t0 = datetime.fromisoformat(start)
    return tuple(((t0 + timedelta(minutes=i)).isoformat(), v) for i, v in enumerate(valores))


class SeriePorMinutoTest(unittest.TestCase):
    """Ruling 2."""

    def test_samples_sharing_a_minute_fold_to_their_mean(self):
        muestras = (("2026-11-20T18:00", 100.0), ("2026-11-20T18:00", 110.0), ("2026-11-20T18:01", 120.0))
        serie = esfuerzo_sesion.serie_por_minuto(muestras, "2026-11-20T18:00", 120.0)
        self.assertEqual(serie, [(datetime(2026, 11, 20, 18, 0), 105.0), (datetime(2026, 11, 20, 18, 1), 120.0)])

    def test_sorted_by_time_even_when_the_input_is_not(self):
        muestras = (("2026-11-20T18:02", 100.0), ("2026-11-20T18:00", 90.0))
        serie = esfuerzo_sesion.serie_por_minuto(muestras, "2026-11-20T18:00", 180.0)
        self.assertEqual([t for t, _ in serie], [datetime(2026, 11, 20, 18, 0), datetime(2026, 11, 20, 18, 2)])

    def test_restricted_to_the_activitys_own_window(self):
        muestras = (("2026-11-20T17:59", 80.0), ("2026-11-20T18:00", 100.0),
                    ("2026-11-20T18:02", 110.0), ("2026-11-20T18:03", 90.0))
        serie = esfuerzo_sesion.serie_por_minuto(muestras, "2026-11-20T18:00", 120.0)
        self.assertEqual([t for t, _ in serie], [datetime(2026, 11, 20, 18, 0), datetime(2026, 11, 20, 18, 2)])

    def test_no_start_local_or_no_duration_is_empty(self):
        muestras = (("2026-11-20T18:00", 1.0),)
        self.assertEqual(esfuerzo_sesion.serie_por_minuto(muestras, None, 60.0), [])
        self.assertEqual(esfuerzo_sesion.serie_por_minuto(muestras, "2026-11-20T18:00", None), [])
        self.assertEqual(esfuerzo_sesion.serie_por_minuto((), "2026-11-20T18:00", 60.0), [])


# Minutes 0-13 (14 points, full coverage): three well-separated peaks (5, 8, 11 -- each exactly
# MODELO['separacion_min'] apart, so none collapse), each with a one-minute drop after it.
_TRES_PICOS = [100, 100, 100, 100, 100, 155, 100, 100, 180, 100, 100, 170, 100, 100]


class EsfuerzosPicosTest(unittest.TestCase):
    """Ruling 3: peaks and the drop after each one."""

    def test_three_well_separated_peaks_and_the_median_of_three_drops(self):
        muestras = _serie_continua("2026-11-20T18:00", _TRES_PICOS)
        r = esfuerzo_sesion.esfuerzos(muestras, "2026-11-20T18:00", 13 * 60.0)
        self.assertTrue(r["suficiente"])
        self.assertEqual(r["n_picos"], 3)
        self.assertEqual(r["pico"], 180.0)
        self.assertEqual(r["recuperacion"], 70.0)  # median(55, 80, 70)

    def test_close_peaks_collapse_to_the_higher_one(self):
        # Minute 3 (150) and minute 5 (155) are 2 minutes apart -- closer than separacion_min (3)
        # -- so only the higher one (155) survives.
        valores = [100, 100, 100, 150, 100, 155, 100, 100, 100, 100]
        muestras = _serie_continua("2026-11-20T18:00", valores)
        r = esfuerzo_sesion.esfuerzos(muestras, "2026-11-20T18:00", 9 * 60.0)
        self.assertTrue(r["suficiente"])
        self.assertEqual(r["n_picos"], 1)
        self.assertEqual(r["pico"], 155.0)

    def test_drop_only_counted_when_the_next_minute_is_present_and_contiguous(self):
        # Same series as the three-peaks test above, but minute 9 (the sample right after the
        # middle peak at minute 8) is missing -- that one peak's own drop is no longer counted,
        # while the other two (whose next minute is still present) are unaffected. Peak detection
        # itself is unaffected (the local-max check at minute 8 still passes: its own "next"
        # comparison is skipped, not failed, once minute 9 is gone -- ruling 3's own "both present
        # and contiguous").
        t0 = datetime.fromisoformat("2026-11-20T18:00")
        muestras = tuple(((t0 + timedelta(minutes=i)).isoformat(), v)
                          for i, v in enumerate(_TRES_PICOS) if i != 9)
        r = esfuerzo_sesion.esfuerzos(muestras, "2026-11-20T18:00", 13 * 60.0)
        self.assertTrue(r["suficiente"])
        self.assertEqual(r["n_picos"], 3)
        self.assertEqual(r["pico"], 180.0)
        self.assertIsNone(r["recuperacion"])  # only 2 of the 3 drops are left -- under caidas_min


class EsfuerzosHonestGapTest(unittest.TestCase):
    """Ruling 3's own honest gap -- no peak, no drop, no recovery figure, never a guess."""

    def test_under_minutos_min_is_the_honest_gap(self):
        valores = [100.0] * 9  # 9 minutes, under MODELO['minutos_min'] (10)
        muestras = _serie_continua("2026-11-20T18:00", valores)
        r = esfuerzo_sesion.esfuerzos(muestras, "2026-11-20T18:00", 8 * 60.0)
        self.assertEqual(r, {"suficiente": False, "n_picos": 0, "pico": None, "recuperacion": None})

    def test_under_cobertura_min_is_the_honest_gap(self):
        # 10 samples, one every other minute over a 19-minute span: coverage 10/19 ~= 0.53, under
        # MODELO['cobertura_min'] (0.6) -- even though the minute COUNT alone clears minutos_min.
        t0 = datetime.fromisoformat("2026-11-20T18:00")
        muestras = tuple(((t0 + timedelta(minutes=2 * i)).isoformat(), 100.0 + i) for i in range(10))
        r = esfuerzo_sesion.esfuerzos(muestras, "2026-11-20T18:00", 19 * 60.0)
        self.assertEqual(r, {"suficiente": False, "n_picos": 0, "pico": None, "recuperacion": None})

    def test_no_muestras_is_the_honest_gap(self):
        r = esfuerzo_sesion.esfuerzos((), "2026-11-20T18:00", 600.0)
        self.assertFalse(r["suficiente"])


class ZonasTest(unittest.TestCase):
    """Ruling 4."""

    def test_empty_or_never_stored_is_no_zone_row(self):
        self.assertIsNone(esfuerzo_sesion.zonas(()))

    def test_minutes_per_zone_and_the_high_share(self):
        filas = ((1, 120.0, 90.0), (2, 300.0, 110.0), (3, 900.0, 130.0), (4, 600.0, 150.0), (5, 180.0, 165.0))
        z = esfuerzo_sesion.zonas(filas)
        self.assertEqual(z["minutos"], {1: 2.0, 2: 5.0, 3: 15.0, 4: 10.0, 5: 3.0})
        self.assertEqual(z["total_min"], 35.0)
        self.assertEqual(z["alta_min"], 13.0)
        self.assertAlmostEqual(z["alta_share"], 13.0 / 35.0)

    def test_zero_total_gives_no_share(self):
        z = esfuerzo_sesion.zonas(((1, 0.0, 90.0),))
        self.assertIsNone(z["alta_share"])


class ClasificaTest(unittest.TestCase):
    """Ruling 5: the three-way band classification, pure."""

    def test_within_the_band_is_igual(self):
        c = esfuerzo_sesion.clasifica({"zona_alta": 0.50, "caida": 20.0},
                                       {"zona_alta": 0.48, "caida": 19.0})
        self.assertEqual(c, {"zona_alta": "igual", "caida": "igual"})

    def test_above_the_band_is_mas(self):
        c = esfuerzo_sesion.clasifica({"zona_alta": 0.60, "caida": 30.0},
                                       {"zona_alta": 0.40, "caida": 10.0})
        self.assertEqual(c, {"zona_alta": "mas", "caida": "mas"})

    def test_below_the_band_is_menos(self):
        c = esfuerzo_sesion.clasifica({"zona_alta": 0.30, "caida": 5.0},
                                       {"zona_alta": 0.50, "caida": 20.0})
        self.assertEqual(c, {"zona_alta": "menos", "caida": "menos"})

    def test_missing_either_side_gives_no_classification(self):
        c = esfuerzo_sesion.clasifica({"zona_alta": None, "caida": None},
                                       {"zona_alta": 0.4, "caida": 20.0})
        self.assertEqual(c, {"zona_alta": None, "caida": None})


def _reset_db(tc: unittest.TestCase, name: str):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    db.local_person_id(conn)
    return conn


def _activity(conn, person_id: int, source_id: str, day: str, start_local: str, type_: str, name: str | None,
              duration_s: float, avg_hr: float | None = None, max_hr: float | None = None,
              calories: float | None = None, source: str = "garmin") -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (person_id, source_id, day, start_local, type_, name, duration_s, avg_hr, max_hr, calories, source),
    )
    conn.commit()


def _detalle_payload(start_local: str, valores: list[float]) -> dict:
    t0 = datetime.fromisoformat(start_local)
    # America/Bogota is UTC-5 year round -- the same convention demo.py's own _muestra_ms uses.
    ms0 = int((t0 + timedelta(hours=5)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return {
        "metricDescriptors": [{"key": "directTimestamp", "index": 0}, {"key": "directHeartRate", "index": 1}],
        "activityDetailMetrics": [{"metrics": [ms0 + i * 60_000, v]} for i, v in enumerate(valores)],
    }


def _zonas_payload(minutos: dict[int, float]) -> list[dict]:
    return [{"zoneNumber": n, "secsInZone": m * 60.0, "zoneLowBoundary": 100.0 + 10 * n}
            for n, m in minutos.items()]


class SesionesComparablesTest(unittest.TestCase):
    """Ruling 5: the SAME selection entreno_sesion.comparaciones already makes."""

    def setUp(self):
        self.conn = _reset_db(self, "comparables.db")

    def test_matches_comparaciones_for_a_named_type(self):
        for i, day in enumerate(("2026-11-01", "2026-11-03", "2026-11-05")):
            _activity(self.conn, 1, f"h{i}", day, f"{day}T18:00:00", "running", None, 1800.0, avg_hr=140)
        _activity(self.conn, 1, "hoy", "2026-11-10", "2026-11-10T18:00:00", "running", None, 1800.0, avg_hr=145)
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "hoy")
        filas = esfuerzo_sesion._sesiones_comparables(self.conn, 1, a)
        comp = entreno_sesion.comparaciones(self.conn, 1, a)
        self.assertEqual(len(filas), 3)
        self.assertEqual(comp["n"], 3)
        self.assertEqual({f["source_id"] for f in filas}, {"h0", "h1", "h2"})

    def test_matches_comparaciones_for_the_generic_other_bucket_by_folded_name(self):
        for i, (day, nombre) in enumerate((
                ("2026-11-01", "baloncesto"), ("2026-11-03", "BALONCESTO"), ("2026-11-05", "Yoga"))):
            _activity(self.conn, 1, f"h{i}", day, f"{day}T18:00:00", "other", nombre, 3000.0, avg_hr=120)
        _activity(self.conn, 1, "hoy", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 3000.0,
                  avg_hr=130)
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "hoy")
        filas = esfuerzo_sesion._sesiones_comparables(self.conn, 1, a)
        comp = entreno_sesion.comparaciones(self.conn, 1, a)
        self.assertEqual({f["source_id"] for f in filas}, {"h0", "h1"})  # "Yoga" excluded
        self.assertEqual(comp["n"], 2)

    def test_no_type_is_no_comparable_sessions(self):
        _activity(self.conn, 1, "manual", "2026-11-10", "2026-11-10T18:00:00", None, "A mano", 1800.0)
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "manual")
        self.assertEqual(esfuerzo_sesion._sesiones_comparables(self.conn, 1, a), [])


class HistorialTest(unittest.TestCase):
    """Ruling 5: the historical median per figure, gated on HISTORIAL_MIN, read in one batched
    query rather than one per comparable session."""

    def setUp(self):
        self.conn = _reset_db(self, "historial.db")

    def _sembrar_partido(self, source_id: str, day: str, alta_min: float, pico: float) -> None:
        _activity(self.conn, 1, source_id, day, f"{day}T18:00:00", "other", "Baloncesto", 12 * 60.0,
                  avg_hr=120)
        # A single, well-separated peak high above the baseline (so esfuerzos() reports exactly
        # that pico), full per-minute coverage over 12 minutes (comfortably clears minutos_min/
        # cobertura_min). zone4+zone5 sum to exactly `alta_min`, zone1+zone2 a fixed 4, zone3 the
        # remainder -- always summing to the full 12 minutes.
        valores = [100.0] * 5 + [pico] + [100.0] * 6
        db.guardar_detalle_actividad(self.conn, 1, "garmin", source_id, "detalle",
                                      _detalle_payload(f"{day}T18:00:00", valores))
        db.guardar_detalle_actividad(self.conn, 1, "garmin", source_id, "zonas", _zonas_payload({
            1: 2.0, 2: 2.0, 3: 12.0 - alta_min - 4.0, 4: alta_min / 2, 5: alta_min / 2,
        }))
        self.conn.commit()

    def test_fewer_than_historial_min_gives_no_comparison_for_any_figure(self):
        self._sembrar_partido("p1", "2026-11-01", alta_min=6.0, pico=160.0)
        self._sembrar_partido("p2", "2026-11-03", alta_min=6.0, pico=160.0)  # only 2 -- under HISTORIAL_MIN (3)
        _activity(self.conn, 1, "hoy", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 3000.0)
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "hoy")
        h = esfuerzo_sesion.historial(self.conn, 1, a)
        self.assertEqual(h, {"zona_alta": None, "caida": None})

    def test_historial_min_sessions_gives_a_median_per_figure(self):
        self._sembrar_partido("p1", "2026-11-01", alta_min=4.0, pico=150.0)
        self._sembrar_partido("p2", "2026-11-03", alta_min=6.0, pico=160.0)
        self._sembrar_partido("p3", "2026-11-05", alta_min=8.0, pico=170.0)
        _activity(self.conn, 1, "hoy", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 3000.0)
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "hoy")
        h = esfuerzo_sesion.historial(self.conn, 1, a)
        self.assertAlmostEqual(h["zona_alta"], 6.0 / 12.0)  # median alta share (4,6,8)/12 -> 6/12
        # Each seeded session's own curve gives too few measured drops (caidas_min needs 3, this
        # fixture's own shape gives fewer) for a recovery figure -- so "caida" has no comparison
        # either, honestly.
        self.assertIsNone(h["caida"])

    def test_no_comparable_sessions_at_all(self):
        _activity(self.conn, 1, "sola", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 3000.0)
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "sola")
        self.assertEqual(esfuerzo_sesion.historial(self.conn, 1, a),
                          {"zona_alta": None, "caida": None})


class TarjetaTest(unittest.TestCase):
    """Ruling 1/6: the card's own presence gate."""

    def setUp(self):
        self.conn = _reset_db(self, "tarjeta.db")

    def test_no_detalle_and_no_zonas_is_no_card(self):
        _activity(self.conn, 1, "a1", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 3000.0)
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "a1")
        self.assertIsNone(esfuerzo_sesion.tarjeta(self.conn, 1, a))

    def test_zonas_alone_still_gives_a_card(self):
        _activity(self.conn, 1, "a1", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 3000.0)
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "zonas",
                                      _zonas_payload({1: 5.0, 2: 10.0, 3: 20.0, 4: 10.0, 5: 5.0}))
        self.conn.commit()
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "a1")
        tarjeta = esfuerzo_sesion.tarjeta(self.conn, 1, a)
        self.assertIsNotNone(tarjeta)
        self.assertFalse(tarjeta["tiene_detalle"])
        self.assertIsNotNone(tarjeta["zonas"])

    def test_detalle_stored_but_insufficient_still_gives_a_card_with_the_honest_gap(self):
        _activity(self.conn, 1, "a1", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 300.0)
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "detalle",
                                      _detalle_payload("2026-11-10T18:00:00", [100.0] * 4))
        self.conn.commit()
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "a1")
        tarjeta = esfuerzo_sesion.tarjeta(self.conn, 1, a)
        self.assertIsNotNone(tarjeta)
        self.assertTrue(tarjeta["tiene_detalle"])
        self.assertFalse(tarjeta["esfuerzos"]["suficiente"])


class CurvaActividadTest(unittest.TestCase):
    """Ruling 7."""

    def setUp(self):
        self.conn = _reset_db(self, "curva_actividad.db")

    def test_none_without_stored_detalle(self):
        _activity(self.conn, 1, "a1", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 600.0)
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "a1")
        self.assertIsNone(esfuerzo_sesion.curva_actividad(self.conn, 1, a))

    def test_the_same_shape_entreno_sesion_curva_returns_with_empty_picos_and_caidas(self):
        _activity(self.conn, 1, "a1", "2026-11-10", "2026-11-10T18:00:00", "other", "Baloncesto", 300.0)
        db.guardar_detalle_actividad(self.conn, 1, "garmin", "a1", "detalle",
                                      _detalle_payload("2026-11-10T18:00:00", [100.0, 110.0, 120.0, 105.0, 95.0]))
        self.conn.commit()
        a = entreno_sesion.actividad(self.conn, 1, "2026-11-10", "a1")
        ctx = esfuerzo_sesion.curva_actividad(self.conn, 1, a)
        self.assertEqual(ctx["min"], 95.0)
        self.assertEqual(ctx["max"], 120.0)
        self.assertEqual(ctx["picos"], {})
        self.assertEqual(ctx["caidas"], {})
        self.assertEqual(len(ctx["puntos"]), 5)
        self.assertEqual(ctx["inicio"], datetime(2026, 11, 10, 18, 0))
        self.assertEqual(ctx["fin"], datetime(2026, 11, 10, 18, 5))


if __name__ == "__main__":
    unittest.main()
