"""body/despertar_real.py: BODY-DESPERTAR-REAL-3. Invented nights only, no real health figures --
the same `_mkconn`/`_guardar_fc`/`_sesion` shape tests/test_sueno_fc.py and tests/test_turno_fc.py
already use.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db, despertar_real

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_turno_fc.py convention)


def _mkconn(case: unittest.TestCase, name: str = "d.db"):
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / name)
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _payload_fc(muestras: list[tuple[str, float]]) -> dict:
    return {"heartRateValues": [[_ms(ts), v] for ts, v in muestras]}


def _payload_estres(muestras: list[tuple[str, float]]) -> dict:
    return {"stressValuesArray": [[_ms(ts), v] for ts, v in muestras]}


def _guardar_fc(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_fc", _payload_fc(muestras))


def _guardar_estres(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_estres", _payload_estres(muestras))


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


def _comida(conn, person_id: int, day: str, ts_local: str) -> None:
    conn.execute(
        "INSERT INTO comida (person_id, day, ts_local, created_at, updated_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
        (person_id, day, ts_local),
    )


def _checkin(conn, person_id: int, day: str, ts_local: str) -> None:
    conn.execute(
        "INSERT INTO checkin (person_id, day, ts_local, momento, created_at) "
        "VALUES (?, ?, ?, 'manana', '2026-01-01T00:00:00')",
        (person_id, day, ts_local),
    )


def _actividad(conn, person_id: int, day: str, start_local: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, source) "
        "VALUES (?, ?, ?, ?, 'running', 'manual')",
        (person_id, source_id, day, start_local),
    )


DAY = "2026-11-11"
BASE_FC = 50.0
BASE_ESTRES = 15.0
# baseline window is MODELO['ventana_base_min'] (30) minutes before sleep end -- 04:30..04:58,
# stopping short of 05:00 itself so a baseline sample never shares Garmin's own sleep-end minute
# with a post-wake one.
BASE_MUESTRAS = _serie(DAY, 4 * 60 + 30, 4 * 60 + 58, 2, BASE_FC)
BASE_ESTRES_MUESTRAS = _serie(DAY, 4 * 60 + 30, 4 * 60 + 58, 3, BASE_ESTRES)
END_LOCAL = f"{DAY}T05:00:00"
START_LOCAL = f"{DAY[:-2]}{int(DAY[-2:]) - 1:02d}T22:00:00"


def _fixture(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = _mkconn(case)
    _sesion(conn, 1, DAY, START_LOCAL, END_LOCAL)
    return conn


class SinSesionYSinLineaBaseTest(unittest.TestCase):
    """Ruling 4's honest gaps."""

    def test_sin_sesion_de_sueno(self):
        conn = _mkconn(self)
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["confianza"], "baja")
        self.assertEqual(resultado["propuestas"], [])
        self.assertEqual(resultado["evidencia"]["motivo"], "sin_sesion")
        self.assertEqual(resultado["segmentos"], [])

    def test_sin_linea_base_sin_ninguna_muestra(self):
        conn = _fixture(self)
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["confianza"], "baja")
        self.assertEqual(resultado["propuestas"], [])
        self.assertEqual(resultado["evidencia"]["motivo"], "sin_linea_base")


class DozingTailTest(unittest.TestCase):
    """A dozing tail after Garmin's own end, then quiet awake with no rise within the horizon --
    proposed as SLEEP, with the later end (ruling: "dozing after Garmin's own end is proposed as
    sleep")."""

    def test_dozing_tail_proposes_sueno_with_the_later_end(self):
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 6 * 60 + 28, 2, BASE_FC + 2.0)
                    + _serie(DAY, 6 * 60 + 30, 9 * 60, 2, BASE_FC + 8.0))
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["propuestas"], [{"tipo": "sueno", "hora": f"{DAY}T06:30:00"}])
        tipos = [s["tipo"] for s in resultado["segmentos"]]
        self.assertEqual(tipos[0], "dormitando")
        self.assertIn("despierto_quieto", tipos)
        self.assertNotIn("levantado", tipos)


class QuietAwakeThenRiseTest(unittest.TestCase):
    """Quiet awake time in bed only bounds the get-up time -- it is never itself proposed as
    sleep; the get-up moment is the sustained rise that follows it."""

    def test_quiet_awake_then_rise_proposes_levantado_at_the_rise(self):
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 5 * 60 + 58, 2, BASE_FC + 8.0)
                    + _serie(DAY, 6 * 60, 9 * 60, 2, BASE_FC + 20.0))
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["propuestas"], [{"tipo": "levantado", "hora": f"{DAY}T06:00:00"}])
        self.assertEqual(resultado["primera_levantada"], f"{DAY}T06:00:00")
        tipos = [s["tipo"] for s in resultado["segmentos"]]
        self.assertEqual(tipos, ["despierto_quieto", "levantado"])


class ImmediateRiseTest(unittest.TestCase):
    """An immediate rise right at Garmin's own end -- Garmin was already right, nothing to
    propose."""

    def test_immediate_rise_proposes_nothing(self):
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 9 * 60, 2, BASE_FC + 20.0))
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["propuestas"], [])
        self.assertEqual(resultado["segmentos"][0]["tipo"], "levantado")


class WatchOffGapTest(unittest.TestCase):
    """A watch-off gap over the whole horizon: 'sin_dato', never classified, and lower
    confidence."""

    def test_watch_off_gap_is_sin_dato_and_lowers_confidence(self):
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS)  # nothing at all after Garmin's own end
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["segmentos"], [
            {"tipo": "sin_dato", "inicio": f"{DAY}T05:00:00", "fin": f"{DAY}T09:00:00"}])
        self.assertEqual(resultado["confianza"], "baja")
        self.assertEqual(resultado["propuestas"], [])


class EventoAcotaLevantadoTest(unittest.TestCase):
    """A logged event before any rise bounds the get-up time, and appears in the evidence by
    source -- never the HR/stress figures, never the event's own content."""

    def test_comida_antes_de_cualquier_subida_acota_la_hora(self):
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 9 * 60, 2, BASE_FC + 8.0))
        _comida(conn, 1, DAY, f"{DAY}T07:15:00")
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["propuestas"], [{"tipo": "levantado", "hora": f"{DAY}T07:15:00"}])
        self.assertEqual(resultado["confianza"], "media")
        self.assertEqual(resultado["evidencia"]["eventos"], [{"fuente": "comida", "hora": f"{DAY}T07:15:00"}])

    def test_checkin_y_actividad_tambien_cuentan_como_fuente(self):
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 9 * 60, 2, BASE_FC + 8.0))
        _checkin(conn, 1, DAY, f"{DAY}T08:00:00")
        _actividad(conn, 1, DAY, f"{DAY}T07:00:00", "zz-act-1")
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        # the EARLIEST logged event is the tighter upper bound.
        self.assertEqual(resultado["propuestas"], [{"tipo": "levantado", "hora": f"{DAY}T07:00:00"}])
        fuentes = {e["fuente"] for e in resultado["evidencia"]["eventos"]}
        self.assertEqual(fuentes, {"checkin", "actividad"})


class RelativeBaselineTest(unittest.TestCase):
    """The same night shape at two different baselines classifies the same (ruling 2: relative
    thresholds, never an absolute bpm)."""

    def test_same_shape_shifted_forty_bpm_classifies_identically(self):
        conn = _mkconn(self)
        offset = 40.0
        for day, delta in ((DAY, 0.0), ("2026-11-12", offset)):
            start = f"{day[:-2]}{int(day[-2:]) - 1:02d}T22:00:00"
            end = f"{day}T05:00:00"
            _sesion(conn, 1, day, start, end)
            base = _serie(day, 4 * 60 + 30, 4 * 60 + 58, 2, BASE_FC + delta)
            quieto = _serie(day, 5 * 60, 5 * 60 + 58, 2, BASE_FC + delta + 8.0)
            subida = _serie(day, 6 * 60, 9 * 60, 2, BASE_FC + delta + 20.0)
            _guardar_fc(conn, 1, day, base + quieto + subida)
        conn.commit()
        base_resultado = despertar_real.estimar(conn, 1, DAY)
        alto_resultado = despertar_real.estimar(conn, 1, "2026-11-12")
        tipos_a = [s["tipo"] for s in base_resultado["segmentos"]]
        tipos_b = [s["tipo"] for s in alto_resultado["segmentos"]]
        self.assertEqual(tipos_a, tipos_b)
        hora_a = datetime.fromisoformat(base_resultado["primera_levantada"])
        fin_sueno_a = datetime.fromisoformat(base_resultado["sueno_fin"])
        hora_b = datetime.fromisoformat(alto_resultado["primera_levantada"])
        fin_sueno_b = datetime.fromisoformat(alto_resultado["sueno_fin"])
        self.assertEqual(hora_a - fin_sueno_a, hora_b - fin_sueno_b)
        self.assertEqual([p["tipo"] for p in base_resultado["propuestas"]],
                          [p["tipo"] for p in alto_resultado["propuestas"]])


class DozingThenQuietThenRiseTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-6A-R2 ruling 2: a morning can truly have BOTH -- dozed past Garmin's
    own end (sueno), then quiet awake time, then a later sustained rise (levantado), neither one
    dropping the other."""

    def test_proposes_both_sueno_and_levantado(self):
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 6 * 60 + 28, 2, BASE_FC + 2.0)
                    + _serie(DAY, 6 * 60 + 30, 6 * 60 + 58, 2, BASE_FC + 8.0)
                    + _serie(DAY, 7 * 60, 9 * 60, 2, BASE_FC + 20.0))
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["propuestas"], [
            {"tipo": "sueno", "hora": f"{DAY}T06:30:00"},
            {"tipo": "levantado", "hora": f"{DAY}T07:00:00"},
        ])
        tipos = [s["tipo"] for s in resultado["segmentos"]]
        self.assertEqual(tipos, ["dormitando", "despierto_quieto", "levantado"])

    def test_dozing_only_proposes_sueno_alone(self):
        """The same shape DozingTailTest uses -- no rise ever follows the dozing tail, so only
        'sueno' is proposed, never a 'levantado' entry invented from nothing."""
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 6 * 60 + 28, 2, BASE_FC + 2.0)
                    + _serie(DAY, 6 * 60 + 30, 9 * 60, 2, BASE_FC + 8.0))
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        self.assertEqual(resultado["propuestas"], [{"tipo": "sueno", "hora": f"{DAY}T06:30:00"}])


class EstresFueraDeDescansoRequeridoTest(unittest.TestCase):
    """Ruling 2's compound condition: a heart-rate rise big enough to be a candidate, but with
    stress staying inside its own rest range, never confirms as 'levantado'."""

    def test_subida_de_fc_sin_subida_de_estres_no_es_levantado(self):
        conn = _fixture(self)
        _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 9 * 60, 2, BASE_FC + 20.0))
        _guardar_estres(conn, 1, DAY, BASE_ESTRES_MUESTRAS + _serie(DAY, 5 * 60, 9 * 60, 3, BASE_ESTRES))
        conn.commit()
        resultado = despertar_real.estimar(conn, 1, DAY)
        tipos = {s["tipo"] for s in resultado["segmentos"]}
        self.assertNotIn("levantado", tipos)


if __name__ == "__main__":
    unittest.main()
