"""BODY-HOGAR-COMPARTIR-1: a person can share ONE figure at a time with their household, and
withdraw it (hogar-v1, wave 7c: "health numbers stay private to their person; sharing is per
figure, chosen by its owner, and can be withdrawn"). Orchestrator ledger BODY-HOGAR-PLAN-1
ruling 3: a share is a row per (person, metric_key, household) with shared_at/withdrawn_at."""
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import compartir, config, db, household, identity, web

client = TestClient(web.app, follow_redirects=False)

_TODAY = date(2026, 11, 12)


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "hc.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _as(tc: unittest.TestCase, person_id: int):
    tc.addCleanup(web.app.dependency_overrides.clear)
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _add_person(conn, pid: int, name: str, lang: str = "es"):
    conn.execute("INSERT INTO person (id, display_name, language) VALUES (?, ?, ?)", (pid, name, lang))
    conn.commit()


def _link_household(conn, *person_ids, hogar_id="h-1"):
    conn.executescript(household.SCHEMA)
    conn.executemany(
        "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
        [(pid, hogar_id) for pid in person_ids])
    conn.commit()


def _rows(conn, person_id: int, metric_key: str):
    """Every row (active or withdrawn) for this person and metric, oldest first."""
    return conn.execute(
        "SELECT * FROM figura_compartida WHERE person_id = ? AND metric_key = ? ORDER BY id",
        (person_id, metric_key)).fetchall()


class ShareWithdrawReshareTest(unittest.TestCase):
    def test_share_creates_one_active_row(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)

        compartir.compartir(conn, 1, "resting_hr")
        rows = _rows(conn, 1, "resting_hr")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hogar_id"], "h-1")
        self.assertIsNotNone(rows[0]["shared_at"])
        self.assertIsNone(rows[0]["withdrawn_at"])

    def test_sharing_an_already_active_figure_is_a_noop(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)

        compartir.compartir(conn, 1, "resting_hr")
        first_shared_at = _rows(conn, 1, "resting_hr")[0]["shared_at"]
        compartir.compartir(conn, 1, "resting_hr")
        rows = _rows(conn, 1, "resting_hr")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shared_at"], first_shared_at)

    def test_withdraw_sets_withdrawn_at_and_keeps_the_row(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)

        compartir.compartir(conn, 1, "resting_hr")
        compartir.retirar(conn, 1, "resting_hr")
        rows = _rows(conn, 1, "resting_hr")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["withdrawn_at"])

    def test_withdraw_with_nothing_active_is_a_noop(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        compartir.retirar(conn, 1, "resting_hr")  # never shared -- should not raise
        self.assertEqual(_rows(conn, 1, "resting_hr"), [])

    def test_resharing_after_withdrawal_inserts_a_new_row_history_kept(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)

        compartir.compartir(conn, 1, "resting_hr")
        compartir.retirar(conn, 1, "resting_hr")
        compartir.compartir(conn, 1, "resting_hr")
        rows = _rows(conn, 1, "resting_hr")
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]["withdrawn_at"])
        self.assertIsNone(rows[1]["withdrawn_at"])

        active = compartir.mis_compartidas(conn, 1)
        self.assertEqual([r["metric_key"] for r in active], ["resting_hr"])

    def test_mis_compartidas_ignores_a_share_left_in_an_old_household(self):
        """BODY-HOGAR-COMPARTIR-1-M fix 1: after moving households, a share active only in the OLD
        household is not listed, and the new household cannot see it either -- the same `hogar_id`
        both functions read."""
        conn = _reset_db(self)
        _add_person(conn, 2, "OldPartner")
        _link_household(conn, 1, 2, hogar_id="h-old")
        compartir.compartir(conn, 1, "resting_hr")
        self.assertEqual([r["metric_key"] for r in compartir.mis_compartidas(conn, 1)], ["resting_hr"])

        # Person 1 moves to a new household; the share stays recorded under h-old.
        conn.execute("UPDATE hogar_miembro SET hogar_id = 'h-new' WHERE person_id = 1")
        _add_person(conn, 3, "NewPartner")
        conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) "
            "VALUES (3, 'h-new', '2026-01-01T00:00:00+00:00')")
        conn.commit()

        self.assertEqual(compartir.mis_compartidas(conn, 1), [])
        self.assertEqual(compartir.visibles_para(conn, 3), [])


class UniqueIndexTest(unittest.TestCase):
    def test_two_active_rows_for_the_same_person_metric_household_is_refused(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        compartir.compartir(conn, 1, "resting_hr")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO figura_compartida (person_id, metric_key, hogar_id, shared_at) "
                "VALUES (1, 'resting_hr', 'h-1', '2026-01-01T00:00:00+00:00')"
            )

    def test_a_withdrawn_and_an_active_row_coexist(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        compartir.compartir(conn, 1, "resting_hr")
        compartir.retirar(conn, 1, "resting_hr")
        # Does not raise: the withdrawn row is outside the partial unique index.
        conn.execute(
            "INSERT INTO figura_compartida (person_id, metric_key, hogar_id, shared_at) "
            "VALUES (1, 'resting_hr', 'h-1', '2026-01-01T00:00:00+00:00')"
        )
        self.assertEqual(len(_rows(conn, 1, "resting_hr")), 2)


class RefusalTest(unittest.TestCase):
    def test_refuses_with_no_active_household(self):
        conn = _reset_db(self)
        with self.assertRaises(ValueError):
            compartir.compartir(conn, 1, "resting_hr")
        self.assertEqual(_rows(conn, 1, "resting_hr"), [])

    def test_refuses_an_unknown_metric_key(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        with self.assertRaises(ValueError):
            compartir.compartir(conn, 1, "not_a_real_metric")
        self.assertEqual(_rows(conn, 1, "not_a_real_metric"), [])


class VisibilityTest(unittest.TestCase):
    """visibles_para(viewer): only an active share, of an active member of THAT SAME household."""

    def test_viewer_sees_the_other_members_active_share(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        compartir.compartir(conn, 2, "resting_hr")

        out = compartir.visibles_para(conn, 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Partner")
        self.assertEqual(out[0]["metric_key"], "resting_hr")

    def test_a_stranger_in_a_different_household_sees_nothing_of_it(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _add_person(conn, 3, "Stranger")
        _link_household(conn, 1, 2, hogar_id="h-1")
        _link_household(conn, 3, hogar_id="h-2")
        compartir.compartir(conn, 2, "resting_hr")

        self.assertEqual(compartir.visibles_para(conn, 3), [])

    def test_a_stranger_with_no_household_at_all_sees_nothing(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _add_person(conn, 3, "Stranger")
        _link_household(conn, 1, 2)
        compartir.compartir(conn, 2, "resting_hr")

        self.assertEqual(compartir.visibles_para(conn, 3), [])

    def test_a_member_who_left_the_household_is_no_longer_shown(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        compartir.compartir(conn, 2, "resting_hr")
        self.assertEqual(len(compartir.visibles_para(conn, 1)), 1)

        conn.execute("DELETE FROM hogar_miembro WHERE person_id = 2")
        conn.commit()
        self.assertEqual(compartir.visibles_para(conn, 1), [])
        # And the person who left sees nothing of their former household either.
        self.assertEqual(compartir.visibles_para(conn, 2), [])

    def test_the_owner_after_withdrawal_is_no_longer_shown(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        compartir.compartir(conn, 2, "resting_hr")
        self.assertEqual(len(compartir.visibles_para(conn, 1)), 1)

        compartir.retirar(conn, 2, "resting_hr")
        self.assertEqual(compartir.visibles_para(conn, 1), [])

    def test_viewer_with_no_household_sees_nothing(self):
        conn = _reset_db(self)
        self.assertEqual(compartir.visibles_para(conn, 1), [])


class LatestValueOnlyTest(unittest.TestCase):
    """Only the owner's latest daily_metric row for the key is ever exposed -- never an older one."""

    def test_only_the_latest_days_value_is_returned(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        db.set_metric(conn, "2026-11-01", "resting_hr", 70.0, "garmin", 2)
        db.set_metric(conn, "2026-11-05", "resting_hr", 60.0, "garmin", 2)
        db.set_metric(conn, "2026-11-10", "resting_hr", 55.0, "garmin", 2)
        conn.commit()
        compartir.compartir(conn, 2, "resting_hr")

        out = compartir.visibles_para(conn, 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["day"], "2026-11-10")
        self.assertEqual(out[0]["value"], 55.0)

    def test_hoy_never_renders_an_older_shared_value(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        for day, v in (("2026-11-01", 70.0), ("2026-11-05", 60.0), ("2026-11-12", 55.0)):
            db.set_metric(conn, day, "resting_hr", v, "garmin", 2)
        db.set_metric(conn, "2026-11-12", "resting_hr", 58.0, "garmin", 1)  # viewer needs a synced day too
        conn.commit()
        compartir.compartir(conn, 2, "resting_hr")

        _as(self, 1)
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Partner", r.text)
        self.assertIn("55 lpm", r.text)
        self.assertNotIn("70 lpm", r.text)
        self.assertNotIn("60 lpm", r.text)

    def test_hoy_shows_no_data_when_owner_has_none_yet(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        db.set_metric(conn, "2026-11-12", "resting_hr", 58.0, "garmin", 1)
        conn.commit()
        compartir.compartir(conn, 2, "resting_hr")

        _as(self, 1)
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Partner", r.text)
        self.assertIn("sin dato", r.text)

    def test_card_absent_when_nothing_is_shared(self):
        conn = _reset_db(self)
        db.set_metric(conn, "2026-11-12", "resting_hr", 58.0, "garmin", 1)
        conn.commit()

        _as(self, 1)
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Tu hogar", r.text)

    def test_card_renders_after_the_persons_own_content(self):
        """BODY-HOGAR-COMPARTIR-1-M fix 2: the «Tu hogar» card is the last card on Hoy, after the
        viewer's own readiness card and the «see the whole day» link, before the page footer."""
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        db.set_metric(conn, "2026-11-12", "resting_hr", 55.0, "garmin", 2)
        db.set_metric(conn, "2026-11-12", "resting_hr", 58.0, "garmin", 1)
        conn.commit()
        compartir.compartir(conn, 2, "resting_hr")

        _as(self, 1)
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        see_day_pos = r.text.index("Ver todo el día")
        hogar_pos = r.text.index("Tu hogar")
        footer_pos = r.text.index("No es consejo médico")
        self.assertLess(see_day_pos, hogar_pos)
        self.assertLess(hogar_pos, footer_pos)


class AjustesCardTest(unittest.TestCase):
    def test_no_household_shows_the_one_line_only(self):
        conn = _reset_db(self)
        _as(self, 1)
        r = client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Compartir con tu hogar", r.text)
        self.assertIn("Cuando tengas un hogar", r.text)

    def test_household_shows_every_metric_with_a_share_button_in_spanish(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        _as(self, 1)
        r = client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Compartir con tu hogar", r.text)
        self.assertIn("Pulso en reposo", r.text)
        self.assertIn("Compartir", r.text)
        self.assertIn("Solo ven el último valor", r.text)

    def test_household_shows_the_card_in_english(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner", lang="en")
        _link_household(conn, 1, 2)
        db.set_language(conn, "en", 1)
        _as(self, 1)
        r = client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Share with your household", r.text)
        self.assertIn("They only see the latest value", r.text)

    def test_share_button_posts_and_toggles_to_stop_sharing(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        _as(self, 1)

        r = client.post("/settings/compartir", data={"metric_key": "resting_hr", "accion": "compartir"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/settings")
        self.assertEqual(len(_rows(conn, 1, "resting_hr")), 1)

        r2 = client.get("/settings")
        self.assertIn("Dejar de compartir", r2.text)

        r3 = client.post("/settings/compartir", data={"metric_key": "resting_hr", "accion": "retirar"})
        self.assertEqual(r3.status_code, 303)
        rows = _rows(conn, 1, "resting_hr")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["withdrawn_at"])

    def test_sharing_with_no_household_through_the_route_changes_nothing(self):
        conn = _reset_db(self)
        _as(self, 1)
        r = client.post("/settings/compartir", data={"metric_key": "resting_hr", "accion": "compartir"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(_rows(conn, 1, "resting_hr"), [])

    def test_with_nothing_shared_the_summary_is_the_only_row_besides_the_privacy_line(self):
        """BODY-HOGAR-COMPARTIR-1-M fix 3: closed by default, no "Dejar de compartir" button yet."""
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        _as(self, 1)
        r = client.get("/settings")
        self.assertIn('<details class="files" style="margin-top:6px">', r.text)
        self.assertNotIn("Dejar de compartir", r.text)
        self.assertIn("Elegir qué compartir", r.text)

    def test_an_active_share_sits_outside_the_details_the_rest_inside(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        _as(self, 1)
        client.post("/settings/compartir", data={"metric_key": "resting_hr", "accion": "compartir"})

        r = client.get("/settings")
        details_pos = r.text.index("<details")
        retirar_pos = r.text.index("Dejar de compartir")
        otra_compartir_pos = r.text.index(">Compartir<")
        self.assertLess(retirar_pos, details_pos)
        self.assertGreater(otra_compartir_pos, details_pos)

    def test_summary_in_english(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner", lang="en")
        _link_household(conn, 1, 2)
        db.set_language(conn, "en", 1)
        _as(self, 1)
        r = client.get("/settings")
        self.assertIn("Choose what to share", r.text)


class ExportDeleteTest(unittest.TestCase):
    """figura_compartida is in FULLY_HANDLED_TABLES: a person's own export carries their shares,
    and delete-all removes them (body/privacidad.py)."""

    def test_export_and_delete_all_include_the_table(self):
        from body import privacidad

        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        compartir.compartir(conn, 1, "resting_hr")

        data = privacidad.export_person(conn, 1)
        self.assertEqual([r["metric_key"] for r in data["figuras_compartidas"]], ["resting_hr"])

        privacidad.delete_person(conn, 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM figura_compartida WHERE person_id = 1").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
