"""body/recuperacion.py: back-to-USUAL-for-that-hour after a workout (BODY-RECUPERACION-1,
BODY-RECUPERACION-1-C) -- the sustained threshold crossing against the person's OWN hourly usual,
honest reason codes, recalcular's idempotence, version-bump and per-run cache behaviour, and
estudio()'s censored median and factor groups/findings. Invented demo data only, no real health
figures.
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

from body import db, demo, pull, recuperacion

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


def _fondo_horas(conn, person_id: int, antes_de: str, dias: int, valores_por_hora: dict[int, float],
                  default: float = 60.0) -> None:
    """`dias` days of a full, evenly-spaced (2-min) background day, ending the day BEFORE
    `antes_de` -- `_usual_hora`'s own look-back window reads strictly before an activity's day,
    exactly as baselines.baseline does. Hour `h` reads `valores_por_hora.get(h, default)` every
    day -- a constant per hour, so each hour's `_usual_hora` is predictable in a test."""
    fin = date.fromisoformat(antes_de) - timedelta(1)
    paso = timedelta(minutes=2)
    for i in range(dias):
        d = fin - timedelta(i)
        t = datetime.combine(d, datetime.min.time())
        fin_dia = t + timedelta(days=1)
        muestras = []
        while t < fin_dia:
            muestras.append((t.isoformat(), valores_por_hora.get(t.hour, default)))
            t += paso
        _guardar_fc(conn, person_id, d.isoformat(), muestras)


def _fondo_dias(conn, person_id: int, antes_de: str, dias: int, valor: float) -> None:
    """Every hour's own usual is the SAME constant `valor` -- the common case in these tests,
    where cutoff = valor + MODELO['margen_bpm'] regardless of which hour a sample falls in."""
    _fondo_horas(conn, person_id, antes_de, dias, {}, default=valor)


def _actividad(day: str, start_local: str, duration_s: float) -> dict:
    return {"day": day, "start_local": start_local, "duration_s": duration_s}


class RecuperacionActividadTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn(self)
        _fondo_dias(self.conn, 1, "2026-11-10", 15, 55.0)  # usual = 55 every hour, cutoff = 55 + 3 = 58

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


class UsualHoraTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2 (replaced, BODY-RECUPERACION-1-C): the cutoff is
    the person's OWN usual for the CLOCK HOUR a sample falls in, not a single resting-range number
    for the whole search."""

    def test_tarde_de_escritorio_se_recupera_donde_el_viejo_reposo_nunca_lo_haria(self):
        conn = _mkconn(self)
        # 15 ordinary days before the activity, desk-level ~70 bpm at 14:00-14:59.
        _fondo_dias(conn, 1, "2026-11-10", 15, 70.0)
        # The activity ends at 14:30; heart rate is elevated briefly, then settles at 72 -- well
        # above any plausible resting-range cutoff (the OLD model's own target, ~55 + margin = 58)
        # for the WHOLE horizon, but at/under this hour's OWN usual (70) + margin (3) = 73.
        muestras = _serie("2026-11-10", 14 * 60, 14 * 60 + 28, 2, 130.0) + \
            _serie("2026-11-10", 14 * 60 + 30, 14 * 60 + 40, 2, 120.0) + \
            _serie("2026-11-10", 14 * 60 + 42, 18 * 60, 2, 72.0)
        _guardar_fc(conn, 1, "2026-11-10", muestras)
        r = recuperacion.recuperacion_actividad(
            conn, 1, _actividad("2026-11-10", "2026-11-10T14:00:00", 1800))
        self.assertIsNone(r.motivo)
        self.assertEqual(r.minutos, 12)
        old_cutoff = 55.0 + recuperacion.MODELO["margen_bpm"]
        self.assertGreater(72.0, old_cutoff)  # the OLD resting-range cutoff never reaches 72

    def test_el_corte_usa_la_hora_de_cada_muestra(self):
        """The search crosses an hour boundary (13 -> 14) with two very different usual levels --
        the SAME heart rate value counts as "still elevated" at 13:58 and "recovered" one minute
        later, once the clock hour it falls in changes."""
        conn = _mkconn(self)
        _fondo_horas(conn, 1, "2026-11-10", 15, {13: 50.0, 14: 90.0})
        # Ends at 13:58 (13:30 start + 28 min); heart rate is a constant 60 afterward -- above
        # hour 13's own cutoff (50 + 3 = 53) but at/under hour 14's (90 + 3 = 93).
        muestras = _serie("2026-11-10", 13 * 60 + 30, 13 * 60 + 56, 2, 130.0) + \
            _serie("2026-11-10", 13 * 60 + 58, 17 * 60 + 58, 2, 60.0)
        _guardar_fc(conn, 1, "2026-11-10", muestras)
        r = recuperacion.recuperacion_actividad(
            conn, 1, _actividad("2026-11-10", "2026-11-10T13:30:00", 1680))
        self.assertIsNone(r.motivo)
        self.assertEqual(r.minutos, 2)


class SinRangoTest(unittest.TestCase):
    def test_sin_ningun_fondo_es_sin_rango(self):
        conn = _mkconn(self)
        muestras = _serie("2026-11-10", 18 * 60 + 30, 22 * 60, 2, 120.0)
        _guardar_fc(conn, 1, "2026-11-10", muestras)
        r = recuperacion.recuperacion_actividad(
            conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertEqual(r.motivo, "sin_rango")
        self.assertIsNone(r.minutos)

    def test_fondo_bajo_el_minimo_de_dias_sigue_siendo_sin_rango(self):
        conn = _mkconn(self)
        _fondo_dias(conn, 1, "2026-11-10", 5, 55.0)  # under MODELO['min_dias_horario'] (10)
        muestras = _serie("2026-11-10", 18 * 60 + 30, 22 * 60, 2, 120.0)
        _guardar_fc(conn, 1, "2026-11-10", muestras)
        r = recuperacion.recuperacion_actividad(
            conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertEqual(r.motivo, "sin_rango")


class DiaMarcadoTest(unittest.TestCase):
    def test_dia_marcado_nunca_llega_a_calcular(self):
        conn = _mkconn(self)
        db.crear_periodo(conn, 1, "malestar", "2026-11-10", "2026-11-10")
        conn.commit()
        r = recuperacion.recuperacion_actividad(
            conn, 1, _actividad("2026-11-10", "2026-11-10T18:00:00", 1800))
        self.assertEqual(r.motivo, "marcado")
        self.assertIsNone(r.minutos)


class RecalcularTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn(self)
        _fondo_dias(self.conn, 1, "2026-11-10", 15, 55.0)
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
        self.assertEqual((row["minutos"], row["motivo"], row["modelo_version"]), (12, None, "recuperacion-v2"))

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
        with mock.patch.dict(recuperacion.MODELO, {"version": "recuperacion-v3"}):
            resultado = recuperacion.recalcular(self.conn, 1)
            self.assertEqual(resultado["nuevas"], 1)
        rows = self.conn.execute(
            "SELECT modelo_version FROM recuperacion_actividad WHERE person_id = 1 AND activity_source_id = 'act-1' "
            "ORDER BY modelo_version"
        ).fetchall()
        self.assertEqual([r["modelo_version"] for r in rows], ["recuperacion-v2", "recuperacion-v3"])

    def test_motivos_son_solo_conteos_nunca_una_cifra_de_salud(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            resultado = recuperacion.recalcular(self.conn, 1)
        self.assertIsInstance(resultado["nuevas"], int)
        for k, v in resultado["motivos"].items():
            self.assertIn(k, recuperacion.REASON_CODES)
            self.assertIsInstance(v, int)


class LecturaPorDiaAcotadaTest(unittest.TestCase):
    """BODY-RECUPERACION-1-C point 1: `recalcular`'s per-run cache (`_leer_dia`) must read a
    given day's intraday_fc series via db.intradia AT MOST ONCE per run, even though several
    activities' horizon AND look-back windows all touch it."""

    def test_recalcular_nunca_relee_el_mismo_dia(self):
        conn = _mkconn(self)
        dias_actividad = ["2026-11-08", "2026-11-09", "2026-11-10"]
        for i, day in enumerate(dias_actividad):
            conn.execute(
                "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s) "
                "VALUES (1, 'garmin', ?, ?, ?, 1800)",
                (f"act-{i}", day, f"{day}T18:00:00"),
            )
        conn.commit()
        # One shared look-back background, well within every one of the three activities' own
        # 30-day window (their windows overlap heavily since the activities are 1-2 days apart).
        _fondo_dias(conn, 1, "2026-11-08", 15, 55.0)
        for day in dias_actividad:
            muestras = _serie(day, 18 * 60 + 30, 22 * 60, 2, 50.0)
            _guardar_fc(conn, 1, day, muestras)

        llamadas: list[str] = []
        original = db.intradia

        def contador(conn_, person_id_, day_, kind_, zona_):
            llamadas.append(day_)
            return original(conn_, person_id_, day_, kind_, zona_)

        with mock.patch.object(db, "intradia", side_effect=contador):
            recuperacion.recalcular(conn, 1)
        self.assertEqual(sorted(llamadas), sorted(set(llamadas)))
        self.assertGreater(len(llamadas), 0)


class RecalcularSeguroTest(unittest.TestCase):
    """body/pull.py's `_recalcular_recuperacion_seguro` (BODY-RECUPERACION-1-B): silent when the
    recompute found nothing new, one counts-only line when it wrote a row -- the same rule
    `_cerrar_semanas_seguro`'s own sweep already follows."""

    def setUp(self):
        self.conn = _mkconn(self)
        _fondo_dias(self.conn, 1, "2026-11-10", 15, 55.0)
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
                    tipo: str, minutos: float | None, avg_hr: float = 130.0, motivo: str | None = None) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, type, avg_hr) "
        "VALUES (?, 'garmin', ?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, start_local, duration_s, tipo, avg_hr),
    )
    conn.execute(
        "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, modelo_version, "
        "minutos, motivo, computed_at) VALUES (?, 'garmin', ?, ?, ?, ?, '2026-11-10T00:00:00+00:00')",
        (person_id, source_id, recuperacion.MODELO["version"], minutos, motivo),
    )


class MedianaCensuradaTest(unittest.TestCase):
    """BODY-RECUPERACION-1-C point 2: a `fuera_de_horizonte` session counts as right-censored --
    known only to be more than the horizon -- never simply dropped."""

    def test_bajo_la_mitad_censurado_da_una_mediana(self):
        valores = [float(x) for x in range(1, 38)]  # 37 finite + 3 censored = 40, 7.5% censored
        r = recuperacion._mediana_censurada(valores, 3, 240.0)
        self.assertIsNone(r["mas_de"])
        self.assertEqual(r["n"], 40)
        self.assertIsNotNone(r["mediana"])

    def test_la_mitad_exacta_censurado_no_tiene_mediana_honesta(self):
        valores = [float(x) for x in range(1, 21)]  # 20 finite + 20 censored = 40, exactly half
        r = recuperacion._mediana_censurada(valores, 20, 240.0)
        self.assertIsNone(r["mediana"])
        self.assertEqual(r["mas_de"], 240.0)

    def test_sin_censura_es_la_mediana_de_siempre(self):
        r = recuperacion._mediana_censurada([10.0, 20.0, 30.0], 0, 240.0)
        self.assertEqual(r["mediana"], 20.0)

    def test_sin_ninguna_sesion(self):
        r = recuperacion._mediana_censurada([], 0, 240.0)
        self.assertEqual(r, {"mediana": None, "mas_de": None, "n": 0})


class EstudioTest(unittest.TestCase):
    def test_sin_filas_es_n_cero(self):
        conn = _mkconn(self)
        est = recuperacion.estudio(conn, 1)
        self.assertEqual(est, {"n": 0, "n_censurados": 0, "mediana": None, "mas_de": None,
                                "bajo": None, "alto": None, "hallazgos": []})

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

    def test_estudio_cuenta_las_fuera_de_horizonte_en_la_mediana_general(self):
        conn = _mkconn(self)
        for i in range(37):
            day = f"2026-07-{(i % 28) + 1:02d}"
            _insertar_fila(conn, 1, f"ok-{i}", day, f"{day}T18:00:00", 1800, "running", float(10 + i))
        for i in range(3):
            day = f"2026-06-0{i + 1}"
            _insertar_fila(conn, 1, f"fuera-{i}", day, f"{day}T18:00:00", 1800, "running", None,
                            motivo="fuera_de_horizonte")
        conn.commit()
        est = recuperacion.estudio(conn, 1)
        self.assertEqual(est["n"], 40)
        self.assertEqual(est["n_censurados"], 3)
        self.assertIsNotNone(est["mediana"])
        self.assertIsNone(est["mas_de"])

    def test_censuradas_cuentan_dentro_de_cada_grupo_del_hallazgo(self):
        """Point 2 applies inside every factor group too: a group whose finite sessions look as
        fast as another's, but where most of the rest never came back within the horizon, is the
        worse group -- not an invisible one."""
        conn = _mkconn(self)
        for i in range(5):
            day = f"2026-05-0{i + 1}"
            db.set_metric(conn, day, "sleep_minutes", 300.0, "garmin", 1)  # corta
            _insertar_fila(conn, 1, f"corta-{i}", day, f"{day}T18:00:00", 1800, "running", 20.0)
        for i in range(2):
            day = f"2026-05-{11 + i}"
            db.set_metric(conn, day, "sleep_minutes", 450.0, "garmin", 1)  # normal, finite
            _insertar_fila(conn, 1, f"normal-{i}", day, f"{day}T18:00:00", 1800, "running", 20.0)
        for i in range(3):
            day = f"2026-05-{14 + i}"
            db.set_metric(conn, day, "sleep_minutes", 450.0, "garmin", 1)  # normal, censored
            _insertar_fila(conn, 1, f"normalfuera-{i}", day, f"{day}T18:00:00", 1800, "running", None,
                            motivo="fuera_de_horizonte")
        conn.commit()
        est = recuperacion.estudio(conn, 1)
        sueno = [h for h in est["hallazgos"] if h["factor"] == "sueno_anoche"]
        self.assertEqual(len(sueno), 1)
        h = sueno[0]
        self.assertEqual(h["peor"], "normal")
        self.assertEqual(h["mejor"], "corta")
        self.assertIsNone(h["min_peor"])
        self.assertEqual(h["mas_de_peor"], recuperacion.MODELO["horizonte_h"] * 60)
        self.assertEqual(h["n_peor"], 5)
        self.assertEqual(h["n_mejor"], 5)


class DemoSeedOrderTest(unittest.TestCase):
    """BODY-RECUPERACION-1-M point 6 (BODY-DEMO-RECUPERACION-SEED-ORDEN-1): body/demo.py's
    `_seed_recuperacion` must run AFTER `_seed_periodo` seeds the marked periods, or
    `recuperacion.recalcular` scores a marked day's activity from its own invented curve instead
    of reading it honestly as `marcado` -- db.dias_marcados() finds nothing marked yet at the time
    it runs."""

    def test_actividad_en_dia_marcado_lleva_marcado(self):
        class _FakeToday(date):
            @classmethod
            def today(cls):
                return date(2026, 11, 10)

        conn = _mkconn(self, "demo-orden.db")
        with mock.patch.object(demo, "date", _FakeToday):
            demo._seed(conn, dias=demo.DIAS_DEFAULT, idioma="es")

        periodos = conn.execute(
            "SELECT start_day FROM period WHERE person_id = 1 AND kind = 'lesion'"
        ).fetchall()
        self.assertTrue(periodos, "the demo's open 'lesion' period was not seeded")
        inicio = date.fromisoformat(periodos[0]["start_day"])

        rows = conn.execute(
            "SELECT a.day, ra.motivo FROM activity a "
            "JOIN recuperacion_actividad ra ON ra.person_id = a.person_id "
            "AND ra.activity_source = a.source AND ra.activity_source_id = a.source_id "
            "WHERE a.person_id = 1"
        ).fetchall()
        marcados = [r for r in rows if date.fromisoformat(r["day"]) >= inicio]
        self.assertTrue(marcados, "no seeded activity falls inside the open 'lesion' period "
                                   "-- this test's own fixed anchor no longer overlaps it")
        for r in marcados:
            self.assertEqual(r["motivo"], "marcado", f"activity on {r['day']} (inside the marked "
                              f"period starting {inicio}) was not scored as 'marcado'")


if __name__ == "__main__":
    unittest.main()
