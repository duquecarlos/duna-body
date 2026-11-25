"""body/rutas/hoy.py's own pure helpers, direct -- BODY-MOCK-R2-HOY-1's "day ahead" merge/sort
(ruling 2), the tile band word (ruling 4), and the night-week-ahead lookup (ruling 6). The route-
level rendering of these is covered by tests/test_mock_r2_hoy.py; this file is the logic underneath
it, so a sort or classification bug is caught without rendering a page.

BODY-REVISION-SYNC-READINESS-1 ruling 3 adds `_synced` itself (the score hero's own one-line sync
fact), direct -- no network, invented demo data only."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_cuenta, goals, turnos_costo
from body.baselines import Baseline
from body.i18n import T
from body.rutas import hoy

ES = T("es")
EN = T("en")
TOKEN_KEY_HEX = "22" * 32


def _mkconn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


def _insert_shift(conn, person_id: int, day: str, kind: str = "night", start_local: str | None = None,
                   end_local: str | None = None) -> None:
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, end_local, source) VALUES (?, ?, ?, ?, ?, 'manual')",
        (person_id, day, kind, start_local, end_local),
    )


class NivelTileTest(unittest.TestCase):
    """Ruling 4: low/usual/high against the person's OWN 25th-75th-percentile range, never a
    generic table -- and the honest gap (None) with no baseline or no value yet."""

    def test_below_low_is_low(self):
        b = Baseline("stress_avg", mean=20.0, sd=5.0, low=15.0, high=25.0, n=20)
        self.assertEqual(hoy._nivel_tile(b, 10.0), "low")

    def test_above_high_is_high(self):
        b = Baseline("stress_avg", mean=20.0, sd=5.0, low=15.0, high=25.0, n=20)
        self.assertEqual(hoy._nivel_tile(b, 30.0), "high")

    def test_inside_range_is_usual(self):
        b = Baseline("stress_avg", mean=20.0, sd=5.0, low=15.0, high=25.0, n=20)
        self.assertEqual(hoy._nivel_tile(b, 20.0), "usual")

    def test_no_baseline_is_none(self):
        self.assertIsNone(hoy._nivel_tile(None, 20.0))

    def test_no_value_is_none(self):
        b = Baseline("stress_avg", mean=20.0, sd=5.0, low=15.0, high=25.0, n=20)
        self.assertIsNone(hoy._nivel_tile(b, None))

    def test_label_es_and_en(self):
        self.assertEqual(hoy._nivel_label(ES, "low"), "baja")
        self.assertEqual(hoy._nivel_label(ES, "usual"), "habitual")
        self.assertEqual(hoy._nivel_label(ES, "high"), "alta")
        self.assertEqual(hoy._nivel_label(EN, "low"), "low")
        self.assertEqual(hoy._nivel_label(EN, "usual"), "usual")
        self.assertEqual(hoy._nivel_label(EN, "high"), "high")

    def test_label_none_is_none(self):
        self.assertIsNone(hoy._nivel_label(ES, None))


class DiaPorDelanteTest(unittest.TestCase):
    """Ruling 2: the merged, time-ordered "day ahead" list -- shift, session, activities, habits,
    experiments, sorted by time with no-time items kept in that same source order after."""

    def test_empty_with_nothing_planned(self):
        conn = _mkconn(self)
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), None)
        self.assertEqual(items, [])

    def test_shift_with_hours_sorts_first(self):
        conn = _mkconn(self)
        turno_hoy = {"kind": "morning", "start_local": "2026-11-11T06:00:00", "end_local": "2026-11-11T14:00:00"}
        conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status, ejercicios_json) VALUES "
            "(1, '2026-11-11', '15:30', 'Upper B', 'accepted', '[{\"ejercicio\":\"press\",\"series\":3}]')")
        conn.commit()
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), turno_hoy)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["tipo"], "turno")
        self.assertEqual(items[0]["hora"], "06:00")
        self.assertEqual(items[0]["titulo"], ES("shift.kind.title.morning"))
        self.assertEqual(items[0]["linea"], ES("today.dia.turno_hasta", fin="14:00"))
        self.assertEqual(items[1]["tipo"], "sesion")
        self.assertEqual(items[1]["hora"], "15:30")
        self.assertEqual(items[1]["titulo"], "Upper B")
        self.assertEqual(items[1]["linea"], ES("today.dia.sesion_n", n=1))

    def test_shift_off_no_hours_still_shows_no_time(self):
        conn = _mkconn(self)
        turno_hoy = {"kind": "off", "start_local": None, "end_local": None}
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), turno_hoy)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["hora"])
        self.assertEqual(items[0]["titulo"], ES("shift.kind.title.off"))

    def test_activity_logged_today_is_included_with_its_own_time(self):
        conn = _mkconn(self)
        conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
            "VALUES (1, 'a1', '2026-11-11', '2026-11-11T18:30:00', 'cycling', NULL, 2700, 'garmin')")
        conn.commit()
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), None)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tipo"], "actividad")
        self.assertEqual(items[0]["hora"], "18:30")
        self.assertEqual(items[0]["titulo"], ES("activity.type.cycling"))
        self.assertEqual(items[0]["linea"], ES.minutes(45))

    def test_habit_due_today_with_no_hora_sorts_after_timed_items(self):
        conn = _mkconn(self)
        gid = goals.propose(conn, 1, "Dormir mejor", "2026-11-01", kind="milestone", deadline="2027-01-01")
        goals.activate(conn, gid)
        goals.add_habit(conn, gid, "Estirar", {"per": "day", "times": 1})
        conn.commit()
        turno_hoy = {"kind": "night", "start_local": "2026-11-11T22:00:00", "end_local": "2026-11-12T06:00:00"}
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), turno_hoy)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["tipo"], "turno")  # has a time
        self.assertEqual(items[1]["tipo"], "habito")  # no time -> after
        self.assertIsNone(items[1]["hora"])
        self.assertEqual(items[1]["titulo"], "Estirar")

    def test_habit_with_hora_sorts_by_its_own_time(self):
        conn = _mkconn(self)
        gid = goals.propose(conn, 1, "Dormir mejor", "2026-11-01", kind="milestone", deadline="2027-01-01")
        goals.activate(conn, gid)
        goals.add_habit(conn, gid, "Respiración", {"per": "day", "times": 1, "hora": "05:00"})
        conn.commit()
        turno_hoy = {"kind": "morning", "start_local": "2026-11-11T06:00:00", "end_local": "2026-11-11T14:00:00"}
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), turno_hoy)
        # the habit's own 05:00 is earlier than the shift's 06:00
        self.assertEqual(items[0]["tipo"], "habito")
        self.assertEqual(items[0]["hora"], "05:00")
        self.assertEqual(items[1]["tipo"], "turno")

    def test_no_autonomous_sounding_copy(self):
        """DESIGN rule 14 / BODY-MOCK-R2-CENSO-1-INV conflict #3: never a claim Body itself moved
        or scheduled anything -- checked against every day-ahead string this slice's own es/en
        catalog entries can assemble."""
        import json
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        es = json.loads((root / "body" / "i18n" / "es.json").read_text(encoding="utf-8"))
        en = json.loads((root / "body" / "i18n" / "en.json").read_text(encoding="utf-8"))
        texto = " ".join([es["today.dia.turno_hasta"], es["today.dia.sesion_n"], es["today.plan_title"],
                          en["today.dia.turno_hasta"], en["today.dia.sesion_n"], en["today.plan_title"]]).lower()
        for prohibido in ("movió", "movido", "moved", "cambió tu plan", "changed your plan"):
            self.assertNotIn(prohibido, texto)


class PlanHoyDoneDropsOffTest(unittest.TestCase):
    """BODY-REVISION-DIA-PERIODO-1 ruling 1 ("done drops off"): a planned session marked 'done'
    whose own time has passed drops off the day-ahead list entirely; one marked 'done' whose time
    has not passed yet stays, checked (`hecho`); one NOT done whose time has passed stays too,
    marked `antes_hoy` ("earlier today") -- never silently dropped."""

    def _plan(self, conn, status: str, hora: str = "15:30"):
        conn.execute(
            "INSERT INTO plan (person_id, day, time, title, status, ejercicios_json) VALUES "
            "(1, '2026-11-11', ?, 'Upper B', ?, '[{\"ejercicio\":\"press\",\"series\":3}]')",
            (hora, status))
        conn.commit()

    def test_done_and_past_its_time_drops_off_entirely(self):
        conn = _mkconn(self)
        self._plan(conn, "done")
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), None, ahora_hhmm="16:00")
        self.assertEqual(items, [])

    def test_done_but_not_past_its_time_stays_checked(self):
        conn = _mkconn(self)
        self._plan(conn, "done")
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), None, ahora_hhmm="10:00")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["hecho"])
        self.assertFalse(items[0]["antes_hoy"])

    def test_not_done_and_past_its_time_stays_marked_earlier_today(self):
        conn = _mkconn(self)
        self._plan(conn, "accepted")
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), None, ahora_hhmm="16:00")
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["hecho"])
        self.assertTrue(items[0]["antes_hoy"])

    def test_not_done_and_not_past_its_time_is_plain(self):
        conn = _mkconn(self)
        self._plan(conn, "accepted")
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), None, ahora_hhmm="10:00")
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["hecho"])
        self.assertFalse(items[0]["antes_hoy"])

    def test_done_with_no_time_at_all_never_drops(self):
        """A session with no clock time can never read as "past", so a 'done' one just stays
        checked -- there is no honest way to say its time has passed when it never had one."""
        conn = _mkconn(self)
        self._plan(conn, "done", hora=None)
        items = hoy._dia_por_delante_ctx(ES, conn, 1, date(2026, 11, 11), None, ahora_hhmm="23:59")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["hecho"])


class SiguienteBloqueNocheTest(unittest.TestCase):
    """Ruling 6: the next night block starting within 7 days -- never one already under way."""

    def test_block_starting_in_3_days_is_found(self):
        conn = _mkconn(self)
        hoy_d = date(2026, 11, 11)
        inicio = hoy_d + timedelta(3)
        _insert_shift(conn, 1, inicio.isoformat(), kind="night")
        _insert_shift(conn, 1, (inicio + timedelta(1)).isoformat(), kind="night")
        conn.commit()
        bloque = turnos_costo.siguiente_bloque_noche(conn, 1, hoy_d)
        self.assertIsNotNone(bloque)
        self.assertEqual(bloque["inicio"], inicio.isoformat())
        self.assertEqual(bloque["fin"], (inicio + timedelta(1)).isoformat())

    def test_block_starting_in_8_days_is_not_found(self):
        conn = _mkconn(self)
        hoy_d = date(2026, 11, 11)
        inicio = hoy_d + timedelta(8)
        _insert_shift(conn, 1, inicio.isoformat(), kind="night")
        conn.commit()
        self.assertIsNone(turnos_costo.siguiente_bloque_noche(conn, 1, hoy_d))

    def test_block_already_under_way_today_is_not_found(self):
        conn = _mkconn(self)
        hoy_d = date(2026, 11, 11)
        _insert_shift(conn, 1, (hoy_d - timedelta(1)).isoformat(), kind="night")
        _insert_shift(conn, 1, hoy_d.isoformat(), kind="night")
        _insert_shift(conn, 1, (hoy_d + timedelta(1)).isoformat(), kind="night")
        conn.commit()
        self.assertIsNone(turnos_costo.siguiente_bloque_noche(conn, 1, hoy_d))

    def test_no_night_at_all_is_none(self):
        conn = _mkconn(self)
        self.assertIsNone(turnos_costo.siguiente_bloque_noche(conn, 1, date(2026, 11, 11)))


class SemanaNocturnaCtxTest(unittest.TestCase):
    """Ruling 6's Hoy card context: the honest gap when there is not yet enough recovery history."""

    def test_none_with_no_upcoming_block(self):
        conn = _mkconn(self)
        self.assertIsNone(hoy._semana_nocturna_ctx(ES, conn, 1, date(2026, 11, 11)))

    def test_honest_gap_with_too_little_history(self):
        conn = _mkconn(self)
        hoy_d = date(2026, 11, 11)
        inicio = hoy_d + timedelta(3)
        _insert_shift(conn, 1, inicio.isoformat(), kind="night")
        conn.commit()
        ctx = hoy._semana_nocturna_ctx(ES, conn, 1, hoy_d)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["recuperacion"], ES("today.semana_nocturna.sin_dato"))


class SyncHeroCtxTest(unittest.TestCase):
    """BODY-REVISION-SYNC-READINESS-1 ruling 3: the score hero's own one-line sync fact -- never
    two lines that both claim "synced" about a different thing. The watch's own last contact with
    Garmin when that fact exists (the SAME field "Tu reloj"'s own card reads), named as the
    watch's own; otherwise Body's own last successful pull, named as Body's own; either way, only
    for an instant that falls on today's own local date."""

    def setUp(self):
        self.conn = _mkconn(self)
        self.person = db.person(self.conn, 1)
        env_patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _registrar_pull_de_body(self, cuando: datetime) -> None:
        self.conn.execute(
            "INSERT INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
            "VALUES (1, ?, 'resumen', 'garmin', ?, '{}')",
            (cuando.date().isoformat(), cuando.isoformat()),
        )
        self.conn.commit()

    def test_no_fact_at_all_is_none(self):
        self.assertIsNone(hoy._sync_hero_ctx(ES, self.conn, self.person))

    def test_bodys_own_pull_today_is_named_as_bodys(self):
        self._registrar_pull_de_body(datetime.now(timezone.utc))
        texto = hoy._sync_hero_ctx(ES, self.conn, self.person)
        self.assertIsNotNone(texto)
        self.assertTrue(texto.startswith(ES("today.synced")))
        self.assertNotIn(ES("today.synced_reloj"), texto)

    def test_watch_fact_today_is_preferred_over_bodys_own_pull(self):
        self._registrar_pull_de_body(datetime.now(timezone.utc) - timedelta(hours=1))
        garmin_cuenta.guardar(self.conn, 1, "tok")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, datetime.now(timezone.utc))
        self.conn.commit()
        texto = hoy._sync_hero_ctx(ES, self.conn, self.person)
        self.assertTrue(texto.startswith(ES("today.synced_reloj")))

    def test_an_older_watch_fact_is_none_not_a_stale_clock(self):
        garmin_cuenta.guardar(self.conn, 1, "tok")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, datetime.now(timezone.utc) - timedelta(days=3))
        self.conn.commit()
        self.assertIsNone(hoy._sync_hero_ctx(ES, self.conn, self.person))

    def test_english_wording_names_each_subject_differently(self):
        self._registrar_pull_de_body(datetime.now(timezone.utc))
        texto = hoy._sync_hero_ctx(EN, self.conn, self.person)
        self.assertTrue(texto.startswith(EN("today.synced")))
        self.assertNotIn("Watch", texto)

    def test_the_bare_caption_time_is_unaffected_by_the_watch_fact(self):
        """`_synced` -- the partial-day tiles' own "as of HH:MM" caption -- stays Body's own pull,
        a bare time, even when a watch fact also exists and would win in the hero."""
        ahora = datetime.now(timezone.utc)
        self._registrar_pull_de_body(ahora)
        sin_reloj = hoy._synced(self.conn, self.person)
        self.assertIsNotNone(sin_reloj)
        self.assertNotIn(" ", sin_reloj)  # a bare "HH:MM", never a prefixed sentence
        garmin_cuenta.guardar(self.conn, 1, "tok")
        garmin_cuenta.registrar_reloj_sincronizo(self.conn, 1, ahora - timedelta(hours=2))
        self.conn.commit()
        self.assertEqual(hoy._synced(self.conn, self.person), sin_reloj)


if __name__ == "__main__":
    unittest.main()
