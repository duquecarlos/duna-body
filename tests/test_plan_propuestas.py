"""BODY-PLAN-PROPUESTAS-1: proponer_plan, ampliar_plan, corregir_meta_plan, leer_plan, leer_planes
-- the whole-plan-is-one-card family (rulings 1-8). Invented demo data only, never a real figure.
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, goals
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.agente.herramientas import ToolError
from body.i18n import T
from body.rutas import asistente, comun


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "p.db"
    conn = db.connect()
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn, orig


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


PLAN_BASE = {"titulo": "Prepararme para una 10K", "start_day": "2026-11-02", "deadline": "2027-02-01"}


class PlanPropuestasTestCase(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def _proponer_plan(self, person_id: int = 1, **over) -> int:
        cid = _cid(self.conn, person_id)
        args = dict(PLAN_BASE, **over)
        out = herramientas.ejecutar(self.conn, person_id, "proponer_plan", args, "ninguno", cid)
        return out["propuesta_id"]

    def _proponer_y_aplicar_plan(self, person_id: int = 1, **over) -> tuple[int, int]:
        pid = self._proponer_plan(person_id, **over)
        res = propuestas.aplicar(self.conn, person_id, pid)
        self.assertEqual(res["estado"], "aplicada")
        goal_id = self.conn.execute(
            "SELECT objetivo_id FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()["objetivo_id"]
        return pid, goal_id


class AtomicWriteTest(PlanPropuestasTestCase):
    def test_crear_plan_horizonte_injected_failure_writes_nothing(self):
        """A duplicate (week_start, medida) in metas_semanales trips the table's own partial
        unique index on the LAST insert this writer makes -- proving the whole transaction,
        including the horizon goal itself, rolls back, never a half-written plan."""
        before_goal = self.conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0]
        before_meta = self.conn.execute("SELECT COUNT(*) FROM horizonte_meta_semanal").fetchone()[0]
        plan = {
            "titulo": "Plan roto", "why": None, "start_day": "2026-11-02", "deadline": "2027-01-01",
            "metas_finales": [], "habitos": [], "reglas": [], "semanas": [],
            "metas_semanales": [
                {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420,
                 "nota": None},
                {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 430,
                 "nota": None},
            ],
            "checkpoints": [],
        }
        with self.assertRaises(db.sqlite3.IntegrityError):
            db.crear_plan_horizonte(self.conn, 1, plan)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0], before_goal)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM horizonte_meta_semanal").fetchone()[0], before_meta)


class ValidationNamesItsItemTest(PlanPropuestasTestCase):
    def test_deadline_not_after_start_day_is_refused(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, "deadline"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan",
                                   dict(PLAN_BASE, deadline="2026-11-02"), "ninguno", cid)

    def test_meta_final_bad_direction_names_its_index(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"metas_finales\[0\]"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, metas_finales=[
                {"titulo": "Bajar FC", "metric_key": "resting_hr", "direction": "sideways",
                 "target_value": 50.0},
            ]), "ninguno", cid)

    def test_habito_sin_titulo_names_its_index(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"habitos\[0\]"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, habitos=[
                {"titulo": "", "cadencia": {"per": "week", "times": 3}},
            ]), "ninguno", cid)

    def test_regla_bad_contexto_names_its_index(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"reglas\[0\]"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, reglas=[
                {"contexto": "nunca", "texto": "Dormir temprano"},
            ]), "ninguno", cid)

    def test_checkpoint_outside_horizon_names_its_index(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"checkpoints\[0\]"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, checkpoints=[
                {"day": "2027-06-01", "titulo": "Chequeo", "criterios": [
                    {"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 2, "ventana": "desde_inicio"}]},
            ]), "ninguno", cid)

    def test_semana_outside_horizon_is_refused(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"semanas\[0\]"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, semanas=[
                {"week_start": "2025-01-06"},
            ]), "ninguno", cid)

    def test_meta_semanal_week_outside_horizon_is_refused(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"metas_semanales\[0\]"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, metas_semanales=[
                {"week_start": "2025-01-06", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
            ]), "ninguno", cid)


class ProponerPlanApplyUndoTest(PlanPropuestasTestCase):
    def test_apply_writes_everything_in_one_go_and_returns_goal_id(self):
        pid, goal_id = self._proponer_y_aplicar_plan(metas_finales=[
            {"titulo": "Bajar FC en reposo", "metric_key": "resting_hr", "direction": "lower",
             "target_value": 50.0},
        ], habitos=[
            {"titulo": "Dormir temprano", "cadencia": {"per": "week", "times": 3}},
        ], reglas=[
            {"contexto": "siempre", "texto": "Hidratarse bien"},
        ], semanas=[
            {"week_start": "2026-11-02", "titulo": "Semana 1"},
        ], metas_semanales=[
            {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
        ], checkpoints=[
            {"day": "2026-12-01", "titulo": "Primer chequeo",
             "criterios": [{"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 2,
                            "ventana": "desde_inicio"}]},
        ])
        self.assertEqual(self.conn.execute("SELECT kind FROM goal WHERE id = ?", (goal_id,)).fetchone()["kind"],
                         "milestone")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM goal WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM habit WHERE goal_id = ?", (goal_id,)).fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM horizonte_regla WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM horizonte_semana WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM horizonte_meta_semanal WHERE horizonte_goal_id = ?",
                (goal_id,)).fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM horizonte_checkpoint WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()[0],
            1)

    def test_undo_of_an_untouched_plan_drops_exactly_what_it_wrote(self):
        pid, goal_id = self._proponer_y_aplicar_plan(habitos=[
            {"titulo": "Dormir temprano", "cadencia": {"per": "week", "times": 3}},
        ], reglas=[{"contexto": "siempre", "texto": "Hidratarse bien"}])
        habit_id = self.conn.execute("SELECT id FROM habit WHERE goal_id = ?", (goal_id,)).fetchone()["id"]

        res = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(res["estado"], "deshecha")
        self.assertEqual(
            self.conn.execute("SELECT status FROM goal WHERE id = ?", (goal_id,)).fetchone()["status"], "dropped")
        self.assertEqual(
            self.conn.execute("SELECT active FROM habit WHERE id = ?", (habit_id,)).fetchone()["active"], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM horizonte_regla WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()[0], 0)

    def test_undo_refused_after_a_habit_check(self):
        pid, goal_id = self._proponer_y_aplicar_plan(habitos=[
            {"titulo": "Dormir temprano", "cadencia": {"per": "week", "times": 3}},
        ])
        habit_id = self.conn.execute("SELECT id FROM habit WHERE goal_id = ?", (goal_id,)).fetchone()["id"]
        goals.check_habit(self.conn, habit_id, 1, "2026-11-03", True, hoy=date(2026, 11, 3))
        self.conn.commit()

        res = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(res["estado"], "propuesta_desactualizada")
        self.assertEqual(
            self.conn.execute("SELECT status FROM goal WHERE id = ?", (goal_id,)).fetchone()["status"], "proposed")

    def test_undo_refused_after_a_correction(self):
        pid, goal_id = self._proponer_y_aplicar_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
        ])
        meta_id = self.conn.execute(
            "SELECT id FROM horizonte_meta_semanal WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()["id"]
        cid = _cid(self.conn)
        corr_pid = herramientas.ejecutar(self.conn, 1, "corregir_meta_plan",
                                         {"meta_id": meta_id, "valor": 400.0}, "ninguno", cid)["propuesta_id"]
        self.assertEqual(propuestas.aplicar(self.conn, 1, corr_pid)["estado"], "aplicada")

        res = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(res["estado"], "propuesta_desactualizada")

    def test_undo_refused_after_an_ampliar_plan(self):
        pid, goal_id = self._proponer_y_aplicar_plan()
        cid = _cid(self.conn)
        amp_pid = herramientas.ejecutar(self.conn, 1, "ampliar_plan", {
            "goal_id": goal_id, "reglas": [{"contexto": "siempre", "texto": "Hidratarse bien"}],
        }, "ninguno", cid)["propuesta_id"]
        self.assertEqual(propuestas.aplicar(self.conn, 1, amp_pid)["estado"], "aplicada")

        res = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(res["estado"], "propuesta_desactualizada")

    def test_undo_refused_after_a_linked_experiment(self):
        pid, goal_id = self._proponer_y_aplicar_plan()
        db.crear_experimento(self.conn, 1, "Ducha fría", ["resting_hr"], {"tipo": "dia"},
                             {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False},
                             fecha_inicio="2026-11-02", goal_id=goal_id)
        self.conn.commit()

        res = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(res["estado"], "propuesta_desactualizada")

    def test_undo_refused_after_a_goal_check(self):
        pid, goal_id = self._proponer_y_aplicar_plan()
        self.conn.execute(
            "INSERT INTO goal_check (goal_id, person_id, week_start, metric_avg, on_track, habits_done, "
            "habits_due, rule_version, note) VALUES (?, 1, '2026-11-02', NULL, 1, 0, 0, 1, NULL)", (goal_id,))
        self.conn.commit()

        res = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(res["estado"], "propuesta_desactualizada")


class ProponerMetaWideningTest(PlanPropuestasTestCase):
    def test_old_shape_row_applies_unchanged(self):
        """A despues_json stored before this slice (no tipo/habitos/horizonte_goal_id keys at
        all) must still apply exactly as a plain metric goal (ruling 2)."""
        cid = _cid(self.conn)
        despues = {"metric_key": "resting_hr", "direction": "lower", "target_value": 50.0,
                   "deadline": "2026-12-01", "titulo": "Bajar FC en reposo", "start_day": "2026-11-02"}
        registro = propuestas.crear(self.conn, 1, cid, "proponer_meta", despues)
        before = self.conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0]
        res = propuestas.aplicar(self.conn, 1, registro["id"])
        self.assertEqual(res["estado"], "aplicada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0], before + 1)
        fila = self.conn.execute("SELECT kind, metric_key FROM goal WHERE title = ?",
                                 ("Bajar FC en reposo",)).fetchone()
        self.assertEqual(fila["kind"], "metric")
        self.assertEqual(fila["metric_key"], "resting_hr")

    def test_milestone_with_habits_applies_and_activates_habits(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_meta", {
            "titulo": "Terminar la 10K", "deadline": "2027-02-01", "tipo": "milestone",
            "habitos": [{"titulo": "Correr 3 veces por semana", "cadencia": {"per": "week", "times": 3}}],
        }, "ninguno", cid)
        res = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(res["estado"], "aplicada")
        goal_id = self.conn.execute("SELECT objetivo_id FROM propuesta_agente WHERE id = ?",
                                    (out["propuesta_id"],)).fetchone()["objetivo_id"]
        fila = self.conn.execute("SELECT kind, metric_key FROM goal WHERE id = ?", (goal_id,)).fetchone()
        self.assertEqual(fila["kind"], "milestone")
        self.assertIsNone(fila["metric_key"])
        habit = self.conn.execute("SELECT title, active FROM habit WHERE goal_id = ?", (goal_id,)).fetchone()
        self.assertEqual(habit["title"], "Correr 3 veces por semana")
        self.assertEqual(habit["active"], 1)

    def test_milestone_with_metric_fields_is_refused(self):
        cid = _cid(self.conn)
        with self.assertRaises(ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_meta", {
                "titulo": "Terminar la 10K", "deadline": "2027-02-01", "tipo": "milestone",
                "metric_key": "resting_hr",
            }, "ninguno", cid)


class CorregirMetaPlanTest(PlanPropuestasTestCase):
    def test_apply_then_undo_leaves_three_rows_with_exactly_one_vigente(self):
        pid, goal_id = self._proponer_y_aplicar_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
        ])
        fila1 = self.conn.execute(
            "SELECT id, valor FROM horizonte_meta_semanal WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()

        cid = _cid(self.conn)
        corr_pid = herramientas.ejecutar(self.conn, 1, "corregir_meta_plan",
                                         {"meta_id": fila1["id"], "valor": 360.0, "nota": "semana dura"},
                                         "ninguno", cid)["propuesta_id"]
        self.assertEqual(propuestas.aplicar(self.conn, 1, corr_pid)["estado"], "aplicada")

        filas = self.conn.execute(
            "SELECT id, valor, vigente FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? ORDER BY id",
            (goal_id,)).fetchall()
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[0]["vigente"], 0)
        self.assertEqual(filas[1]["vigente"], 1)
        self.assertEqual(filas[1]["valor"], 360.0)

        self.assertEqual(propuestas.deshacer(self.conn, 1, corr_pid)["estado"], "deshecha")
        filas = self.conn.execute(
            "SELECT id, valor, vigente FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? ORDER BY id",
            (goal_id,)).fetchall()
        self.assertEqual(len(filas), 3)
        vigentes = [f for f in filas if f["vigente"]]
        self.assertEqual(len(vigentes), 1)
        self.assertEqual(vigentes[0]["valor"], fila1["valor"])

    def test_ya_corregida_is_refused(self):
        pid, goal_id = self._proponer_y_aplicar_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
        ])
        meta_id = self.conn.execute(
            "SELECT id FROM horizonte_meta_semanal WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()["id"]
        cid = _cid(self.conn)
        corr_pid = herramientas.ejecutar(self.conn, 1, "corregir_meta_plan",
                                         {"meta_id": meta_id, "valor": 360.0}, "ninguno", cid)["propuesta_id"]
        propuestas.aplicar(self.conn, 1, corr_pid)
        with self.assertRaises(ToolError):
            herramientas.ejecutar(self.conn, 1, "corregir_meta_plan", {"meta_id": meta_id, "valor": 300.0},
                                  "ninguno", cid)


class AmpliarPlanTest(PlanPropuestasTestCase):
    def test_duplicate_week_medida_against_an_existing_vigente_line_is_refused(self):
        pid, goal_id = self._proponer_y_aplicar_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
        ])
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, "corregir_meta_plan"):
            herramientas.ejecutar(self.conn, 1, "ampliar_plan", {
                "goal_id": goal_id,
                "metas_semanales": [
                    {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 440},
                ],
            }, "ninguno", cid)

    def test_apply_then_undo_removes_exactly_the_rows_it_inserted(self):
        pid, goal_id = self._proponer_y_aplicar_plan(reglas=[
            {"contexto": "siempre", "texto": "Regla original"},
        ])
        cid = _cid(self.conn)
        amp_pid = herramientas.ejecutar(self.conn, 1, "ampliar_plan", {
            "goal_id": goal_id, "reglas": [{"contexto": "siempre", "texto": "Regla nueva"}],
        }, "ninguno", cid)["propuesta_id"]
        self.assertEqual(propuestas.aplicar(self.conn, 1, amp_pid)["estado"], "aplicada")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM horizonte_regla WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()[0], 2)

        self.assertEqual(propuestas.deshacer(self.conn, 1, amp_pid)["estado"], "deshecha")
        restantes = [r["texto"] for r in self.conn.execute(
            "SELECT texto FROM horizonte_regla WHERE horizonte_goal_id = ?", (goal_id,))]
        self.assertEqual(restantes, ["Regla original"])

    def test_undo_refused_once_a_correction_replaced_an_inserted_row(self):
        pid, goal_id = self._proponer_y_aplicar_plan()
        cid = _cid(self.conn)
        amp_pid = herramientas.ejecutar(self.conn, 1, "ampliar_plan", {
            "goal_id": goal_id,
            "metas_semanales": [
                {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
            ],
        }, "ninguno", cid)["propuesta_id"]
        self.assertEqual(propuestas.aplicar(self.conn, 1, amp_pid)["estado"], "aplicada")
        meta_id = self.conn.execute(
            "SELECT id FROM horizonte_meta_semanal WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()["id"]
        corr_pid = herramientas.ejecutar(self.conn, 1, "corregir_meta_plan",
                                         {"meta_id": meta_id, "valor": 360.0}, "ninguno", cid)["propuesta_id"]
        propuestas.aplicar(self.conn, 1, corr_pid)

        res = propuestas.deshacer(self.conn, 1, amp_pid)
        self.assertEqual(res["estado"], "propuesta_desactualizada")


class AnotherPersonsPlanTest(PlanPropuestasTestCase):
    def test_another_persons_plan_is_untouchable(self):
        pid, goal_id = self._proponer_y_aplicar_plan(person_id=1)
        cid2 = _cid(self.conn, 2)

        with self.assertRaises(ToolError):
            herramientas.ejecutar(self.conn, 2, "ampliar_plan",
                                  {"goal_id": goal_id, "reglas": [{"contexto": "siempre", "texto": "x"}]},
                                  "ninguno", cid2)

        meta_id_dummy = 999999
        with self.assertRaises(ToolError):
            herramientas.ejecutar(self.conn, 2, "corregir_meta_plan", {"meta_id": meta_id_dummy, "valor": 1.0},
                                  "ninguno", cid2)

        salida = herramientas.ejecutar(self.conn, 2, "leer_plan", {"goal_id": goal_id}, "resumen")
        self.assertEqual(salida, {"encontrada": False})

        salida_planes = herramientas.ejecutar(self.conn, 2, "leer_planes", {}, "resumen")
        self.assertEqual(salida_planes["planes"], [])


class CardsTest(PlanPropuestasTestCase):
    """Ruling 8: plain-word lines, never a JSON dump, in both es-CO and en."""

    def _render(self, accion: str, despues: dict, antes_json: dict | None, lang: str = "es") -> tuple:
        t = T(lang)
        defs = comun._defs(self.conn)
        return asistente.TARJETAS[accion](t, defs, despues, antes_json, self.conn, 1)

    def test_proponer_plan_card_has_no_raw_json_and_lists_its_parts(self):
        despues = {
            "titulo": "Prepararme para una 10K", "why": "Quiero correr mi primera carrera",
            "start_day": "2026-11-02", "deadline": "2027-02-01",
            "metas_finales": [{"titulo": "Bajar FC en reposo", "metric_key": "resting_hr",
                               "direction": "lower", "target_value": 50.0}],
            "habitos": [{"titulo": "Correr", "cadencia": {"per": "week", "times": 3}}],
            "reglas": [], "semanas": [{"week_start": "2026-11-02"}],
            "metas_semanales": [{"week_start": "2026-11-02", "medida": "sueno_minutos",
                                 "sentido": "al_menos", "valor": 420}],
            "checkpoints": [],
        }
        for lang in ("es", "en"):
            antes, despues_txt, series = self._render("proponer_plan", despues, None, lang)
            self.assertIsNone(antes)
            self.assertIsNone(series)
            self.assertNotIn("{", despues_txt)
            self.assertIn("Prepararme para una 10K", despues_txt)
            self.assertIn("Bajar FC en reposo", despues_txt)
            self.assertIn("Correr", despues_txt)

    def test_ampliar_plan_card_has_no_raw_json(self):
        despues = {"goal_id": 1, "reglas": [{"contexto": "siempre", "texto": "x"}], "semanas": [],
                   "metas_semanales": [{"week_start": "2026-11-02", "medida": "hora_acostarse",
                                        "sentido": "antes_de", "valor": 690}],
                   "checkpoints": []}
        for lang in ("es", "en"):
            antes, despues_txt, series = self._render("ampliar_plan", despues, None, lang)
            self.assertIsNone(antes)
            self.assertNotIn("{", despues_txt)
            self.assertIn("23:30", despues_txt)

    def test_ampliar_plan_card_handles_nothing_added(self):
        despues = {"goal_id": 1, "reglas": [], "semanas": [], "metas_semanales": [], "checkpoints": []}
        _, despues_txt, _ = self._render("ampliar_plan", despues, None)
        self.assertNotIn("{", despues_txt)

    def test_corregir_meta_plan_card_shows_before_and_after_with_unit(self):
        despues = {"meta_id": 1, "valor": 400.0, "nota": None, "medida": "sueno_minutos",
                   "sentido": "al_menos", "week_start": "2026-11-02", "horizonte_goal_id": 1}
        antes_json = {"meta_id": 1, "valor": 420.0, "nota": None, "medida": "sueno_minutos",
                     "sentido": "al_menos", "week_start": "2026-11-02", "horizonte_goal_id": 1}
        for lang in ("es", "en"):
            antes_txt, despues_txt, series = self._render("corregir_meta_plan", despues, antes_json, lang)
            self.assertIsNotNone(antes_txt)
            self.assertNotIn("{", antes_txt)
            self.assertNotIn("{", despues_txt)
            self.assertIn("420", antes_txt)
            self.assertIn("400", despues_txt)

    def test_meta_nueva_card_renders_a_milestone_with_habits(self):
        despues = {"tipo": "milestone", "titulo": "Terminar la 10K", "deadline": "2027-02-01",
                   "habitos": [{"titulo": "Correr", "cadencia": {"per": "week", "times": 3}}]}
        antes, despues_txt, series = self._render("proponer_meta", despues, None)
        self.assertIsNone(antes)
        self.assertNotIn("{", despues_txt)
        self.assertIn("Terminar la 10K", despues_txt)
        self.assertIn("Correr", despues_txt)

    def test_meta_nueva_card_old_shape_still_renders(self):
        despues = {"metric_key": "resting_hr", "direction": "lower", "target_value": 50.0,
                   "deadline": "2026-12-01", "titulo": "Bajar FC en reposo"}
        antes, despues_txt, series = self._render("proponer_meta", despues, None)
        self.assertNotIn("{", despues_txt)


if __name__ == "__main__":
    unittest.main()
