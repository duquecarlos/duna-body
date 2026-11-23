"""BODY-PARTIDO-2 (ORCH-CARLOS-DELEGATES-TIER-1-1): the post-session check-in -- its default
document (body/checkin_sesion.py), its three db.py tables, and its card on the per-session page.
Invented data only, no real health figure.

1. Ruling 2: set matching by type, by folded 'other' name (accents and case), and the generic
   fallback when neither matches.
2. Ruling 2/4: a page view writes nothing; the first save copies the matched default into the
   person's own rows, then stores answers; a re-save upserts one question without touching
   siblings; options are stored by their stable key, never display text.
3. Ruling 1: the session key is (person_id, sets_source, sets_source_id) -- a linked pair shares
   one check-in; another person never sees or writes it.
4. Ruling 3/4: the form renders in es and en with every approved string, and keeps a 375px
   structure (pills wrap, no fixed widths).
5. Ruling 5: `_leer_entreno` carries `checkin` keys only, never display text.
6. Ruling 1/8: the additive migration on a database without the tables.
7. Ruling 6: privacy export/delete and mudanza coverage (also exercised end to end in
   tests/test_privacidad.py, tests/test_mudanza.py and tests/test_persona_fuga.py).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import checkin_sesion, config, db, garmin_adapter, identity, web
from body.agente import herramientas

ROOT = Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / "body" / "i18n"

client = TestClient(web.app, follow_redirects=False)


def _load_lang(lang: str) -> dict:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def _es(key: str) -> str:
    return _load_lang("es")[key]


def _en(key: str) -> str:
    return _load_lang("en")[key]


def _as(tc: unittest.TestCase, person_id: int) -> None:
    tc.addCleanup(web.app.dependency_overrides.clear)
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _reset_db(tc: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    db.local_person_id(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _activity(conn, person_id: int, source_id: str, day: str, start_local: str, type_: str | None,
              name: str | None, duration_s: float, source: str = "garmin") -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
        (person_id, source_id, day, start_local, type_, name, duration_s, source),
    )
    conn.commit()


# ---- checkin_sesion.set_para: matching is data, never a branch on one sport ----------------------

class SetParaTest(unittest.TestCase):
    def test_basketball_by_type_code(self):
        preguntas = checkin_sesion.set_para("basketball", "whatever")
        self.assertEqual([p["clave"] for p in preguntas], ["formato", "sensacion", "calentamiento", "comida"])

    def test_basketball_by_folded_other_name_accents_and_case(self):
        preguntas = checkin_sesion.set_para("other", "BALONCÉSTO")
        self.assertEqual([p["clave"] for p in preguntas], ["formato", "sensacion", "calentamiento", "comida"])

    def test_basketball_by_folded_other_name_basquetbol(self):
        preguntas = checkin_sesion.set_para("other", "Basquetbol")
        self.assertEqual(preguntas[0]["clave"], "formato")

    def test_running_type_codes(self):
        for tipo in ("running", "trail_running", "treadmill_running", "track_running"):
            with self.subTest(tipo=tipo):
                preguntas = checkin_sesion.set_para(tipo, None)
                self.assertEqual([p["clave"] for p in preguntas],
                                  ["terreno", "sensacion", "calentamiento", "comida"])

    def test_strength_training_type_code(self):
        preguntas = checkin_sesion.set_para("strength_training", None)
        self.assertEqual([p["clave"] for p in preguntas], ["enfoque", "sensacion", "calentamiento"])

    def test_unmatched_other_name_falls_back_to_generic(self):
        preguntas = checkin_sesion.set_para("other", "Yoga")
        self.assertEqual([p["clave"] for p in preguntas], list(p["clave"] for p in checkin_sesion.GENERICO))

    def test_unknown_type_falls_back_to_generic(self):
        preguntas = checkin_sesion.set_para("cycling", None)
        self.assertEqual([p["clave"] for p in preguntas], list(p["clave"] for p in checkin_sesion.GENERICO))

    def test_none_type_and_name_falls_back_to_generic(self):
        preguntas = checkin_sesion.set_para(None, None)
        self.assertEqual([p["clave"] for p in preguntas], list(p["clave"] for p in checkin_sesion.GENERICO))

    def test_options_are_stable_keys_never_display_text(self):
        preguntas = checkin_sesion.set_para("basketball", None)
        formato = preguntas[0]
        self.assertEqual([o["clave"] for o in formato["opciones"]],
                          ["uno_uno", "media_cancha", "cancha_completa", "otro"])
        for o in formato["opciones"]:
            self.assertTrue(o["texto"].startswith("checkin_sesion."))


# ---- db.py: set_de_persona / guardar_set_pregunta / registrar_respuestas_sesion / respuestas_de_sesion

class DbFunctionsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "db.db")

    def test_no_stored_set_is_none(self):
        self.assertIsNone(db.set_de_persona(self.conn, 1, "other", "zz-baloncesto"))

    def test_guardar_set_pregunta_then_set_de_persona_round_trips(self):
        preguntas = checkin_sesion.set_para("basketball", None)
        db.guardar_set_pregunta(self.conn, 1, "other", "zz-baloncesto", preguntas)
        fila = db.set_de_persona(self.conn, 1, "other", "zz-baloncesto")
        self.assertEqual([p["clave"] for p in fila], [p["clave"] for p in preguntas])
        self.assertEqual(fila[0]["origen"], "defecto")
        self.assertEqual(fila[0]["opciones"][0]["clave"], "uno_uno")
        # sensacion carries no opciones (escala's own ends are fixed UI strings, not stored data).
        sensacion = next(p for p in fila if p["clave"] == "sensacion")
        self.assertIsNone(sensacion["opciones"])

    def test_registrar_respuestas_then_respuestas_de_sesion_round_trips(self):
        db.registrar_respuestas_sesion(self.conn, 1, "garmin", "s1", {"sensacion": "4", "calentamiento": "si"})
        self.assertEqual(db.respuestas_de_sesion(self.conn, 1, "garmin", "s1"),
                          {"sensacion": "4", "calentamiento": "si"})

    def test_resave_upserts_one_question_without_touching_siblings(self):
        db.registrar_respuestas_sesion(self.conn, 1, "garmin", "s1", {"sensacion": "4", "calentamiento": "si"})
        db.registrar_respuestas_sesion(self.conn, 1, "garmin", "s1", {"sensacion": "2"})
        self.assertEqual(db.respuestas_de_sesion(self.conn, 1, "garmin", "s1"),
                          {"sensacion": "2", "calentamiento": "si"})
        n = self.conn.execute(
            "SELECT COUNT(*) FROM set_pregunta_respuesta WHERE person_id = 1 AND sets_source_id = 's1'"
        ).fetchone()[0]
        self.assertEqual(n, 2)

    def test_another_person_never_sees_it(self):
        db.registrar_respuestas_sesion(self.conn, 1, "garmin", "s1", {"sensacion": "4"})
        db.guardar_set_pregunta(self.conn, 1, "other", "zz-baloncesto", checkin_sesion.set_para("basketball", None))
        self.assertEqual(db.respuestas_de_sesion(self.conn, 2, "garmin", "s1"), {})
        self.assertIsNone(db.set_de_persona(self.conn, 2, "other", "zz-baloncesto"))


# ---- route: GET renders, writes nothing; POST saves, upserts, 404s on another person's session ---

class CardRouteTest(unittest.TestCase):
    DAY = "2026-11-20"

    def setUp(self):
        self.conn = _reset_db(self, "card.db")
        _as(self, 1)
        _activity(self.conn, 1, "b1", self.DAY, f"{self.DAY}T18:00:00", "other", "Baloncesto", 3000.0)

    def test_get_renders_the_blank_form_and_writes_nothing(self):
        r = client.get(f"/day/{self.DAY}/entreno/b1")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_es("checkin_sesion.titulo"), r.text)
        self.assertIn(_es("checkin_sesion.pregunta.formato"), r.text)
        self.assertIn('name="sensacion"', r.text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM set_pregunta").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM set_pregunta_respuesta").fetchone()[0], 0)

    def test_post_copies_the_default_then_stores_answers(self):
        r = client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={
            "formato": "media_cancha", "sensacion": "4", "calentamiento": "si", "comida": "1_3h",
        })
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].endswith("#checkin"))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM set_pregunta WHERE person_id = 1").fetchone()[0], 1)
        self.assertEqual(db.respuestas_de_sesion(self.conn, 1, "garmin", "b1"),
                          {"formato": "media_cancha", "sensacion": "4", "calentamiento": "si",
                           "comida": "1_3h"})
        r2 = client.get(r.headers["location"])
        self.assertIn(_es("checkin_sesion.opcion.formato.media_cancha"), r2.text)
        self.assertIn(_es("checkin_sesion.cambiar"), r2.text)

    def test_partial_answer_leaves_the_rest_unanswered(self):
        client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"sensacion": "4"})
        self.assertEqual(db.respuestas_de_sesion(self.conn, 1, "garmin", "b1"), {"sensacion": "4"})

    def test_resave_upserts_without_disturbing_siblings(self):
        client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"sensacion": "4", "calentamiento": "si"})
        client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"sensacion": "2"})
        self.assertEqual(db.respuestas_de_sesion(self.conn, 1, "garmin", "b1"),
                          {"sensacion": "2", "calentamiento": "si"})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM set_pregunta WHERE person_id = 1").fetchone()[0], 1)

    def test_invalid_option_value_is_rejected_as_no_answer_never_a_500(self):
        r = client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"formato": "no-such-option"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.respuestas_de_sesion(self.conn, 1, "garmin", "b1"), {})

    def test_answered_view_shows_plain_text_with_a_change_link(self):
        client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"sensacion": "4"})
        r = client.get(f"/day/{self.DAY}/entreno/b1")
        self.assertNotIn('name="sensacion"', r.text)
        self.assertIn(f'<br>4</li>', r.text)  # 4 shows the bare number, not a label (ruling 3)
        self.assertIn(_es("checkin_sesion.cambiar"), r.text)

    def test_answered_scale_end_shows_its_own_label(self):
        client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"sensacion": "5"})
        r = client.get(f"/day/{self.DAY}/entreno/b1")
        self.assertIn(_es("checkin_sesion.escala.5"), r.text)

    def test_checkin_editar_query_param_reopens_the_pills_preselected(self):
        client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"sensacion": "4"})
        r = client.get(f"/day/{self.DAY}/entreno/b1?checkin_editar=1")
        self.assertIn('name="sensacion"', r.text)
        self.assertRegex(r.text, r'value="4"\s+checked')

    def test_editing_again_prefills_the_saved_option(self):
        client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"formato": "media_cancha"})
        r = client.get(f"/day/{self.DAY}/entreno/b1?checkin_editar=1")
        self.assertRegex(r.text, r'value="media_cancha"\s+checked')

    def test_another_persons_session_404s_on_get_and_post(self):
        _as(self, 2)
        self.assertEqual(client.get(f"/day/{self.DAY}/entreno/b1").status_code, 404)
        self.assertEqual(
            client.post(f"/day/{self.DAY}/entreno/b1/checkin", data={"sensacion": "4"}).status_code, 404)

    def test_renders_in_english(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = 1")
        self.conn.commit()
        r = client.get(f"/day/{self.DAY}/entreno/b1")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_en("checkin_sesion.titulo"), r.text)
        self.assertIn(_en("checkin_sesion.pregunta.formato"), r.text)
        self.assertIn(_en("checkin_sesion.opcion.formato.media_cancha"), r.text)
        self.assertIn(_en("checkin_sesion.pregunta.sensacion"), r.text)
        self.assertIn(_en("checkin_sesion.escala.1"), r.text)
        self.assertIn(_en("checkin_sesion.escala.3"), r.text)
        self.assertIn(_en("checkin_sesion.escala.5"), r.text)
        self.assertIn(_en("checkin_sesion.pregunta.calentamiento"), r.text)
        self.assertIn(_en("checkin_sesion.si"), r.text)
        self.assertIn(_en("checkin_sesion.no"), r.text)
        self.assertIn(_en("checkin_sesion.pregunta.comida"), r.text)
        self.assertIn(_en("checkin_sesion.opcion.comida.menos_1h"), r.text)
        self.assertIn(_en("checkin_sesion.opcion.comida.1_3h"), r.text)
        self.assertIn(_en("checkin_sesion.opcion.comida.mas_3h"), r.text)
        self.assertIn(_en("checkin_sesion.guardar"), r.text)
        self.assertIn(_en("checkin_sesion.nota"), r.text)

    def test_pill_markup_wraps_and_carries_no_fixed_width(self):
        """Ruling 4: 375px structure -- the pill row wraps (flex-wrap) and neither the row nor a
        single pill is given a fixed width in the template/CSS (min-height/min-width only, the
        cuerpo-vista-btn precedent)."""
        r = client.get(f"/day/{self.DAY}/entreno/b1")
        self.assertIn('class="pregunta-pills', r.text)
        css = (ROOT / "body" / "static" / "body.css").read_text(encoding="utf-8")
        self.assertIn(".pregunta-pills{display:flex;gap:6px;flex-wrap:wrap}", css)
        self.assertNotRegex(css.split(".pregunta-pill{")[1].split("}")[0], r"(?<!min-)width:")


class GenericFallbackRouteTest(unittest.TestCase):
    """Ruling 2: a session matching no specific set shows the generic three questions."""

    DAY = "2026-11-21"

    def setUp(self):
        self.conn = _reset_db(self, "generic.db")
        _as(self, 1)
        _activity(self.conn, 1, "y1", self.DAY, f"{self.DAY}T18:00:00", "yoga", "Yoga", 1800.0)

    def test_generic_set_shows_on_an_unmatched_session(self):
        r = client.get(f"/day/{self.DAY}/entreno/y1")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_es("checkin_sesion.pregunta.sensacion"), r.text)
        self.assertIn(_es("checkin_sesion.pregunta.calentamiento"), r.text)
        self.assertIn(_es("checkin_sesion.pregunta.comida"), r.text)
        self.assertNotIn(_es("checkin_sesion.pregunta.formato"), r.text)


class RunningMatchRouteTest(unittest.TestCase):
    DAY = "2026-11-22"

    def setUp(self):
        self.conn = _reset_db(self, "running.db")
        _as(self, 1)
        _activity(self.conn, 1, "r1", self.DAY, f"{self.DAY}T06:00:00", "trail_running", None, 1800.0)

    def test_running_type_code_shows_terreno(self):
        r = client.get(f"/day/{self.DAY}/entreno/r1")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_es("checkin_sesion.pregunta.terreno"), r.text)


class StrengthMatchRouteTest(unittest.TestCase):
    DAY = "2026-11-23"

    def setUp(self):
        self.conn = _reset_db(self, "strength.db")
        _as(self, 1)
        _activity(self.conn, 1, "s1", self.DAY, f"{self.DAY}T18:00:00", "strength_training", None, 600.0)
        garmin_payload_sets = [garmin_adapter.ExerciseSet(
            set_type="active", category="pecho", exercise="press banca", reps=8, weight_value=40.0,
            weight_unit="kg", duration_s=40.0, start_local=f"{self.DAY}T18:00:00")]
        db.save_exercise_sets(self.conn, 1, "garmin", "s1", garmin_payload_sets)

    def test_strength_type_code_shows_enfoque_and_no_comida(self):
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        self.assertEqual(r.status_code, 200)
        self.assertIn(_es("checkin_sesion.pregunta.enfoque"), r.text)
        self.assertNotIn(_es("checkin_sesion.pregunta.comida"), r.text)

    def test_checkin_card_appears_after_the_sets_and_before_the_pain_card(self):
        self.conn.execute(
            "INSERT INTO period (person_id, kind, start_day, created_at, updated_at, zonas) "
            "VALUES (1, 'lesion', ?, ?, ?, '[\"pecho\"]')", (self.DAY, self.DAY, self.DAY))
        self.conn.commit()
        r = client.get(f"/day/{self.DAY}/entreno/s1")
        pos_sets = r.text.index(_es("entreno_sesion.sets_titulo"))
        pos_checkin = r.text.index(_es("checkin_sesion.titulo"))
        pos_dolor = r.text.index('id="dolor"')
        self.assertLess(pos_sets, pos_checkin)
        self.assertLess(pos_checkin, pos_dolor)


class LinkedPairSharesOneCheckinTest(unittest.TestCase):
    """Ruling 1: a linked watch/manual pair shares one check-in -- the merged session's own
    sets_source/sets_source_id is the manual half's, exactly like entreno_dolor. Linking is
    automatic only for a strength_training watch activity (db.save_day), so this fixture follows
    tests/test_entreno_vinculo.py's own precedent exactly."""

    DAY = "2026-11-24"

    def setUp(self):
        self.conn = _reset_db(self, "linked.db")
        _as(self, 1)
        self.manual_id = db.create_manual_session(self.conn, 1, self.DAY, None, "Fuerza de la tarde")
        db.save_day(self.conn, garmin_adapter.DayData(
            day=self.DAY,
            activities=[{"source_id": "w1", "start_local": f"{self.DAY}T18:00:00",
                         "type": "strength_training", "name": None, "duration_s": 2100.0,
                         "distance_m": None, "avg_hr": 130.0, "max_hr": 150.0, "calories": 300.0}],
        ), 1)
        self.conn.commit()

    def test_linking_happened(self):
        row = self.conn.execute(
            "SELECT vinculo_manual FROM activity WHERE person_id = 1 AND source_id = 'w1'").fetchone()
        self.assertEqual(row["vinculo_manual"], self.manual_id)

    def test_answer_saved_under_the_merged_session_reads_back_on_the_merged_page(self):
        r = client.post(f"/day/{self.DAY}/entreno/w1/checkin", data={"sensacion": "4"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.respuestas_de_sesion(self.conn, 1, "manual", self.manual_id), {"sensacion": "4"})
        r2 = client.get(f"/day/{self.DAY}/entreno/w1")
        self.assertIn(_es("checkin_sesion.cambiar"), r2.text)


# ---- the assistant's own leer_entreno: checkin carries stable keys only ---------------------------

class LeerEntrenoCheckinTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self, "asistente.db")

    def test_unanswered_session_carries_an_empty_dict(self):
        _activity(self.conn, 1, "b1", "2026-11-25", "2026-11-25T18:00:00", "other", "Baloncesto", 3000.0)
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-25"}, "completo")
        self.assertEqual(out["sesiones"][0]["checkin"], {})

    def test_answered_session_carries_claves_and_stable_values_only(self):
        _activity(self.conn, 1, "b1", "2026-11-26", "2026-11-26T18:00:00", "other", "Baloncesto", 3000.0)
        db.registrar_respuestas_sesion(self.conn, 1, "garmin", "b1",
                                        {"formato": "media_cancha", "sensacion": "4"})
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-26"}, "completo")
        self.assertEqual(out["sesiones"][0]["checkin"], {"formato": "media_cancha", "sensacion": "4"})

    def test_another_persons_answer_never_appears(self):
        _activity(self.conn, 2, "b2", "2026-11-27", "2026-11-27T18:00:00", "other", "Baloncesto", 3000.0)
        db.registrar_respuestas_sesion(self.conn, 2, "garmin", "b2", {"sensacion": "1"})
        _activity(self.conn, 1, "b1", "2026-11-27", "2026-11-27T18:00:00", "other", "Baloncesto", 3000.0)
        out = herramientas.ejecutar(self.conn, 1, "leer_entreno", {"day": "2026-11-27"}, "completo")
        self.assertEqual(out["sesiones"][0]["checkin"], {})


if __name__ == "__main__":
    unittest.main()
