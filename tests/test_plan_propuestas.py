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
            self.assertIn("7 h", antes_txt)
            self.assertIn("6 h 40", despues_txt)

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


# BODY-PLAN-PROPUESTAS-1-R: one invented plan fixture -- 2 reglas, 12 metas_semanales across 4
# weeks, 1 checkpoint -- the exact count line and first-week line the orchestrator's 375 px look
# approved the LAYOUT for, the TEXT rulings 1a-1e built here. Every date below is invented demo
# data, never a real figure.
PLAN_FIXTURE_DESPUES = {
    "titulo": "Preparar la media maratón", "why": None,
    "start_day": "2026-11-02", "deadline": "2027-02-01",
    "metas_finales": [], "habitos": [],
    "reglas": [
        {"contexto": "siempre", "texto": "Hidratarse bien"},
        {"contexto": "turno", "contexto_valor": "noche", "texto": "Dormir una siesta corta"},
    ],
    "semanas": [{"week_start": "2026-11-09", "nota": "Semana de arranque"}],
    "metas_semanales": [
        {"week_start": "2026-11-09", "medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 2},
        {"week_start": "2026-11-09", "medida": "sesiones_cardio", "sentido": "al_menos", "valor": 3},
        {"week_start": "2026-11-09", "medida": "hora_acostarse", "sentido": "antes_de", "valor": 660},
        {"week_start": "2026-11-16", "medida": "sesiones_fuerza", "sentido": "como_maximo", "valor": 1},
        {"week_start": "2026-11-16", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
        {"week_start": "2026-11-16", "medida": "hora_levantarse", "sentido": "despues_de", "valor": 1110},
        {"week_start": "2026-11-23", "medida": "minutos_intensidad", "sentido": "al_menos", "valor": 30},
        {"week_start": "2026-11-23", "medida": "sesiones_cardio", "sentido": "como_maximo", "valor": 1},
        {"week_start": "2026-11-23", "medida": "resting_hr", "sentido": "como_maximo", "valor": 55},
        {"week_start": "2026-11-30", "medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 3},
        {"week_start": "2026-11-30", "medida": "sesiones_cardio", "sentido": "al_menos", "valor": 2},
        {"week_start": "2026-11-30", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 400},
    ],
    "checkpoints": [{"day": "2026-12-01", "titulo": "Primer chequeo", "criterios": []}],
}

_PROHIBIDO_POR_IDIOMA = {
    "es": ("(s)", "checkpoint", "sesiones de cardio al menos 3 sesiones"),
    "en": ("(s)",),
}


class PlanTextRulingsTest(PlanPropuestasTestCase):
    """BODY-PLAN-PROPUESTAS-1-R rulings 1a-1e: the cards read as plain sentences, in both
    languages -- the measure's noun said once, counts agreeing in number, no "(s)" form and no
    English "checkpoint" leaking into the Spanish card."""

    def _render(self, accion: str, despues: dict, antes_json: dict | None, lang: str) -> tuple:
        t = T(lang)
        defs = comun._defs(self.conn)
        return asistente.TARJETAS[accion](t, defs, despues, antes_json, self.conn, 1)

    def test_proponer_plan_count_line_and_first_week_line_exact(self):
        esperado = {
            "es": {
                "resumen": "2 reglas · 4 semanas con metas · 1 chequeo",
                "primera_semana": "Primera semana (9 nov): al menos 2 sesiones de fuerza · "
                                   "al menos 3 sesiones de cardio · acostarse antes de las 23:00",
            },
            "en": {
                "resumen": "2 rules · 4 weeks with targets · 1 checkpoint",
                "primera_semana": "First week (Nov 9): at least 2 strength sessions · "
                                   "at least 3 cardio sessions · in bed before 23:00",
            },
        }
        for lang in ("es", "en"):
            _, despues_txt, _ = self._render("proponer_plan", PLAN_FIXTURE_DESPUES, None, lang)
            lineas = despues_txt.split("\n")
            self.assertIn(esperado[lang]["resumen"], lineas, f"[{lang}] count line")
            self.assertIn(esperado[lang]["primera_semana"], lineas, f"[{lang}] first-week line")

    def test_proponer_plan_count_line_uses_singular_at_one(self):
        despues = dict(PLAN_FIXTURE_DESPUES, reglas=[{"contexto": "siempre", "texto": "x"}],
                       metas_semanales=[{"week_start": "2026-11-09", "medida": "sueno_minutos",
                                        "sentido": "al_menos", "valor": 420}],
                       checkpoints=[{"day": "2026-12-01", "titulo": "c", "criterios": []}])
        esperado = {"es": "1 regla · 1 semana con metas · 1 chequeo",
                   "en": "1 rule · 1 week with targets · 1 checkpoint"}
        for lang in ("es", "en"):
            _, despues_txt, _ = self._render("proponer_plan", despues, None, lang)
            self.assertIn(esperado[lang], despues_txt.split("\n"), f"[{lang}]")

    def test_proponer_plan_drops_a_plan_with_no_weekly_targets_first_week_line(self):
        despues = dict(PLAN_FIXTURE_DESPUES, metas_semanales=[])
        for lang in ("es", "en"):
            _, despues_txt, _ = self._render("proponer_plan", despues, None, lang)
            self.assertNotIn("Primera semana", despues_txt)
            self.assertNotIn("First week", despues_txt)

    def test_ampliar_plan_lines_exact(self):
        despues = {
            "goal_id": 1, "reglas": [{"contexto": "siempre", "texto": "x"}], "semanas": [],
            "metas_semanales": [
                {"week_start": "2026-11-09", "medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 2},
                {"week_start": "2026-11-16", "medida": "hora_acostarse", "sentido": "antes_de", "valor": 660},
            ],
            "checkpoints": [{"day": "2026-12-01", "titulo": "c1", "criterios": []},
                            {"day": "2026-12-08", "titulo": "c2", "criterios": []}],
        }
        esperado = {
            "es": ["1 regla nueva",
                  "Metas semanales nuevas: al menos 2 sesiones de fuerza (semana del 9 nov 2026) · "
                  "acostarse antes de las 23:00 (semana del 16 nov 2026)",
                  "2 chequeos nuevos"],
            "en": ["1 new rule",
                  "New weekly targets: at least 2 strength sessions (week of 9 Nov 2026) · "
                  "in bed before 23:00 (week of 16 Nov 2026)",
                  "2 new checkpoints"],
        }
        for lang in ("es", "en"):
            _, despues_txt, _ = self._render("ampliar_plan", despues, None, lang)
            self.assertEqual(despues_txt.split("\n"), esperado[lang], f"[{lang}]")

    def test_corregir_meta_plan_minuto_measure_exact(self):
        despues = {"meta_id": 1, "valor": 400.0, "nota": None, "medida": "sueno_minutos",
                   "sentido": "al_menos", "week_start": "2026-11-02", "horizonte_goal_id": 1}
        antes_json = dict(despues, valor=420.0)
        esperado = {
            "es": ("sueño: al menos 7 h (semana del 2 nov 2026)",
                  "sueño: al menos 6 h 40 (semana del 2 nov 2026)"),
            "en": ("sleep: at least 7 h (week of 2 Nov 2026)",
                  "sleep: at least 6 h 40 (week of 2 Nov 2026)"),
        }
        for lang in ("es", "en"):
            antes_txt, despues_txt, _ = self._render("corregir_meta_plan", despues, antes_json, lang)
            self.assertEqual((antes_txt, despues_txt), esperado[lang], f"[{lang}]")

    def test_corregir_meta_plan_sesion_measure_exact_with_singular(self):
        despues = {"meta_id": 1, "valor": 1.0, "nota": None, "medida": "sesiones_cardio",
                   "sentido": "al_menos", "week_start": "2026-11-02", "horizonte_goal_id": 1}
        antes_json = dict(despues, valor=2.0)
        esperado = {
            "es": ("al menos 2 sesiones de cardio (semana del 2 nov 2026)",
                  "al menos 1 sesión de cardio (semana del 2 nov 2026)"),
            "en": ("at least 2 cardio sessions (week of 2 Nov 2026)",
                  "at least 1 cardio session (week of 2 Nov 2026)"),
        }
        for lang in ("es", "en"):
            antes_txt, despues_txt, _ = self._render("corregir_meta_plan", despues, antes_json, lang)
            self.assertEqual((antes_txt, despues_txt), esperado[lang], f"[{lang}]")

    def test_corregir_meta_plan_reloj_measure_exact(self):
        despues = {"meta_id": 1, "valor": 630.0, "nota": None, "medida": "hora_acostarse",
                   "sentido": "antes_de", "week_start": "2026-11-02", "horizonte_goal_id": 1}
        antes_json = dict(despues, valor=660.0)
        esperado = {
            "es": ("acostarse antes de las 23:00 (semana del 2 nov 2026)",
                  "acostarse antes de las 22:30 (semana del 2 nov 2026)"),
            "en": ("in bed before 23:00 (week of 2 Nov 2026)",
                  "in bed before 22:30 (week of 2 Nov 2026)"),
        }
        for lang in ("es", "en"):
            antes_txt, despues_txt, _ = self._render("corregir_meta_plan", despues, antes_json, lang)
            self.assertEqual((antes_txt, despues_txt), esperado[lang], f"[{lang}]")

    def test_corregir_meta_plan_metric_def_fuera_de_medidas_exact(self):
        """validar_medida's else branch -- a metric_def key outside horizonte.MEDIDAS, the card's
        own metric-value helper, never an invented unit."""
        despues = {"meta_id": 1, "valor": 50.0, "nota": None, "medida": "resting_hr",
                   "sentido": "como_maximo", "week_start": "2026-11-02", "horizonte_goal_id": 1}
        antes_json = dict(despues, valor=55.0)
        esperado = {
            "es": ("Pulso en reposo: como máximo 55 lpm (semana del 2 nov 2026)",
                  "Pulso en reposo: como máximo 50 lpm (semana del 2 nov 2026)"),
            "en": ("Resting heart rate: at most 55 bpm (week of 2 Nov 2026)",
                  "Resting heart rate: at most 50 bpm (week of 2 Nov 2026)"),
        }
        for lang in ("es", "en"):
            antes_txt, despues_txt, _ = self._render("corregir_meta_plan", despues, antes_json, lang)
            self.assertEqual((antes_txt, despues_txt), esperado[lang], f"[{lang}]")

    def test_no_forbidden_text_across_the_four_plan_actions(self):
        """No "(s)" form anywhere, no English "checkpoint" leaking into the Spanish card, no measure
        noun said twice (BODY-PLAN-PROPUESTAS-1-R ruling 1b)."""
        meta_nueva_despues = {"tipo": "milestone", "titulo": "Terminar la 10K", "deadline": "2027-02-01",
                              "habitos": [{"titulo": "Correr", "cadencia": {"per": "week", "times": 3}}]}
        corregir_despues = {"meta_id": 1, "valor": 1.0, "nota": None, "medida": "sesiones_cardio",
                            "sentido": "al_menos", "week_start": "2026-11-02", "horizonte_goal_id": 1}
        corregir_antes = dict(corregir_despues, valor=2.0)
        ampliar_despues = {
            "goal_id": 1, "reglas": [{"contexto": "siempre", "texto": "x"}], "semanas": [],
            "metas_semanales": [{"week_start": "2026-11-09", "medida": "sesiones_cardio",
                                 "sentido": "al_menos", "valor": 3}],
            "checkpoints": [{"day": "2026-12-01", "titulo": "c1", "criterios": []},
                            {"day": "2026-12-08", "titulo": "c2", "criterios": []}],
        }
        for lang in ("es", "en"):
            textos = [
                self._render("proponer_plan", PLAN_FIXTURE_DESPUES, None, lang)[1],
                self._render("ampliar_plan", ampliar_despues, None, lang)[1],
                self._render("corregir_meta_plan", corregir_despues, corregir_antes, lang)[0],
                self._render("corregir_meta_plan", corregir_despues, corregir_antes, lang)[1],
                self._render("proponer_meta", meta_nueva_despues, None, lang)[1],
            ]
            for prohibido in _PROHIBIDO_POR_IDIOMA[lang]:
                for texto in textos:
                    self.assertNotIn(prohibido, texto, f"[{lang}] {prohibido!r} found in {texto!r}")


class HoraEntradaToolTest(PlanPropuestasTestCase):
    """BODY-PLAN-HORA-ENTRADA-1 ruling 1: a clock measure's (hora_acostarse, hora_levantarse)
    `valor` crosses the tool boundary as "HH:MM" -- converted to horizonte's own
    minutes-after-noon storage on the way in, and back to "HH:MM" on the way out through
    leer_plan. A raw number, or a string outside HH:MM, is refused naming the field; every other
    measure keeps taking a plain number, unchanged."""

    def _despues(self, pid: int) -> dict:
        row = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
        return json.loads(row["despues_json"])

    def test_hhmm_in_meta_semanal_is_stored_as_minutes_after_noon(self):
        pid = self._proponer_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "hora_acostarse", "sentido": "antes_de", "valor": "23:00"},
        ])
        self.assertEqual(self._despues(pid)["metas_semanales"][0]["valor"], 660.0)

    def test_hhmm_wrapping_past_midnight_is_stored_as_minutes_after_noon(self):
        pid = self._proponer_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "hora_levantarse", "sentido": "despues_de", "valor": "00:45"},
        ])
        self.assertEqual(self._despues(pid)["metas_semanales"][0]["valor"], 765.0)

    def test_proponer_plan_card_renders_the_hhmm_target_in_both_languages(self):
        pid = self._proponer_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "hora_acostarse", "sentido": "antes_de", "valor": "23:00"},
        ])
        despues = self._despues(pid)
        defs = comun._defs(self.conn)
        for lang, esperado in (("es", "acostarse antes de las 23:00"), ("en", "in bed before 23:00")):
            _, texto, _ = asistente.TARJETAS["proponer_plan"](T(lang), defs, despues, None, self.conn, 1)
            self.assertIn(esperado, texto, f"[{lang}]")

    def test_numero_for_non_clock_measure_is_still_accepted(self):
        pid = self._proponer_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "sueno_minutos", "sentido": "al_menos", "valor": 420},
        ])
        self.assertEqual(self._despues(pid)["metas_semanales"][0]["valor"], 420.0)

    def test_numero_for_clock_measure_in_meta_semanal_is_refused_naming_the_item(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"metas_semanales\[0\]\.valor"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, metas_semanales=[
                {"week_start": "2026-11-02", "medida": "hora_acostarse", "sentido": "antes_de", "valor": 1380},
            ]), "ninguno", cid)

    def test_numero_for_clock_measure_in_ampliar_plan_is_refused_naming_the_item(self):
        pid, goal_id = self._proponer_y_aplicar_plan()
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"metas_semanales\[0\]\.valor"):
            herramientas.ejecutar(self.conn, 1, "ampliar_plan", {
                "goal_id": goal_id,
                "metas_semanales": [
                    {"week_start": "2026-11-02", "medida": "hora_acostarse", "sentido": "antes_de", "valor": 1380},
                ],
            }, "ninguno", cid)

    def test_numero_for_clock_measure_in_checkpoint_criterio_is_refused_naming_the_item(self):
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, r"checkpoints\[0\]\.criterios\[0\]\.valor"):
            herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, checkpoints=[
                {"day": "2026-12-01", "titulo": "Chequeo", "criterios": [
                    {"medida": "hora_acostarse", "sentido": "antes_de", "valor": 1380, "ventana": "semana"}]},
            ]), "ninguno", cid)

    def test_numero_for_clock_measure_in_corregir_meta_plan_is_refused(self):
        pid, goal_id = self._proponer_y_aplicar_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "hora_acostarse", "sentido": "antes_de", "valor": "23:00"},
        ])
        meta_id = self.conn.execute(
            "SELECT id FROM horizonte_meta_semanal WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()["id"]
        cid = _cid(self.conn)
        with self.assertRaisesRegex(ToolError, "valor"):
            herramientas.ejecutar(self.conn, 1, "corregir_meta_plan",
                                  {"meta_id": meta_id, "valor": 1380}, "ninguno", cid)

    def test_hhmm_in_corregir_meta_plan_is_accepted_and_converted(self):
        pid, goal_id = self._proponer_y_aplicar_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "hora_acostarse", "sentido": "antes_de", "valor": "23:00"},
        ])
        meta_id = self.conn.execute(
            "SELECT id FROM horizonte_meta_semanal WHERE horizonte_goal_id = ?", (goal_id,)).fetchone()["id"]
        cid = _cid(self.conn)
        corr_pid = herramientas.ejecutar(self.conn, 1, "corregir_meta_plan",
                                         {"meta_id": meta_id, "valor": "22:30"}, "ninguno", cid)["propuesta_id"]
        self.assertEqual(self._despues(corr_pid)["valor"], 630.0)

    def test_bad_hhmm_strings_are_refused(self):
        cid = _cid(self.conn)
        for valor in ("24:00", "7pm", ""):
            with self.assertRaisesRegex(ToolError, r"metas_semanales\[0\]\.valor", msg=repr(valor)):
                herramientas.ejecutar(self.conn, 1, "proponer_plan", dict(PLAN_BASE, metas_semanales=[
                    {"week_start": "2026-11-02", "medida": "hora_acostarse", "sentido": "antes_de", "valor": valor},
                ]), "ninguno", cid)

    def test_leer_plan_returns_hhmm_for_the_targets_clock_line(self):
        pid, goal_id = self._proponer_y_aplicar_plan(metas_semanales=[
            {"week_start": "2026-11-02", "medida": "hora_acostarse", "sentido": "antes_de", "valor": "23:00"},
        ])
        out = herramientas.ejecutar(self.conn, 1, "leer_plan",
                                    {"goal_id": goal_id, "desde": "2026-11-02", "hasta": "2026-11-02"},
                                    "resumen", None)
        linea = out["plan_vs_real"][0]["lineas"][0]
        self.assertEqual(linea["medida"], "hora_acostarse")
        self.assertEqual(linea["valor"], "23:00")


if __name__ == "__main__":
    unittest.main()
