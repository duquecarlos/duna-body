"""BODY-GARMIN-IMPORT-1: body.importar_garmin end to end, against tiny invented FIT fixtures
(tests/_fit_fixtures.py) zipped up to look like Carlos's real Garmin account export. No network,
no real health figure -- every sample here is made up for the test.
"""
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn
from tests import _fit_fixtures as ff

from body import db, garmin_adapter, importar_garmin as ig

ZONA = "America/Bogota"  # UTC-5 year round -- the person's default timezone


def _mkconn(case: unittest.TestCase, name: str = "ig.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _u(y, m, d, hh, mm) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


def _make_zip(case: unittest.TestCase, inner_files: dict[str, bytes]) -> Path:
    zip_path = Path(tempfile.mkdtemp(dir=TMP)) / "export.zip"
    ff.build_export_zip(zip_path, inner_files)
    return zip_path


class RoundTripTest(unittest.TestCase):
    """One monitoring_b file, well inside a single local day."""

    def setUp(self):
        self.conn = _mkconn(self)
        self.person_id = db.local_person_id(self.conn)
        # 10:00 and 10:05 UTC on 2026-02-01 -> 05:00/05:05 local (America/Bogota, UTC-5), same day.
        hr = [(_u(2026, 2, 1, 10, 0), 58), (_u(2026, 2, 1, 10, 5), 61)]
        stress = [(_u(2026, 2, 1, 10, 0), 20), (_u(2026, 2, 1, 10, 5), 25)]
        self.zip_path = _make_zip(self, {"acct_1.fit": ff.monitoring_b_fit_file(hr, stress)})

    def test_imports_heart_rate_and_stress_for_the_day(self):
        r = ig.importar(self.conn, self.person_id, self.zip_path)
        self.assertEqual(r.dias_importados, {"intradia_fc": 1, "intradia_estres": 1})
        self.assertEqual(r.primer_dia, "2026-02-01")
        self.assertEqual(r.ultimo_dia, "2026-02-01")
        self.assertEqual(r.archivos_con_error, 0)
        self.assertEqual(r.conteo_tipos[ig.TYPE_MONITORING_B], 1)

        pairs_fc = db.intradia(self.conn, self.person_id, "2026-02-01", "intradia_fc", ZONA)
        self.assertEqual(pairs_fc, [("2026-02-01T05:00", 58.0), ("2026-02-01T05:05", 61.0)])
        pairs_estres = db.intradia(self.conn, self.person_id, "2026-02-01", "intradia_estres", ZONA)
        self.assertEqual(pairs_estres, [("2026-02-01T05:00", 20.0), ("2026-02-01T05:05", 25.0)])


class MidnightCrossingTest(unittest.TestCase):
    def test_samples_either_side_of_local_midnight_land_in_different_days(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        # local = UTC - 5h: 2026-02-02 04:30 UTC -> 2026-02-01 23:30 local;
        # 2026-02-02 05:30 UTC -> 2026-02-02 00:30 local.
        hr = [(_u(2026, 2, 2, 4, 30), 55), (_u(2026, 2, 2, 5, 30), 50)]
        zip_path = _make_zip(self, {"acct_1.fit": ff.monitoring_b_fit_file(hr)})
        r = ig.importar(conn, person_id, zip_path)
        self.assertEqual(r.primer_dia, "2026-02-01")
        self.assertEqual(r.ultimo_dia, "2026-02-02")
        before = db.intradia(conn, person_id, "2026-02-01", "intradia_fc", ZONA)
        after = db.intradia(conn, person_id, "2026-02-02", "intradia_fc", ZONA)
        self.assertEqual(before, [("2026-02-01T23:30", 55.0)])
        self.assertEqual(after, [("2026-02-02T00:30", 50.0)])


class DeDuplicationTest(unittest.TestCase):
    def test_the_same_timestamp_from_two_overlapping_files_counts_once(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        ts = _u(2026, 2, 1, 10, 0)
        file1 = ff.monitoring_b_fit_file(hr_samples=[(ts, 58)])
        file2 = ff.monitoring_b_fit_file(hr_samples=[(ts, 58), (ts + 300, 62)])
        zip_path = _make_zip(self, {"acct_1.fit": file1, "acct_2.fit": file2})
        ig.importar(conn, person_id, zip_path)
        pairs = db.intradia(conn, person_id, "2026-02-01", "intradia_fc", ZONA)
        self.assertEqual(len(pairs), 2)  # not 3 -- the shared timestamp counted once


class ActivityFilesLeftAloneTest(unittest.TestCase):
    def test_an_activity_type_file_contributes_no_intraday_data(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        zip_path = _make_zip(self, {"acct_1.fit": ff.activity_fit_file()})
        r = ig.importar(conn, person_id, zip_path)
        self.assertEqual(r.dias_importados, {"intradia_fc": 0, "intradia_estres": 0})
        self.assertEqual(r.conteo_tipos[ff.TYPE_ACTIVITY], 1)


class CorruptFileTest(unittest.TestCase):
    def test_a_corrupt_file_is_counted_as_an_error_others_still_import(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        good = ff.monitoring_b_fit_file(hr_samples=[(_u(2026, 2, 1, 10, 0), 58)])
        zip_path = _make_zip(self, {"acct_1.fit": good, "acct_2.fit": ff.corrupt_fit_bytes()})
        r = ig.importar(conn, person_id, zip_path)
        self.assertEqual(r.archivos_con_error, 1)
        self.assertEqual(r.dias_importados["intradia_fc"], 1)


class ApiPrecedenceTest(unittest.TestCase):
    """db.intradia returns the same pairs for an export day as for an API day built from the same
    invented samples, and an API row always wins over an export row for the same day/kind."""

    def setUp(self):
        self.conn = _mkconn(self)
        self.person_id = db.local_person_id(self.conn)
        self.day = "2026-02-01"
        ts = _u(2026, 2, 1, 10, 0)
        self.zip_path = _make_zip(self, {"acct_1.fit": ff.monitoring_b_fit_file(hr_samples=[(ts, 58)])})

    def test_export_day_matches_an_api_day_built_from_the_same_sample(self):
        ig.importar(self.conn, self.person_id, self.zip_path)
        via_export = db.intradia(self.conn, self.person_id, self.day, "intradia_fc", ZONA)

        other_day = "2026-02-05"
        ts_ms = _u(2026, 2, 5, 10, 0) * 1000
        db.guardar_intradia(self.conn, self.person_id, other_day, "intradia_fc",
                             {"heartRateValues": [[ts_ms, 58]]})  # default source=garmin (the API)
        via_api = db.intradia(self.conn, self.person_id, other_day, "intradia_fc", ZONA)

        self.assertEqual([v for _, v in via_export], [v for _, v in via_api])
        self.assertEqual(via_export[0][0][11:], via_api[0][0][11:])  # same time-of-day

    def test_an_api_row_always_wins_over_an_export_row(self):
        ts_ms = _u(2026, 2, 1, 10, 0) * 1000
        db.guardar_intradia(self.conn, self.person_id, self.day, "intradia_fc",
                             {"heartRateValues": [[ts_ms, 99]]})  # the API's own, different, value
        ig.importar(self.conn, self.person_id, self.zip_path)  # export has 58 for the same instant
        pairs = db.intradia(self.conn, self.person_id, self.day, "intradia_fc", ZONA)
        self.assertEqual([v for _, v in pairs], [99.0])

    def test_a_day_the_api_already_covers_completely_is_not_reimported(self):
        with mock.patch("body.db.intradia_almacenado", return_value=True):
            r = ig.importar(self.conn, self.person_id, self.zip_path)
        self.assertEqual(r.dias_omitidos_api, 1)
        self.assertEqual(r.dias_importados, {"intradia_fc": 0, "intradia_estres": 0})
        pairs = db.intradia(self.conn, self.person_id, self.day, "intradia_fc", ZONA)
        self.assertEqual(pairs, [])  # nothing written -- the API path, not simulated here, owns it


class IdempotentTest(unittest.TestCase):
    def test_a_second_run_leaves_stored_data_unchanged(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        hr = [(_u(2026, 2, 1, 10, 0), 58), (_u(2026, 2, 1, 10, 5), 61)]
        stress = [(_u(2026, 2, 1, 10, 0), 20)]
        zip_path = _make_zip(self, {"acct_1.fit": ff.monitoring_b_fit_file(hr, stress)})

        r1 = ig.importar(conn, person_id, zip_path)
        before_fc = db.intradia(conn, person_id, "2026-02-01", "intradia_fc", ZONA)
        before_estres = db.intradia(conn, person_id, "2026-02-01", "intradia_estres", ZONA)
        row_count_before = conn.execute("SELECT COUNT(*) AS n FROM raw_payload").fetchone()["n"]

        r2 = ig.importar(conn, person_id, zip_path)
        after_fc = db.intradia(conn, person_id, "2026-02-01", "intradia_fc", ZONA)
        after_estres = db.intradia(conn, person_id, "2026-02-01", "intradia_estres", ZONA)
        row_count_after = conn.execute("SELECT COUNT(*) AS n FROM raw_payload").fetchone()["n"]

        self.assertEqual(r1.dias_importados, r2.dias_importados)
        self.assertEqual(before_fc, after_fc)
        self.assertEqual(before_estres, after_estres)
        self.assertEqual(row_count_before, row_count_after)


class DrySeco(unittest.TestCase):
    def test_seco_counts_without_writing(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        hr = [(_u(2026, 2, 1, 10, 0), 58)]
        zip_path = _make_zip(self, {"acct_1.fit": ff.monitoring_b_fit_file(hr)})
        r = ig.importar(conn, person_id, zip_path, seco=True)
        self.assertEqual(r.dias_importados["intradia_fc"], 1)
        row_count = conn.execute("SELECT COUNT(*) AS n FROM raw_payload").fetchone()["n"]
        self.assertEqual(row_count, 0)
        self.assertEqual(db.intradia(conn, person_id, "2026-02-01", "intradia_fc", ZONA), [])


class DesdeHastaTest(unittest.TestCase):
    def test_desde_hasta_restrict_which_days_are_imported(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        hr = [(_u(2026, 2, 1, 10, 0), 58), (_u(2026, 2, 3, 10, 0), 60), (_u(2026, 2, 5, 10, 0), 62)]
        zip_path = _make_zip(self, {"acct_1.fit": ff.monitoring_b_fit_file(hr)})
        from datetime import date
        r = ig.importar(conn, person_id, zip_path, desde=date(2026, 2, 2), hasta=date(2026, 2, 4))
        self.assertEqual(r.primer_dia, "2026-02-03")
        self.assertEqual(r.ultimo_dia, "2026-02-03")
        self.assertEqual(db.intradia(conn, person_id, "2026-02-01", "intradia_fc", ZONA), [])
        self.assertEqual(db.intradia(conn, person_id, "2026-02-05", "intradia_fc", ZONA), [])
        self.assertNotEqual(db.intradia(conn, person_id, "2026-02-03", "intradia_fc", ZONA), [])


class ProfileNeverOpenedTest(unittest.TestCase):
    def test_the_profile_entry_is_never_read(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        zip_path = _make_zip(self, {"acct_1.fit": ff.monitoring_b_fit_file([(_u(2026, 2, 1, 10, 0), 58)])})
        real_open = zipfile.ZipFile.open
        opened_names = []

        def _spy_open(self_zip, name, *a, **kw):
            opened_names.append(name if isinstance(name, str) else name.filename)
            return real_open(self_zip, name, *a, **kw)

        with mock.patch.object(zipfile.ZipFile, "open", _spy_open):
            ig.importar(conn, person_id, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            self.assertIn("Profile/profile.json", z.namelist())  # sanity: the entry really exists
        self.assertFalse(any("profile" in n.lower() for n in opened_names))


if __name__ == "__main__":
    unittest.main()
