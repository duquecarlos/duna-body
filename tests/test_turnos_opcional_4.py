"""BODY-TURNOS-OPCIONAL-4: `vincular-este-pc` in Primeros pasos is listed only for a hosted person
it applies to (orchestrator ruling BODY-TURNOS-OPCIONAL-1): someone who works shifts
(`db.trabaja_turnos_si`) or who already has a device key (`dispositivo.listar` non-empty). Nobody
else is asked to run a script. Invented demo data only, no real health figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, dispositivo, privacidad, tutoriales


def _fresh_conn(case: unittest.TestCase):
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


def _ids(conn, person_id, hosted):
    return {i["id"] for i in tutoriales.primeros_pasos(conn, person_id, hosted=hosted)}


class SettingAndDeviceMatrixTest(unittest.TestCase):
    """Hosted person, every combination of the stored `trabaja_turnos` setting (NULL / 0 / 1) and
    whether a device key exists -- the step is present exactly when the setting is 1, or a device
    key exists, or both."""

    def setUp(self):
        self.conn = _fresh_conn(self)

    def _add_device(self):
        dispositivo.crear(self.conn, 1, "Mi PC", {"turnos:subir"})

    # setting NULL (never answered) ------------------------------------------------------------

    def test_null_no_device_is_absent(self):
        self.assertNotIn("vincular-este-pc", _ids(self.conn, 1, hosted=True))

    def test_null_with_device_is_present(self):
        self._add_device()
        self.assertIn("vincular-este-pc", _ids(self.conn, 1, hosted=True))

    # setting 0 (answered "no") -----------------------------------------------------------------

    def test_no_no_device_is_absent(self):
        db.set_trabaja_turnos(self.conn, False, 1)
        self.assertNotIn("vincular-este-pc", _ids(self.conn, 1, hosted=True))

    def test_no_with_device_is_present(self):
        db.set_trabaja_turnos(self.conn, False, 1)
        self._add_device()
        self.assertIn("vincular-este-pc", _ids(self.conn, 1, hosted=True))

    # setting 1 (answered "si") -----------------------------------------------------------------

    def test_si_no_device_is_present_but_unticked(self):
        db.set_trabaja_turnos(self.conn, True, 1)
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertIn("vincular-este-pc", items)
        self.assertFalse(items["vincular-este-pc"])

    def test_si_with_device_is_present_and_ticked(self):
        db.set_trabaja_turnos(self.conn, True, 1)
        self._add_device()
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertIn("vincular-este-pc", items)
        self.assertTrue(items["vincular-este-pc"])

    # the item's own tick rule (vinculado) is unchanged: present-but-unticked without a device ----

    def test_device_alone_without_turnos_setting_ticks_it(self):
        self._add_device()
        items = {i["id"]: i["hecho"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)}
        self.assertTrue(items["vincular-este-pc"])


class LocalModeNeverListsItTest(unittest.TestCase):
    """Local mode is unchanged: the step is skipped regardless of the setting or a device key."""

    def setUp(self):
        self.conn = _fresh_conn(self)

    def test_null_local_mode_absent(self):
        self.assertNotIn("vincular-este-pc", _ids(self.conn, 1, hosted=False))

    def test_si_with_device_local_mode_still_absent(self):
        db.set_trabaja_turnos(self.conn, True, 1)
        dispositivo.crear(self.conn, 1, "Mi PC", {"turnos:subir"})
        self.assertNotIn("vincular-este-pc", _ids(self.conn, 1, hosted=False))


class AbsentItemKeepsOrderAndCanCompleteTest(unittest.TestCase):
    """When `vincular-este-pc` does not apply, the remaining items keep their relative order, and
    the checklist can be entirely done without it."""

    def setUp(self):
        self.conn = _fresh_conn(self)

    def test_other_items_keep_their_order(self):
        ids = [i["id"] for i in tutoriales.primeros_pasos(self.conn, 1, hosted=True)]
        self.assertEqual(
            ids,
            ["iniciar-sesion-y-aceptar", "conectar-garmin", "trabajas-por-turnos",
             "primera-mirada-a-hoy"],
        )

    def test_checklist_completes_without_the_pc_item(self):
        privacidad.record_consent(self.conn, 1)
        db.set_metric(self.conn, "2026-01-01", "resting_hr", 58, "garmin", 1)
        db.set_trabaja_turnos(self.conn, False, 1)
        items = tutoriales.primeros_pasos(self.conn, 1, hosted=True)
        self.assertNotIn("vincular-este-pc", {i["id"] for i in items})
        self.assertTrue(all(i["hecho"] for i in items))


if __name__ == "__main__":
    unittest.main()
