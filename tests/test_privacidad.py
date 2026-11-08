"""BODY-PRIVACIDAD-1: consent, export and delete-all (host-deploy-v1-amendment-sensitive-data rule
6, Ley 1581 dato sensible; BODY-HOSTING-PLAN-1 step 6). Invented demo data only, no real health
figures.
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import compartir, config, cuenta, db, dispositivo, goals, identity, privacidad, web
from body.agente import conversacion as agente_conversacion
from body.agente import propuestas as agente_propuestas
from body.agente import uso as agente_uso
from body.agente.cliente import Uso
from tests.test_cuenta_rp import CuentaRpTestCase, ISSUER, SUB


class _CloseRecorder:
    """Wraps a real sqlite3.Connection, recording whether close() was ever called on it (same
    technique as tests/test_sesion_conexion_1.py)."""

    def __init__(self, real):
        self._real = real
        self.closed = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        self.closed = True
        self._real.close()


def _recording_connect():
    real_connect = db.connect
    recorders: list[_CloseRecorder] = []

    def _connect(*a, **kw):
        rec = _CloseRecorder(real_connect(*a, **kw))
        recorders.append(rec)
        return rec

    return _connect, recorders


class HostedConsentGateTest(CuentaRpTestCase):
    def test_page_get_redirects_to_consent_when_none_accepted(self):
        self._sign_in(return_to="/")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/privacidad/consentimiento?return_to="))

    def test_post_without_consent_is_403(self):
        self._sign_in()
        r = self.client.post("/goals/1/pause")
        self.assertEqual(r.status_code, 403)

    def test_healthz_and_logout_still_work_without_consent(self):
        self._sign_in()
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.post("/logout").status_code, 303)

    def test_accepting_stores_the_version_and_then_the_original_page_renders(self):
        self._sign_in(return_to="/goals")
        r1 = self.client.get("/goals")
        self.assertEqual(r1.status_code, 303)
        location = r1.headers["location"]
        self.assertTrue(location.startswith("/privacidad/consentimiento?"))
        return_to = parse_qs(urlparse(location).query)["return_to"][0]

        r2 = self.client.get(location)
        self.assertEqual(r2.status_code, 200)

        r3 = self.client.post("/privacidad/consentimiento", data={"decision": "acepto", "return_to": return_to})
        self.assertEqual(r3.status_code, 303)
        self.assertEqual(r3.headers["location"], return_to)
        self.assertEqual(
            self._db_count("SELECT COUNT(*) FROM consentimiento WHERE version = ?", privacidad.CONSENT_VERSION), 1
        )

        r4 = self.client.get("/goals")
        self.assertEqual(r4.status_code, 200)

    def test_declining_signs_out_and_keeps_nothing_new(self):
        self._sign_in(return_to="/")
        location = self.client.get("/").headers["location"]
        return_to = parse_qs(urlparse(location).query)["return_to"][0]

        r = self.client.post("/privacidad/consentimiento", data={"decision": "no", "return_to": return_to})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith(f"{ISSUER}/oauth/logout?"))
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion"), 0)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM consentimiento"), 0)

    def test_declining_closes_its_connection_and_response_is_unchanged(self):
        """BODY-PRIVACIDAD-CONEXION-1: the decline branch now takes its connection from
        comun._ctx(person_id) (the @comun._cierra_conexion_async decorator closes it), rather than
        its own unguarded db.connect()/db.close() -- same recording technique as
        tests/test_sesion_conexion_1.py uses for sesion.py's own direct-connect routes."""
        self._sign_in(return_to="/")
        location = self.client.get("/").headers["location"]
        return_to = parse_qs(urlparse(location).query)["return_to"][0]

        connect, recorders = _recording_connect()
        with mock.patch.object(db, "connect", side_effect=connect):
            r = self.client.post("/privacidad/consentimiento", data={"decision": "no", "return_to": return_to})

        self.assertTrue(recorders)
        self.assertTrue(all(rec.closed for rec in recorders))
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith(f"{ISSUER}/oauth/logout?"))
        self.assertIn("Max-Age=0", r.headers.get("set-cookie", ""))


class ConsentTextLanguageTest(CuentaRpTestCase):
    def test_both_languages_render_the_consent_text(self):
        self._sign_in()
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]

        db.set_language(conn, "es", person_id)
        r_es = self.client.get("/privacidad/consentimiento")
        self.assertEqual(r_es.status_code, 200)
        self.assertIn("Antes de empezar", r_es.text)

        db.set_language(conn, "en", person_id)
        r_en = self.client.get("/privacidad/consentimiento")
        self.assertEqual(r_en.status_code, 200)
        self.assertIn("Before we start", r_en.text)
        conn.close()


class LocalModeNoGateTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local.db"
        conn = db.connect()
        db.init(conn)
        conn.close()
        self.client = TestClient(web.app, follow_redirects=False)

    def test_no_gate_in_local_mode(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_consent_routes_404_in_local_mode(self):
        self.assertEqual(self.client.get("/privacidad/consentimiento").status_code, 404)
        self.assertEqual(self.client.post("/privacidad/consentimiento").status_code, 404)


def _override_person(tc: unittest.TestCase, person_id: int) -> None:
    tc.addCleanup(web.app.dependency_overrides.clear)
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


class ExportTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "export.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        _override_person(self, 1)
        self.client = TestClient(web.app, follow_redirects=False)

    def test_export_has_attachment_header_and_only_this_persons_rows(self):
        db.set_metric(self.conn, "2026-11-01", "resting_hr", 55.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-01", "resting_hr", 60.0, "garmin", 2)
        gid_a = goals.propose(self.conn, 1, "A's goal", "2026-11-01", metric_key="resting_hr", direction="lower",
                              target_value=45.0, baseline_value=55.0, deadline="2027-01-01")
        goals.propose(self.conn, 2, "B's goal", "2026-11-01", metric_key="resting_hr", direction="lower",
                      target_value=45.0, baseline_value=55.0, deadline="2027-01-01")
        self.conn.execute(
            "INSERT INTO sesion (id_hash, person_id, created_at, last_seen_at, expires_at) VALUES "
            "('h1', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00')"
        )
        self.conn.commit()

        r = self.client.get("/privacidad/exportar")
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment; filename="body-', r.headers["content-disposition"])

        data = json.loads(r.text)
        self.assertEqual(data["formato"], "body-export-v1")
        titles = {g["title"] for g in data["vida"]["goals"]}
        self.assertIn("A's goal", titles)
        self.assertNotIn("B's goal", titles)
        days = {d["day"] for d in data["dias"]}
        self.assertIn("2026-11-01", days)
        self.assertEqual(len(data["dias"]), 1)
        self.assertNotIn("sesion", data)

    def test_every_person_table_is_exported_or_excepted(self):
        found = privacidad.person_tables(self.conn)
        accounted = (
            privacidad.FULLY_HANDLED_TABLES | privacidad.PARTIAL_EXPORT_TABLES | privacidad.NEVER_EXPORTED_TABLES
        )
        self.assertEqual(found, accounted)


class GarminExportDeleteTest(unittest.TestCase):
    """BODY-GARMIN-HOST-1: garmin_cuenta is a partial-export table (connected-since date only,
    never the ciphertext) and the generic delete-all removes it."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "garmin.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def _insert_row(self):
        self.conn.execute(
            "INSERT INTO garmin_cuenta (person_id, token_cifrado, nonce, conectado_en) VALUES "
            "(1, ?, ?, '2026-11-01T00:00:00+00:00')",
            (b"not-a-real-ciphertext-marker", b"\x00" * 12),
        )
        self.conn.commit()

    def test_export_shows_connected_date_never_the_ciphertext(self):
        self._insert_row()
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(data["garmin"], {"conectado_desde": "2026-11-01T00:00:00+00:00"})
        self.assertNotIn("not-a-real-ciphertext-marker", json.dumps(data))

    def test_export_with_no_garmin_row_is_none(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertIsNone(data["garmin"])

    def test_delete_all_removes_the_garmin_row(self):
        self._insert_row()
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM garmin_cuenta WHERE person_id = 1").fetchone()[0], 0
        )


class DispositivoExportDeleteTest(unittest.TestCase):
    """BODY-DISPOSITIVO-1: dispositivo is a partial-export table (name, scopes, dates -- never
    token_hash) and the generic delete-all removes it."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "dispositivo.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def test_export_has_the_fields_but_never_token_hash(self):
        key = dispositivo.crear(self.conn, 1, "PC de prueba", {"turnos:subir"})
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["dispositivos"]), 1)
        row = data["dispositivos"][0]
        self.assertEqual(row["nombre"], "PC de prueba")
        self.assertEqual(set(row), {"nombre", "alcances", "creado_en", "usado_en"})
        self.assertNotIn(key, json.dumps(data))
        self.assertNotIn("token_hash", json.dumps(data))

    def test_delete_all_removes_the_dispositivo_rows(self):
        dispositivo.crear(self.conn, 1, "PC de prueba", {"turnos:subir"})
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM dispositivo WHERE person_id = 1").fetchone()[0], 0
        )


class AsistenteExportDeleteTest(unittest.TestCase):
    """BODY-ASISTENTE-PROVEEDOR-1: conversations, their messages, usage rows and the per-person
    ceiling override are FULLY exported and deleted -- invented demo content only, no real figures."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "asistente.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def _seed(self):
        cid = agente_conversacion.crear(self.conn, 1, "Mi conversación")
        agente_conversacion.agregar_mensaje(self.conn, cid, 1, "usuario",
                                            [{"tipo": "texto", "texto": "¿cómo dormí anoche?"}])
        agente_conversacion.agregar_mensaje(self.conn, cid, 1, "asistente",
                                            [{"tipo": "texto", "texto": "dormiste bien"}])
        agente_uso.registrar(self.conn, 1, "anthropic", "claude-sonnet-5-5", Uso(entrada=10, salida=4), 1,
                             "texto", None)
        self.conn.execute(
            "INSERT INTO tope_agente (person_id, diario_micro_usd, conteo_diario) VALUES (1, 500000, 50)"
        )
        self.conn.commit()
        return cid

    def test_export_includes_the_conversation_and_usage_counts(self):
        self._seed()
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["asistente"]["conversaciones"]), 1)
        self.assertEqual(data["asistente"]["conversaciones"][0]["titulo"], "Mi conversación")
        self.assertEqual(len(data["asistente"]["mensajes"]), 2)
        self.assertEqual(len(data["asistente"]["uso"]), 1)
        self.assertEqual(data["asistente"]["uso"][0]["entrada"], 10)
        self.assertEqual(data["asistente"]["tope"], {"diario_micro_usd": 500000, "conteo_diario": 50})

    def test_export_of_another_person_does_not_see_these_rows(self):
        self._seed()
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["asistente"]["conversaciones"], [])
        self.assertEqual(data["asistente"]["mensajes"], [])
        self.assertEqual(data["asistente"]["uso"], [])
        self.assertIsNone(data["asistente"]["tope"])

    def test_delete_all_removes_the_conversation_messages_usage_and_tope(self):
        cid = self._seed()
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversacion_agente WHERE person_id = 1").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM mensaje_agente WHERE conversacion_id = ?", (cid,)).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM uso_agente WHERE person_id = 1").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM tope_agente WHERE person_id = 1").fetchone()[0], 0
        )


class PropuestaExportDeleteTest(unittest.TestCase):
    """BODY-ASISTENTE-PROPUESTAS-1: a proposal record is FULLY exported and deleted -- invented
    demo content only, no real figures."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "propuesta.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def _seed(self):
        cid = agente_conversacion.crear(self.conn, 1, "Mi conversación")
        registro = agente_propuestas.crear(self.conn, 1, cid, "proponer_idioma", {"lang": "en"},
                                           antes={"lang": "es"})
        return registro["id"]

    def test_export_includes_the_proposal(self):
        pid = self._seed()
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["asistente"]["propuestas"]), 1)
        self.assertEqual(data["asistente"]["propuestas"][0]["id"], pid)
        self.assertEqual(data["asistente"]["propuestas"][0]["accion"], "proponer_idioma")

    def test_export_of_another_person_does_not_see_this_row(self):
        self._seed()
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["asistente"]["propuestas"], [])

    def test_delete_all_removes_the_proposal(self):
        self._seed()
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM propuesta_agente WHERE person_id = 1").fetchone()[0], 0
        )

    def test_delete_all_removes_a_proposal_pointing_at_a_deleted_goal(self):
        gid = goals.propose(self.conn, 1, "Meta", "2026-10-01", metric_key="resting_hr", direction="lower",
                            target_value=50.0, baseline_value=60.0, deadline="2026-12-01")
        cid = agente_conversacion.crear(self.conn, 1)
        agente_propuestas.crear(self.conn, 1, cid, "proponer_cambio_meta", {"target_value": 48.0},
                                objetivo_id=gid, antes={"target_value": 50.0})
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goal WHERE id = ?", (gid,)).fetchone()[0], 0)


class CompartidoExportDeleteTest(unittest.TestCase):
    """BODY-HOGAR-COMPARTIR-1: a person's own shares (including withdrawn ones) are FULLY exported
    and deleted -- the metric key and the share's own dates only, never a health figure."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "compartido.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.executemany(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
            [(1, "h-1"), (2, "h-1")])
        self.conn.commit()

    def test_export_includes_metric_key_and_dates_never_a_figure(self):
        compartir.compartir(self.conn, 1, "resting_hr")
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["figuras_compartidas"]), 1)
        row = data["figuras_compartidas"][0]
        self.assertEqual(row["metric_key"], "resting_hr")
        self.assertIsNone(row["withdrawn_at"])
        self.assertEqual(set(row), {"metric_key", "hogar_id", "shared_at", "withdrawn_at"})

    def test_export_keeps_a_withdrawn_row_as_history(self):
        compartir.compartir(self.conn, 1, "resting_hr")
        compartir.retirar(self.conn, 1, "resting_hr")
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["figuras_compartidas"]), 1)
        self.assertIsNotNone(data["figuras_compartidas"][0]["withdrawn_at"])

    def test_delete_all_removes_the_rows(self):
        compartir.compartir(self.conn, 1, "resting_hr")
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM figura_compartida WHERE person_id = 1").fetchone()[0], 0
        )


class DeleteAllTest(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "delete.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        _override_person(self, 1)
        self.client = TestClient(web.app, follow_redirects=False)

    def test_wrong_word_is_refused_and_nothing_is_deleted(self):
        r = self.client.post("/privacidad/borrar", data={"confirmacion": "nope"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("error=confirmacion", r.headers["location"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM person WHERE id = 1").fetchone()[0], 1)

    def test_right_word_deletes_every_row_of_a_and_takes_bs_check_on_as_shared_goal(self):
        gid_a = goals.propose(self.conn, 1, "Shared goal", "2026-11-01", kind="milestone",
                              deadline="2027-01-01", hogar_id="h-1", visibility="household")
        goals.activate(self.conn, gid_a)
        hid = goals.add_habit(self.conn, gid_a, "shared habit", {"per": "day", "times": 1})
        goals.check_habit(self.conn, hid, 1, "2026-11-01", True, hoy=date(2026, 11, 1))
        goals.check_habit(self.conn, hid, 2, "2026-11-01", True, hoy=date(2026, 11, 1))
        self.conn.execute(
            "INSERT INTO goal_check (goal_id, person_id, week_start, metric_avg, on_track, habits_done, "
            "habits_due, rule_version) VALUES (?, 2, '2026-11-02', NULL, 1, 1, 1, 'test')", (gid_a,)
        )
        gid_b = goals.propose(self.conn, 2, "B's own goal", "2026-11-01", metric_key="resting_hr",
                              direction="lower", target_value=45.0, baseline_value=55.0, deadline="2027-01-01")
        goals.activate(self.conn, gid_b)
        db.set_metric(self.conn, "2026-11-01", "resting_hr", 55.0, "garmin", 1)
        db.set_metric(self.conn, "2026-11-01", "resting_hr", 60.0, "garmin", 2)
        self.conn.execute(
            "INSERT INTO sesion (id_hash, person_id, created_at, last_seen_at, expires_at) VALUES "
            "('h1', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00')"
        )
        self.conn.commit()

        r = self.client.post("/privacidad/borrar", data={"confirmacion": "BORRAR"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")

        for table in sorted(privacidad.person_tables(self.conn)):
            n = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE person_id = 1").fetchone()[0]
            self.assertEqual(n, 0, f"{table} still has a row for the deleted person")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM person WHERE id = 1").fetchone()[0], 0)

        # The household consequence: the shared goal A created, and B's own check on it, go too.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goal WHERE id = ?", (gid_a,)).fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM goal_check WHERE goal_id = ?", (gid_a,)).fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM habit_check WHERE habit_id = ?", (hid,)).fetchone()[0], 0
        )

        # B's own goal and metric are untouched.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goal WHERE id = ?", (gid_b,)).fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM daily_metric WHERE person_id = 2 AND day = '2026-11-01'"
            ).fetchone()[0],
            1,
        )


class LocalPersonRecreatedAfterDeleteTest(unittest.TestCase):
    """BODY-RENDIMIENTO-1: local mode (identity.current_person_id -> db.local_person_id) can reach
    /privacidad/borrar -- privacidad_borrar (body/rutas/privacidad.py) carries no cuenta.hosted()
    gate, unlike the consent routes. Before this slice, the next request's db.init(conn) (run
    unconditionally on every request) silently recreated the deleted local person row 1; now that
    db.ensure_ready only runs init() once per process per DB file, nothing would otherwise notice
    the row is gone. db.local_person_id's own SELECT-then-INSERT-if-absent is what recreates it."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "local_borrar.db"
        self.client = TestClient(web.app, follow_redirects=False)

    def test_get_after_local_delete_still_works(self):
        r1 = self.client.get("/")
        self.assertEqual(r1.status_code, 200)

        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM person WHERE id = 1").fetchone()[0], 1)

        r2 = self.client.post("/privacidad/borrar", data={"confirmacion": "BORRAR"})
        self.assertEqual(r2.status_code, 303)
        self.assertEqual(r2.headers["location"], "/")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM person WHERE id = 1").fetchone()[0], 0)

        r3 = self.client.get("/")
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM person WHERE id = 1").fetchone()[0], 1)


class HostedDeleteRedirectTest(CuentaRpTestCase):
    def test_delete_redirects_through_cuenta_logout_and_removes_the_session(self):
        self._sign_in()
        pid = self._db_count("SELECT id FROM person WHERE cuenta_sub = ?", SUB)
        conn = db.connect()
        privacidad.record_consent(conn, pid)
        conn.close()

        r = self.client.post("/privacidad/borrar", data={"confirmacion": "BORRAR"})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith(f"{ISSUER}/oauth/logout?"))
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM person WHERE id = ?", pid), 0)
        self.assertEqual(self._db_count("SELECT COUNT(*) FROM sesion"), 0)


if __name__ == "__main__":
    unittest.main()
