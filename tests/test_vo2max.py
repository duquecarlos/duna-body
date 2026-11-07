"""BODY-VO2MAX-1: cardio-fitness metrics (VO2 max estimate, cycling VO2 max, fitness age) from
Garmin's max-metrics feed. Invented demo data only, no real health figures.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, goals, identity, web
from body.agente import herramientas

PERSON_B = 2


def _conn(case: unittest.TestCase):
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "vo2max.db")


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


class CatalogueTest(unittest.TestCase):
    def test_three_rows_with_expected_directions_and_kind(self):
        conn = _conn(self)
        db.init(conn)
        rows = {r["key"]: r for r in conn.execute(
            "SELECT key, better, kind FROM metric_def WHERE key IN ('vo2max', 'vo2max_cycling', 'fitness_age')"
        )}
        self.assertEqual(set(rows), {"vo2max", "vo2max_cycling", "fitness_age"})
        self.assertEqual(rows["vo2max"]["better"], "higher")
        self.assertEqual(rows["vo2max_cycling"]["better"], "higher")
        self.assertEqual(rows["fitness_age"]["better"], "lower")
        for key in ("vo2max", "vo2max_cycling", "fitness_age"):
            self.assertEqual(rows[key]["kind"], "measured")


class TrendsPageTest(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "trends.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        for i in range(14):
            db.set_metric(conn, f"2026-10-{i + 1:02d}", "vo2max", 44.0 + i * 0.1, "garmin", 1)
        conn.commit()
        self.client = TestClient(web.app, follow_redirects=False)

    def test_vo2max_card_appears_for_that_person_only(self):
        _as(1)
        page = self.client.get("/trends").text
        self.assertIn("VO2 máx.", page)
        self.assertIn("Garmin lo actualiza solo", page)  # the honest sparse-data note

        _as(PERSON_B)
        page_b = self.client.get("/trends").text
        self.assertNotIn("VO2 máx.", page_b)


class GoalOnVo2maxTest(unittest.TestCase):
    def test_goal_created_and_sparse_week_reads_as_no_data_not_failure(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Raise VO2 max", "2026-11-02", metric_key="vo2max", direction="raise",
                             target_value=50.0, baseline_value=44.0, deadline="2026-12-14")
        goals.activate(conn, gid)
        # fewer than MIN_METRIC_DAYS (4) readings this week -- sparse is normal for this metric
        for i in range(2):
            db.set_metric(conn, f"2026-11-{2 + i:02d}", "vo2max", 45.0, "garmin", 1)
        r = goals.weekly_check(conn, gid, 1, "2026-11-02")
        self.assertIsNone(r["on_track"])
        self.assertIsNone(r["metric_avg"])


class AssistantTrendsToolTest(unittest.TestCase):
    def test_leer_tendencias_accepts_vo2max(self):
        conn = _conn(self)
        db.init(conn)
        for i in range(14):
            db.set_metric(conn, f"2026-10-{i + 1:02d}", "vo2max", 44.0 + i * 0.1, "garmin", 1)
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_tendencias", {"metric": "vo2max"}, "completo")
        self.assertIn("vo2max", out["metricas"])
        self.assertEqual(out["metricas"]["vo2max"]["dias_con_datos"], 14)

    def test_leer_tendencias_rejects_unknown_metric(self):
        conn = _conn(self)
        db.init(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "leer_tendencias", {"metric": "vo2max_cycling"}, "completo")


if __name__ == "__main__":
    unittest.main()
