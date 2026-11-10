"""BODY-TURNO-FC-1: the Turnos screen's own «Tu pulso durante el turno» section, in both
languages, plus a direct statement-count check on /shifts and /shifts/como (test_rendimiento.py's
own ceilings, 50 and 25, are not in this slice's touches -- this test pins the same budget
independently so a future regression is caught here too). Invented demo data only, no real health
figures.

BODY-TURNO-FC-1-M wording fixes: the unit on every bpm/lpm difference, the sleep split shown only
when BOTH buckets reach MODELO's min_n_grupo (as one line naming both, never a split with one
side), no "--" in customer copy, and the singular for the night-week recovery line (both
`shifts.ultima.recuperacion` and its `_sin_anteriores` and `_mediana` parts).
"""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._rutas import contar_sql

from body import config, db, identity, turno_fc, web
from body.i18n import T
from body.rutas.turnos import _turno_fc_sueno_lineas, _turnos_ultima_recuperacion

client = TestClient(web.app, follow_redirects=False)

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "tfp.db"
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


def _dia_completo(day: str, valor: float, paso_min: int = 2) -> list[tuple[str, float]]:
    return _serie(day, 0, 24 * 60 - paso_min, paso_min, valor)


def _guardar_fc(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_fc", {"heartRateValues": [[_ms(ts), v] for ts, v in muestras]})


def _insertar_fila(conn, person_id: int, day: str, kind: str, fc_mediana: float, referencia_fc: float) -> None:
    conn.execute(
        "INSERT INTO turno_fc (person_id, day, kind, modelo_version, fc_mediana, referencia_fc, "
        "motivo, computed_at) VALUES (?, ?, ?, ?, ?, ?, NULL, '2026-11-10T00:00:00+00:00')",
        (person_id, day, kind, turno_fc.MODELO["version"], fc_mediana, referencia_fc),
    )


def _activar_turnos(conn, person_id: int = 1) -> None:
    """At least one `shift` row, so `db.trabaja_turnos_si` infers Turnos is on (BODY-TURNOS-
    INFERIR-1) -- real turno_fc rows always come with real shifts anyway."""
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, source) VALUES (?, '2026-01-01', 'off', 'manual')",
        (person_id,),
    )


def _seed_seccion_con_hallazgo(conn, person_id: int = 1) -> None:
    """Six morning shifts, clearly below their own days-off reference -- a real finding, through
    the derived table directly (estudio() is a pure read over it)."""
    _activar_turnos(conn, person_id)
    for i in range(6):
        day = f"2026-09-{i + 1:02d}"
        _insertar_fila(conn, person_id, day, "morning", 60.0, 70.0)
    conn.commit()


class PulsoSeccionTest(unittest.TestCase):
    def test_sin_filas_la_seccion_no_aparece(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-10', 'off', 'manual')")
        conn.commit()
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Tu pulso durante el turno", r.text)

    def test_con_hallazgo_muestra_la_seccion_en_es(self):
        conn = _reset_db(self)
        _seed_seccion_con_hallazgo(conn)
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tu pulso durante el turno", r.text)
        self.assertIn("Mañana: tu pulso estuvo en 60 lpm, 10 lpm por debajo", r.text)
        self.assertIn("tu pulso está 10 lpm por debajo de tus días de descanso, la mayor diferencia "
                      "entre tus turnos", r.text)
        self.assertNotIn("días de descanso -- la mayor diferencia", r.text)
        self.assertIn("Esto compara el horario publicado", r.text)

    def test_con_hallazgo_muestra_la_seccion_en_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        _seed_seccion_con_hallazgo(conn)
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your heart rate during shifts", r.text)
        self.assertIn("Morning: your heart rate was 60 bpm, 10 bpm below", r.text)
        self.assertIn("your heart rate is 10 bpm below your days off, the biggest difference "
                      "among your shifts", r.text)
        self.assertNotIn("days off -- the biggest difference", r.text)
        self.assertIn("This compares the published schedule", r.text)

    def test_grupo_pocos_datos_dice_la_frase_honesta_en_es(self):
        conn = _reset_db(self)
        _activar_turnos(conn)
        for i in range(3):
            _insertar_fila(conn, 1, f"2026-09-0{i + 1}", "morning", 60.0, 70.0)
        conn.commit()
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("todavía pocos turnos con datos del reloj para esto (3)", r.text)

    def test_sin_referencia_dice_la_frase_honesta_en_es(self):
        conn = _reset_db(self)
        _activar_turnos(conn)
        for i in range(6):
            conn.execute(
                "INSERT INTO turno_fc (person_id, day, kind, modelo_version, fc_mediana, referencia_fc, "
                "motivo, computed_at) VALUES (1, ?, 'afternoon', ?, 70.0, NULL, NULL, "
                "'2026-11-10T00:00:00+00:00')",
                (f"2026-08-{i + 1:02d}", turno_fc.MODELO["version"]),
            )
        conn.commit()
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Aún no hay suficientes días de descanso para comparar", r.text)

    def test_diferencia_pequena_dice_muy_similar(self):
        conn = _reset_db(self)
        _activar_turnos(conn)
        for i in range(6):
            _insertar_fila(conn, 1, f"2026-09-{i + 1:02d}", "day", 70.0, 71.0)  # diff=1, under the noise floor
        conn.commit()
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("muy similar a las mismas horas en un día de descanso", r.text)
        self.assertNotIn("Lo que más resalta", r.text)  # no finding clears the noise floor


class SuenoSplitTest(unittest.TestCase):
    """BODY-TURNO-FC-1-M: the sleep split under a kind renders as ONE line naming both buckets, and
    only once BOTH reach MODELO's own min_n_grupo -- a split with one side is not a comparison."""

    def test_ambos_grupos_alcanzan_el_minimo_una_sola_linea(self):
        t = T("es")
        data = {"sleep": {"corta": {"fc_mediana": 69.0, "n": 11}, "normal": {"fc_mediana": 63.0, "n": 24}}}
        self.assertEqual(
            _turno_fc_sueno_lineas(t, data),
            ["Después de una noche corta: 69 lpm (11 turnos) · después de una noche normal: "
             "63 lpm (24 turnos)."])

    def test_un_lado_pocos_datos_no_muestra_nada(self):
        t = T("es")
        data = {"sleep": {"corta": {"fc_mediana": 69.0, "n": 11}, "normal": {"motivo": "pocos_datos", "n": 2}}}
        self.assertEqual(_turno_fc_sueno_lineas(t, data), [])

    def test_un_lado_ausente_no_muestra_nada(self):
        t = T("es")
        data = {"sleep": {"corta": {"fc_mediana": 69.0, "n": 11}}}
        self.assertEqual(_turno_fc_sueno_lineas(t, data), [])

    def test_sin_datos_de_sueno_no_muestra_nada(self):
        t = T("es")
        self.assertEqual(_turno_fc_sueno_lineas(t, {"sleep": {}}), [])

    def test_ambos_grupos_en_la_pagina_real_en_es(self):
        """Through estudio() and /shifts, not just the helper -- the demo's own 5/5 split shows
        at least one kind with the sleep split (10 shifts, half short nights, half normal)."""
        conn = _reset_db(self)
        _activar_turnos(conn)
        for i in range(10):
            day = f"2026-09-{i + 1:02d}"
            _insertar_fila(conn, 1, day, "morning", 60.0, 70.0)
            db.set_metric(conn, day, "sleep_minutes", 250.0 if i < 5 else 420.0, "garmin", 1)
        conn.commit()
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Después de una noche corta: 60 lpm (5 turnos) · después de una noche "
                      "normal: 60 lpm (5 turnos).", r.text)

    def test_ambos_grupos_en_la_pagina_real_en_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        _activar_turnos(conn)
        for i in range(10):
            day = f"2026-09-{i + 1:02d}"
            _insertar_fila(conn, 1, day, "morning", 60.0, 70.0)
            db.set_metric(conn, day, "sleep_minutes", 250.0 if i < 5 else 420.0, "garmin", 1)
        conn.commit()
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("After a short night: 60 bpm (5 shifts) · after a normal night: 60 bpm "
                      "(5 shifts).", r.text)


class RecuperacionSingularTest(unittest.TestCase):
    """BODY-TURNO-FC-1-M: «Volviste a tu normal en 1 día» (not «1 días»), for both the recovery
    days and the median-days-or-fewer part, independently, in both languages."""

    def test_un_dia_de_recuperacion_sin_mediana_es(self):
        t = T("es")
        u = {"recuperacion_dias": 1, "recuperacion_mediana": None}
        self.assertEqual(_turnos_ultima_recuperacion(t, u), "Volviste a tu normal en 1 día.")

    def test_varios_dias_de_recuperacion_sin_mediana_es(self):
        t = T("es")
        u = {"recuperacion_dias": 3, "recuperacion_mediana": None}
        self.assertEqual(_turnos_ultima_recuperacion(t, u), "Volviste a tu normal en 3 días.")

    def test_un_dia_de_recuperacion_y_mediana_de_un_dia_es(self):
        t = T("es")
        u = {"recuperacion_dias": 1, "recuperacion_mediana": 1}
        self.assertEqual(_turnos_ultima_recuperacion(t, u),
                          "Volviste a tu normal en 1 día (antes, la mitad de las veces: 1 día o menos)")

    def test_varios_dias_de_recuperacion_y_mediana_de_un_dia_es(self):
        t = T("es")
        u = {"recuperacion_dias": 5, "recuperacion_mediana": 1}
        self.assertEqual(_turnos_ultima_recuperacion(t, u),
                          "Volviste a tu normal en 5 días (antes, la mitad de las veces: 1 día o menos)")

    def test_un_dia_de_recuperacion_y_mediana_de_varios_es(self):
        t = T("es")
        u = {"recuperacion_dias": 1, "recuperacion_mediana": 3}
        self.assertEqual(_turnos_ultima_recuperacion(t, u),
                          "Volviste a tu normal en 1 día (antes, la mitad de las veces: 3 días o menos)")

    def test_un_dia_de_recuperacion_sin_mediana_en(self):
        t = T("en")
        u = {"recuperacion_dias": 1, "recuperacion_mediana": None}
        self.assertEqual(_turnos_ultima_recuperacion(t, u), "You were back to your normal in 1 day.")

    def test_un_dia_de_recuperacion_y_mediana_de_un_dia_en(self):
        t = T("en")
        u = {"recuperacion_dias": 1, "recuperacion_mediana": 1}
        self.assertEqual(_turnos_ultima_recuperacion(t, u),
                          "You were back to your normal in 1 day (before, half the time: 1 day or fewer)")

    def test_varios_dias_sin_singular_en(self):
        t = T("en")
        u = {"recuperacion_dias": 4, "recuperacion_mediana": 2}
        self.assertEqual(_turnos_ultima_recuperacion(t, u),
                          "You were back to your normal in 4 days (before, half the time: 2 days or fewer)")


class StatementCeilingTest(unittest.TestCase):
    """Pins the same budget test_rendimiento.py's own ceiling table gives /shifts (50) and
    /shifts/como (25) -- that file is not in this slice's touches, so this is this slice's own
    guard against the new section raising either route's statement count."""

    def test_shifts_with_a_finding_stays_within_50_statements(self):
        conn = _reset_db(self)
        _seed_seccion_con_hallazgo(conn)
        with contar_sql() as stats:
            r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(stats["statements"], 50)

    def test_shifts_como_stays_within_25_statements(self):
        conn = _reset_db(self)
        _seed_seccion_con_hallazgo(conn)
        with contar_sql() as stats:
            r = client.get("/shifts/como")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(stats["statements"], 25)


if __name__ == "__main__":
    unittest.main()
