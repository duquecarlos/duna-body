"""body/persona_garmin.py: the host-only diagnostic and repair for a hosted Garmin connection that
landed on a person the Cuenta key can no longer reach (BODY-GARMIN-PERSONA-1). Invented demo data
and an invented token only -- never a real health figure."""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import config, db, garmin_cuenta, goals, persona_garmin, practicas, pull, recuperacion, sueno_fc, turno_fc

NOW = "2026-11-10T08:00:00+00:00"


def _nueva_conn(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "persona_garmin.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    return conn


def _crear_persona(conn, person_id: int, *, cuenta_sub: str | None, nombre: str = "Me") -> None:
    conn.execute(
        "INSERT INTO person (id, display_name, cuenta_sub) VALUES (?, ?, ?)", (person_id, nombre, cuenta_sub)
    )
    conn.commit()


def _dia_garmin(conn, person_id: int, day: str, *, con_actividad: bool = False) -> None:
    """One invented Garmin-pulled day: a daily_metric value, a raw_payload row and a sleep_session
    -- the same four-table shape db.save_day itself writes. `con_actividad` adds an activity plus
    its workout_set/activity_detail rows, the same guarda_detalle-gated extras."""
    conn.execute(
        "INSERT INTO daily_metric (person_id, day, metric_key, value, source) VALUES (?, ?, 'resting_hr', 55, 'garmin')",
        (person_id, day),
    )
    conn.execute(
        "INSERT INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, ?, 'user_summary', 'garmin', ?, '{}')",
        (person_id, day, NOW),
    )
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, f"{day}T23:00:00", f"{day}T06:30:00"),
    )
    if con_actividad:
        source_id = f"act-{person_id}-{day}"
        conn.execute(
            "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, source) "
            "VALUES (?, ?, ?, ?, 'strength_training', 'Gym', 1800, 'garmin')",
            (person_id, source_id, day, f"{day}T07:00:00"),
        )
        conn.execute(
            "INSERT INTO workout_set (person_id, activity_source, activity_source_id, set_order, "
            "set_type, garmin_reps, corrected_by_person, created_at, updated_at) "
            "VALUES (?, 'garmin', ?, 1, 'active', 10, 0, ?, ?)",
            (person_id, source_id, NOW, NOW),
        )
        conn.execute(
            "INSERT INTO activity_detail (person_id, activity_source, activity_source_id, kind, "
            "fetched_at, payload) VALUES (?, 'garmin', ?, 'detalle', ?, '{}')",
            (person_id, source_id, NOW),
        )
    conn.commit()


def _conectar_garmin(conn, person_id: int, token: str) -> None:
    garmin_cuenta.guardar(conn, person_id, token)
    conn.commit()


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = _nueva_conn(self)
        patcher = mock.patch.dict(os.environ, {garmin_cuenta.KEY_ENV: os.urandom(32).hex()})
        patcher.start()
        self.addCleanup(patcher.stop)


def _capturar_stdout(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class DiagnosticoTest(_Base):
    def test_every_person_printed_counts_only_never_sub_name_or_figure(self):
        conn = self.conn
        # Person 1: db.init's own seed row ('Me', no cuenta_sub) -- left exactly as seeded.
        _crear_persona(conn, 2, cuenta_sub="sub-secret-xyz", nombre="Carlos Secreto")
        hoy = date.today()
        dias_recientes = [(hoy - timedelta(i)).isoformat() for i in range(5)]
        for d in dias_recientes:
            _dia_garmin(conn, 2, d)
        dia_viejo = (hoy - timedelta(40)).isoformat()
        _dia_garmin(conn, 2, dia_viejo)
        _conectar_garmin(conn, 2, "FAKE-TOKEN-DIAGNOSTICO")
        conn.execute(
            "INSERT INTO goal (person_id, area, title, kind, metric_key, direction, target_value, "
            "baseline_value, start_day, status, visibility) VALUES "
            "(2, 'body', 'G', 'metric', 'resting_hr', 'lower', 50, 55, ?, 'active', 'private')",
            (dias_recientes[-1],),
        )
        conn.commit()

        salida = _capturar_stdout(persona_garmin._print_diagnostico, persona_garmin.diagnostico(conn))

        self.assertNotIn("sub-secret-xyz", salida)
        self.assertNotIn("Carlos Secreto", salida)
        self.assertNotIn("55", salida)  # the invented resting_hr figure

        lineas = {int(l.split(":")[0].split()[1]): l for l in salida.splitlines()}
        self.assertEqual(set(lineas), {1, 2})
        self.assertIn("cuenta_sub=no", lineas[1])
        self.assertIn("garmin=no", lineas[1])
        self.assertIn("dias_garmin_total=0", lineas[1])
        self.assertIn("cuenta_sub=si", lineas[2])
        self.assertIn("nombre=si", lineas[2])
        self.assertIn("garmin=si", lineas[2])
        self.assertIn("dias_garmin_14=5", lineas[2])
        self.assertIn(f"ultimo_dia_garmin={dias_recientes[0]}", lineas[2])
        self.assertIn("dias_garmin_total=6", lineas[2])
        self.assertIn("filas_propias=1", lineas[2])  # the one manual goal row
        self.assertIn("dispositivos=0", lineas[2])
        self.assertIn("sesiones_vivas=0", lineas[2])

    def test_seed_person_nombre_is_no(self):
        """Person 1's display_name is still db.init's own seed default ('Me') -- nombre=no."""
        conn = self.conn
        rows = persona_garmin.diagnostico(conn)
        yo = next(r for r in rows if r["id"] == 1)
        self.assertFalse(yo["nombre"])
        self.assertFalse(yo["cuenta_sub"])


class RelinkarRefusalTest(_Base):
    def setUp(self):
        super().setUp()
        _crear_persona(self.conn, 2, cuenta_sub="sub-dest")

    def test_equal_ids_refused(self):
        with self.assertRaises(persona_garmin.Refusada) as ctx:
            persona_garmin.relinkar(self.conn, 1, 1, apply=False)
        self.assertTrue(any("misma persona" in m for m in ctx.exception.motivos))

    def test_missing_ids_refused(self):
        with self.assertRaises(persona_garmin.Refusada) as ctx:
            persona_garmin.relinkar(self.conn, 999, 2, apply=False)
        self.assertTrue(any("no existe persona 999" in m for m in ctx.exception.motivos))

        with self.assertRaises(persona_garmin.Refusada) as ctx2:
            persona_garmin.relinkar(self.conn, 1, 998, apply=False)
        self.assertTrue(any("no existe persona 998" in m for m in ctx2.exception.motivos))

    def test_destination_with_no_cuenta_sub_refused(self):
        _crear_persona(self.conn, 3, cuenta_sub=None)
        with self.assertRaises(persona_garmin.Refusada) as ctx:
            persona_garmin.relinkar(self.conn, 1, 3, apply=False)
        self.assertTrue(any("no tiene cuenta_sub" in m for m in ctx.exception.motivos))

    def test_source_with_cuenta_sub_refused(self):
        _crear_persona(self.conn, 3, cuenta_sub="sub-source-is-real")
        with self.assertRaises(persona_garmin.Refusada) as ctx:
            persona_garmin.relinkar(self.conn, 3, 2, apply=False)
        self.assertTrue(any("(--desde) tiene cuenta_sub" in m for m in ctx.exception.motivos))

    def test_source_with_manual_rows_refused_naming_table_and_count(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO goal (person_id, area, title, kind, metric_key, direction, target_value, "
            "baseline_value, start_day, status, visibility) VALUES "
            "(1, 'body', 'G1', 'metric', 'resting_hr', 'lower', 50, 55, '2026-11-01', 'active', 'private')"
        )
        conn.execute(
            "INSERT INTO goal (person_id, area, title, kind, metric_key, direction, target_value, "
            "baseline_value, start_day, status, visibility) VALUES "
            "(1, 'body', 'G2', 'metric', 'resting_hr', 'lower', 50, 55, '2026-11-01', 'active', 'private')"
        )
        conn.execute("INSERT INTO checkin (person_id, day, momento, created_at) VALUES (1, '2026-11-01', 'wake', ?)",
                     (NOW,))
        conn.commit()
        with self.assertRaises(persona_garmin.Refusada) as ctx:
            persona_garmin.relinkar(conn, 1, 2, apply=False)
        motivos = "; ".join(ctx.exception.motivos)
        self.assertIn("2 fila(s) propias en goal", motivos)
        self.assertIn("1 fila(s) propias en checkin", motivos)

    def test_source_with_a_suggested_practica_refused_naming_table_and_count(self):
        """BODY-HISTORIA-VIDA-1 ruling 8: `origen = 'sugerida'` is still a person-authored row --
        relinkar refuses on it, same as any other practica."""
        practicas.crear(self.conn, 1, "ZZ-PRACTICA", "actividad", {"tipos": ["running"]}, origen="sugerida")
        self.conn.commit()
        with self.assertRaises(persona_garmin.Refusada) as ctx:
            persona_garmin.relinkar(self.conn, 1, 2, apply=False)
        self.assertTrue(any("1 fila(s) propias en practica" in m for m in ctx.exception.motivos))

    def test_destination_already_has_garmin_cuenta_refused(self):
        _conectar_garmin(self.conn, 2, "FAKE-TOKEN-DEST-ALREADY")
        with self.assertRaises(persona_garmin.Refusada) as ctx:
            persona_garmin.relinkar(self.conn, 1, 2, apply=False)
        self.assertTrue(any("ya tiene garmin_cuenta" in m for m in ctx.exception.motivos))

    def test_refusal_writes_nothing(self):
        _crear_persona(self.conn, 3, cuenta_sub="sub-3")
        conn = self.conn
        conn.execute(
            "INSERT INTO goal (person_id, area, title, kind, metric_key, direction, target_value, "
            "baseline_value, start_day, status, visibility) VALUES "
            "(1, 'body', 'G', 'metric', 'resting_hr', 'lower', 50, 55, '2026-11-01', 'active', 'private')"
        )
        conn.commit()
        antes = conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0]
        with self.assertRaises(persona_garmin.Refusada):
            persona_garmin.relinkar(conn, 1, 3, apply=False)
        despues = conn.execute("SELECT COUNT(*) FROM goal").fetchone()[0]
        self.assertEqual(antes, despues)
        exit_code = persona_garmin.main(["relinkar", "--desde", "1", "--hacia", "3"])
        self.assertEqual(exit_code, 4)


class RelinkarMoveTest(_Base):
    """P1 (source, no cuenta_sub) connected to Garmin, with days the destination both does and
    does not already have, plus derived rows that must never move."""

    def setUp(self):
        super().setUp()
        conn = self.conn
        _crear_persona(conn, 2, cuenta_sub="sub-dest")
        self.hoy = date.today()
        self.dias = [(self.hoy - timedelta(i)).isoformat() for i in range(5, 0, -1)]  # oldest..newest, 5 days
        for i, d in enumerate(self.dias):
            _dia_garmin(conn, 1, d, con_actividad=(i == 0))
        self.dia_compartido = self.dias[2]  # the destination will already have this one
        _dia_garmin(conn, 2, self.dia_compartido)
        conn.execute(
            "UPDATE daily_metric SET value = 999 WHERE person_id = 2 AND day = ? AND metric_key = 'resting_hr'",
            (self.dia_compartido,),
        )
        _conectar_garmin(conn, 1, "FAKE-TOKEN-RELINKAR")
        # Derived rows for person 1 -- must be left behind, never moved.
        conn.execute(
            "INSERT INTO recuperacion_actividad (person_id, activity_source, activity_source_id, "
            "modelo_version, minutos, computed_at) VALUES (1, 'garmin', ?, 'v1', 30.0, ?)",
            (f"act-1-{self.dias[0]}", NOW),
        )
        conn.execute(
            "INSERT INTO turno_fc (person_id, day, kind, modelo_version, computed_at) "
            "VALUES (1, ?, 'night', 'v1', ?)", (self.dias[0], NOW),
        )
        # goal_check needs a real goal_id (its own FK), but a `goal` row itself is person-authored
        # (ruling 3's census) -- owning it under person 2 instead keeps person 1's own census at
        # zero goal rows while still exercising goal_check's derived-and-left-behind count.
        conn.execute(
            "INSERT INTO goal (person_id, area, title, kind, metric_key, direction, target_value, "
            "baseline_value, start_day, status, visibility) VALUES "
            "(2, 'body', 'G', 'metric', 'resting_hr', 'lower', 50, 55, ?, 'active', 'private')",
            (self.dias[0],),
        )
        goal_id = conn.execute("SELECT id FROM goal WHERE person_id = 2").fetchone()[0]
        conn.execute(
            "INSERT INTO goal_check (goal_id, person_id, week_start, rule_version) VALUES (?, 1, ?, 'v1')",
            (goal_id, self.dias[0]),
        )
        conn.execute(
            "INSERT INTO daily_metric (person_id, day, metric_key, value, source) "
            "VALUES (1, ?, 'sleep_hr_avg', 58.0, 'body-sueno-fc-v1')", (self.dias[0],),
        )
        conn.commit()

    def test_dry_run_writes_nothing(self):
        conn = self.conn
        antes_dm = conn.execute("SELECT COUNT(*) FROM daily_metric WHERE person_id = 1").fetchone()[0]
        antes_gc = garmin_cuenta.leer(conn, 1)
        resultado = persona_garmin.relinkar(conn, 1, 2, apply=False)
        despues_dm = conn.execute("SELECT COUNT(*) FROM daily_metric WHERE person_id = 1").fetchone()[0]
        self.assertEqual(antes_dm, despues_dm)
        self.assertEqual(garmin_cuenta.leer(conn, 1), antes_gc)
        self.assertIsNone(garmin_cuenta.leer(conn, 2))
        self.assertEqual(resultado["dias_movidos"], 4)
        self.assertEqual(resultado["dias_omitidos"], 1)
        self.assertEqual(resultado["tablas"]["daily_metric"]["mov"], 4)
        self.assertEqual(resultado["tablas"]["daily_metric"]["ya"], 1)
        self.assertEqual(resultado["garmin_cuenta"], "moveria")

    @mock.patch.object(recuperacion, "recalcular", return_value={"nuevas": 0})
    @mock.patch.object(turno_fc, "recalcular", return_value={"nuevas": 0})
    @mock.patch.object(sueno_fc, "recalcular", return_value={"nuevas": 0})
    @mock.patch.object(goals, "cerrar_semanas", return_value=0)
    def test_apply_moves_uncontested_days_skips_contested_day_moves_token_recomputes_destination(
        self, cerrar_semanas, sueno_recalc, turno_recalc, recuperacion_recalc,
    ):
        conn = self.conn
        resultado = persona_garmin.relinkar(conn, 1, 2, apply=True)

        self.assertEqual(resultado["dias_movidos"], 4)
        self.assertEqual(resultado["dias_omitidos"], 1)
        self.assertEqual(resultado["tablas"]["daily_metric"]["mov"], 4)
        self.assertEqual(resultado["tablas"]["daily_metric"]["ya"], 1)
        self.assertEqual(resultado["tablas"]["workout_set"]["mov"], 1)
        self.assertEqual(resultado["tablas"]["activity_detail"]["mov"], 1)

        # The 4 uncontested days moved to person 2, gone from person 1.
        for d in self.dias:
            if d == self.dia_compartido:
                continue
            fila_1 = conn.execute(
                "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'resting_hr'", (d,)
            ).fetchone()
            self.assertIsNone(fila_1, f"day {d} not removed from source")
            fila_2 = conn.execute(
                "SELECT value FROM daily_metric WHERE person_id = 2 AND day = ? AND metric_key = 'resting_hr'", (d,)
            ).fetchone()
            self.assertEqual(fila_2["value"], 55)

        # The contested day: untouched on both sides -- person 2's own value survives unchanged.
        fila_1_contested = conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 1 AND day = ? AND metric_key = 'resting_hr'",
            (self.dia_compartido,),
        ).fetchone()
        self.assertEqual(fila_1_contested["value"], 55, "contested day's own source row was moved/deleted")
        fila_2_contested = conn.execute(
            "SELECT value FROM daily_metric WHERE person_id = 2 AND day = ? AND metric_key = 'resting_hr'",
            (self.dia_compartido,),
        ).fetchone()
        self.assertEqual(fila_2_contested["value"], 999, "destination's own contested-day value was overwritten")

        # workout_set/activity_detail followed their activity to person 2.
        source_id = f"act-1-{self.dias[0]}"
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM activity WHERE person_id = 1 AND source_id = ?", (source_id,)
        ).fetchone())
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM activity WHERE person_id = 2 AND source_id = ?", (source_id,)
        ).fetchone())
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM workout_set WHERE person_id = 2 AND activity_source_id = ?", (source_id,)
        ).fetchone())
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM activity_detail WHERE person_id = 2 AND activity_source_id = ?", (source_id,)
        ).fetchone())

        # The garmin_cuenta token moved -- destination reads it, source has none, and the one
        # connected person a future pull would reach is now the destination alone.
        self.assertEqual(garmin_cuenta.leer(conn, 2), "FAKE-TOKEN-RELINKAR")
        self.assertIsNone(garmin_cuenta.leer(conn, 1))
        self.assertEqual(garmin_cuenta.every_person_id(conn), [2])

        # turno_fc/goal_check carry no FK to `activity`, so they stay at the source untouched and
        # are reported as such. recuperacion_actividad's own row was keyed to the activity that
        # just moved -- its FK to `activity` cannot be satisfied at the source any more, so it is
        # removed as an orphan of the move (a measured exception to "derived rows stay put",
        # counted on its own, never silently) rather than left dangling or silently kept.
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM recuperacion_actividad WHERE person_id = 1").fetchone()[0], 0
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turno_fc WHERE person_id = 1").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_check WHERE person_id = 1").fetchone()[0], 1)
        self.assertEqual(resultado["recuperacion_actividad_huerfana_eliminada"], 1)
        self.assertEqual(resultado["derivados_desde"]["recuperacion_actividad"], 0)
        self.assertEqual(resultado["derivados_desde"]["turno_fc"], 1)
        self.assertEqual(resultado["derivados_desde"]["goal_check"], 1)
        self.assertEqual(resultado["derivados_desde"]["daily_metric(estimado)"], 1)

        # Ruling 4: the destination's own post-pull recomputations ran.
        recuperacion_recalc.assert_called_once_with(conn, 2)
        turno_recalc.assert_called_once_with(conn, 2)
        sueno_recalc.assert_called_once_with(conn, 2)
        cerrar_semanas.assert_called_once()
        self.assertEqual(cerrar_semanas.call_args.args[:2], (conn, 2))

    def test_apply_rolls_back_everything_on_mid_move_failure(self):
        conn = self.conn
        antes_dm_1 = conn.execute("SELECT COUNT(*) FROM daily_metric WHERE person_id = 1").fetchone()[0]
        antes_dm_2 = conn.execute("SELECT COUNT(*) FROM daily_metric WHERE person_id = 2").fetchone()[0]
        antes_token = garmin_cuenta.leer(conn, 1)

        with mock.patch.object(garmin_cuenta, "leer", side_effect=RuntimeError("boom mid-move")):
            with self.assertRaises(RuntimeError):
                persona_garmin.relinkar(conn, 1, 2, apply=True)

        despues_dm_1 = conn.execute("SELECT COUNT(*) FROM daily_metric WHERE person_id = 1").fetchone()[0]
        despues_dm_2 = conn.execute("SELECT COUNT(*) FROM daily_metric WHERE person_id = 2").fetchone()[0]
        self.assertEqual(antes_dm_1, despues_dm_1, "source's rows were not fully rolled back")
        self.assertEqual(antes_dm_2, despues_dm_2, "destination gained rows despite the rollback")
        self.assertEqual(garmin_cuenta.leer(conn, 1), antes_token, "the token itself must still decrypt at the source")
        self.assertIsNone(conn.execute("SELECT 1 FROM garmin_cuenta WHERE person_id = 2").fetchone())


if __name__ == "__main__":
    unittest.main()
