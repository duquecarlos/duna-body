"""The SCREEN side of BODY-REGISTRO-COBERTURA-1's own count (ORCH-CARLOS-DELEGATES-TIER-1-1,
BODY-REGISTRO-COBERTURA-2): the "Tu registro" / "Your log" block on a dated plan's goal page
(body/rutas/metas.py's `_registro_ctx`, body/templates/goal.html). `body/registro_cobertura.py`
is called here, never edited -- invented data only, no real figures, no network.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401,E402  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, web
from body.rutas import comun as web_comun

PERSON_A = 1

client = TestClient(web.app, follow_redirects=False)


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "registro_metas.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _milestone(conn, start_day: str, deadline: str, titulo: str = "ZZ plan fechado",
               con_regla: bool = True) -> int:
    gid = goals.propose(conn, PERSON_A, titulo, start_day, kind="milestone", deadline=deadline)
    goals.activate(conn, gid)
    if con_regla:
        # horizonte.es_horizonte's own requirement for a plan row -- deliberately OMITTED by
        # `con_regla=False` callers, to prove ruling 1's own "a dated plan is registro_cobertura's
        # definition (milestone + deadline), never narrowed to es_horizonte" reading.
        db.crear_regla_horizonte(conn, PERSON_A, gid, "siempre", None, "ZZ regla")
    conn.commit()
    return gid


def _sueno(conn, day: str) -> None:
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (PERSON_A, day, f"{day}T22:00:00", f"{day}T06:00:00"))


def _comida(conn, day: str) -> None:
    db.crear_comida(conn, PERSON_A, day, [])


def _proteina(conn, day: str, amount: float = 20.0) -> None:
    db.registrar_ingesta(conn, PERSON_A, day, "protein_g", amount, False)


def _peso(conn, day: str, value: float = 70.0) -> None:
    db.set_metric(conn, day, "body_weight_kg", value, "self", PERSON_A)


def _activity(conn, day: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', 'ZZ', 1800, 'manual')",
        (PERSON_A, source_id, day, f"{day}T07:00:00"))


# Three invented ISO weeks, Monday to Sunday -- the same fixture shape BODY-REGISTRO-COBERTURA-1's
# own tests/test_registro_cobertura.py uses (never imported from there: that module is Tier 1).
START = "2026-01-05"
DEADLINE = "2026-01-25"
WEEK1 = [f"2026-01-{d:02d}" for d in range(5, 12)]
WEEK2 = [f"2026-01-{d:02d}" for d in range(12, 19)]
WEEK3 = [f"2026-01-{d:02d}" for d in range(19, 26)]
ALL_DAYS = WEEK1 + WEEK2 + WEEK3
HOY = date(2026, 1, 25)


def _seed_six_stream_fixture(conn, gid: int) -> None:
    """comidas: 6 of 21 days missing (over the 3-named-then-"y N mas" threshold). proteina: 13 of
    21 missing (over FALTAN_LIMITE=12 too). sueno: 1 of 21 missing. peso: 1 of 3 weeks missing.
    cintura: never used (sin_uso). entreno: 1 of 3 weeks missing."""
    comida_missing = {"2026-01-06", "2026-01-07", "2026-01-08", "2026-01-16", "2026-01-17", "2026-01-24"}
    for day in ALL_DAYS:
        if day not in comida_missing:
            _comida(conn, day)
    proteina_covered = {"2026-01-05", "2026-01-09", "2026-01-10", "2026-01-11",
                        "2026-01-12", "2026-01-13", "2026-01-19", "2026-01-20"}
    for day in proteina_covered:
        _proteina(conn, day)
    for day in ALL_DAYS:
        if day != "2026-01-10":
            _sueno(conn, day)
    _peso(conn, "2026-01-05")
    _peso(conn, "2026-01-25")
    # cintura: never used at all.
    _activity(conn, "2026-01-07", "act-1")
    _activity(conn, "2026-01-15", "act-2")
    conn.commit()


class BloqueAusenteTest(unittest.TestCase):
    """Ruling 1: absent for a goal that is not a dated plan -- a metric goal renders byte-identical
    to before this slice (no "Tu registro" card at all)."""

    def test_metric_goal_has_no_registro_block(self):
        conn = _reset_db(self)
        gid = goals.propose(conn, PERSON_A, "ZZ meta metrica", "2026-01-05", kind="metric",
                            metric_key="resting_hr", direction="lower", target_value=50.0,
                            baseline_value=60.0, deadline="2026-02-01")
        goals.activate(conn, gid)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=HOY):
            r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Tu registro", r.text)
        self.assertNotIn('class="card"><div class="q">Your log', r.text)


class BloquePresenteSinHorizonteTest(unittest.TestCase):
    """Ruling 1: a bare milestone with just a deadline (no plan row at all, so NOT
    horizonte.es_horizonte) is still `registro_cobertura`'s own "dated plan" -- the block shows for
    it too, never narrowed to a horizon with phases/rules."""

    def test_bare_milestone_without_plan_rows_shows_block(self):
        conn = _reset_db(self)
        gid = _milestone(conn, START, DEADLINE, con_regla=False)
        _sueno(conn, "2026-01-05")
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=HOY):
            r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tu registro", r.text)


class SeisFlujosTest(unittest.TestCase):
    """Ruling 2/3: the six-stream shape -- a stream in use shows its own row with a plain-words
    count; a never-used stream (cintura here) shows in NO row of its own, only the shared muted
    line."""

    def setUp(self):
        self.conn = _reset_db(self)
        self.gid = _milestone(self.conn, START, DEADLINE)
        _seed_six_stream_fixture(self.conn, self.gid)
        with patch.object(web_comun, "_today_local", return_value=HOY):
            self.r = client.get(f"/goals/{self.gid}")
        self.body = self.r.text

    def test_status_200(self):
        self.assertEqual(self.r.status_code, 200)

    def test_five_streams_in_use_each_get_their_own_row(self):
        for label in ("Sueño", "Comidas", "Proteína", "Peso", "Entreno"):
            self.assertIn(label, self.body)
        # exactly 5 rows (cintura excluded) -- counted by this block's own <li class="block">.
        bloque = self.body[self.body.index("Tu registro"):self.body.index("No cuenta lo que no registras")]
        self.assertEqual(bloque.count('<li class="block">'), 5)

    def test_sin_uso_stream_named_only_in_the_muted_line(self):
        self.assertIn("No cuenta lo que no registras: Cintura.", self.body)
        bloque = self.body[self.body.index("Tu registro"):self.body.index("No cuenta lo que no registras")]
        self.assertNotIn("Cintura", bloque)

    def test_comidas_count_sentence(self):
        self.assertIn("15 de 21 días", self.body)  # 21 - 6 missing = 15

    def test_missing_days_named_at_most_three_then_y_n_mas(self):
        # comidas: 6 missing (newest first: 24, 17, 16, 08, 07, 06) -> 3 named + "y 3 mas".
        self.assertIn("Falta: sáb 24, sáb 17, vie 16 y 3 más", self.body)

    def test_proteina_missing_over_faltan_limite_still_shows_only_three_then_y_n_mas(self):
        # proteina: 13 of 21 missing (more than registro_cobertura.FALTAN_LIMITE=12 itself) --
        # cobertura already truncates to 12, this screen further names only 3 then "y 9 mas".
        self.assertIn("8 de 21 días", self.body)
        self.assertIn("y 9 más", self.body)

    def test_sueno_single_missing_night_names_it_with_no_y_mas_suffix(self):
        self.assertIn("20 de 21 noches", self.body)
        self.assertIn("Falta: sáb 10", self.body)
        self.assertNotIn("Falta: sáb 10 y", self.body)

    def test_sueno_links_to_the_day_of_its_newest_missing_night(self):
        self.assertIn('<a class="taplink" href="/day/2026-01-10">Sueño</a>', self.body)

    def test_other_streams_never_link(self):
        for label in ("Comidas", "Proteína", "Peso", "Entreno"):
            self.assertNotIn(f'href="/day/{label}', self.body)
        self.assertNotIn('<a class="taplink" href="/day/2026-01-16">Comidas</a>', self.body)

    def test_entreno_missing_week_named_in_words(self):
        self.assertIn("2 de 3 semanas", self.body)
        self.assertIn("la semana del 19 ene", self.body)

    def test_range_line(self):
        self.assertIn("Desde 5 ene 2026 hasta hoy", self.body)


class StreamCompletoTest(unittest.TestCase):
    """Ruling 2: a complete stream reads "completo" in a muted note, never a celebration."""

    def test_peso_complete_every_week_reads_completo(self):
        conn = _reset_db(self)
        gid = _milestone(conn, START, DEADLINE)
        for day in ("2026-01-05", "2026-01-12", "2026-01-19"):
            _peso(conn, day)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=HOY):
            r = client.get(f"/goals/{gid}")
        body = r.text
        self.assertIn("3 de 3 semanas", body)
        self.assertIn("Completo", body)


class RangoTerminadoTest(unittest.TestCase):
    """Ruling 5: the muted range line says "hasta {hasta}" once the plan has already ended, never
    "hasta hoy"."""

    def test_ended_plan_shows_its_own_end_date(self):
        conn = _reset_db(self)
        gid = _milestone(conn, START, DEADLINE)
        conn.commit()
        with patch.object(web_comun, "_today_local", return_value=date(2026, 3, 1)):
            r = client.get(f"/goals/{gid}")
        self.assertIn("Desde 5 ene 2026 hasta 25 ene 2026", r.text)
        self.assertNotIn("hasta hoy", r.text)


class EnglishTest(unittest.TestCase):
    """Ruling 6: the same block, in English."""

    def test_english_strings(self):
        conn = _reset_db(self)
        gid = _milestone(conn, START, DEADLINE)
        _seed_six_stream_fixture(conn, gid)
        db.set_language(conn, "en", PERSON_A)
        with patch.object(web_comun, "_today_local", return_value=HOY):
            r = client.get(f"/goals/{gid}")
        body = r.text
        self.assertIn("Your log", body)
        self.assertIn("Not counted, since you don&#39;t log them: Waist.", body)
        self.assertIn("Missing: ", body)
        self.assertIn("and 3 more", body)
        self.assertIn("the week of 19 Jan", body)
        self.assertIn("From 5 Jan 2026 to today", body)


if __name__ == "__main__":
    unittest.main()
