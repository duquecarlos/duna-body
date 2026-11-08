"""vida-v1 weekly section (what Personal's Sunday review reads)."""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)
from datetime import date, timedelta
from pathlib import Path
import tempfile

from tests._db import open_conn

from body import db, goals, turnos_costo, vida_export

METRICS = turnos_costo.MODELO["metricas"]
CONST = {"sleep_minutes": 420.0, "resting_hr": 55.0, "hrv_last_night": 60.0, "stress_avg": 25.0,
         "body_battery_wake": 75.0}
OFF_DELTA = {"sleep_minutes": -80.0, "resting_hr": 10.0, "hrv_last_night": -20.0, "stress_avg": 15.0,
             "body_battery_wake": -25.0}


def _insert_shift(conn, person_id: int, day: str, kind: str = "night") -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _fill_range(conn, person_id: int, start: date, end: date, values: dict[str, float]) -> None:
    d = start
    while d <= end:
        for m, v in values.items():
            db.set_metric(conn, d.isoformat(), m, v, "garmin", person_id)
        d += timedelta(1)


def _seed_block(conn, person_id: int, inicio: date, dias: int, recovery_target: int) -> date:
    """A night block of `dias` nights starting at `inicio`; the `recovery_target - 1` days right
    after it get off-band values, matching tests/test_turnos_costo.py's own fixture shape."""
    fin = inicio + timedelta(dias - 1)
    d = inicio
    while d <= fin:
        _insert_shift(conn, person_id, d.isoformat())
        d += timedelta(1)
    for i in range(1, recovery_target):
        day = fin + timedelta(i)
        for m in METRICS:
            db.set_metric(conn, day.isoformat(), m, CONST[m] + OFF_DELTA[m], "garmin", person_id)
    return fin


class VidaExportTest(unittest.TestCase):
    def test_week_section_shape(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "v.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        for i in range(5):
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "resting_hr", 55 + i, "garmin", 1)
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "readiness", 70, "body-v1", 1)
        conn.commit()
        s = vida_export.week_section(conn, monday, 1)
        self.assertEqual(s["area"], "body")
        self.assertEqual(s["week_start"], "2026-11-02")
        by_key = {m["key"]: m for m in s["metrics"]}
        self.assertEqual(by_key["body.resting_hr"]["value"], 57.0)
        self.assertEqual(by_key["body.readiness"]["kind"], "estimated")
        self.assertTrue(all(m["visibility"] == "owner" for m in s["metrics"]))
        self.assertIn("5 of 7", s["summary"])
        self.assertEqual(s["goals"], [])
        self.assertEqual(s["goal_checks"], [])

    def test_goals_only_active_with_their_own_check(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "v2.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        proposed_id = goals.propose(conn, 1, "Proposed goal", "2026-11-02", metric_key="steps", direction="raise",
                                     target_value=9000.0, baseline_value=6000.0, deadline="2026-12-14")
        active_id = goals.propose(conn, 1, "Active goal", "2026-11-02", metric_key="resting_hr", direction="lower",
                                   target_value=50.0, baseline_value=60.0, deadline="2026-12-14")
        goals.activate(conn, active_id)
        for i in range(7):
            db.set_metric(conn, (monday + timedelta(i)).isoformat(), "resting_hr", 55, "garmin", 1)
        goals.weekly_check(conn, active_id, 1, "2026-11-02")

        s = vida_export.week_section(conn, monday, 1)
        self.assertEqual(s["goals"], [{"goal_id": active_id, "on_track": True}])
        self.assertNotIn(proposed_id, [g["goal_id"] for g in s["goals"]])

    def test_goal_checks_for_shared_household_goals(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "v3.db")
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        conn.commit()
        monday = date(2026, 11, 2)
        gid = goals.propose(conn, 1, "Shared steps goal", "2026-11-02", kind="milestone",
                             deadline="2026-12-14", hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        hid = goals.add_habit(conn, gid, "walk", {"per": "day", "times": 1})
        for i in range(7):
            goals.check_habit(conn, hid, 1, (monday + timedelta(i)).isoformat(), True)
        goals.weekly_check(conn, gid, 1, "2026-11-02")

        s = vida_export.week_section(conn, monday, 1)
        self.assertEqual(len(s["goal_checks"]), 1)
        gc = s["goal_checks"][0]
        self.assertEqual(gc["goal_id"], gid)
        self.assertEqual(gc["person"], vida_export.PERSON_KEY)
        self.assertEqual(gc["week_start"], "2026-11-02")
        self.assertTrue(gc["on_track"])


class ShiftSectionTest(unittest.TestCase):
    """BODY-TURNOS-EXPORT-WORK-SHIFT-1: shift_nights / shift_working_days from confirmed shift rows."""

    def test_night_and_working_day_counts(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "sh1.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        kinds = ["night", "night", "night", "off", "off", "day", "morning"]
        for i, kind in enumerate(kinds):
            _insert_shift(conn, 1, (monday + timedelta(i)).isoformat(), kind=kind)
        conn.commit()
        s = vida_export.week_section(conn, monday, 1)
        by_key = {m["key"]: m for m in s["metrics"]}
        self.assertEqual(by_key["body.shift_nights"]["value"], 3)
        self.assertEqual(by_key["body.shift_nights"]["kind"], "measured")
        self.assertEqual(by_key["body.shift_working_days"]["value"], 5)
        self.assertEqual(by_key["body.shift_working_days"]["kind"], "measured")

    def test_no_shift_rows_means_no_shift_keys(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "sh2.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        s = vida_export.week_section(conn, monday, 1)
        keys = {m["key"] for m in s["metrics"]}
        self.assertNotIn("body.shift_nights", keys)
        self.assertNotIn("body.shift_working_days", keys)
        self.assertNotIn("body.recovery_days_est", keys)

    def test_proposed_shifts_are_not_counted(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "sh3.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        conn.execute(
            "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, day_count, "
            "created_at) VALUES (1, 'turnos.pdf', '2026-11-01T00:00:00', '2026-11-01T00:00:00', "
            "'proposed', 1, '2026-11-01T00:00:00')"
        )
        batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO shift_batch_item (batch_id, day, kind, start_local) VALUES (?, ?, 'night', ?)",
            (batch_id, monday.isoformat(), f"{monday.isoformat()}T22:00:00"),
        )
        conn.commit()
        s = vida_export.week_section(conn, monday, 1)
        keys = {m["key"] for m in s["metrics"]}
        self.assertNotIn("body.shift_nights", keys)

    def test_person_b_shifts_never_reach_person_a(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "sh4.db")
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        monday = date(2026, 11, 2)
        _insert_shift(conn, 2, monday.isoformat(), kind="night")
        conn.commit()
        s = vida_export.week_section(conn, monday, 1)
        keys = {m["key"] for m in s["metrics"]}
        self.assertNotIn("body.shift_nights", keys)

    def test_shape_still_holds_with_shift_keys_present(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "sh5.db")
        db.init(conn)
        monday = date(2026, 11, 2)
        _insert_shift(conn, 1, monday.isoformat(), kind="night")
        conn.commit()
        s = vida_export.week_section(conn, monday, 1)
        self.assertEqual(s["area"], "body")
        self.assertEqual(s["week_start"], "2026-11-02")
        self.assertTrue(all(m["visibility"] == "owner" for m in s["metrics"]))


class RecoverySectionTest(unittest.TestCase):
    """BODY-TURNOS-EXPORT-WORK-SHIFT-1: body.recovery_days_est, gated on the same recovery-window
    rule turnos_costo.turno_de_hoy uses for Hoy, evaluated for the week's Sunday."""

    def _seeded(self, recovery_target: int = 3, blocks: int = 3):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "rec.db")
        db.init(conn)
        hoy_base = date(2026, 6, 20)
        _fill_range(conn, 1, hoy_base - timedelta(140), hoy_base - timedelta(1), CONST)
        fin = None
        inicio = hoy_base - timedelta(100)
        for _ in range(blocks):
            fin = _seed_block(conn, 1, inicio, 5, recovery_target)
            inicio = fin + timedelta(30)
        conn.commit()
        return conn, fin

    def test_recovery_present_with_a_hero(self):
        conn, fin = self._seeded(recovery_target=3, blocks=3)
        sunday = fin + timedelta(1)
        monday = sunday - timedelta(6)
        s = vida_export.week_section(conn, monday, 1)
        by_key = {m["key"]: m for m in s["metrics"]}
        self.assertEqual(by_key["body.recovery_days_est"]["value"], 3)
        self.assertEqual(by_key["body.recovery_days_est"]["kind"], "estimated")
        self.assertIn("Recovering from nights", s["summary"])

    def test_recovery_absent_with_fewer_than_min_bloques(self):
        conn, fin = self._seeded(recovery_target=3, blocks=1)
        sunday = fin + timedelta(1)
        monday = sunday - timedelta(6)
        s = vida_export.week_section(conn, monday, 1)
        keys = {m["key"] for m in s["metrics"]}
        self.assertNotIn("body.recovery_days_est", keys)


class VidaPersonTest(unittest.TestCase):
    def test_hosted_person_with_a_sub_gets_the_sub(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "p1.db")
        db.init(conn)
        conn.execute("UPDATE person SET cuenta_sub = ? WHERE id = 1", ("sub-demo-1",))
        conn.commit()
        self.assertEqual(vida_export.vida_person(conn, 1), "sub-demo-1")
        monday = date(2026, 11, 2)
        self.assertEqual(vida_export.week_section(conn, monday, 1)["person"], "sub-demo-1")

    def test_two_persons_get_different_values(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "p2.db")
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name, cuenta_sub) VALUES (2, 'B', ?)", ("sub-demo-b",))
        conn.execute("UPDATE person SET cuenta_sub = ? WHERE id = 1", ("sub-demo-a",))
        conn.commit()
        self.assertEqual(vida_export.vida_person(conn, 1), "sub-demo-a")
        self.assertEqual(vida_export.vida_person(conn, 2), "sub-demo-b")

    def test_local_person_keeps_the_old_key(self):
        conn = open_conn(self, Path(tempfile.mkdtemp()) / "p3.db")
        db.init(conn)
        self.assertEqual(vida_export.vida_person(conn, 1), vida_export.PERSON_KEY)
        monday = date(2026, 11, 2)
        self.assertEqual(vida_export.week_section(conn, monday, 1)["person"], vida_export.PERSON_KEY)


if __name__ == "__main__":
    unittest.main()
