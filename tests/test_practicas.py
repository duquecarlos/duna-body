"""BODY-HISTORIA-VIDA-1: body/practicas.py -- validation, person scope, archiving, the three day
states, bridged vs broken day-level rachas, a semana_sin_dato week, a bridged temporada, context
vs baseline with a None key, and suggestions at 7 vs 8 weeks. Invented demo data only."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, practicas


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)  # seeds person id 1 ('Me', DEFAULT_PERSON_ID)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _activity(conn, person_id, source_id, day, *, type, duration_s=1800.0, source="garmin",
              vinculo_manual=None, start_local=None):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, type, duration_s, source, vinculo_manual, "
        "start_local) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, type, duration_s, source, vinculo_manual, start_local),
    )


def _sleep(conn, person_id, day, start_local, end_local):
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, start_local, end_local),
    )


def _shift(conn, person_id, day, kind):
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


# ---- ruling 2: validation ------------------------------------------------------------------------

class ValidacionActividadTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_tipos_vacios_o_no_lista(self):
        self.assertEqual(practicas.crear(self.conn, 1, "T", "actividad", {"tipos": []}), "tipos_invalidos")
        self.assertEqual(practicas.crear(self.conn, 1, "T", "actividad", {"tipos": "running"}), "tipos_invalidos")
        self.assertEqual(practicas.crear(self.conn, 1, "T", "actividad", {"tipos": [1]}), "tipos_invalidos")
        self.assertEqual(practicas.crear(self.conn, 1, "T", "actividad", {}), "tipos_invalidos")

    def test_min_minutos_invalido(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "actividad", {"tipos": ["running"], "min_minutos": -1}),
            "min_minutos_invalido")
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "actividad", {"tipos": ["running"], "min_minutos": 1.5}),
            "min_minutos_invalido")

    def test_campo_extra_es_regla_invalida(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "actividad", {"tipos": ["running"], "zz": 1}),
            "regla_invalida")

    def test_regla_no_dict(self):
        self.assertEqual(practicas.crear(self.conn, 1, "T", "actividad", "no-un-dict"), "regla_invalida")

    def test_valid_shape_creates(self):
        pid = practicas.crear(self.conn, 1, "Correr", "actividad", {"tipos": ["running"], "min_minutos": 20})
        self.assertIsInstance(pid, int)


class ValidacionMetricaUmbralTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_metric_key_desconocida(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "metrica_umbral", {"metric_key": "zz_no_existe", "op": ">=", "valor": 1}),
            "metric_key_desconocida")

    def test_op_invalido(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "metrica_umbral", {"metric_key": "steps", "op": "==", "valor": 1}),
            "op_invalido")

    def test_valor_invalido(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "metrica_umbral", {"metric_key": "steps", "op": ">=", "valor": "mucho"}),
            "valor_invalido")

    def test_valid_shape_creates(self):
        pid = practicas.crear(self.conn, 1, "Pasos", "metrica_umbral",
                               {"metric_key": "steps", "op": ">=", "valor": 8000})
        self.assertIsInstance(pid, int)


class ValidacionSuenoHorarioTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_campo_invalido(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "sueno_horario", {"campo": "medio", "op": "antes", "hora": "23:00"}),
            "campo_invalido")

    def test_op_invalido(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "sueno_horario", {"campo": "inicio", "op": "ya", "hora": "23:00"}),
            "op_invalido")

    def test_hora_invalida(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "sueno_horario", {"campo": "inicio", "op": "antes", "hora": "25:99"}),
            "hora_invalida")
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "sueno_horario", {"campo": "inicio", "op": "antes", "hora": "11pm"}),
            "hora_invalida")

    def test_valid_shape_creates(self):
        pid = practicas.crear(self.conn, 1, "Acostarse", "sueno_horario",
                               {"campo": "inicio", "op": "antes", "hora": "23:00"})
        self.assertIsInstance(pid, int)


class ValidacionGeneralTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_fuente_desconocida(self):
        self.assertEqual(practicas.crear(self.conn, 1, "T", "zz_fuente", {}), "fuente_desconocida")

    def test_origen_invalido(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "actividad", {"tipos": ["running"]}, origen="zz"),
            "origen_invalido")

    def test_titulo_invalido(self):
        self.assertEqual(practicas.crear(self.conn, 1, "   ", "actividad", {"tipos": ["running"]}),
                          "titulo_invalido")

    def test_meta_dias_semana_invalida(self):
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "actividad", {"tipos": ["running"]}, meta_dias_semana=0),
            "meta_dias_semana_invalida")
        self.assertEqual(
            practicas.crear(self.conn, 1, "T", "actividad", {"tipos": ["running"]}, meta_dias_semana=8),
            "meta_dias_semana_invalida")


# ---- person scope and archiving ------------------------------------------------------------------

class PersonScopeTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_listar_is_scoped_by_person(self):
        practicas.crear(self.conn, 1, "De 1", "actividad", {"tipos": ["running"]})
        practicas.crear(self.conn, 2, "De 2", "actividad", {"tipos": ["running"]})
        titulos_1 = {p["titulo"] for p in practicas.listar(self.conn, 1)}
        titulos_2 = {p["titulo"] for p in practicas.listar(self.conn, 2)}
        self.assertEqual(titulos_1, {"De 1"})
        self.assertEqual(titulos_2, {"De 2"})

    def test_archivar_refuses_another_persons_row(self):
        pid = practicas.crear(self.conn, 2, "De 2", "actividad", {"tipos": ["running"]})
        self.assertFalse(practicas.archivar(self.conn, 1, pid))
        self.assertEqual(len(practicas.listar(self.conn, 2)), 1)


class ArchivarTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.pid = practicas.crear(self.conn, 1, "Correr", "actividad", {"tipos": ["running"]})

    def test_archivar_hides_from_default_listing_but_never_deletes(self):
        self.assertTrue(practicas.archivar(self.conn, 1, self.pid))
        self.assertEqual(practicas.listar(self.conn, 1), [])
        con_archivadas = practicas.listar(self.conn, 1, incluir_archivadas=True)
        self.assertEqual(len(con_archivadas), 1)
        self.assertIsNotNone(con_archivadas[0]["archivada_en"])

    def test_archivar_twice_is_a_no_op_the_second_time(self):
        self.assertTrue(practicas.archivar(self.conn, 1, self.pid))
        self.assertFalse(practicas.archivar(self.conn, 1, self.pid))


# ---- ruling 3: three states a day ----------------------------------------------------------------

class EstadosActividadTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.dias = [(date(2026, 11, 1) + timedelta(i)).isoformat() for i in range(5)]
        d = self.dias
        db.set_metric(self.conn, d[0], "steps", 5000, "garmin", 1)  # has data, no running -> no_hecho
        _activity(self.conn, 1, "a1", d[1], type="running")  # hecho
        # d[2]: nothing at all -> sin_dato (a commute day not yet imported)
        _activity(self.conn, 1, "a3", d[3], type="walking")  # wrong type -> no_hecho
        db.set_metric(self.conn, d[4], "steps", 1000, "garmin", 1)  # no_hecho
        self.conn.commit()

    def test_three_states(self):
        regla = {"tipos": ["running"]}
        estados = practicas.dias_estado(self.conn, 1, "actividad", regla,
                                         date.fromisoformat(self.dias[0]), date.fromisoformat(self.dias[4]))
        self.assertEqual(estados, {
            self.dias[0]: "no_hecho", self.dias[1]: "hecho", self.dias[2]: "sin_dato",
            self.dias[3]: "no_hecho", self.dias[4]: "no_hecho",
        })

    def test_min_minutos_disqualifies_a_short_session(self):
        regla = {"tipos": ["running"], "min_minutos": 60}
        estados = practicas.dias_estado(self.conn, 1, "actividad", regla,
                                         date.fromisoformat(self.dias[1]), date.fromisoformat(self.dias[1]))
        self.assertEqual(estados[self.dias[1]], "no_hecho")  # the 1800s (30min) session is < 60min

    def test_linked_manual_half_is_dropped(self):
        """BODY-ENTRENO-VINCULO-1 ruling 3's own drop (history.py:135-140): a manual row linked to
        a watch row of a DIFFERENT type never turns a day the watch alone would not qualify into
        `hecho`."""
        day = (date(2026, 11, 20)).isoformat()
        _activity(self.conn, 1, "watch-1", day, type="walking")
        _activity(self.conn, 1, "manual-1", day, type="running", source="manual")
        self.conn.execute("UPDATE activity SET vinculo_manual = 'manual-1' WHERE source_id = 'watch-1'")
        self.conn.commit()
        regla = {"tipos": ["running"]}
        estados = practicas.dias_estado(self.conn, 1, "actividad", regla, date.fromisoformat(day),
                                         date.fromisoformat(day))
        self.assertEqual(estados[day], "no_hecho")  # the manual 'running' half is dropped


class EstadosMetricaUmbralTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.dias = [(date(2026, 11, 1) + timedelta(i)).isoformat() for i in range(3)]
        db.set_metric(self.conn, self.dias[0], "steps", 9000, "garmin", 1)  # hecho
        db.set_metric(self.conn, self.dias[1], "steps", 5000, "garmin", 1)  # no_hecho
        # self.dias[2]: no row -> sin_dato
        self.conn.commit()

    def test_three_states(self):
        regla = {"metric_key": "steps", "op": ">=", "valor": 8000}
        estados = practicas.dias_estado(self.conn, 1, "metrica_umbral", regla,
                                         date.fromisoformat(self.dias[0]), date.fromisoformat(self.dias[2]))
        self.assertEqual(estados, {self.dias[0]: "hecho", self.dias[1]: "no_hecho", self.dias[2]: "sin_dato"})


class EstadosSuenoHorarioTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.dias = [(date(2026, 11, 1) + timedelta(i)).isoformat() for i in range(3)]
        _sleep(self.conn, 1, self.dias[0], f"{self.dias[0]}T22:30:00", f"{self.dias[0]}T06:00:00")  # hecho
        _sleep(self.conn, 1, self.dias[1], f"{self.dias[1]}T23:45:00", f"{self.dias[1]}T06:00:00")  # no_hecho
        # self.dias[2]: no row -> sin_dato
        self.conn.commit()

    def test_three_states(self):
        regla = {"campo": "inicio", "op": "antes", "hora": "23:00"}
        estados = practicas.dias_estado(self.conn, 1, "sueno_horario", regla,
                                         date.fromisoformat(self.dias[0]), date.fromisoformat(self.dias[2]))
        self.assertEqual(estados, {self.dias[0]: "hecho", self.dias[1]: "no_hecho", self.dias[2]: "sin_dato"})


# ---- ruling 4: day-level racha, bridged vs broken ------------------------------------------------

class RachaDiaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.pid = practicas.crear(self.conn, 1, "Correr", "actividad", {"tipos": ["running"]})

    def _estado(self, desde, hasta):
        return practicas.estado(self.conn, 1, self.pid, desde=desde, hasta=hasta)

    def test_a_two_day_gap_is_bridged_into_one_racha(self):
        base = date(2026, 11, 1)
        for offset in (0, 1, 4, 5):  # hecho on day 0,1 ; gap on 2,3 (sin_dato) ; hecho on 4,5
            _activity(self.conn, 1, f"a{offset}", (base + timedelta(offset)).isoformat(), type="running")
        self.conn.commit()
        out = self._estado(base, base + timedelta(5))
        self.assertEqual(out["racha_mas_larga"]["dias"], 6)
        self.assertEqual(out["racha_mas_larga"]["dias_puente"], 2)
        self.assertEqual(out["n_rachas"], 1)

    def test_a_three_day_gap_breaks_the_racha(self):
        base = date(2026, 11, 1)
        for offset in (0, 1, 5, 6):  # hecho on 0,1 ; gap on 2,3,4 (sin_dato, 3 days) ; hecho on 5,6
            _activity(self.conn, 1, f"a{offset}", (base + timedelta(offset)).isoformat(), type="running")
        self.conn.commit()
        out = self._estado(base, base + timedelta(6))
        self.assertEqual(out["n_rachas"], 2)
        self.assertEqual(out["racha_mas_larga"]["dias"], 2)
        self.assertEqual(out["racha_mas_larga"]["dias_puente"], 0)

    def test_a_no_hecho_day_breaks_the_racha_even_with_no_gap(self):
        base = date(2026, 11, 1)
        _activity(self.conn, 1, "a0", base.isoformat(), type="running")
        _activity(self.conn, 1, "a1", (base + timedelta(1)).isoformat(), type="walking")  # no_hecho
        _activity(self.conn, 1, "a2", (base + timedelta(2)).isoformat(), type="running")
        self.conn.commit()
        out = self._estado(base, base + timedelta(2))
        self.assertEqual(out["n_rachas"], 2)


# ---- ruling 4: semana_sin_dato and a bridged temporada -------------------------------------------

class SemanaYTemporadaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.pid = practicas.crear(self.conn, 1, "Correr", "actividad", {"tipos": ["running"]},
                                    meta_dias_semana=3)

    def test_a_week_under_the_data_floor_is_semana_sin_dato(self):
        from body.fechas import lunes_de
        lunes = lunes_de(date(2026, 11, 2))
        # Only 3 days of the week carry any data at all (< MODELO['MIN_DIAS_CON_DATO'] == 4).
        _activity(self.conn, 1, "a0", lunes.isoformat(), type="running")
        _activity(self.conn, 1, "a1", (lunes + timedelta(1)).isoformat(), type="running")
        _activity(self.conn, 1, "a2", (lunes + timedelta(2)).isoformat(), type="running")
        self.conn.commit()
        out = practicas.estado(self.conn, 1, self.pid, desde=lunes, hasta=lunes + timedelta(6))
        semana = out["semanas"][0]
        self.assertTrue(semana["semana_sin_dato"])
        self.assertIsNone(semana["cumple"])

    def test_temporada_bridges_one_semana_sin_dato(self):
        from body.fechas import lunes_de
        lunes0 = lunes_de(date(2026, 11, 2))
        # Weeks 0, 2, 3: 4 running days each -- hecho=4 >= meta(3), con_dato=4 >= MIN_DIAS_CON_DATO
        # (4) -> cumple. Week 1: no data at all -> semana_sin_dato, bridged.
        for semana_idx in (0, 2, 3):
            lunes = lunes0 + timedelta(weeks=semana_idx)
            for offset in range(4):
                _activity(self.conn, 1, f"a{semana_idx}-{offset}", (lunes + timedelta(offset)).isoformat(),
                          type="running")
        self.conn.commit()
        out = practicas.estado(self.conn, 1, self.pid, desde=lunes0, hasta=lunes0 + timedelta(weeks=4) - timedelta(1))
        self.assertEqual(len(out["temporadas"]), 1)
        t = out["temporadas"][0]
        self.assertEqual(t["semanas"], 4)
        self.assertEqual(t["desde"], lunes0.isoformat())


# ---- ruling 5: context vs baseline, a None key ---------------------------------------------------

class ContextoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.pid = practicas.crear(self.conn, 1, "Correr", "actividad", {"tipos": ["running"]},
                                    meta_dias_semana=3)

    def test_context_has_a_none_key_when_there_is_no_data_for_it(self):
        lunes0_base = date(2026, 11, 2)
        from body.fechas import lunes_de
        lunes0 = lunes_de(lunes0_base)
        for semana_idx in range(3):
            lunes = lunes0 + timedelta(weeks=semana_idx)
            for offset in range(4):  # hecho=4 >= meta(3), con_dato=4 >= MIN_DIAS_CON_DATO -> cumple
                dia = (lunes + timedelta(offset)).isoformat()
                _activity(self.conn, 1, f"a{semana_idx}-{offset}", dia, type="running")
                db.set_metric(self.conn, dia, "sleep_minutes", 420, "garmin", 1)
        self.conn.commit()
        out = practicas.estado(self.conn, 1, self.pid, desde=lunes0, hasta=lunes0 + timedelta(weeks=3) - timedelta(1))
        self.assertEqual(len(out["temporadas"]), 1)
        contexto = out["temporadas"][0]["contexto"]
        self.assertIsNotNone(contexto["sleep_minutes"])
        self.assertIsNone(contexto["readiness"])  # never seeded -- an honest None, never 0
        self.assertIsNotNone(out["baseline"]["sleep_minutes"])
        self.assertEqual(contexto["turnos"], {})  # no shift rows seeded -- an honest empty dict


# ---- ruling 1 (BODY-HISTORIA-VIDA-2-R2): resumen() byte-identical to estado() -------------------

class ResumenTest(unittest.TestCase):
    """practicas.resumen()'s racha_actual/ultima_semana must be byte-identical to estado()'s own
    racha_actual/semanas[-1] for the same practica -- on a home with several practices of every
    fuente, including one with no data at all."""

    def setUp(self):
        self.conn = _conn(self)
        base = date(2026, 10, 1)
        self.dias = [(base + timedelta(i)).isoformat() for i in range(21)]
        d = self.dias
        for i, dia in enumerate(d):
            if i % 7 < 5:
                _activity(self.conn, 1, f"run-{i}", dia, type="running")
            if i % 3 == 0:
                db.set_metric(self.conn, dia, "steps", 9000, "garmin", 1)
            else:
                db.set_metric(self.conn, dia, "steps", 5000, "garmin", 1)
            if i % 5 != 0:
                _sleep(self.conn, 1, dia, f"{dia}T22:30:00", f"{dia}T06:00:00")
        self.pid_actividad = practicas.crear(self.conn, 1, "Correr", "actividad", {"tipos": ["running"]})
        self.pid_metrica = practicas.crear(self.conn, 1, "Pasos", "metrica_umbral",
                                            {"metric_key": "steps", "op": ">=", "valor": 8000})
        self.pid_sueno = practicas.crear(self.conn, 1, "Acostarse", "sueno_horario",
                                          {"campo": "inicio", "op": "antes", "hora": "23:00"})
        self.pid_sin_datos = practicas.crear(self.conn, 1, "HRV", "metrica_umbral",
                                             {"metric_key": "hrv_last_night", "op": ">=", "valor": 50})
        self.conn.commit()

    def test_byte_identical_to_estado_for_every_fuente(self):
        resumen_por_id = {p["id"]: p for p in practicas.resumen(self.conn, 1)}
        for pid in (self.pid_actividad, self.pid_metrica, self.pid_sueno, self.pid_sin_datos):
            e = practicas.estado(self.conn, 1, pid)
            r = resumen_por_id[pid]
            self.assertEqual(r["racha_actual"], e["racha_actual"], f"racha_actual differs for {pid}")
            esperado_semana = e["semanas"][-1] if e["semanas"] else None
            self.assertEqual(r["ultima_semana"], esperado_semana, f"ultima_semana differs for {pid}")

    def test_no_data_practica_gives_none_for_both_fields(self):
        resumen_por_id = {p["id"]: p for p in practicas.resumen(self.conn, 1)}
        self.assertIsNone(resumen_por_id[self.pid_sin_datos]["racha_actual"])
        self.assertIsNone(resumen_por_id[self.pid_sin_datos]["ultima_semana"])

    def test_empty_for_a_person_with_no_active_practicas(self):
        self.assertEqual(practicas.resumen(self.conn, 2), [])


# ---- ruling 6: suggestions, 7 vs 8 weeks, omitted when already covered --------------------------

class SugerenciasTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def _seed_weeks(self, n: int, kind: str = "cycling"):
        from body.fechas import lunes_de
        lunes0 = lunes_de(date(2026, 1, 5))
        for i in range(n):
            lunes = lunes0 + timedelta(weeks=i)
            _activity(self.conn, 1, f"a{i}", lunes.isoformat(), type=kind)
        self.conn.commit()

    def test_seven_weeks_is_not_suggested(self):
        self._seed_weeks(7)
        claves = {s["clave"] for s in practicas.sugerencias(self.conn, 1)}
        self.assertNotIn("cycling", claves)

    def test_eight_weeks_is_suggested(self):
        self._seed_weeks(8)
        sugs = {s["clave"]: s for s in practicas.sugerencias(self.conn, 1)}
        self.assertIn("cycling", sugs)
        self.assertEqual(sugs["cycling"]["semanas"], 8)
        self.assertEqual(sugs["cycling"]["fuente"], "actividad")

    def test_omitted_when_already_an_active_practica_of_the_same_rule(self):
        self._seed_weeks(8)
        practicas.crear(self.conn, 1, "Ciclismo", "actividad", {"tipos": ["cycling"]})
        claves = {s["clave"] for s in practicas.sugerencias(self.conn, 1)}
        self.assertNotIn("cycling", claves)


if __name__ == "__main__":
    unittest.main()
