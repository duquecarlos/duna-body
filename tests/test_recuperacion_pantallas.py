"""BODY-RECUPERACION-1, BODY-RECUPERACION-1-C: the screens -- «Tu historia»'s "Recuperación
después de entrenar" section and Día's own line under an activity, in both languages, plus a
direct statement-count check on /history and /day/{d} (test_rendimiento.py's own ceilings, 29 and
45, are not in this slice's touches -- this test pins the same budget independently so a future
regression is caught here too). Invented demo data only, no real health figures.
"""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._rutas import contar_sql

from body import config, db, identity, recuperacion, web

client = TestClient(web.app, follow_redirects=False)

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "rp.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)  # made ready BEFORE any route is measured (tests/test_rendimiento.py's own pattern)
    return conn


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _serie(day: str, desde_min: int, hasta_min: int, paso_min: int, valor: float) -> list[tuple[str, float]]:
    out = []
    base = datetime.fromisoformat(f"{day}T00:00:00")
    m = desde_min
    while m <= hasta_min:
        out.append(((base + timedelta(minutes=m)).isoformat(), valor))
        m += paso_min
    return out


def _guardar_fc(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_fc", {"heartRateValues": [[_ms(ts), v] for ts, v in muestras]})


def _fondo_dias(conn, person_id: int, antes_de: str, dias: int, valor: float) -> None:
    """`dias` days of a full, constant-valor background day ending the day BEFORE `antes_de` --
    `recuperacion._usual_hora`'s own look-back window, so every hour's usual is `valor` and
    cutoff = valor + MODELO['margen_bpm']."""
    fin = date.fromisoformat(antes_de) - timedelta(1)
    paso = timedelta(minutes=2)
    for i in range(dias):
        d = fin - timedelta(i)
        t = datetime.combine(d, datetime.min.time())
        fin_dia = t + timedelta(days=1)
        muestras = []
        while t < fin_dia:
            muestras.append((t.isoformat(), valor))
            t += paso
        _guardar_fc(conn, person_id, d.isoformat(), muestras)


def _seed_sesion_que_recupera(conn, person_id: int, day: str = "2026-11-10") -> None:
    """One activity on `day` with a clean, fast recovery, through the real pipeline
    (recuperacion.recalcular), so the screens have something real to show."""
    _fondo_dias(conn, person_id, day, 15, 55.0)  # usual = 55 every hour, cutoff = 55 + 3 = 58
    conn.execute(
        "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type, avg_hr) "
        "VALUES (?, 'garmin', 'act-ok', ?, ?, 1800, 'running', 130)",
        (person_id, day, f"{day}T18:00:00"),
    )
    conn.commit()
    muestras = _serie(day, 18 * 60 + 30, 18 * 60 + 40, 2, 120.0) + _serie(day, 18 * 60 + 42, 22 * 60, 2, 50.0)
    _guardar_fc(conn, person_id, day, muestras)
    recuperacion.recalcular(conn, person_id)


def _insertar_fila(conn, person_id: int, source_id: str, day: str, minutos: float, tipo: str = "running") -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type, avg_hr) "
        "VALUES (?, 'garmin', ?, ?, ?, 1800, ?, 130)",
        (person_id, source_id, day, f"{day}T18:00:00", tipo),
    )
    conn.execute(
        "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, modelo_version, "
        "minutos, motivo, computed_at) VALUES (?, 'garmin', ?, ?, ?, NULL, '2026-11-10T00:00:00+00:00')",
        (person_id, source_id, recuperacion.MODELO["version"], minutos),
    )


def _insertar_censurada(conn, person_id: int, source_id: str, day: str) -> None:
    """A `fuera_de_horizonte` row (right-censored, point 2 of BODY-RECUPERACION-1-C): no minutos,
    counted by `estudio()` all the same."""
    conn.execute(
        "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type, avg_hr) "
        "VALUES (?, 'garmin', ?, ?, ?, 1800, 'running', 130)",
        (person_id, source_id, day, f"{day}T18:00:00"),
    )
    conn.execute(
        "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, modelo_version, "
        "minutos, motivo, computed_at) VALUES (?, 'garmin', ?, ?, NULL, 'fuera_de_horizonte', "
        "'2026-11-10T00:00:00+00:00')",
        (person_id, source_id, recuperacion.MODELO["version"]),
    )


def _insertar_turno(conn, person_id: int, day: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, source) VALUES (?, ?, ?, ?, 'manual')",
        (person_id, day, kind, f"{day}T06:00:00"),
    )


class HistoryRecoverySectionTest(unittest.TestCase):
    def test_sin_datos_muestra_el_estado_honesto(self):
        conn = _reset_db(self)
        db.set_metric(conn, "2026-11-01", "resting_hr", 55.0, "garmin", 1)  # so /history leaves soon.html
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Aún no hay suficientes sesiones", r.text)

    def test_con_hallazgo_muestra_la_frase_en_es(self):
        conn = _reset_db(self)
        for i in range(5):
            day = f"2026-09-0{i + 1}"
            db.set_metric(conn, day, "sleep_minutes", 300.0, "garmin", 1)
            _insertar_fila(conn, 1, f"corta-{i}", day, 60.0)
        for i in range(5):
            day = f"2026-09-{11 + i}"
            db.set_metric(conn, day, "sleep_minutes", 450.0, "garmin", 1)
            _insertar_fila(conn, 1, f"normal-{i}", day, 20.0)
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("una noche corta", r.text)
        self.assertIn("volver a lo habitual", r.text)

    def test_con_hallazgo_muestra_la_frase_en_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        for i in range(5):
            day = f"2026-09-0{i + 1}"
            db.set_metric(conn, day, "sleep_minutes", 300.0, "garmin", 1)
            _insertar_fila(conn, 1, f"corta-{i}", day, 60.0)
        for i in range(5):
            day = f"2026-09-{11 + i}"
            db.set_metric(conn, day, "sleep_minutes", 450.0, "garmin", 1)
            _insertar_fila(conn, 1, f"normal-{i}", day, 20.0)
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("a short night", r.text)
        self.assertIn("return to its usual level", r.text)

    def test_pocos_datos_muestra_la_frase_de_pocos_datos(self):
        conn = _reset_db(self)
        for i in range(3):
            day = f"2026-10-0{i + 1}"
            db.set_metric(conn, day, "sleep_minutes", 300.0, "garmin", 1)
            _insertar_fila(conn, 1, f"a-{i}", day, 40.0)
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Pocos datos por ahora", r.text)

    def test_hallazgo_nombra_dia_de_descanso_y_ambos_grupos_en_es(self):
        conn = _reset_db(self)
        for i in range(5):
            day = f"2026-08-0{i + 1}"
            db.set_metric(conn, day, "resting_hr", 55.0, "garmin", 1)  # so /history leaves soon.html
            _insertar_turno(conn, 1, day, "off")
            _insertar_fila(conn, 1, f"off-{i}", day, 60.0)
        for i in range(5):
            day = f"2026-08-{11 + i}"
            db.set_metric(conn, day, "resting_hr", 55.0, "garmin", 1)
            _insertar_turno(conn, 1, day, "morning")
            _insertar_fila(conn, 1, f"morning-{i}", day, 20.0)
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Después de un día de descanso", r.text)
        self.assertIn("frente a 20 min después de un turno de mañana", r.text)
        self.assertNotIn("un turno de descanso", r.text)

    def test_hallazgo_nombra_un_dia_libre_y_ambos_grupos_en_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        for i in range(5):
            day = f"2026-08-0{i + 1}"
            db.set_metric(conn, day, "resting_hr", 55.0, "garmin", 1)  # so /history leaves soon.html
            _insertar_turno(conn, 1, day, "off")
            _insertar_fila(conn, 1, f"off-{i}", day, 60.0)
        for i in range(5):
            day = f"2026-08-{11 + i}"
            db.set_metric(conn, day, "resting_hr", 55.0, "garmin", 1)
            _insertar_turno(conn, 1, day, "morning")
            _insertar_fila(conn, 1, f"morning-{i}", day, 20.0)
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("After a day off", r.text)
        self.assertIn("against 20 min after a morning shift", r.text)
        self.assertNotIn("off shift", r.text)

    def test_hrv_no_dice_hrv_en_es(self):
        conn = _reset_db(self)
        for i in range(5):
            day = f"2026-06-0{i + 1}"
            db.set_metric(conn, day, "hrv_last_night", 20.0, "garmin", 1)
            _insertar_fila(conn, 1, f"baja-{i}", day, 60.0)
        for i in range(5):
            day = f"2026-06-{11 + i}"
            db.set_metric(conn, day, "hrv_last_night", 70.0, "garmin", 1)
            _insertar_fila(conn, 1, f"normal-{i}", day, 20.0)
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("variabilidad del pulso", r.text)
        self.assertNotIn("HRV", r.text)

    def test_censurado_singular_en_es(self):
        conn = _reset_db(self)
        for i, m in enumerate([20.0, 25.0, 30.0]):
            day = f"2026-07-0{i + 1}"
            db.set_metric(conn, day, "resting_hr", 55.0, "garmin", 1)  # so /history leaves soon.html
            _insertar_fila(conn, 1, f"ok-{i}", day, m)
        db.set_metric(conn, "2026-07-10", "resting_hr", 55.0, "garmin", 1)
        _insertar_censurada(conn, 1, "cens-1", "2026-07-10")
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("1 no volvió en", r.text)
        self.assertNotIn("1 no volvieron", r.text)

    def test_censurado_singular_en_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        for i, m in enumerate([20.0, 25.0, 30.0]):
            day = f"2026-07-0{i + 1}"
            db.set_metric(conn, day, "resting_hr", 55.0, "garmin", 1)  # so /history leaves soon.html
            _insertar_fila(conn, 1, f"ok-{i}", day, m)
        db.set_metric(conn, "2026-07-10", "resting_hr", 55.0, "garmin", 1)
        _insertar_censurada(conn, 1, "cens-1", "2026-07-10")
        conn.commit()
        r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("1 did not return within", r.text)


class DayRecoveryLineTest(unittest.TestCase):
    def test_actividad_con_fila_muestra_la_linea_en_es(self):
        conn = _reset_db(self)
        _seed_sesion_que_recupera(conn, 1)
        r = client.get("/day/2026-11-10")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tu pulso volvió a lo habitual para esa hora en", r.text)

    def test_actividad_con_fila_muestra_la_linea_en_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        _seed_sesion_que_recupera(conn, 1)
        r = client.get("/day/2026-11-10")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your heart rate was back to your usual for that hour in", r.text)

    def test_actividad_sin_fila_no_muestra_ninguna_linea(self):
        conn = _reset_db(self)
        conn.execute(
            "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type) "
            "VALUES (1, 'garmin', 'act-sin-fila', '2026-11-10', '2026-11-10T18:00:00', 1800, 'running')"
        )
        conn.commit()
        r = client.get("/day/2026-11-10")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("lo habitual", r.text)

    def test_dia_marcado_no_muestra_ninguna_linea(self):
        conn = _reset_db(self)
        conn.execute(
            "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type) "
            "VALUES (1, 'garmin', 'act-marcado', '2026-11-10', '2026-11-10T18:00:00', 1800, 'running')"
        )
        conn.execute(
            "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, modelo_version, "
            "minutos, motivo, computed_at) VALUES (1, 'garmin', 'act-marcado', ?, NULL, 'marcado', "
            "'2026-11-10T00:00:00+00:00')", (recuperacion.MODELO["version"],),
        )
        conn.commit()
        r = client.get("/day/2026-11-10")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("lo habitual", r.text)


class StatementCeilingTest(unittest.TestCase):
    """Pins the same budget test_rendimiento.py's own ceiling table gives /history (29) and
    /day/{day} (45) -- that file is not in this slice's touches, so this is this slice's own
    guard against the section or the Día line raising either route's statement count."""

    def test_history_with_a_finding_stays_within_29_statements(self):
        conn = _reset_db(self)
        for i in range(5):
            day = f"2026-09-0{i + 1}"
            db.set_metric(conn, day, "sleep_minutes", 300.0, "garmin", 1)
            _insertar_fila(conn, 1, f"corta-{i}", day, 60.0)
        for i in range(5):
            day = f"2026-09-{11 + i}"
            db.set_metric(conn, day, "sleep_minutes", 450.0, "garmin", 1)
            _insertar_fila(conn, 1, f"normal-{i}", day, 20.0)
        conn.commit()
        with contar_sql() as stats:
            r = client.get("/history")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(stats["statements"], 29)

    def test_day_with_an_activity_stays_within_45_statements(self):
        conn = _reset_db(self)
        _seed_sesion_que_recupera(conn, 1)
        with contar_sql() as stats:
            r = client.get("/day/2026-11-10")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(stats["statements"], 45)


if __name__ == "__main__":
    unittest.main()
