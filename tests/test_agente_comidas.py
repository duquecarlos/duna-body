"""ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: a meal proposed through `registrar_comida` and applied
reads back through leer_dia's own `comidas` list with its description and amounts -- the full
propose/apply/read round trip, never just `db.crear_comida` called directly. Invented demo data
only, no real health figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas

DAY = "2026-11-10"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


class RegistrarComidaReadsBackTest(unittest.TestCase):
    def test_a_meal_logged_through_the_applier_reads_back_in_leer_dia(self):
        conn = _conn(self)
        cid = _cid(conn)
        out = herramientas.ejecutar(
            conn, 1, "registrar_comida",
            {"descripcion": "2 huevos y una pechuga",
             "cantidades": [{"metric_key": "protein_g", "amount": 35.0, "estimado": True},
                             {"metric_key": "kcal", "amount": 450.0, "estimado": True}],
             "day": DAY, "hora": "13:00"},
            "ninguno", cid,
        )
        resultado = propuestas.aplicar(conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")

        leido = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(len(leido["comidas"]), 1)
        comida = leido["comidas"][0]
        self.assertEqual(comida["descripcion"], "2 huevos y una pechuga")
        self.assertEqual(comida["ts_local"], f"{DAY}T13:00:00")
        cantidades = {c["metric_key"]: c["amount"] for c in comida["cantidades"]}
        self.assertEqual(cantidades, {"protein_g": 35.0, "kcal": 450.0})


if __name__ == "__main__":
    unittest.main()
