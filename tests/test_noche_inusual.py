"""body/noche_inusual.py: BODY-NOCHE-INUSUAL-1 (ORCH-CARLOS-DELEGATES-TIER-1-1, BODY-INFERENCIA-
FC-1-INV slice 2) -- two and three signals off flag, one alone never does, honest gaps (no signal,
no baseline), the "Ya lo vi" dismissal (per person, per night), Hoy's and Dia's own lines in es and
en, never a cause word, and readiness left untouched. Invented demo data only, no real figures.
"""
import json
import pathlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, noche_inusual, web

DAY = "2026-11-10"
YESTERDAY = "2026-11-09"
HR_KEY = "sleep_hr_avg"
HRV_KEY = "hrv_last_night"
SCORE_KEY = "sleep_score"

ROOT = pathlib.Path(__file__).resolve().parent.parent
_ES = json.loads((ROOT / "body" / "i18n" / "es.json").read_text(encoding="utf-8"))
_EN = json.loads((ROOT / "body" / "i18n" / "en.json").read_text(encoding="utf-8"))

# Never names or guesses a cause (ruling 4) -- checked against every string this module's own
# lines can ever show, in both languages.
FORBIDDEN_ES = ["alcohol", "enferm", "estrés", "estres", "turno", "viaje", "lesión", "lesion", "gripe", "medic"]
FORBIDDEN_EN = ["alcohol", "illness", "sick", "stress", "shift", "travel", "injury", "medical"]


def _mkconn(case: unittest.TestCase, name: str = "noche_inusual.db"):
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / name)
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


def _web_conn(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """A connection the web app's own routes read (tests/test_sueno_fc.py's own convention)."""
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    case.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    web.app.dependency_overrides[identity.current_person_id] = lambda: 1
    return conn


def _seed_window(conn, person_id: int, day: str, key: str, base: float) -> None:
    """30 days of this metric, ending the day BEFORE `day` -- a small repeating cycle (never flat)
    so the baseline's own sd is > 0, same reasoning tests/test_sueno_fc.py's own two-level curve
    fixtures already use."""
    d = date.fromisoformat(day)
    for i in range(30):
        dia = (d - timedelta(30 - i)).isoformat()
        db.set_metric(conn, dia, key, base + (i % 5 - 2), "garmin", person_id)


class EvaluarTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn(self)
        _seed_window(self.conn, 1, DAY, HR_KEY, 55.0)
        _seed_window(self.conn, 1, DAY, HRV_KEY, 60.0)
        _seed_window(self.conn, 1, DAY, SCORE_KEY, 70.0)
        self.conn.commit()

    def test_dos_senales_fuera_marca(self):
        db.set_metric(self.conn, DAY, HRV_KEY, 20.0, "garmin", 1)
        db.set_metric(self.conn, DAY, SCORE_KEY, 15.0, "garmin", 1)
        self.conn.commit()
        noche = noche_inusual.evaluar(self.conn, 1, DAY)
        self.assertIsNotNone(noche)
        self.assertEqual({k for k, _ in noche.senales}, {HRV_KEY, SCORE_KEY})

    def test_tres_senales_fuera_marca(self):
        db.set_metric(self.conn, DAY, HR_KEY, 90.0, "garmin", 1)
        db.set_metric(self.conn, DAY, HRV_KEY, 20.0, "garmin", 1)
        db.set_metric(self.conn, DAY, SCORE_KEY, 15.0, "garmin", 1)
        self.conn.commit()
        noche = noche_inusual.evaluar(self.conn, 1, DAY)
        self.assertIsNotNone(noche)
        self.assertEqual({k for k, _ in noche.senales}, {HR_KEY, HRV_KEY, SCORE_KEY})

    def test_una_sola_senal_nunca_marca(self):
        db.set_metric(self.conn, DAY, HRV_KEY, 20.0, "garmin", 1)
        self.conn.commit()
        self.assertIsNone(noche_inusual.evaluar(self.conn, 1, DAY))

    def test_noche_normal_no_marca(self):
        db.set_metric(self.conn, DAY, HRV_KEY, 61.0, "garmin", 1)
        db.set_metric(self.conn, DAY, SCORE_KEY, 69.0, "garmin", 1)
        self.conn.commit()
        self.assertIsNone(noche_inusual.evaluar(self.conn, 1, DAY))

    def test_direccion_es_fija_por_senal(self):
        """A HIGH hrv or a HIGH sleep_score is never 'off' -- each signal's unusual direction is
        fixed (ruling 2), never read from the night itself."""
        db.set_metric(self.conn, DAY, HRV_KEY, 120.0, "garmin", 1)
        db.set_metric(self.conn, DAY, SCORE_KEY, 150.0, "garmin", 1)
        self.conn.commit()
        self.assertIsNone(noche_inusual.evaluar(self.conn, 1, DAY))

    def test_sin_valor_esa_noche_no_hay_suficientes_senales(self):
        """hrv_last_night has no value at all that night -- only sleep_score can be judged, never
        enough on its own (ruling 3's 'no signal' honest gap, via MODELO['minimo_senales'])."""
        db.set_metric(self.conn, DAY, SCORE_KEY, 15.0, "garmin", 1)
        self.conn.commit()
        self.assertIsNone(noche_inusual.evaluar(self.conn, 1, DAY))


class SinBaselineTest(unittest.TestCase):
    def test_sin_historia_no_juzga(self):
        """Fewer than baselines.MIN_DAYS of history for every signal -- an extreme value still
        never flags, the honest 'not enough data yet' gap (ruling 3)."""
        conn = _mkconn(self)
        db.set_metric(conn, DAY, HRV_KEY, 10.0, "garmin", 1)
        db.set_metric(conn, DAY, SCORE_KEY, 5.0, "garmin", 1)
        conn.commit()
        self.assertIsNone(noche_inusual.evaluar(conn, 1, DAY))


class DismissTest(unittest.TestCase):
    def test_no_marcada_por_defecto(self):
        conn = _mkconn(self)
        self.assertFalse(noche_inusual.vista(conn, 1, DAY))

    def test_marcar_y_leer(self):
        conn = _mkconn(self)
        noche_inusual.marcar_vista(conn, 1, DAY)
        self.assertTrue(noche_inusual.vista(conn, 1, DAY))

    def test_es_por_persona(self):
        conn = _mkconn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        noche_inusual.marcar_vista(conn, 1, DAY)
        self.assertFalse(noche_inusual.vista(conn, 2, DAY), "another person's night read as dismissed")

    def test_es_por_noche(self):
        conn = _mkconn(self)
        noche_inusual.marcar_vista(conn, 1, DAY)
        self.assertFalse(noche_inusual.vista(conn, 1, YESTERDAY))

    def test_idempotente_una_sola_fila(self):
        conn = _mkconn(self)
        noche_inusual.marcar_vista(conn, 1, DAY)
        noche_inusual.marcar_vista(conn, 1, DAY)
        n = conn.execute(
            "SELECT COUNT(*) FROM moment WHERE person_id = 1 AND kind = ?",
            (noche_inusual.DISMISS_KIND,),
        ).fetchone()[0]
        self.assertEqual(n, 1)


def _seed_two_signal_night(conn, person_id: int = 1) -> None:
    _seed_window(conn, person_id, DAY, HRV_KEY, 60.0)
    _seed_window(conn, person_id, DAY, SCORE_KEY, 70.0)
    db.set_metric(conn, DAY, HRV_KEY, 20.0, "garmin", person_id)
    db.set_metric(conn, DAY, SCORE_KEY, 15.0, "garmin", person_id)
    conn.commit()


def _seed_three_signal_night(conn, person_id: int = 1) -> None:
    _seed_window(conn, person_id, DAY, HR_KEY, 55.0)
    _seed_window(conn, person_id, DAY, HRV_KEY, 60.0)
    _seed_window(conn, person_id, DAY, SCORE_KEY, 70.0)
    db.set_metric(conn, DAY, HR_KEY, 90.0, "garmin", person_id)
    db.set_metric(conn, DAY, HRV_KEY, 20.0, "garmin", person_id)
    db.set_metric(conn, DAY, SCORE_KEY, 15.0, "garmin", person_id)
    conn.commit()


class HoyLineaTest(unittest.TestCase):
    def test_dos_senales_es(self):
        conn = _web_conn(self, "hoy_2_es.db")
        _seed_two_signal_night(conn)
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn("Anoche dormiste distinto a tu normal: variabilidad del pulso más baja "
                      "y puntaje de sueño más bajo.", page)
        self.assertIn("Ver la noche", page)
        self.assertIn(f'href="/day/{DAY}"', page)

    def test_tres_senales_en_lista_natural(self):
        conn = _web_conn(self, "hoy_3_es.db")
        _seed_three_signal_night(conn)
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn("pulso durmiendo más alto, variabilidad del pulso más baja "
                      "y puntaje de sueño más bajo", page)

    def test_dos_senales_en(self):
        conn = _web_conn(self, "hoy_2_en.db")
        db.set_language(conn, "en", 1)
        _seed_two_signal_night(conn)
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn("Last night was different from your normal: lower heart rate variability "
                      "and lower sleep score.", page)
        self.assertIn("See the night", page)

    def test_tres_senales_en_lista_natural_en(self):
        conn = _web_conn(self, "hoy_3_en.db")
        db.set_language(conn, "en", 1)
        _seed_three_signal_night(conn)
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn("higher sleeping heart rate, lower heart rate variability, "
                      "and lower sleep score", page)

    def test_noche_normal_sin_linea(self):
        conn = _web_conn(self, "hoy_normal.db")
        _seed_window(conn, 1, DAY, HRV_KEY, 60.0)
        _seed_window(conn, 1, DAY, SCORE_KEY, 70.0)
        db.set_metric(conn, DAY, HRV_KEY, 61.0, "garmin", 1)
        db.set_metric(conn, DAY, SCORE_KEY, 69.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertNotIn("Ver la noche", page)

    def test_linea_desaparece_tras_descartar(self):
        conn = _web_conn(self, "hoy_descartada.db")
        _seed_two_signal_night(conn)
        client = TestClient(web.app, follow_redirects=False)
        self.assertIn("Ver la noche", client.get("/").text)
        noche_inusual.marcar_vista(conn, 1, DAY)
        self.assertNotIn("Ver la noche", client.get("/").text)


class DiaLineaTest(unittest.TestCase):
    def test_linea_y_boton_es(self):
        conn = _web_conn(self, "dia_es.db")
        _seed_two_signal_night(conn)
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertIn(f"Esa noche fue distinta a tu normal: variabilidad del pulso más baja "
                      f"y puntaje de sueño más bajo.", page)
        self.assertIn("Ya lo vi", page)
        self.assertIn(f'action="/day/{DAY}/noche-inusual/visto"', page)

    def test_linea_y_boton_en(self):
        conn = _web_conn(self, "dia_en.db")
        db.set_language(conn, "en", 1)
        _seed_two_signal_night(conn)
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertIn("That night was different from your normal: lower heart rate variability "
                      "and lower sleep score.", page)
        self.assertIn("Got it", page)

    def test_sin_senales_sin_linea(self):
        conn = _web_conn(self, "dia_sin.db")
        db.set_metric(conn, DAY, "sleep_minutes", 420.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertNotIn("Ya lo vi", page)

    def test_descartar_desde_dia_persiste_y_quita_la_linea(self):
        conn = _web_conn(self, "dia_descartar.db")
        _seed_two_signal_night(conn)
        client = TestClient(web.app, follow_redirects=False)
        page1 = client.get(f"/day/{DAY}").text
        self.assertIn("Ya lo vi", page1)

        r = client.post(f"/day/{DAY}/noche-inusual/visto")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/day/{DAY}")

        page2 = client.get(f"/day/{DAY}").text
        self.assertNotIn("Ya lo vi", page2)
        self.assertNotIn("Esa noche fue distinta a tu normal", page2)
        # Hoy's own line is gone too, from the SAME dismissal (ruling 5: per night, per person).
        self.assertNotIn("Ver la noche", client.get("/").text)
        self.assertTrue(noche_inusual.vista(conn, 1, DAY))


class NuncaUnaCausaTest(unittest.TestCase):
    """Ruling 4: this module's own screen strings never name or guess a cause, in either
    language -- checked directly against the catalog strings (every string these lines can ever
    assemble), not just one rendered example."""

    def test_es_sin_palabra_de_causa(self):
        texto = " ".join([
            _ES["today.noche_inusual"], _ES["day.noche_inusual"],
            _ES["noche.senal.sleep_hr_avg"], _ES["noche.senal.hrv_last_night"], _ES["noche.senal.sleep_score"],
        ]).lower()
        for palabra in FORBIDDEN_ES:
            self.assertNotIn(palabra, texto, f"found forbidden cause word {palabra!r} in es strings")

    def test_en_sin_palabra_de_causa(self):
        texto = " ".join([
            _EN["today.noche_inusual"], _EN["day.noche_inusual"],
            _EN["noche.senal.sleep_hr_avg"], _EN["noche.senal.hrv_last_night"], _EN["noche.senal.sleep_score"],
        ]).lower()
        for palabra in FORBIDDEN_EN:
            self.assertNotIn(palabra, texto, f"found forbidden cause word {palabra!r} in en strings")


class ReadinessSinCambiosTest(unittest.TestCase):
    """Ruling 7: readiness is not changed by this slice."""

    def test_readiness_no_referencia_noche_inusual(self):
        import body.readiness as readiness_mod
        src = Path(readiness_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("noche_inusual", src)

    def test_readiness_sigue_siendo_correcta_en_una_noche_marcada(self):
        """The readiness score on a flagged day is whatever it always was -- this module never
        touches it, so there is nothing to compute differently here beyond the day rendering."""
        conn = _web_conn(self, "readiness_ok.db")
        _seed_two_signal_night(conn)
        db.set_metric(conn, DAY, "resting_hr", 55.0, "garmin", 1)
        db.set_metric(conn, DAY, "sleep_minutes", 420.0, "garmin", 1)
        db.set_metric(conn, DAY, "body_battery_wake", 70.0, "garmin", 1)
        conn.commit()
        r1 = TestClient(web.app, follow_redirects=False).get("/").text
        noche_inusual.marcar_vista(conn, 1, DAY)
        r2 = TestClient(web.app, follow_redirects=False).get("/").text
        import re
        score = lambda page: re.search(r'<span class="big">(\d+)</span>', page)
        s1, s2 = score(r1), score(r2)
        self.assertIsNotNone(s1)
        self.assertEqual(s1.group(1), s2.group(1))


if __name__ == "__main__":
    unittest.main()
