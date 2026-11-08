"""BODY-HOGAR-CUENTA-1: household membership comes from Cuenta Duna's `duna_hogar` claim, read at
sign-in and refreshed hourly while signed in, into the `hogar_miembro` mirror -- instead of a table
only tests and the demo wrote directly (hogar-v1; orchestrator ledger BODY-HOGAR-PLAN-1 cont. 2,
against BODY-HOGAR-CUENTA-OBS-1). Cuenta is stubbed throughout (`cuenta._get_json`, or
tests/test_cuenta_rp.py's FakeIssuer for a full sign-in round trip); invented subs only, never a
real health figure.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import cuenta, db, household, privacidad
from tests.test_cuenta_rp import CuentaRpTestCase, SUB

_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only"}


def _conn(tc: unittest.TestCase):
    conn = db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "hc.db")
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _add_person(conn, pid: int, name: str, sub: str | None = None) -> None:
    conn.execute("INSERT INTO person (id, display_name, cuenta_sub) VALUES (?, ?, ?)", (pid, name, sub))
    conn.commit()


class SincronizarDesdeClaimOutcomeTest(unittest.TestCase):
    """Each outcome word, directly against household.sincronizar_desde_claim."""

    def test_absent_key_is_sin_dato_and_changes_nothing(self):
        conn = _conn(self)
        _add_person(conn, 2, "Partner", sub="sub-2")
        self.assertEqual(household.sincronizar_desde_claim(conn, 1, {}), "sin_dato")
        self.assertIsNone(household.household_of(conn, 1))

    def test_none_is_sin_hogar_and_deletes_the_row(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        _add_person(conn, 2, "Partner", sub="sub-2")
        household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-2"]}})
        self.assertIsNotNone(household.household_of(conn, 1))

        self.assertEqual(household.sincronizar_desde_claim(conn, 1, {"duna_hogar": None}), "sin_hogar")
        self.assertIsNone(household.household_of(conn, 1))
        # The partner's own row is untouched by person 1's claim.
        self.assertEqual(household.household_of(conn, 2).member_ids, (2,))

    def test_malformed_values_are_sin_dato_and_change_nothing(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        conn.commit()
        for bad in ("not-a-dict", {"id": 123, "miembros": ["sub-1"]}, {"id": "h-1", "miembros": "sub-1"},
                    {"id": "h-1", "miembros": [1, 2]}, {"miembros": ["sub-1"]}, {"id": "h-1"}):
            with self.subTest(bad=bad):
                self.assertEqual(household.sincronizar_desde_claim(conn, 1, {"duna_hogar": bad}), "sin_dato")
                self.assertIsNone(household.household_of(conn, 1))

    def test_a_well_formed_value_is_actualizado_and_writes_fuente_cuenta(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        _add_person(conn, 2, "Partner", sub="sub-2")
        outcome = household.sincronizar_desde_claim(
            conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-2"]}})
        self.assertEqual(outcome, "actualizado")
        h = household.household_of(conn, 1)
        self.assertEqual((h.id, h.member_ids), ("h-1", (1, 2)))
        row = conn.execute("SELECT fuente, member_sub FROM hogar_miembro WHERE person_id = 1").fetchone()
        self.assertEqual((row["fuente"], row["member_sub"]), ("cuenta", "sub-1"))

    def test_absent_from_miembros_is_treated_like_none(self):
        """Rule: 'this person, if absent from miembros (malformed claim), is treated like None' --
        only their own row is touched, the rest of the household is untouched."""
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        _add_person(conn, 2, "Partner", sub="sub-2")
        household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-2"]}})

        outcome = household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-2"]}})
        self.assertEqual(outcome, "sin_hogar")
        self.assertIsNone(household.household_of(conn, 1))
        self.assertEqual(household.household_of(conn, 2).member_ids, (2,))


class LeftMemberTest(unittest.TestCase):
    """A partner who left disappears from the hogar when EITHER member's claim arrives."""

    def test_disappears_when_the_remaining_members_claim_arrives(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        _add_person(conn, 2, "Partner", sub="sub-2")
        household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-2"]}})
        self.assertEqual(household.household_of(conn, 1).member_ids, (1, 2))

        household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1"]}})
        self.assertEqual(household.household_of(conn, 1).member_ids, (1,))
        self.assertIsNone(household.household_of(conn, 2))

    def test_disappears_when_the_leaving_members_own_claim_arrives(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        _add_person(conn, 2, "Partner", sub="sub-2")
        household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-2"]}})

        household.sincronizar_desde_claim(conn, 2, {"duna_hogar": None})
        self.assertIsNone(household.household_of(conn, 2))
        self.assertEqual(household.household_of(conn, 1).member_ids, (1,))


class SkippedThenJoinsTest(unittest.TestCase):
    def test_member_with_no_body_person_is_skipped_then_joins_on_first_sign_in(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        outcome = household.sincronizar_desde_claim(
            conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-unknown"]}})
        self.assertEqual(outcome, "actualizado")
        self.assertEqual(household.household_of(conn, 1).member_ids, (1,))

        _add_person(conn, 2, "NewMember", sub="sub-unknown")
        household.sincronizar_desde_claim(
            conn, 2, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-unknown"]}})
        self.assertEqual(household.household_of(conn, 1).member_ids, (1, 2))


class MovingHogarTest(unittest.TestCase):
    def test_person_moving_hogar(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        _add_person(conn, 2, "OldPartner", sub="sub-2")
        household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-old", "miembros": ["sub-1", "sub-2"]}})
        self.assertEqual(household.household_of(conn, 1).id, "h-old")

        _add_person(conn, 3, "NewPartner", sub="sub-3")
        household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-new", "miembros": ["sub-1", "sub-3"]}})
        h = household.household_of(conn, 1)
        self.assertEqual((h.id, h.member_ids), ("h-new", (1, 3)))
        # The old partner's own row is untouched by person 1's claim about a different hogar_id --
        # it only goes stale until their own next sign-in or refresh (hogar-v1's known v1 gap).
        self.assertEqual(household.household_of(conn, 2).member_ids, (2,))


class SignInSyncTest(CuentaRpTestCase):
    """handle_callback's call point: synced at sign-in, never fails the sign-in itself."""

    def test_sign_in_syncs_the_household_mirror(self):
        r = self._sign_in(extra={"duna_hogar": {"id": "h-1", "miembros": [SUB]}})
        self.assertEqual(r.status_code, 303)
        conn = db.connect()
        try:
            person_id = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (SUB,)).fetchone()["id"]
            row = conn.execute(
                "SELECT hogar_id, fuente FROM hogar_miembro WHERE person_id = ?", (person_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual((row["hogar_id"], row["fuente"]), ("h-1", "cuenta"))

    def test_sign_in_still_succeeds_when_the_sync_raises(self):
        with mock.patch.object(household, "sincronizar_desde_claim", side_effect=RuntimeError("boom")):
            r = self._sign_in(return_to="/goals", extra={"duna_hogar": {"id": "h-1", "miembros": [SUB]}})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/goals")
        self.assertIsNotNone(r.cookies.get(cuenta.SESSION_COOKIE))


class HourlyRefreshTest(unittest.TestCase):
    """The hourly last-seen touch inside resolve_session."""

    def _signed_in(self, sub="sub-refresh-1", hours_stale=2):
        conn = _conn(self)
        _add_person(conn, 2, "Me", sub=sub)
        token = cuenta.create_session(conn, 2, "sid-1")
        stale = (datetime.now(timezone.utc) - timedelta(hours=hours_stale)).isoformat(timespec="seconds")
        conn.execute("UPDATE sesion SET last_seen_at = ?", (stale,))
        conn.commit()
        return conn, token

    def test_refreshes_when_no_hogar_row_exists(self):
        conn, token = self._signed_in()
        with mock.patch.dict(os.environ, _HOSTED_ENV), \
             mock.patch.object(cuenta, "_get_json",
                                return_value={"duna_hogar": {"id": "h-1", "miembros": ["sub-refresh-1"]}}) as m:
            cuenta.resolve_session(conn, token)
        m.assert_called_once()
        self.assertEqual(m.call_args.args[0], "https://fake-cuenta.test/servicio/cuentas/sub-refresh-1")
        self.assertEqual(m.call_args.kwargs["auth"], (cuenta.CLIENT_ID, "s3cr3t-test-only"))
        row = conn.execute("SELECT fuente, hogar_id FROM hogar_miembro WHERE person_id = 2").fetchone()
        self.assertEqual((row["fuente"], row["hogar_id"]), ("cuenta", "h-1"))

    def test_not_called_when_a_cuenta_row_is_fresh(self):
        conn, token = self._signed_in()
        conn.executescript(household.SCHEMA)
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, fuente, actualizado_at) VALUES (2, 'h-1', 'cuenta', ?)",
            (fresh,))
        conn.commit()
        with mock.patch.dict(os.environ, _HOSTED_ENV), mock.patch.object(cuenta, "_get_json") as m:
            cuenta.resolve_session(conn, token)
        m.assert_not_called()

    def test_not_called_for_a_fuente_local_row_even_when_stale(self):
        conn, token = self._signed_in()
        conn.executescript(household.SCHEMA)
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, fuente, actualizado_at) VALUES (2, 'h-1', 'local', ?)",
            (old,))
        conn.commit()
        with mock.patch.dict(os.environ, _HOSTED_ENV), mock.patch.object(cuenta, "_get_json") as m:
            cuenta.resolve_session(conn, token)
        m.assert_not_called()

    def test_network_error_keeps_the_existing_mirror(self):
        conn, token = self._signed_in()
        conn.executescript(household.SCHEMA)
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, fuente, actualizado_at) VALUES (2, 'h-1', 'cuenta', ?)",
            (old,))
        conn.commit()
        with mock.patch.dict(os.environ, _HOSTED_ENV), \
             mock.patch.object(cuenta, "_get_json", side_effect=httpx.ConnectError("boom")):
            cuenta.resolve_session(conn, token)
        row = conn.execute("SELECT hogar_id FROM hogar_miembro WHERE person_id = 2").fetchone()
        self.assertEqual(row["hogar_id"], "h-1")

    def test_never_called_in_local_mode(self):
        conn, token = self._signed_in()
        self.assertNotIn(cuenta.ISSUER_ENV, os.environ)
        with mock.patch.object(cuenta, "_get_json") as m:
            cuenta.resolve_session(conn, token)
        m.assert_not_called()


class NonDictClaimsTest(unittest.TestCase):
    """Fix 3: an answer that parses as JSON but is not an object reaches sincronizar_desde_claim
    as `claims` (e.g. a service-API answer that is a list or a string)."""

    def test_sincronizar_desde_claim_treats_non_dict_claims_as_sin_dato(self):
        conn = _conn(self)
        for bad in (["duna_hogar"], "duna_hogar", 42, None):
            with self.subTest(bad=bad):
                self.assertEqual(household.sincronizar_desde_claim(conn, 1, bad), "sin_dato")
                self.assertIsNone(household.household_of(conn, 1))

    def test_refrescar_hogar_keeps_the_mirror_when_the_answer_is_not_a_dict(self):
        conn = _conn(self)
        _add_person(conn, 2, "Me", sub="sub-nd-1")
        household.sincronizar_desde_claim(conn, 2, {"duna_hogar": {"id": "h-1", "miembros": ["sub-nd-1"]}})
        with mock.patch.dict(os.environ, _HOSTED_ENV), \
             mock.patch.object(cuenta, "_get_json", return_value=["not", "a", "dict"]):
            cuenta.refrescar_hogar(conn, 2)
        row = conn.execute("SELECT hogar_id FROM hogar_miembro WHERE person_id = 2").fetchone()
        self.assertEqual(row["hogar_id"], "h-1")


class UrlEscapingTest(unittest.TestCase):
    """Fix 2: refrescar_hogar must not put a raw `sub` unescaped into the URL path."""

    def test_refrescar_hogar_escapes_the_sub_in_the_url(self):
        conn = _conn(self)
        _add_person(conn, 2, "Me", sub="sub/weird 1")
        with mock.patch.dict(os.environ, _HOSTED_ENV), \
             mock.patch.object(cuenta, "_get_json", return_value={"duna_hogar": None}) as m:
            cuenta.refrescar_hogar(conn, 2)
        self.assertEqual(m.call_args.args[0], "https://fake-cuenta.test/servicio/cuentas/sub%2Fweird%201")


class UnrelatedPersonTest(unittest.TestCase):
    """Fix 4: the upsert selects only person rows whose cuenta_sub is in the claim's miembros,
    not every person with a cuenta_sub."""

    def test_upsert_does_not_touch_a_person_not_listed_in_miembros(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        _add_person(conn, 2, "Partner", sub="sub-2")
        _add_person(conn, 3, "Unrelated", sub="sub-999")
        outcome = household.sincronizar_desde_claim(
            conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-2"]}})
        self.assertEqual(outcome, "actualizado")
        self.assertIsNone(household.household_of(conn, 3))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM hogar_miembro WHERE person_id = 3").fetchone()[0], 0
        )


class TransactionSafetyTest(unittest.TestCase):
    """Fix 1: sqlite3's `executescript` COMMITs any transaction already open on `conn` before it
    runs. sincronizar_desde_claim and _debe_refrescar_hogar now create the table with a plain
    `execute()` instead, so a caller's open transaction survives a rollback."""

    def test_sincronizar_desde_claim_does_not_commit_an_open_transaction(self):
        conn = _conn(self)
        conn.execute("BEGIN")
        conn.execute("UPDATE person SET display_name = 'Changed' WHERE id = 1")
        self.assertEqual(household.sincronizar_desde_claim(conn, 1, {}), "sin_dato")
        conn.rollback()
        name = conn.execute("SELECT display_name FROM person WHERE id = 1").fetchone()["display_name"]
        self.assertNotEqual(name, "Changed")

    def test_debe_refrescar_hogar_does_not_commit_an_open_transaction(self):
        conn = _conn(self)
        _add_person(conn, 2, "Me", sub="sub-tx-1")
        conn.execute("BEGIN")
        conn.execute("UPDATE person SET display_name = 'Changed' WHERE id = 2")
        cuenta._debe_refrescar_hogar(conn, 2)
        conn.rollback()
        name = conn.execute("SELECT display_name FROM person WHERE id = 2").fetchone()["display_name"]
        self.assertNotEqual(name, "Changed")


class LazyTableCreationTest(unittest.TestCase):
    """`db.init` itself does not create `hogar_miembro` on a database that never had `hogar_local`
    (measured against the repository, BODY-HOGAR-CUENTA-1-M -- db.py is outside this slice's
    `touches`, so the table stays lazily created here, via a single `execute()` that cannot commit
    a caller's open transaction, rather than `db.init`'s job)."""

    def test_household_module_creates_hogar_miembro_lazily_on_a_fresh_db(self):
        path = Path(tempfile.mkdtemp(dir=TMP)) / "lazy.db"
        conn = db.connect(path)
        self.addCleanup(conn.close)
        db.init(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("hogar_miembro", tables)
        household.sincronizar_desde_claim(conn, 1, {})
        tables2 = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("hogar_miembro", tables2)


class MigrationTest(unittest.TestCase):
    def test_hogar_local_migrates_to_hogar_miembro_and_is_idempotent(self):
        path = Path(tempfile.mkdtemp(dir=TMP)) / "mig.db"
        conn = db.connect(path)
        self.addCleanup(conn.close)
        # A database at the shape this slice replaces, built directly so this test exercises
        # _migrate_hogar against a genuinely pre-slice database, never db.init()'s own writer.
        conn.executescript(db.SCHEMA)
        conn.execute(
            "CREATE TABLE hogar_local (person_id INTEGER PRIMARY KEY REFERENCES person(id), "
            "hogar_id TEXT NOT NULL, member_sub TEXT)"
        )
        conn.execute("INSERT OR IGNORE INTO person (id, display_name) VALUES (1, 'Me')")
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Partner')")
        conn.executemany("INSERT INTO hogar_local (person_id, hogar_id) VALUES (?, ?)",
                          [(1, "h-1"), (2, "h-1")])
        conn.commit()

        db.init(conn)

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("hogar_local", tables)
        self.assertIn("hogar_miembro", tables)
        rows = conn.execute(
            "SELECT person_id, hogar_id, fuente FROM hogar_miembro ORDER BY person_id"
        ).fetchall()
        self.assertEqual([(r["person_id"], r["hogar_id"], r["fuente"]) for r in rows],
                         [(1, "h-1", "local"), (2, "h-1", "local")])

        db.init(conn)  # idempotent: nothing left to migrate, nothing changes
        rows2 = conn.execute(
            "SELECT person_id, hogar_id, fuente FROM hogar_miembro ORDER BY person_id"
        ).fetchall()
        self.assertEqual([(r["person_id"], r["hogar_id"], r["fuente"]) for r in rows2],
                         [(1, "h-1", "local"), (2, "h-1", "local")])


class ExportDeleteTest(unittest.TestCase):
    def test_export_and_delete_all_cover_hogar_miembro(self):
        conn = _conn(self)
        conn.execute("UPDATE person SET cuenta_sub = 'sub-1' WHERE id = 1")
        _add_person(conn, 2, "Partner", sub="sub-2")
        household.sincronizar_desde_claim(conn, 1, {"duna_hogar": {"id": "h-1", "miembros": ["sub-1", "sub-2"]}})

        data = privacidad.export_person(conn, 1)
        self.assertEqual(data["hogar"]["hogar_id"], "h-1")
        self.assertEqual(data["hogar"]["fuente"], "cuenta")

        privacidad.delete_person(conn, 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM hogar_miembro WHERE person_id = 1").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
