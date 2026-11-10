"""body/sueno_fc.py: BODY-SUENO-FC-1 (ORCH-CARLOS-DELEGATES-TIER-1-1) -- the sleep-window heart
rate itself, honest reason codes, recalcular's idempotence and version-bump behaviour, the two new
metric_def rows, Tendencias and Dia in es and en. Invented demo data only, no real health figures.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, identity, pull, readiness, sueno_fc, web

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_turno_fc.py convention)


def _mkconn(case: unittest.TestCase, name: str = "s.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _web_conn(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """A connection the web app's own routes read, like tests/test_tendencias_todas.py's `_conn`:
    `config.DB_PATH` is what body.db.connect() (every route's own call) opens, so a TestClient hit
    must seed through the SAME path, never through a separate `_mkconn` temp file the app never
    reads."""
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    case.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    web.app.dependency_overrides[identity.current_person_id] = lambda: 1
    return conn


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _payload(muestras: list[tuple[str, float]]) -> dict:
    return {"heartRateValues": [[_ms(ts), v] for ts, v in muestras]}


def _guardar_fc(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_fc", _payload(muestras))


def _serie(day: str, desde_min: int, hasta_min: int, paso_min: int, valor: float) -> list[tuple[str, float]]:
    out = []
    base = datetime.fromisoformat(f"{day}T00:00:00")
    m = desde_min
    while m <= hasta_min:
        out.append(((base + timedelta(minutes=m)).isoformat(), valor))
        m += paso_min
    return out


def _sesion(conn, person_id: int, wake_day: str, start_local: str, end_local: str) -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, wake_day, start_local, end_local),
    )


class NocheVentanaTest(unittest.TestCase):
    """A night across midnight in a non-UTC zone; honest gaps; the robust low."""

    def test_noche_cruza_medianoche_en_zona_no_utc(self):
        conn = _mkconn(self)
        _sesion(conn, 1, "2026-11-10", "2026-11-09T22:00:00", "2026-11-10T06:00:00")
        muestras = _serie("2026-11-09", 22 * 60, 23 * 60 + 58, 2, 60.0) + _serie("2026-11-10", 0, 6 * 60, 2, 50.0)
        _guardar_fc(conn, 1, "2026-11-09", [m for m in muestras if m[0].startswith("2026-11-09")])
        _guardar_fc(conn, 1, "2026-11-10", [m for m in muestras if m[0].startswith("2026-11-10")])
        conn.commit()
        resultado = sueno_fc.noche(conn, 1, "2026-11-10")
        self.assertIsInstance(resultado, tuple)
        avg, bajo = resultado
        self.assertLess(bajo, avg)  # the low percentile sits under the mean of a two-level curve
        self.assertGreater(avg, 50.0)
        self.assertLessEqual(bajo, 50.0)

    def test_sin_sesion_de_sueno_no_hay_valor(self):
        conn = _mkconn(self)
        resultado = sueno_fc.noche(conn, 1, "2026-11-10")
        self.assertEqual(resultado, "sin_sesion")

    def test_cobertura_baja_no_hay_valor(self):
        conn = _mkconn(self)
        _sesion(conn, 1, "2026-11-10", "2026-11-09T22:00:00", "2026-11-10T06:00:00")
        _guardar_fc(conn, 1, "2026-11-09", [("2026-11-09T22:00:00", 60.0), ("2026-11-09T22:02:00", 60.0)])
        conn.commit()
        resultado = sueno_fc.noche(conn, 1, "2026-11-10")
        self.assertEqual(resultado, "sin_cobertura")

    def test_sin_ninguna_muestra_es_sin_cobertura(self):
        conn = _mkconn(self)
        _sesion(conn, 1, "2026-11-10", "2026-11-09T22:00:00", "2026-11-10T06:00:00")
        conn.commit()
        resultado = sueno_fc.noche(conn, 1, "2026-11-10")
        self.assertEqual(resultado, "sin_cobertura")

    def test_bajo_percentil_ignora_un_solo_pico_hacia_abajo(self):
        conn = _mkconn(self)
        _sesion(conn, 1, "2026-11-10", "2026-11-10T00:00:00", "2026-11-10T06:00:00")
        muestras = _serie("2026-11-10", 0, 6 * 60, 2, 55.0)
        muestras[1] = (muestras[1][0], 10.0)  # one stray very-low sample, deep inside the window
        _guardar_fc(conn, 1, "2026-11-10", muestras)
        conn.commit()
        avg, bajo = sueno_fc.noche(conn, 1, "2026-11-10")
        self.assertGreater(bajo, 40.0)  # nowhere near the single 10.0 spike
        self.assertAlmostEqual(avg, 55.0, delta=1.0)


class RecalcularTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn(self)
        _sesion(self.conn, 1, "2026-11-10", "2026-11-10T00:00:00", "2026-11-10T06:00:00")
        # A two-level curve (not flat) so the robust low differs from the mean -- the same reason
        # test_turno_fc.py's own cross-midnight fixture never uses a single constant either.
        muestras = _serie("2026-11-10", 0, 3 * 60 - 2, 2, 60.0) + _serie("2026-11-10", 3 * 60, 6 * 60, 2, 50.0)
        _guardar_fc(self.conn, 1, "2026-11-10", muestras)
        self.conn.commit()

    def test_escribe_las_dos_claves(self):
        resultado = sueno_fc.recalcular(self.conn, 1)
        self.assertEqual(resultado["nuevas"], 1)
        rows = {r["metric_key"]: r for r in self.conn.execute(
            "SELECT metric_key, value, source FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key IN ('sleep_hr_avg', 'sleep_hr_min')")}
        self.assertEqual(set(rows), {"sleep_hr_avg", "sleep_hr_min"})
        self.assertEqual(rows["sleep_hr_avg"]["source"], "body-sueno-fc-v1")
        self.assertLess(rows["sleep_hr_min"]["value"], rows["sleep_hr_avg"]["value"])

    def test_es_idempotente(self):
        sueno_fc.recalcular(self.conn, 1)
        resultado2 = sueno_fc.recalcular(self.conn, 1)
        self.assertEqual(resultado2["nuevas"], 0)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'sleep_hr_avg'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_un_cambio_de_version_recalcula_y_reemplaza(self):
        sueno_fc.recalcular(self.conn, 1)
        with mock.patch.dict(sueno_fc.MODELO, {"version": "sueno-fc-v2"}):
            resultado = sueno_fc.recalcular(self.conn, 1)
            self.assertEqual(resultado["nuevas"], 1)
        rows = self.conn.execute(
            "SELECT source FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'sleep_hr_avg'"
        ).fetchall()
        # daily_metric carries no model-version column of its own -- the newer row REPLACES the
        # older one (ruling 2), never sits alongside it the way recuperacion_actividad/turno_fc's
        # own version-keyed tables do.
        self.assertEqual([r["source"] for r in rows], ["body-sueno-fc-v2"])

    def test_un_valor_de_api_en_otra_clave_nunca_se_toca(self):
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 58.0, "garmin", 1)
        self.conn.commit()
        sueno_fc.recalcular(self.conn, 1)
        row = self.conn.execute(
            "SELECT value, source FROM daily_metric WHERE person_id = 1 AND day = '2026-11-10' "
            "AND metric_key = 'resting_hr'"
        ).fetchone()
        self.assertEqual((row["value"], row["source"]), (58.0, "garmin"))

    def test_motivos_son_solo_conteos_nunca_una_cifra_de_salud(self):
        conn = _mkconn(self, "sin_cobertura.db")
        _sesion(conn, 1, "2026-11-11", "2026-11-11T00:00:00", "2026-11-11T06:00:00")
        conn.commit()
        buf = io.StringIO()
        with redirect_stdout(buf):
            resultado = sueno_fc.recalcular(conn, 1)
        self.assertIsInstance(resultado["nuevas"], int)
        for k, v in resultado["motivos"].items():
            self.assertIn(k, sueno_fc.REASON_CODES)
            self.assertIsInstance(v, int)
        self.assertEqual(resultado["motivos"].get("sin_cobertura"), 1)


class RecalcularSeguroTest(unittest.TestCase):
    """body/pull.py's `_recalcular_sueno_fc_seguro`: silent when nothing is new, one counts-only
    line when a row was written -- the same rule `_recalcular_turno_fc_seguro` follows."""

    def setUp(self):
        self.conn = _mkconn(self)
        _sesion(self.conn, 1, "2026-11-10", "2026-11-10T00:00:00", "2026-11-10T06:00:00")
        _guardar_fc(self.conn, 1, "2026-11-10", _serie("2026-11-10", 0, 6 * 60, 2, 55.0))
        self.conn.commit()

    def test_prints_a_counts_only_line_when_a_row_is_new(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pull._recalcular_sueno_fc_seguro(self.conn, 1)
        self.assertEqual(buf.getvalue().strip(), "sueno_fc: 1 filas nuevas")

    def test_stays_silent_on_a_re_run_with_nothing_new(self):
        pull._recalcular_sueno_fc_seguro(self.conn, 1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            pull._recalcular_sueno_fc_seguro(self.conn, 1)
        self.assertEqual(buf.getvalue(), "")


class MetricDefTest(unittest.TestCase):
    def test_dos_filas_nuevas_lower_estimado_garmin(self):
        conn = _mkconn(self)
        rows = {r["key"]: dict(r) for r in conn.execute(
            "SELECT * FROM metric_def WHERE key IN ('sleep_hr_avg', 'sleep_hr_min')")}
        self.assertEqual(set(rows), {"sleep_hr_avg", "sleep_hr_min"})
        for row in rows.values():
            self.assertEqual(row["better"], "lower")
            self.assertEqual(row["kind"], "estimated")
            # origen stays 'garmin' (never 'self'), so _registrar_medida's own
            # "WHERE origen = 'self'" filter can never offer these two keys to be self-registered.
            self.assertEqual(row["origen"], "garmin")

    def test_unidad_es_bpm(self):
        conn = _mkconn(self)
        for key in ("sleep_hr_avg", "sleep_hr_min"):
            unit = conn.execute("SELECT unit FROM metric_def WHERE key = ?", (key,)).fetchone()["unit"]
            self.assertEqual(unit, "bpm")


class ReadinessNuncaLasLeeTest(unittest.TestCase):
    """Ruling 5: readiness does not read these keys in this slice."""

    def test_sleep_hr_fuera_de_readiness(self):
        self.assertNotIn("sleep_hr_avg", readiness.METRICS)
        self.assertNotIn("sleep_hr_min", readiness.METRICS)
        self.assertNotIn("sleep_hr_avg", readiness.WEIGHTS)
        self.assertNotIn("sleep_hr_min", readiness.WEIGHTS)


class DiaLineaTest(unittest.TestCase):
    """body/rutas/hoy.py's `_sueno_fc_linea`, in es and en."""

    def test_linea_es(self):
        conn = _web_conn(self, "dia_es.db")
        _sesion(conn, 1, "2026-11-10", "2026-11-10T00:00:00", "2026-11-10T06:00:00")
        _guardar_fc(conn, 1, "2026-11-10", _serie("2026-11-10", 0, 6 * 60, 2, 54.0))
        conn.commit()
        sueno_fc.recalcular(conn, 1)
        client = TestClient(web.app, follow_redirects=False)
        page = client.get("/day/2026-11-10").text
        self.assertIn("Pulso durmiendo", page)
        self.assertIn("lpm", page)

    def test_linea_en(self):
        conn = _web_conn(self, "dia_en.db")
        db.set_language(conn, "en", 1)
        _sesion(conn, 1, "2026-11-10", "2026-11-10T00:00:00", "2026-11-10T06:00:00")
        _guardar_fc(conn, 1, "2026-11-10", _serie("2026-11-10", 0, 6 * 60, 2, 54.0))
        conn.commit()
        sueno_fc.recalcular(conn, 1)
        client = TestClient(web.app, follow_redirects=False)
        page = client.get("/day/2026-11-10").text
        self.assertIn("Sleeping heart rate", page)
        self.assertIn("bpm", page)

    def test_sin_valores_no_hay_linea(self):
        conn = _web_conn(self, "dia_sin.db")
        db.set_metric(conn, "2026-11-10", "sleep_minutes", 420.0, "garmin", 1)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        page = client.get("/day/2026-11-10").text
        self.assertNotIn("Pulso durmiendo", page)


class TendenciasNightGroupTest(unittest.TestCase):
    """The "night" group card, in es and en -- the per-origin monthly floor applies like every
    other Garmin-origin key (TREND_MIN_DAYS), so the fixture seeds 14 days."""

    def _seed_14_dias(self, conn, person_id: int = 1) -> None:
        d0 = date(2026, 10, 1)
        for i in range(16):
            day = (d0 + timedelta(i)).isoformat()
            db.set_metric(conn, day, "resting_hr", 58.0, "garmin", person_id)
            db.set_metric(conn, day, "sleep_minutes", 410.0, "garmin", person_id)
            db.set_metric(conn, day, "sleep_hr_avg", 54.0 + i * 0.1, "body-sueno-fc-v1", person_id)
            db.set_metric(conn, day, "sleep_hr_min", 48.0 + i * 0.1, "body-sueno-fc-v1", person_id)

    def test_tarjeta_en_es(self):
        conn = _web_conn(self, "trends_es.db")
        self._seed_14_dias(conn)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        page = client.get("/trends?todas=1").text
        self.assertIn('id="sleep_hr_avg"', page)
        self.assertIn('id="sleep_hr_min"', page)
        self.assertIn("¿Qué tan bajo late tu corazón mientras duermes?", page)

    def test_tarjeta_en_en(self):
        conn = _web_conn(self, "trends_en.db")
        db.set_language(conn, "en", 1)
        self._seed_14_dias(conn)
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        page = client.get("/trends?todas=1").text
        self.assertIn('id="sleep_hr_avg"', page)
        self.assertIn("How low does your heart rate run while you sleep?", page)


if __name__ == "__main__":
    unittest.main()
