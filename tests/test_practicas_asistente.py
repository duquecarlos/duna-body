"""BODY-HISTORIA-VIDA-2 ruling 2/3: leer_practicas, proponer_practica -- list, detail bounded to
90 days, propose -> apply -> the practica exists with origen 'asistente' -> undo (archived once a
habit cites it, hard-deleted otherwise); refused on another person's practica (a habit item's
practica_id) and on an invalid rule; present in the lectura key, absent from its write tools; es/en
card text in number agreement. Invented demo data only, never a real figure."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, mcp, practicas
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


def _activity(conn, person_id, source_id, day, *, type, duration_s=1800.0, source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, type, duration_s, source) VALUES (?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, type, duration_s, source),
    )


class PracticasAsistenteTestCase(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.cid = _cid(self.conn)

    def _proponer(self, person_id: int = 1, **over):
        args = dict(titulo="Correr", fuente="actividad", regla={"tipos": ["running"]}, meta_dias_semana=4)
        args.update(over)
        return herramientas.ejecutar(self.conn, person_id, "proponer_practica", args, "ninguno", self.cid)

    def _proponer_y_aplicar(self, person_id: int = 1, **over) -> int:
        out = self._proponer(person_id, **over)
        res = propuestas.aplicar(self.conn, person_id, out["propuesta_id"])
        self.assertEqual(res["estado"], "aplicada")
        despues = self.conn.execute(
            "SELECT despues_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)).fetchone()
        import json
        return json.loads(despues[0])["practica_id"], out["propuesta_id"]


# ---- leer_practicas: list ------------------------------------------------------------------------

class LeerPracticasListTest(PracticasAsistenteTestCase):
    def test_lists_active_practices_with_streak_and_suggests_unmade_ones(self):
        pid = practicas.crear(self.conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        self.conn.commit()
        hoy = date(2026, 11, 10)
        for i in range(8):
            d = hoy - timedelta(days=i * 7)
            _activity(self.conn, 1, f"run-{i}", d.isoformat(), type="cycling")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_practicas", {}, "resumen", self.cid)
        self.assertEqual(len(out["practicas"]), 1)
        self.assertEqual(out["practicas"][0]["id"], pid)
        self.assertIn("racha_actual", out["practicas"][0])
        self.assertIn("semana_actual", out["practicas"][0])
        claves = [s["regla"] for s in out["sugerencias"]]
        self.assertIn({"tipos": ["cycling"]}, claves)

    def test_no_practices_is_an_empty_list_not_an_error(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_practicas", {}, "resumen", self.cid)
        self.assertEqual(out["practicas"], [])


# ---- leer_practicas: detail, bounded to 90 days --------------------------------------------------

class LeerPracticasDetailTest(PracticasAsistenteTestCase):
    def test_detail_is_bounded_to_the_last_90_days(self):
        pid = practicas.crear(self.conn, 1, "Correr", "actividad", {"tipos": ["running"]}, meta_dias_semana=3)
        old_day = "2025-01-01"
        _activity(self.conn, 1, "old", old_day, type="running")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_practicas", {"practica_id": pid}, "resumen", self.cid)
        self.assertTrue(out["encontrada"])
        self.assertNotIn(old_day, out["practica"]["dias"])
        self.assertEqual(len(out["practica"]["dias"]), 90)

    def test_unknown_id_is_not_found_never_a_tool_error(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_practicas", {"practica_id": 9999}, "resumen", self.cid)
        self.assertFalse(out["encontrada"])

    def test_another_persons_practica_is_not_found(self):
        pid = practicas.crear(self.conn, 2, "Otra", "actividad", {"tipos": ["running"]})
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_practicas", {"practica_id": pid}, "resumen", self.cid)
        self.assertFalse(out["encontrada"])


# ---- proponer_practica: propose -> apply -> undo -------------------------------------------------

class ProponerPracticaFlowTest(PracticasAsistenteTestCase):
    def test_propose_refuses_an_invalid_rule(self):
        with self.assertRaisesRegex(ToolError, "regla"):
            self._proponer(regla={"tipos": []})

    def test_propose_refuses_a_missing_title(self):
        with self.assertRaisesRegex(ToolError, "título"):
            self._proponer(titulo="  ")

    def test_propose_refuses_a_bad_meta_dias_semana(self):
        with self.assertRaisesRegex(ToolError, "meta_dias_semana"):
            self._proponer(meta_dias_semana=8)

    def test_apply_creates_the_practica_with_origen_asistente(self):
        pid, _ = self._proponer_y_aplicar()
        row = self.conn.execute("SELECT titulo, origen FROM practica WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["titulo"], "Correr")
        self.assertEqual(row["origen"], "asistente")

    def test_undo_hard_deletes_an_unreferenced_practica(self):
        pid, prop_id = self._proponer_y_aplicar()
        res = propuestas.deshacer(self.conn, 1, prop_id)
        self.assertEqual(res["estado"], "deshecha")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM practica WHERE id = ?", (pid,)).fetchone())

    def test_undo_archives_a_practica_a_habit_has_linked_to(self):
        pid, prop_id = self._proponer_y_aplicar()
        self.conn.execute(
            "INSERT INTO goal (person_id, hogar_id, area, title, kind, metric_key, direction, target_value, "
            "baseline_value, start_day, status, visibility) VALUES "
            "(1, NULL, 'body', 'Meta', 'metric', 'resting_hr', 'lower', 55, 60, '2026-10-01', 'active', 'private')"
        )
        gid = self.conn.execute("SELECT id FROM goal WHERE title = 'Meta'").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO habit (goal_id, title, cadence, active, practica_id) VALUES "
            "(?, 'Retomar correr', '{\"per\": \"week\", \"times\": 3}', 1, ?)", (gid, pid))
        self.conn.commit()
        res = propuestas.deshacer(self.conn, 1, prop_id)
        self.assertEqual(res["estado"], "deshecha")
        row = self.conn.execute("SELECT archivada_en FROM practica WHERE id = ?", (pid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["archivada_en"])


# ---- a habit's practica_id: another person's practica is refused --------------------------------

class HabitoPracticaAjenaTest(PracticasAsistenteTestCase):
    def test_proponer_meta_refuses_a_habit_linked_to_another_persons_practica(self):
        ajena = practicas.crear(self.conn, 2, "Ajena", "actividad", {"tipos": ["running"]})
        self.conn.commit()
        with self.assertRaisesRegex(ToolError, "practica_id"):
            herramientas.ejecutar(
                self.conn, 1, "proponer_meta",
                {"deadline": "2026-12-01", "titulo": "M", "tipo": "metric", "metric_key": "resting_hr",
                 "direction": "lower", "target_value": 55,
                 "habitos": [{"titulo": "Retomar correr", "cadencia": {"per": "week", "times": 3},
                              "practica_id": ajena}]},
                "ninguno", self.cid)

    def test_proponer_meta_refuses_an_archived_practica(self):
        propia = practicas.crear(self.conn, 1, "Propia", "actividad", {"tipos": ["running"]})
        practicas.archivar(self.conn, 1, propia)
        self.conn.commit()
        with self.assertRaisesRegex(ToolError, "practica_id"):
            herramientas.ejecutar(
                self.conn, 1, "proponer_meta",
                {"deadline": "2026-12-01", "titulo": "M", "tipo": "metric", "metric_key": "resting_hr",
                 "direction": "lower", "target_value": 55,
                 "habitos": [{"titulo": "Retomar correr", "cadencia": {"per": "week", "times": 3},
                              "practica_id": propia}]},
                "ninguno", self.cid)

    def test_proponer_meta_accepts_the_persons_own_active_practica(self):
        propia = practicas.crear(self.conn, 1, "Propia", "actividad", {"tipos": ["running"]})
        self.conn.commit()
        out = herramientas.ejecutar(
            self.conn, 1, "proponer_meta",
            {"deadline": "2026-12-01", "titulo": "M", "tipo": "metric", "metric_key": "resting_hr",
             "direction": "lower", "target_value": 55,
             "habitos": [{"titulo": "Retomar correr", "cadencia": {"per": "week", "times": 3},
                          "practica_id": propia}]},
            "ninguno", self.cid)
        self.assertIn("propuesta_id", out)


# ---- present in the lectura key, absent from its write tools -------------------------------------

class ClasificacionHerramientaTest(unittest.TestCase):
    def test_leer_practicas_is_a_lectura_tool(self):
        self.assertEqual(herramientas.tipo_de("leer_practicas"), "lectura")
        self.assertIn("leer_practicas", mcp._NOMBRES_LECTURA)

    def test_proponer_practica_is_a_propuesta_tool_not_in_the_lectura_key(self):
        self.assertEqual(herramientas.tipo_de("proponer_practica"), "propuesta")
        self.assertNotIn("proponer_practica", mcp._NOMBRES_LECTURA)


# ---- card: one line, number agreement, never raises -----------------------------------------------

class CardTextTest(PracticasAsistenteTestCase):
    def setUp(self):
        super().setUp()
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, meta_dias_semana: int):
        out = self._proponer(meta_dias_semana=meta_dias_semana)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_card_names_title_and_days_plural_es(self):
        t = self._tarjeta("es", 4)
        self.assertIn("Correr", t["despues"])
        self.assertIn("4 días por semana", t["despues"])

    def test_card_names_title_and_days_singular_es(self):
        t = self._tarjeta("es", 1)
        self.assertIn("1 día por semana", t["despues"])
        self.assertNotIn("1 días", t["despues"])

    def test_card_names_title_and_days_plural_en(self):
        t = self._tarjeta("en", 4)
        self.assertIn("Correr", t["despues"])
        self.assertIn("4 days a week", t["despues"])

    def test_card_names_title_and_days_singular_en(self):
        t = self._tarjeta("en", 1)
        self.assertIn("1 day a week", t["despues"])
        self.assertNotIn("1 days", t["despues"])


if __name__ == "__main__":
    unittest.main()
