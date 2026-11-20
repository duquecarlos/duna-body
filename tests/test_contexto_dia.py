"""body/contexto_dia.py (BODY-MINUTO-ANALISIS-5-CONTEXTO): day and night context, derived on read
from tables that already exist -- no new table, no stored tag. Invented homes only, no real health
figures, no network."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import contexto_dia, db, recuperacion

PERSON_A = 1
PERSON_B = 2


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _shift(conn, person_id, day, kind="night"):
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _activity(conn, person_id, source_id, day, duration_s=1800.0, tipo="running"):
    conn.execute(
        "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type) "
        "VALUES (?, 'garmin', ?, ?, ?, ?, ?)",
        (person_id, source_id, day, f"{day}T07:00:00", duration_s, tipo),
    )


def _recuperacion(conn, person_id, source_id, minutos):
    conn.execute(
        "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, "
        "modelo_version, minutos, computed_at) VALUES (?, 'garmin', ?, ?, ?, '2026-11-10T00:00:00+00:00')",
        (person_id, source_id, recuperacion.VERSION, minutos),
    )


def _viaje(conn, person_id, ref, starts_at, ends_at):
    conn.execute(
        "INSERT INTO viaje_lugares (person_id, ref, starts_at, ends_at, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (person_id, ref, starts_at, ends_at, "2026-11-18T00:00:00+00:00"),
    )


def _meal(conn, person_id, day, ts_local, metric_key="protein_g", amount=20.0):
    return db.registrar_ingesta(conn, person_id, day, metric_key, amount, False, ts_local=ts_local)


def _sleep(conn, person_id, day, start_local, end_local):
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, start_local, end_local),
    )


def _n_statements(conn, fn) -> int:
    vistos = []
    conn.set_trace_callback(lambda sql: vistos.append(sql))
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return len(vistos)


# ---- turno ---------------------------------------------------------------------------------------

class TurnoTest(unittest.TestCase):
    def test_night_after_a_night_shift_carries_tipo_and_bloque_noche(self):
        conn = _conn(self)
        for day in ("2026-11-10", "2026-11-11", "2026-11-12"):
            _shift(conn, PERSON_A, day, "night")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-12")
        self.assertEqual(out["2026-11-11"]["turno"]["tipo"], "night")
        self.assertEqual(out["2026-11-11"]["turno"]["bloque_noche"], {"posicion": 2, "largo": 3})

    def test_recovery_day_carries_dias_desde_ultimo_turno_noche(self):
        conn = _conn(self)
        _shift(conn, PERSON_A, "2026-11-10", "night")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-11", "2026-11-13")
        self.assertEqual(out["2026-11-11"]["turno"]["dias_desde_ultimo_turno_noche"], 1)
        self.assertEqual(out["2026-11-12"]["turno"]["dias_desde_ultimo_turno_noche"], 2)
        self.assertNotIn("tipo", out["2026-11-11"]["turno"])

    def test_a_day_with_no_shift_has_no_turno_key(self):
        conn = _conn(self)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("turno", out["2026-11-10"])

    def test_another_persons_shift_is_never_counted(self):
        conn = _conn(self)
        _shift(conn, PERSON_B, "2026-11-10", "night")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("turno", out["2026-11-10"])


# ---- viaje ----------------------------------------------------------------------------------------

class ViajeTest(unittest.TestCase):
    def test_a_day_inside_a_lugares_trip_carries_ref_and_day_in_trip(self):
        conn = _conn(self)
        _viaje(conn, PERSON_A, "v1", "2026-11-10", "2026-11-14")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-14")
        self.assertEqual(out["2026-11-12"]["viaje"], {"ref": "v1", "dia": 3})

    def test_a_1_day_lugares_entry_is_never_a_trip(self):
        conn = _conn(self)
        _viaje(conn, PERSON_A, "v1", "2026-11-10", "2026-11-10")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("viaje", out["2026-11-10"])

    def test_a_day_inside_a_viaje_period_carries_viaje(self):
        conn = _conn(self)
        db.crear_periodo(conn, PERSON_A, "viaje", "2026-11-10", "2026-11-12")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-12")
        self.assertEqual(out["2026-11-11"]["viaje"], {"dia": 2})

    def test_a_day_with_neither_has_no_viaje_key(self):
        conn = _conn(self)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("viaje", out["2026-11-10"])

    def test_another_persons_trip_is_never_counted(self):
        conn = _conn(self)
        _viaje(conn, PERSON_B, "v1", "2026-11-10", "2026-11-14")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-14")
        self.assertNotIn("viaje", out["2026-11-12"])


# ---- periodos ---------------------------------------------------------------------------------------

class PeriodosTest(unittest.TestCase):
    def test_an_open_period_with_zonas_carries_its_kind_and_zonas(self):
        conn = _conn(self)
        db.crear_periodo(conn, PERSON_A, "lesion", "2026-11-10", None, zonas=["pierna"])
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertEqual(out["2026-11-10"]["periodos"], [{"kind": "lesion", "zonas": ["pierna"]}])

    def test_a_period_without_zonas_has_no_zonas_key(self):
        conn = _conn(self)
        db.crear_periodo(conn, PERSON_A, "malestar", "2026-11-10", "2026-11-10")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertEqual(out["2026-11-10"]["periodos"], [{"kind": "malestar"}])

    def test_a_day_with_no_period_has_no_periodos_key(self):
        conn = _conn(self)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("periodos", out["2026-11-10"])

    def test_another_persons_period_is_never_counted(self):
        conn = _conn(self)
        db.crear_periodo(conn, PERSON_B, "lesion", "2026-11-10", None, zonas=["pierna"])
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("periodos", out["2026-11-10"])


# ---- carga ----------------------------------------------------------------------------------------

class CargaTest(unittest.TestCase):
    def test_a_day_with_activity_carries_actividades_and_minutos(self):
        conn = _conn(self)
        _activity(conn, PERSON_A, "a1", "2026-11-10", duration_s=1800.0)
        _activity(conn, PERSON_A, "a2", "2026-11-10", duration_s=600.0)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        carga = out["2026-11-10"]["carga"]
        self.assertEqual(carga["actividades"], 2)
        self.assertAlmostEqual(carga["minutos"], 40.0)
        self.assertNotIn("recuperacion_min", carga)

    def test_recuperacion_min_present_when_computed(self):
        conn = _conn(self)
        _activity(conn, PERSON_A, "a1", "2026-11-10", duration_s=1800.0)
        _recuperacion(conn, PERSON_A, "a1", 45.0)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertEqual(out["2026-11-10"]["carga"]["recuperacion_min"], 45.0)

    def test_a_day_with_no_activity_has_no_carga_key(self):
        conn = _conn(self)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("carga", out["2026-11-10"])

    def test_another_persons_activity_is_never_counted(self):
        conn = _conn(self)
        _activity(conn, PERSON_B, "a1", "2026-11-10")
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("carga", out["2026-11-10"])


# ---- alcohol_g --------------------------------------------------------------------------------------

class AlcoholTest(unittest.TestCase):
    def test_a_day_with_alcohol_carries_the_figure(self):
        conn = _conn(self)
        db.registrar_ingesta(conn, PERSON_A, "2026-11-10", "alcohol_g", 12.0, False)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertEqual(out["2026-11-10"]["alcohol_g"], 12.0)

    def test_a_day_without_alcohol_has_no_alcohol_g_key(self):
        conn = _conn(self)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("alcohol_g", out["2026-11-10"])

    def test_another_persons_alcohol_is_never_counted(self):
        conn = _conn(self)
        db.registrar_ingesta(conn, PERSON_B, "2026-11-10", "alcohol_g", 12.0, False)
        conn.commit()
        out = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")
        self.assertNotIn("alcohol_g", out["2026-11-10"])


# ---- statement count, bounded and independent of the range's length --------------------------------

class StatementCountTest(unittest.TestCase):
    def test_statement_count_for_a_90_day_range_equals_a_7_day_range(self):
        conn7 = _conn(self)
        n7 = _n_statements(conn7, lambda: contexto_dia.contexto_dias(conn7, PERSON_A, "2026-11-01", "2026-11-07"))
        conn90 = _conn(self)
        n90 = _n_statements(
            conn90, lambda: contexto_dia.contexto_dias(conn90, PERSON_A, "2026-08-01", "2026-10-29"))
        self.assertEqual(n7, n90)
        # One bulk statement per source table: shift (bloques_noche), shift (day->kind), viaje_lugares,
        # period (cached per connection, this call's own first read), activity, recuperacion_actividad,
        # daily_metric_efectiva -- 7, named here as the ceiling this test guards.
        self.assertLessEqual(n7, 7, f"contexto_dias cost {n7} statements, over its named ceiling of 7")


# ---- contexto_noche: the night takes the evening's own context -------------------------------------

class ContextoNocheTest(unittest.TestCase):
    def test_night_takes_the_evenings_shift_and_load(self):
        conn = _conn(self)
        _shift(conn, PERSON_A, "2026-11-10", "night")
        _activity(conn, PERSON_A, "a1", "2026-11-10", duration_s=1200.0)
        conn.commit()
        out = contexto_dia.contexto_noche(conn, PERSON_A, "2026-11-11")
        self.assertEqual(out["turno"]["tipo"], "night")
        self.assertEqual(out["carga"]["actividades"], 1)

    def test_late_meal_carries_comida_antes_de_dormir_equal_to_comida_a_sueno(self):
        conn = _conn(self)
        _meal(conn, PERSON_A, "2026-11-10", "2026-11-10T22:30:00")
        _sleep(conn, PERSON_A, "2026-11-11", "2026-11-11T00:30:00", "2026-11-11T07:00:00")
        conn.commit()
        from body import ventana_comida
        esperado = ventana_comida.comida_a_sueno(conn, PERSON_A, "2026-11-10")
        out = contexto_dia.contexto_noche(conn, PERSON_A, "2026-11-11")
        self.assertAlmostEqual(out["comida_antes_de_dormir"]["minutos"], esperado.horas * 60)

    def test_no_meal_before_sleep_has_no_comida_antes_de_dormir_key(self):
        conn = _conn(self)
        _sleep(conn, PERSON_A, "2026-11-11", "2026-11-11T00:30:00", "2026-11-11T07:00:00")
        conn.commit()
        out = contexto_dia.contexto_noche(conn, PERSON_A, "2026-11-11")
        self.assertNotIn("comida_antes_de_dormir", out)

    def test_a_trip_starting_on_the_wake_day_alone_still_merges_in(self):
        conn = _conn(self)
        _viaje(conn, PERSON_A, "v1", "2026-11-11", "2026-11-15")
        conn.commit()
        out = contexto_dia.contexto_noche(conn, PERSON_A, "2026-11-11")
        self.assertEqual(out["viaje"], {"ref": "v1", "dia": 1})

    def test_a_period_covering_only_the_wake_day_merges_into_periodos(self):
        conn = _conn(self)
        db.crear_periodo(conn, PERSON_A, "malestar", "2026-11-11", "2026-11-12")
        conn.commit()
        out = contexto_dia.contexto_noche(conn, PERSON_A, "2026-11-11")
        self.assertEqual(out.get("periodos"), [{"kind": "malestar"}])

    def test_a_night_with_nothing_at_all_is_an_empty_dict(self):
        conn = _conn(self)
        conn.commit()
        out = contexto_dia.contexto_noche(conn, PERSON_A, "2026-11-11")
        self.assertEqual(out, {})


# ---- census: every key the module can emit, named --------------------------------------------------

class CensusTest(unittest.TestCase):
    def test_every_context_key_the_module_can_emit_is_named(self):
        self.assertEqual(
            contexto_dia.CONTEXT_KEYS,
            ("turno", "viaje", "periodos", "carga", "alcohol_g", "comida_antes_de_dormir"),
        )

    def test_a_rich_fixture_emits_exactly_the_named_keys(self):
        conn = _conn(self)
        _shift(conn, PERSON_A, "2026-11-10", "night")
        _activity(conn, PERSON_A, "a1", "2026-11-10", duration_s=1200.0)
        _recuperacion(conn, PERSON_A, "a1", 30.0)
        _viaje(conn, PERSON_A, "v1", "2026-11-09", "2026-11-13")
        db.crear_periodo(conn, PERSON_A, "lesion", "2026-11-09", None, zonas=["pierna"])
        db.registrar_ingesta(conn, PERSON_A, "2026-11-10", "alcohol_g", 5.0, False)
        _meal(conn, PERSON_A, "2026-11-10", "2026-11-10T22:30:00")
        _sleep(conn, PERSON_A, "2026-11-11", "2026-11-11T00:30:00", "2026-11-11T07:00:00")
        conn.commit()

        dia = contexto_dia.contexto_dias(conn, PERSON_A, "2026-11-10", "2026-11-10")["2026-11-10"]
        self.assertEqual(set(dia), {"turno", "viaje", "periodos", "carga", "alcohol_g"})

        noche = contexto_dia.contexto_noche(conn, PERSON_A, "2026-11-11")
        self.assertEqual(set(noche), {"turno", "viaje", "periodos", "carga", "alcohol_g",
                                       "comida_antes_de_dormir"})
        self.assertTrue(set(noche) <= set(contexto_dia.CONTEXT_KEYS))


if __name__ == "__main__":
    unittest.main()
