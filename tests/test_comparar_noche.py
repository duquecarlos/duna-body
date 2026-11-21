"""BODY-MINUTO-ANALISIS-2: the assistant tool `comparar_noche`, wired through
body/agente/herramientas.py exactly like leer_noche -- nivel gating, date validation, the
localized refusal for a night with nothing to rank, and es/en parity of that one new message.
Invented demo data only, no real health figures, no network.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import captura_sueno, db, desplazamiento_import
from body.agente import herramientas


def _conn(case: unittest.TestCase):
    c = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


class RegistroTest(unittest.TestCase):
    def test_registered_as_lectura_completo(self):
        self.assertEqual(herramientas.tipo_de("comparar_noche"), "lectura")
        entrada = herramientas._BY_NAME["comparar_noche"]
        self.assertEqual(entrada["nivel"], "completo")
        self.assertIn("comparar_noche", {h["nombre"] for h in herramientas.disponibles("completo")})
        self.assertNotIn("comparar_noche", {h["nombre"] for h in herramientas.disponibles("resumen")})


class EjecutarGuardTest(unittest.TestCase):
    def test_refused_below_nivel_completo(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-05", "hrv_last_night", 55.0, "garmin", 1)
        conn.commit()
        for nivel in ("resumen", "ninguno"):
            with self.assertRaises(herramientas.ToolError):
                herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2026-10-05"}, nivel)

    def test_malformed_fecha_refused(self):
        conn = _conn(self)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "no-es-fecha"}, "completo")

    def test_future_fecha_refused(self):
        conn = _conn(self)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2099-01-01"}, "completo")

    def test_malformed_desde_refused(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-05", "hrv_last_night", 55.0, "garmin", 1)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2026-10-05", "desde": "x"}, "completo")

    def test_missing_fecha_refused(self):
        conn = _conn(self)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "comparar_noche", {}, "completo")

    def test_a_night_with_nothing_at_all_is_refused_localized(self):
        conn = _conn(self)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2026-10-05"}, "completo")
        self.assertEqual(str(ctx.exception), "No hay una noche registrada por Garmin para ese día.")

    def test_a_night_with_nothing_at_all_is_refused_localized_in_english(self):
        conn = _conn(self)
        db.set_language(conn, "en", 1)
        conn.commit()
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2026-10-05"}, "completo")
        self.assertEqual(str(ctx.exception), "There is no night recorded by Garmin for that day.")

    def test_an_unknown_grupo_key_is_refused(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-05", "hrv_last_night", 55.0, "garmin", 1)
        conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "comparar_noche",
                                   {"fecha": "2026-10-05", "grupo": {"no_existe": 1}}, "completo")


class SalidaTest(unittest.TestCase):
    def test_basic_call_returns_rasgos_contexto_and_noches_sin_minutos(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-05", "hrv_last_night", 55.0, "garmin", 1)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2026-10-05"}, "completo")
        self.assertEqual(out["dia"], "2026-10-05")
        self.assertIn("rasgos", out)
        self.assertIn("hrv_last_night", out["rasgos"])
        self.assertEqual(out["rasgos"]["hrv_last_night"]["valor"], 55.0)
        self.assertIn("noches_sin_minutos", out)
        self.assertIn("contexto", out)
        json.dumps(out)  # survives serialization, the same contract every lectura tool keeps

    def test_output_never_carries_a_raw_minute_array(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-05", "hrv_last_night", 55.0, "garmin", 1)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2026-10-05"}, "completo")
        self.assertNotIn("frecuencia_cardiaca", out)
        self.assertNotIn("sueno", out)


class CapturaTest(unittest.TestCase):
    """BODY-NOCHE-SIN-REM-1: the same capture-gap key `leer_noche` carries, for this one focal
    night -- read from the night's own stored totals, never from `noche_rasgos.comparar`'s ranked
    features."""

    def test_zero_rem_on_a_long_night_is_marked(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-05", "sleep_deep_min", 120.0, "garmin", 1)
        db.set_metric(conn, "2026-10-05", "sleep_light_min", 240.0, "garmin", 1)
        db.set_metric(conn, "2026-10-05", "sleep_rem_min", 0.0, "garmin", 1)
        db.set_metric(conn, "2026-10-05", "sleep_awake_min", 30.0, "garmin", 1)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2026-10-05"}, "completo")
        self.assertEqual(out["captura"], {"rem_no_capturado": True, "version": captura_sueno.VERSION})

    def test_positive_rem_is_not_marked(self):
        conn = _conn(self)
        db.set_metric(conn, "2026-10-05", "hrv_last_night", 55.0, "garmin", 1)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "comparar_noche", {"fecha": "2026-10-05"}, "completo")
        self.assertIsNone(out["captura"])


class GrupoConDesplazamientoTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-7 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1): `grupo`'s seventh key --
    whether the night's evening day has a commute row. Five "commute evening" nights and five
    plain ones, the same CompararGrupoTest pattern tests/test_noche_rasgos.py's own module uses for
    the other six grupo keys."""

    def setUp(self):
        self.conn = _conn(self)
        for i in range(5):
            evening = f"2026-09-{i + 1:02d}"
            wake = f"2026-09-{i + 2:02d}"
            db.set_metric(self.conn, evening, "vehicle_minutes", 15.0, desplazamiento_import.FUENTE, 1)
            db.set_metric(self.conn, wake, "hrv_last_night", 40.0 + i, "garmin", 1)
        for i in range(5):
            wake = f"2026-10-{i + 1:02d}"
            db.set_metric(self.conn, wake, "hrv_last_night", 60.0 + i, "garmin", 1)
        self.conn.commit()

    def test_con_desplazamiento_true_selects_only_the_commute_evening_nights(self):
        out = herramientas.ejecutar(
            self.conn, 1, "comparar_noche",
            {"fecha": "2026-09-06", "desde": "2026-09-01", "grupo": {"con_desplazamiento": True}},
            "completo",
        )
        self.assertEqual(out["rasgos"]["hrv_last_night"]["grupo"]["n_grupo"], 5)

    def test_con_desplazamiento_false_excludes_the_commute_evening_nights(self):
        out = herramientas.ejecutar(
            self.conn, 1, "comparar_noche",
            {"fecha": "2026-10-05", "desde": "2026-09-01", "grupo": {"con_desplazamiento": False}},
            "completo",
        )
        self.assertEqual(out["rasgos"]["hrv_last_night"]["grupo"]["n_grupo"], 5)


class GrupoConAltitudTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-6-ALTITUD (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3): `grupo`'s eighth
    key -- the signed change in altitude versus home, same CompararGrupoTest pattern as
    GrupoConDesplazamientoTest above. Five nights away, 1200m below an invented home, five plain
    nights at home."""

    def setUp(self):
        self.conn = _conn(self)
        db.crear_altitud_estancia(self.conn, 1, True, "2000-01-01", 2600, "manual")
        for i in range(5):
            evening = f"2026-09-{i + 1:02d}"
            wake = f"2026-09-{i + 2:02d}"
            db.crear_altitud_estancia(self.conn, 1, False, evening, 1400, "manual", end_day=evening)
            db.set_metric(self.conn, wake, "hrv_last_night", 40.0 + i, "garmin", 1)
        for i in range(5):
            wake = f"2026-10-{i + 1:02d}"
            db.set_metric(self.conn, wake, "hrv_last_night", 60.0 + i, "garmin", 1)
        self.conn.commit()

    def test_altitud_cambio_max_selects_only_the_nights_below_home(self):
        out = herramientas.ejecutar(
            self.conn, 1, "comparar_noche",
            {"fecha": "2026-09-06", "desde": "2026-09-01", "grupo": {"altitud_cambio_max": -100}},
            "completo",
        )
        self.assertEqual(out["rasgos"]["hrv_last_night"]["grupo"]["n_grupo"], 5)

    def test_altitud_cambio_min_zero_selects_only_the_nights_at_home(self):
        out = herramientas.ejecutar(
            self.conn, 1, "comparar_noche",
            {"fecha": "2026-10-05", "desde": "2026-09-01", "grupo": {"altitud_cambio_min": 0}},
            "completo",
        )
        self.assertEqual(out["rasgos"]["hrv_last_night"]["grupo"]["n_grupo"], 5)

    def test_altitud_cambio_min_greater_than_max_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(
                self.conn, 1, "comparar_noche",
                {"fecha": "2026-10-05", "desde": "2026-09-01",
                 "grupo": {"altitud_cambio_min": 10, "altitud_cambio_max": -10}},
                "completo",
            )


class StaticGuardTest(unittest.TestCase):
    """tests/test_agente_registro.py's own StaticGuardTest already walks EVERY `lectura` tool's
    call graph for a writer, comparar_noche included (BODY-WEB-ESTRUCTURA-1's own "tests patch the
    module that owns a name" rule applies the same way here) -- this is the same one-tool check
    its own `test_no_lectura_tool_imports_propuestas` makes, scoped to this new tool alone."""

    def test_source_never_mentions_propuestas(self):
        import inspect
        import textwrap
        entrada = herramientas._BY_NAME["comparar_noche"]
        src = textwrap.dedent(inspect.getsource(entrada["funcion"]))
        self.assertNotIn("propuestas", src)


if __name__ == "__main__":
    unittest.main()
