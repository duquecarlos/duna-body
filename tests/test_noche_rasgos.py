"""BODY-MINUTO-ANALISIS-2: body/noche_rasgos.py's own feature catalogue and `comparar` -- one
night's own features ranked against the person's own history. Invented demo data only, no real
health figures, no network.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, noche_rasgos as nr

ZONA_OFFSET_H = 5  # America/Bogota, the same fixed offset tests/test_leer_noche.py already uses


def _conn(case: unittest.TestCase):
    c = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _guardar_sueno(conn, person_id: int, day: str, hr=(), etapas=(), hrv=()) -> None:
    payload = {}
    if hr:
        payload["sleepHeartRate"] = [{"startGMT": _ms(ts), "value": v} for ts, v in hr]
    if etapas:
        payload["sleepLevels"] = [{"startGMT": _ms(ts), "activityLevel": c} for ts, c in etapas]
    if hrv:
        payload["hrvData"] = [{"startGMT": _ms(ts), "value": v} for ts, v in hrv]
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


# ---- a fully hand-computable night: 7h (420 min), 2026-01-02T23:00 -> 2026-01-03T06:00 ----------

SESION = ("2026-01-02T23:00:00", "2026-01-03T06:00:00")

# 10 heart-rate samples, 42 minutes apart, EVERY value 61..70 used exactly once. The nearest-rank
# 10th percentile over 10 sorted values is index round(0.1*9)=1 -- the SECOND-lowest value (62),
# which this layout places at offset 378 (the 9th sample), not at the true minimum (61, offset 168)
# -- the "apples to oranges" distinction the module docstring names (a robust low is a RANK, not
# the single lowest sample).
_HR = [
    ("2026-01-02T23:00:00", 70), ("2026-01-02T23:42:00", 69), ("2026-01-03T00:24:00", 68),
    ("2026-01-03T01:06:00", 67), ("2026-01-03T01:48:00", 61), ("2026-01-03T02:30:00", 66),
    ("2026-01-03T03:12:00", 65), ("2026-01-03T03:54:00", 64), ("2026-01-03T04:36:00", 63),
    ("2026-01-03T05:18:00", 62),
]

# Stages: profundo (0) [30,150) and [160,280), each 120 min (max block = 120); despierto (3)
# [150,160), [280,290), [400,420) = 10+10+20 = 40 min over 3 segments; ligero (1) elsewhere.
_ETAPAS = [
    ("2026-01-02T23:00:00", 1), ("2026-01-02T23:30:00", 0), ("2026-01-03T01:30:00", 3),
    ("2026-01-03T01:40:00", 0), ("2026-01-03T03:40:00", 3), ("2026-01-03T03:50:00", 1),
    ("2026-01-03T05:40:00", 3),
]

# HRV: first half (< 210 min = 03:30) mean (40+50)/2=45; second half mean (60+80)/2=70.
_HRV = [
    ("2026-01-02T23:50:00", 40), ("2026-01-03T00:40:00", 50),
    ("2026-01-03T03:10:00", 60), ("2026-01-03T04:00:00", 80),
]


class RasgosFormaHandComputedTest(unittest.TestCase):
    """Pure unit-level checks on `_rasgos_forma`, no database -- every figure hand-computed in
    this module's own comment above, so a drift in the formula fails loudly here."""

    def setUp(self):
        arrays = {"heart_rate": tuple(_HR), "stages": tuple(_ETAPAS), "hrv": tuple(_HRV)}
        self.out = nr._rasgos_forma(SESION, arrays)

    def test_onset_and_wake_minutes_since_noon_before(self):
        # 23:00 is 11h (660 min) after that day's own noon; 06:00 is 18h (1080 min) after the
        # PREVIOUS day's noon (06:00 < 12:00) -- 1080 - 660 = 420 = the night's own 7h length.
        self.assertEqual(self.out["inicio_sueno_min_mediodia"], 660.0)
        self.assertEqual(self.out["fin_sueno_min_mediodia"], 1080.0)

    def test_nadir_fraccion_is_the_robust_low_moment_not_the_single_minimum(self):
        self.assertEqual(self.out["nadir_fraccion"], 0.9)

    def test_first_and_last_third_heart_rate(self):
        self.assertEqual(self.out["fc_primer_tercio"], 68.5)
        self.assertEqual(self.out["fc_ultimo_tercio"], 63.0)

    def test_awake_segments_and_minutes(self):
        self.assertEqual(self.out["segmentos_despierto"], 3)
        self.assertEqual(self.out["minutos_despierto"], 40.0)

    def test_longest_deep_block(self):
        self.assertEqual(self.out["bloque_profundo_max_min"], 120.0)

    def test_hrv_halves(self):
        self.assertEqual(self.out["vfc_primera_mitad"], 45.0)
        self.assertEqual(self.out["vfc_segunda_mitad"], 70.0)

    def test_no_arrays_at_all_leaves_only_onset_and_wake(self):
        out = nr._rasgos_forma(SESION, {})
        self.assertEqual(set(out), {"inicio_sueno_min_mediodia", "fin_sueno_min_mediodia"})


class PercentilRangoTieRuleTest(unittest.TestCase):
    """`_entrada_rasgo`'s own tie rule: the highest value reads 100, the lowest reads 0, and a
    value TIED with the maximum still reads 100 (ties share the higher rank)."""

    def test_max_is_100_min_is_0(self):
        valores = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(nr._entrada_rasgo(valores, 50.0)["percentil"], 100.0)
        self.assertEqual(nr._entrada_rasgo(valores, 10.0)["percentil"], 0.0)

    def test_a_tie_with_the_maximum_still_reads_100(self):
        valores = [10.0, 20.0, 50.0, 50.0]
        self.assertEqual(nr._entrada_rasgo(valores, 50.0)["percentil"], 100.0)

    def test_fewer_than_two_nights_carries_no_percentile(self):
        out = nr._entrada_rasgo([50.0], 50.0)
        self.assertEqual(out, {"valor": 50.0, "n": 1})

    def test_p25_p50_p75_present_with_two_or_more(self):
        out = nr._entrada_rasgo([10.0, 20.0, 30.0, 40.0, 50.0], 30.0)
        self.assertIn("p25", out)
        self.assertIn("p50", out)
        self.assertIn("p75", out)


class EntradaGrupoThresholdTest(unittest.TestCase):
    def test_under_five_nights_carries_no_percentiles(self):
        out = nr._entrada_grupo([1.0, 2.0, 3.0, 4.0], 2.0)
        self.assertEqual(out, {"n_grupo": 4, "motivo": "pocas_noches_grupo"})

    def test_five_or_more_carries_percentiles(self):
        out = nr._entrada_grupo([1.0, 2.0, 3.0, 4.0, 5.0], 5.0)
        self.assertEqual(out["n_grupo"], 5)
        self.assertEqual(out["percentil"], 100.0)


class CoincideGrupoTest(unittest.TestCase):
    def test_tras_turno_matches_the_stored_kind(self):
        self.assertTrue(nr._coincide_grupo({"turno": {"tipo": "night"}}, {"tras_turno": "night"}))
        self.assertFalse(nr._coincide_grupo({"turno": {"tipo": "morning"}}, {"tras_turno": "night"}))
        self.assertFalse(nr._coincide_grupo({}, {"tras_turno": "night"}))

    def test_tras_turno_noche_matches_inside_or_right_after_a_night_block(self):
        self.assertTrue(nr._coincide_grupo({"turno": {"bloque_noche": {"posicion": 1, "largo": 2}}},
                                            {"tras_turno_noche": True}))
        self.assertTrue(nr._coincide_grupo({"turno": {"dias_desde_ultimo_turno_noche": 1}},
                                            {"tras_turno_noche": True}))
        self.assertFalse(nr._coincide_grupo({"turno": {"tipo": "morning"}}, {"tras_turno_noche": True}))
        self.assertTrue(nr._coincide_grupo({}, {"tras_turno_noche": False}))

    def test_viaje_bool(self):
        self.assertTrue(nr._coincide_grupo({"viaje": {"ref": "x", "dia": 1}}, {"viaje": True}))
        self.assertFalse(nr._coincide_grupo({}, {"viaje": True}))
        self.assertTrue(nr._coincide_grupo({}, {"viaje": False}))

    def test_periodo_matches_one_of_the_nights_own_kinds(self):
        entry = {"periodos": [{"kind": "malestar"}, {"kind": "viaje"}]}
        self.assertTrue(nr._coincide_grupo(entry, {"periodo": "viaje"}))
        self.assertFalse(nr._coincide_grupo(entry, {"periodo": "lesion"}))

    def test_comida_tarde_min_max_is_at_most(self):
        entry = {"comida_antes_de_dormir": {"minutos": 180.0}}
        self.assertTrue(nr._coincide_grupo(entry, {"comida_tarde_min_max": 180}))
        self.assertFalse(nr._coincide_grupo(entry, {"comida_tarde_min_max": 179}))
        self.assertFalse(nr._coincide_grupo({}, {"comida_tarde_min_max": 180}))

    def test_con_alcohol_bool(self):
        self.assertTrue(nr._coincide_grupo({"alcohol_g": 20.0}, {"con_alcohol": True}))
        self.assertFalse(nr._coincide_grupo({}, {"con_alcohol": True}))
        self.assertTrue(nr._coincide_grupo({}, {"con_alcohol": False}))


class CompararStoredFiguresTest(unittest.TestCase):
    def test_stored_figures_equal_the_stored_values(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-02-01", "hrv_last_night", 55.0, "garmin", 1)
        db.set_metric(conn, "2026-02-01", "sleep_score", 82.0, "garmin", 1)
        db.set_metric(conn, "2026-02-01", "sleep_deep_min", 90.0, "garmin", 1)
        conn.commit()
        out = nr.comparar(conn, 1, "2026-02-01")
        self.assertEqual(out["rasgos"]["hrv_last_night"]["valor"], 55.0)
        self.assertEqual(out["rasgos"]["sleep_score"]["valor"], 82.0)
        self.assertEqual(out["rasgos"]["sleep_deep_min"]["valor"], 90.0)
        self.assertEqual(out["dia"], "2026-02-01")


class CompararSinMinutosTest(unittest.TestCase):
    def test_a_night_without_minute_data_lacks_shape_keys_and_counts_in_sin_minutos(self):
        conn = _conn(self)
        _sesion(conn, 1, "2026-02-01", "2026-01-31T23:00:00", "2026-02-01T06:00:00")
        db.set_metric(conn, "2026-02-01", "hrv_last_night", 50.0, "garmin", 1)
        conn.commit()
        out = nr.comparar(conn, 1, "2026-02-01")
        self.assertNotIn("nadir_fraccion", out["rasgos"])
        self.assertNotIn("inicio_sueno_min_mediodia", out["rasgos"])
        self.assertEqual(out["noches_sin_minutos"], 1)

    def test_a_night_with_minute_data_does_not_count(self):
        conn = _conn(self)
        _sesion(conn, 1, "2026-01-03", SESION[0], SESION[1])
        _guardar_sueno(conn, 1, "2026-01-03", hr=_HR, etapas=_ETAPAS, hrv=_HRV)
        conn.commit()
        out = nr.comparar(conn, 1, "2026-01-03")
        self.assertIn("nadir_fraccion", out["rasgos"])
        self.assertEqual(out["noches_sin_minutos"], 0)


class CompararPercentilSobreHistoriaTest(unittest.TestCase):
    def test_percentile_of_max_is_100_and_min_is_0_over_real_history(self):
        # `comparar`'s own range is [desde, fecha] -- the FOCAL night is always the range's own
        # last day, so the night holding the MAXIMUM must be the most recent of an INCREASING
        # run, and the night holding the MINIMUM the most recent of a DECREASING one; each run
        # bounded by its own explicit `desde` so the two never mix.
        conn = _conn(self)
        crecientes = [40.0, 45.0, 50.0, 55.0, 60.0]
        for i, v in enumerate(crecientes):
            db.set_metric(conn, f"2026-03-{i + 1:02d}", "hrv_last_night", v, "garmin", 1)
        decrecientes = [60.0, 55.0, 50.0, 45.0, 40.0]
        for i, v in enumerate(decrecientes):
            db.set_metric(conn, f"2026-04-{i + 1:02d}", "hrv_last_night", v, "garmin", 1)
        conn.commit()
        out_max = nr.comparar(conn, 1, "2026-03-05", desde="2026-03-01")  # 60.0, the highest
        out_min = nr.comparar(conn, 1, "2026-04-05", desde="2026-04-01")  # 40.0, the lowest
        self.assertEqual(out_max["rasgos"]["hrv_last_night"]["percentil"], 100.0)
        self.assertEqual(out_max["rasgos"]["hrv_last_night"]["n"], 5)
        self.assertEqual(out_min["rasgos"]["hrv_last_night"]["percentil"], 0.0)
        self.assertEqual(out_min["rasgos"]["hrv_last_night"]["n"], 5)


class CompararGrupoTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-2 ruling 2's three named examples -- a night after a night shift, a
    trip night, a late-meal night -- each isolated by its own `grupo` filter, with the other
    nights (seeded as plain weekdays with no shift/trip/late meal) excluded. Five matching nights
    each, the minimum this module's own `MIN_NOCHES_GRUPO` requires for a real percentile."""

    def setUp(self):
        self.conn = _conn(self)
        # Five "after a night shift" mornings: a night shift the evening before (day i), this
        # module's own grupo match on tras_turno/tras_turno_noche reads the EVENING's own shift.
        for i in range(5):
            evening = f"2026-04-{i + 1:02d}"
            wake = f"2026-04-{i + 2:02d}"
            self.conn.execute(
                "INSERT INTO shift (person_id, day, kind, start_local, source) VALUES "
                "(1, ?, 'night', ?, 'manual')", (evening, f"{evening}T22:00:00"))
            db.set_metric(self.conn, wake, "hrv_last_night", 40.0 + i, "garmin", 1)
        # Five plain nights, no shift, no trip, no late meal -- the "everything else" control.
        for i in range(5):
            wake = f"2026-05-{i + 1:02d}"
            db.set_metric(self.conn, wake, "hrv_last_night", 60.0 + i, "garmin", 1)
        # Five trip nights (a Lugares trip of 3 days covering the wake day).
        self.conn.execute(
            "INSERT INTO viaje_lugares (person_id, ref, starts_at, ends_at, fetched_at) VALUES "
            "(1, 'zz-viaje', '2026-06-01', '2026-06-05', '2026-01-01T00:00:00+00:00')")
        for i in range(5):
            wake = f"2026-06-{i + 1:02d}"
            db.set_metric(self.conn, wake, "hrv_last_night", 70.0 + i, "garmin", 1)
        self.conn.commit()

    def test_tras_turno_noche_group_excludes_plain_nights(self):
        out = nr.comparar(self.conn, 1, "2026-04-06", desde="2026-04-01",
                           grupo={"tras_turno_noche": True})
        grupo = out["rasgos"]["hrv_last_night"]["grupo"]
        self.assertEqual(grupo["n_grupo"], 5)

    def test_tras_turno_kind_group(self):
        out = nr.comparar(self.conn, 1, "2026-04-06", desde="2026-04-01",
                           grupo={"tras_turno": "night"})
        self.assertEqual(out["rasgos"]["hrv_last_night"]["grupo"]["n_grupo"], 5)

    def test_viaje_group_selects_only_the_trip_nights(self):
        out = nr.comparar(self.conn, 1, "2026-06-05", desde="2026-04-01", grupo={"viaje": True})
        self.assertEqual(out["rasgos"]["hrv_last_night"]["grupo"]["n_grupo"], 5)

    def test_group_under_five_carries_no_percentiles(self):
        out = nr.comparar(self.conn, 1, "2026-06-05", desde="2026-06-03", grupo={"viaje": True})
        grupo = out["rasgos"]["hrv_last_night"]["grupo"]
        self.assertIn("motivo", grupo)
        self.assertNotIn("percentil", grupo)


class CompararOtraPersonaTest(unittest.TestCase):
    def test_another_persons_nights_are_never_counted(self):
        conn = _conn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        for i in range(5):
            db.set_metric(conn, f"2026-07-{i + 1:02d}", "hrv_last_night", 500.0 + i, "garmin", 2)
        db.set_metric(conn, "2026-07-10", "hrv_last_night", 50.0, "garmin", 1)
        conn.commit()
        out = nr.comparar(conn, 1, "2026-07-10")
        self.assertEqual(out["rasgos"]["hrv_last_night"]["n"], 1)
        self.assertEqual(out["rasgos"]["hrv_last_night"]["valor"], 50.0)


class SinDatosTest(unittest.TestCase):
    def test_a_night_with_nothing_at_all_raises_sin_datos(self):
        conn = _conn(self)
        with self.assertRaises(nr.SinDatos):
            nr.comparar(conn, 1, "2026-08-01")


class RasgosRangoTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-3 ruling 1: `rasgos_rango`'s own per-night dict -- every computable
    feature plus its own 'contexto', the ONE table both `comparar` and `buscar` build on."""

    def test_a_night_with_only_a_stored_figure_carries_it_plus_an_empty_contexto(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-09-01", "hrv_last_night", 55.0, "garmin", 1)
        conn.commit()
        tabla = nr.rasgos_rango(conn, 1, "2026-08-25", "2026-09-01")
        self.assertEqual(set(tabla), {"2026-09-01"})
        self.assertEqual(tabla["2026-09-01"]["hrv_last_night"], 55.0)
        self.assertIn("contexto", tabla["2026-09-01"])

    def test_a_night_with_a_full_payload_carries_shape_features_too(self):
        conn = _conn(self)
        _sesion(conn, 1, "2026-01-03", SESION[0], SESION[1])
        _guardar_sueno(conn, 1, "2026-01-03", hr=_HR, etapas=_ETAPAS, hrv=_HRV)
        conn.commit()
        tabla = nr.rasgos_rango(conn, 1, "2026-01-01", "2026-01-03")
        self.assertEqual(tabla["2026-01-03"]["nadir_fraccion"], 0.9)
        self.assertEqual(tabla["2026-01-03"]["segmentos_despierto"], 3)

    def test_comparar_and_rasgos_rango_agree_on_the_same_night(self):
        conn = _conn(self)
        _sesion(conn, 1, "2026-01-03", SESION[0], SESION[1])
        _guardar_sueno(conn, 1, "2026-01-03", hr=_HR, etapas=_ETAPAS, hrv=_HRV)
        conn.commit()
        tabla = nr.rasgos_rango(conn, 1, "2026-01-01", "2026-01-03")
        out = nr.comparar(conn, 1, "2026-01-03", desde="2026-01-01")
        self.assertEqual(tabla["2026-01-03"]["nadir_fraccion"], out["rasgos"]["nadir_fraccion"]["valor"])

    def test_another_persons_nights_never_appear(self):
        conn = _conn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        db.set_metric(conn, "2026-09-05", "hrv_last_night", 999.0, "garmin", 2)
        db.set_metric(conn, "2026-09-05", "hrv_last_night", 55.0, "garmin", 1)
        conn.commit()
        tabla = nr.rasgos_rango(conn, 1, "2026-09-01", "2026-09-05")
        self.assertEqual(tabla["2026-09-05"]["hrv_last_night"], 55.0)


if __name__ == "__main__":
    unittest.main()
