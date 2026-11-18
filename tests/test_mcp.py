"""BODY-MCP-LOCAL-1: the local MCP server over stdio, driven in-process with a fake stdin/stdout.
Invented demo data only, no real health figures, no network, no subprocess.
"""
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, identity, mcp, web
from body.agente import ajustes as agente_ajustes
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas
from body.agente import propuestas as agente_propuestas


def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "mcp.db"
    conn = db.connect()
    db.init(conn)
    conn.commit()
    return conn, orig


def _linea(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _respuestas(salida: io.StringIO) -> list[dict]:
    return [json.loads(l) for l in salida.getvalue().splitlines() if l.strip()]


class _McpTestCase(unittest.TestCase):
    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.person_id = db.local_person_id(self.conn)

    def _ejecutar(self, mensajes: list[dict]):
        entrada = io.StringIO("".join(_linea(m) for m in mensajes))
        salida = io.StringIO()
        sesion = mcp.ejecutar(entrada, salida, self.conn, self.person_id)
        return sesion, _respuestas(salida)


class InitializeTest(_McpTestCase):
    def test_initialize_answers_protocol_version_and_server_info(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
        self.assertEqual(len(respuestas), 1)
        r = respuestas[0]["result"]
        self.assertEqual(r["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertIn("name", r["serverInfo"])
        self.assertIn("tools", r["capabilities"])

    def test_notifications_initialized_gets_no_reply(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(respuestas, [])


class ToolsListTest(_McpTestCase):
    def test_list_matches_the_in_app_level_and_has_no_apply_undo_delete(self):
        herramientas.fijar_turnos_actual(db.trabaja_turnos_si(self.conn, self.person_id))
        nivel = agente_ajustes.nivel_datos(self.conn, self.person_id)
        esperado = {h["nombre"] for h in herramientas.disponibles(nivel)}
        self.assertGreater(len(esperado), 0)

        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        tools = respuestas[0]["result"]["tools"]
        nombres = {t["name"] for t in tools}
        self.assertEqual(nombres, esperado)
        for prohibido in ("aplicar", "descartar", "deshacer", "undo", "delete", "borrar", "apply"):
            self.assertNotIn(prohibido, nombres)
        for t in tools:
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)
            self.assertTrue(t["description"])

    def test_reserved_plan_field_never_reaches_the_mcp_listing(self):
        # BODY-ASISTENTE-CONFORMIDAD-1 ruling 2 (agent-capabilities-v1 section 5): the field
        # exists on the registry entry; it is never sent to the caller.
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        tools = respuestas[0]["result"]["tools"]
        self.assertGreater(len(tools), 0)
        for t in tools:
            self.assertNotIn("plan_minimo", t)
            self.assertEqual(set(t), {"name", "description", "inputSchema"})


class ToolsCallTest(_McpTestCase):
    def test_lectura_call_answers_with_content_and_no_error(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("nivel_datos", salida)

    def test_leer_estado_reloj_is_offered_and_callable(self):
        """BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 3: body.mcp lists leer_estado_reloj like any other
        catalogue tool -- the generic tools/list and tools/call paths need no new code for it
        either. No Garmin connection here (local person): the answer is honest, never a figure."""
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("leer_estado_reloj", nombres)

        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_estado_reloj", "arguments": {}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        salida = json.loads(r["content"][-1]["text"])
        self.assertEqual(salida["conectado"], False)
        self.assertIsNone(salida["ultimo_error"])

    def test_propuesta_call_creates_the_conversation_second_proposal_reuses_it(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_idioma", "arguments": {"lang": "en"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "marcar_periodo",
                        "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}},
        ])
        self.assertNotIn("isError", respuestas[0]["result"])
        self.assertNotIn("isError", respuestas[1]["result"])
        salida1 = json.loads(respuestas[0]["result"]["content"][-1]["text"])
        salida2 = json.loads(respuestas[1]["result"]["content"][-1]["text"])

        c1 = self.conn.execute("SELECT conversacion_id FROM propuesta_agente WHERE id = ?",
                               (salida1["propuesta_id"],)).fetchone()["conversacion_id"]
        c2 = self.conn.execute("SELECT conversacion_id FROM propuesta_agente WHERE id = ?",
                               (salida2["propuesta_id"],)).fetchone()["conversacion_id"]
        self.assertEqual(c1, c2)
        self.assertEqual(agente_conversacion.origen_de(self.conn, c1), "mcp")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversacion_agente WHERE person_id = ?",
                              (self.person_id,)).fetchone()[0],
            1,
        )

    def test_proposal_can_then_be_applied_through_the_existing_route(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_idioma", "arguments": {"lang": "en"}}},
        ])
        salida = json.loads(respuestas[0]["result"]["content"][-1]["text"])
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        client = TestClient(web.app, follow_redirects=False)
        r = client.post(f"/asistente/propuestas/{salida['propuesta_id']}/aplicar")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["estado"], "aplicada")

    def test_card_from_mcp_carries_the_origin_label(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_idioma", "arguments": {"lang": "en"}}},
        ])
        salida = json.loads(respuestas[0]["result"]["content"][-1]["text"])
        cid = self.conn.execute("SELECT conversacion_id FROM propuesta_agente WHERE id = ?",
                                (salida["propuesta_id"],)).fetchone()["conversacion_id"]
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        client = TestClient(web.app, follow_redirects=False)
        r = client.get(f"/asistente/conversacion/{cid}")
        self.assertEqual(r.status_code, 200)
        tarjetas = r.json()["propuestas"]
        self.assertEqual(len(tarjetas), 1)
        self.assertEqual(tarjetas[0]["origen_etiqueta"], "Desde tu asistente personal")

    def test_unknown_tool_is_a_tool_error(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "esto_no_existe", "arguments": {}}},
        ])
        r = respuestas[0]["result"]
        self.assertTrue(r["isError"])
        self.assertIn("text", r["content"][0])

    def test_caps_reached_is_a_tool_error(self):
        tope = mcp.LIMITES["lecturas"]
        mensajes = [
            {"jsonrpc": "2.0", "id": i, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}}
            for i in range(tope + 1)
        ]
        _, respuestas = self._ejecutar(mensajes)
        for i in range(tope):
            self.assertNotIn("isError", respuestas[i]["result"], f"call {i} should not have errored yet")
        self.assertTrue(respuestas[tope]["result"]["isError"])
        self.assertIn("min", respuestas[tope]["result"]["content"][-1]["text"])

    def test_result_of_a_proposal_says_where_the_person_will_see_it(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "marcar_periodo",
                        "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}},
        ])
        contenido = respuestas[0]["result"]["content"]
        # BODY-MCP-COPIA-PC-1: contenido[0] is now the copia-pc freshness line (every LOCAL
        # result's own, tests/test_mcp_copia_pc.py), so the Hoy/Aplicar sentence moved to [1].
        self.assertEqual(len(contenido), 3)
        self.assertIn("Hoy", contenido[1]["text"])
        self.assertIn("Aplicar", contenido[1]["text"])
        salida = json.loads(contenido[-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertIn("estado", salida)

    def test_result_of_a_lectura_carries_no_extra_sentence(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
        ])
        # BODY-MCP-COPIA-PC-1: "no extra sentence" now means none BEYOND the copia-pc freshness
        # line every LOCAL result carries (tests/test_mcp_copia_pc.py covers that line itself).
        self.assertEqual(len(respuestas[0]["result"]["content"]), 2)

    def test_registrar_comida_is_offered_and_callable(self):
        """BODY-COMIDA-1: body.mcp lists registrar_comida (it is a `tipo: propuesta` tool like
        marcar_periodo, so the generic tools/list and tools/call paths need no new code), and a
        call with several amounts proposes one meal, never writing a row on the call itself."""
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("registrar_comida", nombres)

        before = self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0]
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "registrar_comida",
                        "arguments": {"day": "2026-11-05", "hora": "13:00", "descripcion": "arroz con pollo",
                                      "cantidades": [{"metric_key": "protein_g", "amount": 35.0},
                                                     {"metric_key": "kcal", "amount": 600.0}]}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comida").fetchone()[0], before)
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertEqual(salida["estado"], "propuesta")

    def test_corregir_comida_is_offered_and_callable(self):
        """BODY-COMIDA-CORREGIR-1: same generic `tipo: propuesta` path as registrar_comida above --
        body.mcp needs no new code to list or call it, and ONE call proposes the whole correction
        (the desk's 7-apply fix becomes 1)."""
        comida_id = db.crear_comida(self.conn, self.person_id,
                                    "2026-11-05", [{"metric_key": "protein_g", "amount": 35.0}],
                                    "arroz con pollo", "2026-11-05T13:00:00")
        self.conn.commit()
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("corregir_comida", nombres)

        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "corregir_comida",
                        "arguments": {"comida_id": comida_id, "hora": "08:00",
                                      "cantidades": [{"metric_key": "protein_g", "amount": 40.0},
                                                     {"metric_key": "fiber_g", "amount": 6.0}]}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        fila = self.conn.execute("SELECT ts_local FROM comida WHERE id = ?", (comida_id,)).fetchone()
        self.assertEqual(fila["ts_local"], "2026-11-05T13:00:00")  # still unapplied: a propose writes nothing
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertEqual(salida["estado"], "propuesta")

    def test_proponer_objetivo_nutriente_is_offered_and_callable(self):
        """BODY-NUTRIENTE-OBJETIVO-1: same generic `tipo: propuesta` path -- body.mcp needs no new
        code to list or call it."""
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("proponer_objetivo_nutriente", nombres)

        before = self.conn.execute("SELECT COUNT(*) FROM nutrient_target").fetchone()[0]
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_objetivo_nutriente",
                        "arguments": {"metric_key": "protein_g", "kind": "per_kg", "amount": 1.6,
                                      "sentido": "al_menos"}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nutrient_target").fetchone()[0], before)
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertEqual(salida["estado"], "propuesta")

    def test_registrar_checkin_is_offered_and_callable(self):
        """BODY-CHECKIN-1: same generic `tipo: propuesta` path as registrar_comida above -- body.mcp
        needs no new code to list or call it."""
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("registrar_checkin", nombres)

        before = self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0]
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "registrar_checkin",
                        "arguments": {"day": "2026-11-05", "momento": "despertar", "mood": 4, "energy": 3,
                                      "stress_felt": 2}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0], before)
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertEqual(salida["estado"], "propuesta")

    def test_anotar_sesion_is_offered_and_callable(self):
        """BODY-HOGAR-ESCRIBE-1 ruling 4: same generic `tipo: propuesta` path as registrar_comida
        above -- body.mcp needs no new code to list or call it. A call with a plan_id proposes the
        note, never writing it on the call itself."""
        self.conn.execute(
            "INSERT INTO plan (person_id, day, title, status) VALUES (?, '2026-11-05', "
            "'Sesión de piernas', 'proposed')", (self.person_id,))
        self.conn.commit()
        plan_id = self.conn.execute("SELECT id FROM plan WHERE person_id = ?", (self.person_id,)).fetchone()["id"]

        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("anotar_sesion", nombres)

        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "anotar_sesion",
                        "arguments": {"plan_id": plan_id, "nota": "Se sintió fuerte", "hecha": True}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        fila = self.conn.execute("SELECT status, nota_sesion FROM plan WHERE id = ?", (plan_id,)).fetchone()
        self.assertEqual((fila["status"], fila["nota_sesion"]), ("proposed", None))  # still unapplied
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertEqual(salida["estado"], "propuesta")

    def test_corregir_entreno_is_offered_and_callable(self):
        """BODY-CORREGIR-MEDIDA-ENTRENO-1-W ruling B: same generic `tipo: propuesta` path as
        anotar_sesion above -- body.mcp needs no new code to list or call it."""
        source_id = db.create_manual_session(self.conn, self.person_id, "2026-11-05", None, "Pierna")
        db.correct_exercise_set(self.conn, self.person_id, "manual", source_id, 1, exercise="sentadilla",
                                 reps=10, weight_kg=40.0)
        self.conn.commit()

        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("corregir_entreno", nombres)

        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "corregir_entreno",
                        "arguments": {"dia": "2026-11-05", "orden": 1, "reps": 12}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        fila = self.conn.execute(
            "SELECT person_reps FROM workout_set WHERE person_id = ? AND activity_source = 'manual' "
            "AND activity_source_id = ? AND set_order = 1", (self.person_id, source_id)).fetchone()
        self.assertEqual(fila["person_reps"], 10)  # still unapplied
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertEqual(salida["estado"], "propuesta")

    def test_leer_serie_is_offered_and_callable(self):
        """BODY-AGENTE-LEER-SERIE-1 ruling 4: body.mcp lists leer_serie like any other catalogue
        tool -- the generic tools/list and tools/call paths need no new code for it either."""
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("leer_serie", nombres)

        db.set_metric(self.conn, "2026-10-01", "resting_hr", 58, "garmin", self.person_id)
        self.conn.commit()
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_serie",
                        "arguments": {"metricas": ["resting_hr"], "desde": "2026-10-01",
                                      "hasta": "2026-10-01", "paso": "dia"}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("resting_hr", salida["metricas"])

    def test_one_leer_serie_call_counts_as_one_lectura_not_one_per_day(self):
        """BODY-AGENTE-LEER-SERIE-1: the bug report this tool answers -- a desk helper calling
        leer_dia once per day hit this same 5-reads-per-15-minutes cap on a long history. ONE
        leer_serie call over many days and metrics costs exactly ONE lectura, the same as any
        other lectura tool (ruling 4: no special-casing) -- so four more calls still fit inside
        the cap, and it is hit only on the sixth call overall, never sooner because of the range
        leer_serie itself covered."""
        for i in range(28):
            db.set_metric(self.conn, f"2026-10-{i + 1:02d}", "resting_hr", 58, "garmin", self.person_id)
        self.conn.commit()
        tope = mcp.LIMITES["lecturas"]
        mensajes = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_serie",
                        "arguments": {"metricas": ["resting_hr"], "desde": "2026-10-01",
                                      "hasta": "2026-10-28", "paso": "dia"}}},
        ] + [
            {"jsonrpc": "2.0", "id": i + 2, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}}
            for i in range(tope)
        ]
        _, respuestas = self._ejecutar(mensajes)
        for i in range(tope):
            self.assertNotIn("isError", respuestas[i]["result"], f"call {i} should not have errored yet")
        self.assertTrue(respuestas[tope]["result"]["isError"])


class PlanToolsTest(_McpTestCase):
    """BODY-PLAN-PROPUESTAS-1: the plan tools are `tipo: lectura`/`propuesta` like any other
    catalogue entry -- body.mcp needs no new code to list or call them."""

    def test_leer_planes_and_leer_plan_are_offered_and_callable(self):
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("leer_planes", nombres)
        self.assertIn("leer_plan", nombres)

        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "leer_planes", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "leer_plan", "arguments": {"goal_id": 999999}}},
        ])
        self.assertNotIn("isError", respuestas[0]["result"])
        self.assertEqual(json.loads(respuestas[0]["result"]["content"][-1]["text"]), {"planes": []})
        self.assertNotIn("isError", respuestas[1]["result"])
        self.assertEqual(json.loads(respuestas[1]["result"]["content"][-1]["text"]), {"encontrada": False})

    def test_proponer_plan_is_offered_and_callable(self):
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("proponer_plan", nombres)
        self.assertIn("ampliar_plan", nombres)
        self.assertIn("corregir_meta_plan", nombres)

        before = self.conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0]
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_plan",
                        "arguments": {"titulo": "Prueba de carrera 10K", "start_day": "2026-11-05",
                                      "deadline": "2027-02-01"}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0], before)
        salida = json.loads(r["content"][-1]["text"])
        self.assertIn("propuesta_id", salida)
        self.assertEqual(salida["estado"], "propuesta")

    def test_proponer_plan_description_tells_the_hhmm_convention(self):
        """BODY-PLAN-HORA-ENTRADA-1 ruling 2: the model is told a clock measure's valor is "HH:MM",
        and the MCP door is what proves that sentence actually reaches it."""
        _, listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        descripcion = next(t["description"] for t in listado[0]["result"]["tools"]
                           if t["name"] == "proponer_plan")
        self.assertIn("HH:MM", descripcion)

    def test_aplicar_propuesta_applies_a_proponer_plan_only_when_aplicar_mcp_is_on(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "proponer_plan",
                        "arguments": {"titulo": "Prueba de carrera 10K", "start_day": "2026-11-05",
                                      "deadline": "2027-02-01"}}},
        ])
        pid = json.loads(respuestas[0]["result"]["content"][-1]["text"])["propuesta_id"]

        _, respuestas_off = self._ejecutar([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        self.assertTrue(respuestas_off[0]["result"]["isError"])
        self.assertEqual(self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
                          ["estado"], "propuesta")

        db.set_aplicar_mcp(self.conn, self.person_id, True)
        _, respuestas_on = self._ejecutar([
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        r = respuestas_on[0]["result"]
        self.assertNotIn("isError", r)
        salida = json.loads(r["content"][-1]["text"])
        self.assertEqual(salida["estado"], "aplicada")
        row = self.conn.execute(
            "SELECT id FROM goal WHERE person_id = ? AND title = ?", (self.person_id, "Prueba de carrera 10K")
        ).fetchone()
        self.assertIsNotNone(row)


class RegistrarEntrenoRelojFalloTest(_McpTestCase):
    """BODY-SERIES-UNIDAD-1 ruling 3: when the watch's own sets can't be read, this is the one
    place the caller (which never sees the Hoy card directly) is told the proposal still saved on
    the person's own spoken sets."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (?, 'a1', '2026-11-09', '2026-11-09T07:00:00', 'strength_training', NULL, 'garmin')",
            (self.person_id,),
        )
        self.conn.commit()

    def test_a_raising_adapter_still_gets_an_answer_with_the_reloj_note(self):
        with mock.patch("body.pull.traer_series", side_effect=RuntimeError("Garmin unreachable")):
            _, respuestas = self._ejecutar([
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "registrar_entreno",
                            "arguments": {"day": "2026-11-09",
                                          "series": [{"ejercicio": "press banca", "reps": 10,
                                                      "peso_kg": 55.0}]}}},
            ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        contenido = r["content"]
        # BODY-MCP-COPIA-PC-1: contenido[0] is now the copia-pc freshness line, so the
        # reloj-fallo/Hoy sentences each shifted one position right.
        self.assertEqual(len(contenido), 4)
        self.assertEqual(contenido[1]["text"], "No pude leer las series del reloj; guardé las que me contaste.")
        self.assertIn("Hoy", contenido[2]["text"])
        salida = json.loads(contenido[-1]["text"])
        self.assertEqual(salida["estado"], "propuesta")

    def test_a_successful_adapter_carries_no_reloj_note(self):
        with mock.patch("body.pull.traer_series", return_value=0):
            _, respuestas = self._ejecutar([
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "registrar_entreno",
                            "arguments": {"day": "2026-11-09",
                                          "series": [{"ejercicio": "press banca", "reps": 10,
                                                      "peso_kg": 55.0}]}}},
            ])
        contenido = respuestas[0]["result"]["content"]
        self.assertEqual(len(contenido), 3)


class ToolCallInternalErrorTest(_McpTestCase):
    """BODY-SERIES-UNIDAD-1 ruling 4: the MCP server never dies on a tool call."""

    def test_an_unexpected_exception_answers_iserror_and_the_next_request_still_gets_its_answer(self):
        real_ejecutar = herramientas.ejecutar
        llamadas = {"n": 0}

        def falla_la_primera(*args, **kwargs):
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                raise db.sqlite3.IntegrityError("CHECK constraint failed")
            return real_ejecutar(*args, **kwargs)

        buf = io.StringIO()
        with mock.patch("body.mcp.herramientas.ejecutar", side_effect=falla_la_primera), \
             mock.patch("sys.stderr", buf):
            _, respuestas = self._ejecutar([
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "marcar_periodo",
                            "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "leer_ajustes", "arguments": {}}},
            ])
        self.assertEqual(len(respuestas), 2)
        primera = respuestas[0]["result"]
        self.assertTrue(primera["isError"])
        self.assertEqual(
            # BODY-MCP-COPIA-PC-1: content[0] is now the copia-pc freshness line; the internal
            # error sentence moved to content[-1].
            primera["content"][-1]["text"],
            "Body tuvo un error interno con esta herramienta; no se guardó nada. Intenta de nuevo o avísale al equipo.",
        )
        segunda = respuestas[1]["result"]
        self.assertNotIn("isError", segunda)
        self.assertIn("nivel_datos", json.loads(segunda["content"][-1]["text"]))

        # the exception's TYPE and the tool's own name reached stderr -- never its message
        self.assertIn("IntegrityError", buf.getvalue())
        self.assertIn("marcar_periodo", buf.getvalue())
        self.assertNotIn("CHECK constraint failed", buf.getvalue())

        # ruling 5: both attempts -- the one that errored and the one that didn't -- were audited
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM uso_herramienta_externa WHERE person_id = ?",
                              (self.person_id,)).fetchone()[0],
            2,
        )


class VentanaRotanteTest(_McpTestCase):
    """BODY-MCP-LOCAL-1-B: caps bound a RATE, not a per-process total -- a long-lived server
    process must be able to read again once the window has rolled past its first calls, with no
    sleeping in the test (an injectable clock)."""

    def test_calls_past_the_limit_are_refused_then_allowed_again_after_the_window(self):
        ahora = {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}

        def reloj():
            return ahora["t"]

        tope = mcp.LIMITES["lecturas"]
        entrada = io.StringIO("".join(
            _linea({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "leer_ajustes", "arguments": {}}})
            for i in range(tope + 1)
        ))
        salida = io.StringIO()
        sesion = mcp.ejecutar(entrada, salida, self.conn, self.person_id, reloj=reloj)
        respuestas = _respuestas(salida)
        for i in range(tope):
            self.assertNotIn("isError", respuestas[i]["result"])
        self.assertTrue(respuestas[tope]["result"]["isError"])

        # Past the window, on the SAME session (the same process's own state, never a fresh one):
        # allowed again.
        ahora["t"] += timedelta(minutes=mcp.LIMITES["ventana_min"] + 1)
        respuesta = mcp._procesar_mensaje(sesion, {"jsonrpc": "2.0", "id": 999, "method": "tools/call",
                                                     "params": {"name": "leer_ajustes", "arguments": {}}})
        self.assertNotIn("isError", respuesta["result"])

    def test_error_names_minutes_to_wait_in_the_persons_language(self):
        ahora = {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}

        def reloj():
            return ahora["t"]

        tope = mcp.LIMITES["propuestas"]
        entrada = io.StringIO("".join(
            _linea({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "marcar_periodo",
                               "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}})
            for i in range(tope + 1)
        ))
        salida = io.StringIO()
        mcp.ejecutar(entrada, salida, self.conn, self.person_id, reloj=reloj)
        respuestas = _respuestas(salida)
        texto = respuestas[tope]["result"]["content"][-1]["text"]
        self.assertIn(str(mcp.LIMITES["ventana_min"]), texto)

        db.set_language(self.conn, "en", self.person_id)
        self.conn.commit()
        entrada_en = io.StringIO("".join(
            _linea({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "marcar_periodo",
                               "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}})
            for i in range(tope + 1)
        ))
        salida_en = io.StringIO()
        mcp.ejecutar(entrada_en, salida_en, self.conn, self.person_id, reloj=reloj)
        respuestas_en = _respuestas(salida_en)
        texto_en = respuestas_en[tope]["result"]["content"][-1]["text"]
        self.assertIn("min", texto_en)
        self.assertIn(str(mcp.LIMITES["ventana_min"]), texto_en)


class ProtocolErrorsTest(_McpTestCase):
    def test_unknown_method_is_minus_32601(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "no/such/method"}])
        self.assertEqual(respuestas[0]["error"]["code"], -32601)

    def test_malformed_json_is_minus_32700_without_crashing(self):
        entrada = io.StringIO("{esto no es json\n" + _linea({"jsonrpc": "2.0", "id": 2, "method": "initialize"}))
        salida = io.StringIO()
        mcp.ejecutar(entrada, salida, self.conn, self.person_id)
        respuestas = _respuestas(salida)
        self.assertEqual(len(respuestas), 2)
        self.assertEqual(respuestas[0]["error"]["code"], -32700)
        self.assertEqual(respuestas[1]["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)


class AuditTest(_McpTestCase):
    def test_every_call_writes_one_row_with_no_params_or_result(self):
        self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "esto_no_existe", "arguments": {}}},
        ])
        rows = self.conn.execute(
            "SELECT herramienta, caller FROM uso_herramienta_externa WHERE person_id = ? ORDER BY id",
            (self.person_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["herramienta"], "leer_ajustes")
        self.assertEqual(rows[0]["caller"], mcp.CALLER)
        self.assertEqual(rows[1]["herramienta"], "esto_no_existe")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(uso_herramienta_externa)")}
        self.assertEqual(cols, {"id", "person_id", "herramienta", "llamada_en", "caller"})

    def test_stdout_carries_only_protocol_frames(self):
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_ajustes", "arguments": {}}},
        ])
        for r in respuestas:
            self.assertEqual(r.get("jsonrpc"), "2.0")
            self.assertIn("id", r)


class MainStderrTest(_McpTestCase):
    def test_main_logs_counts_only_to_stderr_never_a_figure(self):
        db.set_metric(self.conn, "2026-11-01", "resting_hr", 171717.0, "garmin", self.person_id)
        self.conn.commit()
        entrada = io.StringIO(_linea({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "leer_hoy", "arguments": {}}}))
        salida = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdin", entrada), mock.patch.object(sys, "stdout", salida), \
             mock.patch.object(sys, "stderr", stderr), mock.patch.object(db, "connect", return_value=self.conn):
            mcp.main()
        for linea in salida.getvalue().splitlines():
            if linea.strip():
                json.loads(linea)
        self.assertNotIn("171717", stderr.getvalue())
        self.assertIn("lecturas", stderr.getvalue())


class Utf8WireTest(_McpTestCase):
    """BODY-MCP-LOCAL-1-B: MCP stdio is UTF-8, never the console's own code page. Driven with real
    byte-backed streams whose encoding main() must override -- on the old code (no reconfigure),
    writing "é" through a cp1252-encoded stdout is the single byte 0xE9, which utf-8.decode()
    below raises on; reading "ñ" through a cp1252-decoded stdin turns it into mojibake before it
    ever reaches json.loads. Both fail this test on the code before this slice."""

    def test_round_trips_accented_text_regardless_of_console_encoding(self):
        peticion_lista = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                     ensure_ascii=False).encode("utf-8") + b"\n"
        peticion_marcar = json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "marcar_periodo",
                       "arguments": {"kind": "viaje", "start_day": "2020-01-01", "note": "mañana"}},
        }, ensure_ascii=False).encode("utf-8") + b"\n"
        entrada = io.TextIOWrapper(io.BytesIO(peticion_lista + peticion_marcar), encoding="cp1252", newline="")
        salida_bytes = io.BytesIO()
        salida = io.TextIOWrapper(salida_bytes, encoding="cp1252", newline="")
        with mock.patch.object(sys, "stdin", entrada), mock.patch.object(sys, "stdout", salida), \
             mock.patch.object(sys, "stderr", io.StringIO()), \
             mock.patch.object(db, "connect", return_value=self.conn):
            mcp.main()

        crudo = salida_bytes.getvalue()
        texto = crudo.decode("utf-8")  # raises UnicodeDecodeError on the pre-slice code
        lineas = [json.loads(l) for l in texto.splitlines() if l.strip()]
        descripciones = " ".join(h["description"] for h in lineas[0]["result"]["tools"])
        self.assertIn("métrica", descripciones)

        salida_marcar = json.loads(lineas[1]["result"]["content"][-1]["text"])
        # main() closes the connection it was given (mocked to self.conn) on its own way out, so
        # the row is read back through a fresh one, same db file.
        verificacion = db.connect()
        self.addCleanup(verificacion.close)
        fila = verificacion.execute("SELECT despues_json FROM propuesta_agente WHERE id = ?",
                                     (salida_marcar["propuesta_id"],)).fetchone()
        self.assertEqual(json.loads(fila["despues_json"])["note"], "mañana")


class AplicarExpiradaTest(_McpTestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 4: an honest answer, never just "no longer pending"."""

    def test_message_names_when_it_expired_and_says_to_propose_again(self):
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "marcar_periodo", "arguments": {"kind": "viaje", "start_day": "2020-01-01"}}},
        ])
        pid = json.loads(respuestas[0]["result"]["content"][-1]["text"])["propuesta_id"]
        vencido = "2026-11-10T08:30:00+00:00"
        self.conn.execute("UPDATE propuesta_agente SET expira_en = ? WHERE id = ?", (vencido, pid))
        self.conn.commit()

        _, respuestas2 = self._ejecutar([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_APLICAR, "arguments": {"propuesta_id": pid}}},
        ])
        r = respuestas2[0]["result"]
        self.assertTrue(r["isError"])
        # BODY-MCP-COPIA-PC-1: content[0] is now the copia-pc freshness line.
        texto = r["content"][-1]["text"]
        self.assertIn("03:30", texto)  # 08:30 UTC -> 03:30 local (America/Bogota, UTC-5)
        self.assertIn("noviembre", texto)
        self.assertNotEqual(texto, "Esa propuesta ya no está pendiente.")
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["estado"], "expirada")


class DescartarPropuestaTest(_McpTestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: descartar_propuesta, gated by the same setting as
    aplicar_propuesta, writes no health data and needs no confirm."""

    def _crear_propuesta_mcp(self, start_day: str = "2020-01-01") -> int:
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "marcar_periodo", "arguments": {"kind": "viaje", "start_day": start_day}}},
        ])
        return json.loads(respuestas[0]["result"]["content"][-1]["text"])["propuesta_id"]

    def test_tool_is_absent_from_tools_list_by_default(self):
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in respuestas[0]["result"]["tools"]}
        self.assertNotIn(mcp.NOMBRE_DESCARTAR, nombres)

    def test_calling_it_while_off_is_refused_and_writes_nothing(self):
        pid = self._crear_propuesta_mcp()
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_DESCARTAR, "arguments": {"propuesta_id": pid}}},
        ])
        self.assertTrue(respuestas[0]["result"]["isError"])
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["estado"], "propuesta")

    def test_tool_is_listed_once_the_setting_is_on(self):
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        _, respuestas = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in respuestas[0]["result"]["tools"]}
        self.assertIn(mcp.NOMBRE_DESCARTAR, nombres)
        self.assertIn(mcp.NOMBRE_APLICAR, nombres)

    def test_discards_its_own_pending_proposal_and_writes_no_health_data(self):
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        before = self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0]
        pid = self._crear_propuesta_mcp()
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_DESCARTAR, "arguments": {"propuesta_id": pid}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        salida = json.loads(r["content"][-1]["text"])
        self.assertEqual(salida["estado"], "descartada")
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["estado"], "descartada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0], before)

    def test_refuses_an_in_app_proposal_naming_why(self):
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        cid = agente_conversacion.crear(self.conn, self.person_id)
        registro = agente_propuestas.crear(self.conn, self.person_id, cid, "proponer_idioma", {"lang": "en"})
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_DESCARTAR, "arguments": {"propuesta_id": registro["id"]}}},
        ])
        r = respuestas[0]["result"]
        self.assertTrue(r["isError"])
        self.assertTrue(r["content"][-1]["text"])
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                (registro["id"],)).fetchone()
        self.assertEqual(row["estado"], "propuesta")

    def test_refuses_an_already_applied_proposal(self):
        db.set_aplicar_mcp(self.conn, self.person_id, True)
        pid = self._crear_propuesta_mcp()
        agente_propuestas.aplicar(self.conn, self.person_id, pid)
        _, respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": mcp.NOMBRE_DESCARTAR, "arguments": {"propuesta_id": pid}}},
        ])
        self.assertTrue(respuestas[0]["result"]["isError"])
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(row["estado"], "aplicada")


if __name__ == "__main__":
    unittest.main()
