"""BODY-ENTRENO-HABITO-AUTO-1: `marcar_habito` on the existing propose/apply/undo pattern -- a
generic "check habit H for day D, from this evidence" tool, ball sports being one use of it, never
a hardcoded activity-type list (BODY-ENTRENO-1-INV slice 5). Invented demo data only.
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, goals, identity, web
from body.agente import cliente as agente_cliente
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.agente.cliente import BloqueTexto, LlamadaHerramienta, Respuesta, Uso

HOY = date(2026, 11, 9)


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _seed_habito(conn, person_id: int = 1, *, assignee_person_id=None, active: bool = True) -> tuple[int, int]:
    gid = goals.propose(conn, person_id, "Jugar más deporte", "2026-01-01", kind="milestone",
                        deadline="2026-12-30")
    hid = goals.add_habit(conn, gid, "Jugar pádel", {"per": "week", "times": 1},
                          assignee_person_id=assignee_person_id, active=active)
    conn.commit()
    return gid, hid


def _seed_hogar_habito(conn, hogar_id: str = "h-1", *, assignee_person_id: int = 1) -> tuple[int, int]:
    conn.executemany(
        "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
        [(1, hogar_id), (2, hogar_id)])
    gid = goals.propose(conn, 1, "Meta compartida", "2026-01-01", kind="milestone", deadline="2026-12-30",
                        hogar_id=hogar_id, visibility="household")
    hid = goals.add_habit(conn, gid, "Jugar pádel", {"per": "week", "times": 1},
                          assignee_person_id=assignee_person_id)
    conn.commit()
    return gid, hid


def _insert_activity(conn, person_id, source_id, day, name="Pádel", type_="other", duration_s=3120.0,
                      source="garmin"):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, source_id, day, f"{day}T18:00:00", type_, name, duration_s, source),
    )


class ProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_propose_writes_nothing_to_habit_check(self):
        _, hid = _seed_habito(self.conn)
        before = self.conn.execute("SELECT COUNT(*) FROM habit_check").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                    "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM habit_check").fetchone()[0], before)

    def test_apply_marks_it_done_only_for_that_person(self):
        _, hid = _seed_habito(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                    "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT done, note FROM habit_check WHERE habit_id = ? AND person_id = 1 AND day = '2026-01-15'",
            (hid,)).fetchone()
        self.assertEqual(row["done"], 1)
        self.assertIsNone(row["note"])

    def test_undo_removes_the_row_when_there_was_none_before(self):
        _, hid = _seed_habito(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT * FROM habit_check WHERE habit_id = ? AND person_id = 1 AND day = '2026-01-15'",
            (hid,)).fetchone()
        self.assertIsNone(row)

    def test_undo_restores_the_prior_not_done_row(self):
        _, hid = _seed_habito(self.conn)
        goals.check_habit(self.conn, hid, 1, "2026-01-15", False, "nota vieja", hoy=HOY)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT done, note FROM habit_check WHERE habit_id = ? AND person_id = 1 AND day = '2026-01-15'",
            (hid,)).fetchone()
        self.assertEqual(row["done"], 0)
        self.assertEqual(row["note"], "nota vieja")

    def test_drift_refuses_when_the_row_changed_after_propose(self):
        _, hid = _seed_habito(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                    "ninguno", self.cid)
        goals.check_habit(self.conn, hid, 1, "2026-01-15", True, None, hoy=HOY)  # checked directly meanwhile
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_undo_refuses_when_the_row_changed_again_after_applying(self):
        _, hid = _seed_habito(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        goals.check_habit(self.conn, hid, 1, "2026-01-15", True, "nota nueva", hoy=HOY)
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_person_b_cannot_apply_person_as_proposal(self):
        _, hid = _seed_habito(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                    "ninguno", self.cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])

    def test_apply_with_an_activity_keeps_it_as_evidence(self):
        _, hid = _seed_habito(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-01-15")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", {
            "habit_id": hid, "day": "2026-01-15", "actividad": {"source": "garmin", "source_id": "a1"},
        }, "ninguno", self.cid)
        registro = self.conn.execute("SELECT despues_json FROM propuesta_agente WHERE id = ?",
                                     (out["propuesta_id"],)).fetchone()
        despues = json.loads(registro["despues_json"])
        self.assertEqual(despues["actividad"]["nombre"], "Pádel")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")


class RefusalsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_future_day_is_refused(self):
        _, hid = _seed_habito(self.conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2099-01-01"},
                                  "ninguno", self.cid)

    def test_unknown_habit_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": 999, "day": "2026-01-15"},
                                  "ninguno", self.cid)

    def test_inactive_habit_is_refused(self):
        _, hid = _seed_habito(self.conn, active=False)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                  "ninguno", self.cid)

    def test_another_persons_private_habit_is_refused(self):
        # Person isolation: person 1's habit is on a private goal person 2 has no access to.
        _, hid = _seed_habito(self.conn, person_id=1)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 2, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                  "ninguno", _cid(self.conn, 2))

    def test_a_household_members_own_assigned_habit_is_not_theirs_to_check(self):
        _, hid = _seed_hogar_habito(self.conn, assignee_person_id=1)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 2, "marcar_habito", {"habit_id": hid, "day": "2026-01-15"},
                                  "ninguno", _cid(self.conn, 2))

    def test_an_activity_belonging_to_another_person_is_refused(self):
        _, hid = _seed_habito(self.conn)
        _insert_activity(self.conn, 2, "a1", "2026-01-15")  # person 2's activity, not person 1's
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_habito", {
                "habit_id": hid, "day": "2026-01-15", "actividad": {"source": "garmin", "source_id": "a1"},
            }, "ninguno", self.cid)

    def test_an_activity_on_a_different_day_is_refused(self):
        _, hid = _seed_habito(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-01-14")  # a day earlier than the proposed check
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_habito", {
                "habit_id": hid, "day": "2026-01-15", "actividad": {"source": "garmin", "source_id": "a1"},
            }, "ninguno", self.cid)


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "h.db"
    conn = db.connect()
    db.init(conn)
    conn.commit()
    return conn, orig


class FakeProveedor:
    def __init__(self, guion):
        self.guion = list(guion)

    def responder(self, sistema, mensajes, herramientas_):
        return self.guion.pop(0)


def _evento_data(texto: str, nombre: str) -> dict:
    lineas = texto.splitlines()
    idx = lineas.index(f"event: {nombre}")
    return json.loads(lineas[idx + 1][len("data:"):].strip())


class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _marcar(self, params: dict, pregunta: str = "marca mi hábito"):
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "marcar_habito", params)],
                     motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar", json={"pregunta": pregunta, "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        return _evento_data(r.text, "propuesta")

    def test_card_with_evidence_in_spanish(self):
        _, hid = _seed_habito(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-01-15")
        self.conn.commit()
        datos = self._marcar({"habit_id": hid, "day": "2026-01-15",
                              "actividad": {"source": "garmin", "source_id": "a1"}})
        self.assertEqual(datos["accion"], "marcar_habito")
        self.assertEqual(datos["titulo"], "Marcar hábito")
        self.assertIsNone(datos["antes"])
        self.assertEqual(datos["despues"],
                         "Propuesta: Jugar pádel · hecho el 15 ene 2026, según tu actividad: Pádel · 52 min")

    def test_card_without_evidence_in_spanish(self):
        _, hid = _seed_habito(self.conn)
        datos = self._marcar({"habit_id": hid, "day": "2026-01-15"})
        self.assertEqual(datos["despues"], "Propuesta: Jugar pádel · hecho el 15 ene 2026")

    def test_card_with_evidence_in_english(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        _, hid = _seed_habito(self.conn)
        _insert_activity(self.conn, 1, "a1", "2026-01-15")
        self.conn.commit()
        datos = self._marcar({"habit_id": hid, "day": "2026-01-15",
                              "actividad": {"source": "garmin", "source_id": "a1"}}, pregunta="check off my habit")
        self.assertEqual(datos["titulo"], "Check off a habit")
        self.assertEqual(datos["despues"],
                         "Proposal: Jugar pádel · done on 15 Jan 2026, from your activity: Pádel · 52 min")

    def test_card_without_evidence_in_english(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        _, hid = _seed_habito(self.conn)
        self.conn.commit()
        datos = self._marcar({"habit_id": hid, "day": "2026-01-15"}, pregunta="check off my habit")
        self.assertEqual(datos["despues"], "Proposal: Jugar pádel · done on 15 Jan 2026")

    def test_card_shows_antes_when_the_day_already_had_a_not_done_row(self):
        _, hid = _seed_habito(self.conn)
        goals.check_habit(self.conn, hid, 1, "2026-01-15", False, None, hoy=HOY)
        self.conn.commit()
        datos = self._marcar({"habit_id": hid, "day": "2026-01-15"})
        self.assertEqual(datos["antes"], "Antes: Jugar pádel · sin marcar el 15 ene 2026")
        self.assertEqual(datos["despues"], "Después: Jugar pádel · hecho el 15 ene 2026")


if __name__ == "__main__":
    unittest.main()
