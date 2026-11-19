"""ORCH-CARLOS-DELEGATES-TIER-1-1 rulings 1-2 (BODY-TURNOS-DESHACER-LOTE-3-R): /shifts' own
history of the person's last 10 non-proposed shift batches, its Deshacer disclosure, and the new
POST /shifts/undo route that wires body/shift_import.py's own `undo()` (BODY-TURNOS-DESHACER-
LOTE-2) to the person's tap. Invented demo data only, no network.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, shift_import, web
from body.i18n import T
from body.rutas import turnos

PERSON_ID = 1


def _insert_batch(conn, person_id: int, file_name: str, received: str, status: str, kind: str = "import") -> int:
    cur = conn.execute(
        "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, day_count, created_at, "
        "kind) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (person_id, file_name, received, received, status, received, kind),
    )
    return cur.lastrowid


def _insert_shift_row(conn, person_id: int, day: str, batch_id: int, kind: str = "night") -> None:
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, end_local, source, batch_id) "
        "VALUES (?, ?, ?, ?, ?, 'personal:x.json', ?)",
        (person_id, day, kind, f"{day}T22:00:00", f"{day}T22:00:00", batch_id),
    )


class _WebCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP, prefix="body-turnos-deshacer-")) / "test.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        db.set_trabaja_turnos(self.conn, True, PERSON_ID)
        self.client = TestClient(web.app)

    def tearDown(self):
        config.DB_PATH = self._orig_db_path


class HistorialHiddenTest(_WebCase):
    def test_hidden_when_the_person_has_no_non_proposed_batch(self):
        page = self.client.get("/shifts").text
        self.assertNotIn(T("es")("shifts.historial.deshacer"), page)


class HistorialRenderTest(_WebCase):
    def test_shows_states_counts_origin_newest_first_and_hides_dismissed(self):
        confirmado_id = _insert_batch(self.conn, PERSON_ID, "turnos-nov.json", "2026-11-05T12:00:00-05:00",
                                      "confirmed")
        _insert_shift_row(self.conn, PERSON_ID, "2026-11-05", confirmado_id)
        _insert_shift_row(self.conn, PERSON_ID, "2026-11-06", confirmado_id)
        _insert_batch(self.conn, PERSON_ID, "turnos-oct.json", "2026-10-01T12:00:00-05:00", "superseded")
        _insert_batch(self.conn, PERSON_ID, "turnos-patron.json", "2026-10-15T12:00:00-05:00", "undone",
                      kind="pattern")
        _insert_batch(self.conn, PERSON_ID, "turnos-descartado.json", "2026-10-20T12:00:00-05:00", "dismissed")
        self.conn.commit()

        page = self.client.get("/shifts").text
        self.assertIn(T("es")("shifts.historial.estado.confirmed"), page)
        self.assertIn(T("es")("shifts.historial.estado.superseded"), page)
        self.assertIn(T("es")("shifts.historial.estado.undone"), page)
        self.assertIn("Patrón", page)
        self.assertIn("2 turnos", page)
        self.assertNotIn("turnos-descartado.json", page)
        # newest `received` first: the November batch's own file name reads before October's.
        self.assertLess(page.index("turnos-nov.json"), page.index("turnos-oct.json"))

        self.client.post("/settings/language", data={"lang": "en"})
        try:
            page_en = self.client.get("/shifts").text
            self.assertIn(T("en")("shifts.historial.estado.confirmed"), page_en)
            self.assertIn("Pattern", page_en)
            self.assertIn("2 shifts", page_en)
        finally:
            self.client.post("/settings/language", data={"lang": "es"})

    def test_limited_to_the_last_10(self):
        for i in range(11):
            day = f"2026-01-{i + 1:02d}"
            bid = _insert_batch(self.conn, PERSON_ID, f"turnos-{i}.json", f"{day}T12:00:00-05:00", "confirmed")
            _insert_shift_row(self.conn, PERSON_ID, day, bid)
        self.conn.commit()
        page = self.client.get("/shifts").text
        self.assertNotIn("turnos-0.json", page)  # the oldest of 11: excluded
        for i in range(1, 11):
            self.assertIn(f"turnos-{i}.json", page)

    def test_deshacer_offered_only_on_confirmed_or_superseded_with_at_least_one_current_row(self):
        con_turnos_id = _insert_batch(self.conn, PERSON_ID, "turnos-con.json", "2026-11-01T12:00:00-05:00",
                                      "confirmed")
        _insert_shift_row(self.conn, PERSON_ID, "2026-11-01", con_turnos_id)
        # 'confirmed' but with no current row left (a day another, later batch has since taken --
        # never reached by _mark_superseded's own invariant in real use, but the row's own
        # Deshacer eligibility is read from n, not assumed): no Deshacer.
        _insert_batch(self.conn, PERSON_ID, "turnos-sin.json", "2026-10-01T12:00:00-05:00", "confirmed")
        # 'undone': always 0 current rows (undo() deletes its own batch_id's shift rows): no Deshacer.
        _insert_batch(self.conn, PERSON_ID, "turnos-deshecho.json", "2026-09-01T12:00:00-05:00", "undone")
        self.conn.commit()

        page = self.client.get("/shifts").text
        deshacer = T("es")("shifts.historial.deshacer")
        self.assertEqual(page.count(deshacer), 1)


class UndoRouteTest(_WebCase):
    def _confirm_overlap_pair(self, day1: str, day2: str) -> int:
        """Batch A confirms `day1` alone; batch B (received later) confirms `day1` (overwriting A)
        and `day2` -- the same overlap shape tests/test_shift_import.py's own UndoTest uses, so
        undoing B restores day1 from A (now 'superseded') and empties day2. Returns B's id."""
        data_a = {"source": {"received": f"{day1}T12:00:00-05:00", "extracted_at": f"{day1}T12:00:00-05:00"},
                  "shifts": [{"day": day1, "kind": "night", "start_local": f"{day1}T22:00:00"}]}
        a_id = shift_import.propose_data(self.conn, data_a, f"turnos-a-{day1}.json", PERSON_ID)
        shift_import.confirm(self.conn, a_id)
        data_b = {"source": {"received": f"{day2}T12:00:00-05:00", "extracted_at": f"{day2}T12:00:00-05:00"},
                  "shifts": [{"day": day1, "kind": "night", "start_local": f"{day1}T21:30:00"},
                             {"day": day2, "kind": "day", "start_local": f"{day2}T08:00:00"}]}
        b_id = shift_import.propose_data(self.conn, data_b, f"turnos-b-{day1}.json", PERSON_ID)
        shift_import.confirm(self.conn, b_id)
        self.conn.commit()
        return b_id

    def test_success_restores_the_older_batch_and_shows_the_result_es_and_en(self):
        b_id = self._confirm_overlap_pair("2026-11-01", "2026-11-02")
        resp = self.client.post("/shifts/undo", data={"batch_id": str(b_id)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Importación deshecha: 2 turnos quitados, 1 restaurado.", resp.text)
        self.assertEqual(
            self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (b_id,)).fetchone()[0], "undone")

        self.client.post("/settings/language", data={"lang": "en"})
        try:
            b2_id = self._confirm_overlap_pair("2026-12-01", "2026-12-02")
            resp_en = self.client.post("/shifts/undo", data={"batch_id": str(b2_id)})
            self.assertEqual(resp_en.status_code, 200)
            self.assertIn("Import undone: 2 shifts removed, 1 restored.", resp_en.text)
        finally:
            self.client.post("/settings/language", data={"lang": "es"})

    def test_refused_on_a_proposed_batch_writes_nothing(self):
        data = {"source": {"received": "2026-11-05T12:00:00-05:00", "extracted_at": "2026-11-05T12:00:00-05:00"},
                "shifts": [{"day": "2026-11-05", "kind": "night", "start_local": "2026-11-05T22:00:00"}]}
        batch_id = shift_import.propose_data(self.conn, data, "turnos-proposed.json", PERSON_ID)
        self.conn.commit()

        resp = self.client.post("/shifts/undo", data={"batch_id": str(batch_id)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No se pudo deshacer esta importación.", resp.text)
        self.assertEqual(
            self.conn.execute("SELECT status FROM shift_batch WHERE id = ?", (batch_id,)).fetchone()[0], "proposed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift WHERE person_id = ?",
                                           (PERSON_ID,)).fetchone()[0], 0)

        self.client.post("/settings/language", data={"lang": "en"})
        try:
            resp_en = self.client.post("/shifts/undo", data={"batch_id": str(batch_id)})
            self.assertIn(T("en")("shifts.undo_refused"), resp_en.text.replace("&#39;", "'"))
        finally:
            self.client.post("/settings/language", data={"lang": "es"})

    def test_get_shifts_with_history_present_writes_nothing(self):
        data = {"source": {"received": "2026-11-07T12:00:00-05:00", "extracted_at": "2026-11-07T12:00:00-05:00"},
                "shifts": [{"day": "2026-11-07", "kind": "night", "start_local": "2026-11-07T22:00:00"}]}
        batch_id = shift_import.propose_data(self.conn, data, "turnos-get.json", PERSON_ID)
        shift_import.confirm(self.conn, batch_id)
        self.conn.commit()
        before_shift = [dict(r) for r in self.conn.execute("SELECT * FROM shift")]
        before_batch = [dict(r) for r in self.conn.execute("SELECT * FROM shift_batch")]

        resp = self.client.get("/shifts")

        self.assertEqual(resp.status_code, 200)
        after_shift = [dict(r) for r in self.conn.execute("SELECT * FROM shift")]
        after_batch = [dict(r) for r in self.conn.execute("SELECT * FROM shift_batch")]
        self.assertEqual(before_shift, after_shift)
        self.assertEqual(before_batch, after_batch)


class HistorialCostoTest(_WebCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4: _historial_filas is ONE combined SELECT (a LEFT
    JOIN + GROUP BY), measured directly here -- tests/test_rendimiento.py's own shared /shifts
    ceiling never reaches this code path (its fixture always carries a pending proposed batch,
    which returns before the history is ever computed), so this is this feature's own, named
    measurement rather than a change to that shared table."""

    def test_historial_filas_costs_exactly_one_statement(self):
        _insert_batch(self.conn, PERSON_ID, "turnos-costo.json", "2026-11-01T12:00:00-05:00", "confirmed")
        self.conn.commit()
        contador = {"n": 0}
        self.conn.set_trace_callback(lambda sql: contador.__setitem__("n", contador["n"] + 1))
        try:
            turnos._historial_filas(T("es"), self.conn, PERSON_ID)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(contador["n"], 1)


if __name__ == "__main__":
    unittest.main()
