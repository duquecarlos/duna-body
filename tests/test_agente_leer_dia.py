"""ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: leer_dia's own `comidas` list, through
`db.comidas_del_dia` -- a meal keeps its own description and time, grouped with every one of its
amounts; a legacy stand-alone ingesta row (no comida parent) still reads as its own one-item meal.
Added alongside the tool's existing `ingesta` list, never replacing it. Invented demo data only,
no real health figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import herramientas

DAY = "2026-11-10"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


class ComidasGroupedTest(unittest.TestCase):
    def test_a_meal_groups_its_description_and_every_amount(self):
        conn = _conn(self)
        db.crear_comida(conn, 1, DAY,
                         [{"metric_key": "protein_g", "amount": 35.0, "estimado": True},
                          {"metric_key": "kcal", "amount": 650.0, "estimado": True}],
                         descripcion="pollo con arroz", ts_local=f"{DAY}T13:00:00")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(len(out["comidas"]), 1)
        comida = out["comidas"][0]
        self.assertEqual(comida["descripcion"], "pollo con arroz")
        self.assertEqual(comida["ts_local"], f"{DAY}T13:00:00")
        cantidades = {c["metric_key"]: c["amount"] for c in comida["cantidades"]}
        self.assertEqual(cantidades, {"protein_g": 35.0, "kcal": 650.0})

    def test_a_legacy_stand_alone_ingesta_row_reads_as_its_own_one_item_meal(self):
        conn = _conn(self)
        db.registrar_ingesta(conn, 1, DAY, "protein_g", 20.0, False, "batido")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(len(out["comidas"]), 1)
        comida = out["comidas"][0]
        self.assertEqual(comida["descripcion"], "batido")
        self.assertEqual(len(comida["cantidades"]), 1)
        self.assertEqual(comida["cantidades"][0]["metric_key"], "protein_g")

    def test_ingesta_list_still_carries_every_entry_unchanged(self):
        """`comidas` is ADDED, never a replacement -- tests/test_proteina.py's own
        LeerDiaIncludesIngestaTest already reads `ingesta` through this same tool, and that test
        module is outside this slice's own touches."""
        conn = _conn(self)
        db.registrar_ingesta(conn, 1, DAY, "protein_g", 20.0, False, "arroz con pollo")
        db.registrar_ingesta(conn, 1, DAY, "protein_g", 15.0, True, "batido")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(len(out["ingesta"]), 2)
        self.assertEqual(len(out["comidas"]), 2)

    def test_another_persons_meals_are_never_returned(self):
        conn = _conn(self)
        db.crear_comida(conn, 1, DAY, [{"metric_key": "protein_g", "amount": 35.0, "estimado": True}],
                         descripcion="propia")
        db.crear_comida(conn, 2, DAY, [{"metric_key": "protein_g", "amount": 40.0, "estimado": True}],
                         descripcion="ajena")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        descripciones = {c["descripcion"] for c in out["comidas"]}
        self.assertEqual(descripciones, {"propia"})


if __name__ == "__main__":
    unittest.main()
