"""BODY-TURNO-DIA-PLAN-1: the one definition of "not free to train", its batched read, the
assistant tool that surfaces it, and the planear_entreno card's own render-time warning. Invented
demo shifts only, no real schedule, no network.
"""
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn
from tests._rutas import contar_sql

from body import config, db, identity, mcp, turnos_costo, web
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas
from body.i18n import T


def _mkconn(case: unittest.TestCase):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "t.db")
    db.init(conn)
    return conn


def _insert_other_person(conn) -> int:
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'ZZ-OTRO')")
    return 2


def _insert_shift(conn, person_id: int, day: str, kind: str = "night", start: str | None = None,
                   end: str | None = None) -> None:
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, end_local, source) VALUES (?, ?, ?, ?, ?, 'manual')",
        (person_id, day, kind, start, end),
    )


class MotivoNoLibreTest(unittest.TestCase):
    def test_no_shifts_day_is_free(self):
        conn = _mkconn(self)
        self.assertIsNone(turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 5)))

    def test_off_day_is_free(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="off")
        self.assertIsNone(turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 5)))

    def test_morning_shift_not_free_with_hours(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="morning", start="06:00", end="14:00")
        motivo = turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 5))
        self.assertEqual(motivo, {"tipo": "turno", "kind": "morning", "inicio": "06:00", "fin": "14:00"})

    def test_shift_with_no_hours_inicio_fin_null(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="afternoon")
        motivo = turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 5))
        self.assertEqual(motivo, {"tipo": "turno", "kind": "afternoon", "inicio": None, "fin": None})

    def test_night_shift_day_itself_not_free_as_turno(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="night", start="22:00", end="06:00")
        motivo = turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 5))
        self.assertEqual(motivo["tipo"], "turno")
        self.assertEqual(motivo["kind"], "night")

    def test_day_after_a_night_shift_not_free_sale_de_noche(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="night", start="22:00", end="06:00")
        motivo = turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 6))
        self.assertEqual(motivo, {"tipo": "sale_de_noche"})

    def test_turno_wins_over_sale_de_noche_when_the_next_day_has_its_own_shift(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="night", start="22:00", end="06:00")
        _insert_shift(conn, 1, "2027-01-06", kind="morning", start="06:00", end="14:00")
        motivo = turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 6))
        self.assertEqual(motivo["tipo"], "turno")
        self.assertEqual(motivo["kind"], "morning")

    def test_off_day_after_a_night_shift_still_not_free_sale_de_noche(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="night", start="22:00", end="06:00")
        _insert_shift(conn, 1, "2027-01-06", kind="off")
        motivo = turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 6))
        self.assertEqual(motivo, {"tipo": "sale_de_noche"})

    def test_two_days_after_a_night_shift_is_free(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="night", start="22:00", end="06:00")
        self.assertIsNone(turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 7)))

    def test_another_persons_shift_never_read(self):
        conn = _mkconn(self)
        other = _insert_other_person(conn)
        _insert_shift(conn, other, "2027-01-05", kind="night", start="22:00", end="06:00")
        self.assertIsNone(turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 5)))
        self.assertIsNone(turnos_costo.motivo_no_libre(conn, 1, date(2027, 1, 6)))


class DiasLibresTest(unittest.TestCase):
    def test_matches_motivo_no_libre_per_day(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-05", kind="night", start="22:00", end="06:00")
        _insert_shift(conn, 1, "2027-01-08", kind="morning", start="06:00", end="14:00")
        desde = date(2027, 1, 4)
        salida = turnos_costo.dias_libres(conn, 1, desde, 7)
        self.assertEqual(len(salida), 7)
        for i, item in enumerate(salida):
            dia = desde + timedelta(i)
            self.assertEqual(item["dia"], dia.isoformat())
            esperado = turnos_costo.motivo_no_libre(conn, 1, dia)
            self.assertEqual(item["motivo"], esperado)
            self.assertEqual(item["libre"], esperado is None)

    def test_no_shifts_every_day_free(self):
        conn = _mkconn(self)
        salida = turnos_costo.dias_libres(conn, 1, date(2027, 2, 1), 5)
        self.assertTrue(all(item["libre"] and item["motivo"] is None for item in salida))

    def test_tras_noches_present_inside_recovery_window(self):
        conn = _mkconn(self)
        for day in ("2027-01-05", "2027-01-06", "2027-01-07"):
            _insert_shift(conn, 1, day, kind="night", start="22:00", end="06:00")
        # last night 2027-01-07 -- 2027-01-08 is "sale_de_noche" (motivo), 2027-01-09 onward free
        # with a growing tras_noches, inside ventana_post (10 days).
        salida = turnos_costo.dias_libres(conn, 1, date(2027, 1, 9), 4)
        por_dia = {item["dia"]: item for item in salida}
        self.assertTrue(por_dia["2027-01-09"]["libre"])
        self.assertEqual(por_dia["2027-01-09"]["tras_noches"], 2)
        self.assertEqual(por_dia["2027-01-10"]["tras_noches"], 3)
        self.assertEqual(por_dia["2027-01-11"]["tras_noches"], 4)
        self.assertEqual(por_dia["2027-01-12"]["tras_noches"], 5)

    def test_tras_noches_absent_once_past_the_recovery_window(self):
        conn = _mkconn(self)
        _insert_shift(conn, 1, "2027-01-01", kind="night", start="22:00", end="06:00")
        salida = turnos_costo.dias_libres(conn, 1, date(2027, 1, 20), 1)
        self.assertTrue(salida[0]["libre"])
        self.assertNotIn("tras_noches", salida[0])

    def test_another_persons_shifts_never_read(self):
        conn = _mkconn(self)
        other = _insert_other_person(conn)
        _insert_shift(conn, other, "2027-01-05", kind="night", start="22:00", end="06:00")
        salida = turnos_costo.dias_libres(conn, 1, date(2027, 1, 4), 5)
        self.assertTrue(all(item["libre"] for item in salida))

    def test_flat_query_count_three_vs_fourteen_days(self):
        """A fixed number of statements whatever `dias` is -- `contar_sql` (tests/_rutas.py) only
        traces a connection opened AFTER it starts, so this counts directly on the test's own
        already-open connection instead (`sqlite3.Connection.set_trace_callback`)."""
        conn = _mkconn(self)
        for day in ("2027-01-05", "2027-01-06", "2027-01-20"):
            _insert_shift(conn, 1, day, kind="night", start="22:00", end="06:00")

        def _contar(fn) -> int:
            n = 0

            def _trace(sql):
                nonlocal n
                n += 1

            conn.set_trace_callback(_trace)
            try:
                fn()
            finally:
                conn.set_trace_callback(None)
            return n

        corto = _contar(lambda: turnos_costo.dias_libres(conn, 1, date(2027, 1, 1), 3))
        largo = _contar(lambda: turnos_costo.dias_libres(conn, 1, date(2027, 1, 1), 14))
        self.assertEqual(corto, largo)
        self.assertEqual(corto, 2)


class LeerDiasLibresToolTest(unittest.TestCase):
    def test_default_desde_is_today_in_the_persons_zone_and_seven_days(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        hoy = herramientas._hoy_local(conn, person_id)
        out = herramientas.ejecutar(conn, person_id, "leer_dias_libres", {}, "ninguno")
        self.assertEqual(len(out["dias_libres"]), 7)
        self.assertEqual(out["dias_libres"][0]["dia"], hoy)

    def test_explicit_desde_and_dias(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        out = herramientas.ejecutar(conn, person_id, "leer_dias_libres",
                                     {"desde": "2027-01-05", "dias": 3}, "ninguno")
        self.assertEqual([d["dia"] for d in out["dias_libres"]],
                         ["2027-01-05", "2027-01-06", "2027-01-07"])

    def test_dias_out_of_range_refused(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        for dias in (0, 15):
            with self.assertRaises(herramientas.ToolError):
                herramientas.ejecutar(conn, person_id, "leer_dias_libres",
                                       {"desde": "2027-01-05", "dias": dias}, "ninguno")

    def test_bad_desde_refused(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, person_id, "leer_dias_libres", {"desde": "not-a-date"}, "ninguno")

    def test_available_at_nivel_ninguno_regardless_of_turnos_flag(self):
        self.addCleanup(herramientas.fijar_turnos_actual, False)
        herramientas.fijar_turnos_actual(False)
        nombres = {h["nombre"] for h in herramientas.disponibles("ninguno")}
        self.assertIn("leer_dias_libres", nombres)
        self.assertNotIn("leer_dias_libres", herramientas.TURNOS_HERRAMIENTAS)

    def test_running_the_tool_changes_nothing(self):
        conn = _mkconn(self)
        person_id = db.local_person_id(conn)
        before = conn.total_changes
        herramientas.ejecutar(conn, person_id, "leer_dias_libres", {"desde": "2027-01-05"}, "ninguno")
        self.assertEqual(conn.total_changes, before)


def _mcp_linea(obj: dict) -> str:
    return json.dumps(obj) + "\n"


class LeerDiasLibresMcpTest(unittest.TestCase):
    """BODY-TURNO-DIA-PLAN-1: the tool reaches MCP like any other lectura tool -- the generic
    tools/list and tools/call paths need no new code for it either (the same proof
    test_mcp.py's own test_leer_estado_reloj_is_offered_and_callable makes for its tool)."""

    def setUp(self):
        self.conn = _mkconn(self)
        self.person_id = db.local_person_id(self.conn)

    def _ejecutar(self, mensajes: list[dict]):
        entrada = io.StringIO("".join(_mcp_linea(m) for m in mensajes))
        salida = io.StringIO()
        mcp.ejecutar(entrada, salida, self.conn, self.person_id)
        return [json.loads(l) for l in salida.getvalue().splitlines() if l.strip()]

    def test_offered_and_callable_through_mcp(self):
        listado = self._ejecutar([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        nombres = {t["name"] for t in listado[0]["result"]["tools"]}
        self.assertIn("leer_dias_libres", nombres)

        respuestas = self._ejecutar([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "leer_dias_libres", "arguments": {"desde": "2027-01-05", "dias": 3}}},
        ])
        r = respuestas[0]["result"]
        self.assertNotIn("isError", r)
        salida = json.loads(r["content"][0]["text"])
        self.assertEqual(len(salida["dias_libres"]), 3)


class PlannerDescriptionTest(unittest.TestCase):
    """Ruling 3: planear_entreno's own description points at leer_dias_libres first.
    proponer_plan/ampliar_plan write a horizon's goal/habits/rules/weeks/checkpoints
    (body/db.py's crear_plan_horizonte and ampliar_plan_horizonte) -- no per-day `plan` row, so
    they carry no such sentence (checked by its absence below, documented in the ledger with
    file:line)."""

    def test_planear_entreno_description_carries_the_sentence(self):
        entrada = herramientas._BY_NAME["planear_entreno"]
        self.assertIn("leer_dias_libres", entrada["descripcion"])
        self.assertIn("nunca inventes una fecha", entrada["descripcion"])

    def test_proponer_plan_and_ampliar_plan_write_no_dated_session_and_carry_no_such_sentence(self):
        for nombre in ("proponer_plan", "ampliar_plan"):
            entrada = herramientas._BY_NAME[nombre]
            self.assertNotIn("leer_dias_libres", entrada["descripcion"])


# --- card rendering (ruling 4) -------------------------------------------------------------------

def _reset_db_web(case: unittest.TestCase):
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "web.db"
    conn = db.connect()
    case.addCleanup(conn.close)
    db.ensure_ready(conn)  # made ready BEFORE any route call (tests/test_get_sin_escritura.py's own rule)
    return conn


class TarjetaPlanearEntrenoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db_web(self)
        self.person_id = db.local_person_id(self.conn)
        self.cid = agente_conversacion.crear(self.conn, self.person_id)
        self.conn.commit()
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: self.person_id
        self.client = TestClient(web.app, follow_redirects=False)

    def _planear(self, day: str) -> dict:
        salida = herramientas.ejecutar(
            self.conn, self.person_id, "planear_entreno",
            {"day": day, "titulo": "ZZ-PLAN", "ejercicios": [{"ejercicio": "sentadilla", "series": 3}]},
            "ninguno", conversacion_id=self.cid,
        )
        self.conn.commit()
        return salida

    def _tarjeta(self) -> dict:
        r = self.client.get(f"/asistente/conversacion/{self.cid}")
        self.assertEqual(r.status_code, 200)
        tarjetas = r.json()["propuestas"]
        self.assertEqual(len(tarjetas), 1)
        return tarjetas[0]

    def test_free_day_shows_nothing_new(self):
        self._planear("2027-03-01")
        tarjeta = self._tarjeta()
        self.assertNotIn("turno", tarjeta["despues"].lower())
        self.assertNotIn("noche", tarjeta["despues"].lower())

    def test_day_with_a_shift_shows_the_warning_with_hours_es(self):
        _insert_shift(self.conn, self.person_id, "2027-03-02", kind="morning", start="06:00", end="14:00")
        self.conn.commit()
        self._planear("2027-03-02")
        tarjeta = self._tarjeta()
        self.assertIn("Ese día tienes turno: mañana, 06:00–14:00", tarjeta["despues"])

    def test_day_with_a_shift_with_no_hours_es(self):
        _insert_shift(self.conn, self.person_id, "2027-03-03", kind="afternoon")
        self.conn.commit()
        self._planear("2027-03-03")
        tarjeta = self._tarjeta()
        self.assertIn("Ese día tienes turno: tarde", tarjeta["despues"])
        self.assertNotIn("–", tarjeta["despues"])

    def test_day_after_a_night_shift_shows_sale_de_noche_es(self):
        _insert_shift(self.conn, self.person_id, "2027-03-04", kind="night", start="22:00", end="06:00")
        self.conn.commit()
        self._planear("2027-03-05")
        tarjeta = self._tarjeta()
        self.assertIn("Esa mañana sales de un turno de noche", tarjeta["despues"])

    def test_day_with_a_shift_en(self):
        self.conn.execute("UPDATE person SET language = 'en' WHERE id = ?", (self.person_id,))
        self.conn.commit()
        _insert_shift(self.conn, self.person_id, "2027-03-06", kind="morning", start="06:00", end="14:00")
        self.conn.commit()
        self._planear("2027-03-06")
        tarjeta = self._tarjeta()
        self.assertIn("You have a shift that day: morning, 06:00–14:00", tarjeta["despues"])

    def test_aplicar_still_offered_with_a_shift_collision(self):
        _insert_shift(self.conn, self.person_id, "2027-03-07", kind="morning", start="06:00", end="14:00")
        self.conn.commit()
        self._planear("2027-03-07")
        tarjeta = self._tarjeta()
        self.assertEqual(tarjeta["estado"], "propuesta")
        self.assertTrue(tarjeta["deshacer"] or "aplicar" in tarjeta["etiquetas"])

    def test_card_computed_at_render_time_not_a_stored_snapshot(self):
        """Ruling 4: a shift confirmed AFTER the proposal was created still shows on the card --
        never a value frozen when planear_entreno was called (the zonas precedent's own choice)."""
        self._planear("2027-03-08")
        tarjeta_antes = self._tarjeta()
        self.assertNotIn("turno", tarjeta_antes["despues"].lower())
        _insert_shift(self.conn, self.person_id, "2027-03-08", kind="afternoon")
        self.conn.commit()
        tarjeta_despues = self._tarjeta()
        self.assertIn("Ese día tienes turno: tarde", tarjeta_despues["despues"])

    def test_get_conversacion_writes_nothing(self):
        self._planear("2027-03-09")
        with contar_sql() as stats:
            self.client.get(f"/asistente/conversacion/{self.cid}")
        self.assertEqual(stats["writes"], 0)


if __name__ == "__main__":
    unittest.main()
