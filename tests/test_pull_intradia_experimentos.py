"""BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1: experimentos.dias_intradia_necesarios, the pure
read that tells the scheduled pull (body/pull.py) which intraday days an active intraday
experiment still needs -- the window verdict (BODY-EXPERIMENTOS-INTRADIA-MODELO-1) reads minute
data from raw_payload, but nothing fetched it after the one-time backfill without this. No
network; no real health figures, invented demo data only.
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, experimentos, garmin_adapter

FILTRO_LIBRE = {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False}


def _mkconn(case: unittest.TestCase, name: str = "pie.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _crear_intradia(conn, person_id: int, fecha_inicio: str, fecha_fin: str | None = None,
                     metricas=("heart_rate",), estado: str = "activo") -> int:
    ventana = {"tipo": "intradia", "ancla": "despertar", "antes_min": None, "despues_min": 60}
    return db.crear_experimento(conn, person_id, "T", list(metricas), ventana, dict(FILTRO_LIBRE),
                                 fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, estado=estado)


def _crear_dia(conn, person_id: int, fecha_inicio: str) -> int:
    return db.crear_experimento(conn, person_id, "D", ["stress_avg"], {"tipo": "dia"},
                                 dict(FILTRO_LIBRE), fecha_inicio=fecha_inicio)


def _marcar_completo(conn, person_id: int, day: str) -> None:
    """Writes a raw_payload row with a fetched_at far past any day's own close margin, so
    db.intradia_almacenado reads this day as stored and complete -- deterministic, independent of
    the real wall clock (same technique tests/test_momentos_pull.py uses)."""
    conn.execute(
        "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, ?, 'intradia_fc', ?, ?, ?)",
        (person_id, day, garmin_adapter.SOURCE, "2026-12-31T00:00:00+00:00", json.dumps(None)),
    )
    conn.commit()


class SinExperimentoTest(unittest.TestCase):
    def test_no_active_experiment_returns_nothing(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        self.assertEqual(experimentos.dias_intradia_necesarios(conn, person_id, date(2026, 11, 12)), [])


class VentanaDeDiaTest(unittest.TestCase):
    def test_a_whole_day_experiment_never_triggers_a_fetch(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        _crear_dia(conn, person_id, "2026-11-09")
        self.assertEqual(experimentos.dias_intradia_necesarios(conn, person_id, date(2026, 11, 12)), [])


class OtraPersonaTest(unittest.TestCase):
    def test_another_persons_experiment_never_fetches_for_this_person(self):
        conn = _mkconn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        _crear_intradia(conn, 2, "2026-11-09")
        self.assertEqual(experimentos.dias_intradia_necesarios(conn, 1, date(2026, 11, 12)), [])


class EstadoNoActivoTest(unittest.TestCase):
    def test_a_closed_experiment_contributes_no_day(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        _crear_intradia(conn, person_id, "2026-11-09", estado="cerrado")
        self.assertEqual(experimentos.dias_intradia_necesarios(conn, person_id, date(2026, 11, 12)), [])


class DiasFaltantesTest(unittest.TestCase):
    """fecha_inicio 2026-11-09, hoy 2026-11-12: the test window is 11-09..11-11 (up to yesterday);
    with pull.INTRADIA_ALCANCE_DIAS patched to 5, Garmin's reach starts at 11-07 (inclusive, same
    boundary traer_intradia_rango's own refusal uses), so only the newest two baseline days --
    11-08 and 11-07 (fecha_inicio - 1 and - 2) -- fall inside it; the other ~118 baseline days are
    real but out of reach and dropped."""

    def setUp(self):
        self.conn = _mkconn(self)
        self.person_id = db.local_person_id(self.conn)
        self.alcance = mock.patch("body.pull.INTRADIA_ALCANCE_DIAS", 5)
        self.alcance.start()
        self.addCleanup(self.alcance.stop)

    def test_missing_counting_days_newest_first_across_test_and_baseline(self):
        _crear_intradia(self.conn, self.person_id, "2026-11-09")
        dias = experimentos.dias_intradia_necesarios(self.conn, self.person_id, date(2026, 11, 12))
        self.assertEqual(
            dias, ["2026-11-11", "2026-11-10", "2026-11-09", "2026-11-08", "2026-11-07"])

    def test_a_day_already_stored_and_complete_is_skipped(self):
        _crear_intradia(self.conn, self.person_id, "2026-11-09")
        _marcar_completo(self.conn, self.person_id, "2026-11-10")
        dias = experimentos.dias_intradia_necesarios(self.conn, self.person_id, date(2026, 11, 12))
        self.assertEqual(dias, ["2026-11-11", "2026-11-09", "2026-11-08", "2026-11-07"])

    def test_a_day_out_of_garmins_reach_is_dropped(self):
        _crear_intradia(self.conn, self.person_id, "2026-11-01")
        dias = experimentos.dias_intradia_necesarios(self.conn, self.person_id, date(2026, 11, 12))
        self.assertTrue(all(d >= "2026-11-07" for d in dias))

    def test_days_shared_by_two_experiments_are_not_repeated(self):
        _crear_intradia(self.conn, self.person_id, "2026-11-09", metricas=("heart_rate",))
        _crear_intradia(self.conn, self.person_id, "2026-11-10", metricas=("stress",))
        dias = experimentos.dias_intradia_necesarios(self.conn, self.person_id, date(2026, 11, 12))
        self.assertEqual(len(dias), len(set(dias)))
        self.assertEqual(dias, sorted(dias, reverse=True))


if __name__ == "__main__":
    unittest.main()
