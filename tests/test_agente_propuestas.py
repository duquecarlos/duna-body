"""BODY-ASISTENTE-PROPUESTAS-1: the durable proposal record, its four `propuesta`-kind tools, and
the in-chat Aplicar/Descartar/Deshacer flow (agent-capabilities-v1 section 2 + amendment 1).
Invented demo data only, two persons, a scripted fake transport -- never a real model provider.
"""
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, garmin_adapter, goals, identity, web
from body.agente import bucle as agente_bucle
from body.agente import cliente as agente_cliente
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.agente.cliente import BloqueTexto, LlamadaHerramienta, Respuesta, Uso


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "p.db"
    conn = db.connect()
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn, orig


def _seed_goal(conn, person_id: int = 1) -> int:
    gid = goals.propose(conn, person_id, "Bajar FC en reposo", "2026-10-01", metric_key="resting_hr",
                        direction="lower", target_value=50.0, baseline_value=60.0, deadline="2026-12-01")
    goals.activate(conn, gid)
    conn.commit()
    return gid


def _seed_turnos(conn, person_id: int = 1) -> None:
    cur = conn.execute(
        "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, day_count, created_at) "
        "VALUES (?, 'turnos-test.json', '2026-10-01T00:00:00', '2026-10-01T00:00:00', 'proposed', 1, "
        "'2026-10-01T00:00:00')", (person_id,),
    )
    conn.execute(
        "INSERT INTO shift_batch_item (batch_id, day, kind, start_local) VALUES (?, '2026-10-05', 'night', "
        "'2026-10-05T22:00:00')", (cur.lastrowid,),
    )
    conn.commit()


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _evento_data(texto: str, nombre: str) -> dict:
    lineas = texto.splitlines()
    idx = lineas.index(f"event: {nombre}")
    return json.loads(lineas[idx + 1][len("data:"):].strip())


class FakeProveedor:
    """Scripted transport: each entry in `guion` is a `Respuesta`, consumed one per `responder()`
    call. Captures every system prompt it was given, in order, for the status-block test."""

    def __init__(self, guion):
        self.guion = list(guion)
        self.sistemas: list[str] = []

    def responder(self, sistema, mensajes, herramientas_):
        self.sistemas.append(sistema)
        return self.guion.pop(0)


class ProponerCreaRegistroTest(unittest.TestCase):
    """Each proposal tool creates a record and writes nothing to the target."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def test_proponer_meta_writes_nothing_to_goal(self):
        cid = _cid(self.conn)
        before = self.conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "proponer_meta", {
            "metric_key": "resting_hr", "direction": "lower", "target_value": 50.0,
            "deadline": "2026-12-01", "titulo": "Bajar FC en reposo",
        }, "ninguno", cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0], before)
        row = self.conn.execute("SELECT * FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)).fetchone()
        self.assertEqual(row["accion"], "proponer_meta")
        self.assertEqual(row["estado"], "propuesta")

    def test_proponer_cambio_meta_writes_nothing_to_goal(self):
        gid = _seed_goal(self.conn)
        cid = _cid(self.conn)
        before = self.conn.execute("SELECT target_value FROM goal WHERE id = ?", (gid,)).fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "proponer_cambio_meta",
                                    {"goal_id": gid, "target_value": 48.0}, "ninguno", cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(
            self.conn.execute("SELECT target_value FROM goal WHERE id = ?", (gid,)).fetchone()[0], before
        )

    def test_proponer_confirmar_turnos_writes_nothing_to_shift(self):
        _seed_turnos(self.conn)
        cid = _cid(self.conn)
        before = self.conn.execute("SELECT COUNT(*) FROM shift").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "proponer_confirmar_turnos", {}, "ninguno", cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift").fetchone()[0], before)

    def test_proponer_idioma_writes_nothing_to_person(self):
        cid = _cid(self.conn)
        before = self.conn.execute("SELECT language FROM person WHERE id = 1").fetchone()[0]
        out = herramientas.ejecutar(self.conn, 1, "proponer_idioma", {"lang": "en"}, "ninguno", cid)
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT language FROM person WHERE id = 1").fetchone()[0], before)


class NoProponibleTest(unittest.TestCase):
    def test_privacy_and_account_tools_are_never_proposals(self):
        nombres = {h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta"}
        self.assertEqual(nombres, {"proponer_meta", "proponer_cambio_meta", "proponer_confirmar_turnos",
                                    "proponer_idioma", "planear_entreno", "registrar_entreno",
                                    "registrar_medida", "marcar_habito", "proponer_experimento",
                                    "marcar_experimento", "registrar_ingesta", "corregir_ingesta",
                                    "borrar_ingesta"})


class UnaPropuestaPorRondaTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def test_second_proposal_in_the_same_round_gets_a_tool_error(self):
        cid = _cid(self.conn)
        guion = [
            Respuesta(bloques=[
                LlamadaHerramienta("l1", "proponer_idioma", {"lang": "en"}),
                LlamadaHerramienta("l2", "proponer_idioma", {"lang": "en"}),
            ], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        resultado = agente_bucle.ejecutar(self.conn, 1, proveedor, "sistema", [], cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 1)
        usuario = [m for m in resultado.nuevos_mensajes if m["rol"] == "usuario"][0]
        self.assertIn("resultado", usuario["bloques"][0])
        self.assertIn("error", usuario["bloques"][1])


class AplicarEscribeTest(unittest.TestCase):
    """Aplicar writes through the same function the form uses, is single-use durably, checks
    ownership, expiry and drift."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def test_aplicar_proponer_meta_creates_the_goal_only_for_that_person(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_meta", {
            "metric_key": "resting_hr", "direction": "lower", "target_value": 50.0,
            "deadline": "2026-12-01", "titulo": "Bajar FC en reposo",
        }, "ninguno", cid)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        rows = self.conn.execute("SELECT * FROM goal WHERE title = ?", ("Bajar FC en reposo",)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["person_id"], 1)
        self.assertEqual(rows[0]["status"], "proposed")  # goals.propose()'s own default, proving it was reused

    def test_aplicar_twice_the_second_is_ya_resuelta_and_nothing_written_twice(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_idioma", {"lang": "en"}, "ninguno", cid)
        r1 = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        r2 = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(r1["estado"], "aplicada")
        self.assertEqual(r2["estado"], "ya_resuelta")
        self.assertEqual(self.conn.execute("SELECT language FROM person WHERE id = 1").fetchone()[0], "en")

    def test_another_persons_id_is_refused(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "proponer_idioma", {"lang": "en"}, "ninguno", cid)
        with self.assertRaises(propuestas.PropuestaAjena):
            propuestas.aplicar(self.conn, 2, out["propuesta_id"])

    def test_expired_marks_expirada_and_refuses(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_idioma", {"lang": "en"}, "ninguno", cid)
        vencido = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE propuesta_agente SET expira_en = ? WHERE id = ?", (vencido, out["propuesta_id"]))
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "expirada")
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)).fetchone()
        self.assertEqual(row["estado"], "expirada")
        self.assertEqual(self.conn.execute("SELECT language FROM person WHERE id = 1").fetchone()[0], "es")

    def test_drift_refuses_and_writes_nothing(self):
        gid = _seed_goal(self.conn)
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_cambio_meta",
                                    {"goal_id": gid, "target_value": 48.0}, "ninguno", cid)
        db.update_goal_target(self.conn, gid, 1, target_value=55.0)  # drifts after the proposal was made
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        self.assertEqual(self.conn.execute("SELECT target_value FROM goal WHERE id = ?", (gid,)).fetchone()[0], 55.0)
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)).fetchone()
        self.assertEqual(row["estado"], "propuesta")


class RegistrarEntrenoWeightUnitDriftTest(unittest.TestCase):
    """BODY-REVISION-9B-1-INV finding 3 (fixed here): a watch set's weight UNIT is compared too,
    not just its value -- a unit-only change between proposal and apply must be caught as drift
    like every other field. An old-shape snapshot (no `garmin_weight_unit` key, from a proposal
    made before this slice) still applies: a missing field is "not compared", not "changed"."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'a1', '2026-11-10', '2026-11-10T07:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [garmin_adapter.ExerciseSet(
            set_type="active", category="pecho", exercise="press banca", reps=8.0,
            weight_value=50.0, weight_unit="kg", duration_s=30.0, start_local="2026-11-10T07:00:00",
        )])

    def test_a_weight_unit_only_change_is_detected_as_drift(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
            "day": "2026-11-10", "series": [{"ejercicio": "press banca", "reps": 8, "peso_kg": 50.0}],
        }, "ninguno", cid)
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [garmin_adapter.ExerciseSet(
            set_type="active", category="pecho", exercise="press banca", reps=8.0,
            weight_value=50.0, weight_unit="lb", duration_s=30.0, start_local="2026-11-10T07:00:00",
        )])
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_an_old_shape_snapshot_without_the_field_still_applies(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "registrar_entreno", {
            "day": "2026-11-10", "series": [{"ejercicio": "press banca", "reps": 8, "peso_kg": 50.0}],
        }, "ninguno", cid)
        antes = json.loads(self.conn.execute(
            "SELECT antes_json FROM propuesta_agente WHERE id = ?", (out["propuesta_id"],)).fetchone()[0])
        del antes["sets_antes"][0]["garmin_weight_unit"]
        self.conn.execute("UPDATE propuesta_agente SET antes_json = ? WHERE id = ?",
                          (json.dumps(antes), out["propuesta_id"]))
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [garmin_adapter.ExerciseSet(
            set_type="active", category="pecho", exercise="press banca", reps=8.0,
            weight_value=50.0, weight_unit="lb", duration_s=30.0, start_local="2026-11-10T07:00:00",
        )])
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")


class DeshacerTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def test_deshacer_restores_the_value_before(self):
        gid = _seed_goal(self.conn)
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_cambio_meta",
                                    {"goal_id": gid, "target_value": 48.0}, "ninguno", cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(self.conn.execute("SELECT target_value FROM goal WHERE id = ?", (gid,)).fetchone()[0], 48.0)
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(self.conn.execute("SELECT target_value FROM goal WHERE id = ?", (gid,)).fetchone()[0], 50.0)

    def test_deshacer_refuses_on_drift(self):
        gid = _seed_goal(self.conn)
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_cambio_meta",
                                    {"goal_id": gid, "target_value": 48.0}, "ninguno", cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        db.update_goal_target(self.conn, gid, 1, target_value=47.0)  # changed again after applying
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")
        self.assertEqual(self.conn.execute("SELECT target_value FROM goal WHERE id = ?", (gid,)).fetchone()[0], 47.0)

    def test_confirmar_turnos_has_no_undo(self):
        _seed_turnos(self.conn)
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_confirmar_turnos", {}, "ninguno", cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "sin_deshacer")


class RutasPropuestaTest(unittest.TestCase):
    """POST /asistente/propuestas/{id}/aplicar|descartar|deshacer (decision 4)."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def test_aplicar_route(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_idioma", {"lang": "en"}, "ninguno", cid)
        r = self.client.post(f"/asistente/propuestas/{out['propuesta_id']}/aplicar")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["estado"], "aplicada")

    def test_descartar_route(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_idioma", {"lang": "en"}, "ninguno", cid)
        r = self.client.post(f"/asistente/propuestas/{out['propuesta_id']}/descartar")
        self.assertEqual(r.json()["estado"], "descartada")

    def test_deshacer_route(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(self.conn, 1, "proponer_idioma", {"lang": "en"}, "ninguno", cid)
        self.client.post(f"/asistente/propuestas/{out['propuesta_id']}/aplicar")
        r = self.client.post(f"/asistente/propuestas/{out['propuesta_id']}/deshacer")
        self.assertEqual(r.json()["estado"], "deshecha")

    def test_another_persons_id_is_404(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "proponer_idioma", {"lang": "en"}, "ninguno", cid)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 2
        r = self.client.post(f"/asistente/propuestas/{out['propuesta_id']}/aplicar")
        self.assertEqual(r.status_code, 404)


class EstadoYEventosTest(unittest.TestCase):
    """The SSE `propuesta` event, the system prompt's status block, and the conversation GET's
    proposals -- the full web.py wiring, with a scripted fake provider."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def test_sse_stream_carries_a_propuesta_event_with_labelled_antes_despues(self):
        # BODY-ASISTENTE-PROPUESTAS-1-M fix (c): "Español" / "English" alone read as ambiguous in
        # the browser -- each value now carries its "Antes: " / "Después: " i18n label.
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "proponer_idioma", {"lang": "en"})],
                     motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "cambia el idioma", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["accion"], "proponer_idioma")
        self.assertEqual(datos["antes"], "Antes: Español")
        self.assertEqual(datos["despues"], "Después: English")
        self.assertIn("aplicar", datos["etiquetas"])
        self.assertIn("descartar", datos["etiquetas"])

    def test_insert_shaped_proponer_meta_carries_only_a_propuesta_label(self):
        # proponer_meta has no "before" (amendment 1a, agent-capabilities-v1 section 2): the card
        # must show only "Propuesta: …", never a bare, unlabelled value.
        guion = [
            Respuesta(bloques=[LlamadaHerramienta(
                "l1", "proponer_meta",
                {"titulo": "Bajar FC en reposo", "metric_key": "resting_hr",
                 "direction": "lower", "target_value": 50.0, "deadline": "2026-12-01"})],
                     motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "nueva meta", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["accion"], "proponer_meta")
        self.assertIsNone(datos["antes"])
        self.assertTrue(datos["despues"].startswith("Propuesta: "))

    def test_planear_entreno_carries_a_propuesta_label_with_the_exercises(self):
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "planear_entreno", {
                "day": "2026-11-10", "titulo": "Espalda y bíceps",
                "ejercicios": [{"ejercicio": "remo con barra", "series": 4, "reps": "8-10"}],
            })], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "plan de espalda", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["accion"], "planear_entreno")
        self.assertIsNone(datos["antes"])
        self.assertTrue(datos["despues"].startswith("Propuesta: "))
        self.assertIn("remo con barra", datos["despues"])
        self.assertIsNone(datos["series"])

    def test_registrar_entreno_manual_carries_a_per_set_watch_vs_you_table(self):
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "registrar_entreno", {
                "day": "2026-11-10", "titulo": "Pierna en casa",
                "series": [{"ejercicio": "sentadilla", "reps": 10, "peso_kg": 40.0}],
            })], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "hice pierna", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["accion"], "registrar_entreno")
        self.assertEqual(len(datos["series"]), 1)
        self.assertIsNone(datos["series"][0]["reloj"])  # no watch activity that day
        self.assertIn("sentadilla", datos["series"][0]["tu"])
        self.assertEqual(datos["etiquetas"]["entreno_reloj"], "Reloj")
        self.assertEqual(datos["etiquetas"]["entreno_tu"], "Tú")
        # BODY-ENTRENO-TOOLS-1-M fix 2: a manual session's titulo carries into the card's own line.
        self.assertEqual(datos["despues"], "Propuesta: Pierna en casa · 10 nov 2026")

    def test_registrar_entreno_manual_without_a_titulo_keeps_the_dateonly_wording(self):
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "registrar_entreno", {
                "day": "2026-11-10",
                "series": [{"ejercicio": "sentadilla", "reps": 10, "peso_kg": 40.0}],
            })], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "hice pierna", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["despues"], "Propuesta: Entrenamiento del 10 nov 2026")

    def test_registrar_entreno_manual_without_a_titulo_in_english(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "registrar_entreno", {
                "day": "2026-11-10",
                "series": [{"ejercicio": "sentadilla", "reps": 10, "peso_kg": 40.0}],
            })], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "did legs", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["despues"], "Proposal: Workout from 10 Nov 2026")

    def test_registrar_entreno_manual_with_the_titulo_entrenamiento_is_not_mistaken_for_absent(self):
        # BODY-ENTRENO-TITULO-1: a person who actually says "Entrenamiento" gets the titled
        # wording, exactly like any other spoken title -- never the untitled date-only wording.
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "registrar_entreno", {
                "day": "2026-11-10", "titulo": "Entrenamiento",
                "series": [{"ejercicio": "sentadilla", "reps": 10, "peso_kg": 40.0}],
            })], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "hice pierna", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["despues"], "Propuesta: Entrenamiento · 10 nov 2026")

    def test_registrar_entreno_garmin_shows_a_readable_watch_name(self):
        # BODY-ENTRENO-TOOLS-1-M fix 1: the watch column reads Garmin's raw code as a readable
        # name -- underscores as spaces, only the first letter capital -- never the stored value.
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'a1', '2026-11-10', '2026-11-10T07:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [
            garmin_adapter.ExerciseSet(set_type="active", category="pecho", exercise="BARBELL_ROW", reps=8.0,
                                       weight_value=40.0, weight_unit="kg", duration_s=30.0,
                                       start_local="2026-11-10T07:00:00"),
        ])
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "registrar_entreno", {
                "day": "2026-11-10",
                "series": [{"ejercicio": "remo con barra", "reps": 10, "peso_kg": 40.0}],
            })], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "hice remo", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["series"][0]["reloj"], "Barbell row · 8 rep · 40 kg")
        self.assertIn("remo con barra", datos["series"][0]["tu"])

    def test_registrar_entreno_garmin_falls_back_to_category_when_exercise_is_missing(self):
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'a1', '2026-11-10', '2026-11-10T07:00:00', 'strength_training', NULL, 'garmin')"
        )
        self.conn.commit()
        db.save_exercise_sets(self.conn, 1, "garmin", "a1", [
            garmin_adapter.ExerciseSet(set_type="active", category="CORE", exercise=None, reps=8.0,
                                       weight_value=None, weight_unit=None, duration_s=30.0,
                                       start_local="2026-11-10T07:00:00"),
        ])
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "registrar_entreno", {
                "day": "2026-11-10", "series": [{"ejercicio": "plancha", "reps": 10}],
            })], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "hice core", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["series"][0]["reloj"], "Core · 8 rep")

    def test_registrar_medida_carries_a_propuesta_label(self):
        # No prior value for that day/key -- insert-shaped, like proponer_meta: only "Propuesta: …".
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "registrar_medida", {
                "metric_key": "flex_alcance_cm", "value": 32.0, "day": "2026-11-10",
            })], motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r = self.client.post("/asistente/preguntar",
                                 json={"pregunta": "registra mi flexibilidad", "conversacion_id": None})
        self.assertEqual(r.status_code, 200)
        datos = _evento_data(r.text, "propuesta")
        self.assertEqual(datos["accion"], "registrar_medida")
        self.assertIsNone(datos["antes"])
        self.assertTrue(datos["despues"].startswith("Propuesta: "))

    def test_status_block_appears_in_the_next_turns_system_prompt(self):
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "proponer_idioma", {"lang": "en"})],
                     motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("ok")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r1 = self.client.post("/asistente/preguntar",
                                  json={"pregunta": "cambia el idioma", "conversacion_id": None})
            cid = int(re.search(r'"conversacion_id":\s*(\d+)', r1.text).group(1))
            self.client.post("/asistente/preguntar", json={"pregunta": "¿ya?", "conversacion_id": cid})
        self.assertEqual(len(proveedor.sistemas), 3)
        self.assertNotIn("proponer_idioma:", proveedor.sistemas[0])
        self.assertIn("proponer_idioma", proveedor.sistemas[2])
        self.assertIn("propuesta", proveedor.sistemas[2])

    def test_conversacion_get_returns_proposals_with_state(self):
        guion = [
            Respuesta(bloques=[LlamadaHerramienta("l1", "proponer_idioma", {"lang": "en"})],
                     motivo_fin="tool_use", uso=Uso()),
            Respuesta(bloques=[BloqueTexto("listo")], motivo_fin="end_turn", uso=Uso()),
        ]
        proveedor = FakeProveedor(guion)
        with patch.object(agente_cliente, "proveedor_configurado", return_value=proveedor):
            r1 = self.client.post("/asistente/preguntar",
                                  json={"pregunta": "cambia el idioma", "conversacion_id": None})
        cid = int(re.search(r'"conversacion_id":\s*(\d+)', r1.text).group(1))
        r2 = self.client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r2.status_code, 200)
        datos = r2.json()
        self.assertIn("turnos", datos)
        self.assertEqual(len(datos["propuestas"]), 1)
        self.assertEqual(datos["propuestas"][0]["estado"], "propuesta")
        self.assertEqual(datos["propuestas"][0]["accion"], "proponer_idioma")


if __name__ == "__main__":
    unittest.main()
