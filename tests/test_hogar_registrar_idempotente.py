"""BODY-HOGAR-REGISTRAR-IDEMPOTENTE-1: an optional top-level `ref` on POST /api/hogar/registrar
makes a retry safe -- the first call under a given (person, 'hogar', ref) runs the tool and
records it; a repeat answers with that first write's own result and a stable `"estado":
"ya_estaba"`, never a second proposal. Through the real route only. Invented demo data, no real
figures.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, dispositivo, privacidad, web


def _reset_db(tc: unittest.TestCase, name: str):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)
    return conn


class HogarRegistrarIdempotenteTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "idempotente.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        patcher_env = mock.patch.dict(os.environ, {cuenta.ISSUER_ENV: "https://fake-cuenta.test"})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.client = TestClient(web.app, follow_redirects=False)
        self.key = dispositivo.crear(self.conn, 1, "PC", {"hogar:escribir"})

    def _post(self, body: dict, key: str | None = None):
        return self.client.post("/api/hogar/registrar", json=body,
                                 headers={"Authorization": f"Bearer {key or self.key}"})

    def _comida(self, ref: str | None = None, descripcion: str = "Arroz con pollo", amount: float = 35):
        body = {"herramienta": "registrar_comida",
                "argumentos": {"descripcion": descripcion,
                                "cantidades": [{"metric_key": "protein_g", "amount": amount}]}}
        if ref is not None:
            body["ref"] = ref
        return body

    def _n(self, sql: str, *params) -> int:
        return self.conn.execute(sql, params).fetchone()["n"]

    def test_ref_runs_the_tool_once(self):
        r = self._post(self._comida(ref="meal-1"))
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertTrue(out["ok"])
        self.assertNotIn("estado", out)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1"), 1)
        self.assertEqual(
            self._n("SELECT COUNT(*) AS n FROM escritura_externa WHERE person_id = 1 AND ref = 'meal-1'"), 1)

    def test_same_call_again_is_ya_estaba_and_writes_nothing_new(self):
        out1 = self._post(self._comida(ref="meal-1")).json()
        r2 = self._post(self._comida(ref="meal-1"))
        out2 = r2.json()
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(out2["ok"])
        self.assertEqual(out2["estado"], "ya_estaba")
        self.assertEqual(out2["propuesta_id"], out1["propuesta_id"])
        self.assertEqual(out2["resultado"], out1["resultado"])
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1"), 1)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM propuesta_agente WHERE person_id = 1"), 1)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM escritura_externa WHERE person_id = 1"), 1)

    def test_no_ref_twice_writes_two_meals(self):
        self.assertTrue(self._post(self._comida()).json()["ok"])
        self.assertTrue(self._post(self._comida()).json()["ok"])
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1"), 2)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM escritura_externa"), 0)

    def test_same_ref_for_two_people_is_two_independent_rows(self):
        key_b = dispositivo.crear(self.conn, 2, "PC de B", {"hogar:escribir"})
        out_a = self._post(self._comida(ref="shared-1")).json()
        out_b = self._post(self._comida(ref="shared-1", descripcion="Lo de B"), key=key_b).json()
        self.assertTrue(out_a["ok"])
        self.assertTrue(out_b["ok"])
        self.assertNotIn("estado", out_b)  # person A's success never skips person B's own write
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1"), 1)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 2"), 1)
        self.assertEqual(
            self._n("SELECT COUNT(*) AS n FROM escritura_externa WHERE ref = 'shared-1'"), 2)

    def test_another_persons_ref_is_never_visible(self):
        key_b = dispositivo.crear(self.conn, 2, "PC de B", {"hogar:escribir"})
        self._post(self._comida(ref="only-a"))
        out_b = self._post(self._comida(ref="only-a", descripcion="Lo de B"), key=key_b).json()
        self.assertTrue(out_b["ok"])
        self.assertNotIn("estado", out_b)  # B's own call never sees A's row under the same ref

    def test_bs_refused_write_leaves_as_row_and_a_retry_for_b_then_succeeds(self):
        key_b = dispositivo.crear(self.conn, 2, "PC de B", {"hogar:escribir"})
        out_a = self._post(self._comida(ref="shared-2")).json()
        self.assertTrue(out_a["ok"])

        # B's own first attempt is refused by the tool itself (an empty description) -- nothing
        # recorded for B, and A's own row is untouched.
        refused = self._post(
            {"herramienta": "registrar_comida",
             "argumentos": {"descripcion": "", "cantidades": [{"metric_key": "protein_g", "amount": 10}]},
             "ref": "shared-2"},
            key=key_b,
        ).json()
        self.assertFalse(refused["ok"])
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM escritura_externa WHERE person_id = 2"), 0)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1"), 1)

        retried = self._post(self._comida(ref="shared-2", descripcion="Lo de B"), key=key_b).json()
        self.assertTrue(retried["ok"])
        self.assertNotIn("estado", retried)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 2"), 1)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1"), 1)

    def test_refused_write_records_no_key(self):
        refused = self._post(
            {"herramienta": "registrar_comida",
             "argumentos": {"descripcion": "", "cantidades": [{"metric_key": "protein_g", "amount": 10}]},
             "ref": "bad-1"}
        ).json()
        self.assertFalse(refused["ok"])
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM escritura_externa"), 0)

    def test_same_ref_with_a_different_tool_is_refused_by_name(self):
        self._post(self._comida(ref="dup-1"))
        r = self._post({"herramienta": "anotar_sesion", "argumentos": {}, "ref": "dup-1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            self._n("SELECT COUNT(*) AS n FROM propuesta_agente WHERE person_id = 1 AND accion = 'anotar_sesion'"), 0)
        # The original registrar_comida row under this ref is untouched.
        row = self.conn.execute(
            "SELECT herramienta FROM escritura_externa WHERE person_id = 1 AND ref = 'dup-1'"
        ).fetchone()
        self.assertEqual(row["herramienta"], "registrar_comida")

    def test_malformed_ref_refused(self):
        for bad_ref in (123, "", "x" * 129, [], {}, True):
            body = self._comida()
            body["ref"] = bad_ref
            r = self._post(body)
            self.assertEqual(r.status_code, 400, f"ref={bad_ref!r} should be refused")
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM comida WHERE person_id = 1"), 0)

    def test_export_contains_the_row_and_delete_removes_it(self):
        out = self._post(self._comida(ref="exp-1")).json()
        propuesta_id = out["propuesta_id"]
        data = privacidad.export_person(self.conn, 1)
        rows = data["asistente"]["escrituras_externas"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ref"], rows[0]["herramienta"], rows[0]["propuesta_id"]),
                         ("exp-1", "registrar_comida", propuesta_id))
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(self._n("SELECT COUNT(*) AS n FROM escritura_externa WHERE person_id = 1"), 0)


if __name__ == "__main__":
    unittest.main()
