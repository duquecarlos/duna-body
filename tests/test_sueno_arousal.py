"""body/sueno_arousal.py: BODY-SUENO-DETECTOR-1. Invented demo nights only, no real health
figures -- the same `_mkconn`/`_guardar_sueno`/`_sesion` shape tests/test_despertar_real.py and
tests/test_leer_serie_minuto.py already use.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db, garmin_adapter, sueno_arousal

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_despertar_real.py convention)

DIA = "2026-11-11"
BASE = datetime.fromisoformat(f"{DIA}T01:00:00")
BASE_FC = 50.0
BASE_MOV = 1.0
MOV_ALTO = 5.0


def _mkconn(case: unittest.TestCase, name: str = "a.db"):
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / name)
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _guardar_sueno(conn, person_id: int, day: str, hr=(), hrv=(), movimiento=()) -> None:
    payload = {}
    if hr:
        payload["sleepHeartRate"] = [{"startGMT": _ms(ts), "value": v} for ts, v in hr]
    if hrv:
        payload["hrvData"] = [{"startGMT": _ms(ts), "value": v} for ts, v in hrv]
    if movimiento:
        payload["sleepMovement"] = [{"startGMT": _ms(ts), "activityLevel": v} for ts, v in movimiento]
    conn.execute(
        "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, ?, 'sleep', ?, ?, ?)",
        (person_id, day, garmin_adapter.SOURCE, "2026-01-01T00:00:00+00:00", json.dumps(payload)),
    )


def _sesion(conn, person_id: int, day: str, start_local: str, end_local: str) -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, start_local, end_local),
    )


def _serie(desde_min: int, hasta_min: int, paso_min: int, valor: float) -> list[tuple[str, float]]:
    out = []
    m = desde_min
    while m <= hasta_min:
        out.append(((BASE + timedelta(minutes=m)).isoformat(), valor))
        m += paso_min
    return out


def _marcar(muestras: list[tuple[str, float]], desde_min: int, hasta_min: int, paso_min: int,
            valor: float) -> list[tuple[str, float]]:
    """`muestras` with every sample whose own minute offset falls in [desde_min, hasta_min]
    replaced by `valor` -- used to carve an elevated stretch out of an otherwise-constant series
    without having to re-list every untouched minute by hand."""
    out = []
    for ts, v in muestras:
        offset = int((datetime.fromisoformat(ts) - BASE).total_seconds() // 60)
        out.append((ts, valor if desde_min <= offset <= hasta_min else v))
    return out


FIN_30 = f"{DIA}T01:30:00"
INICIO = f"{DIA}T01:00:00"


def _fixture(case: unittest.TestCase, end_local: str = FIN_30, name: str = "a.db"):
    conn = _mkconn(case, name)
    _sesion(conn, 1, DIA, INICIO, end_local)
    return conn


class SinSesionTest(unittest.TestCase):
    def test_sin_sesion_de_sueno_da_none(self):
        conn = _mkconn(self)
        self.assertIsNone(sueno_arousal.estimar(conn, 1, DIA))


class CalmNightTest(unittest.TestCase):
    def test_noche_calma_todo_dormido(self):
        conn = _fixture(self)
        _guardar_sueno(conn, 1, DIA, hr=_serie(0, 30, 1, BASE_FC), movimiento=_serie(0, 30, 1, BASE_MOV))
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(resultado.version, sueno_arousal.VERSION)
        self.assertEqual(resultado.bouts, ())
        self.assertEqual(resultado.totales_min["dormido"], 31)
        self.assertEqual(resultado.totales_min["arousal"], 0)
        self.assertEqual(resultado.totales_min["despierto"], 0)
        self.assertEqual(resultado.totales_min["sin_dato"], 0)
        self.assertEqual(resultado.cobertura, 1.0)
        self.assertTrue(all(e == "dormido" for _, e in resultado.minutos))


class OneMinuteStirTest(unittest.TestCase):
    def test_un_minuto_de_movimiento_vuelve_a_dormido(self):
        conn = _fixture(self)
        hr = _marcar(_serie(0, 30, 1, BASE_FC), 15, 15, 1, BASE_FC + 8.0)
        mov = _marcar(_serie(0, 30, 1, BASE_MOV), 15, 15, 1, MOV_ALTO)
        _guardar_sueno(conn, 1, DIA, hr=hr, movimiento=mov)
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(resultado.bouts, ())
        self.assertEqual(resultado.totales_min["dormido"], 31)


class ThreeMinuteRiseTest(unittest.TestCase):
    def test_tres_minutos_de_subida_es_un_bout_de_arousal(self):
        conn = _fixture(self)
        hr = _marcar(_serie(0, 30, 1, BASE_FC), 15, 17, 1, BASE_FC + 8.0)
        mov = _marcar(_serie(0, 30, 1, BASE_MOV), 15, 17, 1, MOV_ALTO)
        _guardar_sueno(conn, 1, DIA, hr=hr, movimiento=mov)
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(len(resultado.bouts), 1)
        inicio, fin, tipo = resultado.bouts[0]
        self.assertEqual(tipo, "arousal")
        self.assertEqual(inicio, (BASE + timedelta(minutes=15)).isoformat())
        self.assertEqual(fin, (BASE + timedelta(minutes=18)).isoformat())
        self.assertEqual(resultado.totales_min["arousal"], 3)
        self.assertEqual(resultado.totales_min["dormido"], 28)


class FifteenMinuteRiseTest(unittest.TestCase):
    def test_quince_minutos_de_subida_es_despierto(self):
        conn = _fixture(self)
        hr = _marcar(_serie(0, 30, 1, BASE_FC), 10, 24, 1, BASE_FC + 8.0)
        mov = _marcar(_serie(0, 30, 1, BASE_MOV), 10, 24, 1, MOV_ALTO)
        _guardar_sueno(conn, 1, DIA, hr=hr, movimiento=mov)
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(len(resultado.bouts), 1)
        inicio, fin, tipo = resultado.bouts[0]
        self.assertEqual(tipo, "despierto")
        self.assertEqual(inicio, (BASE + timedelta(minutes=10)).isoformat())
        self.assertEqual(fin, (BASE + timedelta(minutes=25)).isoformat())
        self.assertEqual(resultado.totales_min["despierto"], 15)


class StillRacingHeartTest(unittest.TestCase):
    def test_doce_minutos_quieto_pero_con_el_corazon_acelerado_es_despierto(self):
        conn = _fixture(self)
        hr = _marcar(_serie(0, 30, 1, BASE_FC), 10, 21, 1, BASE_FC + 12.0)
        mov = _serie(0, 30, 1, BASE_MOV)  # never above its own median -- movement alone misses this
        _guardar_sueno(conn, 1, DIA, hr=hr, movimiento=mov)
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(len(resultado.bouts), 1)
        inicio, fin, tipo = resultado.bouts[0]
        self.assertEqual(tipo, "despierto")
        self.assertEqual(inicio, (BASE + timedelta(minutes=10)).isoformat())
        self.assertEqual(fin, (BASE + timedelta(minutes=22)).isoformat())
        self.assertEqual(resultado.totales_min["despierto"], 12)


class GapInSeriesTest(unittest.TestCase):
    def test_un_hueco_en_la_serie_es_sin_dato_y_baja_la_cobertura(self):
        conn = _fixture(self)
        hr = _serie(0, 30, 1, BASE_FC)
        mov_completo = _serie(0, 30, 1, BASE_MOV)
        mov = [(ts, v) for ts, v in mov_completo
               if not (10 <= int((datetime.fromisoformat(ts) - BASE).total_seconds() // 60) <= 19)]
        _guardar_sueno(conn, 1, DIA, hr=hr, movimiento=mov)
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(resultado.totales_min["sin_dato"], 11)
        self.assertLess(resultado.cobertura, 1.0)
        self.assertEqual(round(resultado.cobertura, 4), round((31 - 11) / 31, 4))


class NoMovementSeriesTest(unittest.TestCase):
    def test_sin_serie_de_movimiento_todo_sin_dato_con_motivo(self):
        conn = _fixture(self)
        _guardar_sueno(conn, 1, DIA, hr=_serie(0, 30, 1, BASE_FC))
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(resultado.motivo, "sin_movimiento")
        self.assertEqual(resultado.totales_min["sin_dato"], 31)
        self.assertEqual(resultado.cobertura, 0.0)
        self.assertEqual(resultado.bouts, ())
        self.assertTrue(all(e == "sin_dato" for _, e in resultado.minutos))


class HeartRateSpacingTest(unittest.TestCase):
    """Cadence measured, never assumed (ruling 5): a 2-minute and a 5-minute heart-rate spacing
    both tile the same still-but-racing rise correctly, and `espaciado_s` reports the real one."""

    def test_espaciado_de_dos_minutos_tila_correctamente(self):
        conn = _fixture(self, end_local=f"{DIA}T01:40:00")
        hr = _marcar(_serie(0, 40, 2, BASE_FC), 10, 20, 2, BASE_FC + 12.0)
        mov = _serie(0, 40, 1, BASE_MOV)
        _guardar_sueno(conn, 1, DIA, hr=hr, movimiento=mov)
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(resultado.espaciado_s["heart_rate"], 120.0)
        self.assertEqual(len(resultado.bouts), 1)
        inicio, fin, tipo = resultado.bouts[0]
        self.assertEqual(tipo, "despierto")
        self.assertEqual(inicio, (BASE + timedelta(minutes=10)).isoformat())
        self.assertEqual(fin, (BASE + timedelta(minutes=22)).isoformat())

    def test_espaciado_de_cinco_minutos_tila_correctamente(self):
        conn = _fixture(self, end_local=f"{DIA}T01:40:00")
        hr = _marcar(_serie(0, 40, 5, BASE_FC), 10, 20, 5, BASE_FC + 12.0)
        mov = _serie(0, 40, 1, BASE_MOV)
        _guardar_sueno(conn, 1, DIA, hr=hr, movimiento=mov)
        conn.commit()
        resultado = sueno_arousal.estimar(conn, 1, DIA)
        self.assertEqual(resultado.espaciado_s["heart_rate"], 300.0)
        self.assertEqual(len(resultado.bouts), 1)
        inicio, fin, tipo = resultado.bouts[0]
        self.assertEqual(tipo, "despierto")
        self.assertEqual(inicio, (BASE + timedelta(minutes=10)).isoformat())
        self.assertEqual(fin, (BASE + timedelta(minutes=25)).isoformat())


class OwnBaselineTest(unittest.TestCase):
    """Baselines are the night's own and the person's own (ruling 3): two nights with different
    resting heart rates give the same labels for the same RELATIVE rise."""

    def test_dos_noches_con_distinta_base_dan_la_misma_etiqueta(self):
        conn_bajo = _fixture(self, name="bajo.db")
        hr_bajo = _marcar(_serie(0, 30, 1, 50.0), 15, 17, 1, 58.0)
        mov_bajo = _marcar(_serie(0, 30, 1, BASE_MOV), 15, 17, 1, MOV_ALTO)
        _guardar_sueno(conn_bajo, 1, DIA, hr=hr_bajo, movimiento=mov_bajo)
        conn_bajo.commit()
        resultado_bajo = sueno_arousal.estimar(conn_bajo, 1, DIA)

        conn_alto = _fixture(self, name="alto.db")
        hr_alto = _marcar(_serie(0, 30, 1, 70.0), 15, 17, 1, 78.0)
        mov_alto = _marcar(_serie(0, 30, 1, BASE_MOV), 15, 17, 1, MOV_ALTO)
        _guardar_sueno(conn_alto, 1, DIA, hr=hr_alto, movimiento=mov_alto)
        conn_alto.commit()
        resultado_alto = sueno_arousal.estimar(conn_alto, 1, DIA)

        self.assertEqual(
            [e for _, e in resultado_bajo.minutos],
            [e for _, e in resultado_alto.minutos],
        )
        self.assertEqual(len(resultado_bajo.bouts), 1)
        self.assertEqual(resultado_bajo.bouts[0][2], "arousal")
        self.assertEqual(resultado_alto.bouts[0][2], "arousal")


class PersonaFugaTest(unittest.TestCase):
    """A second person's elevated night never leaks into this person's own quiet one."""

    def test_la_noche_de_otra_persona_no_se_filtra(self):
        conn = _fixture(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        _sesion(conn, 2, DIA, INICIO, FIN_30)
        _guardar_sueno(conn, 1, DIA, hr=_serie(0, 30, 1, BASE_FC), movimiento=_serie(0, 30, 1, BASE_MOV))
        hr_b = _marcar(_serie(0, 30, 1, BASE_FC), 10, 24, 1, BASE_FC + 8.0)
        mov_b = _marcar(_serie(0, 30, 1, BASE_MOV), 10, 24, 1, MOV_ALTO)
        _guardar_sueno(conn, 2, DIA, hr=hr_b, movimiento=mov_b)
        conn.commit()

        resultado_1 = sueno_arousal.estimar(conn, 1, DIA)
        resultado_2 = sueno_arousal.estimar(conn, 2, DIA)
        self.assertTrue(all(e == "dormido" for _, e in resultado_1.minutos))
        self.assertEqual(resultado_1.bouts, ())
        self.assertEqual(len(resultado_2.bouts), 1)
        self.assertEqual(resultado_2.bouts[0][2], "despierto")


if __name__ == "__main__":
    unittest.main()
