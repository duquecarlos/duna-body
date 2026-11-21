"""body/registro_cobertura.py: BODY-REGISTRO-COBERTURA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1). The
CAPTURE side of a dated plan -- sleep, meals, protein, a weekly weight and waist, training -- read
against the plan's own date range (`cobertura`) or a single day (`pendientes`). Invented demo data
only, no real figures, no network.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import db, goals, horizonte, registro_cobertura

PERSON_A = 1
PERSON_B = 2


def _mkconn(case: unittest.TestCase, name: str = "registro_cobertura.db"):
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / name)
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


def _milestone(conn, person_id: int, start_day: str, deadline: str, titulo: str = "ZZ plan") -> int:
    gid = goals.propose(conn, person_id, titulo, start_day, kind="milestone", deadline=deadline)
    goals.activate(conn, gid)
    # horizonte.horizontes only recognises a milestone as a horizon once it carries at least one
    # plan row (horizonte.py's own _PLAN_ROW_HORIZONTES) -- a plain, unscoped rule is the cheapest.
    db.crear_regla_horizonte(conn, person_id, gid, "siempre", None, "ZZ regla")
    conn.commit()
    return gid


def _sueno(conn, person_id: int, day: str) -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, f"{day}T22:00:00", f"{day}T06:00:00"))


def _comida(conn, person_id: int, day: str) -> None:
    db.crear_comida(conn, person_id, day, [])


def _proteina(conn, person_id: int, day: str, amount: float = 20.0) -> None:
    db.registrar_ingesta(conn, person_id, day, "protein_g", amount, False)


def _peso(conn, person_id: int, day: str, value: float = 70.0) -> None:
    db.set_metric(conn, day, "body_weight_kg", value, "self", person_id)


def _cintura(conn, person_id: int, day: str, value: float = 80.0) -> None:
    db.set_metric(conn, day, "waist_cm", value, "self", person_id)


def _activity(conn, person_id: int, day: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', 'ZZ', 1800, 'manual')",
        (person_id, source_id, day, f"{day}T07:00:00"))


def _plan_hecho(conn, person_id: int, day: str) -> None:
    conn.execute(
        "INSERT INTO plan (person_id, day, title, status, ejercicios_json) VALUES (?, ?, 'ZZ', 'done', '[]')",
        (person_id, day))


# Three invented ISO weeks, Monday to Sunday.
START = "2026-01-05"       # Monday, week 1
DEADLINE = "2026-01-25"    # Sunday, week 3
WEEK1 = [f"2026-01-{d:02d}" for d in range(5, 12)]
WEEK2 = [f"2026-01-{d:02d}" for d in range(12, 19)]
WEEK3 = [f"2026-01-{d:02d}" for d in range(19, 26)]
ALL_DAYS = WEEK1 + WEEK2 + WEEK3


class CoberturaExactCountsTest(unittest.TestCase):
    """The spec's own invented fixture: meals on some days, protein on fewer, one weekly weight
    missing, waist never used, sleep missing one night, training in 2 of 3 weeks."""

    def setUp(self):
        self.conn = _mkconn(self)
        self.gid = _milestone(self.conn, PERSON_A, START, DEADLINE)

        # comidas: every day except 2026-01-06/07/08, 16/17, 24 (6 missing of 21).
        comida_missing = {"2026-01-06", "2026-01-07", "2026-01-08", "2026-01-16", "2026-01-17", "2026-01-24"}
        for day in ALL_DAYS:
            if day not in comida_missing:
                _comida(self.conn, PERSON_A, day)

        # proteina: only 8 of 21 days (fewer than comidas) -- 13 missing, more than FALTAN_LIMITE.
        proteina_covered = {"2026-01-05", "2026-01-09", "2026-01-10", "2026-01-11",
                             "2026-01-12", "2026-01-13", "2026-01-19", "2026-01-20"}
        for day in proteina_covered:
            _proteina(self.conn, PERSON_A, day)

        # sueno: every night except 2026-01-10.
        for day in ALL_DAYS:
            if day != "2026-01-10":
                _sueno(self.conn, PERSON_A, day)

        # peso: week 1 and week 3, missing week 2.
        _peso(self.conn, PERSON_A, "2026-01-05")
        _peso(self.conn, PERSON_A, "2026-01-25")

        # cintura: never used at all.

        # entreno: week 1 and week 2, missing week 3.
        _activity(self.conn, PERSON_A, "2026-01-07", "act-1")
        _activity(self.conn, PERSON_A, "2026-01-15", "act-2")

        self.conn.commit()
        self.out = registro_cobertura.cobertura(self.conn, PERSON_A, self.gid, hasta=date(2026, 1, 25))

    def test_desde_hasta(self):
        self.assertEqual(self.out["desde"], START)
        self.assertEqual(self.out["hasta"], DEADLINE)

    def test_comidas(self):
        s = self.out["streams"]["comidas"]
        self.assertEqual(s["cadencia"], "daily")
        self.assertFalse(s["sin_uso"])
        self.assertEqual((s["cubiertos"], s["esperados"]), (15, 21))
        self.assertEqual(s["faltan"], ["2026-01-24", "2026-01-17", "2026-01-16", "2026-01-08",
                                        "2026-01-07", "2026-01-06"])

    def test_proteina_caps_faltan_at_the_named_limit(self):
        s = self.out["streams"]["proteina"]
        self.assertFalse(s["sin_uso"])
        self.assertEqual((s["cubiertos"], s["esperados"]), (8, 21))
        self.assertEqual(len(s["faltan"]), registro_cobertura.FALTAN_LIMITE)
        self.assertEqual(s["faltan"], ["2026-01-25", "2026-01-24", "2026-01-23", "2026-01-22",
                                        "2026-01-21", "2026-01-18", "2026-01-17", "2026-01-16",
                                        "2026-01-15", "2026-01-14", "2026-01-08", "2026-01-07"])

    def test_sueno(self):
        s = self.out["streams"]["sueno"]
        self.assertEqual(s["cadencia"], "nightly")
        self.assertEqual((s["cubiertos"], s["esperados"]), (20, 21))
        self.assertEqual(s["faltan"], ["2026-01-10"])

    def test_peso_weekly(self):
        s = self.out["streams"]["peso"]
        self.assertEqual(s["cadencia"], "weekly")
        self.assertEqual((s["cubiertos"], s["esperados"]), (2, 3))
        self.assertEqual(s["faltan"], ["2026-01-12"])  # week 2's own Monday

    def test_cintura_never_used_is_sin_uso_not_a_run_of_misses(self):
        s = self.out["streams"]["cintura"]
        self.assertTrue(s["sin_uso"])
        self.assertNotIn("faltan", s)
        self.assertNotIn("cubiertos", s)

    def test_entreno_counts_activity_rows(self):
        s = self.out["streams"]["entreno"]
        self.assertEqual(s["cadencia"], "weekly")
        self.assertEqual((s["cubiertos"], s["esperados"]), (2, 3))
        self.assertEqual(s["faltan"], ["2026-01-19"])  # week 3's own Monday


class EntrenoCountsPlanSessionsDoneTest(unittest.TestCase):
    """Ruling 1: `entreno` is covered by an `activity` row OR a plan session marked done
    (horizonte._planes_hechos_fuerza) -- a week with only the latter still counts."""

    def test_a_done_plan_session_alone_covers_its_week(self):
        conn = _mkconn(self)
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        _plan_hecho(conn, PERSON_A, "2026-01-14")  # week 2, no activity row at all
        conn.commit()
        out = registro_cobertura.cobertura(conn, PERSON_A, gid, hasta=date(2026, 1, 25))
        s = out["streams"]["entreno"]
        self.assertFalse(s["sin_uso"])
        self.assertEqual(s["cubiertos"], 1)
        self.assertIn("2026-01-05", s["faltan"])
        self.assertIn("2026-01-19", s["faltan"])
        self.assertNotIn("2026-01-12", s["faltan"])  # week 2's Monday, covered by the done session

    def test_sin_uso_requires_neither_activity_nor_a_done_plan_session_ever(self):
        conn = _mkconn(self)
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        conn.commit()
        out = registro_cobertura.cobertura(conn, PERSON_A, gid, hasta=date(2026, 1, 25))
        self.assertTrue(out["streams"]["entreno"]["sin_uso"])


class WeeklyStreamAnyWeekdayTest(unittest.TestCase):
    """Ruling 1: "no weekday is hardcoded for a weekly stream -- a week is covered by any reading
    inside it", checked once for each weekday a reading could land on."""

    def test_a_reading_on_any_weekday_covers_that_whole_week(self):
        for weekday_offset in range(7):
            day = WEEK1[weekday_offset]
            with self.subTest(day=day):
                conn = _mkconn(self, name=f"weekday-{weekday_offset}.db")
                gid = _milestone(conn, PERSON_A, START, DEADLINE)
                _peso(conn, PERSON_A, day)
                conn.commit()
                out = registro_cobertura.cobertura(conn, PERSON_A, gid, hasta=date(2026, 1, 11))
                s = out["streams"]["peso"]
                self.assertEqual((s["cubiertos"], s["esperados"]), (1, 1))
                self.assertEqual(s["faltan"], [])


class PlanNotYetStartedOrEndedTest(unittest.TestCase):
    def test_hasta_before_the_plans_own_start_is_an_empty_window(self):
        conn = _mkconn(self)
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        # Each stream used ONCE, inside the plan's own range but after `hasta` below -- so none of
        # them is sin_uso, and the empty-window branch (never "never used") is what is exercised.
        _comida(conn, PERSON_A, "2026-01-10")
        _proteina(conn, PERSON_A, "2026-01-10")
        _sueno(conn, PERSON_A, "2026-01-10")
        _peso(conn, PERSON_A, "2026-01-10")
        _cintura(conn, PERSON_A, "2026-01-10")
        _activity(conn, PERSON_A, "2026-01-10", "act-1")
        conn.commit()
        out = registro_cobertura.cobertura(conn, PERSON_A, gid, hasta=date(2026, 1, 1))
        for stream in registro_cobertura.STREAMS:
            s = out["streams"][stream]
            self.assertFalse(s["sin_uso"], f"{stream} was used once -- should not read as sin_uso")
            self.assertEqual((s["cubiertos"], s["esperados"]), (0, 0))
            self.assertEqual(s["faltan"], [])

    def test_hasta_after_the_plans_own_deadline_stops_at_the_deadline(self):
        conn = _mkconn(self)
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        _comida(conn, PERSON_A, "2026-01-05")
        conn.commit()
        out = registro_cobertura.cobertura(conn, PERSON_A, gid, hasta=date(2026, 6, 1))
        self.assertEqual(out["hasta"], DEADLINE)
        self.assertEqual(out["streams"]["comidas"]["esperados"], 21)


class TwoActivePlansTest(unittest.TestCase):
    def test_pendientes_lists_both_active_plans_on_the_same_day(self):
        conn = _mkconn(self)
        gid1 = _milestone(conn, PERSON_A, START, DEADLINE, "ZZ plan 1")
        gid2 = _milestone(conn, PERSON_A, "2026-01-01", "2026-02-01", "ZZ plan 2")
        conn.commit()
        dia = date(2026, 1, 10)
        out = registro_cobertura.pendientes(conn, PERSON_A, dia)
        self.assertEqual(set(out), {gid1, gid2})
        self.assertEqual(out[gid1], out[gid2])


class PendientesTest(unittest.TestCase):
    def test_missing_meal_today_and_no_weight_this_week_are_both_reported(self):
        conn = _mkconn(self)
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        # comidas/peso HAVE been used before (never sin_uso) -- just not today / not this week.
        _comida(conn, PERSON_A, "2026-01-05")       # a different day, not 'dia' below
        _peso(conn, PERSON_A, "2025-12-01")         # a different ISO week entirely
        # Everything else covered on this day/week so only comidas/peso show up in faltan.
        _proteina(conn, PERSON_A, "2026-01-07")
        _sueno(conn, PERSON_A, "2026-01-07")
        _cintura(conn, PERSON_A, "2026-01-06")
        _activity(conn, PERSON_A, "2026-01-05", "act-1")
        conn.commit()
        out = registro_cobertura.pendientes(conn, PERSON_A, date(2026, 1, 7))
        self.assertEqual(out[gid]["faltan"], ["comidas", "peso"])
        self.assertEqual(out[gid]["sin_uso"], [])

    def test_a_stream_never_used_is_sin_uso_not_faltan(self):
        conn = _mkconn(self)
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        _comida(conn, PERSON_A, "2026-01-07")
        _proteina(conn, PERSON_A, "2026-01-07")
        _sueno(conn, PERSON_A, "2026-01-07")
        _peso(conn, PERSON_A, "2026-01-05")
        _activity(conn, PERSON_A, "2026-01-05", "act-1")
        conn.commit()
        out = registro_cobertura.pendientes(conn, PERSON_A, date(2026, 1, 7))
        self.assertEqual(out[gid]["faltan"], [])
        self.assertEqual(out[gid]["sin_uso"], ["cintura"])

    def test_no_active_plan_on_dia_is_an_empty_dict(self):
        conn = _mkconn(self)
        _milestone(conn, PERSON_A, "2026-02-01", "2026-03-01")  # not active on 2026-01-07
        conn.commit()
        out = registro_cobertura.pendientes(conn, PERSON_A, date(2026, 1, 7))
        self.assertEqual(out, {})

    def test_sueno_checks_the_night_ending_on_dia_not_the_whole_week(self):
        conn = _mkconn(self)
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        _sueno(conn, PERSON_A, "2026-01-06")  # a different night in the same ISO week
        _comida(conn, PERSON_A, "2026-01-07")
        _proteina(conn, PERSON_A, "2026-01-07")
        _peso(conn, PERSON_A, "2026-01-05")
        _cintura(conn, PERSON_A, "2026-01-05")
        _activity(conn, PERSON_A, "2026-01-05", "act-1")
        conn.commit()
        out = registro_cobertura.pendientes(conn, PERSON_A, date(2026, 1, 7))
        self.assertEqual(out[gid]["faltan"], ["sueno"])


class OtherPersonNeverCountedTest(unittest.TestCase):
    def test_cobertura_never_counts_or_shows_another_persons_rows(self):
        conn = _mkconn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        conn.commit()
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        # B logs everything, every day -- none of it is A's.
        for day in ALL_DAYS:
            _comida(conn, PERSON_B, day)
            _proteina(conn, PERSON_B, day)
            _sueno(conn, PERSON_B, day)
            _peso(conn, PERSON_B, day)
            _cintura(conn, PERSON_B, day)
            _activity(conn, PERSON_B, day, f"b-{day}")
        conn.commit()
        out = registro_cobertura.cobertura(conn, PERSON_A, gid, hasta=date(2026, 1, 25))
        for stream in registro_cobertura.STREAMS:
            s = out["streams"][stream]
            self.assertTrue(s["sin_uso"], f"{stream} should be sin_uso for A -- B's rows must not count")

    def test_pendientes_never_counted_across_people(self):
        conn = _mkconn(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        conn.commit()
        gid = _milestone(conn, PERSON_A, START, DEADLINE)
        # A used these streams before (never sin_uso) but not on 'dia' itself.
        _comida(conn, PERSON_A, "2026-01-01")
        _proteina(conn, PERSON_A, "2026-01-01")
        _sueno(conn, PERSON_A, "2026-01-01")
        _peso(conn, PERSON_A, "2026-01-05")
        _cintura(conn, PERSON_A, "2026-01-05")
        _activity(conn, PERSON_A, "2026-01-05", "act-a")
        # B logs exactly on 'dia' -- must never read as covering A's own missing streams.
        _comida(conn, PERSON_B, "2026-01-07")
        _proteina(conn, PERSON_B, "2026-01-07")
        _sueno(conn, PERSON_B, "2026-01-07")
        conn.commit()
        out = registro_cobertura.pendientes(conn, PERSON_A, date(2026, 1, 7))
        self.assertEqual(sorted(out[gid]["faltan"]), ["comidas", "proteina", "sueno"])
        self.assertEqual(out[gid]["sin_uso"], [])


class ModeloCensusTest(unittest.TestCase):
    def test_modelo_is_versioned(self):
        self.assertEqual(registro_cobertura.MODELO["version"], registro_cobertura.VERSION)
        self.assertEqual(registro_cobertura.VERSION, "registro-cobertura-v1")

    def test_every_stream_is_named(self):
        esperado = {"sueno", "comidas", "proteina", "peso", "cintura", "entreno"}
        self.assertEqual(set(registro_cobertura.MODELO["cadencia"]), esperado)
        self.assertEqual(set(registro_cobertura.MODELO["unidad"]), esperado)
        self.assertEqual(set(registro_cobertura.STREAMS), esperado)

    def test_cadencias(self):
        self.assertEqual(registro_cobertura.MODELO["cadencia"], {
            "sueno": "nightly", "comidas": "daily", "proteina": "daily",
            "peso": "weekly", "cintura": "weekly", "entreno": "weekly",
        })

    def test_faltan_limite_is_named(self):
        self.assertEqual(registro_cobertura.FALTAN_LIMITE, 12)


if __name__ == "__main__":
    unittest.main()
