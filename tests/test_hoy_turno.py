"""Hoy's second promise (BODY-HOY-TURNO-1): today's confirmed shift and the next one, banded like
Turnos' strip, plus a recovery line after a night block -- all read from confirmed `shift` rows and
turnos_costo's own model, nothing stored. Invented shifts, two persons, "today" frozen by patching
body.web._today_local (the calendar-day source every other Hoy/Turnos test already patches)."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, identity, turnos_costo, web
from body.i18n import T
from body.rutas import comun

METRICS = turnos_costo.MODELO["metricas"]
CONST = {"sleep_minutes": 420.0, "resting_hr": 55.0, "hrv_last_night": 60.0, "stress_avg": 25.0,
         "body_battery_wake": 75.0}
OFF_DELTA = {"sleep_minutes": -80.0, "resting_hr": 10.0, "hrv_last_night": -20.0, "stress_avg": 15.0,
             "body_battery_wake": -25.0}

PERSON_A = 1
PERSON_B = 2


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


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


class NextShiftPureTest(unittest.TestCase):
    """turnos_costo.turno_de_hoy directly: 'siguiente' skips off days and stops at 14 days."""

    def test_skips_an_off_day_in_between(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        _insert_shift(conn, PERSON_A, (hoy + timedelta(1)).isoformat(), kind="off")
        _insert_shift(conn, PERSON_A, (hoy + timedelta(2)).isoformat(), kind="night",
                     start_local=f"{(hoy + timedelta(2)).isoformat()}T22:00:00",
                     end_local=f"{(hoy + timedelta(3)).isoformat()}T06:00:00")
        conn.commit()
        turno = turnos_costo.turno_de_hoy(conn, PERSON_A, hoy)
        self.assertIsNotNone(turno["siguiente"])
        self.assertEqual(turno["siguiente"]["kind"], "night")
        self.assertEqual(turno["siguiente"]["start_local"][:10], (hoy + timedelta(2)).isoformat())

    def test_a_shift_within_14_days_is_found(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        _insert_shift(conn, PERSON_A, (hoy + timedelta(14)).isoformat(), kind="day",
                     start_local=f"{(hoy + timedelta(14)).isoformat()}T06:00:00",
                     end_local=f"{(hoy + timedelta(14)).isoformat()}T14:00:00")
        conn.commit()
        turno = turnos_costo.turno_de_hoy(conn, PERSON_A, hoy)
        self.assertIsNotNone(turno["siguiente"])

    def test_a_shift_beyond_14_days_is_not_found(self):
        conn = _mkconn(self)
        hoy = date(2026, 6, 1)
        _insert_shift(conn, PERSON_A, (hoy + timedelta(15)).isoformat(), kind="day",
                     start_local=f"{(hoy + timedelta(15)).isoformat()}T06:00:00",
                     end_local=f"{(hoy + timedelta(15)).isoformat()}T14:00:00")
        conn.commit()
        turno = turnos_costo.turno_de_hoy(conn, PERSON_A, hoy)
        self.assertIsNone(turno["siguiente"])


class RecoveryPureTest(unittest.TestCase):
    """turnos_costo.turno_de_hoy directly: the recovery window, its day count against resumen's
    hero estimate, and the two things that suppress it (recovered already, a new night today)."""

    def _seeded(self, recovery_target: int = 3, blocks: int = 3) -> tuple:
        """`blocks` night blocks, 30 days apart, each shaped to recover on `recovery_target`
        (tests/test_turnos_costo.py's own fixture). With 3+ blocks the first two are far enough
        before the last one that their own recovery has already resolved by the time `turno_de_hoy`
        is asked about the last block -- so the hero median (`resumen`'s `dias_recuperacion`) is
        established from blocks other than the one under test, as it would be in real use."""
        conn = _mkconn(self)
        hoy_base = date(2026, 6, 20)
        _fill_range(conn, PERSON_A, hoy_base - timedelta(140), hoy_base - timedelta(1), CONST)
        fin = None
        inicio = hoy_base - timedelta(100)
        for _ in range(blocks):
            fin = _seed_block(conn, PERSON_A, inicio, 5, recovery_target)
            inicio = fin + timedelta(30)
        conn.commit()
        return conn, fin

    def test_recovery_present_day_1_and_2_of_3(self):
        conn, fin = self._seeded(recovery_target=3)
        turno1 = turnos_costo.turno_de_hoy(conn, PERSON_A, fin + timedelta(1))
        self.assertEqual(turno1["recuperacion"], {"dia": 1, "estimado": 3})
        turno2 = turnos_costo.turno_de_hoy(conn, PERSON_A, fin + timedelta(2))
        self.assertEqual(turno2["recuperacion"], {"dia": 2, "estimado": 3})

    def test_recovery_absent_once_past_the_estimate(self):
        conn, fin = self._seeded(recovery_target=3)
        turno = turnos_costo.turno_de_hoy(conn, PERSON_A, fin + timedelta(4))
        self.assertIsNone(turno["recuperacion"])

    def test_recovery_absent_when_a_night_is_today(self):
        conn, fin = self._seeded(recovery_target=3)
        dia = fin + timedelta(1)
        _insert_shift(conn, PERSON_A, dia.isoformat(), kind="night")
        conn.commit()
        turno = turnos_costo.turno_de_hoy(conn, PERSON_A, dia)
        self.assertIsNone(turno["recuperacion"])

    def test_recovery_absent_with_fewer_than_min_bloques(self):
        conn, fin = self._seeded(recovery_target=3, blocks=1)
        turno = turnos_costo.turno_de_hoy(conn, PERSON_A, fin + timedelta(1))
        self.assertIsNone(turno["recuperacion"])


class WebRenderTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
        db.set_trabaja_turnos(conn, True, PERSON_A)
        conn.commit()
        return conn

    def test_night_today_shows_word_hours_and_band_class(self):
        conn = self._reset_db("night.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        _insert_shift(conn, PERSON_A, hoy.isoformat(), kind="night",
                     start_local=f"{hoy.isoformat()}T22:00:00", end_local=f"{(hoy + timedelta(1)).isoformat()}T06:00:00")
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="shift night"', r.text)
        self.assertIn(_t("es", "today.shift_today_hours", turno=_t("es", "shift.kind.night"),
                        inicio="22:00", fin="06:00"), r.text)

    def test_off_day_reads_descanso(self):
        conn = self._reset_db("off.db")
        hoy = date(2026, 6, 11)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        _insert_shift(conn, PERSON_A, hoy.isoformat(), kind="off")
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "today.shift_today", turno=_t("es", "shift.kind.off")), r.text)

    def test_proposed_batch_link_leads_above_the_shift(self):
        conn = self._reset_db("proposed.db")
        hoy = date(2026, 6, 12)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        _insert_shift(conn, PERSON_A, hoy.isoformat(), kind="off")
        conn.execute(
            "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, day_count, created_at) "
            "VALUES (?, 'turnos.pdf', ?, ?, 'proposed', 1, ?)",
            (PERSON_A, "2026-06-01T00:00:00", "2026-06-01T00:00:00", "2026-06-01T00:00:00"),
        )
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        link = _t("es", "today.shift_proposed")
        hoy_text = _t("es", "today.shift_today", turno=_t("es", "shift.kind.off"))
        self.assertIn(link, r.text)
        self.assertIn(hoy_text, r.text)
        self.assertLess(r.text.index(link), r.text.index(hoy_text))

    def test_no_data_soon_shows_the_same_block(self):
        conn = self._reset_db("soon.db")
        hoy = date(2026, 6, 13)
        _insert_shift(conn, PERSON_A, hoy.isoformat(), kind="night",
                     start_local=f"{hoy.isoformat()}T22:00:00", end_local=f"{(hoy + timedelta(1)).isoformat()}T06:00:00")
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "soon.badge"), r.text)  # confirms this is soon.html, the no-data Hoy
        self.assertIn('class="shift night"', r.text)
        self.assertIn(_t("es", "shift.kind.night"), r.text)

    def test_person_b_shift_never_reaches_person_a(self):
        conn = self._reset_db("scope.db")
        hoy = date(2026, 6, 14)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        _insert_shift(conn, PERSON_A, hoy.isoformat(), kind="off")
        _insert_shift(conn, PERSON_B, hoy.isoformat(), kind="night",
                     start_local=f"{hoy.isoformat()}T22:00:00", end_local=f"{(hoy + timedelta(1)).isoformat()}T06:00:00")
        conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: PERSON_A
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('class="shift night"', r.text)
        self.assertNotIn("22:00", r.text)
        self.assertIn(_t("es", "today.shift_today", turno=_t("es", "shift.kind.off")), r.text)

    def test_renders_in_spanish_and_english(self):
        conn = self._reset_db("lang.db")
        hoy = date(2026, 6, 15)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        _insert_shift(conn, PERSON_A, hoy.isoformat(), kind="night",
                     start_local=f"{hoy.isoformat()}T22:00:00", end_local=f"{(hoy + timedelta(1)).isoformat()}T06:00:00")
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertIn(_t("es", "shift.kind.night"), r.text)

        db.set_language(conn, "en", PERSON_A)
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertIn(_t("en", "shift.kind.night"), r.text)
        self.assertIn(_t("en", "today.shift_today_hours", turno=_t("en", "shift.kind.night"),
                        inicio="22:00", fin="06:00"), r.text)
