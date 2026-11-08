"""Equality harness for BODY-RENDIMIENTO-2: the reader-optimized read path (`body/lector.py`'s
`Lector`, threaded through `readiness.compute`, `baselines.baseline` and `turnos_costo`'s block
math) must give EXACTLY what the original per-call-query path gave, on a fixture with night
blocks, day-shift blocks, gaps and missing days -- not only on the small fixtures the other
Lector-equality tests (tests/test_baselines.py, tests/test_readiness.py,
tests/test_turnos_costo.py's LectorEqualityTest) use.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import baselines, config, db, readiness, turnos_costo, web
from body.lector import Lector
from body.rutas import turnos as turnos_ruta

METRICS = turnos_costo.MODELO["metricas"]
CONST = {"sleep_minutes": 420.0, "resting_hr": 55.0, "hrv_last_night": 60.0, "stress_avg": 25.0,
         "body_battery_wake": 75.0}


def _mkconn(case: unittest.TestCase, name: str = "t.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _insert_shift(conn, person_id: int, day: str, kind: str = "night") -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _seed_rich_fixture(conn, person_id: int, hoy: date) -> date:
    """~220 days before `hoy`: every metric at a steady value, one day in five left as a gap (no
    metrics at all -- an honest missing day), three night blocks of different lengths and two
    day-shift blocks -- both shift kinds, with plain off days between every block so no two blocks
    touch. Returns the start day."""
    start = hoy - timedelta(220)
    d, wobble = start, 0
    while d < hoy:
        wobble = (wobble + 1) % 5
        if wobble != 4:  # one day in five: no metrics at all
            for m, v in CONST.items():
                db.set_metric(conn, d.isoformat(), m, v + (wobble - 2), "garmin", person_id)
            db.set_metric(conn, d.isoformat(), "sleep_need_min", 480, "garmin", person_id)
        d += timedelta(1)

    for inicio_offset, dias in ((20, 5), (70, 3), (150, 6)):
        d = start + timedelta(inicio_offset)
        for _ in range(dias):
            _insert_shift(conn, person_id, d.isoformat(), kind="night")
            d += timedelta(1)
    for inicio_offset, dias in ((40, 4), (110, 10)):
        d = start + timedelta(inicio_offset)
        for _ in range(dias):
            _insert_shift(conn, person_id, d.isoformat(), kind="day")
            d += timedelta(1)
    return start


class ReadinessStripEqualityTest(unittest.TestCase):
    """Every day of /shifts' own 56-day strip, with the reader readiness.compute would use there,
    equals the original per-day query path, field by field."""

    def test_every_strip_day_matches(self):
        conn = _mkconn(self)
        hoy = date(2026, 9, 1)
        _seed_rich_fixture(conn, 1, hoy)
        conn.commit()
        start = hoy - timedelta(55)
        # +1 on top of the baseline window: the stress part's baseline is computed on yesterday,
        # so the earliest strip day's own window reaches one day further back (body/rutas/turnos.py).
        lector = Lector(conn, 1, (start - timedelta(baselines.WINDOW_DAYS + 1)).isoformat(),
                         hoy.isoformat(), readiness.METRICS)
        for i in range(56):
            d = (start + timedelta(i)).isoformat()
            with_lector = readiness.compute(conn, d, 1, lector=lector)
            without = readiness.compute(conn, d, 1)
            self.assertEqual(with_lector.score, without.score, d)
            self.assertEqual(with_lector.version, without.version, d)
            self.assertEqual(with_lector.missing, without.missing, d)
            parts_with = [(p.key, p.value, p.reference, p.subscore, p.weight) for p in with_lector.parts]
            parts_without = [(p.key, p.value, p.reference, p.subscore, p.weight) for p in without.parts]
            self.assertEqual(parts_with, parts_without, d)


class ResumenEqualityTest(unittest.TestCase):
    """turnos_costo.resumen() with its own reader equals the pre-reader path -- forced by
    patching turnos_costo.Lector to a stub returning None, so every `lector=` kwarg below falls
    back to its original branch (BODY-RENDIMIENTO-2's own test-only switch)."""

    def _resumen_sin_lector(self, conn, person_id: int, hoy: date) -> dict:
        with mock.patch.object(turnos_costo, "Lector", lambda *a, **kw: None):
            return turnos_costo.resumen(conn, person_id, hoy=hoy)

    def test_matches_for_several_hoy_values(self):
        conn = _mkconn(self)
        hoy = date(2026, 9, 1)
        _seed_rich_fixture(conn, 1, hoy)
        conn.commit()
        # includes a probe with no night block yet (hoy far before the first block) and one deep
        # inside the fixture's gaps (missing data).
        for probe in (hoy, hoy - timedelta(90), hoy - timedelta(200), date(2026, 1, 1)):
            with_lector = turnos_costo.resumen(conn, 1, hoy=probe)
            without = self._resumen_sin_lector(conn, 1, probe)
            self.assertEqual(with_lector, without, probe)

    def test_matches_with_no_night_block_at_all(self):
        conn = _mkconn(self, "sin_bloques.db")
        hoy = date(2026, 9, 1)
        db.set_metric(conn, (hoy - timedelta(1)).isoformat(), "resting_hr", 55.0, "garmin", 1)
        conn.commit()
        with_lector = turnos_costo.resumen(conn, 1, hoy=hoy)
        without = self._resumen_sin_lector(conn, 1, hoy)
        self.assertEqual(with_lector, without)
        self.assertEqual(with_lector["motivo"], "sin_turnos")


class ShiftsHtmlEqualityTest(unittest.TestCase):
    """The /shifts page's own rendered HTML, with the reader path exercised normally versus the
    pre-reader path forced by the same test-only switch as ResumenEqualityTest (patching the
    `Lector` name in both body.rutas.turnos and body.turnos_costo, where each is imported, to a
    stub returning None -- every `lector=` kwarg this slice added then takes its `is None`
    branch, the original per-call query code, unchanged)."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self) -> None:
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "shtml.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_shifts_html_is_byte_identical_with_and_without_the_reader(self):
        conn = self._reset_db()
        hoy = date.today()
        _seed_rich_fixture(conn, 1, hoy)
        conn.commit()

        with_reader = self.client.get("/shifts")
        self.assertEqual(with_reader.status_code, 200)

        def _no_lector(*a, **kw):
            return None

        with mock.patch.object(turnos_ruta, "Lector", _no_lector), \
             mock.patch.object(turnos_costo, "Lector", _no_lector):
            without_reader = self.client.get("/shifts")
        self.assertEqual(without_reader.status_code, 200)

        self.assertEqual(with_reader.text, without_reader.text)


if __name__ == "__main__":
    unittest.main()
