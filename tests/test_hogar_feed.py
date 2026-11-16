"""BODY-SERVICIO-HOGAR-1: Hogar's own feed -- hogar_feed.py's pure reads and the two device-key
routes they sit behind (GET /api/hogar/manana, GET /api/hogar/serie). Synthetic data only, no real
health figures.
"""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, dispositivo, goals, hogar_feed, web
from body.rutas import comun
from tests._rutas import contar_sql


def _reset_db(tc: unittest.TestCase, name: str):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)  # made ready BEFORE any route is measured, same as tests/test_get_sin_escritura.py
    return conn


# ---- hogar_feed.manana, no HTTP ---------------------------------------------------------------

class MananaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "manana.db")

    def test_figuras_every_metric_each_at_its_own_latest_day(self):
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 58.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-12", "resting_hr", 55.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-11", "hrv_last_night", 60.0, "garmin", 1)
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        by_key = {f["metric_key"]: f for f in out["figuras"]}
        self.assertEqual(by_key["resting_hr"],
                          {"metric_key": "resting_hr", "value": 55.0, "kind": "measured", "day": "2026-11-12"})
        self.assertEqual(by_key["hrv_last_night"]["day"], "2026-11-11")

    def test_figuras_respects_dia_not_just_the_latest_overall(self):
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 58.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-12", "resting_hr", 55.0, "garmin", 1)
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 11))
        by_key = {f["metric_key"]: f for f in out["figuras"]}
        self.assertEqual(by_key["resting_hr"]["value"], 58.0)
        self.assertEqual(by_key["resting_hr"]["day"], "2026-11-10")

    def test_readiness_marked_estimated(self):
        db.set_metric(self.conn, "2026-11-10", "readiness", 70.0, "garmin", 1)
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        by_key = {f["metric_key"]: f for f in out["figuras"]}
        self.assertEqual(by_key["readiness"]["kind"], "estimated")

    def test_plan_for_dia_and_dia_plus_one_only(self):
        self.conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status) VALUES (1, '2026-11-15', '18:00', "
            "'ZZ-HOY', 'proposed')")
        self.conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status) VALUES (1, '2026-11-16', '07:00', "
            "'ZZ-MANANA', 'accepted')")
        self.conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status) VALUES (1, '2026-11-17', '07:00', "
            "'ZZ-FUERA-DE-VENTANA', 'proposed')")
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        titles = {p["title"] for p in out["plan"]}
        self.assertEqual(titles, {"ZZ-HOY", "ZZ-MANANA"})

    def test_horizonte_null_when_no_active_milestone(self):
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        self.assertIsNone(out["horizonte"])

    def test_horizonte_present_with_week_number_out_of_the_total(self):
        gid = goals.propose(self.conn, 1, "ZZ-HORIZONTE", "2026-11-02", kind="milestone", deadline="2026-12-27")
        goals.activate(self.conn, gid)
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 16))
        self.assertEqual(out["horizonte"],
                          {"goal_id": gid, "title": "ZZ-HORIZONTE", "deadline": "2026-12-27",
                           "semana": 3, "semanas": 8})

    def test_plan_ejercicios_verbatim_when_present_null_when_absent(self):
        ejercicios = [{"ejercicio": "sentadilla", "series": 4, "reps": 10, "categoria": "pierna"},
                      {"ejercicio": "press banca", "series": 3}]
        self.conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status, ejercicios_json) VALUES "
            "(1, '2026-11-15', '18:00', 'ZZ-CON-EJERCICIOS', 'proposed', ?)", (json.dumps(ejercicios),))
        self.conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status) VALUES "
            "(1, '2026-11-15', '19:00', 'ZZ-SIN-EJERCICIOS', 'proposed')")
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        by_title = {p["title"]: p for p in out["plan"]}
        self.assertEqual(by_title["ZZ-CON-EJERCICIOS"]["ejercicios"], ejercicios)
        self.assertIsNone(by_title["ZZ-SIN-EJERCICIOS"]["ejercicios"])
        self.assertEqual(set(by_title["ZZ-CON-EJERCICIOS"].keys()),
                          {"id", "day", "time", "title", "status", "ejercicios"})

    # ---- turnos (BODY-HOGAR-TURNOS-1 ruling 1) --------------------------------------------------

    def test_turnos_empty_list_when_none(self):
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        self.assertEqual(out["turnos"], [])

    def test_turnos_night_started_dia_minus_1_ending_dia_is_included(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-14', 'night', 'N', '2026-11-14T22:00:00', '2026-11-15T06:00:00')")
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        self.assertEqual(out["turnos"], [
            {"day": "2026-11-14", "kind": "night", "code": "N",
             "start_local": "2026-11-14T22:00:00", "end_local": "2026-11-15T06:00:00"},
        ])

    def test_turnos_night_ending_before_dia_is_excluded(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-13', 'night', 'N', '2026-11-13T22:00:00', '2026-11-14T06:00:00')")
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        self.assertEqual(out["turnos"], [])

    def test_turnos_dia_plus_1_included_dia_plus_2_not(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-16', 'morning', 'M1', '2026-11-16T06:00:00', '2026-11-16T14:00:00')")
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-17', 'morning', 'M2', '2026-11-17T06:00:00', '2026-11-17T14:00:00')")
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        self.assertEqual([t["day"] for t in out["turnos"]], ["2026-11-16"])

    def test_turnos_off_row_with_null_times_is_included(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-15', 'off', NULL, NULL, NULL)")
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        self.assertEqual(out["turnos"], [
            {"day": "2026-11-15", "kind": "off", "code": None, "start_local": None, "end_local": None},
        ])

    def test_four_old_keys_unchanged_with_turnos_present(self):
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 58.0, "garmin", 1)
        self.conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status) VALUES (1, '2026-11-15', '18:00', "
            "'ZZ-HOY', 'proposed')")
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-15', 'morning', 'M', '2026-11-15T06:00:00', '2026-11-15T14:00:00')")
        self.conn.commit()
        out = hogar_feed.manana(self.conn, 1, date(2026, 11, 15))
        self.assertEqual(out["dia"], "2026-11-15")
        self.assertEqual([{"metric_key": f["metric_key"], "value": f["value"], "kind": f["kind"], "day": f["day"]}
                           for f in out["figuras"]],
                          [{"metric_key": "resting_hr", "value": 58.0, "kind": "measured", "day": "2026-11-10"}])
        plan_id = out["plan"][0]["id"]
        self.assertEqual(out["plan"], [
            {"id": plan_id, "day": "2026-11-15", "time": "18:00", "title": "ZZ-HOY",
             "status": "proposed", "ejercicios": None},
        ])
        self.assertIsNone(out["horizonte"])
        self.assertEqual(set(out.keys()), {"dia", "figuras", "plan", "horizonte", "turnos"})


# ---- hogar_feed.serie, no HTTP ------------------------------------------------------------------

class SerieTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "serie.db")
        db.set_metric(self.conn, "2026-11-01", "resting_hr", 58.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-02", "resting_hr", 57.0, "garmin", 1)
        self.conn.commit()

    def test_puntos_carry_value_and_kind(self):
        out = hogar_feed.serie(self.conn, 1, "resting_hr", "2026-11-01", "2026-11-02")
        self.assertEqual(out, {"metric_key": "resting_hr", "puntos": [
            {"day": "2026-11-01", "value": 58.0, "kind": "measured"},
            {"day": "2026-11-02", "value": 57.0, "kind": "measured"},
        ]})

    def test_unknown_metric_key_raises(self):
        with self.assertRaises(hogar_feed.MetricDesconocida):
            hogar_feed.serie(self.conn, 1, "zz_no_existe", "2026-11-01", "2026-11-02")

    def test_366_days_ok(self):
        out = hogar_feed.serie(self.conn, 1, "resting_hr", "2026-01-01", "2027-01-01")
        self.assertEqual(out["metric_key"], "resting_hr")

    def test_367_days_raises(self):
        with self.assertRaises(hogar_feed.RangoDemasiadoLargo):
            hogar_feed.serie(self.conn, 1, "resting_hr", "2026-01-01", "2027-01-02")


# ---- hogar_feed.turnos / turnos_rango, no HTTP --------------------------------------------------

class TurnosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "turnos.db")
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-10', 'night', 'N1', '2026-11-10T22:00:00', '2026-11-11T06:00:00')")
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-12', 'off', NULL, NULL, NULL)")
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-15', 'morning', 'M1', '2026-11-15T06:00:00', '2026-11-15T14:00:00')")
        self.conn.commit()

    def test_range_returns_rows_in_range_ordered(self):
        out = hogar_feed.turnos(self.conn, 1, "2026-11-10", "2026-11-12")
        self.assertEqual(out, {"desde": "2026-11-10", "hasta": "2026-11-12", "turnos": [
            {"day": "2026-11-10", "kind": "night", "code": "N1",
             "start_local": "2026-11-10T22:00:00", "end_local": "2026-11-11T06:00:00"},
            {"day": "2026-11-12", "kind": "off", "code": None, "start_local": None, "end_local": None},
        ]})

    def test_no_shift_in_range_is_empty_list_not_an_error(self):
        out = hogar_feed.turnos(self.conn, 1, "2026-01-01", "2026-01-02")
        self.assertEqual(out["turnos"], [])

    def test_ordering_by_day_then_start_local_nulls_first(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-15', 'off', 'LATER', NULL, NULL)")
        self.conn.commit()
        out = hogar_feed.turnos_rango(self.conn, 1, "2026-11-15", "2026-11-15")
        self.assertEqual([t["code"] for t in out], ["LATER", "M1"])

    def test_366_days_ok_367_days_raises(self):
        out = hogar_feed.turnos(self.conn, 1, "2026-01-01", "2027-01-01")
        self.assertEqual(out["desde"], "2026-01-01")
        with self.assertRaises(hogar_feed.RangoDemasiadoLargo):
            hogar_feed.turnos(self.conn, 1, "2026-01-01", "2027-01-02")


# ---- the two routes, hosted mode, device-key auth -----------------------------------------------

class DeviceRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "routes.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.client = TestClient(web.app, follow_redirects=False)


class ApiHogarAuthTest(DeviceRoutesTestCase):
    def test_manana_no_header_401(self):
        r = self.client.get("/api/hogar/manana")
        self.assertEqual(r.status_code, 401)

    def test_serie_no_header_401(self):
        r = self.client.get("/api/hogar/serie?metric_key=resting_hr&desde=2026-11-01&hasta=2026-11-02")
        self.assertEqual(r.status_code, 401)

    def test_manana_wrong_scope_403(self):
        key = dispositivo.crear(self.conn, 1, "sin alcance", {"vida:leer"})
        r = self.client.get("/api/hogar/manana", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 403)

    def test_serie_wrong_scope_403(self):
        key = dispositivo.crear(self.conn, 1, "sin alcance", {"vida:leer"})
        r = self.client.get("/api/hogar/serie?metric_key=resting_hr&desde=2026-11-01&hasta=2026-11-02",
                             headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 403)

    def test_revoked_key_401(self):
        key = dispositivo.crear(self.conn, 1, "PC", {"hogar:leer"})
        row = self.conn.execute("SELECT id FROM dispositivo WHERE person_id = 1").fetchone()
        dispositivo.revocar(self.conn, 1, row["id"])
        r = self.client.get("/api/hogar/manana", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 401)


class ApiHogarMananaTest(DeviceRoutesTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC", {"hogar:leer"})

    def test_returns_every_metric_no_store(self):
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 58.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-11", "hrv_last_night", 60.0, "garmin", 1)
        self.conn.commit()
        r = self.client.get("/api/hogar/manana?dia=2026-11-15", headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        out = r.json()
        self.assertEqual(out["dia"], "2026-11-15")
        keys = {f["metric_key"] for f in out["figuras"]}
        self.assertIn("resting_hr", keys)
        self.assertIn("hrv_last_night", keys)
        self.assertIsNone(out["horizonte"])

    def test_bad_dia_400(self):
        r = self.client.get("/api/hogar/manana?dia=not-a-date", headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 400)

    def test_default_dia_is_the_routes_own_local_clock(self):
        with mock.patch.object(comun, "_today_local", return_value=date(2026, 11, 20)):
            r = self.client.get("/api/hogar/manana", headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["dia"], "2026-11-20")


class ApiHogarSerieRouteTest(DeviceRoutesTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC", {"hogar:leer"})
        db.set_metric(self.conn, "2026-11-01", "resting_hr", 58.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-02", "resting_hr", 57.0, "garmin", 1)
        self.conn.commit()

    def _get(self, metric_key="resting_hr", desde="2026-11-01", hasta="2026-11-02"):
        return self.client.get(f"/api/hogar/serie?metric_key={metric_key}&desde={desde}&hasta={hasta}",
                                headers={"Authorization": f"Bearer {self.key}"})

    def test_returns_points_no_store(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        out = r.json()
        self.assertEqual(out["metric_key"], "resting_hr")
        self.assertEqual([p["value"] for p in out["puntos"]], [58.0, 57.0])

    def test_unknown_metric_key_404(self):
        r = self._get(metric_key="zz_no_existe")
        self.assertEqual(r.status_code, 404)

    def test_366_days_ok_367_days_400(self):
        r_ok = self._get(desde="2026-01-01", hasta="2027-01-01")
        self.assertEqual(r_ok.status_code, 200)
        r_over = self._get(desde="2026-01-01", hasta="2027-01-02")
        self.assertEqual(r_over.status_code, 400)

    def test_hasta_before_desde_400(self):
        r = self._get(desde="2026-11-02", hasta="2026-11-01")
        self.assertEqual(r.status_code, 400)


class ApiHogarTurnosRouteTest(DeviceRoutesTestCase):
    def setUp(self):
        super().setUp()
        self.key = dispositivo.crear(self.conn, 1, "PC", {"hogar:leer"})
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-10', 'night', 'N1', '2026-11-10T22:00:00', '2026-11-11T06:00:00')")
        self.conn.commit()

    def _get(self, desde="2026-11-01", hasta="2026-11-20"):
        return self.client.get(f"/api/hogar/turnos?desde={desde}&hasta={hasta}",
                                headers={"Authorization": f"Bearer {self.key}"})

    def test_returns_turnos_no_store(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        out = r.json()
        self.assertEqual(out["desde"], "2026-11-01")
        self.assertEqual(out["hasta"], "2026-11-20")
        self.assertEqual(len(out["turnos"]), 1)

    def test_no_range_is_empty_list_not_an_error(self):
        r = self._get(desde="2020-01-01", hasta="2020-01-02")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["turnos"], [])

    def test_bad_date_400(self):
        r = self._get(desde="not-a-date")
        self.assertEqual(r.status_code, 400)

    def test_hasta_before_desde_400(self):
        r = self._get(desde="2026-11-20", hasta="2026-11-01")
        self.assertEqual(r.status_code, 400)

    def test_366_days_ok_367_days_400(self):
        r_ok = self._get(desde="2026-01-01", hasta="2027-01-01")
        self.assertEqual(r_ok.status_code, 200)
        r_over = self._get(desde="2026-01-01", hasta="2027-01-02")
        self.assertEqual(r_over.status_code, 400)

    def test_no_header_401(self):
        r = self.client.get("/api/hogar/turnos?desde=2026-11-01&hasta=2026-11-02")
        self.assertEqual(r.status_code, 401)

    def test_wrong_scope_403(self):
        key = dispositivo.crear(self.conn, 1, "sin alcance", {"vida:leer"})
        r = self.client.get("/api/hogar/turnos?desde=2026-11-01&hasta=2026-11-02",
                             headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 403)

    def test_revoked_key_401(self):
        key = dispositivo.crear(self.conn, 1, "PC2", {"hogar:leer"})
        row = self.conn.execute(
            "SELECT id FROM dispositivo WHERE person_id = 1 AND nombre = 'PC2'").fetchone()
        dispositivo.revocar(self.conn, 1, row["id"])
        r = self.client.get("/api/hogar/turnos?desde=2026-11-01&hasta=2026-11-02",
                             headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 401)


# ---- another person's data never returned ------------------------------------------------------

class PersonaFugaTest(DeviceRoutesTestCase):
    def setUp(self):
        super().setUp()
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 58.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 70.0, "garmin", 2)
        self.key_a = dispositivo.crear(self.conn, 1, "PC A", {"hogar:leer"})
        self.key_b = dispositivo.crear(self.conn, 2, "PC B", {"hogar:leer"})
        self.conn.commit()

    def test_manana_only_returns_the_keys_own_person(self):
        r_a = self.client.get("/api/hogar/manana?dia=2026-11-15", headers={"Authorization": f"Bearer {self.key_a}"})
        r_b = self.client.get("/api/hogar/manana?dia=2026-11-15", headers={"Authorization": f"Bearer {self.key_b}"})
        val_a = next(f["value"] for f in r_a.json()["figuras"] if f["metric_key"] == "resting_hr")
        val_b = next(f["value"] for f in r_b.json()["figuras"] if f["metric_key"] == "resting_hr")
        self.assertEqual(val_a, 58.0)
        self.assertEqual(val_b, 70.0)

    def test_serie_only_returns_the_keys_own_person(self):
        r_a = self.client.get(
            "/api/hogar/serie?metric_key=resting_hr&desde=2026-11-10&hasta=2026-11-10",
            headers={"Authorization": f"Bearer {self.key_a}"})
        self.assertEqual(r_a.json()["puntos"][0]["value"], 58.0)

    def test_turnos_route_only_returns_the_keys_own_person(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-10', 'night', 'ZZA-TURNO', '2026-11-10T22:00:00', '2026-11-11T06:00:00')")
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(2, '2026-11-10', 'night', 'ZZB-TURNO', '2026-11-10T22:00:00', '2026-11-11T06:00:00')")
        self.conn.commit()
        r_a = self.client.get("/api/hogar/turnos?desde=2026-11-10&hasta=2026-11-10",
                               headers={"Authorization": f"Bearer {self.key_a}"})
        r_b = self.client.get("/api/hogar/turnos?desde=2026-11-10&hasta=2026-11-10",
                               headers={"Authorization": f"Bearer {self.key_b}"})
        self.assertEqual([t["code"] for t in r_a.json()["turnos"]], ["ZZA-TURNO"])
        self.assertEqual([t["code"] for t in r_b.json()["turnos"]], ["ZZB-TURNO"])

    def test_manana_turnos_only_returns_the_keys_own_person(self):
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-15', 'morning', 'ZZA-TURNO', '2026-11-15T06:00:00', '2026-11-15T14:00:00')")
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(2, '2026-11-15', 'morning', 'ZZB-TURNO', '2026-11-15T06:00:00', '2026-11-15T14:00:00')")
        self.conn.commit()
        r_a = self.client.get("/api/hogar/manana?dia=2026-11-15", headers={"Authorization": f"Bearer {self.key_a}"})
        r_b = self.client.get("/api/hogar/manana?dia=2026-11-15", headers={"Authorization": f"Bearer {self.key_b}"})
        self.assertEqual([t["code"] for t in r_a.json()["turnos"]], ["ZZA-TURNO"])
        self.assertEqual([t["code"] for t in r_b.json()["turnos"]], ["ZZB-TURNO"])


# ---- no write ---------------------------------------------------------------------------------
# dispositivo.resolver's own usado_en bookkeeping write (the same one every other device-key scope
# triggers on a successful auth, e.g. GET /api/vida/semana) is the one write any of these routes may
# ever cause -- never the person's own data (daily_metric, plan, goal, shift).

class NoWriteTest(DeviceRoutesTestCase):
    def _dump(self, table: str) -> tuple:
        cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]
        order = ", ".join(f'"{c}"' for c in cols)
        return tuple(self.conn.execute(f"SELECT {order} FROM {table} ORDER BY {order}").fetchall())

    def test_manana_serie_and_turnos_write_no_person_data(self):
        key = dispositivo.crear(self.conn, 1, "PC", {"hogar:leer"})
        db.set_metric(self.conn, "2026-11-10", "resting_hr", 58.0, "garmin", 1)
        gid = goals.propose(self.conn, 1, "ZZ-HORIZONTE", "2026-11-02", kind="milestone", deadline="2026-12-27")
        goals.activate(self.conn, gid)
        self.conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local) VALUES "
            "(1, '2026-11-15', 'morning', 'M', '2026-11-15T06:00:00', '2026-11-15T14:00:00')")
        self.conn.commit()
        before = {t: self._dump(t) for t in ("daily_metric", "plan", "goal", "shift")}
        with contar_sql() as stats:
            self.client.get("/api/hogar/manana?dia=2026-11-15", headers={"Authorization": f"Bearer {key}"})
            self.client.get("/api/hogar/serie?metric_key=resting_hr&desde=2026-11-01&hasta=2026-11-15",
                             headers={"Authorization": f"Bearer {key}"})
            self.client.get("/api/hogar/turnos?desde=2026-11-01&hasta=2026-11-15",
                             headers={"Authorization": f"Bearer {key}"})
        after = {t: self._dump(t) for t in ("daily_metric", "plan", "goal", "shift")}
        self.assertEqual(before, after)
        # dispositivo.resolver's own UPDATE + its commit, once per resolved request (contar_sql
        # counts a write transaction's UPDATE and its COMMIT as two) -- 3 requests, 6 writes.
        self.assertEqual(stats["writes"], 6)


if __name__ == "__main__":
    unittest.main()
