"""BODY-ESTADOS-VACIOS-2: the three empty-state follow-ups BODY-ESTADOS-VACIOS-1 reported wording
or layout for, rather than a crash or a raw-value leak -- Hoy's stale-data flag, Turnos' dropped
cost table when every row is empty, and Metas' zero-points chart caption.

Reuses tests/test_estados_vacios.py's state builders (seed_B, seed_E, seed_F, _reset_db,
_mk_person, _full_day, TODAY) rather than re-deriving them, and adds the handful of new shapes this
slice's three checks need: one day short of "stale" (dias=1), a shifts state with one metric
present (the partial-data case the hidden-table rule must NOT hide), and a metric goal with real
weekly readings (the chart_hint case the zero-points caption rule must NOT touch).

BODY-ESTADOS-VACIOS-2-M (a follow-up slice) moved the all-empty cost-table case to
tests/test_turnos_enlaces.py::CostRowNoDataTest, which also owns _seed_D's import now; this
module's own _seed_turnos_parcial stays here and is reused from there.

BODY-ESTADOS-VACIOS-2-M2 (a second follow-up, from the orchestrator's own 375 px browser
measurement of this slice's work) restyles the stale line as the `.aviso` flag, drops its link in
local mode (no local page shows the sync) and relabels it to /garmin in hosted mode, and makes the
goal chart's legend omit "Tu promedio semanal" when zero weekly points are actually plotted.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, cuenta, db, demo, fechas, garmin_cuenta, goals, identity, web
from body.i18n import T
from body.rutas import comun
from tests.test_estados_vacios import TODAY, _full_day, _mk_person, _reset_db, _seed_B, _seed_E, _seed_F

# Hosted-mode env, the same three variables tests/test_cuenta_rp.py's CuentaRpTestCase patches.
_HOSTED_ENV = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-test-only", garmin_cuenta.KEY_ENV: "11" * 32}


def _aviso_html(page_text: str) -> str:
    start = page_text.index('<div class="aviso">')
    return page_text[start:page_text.index("</div>", start)]


client = TestClient(web.app, follow_redirects=False)


def _use(conn, person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = (lambda pid=person_id: pid)


def _clear(tc: unittest.TestCase) -> None:
    tc.addCleanup(web.app.dependency_overrides.clear)


# ---- one day short of "stale" (dias=1): yesterday's night, not yet synced today -----------------
def _seed_dias_1(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "G1")
    day = (TODAY - timedelta(1)).isoformat()
    _full_day(conn, pid, day)
    conn.commit()
    return {"person_id": pid, "day": day}


# ---- Turnos, one metric present: the partial-data case the hidden-table rule must keep -----------
_PARTIAL_METRIC = "resting_hr"


def _seed_turnos_parcial(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "H")
    db.set_trabaja_turnos(conn, True, pid)
    night_fin = TODAY - timedelta(13)
    for i in range(3):
        d = (night_fin - timedelta(2 - i)).isoformat()
        nd = (night_fin - timedelta(1 - i)).isoformat()
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source) "
            "VALUES (?, ?, 'night', 'N', ?, ?, 'manual')",
            (pid, d, f"{d}T22:00:00", f"{nd}T06:00:00"))
    # The block's own cost window (inicio..fin+dias_estables=2): one metric, every other left unset.
    for i in range(5):
        d = (night_fin - timedelta(2) + timedelta(i)).isoformat()
        db.set_metric(conn, d, _PARTIAL_METRIC, 60.0 + i, "garmin", pid)
    # The reference window (>= min_dias_referencia=10), well outside the block's own excluded span.
    for i in range(12):
        d = (TODAY - timedelta(100 + i)).isoformat()
        db.set_metric(conn, d, _PARTIAL_METRIC, 58.0 + i * 0.1, "garmin", pid)
    conn.commit()
    return {"person_id": pid}


# ---- a metric goal with real weekly readings: the chart_hint case the caption rule must keep -----
def _seed_meta_con_lecturas(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "I")
    today_iso = TODAY.isoformat()
    deadline = (TODAY + timedelta(60)).isoformat()
    gid = goals.propose(conn, pid, "ZZI-METRIC", today_iso, kind="metric", metric_key="vo2max",
                        direction="raise", target_value=50.0, baseline_value=40.0, deadline=deadline)
    goals.activate(conn, gid)
    monday = fechas.lunes_de(TODAY)
    for i in range(4):
        db.set_metric(conn, (monday + timedelta(i)).isoformat(), "vo2max", 42.0 + i, "garmin", pid)
    conn.commit()
    return {"person_id": pid, "goal_id": gid}


class HoyStaleDataTest(unittest.TestCase):
    """BODY-HOY-STALE-DATA-1: the stale line and its n, in both languages; nothing for dias 0 or 1.
    BODY-ESTADOS-VACIOS-2-M2: the line is the `.aviso` flag, never the old muted footnote, and in
    local mode (no hosted env patched here) it carries no link at all -- /settings has no sync row
    for it to point at. The hosted link is covered separately, in StaleDataHostedLinkTest."""

    def setUp(self):
        self.conn = _reset_db(self)
        _clear(self)

    def test_stale_line_f_40_dias_es_y_en(self):
        last_day = TODAY - timedelta(40)
        for lang in ("es", "en"):
            ids = _seed_F(self.conn, lang)
            _use(self.conn, ids["person_id"])
            t = T(lang)
            expected_date = comun._short_date(t, last_day)
            expected = t("today.stale", date=expected_date, n=40)
            r = client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn('class="aviso"', r.text, f"{lang}: stale line must render as the .aviso flag")
            self.assertIn(expected, r.text, f"{lang}: stale line missing or wrong n for a 40-day-old day")

    def test_local_mode_aviso_has_no_link(self):
        ids = _seed_F(self.conn, "es")
        _use(self.conn, ids["person_id"])
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<a", _aviso_html(r.text),
                         "local mode has no page that shows the sync, so the aviso stands alone")

    def test_dias_0_no_stale_line(self):
        ids = _seed_B(self.conn, "es")
        _use(self.conn, ids["person_id"])
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('class="aviso"', r.text)

    def test_dias_1_no_stale_line(self):
        ids = _seed_dias_1(self.conn, "es")
        _use(self.conn, ids["person_id"])
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('class="aviso"', r.text)


class StaleDataHostedLinkTest(unittest.TestCase):
    """BODY-ESTADOS-VACIOS-2-M2: hosted mode's aviso links /garmin, the page that actually shows
    the connection, with the relabeled text -- never the old /settings guess."""

    def setUp(self):
        self.conn = _reset_db(self)
        _clear(self)
        patcher = mock.patch.dict(os.environ, _HOSTED_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(web.app, follow_redirects=False)

    def test_hosted_aviso_links_garmin_with_new_label(self):
        ids = _seed_F(self.conn, "es")
        _use(self.conn, ids["person_id"])
        t = T("es")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        aviso = _aviso_html(r.text)
        self.assertIn('<a class="taplink" href="/garmin">', aviso)
        self.assertIn(t("today.stale_link"), aviso)


class TurnosCostoVacioTest(unittest.TestCase):
    """BODY-SHIFTS-REDUNDANT-EMPTY-TABLE-1: the cost table drops entirely when every row is empty,
    and keeps rendering, partial figures and all, when at least one row has one. The all-empty case
    (cost_title absent, hero reason present) moved to tests/test_turnos_enlaces.py::CostRowNoDataTest
    ::test_all_empty_drops_the_table (BODY-ESTADOS-VACIOS-2-M), which keeps the same assertions and
    adds a check that no em-dash row survives -- that test is the stronger of the two, so it is not
    repeated here."""

    def setUp(self):
        self.conn = _reset_db(self)
        _clear(self)

    def test_parcial_una_fila_con_dato_mantiene_tabla(self):
        ids = _seed_turnos_parcial(self.conn, "es")
        _use(self.conn, ids["person_id"])
        r = client.get("/shifts")
        self.assertEqual(r.status_code, 200)
        t = T("es")
        self.assertIn(t("shifts.cost_title"), r.text)
        table = r.text[r.text.index('<table class="parts mt-8">'):r.text.index("</table>")]
        self.assertEqual(table.count('class="costrow"'), 1,
                         "exactly one metric (resting_hr) has a figure in this fixture")
        self.assertEqual(table.count(t("shifts.sin_dato_corto")), 4,
                         "the other four metrics stay honest gaps")


class GoalChartCaptionTest(unittest.TestCase):
    """BODY-GOAL-CHART-NO-CAPTION-1: chart_soon (not chart_hint) when the plan line has zero actual
    points; chart_hint stays once there is at least one.
    BODY-ESTADOS-VACIOS-2-M2: the legend follows the same rule -- "Tu promedio semanal" (goals.
    legend_avg) is omitted with zero points, present with at least one."""

    def setUp(self):
        self.conn = _reset_db(self)
        _clear(self)

    def test_cero_puntos_usa_chart_soon(self):
        ids = _seed_E(self.conn, "es")
        gid_metric = ids["goal_ids"][0]
        _use(self.conn, ids["person_id"])
        r = client.get(f"/goals/{gid_metric}")
        self.assertEqual(r.status_code, 200)
        t = T("es")
        self.assertIn(t("goals.chart_soon"), r.text)
        self.assertNotIn(t("goals.chart_hint"), r.text)
        self.assertNotIn(t("goals.legend_avg"), r.text, "no points plotted, so no legend entry for the average line")

    def test_con_puntos_usa_chart_hint(self):
        ids = _seed_meta_con_lecturas(self.conn, "es")
        _use(self.conn, ids["person_id"])
        r = client.get(f"/goals/{ids['goal_id']}")
        self.assertEqual(r.status_code, 200)
        t = T("es")
        self.assertIn(t("goals.chart_hint"), r.text)
        self.assertNotIn(t("goals.chart_soon"), r.text)
        self.assertIn(t("goals.legend_avg"), r.text, "real weekly points are plotted, so the legend names them")


class PersonaCompletaSinCambiosTest(unittest.TestCase):
    """None of the three follow-ups touches a fully populated person's own screens -- the new
    strings never appear, and the sections they could have hidden/changed stay exactly as before."""

    def setUp(self):
        self.conn = _reset_db(self)
        self.addCleanup(web.app.dependency_overrides.clear)

    def test_demo_person_unchanged(self):
        demo._seed(self.conn, dias=30, idioma="es")
        person_id = 1
        db.set_trabaja_turnos(self.conn, True, person_id)
        goal_id = self.conn.execute(
            "SELECT id FROM goal WHERE person_id = ? ORDER BY id LIMIT 1", (person_id,)).fetchone()[0]
        self.conn.commit()
        _use(self.conn, person_id)
        t = T("es")

        r_home = client.get("/")
        self.assertEqual(r_home.status_code, 200)
        self.assertNotIn(t("today.stale_link"), r_home.text)
        self.assertEqual(r_home.text.count('class="aviso"'), 1,
                         "a fully populated person sees no warning except the one real thing "
                         "covering today: demo's own open period (BODY-PERIODO-PANTALLAS-1-B)")
        self.assertIn(t("today.periodo_enlace"), r_home.text,
                     "the one aviso present is the period line, not a stale-data warning")

        r_shifts = client.get("/shifts")
        self.assertEqual(r_shifts.status_code, 200)
        self.assertIn(t("shifts.cost_title"), r_shifts.text,
                     "a populated person's cost table keeps rendering, same as before this slice")

        r_goal = client.get(f"/goals/{goal_id}")
        self.assertEqual(r_goal.status_code, 200)
        self.assertIn(t("goals.chart_hint"), r_goal.text,
                     "demo's own weekly checks give this goal real points -- chart_hint, not chart_soon")
        self.assertNotIn(t("goals.chart_soon"), r_goal.text)
        self.assertIn(t("goals.legend_avg"), r_goal.text, "real points plotted -- the legend keeps naming them")


if __name__ == "__main__":
    unittest.main()
