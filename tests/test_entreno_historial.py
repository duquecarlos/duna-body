"""BODY-ENTRENO-ULTIMA-VEZ-1: `body.entreno_historial.ultima_vez`/`ultimas_veces`, the pure reads
behind "last time -> today". Invented demo data only."""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, entreno_historial, garmin_adapter


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _insert_activity(conn, person_id, source_id, day, start_local=None, source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, 'strength_training', NULL, ?)",
        (person_id, source_id, day, start_local if start_local is not None else f"{day}T07:00:00", source),
    )


def _watch_set(exercise="press banca", category="pecho", reps=8.0, weight_value=50.0, set_type="active"):
    return garmin_adapter.ExerciseSet(
        set_type=set_type, category=category, exercise=exercise, reps=reps,
        weight_value=weight_value, weight_unit="kg", duration_s=30.0, start_local="2026-11-01T07:00:00",
    )


class _ContadorConexion:
    """Wraps a real connection, counting `.execute` calls -- everything else (commit, row_factory,
    close via addCleanup on the WRAPPED conn) passes straight through."""

    def __init__(self, conn):
        self._conn = conn
        self.llamadas = 0

    def execute(self, *a, **kw):
        self.llamadas += 1
        return self._conn.execute(*a, **kw)

    def __getattr__(self, nombre):
        return getattr(self._conn, nombre)


class UltimaVezTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_the_latest_earlier_session_wins(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-01")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(reps=8, weight_value=40)])
        _insert_activity(self.conn, 1, "a2", "2026-11-05")
        db.save_exercise_sets(self.conn, 1, "garmin", "a2", [_watch_set(reps=10, weight_value=45)])
        self.conn.commit()
        out = entreno_historial.ultima_vez(self.conn, 1, "press banca")
        self.assertEqual(out["source_id"], "a2")
        self.assertEqual(out["day"], "2026-11-05")
        self.assertEqual(out["mejor"], {"reps": 10, "weight_kg": 45.0, "corrected": False})

    def test_a_corrected_name_is_matched_by_the_correction_not_the_watchs_name(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-05")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(exercise="press banca")])
        db.correct_exercise_set(self.conn, 1, "garmin", "a1", 1, exercise="press banca inclinado")
        self.conn.commit()
        self.assertIsNone(entreno_historial.ultima_vez(self.conn, 1, "press banca"))
        encontrado = entreno_historial.ultima_vez(self.conn, 1, "press banca inclinado")
        self.assertEqual(encontrado["source_id"], "a1")

    def test_rest_and_excluded_sets_are_ignored(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-05")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [
            _watch_set(exercise="sentadilla", set_type="rest"),
        ])
        self.conn.commit()
        self.assertIsNone(entreno_historial.ultima_vez(self.conn, 1, "sentadilla"))
        # An active set that the person marked as not having happened is ignored the same way.
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(exercise="sentadilla")])
        db.set_series_excluida(self.conn, 1, "garmin", "a1", 1, True)
        self.conn.commit()
        self.assertIsNone(entreno_historial.ultima_vez(self.conn, 1, "sentadilla"))

    def test_antes_de_is_strict(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-01", start_local="2026-11-01T07:00:00")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [_watch_set(reps=8, weight_value=40)])
        _insert_activity(self.conn, 1, "a2", "2026-11-05", start_local="2026-11-05T07:00:00")
        db.save_exercise_sets(self.conn, 1, "garmin", "a2", [_watch_set(reps=10, weight_value=45)])
        self.conn.commit()
        out = entreno_historial.ultima_vez(self.conn, 1, "press banca", antes_de="2026-11-05T07:00:00")
        self.assertEqual(out["source_id"], "a1")  # a2 itself is not strictly before its own start

    def test_mejor_prefers_heavier_then_more_reps_on_a_tie(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-05")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [
            _watch_set(reps=10, weight_value=40),
            _watch_set(reps=8, weight_value=50),
            _watch_set(reps=12, weight_value=50),
        ])
        self.conn.commit()
        out = entreno_historial.ultima_vez(self.conn, 1, "press banca")
        self.assertEqual(out["mejor"], {"reps": 12, "weight_kg": 50.0, "corrected": False})
        self.assertEqual(len(out["sets"]), 3)

    def test_bodyweight_sets_compare_by_reps(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-05")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [
            garmin_adapter.ExerciseSet(set_type="active", category="pecho", exercise="flexiones",
                                        reps=10.0, weight_value=None, weight_unit=None),
            garmin_adapter.ExerciseSet(set_type="active", category="pecho", exercise="flexiones",
                                        reps=15.0, weight_value=None, weight_unit=None),
        ])
        self.conn.commit()
        out = entreno_historial.ultima_vez(self.conn, 1, "flexiones")
        self.assertEqual(out["mejor"], {"reps": 15, "weight_kg": None, "corrected": False})

    def test_another_persons_sets_are_invisible(self):
        _insert_activity(self.conn, 2, "a1", "2026-11-05")
        db.save_exercise_sets(self.conn, 2, "garmin", "a1", [_watch_set()])
        self.conn.commit()
        self.assertIsNone(entreno_historial.ultima_vez(self.conn, 1, "press banca"))

    def test_none_when_never_done(self):
        self.assertIsNone(entreno_historial.ultima_vez(self.conn, 1, "press banca"))


class UltimasVecesTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_covers_every_exercise_of_the_session_before_its_own_start(self):
        _insert_activity(self.conn, 1, "a1", "2026-11-01", start_local="2026-11-01T07:00:00")
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [
            _watch_set(exercise="press banca", reps=8, weight_value=40),
        ])
        _insert_activity(self.conn, 1, "a2", "2026-11-08", start_local="2026-11-08T07:00:00")
        db.save_exercise_sets(self.conn, 1, "garmin", "a2", [
            _watch_set(exercise="press banca", reps=10, weight_value=45),
            _watch_set(exercise="remo", reps=10, weight_value=30),
        ])
        self.conn.commit()
        out = entreno_historial.ultimas_veces(self.conn, 1, "garmin", "a2")
        self.assertEqual(set(out), {"press banca", "remo"})
        self.assertEqual(out["press banca"]["source_id"], "a1")
        self.assertEqual(out["press banca"]["mejor"]["weight_kg"], 40.0)
        self.assertIsNone(out["remo"])  # never done before

    def test_statement_count_does_not_grow_with_the_number_of_exercises(self):
        nombres = ["press banca", "remo", "sentadilla", "curl", "zancada"]
        _insert_activity(self.conn, 1, "e1", "2026-11-01", start_local="2026-11-01T07:00:00")
        db.save_exercise_sets(self.conn, 1, "garmin", "e1", [
            _watch_set(exercise=n, reps=8, weight_value=40) for n in nombres
        ])
        self.conn.commit()
        conteos = []
        for n in (2, 3, 5):
            _insert_activity(self.conn, 1, f"cur{n}", "2026-11-08", start_local="2026-11-08T07:00:00")
            db.save_exercise_sets(self.conn, 1, "garmin", f"cur{n}", [
                _watch_set(exercise=nombre, reps=10, weight_value=45) for nombre in nombres[:n]
            ])
            self.conn.commit()
            contador = _ContadorConexion(self.conn)
            out = entreno_historial.ultimas_veces(contador, 1, "garmin", f"cur{n}")
            self.assertEqual(len(out), n)
            conteos.append(contador.llamadas)
        self.assertEqual(conteos, [conteos[0]] * len(conteos),
                          f"statement count grew with the number of exercises: {conteos}")

    def test_another_persons_sets_are_invisible(self):
        _insert_activity(self.conn, 1, "a2", "2026-11-08", start_local="2026-11-08T07:00:00")
        db.save_exercise_sets(self.conn, 1, "garmin", "a2", [_watch_set(exercise="press banca")])
        _insert_activity(self.conn, 2, "b1", "2026-11-01", start_local="2026-11-01T07:00:00")
        db.save_exercise_sets(self.conn, 2, "garmin", "b1", [_watch_set(exercise="press banca")])
        self.conn.commit()
        out = entreno_historial.ultimas_veces(self.conn, 1, "garmin", "a2")
        self.assertIsNone(out["press banca"])


if __name__ == "__main__":
    unittest.main()
