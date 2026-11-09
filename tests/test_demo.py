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
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, demo, turnos_costo
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
              "shift_batch_item", "goal", "habit", "habit_check", "goal_check", "goal_enlace"]
    excluded = {"creado_en", "created_at", "creado_at", "decided_at", "extracted_at", "received"}
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

    def test_has_two_demo_persons(self):
        rows = self.conn.execute("SELECT display_name FROM person ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["display_name"] for r in rows], ["Demo", "Ana"])

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

    def test_has_at_least_three_night_blocks(self):
        row = self.conn.execute("SELECT MIN(day), MAX(day) FROM shift").fetchone()
        bloques = turnos_costo.bloques_noche(self.conn, 1, row[0], row[1])
        self.assertGreaterEqual(len(bloques), 3)

    def test_has_four_active_goals_with_checks(self):
        goals = self.conn.execute("SELECT id FROM goal WHERE status = 'active'").fetchall()
        self.assertEqual(len(goals), 4)
        checks = self.conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0]
        self.assertGreater(checks, 0)

    def test_has_three_habits(self):
        habits = self.conn.execute("SELECT COUNT(*) FROM habit").fetchone()[0]
        self.assertEqual(habits, 3)

    def test_has_one_household_goal_created_by_ana(self):
        row = self.conn.execute(
            "SELECT person_id FROM goal WHERE visibility = 'household'").fetchone()
        self.assertIsNotNone(row)
        creator = self.conn.execute("SELECT display_name FROM person WHERE id = ?", (row[0],)).fetchone()
        self.assertEqual(creator["display_name"], "Ana")

    def test_ana_shares_one_figure_with_the_household(self):
        ana_id = self.conn.execute("SELECT id FROM person WHERE display_name = 'Ana'").fetchone()[0]
        rows = self.conn.execute(
            "SELECT metric_key FROM figura_compartida WHERE person_id = ? AND withdrawn_at IS NULL",
            (ana_id,)).fetchall()
        self.assertEqual([r[0] for r in rows], ["resting_hr"])

    def test_ana_has_her_own_short_history_of_the_shared_figure(self):
        """BODY-HOGAR-COMPARTIR-1-M fix 4: Ana needs values of her OWN, or the Hoy card she feeds
        can only ever say "sin dato" and the value path is never measurable."""
        ana_id = self.conn.execute("SELECT id FROM person WHERE display_name = 'Ana'").fetchone()[0]
        rows = self.conn.execute(
            "SELECT day, value FROM daily_metric WHERE person_id = ? AND metric_key = 'resting_hr' ORDER BY day",
            (ana_id,)).fetchall()
        self.assertGreaterEqual(len(rows), 1)
        for _day, value in rows:
            self.assertIsNotNone(value)

    def test_ana_s_household_goal_has_a_lugares_link(self):
        row = self.conn.execute(
            "SELECT ge.app, ge.ref FROM goal_enlace ge JOIN goal g ON g.id = ge.goal_id "
            "WHERE g.visibility = 'household'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "lugares")
        self.assertRegex(row[1], r"^[1-9][0-9]{0,11}$")

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


class _FakeToday(date):
    """`date.today()` returns a fixed value -- patched onto body.demo's own `date` name only, so
    `_seed()`'s `anchor = date.today()` picks a chosen anchor instead of the real clock
    (BODY-DEMO-TURNOS-1), with no change to demo.py's seeding logic itself."""
    _valor = date(2024, 1, 1)

    @classmethod
    def today(cls):
        return cls._valor


def _seed_at(case: unittest.TestCase, hoy: date, dias: int) -> sqlite3.Connection:
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    _FakeToday._valor = hoy
    with mock.patch.object(demo, "date", _FakeToday):
        demo._seed(conn, dias=dias, idioma="es")
    return conn


class UltimaSemanaReachableTest(unittest.TestCase):
    """BODY-DEMO-TURNOS-1: the default demo seed must show BOTH of REFERENCIA-NOCHE-1's states
    honestly from its own seed, whatever day it runs on -- a verdict for at least one metric from
    the default window, the gap reason from a short one. Checked across one full 28-day cycle of
    anchors (the demo's own 4-week shift pattern repeats every 28 days), not just today's real date,
    so the seeded default cannot depend on which day of that cycle "today" happens to fall on."""

    ANCHOR0 = date(2026, 1, 5)  # arbitrary; a full cycle of offsets covers every phase regardless

    def test_default_seed_gives_a_verdict_every_day_of_the_cycle(self):
        for offset in range(28):
            hoy = self.ANCHOR0 + timedelta(offset)
            with self.subTest(hoy=hoy.isoformat()):
                conn = _seed_at(self, hoy, demo.DIAS_DEFAULT)
                u = turnos_costo.resumen(conn, 1, hoy=hoy)["ultima_semana"]
                self.assertIsNotNone(u, f"no completed night block seeded for hoy={hoy}")
                self.assertTrue(u["metricas"], f"no metric reached a verdict for hoy={hoy}")

    def test_short_seed_still_shows_the_gap_every_day_of_the_cycle(self):
        corto = 40  # well under MIN_BLOQUES_PREVIOS's reach (BODY-TURNOS-REFERENCIA-NOCHE-1)
        for offset in range(28):
            hoy = self.ANCHOR0 + timedelta(offset)
            with self.subTest(hoy=hoy.isoformat()):
                conn = _seed_at(self, hoy, corto)
                u = turnos_costo.resumen(conn, 1, hoy=hoy)["ultima_semana"]
                self.assertTrue(u is None or u["motivo"] == "pocos_bloques_previos",
                                 f"expected a gap state for hoy={hoy}, got {u}")


if __name__ == "__main__":
    unittest.main()
