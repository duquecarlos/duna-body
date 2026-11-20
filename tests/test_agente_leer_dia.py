"""ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: leer_dia's own `comidas` list, through
`db.comidas_del_dia` -- a meal keeps its own description and time, grouped with every one of its
amounts; a legacy stand-alone ingesta row (no comida parent) still reads as its own one-item meal.
Added alongside the tool's existing `ingesta` list, never replacing it. Invented demo data only,
no real health figures.

ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: each entry also carries the id a later `corregir_comida`
call needs -- `comida_id` for a real meal, `ingesta_id` for a legacy stand-alone row -- read
through a second, bounded query in `body/agente/herramientas.py` (db.py is NOT edited).
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

    def test_a_real_meal_carries_its_own_comida_id_never_an_ingesta_id(self):
        conn = _conn(self)
        comida_id = db.crear_comida(conn, 1, DAY,
                                    [{"metric_key": "protein_g", "amount": 35.0, "estimado": True}],
                                    descripcion="pollo con arroz")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(out["comidas"][0]["comida_id"], comida_id)
        self.assertNotIn("ingesta_id", out["comidas"][0])

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

    def test_a_legacy_stand_alone_ingesta_row_carries_its_own_ingesta_id_never_a_comida_id(self):
        conn = _conn(self)
        ingesta_id = db.registrar_ingesta(conn, 1, DAY, "protein_g", 20.0, False, "batido")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(out["comidas"][0]["ingesta_id"], ingesta_id)
        self.assertNotIn("comida_id", out["comidas"][0])

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
        ajeno_id = db.crear_comida(conn, 2, DAY, [{"metric_key": "protein_g", "amount": 40.0, "estimado": True}],
                                    descripcion="ajena")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        descripciones = {c["descripcion"] for c in out["comidas"]}
        self.assertEqual(descripciones, {"propia"})
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: the new comida_id carries the same person
        # scoping -- never person 2's own id, even though it exists in the same table.
        self.assertNotIn(ajeno_id, {c["comida_id"] for c in out["comidas"]})


class ContextoTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-5-CONTEXTO ruling 3: leer_dia's own `contexto` -- that one day's own
    derived shift/night-block standing, trip or marked period, activity load, and alcohol figure
    (body/contexto_dia.py's own module docstring)."""

    def test_contexto_carries_this_days_own_shift(self):
        conn = _conn(self)
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, ?, 'night', 'manual')",
                     (DAY,))
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(out["contexto"]["turno"]["tipo"], "night")

    def test_a_day_with_nothing_derived_has_an_empty_contexto(self):
        conn = _conn(self)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(out["contexto"], {})


class StatementCountTest(unittest.TestCase):
    """BODY-MINUTO-ANALISIS-5-CONTEXTO ruling 4: leer_dia's own statement count, re-measured with
    its new `contexto` (contexto_dia.contexto_dias, a fixed cost independent of this day's own
    data -- tests/test_contexto_dia.py's own guard). Measured on this file's own fixtures: 19 on an
    empty day, 20 on a day carrying a night shift, an activity, a marked period, an alcohol figure
    and a meal. Ceiling gives both a comfortable, named margin: ceil(20 * 1.3) = 26.

    ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: re-measured again with `comidas`' new comida_id/
    ingesta_id (the one bounded extra SELECT `_comidas_del_dia_con_id` adds, always run, on an
    empty day too): 20 on an empty day (19 before), 21 on the full day (20 before). Both stay
    comfortably under the same, unchanged ceiling of 26."""

    _CEILING = 26

    def _contar(self, conn, fn) -> int:
        vistos = []
        conn.set_trace_callback(lambda sql: vistos.append(sql))
        try:
            fn()
        finally:
            conn.set_trace_callback(None)
        return len(vistos)

    def test_an_empty_day_stays_within_the_named_ceiling(self):
        conn = _conn(self)
        conn.commit()
        n = self._contar(conn, lambda: herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo"))
        self.assertLessEqual(n, self._CEILING)

    def test_a_day_carrying_every_new_context_source_stays_within_the_named_ceiling(self):
        conn = _conn(self)
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, ?, 'night', 'manual')",
                     (DAY,))
        conn.execute(
            "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type) "
            "VALUES (1, 'garmin', 'a1', ?, ?, 1800.0, 'running')", (DAY, f"{DAY}T07:00:00"),
        )
        db.crear_periodo(conn, 1, "lesion", DAY, None, zonas=["pierna"])
        db.registrar_ingesta(conn, 1, DAY, "alcohol_g", 5.0, False)
        db.crear_comida(conn, 1, DAY, [{"metric_key": "protein_g", "amount": 35.0, "estimado": True}],
                         descripcion="pollo", ts_local=f"{DAY}T13:00:00")
        conn.commit()
        n = self._contar(conn, lambda: herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo"))
        self.assertLessEqual(n, self._CEILING)


if __name__ == "__main__":
    unittest.main()
