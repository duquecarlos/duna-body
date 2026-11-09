"""BODY-EXPERIMENTOS-PANTALLA-1: experiments get a place on screen -- a Metas section, a detail
page, and a short line on Hoy. Invented demo data only, mirrors tests/test_experimentos_modelo.py's
own fixture shapes (noisy baseline, clear test-day shift) for each verdict code."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, identity, web
from body.rutas import comun

client = TestClient(web.app, follow_redirects=False)

FILTRO_TARDE = {"turno_en": ["afternoon"], "turno_fuera": None, "excluir_tras_noche": False}
BASE_INICIO = date(2026, 11, 9)
HOY = BASE_INICIO + timedelta(4)


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "exp.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _insert_shift(conn, person_id: int, day: str, kind: str = "afternoon") -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _seed_baseline(conn, person_id: int, metric: str, hasta: date, dias: int = 120) -> None:
    """120 noisy 'afternoon' days ending `hasta`, mean 50 / pstdev 5 -- same pattern
    tests/test_experimentos_modelo.py's VeredictoDiaTest uses."""
    desde = hasta - timedelta(dias - 1)
    d, i = desde, 0
    pattern = [45.0, 55.0]
    while d <= hasta:
        _insert_shift(conn, person_id, d.isoformat())
        db.set_metric(conn, d.isoformat(), metric, pattern[i % len(pattern)], "garmin", person_id)
        d += timedelta(1)
        i += 1


def _build_experimento(conn, person_id: int, titulo: str, metric: str, test_values: list[float], *,
                        ocurrio: list[bool] | None = None, habito_id: int | None = None,
                        fecha_inicio: date = BASE_INICIO, nota_por_dia: dict | None = None) -> tuple[int, list[str]]:
    """One whole-day experiment on `metric`, `len(test_values)` test days starting `fecha_inicio`,
    a noisy baseline ending the day before. Returns (experimento_id, test day isoformats)."""
    _seed_baseline(conn, person_id, metric, fecha_inicio - timedelta(1))
    eid = db.crear_experimento(conn, person_id, titulo, [metric], {"tipo": "dia"}, dict(FILTRO_TARDE),
                               habito_id=habito_id, fecha_inicio=fecha_inicio.isoformat())
    ocurrio = ocurrio if ocurrio is not None else [True] * len(test_values)
    dias = []
    for i, v in enumerate(test_values):
        day = fecha_inicio + timedelta(i)
        day_iso = day.isoformat()
        _insert_shift(conn, person_id, day_iso)
        db.set_metric(conn, day_iso, metric, v, "garmin", person_id)
        if ocurrio[i]:
            if habito_id is not None:
                conn.execute("INSERT INTO habit_check (habit_id, person_id, day, done) VALUES (?, ?, ?, 1)",
                            (habito_id, person_id, day_iso))
            else:
                nota = (nota_por_dia or {}).get(i)
                db.registrar_dia_experimento(conn, eid, person_id, day_iso, True, nota)
        dias.append(day_iso)
    conn.commit()
    return eid, dias


def _get(path: str, hoy: date = HOY):
    with mock.patch.object(comun, "_today_local", return_value=hoy):
        return client.get(path)


def _post(path: str, data: dict, hoy: date = HOY):
    with mock.patch.object(comun, "_today_local", return_value=hoy):
        return client.post(path, data=data)


class VerdictWordsTest(unittest.TestCase):
    def test_mas_bajo_es(self):
        conn = _reset_db(self)
        _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5)
        r = _get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("claramente más bajo", r.text)

    def test_mas_alto_en(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        _build_experimento(conn, 1, "Hot shower", "stress_avg", [80.0] * 5)
        r = _get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("clearly higher", r.text)

    def test_sin_cambio(self):
        conn = _reset_db(self)
        _build_experimento(conn, 1, "Sin efecto", "stress_avg", [49.0, 51.0, 50.0, 50.0, 50.0])
        r = _get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sin cambio claro", r.text)

    def test_pocos_dias(self):
        conn = _reset_db(self)
        _build_experimento(conn, 1, "Muy pocos dias", "stress_avg", [20.0] * 5,
                           ocurrio=[True, True, False, False, False])
        r = _get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("muy pocos días todavía", r.text)

    def test_aun_no_for_an_intraday_window(self):
        conn = _reset_db(self)
        db.crear_experimento(conn, 1, "Enraizamiento", ["heart_rate"],
                             {"tipo": "intradia", "ancla": "despertar", "antes_min": None, "despues_min": 60},
                             {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False})
        r = _get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("todavía no se puede medir", r.text)

    def test_no_grading_colour_class_in_the_section(self):
        """DESIGN rule 11's own exception for this screen: verdict words, never .good/.bad."""
        conn = _reset_db(self)
        _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5)
        r = _get("/goals")
        chunk = r.text.split("Experimentos", 1)[1].split("Propuestas", 1)[0]
        self.assertNotIn('"good"', chunk)
        self.assertNotIn('"bad"', chunk)
        self.assertIn("↓", chunk)


class EmptyStateTest(unittest.TestCase):
    def test_no_experiment_shows_one_line_no_card(self):
        _reset_db(self)
        r = client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Puedes empezar un experimento pidiéndoselo al asistente.", r.text)
        self.assertNotIn('href="/experiments/', r.text)


class ClosedSectionTest(unittest.TestCase):
    def test_active_and_closed_are_both_rendered_closed_folded(self):
        conn = _reset_db(self)
        eid_active, _ = _build_experimento(conn, 1, "Activo", "stress_avg", [20.0] * 5)
        # 130 days clear of "Activo"'s own range (its 120-day baseline plus 5 test days) so the two
        # experiments' shift/metric fixtures never write the same (person_id, day) twice.
        eid_closed, _ = _build_experimento(conn, 1, "Cerrado ya", "resting_hr", [20.0] * 5,
                                           fecha_inicio=BASE_INICIO + timedelta(130))
        db.cerrar_experimento(conn, 1, eid_closed, fecha_fin=(BASE_INICIO + timedelta(140)).isoformat())
        conn.commit()
        r = _get("/goals", hoy=BASE_INICIO + timedelta(150))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Activo", r.text)
        self.assertIn("Cerrado ya", r.text)
        self.assertIn("Cerrados", r.text)  # the <details> summary that folds closed experiments


class DetailPageTest(unittest.TestCase):
    def test_detail_shows_means_counts_marked_days_and_explanation(self):
        conn = _reset_db(self)
        eid, dias = _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5,
                                       nota_por_dia={0: "fria de verdad"})
        r = _get(f"/experiments/{eid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ducha fria", r.text)
        self.assertIn("en los días de prueba", r.text)
        self.assertIn("tu normal en días parecidos", r.text)
        self.assertIn("fria de verdad", r.text)
        self.assertIn("No es consejo médico", r.text)
        self.assertIn(f'action="/experiments/{eid}/close"', r.text)

    def test_offset_metric_carries_its_own_note(self):
        conn = _reset_db(self)
        eid, _ = _build_experimento(conn, 1, "Dormir mejor", "hrv_last_night", [40.0] * 5)
        r = _get(f"/experiments/{eid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("noche siguiente", r.text)

    def test_another_persons_experiment_404s(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        eid, _ = _build_experimento(conn, 1, "Privado", "stress_avg", [20.0] * 5)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 2
        r = _get(f"/experiments/{eid}")
        self.assertEqual(r.status_code, 404)

    def test_get_writes_nothing(self):
        conn = _reset_db(self)
        eid, _ = _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5)
        before = conn.execute("SELECT * FROM experimento WHERE id = ?", (eid,)).fetchone()
        _get(f"/experiments/{eid}")
        after = conn.execute("SELECT * FROM experimento WHERE id = ?", (eid,)).fetchone()
        self.assertEqual(dict(before), dict(after))


class CloseActionTest(unittest.TestCase):
    def test_owner_can_close(self):
        conn = _reset_db(self)
        eid, _ = _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5)
        r = _post(f"/experiments/{eid}/close", {})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/experiments/{eid}")
        row = conn.execute("SELECT estado, fecha_fin FROM experimento WHERE id = ?", (eid,)).fetchone()
        self.assertEqual(row["estado"], "cerrado")
        self.assertEqual(row["fecha_fin"], HOY.isoformat())

    def test_another_persons_close_is_refused(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        eid, _ = _build_experimento(conn, 1, "Privado", "stress_avg", [20.0] * 5)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 2
        r = _post(f"/experiments/{eid}/close", {})
        self.assertEqual(r.status_code, 404)
        row = conn.execute("SELECT estado FROM experimento WHERE id = ?", (eid,)).fetchone()
        self.assertEqual(row["estado"], "activo")

    def test_already_closed_is_refused(self):
        conn = _reset_db(self)
        eid, _ = _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5)
        r1 = _post(f"/experiments/{eid}/close", {})
        self.assertEqual(r1.status_code, 303)
        r2 = _post(f"/experiments/{eid}/close", {})
        self.assertEqual(r2.status_code, 404)


class HoyLineTest(unittest.TestCase):
    def test_line_shown_on_a_counting_day(self):
        conn = _reset_db(self)
        eid, _ = _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5)
        today = HOY  # the experiment's own last test day, already an 'afternoon' shift
        r = _get("/", hoy=today)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ducha fria", r.text)
        self.assertIn("hoy cuenta", r.text)
        self.assertIn(f'action="/experiments/{eid}/day"', r.text)
        self.assertIn("Lo hice", r.text)
        self.assertIn("Hoy no", r.text)

    def test_line_absent_on_a_non_counting_day(self):
        conn = _reset_db(self)
        _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5)
        today = HOY + timedelta(10)  # still within the open-ended experiment, but no shift row yet
        _insert_shift(conn, 1, today.isoformat(), kind="morning")  # not 'afternoon' -> filter excludes it
        conn.commit()
        r = _get("/", hoy=today)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Ducha fria", r.text)


class MarkFromHoyTest(unittest.TestCase):
    def test_marking_writes_one_row_and_a_second_post_changes_it(self):
        conn = _reset_db(self)
        # The last day (today) is never pre-registered, so the first POST below is the one that
        # creates its experimento_registro row -- not an upsert over one the fixture already made.
        eid, _ = _build_experimento(conn, 1, "Ducha fria", "stress_avg", [20.0] * 5,
                                    ocurrio=[True, True, True, True, False])
        today = HOY  # the experiment's own last test day, already an 'afternoon' shift

        r = _post(f"/experiments/{eid}/day", {"day": today.isoformat(), "ocurrio": "1", "back": "/"}, hoy=today)
        self.assertEqual(r.status_code, 303)
        rows = conn.execute("SELECT ocurrio FROM experimento_registro WHERE experimento_id = ? AND day = ?",
                           (eid, today.isoformat())).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ocurrio"], 1)

        r2 = _post(f"/experiments/{eid}/day", {"day": today.isoformat(), "ocurrio": "0", "back": "/"}, hoy=today)
        self.assertEqual(r2.status_code, 303)
        rows2 = conn.execute("SELECT ocurrio FROM experimento_registro WHERE experimento_id = ? AND day = ?",
                            (eid, today.isoformat())).fetchall()
        self.assertEqual(len(rows2), 1)
        self.assertEqual(rows2[0]["ocurrio"], 0)


class HabitLinkedExperimentTest(unittest.TestCase):
    def test_no_buttons_and_marks_with_the_habit(self):
        conn = _reset_db(self)
        gid = goals.propose(conn, 1, "Dormir mejor", "2026-01-01", kind="milestone", deadline="2027-01-01")
        goals.activate(conn, gid)
        hid = goals.add_habit(conn, gid, "Ducha fria", {"per": "day", "times": 1})
        eid, _ = _build_experimento(conn, 1, "Ducha fria ligada", "stress_avg", [20.0] * 5, habito_id=hid)
        today = HOY  # the experiment's own last test day, already an 'afternoon' shift

        r = _get("/", hoy=today)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ducha fria ligada", r.text)
        self.assertIn("se marca con el hábito", r.text)
        self.assertNotIn(f'action="/experiments/{eid}/day"', r.text)

        r2 = _post(f"/experiments/{eid}/day", {"day": today.isoformat(), "ocurrio": "1", "back": "/"}, hoy=today)
        self.assertEqual(r2.status_code, 404)


if __name__ == "__main__":
    unittest.main()
