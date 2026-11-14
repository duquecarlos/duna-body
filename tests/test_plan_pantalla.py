"""BODY-PLAN-PANTALLA-1: the plan-vs-real screen lives in goal detail, not a new tab -- the "This
week of your plan" card with its estado chips and honest gaps, end targets, next/past
checkpoints and linked experiments for a horizon goal; a child goal's own "part of the plan"
link; and /goals nesting a horizon's children under its own card. Invented demo data only, built
through goals.propose/activate and db.py's own horizonte_* writers -- never a hand-written INSERT
except on the `plan` table, which has no db.py writer (the same one exception body/demo.py's
_seed_plan_hoy already takes)."""
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, fechas, goals, web
from body.rutas import comun, metas

client = TestClient(web.app, follow_redirects=False)

HOY = date(2026, 11, 12)
MONDAY_PAST = date(2026, 11, 2)
MONDAY_CURRENT = date(2026, 11, 9)
START_DAY = MONDAY_PAST.isoformat()
DEADLINE = date(2026, 11, 22).isoformat()


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "plan.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _get(path: str, hoy: date = HOY):
    with mock.patch.object(comun, "_today_local", return_value=hoy):
        return client.get(path)


def _horizonte(conn, person_id: int = 1, titulo: str = "ZZ Horizonte") -> int:
    return goals.propose(conn, person_id, titulo, START_DAY, kind="milestone", deadline=DEADLINE)


def _hijo(conn, horizonte_id: int, titulo: str, metric_key: str, person_id: int = 1) -> int:
    gid = goals.propose(conn, person_id, titulo, START_DAY, kind="metric", metric_key=metric_key,
                        direction="lower", target_value=55.0, baseline_value=65.0, deadline=DEADLINE)
    conn.execute("UPDATE goal SET horizonte_goal_id = ? WHERE id = ?", (horizonte_id, gid))
    return gid


def _activity(conn, day: str, type_: str, source_id: str, duration_s: float = 1800.0, person_id: int = 1):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, duration_s, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'garmin')",
        (person_id, source_id, day, f"{day}T07:00:00", type_, duration_s))


def _plan_row(conn, goal_id: int, day: str, title: str, status: str, time_: str = "07:00", person_id: int = 1):
    conn.execute(
        "INSERT INTO plan (person_id, goal_id, day, time, title, status) VALUES (?, ?, ?, ?, ?, ?)",
        (person_id, goal_id, day, time_, title, status))


def _build_rich_horizon(conn, person_id: int = 1) -> dict:
    """One horizon, 2026-11-02..2026-11-22 (three Mondays), with everything rulings 2/3 need to
    render at once: a cumple and a no_cumple line in the terminated past week (2026-11-02), an
    en_curso cardio line (with its own zone note) and a sin_dato clock line in the open current
    week (2026-11-09, hoy=2026-11-12 Thursday), two planned sessions this week (one done, one
    not), a measurable and a text-only rule, a past (cumple) and a future (pendiente) checkpoint,
    two child goals, and one linked experiment."""
    gid = _horizonte(conn, person_id)
    hijo1 = _hijo(conn, gid, "ZZ Pulso en reposo", "resting_hr", person_id)
    hijo2 = _hijo(conn, gid, "ZZ Pasos diarios", "steps", person_id)

    db.crear_meta_semanal(conn, person_id, gid, MONDAY_PAST.isoformat(), "sesiones_fuerza", "al_menos", 2.0)
    db.crear_meta_semanal(conn, person_id, gid, MONDAY_PAST.isoformat(), "sesiones_cardio", "al_menos", 5.0)
    db.crear_meta_semanal(conn, person_id, gid, MONDAY_CURRENT.isoformat(), "sesiones_cardio", "al_menos", 3.0)
    db.crear_meta_semanal(conn, person_id, gid, MONDAY_CURRENT.isoformat(), "hora_acostarse", "antes_de", 660.0)

    _activity(conn, "2026-11-03", "strength_training", "a1", person_id=person_id)
    _activity(conn, "2026-11-05", "strength_training", "a2", person_id=person_id)
    _activity(conn, "2026-11-10", "running", "a3", duration_s=1800.0, person_id=person_id)

    _plan_row(conn, gid, "2026-11-10", "ZZ Sesión de fuerza", "accepted", person_id=person_id)
    _plan_row(conn, gid, "2026-11-11", "ZZ Caminata larga", "done", person_id=person_id)

    db.crear_regla_horizonte(conn, person_id, gid, "siempre", None, "Duerme cuando puedas.", chequeo=None)
    db.crear_regla_horizonte(conn, person_id, gid, "siempre", None, "Duerme al menos 7 horas.",
                             chequeo={"tipo": "sueno_minimo", "minutos": 420})

    cp_past = db.crear_checkpoint_horizonte(
        conn, person_id, gid, "2026-11-05", "ZZ primer checkpoint",
        [{"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 1, "ventana": "semana"}])
    cp_future = db.crear_checkpoint_horizonte(
        conn, person_id, gid, "2026-11-20", "ZZ checkpoint siguiente",
        [{"medida": "sesiones_cardio", "sentido": "al_menos", "valor": 1, "ventana": "semana"}])

    eid = db.crear_experimento(
        conn, person_id, "ZZ Experimento del plan", ["resting_hr"], {"tipo": "dia"},
        {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False},
        fecha_inicio=START_DAY, goal_id=gid)
    conn.commit()
    return {"horizonte": gid, "hijo1": hijo1, "hijo2": hijo2, "cp_past": cp_past, "cp_future": cp_future,
            "experimento": eid}


class PlanSectionOnlyOnHorizonTest(unittest.TestCase):
    def test_plain_milestone_has_no_plan_section(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Meta simple")
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Esta semana de tu plan", r.text)

    def test_one_weekly_line_already_makes_it_a_horizon(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Meta con plan")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 2.0)
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Esta semana de tu plan", r.text)

    def test_metric_goal_is_never_a_horizon(self):
        conn = _reset_db(self)
        gid = goals.propose(conn, 1, "ZZ Meta metrica", START_DAY, metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline=DEADLINE)
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Esta semana de tu plan", r.text)


class EstadoChipsAndHonestGapTest(unittest.TestCase):
    def test_current_week_shows_en_curso_and_sin_dato_and_the_zone_note(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("En curso", r.text)
        self.assertIn("sin datos del reloj todavía", r.text)
        self.assertIn("contada por sesión; no se mide la zona de pulso", r.text)

    def test_past_week_shows_cumple_and_no_cumple(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}?semana={MONDAY_PAST.isoformat()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Cumple", r.text)
        self.assertIn("No cumple", r.text)


class RulesCardTest(unittest.TestCase):
    def test_measurable_rule_is_judged_text_only_rule_is_not(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Reglas de esta semana", r.text)
        self.assertIn("Duerme al menos 7 horas. ·", r.text)
        self.assertNotIn("Duerme cuando puedas. ·", r.text)
        self.assertIn("Duerme cuando puedas.", r.text)


class SessionsByDayCardTest(unittest.TestCase):
    def test_planned_sessions_show_done_and_not_done(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sesiones planeadas", r.text)
        self.assertIn("ZZ Sesión de fuerza", r.text)
        self.assertIn("ZZ Caminata larga", r.text)
        self.assertIn("Hecho", r.text)
        self.assertIn("Pendiente", r.text)


class EndTargetsCardTest(unittest.TestCase):
    def test_child_goals_listed_as_links_with_a_verdict_chip(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Metas finales", r.text)
        self.assertIn("ZZ Pulso en reposo", r.text)
        self.assertIn("ZZ Pasos diarios", r.text)
        self.assertIn(f'href="/goals/{ids["hijo1"]}"', r.text)
        self.assertIn(f'href="/goals/{ids["hijo2"]}"', r.text)


class CheckpointsCardTest(unittest.TestCase):
    def test_next_checkpoint_shown_past_one_inside_closed_details(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}")
        self.assertEqual(r.status_code, 200)
        text = r.text
        self.assertIn("ZZ checkpoint siguiente", text)
        self.assertIn("ZZ primer checkpoint", text)
        self.assertLess(text.index("ZZ checkpoint siguiente"), text.index("Chequeos pasados"))
        self.assertGreater(text.index("ZZ primer checkpoint"), text.index("Chequeos pasados"))
        m = re.search(r'<details class="files[^>]*>\s*<summary[^>]*>Chequeos pasados', text)
        self.assertIsNotNone(m)
        self.assertNotIn("open", m.group(0))

    def test_no_checkpoints_at_all_omits_the_whole_checkpoint_card(self):
        # Zero checkpoints ever (not just none upcoming) means checkpoint_next and
        # checkpoints_past are both empty, so the whole card is never rendered -- the empty note
        # is reserved for a horizon that HAS past checkpoints but none still ahead.
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Horizonte sin esos hitos")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 2.0)
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("checkpoint", r.text.lower())

    def test_only_a_past_checkpoint_shows_the_no_upcoming_note(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Horizonte con checkpoint pasado")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 2.0)
        db.crear_checkpoint_horizonte(
            conn, 1, gid, "2026-11-05", "ZZ unico checkpoint",
            [{"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 1, "ventana": "semana"}])
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No hay un próximo chequeo.", r.text)
        self.assertIn("ZZ unico checkpoint", r.text)


class MinuteUnitTargetCardTest(unittest.TestCase):
    """Ruling 2 (BODY-PLAN-PANTALLA-1-R): a `min`-unit weekly target (sueno_minutos,
    minutos_intensidad) is written in the same hours-and-minutes shape its real figure already
    uses on the row -- never "420 min" beside "6 h 51 de media"."""

    def test_sueno_minutos_whole_hour_target_drops_the_bare_00(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Horizonte con meta de sueño")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sueno_minutos", "al_menos", 420.0)
        for i in range(3):
            day = MONDAY_CURRENT + timedelta(i)
            db.set_metric(conn, day.isoformat(), "sleep_minutes", 411.0, "garmin", 1)
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sueño: al menos 7 h", r.text)
        self.assertIn("6 h 51 de media", r.text)
        self.assertNotIn("7 h 00", r.text)
        self.assertNotIn("420 min", r.text)

    def test_sueno_minutos_whole_hour_target_in_english(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Horizon with a sleep target")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "sueno_minutos", "al_menos", 420.0)
        for i in range(3):
            day = MONDAY_CURRENT + timedelta(i)
            db.set_metric(conn, day.isoformat(), "sleep_minutes", 411.0, "garmin", 1)
        db.set_language(conn, "en", 1)
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sleep: at least 7 h", r.text)
        self.assertIn("6 h 51 on average", r.text)

    def test_minutos_intensidad_keeps_minutes_above_and_below_an_hour(self):
        conn = _reset_db(self)
        gid = _horizonte(conn, titulo="ZZ Horizonte con meta de intensidad")
        db.crear_meta_semanal(conn, 1, gid, MONDAY_CURRENT.isoformat(), "minutos_intensidad", "al_menos", 90.0)
        for i in range(2):
            day = MONDAY_CURRENT + timedelta(i)
            db.set_metric(conn, day.isoformat(), "intensity_minutes", 22.5, "garmin", 1)
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("minutos de intensidad: al menos 1 h 30", r.text)
        self.assertIn("45 min de media", r.text)


class ExperimentsLinkedCardTest(unittest.TestCase):
    def test_linked_experiment_appears_as_a_link(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Experimentos de tu plan", r.text)
        self.assertIn(f'href="/experiments/{ids["experimento"]}"', r.text)
        self.assertIn("ZZ Experimento del plan", r.text)


class DaysLeftTextTest(unittest.TestCase):
    def test_days_to_end_date(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}")
        n = (date.fromisoformat(DEADLINE) - HOY).days
        self.assertIn(f"{n} días para el final", r.text)

    def test_ended_once_past_the_deadline(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}", hoy=date(2026, 12, 1))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Terminó", r.text)


class WeekNavigationBoundsTest(unittest.TestCase):
    def test_before_the_horizons_start_clamps_to_its_first_week(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        gid = ids["horizonte"]
        before = _get(f"/goals/{gid}?semana=2026-10-01")
        exact = _get(f"/goals/{gid}?semana={MONDAY_PAST.isoformat()}")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.text, exact.text)

    def test_after_the_horizons_deadline_clamps_to_its_last_week(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        gid = ids["horizonte"]
        last_monday = fechas.lunes_de(date.fromisoformat(DEADLINE))
        after = _get(f"/goals/{gid}?semana=2026-12-25")
        exact = _get(f"/goals/{gid}?semana={last_monday.isoformat()}")
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.text, exact.text)

    def test_first_week_has_no_previous_link(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['horizonte']}?semana={MONDAY_PAST.isoformat()}")
        self.assertNotIn(f"?semana={(MONDAY_PAST - timedelta(7)).isoformat()}", r.text)

    def test_last_week_has_no_next_link(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        last_monday = fechas.lunes_de(date.fromisoformat(DEADLINE))
        r = _get(f"/goals/{ids['horizonte']}?semana={last_monday.isoformat()}")
        self.assertNotIn(f"?semana={(last_monday + timedelta(7)).isoformat()}", r.text)


class PartOfPlanLinkTest(unittest.TestCase):
    def test_child_goal_links_back_to_its_horizon(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        r = _get(f"/goals/{ids['hijo1']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Parte del plan:", r.text)
        self.assertIn(f'href="/goals/{ids["horizonte"]}"', r.text)
        self.assertIn("ZZ Horizonte", r.text)

    def test_plain_goal_has_no_part_of_line(self):
        conn = _reset_db(self)
        gid = goals.propose(conn, 1, "ZZ Meta suelta", START_DAY, metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline=DEADLINE)
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Parte del plan:", r.text)


class NestHorizontesUnitTest(unittest.TestCase):
    """Pure-Python coverage of _nest_horizontes itself, no HTTP: precise over the matching logic
    without a database round trip."""

    def test_nests_children_under_their_horizon_card(self):
        rows = [
            {"id": 10, "horizonte_goal_id": None},
            {"id": 11, "horizonte_goal_id": 10},
            {"id": 12, "horizonte_goal_id": 10},
            {"id": 13, "horizonte_goal_id": None},
        ]
        cards = [{"id": r["id"], "title": f"g{r['id']}"} for r in rows]
        top = metas._nest_horizontes(rows, cards)
        self.assertEqual([c["id"] for c in top], [10, 13])
        by_id = {c["id"]: c for c in top}
        self.assertEqual([ch["id"] for ch in by_id[10]["children"]], [11, 12])
        self.assertEqual(by_id[13]["children"], [])

    def test_child_whose_horizon_is_not_in_this_list_stays_top_level(self):
        rows = [{"id": 20, "horizonte_goal_id": 999}]
        cards = [{"id": 20, "title": "g20"}]
        top = metas._nest_horizontes(rows, cards)
        self.assertEqual([c["id"] for c in top], [20])
        self.assertEqual(top[0]["children"], [])


class GoalsListNestingTest(unittest.TestCase):
    def test_children_nest_under_their_horizon_card(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        goals.activate(conn, ids["horizonte"])
        goals.activate(conn, ids["hijo1"])
        goals.activate(conn, ids["hijo2"])
        conn.commit()
        r = _get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("plan-children", r.text)
        self.assertLess(r.text.index("ZZ Horizonte"), r.text.index("ZZ Pulso en reposo"))

    def test_inaccessible_horizon_leaves_its_child_top_level(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        horizonte_ajeno = goals.propose(conn, 2, "ZZ Horizonte ajeno", START_DAY, kind="milestone",
                                        deadline=DEADLINE)
        gid_hijo = goals.propose(conn, 1, "ZZ Meta huerfana", START_DAY, metric_key="steps", direction="raise",
                                 target_value=9000.0, baseline_value=6000.0, deadline=DEADLINE)
        conn.execute("UPDATE goal SET horizonte_goal_id = ? WHERE id = ?", (horizonte_ajeno, gid_hijo))
        goals.activate(conn, gid_hijo)
        conn.commit()
        r = _get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ZZ Meta huerfana", r.text)
        self.assertNotIn("plan-children", r.text)


class AnotherPersonHorizonIs404Test(unittest.TestCase):
    def test_someone_elses_horizon_is_404_like_any_goal(self):
        conn = _reset_db(self)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        gid = goals.propose(conn, 2, "ZZ Horizonte privado", START_DAY, kind="milestone", deadline=DEADLINE)
        db.crear_meta_semanal(conn, 2, gid, MONDAY_CURRENT.isoformat(), "sesiones_fuerza", "al_menos", 2.0)
        conn.commit()
        r = _get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 404)


class EnglishCoverageTest(unittest.TestCase):
    def test_plan_section_in_english(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        db.set_language(conn, "en", 1)
        r = _get(f"/goals/{ids['horizonte']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("This week of your plan", r.text)
        self.assertIn("In progress", r.text)
        self.assertIn("no watch data yet", r.text)
        self.assertIn("counted by session; heart-rate zone not measured", r.text)
        self.assertIn("End targets", r.text)
        self.assertIn("Next checkpoint", r.text)
        self.assertIn("Past checkpoints", r.text)
        self.assertIn("Experiments in your plan", r.text)

    def test_part_of_the_plan_link_in_english(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        db.set_language(conn, "en", 1)
        r = _get(f"/goals/{ids['hijo1']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Part of the plan:", r.text)

    def test_goals_list_nesting_in_english(self):
        conn = _reset_db(self)
        ids = _build_rich_horizon(conn)
        goals.activate(conn, ids["horizonte"])
        goals.activate(conn, ids["hijo1"])
        db.set_language(conn, "en", 1)
        conn.commit()
        r = _get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("plan-children", r.text)


if __name__ == "__main__":
    unittest.main()
