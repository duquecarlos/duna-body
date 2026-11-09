"""body/experimentos.py's intraday verdict (BODY-EXPERIMENTOS-INTRADIA-MODELO-1): the same
whole-day rule (`_veredicto_metrica`: MIN_PRUEBA, MIN_BASE, UMBRAL) fed a per-day WINDOW mean
instead of a whole day's own figure, anchored at that day's own wake time ('despertar') or its
own logged moment ('momento'). Mirrors tests/test_experimentos_modelo.py's own fixture style
(noisy baseline, clear test-day shift). Invented demo data only, no real health figures."""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, experimentos

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_momentos_pull.py)


def _mkconn(case: unittest.TestCase, name: str = "ei.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _ms(day: str, minuto: int) -> int:
    local = datetime.fromisoformat(day) + timedelta(minutes=minuto)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _serie(day: str, desde_min: int, hasta_min: int, paso_min: int, valor: float) -> list[list]:
    out = []
    m = desde_min
    while m <= hasta_min:
        out.append([_ms(day, m), valor])
        m += paso_min
    return out


def _serie_con_hueco(day: str, desde_min: int, hasta_min: int, paso_min: int, valor: float,
                      hueco_desde: int, hueco_hasta: int, mantener: int) -> list[list]:
    """Like `_serie`, but keeps only `mantener` samples inside [hueco_desde, hueco_hasta] -- a
    watch not worn for part of the day, which drops samples without changing the device's own
    (still 2-minute) spacing."""
    out, m, contados = [], desde_min, 0
    while m <= hasta_min:
        en_hueco = hueco_desde <= m <= hueco_hasta
        if not en_hueco or contados < mantener:
            out.append([_ms(day, m), valor])
            if en_hueco:
                contados += 1
        m += paso_min
    return out


def _payload_fc(muestras: list[list]) -> dict:
    return {"heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
            "heartRateValues": muestras}


def _payload_estres(muestras: list[list]) -> dict:
    return {"stressValueDescriptorsDTOList": [{"key": "timestamp", "index": 0}, {"key": "stressLevel", "index": 1}],
            "stressValuesArray": muestras}


def _guardar_dia(conn, person_id: int, day: str, valor: float, *, desde_min: int = 240,
                  hasta_min: int = 600, paso_min: int = 2) -> None:
    """Stores a day's HR and stress series, every `paso_min` minutes from `desde_min` to
    `hasta_min` (240..600 = 04:00..10:00 local, wide enough to cover any window these tests
    build), a constant `valor` so a fully-covered window's mean is exactly `valor`."""
    muestras = _serie(day, desde_min, hasta_min, paso_min, valor)
    db.guardar_intradia(conn, person_id, day, "intradia_fc", _payload_fc(muestras))
    db.guardar_intradia(conn, person_id, day, "intradia_estres", _payload_estres(muestras))


def _set_wake(conn, person_id: int, day: str, hora: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, None, f"{day}T{hora}"),
    )


def _seed_baseline_despertar(conn, person_id: int, dias: int = 10) -> list[str]:
    """`dias` noisy baseline days ending 2026-11-10, wake 06:00, HR/stress alternating 58/62
    (mean 60, pstdev 2) -- same noisy-baseline idea as test_experimentos_modelo.py's own fixture."""
    inicio = date(2026, 11, 10) - timedelta(dias - 1)
    out = []
    for i in range(dias):
        day = (inicio + timedelta(i)).isoformat()
        _set_wake(conn, person_id, day, "06:00")
        _guardar_dia(conn, person_id, day, 58.0 if i % 2 == 0 else 62.0)
        out.append(day)
    return out


FILTRO_LIBRE = {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False}
FECHA_INICIO = "2026-11-11"


def _crear_despertar(conn, person_id: int, metricas: list[str], *, antes_min=None, despues_min=60,
                      fecha_inicio: str = FECHA_INICIO) -> int:
    ventana = {"tipo": "intradia", "ancla": "despertar", "antes_min": antes_min, "despues_min": despues_min}
    return db.crear_experimento(conn, person_id, "Enraizamiento", metricas, ventana, dict(FILTRO_LIBRE),
                                 fecha_inicio=fecha_inicio)


class DespertarVentanaTest(unittest.TestCase):
    def test_mas_bajo_from_a_wake_anchored_window(self):
        conn = _mkconn(self)
        _seed_baseline_despertar(conn, 1)
        eid = _crear_despertar(conn, 1, ["heart_rate"])
        dias = [(date(2026, 11, 11) + timedelta(i)).isoformat() for i in range(5)]
        for day in dias:
            _set_wake(conn, 1, day, "06:00")
            _guardar_dia(conn, 1, day, 40.0)
            db.registrar_dia_experimento(conn, eid, 1, day, True)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 15))
        m = v["metricas"]["heart_rate"]
        self.assertEqual(m["resultado"], "mas_bajo")
        self.assertEqual(m["n_prueba"], 5)
        self.assertEqual(m["n_base"], 10)
        self.assertAlmostEqual(m["media_prueba"], 40.0)
        self.assertAlmostEqual(m["media_base"], 60.0)
        self.assertEqual(v["dias_prueba"], dias)

    def test_mas_alto_from_a_wake_anchored_window(self):
        conn = _mkconn(self)
        _seed_baseline_despertar(conn, 1)
        eid = _crear_despertar(conn, 1, ["stress"])
        dias = [(date(2026, 11, 11) + timedelta(i)).isoformat() for i in range(5)]
        for day in dias:
            _set_wake(conn, 1, day, "06:00")
            _guardar_dia(conn, 1, day, 80.0)
            db.registrar_dia_experimento(conn, eid, 1, day, True)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 15))
        m = v["metricas"]["stress"]
        self.assertEqual(m["resultado"], "mas_alto")
        self.assertAlmostEqual(m["media_prueba"], 80.0)

    def test_a_small_change_inside_the_noise_reads_sin_cambio(self):
        conn = _mkconn(self)
        _seed_baseline_despertar(conn, 1)
        eid = _crear_despertar(conn, 1, ["heart_rate"])
        dias = [(date(2026, 11, 11) + timedelta(i)).isoformat() for i in range(5)]
        for i, day in enumerate(dias):
            _set_wake(conn, 1, day, "06:00")
            _guardar_dia(conn, 1, day, 59.0 if i % 2 == 0 else 61.0)
            db.registrar_dia_experimento(conn, eid, 1, day, True)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 15))
        self.assertEqual(v["metricas"]["heart_rate"]["resultado"], "sin_cambio")

    def test_fewer_than_min_prueba_days_reads_pocos_dias(self):
        conn = _mkconn(self)
        _seed_baseline_despertar(conn, 1)
        eid = _crear_despertar(conn, 1, ["heart_rate"])
        dias = [(date(2026, 11, 11) + timedelta(i)).isoformat() for i in range(2)]  # below MIN_PRUEBA (3)
        for day in dias:
            _set_wake(conn, 1, day, "06:00")
            _guardar_dia(conn, 1, day, 40.0)
            db.registrar_dia_experimento(conn, eid, 1, day, True)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 12))
        m = v["metricas"]["heart_rate"]
        self.assertEqual(m["resultado"], "pocos_dias")
        self.assertEqual(m["n_prueba"], 2)


class MomentoVentanaTest(unittest.TestCase):
    def test_mas_bajo_with_the_median_clock_baseline(self):
        """Baseline days have no moment of their own, so their anchor is the MEDIAN local clock
        time of the test days' own moments (06:00, 06:10, 06:20 -> 06:10)."""
        conn = _mkconn(self)
        base_dias = []
        inicio = date(2026, 11, 1)
        for i in range(10):
            day = (inicio + timedelta(i)).isoformat()
            _guardar_dia(conn, 1, day, 58.0 if i % 2 == 0 else 62.0)
            base_dias.append(day)
        ventana = {"tipo": "intradia", "ancla": "momento", "antes_min": 0, "despues_min": 30}
        eid = db.crear_experimento(conn, 1, "Ducha fria", ["heart_rate"], ventana, dict(FILTRO_LIBRE),
                                    fecha_inicio="2026-11-11")
        dias = [(date(2026, 11, 11) + timedelta(i)).isoformat() for i in range(3)]
        horas = ["06:00:00-05:00", "06:10:00-05:00", "06:20:00-05:00"]
        for day, hora in zip(dias, horas):
            mid = db.crear_momento(conn, 1, f"{day}T{hora}", "ducha_fria")
            _guardar_dia(conn, 1, day, 40.0)
            db.registrar_dia_experimento(conn, eid, 1, day, True, momento_id=mid)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 13))
        m = v["metricas"]["heart_rate"]
        self.assertEqual(m["resultado"], "mas_bajo")
        self.assertEqual(m["n_prueba"], 3)
        self.assertEqual(m["n_base"], 10)
        self.assertAlmostEqual(m["media_prueba"], 40.0)
        self.assertAlmostEqual(m["media_base"], 60.0)

    def test_no_moment_yet_on_any_test_day_leaves_the_baseline_anchorless(self):
        conn = _mkconn(self)
        for i in range(10):
            day = (date(2026, 11, 1) + timedelta(i)).isoformat()
            _guardar_dia(conn, 1, day, 58.0 if i % 2 == 0 else 62.0)
        ventana = {"tipo": "intradia", "ancla": "momento", "antes_min": 0, "despues_min": 30}
        eid = db.crear_experimento(conn, 1, "Ducha fria", ["heart_rate"], ventana, dict(FILTRO_LIBRE),
                                    fecha_inicio="2026-11-11")
        dia = "2026-11-11"
        db.registrar_dia_experimento(conn, eid, 1, dia, True)  # occurred, but no momento_id
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 11))
        m = v["metricas"]["heart_rate"]
        self.assertEqual(m["n_prueba"], 0)
        self.assertEqual(m["n_base"], 0)  # no median to anchor any baseline day either
        self.assertIn(dia, v["dias_prueba"])
        self.assertIn(dia, v["dias_sin_dato"]["prueba"])


class CoberturaTest(unittest.TestCase):
    def test_a_window_below_half_its_spacing_is_dropped_as_no_data(self):
        conn = _mkconn(self)
        _seed_baseline_despertar(conn, 1)
        eid = _crear_despertar(conn, 1, ["heart_rate"])
        dias_llenos = [(date(2026, 11, 11) + timedelta(i)).isoformat() for i in range(3)]
        for day in dias_llenos:
            _set_wake(conn, 1, day, "06:00")
            _guardar_dia(conn, 1, day, 40.0)
            db.registrar_dia_experimento(conn, eid, 1, day, True)
        dia_hueco = (date(2026, 11, 11) + timedelta(3)).isoformat()
        _set_wake(conn, 1, dia_hueco, "06:00")
        # The window [06:00, 07:00] normally holds 31 samples at this 2-minute spacing; only 2
        # survive here -- well under COBERTURA_MIN's 15.5 floor.
        muestras = _serie_con_hueco(dia_hueco, 240, 600, 2, 999.0, 360, 420, 2)
        db.guardar_intradia(conn, 1, dia_hueco, "intradia_fc", _payload_fc(muestras))
        db.registrar_dia_experimento(conn, eid, 1, dia_hueco, True)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 14))
        m = v["metricas"]["heart_rate"]
        self.assertEqual(m["n_prueba"], 3)  # the sparse day is dropped, not averaged in thin
        self.assertAlmostEqual(m["media_prueba"], 40.0)  # the 999s never entered the mean
        self.assertIn(dia_hueco, v["dias_prueba"])  # it DID occur
        self.assertIn(dia_hueco, v["dias_sin_dato"]["prueba"])  # its window just lacked data


class DiaNoAlmacenadoTest(unittest.TestCase):
    def test_a_day_never_fetched_has_no_data_not_an_error(self):
        conn = _mkconn(self)
        _seed_baseline_despertar(conn, 1)
        eid = _crear_despertar(conn, 1, ["heart_rate"])
        dias_llenos = [(date(2026, 11, 11) + timedelta(i)).isoformat() for i in range(3)]
        for day in dias_llenos:
            _set_wake(conn, 1, day, "06:00")
            _guardar_dia(conn, 1, day, 40.0)
            db.registrar_dia_experimento(conn, eid, 1, day, True)
        dia_sin_fetch = (date(2026, 11, 11) + timedelta(3)).isoformat()
        _set_wake(conn, 1, dia_sin_fetch, "06:00")  # a real wake anchor, but intraday never fetched
        db.registrar_dia_experimento(conn, eid, 1, dia_sin_fetch, True)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 14))
        m = v["metricas"]["heart_rate"]
        self.assertEqual(m["n_prueba"], 3)
        self.assertIn(dia_sin_fetch, v["dias_prueba"])
        self.assertIn(dia_sin_fetch, v["dias_sin_dato"]["prueba"])


class ZonaMedianocheTest(unittest.TestCase):
    def test_a_window_crossing_midnight_uses_both_calendar_days_own_data(self):
        """Wake at 00:10, antes_min=30/despues_min=30 -> window [23:40 the day before, 00:40] --
        spans two of db.intradia's own stored days. A sample stored under either day is included
        or excluded by its OWN parsed local timestamp, never by which day it was filed under."""
        conn = _mkconn(self)
        _seed_baseline_despertar(conn, 1)
        eid = _crear_despertar(conn, 1, ["heart_rate"], antes_min=30, despues_min=30)
        # Spaced two days apart so no test day's own data collides with another test day's
        # "day before" write, or with the baseline's own last day (2026-11-10).
        dias = [(date(2026, 11, 13) + timedelta(2 * i)).isoformat() for i in range(3)]
        for day in dias:
            _set_wake(conn, 1, day, "00:10")
            anterior = (date.fromisoformat(day) - timedelta(1)).isoformat()
            antes = _serie(anterior, 1410, 1438, 2, 40.0)  # 23:30..23:58, inside the window from 23:40
            antes.append([_ms(anterior, 1380), 999.0])  # 23:00 -- outside [23:40, 00:40]
            db.guardar_intradia(conn, 1, anterior, "intradia_fc", _payload_fc(antes))
            hoy_muestras = _serie(day, 0, 40, 2, 40.0)  # 00:00..00:40, inside the window
            hoy_muestras.append([_ms(day, 60), 999.0])  # 01:00 -- outside the window
            db.guardar_intradia(conn, 1, day, "intradia_fc", _payload_fc(hoy_muestras))
            db.registrar_dia_experimento(conn, eid, 1, day, True)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 17))
        m = v["metricas"]["heart_rate"]
        self.assertEqual(m["n_prueba"], 3)
        self.assertAlmostEqual(m["media_prueba"], 40.0)  # the 999s on either side never entered
        self.assertEqual(m["resultado"], "mas_bajo")


class AislamientoTest(unittest.TestCase):
    def test_another_persons_series_never_enters_this_persons_verdict(self):
        conn = _mkconn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        base_dias = _seed_baseline_despertar(conn, 1)
        for day in base_dias:
            _set_wake(conn, 2, day, "06:00")
            _guardar_dia(conn, 2, day, 999.0)  # would badly skew A's baseline if it ever leaked
        eid = _crear_despertar(conn, 1, ["heart_rate"])
        dias = [(date(2026, 11, 11) + timedelta(i)).isoformat() for i in range(3)]
        for day in dias:
            _set_wake(conn, 1, day, "06:00")
            _guardar_dia(conn, 1, day, 40.0)
            db.registrar_dia_experimento(conn, eid, 1, day, True)
        v = experimentos.veredicto(conn, 1, eid, hoy=date(2026, 11, 13))
        m = v["metricas"]["heart_rate"]
        self.assertAlmostEqual(m["media_base"], 60.0)  # never person B's 999s
        self.assertEqual(m["n_base"], 10)


if __name__ == "__main__":
    unittest.main()
