"""BODY-HOGAR-ENLACE-1: a goal can point at where the rest of its plan lives in another Duna app --
first a Lugares trip -- instead of copying it (DESIGN rule 17; orchestrator ledger BODY-HOGAR-PLAN-1
ruling 5 and (cont. 4)). Invented demo ids only, never a real figure.
"""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, enlaces, goals, identity, privacidad, web

client = TestClient(web.app, follow_redirects=False)

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "body" / "templates" / "goal.html"


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "he.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _as(tc: unittest.TestCase, person_id: int):
    tc.addCleanup(web.app.dependency_overrides.clear)
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _add_person(conn, pid: int, name: str):
    conn.execute("INSERT INTO person (id, display_name) VALUES (?, ?)", (pid, name))
    conn.commit()


def _link_household(conn, *person_ids, hogar_id="h-1"):
    conn.executemany(
        "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
        [(pid, hogar_id) for pid in person_ids])
    conn.commit()


def _goal(conn, creator_id: int, title="Preparar El Cocuy", **kw):
    gid = goals.propose(conn, creator_id, title, "2026-11-01", **kw)
    goals.activate(conn, gid)
    return gid


class RefDesdeEntradaTest(unittest.TestCase):
    def test_bare_id(self):
        self.assertEqual(enlaces.ref_desde_entrada("lugares", "123"), "123")

    def test_full_url(self):
        self.assertEqual(
            enlaces.ref_desde_entrada("lugares", "https://lugares.duna.solutions/viajes/123"), "123")

    def test_trailing_slash(self):
        self.assertEqual(
            enlaces.ref_desde_entrada("lugares", "https://lugares.duna.solutions/viajes/123/"), "123")

    def test_query_string(self):
        self.assertEqual(
            enlaces.ref_desde_entrada("lugares", "https://lugares.duna.solutions/viajes/123?tab=gastos"), "123")

    def test_fragment(self):
        self.assertEqual(
            enlaces.ref_desde_entrada("lugares", "https://lugares.duna.solutions/viajes/123#dias"), "123")

    def test_another_host_is_rejected(self):
        self.assertIsNone(enlaces.ref_desde_entrada("lugares", "https://evil.example/viajes/1"))

    def test_another_path_is_rejected(self):
        self.assertIsNone(enlaces.ref_desde_entrada("lugares", "https://lugares.duna.solutions/otra/123"))

    def test_zero_is_rejected(self):
        self.assertIsNone(enlaces.ref_desde_entrada("lugares", "0"))

    def test_negative_is_rejected(self):
        self.assertIsNone(enlaces.ref_desde_entrada("lugares", "-5"))

    def test_letters_are_rejected(self):
        self.assertIsNone(enlaces.ref_desde_entrada("lugares", "abc"))

    def test_overlong_number_is_rejected(self):
        self.assertIsNone(enlaces.ref_desde_entrada("lugares", "1234567890123"))

    def test_empty_is_rejected(self):
        self.assertIsNone(enlaces.ref_desde_entrada("lugares", ""))
        self.assertIsNone(enlaces.ref_desde_entrada("lugares", None))

    def test_unknown_app_is_rejected(self):
        self.assertIsNone(enlaces.ref_desde_entrada("finanzas", "123"))


class UrlDeTest(unittest.TestCase):
    def test_default_origin(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DUNA_LUGARES_ORIGEN", None)
            self.assertEqual(enlaces.url_de("lugares", "123"), "https://lugares.duna.solutions/viajes/123")

    def test_env_origin(self):
        with mock.patch.dict(os.environ, {"DUNA_LUGARES_ORIGEN": "https://lugares.local:4000"}):
            self.assertEqual(enlaces.url_de("lugares", "123"), "https://lugares.local:4000/viajes/123")

    def test_unknown_app_is_none(self):
        self.assertIsNone(enlaces.url_de("finanzas", "123"))

    def test_invalid_ref_is_none(self):
        self.assertIsNone(enlaces.url_de("lugares", "not-a-number"))


class UnknownAppStoredTest(unittest.TestCase):
    def test_unknown_app_stored_is_not_rendered(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")
        conn.execute(
            "INSERT INTO goal_enlace (goal_id, app, ref, creado_at) VALUES (?, 'finanzas', '7', '2026-11-01T00:00:00+00:00')",
            (gid,))
        conn.commit()
        self.assertEqual(enlaces.enlaces_de(conn, gid), [])


class CreatorAddReplaceRemoveTest(unittest.TestCase):
    def test_creator_adds_a_link(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")

        _as(self, 1)
        r = client.post(f"/goals/{gid}/enlaces", data={"app": "lugares", "entrada": "4821"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/goals/{gid}#enlaces")
        row = conn.execute("SELECT app, ref FROM goal_enlace WHERE goal_id = ?", (gid,)).fetchone()
        self.assertEqual((row["app"], row["ref"]), ("lugares", "4821"))

    def test_adding_again_replaces_the_one_link_per_app(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")

        _as(self, 1)
        client.post(f"/goals/{gid}/enlaces", data={"app": "lugares", "entrada": "100"})
        client.post(f"/goals/{gid}/enlaces", data={"app": "lugares", "entrada": "200"})
        rows = conn.execute("SELECT ref FROM goal_enlace WHERE goal_id = ?", (gid,)).fetchall()
        self.assertEqual([r["ref"] for r in rows], ["200"])

    def test_creator_removes_a_link(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")
        enlaces.enlazar(conn, gid, "lugares", "4821")

        _as(self, 1)
        r = client.post(f"/goals/{gid}/enlaces/lugares/quitar")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/goals/{gid}#enlaces")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_enlace WHERE goal_id = ?", (gid,)).fetchone()[0], 0)

    def test_invalid_entrada_redirects_with_error_flag_and_writes_nothing(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")

        _as(self, 1)
        r = client.post(f"/goals/{gid}/enlaces", data={"app": "lugares", "entrada": "not-a-trip"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/goals/{gid}?enlace=invalido&app=lugares#enlaces")
        self.assertNotIn("not-a-trip", r.headers["location"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_enlace WHERE goal_id = ?", (gid,)).fetchone()[0], 0)

    def test_invalid_entrada_render_opens_the_fold_with_the_error_above_the_field(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")

        _as(self, 1)
        client.post(f"/goals/{gid}/enlaces", data={"app": "lugares", "entrada": "not-a-trip"})
        r = client.get(f"/goals/{gid}?enlace=invalido&app=lugares")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="enlaces"', r.text)
        match = re.search(
            r"<details[^>]*\bopen\b[^>]*>.*?No pudimos leer ese enlace de Lugares\..*?Pega el enlace del viaje",
            r.text, re.DOTALL,
        )
        self.assertIsNotNone(match, r.text)

    def test_unknown_app_query_param_is_ignored(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")

        _as(self, 1)
        r = client.get(f"/goals/{gid}?enlace=invalido&app=bogus")
        self.assertEqual(r.status_code, 200)
        self.assertNotRegex(r.text, r"<details[^>]*\bopen\b")


class RegistryIsTheAuthorityTest(unittest.TestCase):
    """The template carries no literal app key or app-specific wording -- it loops over
    enlaces.apps_por_enlazar, built from the APPS registry (fix 3)."""

    def test_template_has_no_hardcoded_lugares_value(self):
        self.assertNotIn('value="lugares"', TEMPLATE_PATH.read_text(encoding="utf-8"))

    def test_both_unlinked_apps_fold_then_only_the_unlinked_one_after_linking(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")
        fake_app = {
            "label_key": "goals.enlace.app.lugares",
            "origin_env": "DUNA_FAKE_ORIGEN",
            "origin_default": "https://fake.duna.solutions",
            "path": "/cosas/{ref}",
            "ref_pattern": enlaces.APPS["lugares"]["ref_pattern"],
            "add_summary_key": "test.fake.add_summary",
            "field_key": "test.fake.field_entrada",
            "error_key": "test.fake.error",
        }
        with mock.patch.dict(enlaces.APPS, {"fake": fake_app}):
            _as(self, 1)
            r1 = client.get(f"/goals/{gid}")
            self.assertEqual(r1.status_code, 200)
            self.assertIn("Enlazar un viaje de Lugares", r1.text)
            self.assertIn("test.fake.add_summary", r1.text)
            self.assertIn('value="fake"', r1.text)

            enlaces.enlazar(conn, gid, "fake", "123")
            r2 = client.get(f"/goals/{gid}")
            self.assertEqual(r2.status_code, 200)
            self.assertIn("Enlazar un viaje de Lugares", r2.text)
            self.assertNotIn("test.fake.add_summary", r2.text)

    def test_linked_goal_shows_no_fold_for_that_app(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")
        enlaces.enlazar(conn, gid, "lugares", "4821")

        _as(self, 1)
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Enlazar un viaje de Lugares", r.text)
        self.assertIn("Se abre en Lugares", r.text)


class MemberAndStrangerAccessTest(unittest.TestCase):
    def test_member_sees_the_link_but_gets_404_on_add(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01", hogar_id="h-1", visibility="household")
        enlaces.enlazar(conn, gid, "lugares", "4821")

        _as(self, 2)
        r_get = client.get(f"/goals/{gid}")
        self.assertEqual(r_get.status_code, 200)
        self.assertIn("lugares.duna.solutions/viajes/4821", r_get.text)

        r_add = client.post(f"/goals/{gid}/enlaces", data={"app": "lugares", "entrada": "999"})
        self.assertEqual(r_add.status_code, 404)
        r_quitar = client.post(f"/goals/{gid}/enlaces/lugares/quitar")
        self.assertEqual(r_quitar.status_code, 404)
        self.assertEqual(
            conn.execute("SELECT ref FROM goal_enlace WHERE goal_id = ?", (gid,)).fetchone()["ref"], "4821")

    def test_stranger_sees_neither_goal_nor_link(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _add_person(conn, 3, "Stranger")
        _link_household(conn, 1, 2)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01", hogar_id="h-1", visibility="household")
        enlaces.enlazar(conn, gid, "lugares", "4821")

        _as(self, 3)
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("4821", r.text)


class RenderedLinkTest(unittest.TestCase):
    def test_rel_and_built_url_never_the_pasted_string(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")

        _as(self, 1)
        client.post(f"/goals/{gid}/enlaces",
                    data={"app": "lugares", "entrada": "https://lugares.duna.solutions/viajes/4821?tab=gastos"})
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn('rel="noopener noreferrer"', r.text)
        self.assertIn('href="https://lugares.duna.solutions/viajes/4821"', r.text)
        self.assertNotIn("tab=gastos", r.text)


class LanguageTest(unittest.TestCase):
    def test_spanish_strings(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")

        _as(self, 1)
        r1 = client.get(f"/goals/{gid}")
        self.assertIn("Enlaces", r1.text)
        self.assertIn("Enlazar un viaje de Lugares", r1.text)
        self.assertIn("Pega el enlace del viaje o su número", r1.text)

        enlaces.enlazar(conn, gid, "lugares", "4821")
        r2 = client.get(f"/goals/{gid}")
        self.assertIn("Viaje en Lugares", r2.text)
        self.assertIn("Quitar", r2.text)
        self.assertIn("Se abre en Lugares", r2.text)
        self.assertNotIn("Enlazar un viaje de Lugares", r2.text)

    def test_english_strings(self):
        conn = _reset_db(self)
        db.set_language(conn, "en", 1)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")

        _as(self, 1)
        r1 = client.get(f"/goals/{gid}")
        self.assertIn("Links", r1.text)
        self.assertIn("Link a Lugares trip", r1.text)
        self.assertIn("Paste the trip link or its number", r1.text)

        enlaces.enlazar(conn, gid, "lugares", "4821")
        r2 = client.get(f"/goals/{gid}")
        self.assertIn("Lugares trip", r2.text)
        self.assertIn("Remove", r2.text)
        self.assertIn("Opens in Lugares", r2.text)
        self.assertNotIn("Link a Lugares trip", r2.text)


class ExportDeleteTest(unittest.TestCase):
    def test_export_includes_the_link(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")
        enlaces.enlazar(conn, gid, "lugares", "4821")

        data = privacidad.export_person(conn, 1)
        rows = data["vida"]["enlaces"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["app"], "lugares")
        self.assertEqual(rows[0]["ref"], "4821")
        self.assertEqual(rows[0]["goal_id"], gid)

    def test_export_of_another_person_does_not_see_this_link(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Other")
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")
        enlaces.enlazar(conn, gid, "lugares", "4821")

        data = privacidad.export_person(conn, 2)
        self.assertEqual(data["vida"]["enlaces"], [])

    def test_delete_all_removes_the_links_with_the_goal(self):
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")
        enlaces.enlazar(conn, gid, "lugares", "4821")

        privacidad.delete_person(conn, 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_enlace").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal WHERE id = ?", (gid,)).fetchone()[0], 0)

    def test_delete_all_still_deletes_everything_with_no_executescript_commit_to_lean_on(self):
        """privacidad.delete_person no longer runs an executescript of the household module's old
        schema constant (fix 4, now redundant since BODY-HOGAR-CUENTA-DB-INIT-1 made db.init create
        hogar_miembro unconditionally) -- confirms delete-all still deletes everything through its
        own `with conn:` transaction, not through that executescript's implicit commit."""
        conn = _reset_db(self)
        gid = _goal(conn, 1, kind="milestone", deadline="2027-01-01")
        enlaces.enlazar(conn, gid, "lugares", "4821")
        conn.execute(
            "INSERT INTO ajuste_agente (person_id, nivel_datos, updated_at) VALUES "
            "(1, 'completo', '2026-11-01T00:00:00+00:00')")

        privacidad.delete_person(conn, 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_enlace").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal WHERE id = ?", (gid,)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ajuste_agente WHERE person_id = 1").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM person WHERE id = 1").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
