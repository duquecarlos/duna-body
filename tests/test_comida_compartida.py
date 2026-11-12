"""BODY-COMIDA-COMPARTIDA-1: one cooks, several eat, each eater's own log gets their own portion.
Rulings 1-3 (consent, schema, the caller-agnostic service) -- rulings 4-7 (the assistant's own
flow, the Hoy card, privacidad and demo) are covered by their own test modules. Invented demo data
only, no real health figures.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import comida_compartida as cc
from body import compartir, db, household

DAY = "2026-11-10"
HOGAR_ID = "hogar-test-1"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Ana')")
    conn.commit()
    return conn


def _en_hogar(conn, *person_ids: int) -> None:
    now = "2026-11-01T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, ?)",
        [(pid, HOGAR_ID, now) for pid in person_ids],
    )
    conn.commit()


_NUTRIENTES_AUTOR = {"protein_g": 30.0, "kcal": 500.0}
_NUTRIENTES_ANA = {"protein_g": 15.0, "kcal": 250.0}


class FreshDbSchemaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_comida_compartida_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(comida_compartida)")}
        self.assertEqual(cols, {"id", "hogar_id", "autor_person_id", "day", "ts_local", "origen",
                                 "origen_ref", "created_at"})

    def test_comida_compartida_porcion_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(comida_compartida_porcion)")}
        self.assertEqual(cols, {"id", "comida_compartida_id", "person_id", "descripcion",
                                 "porcion_texto", "nutrientes", "estado", "comida_id", "decided_at"})

    def test_consentimiento_table_has_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(consentimiento_comida_compartida)")}
        self.assertEqual(cols, {"id", "person_id", "hogar_id", "granted_at", "withdrawn_at"})

    def test_tables_start_empty(self):
        for table in ("comida_compartida", "comida_compartida_porcion", "consentimiento_comida_compartida"):
            n = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(n, 0, table)


class ConsentimientoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _en_hogar(self.conn, 1, 2)

    def test_off_by_default(self):
        self.assertFalse(compartir.recibe_comidas(self.conn, 2))

    def test_activar_turns_it_on(self):
        compartir.activar_recibir_comidas(self.conn, 2)
        self.assertTrue(compartir.recibe_comidas(self.conn, 2))

    def test_activar_without_a_household_refuses(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'Solo')")
        self.conn.commit()
        with self.assertRaises(ValueError):
            compartir.activar_recibir_comidas(self.conn, 3)

    def test_activar_twice_is_a_no_op(self):
        compartir.activar_recibir_comidas(self.conn, 2)
        compartir.activar_recibir_comidas(self.conn, 2)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM consentimiento_comida_compartida WHERE person_id = 2"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_desactivar_turns_it_off(self):
        compartir.activar_recibir_comidas(self.conn, 2)
        compartir.desactivar_recibir_comidas(self.conn, 2)
        self.assertFalse(compartir.recibe_comidas(self.conn, 2))

    def test_desactivar_with_nothing_active_is_a_no_op(self):
        compartir.desactivar_recibir_comidas(self.conn, 2)  # must not raise
        self.assertFalse(compartir.recibe_comidas(self.conn, 2))


# ---- compartir_comida: validation before any write ---------------------------------------------

class CompartirComidaValidationTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _en_hogar(self.conn, 1, 2)

    def _comensales(self, **extra_ana):
        ana = {"person_id": 2, "nutrientes": _NUTRIENTES_ANA}
        ana.update(extra_ana)
        return [{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR}, ana]

    def test_no_active_household_refuses(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'Solo')")
        self.conn.commit()
        with self.assertRaises(ValueError):
            cc.compartir_comida(self.conn, 3, day=DAY, comensales=[{"person_id": 3, "nutrientes": _NUTRIENTES_AUTOR}],
                                 origen="body")

    def test_eater_outside_the_household_is_refused_by_id(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'Fuera')")
        self.conn.commit()
        comensales = [{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR},
                      {"person_id": 3, "nutrientes": _NUTRIENTES_ANA}]
        with self.assertRaises(cc.ComensalFueraDeHogar) as ctx:
            cc.compartir_comida(self.conn, 1, day=DAY, comensales=comensales, origen="body")
        self.assertEqual(ctx.exception.person_id, 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida_compartida").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 0)

    def test_eater_without_consent_is_refused_by_name_nothing_written(self):
        with self.assertRaises(cc.ComensalSinConsentimiento) as ctx:
            cc.compartir_comida(self.conn, 1, day=DAY, comensales=self._comensales(), origen="body")
        self.assertEqual(ctx.exception.person_id, 2)
        self.assertEqual(ctx.exception.display_name, "Ana")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida_compartida").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], 0)

    def test_repeated_person_id_is_refused(self):
        compartir.activar_recibir_comidas(self.conn, 2)
        comensales = [{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR},
                      {"person_id": 2, "nutrientes": _NUTRIENTES_ANA},
                      {"person_id": 2, "nutrientes": _NUTRIENTES_ANA}]
        with self.assertRaises(ValueError):
            cc.compartir_comida(self.conn, 1, day=DAY, comensales=comensales, origen="body")

    def test_missing_author_portion_is_refused(self):
        compartir.activar_recibir_comidas(self.conn, 2)
        with self.assertRaises(ValueError):
            cc.compartir_comida(self.conn, 1, day=DAY,
                                 comensales=[{"person_id": 2, "nutrientes": _NUTRIENTES_ANA}], origen="body")

    def test_bad_nutrientes_is_refused(self):
        compartir.activar_recibir_comidas(self.conn, 2)
        comensales = [{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR},
                      {"person_id": 2, "nutrientes": {"protein_g": -5.0}}]
        with self.assertRaises(ValueError):
            cc.compartir_comida(self.conn, 1, day=DAY, comensales=comensales, origen="body")


# ---- compartir_comida: author applied at once, others pending ----------------------------------

class CompartirComidaWriteTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _en_hogar(self.conn, 1, 2)
        compartir.activar_recibir_comidas(self.conn, 2)

    def _comensales(self):
        return [{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR},
                {"person_id": 2, "nutrientes": _NUTRIENTES_ANA, "porcion_texto": "mitad"}]

    def test_author_portion_is_applied_at_once(self):
        resultado = cc.compartir_comida(self.conn, 1, day=DAY, descripcion="pollo con arroz",
                                         comensales=self._comensales(), origen="body")
        self.assertEqual(resultado["estados"][1], "aplicada")
        comida = self.conn.execute(
            "SELECT COUNT(*) FROM comida WHERE person_id = 1 AND day = ?", (DAY,)
        ).fetchone()[0]
        self.assertEqual(comida, 1)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'protein_g'",
            (DAY,),
        ).fetchone()
        self.assertEqual(row["value"], 30.0)

    def test_other_eater_portion_starts_pending_nothing_in_their_log(self):
        resultado = cc.compartir_comida(self.conn, 1, day=DAY, comensales=self._comensales(), origen="body")
        self.assertEqual(resultado["estados"][2], "pendiente")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida WHERE person_id = 2").fetchone()[0], 0)
        porcion = self.conn.execute(
            "SELECT porcion_texto, comida_id FROM comida_compartida_porcion WHERE comida_compartida_id = ? "
            "AND person_id = 2", (resultado["comida_compartida_id"],),
        ).fetchone()
        self.assertEqual(porcion["porcion_texto"], "mitad")
        self.assertIsNone(porcion["comida_id"])

    def test_idempotent_on_origen_ref(self):
        r1 = cc.compartir_comida(self.conn, 1, day=DAY, comensales=self._comensales(), origen="body",
                                  origen_ref="prop-1")
        r2 = cc.compartir_comida(self.conn, 1, day=DAY, comensales=self._comensales(), origen="body",
                                  origen_ref="prop-1")
        self.assertEqual(r1["comida_compartida_id"], r2["comida_compartida_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida_compartida").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida WHERE person_id = 1").fetchone()[0], 1)


# ---- aplicar_porcion / descartar_porcion: owner only, idempotent --------------------------------

class PorcionTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _en_hogar(self.conn, 1, 2)
        compartir.activar_recibir_comidas(self.conn, 2)
        resultado = cc.compartir_comida(
            self.conn, 1, day=DAY, descripcion="pollo",
            comensales=[{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR},
                        {"person_id": 2, "nutrientes": _NUTRIENTES_ANA}],
            origen="body")
        self.porcion_id = self.conn.execute(
            "SELECT id FROM comida_compartida_porcion WHERE comida_compartida_id = ? AND person_id = 2",
            (resultado["comida_compartida_id"],),
        ).fetchone()["id"]

    def test_another_person_cannot_apply(self):
        with self.assertRaises(cc.PorcionAjena):
            cc.aplicar_porcion(self.conn, 1, self.porcion_id)

    def test_another_person_cannot_discard(self):
        with self.assertRaises(cc.PorcionAjena):
            cc.descartar_porcion(self.conn, 1, self.porcion_id)

    def test_unknown_porcion_id_is_porcion_ajena(self):
        with self.assertRaises(cc.PorcionAjena):
            cc.aplicar_porcion(self.conn, 2, 999999)

    def test_apply_writes_the_eaters_own_comida_and_ingesta(self):
        resultado = cc.aplicar_porcion(self.conn, 2, self.porcion_id)
        self.assertEqual(resultado["estado"], "aplicada")
        cid = self.conn.execute(
            "SELECT comida_id FROM comida_compartida_porcion WHERE id = ?", (self.porcion_id,)
        ).fetchone()["comida_id"]
        self.assertEqual(cid, resultado["comida_id"])
        rows = self.conn.execute("SELECT COUNT(*) FROM ingesta WHERE comida_id = ?", (cid,)).fetchone()[0]
        self.assertEqual(rows, len(_NUTRIENTES_ANA))

    def test_apply_updates_the_eaters_own_derived_day_figure(self):
        cc.aplicar_porcion(self.conn, 2, self.porcion_id)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 2 AND day = ? AND metric_key = 'protein_g'",
            (DAY,),
        ).fetchone()
        self.assertEqual(row["value"], _NUTRIENTES_ANA["protein_g"])

    def test_apply_is_idempotent(self):
        first = cc.aplicar_porcion(self.conn, 2, self.porcion_id)
        second = cc.aplicar_porcion(self.conn, 2, self.porcion_id)
        self.assertEqual(second, first)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida WHERE person_id = 2").fetchone()[0], 1)

    def test_discard_leaves_no_row_in_the_eaters_log(self):
        resultado = cc.descartar_porcion(self.conn, 2, self.porcion_id)
        self.assertEqual(resultado["estado"], "descartada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida WHERE person_id = 2").fetchone()[0], 0)

    def test_discard_is_idempotent(self):
        cc.descartar_porcion(self.conn, 2, self.porcion_id)
        second = cc.descartar_porcion(self.conn, 2, self.porcion_id)
        self.assertEqual(second["estado"], "descartada")

    def test_pendientes_lists_it_for_ana_only(self):
        propios = cc.pendientes(self.conn, 2)
        self.assertEqual(len(propios), 1)
        self.assertEqual(propios[0]["porcion_id"], self.porcion_id)
        self.assertEqual(propios[0]["autor_nombre"], "Me")
        self.assertEqual(cc.pendientes(self.conn, 1), [])

    def test_pendientes_empties_once_applied(self):
        cc.aplicar_porcion(self.conn, 2, self.porcion_id)
        self.assertEqual(cc.pendientes(self.conn, 2), [])


# ---- revoking consent discards pending, keeps applied -------------------------------------------

class RevocarConsentimientoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _en_hogar(self.conn, 1, 2)
        compartir.activar_recibir_comidas(self.conn, 2)

    def test_revoking_discards_every_pending_portion(self):
        resultado = cc.compartir_comida(
            self.conn, 1, day=DAY,
            comensales=[{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR},
                        {"person_id": 2, "nutrientes": _NUTRIENTES_ANA}],
            origen="body")
        compartir.desactivar_recibir_comidas(self.conn, 2)
        cc.descartar_pendientes_de(self.conn, 2)
        estado = self.conn.execute(
            "SELECT estado FROM comida_compartida_porcion WHERE comida_compartida_id = ? AND person_id = 2",
            (resultado["comida_compartida_id"],),
        ).fetchone()["estado"]
        self.assertEqual(estado, "descartada")

    def test_revoking_keeps_an_already_applied_portion(self):
        resultado = cc.compartir_comida(
            self.conn, 1, day=DAY,
            comensales=[{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR},
                        {"person_id": 2, "nutrientes": _NUTRIENTES_ANA}],
            origen="body")
        porcion_id = self.conn.execute(
            "SELECT id FROM comida_compartida_porcion WHERE comida_compartida_id = ? AND person_id = 2",
            (resultado["comida_compartida_id"],),
        ).fetchone()["id"]
        cc.aplicar_porcion(self.conn, 2, porcion_id)
        compartir.desactivar_recibir_comidas(self.conn, 2)
        cc.descartar_pendientes_de(self.conn, 2)
        estado_despues = self.conn.execute(
            "SELECT estado FROM comida_compartida_porcion WHERE id = ?", (porcion_id,)
        ).fetchone()["estado"]
        self.assertEqual(estado_despues, "aplicada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida WHERE person_id = 2").fetchone()[0], 1)


# ---- deshacer_autor: ruling 4's undo ------------------------------------------------------------

class DeshacerAutorTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        _en_hogar(self.conn, 1, 2)
        compartir.activar_recibir_comidas(self.conn, 2)
        self.resultado = cc.compartir_comida(
            self.conn, 1, day=DAY,
            comensales=[{"person_id": 1, "nutrientes": _NUTRIENTES_AUTOR},
                        {"person_id": 2, "nutrientes": _NUTRIENTES_ANA}],
            origen="body")

    def test_removes_the_authors_own_comida(self):
        cc.deshacer_autor(self.conn, 1, self.resultado["comida_compartida_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida WHERE person_id = 1").fetchone()[0], 0)

    def test_discards_the_still_pending_portion(self):
        cc.deshacer_autor(self.conn, 1, self.resultado["comida_compartida_id"])
        estado = self.conn.execute(
            "SELECT estado FROM comida_compartida_porcion WHERE comida_compartida_id = ? AND person_id = 2",
            (self.resultado["comida_compartida_id"],),
        ).fetchone()["estado"]
        self.assertEqual(estado, "descartada")

    def test_leaves_an_already_applied_other_portion_untouched(self):
        porcion_id = self.conn.execute(
            "SELECT id FROM comida_compartida_porcion WHERE comida_compartida_id = ? AND person_id = 2",
            (self.resultado["comida_compartida_id"],),
        ).fetchone()["id"]
        cc.aplicar_porcion(self.conn, 2, porcion_id)
        cc.deshacer_autor(self.conn, 1, self.resultado["comida_compartida_id"])
        row = self.conn.execute(
            "SELECT estado, comida_id FROM comida_compartida_porcion WHERE id = ?", (porcion_id,)
        ).fetchone()
        self.assertEqual(row["estado"], "aplicada")
        self.assertIsNotNone(row["comida_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida WHERE person_id = 2").fetchone()[0], 1)

    def test_is_a_no_op_once_already_undone(self):
        cc.deshacer_autor(self.conn, 1, self.resultado["comida_compartida_id"])
        cc.deshacer_autor(self.conn, 1, self.resultado["comida_compartida_id"])  # must not raise


if __name__ == "__main__":
    unittest.main()
