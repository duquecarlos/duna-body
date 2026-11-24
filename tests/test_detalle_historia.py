"""BODY-DETALLE-HISTORIA-1 ruling 4: body/detalle_historia.py's CLI -- resolves the person
(mirroring body/cobertura.py's own --cuenta-sub shape), prints a before/after coverage table
(priority group x calendar month, con_detalle/garmin_no_tiene/pendientes counts) and, on a live
run, this run's own counts and stop reason. No network: pull.traer_detalle_historia is mocked
throughout; every fixture activity below is invented demo data, never a real figure, a real
source_id or a real name."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, detalle_historia, garmin_adapter


def _insertar_actividad(conn, person_id, source_id, day, tipo, nombre=None):
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'garmin')",
        (person_id, source_id, day, f"{day}T07:00:00", tipo, nombre),
    )


class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "cli.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)
        _insertar_actividad(self.conn, self.person_id, "b1", "2026-10-15", "basketball")
        for kind in db.DETALLE_KINDS:
            db.guardar_detalle_actividad(
                self.conn, self.person_id, garmin_adapter.SOURCE, "b1", kind, {"v": 1})
        self.conn.commit()

    def test_makes_no_garmin_call(self):
        buf = io.StringIO()
        with mock.patch("body.detalle_historia.db.connect", return_value=self.conn), \
             mock.patch("body.detalle_historia.garmin_cuenta.leer") as m_leer, \
             mock.patch("body.detalle_historia.pull.traer_detalle_historia") as m_historia:
            with contextlib.redirect_stdout(buf):
                codigo = detalle_historia.main(["--dry-run"])
        self.assertEqual(codigo, 0)
        m_leer.assert_not_called()
        m_historia.assert_not_called()

    def test_prints_only_counts_group_names_and_months(self):
        buf = io.StringIO()
        with mock.patch("body.detalle_historia.db.connect", return_value=self.conn):
            with contextlib.redirect_stdout(buf):
                detalle_historia.main(["--dry-run"])
        salida = buf.getvalue()
        self.assertIn(
            "antes: grupo=basketball mes=2026-10 con_detalle=1 garmin_no_tiene=0 pendientes=0", salida
        )
        self.assertIn("mes_mas_antiguo_con_detalle=2026-10", salida)
        self.assertNotIn("b1", salida)


class MaxRangeTest(unittest.TestCase):
    def test_zero_is_refused_without_opening_a_connection(self):
        buf = io.StringIO()
        with mock.patch("body.detalle_historia.db.connect") as m_connect:
            with contextlib.redirect_stdout(buf):
                codigo = detalle_historia.main(["--max", "0"])
        self.assertEqual(codigo, 1)
        m_connect.assert_not_called()
        self.assertIn("refused", buf.getvalue())

    def test_201_is_refused_without_opening_a_connection(self):
        buf = io.StringIO()
        with mock.patch("body.detalle_historia.db.connect") as m_connect:
            with contextlib.redirect_stdout(buf):
                codigo = detalle_historia.main(["--max", "201"])
        self.assertEqual(codigo, 1)
        m_connect.assert_not_called()
        self.assertIn("refused", buf.getvalue())


class LiveRunTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "cli_live.db")
        db.init(self.conn)
        self.person_id = db.local_person_id(self.conn)

    def test_no_token_refuses_without_calling_the_backfill(self):
        buf = io.StringIO()
        with mock.patch("body.detalle_historia.db.connect", return_value=self.conn), \
             mock.patch("body.detalle_historia.garmin_cuenta.leer", return_value=None), \
             mock.patch("body.detalle_historia.pull.traer_detalle_historia") as m_historia:
            with contextlib.redirect_stdout(buf):
                codigo = detalle_historia.main([])
        self.assertEqual(codigo, 1)
        m_historia.assert_not_called()
        self.assertIn("refused", buf.getvalue())

    def test_live_run_calls_the_backfill_with_the_resolved_token_and_max_and_prints_its_counts(self):
        resultado = {"resueltas": 3, "restantes": 1, "fallidas_transitorias": {}, "detenida_por": None}
        buf = io.StringIO()
        with mock.patch("body.detalle_historia.db.connect", return_value=self.conn), \
             mock.patch("body.detalle_historia.garmin_cuenta.leer", return_value="tok-x"), \
             mock.patch("body.detalle_historia.pull.traer_detalle_historia",
                         return_value=resultado) as m_historia:
            with contextlib.redirect_stdout(buf):
                codigo = detalle_historia.main(["--max", "5"])
        self.assertEqual(codigo, 0)
        m_historia.assert_called_once_with(
            self.conn, self.person_id, token_str="tok-x", max_actividades=5)
        salida = buf.getvalue()
        self.assertIn("resueltas=3", salida)
        self.assertIn("restantes=1", salida)
        self.assertIn("detenida_por=ninguna", salida)

    def test_live_run_prints_fallidas_transitorias_when_the_backfill_stopped_early(self):
        resultado = {
            "resueltas": 1, "restantes": 4,
            "fallidas_transitorias": {"GarminConnectTooManyRequestsError": 1},
            "detenida_por": "transitorio",
        }
        buf = io.StringIO()
        with mock.patch("body.detalle_historia.db.connect", return_value=self.conn), \
             mock.patch("body.detalle_historia.garmin_cuenta.leer", return_value="tok-x"), \
             mock.patch("body.detalle_historia.pull.traer_detalle_historia", return_value=resultado):
            with contextlib.redirect_stdout(buf):
                codigo = detalle_historia.main([])
        self.assertEqual(codigo, 0)
        salida = buf.getvalue()
        self.assertIn("detenida_por=transitorio", salida)
        self.assertIn("fallidas_transitorias=GarminConnectTooManyRequestsError x1", salida)


class ResolverPersonaTest(unittest.TestCase):
    """Mirrors body/cobertura.py's own _resolver_persona -- not hosted, no --cuenta-sub ->
    db.default_person_id(); a --cuenta-sub with no matching row refuses."""

    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "cli_resolver.db")
        db.init(self.conn)

    def test_unknown_cuenta_sub_refuses(self):
        buf = io.StringIO()
        with mock.patch("body.detalle_historia.db.connect", return_value=self.conn):
            with contextlib.redirect_stdout(buf):
                codigo = detalle_historia.main(["--dry-run", "--cuenta-sub", "no-such-sub"])
        self.assertEqual(codigo, 1)
        self.assertIn("refused", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
