"""BODY-EXPERIMENTOS-REGISTRO-1: `leer_experimentos`, `proponer_experimento` and `marcar_experimento`
-- the propose/apply/undo shape `marcar_habito` already uses, on top of the tables merged by
BODY-EXPERIMENTOS-ESQUEMA-1-M and the verdict merged by BODY-EXPERIMENTOS-MODELO-1. Invented demo
data only, no real health figures.
"""
import json
import tempfile
import unittest
from datetime import date, timedelta
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

FILTRO_TARDE = {"turno_en": ["afternoon"], "turno_fuera": None, "excluir_tras_noche": False}
FILTRO_SIN_NOCHE = {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": True}
VENTANA_DIA = {"tipo": "dia"}


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _insert_shift(conn, person_id: int, day: str, kind: str = "afternoon") -> None:
    conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, ?, 'manual')",
                 (person_id, day, kind))


def _seed_dato(conn, person_id: int = 1, day: str = "2026-10-01") -> None:
    db.set_metric(conn, day, "resting_hr", 58.0, "garmin", person_id)
    conn.commit()


def _proponer_params(**over) -> dict:
    base = dict(titulo="Ducha fría corta en turno de tarde", metricas=["resting_hr"], ventana=dict(VENTANA_DIA),
                filtro_dias=dict(FILTRO_TARDE), fecha_inicio="2026-11-09")
    base.update(over)
    return base


class ProponerValidacionTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _seed_dato(self.conn)

    def test_blank_titulo_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_experimento", _proponer_params(titulo="   "),
                                  "ninguno", self.cid)

    def test_unknown_metrica_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_experimento",
                                  _proponer_params(metricas=["no_existe"]), "ninguno", self.cid)

    def test_invalid_ventana_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_experimento",
                                  _proponer_params(ventana={"tipo": "semana"}), "ninguno", self.cid)

    def test_invalid_filtro_dias_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_experimento",
                                  _proponer_params(filtro_dias={"turno_en": None}), "ninguno", self.cid)

    def test_another_persons_habit_is_refused(self):
        gid = goals.propose(self.conn, 2, "Meta de B", "2026-01-01", kind="milestone", deadline="2026-12-30")
        hid = goals.add_habit(self.conn, gid, "Hábito de B", {"per": "week", "times": 1})
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_experimento",
                                  _proponer_params(habito_id=hid), "ninguno", self.cid)

    def test_future_fecha_inicio_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_experimento",
                                  _proponer_params(fecha_inicio="2099-01-01"), "ninguno", self.cid)

    def test_fecha_inicio_before_first_stored_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_experimento",
                                  _proponer_params(fecha_inicio="2026-01-01"), "ninguno", self.cid)

    def test_malformed_fecha_fin_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "proponer_experimento",
                                  _proponer_params(fecha_fin="no-es-fecha"), "ninguno", self.cid)


class ProponerProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        _seed_dato(self.conn)

    def test_propose_writes_nothing_to_experimento(self):
        before = self.conn.execute("SELECT COUNT(*) FROM experimento").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "proponer_experimento", _proponer_params(), "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM experimento").fetchone()[0], before)

    def test_apply_creates_the_experimento_for_that_person_only(self):
        out = herramientas.ejecutar(self.conn, 1, "proponer_experimento", _proponer_params(), "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT * FROM experimento WHERE person_id = 1").fetchone()
        self.assertEqual(row["titulo"], "Ducha fría corta en turno de tarde")
        self.assertEqual(row["estado"], "activo")

    def test_apply_with_habito_id_links_it(self):
        gid = goals.propose(self.conn, 1, "Meta", "2026-01-01", kind="milestone", deadline="2026-12-30")
        hid = goals.add_habit(self.conn, gid, "Jugar pádel", {"per": "week", "times": 1})
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "proponer_experimento", _proponer_params(habito_id=hid),
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        row = self.conn.execute("SELECT habito_id FROM experimento WHERE person_id = 1").fetchone()
        self.assertEqual(row["habito_id"], hid)

    def test_undo_removes_the_experimento_when_untouched(self):
        out = herramientas.ejecutar(self.conn, 1, "proponer_experimento", _proponer_params(), "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM experimento").fetchone()[0], 0)

    def test_undo_refuses_once_a_day_has_been_marked(self):
        out = herramientas.ejecutar(self.conn, 1, "proponer_experimento", _proponer_params(), "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        eid = self.conn.execute("SELECT id FROM experimento WHERE person_id = 1").fetchone()["id"]
        db.registrar_dia_experimento(self.conn, eid, 1, "2026-11-09", True)
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM experimento").fetchone()[0], 1)

    def test_person_b_cannot_apply_person_as_proposal(self):
        out = herramientas.ejecutar(self.conn, 1, "proponer_experimento", _proponer_params(), "ninguno", self.cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])


def _seed_experimento(conn, person_id: int = 1, *, habito_id=None, fecha_inicio="2026-11-09",
                      fecha_fin=None, estado="activo") -> int:
    return db.crear_experimento(conn, person_id, "Ducha fría", ["resting_hr"], dict(VENTANA_DIA),
                                dict(FILTRO_TARDE), habito_id=habito_id, fecha_inicio=fecha_inicio,
                                fecha_fin=fecha_fin, estado=estado)


class MarcarProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.eid = _seed_experimento(self.conn)
        self.conn.commit()

    def test_propose_writes_nothing_to_experimento_registro(self):
        before = self.conn.execute("SELECT COUNT(*) FROM experimento_registro").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                    {"experimento_id": self.eid, "day": "2026-11-09", "ocurrio": True},
                                    "ninguno", self.cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM experimento_registro").fetchone()[0], before)

    def test_apply_marks_it_for_that_person_only(self):
        out = herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                    {"experimento_id": self.eid, "day": "2026-11-09", "ocurrio": True},
                                    "ninguno", self.cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT ocurrio FROM experimento_registro WHERE experimento_id = ? AND person_id = 1 AND day = ?",
            (self.eid, "2026-11-09")).fetchone()
        self.assertEqual(row["ocurrio"], 1)

    def test_undo_removes_the_row_when_there_was_none_before(self):
        out = herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                    {"experimento_id": self.eid, "day": "2026-11-09", "ocurrio": True},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT * FROM experimento_registro WHERE experimento_id = ? AND day = ?",
            (self.eid, "2026-11-09")).fetchone()
        self.assertIsNone(row)

    def test_undo_restores_the_prior_row(self):
        db.registrar_dia_experimento(self.conn, self.eid, 1, "2026-11-09", False, "nota vieja")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                    {"experimento_id": self.eid, "day": "2026-11-09", "ocurrio": True},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        row = self.conn.execute(
            "SELECT ocurrio, nota FROM experimento_registro WHERE experimento_id = ? AND day = ?",
            (self.eid, "2026-11-09")).fetchone()
        self.assertEqual(row["ocurrio"], 0)
        self.assertEqual(row["nota"], "nota vieja")

    def test_drift_refuses_when_the_row_changed_after_propose(self):
        out = herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                    {"experimento_id": self.eid, "day": "2026-11-09", "ocurrio": True},
                                    "ninguno", self.cid)
        db.registrar_dia_experimento(self.conn, self.eid, 1, "2026-11-09", False)
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_undo_refuses_when_the_row_changed_again_after_applying(self):
        out = herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                    {"experimento_id": self.eid, "day": "2026-11-09", "ocurrio": True},
                                    "ninguno", self.cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        db.registrar_dia_experimento(self.conn, self.eid, 1, "2026-11-09", False, "nota nueva")
        self.conn.commit()
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_person_b_cannot_apply_person_as_proposal(self):
        out = herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                    {"experimento_id": self.eid, "day": "2026-11-09", "ocurrio": True},
                                    "ninguno", self.cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])


class MarcarRefusalsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.eid = _seed_experimento(self.conn, fecha_inicio="2026-11-01", fecha_fin="2026-11-05")
        self.conn.commit()

    def test_unknown_experimento_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                  {"experimento_id": 999, "day": "2026-11-03", "ocurrio": True},
                                  "ninguno", self.cid)

    def test_another_persons_experimento_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 2, "marcar_experimento",
                                  {"experimento_id": self.eid, "day": "2026-11-03", "ocurrio": True},
                                  "ninguno", _cid(self.conn, 2))

    def test_closed_experimento_is_refused(self):
        self.conn.execute("UPDATE experimento SET estado = 'cerrado' WHERE id = ?", (self.eid,))
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                  {"experimento_id": self.eid, "day": "2026-11-03", "ocurrio": True},
                                  "ninguno", self.cid)

    def test_day_before_fecha_inicio_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                  {"experimento_id": self.eid, "day": "2026-10-31", "ocurrio": True},
                                  "ninguno", self.cid)

    def test_day_after_fecha_fin_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                  {"experimento_id": self.eid, "day": "2026-11-06", "ocurrio": True},
                                  "ninguno", self.cid)

    def test_future_day_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                  {"experimento_id": self.eid, "day": "2099-01-01", "ocurrio": True},
                                  "ninguno", self.cid)

    def test_habit_linked_experiment_is_refused(self):
        gid = goals.propose(self.conn, 1, "Meta", "2026-01-01", kind="milestone", deadline="2026-12-30")
        hid = goals.add_habit(self.conn, gid, "Jugar pádel", {"per": "week", "times": 1})
        self.conn.commit()
        eid_habito = _seed_experimento(self.conn, habito_id=hid, fecha_inicio="2026-11-01", fecha_fin="2026-11-05")
        self.conn.commit()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "marcar_experimento",
                                  {"experimento_id": eid_habito, "day": "2026-11-03", "ocurrio": True},
                                  "ninguno", self.cid)


class LeerExperimentosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def test_returns_active_before_closed(self):
        _seed_experimento(self.conn, fecha_inicio="2026-10-01", estado="cerrado")
        activo = _seed_experimento(self.conn, fecha_inicio="2026-11-09", estado="activo")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_experimentos", {}, "resumen", self.cid)
        estados = [e["estado"] for e in out["experimentos"]]
        self.assertEqual(estados, ["activo", "cerrado"])
        self.assertEqual(out["experimentos"][0]["id"], activo)

    def test_an_unmarked_day_counts_for_neither_side_and_reads_pocos_dias(self):
        _insert_shift(self.conn, 1, "2026-11-09", "afternoon")
        db.set_metric(self.conn, "2026-11-09", "resting_hr", 55.0, "garmin", 1)
        eid = _seed_experimento(self.conn, fecha_inicio="2026-11-09")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_experimentos", {"experimento_id": eid}, "resumen", self.cid)
        exp = out["experimentos"][0]
        self.assertEqual(exp["dias_marcados"], [])
        self.assertEqual(exp["veredicto"]["resting_hr"]["resultado"], "pocos_dias")

    def test_unknown_experimento_id_returns_an_empty_list(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_experimentos", {"experimento_id": 999}, "resumen", self.cid)
        self.assertEqual(out["experimentos"], [])

    def test_person_isolation(self):
        _seed_experimento(self.conn, person_id=1)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 2, "leer_experimentos", {}, "resumen", _cid(self.conn, 2))
        self.assertEqual(out["experimentos"], [])

    def test_habito_linked_experiment_reads_marked_days_from_habit_check(self):
        gid = goals.propose(self.conn, 1, "Meta", "2026-01-01", kind="milestone", deadline="2026-12-30")
        hid = goals.add_habit(self.conn, gid, "Jugar pádel", {"per": "week", "times": 1})
        self.conn.commit()
        goals.check_habit(self.conn, hid, 1, "2026-11-09", True, None, hoy=HOY)
        self.conn.commit()
        eid = _seed_experimento(self.conn, habito_id=hid, fecha_inicio="2026-11-09")
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_experimentos", {"experimento_id": eid}, "resumen", self.cid)
        self.assertEqual(out["experimentos"][0]["dias_marcados"], [{"dia": "2026-11-09", "ocurrio": True}])


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
        _seed_dato(self.conn)

    def _llamar(self, nombre: str, params: dict, pregunta: str = "experimento") -> dict:
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", nombre, params)], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar", json={"pregunta": pregunta, "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        return _evento_data(r.text, "propuesta")

    def test_proponer_experimento_card_in_spanish(self):
        datos = self._llamar("proponer_experimento", _proponer_params())
        self.assertEqual(datos["accion"], "proponer_experimento")
        self.assertEqual(datos["titulo"], "Nuevo experimento")
        self.assertIsNone(datos["antes"])
        self.assertEqual(
            datos["despues"],
            "Propuesta: Ducha fría corta en turno de tarde · mide Pulso en reposo · "
            "días de turno de tarde · desde el 9 nov 2026",
        )

    def test_proponer_experimento_card_in_english(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        datos = self._llamar("proponer_experimento", _proponer_params(), pregunta="experiment")
        self.assertEqual(datos["titulo"], "New experiment")
        self.assertEqual(
            datos["despues"],
            "Proposal: Ducha fría corta en turno de tarde · measures Resting heart rate · "
            "afternoon shift days · starting 9 Nov 2026",
        )

    def test_marcar_experimento_card_in_spanish(self):
        eid = _seed_experimento(self.conn, fecha_inicio="2026-11-01")
        self.conn.commit()
        datos = self._llamar("marcar_experimento", {"experimento_id": eid, "day": "2026-11-09", "ocurrio": True})
        self.assertEqual(datos["titulo"], "Marcar experimento")
        self.assertEqual(datos["despues"], "Propuesta: Ducha fría · hecho el 9 nov 2026")

    def test_marcar_experimento_card_shows_antes_when_already_marked(self):
        eid = _seed_experimento(self.conn, fecha_inicio="2026-11-01")
        db.registrar_dia_experimento(self.conn, eid, 1, "2026-11-09", False)
        self.conn.commit()
        datos = self._llamar("marcar_experimento", {"experimento_id": eid, "day": "2026-11-09", "ocurrio": True})
        self.assertEqual(datos["antes"], "Antes: Ducha fría · no hecho el 9 nov 2026")
        self.assertEqual(datos["despues"], "Después: Ducha fría · hecho el 9 nov 2026")


if __name__ == "__main__":
    unittest.main()
