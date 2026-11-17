"""BODY-HOGAR-NOTA-SESION-GUARD-1: an app writing into another app must not overwrite
human-entered data even when the API allows it -- Hogar may write `plan.nota_sesion` only where
there is none, or where the existing note is itself Hogar's own; a note the person wrote (through
Body's own assistant, in-app or their own MCP client), including one from before this column
existed (NULL origin, read conservatively as the person's own), is never replaced by Hogar.
Invented demo data only, never a real figure.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import anatomia, config, cuenta, db, dispositivo, goals, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.agente.herramientas import ToolError

_EJERCICIOS = [{"ejercicio": "sentadilla", "series": 3, "categoria": anatomia.CATEGORIA_ENUM[0]}]


def _reset_db(name: str):
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn, orig


def _cid(conn, person_id: int = 1, origen: str | None = None) -> int:
    return agente_conversacion.crear(conn, person_id, origen=origen)


def _crear_plan(conn, person_id: int = 1, titulo: str = "Sesión de piernas", day: str = "2026-11-16") -> int:
    goals.propose(conn, person_id, f"Plan de {titulo}", "2026-11-10", kind="milestone", deadline="2027-01-04")
    cid = _cid(conn, person_id)
    out = herramientas.ejecutar(conn, person_id, "planear_entreno", {
        "day": day, "titulo": titulo, "ejercicios": _EJERCICIOS,
    }, "ninguno", cid)
    propuestas.aplicar(conn, person_id, out["propuesta_id"])
    return conn.execute(
        "SELECT id FROM plan WHERE person_id = ? AND title = ?", (person_id, titulo)
    ).fetchone()["id"]


class HogarNotaGuardToolTest(unittest.TestCase):
    """Rulings 2-3, exercised directly through herramientas.ejecutar -- the same path Hogar's own
    route (body/rutas/dispositivos.py) and the in-app/MCP assistants all reach."""

    def setUp(self):
        self.conn, self.orig = _reset_db("guard_tool.db")
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.plan_id = _crear_plan(self.conn)

    def _anotar(self, plan_id: int, nota: str, *, origen: str | None = None, person_id: int = 1,
                hecha: bool | None = None) -> dict:
        cid = _cid(self.conn, person_id, origen=origen)
        args = {"plan_id": plan_id, "nota": nota}
        if hecha is not None:
            args["hecha"] = hecha
        return herramientas.ejecutar(self.conn, person_id, "anotar_sesion", args, "ninguno", cid)

    def _fila(self) -> dict:
        return self.conn.execute(
            "SELECT status, nota_sesion, nota_sesion_en, nota_sesion_origen FROM plan WHERE id = ?",
            (self.plan_id,),
        ).fetchone()

    def _n_propuestas(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente").fetchone()["n"]

    def test_hogar_on_empty_note_applies_with_hogar_origin(self):
        out = self._anotar(self.plan_id, "Buena sesión", origen="hogar")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self._fila()
        self.assertEqual(row["nota_sesion"], "Buena sesión")
        self.assertEqual(row["nota_sesion_origen"], "hogar")

    def test_hogar_over_its_own_note_applies_directly(self):
        out1 = self._anotar(self.plan_id, "Primera nota de Hogar", origen="hogar")
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        out2 = self._anotar(self.plan_id, "Segunda nota de Hogar", origen="hogar")
        resultado = propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self._fila()
        self.assertEqual(row["nota_sesion"], "Segunda nota de Hogar")
        self.assertEqual(row["nota_sesion_origen"], "hogar")

    def test_hogar_over_the_persons_note_is_refused_before_any_proposal(self):
        out1 = self._anotar(self.plan_id, "Nota de la persona")  # in-app: origen None -> 'persona'
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        antes = self._n_propuestas()
        with self.assertRaises(ToolError) as ctx:
            self._anotar(self.plan_id, "Intento de Hogar", origen="hogar")
        self.assertEqual(ctx.exception.codigo, "nota_de_la_persona")
        row = self._fila()
        self.assertEqual(row["nota_sesion"], "Nota de la persona")
        self.assertEqual(row["nota_sesion_origen"], "persona")
        self.assertEqual(self._n_propuestas(), antes)

    def test_hogar_over_a_legacy_null_origin_note_is_refused(self):
        with self.conn:
            self.conn.execute(
                "UPDATE plan SET nota_sesion = 'Nota vieja', nota_sesion_en = '2026-01-01T00:00:00+00:00', "
                "nota_sesion_origen = NULL WHERE id = ?", (self.plan_id,),
            )
        antes = self._n_propuestas()
        with self.assertRaises(ToolError) as ctx:
            self._anotar(self.plan_id, "Intento de Hogar", origen="hogar")
        self.assertEqual(ctx.exception.codigo, "nota_de_la_persona")
        row = self._fila()
        self.assertEqual(row["nota_sesion"], "Nota vieja")
        self.assertIsNone(row["nota_sesion_origen"])
        self.assertEqual(self._n_propuestas(), antes)

    def test_persons_in_app_assistant_over_a_hogar_note_is_proposed_and_applied_with_persona_origin(self):
        out1 = self._anotar(self.plan_id, "Nota de Hogar", origen="hogar")
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        out2 = self._anotar(self.plan_id, "Corrección de la persona")  # in-app, origen None
        self.assertEqual(out2["estado"], "propuesta")
        resultado = propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self._fila()
        self.assertEqual(row["nota_sesion"], "Corrección de la persona")
        self.assertEqual(row["nota_sesion_origen"], "persona")

    def test_persons_own_mcp_assistant_over_a_hogar_note_applies_with_persona_origin(self):
        out1 = self._anotar(self.plan_id, "Nota de Hogar", origen="hogar")
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        out2 = self._anotar(self.plan_id, "Corrección del MCP", origen="mcp")
        resultado = propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self._fila()
        self.assertEqual(row["nota_sesion"], "Corrección del MCP")
        self.assertEqual(row["nota_sesion_origen"], "persona")

    def test_the_persons_own_assistant_is_never_refused_by_this_guard(self):
        out1 = self._anotar(self.plan_id, "Nota de Hogar", origen="hogar")
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        # Never raises, unlike the Hogar-over-persona case above.
        out2 = self._anotar(self.plan_id, "La persona corrige su propio Hogar")
        resultado = propuestas.aplicar(self.conn, 1, out2["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")

    def test_undo_after_a_hogar_apply_restores_note_timestamp_status_and_origin(self):
        out = self._anotar(self.plan_id, "Nota de Hogar", hecha=True, origen="hogar")
        pid = out["propuesta_id"]
        propuestas.aplicar(self.conn, 1, pid)
        resultado = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(resultado["estado"], "deshecha")
        row = self._fila()
        self.assertIsNone(row["nota_sesion"])
        self.assertIsNone(row["nota_sesion_en"])
        self.assertIsNone(row["nota_sesion_origen"])
        self.assertEqual(row["status"], "proposed")

    def test_undo_after_persona_apply_over_a_hogar_note_restores_hogar_origin_exactly(self):
        out1 = self._anotar(self.plan_id, "Nota de Hogar", origen="hogar")
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])
        out2 = self._anotar(self.plan_id, "Corrección de la persona")
        pid2 = out2["propuesta_id"]
        propuestas.aplicar(self.conn, 1, pid2)
        resultado = propuestas.deshacer(self.conn, 1, pid2)
        self.assertEqual(resultado["estado"], "deshecha")
        row = self._fila()
        self.assertEqual(row["nota_sesion"], "Nota de Hogar")
        self.assertEqual(row["nota_sesion_origen"], "hogar")

    def test_another_persons_plan_is_never_reachable(self):
        with self.assertRaisesRegex(ToolError, str(self.plan_id)):
            self._anotar(self.plan_id, "No es mía", person_id=2)
        self.assertIsNone(self._fila()["nota_sesion"])


class HogarNotaGuardRouteTest(unittest.TestCase):
    """Ruling 3-4, through the real route: POST /api/hogar/registrar, as Hogar itself calls it."""

    def setUp(self):
        self.conn, self.orig = _reset_db("guard_route.db")
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.client = TestClient(web.app, follow_redirects=False)
        self.key = dispositivo.crear(self.conn, 1, "PC", {"hogar:escribir"})
        self.plan_id = _crear_plan(self.conn)

    def _post(self, plan_id: int, nota: str):
        return self.client.post(
            "/api/hogar/registrar",
            json={"herramienta": "anotar_sesion", "argumentos": {"plan_id": plan_id, "nota": nota}},
            headers={"Authorization": f"Bearer {self.key}"},
        )

    def test_hogar_applies_directly_with_hogar_origin_through_the_real_route(self):
        r = self._post(self.plan_id, "Nota desde Hogar")
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertTrue(out["ok"])
        row = self.conn.execute(
            "SELECT nota_sesion, nota_sesion_origen FROM plan WHERE id = ?", (self.plan_id,)
        ).fetchone()
        self.assertEqual(row["nota_sesion"], "Nota desde Hogar")
        self.assertEqual(row["nota_sesion_origen"], "hogar")

    def test_refusal_through_the_real_route_carries_the_es_text_and_motivo(self):
        cid = _cid(self.conn)
        out = herramientas.ejecutar(
            self.conn, 1, "anotar_sesion", {"plan_id": self.plan_id, "nota": "Nota de la persona"}, "ninguno", cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        antes = self.conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente").fetchone()["n"]

        r = self._post(self.plan_id, "Intento de Hogar")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        salida = r.json()
        self.assertFalse(salida["ok"])
        self.assertEqual(salida["error"], "Ya hay una nota tuya en esta sesión; corrígela en Body.")
        self.assertEqual(salida["motivo"], "nota_de_la_persona")
        row = self.conn.execute(
            "SELECT nota_sesion, nota_sesion_origen FROM plan WHERE id = ?", (self.plan_id,)
        ).fetchone()
        self.assertEqual(row["nota_sesion"], "Nota de la persona")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM propuesta_agente").fetchone()["n"], antes)

    def test_refusal_through_the_real_route_carries_the_en_text_and_motivo(self):
        db.set_language(self.conn, "en", 1)
        cid = _cid(self.conn)
        out = herramientas.ejecutar(
            self.conn, 1, "anotar_sesion", {"plan_id": self.plan_id, "nota": "Person's own note"}, "ninguno", cid)
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])

        r = self._post(self.plan_id, "Hogar's attempt")
        self.assertEqual(r.status_code, 200)
        salida = r.json()
        self.assertFalse(salida["ok"])
        self.assertEqual(salida["error"], "There is already a note of yours on this session; edit it in Body.")
        self.assertEqual(salida["motivo"], "nota_de_la_persona")

    def test_hogar_over_its_own_note_through_the_real_route_carries_no_motivo(self):
        r1 = self._post(self.plan_id, "Primera de Hogar")
        self.assertTrue(r1.json()["ok"])
        r2 = self._post(self.plan_id, "Segunda de Hogar")
        salida = r2.json()
        self.assertTrue(salida["ok"])
        self.assertNotIn("motivo", salida)


if __name__ == "__main__":
    unittest.main()
