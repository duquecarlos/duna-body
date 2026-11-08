"""BODY-TURNOS-OPCIONAL-5: a person who works shifts but has no file source can type a repeating
pattern (`shift_pattern`), and Body proposes the coming weeks through the same propose-then-confirm
flow a file import uses (`body/patron_turnos.py`, `GET/POST /shifts/patron`). Invented demo data
only, no real schedules.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, patron_turnos, shift_import, web
from body.i18n import T

NIGHT_OFF_CYCLE = [
    {"kind": "night", "start": "22:00", "end": "06:00"},
    {"kind": "off", "start": None, "end": None},
]


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _fresh_conn(case: unittest.TestCase, name: str = "a.db"):
    orig_db_path = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    return conn


# ============================================================================================
# patron_turnos: validation
# ============================================================================================

class ValidarRefusalsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_conn(self)

    def test_unknown_kind(self):
        with self.assertRaises(patron_turnos.PatronInvalido) as cm:
            patron_turnos.validar([{"kind": "siesta", "start": None, "end": None}])
        self.assertEqual(cm.exception.errors, [{"index": 0, "code": "tipo_desconocido"}])

    def test_working_day_missing_time(self):
        with self.assertRaises(patron_turnos.PatronInvalido) as cm:
            patron_turnos.validar([{"kind": "morning", "start": None, "end": "14:00"}])
        self.assertEqual(cm.exception.errors, [{"index": 0, "code": "hora_invalida"}])

    def test_working_day_malformed_time(self):
        with self.assertRaises(patron_turnos.PatronInvalido) as cm:
            patron_turnos.validar([{"kind": "morning", "start": "6am", "end": "14:00"}])
        self.assertEqual(cm.exception.errors, [{"index": 0, "code": "hora_invalida"}])

    def test_same_start_and_end(self):
        with self.assertRaises(patron_turnos.PatronInvalido) as cm:
            patron_turnos.validar([{"kind": "morning", "start": "06:00", "end": "06:00"}])
        self.assertEqual(cm.exception.errors, [{"index": 0, "code": "misma_hora"}])

    def test_off_day_with_a_time(self):
        with self.assertRaises(patron_turnos.PatronInvalido) as cm:
            patron_turnos.validar([{"kind": "off", "start": "06:00", "end": None}])
        self.assertEqual(cm.exception.errors, [{"index": 0, "code": "descanso_con_hora"}])

    def test_crossing_midnight_is_valid(self):
        patron_turnos.validar([{"kind": "night", "start": "22:00", "end": "06:00"}])  # no raise

    def test_cycle_too_long_refused(self):
        with self.assertRaises(patron_turnos.PatronInvalido) as cm:
            patron_turnos.validar([{"kind": "off", "start": None, "end": None}] * 57)
        self.assertEqual(cm.exception.errors, [{"index": None, "code": "largo_invalido"}])

    def test_cycle_empty_refused(self):
        with self.assertRaises(patron_turnos.PatronInvalido):
            patron_turnos.validar([])

    def test_exception_message_never_carries_a_date_or_a_time(self):
        with self.assertRaises(patron_turnos.PatronInvalido) as cm:
            patron_turnos.validar([{"kind": "morning", "start": "06:00", "end": "06:00"}])
        self.assertNotIn(":", str(cm.exception))
        self.assertNotIn("-", str(cm.exception))

    def test_multiple_bad_rows_all_reported(self):
        with self.assertRaises(patron_turnos.PatronInvalido) as cm:
            patron_turnos.validar([
                {"kind": "siesta", "start": None, "end": None},
                {"kind": "off", "start": None, "end": None},
                {"kind": "morning", "start": "06:00", "end": "06:00"},
            ])
        self.assertEqual(
            cm.exception.errors,
            [{"index": 0, "code": "tipo_desconocido"}, {"index": 2, "code": "misma_hora"}],
        )


# ============================================================================================
# patron_turnos: expandir
# ============================================================================================

class ExpandirTest(unittest.TestCase):
    def test_night_crosses_midnight(self):
        pattern = {"start_day": "2026-01-01", "cycle": NIGHT_OFF_CYCLE, "horizon_weeks": 1}
        shifts = patron_turnos.expandir(pattern, date(2026, 1, 1))
        day0 = shifts[0]
        self.assertEqual(day0["day"], "2026-01-01")
        self.assertEqual(day0["kind"], "night")
        self.assertEqual(day0["start_local"], "2026-01-01T22:00")
        self.assertEqual(day0["end_local"], "2026-01-02T06:00")  # the next day
        self.assertIsNone(day0["code"])
        day1 = shifts[1]
        self.assertEqual(day1["kind"], "off")
        self.assertIsNone(day1["start_local"])
        self.assertIsNone(day1["end_local"])

    def test_never_emits_a_day_before_hoy_even_when_start_day_is_in_the_past(self):
        pattern = {"start_day": "2020-01-01", "cycle": NIGHT_OFF_CYCLE, "horizon_weeks": 1}
        hoy = date(2026, 6, 15)
        shifts = patron_turnos.expandir(pattern, hoy)
        self.assertEqual(shifts[0]["day"], hoy.isoformat())
        self.assertTrue(all(date.fromisoformat(s["day"]) >= hoy for s in shifts))

    def test_index_wraps(self):
        cycle = [
            {"kind": "morning", "start": "06:00", "end": "14:00"},
            {"kind": "off", "start": None, "end": None},
            {"kind": "afternoon", "start": "14:00", "end": "22:00"},
        ]
        pattern = {"start_day": "2026-01-01", "cycle": cycle, "horizon_weeks": 1}
        shifts = patron_turnos.expandir(pattern, date(2026, 1, 1))
        # day offset 3 wraps back to index 0 (morning again)
        self.assertEqual(shifts[3]["kind"], "morning")
        self.assertEqual(shifts[3]["day"], "2026-01-04")

    def test_horizon_length_exact(self):
        pattern = {"start_day": "2026-01-01", "cycle": NIGHT_OFF_CYCLE, "horizon_weeks": 3}
        shifts = patron_turnos.expandir(pattern, date(2026, 1, 1))
        self.assertEqual(len(shifts), 21)

    def test_anchor_is_start_day_not_the_effective_start(self):
        # start_day is a Thursday-equivalent offset 2 into a 4-day cycle; expanding from 3 days later
        # must keep the same cycle phase as if it had started counting from start_day.
        cycle = [{"kind": "morning", "start": "06:00", "end": "14:00"},
                 {"kind": "afternoon", "start": "14:00", "end": "22:00"},
                 {"kind": "night", "start": "22:00", "end": "06:00"},
                 {"kind": "off", "start": None, "end": None}]
        pattern = {"start_day": "2026-01-01", "cycle": cycle, "horizon_weeks": 1}
        full = patron_turnos.expandir(pattern, date(2026, 1, 1))
        later = patron_turnos.expandir(pattern, date(2026, 1, 4))
        # day 2026-01-04 is offset 3 in both expansions -- same kind either way
        by_day_full = {s["day"]: s["kind"] for s in full}
        by_day_later = {s["day"]: s["kind"] for s in later}
        self.assertEqual(by_day_full["2026-01-04"], by_day_later["2026-01-04"])


# ============================================================================================
# propose_data: kind
# ============================================================================================

class ProposeDataKindTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_conn(self)

    def _data(self):
        return {"source": {"received": "2026-01-01", "extracted_at": "2026-01-01T08:00:00"},
                "shifts": [{"day": "2026-01-01", "kind": "off"}]}

    def test_default_kind_is_import(self):
        bid = shift_import.propose_data(self.conn, self._data(), "f.json", 1)
        row = self.conn.execute("SELECT kind FROM shift_batch WHERE id = ?", (bid,)).fetchone()
        self.assertEqual(row["kind"], "import")

    def test_unknown_kind_refused_writes_nothing(self):
        with self.assertRaises(shift_import.ImportRefused):
            shift_import.propose_data(self.conn, self._data(), "f.json", 1, kind="bogus")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift_batch").fetchone()[0], 0)

    def test_pattern_kind_is_written(self):
        bid = shift_import.propose_data(self.conn, self._data(), "f.json", 1, kind="pattern")
        row = self.conn.execute("SELECT kind FROM shift_batch WHERE id = ?", (bid,)).fetchone()
        self.assertEqual(row["kind"], "pattern")


# ============================================================================================
# guardar / proponer / confirm flow
# ============================================================================================

class GuardarYProponerTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_conn(self)

    def test_guardar_then_proponer_creates_one_proposed_pattern_batch(self):
        patron_turnos.guardar(self.conn, 1, "2026-01-01", NIGHT_OFF_CYCLE, 1)
        bid = patron_turnos.proponer(self.conn, 1, date(2026, 1, 1))
        row = self.conn.execute("SELECT status, kind FROM shift_batch WHERE id = ?", (bid,)).fetchone()
        self.assertEqual(row["status"], "proposed")
        self.assertEqual(row["kind"], "pattern")

    def test_resave_dismisses_earlier_pattern_proposal_leaves_import_untouched(self):
        import_bid = shift_import.propose(self.conn, Path(__file__).parent / "fixtures" / "turnos-demo-a.json", 1)
        patron_turnos.guardar(self.conn, 1, "2026-01-01", NIGHT_OFF_CYCLE, 1)
        bid1 = patron_turnos.proponer(self.conn, 1, date(2026, 1, 1))
        patron_turnos.guardar(self.conn, 1, "2026-01-01", NIGHT_OFF_CYCLE, 1)
        bid2 = patron_turnos.proponer(self.conn, 1, date(2026, 1, 2))
        self.assertNotEqual(bid1, bid2)
        self.assertEqual(
            self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (bid1,)).fetchone()["status"],
            "dismissed",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (bid2,)).fetchone()["status"],
            "proposed",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (import_bid,)).fetchone()["status"],
            "proposed",
        )

    def test_confirm_pattern_rows_carry_patron_source(self):
        patron_turnos.guardar(self.conn, 1, "2026-01-01", NIGHT_OFF_CYCLE, 1)
        bid = patron_turnos.proponer(self.conn, 1, date(2026, 1, 1))
        shift_import.confirm(self.conn, bid)
        row = self.conn.execute(
            "SELECT source, kind FROM shift WHERE person_id = 1 AND day = '2026-01-01'"
        ).fetchone()
        self.assertEqual(row["source"], "patron:patron-manual")
        self.assertEqual(row["kind"], "night")

    def test_manual_row_on_a_covered_day_is_kept(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, source) VALUES (1, '2026-01-01', 'day', 'manual')"
        )
        self.conn.commit()
        patron_turnos.guardar(self.conn, 1, "2026-01-01", NIGHT_OFF_CYCLE, 1)
        bid = patron_turnos.proponer(self.conn, 1, date(2026, 1, 1))
        shift_import.confirm(self.conn, bid)
        row = self.conn.execute(
            "SELECT source, kind FROM shift WHERE person_id = 1 AND day = '2026-01-01'"
        ).fetchone()
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["kind"], "day")

    def test_pattern_confirmed_after_import_wins_on_overlap(self):
        data_import = {"source": {"received": "2026-04-01", "extracted_at": "2026-04-01T08:00:00"},
                       "shifts": [{"day": "2026-04-05", "kind": "morning", "start_local": "2026-04-05T06:00",
                                   "end_local": "2026-04-05T14:00"}]}
        import_bid = shift_import.propose_data(self.conn, data_import, "archivo1.json", 1)
        shift_import.confirm(self.conn, import_bid)
        patron_turnos.guardar(self.conn, 1, "2026-04-05", NIGHT_OFF_CYCLE, 1)
        pattern_bid = patron_turnos.proponer(self.conn, 1, date(2026, 4, 5))  # received 2026-04-05 > 2026-04-01
        shift_import.confirm(self.conn, pattern_bid)
        row = self.conn.execute(
            "SELECT source, kind FROM shift WHERE person_id = 1 AND day = '2026-04-05'"
        ).fetchone()
        self.assertEqual(row["source"], "patron:patron-manual")
        self.assertEqual(row["kind"], "night")

    def test_import_confirmed_after_pattern_wins_on_overlap(self):
        patron_turnos.guardar(self.conn, 1, "2026-03-01", NIGHT_OFF_CYCLE, 1)
        pattern_bid = patron_turnos.proponer(self.conn, 1, date(2026, 3, 1))  # received 2026-03-01
        shift_import.confirm(self.conn, pattern_bid)
        data_import = {"source": {"received": "2026-03-02", "extracted_at": "2026-03-02T09:00:00"},
                       "shifts": [{"day": "2026-03-03", "kind": "morning", "start_local": "2026-03-03T06:00",
                                   "end_local": "2026-03-03T14:00"}]}
        import_bid = shift_import.propose_data(self.conn, data_import, "archivo2.json", 1)  # 2026-03-02 > 2026-03-01
        shift_import.confirm(self.conn, import_bid)
        row = self.conn.execute(
            "SELECT source, kind FROM shift WHERE person_id = 1 AND day = '2026-03-03'"
        ).fetchone()
        self.assertEqual(row["source"], "personal:archivo2.json")
        self.assertEqual(row["kind"], "morning")

    def test_quitar_leaves_confirmed_rows(self):
        patron_turnos.guardar(self.conn, 1, "2026-01-01", NIGHT_OFF_CYCLE, 1)
        bid = patron_turnos.proponer(self.conn, 1, date(2026, 1, 1))
        shift_import.confirm(self.conn, bid)
        patron_turnos.quitar(self.conn, 1)
        self.assertIsNone(patron_turnos.leer(self.conn, 1))
        row = self.conn.execute(
            "SELECT source FROM shift WHERE person_id = 1 AND day = '2026-01-01'"
        ).fetchone()
        self.assertEqual(row["source"], "patron:patron-manual")


# ============================================================================================
# Web: /shifts/patron
# ============================================================================================

class GateOffTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.conn = _fresh_conn(self)
        self.addCleanup(web.app.dependency_overrides.clear)
        _as(1)

    def test_get_404_when_setting_off(self):
        self.assertEqual(self.client.get("/shifts/patron").status_code, 404)

    def test_post_save_404_when_setting_off(self):
        self.assertEqual(self.client.post("/shifts/patron", data={"dias": "1"}).status_code, 404)

    def test_post_quitar_404_when_setting_off(self):
        self.assertEqual(self.client.post("/shifts/patron/quitar").status_code, 404)


class FormRenderTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def setUp(self):
        self.conn = _fresh_conn(self)
        db.set_trabaja_turnos(self.conn, True, 1)
        self.conn.commit()
        self.addCleanup(web.app.dependency_overrides.clear)
        _as(1)

    def test_renders_requested_row_count(self):
        page = self.client.get("/shifts/patron?dias=5").text
        self.assertEqual(page.count('name="kind_'), 5)

    def test_default_row_count_is_seven(self):
        page = self.client.get("/shifts/patron").text
        self.assertEqual(page.count('name="kind_'), 7)

    def test_bad_row_rerenders_with_message_and_input_kept(self):
        data = {"dias": "2", "start_day": "2026-01-01", "horizon_weeks": "4",
                "kind_0": "morning", "start_0": "", "end_0": "",
                "kind_1": "off", "start_1": "", "end_1": ""}
        resp = self.client.post("/shifts/patron", data=data)
        self.assertEqual(resp.status_code, 200)
        page = resp.text
        self.assertIn(T("es")("patron.error.hora_invalida"), page)
        self.assertIn('value="2026-01-01"', page)
        self.assertIn('value="morning" selected', page)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM shift_pattern").fetchone()[0], 0,
        )

    def test_save_redirects_and_shifts_shows_pattern_card_es(self):
        data = {"dias": "2", "start_day": date.today().isoformat(), "horizon_weeks": "1",
                "kind_0": "night", "start_0": "22:00", "end_0": "06:00",
                "kind_1": "off", "start_1": "", "end_1": ""}
        resp = self.client.post("/shifts/patron", data=data)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/shifts")
        page = self.client.get("/shifts").text
        self.assertIn("Tu patrón:", page)

    def test_save_redirects_and_shifts_shows_pattern_card_en(self):
        db.set_language(self.conn, "en", 1)
        self.conn.commit()
        data = {"dias": "2", "start_day": date.today().isoformat(), "horizon_weeks": "1",
                "kind_0": "night", "start_0": "22:00", "end_0": "06:00",
                "kind_1": "off", "start_1": "", "end_1": ""}
        resp = self.client.post("/shifts/patron", data=data)
        self.assertEqual(resp.status_code, 303)
        page = self.client.get("/shifts").text
        self.assertIn("Your pattern:", page)

    def test_door_wording_before_and_after_saving(self):
        before = self.client.get("/shifts").text
        self.assertIn(T("es")("patron.door_new"), before)
        self.assertNotIn(T("es")("patron.door_change"), before)
        data = {"dias": "1", "start_day": date.today().isoformat(), "horizon_weeks": "1",
                "kind_0": "off", "start_0": "", "end_0": ""}
        self.client.post("/shifts/patron", data=data)
        after = self.client.get("/shifts").text
        self.assertIn(T("es")("patron.door_change"), after)

    def test_quitar_dismisses_proposed_batch_but_keeps_confirmed_rows(self):
        patron_turnos.guardar(self.conn, 1, "2026-01-01", NIGHT_OFF_CYCLE, 1)
        bid = patron_turnos.proponer(self.conn, 1, date(2026, 1, 1))
        shift_import.confirm(self.conn, bid)
        patron_turnos.guardar(self.conn, 1, "2026-01-02", NIGHT_OFF_CYCLE, 1)
        bid2 = patron_turnos.proponer(self.conn, 1, date(2026, 1, 2))
        resp = self.client.post("/shifts/patron/quitar")
        self.assertEqual(resp.status_code, 303)
        self.assertIsNone(patron_turnos.leer(self.conn, 1))
        self.assertEqual(
            self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (bid2,)).fetchone()["status"],
            "dismissed",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM shift WHERE person_id = 1 AND day = '2026-01-01'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
