"""body/turnos_costo.py's per-day-offset profile (BODY-TURNOS-PERFIL-DIA-1): `perfil_por_dia`, read
against the same referencia()/bloques_noche() this module's hero already uses, on invented data."""
import statistics
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, turnos_costo

HOY = date(2026, 6, 1)
# Spread enough (not a single constant) for referencia()'s own 25th-75th band to be real, not
# degenerate -- mean 60.0.
REF_CYCLE = [50.0, 55.0, 60.0, 65.0, 70.0]


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


def _insert_shift(conn, person_id: int, day: str) -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, 'night', 'manual')",
                 (person_id, day))


def _fill_reference(conn, person_id: int, metric: str, hoy: date) -> list[float]:
    start, end = hoy - timedelta(turnos_costo.REFERENCIA_DIAS), hoy - timedelta(1)
    vals, d, i = [], start, 0
    while d <= end:
        v = REF_CYCLE[i % len(REF_CYCLE)]
        db.set_metric(conn, d.isoformat(), metric, v, "garmin", person_id)
        vals.append(v)
        d, i = d + timedelta(1), i + 1
    return vals


def _place_blocks(hoy: date, lengths: list[int], gap: int = 10) -> list[date]:
    """Start dates for each block in `lengths`, laid out back to back with `gap` free days between
    one block's post-window and the next block's own 'antes' day -- entirely before
    hoy - REFERENCIA_DIAS, so none of these invented days can ever enter referencia()'s own
    [hoy - REFERENCIA_DIAS, hoy - 1] window and a test's expected reference figures never drift
    once the blocks are seeded."""
    end_limit = hoy - timedelta(turnos_costo.REFERENCIA_DIAS + 6)  # +6 clears post_4 (fin + 4) with margin
    total = sum(lengths) + gap * (len(lengths) - 1)
    start = end_limit - timedelta(total - 1)
    assert start >= hoy - timedelta(turnos_costo.RESUMEN_DIAS)
    inicios, cur = [], start
    for n in lengths:
        inicios.append(cur)
        cur += timedelta(n + gap)
    return inicios


def _seed_block(conn, person_id: int, metric: str, inicio: date, dias: int,
                 valores: dict[str, float | None]) -> date:
    """A night block of `dias` nights starting at `inicio`; `valores` maps an OFFSET name to the
    value `metric` gets that day -- an offset left out of `valores`, or mapped to None, is never
    written at all (an honest missing day, never a zero)."""
    fin = inicio + timedelta(dias - 1)
    d = inicio
    while d <= fin:
        _insert_shift(conn, person_id, d.isoformat())
        d += timedelta(1)
    bloque = turnos_costo.Bloque(inicio=inicio.isoformat(), fin=fin.isoformat(), dias=dias)
    for offset, v in valores.items():
        if v is None:
            continue
        dia = turnos_costo._offset_day(bloque, offset)
        if dia is not None:
            db.set_metric(conn, dia.isoformat(), metric, v, "garmin", person_id)
    return fin


class PerfilPorDiaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn(self)
        self.hrv_vals = _fill_reference(self.conn, 1, "hrv_last_night", HOY)
        self.hrv_media = statistics.fmean(self.hrv_vals)
        q = statistics.quantiles(self.hrv_vals, n=4)
        self.hrv_bajo, self.hrv_alto = q[0], q[2]
        self.banda_bajo_diff = self.hrv_bajo - self.hrv_media
        self.banda_alto_diff = self.hrv_alto - self.hrv_media

    def _seed(self, lengths: list[int], valores: dict[str, float | None], metric: str = "hrv_last_night"):
        for inicio, dias in zip(_place_blocks(HOY, lengths), lengths):
            _seed_block(self.conn, 1, metric, inicio, dias, valores)

    def test_two_and_three_night_blocks_average_each_offset_and_band_from_referencia(self):
        lengths = [2, 3, 2]
        noche_1_diffs = [-18.0, -20.0, -22.0]  # distinct per block -- the mean must be -20, not any one
        for inicio, dias, diff in zip(_place_blocks(HOY, lengths), lengths, noche_1_diffs):
            valores = {"noche_1": self.hrv_media + diff, "noche_ultima": self.hrv_media + self.banda_bajo_diff - 24,
                       "post_1": self.hrv_media + self.banda_bajo_diff - 10,
                       "post_2": self.hrv_media + self.banda_bajo_diff - 6,
                       "post_3": self.hrv_media + self.banda_bajo_diff - 2, "post_4": self.hrv_media}
            _seed_block(self.conn, 1, "hrv_last_night", inicio, dias, valores)
        p = turnos_costo.perfil_por_dia(self.conn, 1, "hrv_last_night", HOY)
        self.assertIsNone(p["motivo"])
        self.assertEqual(p["n_bloques"], 3)
        self.assertEqual(p["noches_tipicas"], 2)  # median of [2, 3, 2]
        by_offset = {o["offset"]: o for o in p["offsets"]}
        self.assertEqual(by_offset["noche_1"]["n"], 3)
        self.assertAlmostEqual(by_offset["noche_1"]["media"], -20.0, delta=0.15)
        self.assertEqual(by_offset["noche_ultima"]["n"], 3)
        self.assertAlmostEqual(by_offset["noche_ultima"]["media"], self.banda_bajo_diff - 24, delta=0.15)
        self.assertNotIn("antes", by_offset)  # never set for any block -- skipped, not a zero row
        # referencia()'s own centre and band, re-expressed around 0 (ruling 1): centro is the day-week
        # reference mean; banda is its 25th-75th percentile shifted by that same mean.
        self.assertAlmostEqual(p["centro"], round(self.hrv_media, 1), places=1)
        self.assertAlmostEqual(p["banda"]["bajo"], round(self.banda_bajo_diff, 1), places=1)
        self.assertAlmostEqual(p["banda"]["alto"], round(self.banda_alto_diff, 1), places=1)

    def test_missing_day_is_skipped_not_zeroed(self):
        con_antes = {"noche_1": self.hrv_media - 5, "antes": self.hrv_media + 10}
        sin_antes = {"noche_1": self.hrv_media - 5}  # 'antes' never written for this block
        for inicio, dias, valores in zip(_place_blocks(HOY, [2, 2, 2]), [2, 2, 2],
                                          [con_antes, con_antes, sin_antes]):
            _seed_block(self.conn, 1, "hrv_last_night", inicio, dias, valores)
        p = turnos_costo.perfil_por_dia(self.conn, 1, "hrv_last_night", HOY)
        by_offset = {o["offset"]: o for o in p["offsets"]}
        self.assertEqual(by_offset["antes"]["n"], 2)  # the third block's missing day never counted
        self.assertAlmostEqual(by_offset["antes"]["media"], 10.0, delta=0.15)  # not (10+10+0)/3

    def test_mas_bajo_honours_a_higher_is_better_metric(self):
        valores = {"noche_1": self.hrv_media - 10, "noche_ultima": self.hrv_media - 30,
                   "post_1": self.hrv_media - 5}
        self._seed([2, 2, 2], valores)
        p = turnos_costo.perfil_por_dia(self.conn, 1, "hrv_last_night", HOY)
        self.assertEqual(p["mas_bajo"], "noche_ultima")  # hrv: higher is better, lower is worse
        self.assertNotIn("mas_alto", p)

    def test_mas_alto_honours_a_lower_is_better_metric(self):
        rhr_vals = _fill_reference(self.conn, 1, "resting_hr", HOY)
        rhr_media = statistics.fmean(rhr_vals)
        valores = {"noche_1": rhr_media + 12, "noche_ultima": rhr_media + 25, "post_1": rhr_media + 6}
        self._seed([2, 2, 2], valores, metric="resting_hr")
        p = turnos_costo.perfil_por_dia(self.conn, 1, "resting_hr", HOY)
        self.assertEqual(p["mas_alto"], "noche_ultima")  # resting_hr: lower is better, higher is worse
        self.assertNotIn("mas_bajo", p)

    def test_vuelta_none_when_never_back_in_band(self):
        fuera = self.banda_bajo_diff - 8
        valores = {"noche_1": self.hrv_media - 15, "post_1": self.hrv_media + fuera,
                   "post_2": self.hrv_media + fuera, "post_3": self.hrv_media + fuera,
                   "post_4": self.hrv_media + fuera}
        self._seed([2, 2, 2], valores)
        p = turnos_costo.perfil_por_dia(self.conn, 1, "hrv_last_night", HOY)
        self.assertIsNone(p["vuelta"])

    def test_vuelta_is_the_first_post_offset_back_in_band(self):
        fuera = self.banda_bajo_diff - 8
        valores = {"noche_1": self.hrv_media - 15, "post_1": self.hrv_media + fuera,
                   "post_2": self.hrv_media + fuera, "post_3": self.hrv_media,
                   "post_4": self.hrv_media}
        self._seed([2, 2, 2], valores)
        p = turnos_costo.perfil_por_dia(self.conn, 1, "hrv_last_night", HOY)
        self.assertEqual(p["vuelta"], "post_3")

    def test_fewer_than_min_bloques_previos_is_a_reason_code(self):
        valores = {"noche_1": self.hrv_media - 10}
        self._seed([2, 2], valores)  # fewer than MIN_BLOQUES_PREVIOS (3)
        p = turnos_costo.perfil_por_dia(self.conn, 1, "hrv_last_night", HOY)
        self.assertEqual(p, {"metric": "hrv_last_night", "motivo": "pocos_bloques_previos"})

    def test_no_reference_is_a_reason_code(self):
        conn = _mkconn(self)  # no _fill_reference at all -- the metric has no reference yet
        for inicio, dias in zip(_place_blocks(HOY, [2, 2, 2]), [2, 2, 2]):
            _seed_block(conn, 1, "hrv_last_night", inicio, dias, {"noche_1": 40.0})
        p = turnos_costo.perfil_por_dia(conn, 1, "hrv_last_night", HOY)
        self.assertEqual(p["motivo"], "pocos_bloques_previos")
        self.assertNotIn("offsets", p)


class ResumenCarriesPerfilTest(unittest.TestCase):
    """BODY-TURNOS-PERFIL-DIA-1 ruling 2: additive -- the hero's own figures (VERSION, every
    existing key) stay exactly as resumen() already reported them, with perfil_hrv added alongside."""

    def test_perfil_hrv_is_additive_and_version_unchanged(self):
        conn = _mkconn(self)
        _fill_reference(conn, 1, "hrv_last_night", HOY)
        for inicio, dias in zip(_place_blocks(HOY, [2, 2, 2]), [2, 2, 2]):
            _seed_block(conn, 1, "hrv_last_night", inicio, dias, {"noche_1": 40.0})
        r = turnos_costo.resumen(conn, 1, hoy=HOY)
        self.assertEqual(r["version"], "turnos-v1")
        self.assertIn("perfil_hrv", r)
        self.assertEqual(r["perfil_hrv"], turnos_costo.perfil_por_dia(conn, 1, "hrv_last_night", HOY))


if __name__ == "__main__":
    unittest.main()
