"""BODY-HOGAR-MIEMBRO-1: a household goal reaches every member of its household, not only its
creator -- orchestrator ledger BODY-HOGAR-PLAN-1 rulings 1-2 against BODY-HOGAR-OBS-1. Each class
below covers one numbered rule from that slice's spec."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, household, identity, web

client = TestClient(web.app, follow_redirects=False)

_TODAY = date(2026, 11, 12)
_MONDAY = _TODAY - timedelta(_TODAY.weekday())
_WEEK_DAYS = [(_MONDAY + timedelta(i)).isoformat() for i in range(7)]


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "hm.db"
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


def _rename(conn, pid: int, name: str):
    conn.execute("UPDATE person SET display_name = ? WHERE id = ?", (name, pid))
    conn.commit()


def _link_household(conn, *person_ids, hogar_id="h-1"):
    conn.executescript(household.SCHEMA)
    conn.executemany("INSERT INTO hogar_local (person_id, hogar_id) VALUES (?, ?)",
                      [(pid, hogar_id) for pid in person_ids])
    conn.commit()


def _shared_milestone(conn, creator_id: int, title="Preparar el ascenso", hogar_id="h-1"):
    gid = goals.propose(conn, creator_id, title, _WEEK_DAYS[0], kind="milestone", deadline="2027-06-01",
                        hogar_id=hogar_id, visibility="household")
    goals.activate(conn, gid)
    return gid


class VisibilityTest(unittest.TestCase):
    """Rule 1: a member sees G in Metas (by status) and on its page; an outsider or a person who
    left the household gets 404 exactly as today."""

    def test_member_sees_shared_goal_in_each_status_section(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        _shared_milestone(conn, 1, "Meta activa")
        goals.propose(conn, 1, "Meta propuesta", _WEEK_DAYS[0], kind="milestone", deadline="2027-06-01",
                      hogar_id="h-1", visibility="household")
        gid_archived = _shared_milestone(conn, 1, "Meta archivada")
        goals.pause(conn, gid_archived)

        _as(self, 2)
        r = client.get("/goals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Meta activa", r.text)
        self.assertIn("Meta propuesta", r.text)
        self.assertIn("Meta archivada", r.text)

    def test_member_can_open_the_goal_page(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 2)
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)

    def test_stranger_gets_404(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _add_person(conn, 3, "Stranger")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 3)
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 404)

    def test_person_who_left_gets_404(self):
        conn = _reset_db(self)
        _add_person(conn, 4, "Left")
        _link_household(conn, 1, 4)
        gid = _shared_milestone(conn, 1)
        conn.execute("DELETE FROM hogar_local WHERE person_id = 4")
        conn.commit()

        _as(self, 4)
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 404)


class SharedLineTest(unittest.TestCase):
    """Rule 1's banner, both languages."""

    def test_creator_sees_the_compact_line_in_spanish(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 1)
        r = client.get(f"/goals/{gid}")
        self.assertIn("Compartida con tu hogar", r.text)
        self.assertNotIn("la creó", r.text)

    def test_member_sees_who_created_it_in_spanish(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _rename(conn, 1, "Owner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 2)
        r = client.get(f"/goals/{gid}")
        self.assertIn("Compartida con tu hogar", r.text)
        self.assertIn("la creó Owner", r.text)

    def test_member_sees_who_created_it_in_english(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _rename(conn, 1, "Owner")
        _link_household(conn, 1, 2)
        db.set_language(conn, "en", 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 2)
        r = client.get(f"/goals/{gid}")
        self.assertIn("Shared with your household", r.text)
        self.assertIn("created by Owner", r.text)


class HabitCheckTest(unittest.TestCase):
    """Rule 2: a member checks off a habit whose assignee is NULL (both) or themselves; a habit
    assigned to someone else is theirs to check, not this viewer's."""

    def test_member_checks_shared_and_own_habit_not_others(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)
        hid_both = goals.add_habit(conn, gid, "Entrenar piernas", {"per": "week", "times": 2})
        hid_creator = goals.add_habit(conn, gid, "Revisar el equipo", {"per": "week", "times": 1},
                                      assignee_person_id=1)
        hid_partner = goals.add_habit(conn, gid, "Comprar botas", {"per": "week", "times": 1},
                                      assignee_person_id=2)

        _as(self, 2)
        day = _WEEK_DAYS[1]
        self.assertEqual(client.post(f"/habits/{hid_both}/check",
                                     data={"day": day, "done": "1", "back": "/"}).status_code, 303)
        self.assertEqual(client.post(f"/habits/{hid_partner}/check",
                                     data={"day": day, "done": "1", "back": "/"}).status_code, 303)
        self.assertEqual(client.post(f"/habits/{hid_creator}/check",
                                     data={"day": day, "done": "1", "back": "/"}).status_code, 404)

        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM habit_check WHERE habit_id = ? AND person_id = 2 AND day = ?",
            (hid_both, day)).fetchone())
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM habit_check WHERE habit_id = ? AND person_id = 2 AND day = ?",
            (hid_partner, day)).fetchone())

    def test_stranger_cannot_check_a_shared_habit(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _add_person(conn, 3, "Stranger")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)
        hid_both = goals.add_habit(conn, gid, "Entrenar piernas", {"per": "week", "times": 2})

        _as(self, 3)
        r = client.post(f"/habits/{hid_both}/check",
                        data={"day": _WEEK_DAYS[1], "done": "1", "back": "/"})
        self.assertEqual(r.status_code, 404)

    def test_hoy_lists_due_habits_for_member_not_others(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)
        goals.add_habit(conn, gid, "Entrenar piernas", {"per": "week", "times": 2})
        goals.add_habit(conn, gid, "Revisar el equipo", {"per": "week", "times": 1}, assignee_person_id=1)
        db.set_metric(conn, _WEEK_DAYS[0], "resting_hr", 55, "garmin", 2)  # Hoy needs a synced day to show the plan
        conn.commit()

        _as(self, 2)
        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Entrenar piernas", r.text)
        self.assertNotIn("Revisar el equipo", r.text)


class OwnWeeklyCheckTest(unittest.TestCase):
    """Rule 3: the viewer's own history/chart come from their own data; the members block lists
    every OTHER member (the creator included) by verdict word only."""

    def test_members_block_lists_the_creator_by_verdict_word(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _rename(conn, 1, "Owner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)
        goals.weekly_check(conn, gid, 1, _MONDAY.isoformat())

        _as(self, 2)
        r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Owner", r.text)

    def test_viewer_sees_their_own_history_not_the_creators(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = goals.propose(conn, 1, "Caminar juntos", _WEEK_DAYS[0], metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2027-01-01",
                            hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        for day in _WEEK_DAYS[:4]:
            db.set_metric(conn, day, "steps", 7000.0, "garmin", 1)
            db.set_metric(conn, day, "steps", 11000.0, "garmin", 2)
        conn.commit()

        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            _as(self, 2)
            r = client.get(f"/goals/{gid}")
        self.assertEqual(r.status_code, 200)
        # the viewer's own weekly average (11000) was upserted; the creator's (7000) stays theirs
        own_check = conn.execute(
            "SELECT metric_avg FROM goal_check WHERE goal_id = ? AND person_id = 2", (gid,)).fetchone()
        self.assertEqual(own_check["metric_avg"], 11000.0)
        self.assertNotIn("7000", r.text)


class AddHabitChoiceTest(unittest.TestCase):
    """Rule 4: for a household goal, the adder (member or creator) picks "for me" or "for both";
    a private goal shows no such choice."""

    def test_household_goal_offers_the_choice(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 1)
        r = client.get(f"/goals/{gid}")
        self.assertIn('name="for" value="both"', r.text)
        self.assertIn('name="for" value="me"', r.text)

    def test_private_goal_has_no_for_choice(self):
        conn = _reset_db(self)
        gid = goals.propose(conn, 1, "Meta privada", _WEEK_DAYS[0], kind="milestone", deadline="2027-06-01")
        goals.activate(conn, gid)

        _as(self, 1)
        r = client.get(f"/goals/{gid}")
        self.assertNotIn('name="for"', r.text)

    def test_member_adds_a_habit_for_self(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 2)
        r = client.post(f"/goals/{gid}/habits",
                        data={"title": "Comprar mapa", "per": "week", "times": "1", "for": "me"})
        self.assertEqual(r.status_code, 303)
        row = conn.execute("SELECT assignee_person_id FROM habit WHERE goal_id = ? AND title = ?",
                           (gid, "Comprar mapa")).fetchone()
        self.assertEqual(row["assignee_person_id"], 2)

    def test_for_both_leaves_the_habit_unassigned(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 2)
        client.post(f"/goals/{gid}/habits", data={"title": "Entrenar", "per": "week", "times": "1", "for": "both"})
        row = conn.execute("SELECT assignee_person_id FROM habit WHERE goal_id = ? AND title = ?",
                           (gid, "Entrenar")).fetchone()
        self.assertIsNone(row["assignee_person_id"])

    def test_creator_gets_the_same_choice(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 1)
        client.post(f"/goals/{gid}/habits",
                    data={"title": "Mapa del creador", "per": "week", "times": "1", "for": "me"})
        row = conn.execute("SELECT assignee_person_id FROM habit WHERE goal_id = ? AND title = ?",
                           (gid, "Mapa del creador")).fetchone()
        self.assertEqual(row["assignee_person_id"], 1)


class CreatorOnlyTest(unittest.TestCase):
    """Rule 5: editing G and activate/pause/finish/drop stay with the creator; a member gets 404
    and never sees the buttons."""

    def test_member_cannot_activate_pause_finish_drop(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid_proposed = goals.propose(conn, 1, "Meta propuesta", _WEEK_DAYS[0], kind="milestone",
                                     deadline="2027-06-01", hogar_id="h-1", visibility="household")
        gid_active = _shared_milestone(conn, 1, "Meta activa")

        _as(self, 2)
        self.assertEqual(client.post(f"/goals/{gid_proposed}/activate").status_code, 404)
        self.assertEqual(client.post(f"/goals/{gid_active}/pause").status_code, 404)
        self.assertEqual(client.post(f"/goals/{gid_active}/finish").status_code, 404)
        self.assertEqual(client.post(f"/goals/{gid_active}/drop").status_code, 404)

    def test_buttons_shown_to_creator_hidden_from_member(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        _as(self, 1)
        r = client.get(f"/goals/{gid}")
        self.assertIn("Pausar", r.text)

        _as(self, 2)
        r = client.get(f"/goals/{gid}")
        self.assertNotIn("Pausar", r.text)


class PrivacyTest(unittest.TestCase):
    """Rule 6: on a shared goal no viewer ever sees another member's figures -- no metric average,
    no chart series, no habit-check count."""

    def test_other_members_figures_are_absent(self):
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = goals.propose(conn, 1, "Bajar juntos el pulso", _WEEK_DAYS[0], metric_key="resting_hr",
                            direction="lower", target_value=45.0, baseline_value=60.0, deadline="2027-01-01",
                            hogar_id="h-1", visibility="household")
        goals.activate(conn, gid)
        for day in _WEEK_DAYS[:4]:
            db.set_metric(conn, day, "resting_hr", 51.0, "garmin", 1)
            db.set_metric(conn, day, "resting_hr", 93.0, "garmin", 2)
        hid_partner = goals.add_habit(conn, gid, "Estiramientos", {"per": "week", "times": 1},
                                      assignee_person_id=2)
        conn.execute("INSERT INTO habit_check (habit_id, person_id, day, done) VALUES (?, 2, ?, 1)",
                     (hid_partner, _WEEK_DAYS[1]))
        conn.commit()

        with mock.patch.object(web, "_today_local", return_value=_TODAY):
            _as(self, 1)
            r_owner = client.get(f"/goals/{gid}")
            _as(self, 2)
            r_partner = client.get(f"/goals/{gid}")

        self.assertEqual(r_owner.status_code, 200)
        self.assertEqual(r_partner.status_code, 200)
        self.assertNotIn("93", r_owner.text)    # the creator never sees Partner's average
        self.assertNotIn("51", r_partner.text)  # Partner never sees the creator's average
        self.assertNotIn("1 de 7", r_owner.text)  # nor Partner's habit-check count


class AssistantReadToolTest(unittest.TestCase):
    """Rule 7: leer_metas/leer_meta read household goals under the same access rule, the viewer's
    own numbers only."""

    def test_leer_metas_lists_a_shared_goal_for_a_member(self):
        from body.agente import herramientas
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1, "Meta compartida")

        out = herramientas.ejecutar(conn, 2, "leer_metas", {}, "resumen")
        self.assertIn(gid, [m["id"] for m in out["metas"]])
        shared = next(m for m in out["metas"] if m["id"] == gid)
        self.assertTrue(shared["compartida"])

    def test_leer_meta_readable_by_member_not_by_stranger(self):
        from body.agente import herramientas
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _add_person(conn, 3, "Stranger")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)

        out = herramientas.ejecutar(conn, 2, "leer_meta", {"goal_id": gid}, "resumen")
        self.assertTrue(out["encontrada"])
        out_stranger = herramientas.ejecutar(conn, 3, "leer_meta", {"goal_id": gid}, "resumen")
        self.assertFalse(out_stranger["encontrada"])

    def test_leer_meta_verificaciones_are_the_viewers_own(self):
        from body.agente import herramientas
        conn = _reset_db(self)
        _add_person(conn, 2, "Partner")
        _link_household(conn, 1, 2)
        gid = _shared_milestone(conn, 1)
        goals.weekly_check(conn, gid, 1, _MONDAY.isoformat())

        out = herramientas.ejecutar(conn, 2, "leer_meta", {"goal_id": gid}, "resumen")
        self.assertEqual(out["verificaciones"], "sin verificación aún")


if __name__ == "__main__":
    unittest.main()
