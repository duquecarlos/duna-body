"""BODY-ENTRENO-NUMEROS-1: `registrar_entreno`'s `reps`/`peso_kg` are validated at the door (so a
string like "12-15" never becomes a stored proposal), and one bad legacy card -- stored before this
slice, when the schema had no type at all -- can never take a whole conversation's card list down.
"""
import os
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from fastapi.testclient import TestClient

from body import config, db, identity, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas as agente_propuestas


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


class SchemaTypesTest(unittest.TestCase):
    """Ruling 1: the series item schema says "a number", for the outside client reading it."""

    def test_registrar_entreno_reps_and_peso_kg_declare_number_types(self):
        props = herramientas._PARAM_REGISTRAR_ENTRENO["properties"]["series"]["items"]["properties"]
        self.assertEqual(props["reps"], {"type": ["integer", "null"], "minimum": 0})
        self.assertEqual(props["peso_kg"], {"type": ["number", "null"], "minimum": 0})


class DoorValidationTest(unittest.TestCase):
    """Ruling 2: `registrar_entreno` refuses a non-number reps/peso_kg before a proposal is ever
    created -- a plain numeric string is the one leniency, coerced rather than refused."""

    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def _count_propuestas(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0]

    def test_a_rep_range_is_refused_with_the_message_and_writes_no_row(self):
        before = self._count_propuestas()
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
                "day": "2026-11-10",
                "series": [{"ejercicio": "Sentadilla", "reps": "12-15"}],
            }, "ninguno", self.cid)
        self.assertIn("Sentadilla", str(ctx.exception))
        self.assertIn("número", str(ctx.exception))
        self.assertEqual(self._count_propuestas(), before)

    def test_a_unit_suffixed_reps_is_refused(self):
        before = self._count_propuestas()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
                "day": "2026-11-10",
                "series": [{"ejercicio": "Sentadilla", "reps": "12 reps"}],
            }, "ninguno", self.cid)
        self.assertEqual(self._count_propuestas(), before)

    def test_an_empty_string_reps_is_refused(self):
        before = self._count_propuestas()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
                "day": "2026-11-10",
                "series": [{"ejercicio": "Sentadilla", "reps": ""}],
            }, "ninguno", self.cid)
        self.assertEqual(self._count_propuestas(), before)

    def test_a_bool_reps_is_refused(self):
        before = self._count_propuestas()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
                "day": "2026-11-10",
                "series": [{"ejercicio": "Sentadilla", "reps": True}],
            }, "ninguno", self.cid)
        self.assertEqual(self._count_propuestas(), before)

    def test_a_non_numeric_peso_kg_is_refused(self):
        before = self._count_propuestas()
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
                "day": "2026-11-10",
                "series": [{"ejercicio": "Sentadilla", "peso_kg": "20kg"}],
            }, "ninguno", self.cid)
        self.assertIn("Sentadilla", str(ctx.exception))
        self.assertEqual(self._count_propuestas(), before)

    def test_a_numeric_string_reps_is_coerced_to_an_int(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
            "day": "2026-11-10",
            "series": [{"ejercicio": "Sentadilla", "reps": "12"}],
        }, "ninguno", self.cid)
        row = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        import json as _json
        reps = _json.loads(row["despues_json"])["series"][0]["reps"]
        self.assertEqual(reps, 12)
        self.assertIsInstance(reps, int)

    def test_a_numeric_string_peso_kg_is_coerced_to_a_float(self):
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
            "day": "2026-11-10",
            "series": [{"ejercicio": "Sentadilla", "peso_kg": "17.5"}],
        }, "ninguno", self.cid)
        row = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)
        ).fetchone()
        import json as _json
        peso = _json.loads(row["despues_json"])["series"][0]["peso_kg"]
        self.assertEqual(peso, 17.5)
        self.assertIsInstance(peso, float)


_PROVIDER_ENV_VARS = ("BODY_AGENTE_PROVEEDOR", "BODY_AGENTE_MODELO", "BODY_AGENTE_ANTHROPIC_API_KEY",
                      "BODY_AGENTE_OPENAI_API_KEY", "BODY_AGENTE_GEMINI_API_KEY")


def _borrar_env(*nombres):
    guardado = {n: os.environ.pop(n, None) for n in nombres}

    def _restaurar():
        for n, v in guardado.items():
            if v is not None:
                os.environ[n] = v
            else:
                os.environ.pop(n, None)
    return _restaurar


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "g.db"
    conn = db.connect()
    db.init(conn)
    return conn, orig


class LegacyCardRenderTest(unittest.TestCase):
    """Ruling 3: render never crashes on a row stored before this slice, and one bad card never
    takes the whole conversation down."""

    def setUp(self):
        self.conn, self.orig_db_path = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(_borrar_env(*_PROVIDER_ENV_VARS))
        for n in _PROVIDER_ENV_VARS:
            os.environ.pop(n, None)
        os.environ["BODY_AGENTE_PROVEEDOR"] = "demo"
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)
        self.cid = agente_conversacion.crear(self.conn, 1)

    def _tarjeta(self, datos: dict, propuesta_id: int) -> dict:
        for p in datos["propuestas"]:
            if p["id"] == propuesta_id:
                return p
        self.fail(f"no card for propuesta {propuesta_id} in {datos['propuestas']}")

    def test_a_legacy_non_numeric_reps_row_renders_200_with_the_raw_text(self):
        registro = agente_propuestas.crear(self.conn, 1, self.cid, "registrar_entreno", {
            "manual": True, "day": "2026-11-10", "time": None, "titulo": None,
            "series": [{"ejercicio": "Sentadilla", "reps": "12-15", "peso_kg": 20}],
        }, antes={"manual": True})
        r = self.client.get(f"/asistente/conversacion/{self.cid}")
        self.assertEqual(r.status_code, 200)
        datos = r.json()
        card = self._tarjeta(datos, registro["id"])
        self.assertIn("12-15", str(card["series"]))

    def test_a_row_forced_to_raise_shows_the_fallback_while_a_sibling_still_renders(self):
        malo = agente_propuestas.crear(self.conn, 1, self.cid, "registrar_entreno", {
            "manual": True, "day": "2026-11-10", "time": None, "titulo": None,
            # no "series" key at all -- a shape _tarjeta_registrar_entreno cannot render,
            # forcing the same KeyError a genuinely corrupt legacy row would raise.
        }, antes={"manual": True})
        bueno = agente_propuestas.crear(self.conn, 1, self.cid, "registrar_entreno", {
            "manual": True, "day": "2026-11-10", "time": None, "titulo": None,
            "series": [{"ejercicio": "Sentadilla", "reps": 10, "peso_kg": 20.0}],
        }, antes={"manual": True})
        r = self.client.get(f"/asistente/conversacion/{self.cid}")
        self.assertEqual(r.status_code, 200)
        datos = r.json()
        self.assertEqual(len(datos["propuestas"]), 2)
        tarjeta_mala = self._tarjeta(datos, malo["id"])
        tarjeta_buena = self._tarjeta(datos, bueno["id"])
        self.assertEqual(tarjeta_mala["despues"], "No se pudo mostrar esta propuesta.")
        self.assertIsNone(tarjeta_mala["series"])
        self.assertNotEqual(tarjeta_buena["despues"], "No se pudo mostrar esta propuesta.")
        self.assertIn("Sentadilla", str(tarjeta_buena["series"]))

    def test_aplicar_on_the_legacy_card_refuses_without_500(self):
        registro = agente_propuestas.crear(self.conn, 1, self.cid, "registrar_entreno", {
            "manual": True, "day": "2026-11-10", "time": None, "titulo": None,
            "series": [{"ejercicio": "Sentadilla", "reps": "12-15", "peso_kg": 20}],
        }, antes={"manual": True})
        r = self.client.post(f"/asistente/propuestas/{registro['id']}/aplicar")
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.json()["estado"], "aplicada")
        estado = self.conn.execute(
            "SELECT estado FROM propuesta_agente WHERE id = ?", (registro["id"],)
        ).fetchone()["estado"]
        self.assertEqual(estado, "propuesta")
        actividades = self.conn.execute("SELECT COUNT(*) FROM activity WHERE person_id = 1").fetchone()[0]
        self.assertEqual(actividades, 0)


if __name__ == "__main__":
    unittest.main()
