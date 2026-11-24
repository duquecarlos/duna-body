"""BODY-EXPERIMENTOS-INTRADIA-DIAS-FALTANTES-1: body/pull.py's own wiring of the on-demand
intraday fetch (pull.traer_intradia) into the scheduled pull -- pull._pull_intradia_pendientes,
and its call sites in the local default/--days path (pull._run, via pull.main) and the hosted
daily sweep (pull.pull_hosted). experimentos.dias_intradia_necesarios' own logic (which days,
newest first, Garmin's reach, already-stored skip) is tested in
tests/test_pull_intradia_experimentos.py; here it is mocked where the test only cares about the
wiring, and left real where a test's whole point is "no active experiment, no call".

BODY-ENTRENO-DETALLE-1 ruling 5 widens the candidate list: pull._dias_intradia_necesarios_todos
unions those same experiment days with, once privacidad.guarda_detalle agrees for the person,
every day in Garmin's intraday reach not yet stored. _pull_intradia_pendientes itself is
UNCHANGED (still just caps and fetches whatever list it is given) -- its own tests below now
mock _dias_intradia_necesarios_todos directly instead of experimentos.dias_intradia_necesarios,
decoupling them from the union logic the same way they were already decoupled from
experimentos.py's own logic; DiasIntradiaNecesariosTodosTest covers the union itself. Ruling 4
adds a second, independent call (pull._pull_detalle_pendientes, over pull.traer_detalle_
actividad) fetching each still-missing activity's full detail -- tested in its own section below.
Every local/hosted wiring test that exercises the implicit "today" sweep now pins
privacidad.guarda_detalle explicitly, since its real default (True whenever cuenta.hosted() is
False, which every test here leaves unset) would otherwise widen or shrink what each test's
pre-BODY-ENTRENO-DETALLE-1 assertions expected. No network; no real health figures, invented
demo data only."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter, garmin_cuenta, pull

TOKEN_KEY_HEX = "44" * 32


def _empty_fetch_day(day, token_str=None):
    data = garmin_adapter.DayData(day=day)
    return data if token_str is None else (data, token_str)


class PullIntradiaPendientesTest(unittest.TestCase):
    """Unit-level coverage of the helper itself, decoupled from _dias_intradia_necesarios_todos'
    own logic (mocked here -- itself covered by DiasIntradiaNecesariosTodosTest below) and from
    the local/hosted call sites (tested further below)."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "helper.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.person_id = db.local_person_id(self.conn)

    def test_no_pending_days_makes_no_call_and_returns_zero(self):
        with mock.patch("body.pull._dias_intradia_necesarios_todos", return_value=[]), \
             mock.patch("body.pull.traer_intradia") as m_traer:
            fetched = pull._pull_intradia_pendientes(self.conn, self.person_id, date(2026, 11, 9))
        self.assertEqual(fetched, 0)
        m_traer.assert_not_called()

    def test_fetches_up_to_the_cap_in_the_given_newest_first_order(self):
        dias = [f"2026-11-{d:02d}" for d in range(20, 1, -1)]  # 19 candidate days, newest first
        visitados = []
        with mock.patch("body.pull._dias_intradia_necesarios_todos", return_value=dias), \
             mock.patch("body.pull.traer_intradia", side_effect=lambda c, p, d: visitados.append(d)):
            fetched = pull._pull_intradia_pendientes(self.conn, self.person_id, date(2026, 11, 20))
        self.assertEqual(fetched, pull.INTRADIA_MAX_POR_CORRIDA)
        self.assertEqual(visitados, dias[:pull.INTRADIA_MAX_POR_CORRIDA])
        self.assertLess(pull.INTRADIA_MAX_POR_CORRIDA, len(dias))  # the cap actually bound this run

    def test_a_fetch_error_on_one_day_leaves_the_others_fetched_and_the_run_successful(self):
        dias = ["2026-11-03", "2026-11-02", "2026-11-01"]

        def _traer(c, p, d):
            if d == "2026-11-02":
                raise RuntimeError("boom")

        with mock.patch("body.pull._dias_intradia_necesarios_todos", return_value=dias), \
             mock.patch("body.pull.traer_intradia", side_effect=_traer):
            fetched = pull._pull_intradia_pendientes(self.conn, self.person_id, date(2026, 11, 3))
        self.assertEqual(fetched, 2)

    def test_hosted_token_path_persists_a_refreshed_token(self):
        with mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}), \
             mock.patch("body.pull._dias_intradia_necesarios_todos", return_value=["2026-11-05"]), \
             mock.patch("body.pull.traer_intradia", return_value=("fetched", "tok-after")) as m_traer:
            fetched = pull._pull_intradia_pendientes(
                self.conn, self.person_id, date(2026, 11, 5), token_str="tok-before")
            self.assertEqual(fetched, 1)
            m_traer.assert_called_once_with(self.conn, self.person_id, "2026-11-05", token_str="tok-before")
            self.assertEqual(garmin_cuenta.leer(self.conn, self.person_id), "tok-after")

    def test_local_path_never_touches_garmin_cuenta(self):
        with mock.patch("body.pull._dias_intradia_necesarios_todos", return_value=["2026-11-05"]), \
             mock.patch("body.pull.traer_intradia", return_value="fetched"), \
             mock.patch("body.pull.garmin_cuenta.guardar") as m_guardar:
            pull._pull_intradia_pendientes(self.conn, self.person_id, date(2026, 11, 5))
        m_guardar.assert_not_called()


class DiasIntradiaNecesariosTodosTest(unittest.TestCase):
    """BODY-ENTRENO-DETALLE-1 ruling 5: the union itself -- experimentos.dias_intradia_
    necesarios' own days (mocked, its own logic is tested elsewhere) plus, only when
    privacidad.guarda_detalle agrees, every day in reach not yet db.intradia_almacenado."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "union.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.person_id = db.local_person_id(self.conn)

    def test_no_agreement_returns_exactly_the_experiment_days(self):
        with mock.patch("body.pull.experimentos.dias_intradia_necesarios",
                         return_value=["2026-11-03", "2026-11-01"]), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=False):
            dias = pull._dias_intradia_necesarios_todos(self.conn, self.person_id, date(2026, 11, 9))
        self.assertEqual(dias, ["2026-11-03", "2026-11-01"])

    def test_agreement_adds_every_day_in_reach_not_yet_stored(self):
        hoy = date(2026, 11, 9)
        with mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=[]), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=True):
            dias = pull._dias_intradia_necesarios_todos(self.conn, self.person_id, hoy)
        esperado = [(hoy - timedelta(d)).isoformat() for d in range(1, pull.INTRADIA_ALCANCE_DIAS + 1)]
        self.assertEqual(dias, esperado)

    def test_a_day_already_stored_complete_is_left_out(self):
        hoy = date(2026, 11, 9)
        ayer = (hoy - timedelta(1)).isoformat()
        db.guardar_intradia(self.conn, self.person_id, ayer, "intradia_fc", {"heartRateValues": []})
        db.guardar_intradia(self.conn, self.person_id, ayer, "intradia_estres", {"stressValuesArray": []})
        self.conn.execute(
            "UPDATE raw_payload SET fetched_at = ? WHERE person_id = ? AND day = ? AND kind = 'intradia_fc'",
            ("2026-11-10T12:00:00+00:00", self.person_id, ayer),
        )
        self.conn.commit()
        with mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=[]), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=True):
            dias = pull._dias_intradia_necesarios_todos(self.conn, self.person_id, hoy)
        self.assertNotIn(ayer, dias)

    def test_an_experiment_day_is_never_duplicated(self):
        hoy = date(2026, 11, 9)
        ayer = (hoy - timedelta(1)).isoformat()
        with mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=[ayer]), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=True):
            dias = pull._dias_intradia_necesarios_todos(self.conn, self.person_id, hoy)
        self.assertEqual(dias.count(ayer), 1)

    def test_newest_first_overall_not_only_within_each_part(self):
        hoy = date(2026, 11, 9)
        antiguo = (hoy - timedelta(days=pull.INTRADIA_ALCANCE_DIAS - 5)).isoformat()  # older than "ayer"
        with mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=[antiguo]), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=True):
            dias = pull._dias_intradia_necesarios_todos(self.conn, self.person_id, hoy)
        self.assertEqual(dias, sorted(dias, reverse=True))
        self.assertIn(antiguo, dias)


class LocalScheduledPullWiringTest(unittest.TestCase):
    """pull.main / pull._run -- the local default/--days path (manana's own --days 1, and the
    plain scheduled daily sync's default --days 2)."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "local.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_default_pull_fetches_the_days_experimentos_says_are_needed(self):
        person_id = db.local_person_id(self.conn)
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=False), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=["2026-11-08"]), \
             mock.patch("body.pull.traer_intradia") as m_traer:
            codigo = pull.main([])
        self.assertEqual(codigo, 0)
        m_traer.assert_called_once_with(self.conn, person_id, "2026-11-08")

    def test_explicit_date_never_triggers_an_intraday_fetch(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios") as m_needed, \
             mock.patch("body.pull.traer_intradia") as m_traer:
            codigo = pull.main(["--date", "2026-11-05"])
        self.assertEqual(codigo, 0)
        m_needed.assert_not_called()
        m_traer.assert_not_called()

    def test_no_active_experiment_and_no_agreement_makes_no_intraday_garmin_call(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_intraday") as m_intraday:
            codigo = pull.main([])
        self.assertEqual(codigo, 0)
        m_intraday.assert_not_called()

    def test_no_active_experiment_still_fetches_full_day_history_once_agreed(self):
        # BODY-ENTRENO-DETALLE-1 ruling 5: local mode's own default (privacidad.guarda_detalle is
        # True whenever cuenta.hosted() is False, left unset by this test) -- "yes, everything,
        # man." applies even with no active experiment.
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.traer_intradia") as m_traer:
            codigo = pull.main([])
        self.assertEqual(codigo, 0)
        self.assertEqual(m_traer.call_count, pull.INTRADIA_MAX_POR_CORRIDA)

    def test_detalle_pendientes_is_called_on_the_implicit_today_sweep(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull._pull_intradia_pendientes"), \
             mock.patch("body.pull._pull_detalle_pendientes") as m_detalle:
            codigo = pull.main([])
        self.assertEqual(codigo, 0)
        m_detalle.assert_called_once()

    def test_detalle_pendientes_is_not_called_on_an_explicit_date(self):
        with mock.patch("body.pull.db.connect", return_value=self.conn), \
             mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull._pull_detalle_pendientes") as m_detalle:
            codigo = pull.main(["--date", "2026-11-05"])
        self.assertEqual(codigo, 0)
        m_detalle.assert_not_called()


class HostedScheduledPullWiringTest(unittest.TestCase):
    """pull.pull_hosted -- the daily hosted sweep, per connected person."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "hosted.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        env_patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        sleep_patcher = mock.patch("body.pull.time.sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        # BODY-SYNC-VIVO-ESTADO-1 ruling 4: _pull_one_person now also calls this once per person;
        # this test's own concern is the intraday wiring, so it is stubbed to a no-op here rather
        # than reaching the real (unmocked) garminconnect.Garmin for these tokens.
        reloj_patcher = mock.patch(
            "body.pull.garmin_adapter.fetch_device_last_sync", side_effect=lambda token_str=None: (None, token_str))
        reloj_patcher.start()
        self.addCleanup(reloj_patcher.stop)
        garmin_cuenta.guardar(self.conn, 1, "tok-before")
        self.conn.commit()

    def test_implicit_today_sweep_fetches_pending_intraday_and_persists_the_refreshed_token(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=False), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios", return_value=["2026-11-08"]), \
             mock.patch("body.pull.traer_intradia", return_value=("fetched", "tok-after")) as m_traer:
            ok, failed, saved = pull.pull_hosted(self.conn)
        self.assertEqual((ok, failed), (1, 0))
        m_traer.assert_called_once_with(self.conn, 1, "2026-11-08", token_str="tok-before")
        self.assertEqual(garmin_cuenta.leer(self.conn, 1), "tok-after")

    def test_explicit_fecha_never_triggers_an_intraday_fetch(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.experimentos.dias_intradia_necesarios") as m_needed:
            pull.pull_hosted(self.conn, fecha="2026-11-09")
        m_needed.assert_not_called()

    def test_no_active_experiment_and_no_agreement_makes_no_intraday_garmin_call(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=False), \
             mock.patch("body.pull.garmin_adapter.fetch_intraday") as m_intraday:
            pull.pull_hosted(self.conn)
        m_intraday.assert_not_called()

    def test_no_active_experiment_still_fetches_full_day_history_once_agreed(self):
        # BODY-ENTRENO-DETALLE-1 ruling 5: a hosted person who HAS accepted consentimiento-v4
        # (guarda_detalle mocked True here -- the real consent-row path is covered in
        # tests/test_consentimiento_v4.py) gets every day in reach too, not only an active
        # experiment's own window.
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.traer_intradia", return_value=("fetched", "tok-before")) as m_traer:
            pull.pull_hosted(self.conn)
        self.assertEqual(m_traer.call_count, pull.INTRADIA_MAX_POR_CORRIDA)

    def test_detalle_pendientes_is_called_on_the_implicit_today_sweep(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull._pull_intradia_pendientes"), \
             mock.patch("body.pull._pull_detalle_pendientes") as m_detalle:
            pull.pull_hosted(self.conn)
        m_detalle.assert_called_once_with(self.conn, 1, mock.ANY, token_str="tok-before")

    def test_detalle_pendientes_is_not_called_on_an_explicit_fecha(self):
        with mock.patch("body.pull.garmin_adapter.fetch_day", side_effect=_empty_fetch_day), \
             mock.patch("body.pull._pull_detalle_pendientes") as m_detalle:
            pull.pull_hosted(self.conn, fecha="2026-11-09")
        m_detalle.assert_not_called()


class PullDetallePendientesTest(unittest.TestCase):
    """Unit-level coverage of _pull_detalle_pendientes itself, decoupled from db.actividades_
    sin_detalle's own logic (mocked here) and from the local/hosted call sites (tested above)."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "detalle_helper.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.person_id = db.local_person_id(self.conn)

    def test_no_agreement_makes_no_call_and_returns_zero(self):
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=False), \
             mock.patch("body.pull.db.actividades_sin_detalle") as m_pendientes, \
             mock.patch("body.pull.traer_detalle_actividad") as m_traer:
            fetched = pull._pull_detalle_pendientes(self.conn, self.person_id, date(2026, 11, 9))
        self.assertEqual(fetched, 0)
        m_pendientes.assert_not_called()
        m_traer.assert_not_called()

    def test_no_pending_activities_makes_no_call_and_returns_zero(self):
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.db.actividades_sin_detalle", return_value=[]), \
             mock.patch("body.pull.traer_detalle_actividad") as m_traer:
            fetched = pull._pull_detalle_pendientes(self.conn, self.person_id, date(2026, 11, 9))
        self.assertEqual(fetched, 0)
        m_traer.assert_not_called()

    def test_fetches_up_to_the_cap_in_the_given_newest_first_order(self):
        actividades = [f"a{n}" for n in range(15, 0, -1)]  # 15 candidates, newest first
        visitadas = []
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.db.actividades_sin_detalle", return_value=actividades), \
             mock.patch("body.pull.traer_detalle_actividad", side_effect=lambda c, p, s: visitadas.append(s)):
            fetched = pull._pull_detalle_pendientes(self.conn, self.person_id, date(2026, 11, 9))
        self.assertEqual(fetched, pull.DETALLE_MAX_POR_CORRIDA)
        self.assertEqual(visitadas, actividades[:pull.DETALLE_MAX_POR_CORRIDA])
        self.assertLess(pull.DETALLE_MAX_POR_CORRIDA, len(actividades))  # the cap actually bound this run

    def test_a_fetch_error_on_one_activity_leaves_the_others_fetched_and_the_run_successful(self):
        actividades = ["a3", "a2", "a1"]

        def _traer(c, p, s):
            if s == "a2":
                raise RuntimeError("boom")

        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.db.actividades_sin_detalle", return_value=actividades), \
             mock.patch("body.pull.traer_detalle_actividad", side_effect=_traer):
            fetched = pull._pull_detalle_pendientes(self.conn, self.person_id, date(2026, 11, 9))
        self.assertEqual(fetched, 2)

    def test_hosted_token_path_persists_a_refreshed_token(self):
        with mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.db.actividades_sin_detalle", return_value=["a1"]), \
             mock.patch("body.pull.traer_detalle_actividad", return_value=(3, {}, "tok-after")) as m_traer:
            fetched = pull._pull_detalle_pendientes(
                self.conn, self.person_id, date(2026, 11, 9), token_str="tok-before")
            self.assertEqual(fetched, 1)
            m_traer.assert_called_once_with(self.conn, self.person_id, "a1", token_str="tok-before")
            self.assertEqual(garmin_cuenta.leer(self.conn, self.person_id), "tok-after")

    def test_local_path_never_touches_garmin_cuenta(self):
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.db.actividades_sin_detalle", return_value=["a1"]), \
             mock.patch("body.pull.traer_detalle_actividad", return_value=3), \
             mock.patch("body.pull.garmin_cuenta.guardar") as m_guardar:
            pull._pull_detalle_pendientes(self.conn, self.person_id, date(2026, 11, 9))
        m_guardar.assert_not_called()


class TraerDetalleActividadTest(unittest.TestCase):
    """pull.py's on-demand traer_detalle_actividad: calls garmin_adapter.fetch_activity_detail
    and saves each kind actually returned with db.guardar_detalle_actividad. BODY-DETALLE-
    HISTORIA-1 ruling 2 widens the return with a `fallidas` dict ({class name: count} for a kind
    whose own error was not a 404), in the middle of the tuple so a hosted caller's own
    `token_after` stays its last element."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "traer_detalle.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (1, 'a1', '2026-11-09', '2026-11-09T07:00:00', 'running', NULL, 'garmin')"
        )
        self.conn.commit()

    def test_local_mode_stores_every_kind_returned_and_returns_the_count(self):
        data = garmin_adapter.ActivityDetailData(
            activity_id="a1", raw={"detalle": {"v": 1}, "vueltas": {"v": 2}, "zonas": [{"v": 3}]})
        with mock.patch("body.pull.garmin_adapter.fetch_activity_detail", return_value=data) as m:
            n, fallidas = pull.traer_detalle_actividad(self.conn, 1, "a1")
        m.assert_called_once_with("a1")
        self.assertEqual(n, 3)
        self.assertEqual(fallidas, {})
        self.assertTrue(db.detalle_actividad_almacenado(self.conn, 1, "garmin", "a1"))

    def test_a_transient_errors_kind_is_not_stored_and_is_counted_as_fallida(self):
        data = garmin_adapter.ActivityDetailData(
            activity_id="a1", raw={"vueltas": {}, "zonas": [], "_errors": {"detalle": "RuntimeError"}})
        with mock.patch("body.pull.garmin_adapter.fetch_activity_detail", return_value=data):
            n, fallidas = pull.traer_detalle_actividad(self.conn, 1, "a1")
        self.assertEqual(n, 2)
        self.assertEqual(fallidas, {"RuntimeError": 1})
        self.assertFalse(db.detalle_actividad_almacenado(self.conn, 1, "garmin", "a1"))

    def test_a_404_kind_is_stored_as_the_empty_marker_and_not_counted_as_fallida(self):
        """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1-2."""
        data = garmin_adapter.ActivityDetailData(
            activity_id="a1",
            raw={"vueltas": {}, "zonas": [],
                 "_errors": {"detalle": "GarminConnectNotFoundError"}})
        with mock.patch("body.pull.garmin_adapter.fetch_activity_detail", return_value=data):
            n, fallidas = pull.traer_detalle_actividad(self.conn, 1, "a1")
        self.assertEqual(n, 3)
        self.assertEqual(fallidas, {})
        self.assertTrue(db.detalle_actividad_almacenado(self.conn, 1, "garmin", "a1"))
        stored = self.conn.execute(
            "SELECT payload FROM activity_detail WHERE person_id = 1 AND activity_source_id = 'a1' "
            "AND kind = 'detalle'"
        ).fetchone()
        self.assertEqual(stored["payload"], "{}")

    def test_hosted_token_path_returns_the_count_fallidas_and_the_refreshed_token(self):
        data = garmin_adapter.ActivityDetailData(activity_id="a1", raw={"detalle": {}, "vueltas": {}, "zonas": []})
        with mock.patch("body.pull.garmin_adapter.fetch_activity_detail",
                         return_value=(data, "tok-after")) as m:
            n, fallidas, token_after = pull.traer_detalle_actividad(self.conn, 1, "a1", token_str="tok-before")
        m.assert_called_once_with("a1", token_str="tok-before")
        self.assertEqual((n, fallidas, token_after), (3, {}, "tok-after"))


class TraerDetalleHistoriaTest(unittest.TestCase):
    """BODY-DETALLE-HISTORIA-1 ruling 3: the manual, paced, resumable backfill over the WHOLE
    history, in priority order (basketball, soccer, strength, everything else -- newest first
    within each group). No network: traer_detalle_actividad is mocked throughout."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "historia.db")
        db.init(self.conn)
        self.addCleanup(self.conn.close)
        self.person_id = db.local_person_id(self.conn)

    def _insertar(self, source_id, day, tipo, nombre=None):
        self.conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'garmin')",
            (self.person_id, source_id, day, f"{day}T07:00:00", tipo, nombre),
        )
        self.conn.commit()

    def test_no_agreement_makes_no_garmin_call(self):
        self._insertar("a1", "2026-11-01", "running")
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=False), \
             mock.patch("body.pull.traer_detalle_actividad") as m_traer:
            resultado = pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok", max_actividades=10)
        m_traer.assert_not_called()
        self.assertEqual(resultado["detenida_por"], "sin_consentimiento")
        self.assertEqual(resultado["resueltas"], 0)

    def test_no_pending_activities_returns_immediately(self):
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.traer_detalle_actividad") as m_traer:
            resultado = pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok", max_actividades=10)
        m_traer.assert_not_called()
        self.assertEqual(
            resultado, {"resueltas": 0, "restantes": 0, "fallidas_transitorias": {}, "detenida_por": None}
        )

    def test_lock_held_refuses_with_counts_only_and_makes_no_call(self):
        self._insertar("a1", "2026-11-01", "running")
        held = pull._try_lock()
        self.addCleanup(pull._unlock, held)
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.LOCK_ESPERA_S", 0.1), \
             mock.patch("body.pull.LOCK_SONDEO_S", 0.05), \
             mock.patch("body.pull.traer_detalle_actividad") as m_traer:
            resultado = pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok", max_actividades=10)
        m_traer.assert_not_called()
        self.assertEqual(resultado["detenida_por"], "bloqueo")
        self.assertEqual(resultado["restantes"], 1)

    def test_priority_order_basketball_soccer_strength_rest_newest_first_within_each(self):
        self._insertar("r2", "2026-11-05", "running")
        self._insertar("r1", "2026-11-01", "running")
        self._insertar("s2", "2026-11-06", "strength_training")
        self._insertar("s1", "2026-11-02", "strength_training")
        self._insertar("f2", "2026-11-07", "other", "football")
        self._insertar("f1", "2026-11-03", "other", "futbol")
        self._insertar("b2", "2026-11-08", "basketball")
        self._insertar("b1", "2026-11-04", "other", "baloncesto")
        visitadas = []

        def _traer(c, p, s, token_str=None):
            visitadas.append(s)
            return 3, {}, token_str

        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.traer_detalle_actividad", side_effect=_traer):
            pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok", max_actividades=100, pausa_s=0)
        self.assertEqual(visitadas, ["b2", "b1", "f2", "f1", "s2", "s1", "r2", "r1"])

    def test_max_cap_limits_how_many_are_resolved(self):
        for i in range(5):
            self._insertar(f"a{i}", f"2026-11-0{i + 1}", "running")
        visitadas = []

        def _traer(c, p, s, token_str=None):
            visitadas.append(s)
            return 3, {}, token_str

        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.traer_detalle_actividad", side_effect=_traer):
            resultado = pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok", max_actividades=2, pausa_s=0)
        self.assertEqual(len(visitadas), 2)
        self.assertEqual(resultado["resueltas"], 2)
        self.assertEqual(resultado["restantes"], 3)

    def test_pause_is_called_between_activities_but_not_before_the_first(self):
        self._insertar("a2", "2026-11-02", "running")
        self._insertar("a1", "2026-11-01", "running")
        sleeps = []
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.traer_detalle_actividad", return_value=(3, {}, "tok")), \
             mock.patch("body.pull.time.sleep", side_effect=sleeps.append):
            pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok", max_actividades=10, pausa_s=0.75)
        self.assertEqual(sleeps, [0.75])

    def test_stops_at_the_first_transient_failure_with_counts_so_far(self):
        self._insertar("a2", "2026-11-02", "running")
        self._insertar("a1", "2026-11-01", "running")
        calls = []

        def _traer(c, p, s, token_str=None):
            calls.append(s)
            if s == "a2":
                return 3, {}, token_str
            return 0, {"GarminConnectTooManyRequestsError": 1}, token_str

        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.traer_detalle_actividad", side_effect=_traer):
            resultado = pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok", max_actividades=10, pausa_s=0)
        self.assertEqual(calls, ["a2", "a1"])
        self.assertEqual(resultado["resueltas"], 1)
        self.assertEqual(resultado["detenida_por"], "transitorio")
        self.assertEqual(resultado["fallidas_transitorias"], {"GarminConnectTooManyRequestsError": 1})
        self.assertEqual(resultado["restantes"], 1)

    def test_stops_on_not_signed_in(self):
        self._insertar("a1", "2026-11-01", "running")
        with mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.traer_detalle_actividad",
                         side_effect=garmin_adapter.NotSignedIn("token rejected")):
            resultado = pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok", max_actividades=10, pausa_s=0)
        self.assertEqual(resultado["detenida_por"], "sin_firma")
        self.assertEqual(resultado["resueltas"], 0)

    def test_token_persisted_when_refreshed(self):
        self._insertar("a1", "2026-11-01", "running")
        with mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: TOKEN_KEY_HEX}), \
             mock.patch("body.pull.privacidad.guarda_detalle", return_value=True), \
             mock.patch("body.pull.traer_detalle_actividad", return_value=(3, {}, "tok-after")):
            pull.traer_detalle_historia(
                self.conn, self.person_id, token_str="tok-before", max_actividades=10, pausa_s=0)
            self.assertEqual(garmin_cuenta.leer(self.conn, self.person_id), "tok-after")


class ActualizarIntradiaSiCorrespondeTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-2 ruling 2: leer_intradia's own on-demand refresh. `body.pull.fechas.
    hoy_local` is pinned so "today"/"yesterday"/"older" are fixed facts, never the real wall clock;
    `body.pull.db.connect` is mocked to open a FRESH connection to this test's own db file on every
    call (never the same object twice), matching the function's own `db.connect()`/`finally:
    conn.close()` in real use -- a test calling it twice (the 30-minute guard tests) must not be
    handed back a connection the first call already closed."""

    HOY = date(2026, 11, 18)

    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp(dir=TMP)) / "frescura.db"
        self.conn = open_conn(self, self.db_path)
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        pull._ULTIMO_INTENTO_FRESCURA_INTRADIA.clear()
        self.addCleanup(pull._ULTIMO_INTENTO_FRESCURA_INTRADIA.clear)
        # A fresh connection per call, like the function's own `db.connect()` in real use --
        # `actualizar_intradia_si_corresponde` closes what it opens, so a test calling it more
        # than once (the 30-minute guard tests) must not be handed back an already-closed one.
        # `db.connect` captured BEFORE patching: "body.pull.db" and "body.db" are the same module
        # object, so patching "body.pull.db.connect" replaces body.db.connect globally too.
        conectar_real = db.connect
        patcher = mock.patch("body.pull.db.connect", side_effect=lambda: conectar_real(self.db_path))
        patcher.start()
        self.addCleanup(patcher.stop)
        hoy_patcher = mock.patch("body.pull.fechas.hoy_local", return_value=self.HOY)
        hoy_patcher.start()
        self.addCleanup(hoy_patcher.stop)

    def test_todays_missing_day_triggers_exactly_one_forced_fetch(self):
        dia = self.HOY.isoformat()
        with mock.patch("body.pull.traer_intradia") as m_traer:
            fallo = pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertFalse(fallo)
        m_traer.assert_called_once_with(mock.ANY, self.person_id, dia, forzar=True)

    def test_a_second_call_within_30_minutes_does_not_fetch_again(self):
        dia = self.HOY.isoformat()
        with mock.patch("body.pull.traer_intradia") as m_traer:
            pull.actualizar_intradia_si_corresponde(self.person_id, dia)
            fallo = pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertFalse(fallo)
        self.assertEqual(m_traer.call_count, 1)

    def test_a_call_past_the_30_minute_guard_fetches_again(self):
        dia = self.HOY.isoformat()
        with mock.patch("body.pull.traer_intradia") as m_traer:
            pull.actualizar_intradia_si_corresponde(self.person_id, dia)
            clave = (self.person_id, dia)
            pull._ULTIMO_INTENTO_FRESCURA_INTRADIA[clave] -= timedelta(minutes=31)
            pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertEqual(m_traer.call_count, 2)

    def test_a_complete_stored_day_never_fetches(self):
        dia = self.HOY.isoformat()
        with mock.patch("body.pull.db.intradia_almacenado", return_value=True), \
             mock.patch("body.pull.traer_intradia") as m_traer:
            fallo = pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertFalse(fallo)
        m_traer.assert_not_called()

    def test_yesterday_missing_also_triggers_a_fetch(self):
        dia = (self.HOY - timedelta(days=1)).isoformat()
        with mock.patch("body.pull.traer_intradia") as m_traer:
            pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        m_traer.assert_called_once_with(mock.ANY, self.person_id, dia, forzar=True)

    def test_a_day_older_than_yesterday_never_fetches(self):
        dia = (self.HOY - timedelta(days=2)).isoformat()
        with mock.patch("body.pull.traer_intradia") as m_traer:
            fallo = pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertFalse(fallo)
        m_traer.assert_not_called()

    def test_a_future_day_never_fetches(self):
        dia = (self.HOY + timedelta(days=1)).isoformat()
        with mock.patch("body.pull.traer_intradia") as m_traer:
            fallo = pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertFalse(fallo)
        m_traer.assert_not_called()

    def test_a_failing_fetch_reports_true_and_never_raises(self):
        dia = self.HOY.isoformat()
        with mock.patch("body.pull.traer_intradia", side_effect=garmin_adapter.NotSignedIn("no token")):
            fallo = pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertTrue(fallo)

    def test_hosted_path_uses_the_persons_own_token_and_persists_a_refreshed_one(self):
        dia = self.HOY.isoformat()
        with mock.patch("body.pull.cuenta.hosted", return_value=True), \
             mock.patch("body.pull.garmin_cuenta.leer", return_value="tok-before"), \
             mock.patch("body.pull.traer_intradia", return_value=("fetched", "tok-after")) as m_traer, \
             mock.patch("body.pull.garmin_cuenta.guardar") as m_guardar:
            fallo = pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertFalse(fallo)
        m_traer.assert_called_once_with(mock.ANY, self.person_id, dia, token_str="tok-before", forzar=True)
        m_guardar.assert_called_once_with(mock.ANY, self.person_id, "tok-after")

    def test_hosted_path_with_no_stored_token_is_refused_without_a_garmin_call(self):
        dia = self.HOY.isoformat()
        with mock.patch("body.pull.cuenta.hosted", return_value=True), \
             mock.patch("body.pull.garmin_cuenta.leer", return_value=None), \
             mock.patch("body.pull.traer_intradia") as m_traer:
            fallo = pull.actualizar_intradia_si_corresponde(self.person_id, dia)
        self.assertTrue(fallo)
        m_traer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
