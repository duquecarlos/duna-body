"""body/turnos_costo.py: Turnos' hero (BODY-TURNOS-COSTO-1) -- block detection, the "tus semanas de
día" reference and its normal band (body/baselines.py's rule, reused), the signed cost of a night
block, when recovery is reached, and the median hero figure with its honest reason codes. Also the
/shifts and /shifts/como screens and the assistant's leer_costo_turnos tool, on invented demo data."""
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, turnos_costo, web
from body.agente import herramientas
from body.i18n import T

METRICS = turnos_costo.MODELO["metricas"]
CONST = {"sleep_minutes": 420.0, "resting_hr": 55.0, "hrv_last_night": 60.0, "stress_avg": 25.0,
         "body_battery_wake": 75.0}
OFF_DELTA = {"sleep_minutes": -80.0, "resting_hr": 10.0, "hrv_last_night": -20.0, "stress_avg": 15.0,
             "body_battery_wake": -25.0}
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _mkconn():
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


def _insert_shift(conn, person_id: int, day: str, kind: str = "night") -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _fill_range(conn, person_id: int, start: date, end: date, values: dict[str, float]) -> None:
    d = start
    while d <= end:
        for m, v in values.items():
            db.set_metric(conn, d.isoformat(), m, v, "garmin", person_id)
        d += timedelta(1)


def _seed_block(conn, person_id: int, inicio: date, dias: int, recovery_target: int) -> date:
    """A night block of `dias` nights starting at `inicio`; the `recovery_target - 1` days right
    after it get off-band values, and the rest of the range is left at the CONST baseline already
    filled by `_fill_range` -- so the block recovers, by construction, on exactly `recovery_target`."""
    fin = inicio + timedelta(dias - 1)
    d = inicio
    while d <= fin:
        _insert_shift(conn, person_id, d.isoformat())
        d += timedelta(1)
    for i in range(1, recovery_target):
        day = fin + timedelta(i)
        for m in METRICS:
            db.set_metric(conn, day.isoformat(), m, CONST[m] + OFF_DELTA[m], "garmin", person_id)
    return fin


class BloquesNocheTest(unittest.TestCase):
    def test_consecutive_nights_form_one_block(self):
        conn = _mkconn()
        for day in ("2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"):
            _insert_shift(conn, 1, day)
        bloques = turnos_costo.bloques_noche(conn, 1, "2026-03-01", "2026-03-10")
        self.assertEqual(len(bloques), 1)
        self.assertEqual((bloques[0].inicio, bloques[0].fin, bloques[0].dias), ("2026-03-02", "2026-03-06", 5))

    def test_an_off_day_splits_the_run(self):
        conn = _mkconn()
        for day in ("2026-03-02", "2026-03-03"):
            _insert_shift(conn, 1, day)
        _insert_shift(conn, 1, "2026-03-04", kind="off")
        for day in ("2026-03-05", "2026-03-06"):
            _insert_shift(conn, 1, day)
        bloques = turnos_costo.bloques_noche(conn, 1, "2026-03-01", "2026-03-10")
        self.assertEqual(len(bloques), 2)
        self.assertEqual([b.dias for b in bloques], [2, 2])

    def test_a_single_night_is_a_block_of_one(self):
        conn = _mkconn()
        _insert_shift(conn, 1, "2026-03-02")
        bloques = turnos_costo.bloques_noche(conn, 1, "2026-03-01", "2026-03-10")
        self.assertEqual(len(bloques), 1)
        self.assertEqual(bloques[0].dias, 1)

    def test_person_b_shifts_never_affect_person_a(self):
        conn = _mkconn()
        conn.execute("INSERT INTO person (id, display_name, timezone, language) VALUES (2, 'B', "
                     "'America/Bogota', 'es')")
        for day in ("2026-03-02", "2026-03-03", "2026-03-04"):
            _insert_shift(conn, 2, day)
        bloques_a = turnos_costo.bloques_noche(conn, 1, "2026-03-01", "2026-03-10")
        bloques_b = turnos_costo.bloques_noche(conn, 2, "2026-03-01", "2026-03-10")
        self.assertEqual(bloques_a, [])
        self.assertEqual(len(bloques_b), 1)


class ReferenciaTest(unittest.TestCase):
    def test_mean_and_band_match_the_baseline_quantile_rule(self):
        import statistics
        conn = _mkconn()
        hoy = date(2026, 6, 1)
        vals = list(range(1, 16))  # 15 distinct values, well over min_dias_referencia (10)
        start = hoy - timedelta(120)
        for i, v in enumerate(vals):
            db.set_metric(conn, (start + timedelta(i)).isoformat(), "resting_hr", float(v), "garmin", 1)
        ref = turnos_costo.referencia(conn, 1, hoy)
        r = ref["resting_hr"]
        self.assertIsNotNone(r)
        q = statistics.quantiles(vals, n=4)
        self.assertAlmostEqual(r.media, statistics.fmean(vals))
        self.assertAlmostEqual(r.bajo, q[0])
        self.assertAlmostEqual(r.alto, q[2])
        self.assertEqual(r.n, 15)

    def test_fewer_than_min_dias_referencia_is_a_gap(self):
        conn = _mkconn()
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(120)
        for i in range(5):  # fewer than MODELO['min_dias_referencia'] (10)
            db.set_metric(conn, (start + timedelta(i)).isoformat(), "resting_hr", 55.0, "garmin", 1)
        ref = turnos_costo.referencia(conn, 1, hoy)
        self.assertIsNone(ref["resting_hr"])

    def test_excludes_night_days_and_the_post_window(self):
        conn = _mkconn()
        hoy = date(2026, 6, 1)
        start, end = hoy - timedelta(120), hoy - timedelta(1)
        _fill_range(conn, 1, start, end, {"resting_hr": 55.0})
        block_fin = _seed_block(conn, 1, date(2026, 4, 1), 5, recovery_target=1)
        # the block's own nights and its post-window got overwritten with an outlier value
        d = date(2026, 4, 1)
        while d <= block_fin:
            db.set_metric(conn, d.isoformat(), "resting_hr", 999.0, "garmin", 1)
            d += timedelta(1)
        for i in range(1, turnos_costo.MODELO["ventana_post"] + 1):
            db.set_metric(conn, (block_fin + timedelta(i)).isoformat(), "resting_hr", 999.0, "garmin", 1)
        ref = turnos_costo.referencia(conn, 1, hoy)
        self.assertEqual(ref["resting_hr"].media, 55.0)  # the 999s never entered the reference


class CostoBloqueTest(unittest.TestCase):
    def test_a_lower_resting_hr_during_nights_is_not_worse(self):
        conn = _mkconn()
        bloque = turnos_costo.Bloque(inicio="2026-04-01", fin="2026-04-05", dias=5)
        ref = {"resting_hr": turnos_costo.Referencia(media=60.0, bajo=55.0, alto=65.0, n=20)}
        d = date(2026, 4, 1)
        while d <= date(2026, 4, 7):  # block + dias_estables (2) days after
            db.set_metric(conn, d.isoformat(), "resting_hr", 50.0, "garmin", 1)  # lower than the reference
            d += timedelta(1)
        costo = turnos_costo._costo_bloque(conn, 1, bloque, ref)
        self.assertLess(costo["resting_hr"], 0)  # negative: a lower resting HR is NOT worse

    def test_higher_resting_hr_during_nights_is_worse(self):
        conn = _mkconn()
        bloque = turnos_costo.Bloque(inicio="2026-04-01", fin="2026-04-05", dias=5)
        ref = {"resting_hr": turnos_costo.Referencia(media=60.0, bajo=55.0, alto=65.0, n=20)}
        d = date(2026, 4, 1)
        while d <= date(2026, 4, 7):
            db.set_metric(conn, d.isoformat(), "resting_hr", 70.0, "garmin", 1)
            d += timedelta(1)
        costo = turnos_costo._costo_bloque(conn, 1, bloque, ref)
        self.assertGreater(costo["resting_hr"], 0)

    def test_missing_values_stay_missing(self):
        conn = _mkconn()
        bloque = turnos_costo.Bloque(inicio="2026-04-01", fin="2026-04-05", dias=5)
        ref = {"resting_hr": turnos_costo.Referencia(media=60.0, bajo=55.0, alto=65.0, n=20)}
        costo = turnos_costo._costo_bloque(conn, 1, bloque, ref)
        self.assertIsNone(costo["resting_hr"])

    def test_no_reference_is_a_gap(self):
        conn = _mkconn()
        bloque = turnos_costo.Bloque(inicio="2026-04-01", fin="2026-04-05", dias=5)
        costo = turnos_costo._costo_bloque(conn, 1, bloque, {"resting_hr": None})
        self.assertIsNone(costo["resting_hr"])


class RecuperacionBloqueTest(unittest.TestCase):
    BAND = {m: turnos_costo.Referencia(media=50.0, bajo=40.0, alto=60.0, n=20) for m in METRICS}
    IN, OUT = 50.0, 100.0

    def _set_day(self, conn, day: date, n_in_band: int):
        for i, m in enumerate(METRICS):
            db.set_metric(conn, day.isoformat(), m, self.IN if i < n_in_band else self.OUT, "garmin", 1)

    def test_recovery_found_when_three_of_five_are_back_for_two_days(self):
        conn = _mkconn()
        fin = date(2026, 1, 10)
        bloque = turnos_costo.Bloque(inicio="2026-01-06", fin=fin.isoformat(), dias=5)
        self._set_day(conn, fin + timedelta(1), 2)  # d=1: only 2 in band
        self._set_day(conn, fin + timedelta(2), 2)  # d=2: only 2 in band
        self._set_day(conn, fin + timedelta(3), 3)  # d=3: 3 in band
        self._set_day(conn, fin + timedelta(4), 3)  # d=4: 3 in band -- window(3,4) qualifies
        hoy = date(2026, 2, 1)
        recup = turnos_costo._recuperacion_bloque(conn, 1, bloque, self.BAND, hoy)
        self.assertEqual(recup, 3)

    def test_not_reached_within_the_window_is_none(self):
        conn = _mkconn()
        fin = date(2026, 1, 10)
        bloque = turnos_costo.Bloque(inicio="2026-01-06", fin=fin.isoformat(), dias=5)
        for i in range(1, turnos_costo.MODELO["ventana_post"] + 2):
            self._set_day(conn, fin + timedelta(i), 1)  # never enough metrics in band
        hoy = date(2026, 2, 1)
        recup = turnos_costo._recuperacion_bloque(conn, 1, bloque, self.BAND, hoy)
        self.assertIsNone(recup)

    def test_fewer_than_three_metrics_with_values_is_a_gap(self):
        conn = _mkconn()
        fin = date(2026, 1, 10)
        bloque = turnos_costo.Bloque(inicio="2026-01-06", fin=fin.isoformat(), dias=5)
        hoy = date(2026, 2, 1)  # no values set at all for the scan window
        recup = turnos_costo._recuperacion_bloque(conn, 1, bloque, self.BAND, hoy)
        self.assertIsNone(recup)


class ResumenTest(unittest.TestCase):
    HOY = date(2026, 6, 1)

    def _seed_three_blocks(self, conn, person_id: int):
        start, end = self.HOY - timedelta(140), self.HOY - timedelta(1)
        _fill_range(conn, person_id, start, end, CONST)
        _seed_block(conn, person_id, date(2026, 2, 25), 5, recovery_target=6)
        _seed_block(conn, person_id, date(2026, 3, 28), 5, recovery_target=4)
        _seed_block(conn, person_id, date(2026, 4, 27), 5, recovery_target=2)

    def test_median_over_blocks_with_a_figure(self):
        conn = _mkconn()
        self._seed_three_blocks(conn, 1)
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertEqual(r["dias_recuperacion"], 4)  # median of [6, 4, 2]
        self.assertIsNone(r["motivo"])
        self.assertEqual(r["bloques_con_recuperacion"], 3)
        self.assertEqual(len(r["bloques"]), 3)

    def test_person_isolation(self):
        conn = _mkconn()
        self._seed_three_blocks(conn, 1)
        conn.execute("INSERT INTO person (id, display_name, timezone, language) VALUES (2, 'B', "
                     "'America/Bogota', 'es')")
        r2 = turnos_costo.resumen(conn, 2, hoy=self.HOY)
        self.assertEqual(r2["motivo"], "sin_datos_reloj")
        r1 = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertEqual(r1["dias_recuperacion"], 4)

    def test_sin_datos_reloj(self):
        conn = _mkconn()
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertIsNone(r["dias_recuperacion"])
        self.assertEqual(r["motivo"], "sin_datos_reloj")

    def test_sin_turnos(self):
        conn = _mkconn()
        db.set_metric(conn, "2026-05-01", "resting_hr", 55.0, "garmin", 1)
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertEqual(r["motivo"], "sin_turnos")

    def test_sin_noches(self):
        conn = _mkconn()
        db.set_metric(conn, "2026-05-01", "resting_hr", 55.0, "garmin", 1)
        _insert_shift(conn, 1, "2026-05-01", kind="day")
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertEqual(r["motivo"], "sin_noches")

    def test_pocos_bloques(self):
        conn = _mkconn()
        db.set_metric(conn, "2026-05-01", "resting_hr", 55.0, "garmin", 1)
        for day in ("2026-05-10", "2026-05-11", "2026-05-12"):
            _insert_shift(conn, 1, day)
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertIsNone(r["dias_recuperacion"])
        self.assertEqual(r["motivo"], "pocos_bloques")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


def _no_day_keyed_values(obj) -> bool:
    """True when nothing in `obj` is a dict keyed by an ISO day mapped to a plain number -- the
    nivel 'resumen' guard: aggregates over a week or more, never a single day's value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _ISO_DAY.match(k) and isinstance(v, (int, float)):
                return False
            if not _no_day_keyed_values(v):
                return False
        return True
    if isinstance(obj, list):
        return all(_no_day_keyed_values(x) for x in obj)
    return True


class WebAndToolTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "w.db"
        conn = db.connect()
        db.init(conn)
        return conn

    def _seed_today_anchored(self, conn):
        today = date.today()
        _fill_range(conn, 1, today - timedelta(140), today - timedelta(1), CONST)
        _seed_block(conn, 1, today - timedelta(100), 5, recovery_target=2)
        _seed_block(conn, 1, today - timedelta(40), 5, recovery_target=2)
        conn.commit()
        return today

    def test_shifts_renders_hero_rows_and_strip_in_spanish(self):
        conn = self._reset_db()
        self._seed_today_anchored(conn)
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("≈", r.text)
        self.assertIn(_t("es", "shifts.cost_vs"), r.text)
        self.assertIn(_t("es", "metric.resting_hr"), r.text)
        self.assertEqual(r.text.count('href="/day/'), 56)

    def test_shifts_renders_in_english(self):
        conn = self._reset_db()
        self._seed_today_anchored(conn)
        self.client.post("/settings/language", data={"lang": "en"})
        try:
            r = self.client.get("/shifts")
            self.assertEqual(r.status_code, 200)
            self.assertIn(_t("en", "shifts.cost_vs"), r.text)
        finally:
            self.client.post("/settings/language", data={"lang": "es"})

    def test_empty_state_with_no_shifts(self):
        self._reset_db()
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "shifts.reason.sin_datos_reloj"), r.text)

    def test_como_names_the_model_version(self):
        conn = self._reset_db()
        self._seed_today_anchored(conn)
        r = self.client.get("/shifts/como")
        self.assertEqual(r.status_code, 200)
        self.assertIn("turnos-v1", r.text)

    def test_assistant_tool_has_no_per_day_values(self):
        conn = self._reset_db()
        self._seed_today_anchored(conn)
        out = herramientas.ejecutar(conn, 1, "leer_costo_turnos", {}, "resumen")
        self.assertTrue(_no_day_keyed_values(out), "leer_costo_turnos payload keys a metric value by day")
        json.dumps(out)  # also: JSON-serializable


if __name__ == "__main__":
    unittest.main()
