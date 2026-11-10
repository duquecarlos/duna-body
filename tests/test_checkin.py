"""BODY-CHECKIN-1: a 10-second self-report -- mood, energy, stress_felt, each 1-5 -- plus an
optional one-line note, set against the watch's own stress/HRV/Body Battery on Día. The note is
private by default (ruling 4): never read by any assistant tool, in-app or MCP. Invented demo
data only, no real health figures.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, privacidad
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun, hoy

DAY = "2026-11-05"


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


# ---- schema -------------------------------------------------------------------------------------

class FreshDbTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_checkin_table_exists_with_the_contract_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(checkin)")}
        expected = {"id", "person_id", "day", "ts_local", "momento", "mood", "energy", "stress_felt",
                    "created_at"}
        self.assertEqual(cols, expected)

    def test_checkin_nota_table_exists_with_the_ruling_2_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(checkin_nota)")}
        expected = {"id", "person_id", "day", "ts_local", "momento", "nota", "created_at"}
        self.assertEqual(cols, expected)

    def test_the_three_rows_are_seeded_self_not_summed(self):
        for key in ("mood", "energy", "stress_felt"):
            with self.subTest(key=key):
                row = self.conn.execute(
                    "SELECT origen, agregado, unit, better FROM metric_def WHERE key = ?", (key,)
                ).fetchone()
                self.assertIsNotNone(row, f"{key} missing from metric_def")
                self.assertEqual(row["origen"], "self")
                self.assertIsNone(row["agregado"])
                self.assertEqual(row["unit"], "of5")
                self.assertEqual(row["better"], "neutral")

    def test_checkin_metric_keys_is_exactly_the_three(self):
        self.assertEqual(db.CHECKIN_METRIC_KEYS, ("mood", "energy", "stress_felt"))

    def test_checkin_keys_excluded_from_self_metric_keys(self):
        for key in db.CHECKIN_METRIC_KEYS:
            self.assertNotIn(key, herramientas.SELF_METRIC_KEYS)

    def test_propuesta_agente_accion_accepts_registrar_checkin(self):
        cid = _cid(self.conn)
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, "
            "creada_en, expira_en) VALUES (1, ?, 'registrar_checkin', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (cid,),
        )
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE accion = 'registrar_checkin'"
        ).fetchone()[0]
        self.assertEqual(count, 1)


class OldDbMigrationTest(unittest.TestCase):
    """A database built before `checkin`/`checkin_nota` existed, and before propuesta_agente's own
    `accion` CHECK knew 'registrar_checkin', gains both -- without disturbing an existing row."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "vieja.db")
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TABLE checkin")
        self.conn.execute("DROP TABLE checkin_nota")
        self.conn.execute(
            "CREATE TABLE propuesta_agente_vieja ("
            "id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, conversacion_id INTEGER NOT NULL, "
            "accion TEXT NOT NULL CHECK (accion IN ('registrar_comida')), objetivo_id INTEGER, "
            "antes_json TEXT, despues_json TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'propuesta', "
            "creada_en TEXT NOT NULL, expira_en TEXT NOT NULL, resuelta_en TEXT)"
        )
        self.conn.execute("DROP TABLE propuesta_agente")
        self.conn.execute("ALTER TABLE propuesta_agente_vieja RENAME TO propuesta_agente")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (1, 'Me')")
        cid = agente_conversacion.crear(self.conn, 1)
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'registrar_comida', '{}', '2026-11-01T00:00:00+00:00', "
            "'2026-11-01T00:10:00+00:00')", (cid,),
        )
        self.conn.commit()

    def test_migration_adds_checkin_tables_and_widens_the_check_keeping_the_old_row(self):
        db.init(self.conn)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("checkin", tables)
        self.assertIn("checkin_nota", tables)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE accion = 'registrar_comida'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        cid = agente_conversacion.crear(self.conn, 1)
        self.conn.execute(
            "INSERT INTO propuesta_agente (person_id, conversacion_id, accion, despues_json, creada_en, "
            "expira_en) VALUES (1, ?, 'registrar_checkin', '{}', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:10:00+00:00')", (cid,),
        )
        self.conn.commit()

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM propuesta_agente WHERE accion = 'registrar_comida'"
        ).fetchone()[0]
        self.assertEqual(count, 1)


# ---- db.crear_checkin / borrar_checkin: one transaction, latest-of-day wins --------------------

class CrearCheckinTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)

    def test_creates_the_checkin_row(self):
        cid = db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4, energy=3, stress_felt=2)
        row = self.conn.execute(
            "SELECT day, momento, mood, energy, stress_felt FROM checkin WHERE id = ?", (cid,)
        ).fetchone()
        self.assertEqual((row["day"], row["momento"], row["mood"], row["energy"], row["stress_felt"]),
                          (DAY, "despertar", 4, 3, 2))

    def test_writes_each_given_value_into_daily_metric(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4, energy=3, stress_felt=2)
        for key, expected in (("mood", 4), ("energy", 3), ("stress_felt", 2)):
            row = self.conn.execute(
                "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = ?",
                (DAY, key),
            ).fetchone()
            self.assertEqual(row["value"], expected)

    def test_a_field_not_given_writes_no_daily_metric_row(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4)
        for key in ("energy", "stress_felt"):
            row = self.conn.execute(
                "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = ?",
                (DAY, key),
            ).fetchone()
            self.assertIsNone(row)

    def test_note_given_creates_a_checkin_nota_row_sharing_the_same_id(self):
        cid = db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4, nota="durmiendo mal")
        row = self.conn.execute("SELECT id, nota FROM checkin_nota WHERE id = ?", (cid,)).fetchone()
        self.assertEqual((row["id"], row["nota"]), (cid, "durmiendo mal"))

    def test_no_note_given_creates_no_checkin_nota_row(self):
        cid = db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4)
        row = self.conn.execute("SELECT 1 FROM checkin_nota WHERE id = ?", (cid,)).fetchone()
        self.assertIsNone(row)

    def test_second_checkin_of_the_day_overwrites_daily_metric_with_the_latest(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", mood=2)
        db.crear_checkin(self.conn, 1, DAY, "post_turno", mood=5)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'mood'", (DAY,)
        ).fetchone()
        self.assertEqual(row["value"], 5)

    def test_several_checkins_a_day_are_not_summed(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", mood=2)
        db.crear_checkin(self.conn, 1, DAY, "post_turno", mood=5)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'mood'", (DAY,)
        ).fetchone()
        self.assertNotEqual(row["value"], 7)  # 2 + 5, the sum this ruling explicitly forbids

    def test_borrar_checkin_removes_the_checkin_and_its_note_together(self):
        cid = db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4, nota="x")
        db.borrar_checkin(self.conn, 1, cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin_nota").fetchone()[0], 0)

    def test_borrar_falls_back_to_the_remaining_checkin_of_the_day(self):
        first = db.crear_checkin(self.conn, 1, DAY, "despertar", mood=2)
        db.crear_checkin(self.conn, 1, DAY, "post_turno", mood=5)
        second = self.conn.execute("SELECT id FROM checkin WHERE mood = 5").fetchone()["id"]
        db.borrar_checkin(self.conn, 1, second)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'mood'", (DAY,)
        ).fetchone()
        self.assertEqual(row["value"], 2)
        self.assertIsNotNone(first)

    def test_borrar_the_last_checkin_of_the_day_removes_the_daily_metric_row(self):
        cid = db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4)
        db.borrar_checkin(self.conn, 1, cid)
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'mood'", (DAY,)
        ).fetchone()
        self.assertIsNone(row)

    def test_another_persons_checkin_is_refused_by_borrar(self):
        cid = db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4)
        with self.assertRaises(ValueError):
            db.borrar_checkin(self.conn, 2, cid)


# ---- registrar_checkin: propose -> apply -> undo ------------------------------------------------

class RegistrarCheckinProposeApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)

    def _proponer(self, **extra):
        params = {"day": DAY, "momento": "despertar", "mood": 4, "energy": 3, "stress_felt": 2,
                  "nota": "durmiendo mal"}
        params.update(extra)
        params = {k: v for k, v in params.items() if v is not None}
        return herramientas.ejecutar(self.conn, 1, "registrar_checkin", params, "ninguno", self.cid)

    def test_propose_writes_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0]
        out = self._proponer()
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0], before)

    def test_apply_writes_the_checkin_and_its_note_together(self):
        out = self._proponer()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin_nota").fetchone()[0], 1)

    def test_undo_removes_the_checkin_and_its_note_together(self):
        out = self._proponer()
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        resultado = propuestas.deshacer(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin_nota").fetchone()[0], 0)

    def test_numeric_string_is_coerced(self):
        out = self._proponer(mood="4", energy="3", stress_felt="2")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT mood FROM checkin").fetchone()
        self.assertEqual(row["mood"], 4)

    def test_a_range_string_is_refused_with_no_row_written(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(mood="4-5")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0], 0)

    def test_zero_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(mood=0)

    def test_six_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(mood=6)

    def test_a_bool_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(mood=True)

    def test_unknown_momento_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(momento="siesta")

    def test_all_four_empty_is_refused(self):
        with self.assertRaises(herramientas.ToolError):
            self._proponer(mood=None, energy=None, stress_felt=None, nota=None)

    def test_only_a_note_is_enough(self):
        out = self._proponer(mood=None, energy=None, stress_felt=None, nota="solo una nota")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")
        row = self.conn.execute("SELECT mood, energy, stress_felt FROM checkin").fetchone()
        self.assertEqual((row["mood"], row["energy"], row["stress_felt"]), (None, None, None))

    def test_only_one_number_is_enough(self):
        out = self._proponer(mood=4, energy=None, stress_felt=None, nota=None)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")

    def test_registrar_medida_refuses_a_checkin_key(self):
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "registrar_medida", {"metric_key": "mood", "value": 4}, "ninguno",
                                   self.cid)


# ---- card: labels, «4 de 5», the note in quotes, the moment word -------------------------------

class CardTextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = _cid(self.conn)
        self.defs = comun._defs(self.conn)

    def _tarjeta(self, lang: str, **extra):
        params = {"day": DAY, "momento": "despertar", "mood": 4, "energy": 3, "stress_felt": 2,
                  "nota": "durmiendo mal"}
        params.update(extra)
        params = {k: v for k, v in params.items() if v is not None}
        out = herramientas.ejecutar(self.conn, 1, "registrar_checkin", params, "ninguno", self.cid)
        return asistente._tarjeta_propuesta(T(lang), self.defs, out["_registro"])

    def test_card_names_the_values_and_note_es(self):
        t = self._tarjeta("es")
        self.assertIn("Ánimo", t["despues"])
        self.assertIn("4 de 5", t["despues"])
        self.assertIn("Energía", t["despues"])
        self.assertIn("Estrés que sentiste", t["despues"])
        self.assertIn("«durmiendo mal»", t["despues"])

    def test_card_names_the_values_and_note_en(self):
        t = self._tarjeta("en")
        self.assertIn("Mood", t["despues"])
        self.assertIn("4 of 5", t["despues"])
        self.assertIn("Energy", t["despues"])
        self.assertIn("Stress you felt", t["despues"])
        self.assertIn("“durmiendo mal”", t["despues"])

    def test_card_names_the_moment_word(self):
        t = self._tarjeta("es", momento="post_turno")
        self.assertIn("después del turno", t["despues"])

    def test_card_with_no_note_has_no_quotes(self):
        t = self._tarjeta("es", nota=None)
        self.assertNotIn("«", t["despues"])

    def test_card_render_never_raises_on_a_malformed_despues(self):
        tarjeta = asistente._tarjeta_propuesta_segura(T("es"), self.defs, {
            "id": 999, "accion": "registrar_checkin", "despues_json": {}, "antes_json": None,
            "estado": "propuesta", "expira_en": "2026-11-09T00:10:00+00:00", "aplicada_via": None,
        })
        self.assertIn("despues", tarjeta)


# ---- Día's own "Cómo te sentiste" section --------------------------------------------------------

class DiaSectionTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.t = T("es")
        self.defs = comun._defs(self.conn)

    def test_empty_day_has_no_section(self):
        self.assertIsNone(hoy._checkins_dia_ctx(self.t, self.conn, 1, self.defs, {}, DAY))

    def test_a_days_checkin_lists_its_values_and_note(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", ts_local=f"{DAY}T07:00:00", mood=4, energy=3,
                          stress_felt=2, nota="durmiendo mal")
        ctx = hoy._checkins_dia_ctx(self.t, self.conn, 1, self.defs, {}, DAY)
        self.assertIsNotNone(ctx)
        self.assertEqual(len(ctx["eventos"]), 1)
        evento = ctx["eventos"][0]
        self.assertEqual(evento["hora"], "07:00")
        self.assertEqual(evento["momento"], "al despertar")
        self.assertEqual({v["etiqueta"]: v["valor"] for v in evento["valores"]},
                          {"Ánimo": "4 de 5", "Energía": "3 de 5", "Estrés que sentiste": "2 de 5"})
        self.assertEqual(evento["nota"], "«durmiendo mal»")

    def test_several_checkins_a_day_each_get_their_own_entry(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", mood=2)
        db.crear_checkin(self.conn, 1, DAY, "post_turno", mood=5)
        ctx = hoy._checkins_dia_ctx(self.t, self.conn, 1, self.defs, {}, DAY)
        self.assertEqual(len(ctx["eventos"]), 2)

    def test_comparison_line_only_when_both_sides_exist(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", stress_felt=4)
        ctx = hoy._checkins_dia_ctx(self.t, self.conn, 1, self.defs, {"stress_avg": 37.0, "stress_felt": 4}, DAY)
        self.assertEqual(ctx["comparacion"], "El reloj midió estrés promedio 37; tú lo sentiste 4 de 5.")

    def test_no_comparison_line_when_the_watch_side_is_missing(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", stress_felt=4)
        ctx = hoy._checkins_dia_ctx(self.t, self.conn, 1, self.defs, {}, DAY)
        self.assertIsNone(ctx["comparacion"])

    def test_no_comparison_line_when_the_felt_side_is_missing(self):
        db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4)
        ctx = hoy._checkins_dia_ctx(self.t, self.conn, 1, self.defs, {"stress_avg": 37.0}, DAY)
        self.assertIsNone(ctx["comparacion"])


# ---- ruling 4: the note never reaches a read tool -----------------------------------------------

class NotaPrivadaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4, energy=3, stress_felt=2,
                          nota="ZZ-NOTA-SECRETA")

    def test_leer_dia_never_mentions_checkin(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_dia", {"day": DAY}, "completo")
        self.assertNotIn("ZZ-NOTA-SECRETA", repr(out))
        self.assertNotIn("checkin", out)

    def test_leer_tendencias_never_mentions_the_note(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_tendencias", {}, "completo")
        self.assertNotIn("ZZ-NOTA-SECRETA", repr(out))

    def test_every_read_tool_omits_the_note(self):
        for nombre in herramientas.disponibles("completo"):
            if nombre["tipo"] != "lectura":
                continue
            params = {"day": DAY} if "day" in nombre["parametros"].get("properties", {}) and \
                "day" in nombre["parametros"].get("required", []) else {}
            try:
                out = herramientas.ejecutar(self.conn, 1, nombre["nombre"], params, "completo")
            except herramientas.ToolError:
                continue
            self.assertNotIn("ZZ-NOTA-SECRETA", repr(out),
                              f"{nombre['nombre']} returned the private checkin note")


# ---- registrar_checkin is a registered propuesta tool (body.mcp lists it generically, tested in
# tests/test_mcp.py's own ToolsCallTest) -----------------------------------------------------------

class ToolRegistryTest(unittest.TestCase):
    def test_registrar_checkin_is_registered_as_a_propuesta_tool(self):
        nombres = {h["nombre"]: h for h in herramientas.disponibles("ninguno")}
        self.assertIn("registrar_checkin", nombres)
        self.assertEqual(nombres["registrar_checkin"]["tipo"], "propuesta")


# ---- export / delete ------------------------------------------------------------------------------

class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self)
        self.cid = db.crear_checkin(self.conn, 1, DAY, "despertar", mood=4, energy=3, stress_felt=2,
                                     nota="durmiendo mal")
        self.conn.commit()

    def test_checkin_is_a_fully_handled_table(self):
        self.assertIn("checkin", privacidad.FULLY_HANDLED_TABLES)

    def test_checkin_nota_is_never_exported(self):
        self.assertIn("checkin_nota", privacidad.NEVER_EXPORTED_TABLES)

    def test_export_includes_the_three_values_but_never_the_note(self):
        data = privacidad.export_person(self.conn, 1)
        self.assertEqual(len(data["checkins"]), 1)
        fila = data["checkins"][0]
        self.assertEqual((fila["mood"], fila["energy"], fila["stress_felt"]), (4, 3, 2))
        self.assertNotIn("nota", fila)
        import json
        self.assertNotIn("durmiendo mal", json.dumps(data, ensure_ascii=False))

    def test_export_of_another_person_sees_nothing(self):
        data = privacidad.export_person(self.conn, 2)
        self.assertEqual(data["checkins"], [])

    def test_delete_all_removes_the_checkin_and_its_note(self):
        privacidad.delete_person(self.conn, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkin_nota").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
