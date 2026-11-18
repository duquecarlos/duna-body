"""BODY-DESPERTAR-REAL-4 ruling 2: `corregir_despertar`, a propuesta tool correcting Garmin's own
recorded sleep end for one night -- 'sueno' extends sleep to `hora`, 'levantado' names the actual
get-up time. Garmin's own sleep_session row is never edited; this is Body's own correction beside
it, through db.guardar_despertar_correccion/db.borrar_despertar_correccion. Invented demo data
only, no real health figures, no network.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, mcp
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun

DAY = "2026-11-05"
SLEEP_END = f"{DAY}T06:00:00"


def _conn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _sesion(conn, person_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, DAY, "2026-11-04T23:00:00", SLEEP_END),
    )
    conn.commit()


# ---- validation / refusals ----------------------------------------------------------------------

class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_no_sleep_that_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                  {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        self.assertEqual(ctx.exception.args[0], T("es")("agente.error.despertar_sin_sueno"))

    def test_hora_at_sleep_end_is_refused(self):
        _sesion(self.conn)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                  {"fecha": DAY, "tipo": "levantado", "hora": "06:00"}, "ninguno", self.cid)
        self.assertEqual(ctx.exception.args[0], T("es")("agente.error.despertar_hora_no_posterior"))

    def test_hora_before_sleep_end_is_refused(self):
        _sesion(self.conn)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                  {"fecha": DAY, "tipo": "levantado", "hora": "05:00"}, "ninguno", self.cid)
        self.assertEqual(ctx.exception.args[0], T("es")("agente.error.despertar_hora_no_posterior"))

    def test_hora_past_the_horizon_is_refused(self):
        _sesion(self.conn)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                  {"fecha": DAY, "tipo": "levantado", "hora": "11:00"}, "ninguno", self.cid)
        self.assertEqual(ctx.exception.args[0], T("es")("agente.error.despertar_hora_fuera_horizonte"))

    def test_hora_just_inside_the_horizon_is_allowed(self):
        _sesion(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "09:59"}, "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")

    def test_an_unknown_tipo_is_refused(self):
        _sesion(self.conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                  {"fecha": DAY, "tipo": "dormido", "hora": "07:00"}, "ninguno", self.cid)

    def test_a_future_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                  {"fecha": "2099-01-01", "tipo": "levantado", "hora": "07:00"},
                                  "ninguno", self.cid)

    def test_another_persons_sleep_is_never_seen(self):
        _sesion(self.conn, person_id=2)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                  {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        self.assertEqual(ctx.exception.args[0], T("es")("agente.error.despertar_sin_sueno"))


# ---- apply / undo ---------------------------------------------------------------------------------

class ApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _sesion(self.conn)

    def test_apply_writes_the_correction(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        fila = db.despertar_correccion(self.conn, 1, DAY)
        self.assertEqual(fila["tipo"], "levantado")
        self.assertEqual(fila["hora_local"], "07:00")
        self.assertEqual(fila["origen"], "asistente")
        # Garmin's own row is never touched
        sesion = self.conn.execute(
            "SELECT end_local FROM sleep_session WHERE person_id = 1 AND day = ?", (DAY,)).fetchone()
        self.assertEqual(sesion["end_local"], SLEEP_END)

    def test_undo_removes_a_correction_that_did_not_exist_before(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertIsNone(db.despertar_correccion(self.conn, 1, DAY))

    def test_undo_restores_a_prior_correction(self):
        db.guardar_despertar_correccion(self.conn, 1, DAY, "sueno", "06:20", "despertar-real-v1", "persona")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        fila = db.despertar_correccion(self.conn, 1, DAY)
        self.assertEqual((fila["tipo"], fila["hora_local"], fila["origen"]), ("sueno", "06:20", "persona"))

    def test_applying_twice_the_second_is_refused(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "ya_resuelta")


# ---- drift ----------------------------------------------------------------------------------------

class DriftTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _sesion(self.conn)

    def test_drift_on_apply_refuses(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        db.guardar_despertar_correccion(self.conn, 1, DAY, "sueno", "06:20", "despertar-real-v1", "persona")
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_drift_on_undo_refuses(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        db.guardar_despertar_correccion(self.conn, 1, DAY, "sueno", "06:20", "despertar-real-v1", "persona")
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")


# ---- card: title and summary, es/en ----------------------------------------------------------------

class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _sesion(self.conn)
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, tipo: str = "levantado", hora: str = "07:00"):
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": tipo, "hora": hora}, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_title_es(self):
        self.assertEqual(T("es")("asistente.propuesta.titulo.corregir_despertar"), "Corregir despertar")

    def test_title_en(self):
        self.assertEqual(T("en")("asistente.propuesta.titulo.corregir_despertar"), "Correct wake-up")

    def test_levantado_summary_names_both_times_es(self):
        t = self._tarjeta("es", tipo="levantado", hora="07:00")
        self.assertIn("07:00", t["despues"])
        self.assertIn("06:00", t["despues"])

    def test_levantado_summary_names_both_times_en(self):
        t = self._tarjeta("en", tipo="levantado", hora="07:00")
        self.assertIn("07:00", t["despues"])
        self.assertIn("06:00", t["despues"])

    def test_sueno_summary_es(self):
        t = self._tarjeta("es", tipo="sueno", hora="06:30")
        self.assertIn("06:30", t["despues"])
        self.assertIn("06:00", t["despues"])

    def test_sueno_summary_en(self):
        t = self._tarjeta("en", tipo="sueno", hora="06:30")
        self.assertIn("06:30", t["despues"])
        self.assertIn("06:00", t["despues"])

    def test_card_render_never_raises_on_a_malformed_despues(self):
        tarjeta = asistente._tarjeta_propuesta_segura(T("es"), self.defs, {
            "id": 999, "accion": "corregir_despertar", "despues_json": {}, "antes_json": {},
            "estado": "propuesta", "expira_en": "2026-11-09T00:10:00+00:00", "aplicada_via": None,
        })
        self.assertIn("despues", tarjeta)


class EvidenceLineTest(unittest.TestCase):
    """The card names the evidence by source, only when despertar_real.estimar found any."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _sesion(self.conn)
        self.defs = comun._defs(self.conn)

    def test_no_evidence_adds_no_extra_line(self):
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        tarjeta = asistente._tarjeta_propuesta(T("es"), self.defs, out["_registro"])
        self.assertNotIn("\n", tarjeta["despues"])

    def test_a_logged_meal_is_named_as_evidence(self):
        db.set_metric(self.conn, DAY, "resting_hr", 55.0, "garmin", 1)
        db.crear_comida(self.conn, 1, DAY, [{"metric_key": db.INGESTA_METRIC_KEYS[0], "amount": 50.0,
                                              "estimado": True}], "Desayuno", f"{DAY}T06:40:00")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "corregir_despertar",
                                    {"fecha": DAY, "tipo": "levantado", "hora": "07:00"}, "ninguno", self.cid)
        tarjeta = asistente._tarjeta_propuesta(T("es"), self.defs, out["_registro"])
        self.assertIn("comida registrada", tarjeta["despues"])


# ---- catalog parity: in-app registry, stdio MCP -----------------------------------------------

class CatalogParityTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _sesion(self.conn)

    def test_listed_in_the_in_app_registry(self):
        nombres = {h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta"}
        self.assertIn("corregir_despertar", nombres)

    def test_listed_over_stdio_mcp(self):
        sesion = mcp._Sesion(self.conn, 1)
        nombres = {t["name"] for t in mcp._lista_herramientas(sesion)}
        self.assertIn("corregir_despertar", nombres)

    def test_not_in_acciones_reemplazables(self):
        self.assertNotIn("corregir_despertar", propuestas.ACCIONES_REEMPLAZABLES)


if __name__ == "__main__":
    unittest.main()
