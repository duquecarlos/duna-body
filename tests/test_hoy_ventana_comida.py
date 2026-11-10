"""Hoy's and Día's eating-window card (BODY-AYUNO-1): renders with and without `ingesta` entries,
in es and en, reading body/ventana_comida.py -- nothing stored by a GET. Invented data only,
"today" frozen the same way tests/test_hoy_turno.py freezes it."""
import contextlib
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, web
from body.i18n import T
from body.rutas import comun

PERSON_A = 1


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("'", "&#39;")


def _t(lang: str, key: str, **kw) -> str:
    return _esc(T(lang)(key, **kw))


class VentanaWebTest(unittest.TestCase):
    client = TestClient(web.app, follow_redirects=False)

    def _reset_db(self, name: str):
        orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        conn.commit()
        return conn

    # ---- Hoy --------------------------------------------------------------------------------

    def test_hoy_with_no_entries_shows_the_plain_invite(self):
        conn = self._reset_db("hoy_sin.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "today.ventana.titulo"), r.text)
        self.assertIn(_t("es", "today.ventana.sin_registro"), r.text)

    def test_hoy_window_across_several_meals(self):
        conn = self._reset_db("hoy_ventana.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T07:40:00")
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T17:50:00")
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "today.ventana.rango", primera="07:40", ultima="17:50"), r.text)
        self.assertIn("10 h 10", r.text)  # t.minutes(610) -- the window's own length
        self.assertNotIn(_t("es", "today.ventana.sin_registro"), r.text)

    def test_hoy_untimed_entry_says_so_without_inventing_a_window(self):
        conn = self._reset_db("hoy_sinhora.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "today.ventana.sin_hora"), r.text)

    def test_hoy_one_meal_states_it_as_such(self):
        conn = self._reset_db("hoy_una.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T07:40:00")
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "today.ventana.una_comida", hora="07:40"), r.text)

    def test_hoy_yesterdays_meal_to_sleep_gap_shown_when_known(self):
        conn = self._reset_db("hoy_sueno.db")
        hoy = date(2026, 6, 10)
        ayer = date(2026, 6, 9)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.registrar_ingesta(conn, PERSON_A, ayer.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{ayer.isoformat()}T18:00:00")
        conn.execute(
            "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
            (PERSON_A, hoy.isoformat(), f"{ayer.isoformat()}T22:00:00", f"{hoy.isoformat()}T06:00:00"),
        )
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "today.ventana.comida_sueno", horas="4 h 00"), r.text)

    def test_hoy_renders_in_english(self):
        conn = self._reset_db("hoy_en.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T07:40:00")
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T17:50:00")
        db.set_language(conn, "en", PERSON_A)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=hoy):
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("en", "today.ventana.titulo"), r.text)
        self.assertIn(_t("en", "today.ventana.rango", primera="07:40", ultima="17:50"), r.text)

    # ---- Día ---------------------------------------------------------------------------------

    def test_dia_window_and_overnight_fast_before_it(self):
        conn = self._reset_db("dia_ventana.db")
        ayer = date(2026, 6, 9)
        hoy = date(2026, 6, 10)
        db.registrar_ingesta(conn, PERSON_A, ayer.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{ayer.isoformat()}T19:00:00")
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T08:00:00")
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T18:00:00")
        conn.commit()
        r = self.client.get(f"/day/{hoy.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "day.ventana.rango", primera="08:00", ultima="18:00"), r.text)
        self.assertIn(_t("es", "day.ventana.ayuno_previo", horas="13 h 00"), r.text)

    def test_dia_with_no_entries_shows_the_plain_gap(self):
        conn = self._reset_db("dia_sin.db")
        hoy = date(2026, 6, 10)
        conn.commit()
        r = self.client.get(f"/day/{hoy.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("es", "day.ventana.sin_registro"), r.text)

    def test_dia_renders_in_english(self):
        conn = self._reset_db("dia_en.db")
        hoy = date(2026, 6, 10)
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T08:00:00")
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T18:00:00")
        db.set_language(conn, "en", PERSON_A)
        conn.commit()
        r = self.client.get(f"/day/{hoy.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_t("en", "day.ventana.titulo"), r.text)
        self.assertIn(_t("en", "day.ventana.rango", primera="08:00", ultima="18:00"), r.text)

    # ---- never logged -------------------------------------------------------------------------

    def test_no_figure_printed_while_rendering_either_screen(self):
        conn = self._reset_db("sin_log.db")
        hoy = date(2026, 6, 10)
        db.set_metric(conn, hoy.isoformat(), "resting_hr", 55.0, "garmin", PERSON_A)
        db.registrar_ingesta(conn, PERSON_A, hoy.isoformat(), "protein_g", 20.0, False,
                              ts_local=f"{hoy.isoformat()}T07:40:00")
        conn.commit()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with mock.patch.object(comun, "_today_local", return_value=hoy):
                r1 = self.client.get("/")
            r2 = self.client.get(f"/day/{hoy.isoformat()}")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
