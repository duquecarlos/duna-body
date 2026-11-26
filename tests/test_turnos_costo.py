"""body/turnos_costo.py: Turnos' hero (BODY-TURNOS-COSTO-1) -- block detection, the "tus semanas de
día" reference and its normal band (body/baselines.py's rule, reused), the signed cost of a night
block, when recovery is reached, and the median hero figure with its honest reason codes. Also the
/shifts and /shifts/como screens and the assistant's leer_costo_turnos tool, on invented demo data."""
import html
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, turnos_costo, web
from body.agente import herramientas
from body.i18n import T
from body.rutas import comun, turnos

METRICS = turnos_costo.MODELO["metricas"]
CONST = {"sleep_minutes": 420.0, "resting_hr": 55.0, "hrv_last_night": 60.0, "stress_avg": 25.0,
         "body_battery_wake": 75.0}
OFF_DELTA = {"sleep_minutes": -80.0, "resting_hr": 10.0, "hrv_last_night": -20.0, "stress_avg": 15.0,
             "body_battery_wake": -25.0}
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
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


def _seed_block_off_baseline(conn, person_id: int, inicio: date, dias: int) -> date:
    """A night block whose OWN nights (not just the recovery tail) carry OFF_DELTA values, on all
    five metrics -- so every row's real difference is non-zero and well clear of UMBRAL_IGUAL,
    for a direction word distinct from "igual"."""
    fin = inicio + timedelta(dias - 1)
    d = inicio
    while d <= fin:
        _insert_shift(conn, person_id, d.isoformat())
        for m in METRICS:
            db.set_metric(conn, d.isoformat(), m, CONST[m] + OFF_DELTA[m], "garmin", person_id)
        d += timedelta(1)
    return fin


class BloquesNocheTest(unittest.TestCase):
    def test_consecutive_nights_form_one_block(self):
        conn = _mkconn(self)
        for day in ("2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"):
            _insert_shift(conn, 1, day)
        bloques = turnos_costo.bloques_noche(conn, 1, "2026-03-01", "2026-03-10")
        self.assertEqual(len(bloques), 1)
        self.assertEqual((bloques[0].inicio, bloques[0].fin, bloques[0].dias), ("2026-03-02", "2026-03-06", 5))

    def test_an_off_day_splits_the_run(self):
        conn = _mkconn(self)
        for day in ("2026-03-02", "2026-03-03"):
            _insert_shift(conn, 1, day)
        _insert_shift(conn, 1, "2026-03-04", kind="off")
        for day in ("2026-03-05", "2026-03-06"):
            _insert_shift(conn, 1, day)
        bloques = turnos_costo.bloques_noche(conn, 1, "2026-03-01", "2026-03-10")
        self.assertEqual(len(bloques), 2)
        self.assertEqual([b.dias for b in bloques], [2, 2])

    def test_a_single_night_is_a_block_of_one(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2026-03-02")
        bloques = turnos_costo.bloques_noche(conn, 1, "2026-03-01", "2026-03-10")
        self.assertEqual(len(bloques), 1)
        self.assertEqual(bloques[0].dias, 1)

    def test_person_b_shifts_never_affect_person_a(self):
        conn = _mkconn(self)
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
        conn = _mkconn(self)
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
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start = hoy - timedelta(120)
        for i in range(5):  # fewer than MODELO['min_dias_referencia'] (10)
            db.set_metric(conn, (start + timedelta(i)).isoformat(), "resting_hr", 55.0, "garmin", 1)
        ref = turnos_costo.referencia(conn, 1, hoy)
        self.assertIsNone(ref["resting_hr"])

    def test_excludes_night_days_and_the_post_window(self):
        conn = _mkconn(self)
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
        conn = _mkconn(self)
        bloque = turnos_costo.Bloque(inicio="2026-04-01", fin="2026-04-05", dias=5)
        ref = {"resting_hr": turnos_costo.Referencia(media=60.0, bajo=55.0, alto=65.0, n=20)}
        d = date(2026, 4, 1)
        while d <= date(2026, 4, 7):  # block + dias_estables (2) days after
            db.set_metric(conn, d.isoformat(), "resting_hr", 50.0, "garmin", 1)  # lower than the reference
            d += timedelta(1)
        costo = turnos_costo._costo_bloque(conn, 1, bloque, ref)
        self.assertLess(costo["resting_hr"], 0)  # negative: a lower resting HR is NOT worse

    def test_higher_resting_hr_during_nights_is_worse(self):
        conn = _mkconn(self)
        bloque = turnos_costo.Bloque(inicio="2026-04-01", fin="2026-04-05", dias=5)
        ref = {"resting_hr": turnos_costo.Referencia(media=60.0, bajo=55.0, alto=65.0, n=20)}
        d = date(2026, 4, 1)
        while d <= date(2026, 4, 7):
            db.set_metric(conn, d.isoformat(), "resting_hr", 70.0, "garmin", 1)
            d += timedelta(1)
        costo = turnos_costo._costo_bloque(conn, 1, bloque, ref)
        self.assertGreater(costo["resting_hr"], 0)

    def test_missing_values_stay_missing(self):
        conn = _mkconn(self)
        bloque = turnos_costo.Bloque(inicio="2026-04-01", fin="2026-04-05", dias=5)
        ref = {"resting_hr": turnos_costo.Referencia(media=60.0, bajo=55.0, alto=65.0, n=20)}
        costo = turnos_costo._costo_bloque(conn, 1, bloque, ref)
        self.assertIsNone(costo["resting_hr"])

    def test_no_reference_is_a_gap(self):
        conn = _mkconn(self)
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
        conn = _mkconn(self)
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
        conn = _mkconn(self)
        fin = date(2026, 1, 10)
        bloque = turnos_costo.Bloque(inicio="2026-01-06", fin=fin.isoformat(), dias=5)
        for i in range(1, turnos_costo.MODELO["ventana_post"] + 2):
            self._set_day(conn, fin + timedelta(i), 1)  # never enough metrics in band
        hoy = date(2026, 2, 1)
        recup = turnos_costo._recuperacion_bloque(conn, 1, bloque, self.BAND, hoy)
        self.assertIsNone(recup)

    def test_fewer_than_three_metrics_with_values_is_a_gap(self):
        conn = _mkconn(self)
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
        conn = _mkconn(self)
        self._seed_three_blocks(conn, 1)
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertEqual(r["dias_recuperacion"], 4)  # median of [6, 4, 2]
        self.assertIsNone(r["motivo"])
        self.assertEqual(r["bloques_con_recuperacion"], 3)
        self.assertEqual(len(r["bloques"]), 3)

    def test_person_isolation(self):
        conn = _mkconn(self)
        self._seed_three_blocks(conn, 1)
        conn.execute("INSERT INTO person (id, display_name, timezone, language) VALUES (2, 'B', "
                     "'America/Bogota', 'es')")
        r2 = turnos_costo.resumen(conn, 2, hoy=self.HOY)
        self.assertEqual(r2["motivo"], "sin_datos_reloj")
        r1 = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertEqual(r1["dias_recuperacion"], 4)

    def test_sin_datos_reloj(self):
        conn = _mkconn(self)
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertIsNone(r["dias_recuperacion"])
        self.assertEqual(r["motivo"], "sin_datos_reloj")

    def test_sin_turnos(self):
        conn = _mkconn(self)
        db.set_metric(conn, "2026-05-01", "resting_hr", 55.0, "garmin", 1)
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertEqual(r["motivo"], "sin_turnos")

    def test_sin_noches(self):
        conn = _mkconn(self)
        db.set_metric(conn, "2026-05-01", "resting_hr", 55.0, "garmin", 1)
        _insert_shift(conn, 1, "2026-05-01", kind="day")
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertEqual(r["motivo"], "sin_noches")

    def test_pocos_bloques(self):
        conn = _mkconn(self)
        db.set_metric(conn, "2026-05-01", "resting_hr", 55.0, "garmin", 1)
        for day in ("2026-05-10", "2026-05-11", "2026-05-12"):
            _insert_shift(conn, 1, day)
        r = turnos_costo.resumen(conn, 1, hoy=self.HOY)
        self.assertIsNone(r["dias_recuperacion"])
        self.assertEqual(r["motivo"], "pocos_bloques")


class LectorEqualityTest(unittest.TestCase):
    """body/lector.py's Lector, threaded through referencia/_costo_bloque/_dia_en_banda/
    _recuperacion_bloque, must give EXACTLY the same answer as the pre-reader, per-call query path
    (BODY-RENDIMIENTO-2). turnos_costo.Lector is the same class as body.lector.Lector."""

    def test_referencia_matches(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        start, end = hoy - timedelta(120), hoy - timedelta(1)
        _fill_range(conn, 1, start, end, CONST)
        without = turnos_costo.referencia(conn, 1, hoy)
        lector = turnos_costo.Lector(conn, 1, start.isoformat(), hoy.isoformat(), METRICS)
        with_lector = turnos_costo.referencia(conn, 1, hoy, lector=lector)
        self.assertEqual(without, with_lector)

    def test_costo_bloque_matches(self):
        conn = _mkconn(self)
        bloque = turnos_costo.Bloque(inicio="2026-04-01", fin="2026-04-05", dias=5)
        ref = {"resting_hr": turnos_costo.Referencia(media=60.0, bajo=55.0, alto=65.0, n=20)}
        d = date(2026, 4, 1)
        while d <= date(2026, 4, 7):
            db.set_metric(conn, d.isoformat(), "resting_hr", 50.0, "garmin", 1)
            d += timedelta(1)
        lector = turnos_costo.Lector(conn, 1, "2026-04-01", "2026-04-07", METRICS)
        without = turnos_costo._costo_bloque(conn, 1, bloque, ref)
        with_lector = turnos_costo._costo_bloque(conn, 1, bloque, ref, lector=lector)
        self.assertEqual(without, with_lector)

    def test_dia_en_banda_matches(self):
        conn = _mkconn(self)
        day = date(2026, 1, 11)
        ref = {m: turnos_costo.Referencia(media=50.0, bajo=40.0, alto=60.0, n=20) for m in METRICS}
        for i, m in enumerate(METRICS):
            db.set_metric(conn, day.isoformat(), m, 50.0 if i < 3 else 100.0, "garmin", 1)
        lector = turnos_costo.Lector(conn, 1, day.isoformat(), day.isoformat(), METRICS)
        without = turnos_costo._dia_en_banda(conn, 1, day.isoformat(), ref)
        with_lector = turnos_costo._dia_en_banda(conn, 1, day.isoformat(), ref, lector=lector)
        self.assertEqual(without, with_lector)

    def test_recuperacion_bloque_matches(self):
        conn = _mkconn(self)
        fin = date(2026, 1, 10)
        bloque = turnos_costo.Bloque(inicio="2026-01-06", fin=fin.isoformat(), dias=5)
        ref = {m: turnos_costo.Referencia(media=50.0, bajo=40.0, alto=60.0, n=20) for m in METRICS}
        for i in range(1, 5):
            day = fin + timedelta(i)
            for j, m in enumerate(METRICS):
                db.set_metric(conn, day.isoformat(), m, 50.0 if j < 3 else 100.0, "garmin", 1)
        hoy = date(2026, 2, 1)
        lector = turnos_costo.Lector(conn, 1, bloque.inicio, hoy.isoformat(), METRICS)
        without = turnos_costo._recuperacion_bloque(conn, 1, bloque, ref, hoy)
        with_lector = turnos_costo._recuperacion_bloque(conn, 1, bloque, ref, hoy, lector=lector)
        self.assertEqual(without, with_lector)


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


class DireccionYPalabraTest(unittest.TestCase):
    """BODY-TURNOS-PALABRA-DIRECCION-1: the row shows the REAL difference (its own sign), not the
    always-positive signed cost, with a plain DIRECTION word -- 'mas'/'menos'/'igual' -- never a
    judgement, whatever the metric's own better-direction is."""

    def test_less_sleep_and_lower_hrv_read_negative_and_menos(self):
        # sleep_minutes and hrv_last_night are both "higher is better" -- a signed cost that is
        # positive (worse) came from a value BELOW the reference, so the real difference is negative.
        diff, palabra = turnos_costo.diferencia_y_palabra("sleep_minutes", 49.0)
        self.assertLess(diff, 0)
        self.assertEqual(palabra, "menos")
        diff, palabra = turnos_costo.diferencia_y_palabra("hrv_last_night", 11.4)
        self.assertLess(diff, 0)
        self.assertEqual(palabra, "menos")

    def test_higher_resting_hr_reads_positive_and_mas(self):
        # resting_hr is "lower is better" -- a positive signed cost came from a value ABOVE the
        # reference, so the real difference stays positive. A HIGHER resting HR must read "mas"
        # (more), never "peor" (worse) -- the word names the direction, not a verdict.
        diff, palabra = turnos_costo.diferencia_y_palabra("resting_hr", 5.0)
        self.assertGreater(diff, 0)
        self.assertEqual(palabra, "mas")

    def test_a_lower_resting_hr_reads_menos_not_mejor(self):
        # A LOWER resting HR during nights is medically good (test_a_lower_resting_hr_during_nights_is_not_worse,
        # above), but the row must still read "menos" (less), never "mejor" (better): the word names
        # the direction the mean moved, not a verdict on whether that direction is good.
        diff, palabra = turnos_costo.diferencia_y_palabra("resting_hr", -5.0)
        self.assertLess(diff, 0)
        self.assertEqual(palabra, "menos")

    def test_under_the_threshold_reads_igual(self):
        diff, palabra = turnos_costo.diferencia_y_palabra("resting_hr", 0.4)  # under UMBRAL_IGUAL["bpm"] (1.0)
        self.assertEqual(palabra, "igual")

    def test_no_metric_ever_reads_a_judgement_word(self):
        """Every metric, both directions of signed cost: the word is always in {'mas', 'menos',
        'igual'}, never 'peor'/'mejor' -- whatever the metric's own better-direction (db.METRIC_DEFS)."""
        for metric in METRICS:
            for costo in (37.0, -37.0):
                _, palabra = turnos_costo.diferencia_y_palabra(metric, costo)
                self.assertIn(palabra, ("mas", "menos", "igual"))


class SubtitlesTest(unittest.TestCase):
    """BODY-TURNOS-COSTO-1-M fix c: a plain explanation, never the metric's own name repeated."""

    def test_no_row_subtitle_equals_its_name(self):
        for lang in ("es", "en"):
            t = T(lang)
            for key in turnos_costo.MODELO["metricas"]:
                name = t("metric." + key)
                sub = t("shifts.cost_sub." + key)
                self.assertNotEqual(sub.casefold(), name.casefold(), f"{lang}/{key}")


class HeroQuestionsTest(unittest.TestCase):
    """BODY-TURNOS-COSTO-1-M fix d: the hero and the rows ask two different questions, and the hero
    shows its own number only once."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "hq.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_hero_question_differs_from_rows_question_and_number_shown_once(self):
        conn = self._reset_db()
        today = date.today()
        _fill_range(conn, 1, today - timedelta(140), today - timedelta(1), CONST)
        _seed_block(conn, 1, today - timedelta(100), 5, recovery_target=2)
        _seed_block(conn, 1, today - timedelta(40), 5, recovery_target=2)
        conn.commit()
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "shifts.hero_q"), r.text)
        self.assertIn(_t("es", "shifts.cost_title"), r.text)
        self.assertNotEqual(_t("es", "shifts.hero_q"), _t("es", "shifts.cost_title"))
        hero = re.search(r'<a class="card est"[^>]*>.*?</a>', r.text, re.S)
        self.assertIsNotNone(hero)
        self.assertEqual(hero.group(0).count("≈"), 1)  # the big figure, not repeated in the sentence below it


class HeroNoVuelveTest(unittest.TestCase):
    """BODY-REVISION-TURNOS-TARJETAS-1 ruling 3: the headline and the caption under it come from
    ONE computation. When HRV's own day-profile never returns to band, the headline says so
    instead of showing a day count (the OTHER 4 metrics' own historical median) the caption would
    otherwise contradict."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def _seed(self, conn, today: date) -> None:
        """Three 2-night blocks: every metric but HRV recovers the day right after (so the hero
        gets a day count), while HRV itself is pushed off-band for the whole block and every
        post-day through the window (so its own day-profile never returns)."""
        _fill_range(conn, 1, today - timedelta(140), today - timedelta(1), CONST)
        ventana = turnos_costo.MODELO["ventana_post"]
        for offset_days in (100, 60, 20):
            inicio = today - timedelta(offset_days)
            fin = inicio + timedelta(1)
            d = inicio
            while d <= fin:
                _insert_shift(conn, 1, d.isoformat())
                db.set_metric(conn, d.isoformat(), "hrv_last_night",
                              CONST["hrv_last_night"] + OFF_DELTA["hrv_last_night"], "garmin", 1)
                d += timedelta(1)
            for i in range(1, ventana + 1):
                day = fin + timedelta(i)
                db.set_metric(conn, day.isoformat(), "hrv_last_night",
                              CONST["hrv_last_night"] + OFF_DELTA["hrv_last_night"], "garmin", 1)

    def test_fixture_actually_disagrees_before_the_fix(self):
        """Proves the seeding reproduces finding 10's own disagreement: a day count AND a
        never-returned HRV profile, from the same resumen() call."""
        conn = self._reset_db("nv_fixture.db")
        today = date.today()
        self._seed(conn, today)
        conn.commit()
        resumen = turnos_costo.resumen(conn, 1, hoy=today)
        self.assertIsNotNone(resumen["dias_recuperacion"])
        patron = turnos_costo.frase_patron(resumen["perfil_hrv"], "hrv_last_night")
        self.assertIsNotNone(patron)
        self.assertEqual(patron["kind"], "no_vuelve")

    def test_headline_says_not_back_yet_instead_of_a_contradicting_number_es(self):
        conn = self._reset_db("nv_es.db")
        today = date.today()
        self._seed(conn, today)
        conn.commit()
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        hero = re.search(r'<a class="card est"[^>]*>.*?</a>', r.text, re.S).group(0)
        self.assertIn(_t("es", "shifts.hero_no_vuelve_fig"), hero)
        self.assertNotIn('<span class="big">', hero)

    def test_headline_says_not_back_yet_instead_of_a_contradicting_number_en(self):
        conn = self._reset_db("nv_en.db")
        today = date.today()
        self._seed(conn, today)
        db.set_language(conn, "en", 1)
        conn.commit()
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        hero = re.search(r'<a class="card est"[^>]*>.*?</a>', r.text, re.S).group(0)
        self.assertIn(_t("en", "shifts.hero_no_vuelve_fig"), hero)
        self.assertNotIn('<span class="big">', hero)


class StripBandsTest(unittest.TestCase):
    """BODY-TURNOS-COSTO-1-M fix e: every shift.kind gets its own class and word, a day without a
    shift reads 'none', a day without readiness shows an em dash, and every cell is labelled."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_morning_and_afternoon_cells_carry_their_kind_classes(self):
        conn = self._reset_db("strip1.db")
        today = date.today()
        _insert_shift(conn, 1, (today - timedelta(5)).isoformat(), kind="morning")
        _insert_shift(conn, 1, (today - timedelta(6)).isoformat(), kind="afternoon")
        conn.commit()
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="strip-cell morning"', r.text)
        self.assertIn('class="strip-cell afternoon"', r.text)
        self.assertIn(_t("es", "shift.kind.morning"), r.text)
        self.assertIn(_t("es", "shift.kind.afternoon"), r.text)
        self.assertNotIn(_t("es", "shift.kind.off"), r.text)  # legend lists only the kinds present

    def test_day_without_readiness_shows_em_dash_and_a_label(self):
        conn = self._reset_db("strip2.db")  # no shifts, no metrics at all
        # a shift worker whose shifts have not arrived yet (ruling BODY-TURNOS-OPCIONAL-1)
        db.set_trabaja_turnos(conn, True, 1)
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        cells = re.findall(r'<a class="strip-cell[^"]*"[^>]*>', r.text)
        self.assertEqual(len(cells), 56)
        self.assertTrue(all('aria-label="' in c for c in cells))
        self.assertIn('class="strip-cell none"', r.text)
        self.assertIn(_t("es", "shifts.sin_dato_corto"), r.text)  # trailing segment of every cell's label
        self.assertIn(">—</a>", r.text)


class StripWeekLabelsTest(unittest.TestCase):
    """BODY-REVISION-TURNOS-TARJETAS-1 ruling 4: the 8-week grid gets a header per week (its own
    starting date), a label naming what the numbers are, and a caption saying what a cell is --
    still 56 cells, still the existing 7-column .strip grid (no new horizontal scroll)."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_eight_week_labels_and_caption_appear_once_per_week(self):
        conn = self._reset_db("stripweeks.db")
        today = date.today()
        db.set_trabaja_turnos(conn, True, 1)
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        start = today - timedelta(55)
        for w in range(8):
            label = turnos._strip_week_label(T("es"), start + timedelta(w * 7),
                                              con_anio=start.year != today.year)
            self.assertIn(_esc(label), r.text)
        self.assertIn(_t("es", "shifts.strip_medida"), r.text)
        self.assertIn(_t("es", "shifts.strip_caption"), r.text)
        cells = re.findall(r'<a class="strip-cell[^"]*"[^>]*>', r.text)
        self.assertEqual(len(cells), 56)

    def test_year_shown_only_when_the_range_crosses_one(self):
        t = T("es")
        same_year = turnos._strip_week_label(t, date(2026, 11, 3), con_anio=False)
        crossing = turnos._strip_week_label(t, date(2025, 12, 29), con_anio=True)
        self.assertNotIn("2026", same_year)
        self.assertIn("2025", crossing)


class ComoListsBlocksTest(unittest.TestCase):
    """BODY-TURNOS-COSTO-1-M fix f: /shifts/como lists the blocks counted, and the not-medical-advice
    line appears at most twice (the lead and the footer, not a third closing duplicate)."""

    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "como.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        return conn

    def test_lists_each_block_with_its_recovery_and_the_phrase_is_not_tripled(self):
        conn = self._reset_db()
        today = date.today()
        _fill_range(conn, 1, today - timedelta(140), today - timedelta(1), CONST)
        _seed_block(conn, 1, today - timedelta(100), 5, recovery_target=2)
        _seed_block(conn, 1, today - timedelta(40), 5, recovery_target=2)
        conn.commit()
        resumen = turnos_costo.resumen(conn, 1, hoy=today)
        r = self.client.get("/shifts/como")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(r.text.lower().count("consejo médico"), 2)
        for b in resumen["bloques"]:
            self.assertIn(comun._short_date(T("es"), date.fromisoformat(b["inicio"])), r.text)
        self.assertIn(_t("es", "shifts.como_bloque_dias", n=2), r.text)


class WebAndToolTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "w.db"
        conn = db.connect()
        self.addCleanup(conn.close)
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
        conn = self._reset_db()
        # a shift worker whose shifts have not arrived yet (ruling BODY-TURNOS-OPCIONAL-1)
        db.set_trabaja_turnos(conn, True, 1)
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

    def _seed_with_direction_words(self, conn):
        today = date.today()
        _fill_range(conn, 1, today - timedelta(140), today - timedelta(1), CONST)
        _seed_block_off_baseline(conn, 1, today - timedelta(100), 5)
        _seed_block_off_baseline(conn, 1, today - timedelta(40), 5)
        conn.commit()

    def test_shifts_word_badges_are_directions_not_verdicts_in_spanish(self):
        """BODY-TURNOS-PALABRA-DIRECCION-1: the word next to each figure is a direction, never a
        judgement -- and the figure itself carries no +/- sign now that the word already says which
        way it moved."""
        conn = self._reset_db()
        self._seed_with_direction_words(conn)
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        rows = re.findall(r'<span class="nw">([^<]*)\s*<strong>([^<]+)</strong>', r.text)
        self.assertTrue(rows)
        badges = [palabra for _value, palabra in rows]
        self.assertEqual(set(badges), {"más", "menos"} | ({"igual"} & set(badges)))
        for b in badges:
            self.assertNotIn(b, ("peor", "mejor"))
        for value, _palabra in rows:
            self.assertNotIn("+", value)
            self.assertNotIn("−", value)
            self.assertNotIn("-", value)

    def test_shifts_word_badges_are_directions_not_verdicts_in_english(self):
        conn = self._reset_db()
        self._seed_with_direction_words(conn)
        self.client.post("/settings/language", data={"lang": "en"})
        try:
            r = self.client.get("/shifts")
            self.assertEqual(r.status_code, 200)
            rows = re.findall(r'<span class="nw">([^<]*)\s*<strong>([^<]+)</strong>', r.text)
            self.assertTrue(rows)
            badges = [palabra for _value, palabra in rows]
            self.assertEqual(set(badges), {"more", "less"} | ({"about the same"} & set(badges)))
            for b in badges:
                self.assertNotIn(b, ("worse", "better"))
        finally:
            self.client.post("/settings/language", data={"lang": "es"})

    def _seed_three_blocks_anchored(self, conn):
        """Three 2-night blocks with a real HRV dip (BODY-TURNOS-PERFIL-DIA-1) -- enough blocks for
        turnos_costo.perfil_por_dia's own MIN_BLOQUES_PREVIOS floor, anchored to date.today() so a
        route-level /shifts request (which reads comun._today_local, never a fixed test date) sees
        them."""
        today = date.today()
        _fill_range(conn, 1, today - timedelta(140), today - timedelta(1), CONST)
        _seed_block_off_baseline(conn, 1, today - timedelta(100), 2)
        _seed_block_off_baseline(conn, 1, today - timedelta(60), 2)
        _seed_block_off_baseline(conn, 1, today - timedelta(20), 2)
        conn.commit()
        return today

    def test_shifts_renders_the_hrv_profile_chart_in_spanish(self):
        conn = self._reset_db()
        self._seed_three_blocks_anchored(conn)
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="chart-svg"', r.text)
        self.assertIn(_t("es", "shifts.perfil.eje.noche_1"), r.text)
        self.assertIn(_t("es", "shifts.perfil.banda"), r.text)
        self.assertIn("+1", r.text)
        self.assertNotIn('style="', r.text)

    def test_shifts_renders_the_hrv_profile_chart_in_english(self):
        conn = self._reset_db()
        self._seed_three_blocks_anchored(conn)
        self.client.post("/settings/language", data={"lang": "en"})
        try:
            r = self.client.get("/shifts")
            self.assertEqual(r.status_code, 200)
            self.assertIn('class="chart-svg"', r.text)
            self.assertIn(_t("en", "shifts.perfil.eje.noche_1"), r.text)
            self.assertIn(_t("en", "shifts.perfil.banda"), r.text)
        finally:
            self.client.post("/settings/language", data={"lang": "es"})

    def test_no_chart_without_a_profile(self):
        """BODY-TURNOS-PERFIL-DIA-1 ruling 3: no chart, no empty frame -- only 2 blocks here, under
        perfil_por_dia's own MIN_BLOQUES_PREVIOS floor (the hero's own dias_recuperacion still shows,
        since its floor is lower)."""
        conn = self._reset_db()
        self._seed_today_anchored(conn)
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('class="chart-svg"', r.text)

    def test_singular_caption_and_axis_for_one_night_blocks(self):
        conn = self._reset_db()
        today = date.today()
        _fill_range(conn, 1, today - timedelta(140), today - timedelta(1), CONST)
        _seed_block_off_baseline(conn, 1, today - timedelta(100), 1)
        _seed_block_off_baseline(conn, 1, today - timedelta(60), 1)
        _seed_block_off_baseline(conn, 1, today - timedelta(20), 1)
        conn.commit()
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("alrededor de una noche", r.text)  # the singular caption, not "{n} noches"
        # no separate "last night" column when every block seen is a single night
        self.assertNotIn(_t("es", "shifts.perfil.offset.noche_ultima"), r.text)

    def test_assistant_tool_carries_perfil_hrv(self):
        conn = self._reset_db()
        self._seed_three_blocks_anchored(conn)
        out = herramientas.ejecutar(conn, 1, "leer_costo_turnos", {}, "resumen")
        self.assertIn("perfil_hrv", out)
        self.assertIsNone(out["perfil_hrv"]["motivo"])
        self.assertTrue(out["perfil_hrv"]["offsets"])
        json.dumps(out)  # JSON-serializable

    def test_shifts_page_shows_a_visible_caption_matching_the_aria_label(self):
        """BODY-TURNOS-PERFIL-DIA-1-M fix 1: the sentence the aria-label carries is also a visible
        <p>, built by the same _perfil_caption call, never a second copy."""
        conn = self._reset_db()
        self._seed_three_blocks_anchored(conn)
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        svg = re.search(r'<svg[^>]*class="chart-svg"[^>]*>', r.text).group(0)
        aria = re.search(r'aria-label="([^"]*)"', svg).group(1)
        caption_p = re.search(r'<p class="read mt-6">([^<]*)</p>', r.text)
        self.assertIsNotNone(caption_p, "no visible caption <p> found under the chart")
        self.assertEqual(html.escape(caption_p.group(1)), aria)

    def test_legend_calls_the_night_swatch_night_shift_not_bare_night(self):
        """BODY-TURNOS-PERFIL-DIA-1-M fix 4 (minor): the swatch gets its own key, distinct from the
        generic shift.kind.night the strip legend still uses."""
        conn = self._reset_db()
        self._seed_three_blocks_anchored(conn)
        r = self.client.get("/shifts")
        self.assertIn(_t("es", "shifts.perfil.noche"), r.text)
        self.client.post("/settings/language", data={"lang": "en"})
        try:
            r_en = self.client.get("/shifts")
            self.assertIn(_t("en", "shifts.perfil.noche"), r_en.text)
        finally:
            self.client.post("/settings/language", data={"lang": "es"})


class PerfilChartGeometryTest(unittest.TestCase):
    """BODY-TURNOS-PERFIL-DIA-1-M fixes 2/3: the chart's own geometry -- on a constructed profile,
    so the test is independent of any particular seeding helper's data shape, and exercises the
    seven-offset case (antes..post_4) every time."""

    def _perfil(self) -> dict:
        return {
            "metric": "hrv_last_night", "motivo": None, "centro": 60.0,
            "banda": {"bajo": -14.2, "alto": 12.3},
            "offsets": [
                {"offset": "antes", "media": 2.0, "n": 3},
                {"offset": "noche_1", "media": -18.0, "n": 3},
                {"offset": "noche_ultima", "media": -24.0, "n": 3},
                {"offset": "post_1", "media": -16.0, "n": 3},
                {"offset": "post_2", "media": -10.0, "n": 3},
                {"offset": "post_3", "media": -4.0, "n": 3},
                {"offset": "post_4", "media": -1.0, "n": 3},
            ],
            "mas_bajo": "noche_ultima", "vuelta": "post_4", "noches_tipicas": 2, "n_bloques": 3,
        }

    def _render(self, lang: str = "es") -> tuple[str, str]:
        t = T(lang)
        perfil = self._perfil()
        caption = turnos._perfil_caption(t, perfil)
        return turnos._perfil_svg(t, perfil, caption, "ms"), caption

    def test_every_circle_and_label_sits_inside_the_viewbox(self):
        svg, _ = self._render()
        width = float(re.search(r'viewBox="0 0 ([\d.]+) [\d.]+"', svg).group(1))
        cxs = [float(x) for x in re.findall(r'<circle cx="([\d.]+)"', svg)]
        text_xs = [float(x) for x in re.findall(r'<text x="([\d.]+)"', svg)]
        self.assertTrue(cxs and text_xs)
        for x in cxs + text_xs:
            self.assertGreaterEqual(x, 0.0)
            self.assertLessEqual(x, width)

    def _perfil_n_noches(self, n_noches: int, con_ultima: bool) -> dict:
        """A 1-night block has no distinct last night (`_offset_day` returns None for noche_ultima
        when fin == inicio, body/turnos_costo.py), so that offset never appears -- a 5-night block
        has one. BODY-TURNOS-PERFIL-LEGIBLE-1-M: the two label sets the -M's own defect report
        measured against (the right-edge label cut at "+4", the viewBox at 313)."""
        offsets = [{"offset": "antes", "media": 2.0, "n": 3}, {"offset": "noche_1", "media": -18.0, "n": 3}]
        if con_ultima:
            offsets.append({"offset": "noche_ultima", "media": -24.0, "n": 3})
        offsets += [{"offset": "post_1", "media": -16.0, "n": 3}, {"offset": "post_2", "media": -10.0, "n": 3},
                    {"offset": "post_3", "media": -4.0, "n": 3}, {"offset": "post_4", "media": -1.0, "n": 3}]
        return {"metric": "hrv_last_night", "motivo": None, "centro": 60.0,
                "banda": {"bajo": -14.2, "alto": 12.3}, "offsets": offsets,
                "mas_bajo": "noche_ultima" if con_ultima else "noche_1",
                "vuelta": "post_4", "noches_tipicas": n_noches, "n_bloques": 3}

    def test_right_inset_keeps_the_last_point_and_label_inside_the_viewbox(self):
        """BODY-TURNOS-PERFIL-LEGIBLE-1-M: the defect this fixes -- the last point and its "+4"
        label were drawn past the viewBox's own right edge and clipped by the card's overflow
        hidden. Checked for both label sets (1-night: no noche_ultima; 5-night: with it) in both
        languages, against every label LINE's own approximate box (`_label_px`'s character-count
        estimate), not just its anchor point."""
        tspan_re = re.compile(r'<tspan[^>]*>([^<]*)</tspan>')
        label_block_re = re.compile(
            r'<text x="([\d.]+)" y="[\d.]+" text-anchor="middle" class="tick">(.*?)</text>')
        for lang in ("es", "en"):
            t = T(lang)
            for n_noches, con_ultima in ((1, False), (5, True)):
                perfil = self._perfil_n_noches(n_noches, con_ultima)
                caption = turnos._perfil_caption(t, perfil)
                svg = turnos._perfil_svg(t, perfil, caption, "ms")
                width = float(re.search(r'viewBox="0 0 ([\d.]+) [\d.]+"', svg).group(1))

                cxs = [float(x) for x in re.findall(r'<circle cx="([\d.]+)"', svg)]
                self.assertTrue(cxs)
                for cx in cxs:
                    self.assertGreaterEqual(cx - turnos._PERFIL_POINT_R, -0.01,
                                             f"{lang} n={n_noches}: a point's circle sits left of the viewBox")
                    self.assertLessEqual(cx + turnos._PERFIL_POINT_R, width + 0.01,
                                          f"{lang} n={n_noches}: a point's circle sits past the right edge")

                boxes = []
                for x, body in label_block_re.findall(svg):
                    x = float(x)
                    lines = tspan_re.findall(body) if "<tspan" in body else [body]
                    widest = max(len(html.unescape(line)) * turnos._PERFIL_CHAR_W for line in lines)
                    boxes.append((x - widest / 2, x + widest / 2))
                self.assertTrue(boxes)
                for lo, hi in boxes:
                    self.assertGreaterEqual(lo, -0.01,
                                             f"{lang} n={n_noches}: a label box sits left of the viewBox")
                    self.assertLessEqual(hi, width + 0.01,
                                          f"{lang} n={n_noches}: a label box sits past the right edge")
                for (lo_a, hi_a), (lo_b, hi_b) in zip(boxes, boxes[1:]):
                    self.assertLessEqual(hi_a, lo_b + 0.01,
                                          f"{lang} n={n_noches}: two label boxes overlap")

    def test_each_x_axis_label_x_equals_its_points_cx(self):
        svg, _ = self._render()
        cxs = [float(x) for x in re.findall(r'<circle cx="([\d.]+)"', svg)]
        label_xs = [float(x) for x in
                    re.findall(r'<text x="([\d.]+)" y="[\d.]+" text-anchor="middle" class="tick">', svg)]
        self.assertEqual(cxs, label_xs)

    def test_y_ticks_are_whole_numbers_include_zero_and_sit_left_of_the_plot(self):
        svg, _ = self._render()
        plot_left = min(float(x) for x in re.findall(r'<circle cx="([\d.]+)"', svg))
        yticks = re.findall(r'<text x="([\d.]+)" y="[\d.]+" class="tick">([^<]*)</text>', svg)
        self.assertTrue(yticks)
        values = []
        for x, text in yticks:
            self.assertLess(float(x), plot_left, "a y-tick is drawn inside the plot area")
            num = text.split(" ")[0]
            self.assertNotIn(".", num, f"tick {text!r} is not a whole number")
            values.append(int(num))
        self.assertIn(0, values)
        self.assertIn("ms", " ".join(text for _, text in yticks))  # the unit, carried once

    def test_adjacent_x_axis_labels_never_overlap_in_either_language(self):
        """Approximation from character count: `turnos._PERFIL_CHAR_W` px per character at the
        chart's own floor axis font-size (CSS class "tick") -- real glyph metrics are not available
        server-side, so the layout keeps every adjacent pair at least this far apart. A label that
        does not fit its own slot wraps into two <tspan> lines (BODY-TURNOS-PERFIL-LEGIBLE-1 ruling
        2); the box used here is the wider of a wrapped label's two lines, rather than its full
        unwrapped width -- updated markup, same promise (never loosened)."""
        label_block_re = re.compile(
            r'<text x="([\d.]+)" y="[\d.]+" text-anchor="middle" class="tick">(.*?)</text>')
        tspan_re = re.compile(r'<tspan[^>]*>([^<]*)</tspan>')
        for lang in ("es", "en"):
            svg, _ = self._render(lang)
            blocks = label_block_re.findall(svg)
            self.assertTrue(blocks)
            cxs = [float(x) for x, _ in blocks]
            widths = []
            for _, body in blocks:
                lines = tspan_re.findall(body) if "<tspan" in body else [body]
                widths.append(max(len(html.unescape(line)) * turnos._PERFIL_CHAR_W for line in lines))
            for i in range(len(cxs) - 1):
                gap = cxs[i + 1] - cxs[i]
                needed = widths[i] / 2 + widths[i + 1] / 2
                self.assertGreaterEqual(gap, needed, f"{lang}: label {i} and {i + 1} boxes would overlap")

    def test_viewbox_width_is_the_fixed_constant_for_every_label_set(self):
        """BODY-TURNOS-PERFIL-LEGIBLE-1 ruling 1: the viewBox width never grows with a label's
        length -- true with the full seven-offset set (which wraps) and a short two-offset set
        (which does not), in either language."""
        for lang in ("es", "en"):
            svg7, _ = self._render(lang)
            width7 = float(re.search(r'viewBox="0 0 ([\d.]+) [\d.]+"', svg7).group(1))
            self.assertEqual(width7, turnos._PERFIL_WIDTH)

            t = T(lang)
            perfil_corto = {**self._perfil(), "offsets": [
                {"offset": "post_1", "media": -16.0, "n": 3},
                {"offset": "post_2", "media": -10.0, "n": 3},
            ]}
            caption = turnos._perfil_caption(t, perfil_corto)
            svg2 = turnos._perfil_svg(t, perfil_corto, caption, "ms")
            width2 = float(re.search(r'viewBox="0 0 ([\d.]+) [\d.]+"', svg2).group(1))
            self.assertEqual(width2, turnos._PERFIL_WIDTH)

    def test_axis_font_size_meets_the_small_text_floor(self):
        """BODY-TURNOS-PERFIL-LEGIBLE-1 ruling 1: the axis text (.tick) is at least the app's own
        small-text floor -- .fs-12 (12px), the smallest of body.css's general small-text classes --
        and never below 11px regardless."""
        css = (Path(__file__).resolve().parent.parent / "body" / "static" / "body.css").read_text(encoding="utf-8")
        m = re.search(r'\.tick\{[^}]*font-size:(\d+(?:\.\d+)?)px', css)
        self.assertIsNotNone(m, "no .tick font-size rule found in body.css")
        size = float(m.group(1))
        self.assertGreaterEqual(size, 11.0)
        self.assertEqual(size, turnos._PERFIL_TICK_FONT)

    def test_labels_wrap_only_when_they_do_not_fit_their_own_slot(self):
        """Unit-level check of the wrap rule itself (BODY-TURNOS-PERFIL-LEGIBLE-1 ruling 2, restating
        BODY-TURNOS-PERFIL-DIA-1-M's own character-count approximation)."""
        self.assertEqual(turnos._perfil_wrap("+1", 5.0), ["+1"])  # no space to break at -- never wraps
        label = "noche 1"
        wide_slot, narrow_slot = turnos._label_px(label) + 1, turnos._label_px(label) - 1
        self.assertEqual(turnos._perfil_wrap(label, wide_slot), [label])
        self.assertEqual(turnos._perfil_wrap(label, narrow_slot), ["noche", "1"])

    def test_chart_height_grows_by_one_line_only_when_a_label_wraps(self):
        svg7, _ = self._render()  # seven long labels -- at least one wraps
        height7 = float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', svg7).group(1))
        self.assertEqual(height7, turnos._PERFIL_HEIGHT + turnos._PERFIL_WRAP_LINE_HEIGHT)

        t = T("es")
        perfil_corto = {**self._perfil(), "offsets": [
            {"offset": "post_1", "media": -16.0, "n": 3},
            {"offset": "post_2", "media": -10.0, "n": 3},
        ]}
        caption = turnos._perfil_caption(t, perfil_corto)
        svg2 = turnos._perfil_svg(t, perfil_corto, caption, "ms")  # short labels -- never wrap
        height2 = float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', svg2).group(1))
        self.assertEqual(height2, turnos._PERFIL_HEIGHT)

    def test_every_label_lines_anchor_x_equals_its_points_cx(self):
        """BODY-TURNOS-PERFIL-LEGIBLE-1 ruling 2: a wrapped label's own <tspan> lines share its
        <text>'s x, which is also its point's cx (restates the single-line case above for the
        wrapped markup)."""
        for lang in ("es", "en"):
            svg, _ = self._render(lang)
            cxs = [float(x) for x in re.findall(r'<circle cx="([\d.]+)"', svg)]
            label_blocks = re.findall(
                r'<text x="([\d.]+)" y="[\d.]+" text-anchor="middle" class="tick">(.*?)</text>', svg)
            self.assertEqual(len(cxs), len(label_blocks))
            for (x, body), cx in zip(label_blocks, cxs):
                self.assertEqual(float(x), cx)
                for tspan_x in re.findall(r'<tspan x="([\d.]+)"', body):
                    self.assertEqual(float(tspan_x), cx)

    def test_visible_caption_equals_the_aria_label_in_both_languages(self):
        for lang in ("es", "en"):
            svg, caption = self._render(lang)
            aria = re.search(r'aria-label="([^"]*)"', svg).group(1)
            self.assertEqual(aria, html.escape(caption))


class FragSingularTest(unittest.TestCase):
    """BODY-TURNOS-PERFIL-LEGIBLE-1 ruling 3: every two-count sentence (shifts.confirm_summary*,
    shifts.confirm_summary_patron, shifts.confirm_summary_many(_mixed), shifts.confirm_all_result,
    shifts.como_bloques) is built from one fragment per count, each taking its own singular for
    one through turnos._frag -- checked here at 0, 1 and 2 for every fragment, in both languages."""

    # (fragment base, the kwarg name its own placeholder uses, {lang: (text at 0, at 1, at 2)})
    CASES = [
        ("shifts.confirm_days", "days",
         {"es": ("0 días", "1 día", "2 días"), "en": ("0 days", "1 day", "2 days")}),
        ("shifts.confirm_nights", "nights",
         {"es": ("0 noches", "1 noche", "2 noches"), "en": ("0 nights", "1 night", "2 nights")}),
        ("shifts.confirm_files", "n",
         {"es": ("0 archivos de turnos", "1 archivo de turnos", "2 archivos de turnos"),
          "en": ("0 shift files", "1 shift file", "2 shift files")}),
        ("shifts.confirm_proposals", "n",
         {"es": ("0 propuestas de turnos", "1 propuesta de turnos", "2 propuestas de turnos"),
          "en": ("0 shift proposals", "1 shift proposal", "2 shift proposals")}),
        ("shifts.confirm_all_batches", "batches",
         {"es": ("0 archivos", "1 archivo", "2 archivos"), "en": ("0 files", "1 file", "2 files")}),
        ("shifts.confirm_all_applied", "applied",
         {"es": ("0 días aplicados", "1 día aplicado", "2 días aplicados"),
          "en": ("0 days applied", "1 day applied", "2 days applied")}),
        ("shifts.como_bloques_n", "n",
         {"es": ("Se contaron 0 bloques de noches en los últimos 180 días",
                 "Se contó 1 bloque de noches en los últimos 180 días",
                 "Se contaron 2 bloques de noches en los últimos 180 días"),
          "en": ("0 night blocks counted in the last 180 days",
                 "1 night block counted in the last 180 days",
                 "2 night blocks counted in the last 180 days")}),
        ("shifts.como_bloques_con", "con",
         {"es": ("0 con un día de recuperación calculado", "1 con un día de recuperación calculado",
                 "2 con un día de recuperación calculado"),
          "en": ("0 with a recovery day calculated", "1 with a recovery day calculated",
                 "2 with a recovery day calculated")}),
    ]

    def test_each_fragment_at_zero_one_and_two(self):
        for base, kwname, by_lang in self.CASES:
            for lang, (zero, one, two) in by_lang.items():
                t = T(lang)
                for n, expected in ((0, zero), (1, one), (2, two)):
                    self.assertEqual(turnos._frag(t, base, n, **{kwname: n}), expected,
                                      f"{base}/{lang}/{n}")

    def test_como_bloques_joins_both_fragments(self):
        for lang in ("es", "en"):
            t = T(lang)
            n_frag = turnos._frag(t, "shifts.como_bloques_n", 1, n=1)
            con_frag = turnos._frag(t, "shifts.como_bloques_con", 0, con=0)
            self.assertEqual(t("shifts.como_bloques", n_frag=n_frag, con_frag=con_frag),
                              f"{n_frag}; {con_frag}.")

    def test_confirm_all_result_joins_both_fragments(self):
        for lang in ("es", "en"):
            t = T(lang)
            batches_frag = turnos._frag(t, "shifts.confirm_all_batches", 1, batches=1)
            applied_frag = turnos._frag(t, "shifts.confirm_all_applied", 1, applied=1)
            self.assertEqual(t("shifts.confirm_all_result", batches_frag=batches_frag, applied_frag=applied_frag),
                              f"Confirmaste {batches_frag}: {applied_frag}." if lang == "es"
                              else f"You confirmed {batches_frag}: {applied_frag}.")


if __name__ == "__main__":
    unittest.main()
