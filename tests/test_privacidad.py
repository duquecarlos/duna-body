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

from body import compartir, config, cuenta, db, dispositivo, goals, identity, noche_inusual, privacidad, web
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

    def test_v1_only_acceptance_is_sent_to_consent_and_accepting_adds_v2_keeping_v1(self):
        """BODY-ENTRENO-PRIVACIDAD-1: CONSENT_VERSION moved to v2 -- a person whose only accepted
        row is the old v1 is not considered consented, and accepting the new text keeps the v1
        row as history rather than replacing it."""
        self._sign_in(return_to="/")
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id, "consentimiento-v1")
        conn.close()

        r1 = self.client.get("/")
        self.assertEqual(r1.status_code, 303)
        location = r1.headers["location"]
        self.assertTrue(location.startswith("/privacidad/consentimiento?return_to="))
        return_to = parse_qs(urlparse(location).query)["return_to"][0]

        r2 = self.client.post("/privacidad/consentimiento", data={"decision": "acepto", "return_to": return_to})
        self.assertEqual(r2.status_code, 303)
        self.assertEqual(r2.headers["location"], return_to)

        self.assertEqual(
            self._db_count(
                "SELECT COUNT(*) FROM consentimiento WHERE version = ?", privacidad.CONSENT_VERSION
            ),
            1,
        )
        self.assertEqual(
            self._db_count("SELECT COUNT(*) FROM consentimiento WHERE version = ?", "consentimiento-v1"), 1
        )

        r3 = self.client.get("/")
        self.assertEqual(r3.status_code, 200)

    def test_v2_only_acceptance_is_sent_to_consent_and_accepting_adds_v3_keeping_v2(self):
        """BODY-EXPERIMENTOS-CONSENTIMIENTO-1: CONSENT_VERSION moved to v3 -- a person whose only
        accepted row is the prior v2 is not considered consented, and accepting the new text keeps
        the v2 row as history rather than replacing it."""
        self._sign_in(return_to="/")
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id, "consentimiento-v2")
        conn.close()

        r1 = self.client.get("/")
        self.assertEqual(r1.status_code, 303)
        location = r1.headers["location"]
        self.assertTrue(location.startswith("/privacidad/consentimiento?return_to="))
        return_to = parse_qs(urlparse(location).query)["return_to"][0]

        r2 = self.client.post("/privacidad/consentimiento", data={"decision": "acepto", "return_to": return_to})
        self.assertEqual(r2.status_code, 303)
        self.assertEqual(r2.headers["location"], return_to)

        self.assertEqual(
            self._db_count(
                "SELECT COUNT(*) FROM consentimiento WHERE version = ?", privacidad.CONSENT_VERSION
            ),
            1,
        )
        self.assertEqual(
            self._db_count("SELECT COUNT(*) FROM consentimiento WHERE version = ?", "consentimiento-v2"), 1
        )

        r3 = self.client.get("/")
        self.assertEqual(r3.status_code, 200)

    def test_v3_acceptance_is_not_sent_to_consent_again(self):
        self._sign_in(return_to="/")
        conn = db.connect()
        person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
        privacidad.record_consent(conn, person_id, privacidad.CONSENT_VERSION)
        conn.close()

        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

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


class ConsentTextNamesTheNewDataTest(unittest.TestCase):
    """BODY-EXPERIMENTOS-CONSENTIMIENTO-1: consentimiento.que names the minute-by-minute figures,
    experiments and logged moments, in both catalogs."""

    def _que(self, lang: str) -> str:
        path = Path(__file__).resolve().parent.parent / "body" / "i18n" / f"{lang}.json"
        return json.loads(path.read_text(encoding="utf-8"))["privacidad.consentimiento.que"]

    def test_spanish_names_minute_by_minute_experiments_and_moments(self):
        text = self._que("es")
        self.assertIn("minuto a minuto", text)
        self.assertIn("experimento", text)
        self.assertIn("momentos", text)

    def test_english_names_minute_by_minute_experiments_and_moments(self):
        text = self._que("en")
        self.assertIn("minute by minute", text)
        self.assertIn("experiment", text)
        self.assertIn("moments", text)


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


class PeriodExportDeleteTest(unittest.TestCase):
    """BODY-PERIODO-1: a marked period (illness, travel, injury) is FULLY exported, with its own
    note, and fully deleted -- invented demo content only, no real figures."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "periodo.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def test_period_is_a_fully_handled_table(self):
        self.assertIn("period", privacidad.FULLY_HANDLED_TABLES)

    def test_export_includes_the_period_and_its_note(self):
        db.crear_periodo(self.conn, 1, "malestar", "2026-11-01", "2026-11-03", "gripe")
        self.conn.commit()
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["periodos"]), 1)
        row = data["periodos"][0]
        self.assertEqual((row["kind"], row["start_day"], row["end_day"], row["note"]),
                         ("malestar", "2026-11-01", "2026-11-03", "gripe"))

    def test_export_of_another_person_sees_nothing(self):
        db.crear_periodo(self.conn, 1, "malestar", "2026-11-01", "2026-11-03", "gripe")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["periodos"], [])

    def test_delete_all_removes_the_period(self):
        db.crear_periodo(self.conn, 1, "viaje", "2026-11-01", note="secreto")
        self.conn.commit()
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM period").fetchone()[0], 0)


class RecuperacionExportDeleteTest(unittest.TestCase):
    """BODY-RECUPERACION-1: a derived recovery-after-training row is FULLY exported and deleted --
    invented demo content only, no real figures."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "recuperacion.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute(
            "INSERT INTO activity (person_id, source, source_id, day, start_local, duration_s, avg_hr) "
            "VALUES (1, 'garmin', 'act-1', '2026-11-01', '2026-11-01T18:00:00', 1800, 130)"
        )
        self.conn.execute(
            "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, "
            "modelo_version, minutos, motivo, computed_at) VALUES "
            "(1, 'garmin', 'act-1', 'recuperacion-v1', 22, NULL, '2026-11-02T00:00:00+00:00')"
        )
        self.conn.commit()

    def test_recuperacion_actividad_is_a_fully_handled_table(self):
        self.assertIn("recuperacion_actividad", privacidad.FULLY_HANDLED_TABLES)

    def test_export_includes_the_row(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["recuperacion_actividades"]), 1)
        row = data["recuperacion_actividades"][0]
        self.assertEqual((row["activity_source_id"], row["modelo_version"], row["minutos"]),
                         ("act-1", "recuperacion-v1", 22))

    def test_export_of_another_person_sees_nothing(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["recuperacion_actividades"], [])

    def test_delete_all_removes_the_row(self):
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM recuperacion_actividad").fetchone()[0], 0
        )


class TurnoFcExportDeleteTest(unittest.TestCase):
    """BODY-TURNO-FC-1: a derived shift-heart-rate row is FULLY exported and deleted -- invented
    demo content only, no real figures."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "turno_fc.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        self.conn.execute(
            "INSERT INTO turno_fc (person_id, day, kind, modelo_version, fc_mediana, estres_mediana, "
            "bajo_usual_pct, referencia_fc, cobertura, motivo, computed_at) VALUES "
            "(1, '2026-11-01', 'morning', 'turno-fc-v1', 60.0, 22.0, 70, 70.0, 0.95, NULL, "
            "'2026-11-02T00:00:00+00:00')"
        )
        self.conn.commit()

    def test_turno_fc_is_a_fully_handled_table(self):
        self.assertIn("turno_fc", privacidad.FULLY_HANDLED_TABLES)

    def test_export_includes_the_row(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["turno_fc"]), 1)
        row = data["turno_fc"][0]
        self.assertEqual((row["day"], row["kind"], row["modelo_version"], row["fc_mediana"]),
                         ("2026-11-01", "morning", "turno-fc-v1", 60.0))

    def test_export_of_another_person_sees_nothing(self):
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["turno_fc"], [])

    def test_delete_all_removes_the_row(self):
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM turno_fc").fetchone()[0], 0
        )


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


class UsoHerramientaExternaExportDeleteTest(unittest.TestCase):
    """BODY-MCP-LOCAL-1: one row per call an outside caller (the local MCP server) made against the
    tool registry -- tool name, UTC time, caller id only -- FULLY exported and deleted."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "uso_externo.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def test_is_a_fully_handled_table(self):
        self.assertIn("uso_herramienta_externa", privacidad.FULLY_HANDLED_TABLES)

    def test_export_includes_the_row(self):
        db.registrar_uso_herramienta_externa(self.conn, 1, "leer_hoy", "mcp-local")
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["asistente"]["uso_externo"]), 1)
        row = data["asistente"]["uso_externo"][0]
        self.assertEqual(row["herramienta"], "leer_hoy")
        self.assertEqual(row["caller"], "mcp-local")

    def test_export_of_another_person_sees_nothing(self):
        db.registrar_uso_herramienta_externa(self.conn, 1, "leer_hoy", "mcp-local")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["asistente"]["uso_externo"], [])

    def test_delete_all_removes_the_row(self):
        db.registrar_uso_herramienta_externa(self.conn, 1, "leer_hoy", "mcp-local")
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM uso_herramienta_externa").fetchone()[0], 0
        )


class AplicarMcpExportDeleteTest(unittest.TestCase):
    """BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 7: the person's own `aplicar_mcp`
    setting is in their export, and delete-all removes it with the rest of ajuste_agente (already
    a FULLY_HANDLED_TABLES member -- this only proves that stays true for this one new column)."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "aplicar_mcp.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def test_export_shows_off_by_default(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertFalse(data["ajustes"]["aplicar_mcp"])

    def test_export_shows_on_once_set(self):
        db.set_aplicar_mcp(self.conn, 1, True)
        data = privacidad.export_person(self.conn, 1)
        self.assertTrue(data["ajustes"]["aplicar_mcp"])

    def test_delete_all_removes_the_setting_row(self):
        db.set_aplicar_mcp(self.conn, 1, True)
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM ajuste_agente WHERE person_id = 1").fetchone()[0], 0
        )


class NocheInusualVistaExportDeleteTest(unittest.TestCase):
    """BODY-NOCHE-INUSUAL-1 ruling 6: the "Ya lo vi" dismissal reuses `moment` -- already a fully
    exported/deleted table -- instead of a new one, so export and delete-all already cover it with
    no code change here; this only proves that stays true for its own `kind`."""

    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "noche_inusual.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)

    def test_export_includes_the_dismissal(self):
        noche_inusual.marcar_vista(self.conn, 1, "2026-11-09")
        data = privacidad.export_person(self.conn, 1)
        kinds = {m["kind"] for m in data["experimentos"]["momentos"]}
        self.assertIn(noche_inusual.DISMISS_KIND, kinds)

    def test_delete_all_removes_the_dismissal(self):
        noche_inusual.marcar_vista(self.conn, 1, "2026-11-09")
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM moment WHERE person_id = 1").fetchone()[0], 0)


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
