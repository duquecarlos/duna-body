"""body/demo.py: refusals (never writing anything), a seeded temp home has what the catalog's tasks
need, and two runs give identical data (BODY-TUTORIALES-1). Runs the CLI as a subprocess so this
module never mutates body.config's process-wide BODY_HOME for tests that run after it."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body.db import METRIC_DEFS

ROOT = Path(__file__).resolve().parent.parent
MEASURED_METRIC_KEYS = [k for (k, *_rest) in METRIC_DEFS if k != "readiness"]


def _run(args: list[str], env: dict | None = None, timeout: int = 120) -> "subprocess.CompletedProcess":
    import subprocess
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, "-m", "body.demo", *args], cwd=ROOT, capture_output=True,
                           text=True, timeout=timeout, env=full_env)


def _fresh_dir() -> str:
    return tempfile.mkdtemp(prefix="body-demo-test-")


def _row_checksum(conn: sqlite3.Connection) -> str:
    """A checksum of every seeded table's rows, with creation timestamps excluded, so two runs with
    the same seed can be compared without their wall-clock-dependent columns disagreeing."""
    h = hashlib.sha256()
    tables = ["person", "daily_metric", "sleep_session", "activity", "shift", "shift_batch",
              "shift_batch_item", "goal", "habit", "habit_check", "goal_check"]
    excluded = {"creado_en", "created_at", "decided_at", "extracted_at", "received"}
    for name in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})") if r[1] not in excluded]
        order = ", ".join(cols)
        rows = conn.execute(f"SELECT {order} FROM {name} ORDER BY {order}").fetchall()
        h.update(name.encode())
        for row in rows:
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


class RefusalTest(unittest.TestCase):
    def test_refuses_without_home(self):
        result = _run([])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--home", result.stderr)

    def test_refuses_default_home(self):
        fake_home_dir = _fresh_dir()
        default_target = str(Path(fake_home_dir) / ".body-app")
        result = _run(["--home", default_target], env={"HOME": fake_home_dir, "USERPROFILE": fake_home_dir})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("default", result.stderr)
        self.assertFalse((Path(fake_home_dir) / ".body-app" / "body.db").exists())

    def test_refuses_current_env_home(self):
        env_home = _fresh_dir()
        result = _run(["--home", env_home], env={"BODY_HOME": env_home})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BODY_HOME", result.stderr)
        self.assertFalse((Path(env_home) / "body.db").exists())

    def test_refuses_existing_non_demo_db(self):
        folder = _fresh_dir()
        conn = sqlite3.connect(str(Path(folder) / "body.db"))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()
        result = _run(["--home", folder])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEMO", result.stderr)

    def test_rerun_into_demo_marked_folder_rebuilds(self):
        folder = _fresh_dir()
        first = _run(["--home", folder, "--dias", "20"])
        self.assertEqual(first.returncode, 0, first.stderr)
        second = _run(["--home", folder, "--dias", "20"])
        self.assertEqual(second.returncode, 0, second.stderr)


class OutputHasNoFigureTest(unittest.TestCase):
    def test_output_is_counts_and_folder_only(self):
        folder = _fresh_dir()
        result = _run(["--home", folder, "--dias", "20"])
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stdout.splitlines():
            if line.startswith("demo home:"):
                continue
            if ":" not in line:
                continue
            _, _, value = line.partition(":")
            self.assertRegex(value.strip(), r"^-?\d+$", f"line {line!r} is not a plain count")


class SeededContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = _fresh_dir()
        result = _run(["--home", cls.folder, "--dias", "90"])
        assert result.returncode == 0, result.stderr
        cls.conn = sqlite3.connect(str(Path(cls.folder) / "body.db"))
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_has_one_demo_person(self):
        rows = self.conn.execute("SELECT display_name FROM person").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "Demo")

    def test_day_count_matches_requested_span(self):
        days = {r["day"] for r in self.conn.execute("SELECT DISTINCT day FROM daily_metric")}
        self.assertGreater(len(days), 60)
        self.assertLessEqual(len(days), 90)

    def test_every_catalogue_metric_present_at_least_once(self):
        present = {r["metric_key"] for r in self.conn.execute("SELECT DISTINCT metric_key FROM daily_metric")}
        missing = [k for k in MEASURED_METRIC_KEYS if k not in present]
        self.assertEqual(missing, [], f"metrics never seeded: {missing}")

    def test_has_a_night_week(self):
        nights = self.conn.execute("SELECT COUNT(*) FROM shift WHERE kind = 'night'").fetchone()[0]
        self.assertGreater(nights, 0)

    def test_has_three_active_goals_with_checks(self):
        goals = self.conn.execute("SELECT id FROM goal WHERE status = 'active'").fetchall()
        self.assertEqual(len(goals), 3)
        checks = self.conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0]
        self.assertGreater(checks, 0)

    def test_has_one_habit(self):
        habits = self.conn.execute("SELECT COUNT(*) FROM habit").fetchone()[0]
        self.assertEqual(habits, 1)

    def test_demo_marker_written(self):
        marker = Path(self.folder) / "DEMO"
        self.assertTrue(marker.exists())
        meta = json.loads(marker.read_text(encoding="utf-8"))
        self.assertIn("seed", meta)


class DeterminismTest(unittest.TestCase):
    def test_two_runs_give_identical_rows(self):
        folder_a, folder_b = _fresh_dir(), _fresh_dir()
        ra = _run(["--home", folder_a, "--dias", "40"])
        rb = _run(["--home", folder_b, "--dias", "40"])
        self.assertEqual(ra.returncode, 0, ra.stderr)
        self.assertEqual(rb.returncode, 0, rb.stderr)
        conn_a = sqlite3.connect(str(Path(folder_a) / "body.db"))
        conn_b = sqlite3.connect(str(Path(folder_b) / "body.db"))
        try:
            self.assertEqual(_row_checksum(conn_a), _row_checksum(conn_b))
        finally:
            conn_a.close()
            conn_b.close()


if __name__ == "__main__":
    unittest.main()
