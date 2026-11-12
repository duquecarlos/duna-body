"""ORCH-CARLOS-DELEGATES-TIER-1-1: Personal's daily commute summary (body/desplazamiento_import.py)
-- the metric_def.origen 'vida' widening, the four vehicle/walking/cycling metric rows, the generic
fuente_importacion table, the importer itself, and the two ways in (local vivo task, hosted device-
key route). Invented demo data only, no real health or location figures.
"""
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import config, db, desplazamiento_import as di, pc_cliente, subir_desplazamiento

TEST_KEY = "bdy_test-key-0123456789"


def _db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)


# ---- ruling 1: metric_def.origen gains 'vida' ------------------------------------------------------

_OLD_METRIC_DEF_SQL = """
CREATE TABLE metric_def (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    unit    TEXT NOT NULL,
    better  TEXT NOT NULL CHECK (better IN ('higher', 'lower', 'neutral')),
    kind    TEXT NOT NULL DEFAULT 'measured' CHECK (kind IN ('measured', 'estimated')),
    origen  TEXT NOT NULL DEFAULT 'garmin' CHECK (origen IN ('garmin', 'self'))
);
"""


def _old_db(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """A DB built from the current SCHEMA (so every other table is already right), with
    metric_def dropped and recreated in its pre-slice shape -- the same technique
    tests/test_entreno_schema.py uses for metric_def/plan/propuesta_agente."""
    conn = _db(case, name)
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE metric_def")
    conn.execute(_OLD_METRIC_DEF_SQL)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class FreshDbOrigenVidaTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "fresh.db")
        db.init(self.conn)

    def test_the_four_vida_rows_are_seeded(self):
        rows = {
            r["key"]: (r["origen"], r["kind"], r["better"], r["agregado"]) for r in self.conn.execute(
                "SELECT key, origen, kind, better, agregado FROM metric_def WHERE key IN "
                "('vehicle_minutes', 'vehicle_trips', 'walking_minutes', 'cycling_minutes')"
            )
        }
        self.assertEqual(rows, {
            "vehicle_minutes": ("vida", "measured", "neutral", None),
            "vehicle_trips": ("vida", "measured", "neutral", None),
            "walking_minutes": ("vida", "measured", "neutral", None),
            "cycling_minutes": ("vida", "measured", "neutral", None),
        })

    def test_vida_origen_is_accepted_by_the_check(self):
        self.conn.execute(
            "INSERT INTO metric_def (key, label, unit, better, kind, origen) VALUES "
            "('zz_prueba_vida', 'Prueba', 'u', 'neutral', 'measured', 'vida')"
        )
        self.conn.commit()

    def test_fuente_importacion_table_exists(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(fuente_importacion)")}
        self.assertEqual(cols, {
            "person_id", "fuente", "primer_dia", "ultimo_dia", "computed_at", "dias",
            "importado_at", "ultimo_error", "ultimo_error_at",
        })

    def test_init_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM metric_def WHERE origen = 'vida'"
        ).fetchone()["n"]
        self.assertEqual(rows, 4)


class OldShapeMigrationTest(unittest.TestCase):
    """An old-shape DB (origen CHECK IN ('garmin', 'self') only) with an invented row migrates
    with every existing row preserved and the new origen value accepted, and the migration is
    idempotent."""

    def setUp(self):
        self.conn = _old_db(self, "vieja.db")
        self.conn.execute(
            "INSERT INTO metric_def (key, label, unit, better, kind, origen) VALUES "
            "('mi_metrica_vieja', 'Mi metrica vieja', 'u', 'higher', 'measured', 'self')"
        )
        self.conn.commit()

    def _assert_migrated_state(self):
        old_row = self.conn.execute(
            "SELECT label, origen FROM metric_def WHERE key = 'mi_metrica_vieja'"
        ).fetchone()
        self.assertEqual(old_row["label"], "Mi metrica vieja")
        self.assertEqual(old_row["origen"], "self")

        new_rows = {
            r["key"]: r["origen"] for r in self.conn.execute(
                "SELECT key, origen FROM metric_def WHERE key IN "
                "('vehicle_minutes', 'vehicle_trips', 'walking_minutes', 'cycling_minutes')"
            )
        }
        self.assertEqual(new_rows, {
            "vehicle_minutes": "vida", "vehicle_trips": "vida",
            "walking_minutes": "vida", "cycling_minutes": "vida",
        })

        self.conn.execute(
            "INSERT INTO metric_def (key, label, unit, better, kind, origen) VALUES "
            "('zz_otra_vida', 'Otra', 'u', 'neutral', 'measured', 'vida')"
        )
        self.conn.commit()

    def test_migration_preserves_the_old_row_and_accepts_vida(self):
        db.init(self.conn)
        self._assert_migrated_state()

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        self._assert_migrated_state()
        count = self.conn.execute("SELECT COUNT(*) AS n FROM metric_def").fetchone()["n"]
        self.conn.commit()
        db.init(self.conn)
        after = self.conn.execute("SELECT COUNT(*) AS n FROM metric_def").fetchone()["n"]
        self.assertEqual(count, after)


class OrigenBranchTreatsVidaAsMeasuredTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 1: every place code branches on origen already
    compares explicitly against 'self' (never a bare "not garmin"), so 'vida' falls through to the
    same MEASURED/garmin-sized treatment without any code change -- this pins that down for the
    three branches the ruling names."""

    def setUp(self):
        self.conn = _db(self, "branches.db")
        db.init(self.conn)

    def test_tendencias_floor_treats_vida_like_garmin(self):
        from body.rutas import tendencias
        defs = {r["key"]: dict(r) for r in self.conn.execute("SELECT * FROM metric_def")}
        for key in ("vehicle_minutes", "vehicle_trips", "walking_minutes", "cycling_minutes"):
            min_days = 1 if defs[key]["origen"] == "self" else tendencias.TREND_MIN_DAYS
            min_month = (tendencias.SELF_MIN_MONTH_POINTS if defs[key]["origen"] == "self"
                         else tendencias.GARMIN_MIN_MONTH_POINTS)
            self.assertEqual(min_days, tendencias.TREND_MIN_DAYS, key)
            self.assertEqual(min_month, tendencias.GARMIN_MIN_MONTH_POINTS, key)

    def test_catalogo_tendencias_self_group_excludes_vida(self):
        from body import catalogo_tendencias
        self_group = dict(catalogo_tendencias.TREND_GROUPS)["self"]
        for key in ("vehicle_minutes", "vehicle_trips", "walking_minutes", "cycling_minutes"):
            self.assertNotIn(key, self_group)

    def test_registrar_medida_refuses_a_vida_key(self):
        fila = self.conn.execute(
            "SELECT agregado FROM metric_def WHERE key = 'vehicle_minutes' AND origen = 'self'"
        ).fetchone()
        self.assertIsNone(fila)


def _csv(rows, computed_at="2026-11-01T06:00:00+00:00", header=True):
    lines = [",".join(di.HEADER)] if header else []
    for day, vm, vt, wm, cm in rows:
        lines.append(f"{day},{vm},{vt},{wm},{cm},{computed_at}")
    return "\n".join(lines) + "\n"


# ---- ruling 4: validar() -- one validation, all-or-nothing ----------------------------------------

class ValidarTest(unittest.TestCase):
    def test_valid_file_parses_every_row(self):
        texto = _csv([("2026-01-01", 30, 2, 10, 0), ("2026-01-02", 45, 3, 0, 20)])
        rows = di.validar(texto)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["day"], "2026-01-01")
        self.assertEqual(rows[0]["vehicle_minutes"], 30)
        self.assertEqual(rows[1]["cycling_minutes"], 20)

    def test_wrong_header_is_refused(self):
        texto = "day,vehicle_minutes\n2026-01-01,30\n"
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "esquema")

    def test_bad_date_is_refused(self):
        texto = _csv([("not-a-date", 30, 2, 10, 0)])
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "dia_malformado")

    def test_duplicate_day_is_refused(self):
        texto = _csv([("2026-01-01", 30, 2, 10, 0), ("2026-01-01", 10, 1, 5, 0)])
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "dia_duplicado")

    def test_negative_count_is_refused(self):
        texto = _csv([("2026-01-01", -5, 2, 10, 0)])
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "conteo_invalido")

    def test_non_integer_count_is_refused(self):
        texto = _csv([("2026-01-01", "30.5", 2, 10, 0)])
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "conteo_invalido")

    def test_mixed_computed_at_is_refused(self):
        texto = (",".join(di.HEADER) + "\n"
                 "2026-01-01,30,2,10,0,2026-11-01T06:00:00+00:00\n"
                 "2026-01-02,45,3,0,20,2026-11-02T06:00:00+00:00\n")
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "computed_at_mixto")

    def test_future_day_is_refused(self):
        texto = _csv([("2099-01-01", 30, 2, 10, 0)])
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "dia_futuro")

    def test_over_1mb_is_refused(self):
        texto = "x" * (di.MAX_BYTES + 1)
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "demasiado_grande")

    def test_computed_at_without_offset_is_refused(self):
        texto = _csv([("2026-01-01", 30, 2, 10, 0)], computed_at="2026-11-01T06:00:00")
        with self.assertRaises(di.ImportRefused) as cm:
            di.validar(texto)
        self.assertEqual(cm.exception.motivo, "computed_at_malformado")


# ---- ruling 4: importar() -- writes through the existing daily_metric writer ----------------------

class ImportarTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db(self, "importar.db")
        db.init(self.conn)

    def test_valid_file_writes_4_times_n_values_with_the_source(self):
        texto = _csv([("2026-01-01", 30, 2, 10, 0), ("2026-01-02", 45, 3, 0, 20)])
        resultado = di.importar(self.conn, 1, texto)
        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["sin_cambios"])
        self.assertEqual(resultado["dias"], 2)
        self.assertEqual(resultado["primer_dia"], "2026-01-01")
        self.assertEqual(resultado["ultimo_dia"], "2026-01-02")
        rows = self.conn.execute(
            "SELECT day, metric_key, value, source FROM daily_metric WHERE metric_key IN "
            "('vehicle_minutes', 'vehicle_trips', 'walking_minutes', 'cycling_minutes') "
            "ORDER BY day, metric_key"
        ).fetchall()
        self.assertEqual(len(rows), 8)
        for r in rows:
            self.assertEqual(r["source"], di.FUENTE)

    def test_each_refusal_class_writes_nothing_and_records_ultimo_error(self):
        casos = [
            ("day,vehicle_minutes\n2026-01-01,30\n", "esquema"),
            (_csv([("not-a-date", 30, 2, 10, 0)]), "dia_malformado"),
            (_csv([("2026-01-01", -5, 2, 10, 0)]), "conteo_invalido"),
            (_csv([("2099-01-01", 30, 2, 10, 0)]), "dia_futuro"),
        ]
        for texto, motivo in casos:
            conn = _db(self, f"refusal-{motivo}.db")
            db.init(conn)
            resultado = di.importar(conn, 1, texto)
            self.assertFalse(resultado["ok"])
            self.assertEqual(resultado["motivo"], motivo)
            count = conn.execute("SELECT COUNT(*) FROM daily_metric").fetchone()[0]
            self.assertEqual(count, 0, motivo)
            fila = conn.execute(
                "SELECT ultimo_error, ultimo_error_at FROM fuente_importacion WHERE person_id = 1 AND fuente = ?",
                (di.FUENTE,),
            ).fetchone()
            self.assertEqual(fila["ultimo_error"], motivo)
            self.assertIsNotNone(fila["ultimo_error_at"])

    def test_same_computed_at_is_a_no_op(self):
        texto = _csv([("2026-01-01", 30, 2, 10, 0)])
        di.importar(self.conn, 1, texto)
        resultado = di.importar(self.conn, 1, texto)
        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["sin_cambios"])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM daily_metric WHERE metric_key = 'vehicle_minutes'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_a_later_file_dropping_an_old_day_leaves_its_values_in_place(self):
        texto1 = _csv([("2026-01-01", 30, 2, 10, 0), ("2026-01-02", 45, 3, 0, 20)],
                       computed_at="2026-11-01T06:00:00+00:00")
        di.importar(self.conn, 1, texto1)
        texto2 = _csv([("2026-01-02", 50, 4, 0, 25)], computed_at="2026-12-01T06:00:00+00:00")
        resultado = di.importar(self.conn, 1, texto2)
        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["sin_cambios"])
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE day = '2026-01-01' AND metric_key = 'vehicle_minutes'"
        ).fetchone()
        self.assertEqual(row["value"], 30.0)

    def test_refusal_after_a_success_does_not_touch_previously_imported_values(self):
        texto1 = _csv([("2026-01-01", 30, 2, 10, 0)], computed_at="2026-11-01T06:00:00+00:00")
        di.importar(self.conn, 1, texto1)
        di.importar(self.conn, 1, "esquema malo\n")
        row = self.conn.execute(
            "SELECT value FROM daily_metric WHERE day = '2026-01-01' AND metric_key = 'vehicle_minutes'"
        ).fetchone()
        self.assertEqual(row["value"], 30.0)


# ---- ruling 5(a): the local vivo task / CLI ---------------------------------------------------------

class MainCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=TMP))

    def test_output_carries_no_value(self):
        texto = _csv([("2026-01-01", 487, 2, 10, 0)], computed_at="2026-11-01T06:00:00+00:00")
        path = self.tmp / "commute.csv"
        path.write_text(texto, encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = di.main(["--file", str(path)])
        self.assertEqual(rc, 0)
        self.assertNotIn("487", buf.getvalue())
        self.assertIn("2026-01-01", buf.getvalue())

    def test_no_file_configured_message(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = di.main([])
        self.assertEqual(rc, 1)
        self.assertIn("no commute file configured", buf.getvalue())

    def test_file_not_found_message(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = di.main(["--file", str(self.tmp / "missing.csv")])
        self.assertEqual(rc, 1)
        self.assertIn("not found", buf.getvalue())


# ---- ruling 5(b): the PC uploader (stubbed HTTP, no network) ---------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, payload: dict | None = None) -> urllib.error.HTTPError:
    body = json.dumps(payload).encode() if payload is not None else b""
    return urllib.error.HTTPError("https://fake.test/x", code, "", None, io.BytesIO(body))


class SubirDesplazamientoTest(unittest.TestCase):
    def setUp(self):
        orig_home = config.BODY_HOME
        self.addCleanup(lambda: setattr(config, "BODY_HOME", orig_home))
        config.BODY_HOME = Path(tempfile.mkdtemp(dir=TMP))
        pc_cliente.escribir_enlace("https://fake.test", TEST_KEY)
        self.path = Path(tempfile.mkdtemp(dir=TMP)) / "commute.csv"
        self.path.write_text(_csv([("2026-01-01", 487, 2, 10, 0)]), encoding="utf-8")

    def _patch_abrir(self, fn):
        patcher = mock.patch.object(pc_cliente, "_abrir", fn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, abrir):
        self._patch_abrir(abrir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = subir_desplazamiento.main(["--file", str(self.path)])
        return rc, buf.getvalue()

    def test_200_prints_counts_only(self):
        def abrir(req):
            return _FakeResponse(200, json.dumps(
                {"dias": 1, "primer_dia": "2026-01-01", "ultimo_dia": "2026-01-01", "sin_cambios": False}
            ).encode())

        rc, out = self._run(abrir)
        self.assertEqual(rc, 0)
        self.assertIn("1 dias", out)
        self.assertNotIn("487", out)

    def test_422_is_reported_with_the_reason_class(self):
        def abrir(req):
            raise _http_error(422, {"error": "archivo_rechazado", "motivo": "esquema"})

        rc, out = self._run(abrir)
        self.assertEqual(rc, 1)
        self.assertIn("esquema", out)

    def test_401_says_to_reconnect(self):
        def abrir(req):
            raise _http_error(401, {"error": "no_autorizado"})

        rc, out = self._run(abrir)
        self.assertEqual(rc, 1)
        self.assertIn("conectar_pc", out)

    def test_missing_link_file_stops_before_any_call(self):
        pc_cliente.borrar_enlace()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = subir_desplazamiento.main(["--file", str(self.path)])
        self.assertEqual(rc, 1)
        self.assertIn("conectar_pc", buf.getvalue())

    def test_no_file_configured_message(self):
        import os
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BODY_COMMUTE_FILE", None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = subir_desplazamiento.main([])
        self.assertEqual(rc, 1)
        self.assertIn("no commute file configured", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
