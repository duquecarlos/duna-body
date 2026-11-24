"""body/reentrada.py (BODY-PLAN-FASES-6): the gap ramp (`_reentrada`) and the heavy-day flag
(`_tras_carga`), each read fresh, each scoped to its own person. Invented demo data only, no real
health figures.
"""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, reentrada

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_momentos_pull.py convention)


def _mkconn(case: unittest.TestCase, name: str = "reentrada.db"):
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


def _fondo_dias(conn, person_id: int, antes_de: str, dias: int, valor: float) -> None:
    """`dias` days of a flat background, ending the day BEFORE `antes_de` -- the same helper
    tests/test_recuperacion.py already uses, duplicated here for the same reason every small test
    helper in this repo is duplicated rather than shared."""
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


def _actividad(conn, person_id: int, day: str, tipo: str, nombre: str, start_local: str, duration_s: float,
               source_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'garmin')",
        (person_id, source_id or f"act-{day}-{tipo}", day, start_local, tipo, nombre, duration_s),
    )
    conn.commit()


class ReentradaGapTest(unittest.TestCase):
    """Ruling 1 (BODY-REENTRADA-ESCALA-1): the gap ramp -- 7+ days without strength reads as
    session 1, at the 'corto' band's own first factor (90 %), the next real session back to
    normal (no key at all, since the band's own 2nd factor is 1.0); a 6-day gap never qualifies
    in the first place."""

    def setUp(self):
        self.conn = _mkconn(self)

    def test_6_dias_no_califica(self):
        _actividad(self.conn, 1, "2026-11-01", "strength_training", "Fuerza", "2026-11-01T18:00:00", 1800)
        out = reentrada.senales(self.conn, 1, date(2026, 11, 7))  # 6 days later
        self.assertNotIn("reentrada", out)

    def test_7_mas_dias_escala_por_sesion_hasta_volver_a_normal(self):
        dia0 = "2026-10-01"
        _actividad(self.conn, 1, dia0, "strength_training", "Fuerza", f"{dia0}T18:00:00", 1800)

        # Session 1 back, 7 days after dia0: the 'corto' band's own first factor, 90 %.
        dia1 = date(2026, 10, 8)
        out1 = reentrada.senales(self.conn, 1, dia1)
        self.assertEqual(out1["reentrada"], {"dias_sin_fuerza": 7, "sesion_n": 1, "factor": 0.9,
                                              "escalon": "corto", "dias_activos_en_brecha": 0})

        _actividad(self.conn, 1, dia1.isoformat(), "strength_training", "Fuerza", f"{dia1.isoformat()}T18:00:00", 1800)

        # Session 2 back, 3 days after session 1 (still inside the original gap's own ramp): the
        # 'corto' band's own 2nd factor is 1.0 -- back to normal, no key at all.
        dia2 = dia1 + timedelta(3)
        out2 = reentrada.senales(self.conn, 1, dia2)
        self.assertNotIn("reentrada", out2)

    def test_sin_historia_nunca_senala(self):
        self.assertNotIn("reentrada", reentrada.senales(self.conn, 1, date(2026, 11, 10)))

    def test_otra_persona_nunca_cuenta(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        _actividad(self.conn, 2, "2026-10-01", "strength_training", "Fuerza", "2026-10-01T18:00:00", 1800)
        self.assertNotIn("reentrada", reentrada.senales(self.conn, 1, date(2026, 10, 8)))


class EscalonesBandaTest(unittest.TestCase):
    """Ruling 1 (BODY-REENTRADA-ESCALA-1): the ramp's own band depends on the gap's own length --
    each band's first and second session factor, at its own edges (7, 14, 15, 28, 29 and 60
    days). `escalon` and `dias_activos_en_brecha` ride along on every qualifying signal."""

    def _brecha(self, gap_dias_reales: int, nombre: str = "brecha.db"):
        """One qualifying gap of exactly `gap_dias_reales` days -- `dia0` the last strength
        session before it, `dia1` the day training resumes, that first session's own signal."""
        conn = _mkconn(self, nombre)
        dia0 = date(2026, 1, 1)
        _actividad(conn, 1, dia0.isoformat(), "strength_training", "Fuerza", f"{dia0.isoformat()}T18:00:00", 1800)
        dia1 = dia0 + timedelta(gap_dias_reales)
        return conn, dia0, dia1

    def test_10_dias_da_90_luego_sin_clave(self):
        """The Personal desk's own case, 2026-11-24, in the slice's own words: "a 10-day gap
        gives 0.9, then 1.0, then no key" -- session 1 at 90 %, session 2's own factor reaches
        1.0, so `senales` omits the key entirely rather than ever saying "1.0" out loud."""
        conn, dia0, dia1 = self._brecha(10, "brecha10.db")
        out1 = reentrada.senales(conn, 1, dia1)
        self.assertEqual(out1["reentrada"], {"dias_sin_fuerza": 10, "sesion_n": 1, "factor": 0.9,
                                              "escalon": "corto", "dias_activos_en_brecha": 0})
        _actividad(conn, 1, dia1.isoformat(), "strength_training", "Fuerza", f"{dia1.isoformat()}T18:00:00", 1800)
        out2 = reentrada.senales(conn, 1, dia1 + timedelta(3))
        self.assertNotIn("reentrada", out2)

    def test_7_dias_banda_corto(self):
        conn, dia0, dia1 = self._brecha(7, "brecha7.db")
        out = reentrada.senales(conn, 1, dia1)
        self.assertEqual(out["reentrada"]["factor"], 0.9)
        self.assertEqual(out["reentrada"]["escalon"], "corto")

    def test_14_dias_banda_corto(self):
        conn, dia0, dia1 = self._brecha(14, "brecha14.db")
        out = reentrada.senales(conn, 1, dia1)
        self.assertEqual(out["reentrada"]["factor"], 0.9)
        self.assertEqual(out["reentrada"]["escalon"], "corto")

    def test_15_dias_banda_medio(self):
        conn, dia0, dia1 = self._brecha(15, "brecha15.db")
        out1 = reentrada.senales(conn, 1, dia1)
        self.assertEqual(out1["reentrada"]["factor"], 0.75)
        self.assertEqual(out1["reentrada"]["escalon"], "medio")
        _actividad(conn, 1, dia1.isoformat(), "strength_training", "Fuerza", f"{dia1.isoformat()}T18:00:00", 1800)
        out2 = reentrada.senales(conn, 1, dia1 + timedelta(3))
        self.assertEqual(out2["reentrada"]["factor"], 0.9)
        self.assertEqual(out2["reentrada"]["escalon"], "medio")

    def test_28_dias_banda_medio(self):
        conn, dia0, dia1 = self._brecha(28, "brecha28.db")
        out = reentrada.senales(conn, 1, dia1)
        self.assertEqual(out["reentrada"]["factor"], 0.75)
        self.assertEqual(out["reentrada"]["escalon"], "medio")

    def test_29_dias_banda_largo(self):
        """reentrada-v1's own original 3-step ramp, now reserved for a gap this long: session 1
        at 60 %, session 2 at 80 %, session 3 back to normal (no key)."""
        conn, dia0, dia1 = self._brecha(29, "brecha29.db")
        out1 = reentrada.senales(conn, 1, dia1)
        self.assertEqual(out1["reentrada"]["factor"], 0.6)
        self.assertEqual(out1["reentrada"]["escalon"], "largo")
        _actividad(conn, 1, dia1.isoformat(), "strength_training", "Fuerza", f"{dia1.isoformat()}T18:00:00", 1800)
        dia2 = dia1 + timedelta(3)
        out2 = reentrada.senales(conn, 1, dia2)
        self.assertEqual(out2["reentrada"]["factor"], 0.8)
        self.assertEqual(out2["reentrada"]["escalon"], "largo")
        _actividad(conn, 1, dia2.isoformat(), "strength_training", "Fuerza", f"{dia2.isoformat()}T18:00:00", 1800)
        out3 = reentrada.senales(conn, 1, dia2 + timedelta(3))
        self.assertNotIn("reentrada", out3)

    def test_60_dias_banda_largo(self):
        conn, dia0, dia1 = self._brecha(60, "brecha60.db")
        out = reentrada.senales(conn, 1, dia1)
        self.assertEqual(out["reentrada"]["factor"], 0.6)
        self.assertEqual(out["reentrada"]["escalon"], "largo")

    def test_dias_activos_en_brecha_cuenta_actividad_sin_cambiar_el_factor(self):
        conn, dia0, dia1 = self._brecha(10, "brecha-activa.db")
        _actividad(conn, 1, (dia0 + timedelta(3)).isoformat(), "walking", "Caminata",
                   f"{(dia0 + timedelta(3)).isoformat()}T07:00:00", 600)
        _actividad(conn, 1, (dia0 + timedelta(5)).isoformat(), "walking", "Caminata",
                   f"{(dia0 + timedelta(5)).isoformat()}T07:00:00", 600)
        out = reentrada.senales(conn, 1, dia1)
        self.assertEqual(out["reentrada"]["factor"], 0.9)  # unchanged by the activity in the gap
        self.assertEqual(out["reentrada"]["dias_activos_en_brecha"], 2)


class TrasCargaTest(unittest.TestCase):
    """Ruling 2: a heavy day before `dia` -- recovery well past this person's OWN typical -- flags
    the groups it loaded; a short, easy one never does."""

    def setUp(self):
        self.conn = _mkconn(self)
        _fondo_dias(self.conn, 1, "2026-11-10", 45, 55.0)  # usual = 55 every hour, cutoff = 55 + 3 = 58

    def _sesion_corta(self, day: str, tipo: str, nombre: str) -> None:
        """A 30-minute session, ending 18:30, that recovers by minute 12 -- the exact shape
        tests/test_recuperacion.py's own worked example uses."""
        _actividad(self.conn, 1, day, tipo, nombre, f"{day}T18:00:00", 1800.0)
        muestras = _serie(day, 18 * 60 + 30, 18 * 60 + 40, 2, 120.0) + _serie(day, 18 * 60 + 42, 22 * 60, 2, 50.0)
        _guardar_fc(self.conn, 1, day, muestras)

    def test_dia_pesado_marca_sus_grupos(self):
        from body import recuperacion

        self._sesion_corta("2026-11-05", "running", "Carrera")
        self._sesion_corta("2026-11-07", "running", "Carrera")
        recuperacion.recalcular(self.conn, 1)  # stores both at minutos=12 -> typical = 12

        dia_basket = "2026-11-09"
        _actividad(self.conn, 1, dia_basket, "other", "Baloncesto", f"{dia_basket}T18:00:00", 3000.0)
        muestras = (_serie(dia_basket, 18 * 60 + 50, 19 * 60 + 40, 2, 120.0)
                    + _serie(dia_basket, 19 * 60 + 42, 22 * 60, 2, 50.0))
        _guardar_fc(self.conn, 1, dia_basket, muestras)

        out = reentrada.senales(self.conn, 1, date(2026, 11, 10))
        self.assertNotIn("reentrada", out)
        self.assertEqual(out["tras_carga"]["actividad"], {"tipo": "other", "nombre": "Baloncesto"})
        self.assertEqual(out["tras_carga"]["recuperacion_min"], 52)
        self.assertEqual(out["tras_carga"]["grupos"], ["cuadriceps", "isquios"])

    def test_dia_corto_y_facil_no_marca(self):
        dia_caminata = "2026-11-09"
        _actividad(self.conn, 1, dia_caminata, "walking", "Caminata", f"{dia_caminata}T07:00:00", 600.0)
        muestras = _serie(dia_caminata, 7 * 60 + 10, 7 * 60 + 12, 2, 90.0) + \
            _serie(dia_caminata, 7 * 60 + 14, 10 * 60, 2, 55.0)
        _guardar_fc(self.conn, 1, dia_caminata, muestras)

        out = reentrada.senales(self.conn, 1, date(2026, 11, 10))
        self.assertNotIn("tras_carga", out)

    def test_otra_persona_nunca_cuenta(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        dia_basket = "2026-11-09"
        _actividad(self.conn, 2, dia_basket, "other", "Baloncesto", f"{dia_basket}T18:00:00", 3000.0)
        muestras = (_serie(dia_basket, 18 * 60 + 50, 19 * 60 + 40, 2, 120.0)
                    + _serie(dia_basket, 19 * 60 + 42, 22 * 60, 2, 50.0))
        _guardar_fc(self.conn, 2, dia_basket, muestras)
        out = reentrada.senales(self.conn, 1, date(2026, 11, 10))
        self.assertNotIn("tras_carga", out)


class ModeloCensoTest(unittest.TestCase):
    """Every threshold this module leans on, named -- never "at least these" (ORCH-CARLOS-DELEGATES-
    TIER-1-1)."""

    def test_version_y_umbrales(self):
        self.assertEqual(reentrada.MODELO["version"], "reentrada-v2")
        self.assertEqual(set(reentrada.MODELO.keys()), {"version", "gap_dias", "escalones", "carga_alta_factor"})
        self.assertEqual(reentrada.MODELO["gap_dias"], 7)
        self.assertEqual(reentrada.MODELO["escalones"], [
            {"desde_dias": 7, "hasta_dias": 14, "nombre": "corto", "factores": [0.9, 1.0]},
            {"desde_dias": 15, "hasta_dias": 28, "nombre": "medio", "factores": [0.75, 0.9, 1.0]},
            {"desde_dias": 29, "hasta_dias": None, "nombre": "largo", "factores": [0.6, 0.8, 1.0]},
        ])
        self.assertEqual(reentrada.MODELO["carga_alta_factor"], 1.5)


if __name__ == "__main__":
    unittest.main()
