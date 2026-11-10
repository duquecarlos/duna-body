"""body/recuperacion.py: back-to-resting after a workout (BODY-RECUPERACION-1) -- the sustained
threshold crossing, honest reason codes, recalcular's idempotence and version-bump behaviour, and
estudio()'s factor groups and findings. Invented demo data only, no real health figures.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, pull, recuperacion

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_momentos_pull.py convention)


def _mkconn(case: unittest.TestCase, name: str = "r.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _payload_fc(muestras: list[tuple[str, float]]) -> dict:
    return {"heartRateValues": [[_ms(ts), v] for ts, v in muestras]}


def _guardar_fc(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_fc", _payload_fc(muestras))


def _serie(day: str, desde_min: int, hasta_min: int, paso_min: int, valor: float) -> list[tuple[str, float]]:
    out = []
    base = datetime.fromisoformat(f"{day}T00:00:00")
    m = desde_min
    while m <= hasta_min:
        out.append(((base + timedelta(minutes=m)).isoformat(), valor))
        m += paso_min
    return out


def _set_resting(conn, person_id: int, hasta_day: str, dias: int, valor: float) -> None:
    """`dias` days of a constant resting_hr ending the day BEFORE `hasta_day` -- baselines.baseline
    reads the window strictly before the day given, so this is exactly its own reference window,
    and a constant value gives a predictable high = valor (pstdev 0, so q1 = q3 = the mean)."""
    fin = date.fromisoformat(hasta_day) - timedelta(1)
    for i in range(dias):
        d = fin - timedelta(i)
        db.set_metric(conn, d.isoformat(), "resting_hr", valor, "garmin", person_id)
    conn.commit()


def _actividad(day: str, start_local: str, duration_s: float) -> dict:
    return {"day": day, "start_local": start_local, "duration_s": duration_s}


class RecuperacionActividadTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn(self)
        _set_resting(self.conn, 1, "2026-11-10", 10, 55.0)  # high = 55, cutoff = 55 + 3 = 58

    def test_recupera_normalmente(self):
        # The activity ends at 18:30 (18:00 start + 30 min) -- the window searched starts exactly
        # there, so the elevated/recovered split below is relative to 18:30, not to the start.
        muestras = _serie("2026-11-10", 18 * 60 + 30, 18 * 60 + 40, 2, 120.0) + \
            _serie("2026-11-10", 18 * 60 + 42, 22 * 60, 2, 50.0)
        _guardar_fc(self.conn, 1, "2026-11-10", muestras)
        r = recuperacion.recuperacion_actividad(
            self.conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertIsNone(r.motivo)
        self.assertEqual(r.minutos, 12)

    def test_nunca_recupera_dentro_del_horizonte(self):
        muestras = _serie("2026-11-10", 18 * 60 + 30, 22 * 60 + 30, 2, 120.0)
        _guardar_fc(self.conn, 1, "2026-11-10", muestras)
        r = recuperacion.recuperacion_actividad(
            self.conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertEqual(r.motivo, "fuera_de_horizonte")
        self.assertIsNone(r.minutos)

    def test_un_bajon_que_sube_de_nuevo_no_cuenta_el_sostenido_manda(self):
        """A single dip below cutoff that rises again within MODELO['sostenido_min'] never counts
        -- the first TRUE sustained drop does."""
        muestras = (
            _serie("2026-11-10", 18 * 60 + 30, 18 * 60 + 38, 2, 120.0)
            + [("2026-11-10T18:40:00", 50.0)]
            + [("2026-11-10T18:42:00", 70.0)]
            + _serie("2026-11-10", 18 * 60 + 44, 22 * 60, 2, 50.0)
        )
        _guardar_fc(self.conn, 1, "2026-11-10", muestras)
        r = recuperacion.recuperacion_actividad(
            self.conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertIsNone(r.motivo)
        self.assertEqual(r.minutos, 14)

    def test_hueco_con_poca_cobertura_es_sin_cobertura(self):
        """Only 2 samples (2 minutes apart) in a 4-hour window: far below MODELO['cobertura_min']
        of what that spacing implies -- a watch-off gap, not a thin-but-honest reading."""
        muestras = [("2026-11-10T18:30:00", 100.0), ("2026-11-10T18:32:00", 90.0)]
        _guardar_fc(self.conn, 1, "2026-11-10", muestras)
        r = recuperacion.recuperacion_actividad(
            self.conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertEqual(r.motivo, "sin_cobertura")

    def test_sin_ninguna_muestra_es_sin_cobertura(self):
        r = recuperacion.recuperacion_actividad(
            self.conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertEqual(r.motivo, "sin_cobertura")

    def test_cruza_medianoche_en_zona_no_utc(self):
        """The activity ends right at local midnight; the window it searches spans two calendar
        days (America/Bogota, UTC-5) -- the samples that matter are stored under the NEXT day."""
        muestras = _serie("2026-11-11", 0, 10, 2, 120.0) + _serie("2026-11-11", 12, 4 * 60, 2, 50.0)
        _guardar_fc(self.conn, 1, "2026-11-11", muestras)
        r = recuperacion.recuperacion_actividad(
            self.conn, 1, _actividad("2026-11-10", "2026-11-10T23:50:00", 600))
        self.assertIsNone(r.motivo)
        self.assertEqual(r.minutos, 12)


class SinRangoTest(unittest.TestCase):
    def test_sin_baseline_de_resting_hr_es_sin_rango(self):
        conn = _mkconn(self)
        r = recuperacion.recuperacion_actividad(
            conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertEqual(r.motivo, "sin_rango")
        self.assertIsNone(r.minutos)


class DiaMarcadoTest(unittest.TestCase):
    def test_dia_marcado_nunca_llega_a_calcular(self):
        conn = _mkconn(self)
        _set_resting(conn, 1, "2026-11-10", 10, 55.0)
        db.crear_periodo(conn, 1, "malestar", "2026-11-10", "2026-11-10")
        conn.commit()
        r = recuperacion.recuperacion_actividad(
            conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertEqual(r.motivo, "marcado")
        self.assertIsNone(r.minutos)


class RecalcularTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn(self)
        _set_resting(self.conn, 1, "2026-11-10", 10, 55.0)
        self.conn.execute(
            "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s) "
            "VALUES (1, 'garmin', 'act-1', '2026-11-10', '2026-11-10T18:00:00', 1800)"
        )
        self.conn.commit()
        muestras = _serie("2026-11-10", 18 * 60 + 30, 18 * 60 + 40, 2, 120.0) + \
            _serie("2026-11-10", 18 * 60 + 42, 22 * 60, 2, 50.0)
        _guardar_fc(self.conn, 1, "2026-11-10", muestras)

    def test_escribe_una_fila_nueva(self):
        resultado = recuperacion.recalcular(self.conn, 1)
        self.assertEqual(resultado["nuevas"], 1)
        row = self.conn.execute(
            "SELECT minutos, motivo, modelo_version FROM recuperacion_actividad "
            "WHERE person_id = 1 AND activity_source_id = 'act-1'"
        ).fetchone()
        self.assertEqual((row["minutos"], row["motivo"], row["modelo_version"]), (12, None, "recuperacion-v1"))

    def test_es_idempotente(self):
        recuperacion.recalcular(self.conn, 1)
        resultado2 = recuperacion.recalcular(self.conn, 1)
        self.assertEqual(resultado2["nuevas"], 0)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM recuperacion_actividad WHERE person_id = 1 AND activity_source_id = 'act-1'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_un_cambio_de_version_recalcula_sin_borrar_la_version_anterior(self):
        recuperacion.recalcular(self.conn, 1)
        with mock.patch.dict(recuperacion.MODELO, {"version": "recuperacion-v2"}):
            resultado = recuperacion.recalcular(self.conn, 1)
            self.assertEqual(resultado["nuevas"], 1)
        rows = self.conn.execute(
            "SELECT modelo_version FROM recuperacion_actividad WHERE person_id = 1 AND activity_source_id = 'act-1' "
            "ORDER BY modelo_version"
        ).fetchall()
        self.assertEqual([r["modelo_version"] for r in rows], ["recuperacion-v1", "recuperacion-v2"])

    def test_motivos_son_solo_conteos_nunca_una_cifra_de_salud(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            resultado = recuperacion.recalcular(self.conn, 1)
        self.assertIsInstance(resultado["nuevas"], int)
        for k, v in resultado["motivos"].items():
            self.assertIn(k, recuperacion.REASON_CODES)
            self.assertIsInstance(v, int)


class RecalcularSeguroTest(unittest.TestCase):
    """body/pull.py's `_recalcular_recuperacion_seguro` (BODY-RECUPERACION-1-B): silent when the
    recompute found nothing new, one counts-only line when it wrote a row -- the same rule
    `_cerrar_semanas_seguro`'s own sweep already follows."""

    def setUp(self):
        self.conn = _mkconn(self)
        _set_resting(self.conn, 1, "2026-11-10", 10, 55.0)
        self.conn.execute(
            "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s) "
            "VALUES (1, 'garmin', 'act-1', '2026-11-10', '2026-11-10T18:00:00', 1800)"
        )
        self.conn.commit()
        muestras = _serie("2026-11-10", 18 * 60 + 30, 18 * 60 + 40, 2, 120.0) + \
            _serie("2026-11-10", 18 * 60 + 42, 22 * 60, 2, 50.0)
        _guardar_fc(self.conn, 1, "2026-11-10", muestras)

    def test_prints_a_counts_only_line_when_a_row_is_new(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pull._recalcular_recuperacion_seguro(self.conn, 1)
        self.assertEqual(buf.getvalue().strip(), "recuperacion: 1 filas nuevas")

    def test_stays_silent_on_a_re_run_with_nothing_new(self):
        pull._recalcular_recuperacion_seguro(self.conn, 1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            pull._recalcular_recuperacion_seguro(self.conn, 1)
        self.assertEqual(buf.getvalue(), "")


def _insertar_fila(conn, person_id: int, source_id: str, day: str, start_local: str, duration_s: float,
                    tipo: str, minutos: float | None, avg_hr: float = 130.0) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type, avg_hr) "
        "VALUES (?, 'garmin', ?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, start_local, duration_s, tipo, avg_hr),
    )
    conn.execute(
        "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, modelo_version, "
        "minutos, motivo, computed_at) VALUES (?, 'garmin', ?, ?, ?, NULL, '2026-11-10T00:00:00+00:00')",
        (person_id, source_id, recuperacion.MODELO["version"], minutos),
    )


class EstudioTest(unittest.TestCase):
    def test_sin_filas_es_n_cero(self):
        conn = _mkconn(self)
        est = recuperacion.estudio(conn, 1)
        self.assertEqual(est, {"n": 0, "mediana": None, "bajo": None, "alto": None, "hallazgos": []})

    def test_pocos_datos_no_produce_hallazgos(self):
        conn = _mkconn(self)
        for i in range(3):
            day = f"2026-10-0{i + 1}"
            db.set_metric(conn, day, "sleep_minutes", 300.0, "garmin", 1)
            _insertar_fila(conn, 1, f"act-{i}", day, f"{day}T18:00:00", 1800, "running", 60.0)
        conn.commit()
        est = recuperacion.estudio(conn, 1)
        self.assertEqual(est["n"], 3)
        self.assertEqual(est["hallazgos"], [])

    def test_un_factor_con_grupos_calificados_produce_un_hallazgo(self):
        conn = _mkconn(self)
        for i in range(5):
            day = f"2026-09-0{i + 1}"
            db.set_metric(conn, day, "sleep_minutes", 300.0, "garmin", 1)  # corta
            _insertar_fila(conn, 1, f"corta-{i}", day, f"{day}T18:00:00", 1800, "running", 60.0)
        for i in range(5):
            day = f"2026-09-{11 + i}"
            db.set_metric(conn, day, "sleep_minutes", 450.0, "garmin", 1)  # normal
            _insertar_fila(conn, 1, f"normal-{i}", day, f"{day}T18:00:00", 1800, "running", 20.0)
        conn.commit()
        est = recuperacion.estudio(conn, 1)
        self.assertEqual(est["n"], 10)
        sueno = [h for h in est["hallazgos"] if h["factor"] == "sueno_anoche"]
        self.assertEqual(len(sueno), 1)
        h = sueno[0]
        self.assertEqual((h["peor"], h["mejor"], h["min_peor"], h["min_mejor"], h["n_peor"], h["n_mejor"]),
                         ("corta", "normal", 60, 20, 5, 5))

    def test_una_diferencia_bajo_el_piso_de_ruido_no_es_hallazgo(self):
        conn = _mkconn(self)
        for i in range(5):
            day = f"2026-08-0{i + 1}"
            db.set_metric(conn, day, "sleep_minutes", 300.0, "garmin", 1)
            _insertar_fila(conn, 1, f"a-{i}", day, f"{day}T18:00:00", 1800, "running", 30.0)
        for i in range(5):
            day = f"2026-08-{11 + i}"
            db.set_metric(conn, day, "sleep_minutes", 450.0, "garmin", 1)
            _insertar_fila(conn, 1, f"b-{i}", day, f"{day}T18:00:00", 1800, "running", 32.0)
        conn.commit()
        est = recuperacion.estudio(conn, 1)
        self.assertEqual([h for h in est["hallazgos"] if h["factor"] == "sueno_anoche"], [])


if __name__ == "__main__":
    unittest.main()
