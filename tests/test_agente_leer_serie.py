"""BODY-AGENTE-LEER-SERIE-1: leer_serie, a bounded range read of day-by-day figures, so a whole
watch history is a handful of calls inside bucle.MAX_LECTURAS instead of one leer_dia per day."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, fechas, profile
from body.agente import herramientas


def _conn(case: unittest.TestCase):
    c = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


def _seed_diario(conn, key: str, desde: date, dias: int, valor_base: float, person_id: int = 1) -> None:
    for i in range(dias):
        day = (desde + timedelta(days=i)).isoformat()
        db.set_metric(conn, day, key, valor_base + i % 5, "garmin", person_id)
    conn.commit()


def _ejecutar(conn, nivel: str, **kwargs):
    params = {"metricas": ["resting_hr"], "paso": "dia"}
    params.update(kwargs)
    return herramientas.ejecutar(conn, 1, "leer_serie", params, nivel)


def _contar_statements_daily_metric(conn, fn) -> int:
    """Runs fn() while tracing every SQL statement this connection executes, and returns how many
    of them touch daily_metric -- the table leer_serie reads."""
    vistos = []
    conn.set_trace_callback(lambda sql: vistos.append(sql))
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return sum(1 for sql in vistos if "daily_metric" in sql)


def _ejecutar_contando(conn, fn):
    """Same as _contar_statements_daily_metric, but swallows a ToolError raised by fn() and
    returns (error, n_statements) -- for a refusal test that also wants to check nothing was read,
    in ONE call instead of two."""
    vistos = []
    conn.set_trace_callback(lambda sql: vistos.append(sql))
    error = None
    try:
        fn()
    except herramientas.ToolError as e:
        error = e
    finally:
        conn.set_trace_callback(None)
    return error, sum(1 for sql in vistos if "daily_metric" in sql)


class DiaShapeTest(unittest.TestCase):
    def test_one_point_per_day_with_data_in_order(self):
        conn = _conn(self)
        _seed_diario(conn, "resting_hr", date(2026, 10, 1), 5, 55)
        out = _ejecutar(conn, "completo", desde="2026-10-01", hasta="2026-10-05")
        puntos = out["metricas"]["resting_hr"]["puntos"]
        self.assertEqual([p["fecha"] for p in puntos],
                          ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"])
        self.assertTrue(all(p["n_dias"] == 1 for p in puntos))
        self.assertEqual(puntos[0]["valor"], 55.0)
        self.assertEqual(out["paso"], "dia")
        self.assertEqual(out["desde"], "2026-10-01")
        self.assertEqual(out["hasta"], "2026-10-05")
        self.assertEqual(out["metricas"]["resting_hr"]["unidad"], "bpm")


class SemanaShapeTest(unittest.TestCase):
    def test_points_keyed_by_mondays_averaged_with_count(self):
        conn = _conn(self)
        # Two full ISO weeks, Monday 2026-10-05 through Sunday 2026-10-18.
        _seed_diario(conn, "resting_hr", date(2026, 10, 5), 14, 50)
        out = herramientas.ejecutar(
            conn, 1, "leer_serie",
            {"metricas": ["resting_hr"], "desde": "2026-10-05", "hasta": "2026-10-18", "paso": "semana"},
            "completo",
        )
        puntos = out["metricas"]["resting_hr"]["puntos"]
        self.assertEqual([p["fecha"] for p in puntos], ["2026-10-05", "2026-10-12"])
        for p in puntos:
            self.assertEqual(p["n_dias"], 7)
            self.assertIsInstance(p["fecha"], str)
        # Every Monday-of fecha really is a Monday.
        for p in puntos:
            self.assertEqual(date.fromisoformat(p["fecha"]).weekday(), 0)


class MesShapeTest(unittest.TestCase):
    def test_points_keyed_by_months_first_day(self):
        conn = _conn(self)
        _seed_diario(conn, "resting_hr", date(2026, 9, 15), 25, 50)  # spans Sep + Oct
        out = herramientas.ejecutar(
            conn, 1, "leer_serie",
            {"metricas": ["resting_hr"], "desde": "2026-09-01", "hasta": "2026-10-31", "paso": "mes"},
            "completo",
        )
        puntos = out["metricas"]["resting_hr"]["puntos"]
        self.assertEqual([p["fecha"] for p in puntos], ["2026-09-01", "2026-10-01"])
        self.assertEqual(puntos[0]["n_dias"] + puntos[1]["n_dias"], 25)


class HonestGapsTest(unittest.TestCase):
    def test_a_day_with_no_data_is_absent_never_zero(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-01", "resting_hr", 58, "garmin", 1)
        db.set_metric(conn, "2026-10-03", "resting_hr", 60, "garmin", 1)
        conn.commit()
        out = _ejecutar(conn, "completo", desde="2026-10-01", hasta="2026-10-03")
        puntos = out["metricas"]["resting_hr"]["puntos"]
        self.assertEqual([p["fecha"] for p in puntos], ["2026-10-01", "2026-10-03"])
        for p in puntos:
            self.assertNotEqual(p["valor"], 0)

    def test_a_metric_with_no_data_at_all_is_present_with_an_empty_list(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-01", "resting_hr", 58, "garmin", 1)
        conn.commit()
        out = herramientas.ejecutar(
            conn, 1, "leer_serie",
            {"metricas": ["resting_hr", "steps"], "desde": "2026-10-01", "hasta": "2026-10-03", "paso": "dia"},
            "completo",
        )
        self.assertIn("steps", out["metricas"])
        self.assertEqual(out["metricas"]["steps"]["puntos"], [])


class AggregationMatchesTrendsTest(unittest.TestCase):
    def test_mes_figure_equals_the_trends_monthly_line_for_the_same_month(self):
        conn = _conn(self)
        _seed_diario(conn, "resting_hr", date(2026, 10, 1), 31, 52)
        out = herramientas.ejecutar(
            conn, 1, "leer_serie",
            {"metricas": ["resting_hr"], "desde": "2026-10-01", "hasta": "2026-10-31", "paso": "mes"},
            "completo",
        )
        punto = out["metricas"]["resting_hr"]["puntos"][0]
        perfil = profile.metric_profile(conn, "resting_hr", person_id=1)
        mes_oct = next(m for m in perfil.months if m[0] == "2026-10")
        self.assertEqual(punto["fecha"], "2026-10-01")
        self.assertEqual(punto["valor"], mes_oct[1])
        self.assertEqual(punto["n_dias"], mes_oct[2])


class BoundsTest(unittest.TestCase):
    def test_paso_dia_over_366_days_is_refused_with_nothing_read(self):
        conn = _conn(self)
        _seed_diario(conn, "resting_hr", date(2024, 1, 1), 3, 55)

        def llamar():
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr"], "desde": "2024-01-01", "hasta": "2025-01-05", "paso": "dia"},
                "completo",
            )

        error, n = _ejecutar_contando(conn, llamar)
        self.assertIsNotNone(error)
        self.assertIn("366", str(error))
        self.assertEqual(n, 0)

    def test_paso_semana_over_260_weeks_is_refused_with_nothing_read(self):
        conn = _conn(self)

        def llamar():
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr"], "desde": "2010-01-04", "hasta": "2026-10-01", "paso": "semana"},
                "completo",
            )

        error, n = _ejecutar_contando(conn, llamar)
        self.assertIsNotNone(error)
        self.assertIn("260", str(error))
        self.assertEqual(n, 0)

    def test_paso_mes_has_no_bound(self):
        conn = _conn(self)
        out = herramientas.ejecutar(
            conn, 1, "leer_serie",
            {"metricas": ["resting_hr"], "desde": "2010-01-01", "hasta": "2026-10-01", "paso": "mes"},
            "completo",
        )
        self.assertEqual(out["metricas"]["resting_hr"]["puntos"], [])

    def test_desde_after_hasta_is_refused(self):
        conn = _conn(self)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr"], "desde": "2026-10-10", "hasta": "2026-10-01", "paso": "dia"},
                "completo",
            )


class FutureHastaTest(unittest.TestCase):
    def test_hasta_after_local_today_is_refused(self):
        conn = _conn(self)
        manana = (fechas.hoy_local(db.person(conn, 1)) + timedelta(days=1)).isoformat()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr"], "desde": "2026-01-01", "hasta": manana, "paso": "mes"},
                "completo",
            )


class UnknownMetricTest(unittest.TestCase):
    def test_unknown_metric_is_refused_with_nothing_read(self):
        conn = _conn(self)
        _seed_diario(conn, "resting_hr", date(2026, 10, 1), 3, 55)

        def llamar():
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["no_existe"], "desde": "2026-10-01", "hasta": "2026-10-03", "paso": "dia"},
                "completo",
            )

        error, n = _ejecutar_contando(conn, llamar)
        self.assertIsNotNone(error)
        self.assertEqual(n, 0)


class NivelDatosTest(unittest.TestCase):
    def test_paso_dia_refused_at_nivel_resumen(self):
        conn = _conn(self)
        _seed_diario(conn, "resting_hr", date(2026, 10, 1), 3, 55)
        with self.assertRaises(herramientas.ToolError):
            _ejecutar(conn, "resumen", desde="2026-10-01", hasta="2026-10-03")

    def test_paso_semana_and_mes_allowed_at_nivel_resumen(self):
        conn = _conn(self)
        _seed_diario(conn, "resting_hr", date(2026, 10, 1), 14, 55)
        for paso in ("semana", "mes"):
            out = herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr"], "desde": "2026-10-01", "hasta": "2026-10-14", "paso": paso},
                "resumen",
            )
            self.assertIn("resting_hr", out["metricas"])

    def test_tool_absent_from_disponibles_at_nivel_ninguno(self):
        nombres = {h["nombre"] for h in herramientas.disponibles("ninguno")}
        self.assertNotIn("leer_serie", nombres)

    def test_ejecutar_refuses_leer_serie_at_nivel_ninguno(self):
        conn = _conn(self)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr"], "desde": "2026-10-01", "hasta": "2026-10-03", "paso": "mes"},
                "ninguno",
            )

    def test_leer_serie_offered_at_resumen_and_completo(self):
        for nivel in ("resumen", "completo"):
            nombres = {h["nombre"] for h in herramientas.disponibles(nivel)}
            self.assertIn("leer_serie", nombres)


class OtraPersonaInvisibleTest(unittest.TestCase):
    def test_another_persons_data_never_appears(self):
        conn = _conn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        conn.commit()
        db.set_metric(conn, "2026-10-01", "resting_hr", 58, "garmin", 1)
        db.set_metric(conn, "2026-10-01", "resting_hr", 999, "garmin", 2)
        conn.commit()
        out = herramientas.ejecutar(
            conn, 1, "leer_serie",
            {"metricas": ["resting_hr"], "desde": "2026-10-01", "hasta": "2026-10-01", "paso": "dia"},
            "completo",
        )
        puntos = out["metricas"]["resting_hr"]["puntos"]
        self.assertEqual(len(puntos), 1)
        self.assertEqual(puntos[0]["valor"], 58.0)


class StatementCountTest(unittest.TestCase):
    def test_statement_count_is_independent_of_range_length(self):
        conn = _conn(self)
        _seed_diario(conn, "resting_hr", date(2020, 1, 1), 366, 55)

        def corto():
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr"], "desde": "2020-01-01", "hasta": "2020-01-10", "paso": "dia"},
                "completo",
            )

        def largo():
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr"], "desde": "2020-01-01", "hasta": "2020-12-31", "paso": "dia"},
                "completo",
            )

        n_corto = _contar_statements_daily_metric(conn, corto)
        n_largo = _contar_statements_daily_metric(conn, largo)
        self.assertEqual(n_corto, 1)
        self.assertEqual(n_corto, n_largo)

    def test_statement_count_is_one_regardless_of_metric_count(self):
        conn = _conn(self)
        for clave in ("resting_hr", "steps", "hrv_last_night"):
            _seed_diario(conn, clave, date(2026, 10, 1), 5, 55)

        def llamar():
            herramientas.ejecutar(
                conn, 1, "leer_serie",
                {"metricas": ["resting_hr", "steps", "hrv_last_night"], "desde": "2026-10-01",
                 "hasta": "2026-10-05", "paso": "dia"},
                "completo",
            )

        self.assertEqual(_contar_statements_daily_metric(conn, llamar), 1)


if __name__ == "__main__":
    unittest.main()
