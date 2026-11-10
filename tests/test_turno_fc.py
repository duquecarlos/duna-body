"""body/turno_fc.py: Carlos's study 2 (ORCH-CARLOS-DELEGATES-TIER-1-1) -- the shift-window figure
against the person's own days-off reference for the same hours, honest reason codes, activity and
marked-day exclusion, recalcular's idempotence and version-bump behaviour, and estudio()'s kind and
sleep groups with the top findings. Invented demo data only, no real health figures.
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

from body import db, pull, turno_fc

ZONA_OFFSET_H = 5  # America/Bogota is UTC-5 year round (tests/test_momentos_pull.py convention)


def _mkconn(case: unittest.TestCase, name: str = "t.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _payload(muestras: list[tuple[str, float]]) -> dict:
    return {"heartRateValues": [[_ms(ts), v] for ts, v in muestras]}


def _guardar_fc(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_fc", _payload(muestras))


def _guardar_estres(conn, person_id: int, day: str, muestras: list[tuple[str, float]]) -> None:
    db.guardar_intradia(conn, person_id, day, "intradia_estres", _payload(muestras))


def _serie(day: str, desde_min: int, hasta_min: int, paso_min: int, valor: float) -> list[tuple[str, float]]:
    out = []
    base = datetime.fromisoformat(f"{day}T00:00:00")
    m = desde_min
    while m <= hasta_min:
        out.append(((base + timedelta(minutes=m)).isoformat(), valor))
        m += paso_min
    return out


def _dia_completo(day: str, valor: float, paso_min: int = 2) -> list[tuple[str, float]]:
    """A full 24h of samples (the whole day, not just up to the last whole hour) -- an off-day
    background day needs every hour fully covered, including the 23rd."""
    return _serie(day, 0, 24 * 60 - paso_min, paso_min, valor)


def _fondo_off(conn, person_id: int, dias: list[str], valor: float = 60.0) -> None:
    """`dias` as confirmed days off, each with a full flat background day -- turno_fc's own
    days-off reference (ruling 2)."""
    for d in dias:
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, 'off', 'manual')",
            (person_id, d),
        )
        _guardar_fc(conn, person_id, d, _dia_completo(d, valor))


def _turno(conn, person_id: int, day: str, kind: str, start_local: str, end_local: str) -> None:
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, end_local, source) "
        "VALUES (?, ?, ?, ?, ?, 'manual')",
        (person_id, day, kind, start_local, end_local),
    )


def _shift_row(conn, person_id: int, day: str):
    return conn.execute(
        "SELECT day, kind, start_local, end_local FROM shift WHERE person_id = ? AND day = ? "
        "AND kind != 'off'", (person_id, day),
    ).fetchone()


class TurnoFcVentanaTest(unittest.TestCase):
    """Ruling 1: the unit is the shift's own window, including a night shift that crosses
    midnight in a non-UTC zone. Ruling 2: the reference is the SAME CLOCK HOURS on days off."""

    def test_turno_de_mañana_normal(self):
        conn = _mkconn(self)
        off_days = [f"2026-11-{d:02d}" for d in range(1, 9)]  # 8 off days, clears min_dias_horario
        _fondo_off(conn, 1, off_days, 70.0)
        _turno(conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        _guardar_fc(conn, 1, "2026-11-10", _serie("2026-11-10", 6 * 60, 14 * 60, 2, 60.0))
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertIsNone(r.motivo)
        self.assertEqual(r.fc_mediana, 60.0)
        self.assertEqual(r.referencia_fc, 70.0)
        self.assertEqual(r.bajo_usual_pct, 100)

    def test_turno_nocturno_cruza_medianoche_en_zona_no_utc(self):
        conn = _mkconn(self)
        off_days = [f"2026-10-{d:02d}" for d in range(1, 9)]
        _fondo_off(conn, 1, off_days, 55.0)
        _turno(conn, 1, "2026-11-10", "night", "2026-11-10T22:00:00", "2026-11-11T06:00:00")
        muestras = _serie("2026-11-10", 22 * 60, 23 * 60 + 58, 2, 58.0) + \
            _serie("2026-11-11", 0, 6 * 60, 2, 58.0)
        _guardar_fc(conn, 1, "2026-11-10", muestras[:len(muestras) // 2])
        _guardar_fc(conn, 1, "2026-11-11", muestras[len(muestras) // 2:])
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertIsNone(r.motivo)
        self.assertEqual(r.fc_mediana, 58.0)

    def test_estres_sin_cobertura_no_bloquea_la_fc(self):
        """Stress has no coverage floor of its own (ruling 3 only requires a reading, not a gate):
        missing stress samples still give a heart-rate figure."""
        conn = _mkconn(self)
        off_days = [f"2026-11-{d:02d}" for d in range(1, 9)]
        _fondo_off(conn, 1, off_days, 70.0)
        _turno(conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        _guardar_fc(conn, 1, "2026-11-10", _serie("2026-11-10", 6 * 60, 14 * 60, 2, 60.0))
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertIsNone(r.motivo)
        self.assertIsNone(r.estres_mediana)


class ExclusionesTest(unittest.TestCase):
    """Ruling 5: a marked day, and an hour touched by one of the person's own activities, are
    left out."""

    def test_dia_marcado_nunca_llega_a_calcular(self):
        conn = _mkconn(self)
        _turno(conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        db.crear_periodo(conn, 1, "malestar", "2026-11-10", "2026-11-10")
        conn.commit()
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertEqual(r.motivo, "marcado")

    def test_hora_con_actividad_se_excluye_de_la_ventana(self):
        conn = _mkconn(self)
        off_days = [f"2026-11-{d:02d}" for d in range(1, 9)]
        _fondo_off(conn, 1, off_days, 70.0)
        _turno(conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        conn.execute(
            "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s) "
            "VALUES (1, 'garmin', 'act-1', '2026-11-10', '2026-11-10T08:00:00', 1800)"
        )
        conn.commit()
        # Hour 8 (the activity's own hour) runs hot; every other shift hour is flat at 60.
        muestras = _serie("2026-11-10", 6 * 60, 7 * 60 + 58, 2, 60.0) + \
            _serie("2026-11-10", 8 * 60, 8 * 60 + 58, 2, 150.0) + \
            _serie("2026-11-10", 9 * 60, 14 * 60, 2, 60.0)
        _guardar_fc(conn, 1, "2026-11-10", muestras)
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertIsNone(r.motivo)
        self.assertEqual(r.fc_mediana, 60.0)  # the hot hour-8 samples never entered the median


class CoberturaTest(unittest.TestCase):
    def test_pocas_muestras_en_la_ventana_es_sin_cobertura(self):
        conn = _mkconn(self)
        _turno(conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        _guardar_fc(conn, 1, "2026-11-10", [("2026-11-10T06:00:00", 60.0), ("2026-11-10T06:02:00", 61.0)])
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertEqual(r.motivo, "sin_cobertura")

    def test_sin_ninguna_muestra_es_sin_cobertura(self):
        conn = _mkconn(self)
        _turno(conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertEqual(r.motivo, "sin_cobertura")

    def test_sin_dias_de_descanso_es_sin_rango(self):
        conn = _mkconn(self)
        _turno(conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        _guardar_fc(conn, 1, "2026-11-10", _serie("2026-11-10", 6 * 60, 14 * 60, 2, 60.0))
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertEqual(r.motivo, "sin_rango")
        self.assertIsNone(r.fc_mediana)

    def test_start_local_ausente_es_sin_cobertura(self):
        conn = _mkconn(self)
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-10', 'morning', 'manual')"
        )
        conn.commit()
        row = _shift_row(conn, 1, "2026-11-10")
        r = turno_fc.turno_fc(conn, 1, row)
        self.assertEqual(r.motivo, "sin_cobertura")


class RecalcularTest(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn(self)
        off_days = [f"2026-11-{d:02d}" for d in range(1, 9)]
        _fondo_off(self.conn, 1, off_days, 70.0)
        _turno(self.conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        _guardar_fc(self.conn, 1, "2026-11-10", _serie("2026-11-10", 6 * 60, 14 * 60, 2, 60.0))
        self.conn.commit()

    def test_escribe_una_fila_nueva(self):
        resultado = turno_fc.recalcular(self.conn, 1)
        self.assertEqual(resultado["nuevas"], 1)
        row = self.conn.execute(
            "SELECT fc_mediana, referencia_fc, motivo, modelo_version FROM turno_fc "
            "WHERE person_id = 1 AND day = '2026-11-10'"
        ).fetchone()
        self.assertEqual((row["fc_mediana"], row["referencia_fc"], row["motivo"], row["modelo_version"]),
                         (60.0, 70.0, None, "turno-fc-v1"))

    def test_turno_de_descanso_nunca_entra(self):
        turno_fc.recalcular(self.conn, 1)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM turno_fc WHERE person_id = 1 AND kind = 'off'"
        ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_es_idempotente(self):
        turno_fc.recalcular(self.conn, 1)
        resultado2 = turno_fc.recalcular(self.conn, 1)
        self.assertEqual(resultado2["nuevas"], 0)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM turno_fc WHERE person_id = 1 AND day = '2026-11-10'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_un_cambio_de_version_recalcula_sin_borrar_la_version_anterior(self):
        turno_fc.recalcular(self.conn, 1)
        with mock.patch.dict(turno_fc.MODELO, {"version": "turno-fc-v2"}):
            resultado = turno_fc.recalcular(self.conn, 1)
            self.assertEqual(resultado["nuevas"], 1)
        rows = self.conn.execute(
            "SELECT modelo_version FROM turno_fc WHERE person_id = 1 AND day = '2026-11-10' "
            "ORDER BY modelo_version"
        ).fetchall()
        self.assertEqual([r["modelo_version"] for r in rows], ["turno-fc-v1", "turno-fc-v2"])

    def test_motivos_son_solo_conteos_nunca_una_cifra_de_salud(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            resultado = turno_fc.recalcular(self.conn, 1)
        self.assertIsInstance(resultado["nuevas"], int)
        for k, v in resultado["motivos"].items():
            self.assertIn(k, turno_fc.REASON_CODES)
            self.assertIsInstance(v, int)


class CacheUnaSolaLecturaTest(unittest.TestCase):
    """A day touched by several shifts' own windows or look-back is read via db.intradia AT MOST
    ONCE per (day, kind) in a single `recalcular` run."""

    def test_recalcular_nunca_relee_el_mismo_dia_y_tipo(self):
        conn = _mkconn(self)
        off_days = [f"2026-10-{d:02d}" for d in range(1, 29)]
        _fondo_off(conn, 1, off_days, 65.0)
        dias_turno = ["2026-11-08", "2026-11-09", "2026-11-10"]
        for d in dias_turno:
            _turno(conn, 1, d, "morning", f"{d}T06:00:00", f"{d}T14:00:00")
            _guardar_fc(conn, 1, d, _serie(d, 6 * 60, 14 * 60, 2, 60.0))
        conn.commit()

        llamadas: list[tuple[str, str]] = []
        original = db.intradia

        def contador(conn_, person_id_, day_, kind_, zona_):
            llamadas.append((day_, kind_))
            return original(conn_, person_id_, day_, kind_, zona_)

        with mock.patch.object(db, "intradia", side_effect=contador):
            turno_fc.recalcular(conn, 1)
        self.assertEqual(sorted(llamadas), sorted(set(llamadas)))
        self.assertGreater(len(llamadas), 0)


class EstudioTest(unittest.TestCase):
    """Ruling 4: groups with counts by kind, and inside each kind by the previous night's sleep --
    a group under MODELO['min_n_grupo'] reads "pocos_datos". The top differences come back as
    findings; a difference under MODELO's noise floor is never one."""

    def _insertar_fila(self, conn, day: str, kind: str, fc_mediana: float, referencia_fc: float) -> None:
        conn.execute(
            "INSERT INTO turno_fc (person_id, day, kind, modelo_version, fc_mediana, referencia_fc, "
            "motivo, computed_at) VALUES (1, ?, ?, ?, ?, ?, NULL, '2026-11-10T00:00:00+00:00')",
            (day, kind, turno_fc.MODELO["version"], fc_mediana, referencia_fc),
        )

    def test_grupo_bajo_el_minimo_dice_pocos_datos(self):
        conn = _mkconn(self)
        for i in range(3):
            self._insertar_fila(conn, f"2026-09-0{i + 1}", "morning", 60.0, 70.0)
        conn.commit()
        est = turno_fc.estudio(conn, 1)
        self.assertEqual(est["kinds"]["morning"]["motivo"], "pocos_datos")
        self.assertEqual(est["kinds"]["morning"]["n"], 3)

    def test_diferencia_bajo_el_ruido_no_es_hallazgo(self):
        conn = _mkconn(self)
        for i in range(6):
            day = f"2026-09-{i + 1:02d}"
            self._insertar_fila(conn, day, "morning", 70.0, 71.0)  # diff=1, under the 2.0bpm floor
        conn.commit()
        est = turno_fc.estudio(conn, 1)
        self.assertEqual(est["kinds"]["morning"]["diff"], 1)
        self.assertEqual(est["hallazgos"], [])

    def test_diferencia_clara_es_hallazgo_y_se_ordena_por_magnitud(self):
        conn = _mkconn(self)
        for i in range(6):
            day = f"2026-09-{i + 1:02d}"
            self._insertar_fila(conn, day, "morning", 60.0, 70.0)  # diff=10
        for i in range(6):
            day = f"2026-10-{i + 1:02d}"
            self._insertar_fila(conn, day, "night", 58.0, 62.0)  # diff=4
        conn.commit()
        est = turno_fc.estudio(conn, 1)
        self.assertEqual([h["kind"] for h in est["hallazgos"]], ["morning", "night"])

    def test_sin_filas_la_seccion_queda_vacia(self):
        conn = _mkconn(self)
        est = turno_fc.estudio(conn, 1)
        self.assertEqual(est, {"kinds": {}, "hallazgos": []})

    def test_division_por_sueno_corta_vs_normal(self):
        conn = _mkconn(self)
        for i in range(10):
            day = f"2026-09-{i + 1:02d}"
            self._insertar_fila(conn, day, "morning", 60.0, 70.0)
            db.set_metric(conn, day, "sleep_minutes", 250.0 if i < 5 else 420.0, "garmin", 1)
        conn.commit()
        est = turno_fc.estudio(conn, 1)
        sleep = est["kinds"]["morning"]["sleep"]
        self.assertEqual(sleep["corta"]["n"], 5)
        self.assertEqual(sleep["normal"]["n"], 5)
        self.assertNotIn("motivo", sleep["corta"])
        self.assertNotIn("motivo", sleep["normal"])

    def test_grupo_de_sueno_bajo_el_minimo_dice_pocos_datos(self):
        conn = _mkconn(self)
        for i in range(6):
            day = f"2026-09-{i + 1:02d}"
            self._insertar_fila(conn, day, "morning", 60.0, 70.0)
            db.set_metric(conn, day, "sleep_minutes", 250.0 if i < 1 else 420.0, "garmin", 1)
        conn.commit()
        est = turno_fc.estudio(conn, 1)
        sleep = est["kinds"]["morning"]["sleep"]
        self.assertEqual(sleep["corta"]["motivo"], "pocos_datos")
        self.assertEqual(sleep["corta"]["n"], 1)


class RecalcularSeguroTest(unittest.TestCase):
    """body/pull.py's `_recalcular_turno_fc_seguro`: silent when nothing is new, one counts-only
    line when a row was written -- the same rule `_recalcular_recuperacion_seguro` follows."""

    def setUp(self):
        self.conn = _mkconn(self)
        off_days = [f"2026-11-{d:02d}" for d in range(1, 9)]
        _fondo_off(self.conn, 1, off_days, 70.0)
        _turno(self.conn, 1, "2026-11-10", "morning", "2026-11-10T06:00:00", "2026-11-10T14:00:00")
        _guardar_fc(self.conn, 1, "2026-11-10", _serie("2026-11-10", 6 * 60, 14 * 60, 2, 60.0))
        self.conn.commit()

    def test_prints_a_counts_only_line_when_a_row_is_new(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pull._recalcular_turno_fc_seguro(self.conn, 1)
        self.assertEqual(buf.getvalue().strip(), "turno_fc: 1 filas nuevas")

    def test_stays_silent_on_a_re_run_with_nothing_new(self):
        pull._recalcular_turno_fc_seguro(self.conn, 1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            pull._recalcular_turno_fc_seguro(self.conn, 1)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
