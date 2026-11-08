"""BODY-TURNOS-OPCIONAL-3: Turnos disappears for a person who has not said they work shifts, slice 3 of
BODY-TURNOS-OPCIONAL-OBS-1's plan under Carlos's ruling A (orchestrator ledger BODY-TURNOS-OPCIONAL-1:
"a screen whose data cannot arrive for this person is not shown to them"). Every shift surface the
census found is gated on db.trabaja_turnos_si, whose rule BODY-TURNOS-INFERIR-1 fixed: stored 1 shows,
stored 0 hides, and stored NULL (not answered) follows whether the person already has shift data --
shown if they do (read-time inference, since a fresh database never runs the migration's backfill),
hidden if they do not. Four settings below: NULL/False/True share the SAME stored shift rows (so
hiding or showing is proved to come from the setting, never from missing data), and a fourth NULL
case has no shift rows at all (so inference has nothing to infer from, and it hides). Invented demo
data only, no real health figures.
"""
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, tutoriales, web
from body.agente import herramientas
from body.rutas import comun

PERSON_ID = 1
CONST = {"sleep_minutes": 420.0, "resting_hr": 55.0, "hrv_last_night": 60.0, "stress_avg": 25.0,
         "body_battery_wake": 75.0}


def _insert_shift(conn, person_id: int, day: str, kind: str = "night", start_local: str | None = None,
                  end_local: str | None = None) -> None:
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, end_local, source) VALUES (?, ?, ?, ?, ?, 'manual')",
        (person_id, day, kind, start_local, end_local),
    )


def _fill_range(conn, person_id: int, start: date, end: date, values: dict[str, float]) -> None:
    d = start
    while d <= end:
        for m, v in values.items():
            db.set_metric(conn, d.isoformat(), m, v, "garmin", person_id)
        d += timedelta(1)


def _seed(conn, person_id: int, hoy: date, with_shifts: bool = True) -> date:
    """A night today, a 5-night block 20 days back, and 140 days of steady metrics -- enough for Hoy's
    shift card, Día's block-position line, Historial's por-tipo card and Tendencias' night shading/
    cards to all have something to show when the setting is True, proving a False/NULL setting hides
    them rather than their data being absent. with_shifts=False seeds metrics but no shift rows at
    all, for the NULL case where there is no shift data to infer from. Returns the block's first day."""
    _fill_range(conn, person_id, hoy - timedelta(140), hoy - timedelta(1), CONST)
    db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", person_id)
    bloque_inicio = hoy - timedelta(20)
    if with_shifts:
        _insert_shift(conn, person_id, hoy.isoformat(), kind="night",
                      start_local=f"{hoy.isoformat()}T22:00:00",
                      end_local=f"{(hoy + timedelta(1)).isoformat()}T06:00:00")
        for i in range(5):
            _insert_shift(conn, person_id, (bloque_inicio + timedelta(i)).isoformat())
    conn.commit()
    return bloque_inicio


class _SettingChecks:
    """A mixin, never a TestCase itself (it would otherwise be collected and run an extra time with
    SETTING=None, duplicating NullSettingTest) -- subclasses below pair it with unittest.TestCase,
    each with a different db.trabaja_turnos setting: SETTING is None (not answered), False (no) or
    True (si). WITH_SHIFTS controls whether shift rows are seeded at all; it is True for every
    subclass except the NULL-with-no-shift-data case, so that case's self.on comes out False with
    nothing to infer from."""
    SETTING: bool | None = None
    WITH_SHIFTS: bool = True

    def setUp(self):
        self.addCleanup(tutoriales._manifest_cache.clear)
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "a.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.hoy = date(2026, 6, 20)
        self.bloque_inicio = _seed(self.conn, PERSON_ID, self.hoy, with_shifts=self.WITH_SHIFTS)
        if self.SETTING is not None:
            db.set_trabaja_turnos(self.conn, self.SETTING, PERSON_ID)
        self.on = db.trabaja_turnos_si(self.conn, PERSON_ID)

        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: PERSON_ID
        self.client = TestClient(web.app, follow_redirects=False)

        patcher = mock.patch.object(comun, "_today_local", return_value=self.hoy)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(herramientas._TURNOS_ACTUAL.set, True)  # back to the default for later tests

    # ---- the nav's Turnos tab, present on every page --------------------------------------------

    def test_nav_shifts_link(self):
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        if self.on:
            self.assertIn('href="/shifts"', r.text)
        else:
            self.assertNotIn('href="/shifts"', r.text)

    # ---- Hoy's shift card -------------------------------------------------------------------------

    def test_hoy_shift_card(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        if self.on:
            self.assertIn('class="shift', r.text)
        else:
            self.assertNotIn('class="shift', r.text)

    # ---- Día's «Tus turnos» block and the block-position line -------------------------------------

    def test_dia_shift_block(self):
        dia = self.bloque_inicio + timedelta(2)
        r = self.client.get(f"/day/{dia.isoformat()}")
        self.assertEqual(r.status_code, 200)
        if self.on:
            self.assertIn("Noche 3 de 5", r.text)
            self.assertIn('class="shift', r.text)
        else:
            self.assertNotIn("Noche", r.text)
            self.assertNotIn('class="shift', r.text)

    # ---- Historial's «Tus turnos por tipo» card ----------------------------------------------------

    def test_history_shift_card(self):
        r = self.client.get("/history")
        self.assertEqual(r.status_code, 200)
        if self.on:
            self.assertIn('class="shift', r.text)
        else:
            self.assertNotIn('class="shift', r.text)
            self.assertNotIn('href="/shifts"', r.text)

    # ---- Tendencias' night-shift shading and its legend --------------------------------------------

    def test_trends_night_shading_and_legend(self):
        r = self.client.get("/trends?r=90")
        self.assertEqual(r.status_code, 200)
        if self.on:
            self.assertIn('class="c-night op-18"', r.text)
        else:
            self.assertNotIn('class="c-night op-18"', r.text)
            self.assertNotIn('href="/shifts"', r.text)

    # ---- /shifts and /shifts/como ------------------------------------------------------------------

    def test_shifts_route(self):
        r = self.client.get("/shifts")
        self.assertEqual(r.status_code, 200 if self.on else 404)

    def test_shifts_como_route(self):
        r = self.client.get("/shifts/como")
        self.assertEqual(r.status_code, 200 if self.on else 404)

    # ---- Ayuda's turnos chapter --------------------------------------------------------------------

    def test_ayuda_turnos_chapter(self):
        r = self.client.get("/ayuda")
        self.assertEqual(r.status_code, 200)
        titulo = next(c["titulo"] for c in tutoriales.catalogo()["capitulos"] if c["id"] == "turnos")
        if self.on:
            self.assertIn(titulo, r.text)
        else:
            self.assertNotIn(titulo, r.text)

    # ---- the assistant's tool list and the history tool's por_turno --------------------------------

    def test_assistant_tool_list(self):
        herramientas.fijar_turnos_actual(self.on)
        names = {h["nombre"] for h in herramientas.disponibles("completo")}
        if self.on:
            self.assertTrue(herramientas.TURNOS_HERRAMIENTAS <= names)
        else:
            self.assertFalse(names & herramientas.TURNOS_HERRAMIENTAS)

    def test_history_tool_por_turno(self):
        out = herramientas.ejecutar(self.conn, PERSON_ID, "leer_historia", {}, "resumen")
        if self.on:
            self.assertIn("por_turno", out)
        else:
            self.assertNotIn("por_turno", out)


class NullSettingTest(_SettingChecks, unittest.TestCase):
    """NULL with the shared shift rows present: inferred as True under BODY-TURNOS-INFERIR-1, so
    this shows exactly what SiSettingTest shows."""
    SETTING = None


class NullSettingNoShiftsTest(_SettingChecks, unittest.TestCase):
    """NULL with no shift rows at all: nothing to infer from, so this hides everything, same as
    NoSettingTest."""
    SETTING = None
    WITH_SHIFTS = False


class NoSettingTest(_SettingChecks, unittest.TestCase):
    SETTING = False


class SiSettingTest(_SettingChecks, unittest.TestCase):
    SETTING = True


class TabsBarSharesWidthTest(unittest.TestCase):
    """BODY-TURNOS-OPCIONAL-3-M: the phone bottom bar must share its width among however many tabs
    it has, not fix four columns -- with Turnos hidden, three tabs must not sit bunched in the left
    three quarters with an empty slot on the right. Regression pin; the visual check at 375 px is
    the orchestrator's own measurement."""

    def test_tabs_rule_has_no_fixed_four_column_grid(self):
        css = Path("body/static/body.css").read_text(encoding="utf-8")
        m = re.search(r"\.tabs\{[^}]*\}", css)
        self.assertIsNotNone(m, ".tabs rule not found in body.css")
        self.assertNotIn("repeat(4", m.group(0))


if __name__ == "__main__":
    unittest.main()
