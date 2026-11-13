"""BODY-PLAN-MODULO-1: body.horizonte, the plan's own plan-vs-real module. Invented demo data
only -- no shift code, city, sport, hour or figure of any real person."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db, goals, horizonte

HOY = date(2026, 11, 12)  # a Thursday: week 2026-11-09..15 is "this week", 2026-11-02..08 is past


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    case.addCleanup(conn.close)
    db.init(conn)  # seeds person id 1 ('Me', DEFAULT_PERSON_ID)
    return conn


def _horizonte(conn, person_id=1, *, start_day="2026-10-05", deadline="2027-01-04",
               hogar_id=None, visibility="private") -> int:
    return goals.propose(conn, person_id, "Plan de prueba", start_day, kind="milestone",
                         deadline=deadline, hogar_id=hogar_id, visibility=visibility)


def _metric(conn, day, key, value, person_id=1, source="garmin"):
    db.set_metric(conn, day, key, value, source, person_id)


def _activity(conn, person_id, source_id, day, *, type, source="garmin", start_local=None,
              duration_s=None, vinculo_manual=None):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, type, source, start_local, duration_s, "
        "vinculo_manual) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, type, source, start_local, duration_s, vinculo_manual),
    )
    conn.commit()


def _shift(conn, person_id, day, kind):
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))
    conn.commit()


def _sueno(conn, person_id, dia_inicio: str, hora_inicio: str, hora_fin: str, dia_fin: str | None = None):
    """A night that started on `dia_inicio` at `hora_inicio` and ended the next day at `hora_fin`
    -- stored under Garmin's own wake-day key (`dia_fin`, default the day after `dia_inicio`),
    exactly the convention body/horizonte.py's `_sesiones_sueno` re-keys away from."""
    wake = dia_fin or (date.fromisoformat(dia_inicio) + timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, wake, f"{dia_inicio}T{hora_inicio}:00", f"{wake}T{hora_fin}:00"),
    )
    conn.commit()


def _plan(conn, person_id, goal_id, day, title, status):
    conn.execute("INSERT INTO plan (person_id, goal_id, day, title, status) VALUES (?, ?, ?, ?, ?)",
                 (person_id, goal_id, day, title, status))
    conn.commit()


class ValidarMedidaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_known_medida_accepts_its_own_sentidos(self):
        horizonte.validar_medida(self.conn, "sesiones_fuerza", "al_menos")
        horizonte.validar_medida(self.conn, "hora_acostarse", "antes_de")

    def test_known_medida_refuses_the_wrong_sentido(self):
        with self.assertRaises(ValueError):
            horizonte.validar_medida(self.conn, "sesiones_fuerza", "antes_de")
        with self.assertRaises(ValueError):
            horizonte.validar_medida(self.conn, "hora_acostarse", "al_menos")

    def test_generic_metric_def_key_accepts_al_menos_como_maximo(self):
        horizonte.validar_medida(self.conn, "resting_hr", "al_menos")
        horizonte.validar_medida(self.conn, "resting_hr", "como_maximo")

    def test_generic_metric_def_key_refuses_antes_de(self):
        with self.assertRaises(ValueError):
            horizonte.validar_medida(self.conn, "resting_hr", "antes_de")

    def test_unknown_medida_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_medida(self.conn, "no_existe", "al_menos")


class ValidarChequeoTest(unittest.TestCase):
    def test_null_is_solo_texto(self):
        horizonte.validar_chequeo(None)

    def test_acostarse_antes_valid(self):
        horizonte.validar_chequeo({"tipo": "acostarse_antes", "hora": "23:00"})

    def test_bad_hora_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_chequeo({"tipo": "acostarse_antes", "hora": "25:00"})
        with self.assertRaises(ValueError):
            horizonte.validar_chequeo({"tipo": "levantarse_antes", "hora": "7:30"})

    def test_sueno_minimo_valid_and_invalid(self):
        horizonte.validar_chequeo({"tipo": "sueno_minimo", "minutos": 420})
        with self.assertRaises(ValueError):
            horizonte.validar_chequeo({"tipo": "sueno_minimo", "minutos": 0})
        with self.assertRaises(ValueError):
            horizonte.validar_chequeo({"tipo": "sueno_minimo", "minutos": "420"})

    def test_sin_sesion_valid_and_bad_clase(self):
        horizonte.validar_chequeo({"tipo": "sin_sesion", "clase": "fuerza"})
        with self.assertRaises(ValueError):
            horizonte.validar_chequeo({"tipo": "sin_sesion", "clase": "nado"})

    def test_sesion_antes_de_valid(self):
        horizonte.validar_chequeo({"tipo": "sesion_antes_de", "hora": "08:00", "clase": "cualquiera"})

    def test_unknown_tipo_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_chequeo({"tipo": "siesta"})

    def test_not_a_dict_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_chequeo("acostarse_antes")


class ValidarCriteriosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_valid_list(self):
        horizonte.validar_criterios(self.conn, [
            {"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 3, "ventana": "semana"},
        ])

    def test_not_a_list_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_criterios(self.conn, "nope")

    def test_empty_list_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_criterios(self.conn, [])

    def test_bad_medida_inside_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_criterios(self.conn, [
                {"medida": "no_existe", "sentido": "al_menos", "valor": 1, "ventana": "semana"}])

    def test_bad_valor_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_criterios(self.conn, [
                {"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": "3", "ventana": "semana"}])

    def test_bad_ventana_refused(self):
        with self.assertRaises(ValueError):
            horizonte.validar_criterios(self.conn, [
                {"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 3, "ventana": "mes"}])


class RealFigurePorMedidaTest(unittest.TestCase):
    """Every MEDIDAS key's real figure, on invented data for the past week 2026-11-02..08."""

    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_sueno_minutos_mean_ignores_days_with_no_reading(self):
        _metric(self.conn, "2026-11-02", "sleep_minutes", 400)
        _metric(self.conn, "2026-11-03", "sleep_minutes", 440)
        # the other 5 days of the week carry no sleep_minutes row at all
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "sueno_minutos", "al_menos", 420)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["real"], 420.0)
        self.assertEqual(linea["dias_con_dato"], 2)
        self.assertEqual(linea["estado"], "cumple")

    def test_hora_acostarse_median_crosses_midnight_in_order(self):
        _sueno(self.conn, 1, "2026-11-02", "23:40", "06:00")  # minutes-after-noon = 700
        _sueno(self.conn, 1, "2026-11-03", "00:20", "06:00")  # minutes-after-noon = 740
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "hora_acostarse", "antes_de", 750)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["real"], 720.0)  # median of 700 and 740
        self.assertEqual(linea["noches_con_dato"], 2)
        self.assertEqual(linea["noches_cumplen"], 2)  # both <= 750
        self.assertEqual(linea["estado"], "cumple")

    def test_hora_levantarse_from_end_local(self):
        _sueno(self.conn, 1, "2026-11-02", "22:30", "05:30")  # end minutes-after-noon = (5-12)%24*60+30=1050
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "hora_levantarse", "antes_de", 1080)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["real"], 1050.0)
        self.assertEqual(linea["estado"], "cumple")

    def test_sesiones_fuerza_counts_classified_sessions(self):
        _activity(self.conn, 1, "f1", "2026-11-03", type="strength_training")
        _activity(self.conn, 1, "f2", "2026-11-05", type="strength_training")
        _activity(self.conn, 1, "r1", "2026-11-04", type="running")  # not strength
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "sesiones_fuerza", "al_menos", 2)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["real"], 2)
        self.assertEqual(linea["estado"], "cumple")
        self.assertNotIn("zona_medida", linea)

    def test_sesiones_cardio_counts_long_enough_sessions_only(self):
        _activity(self.conn, 1, "c1", "2026-11-03", type="running", duration_s=30 * 60)
        _activity(self.conn, 1, "c2", "2026-11-04", type="cycling", duration_s=10 * 60)  # too short
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "sesiones_cardio", "al_menos", 1)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["real"], 1)
        self.assertEqual(linea["zona_medida"], False)

    def test_minutos_intensidad_sums_the_week(self):
        _metric(self.conn, "2026-11-02", "intensity_minutes", 20)
        _metric(self.conn, "2026-11-03", "intensity_minutes", 30)
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "minutos_intensidad", "al_menos", 40)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["real"], 50)
        self.assertEqual(linea["estado"], "cumple")

    def test_generic_metric_def_key_is_the_weeks_mean(self):
        _metric(self.conn, "2026-11-02", "resting_hr", 58)
        _metric(self.conn, "2026-11-03", "resting_hr", 60)
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "resting_hr", "como_maximo", 62)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["real"], 59.0)
        self.assertEqual(linea["estado"], "cumple")


class SinDatoVsZeroTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_mean_line_with_no_reading_is_sin_dato_count_line_with_zero_sessions_is_not(self):
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "sueno_minutos", "al_menos", 420)
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "sesiones_fuerza", "al_menos", 2)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        por_medida = {l["medida"]: l for l in s["lineas"]}
        self.assertIsNone(por_medida["sueno_minutos"]["real"])
        self.assertEqual(por_medida["sueno_minutos"]["estado"], "sin_dato")
        self.assertEqual(por_medida["sesiones_fuerza"]["real"], 0)
        self.assertEqual(por_medida["sesiones_fuerza"]["dias_con_dato"], 7)
        self.assertEqual(por_medida["sesiones_fuerza"]["estado"], "no_cumple")  # week over, 0 < 2


class EstadoCuotaEnCursoVsNoCumpleTest(unittest.TestCase):
    """Thursday 2026-11-12, inside week 2026-11-09..15: 4 days left (Thu..Sun, inclusive)."""

    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_en_curso_when_still_reachable(self):
        _activity(self.conn, 1, "f1", "2026-11-09", type="strength_training")
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-09", "sesiones_fuerza", "al_menos", 5)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-09", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["real"], 1)
        self.assertEqual(linea["estado"], "en_curso")  # deficit 4, dias_restantes 4

    def test_no_cumple_when_no_longer_reachable(self):
        _activity(self.conn, 1, "f1", "2026-11-09", type="strength_training")
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-09", "sesiones_fuerza", "al_menos", 6)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-09", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["estado"], "no_cumple")  # deficit 5, dias_restantes 4

    def test_como_maximo_breaks_immediately_once_exceeded(self):
        _activity(self.conn, 1, "f1", "2026-11-09", type="strength_training")
        _activity(self.conn, 1, "f2", "2026-11-10", type="strength_training")
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-09", "sesiones_fuerza", "como_maximo", 1)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-09", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["estado"], "no_cumple")

    def test_como_maximo_still_en_curso_while_under(self):
        _activity(self.conn, 1, "f1", "2026-11-09", type="strength_training")
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-09", "sesiones_fuerza", "como_maximo", 3)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-09", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["estado"], "en_curso")

    def test_mean_line_stays_en_curso_mid_week_never_a_reachability_guess(self):
        _metric(self.conn, "2026-11-09", "sleep_minutes", 300)  # already below 420
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-09", "sueno_minutos", "al_menos", 420)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-09", hoy=HOY)
        linea = s["lineas"][0]
        self.assertEqual(linea["estado"], "en_curso")


class CorrectedLineTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_only_the_vigente_value_is_the_target_historial_has_both(self):
        meta_id = db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "sesiones_fuerza",
                                         "al_menos", 2)
        nuevo_id = db.corregir_meta_semanal(self.conn, 1, meta_id, 3)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        self.assertEqual(len(s["lineas"]), 1)
        self.assertEqual(s["lineas"][0]["valor"], 3)
        self.assertEqual(s["lineas"][0]["meta_id"], nuevo_id)

        historia = horizonte.historial_meta(self.conn, nuevo_id)
        self.assertEqual([h["id"] for h in historia], [meta_id, nuevo_id])
        self.assertEqual([h["valor"] for h in historia], [2, 3])
        self.assertEqual(historia[0]["vigente"], 0)
        self.assertEqual(historia[1]["vigente"], 1)


class PlannedSessionsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_done_vs_not_done_and_other_goals_excluded(self):
        other_goal = goals.propose(self.conn, 1, "Otra meta", "2026-10-05", kind="milestone",
                                   deadline="2027-01-04")
        _plan(self.conn, 1, self.horizon, "2026-11-02", "Sesion A", "done")
        _plan(self.conn, 1, self.horizon, "2026-11-04", "Sesion B", "proposed")
        _plan(self.conn, 1, self.horizon, "2026-11-05", "Sesion C", "skipped")
        _plan(self.conn, 1, other_goal, "2026-11-03", "No es de este plan", "done")

        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        self.assertEqual(s["sesiones_total"], 3)
        self.assertEqual(s["sesiones_hechas"], 1)
        titulos = {p["title"] for p in s["sesiones_planeadas"]}
        self.assertEqual(titulos, {"Sesion A", "Sesion B", "Sesion C"})


class ContextoReglaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_siempre_applies_every_day(self):
        db.crear_regla_horizonte(self.conn, 1, self.horizon, "siempre", None, "Todos los dias")
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        self.assertEqual(s["reglas"][0]["dias_aplica"], 7)

    def test_turno_applies_only_on_matching_shift_days(self):
        _shift(self.conn, 1, "2026-11-02", "night")
        _shift(self.conn, 1, "2026-11-03", "night")
        _shift(self.conn, 1, "2026-11-04", "morning")
        db.crear_regla_horizonte(self.conn, 1, self.horizon, "turno", "night", "En turno de noche")
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        self.assertEqual(s["reglas"][0]["dias_aplica"], 2)

    def test_descanso_off_day_applies_no_shift_row_is_sin_dato_not_rest(self):
        _shift(self.conn, 1, "2026-11-05", "off")
        _shift(self.conn, 1, "2026-11-06", "morning")
        # 2026-11-07 carries no shift row at all
        db.crear_regla_horizonte(self.conn, 1, self.horizon, "descanso", None, "Dia de descanso")
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        regla = s["reglas"][0]
        dias = {d["day"]: d for d in regla["dias"]}
        self.assertIn("2026-11-05", dias)       # off day: applies
        self.assertNotIn("2026-11-06", dias)    # confirmed non-off: does not apply
        self.assertIn("2026-11-07", dias)       # unknown: still counted, as sin_dato
        self.assertGreaterEqual(regla["dias_sin_dato"], 1)

    def test_tras_noche_extends_beyond_the_ventana_post_cap_of_ten(self):
        _shift(self.conn, 1, "2026-11-02", "night")
        _shift(self.conn, 1, "2026-11-03", "night")  # block's last night
        db.crear_regla_horizonte(self.conn, 1, self.horizon, "tras_noche", "12", "Recuperacion")
        # week 2026-11-09..15 is days 6..12 after the block's last night (2026-11-03):
        # day 11 (2026-11-14) and day 12 (2026-11-15) are both beyond ventana_post (10).
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-09", hoy=HOY)
        self.assertEqual(s["reglas"][0]["dias_aplica"], 7)

    def test_periodo_applies_only_inside_the_marked_stretch(self):
        db.crear_periodo(self.conn, 1, "malestar", "2026-11-03", "2026-11-05")
        db.crear_regla_horizonte(self.conn, 1, self.horizon, "periodo", "malestar", "Durante el malestar")
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        regla = s["reglas"][0]
        dias = {d["day"] for d in regla["dias"]}
        self.assertEqual(dias, {"2026-11-03", "2026-11-04", "2026-11-05"})


class ChequeoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def _regla(self, chequeo):
        return db.crear_regla_horizonte(self.conn, 1, self.horizon, "siempre", None, "Chequeo", chequeo=chequeo)

    def test_acostarse_antes(self):
        _sueno(self.conn, 1, "2026-11-02", "22:30", "06:00")  # before 23:00: cumple
        _sueno(self.conn, 1, "2026-11-03", "23:45", "06:00")  # after 23:00: no cumple
        self._regla({"tipo": "acostarse_antes", "hora": "23:00"})
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        regla = s["reglas"][0]
        por_dia = {d["day"]: d["cumple"] for d in regla["dias"]}
        self.assertTrue(por_dia["2026-11-02"])
        self.assertFalse(por_dia["2026-11-03"])

    def test_levantarse_antes(self):
        _sueno(self.conn, 1, "2026-11-02", "22:30", "06:00")  # before 06:30: cumple
        _sueno(self.conn, 1, "2026-11-03", "22:30", "07:15")  # after 06:30: no cumple
        self._regla({"tipo": "levantarse_antes", "hora": "06:30"})
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        por_dia = {d["day"]: d["cumple"] for d in s["reglas"][0]["dias"]}
        self.assertTrue(por_dia["2026-11-02"])
        self.assertFalse(por_dia["2026-11-03"])

    def test_sueno_minimo_and_sin_dato_when_no_reading(self):
        _metric(self.conn, "2026-11-02", "sleep_minutes", 430)
        _metric(self.conn, "2026-11-03", "sleep_minutes", 300)
        # 2026-11-04 carries no sleep_minutes reading at all
        self._regla({"tipo": "sueno_minimo", "minutos": 420})
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        por_dia = {d["day"]: d["cumple"] for d in s["reglas"][0]["dias"]}
        self.assertTrue(por_dia["2026-11-02"])
        self.assertFalse(por_dia["2026-11-03"])
        self.assertIsNone(por_dia["2026-11-04"])

    def test_sin_sesion_clase_fuerza_cardio_cualquiera(self):
        _activity(self.conn, 1, "f1", "2026-11-02", type="strength_training")
        _activity(self.conn, 1, "c1", "2026-11-03", type="running", duration_s=30 * 60)
        # 2026-11-04 has neither
        self._regla({"tipo": "sin_sesion", "clase": "cualquiera"})
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        por_dia = {d["day"]: d["cumple"] for d in s["reglas"][0]["dias"]}
        self.assertFalse(por_dia["2026-11-02"])
        self.assertFalse(por_dia["2026-11-03"])
        self.assertTrue(por_dia["2026-11-04"])

    def test_sesion_antes_de_cumple_no_cumple_no_session_and_sin_dato(self):
        _activity(self.conn, 1, "f1", "2026-11-02", type="strength_training", start_local="2026-11-02T07:00:00")
        _activity(self.conn, 1, "f2", "2026-11-03", type="strength_training", start_local="2026-11-03T09:00:00")
        _activity(self.conn, 1, "f3", "2026-11-05", type="strength_training", start_local=None)
        # 2026-11-04 has no strength session at all
        self._regla({"tipo": "sesion_antes_de", "hora": "08:00", "clase": "fuerza"})
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        por_dia = {d["day"]: d["cumple"] for d in s["reglas"][0]["dias"]}
        self.assertTrue(por_dia["2026-11-02"])
        self.assertFalse(por_dia["2026-11-03"])
        self.assertFalse(por_dia["2026-11-04"])
        self.assertIsNone(por_dia["2026-11-05"])


class SoloTextoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)

    def test_null_chequeo_judges_nothing(self):
        db.crear_regla_horizonte(self.conn, 1, self.horizon, "siempre", None, "Una siesta de 20 min", chequeo=None)
        s = horizonte.semana(self.conn, self.horizon, 1, "2026-11-02", hoy=HOY)
        regla = s["reglas"][0]
        self.assertTrue(regla["solo_texto"])
        self.assertEqual(regla["dias_aplica"], 7)
        self.assertEqual(regla["dias_cumple"], 0)
        self.assertEqual(regla["dias_no_cumple"], 0)
        self.assertEqual(regla["dias_sin_dato"], 0)
        self.assertTrue(all(d["cumple"] is None for d in regla["dias"]))


class CheckpointsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn, start_day="2026-10-05")

    def test_pendiente_for_a_day_after_hoy(self):
        db.crear_checkpoint_horizonte(self.conn, 1, self.horizon, "2026-12-01", "Revision", [
            {"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 1, "ventana": "semana"}])
        cps = horizonte.checkpoints(self.conn, self.horizon, 1, hoy=HOY)
        self.assertEqual(cps[0]["estado"], "pendiente")
        self.assertIsNone(cps[0]["criterios"][0]["real"])

    def test_cumple_in_the_semana_ventana(self):
        _activity(self.conn, 1, "f1", "2026-11-03", type="strength_training")
        _activity(self.conn, 1, "f2", "2026-11-05", type="strength_training")
        db.crear_checkpoint_horizonte(self.conn, 1, self.horizon, "2026-11-05", "Revision semana", [
            {"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 2, "ventana": "semana"}])
        cps = horizonte.checkpoints(self.conn, self.horizon, 1, hoy=HOY)
        self.assertEqual(cps[0]["estado"], "cumple")

    def test_no_cumple_in_the_desde_inicio_ventana(self):
        _activity(self.conn, 1, "f1", "2026-11-03", type="strength_training")
        db.crear_checkpoint_horizonte(self.conn, 1, self.horizon, "2026-11-05", "Revision acumulada", [
            {"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 20, "ventana": "desde_inicio"}])
        cps = horizonte.checkpoints(self.conn, self.horizon, 1, hoy=HOY)
        self.assertEqual(cps[0]["estado"], "no_cumple")

    def test_sin_dato_when_the_criterion_has_no_reading_at_all(self):
        db.crear_checkpoint_horizonte(self.conn, 1, self.horizon, "2026-11-05", "Revision sueno", [
            {"medida": "hora_acostarse", "sentido": "antes_de", "valor": 690, "ventana": "semana"}])
        cps = horizonte.checkpoints(self.conn, self.horizon, 1, hoy=HOY)
        self.assertEqual(cps[0]["estado"], "sin_dato")

    def test_no_cumple_wins_over_sin_dato_overall(self):
        db.crear_checkpoint_horizonte(self.conn, 1, self.horizon, "2026-11-05", "Revision mixta", [
            {"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 5, "ventana": "semana"},
            {"medida": "hora_acostarse", "sentido": "antes_de", "valor": 690, "ventana": "semana"},
        ])
        cps = horizonte.checkpoints(self.conn, self.horizon, 1, hoy=HOY)
        self.assertEqual(cps[0]["estado"], "no_cumple")


class ResumenHouseholdTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Compa')")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (3, 'Ajeno')")
        self.conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        self.conn.commit()
        self.horizon = _horizonte(self.conn, person_id=1, hogar_id="h-1", visibility="private")

    def test_household_child_goal_seen_via_member_access_private_goal_of_another_excluded(self):
        hijo_household = goals.propose(self.conn, 2, "Meta de Compa", "2026-10-05", kind="milestone",
                                       deadline="2027-01-04", hogar_id="h-1", visibility="household")
        self.conn.execute("UPDATE goal SET horizonte_goal_id = ? WHERE id = ?", (self.horizon, hijo_household))
        goals.activate(self.conn, hijo_household)

        hijo_ajeno = goals.propose(self.conn, 3, "Meta de Ajeno", "2026-10-05", metric_key="steps",
                                   direction="raise", target_value=9000.0, baseline_value=6000.0,
                                   deadline="2027-01-04")
        self.conn.execute("UPDATE goal SET horizonte_goal_id = ? WHERE id = ?", (self.horizon, hijo_ajeno))
        self.conn.commit()

        r = horizonte.resumen(self.conn, self.horizon, 1, hoy=HOY)
        ids_vistos = {h["goal_id"] for h in r["hijos"]}
        self.assertIn(hijo_household, ids_vistos)
        self.assertNotIn(hijo_ajeno, ids_vistos)
        visto = next(h for h in r["hijos"] if h["goal_id"] == hijo_household)
        self.assertEqual(visto["acceso"], "member")

    def test_experiments_and_deadline_days(self):
        exp_id = db.crear_experimento(self.conn, 1, "Prueba", ["resting_hr"], {"tipo": "dia"},
                                      {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False},
                                      fecha_inicio="2026-11-01")
        self.conn.execute("UPDATE experimento SET goal_id = ? WHERE id = ?", (self.horizon, exp_id))
        self.conn.commit()
        r = horizonte.resumen(self.conn, self.horizon, 1, hoy=HOY)
        self.assertEqual([e["id"] for e in r["experimentos"]], [exp_id])
        self.assertEqual(r["dias_a_deadline"], (date(2027, 1, 4) - HOY).days)


class _ContadorConexion:
    """Counts `.execute` calls on an already-open connection (the same pattern
    tests/test_entreno_racha.py's own statement-count guard uses)."""
    def __init__(self, conn):
        self._conn = conn
        self.llamadas = 0

    def execute(self, *a, **kw):
        self.llamadas += 1
        return self._conn.execute(*a, **kw)

    def __getattr__(self, nombre):
        return getattr(self._conn, nombre)


class StatementCountBoundedTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.horizon = _horizonte(self.conn)
        for i, day in enumerate(("2026-11-02", "2026-11-04", "2026-11-06")):
            _activity(self.conn, 1, f"f{i}", day, type="strength_training")

    def test_one_week_vs_twelve_weeks_same_count(self):
        conteos = []
        for semanas in (1, 12):
            hasta = "2026-11-02" if semanas == 1 else "2027-01-18"  # 12 Mondays apart
            contador = _ContadorConexion(self.conn)
            horizonte.plan_vs_real(contador, self.horizon, 1, "2026-11-02", hasta, hoy=HOY)
            conteos.append(contador.llamadas)
        self.assertEqual(conteos[0], conteos[1], f"statement count grew with week count: {conteos}")

    def test_one_line_vs_eight_lines_same_count(self):
        db.crear_meta_semanal(self.conn, 1, self.horizon, "2026-11-02", "sesiones_fuerza", "al_menos", 2)
        semana_ocho = "2026-11-09"
        for medida, sentido, valor in [
            ("sesiones_fuerza", "al_menos", 2), ("sesiones_cardio", "al_menos", 1),
            ("sueno_minutos", "al_menos", 400), ("minutos_intensidad", "al_menos", 30),
            ("hora_acostarse", "antes_de", 700), ("hora_levantarse", "antes_de", 1100),
            ("resting_hr", "como_maximo", 60), ("steps", "al_menos", 5000),
        ]:
            db.crear_meta_semanal(self.conn, 1, self.horizon, semana_ocho, medida, sentido, valor)

        contador1 = _ContadorConexion(self.conn)
        horizonte.semana(contador1, self.horizon, 1, "2026-11-02", hoy=HOY)
        contador8 = _ContadorConexion(self.conn)
        horizonte.semana(contador8, self.horizon, 1, semana_ocho, hoy=HOY)
        self.assertEqual(contador1.llamadas, contador8.llamadas,
                          f"statement count grew with line count: {contador1.llamadas} vs {contador8.llamadas}")


if __name__ == "__main__":
    unittest.main()
