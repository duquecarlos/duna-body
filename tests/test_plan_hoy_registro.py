"""BODY-HOY-PENDIENTES-REGISTRO-1 (ORCH-CARLOS-DELEGATES-TIER-1-1): what the person's own dated
plan still needs logged today and this ISO week, computed from `registro_cobertura.pendientes`.
BODY-HOY-LO-QUE-VENCE-1 moved this fact off the plan card's own trailing note and into Hoy's
"Por vencer" strip, one line per missing stream, each with its own link -- these tests now assert
each stream's own line there instead of the old combined sentence. Invented demo data only, built
through goals.propose/activate and db.py's own writers, the same fixture helpers
tests/test_plan_hoy.py and tests/test_registro_cobertura.py use.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, fechas, goals, registro_cobertura, web
from body.rutas import comun

client = TestClient(web.app, follow_redirects=False)

HOY = date(2026, 11, 12)  # a Thursday
START_DAY = date(2026, 10, 19).isoformat()  # 3 weeks before the current Monday
DEADLINE = date(2026, 12, 6).isoformat()


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "planhoyregistro.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    db.set_metric(conn, HOY.isoformat(), "sleep_minutes", 400, "garmin", 1)
    conn.commit()
    return conn


def _get(path: str = "/", hoy_dia: date = HOY):
    with mock.patch.object(comun, "_today_local", return_value=hoy_dia):
        return client.get(path)


def _horizonte(conn, person_id: int = 1, titulo: str = "ZZ Horizonte", start_day: str = START_DAY,
               deadline: str = DEADLINE) -> int:
    gid = goals.propose(conn, person_id, titulo, start_day, kind="milestone", deadline=deadline)
    goals.activate(conn, gid)
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


# Well before any fixture's own plan start (2026-10-19) or `HOY`'s own ISO week -- used to log a
# stream's PRIOR use for a stream that must land in `faltan` (used before, just not today/this
# week), never in `sin_uso` (never used at all).
_ANTES = "2026-01-05"

_ESCRITORES = {
    "sueno": _sueno, "comidas": _comida, "proteina": _proteina,
    "peso": _peso, "cintura": _cintura,
}


def _preparar_streams(conn, person_id: int, dia: date, *, faltan: set[str] = frozenset(),
                       sin_uso: set[str] = frozenset()) -> None:
    """Covers every one of `registro_cobertura.STREAMS` on `dia` (daily) / `dia`'s own ISO week
    (weekly), except: `faltan` streams, logged once on `_ANTES` instead (so they read as used
    before, not sin_uso) -- `pendientes`'s own "faltan" bucket; `sin_uso` streams, never logged at
    all -- `pendientes`'s own "sin_uso" bucket (ruling 4: never named on Hoy's line)."""
    d = dia.isoformat()
    lunes_iso = fechas.lunes_de(dia).isoformat()
    for stream in registro_cobertura.STREAMS:
        if stream in sin_uso:
            continue
        if stream == "entreno":
            if stream in faltan:
                _activity(conn, person_id, _ANTES, f"act-{person_id}-antes")
            else:
                _activity(conn, person_id, lunes_iso, f"act-{person_id}-{d}")
            continue
        escritor = _ESCRITORES[stream]
        cadencia = registro_cobertura.MODELO["cadencia"][stream]
        dia_cubierto = lunes_iso if cadencia == "weekly" else d
        escritor(conn, person_id, _ANTES if stream in faltan else dia_cubierto)


class AusenteSinPlanTest(unittest.TestCase):
    """Ruling 5: absent, byte-identical to before this slice, for a person with no active plan."""

    def test_no_plan_at_all_has_no_pendientes_line(self):
        _reset_db(self)
        page = _get().text
        self.assertNotIn("Tu plan esta semana", page)
        self.assertNotIn("Por vencer", page)
        self.assertNotIn("Falta registrar", page)


class AusenteNadaFaltaTest(unittest.TestCase):
    """Ruling 3/5: absent when every non-sleep stream is covered -- sueno missing alone changes
    nothing, since sueno's own cadence (BODY-HOY-LO-QUE-VENCE-1 ruling 2) is 'nightly', neither
    'daily' nor 'weekly', so vence.py never gives it a line either."""

    def test_only_sueno_missing_gives_no_line(self):
        conn = _reset_db(self)
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY, faltan={"sueno"})
        conn.commit()
        page = _get().text
        self.assertIn("Tu plan esta semana", page)
        self.assertNotIn("Por vencer", page)
        self.assertNotIn("Falta registrar", page)

    def test_nothing_missing_at_all_gives_no_line(self):
        conn = _reset_db(self)
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY)
        conn.commit()
        page = _get().text
        self.assertIn("Tu plan esta semana", page)
        self.assertNotIn("Por vencer", page)
        self.assertNotIn("Falta registrar", page)


class DailyOnlyTest(unittest.TestCase):
    """BODY-HOY-LO-QUE-VENCE-1 ruling 2/6: each missing daily stream is now its own line in the
    "Por vencer" strip, each its own link -- never one combined sentence."""

    def test_daily_only_lines_es(self):
        conn = _reset_db(self)
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY, faltan={"comidas", "proteina"})
        conn.commit()
        page = _get().text
        self.assertIn("Por vencer", page)
        self.assertIn('href="#asis-btn">Falta registrar hoy: comidas</a>', page)
        self.assertIn('href="#asis-btn">Falta registrar hoy: proteína</a>', page)

    def test_daily_only_lines_en(self):
        conn = _reset_db(self)
        conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY, faltan={"comidas", "proteina"})
        conn.commit()
        page = _get().text
        self.assertIn("Coming due", page)
        self.assertIn('href="#asis-btn">Still to log today: meals</a>', page)
        self.assertIn('href="#asis-btn">Still to log today: protein</a>', page)


class WeeklyOnlyTest(unittest.TestCase):
    def test_weekly_only_line_es(self):
        conn = _reset_db(self)
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY, faltan={"peso"})
        conn.commit()
        page = _get().text
        self.assertIn('href="#asis-btn">Falta registrar esta semana: peso</a>', page)

    def test_weekly_only_line_en(self):
        conn = _reset_db(self)
        conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY, faltan={"peso"})
        conn.commit()
        page = _get().text
        self.assertIn('href="#asis-btn">Still to log this week: weight</a>', page)


class BothPartsTest(unittest.TestCase):
    """A daily gap and a weekly gap together are still two separate lines, never joined into one
    sentence (BODY-HOY-LO-QUE-VENCE-1 ruling 1: one line per dated fact)."""

    def test_daily_and_weekly_both_present_as_separate_lines_es(self):
        conn = _reset_db(self)
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY, faltan={"comidas", "proteina", "peso"})
        conn.commit()
        page = _get().text
        self.assertIn('href="#asis-btn">Falta registrar hoy: comidas</a>', page)
        self.assertIn('href="#asis-btn">Falta registrar hoy: proteína</a>', page)
        self.assertIn('href="#asis-btn">Falta registrar esta semana: peso</a>', page)

    def test_daily_and_weekly_both_present_as_separate_lines_en(self):
        conn = _reset_db(self)
        conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY, faltan={"comidas", "proteina", "peso"})
        conn.commit()
        page = _get().text
        self.assertIn('href="#asis-btn">Still to log today: meals</a>', page)
        self.assertIn('href="#asis-btn">Still to log today: protein</a>', page)
        self.assertIn('href="#asis-btn">Still to log this week: weight</a>', page)


class SinUsoNuncaNombradoTest(unittest.TestCase):
    """Ruling 4: a never-used stream is never named on this line, even though it IS missing."""

    def test_sin_uso_stream_stays_unnamed(self):
        conn = _reset_db(self)
        _horizonte(conn)
        # cintura never used at all (sin_uso); comidas/proteina missing today (faltan).
        _preparar_streams(conn, 1, HOY, faltan={"comidas", "proteina"}, sin_uso={"cintura"})
        conn.commit()
        out = registro_cobertura.pendientes(conn, 1, HOY)
        self.assertIn("cintura", next(iter(out.values()))["sin_uso"])
        page = _get().text
        self.assertIn('href="#asis-btn">Falta registrar hoy: comidas</a>', page)
        self.assertNotIn("cintura", page)
        self.assertNotIn("waist", page)


class UnaLlamadaPorRenderTest(unittest.TestCase):
    """Ruling 1: `registro_cobertura.pendientes` is called exactly once per render, only reached
    when at least one plan is already active -- never twice (once per plan)."""

    def test_one_call_with_one_active_plan(self):
        conn = _reset_db(self)
        _horizonte(conn)
        _preparar_streams(conn, 1, HOY, faltan={"comidas"})
        conn.commit()
        with mock.patch.object(registro_cobertura, "pendientes",
                                wraps=registro_cobertura.pendientes) as spy:
            _get()
        self.assertEqual(spy.call_count, 1)

    def test_two_active_plans_still_one_call(self):
        conn = _reset_db(self)
        _horizonte(conn, titulo="ZZ Plan 1")
        _horizonte(conn, titulo="ZZ Plan 2", start_day="2026-10-26")
        _preparar_streams(conn, 1, HOY, faltan={"comidas"})
        conn.commit()
        with mock.patch.object(registro_cobertura, "pendientes",
                                wraps=registro_cobertura.pendientes) as spy:
            page = _get().text
        self.assertEqual(spy.call_count, 1)
        self.assertIn("comidas", page)

    def test_no_active_plan_never_calls_pendientes(self):
        _reset_db(self)
        with mock.patch.object(registro_cobertura, "pendientes",
                                wraps=registro_cobertura.pendientes) as spy:
            _get()
        self.assertEqual(spy.call_count, 0)


class PersonScopeTest(unittest.TestCase):
    def test_another_persons_missing_stream_never_shows(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        conn.commit()
        _horizonte(conn, person_id=2)
        # Person 2 has gaps; person 1 has no plan at all, so the line must never appear for 1.
        page = _get().text
        self.assertNotIn("Falta registrar", page)
        self.assertNotIn("Por vencer", page)


if __name__ == "__main__":
    unittest.main()
