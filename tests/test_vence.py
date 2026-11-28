"""body/vence.py: BODY-HOY-LO-QUE-VENCE-1's own "coming due" census, direct -- the INV's own
section 7 cases. Hoy's route-level rendering of the strip is covered by tests/test_mock_r2_hoy.py
and tests/test_plan_hoy_registro.py; this file is the pure module underneath it, plus one
end-to-end guard that every fact lands on the page exactly once. Invented demo data only, no real
figures, no network.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, goals, identity, vence, web
from body.i18n import T
from body.rutas import comun, hoy
from tests._db import open_conn

ES = T("es")
EN = T("en")
HOY = date(2026, 11, 20)

_CADENCIA = {"comidas": "daily", "proteina": "daily", "peso": "weekly", "cintura": "weekly",
             "entreno": "weekly", "sueno": "nightly"}


def _mkconn(case: unittest.TestCase, name: str = "vence.db"):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _lineas(conn, person_id: int = 1, *, mcp_pendientes=None, cobertura_pendientes=None, ids_extra=()):
    return vence.lineas(conn, person_id, HOY, mcp_pendientes=mcp_pendientes,
                         cobertura_pendientes=cobertura_pendientes or {}, cadencia=_CADENCIA,
                         ids_extra=list(ids_extra))


def _metric_goal(conn, person_id: int, deadline: str | None, titulo: str = "ZZ meta") -> int:
    gid = goals.propose(conn, person_id, titulo, "2026-01-01", kind="metric", metric_key="resting_hr",
                         direction="lower", target_value=50.0, baseline_value=60.0, deadline=deadline)
    goals.activate(conn, gid)
    conn.commit()
    return gid


def _horizonte(conn, person_id: int, deadline: str, titulo: str = "ZZ horizonte") -> int:
    gid = goals.propose(conn, person_id, titulo, "2026-01-01", kind="milestone", deadline=deadline)
    goals.activate(conn, gid)
    db.crear_regla_horizonte(conn, person_id, gid, "siempre", None, "ZZ regla")
    conn.commit()
    return gid


class NadaVenceTest(unittest.TestCase):
    """Ruling 7: nothing due is an empty list -- no mcp proposal, no missing stream, no extra
    horizon, no metric-goal deadline in range."""

    def test_nothing_due_is_empty_list(self):
        conn = _mkconn(self, "nada.db")
        self.assertEqual(_lineas(conn), [])


class OrdenTest(unittest.TestCase):
    """Ruling 1/7: sorted overdue-first, then by state, and ascending by date inside each state --
    a single plain date sort would put every overdue fact last."""

    def test_overdue_today_soon_sorted_then_ascending_within_each(self):
        conn = _mkconn(self, "orden.db")
        _metric_goal(conn, 1, (HOY - timedelta(days=5)).isoformat(), "A")
        _metric_goal(conn, 1, (HOY - timedelta(days=2)).isoformat(), "B")
        _metric_goal(conn, 1, HOY.isoformat(), "C")
        _metric_goal(conn, 1, (HOY + timedelta(days=3)).isoformat(), "D")
        _metric_goal(conn, 1, (HOY + timedelta(days=10)).isoformat(), "E")
        out = _lineas(conn)
        self.assertEqual([l["datos"]["titulo"] for l in out], ["A", "B", "C", "D", "E"])
        self.assertEqual([l["estado"] for l in out], ["vencido", "vencido", "hoy", "pronto", "pronto"])


class CapTest(unittest.TestCase):
    """Ruling 3/7: the card shows at most max_lineas (4) lines; the rest collapse into one
    "+N more" line. `lineas()` itself returns every one of them, uncapped -- the cap is the
    card's own job (body/rutas/hoy.py's `_vence_ctx`)."""

    def _seed_six(self, conn):
        for i, dias in enumerate((-3, -1, 0, 2, 5, 9)):
            _metric_goal(conn, 1, (HOY + timedelta(days=dias)).isoformat(), f"ZZ-{i}")

    def test_lineas_returns_all_six_uncapped(self):
        conn = _mkconn(self, "cap_lineas.db")
        self._seed_six(conn)
        self.assertEqual(len(_lineas(conn)), 6)

    def test_card_shows_exactly_four_then_plus_n_more(self):
        conn = _mkconn(self, "cap_ctx.db")
        self._seed_six(conn)
        ctx = hoy._vence_ctx(ES, conn, 1, HOY, None, {}, [])
        self.assertEqual(len(ctx["lineas"]), 4)
        self.assertEqual(ctx["mas"], "+2 más")
        # BODY-HOY-LO-QUE-VENCE-1.W ruling 2: the card's own "tiempo" is each line's relative words
        # (vence.cuando) -- "en 2 días", never a bare absolute date the unwidened `_dia_relativo`
        # would have shown for anything past tomorrow.
        self.assertEqual([l["tiempo"] for l in ctx["lineas"]], ["hace 3 días", "ayer", "hoy", "en 2 días"])

    def test_no_more_line_when_four_or_fewer(self):
        conn = _mkconn(self, "cap_ctx_none.db")
        _metric_goal(conn, 1, HOY.isoformat(), "unica")
        ctx = hoy._vence_ctx(ES, conn, 1, HOY, None, {}, [])
        self.assertIsNone(ctx["mas"])

    def test_nothing_due_gives_no_card_at_all(self):
        conn = _mkconn(self, "cap_ctx_vacio.db")
        self.assertIsNone(hoy._vence_ctx(ES, conn, 1, HOY, None, {}, []))


class DatelessTest(unittest.TestCase):
    """Ruling 1/7: a source with no date is dropped before `lineas()` ever runs -- a metric goal
    with no deadline at all never reaches it."""

    def test_a_metric_goal_with_no_deadline_never_appears(self):
        conn = _mkconn(self, "dateless.db")
        _metric_goal(conn, 1, None, "ZZ sin fecha")
        self.assertEqual(_lineas(conn), [])


class PersonScopeTest(unittest.TestCase):
    """Ruling 7: two people never mix -- person 2's own near-term metric-goal deadline never
    appears in person 1's own strip."""

    def test_another_persons_deadline_never_shows(self):
        conn = _mkconn(self, "scope.db")
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'Otra')")
        conn.commit()
        _metric_goal(conn, 2, HOY.isoformat(), "de otra persona")
        self.assertEqual(_lineas(conn), [])


class HorizonteCapTest(unittest.TestCase):
    """Ruling 2/7: a plan horizon beyond VENCE_MODELO's own horizontes_max adds nothing, however
    many `ids_extra` the caller hands in -- `lineas()` enforces the cap itself, never trusting the
    caller to have already sliced it."""

    def test_a_horizon_beyond_the_cap_adds_nothing(self):
        conn = _mkconn(self, "horizonte_cap.db")
        gid_1 = _horizonte(conn, 1, (HOY + timedelta(days=5)).isoformat(), "ZZ horizonte 1")
        gid_2 = _horizonte(conn, 1, (HOY + timedelta(days=6)).isoformat(), "ZZ horizonte 2")
        gid_3 = _horizonte(conn, 1, (HOY + timedelta(days=7)).isoformat(), "ZZ horizonte 3 -- beyond the cap")
        out = _lineas(conn, ids_extra=[gid_1, gid_2, gid_3])
        titulos = [l["datos"]["titulo"] for l in out]
        self.assertIn("ZZ horizonte 1", titulos)
        self.assertIn("ZZ horizonte 2", titulos)
        self.assertNotIn("ZZ horizonte 3 -- beyond the cap", titulos)


class CadaHechoUnaVezTest(unittest.TestCase):
    """Ruling 4/7: a fixture carrying a proposal waiting, a missing weekly measure, a third
    horizon's checkpoint, a metric-goal deadline and a night-shift week renders Hoy with each
    fact's own text exactly once on the page."""

    def setUp(self):
        self.orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "cada_hecho.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1

    def test_each_fact_shown_exactly_once(self):
        from body.agente import conversacion as agente_conversacion
        from body.agente import propuestas as agente_propuestas
        conn = self.conn

        # horizonte.horizontes() orders newest-id first, so the horizon created FIRST (the lowest
        # id) is the one that falls beyond the plan card's own PLANES_HOY_MAX cap -- its
        # checkpoint is this strip's own job. Two more horizons, created after it, sit within the
        # cap and keep their own deadline/checkpoint on the plan card instead.
        gid_beyond = _horizonte(conn, 1, (HOY + timedelta(days=62)).isoformat(), "ZZ horizonte beyond cap")
        db.crear_checkpoint_horizonte(conn, 1, gid_beyond, (HOY + timedelta(days=4)).isoformat(),
                                       "ZZ checkpoint tercero",
                                       [{"medida": "sesiones_fuerza", "sentido": "al_menos", "valor": 1}])
        _horizonte(conn, 1, (HOY + timedelta(days=60)).isoformat(), "ZZ horizonte 1")
        _horizonte(conn, 1, (HOY + timedelta(days=61)).isoformat(), "ZZ horizonte 2")

        _metric_goal(conn, 1, (HOY + timedelta(days=6)).isoformat(), "ZZ meta con plazo")

        # Some daily figure for today, so Hoy's own early "no data yet" branch (soon.html) never
        # takes over before the strip is ever computed.
        db.set_metric(conn, HOY.isoformat(), "resting_hr", 60.0, "garmin", 1)

        # A missing weekly measure: peso logged once, long before this ISO week (used before,
        # never sin_uso), nothing logged this week.
        db.set_metric(conn, "2026-01-05", "body_weight_kg", 70.0, "self", 1)

        # A proposal still waiting, through the local MCP server.
        cid = agente_conversacion.crear(conn, 1, origen="mcp")
        agente_propuestas.crear(conn, 1, cid, "marcar_periodo",
                                 {"kind": "viaje", "start_day": "2020-01-01", "end_day": None, "note": None})

        # A night-shift week starting within 7 days -- left OUT of this strip on purpose (ruling
        # 2); its own existing card must still show, exactly once, never duplicated by the strip.
        inicio = HOY + timedelta(days=3)
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, ?, 'night', 'manual')",
                     (inicio.isoformat(),))
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (1, ?, 'night', 'manual')",
                     ((inicio + timedelta(1)).isoformat(),))
        conn.commit()

        with mock.patch.object(comun, "_today_local", return_value=HOY):
            page = TestClient(web.app, follow_redirects=False).get("/").text

        self.assertEqual(page.count("ZZ checkpoint tercero"), 1)
        self.assertEqual(page.count("ZZ meta con plazo"), 1)
        self.assertEqual(page.count("Falta registrar esta semana: peso"), 1)
        self.assertEqual(page.count("Tu asistente tiene una propuesta esperando"), 1)


class CuandoTest(unittest.TestCase):
    """BODY-HOY-LO-QUE-VENCE-1.W ruling 2: the strip's own relative-time words -- hoy/mañana/ayer,
    otherwise the n-days form, never an absolute date however far ahead `dia` falls (the gap
    `_dia_relativo` itself leaves, since it only ever looks backward)."""

    def test_today(self):
        self.assertEqual(vence.cuando(ES, HOY.isoformat(), HOY), "hoy")
        self.assertEqual(vence.cuando(EN, HOY.isoformat(), HOY), "today")

    def test_tomorrow(self):
        dia = (HOY + timedelta(days=1)).isoformat()
        self.assertEqual(vence.cuando(ES, dia, HOY), "mañana")
        self.assertEqual(vence.cuando(EN, dia, HOY), "tomorrow")

    def test_yesterday(self):
        dia = (HOY - timedelta(days=1)).isoformat()
        self.assertEqual(vence.cuando(ES, dia, HOY), "ayer")
        self.assertEqual(vence.cuando(EN, dia, HOY), "yesterday")

    def test_several_days_ahead_never_falls_back_to_an_absolute_date(self):
        dia = (HOY + timedelta(days=5)).isoformat()
        self.assertEqual(vence.cuando(ES, dia, HOY), "en 5 días")
        self.assertEqual(vence.cuando(EN, dia, HOY), "in 5 days")

    def test_several_days_overdue_never_falls_back_to_an_absolute_date(self):
        dia = (HOY - timedelta(days=5)).isoformat()
        self.assertEqual(vence.cuando(ES, dia, HOY), "hace 5 días")
        self.assertEqual(vence.cuando(EN, dia, HOY), "5 days ago")


class VenceTiempoCoberturaTest(unittest.TestCase):
    """BODY-HOY-LO-QUE-VENCE-PULIDO-2 ruling 1: a cobertura line's own `cadencia` decides whether
    it carries a relative-time word at all -- a daily stream's words already say "hoy"/"today"
    (vence.linea.cobertura_hoy), so the day is never said twice. A weekly cobertura line, and
    every other line kind due today, keep `vence.cuando` exactly as before (ruling 2/3)."""

    def test_daily_cobertura_line_has_no_tiempo(self):
        linea = {"tipo": "cobertura", "dia": HOY.isoformat(), "datos": {"stream": "comidas", "cadencia": "daily"}}
        self.assertEqual(hoy._vence_tiempo(ES, linea, HOY), "")
        self.assertEqual(hoy._vence_tiempo(EN, linea, HOY), "")

    def test_weekly_cobertura_line_keeps_its_relative_time(self):
        dia = (HOY + timedelta(days=3)).isoformat()
        linea = {"tipo": "cobertura", "dia": dia, "datos": {"stream": "peso", "cadencia": "weekly"}}
        self.assertEqual(hoy._vence_tiempo(ES, linea, HOY), "en 3 días")
        self.assertEqual(hoy._vence_tiempo(EN, linea, HOY), "in 3 days")

    def test_non_cobertura_line_due_today_still_shows_hoy(self):
        linea = {"tipo": "deadline", "dia": HOY.isoformat(), "datos": {"titulo": "ZZ"}}
        self.assertEqual(hoy._vence_tiempo(ES, linea, HOY), "hoy")
        self.assertEqual(hoy._vence_tiempo(EN, linea, HOY), "today")


class OverdueMarkupTest(unittest.TestCase):
    """BODY-HOY-LO-QUE-VENCE-PULIDO-1 ruling 1/4: the overdue line's own <li> renders with the
    padded `vence-vencido` class (body.css gives it the inner left padding the mark needs), never
    a bare overdue row with no padding for the mark to sit beside."""

    def setUp(self):
        self.orig = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "overdue_markup.db"
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1

    def test_overdue_li_carries_the_vence_vencido_class(self):
        _metric_goal(self.conn, 1, (HOY - timedelta(days=2)).isoformat(), "ZZ vencida")
        db.set_metric(self.conn, HOY.isoformat(), "resting_hr", 60.0, "garmin", 1)
        self.conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=HOY):
            page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn('class="vence-linea vence-vencido"', page)


if __name__ == "__main__":
    unittest.main()
