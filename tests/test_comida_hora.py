"""ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: `_hora_a_ts_local` becomes a validating parser shared
by every assistant tool that takes `hora` (census: registrar_comida, corregir_comida,
registrar_ingesta, registrar_checkin) -- replacing the old `f"{day}T{hora}:00"`, which stored a
NESTED ts_local (`<day>T<day>T<hh:mm:ss><offset>:00`) on three applied meals when `hora` was itself
already a full timestamp. Invented demo data only, no real health figures, no network.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas

DAY = "2026-11-19"
OTHER_DAY = "2026-11-18"

_CANTIDADES = [{"metric_key": "protein_g", "amount": 35.0, "estimado": True}]


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _proponer_y_aplicar(conn, nombre: str, params: dict, person_id: int = 1):
    out = herramientas.ejecutar(conn, person_id, nombre, params, "ninguno", _cid(conn, person_id))
    return propuestas.aplicar(conn, person_id, out["propuesta_id"])


class HoraFormatosAceptadosTest(unittest.TestCase):
    """registrar_comida is the surface's own repro, exercised through the full propose/apply
    round trip -- the same path that stored the nested ts_local on three real meals."""

    def _registrar(self, hora, day=DAY):
        return _proponer_y_aplicar(
            self._conn, "registrar_comida",
            {"descripcion": "pollo con arroz", "cantidades": _CANTIDADES, "day": day, "hora": hora},
        )

    def setUp(self):
        self._conn = _conn(self)

    def _ts_local(self):
        return self._conn.execute("SELECT ts_local FROM comida ORDER BY id DESC LIMIT 1").fetchone()["ts_local"]

    def test_hhmm_stores_a_flat_ts_local(self):
        self._registrar("07:55")
        self.assertEqual(self._ts_local(), f"{DAY}T07:55:00")

    def test_h_mm_single_digit_hour_stores_a_flat_ts_local(self):
        self._registrar("7:55")
        self.assertEqual(self._ts_local(), f"{DAY}T07:55:00")

    def test_full_iso_with_matching_offset_stores_a_flat_ts_local(self):
        # America/Bogota (the person's own default timezone, db.py's own schema default) is -05:00
        # year-round -- no DST to drift this comparison.
        self._registrar(f"{DAY}T07:55:00-05:00")
        self.assertEqual(self._ts_local(), f"{DAY}T07:55:00")

    def test_full_iso_without_offset_stores_a_flat_ts_local(self):
        self._registrar(f"{DAY}T07:55:00")
        self.assertEqual(self._ts_local(), f"{DAY}T07:55:00")

    def test_day_omitted_takes_the_datetimes_own_local_date(self):
        _proponer_y_aplicar(
            self._conn, "registrar_comida",
            {"descripcion": "pollo con arroz", "cantidades": _CANTIDADES, "hora": f"{DAY}T07:55:00"},
        )
        fila = self._conn.execute("SELECT day, ts_local FROM comida ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(fila["day"], DAY)
        self.assertEqual(fila["ts_local"], f"{DAY}T07:55:00")

    def test_a_datetime_whose_local_date_differs_from_day_is_refused_by_name(self):
        with self.assertRaises(herramientas.ToolError) as cm:
            self._registrar(f"{DAY}T07:55:00", day=OTHER_DAY)
        mensaje = str(cm.exception)
        self.assertIn(DAY, mensaje)
        self.assertIn(OTHER_DAY, mensaje)

    def test_garbage_seconds_only_is_refused_naming_the_accepted_forms(self):
        with self.assertRaises(herramientas.ToolError) as cm:
            self._registrar("07:55:00")
        self.assertIn("HH:MM", str(cm.exception))

    def test_garbage_out_of_range_hour_is_refused_naming_the_accepted_forms(self):
        with self.assertRaises(herramientas.ToolError) as cm:
            self._registrar("25:00")
        self.assertIn("HH:MM", str(cm.exception))

    def test_empty_hora_is_refused_naming_the_accepted_forms(self):
        with self.assertRaises(herramientas.ToolError) as cm:
            self._registrar("")
        self.assertIn("HH:MM", str(cm.exception))


class ComparteElParserCensoTest(unittest.TestCase):
    """registrar_ingesta and registrar_checkin share the exact same parser `registrar_comida`
    does -- the surface's own point: the nested-ts_local defect reached every tool that took
    `hora`, not just the one it was first noticed on."""

    def setUp(self):
        self.conn = _conn(self)

    def test_registrar_ingesta_stores_a_flat_ts_local_from_a_full_iso_hora(self):
        _proponer_y_aplicar(
            self.conn, "registrar_ingesta",
            {"metric_key": "protein_g", "amount": 20.0, "estimado": False,
             "day": DAY, "hora": f"{DAY}T07:55:00-05:00"},
        )
        fila = self.conn.execute("SELECT ts_local FROM ingesta ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(fila["ts_local"], f"{DAY}T07:55:00")

    def test_registrar_checkin_stores_a_flat_ts_local_from_a_full_iso_hora(self):
        _proponer_y_aplicar(
            self.conn, "registrar_checkin",
            {"momento": "otro", "mood": 4, "day": DAY, "hora": f"{DAY}T07:55:00-05:00"},
        )
        fila = self.conn.execute("SELECT ts_local FROM checkin ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(fila["ts_local"], f"{DAY}T07:55:00")

    def test_registrar_ingesta_still_refuses_garbage_hora_by_name(self):
        with self.assertRaises(herramientas.ToolError) as cm:
            herramientas.ejecutar(
                self.conn, 1, "registrar_ingesta",
                {"metric_key": "protein_g", "amount": 20.0, "estimado": False, "day": DAY, "hora": "25:00"},
                "ninguno", _cid(self.conn),
            )
        self.assertIn("HH:MM", str(cm.exception))


class CorregirComidaHoraTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.comida_id = db.crear_comida(self.conn, 1, DAY, _CANTIDADES, "pollo con arroz", f"{DAY}T13:00:00")
        self.conn.commit()

    def _corregir(self, **params):
        return _proponer_y_aplicar(self.conn, "corregir_comida", {"comida_id": self.comida_id, **params})

    def test_bare_clock_hora_keeps_the_existing_day(self):
        self._corregir(hora="08:00")
        fila = self.conn.execute("SELECT day, ts_local FROM comida WHERE id = ?", (self.comida_id,)).fetchone()
        self.assertEqual(fila["day"], DAY)
        self.assertEqual(fila["ts_local"], f"{DAY}T08:00:00")

    def test_full_iso_hora_moves_the_day_when_day_is_not_given(self):
        self._corregir(hora=f"{OTHER_DAY}T08:00:00")
        fila = self.conn.execute("SELECT day, ts_local FROM comida WHERE id = ?", (self.comida_id,)).fetchone()
        self.assertEqual(fila["day"], OTHER_DAY)
        self.assertEqual(fila["ts_local"], f"{OTHER_DAY}T08:00:00")

    def test_full_iso_hora_mismatched_against_an_explicit_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError) as cm:
            self._corregir(hora=f"{OTHER_DAY}T08:00:00", day=DAY)
        mensaje = str(cm.exception)
        self.assertIn(DAY, mensaje)
        self.assertIn(OTHER_DAY, mensaje)
        fila = self.conn.execute("SELECT day, ts_local FROM comida WHERE id = ?", (self.comida_id,)).fetchone()
        self.assertEqual(fila["ts_local"], f"{DAY}T13:00:00")  # untouched: the whole call is refused


class MomentoListaSusPropiasClavesTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: 'momento desconocido' names the person's own
    allowed claves (comidas.momentos -- their own rows, or DEFAULT_MOMENTOS when they have none),
    never a hardcoded list, and never a synonym guess ('mañana' maps to nothing)."""

    def setUp(self):
        self.conn = _conn(self)

    def _registrar(self, momento):
        herramientas.ejecutar(
            self.conn, 1, "registrar_comida",
            {"descripcion": "pollo con arroz", "cantidades": _CANTIDADES, "day": DAY, "momento": momento},
            "ninguno", _cid(self.conn),
        )

    def test_an_unknown_word_is_refused_naming_the_defaults(self):
        with self.assertRaises(herramientas.ToolError) as cm:
            self._registrar("mañana")
        mensaje = str(cm.exception)
        self.assertIn("mañana", mensaje)
        for clave in ("desayuno", "almuerzo", "merienda", "cena"):
            self.assertIn(clave, mensaje)

    def test_an_unknown_word_is_refused_naming_the_persons_own_claves(self):
        self.conn.execute(
            "INSERT INTO momento_comida (person_id, clave, desde_hora, hasta_hora, esperado, orden) "
            "VALUES (1, 'media_noche', 0, 4, 1, 1)")
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError) as cm:
            self._registrar("mañana")
        mensaje = str(cm.exception)
        self.assertIn("media_noche", mensaje)
        self.assertNotIn("desayuno", mensaje)  # the person's own claves REPLACE the defaults, never add to them

    def test_a_known_clave_is_accepted(self):
        self._registrar("desayuno")  # no raise


class LeerDiaComidaIdTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: leer_dia's `comidas` entries carry the id a later
    corregir_comida call needs -- comida_id for a real meal, ingesta_id for a legacy stand-alone
    row -- never another person's."""

    def setUp(self):
        self.conn = _conn(self)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()

    def test_comida_id_is_present_and_usable_by_corregir_comida(self):
        db.crear_comida(self.conn, 1, DAY, _CANTIDADES, "pollo con arroz", f"{DAY}T13:00:00")
        self.conn.commit()
        leido = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        comida_id = leido["comidas"][0]["comida_id"]
        herramientas.ejecutar(self.conn, 1, "corregir_comida", {"comida_id": comida_id, "hora": "09:00"},
                              "ninguno", _cid(self.conn))  # no raise: the id read back is a real one

    def test_ingesta_id_is_present_for_a_legacy_stand_alone_row(self):
        db.registrar_ingesta(self.conn, 1, DAY, "protein_g", 20.0, False, "batido")
        self.conn.commit()
        leido = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertIn("ingesta_id", leido["comidas"][0])
        self.assertNotIn("comida_id", leido["comidas"][0])

    def test_another_persons_comida_id_is_never_returned(self):
        db.crear_comida(self.conn, 1, DAY, _CANTIDADES, "propia")
        db.crear_comida(self.conn, 2, DAY, _CANTIDADES, "ajena")
        self.conn.commit()
        leido = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertEqual(len(leido["comidas"]), 1)
        propio_id = leido["comidas"][0]["comida_id"]
        ajeno_id = self.conn.execute(
            "SELECT id FROM comida WHERE person_id = 2").fetchone()["id"]
        self.assertNotEqual(propio_id, ajeno_id)


if __name__ == "__main__":
    unittest.main()
