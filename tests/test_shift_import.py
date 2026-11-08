"""The Turnos import: propose -> confirm/dismiss, the whole-file refusal rules, the slot-index regression
(BODY-TURNOS-IMPORT-OBS-1), the Turnos confirm card and the Hoy notice, and the CLI."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient  # noqa: E402

from body import config, db, shift_import, web  # noqa: E402
from body.i18n import T  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _write(obj: dict, name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="body-shift-src-"))
    p = d / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


class ShiftImportTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(str(TMP))  # guard present
        path = Path(tempfile.mkdtemp(prefix="body-shift-test-")) / "test.db"
        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    # ---- propose / idempotency

    def test_propose_idempotent(self):
        b1 = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        b2 = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        self.assertEqual(b1, b2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_batch").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM shift_batch_item WHERE batch_id = ?", (b1,)).fetchone()[0], 4
        )

    def test_reextraction_wins_after_confirm(self):
        a_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        shift_import.confirm(self.conn, a_id)
        a2_path = _write({
            "source": {"mail_subject": "TURNOS NOVIEMBRE A ENERO", "received": "2026-11-04T12:00:00-05:00",
                       "attachment": "turnos.xlsx", "extracted_at": "2026-11-04T18:00:00-05:00"},
            "person": "carlos", "timezone": "America/Bogota",
            "shifts": [
                {"day": "2026-11-09", "kind": "night", "code": "N",
                 "start_local": "2026-11-09T21:00", "end_local": "2026-11-10T05:00",
                 "holiday": False, "sunday": False, "note": "corregido"},
            ],
        }, "turnos-2026-11_2027-01-b.json")
        a2_id = shift_import.propose(self.conn, a2_path, 1)
        self.assertNotEqual(a_id, a2_id)
        shift_import.confirm(self.conn, a2_id)
        row = self.conn.execute(
            "SELECT start_local, batch_id FROM shift WHERE person_id = 1 AND day = '2026-11-09'"
        ).fetchone()
        self.assertEqual(row["start_local"], "2026-11-09T21:00")
        self.assertEqual(row["batch_id"], a2_id)

    # ---- whole-file refusal (decision 3), zero rows written

    def _refused(self, shifts, source_extra=None):
        src = {"mail_subject": "T", "received": "2026-11-04T12:00:00-05:00", "attachment": "t.xlsx",
               "extracted_at": "2026-11-04T12:05:00-05:00"}
        if source_extra is not None:
            src = source_extra
        path = _write({"source": src, "person": "carlos", "timezone": "America/Bogota", "shifts": shifts},
                      "turnos-refused.json")
        with self.assertRaises(shift_import.ImportRefused):
            shift_import.propose(self.conn, path, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_batch").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_batch_item").fetchone()[0], 0)

    def test_refuses_unknown_kind(self):
        self._refused([{"day": "2026-11-09", "kind": "siesta", "code": "X",
                        "start_local": "2026-11-09T22:00", "end_local": "2026-11-10T06:00",
                        "holiday": False, "sunday": False, "note": ""}])

    def test_refuses_malformed_day(self):
        self._refused([{"day": "2026-13-40", "kind": "night", "code": "N",
                        "start_local": "2026-13-40T22:00", "end_local": "2026-13-41T06:00",
                        "holiday": False, "sunday": False, "note": ""}])

    def test_refuses_start_local_date_mismatch(self):
        self._refused([{"day": "2026-11-09", "kind": "night", "code": "N",
                        "start_local": "2026-11-10T22:00", "end_local": "2026-11-11T06:00",
                        "holiday": False, "sunday": False, "note": ""}])

    def test_refuses_start_local_on_off_day(self):
        self._refused([{"day": "2026-11-10", "kind": "off", "code": "D",
                        "start_local": "2026-11-10T08:00", "end_local": None,
                        "holiday": False, "sunday": False, "note": ""}])

    def test_refuses_missing_start_local_on_working_day(self):
        self._refused([{"day": "2026-11-09", "kind": "night", "code": "N",
                        "start_local": None, "end_local": "2026-11-10T06:00",
                        "holiday": False, "sunday": False, "note": ""}])

    def test_refuses_duplicate_slot(self):
        self._refused([
            {"day": "2026-11-09", "kind": "night", "code": "N",
             "start_local": "2026-11-09T22:00", "end_local": "2026-11-10T06:00",
             "holiday": False, "sunday": False, "note": ""},
            {"day": "2026-11-09", "kind": "night", "code": "N",
             "start_local": "2026-11-09T22:00", "end_local": "2026-11-10T06:00",
             "holiday": False, "sunday": False, "note": "duplicado"},
        ])

    def test_refuses_off_and_working_same_day(self):
        self._refused([
            {"day": "2026-11-10", "kind": "off", "code": "D",
             "start_local": None, "end_local": None, "holiday": False, "sunday": False, "note": ""},
            {"day": "2026-11-10", "kind": "night", "code": "N",
             "start_local": "2026-11-10T22:00", "end_local": "2026-11-11T06:00",
             "holiday": False, "sunday": False, "note": ""},
        ])

    def test_refuses_missing_extracted_at(self):
        self._refused(
            [{"day": "2026-11-09", "kind": "night", "code": "N",
              "start_local": "2026-11-09T22:00", "end_local": "2026-11-10T06:00",
              "holiday": False, "sunday": False, "note": ""}],
            source_extra={"mail_subject": "T", "received": "2026-11-04T12:00:00-05:00", "attachment": "t.xlsx"},
        )

    def test_two_slots_same_day_allowed(self):
        path = _write({
            "source": {"mail_subject": "T", "received": "2026-12-10T12:00:00-05:00", "attachment": "t.xlsx",
                       "extracted_at": "2026-12-10T12:05:00-05:00"},
            "person": "carlos", "timezone": "America/Bogota",
            "shifts": [
                {"day": "2026-12-15", "kind": "morning", "code": "M",
                 "start_local": "2026-12-15T06:00", "end_local": "2026-12-15T10:00",
                 "holiday": False, "sunday": False, "note": "split"},
                {"day": "2026-12-15", "kind": "afternoon", "code": "T",
                 "start_local": "2026-12-15T16:00", "end_local": "2026-12-15T20:00",
                 "holiday": False, "sunday": False, "note": "split"},
            ],
        }, "turnos-split.json")
        batch_id = shift_import.propose(self.conn, path, 1)
        shift_import.confirm(self.conn, batch_id)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM shift WHERE person_id = 1 AND day = '2026-12-15'"
        ).fetchone()[0]
        self.assertEqual(rows, 2)

    # ---- confirm precedence

    def test_newer_batch_wins_overlapping_days(self):
        a_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        shift_import.confirm(self.conn, a_id)
        b_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-b.json", 1)
        shift_import.confirm(self.conn, b_id)
        overlap = self.conn.execute(
            "SELECT start_local, batch_id FROM shift WHERE person_id = 1 AND day = '2026-11-09'"
        ).fetchone()
        self.assertEqual(overlap["start_local"], "2026-11-09T21:30")
        self.assertEqual(overlap["batch_id"], b_id)
        untouched = self.conn.execute(
            "SELECT batch_id FROM shift WHERE person_id = 1 AND day = '2026-11-11'"
        ).fetchone()
        self.assertEqual(untouched["batch_id"], a_id)
        new_day = self.conn.execute(
            "SELECT 1 FROM shift WHERE person_id = 1 AND day = '2026-11-13'"
        ).fetchone()
        self.assertIsNotNone(new_day)

    def test_older_file_after_newer_confirmed_marks_newer_kept(self):
        b_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-b.json", 1)
        shift_import.confirm(self.conn, b_id)
        a_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        shift_import.confirm(self.conn, a_id)
        outcome = self.conn.execute(
            "SELECT outcome FROM shift_batch_item WHERE batch_id = ? AND day = '2026-11-09'", (a_id,)
        ).fetchone()
        self.assertEqual(outcome["outcome"], "newer_kept")
        row = self.conn.execute(
            "SELECT batch_id FROM shift WHERE person_id = 1 AND day = '2026-11-09'"
        ).fetchone()
        self.assertEqual(row["batch_id"], b_id)
        applied = self.conn.execute(
            "SELECT outcome FROM shift_batch_item WHERE batch_id = ? AND day = '2026-11-11'", (a_id,)
        ).fetchone()
        self.assertEqual(applied["outcome"], "applied")

    def test_manual_row_kept(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, start_local, end_local, source) "
            "VALUES (1, '2026-11-09', 'night', '2026-11-09T23:00', '2026-11-10T07:00', 'manual')"
        )
        self.conn.commit()
        batch_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        shift_import.confirm(self.conn, batch_id)
        row = self.conn.execute(
            "SELECT source, start_local FROM shift WHERE person_id = 1 AND day = '2026-11-09'"
        ).fetchone()
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["start_local"], "2026-11-09T23:00")
        outcome = self.conn.execute(
            "SELECT outcome FROM shift_batch_item WHERE batch_id = ? AND day = '2026-11-09'", (batch_id,)
        ).fetchone()
        self.assertEqual(outcome["outcome"], "manual_kept")

    def test_batch_becomes_superseded_when_fully_replaced(self):
        x_path = _write({
            "source": {"mail_subject": "T", "received": "2026-12-01T12:00:00-05:00", "attachment": "t.xlsx",
                       "extracted_at": "2026-12-01T12:05:00-05:00"},
            "person": "carlos", "timezone": "America/Bogota",
            "shifts": [{"day": "2026-12-05", "kind": "night", "code": "N",
                        "start_local": "2026-12-05T22:00", "end_local": "2026-12-06T06:00",
                        "holiday": False, "sunday": False, "note": ""}],
        }, "turnos-x.json")
        x_id = shift_import.propose(self.conn, x_path, 1)
        shift_import.confirm(self.conn, x_id)

        y_path = _write({
            "source": {"mail_subject": "T", "received": "2026-12-02T12:00:00-05:00", "attachment": "t.xlsx",
                       "extracted_at": "2026-12-02T12:05:00-05:00"},
            "person": "carlos", "timezone": "America/Bogota",
            "shifts": [{"day": "2026-12-05", "kind": "night", "code": "N",
                        "start_local": "2026-12-05T21:30", "end_local": "2026-12-06T05:30",
                        "holiday": False, "sunday": False, "note": "ajustado"}],
        }, "turnos-y.json")
        y_id = shift_import.propose(self.conn, y_path, 1)
        shift_import.confirm(self.conn, y_id)

        status = self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (x_id,)).fetchone()["status"]
        self.assertEqual(status, "superseded")

    def test_dismiss(self):
        batch_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        shift_import.dismiss(self.conn, batch_id)
        status = self.conn.execute(
            "SELECT status FROM shift_batch WHERE id = ?", (batch_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "dismissed")
        again = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        self.assertEqual(again, batch_id)
        status2 = self.conn.execute(
            "SELECT status FROM shift_batch WHERE id = ?", (batch_id,)
        ).fetchone()["status"]
        self.assertEqual(status2, "dismissed")

    # ---- confirm_all (BODY-TURNOS-SYNC-1): same confirm(), many files, one tap

    def test_confirm_all_applies_in_order_matches_one_by_one(self):
        a_path = FIXTURES / "turnos-demo-a.json"
        b_path = FIXTURES / "turnos-demo-b.json"
        c_path = _write({
            "source": {"mail_subject": "T", "received": "2026-11-06T12:00:00-05:00", "attachment": "t.xlsx",
                       "extracted_at": "2026-11-06T12:05:00-05:00"},
            "person": "carlos", "timezone": "America/Bogota",
            "shifts": [
                {"day": "2026-11-11", "kind": "day", "code": "C", "start_local": "2026-11-11T08:00",
                 "end_local": "2026-11-11T16:00", "holiday": False, "sunday": False, "note": "override"},
                {"day": "2026-11-14", "kind": "night", "code": "N", "start_local": "2026-11-14T22:00",
                 "end_local": "2026-11-15T06:00", "holiday": False, "sunday": False, "note": ""},
            ],
        }, "turnos-c.json")

        # propose out of (received, extracted_at) order on purpose: C, A, B
        shift_import.propose(self.conn, c_path, 1)
        shift_import.propose(self.conn, a_path, 1)
        shift_import.propose(self.conn, b_path, 1)
        counts = shift_import.confirm_all(self.conn, 1)
        self.assertEqual(counts["batches"], 3)
        rows1 = {(r["day"], r["kind"], r["start_local"], r["source"]) for r in self.conn.execute(
            "SELECT day, kind, start_local, source FROM shift WHERE person_id = 1"
        ).fetchall()}

        other_path = Path(tempfile.mkdtemp(prefix="body-shift-oneby-")) / "test.db"
        conn2 = db.connect(other_path)
        self.addCleanup(conn2.close)
        db.init(conn2)
        a_id = shift_import.propose(conn2, a_path, 1)
        shift_import.confirm(conn2, a_id)
        b_id = shift_import.propose(conn2, b_path, 1)
        shift_import.confirm(conn2, b_id)
        c_id = shift_import.propose(conn2, c_path, 1)
        shift_import.confirm(conn2, c_id)
        rows2 = {(r["day"], r["kind"], r["start_local"], r["source"]) for r in conn2.execute(
            "SELECT day, kind, start_local, source FROM shift WHERE person_id = 1"
        ).fetchall()}
        self.assertTrue(rows1)
        self.assertEqual(rows1, rows2)

    def test_confirm_all_manual_day_survives(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, start_local, end_local, source) "
            "VALUES (1, '2026-11-09', 'night', '2026-11-09T23:00', '2026-11-10T07:00', 'manual')"
        )
        self.conn.commit()
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-b.json", 1)
        counts = shift_import.confirm_all(self.conn, 1)
        row = self.conn.execute(
            "SELECT source, start_local FROM shift WHERE person_id = 1 AND day = '2026-11-09'"
        ).fetchone()
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["start_local"], "2026-11-09T23:00")
        self.assertGreater(counts["days_kept_manual"], 0)

    def test_confirm_all_refuses_when_slot_index_missing(self):
        path = Path(tempfile.mkdtemp(prefix="body-shift-idx2-")) / "idx.db"
        conn = db.connect(path)
        self.addCleanup(conn.close)
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-10', 'off', 'manual')")
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-10', 'off', 'manual')")
        conn.commit()
        db._migrate(conn)
        self.assertFalse(shift_import.slot_index_ok(conn))
        shift_import.propose(conn, FIXTURES / "turnos-demo-a.json", 1)
        with self.assertRaises(shift_import.SlotIndexMissing):
            shift_import.confirm_all(conn, 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shift").fetchone()[0], 2)
        self.assertEqual(
            conn.execute("SELECT status FROM shift_batch WHERE person_id = 1").fetchone()["status"], "proposed"
        )

    def test_summary_proposed_counts(self):
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-b.json", 1)
        c_path = _write({
            "source": {"mail_subject": "T", "received": "2026-12-01T12:00:00-05:00", "attachment": "t.xlsx",
                       "extracted_at": "2026-12-01T12:05:00-05:00"},
            "person": "carlos", "timezone": "America/Bogota",
            "shifts": [{"day": "2026-12-05", "kind": "night", "code": "N",
                        "start_local": "2026-12-05T22:00", "end_local": "2026-12-06T06:00",
                        "holiday": False, "sunday": False, "note": ""}],
        }, "turnos-summary-c.json")
        shift_import.propose(self.conn, c_path, 1)
        summary = shift_import.summary_proposed(self.conn, 1)
        self.assertEqual(summary["batch_count"], 3)
        self.assertEqual(summary["first_day"], "2026-11-09")
        self.assertEqual(summary["last_day"], "2026-12-05")
        self.assertEqual(summary["day_count"], 6)
        self.assertEqual(summary["night_count"], 3)

    def test_summary_proposed_empty(self):
        summary = shift_import.summary_proposed(self.conn, 1)
        self.assertEqual(summary, {"batch_count": 0, "first_day": None, "last_day": None,
                                    "day_count": 0, "night_count": 0})

    # ---- the measured gap (BODY-TURNOS-IMPORT-OBS-1) and its guard

    def test_slot_index_rejects_second_null_off_day(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-10', 'off', 'manual')"
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-10', 'off', 'manual')"
            )

    def test_migration_swallows_integrity_error_and_leaves_rows_untouched(self):
        path = Path(tempfile.mkdtemp(prefix="body-shift-idx-")) / "idx.db"
        conn = db.connect(path)
        self.addCleanup(conn.close)
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-10', 'off', 'manual')")
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-11-10', 'off', 'manual')")
        conn.commit()
        db._migrate(conn)  # must not raise, must not delete anything
        self.assertFalse(shift_import.slot_index_ok(conn))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shift").fetchone()[0], 2)
        batch_id = shift_import.propose(conn, FIXTURES / "turnos-demo-a.json", 1)
        with self.assertRaises(shift_import.SlotIndexMissing):
            shift_import.confirm(conn, batch_id)
        # confirm refused before touching anything
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shift").fetchone()[0], 2)


class ShiftWebTest(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = Path(tempfile.mkdtemp(prefix="body-shift-web-")) / "test.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.client = TestClient(web.app)

    def tearDown(self):
        config.DB_PATH = self._orig_db_path

    def test_shifts_page_without_proposed_batch_es(self):
        # a shift worker whose shifts have not arrived yet (ruling BODY-TURNOS-OPCIONAL-1)
        db.set_trabaja_turnos(self.conn, True, 1)
        page = self.client.get("/shifts").text
        self.assertIn("Turnos", page)
        # no shifts and no watch data at all -- an honest empty state, not the removed "soon" placeholder
        self.assertIn(T("es")("shifts.reason.sin_datos_reloj"), page)

    def test_shifts_page_with_proposed_batch_both_languages(self):
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        page = self.client.get("/shifts").text
        self.assertIn("Turnos nuevos", page)
        self.assertIn("Confirmar", page)
        self.assertIn("Descartar", page)
        self.client.post("/settings/language", data={"lang": "en"})
        page_en = self.client.get("/shifts").text
        self.assertIn("New shifts", page_en)
        self.assertIn("Confirm", page_en)
        self.assertIn("Discard", page_en)

    def test_shifts_page_shows_kept_note_for_manual_day(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, start_local, end_local, source) "
            "VALUES (1, '2026-11-09', 'night', '2026-11-09T23:00', '2026-11-10T07:00', 'manual')"
        )
        self.conn.commit()
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        page = self.client.get("/shifts").text
        self.assertIn("se mantiene lo que tú cambiaste", page)

    def test_today_page_links_to_shifts_when_batch_proposed(self):
        from body import garmin_adapter
        self.conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value) VALUES (1, '2026-11-05', 'steps', 100)"
        )
        self.conn.commit()
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        page = self.client.get("/").text
        self.assertIn("Llegaron tus turnos", page)
        self.assertNotIn("Aún no hay turnos", page)

    def test_shifts_page_with_three_proposed_batches_both_languages(self):
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        shift_import.propose(self.conn, FIXTURES / "turnos-demo-b.json", 1)
        c_path = _write({
            "source": {"mail_subject": "T", "received": "2026-12-01T12:00:00-05:00", "attachment": "t.xlsx",
                       "extracted_at": "2026-12-01T12:05:00-05:00"},
            "person": "carlos", "timezone": "America/Bogota",
            "shifts": [{"day": "2026-12-05", "kind": "night", "code": "N",
                        "start_local": "2026-12-05T22:00", "end_local": "2026-12-06T06:00",
                        "holiday": False, "sunday": False, "note": ""}],
        }, "turnos-three-c.json")
        shift_import.propose(self.conn, c_path, 1)
        page = self.client.get("/shifts").text
        self.assertIn("Confirmar todos", page)
        self.assertIn("Ver archivo por archivo", page)
        self.assertIn("Noviembre 2026", page)
        self.assertIn("Diciembre 2026", page)
        self.assertIn("6 días, 3 noches", page)
        self.client.post("/settings/language", data={"lang": "en"})
        page_en = self.client.get("/shifts").text
        self.assertIn("Confirm all", page_en)
        self.assertIn("See file by file", page_en)
        self.assertIn("November 2026", page_en)
        self.assertIn("December 2026", page_en)
        self.assertIn("6 days, 3 nights", page_en)

    def test_post_confirm_all(self):
        a_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        b_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-b.json", 1)
        resp = self.client.post("/shifts/confirm-all")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Confirmaste 2 archivos", resp.text)
        status_a = self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (a_id,)).fetchone()["status"]
        status_b = self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (b_id,)).fetchone()["status"]
        self.assertEqual(status_a, "confirmed")
        self.assertEqual(status_b, "confirmed")

    def test_confirm_and_dismiss_posts(self):
        a_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-a.json", 1)
        b_id = shift_import.propose(self.conn, FIXTURES / "turnos-demo-b.json", 1)
        resp = self.client.post("/shifts/confirm", data={"batch_id": str(a_id)})
        self.assertEqual(resp.status_code, 200)
        status_a = self.conn.execute(
            "SELECT status FROM shift_batch WHERE id = ?", (a_id,)
        ).fetchone()["status"]
        self.assertEqual(status_a, "confirmed")
        resp2 = self.client.post("/shifts/dismiss", data={"batch_id": str(b_id)})
        self.assertEqual(resp2.status_code, 200)
        status_b = self.conn.execute(
            "SELECT status FROM shift_batch WHERE id = ?", (b_id,)
        ).fetchone()["status"]
        self.assertEqual(status_b, "dismissed")


class ShiftImportCliTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("BODY_SHIFTS_DIR", None)

    def test_cli_no_folder_exits_cleanly(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shift_import.main([])
        self.assertIn("no shifts folder configured", buf.getvalue())

    def test_cli_proposes_files_in_received_order_and_ignores_sidecars(self):
        folder = Path(tempfile.mkdtemp(prefix="body-shift-cli-"))
        (folder / "turnos-2026-11_2027-01.json").write_text(
            (FIXTURES / "turnos-demo-a.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (folder / "turnos-2026-12_2027-02.json").write_text(
            (FIXTURES / "turnos-demo-b.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (folder / "codigos.json").write_text(json.dumps({"N": "night"}), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            shift_import.main(["--dir", str(folder), "--person-id", "1"])
        out = buf.getvalue()
        self.assertIn("files read: 2", out)
        self.assertIn("batches proposed: 2", out)


class InstallScriptTest(unittest.TestCase):
    """The installer is never executed here; only its text is checked (BODY-TURNOS-SYNC-1)."""

    def test_shiftsdir_param_used_only_inside_its_own_branch_and_no_personal_path(self):
        text = (Path(__file__).parent.parent / "scripts" / "install-daily-sync.ps1").read_text(encoding="utf-8")
        self.assertIn("$ShiftsDir", text)
        branch_start = text.index("if ($ShiftsDir)")
        branch_end = text.index("}", branch_start)
        branch = text[branch_start:branch_end]
        self.assertIn("body.vivo shift_import", branch)
        outside = text[:branch_start] + text[branch_end:]
        self.assertNotIn("body.vivo shift_import", outside)
        self.assertNotIn("Users", text)
        self.assertNotIn("Carlos", text)


if __name__ == "__main__":
    unittest.main()
