"""BODY-DESPLAZAMIENTO-ASISTENTE-1: the assistant's own door onto Personal's commute-summary CSV
-- a `propuesta` tool, `importar_desplazamiento`, validated by the SAME `desplazamiento_import.
validar` the device route (POST /api/desplazamiento) calls, before any proposal exists. Invented
30-day CSV fixtures only, no real health or location figure.
"""
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, desplazamiento_import as di
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas, propuestas
from body.i18n import T
from body.rutas import asistente, comun


def _csv(rows, computed_at="2026-11-01T06:00:00+00:00", header=True):
    lines = [",".join(di.HEADER)] if header else []
    for day, vm, vt, wm, cm in rows:
        lines.append(f"{day},{vm},{vt},{wm},{cm},{computed_at}")
    return "\n".join(lines) + "\n"


def _treinta_dias(computed_at="2026-11-01T06:00:00+00:00", start="2026-10-01"):
    from datetime import date, timedelta
    inicio = date.fromisoformat(start)
    rows = [((inicio + timedelta(days=i)).isoformat(), 20 + i, 1 + (i % 3), 5 + (i % 7), i % 4)
            for i in range(30)]
    return _csv(rows, computed_at=computed_at)


def _conn(case: unittest.TestCase, name: str):
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / name)
    db.init(conn)
    return conn


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _commute_rows(conn, person_id: int = 1):
    return conn.execute(
        "SELECT day, metric_key, value, source FROM daily_metric WHERE person_id = ? AND metric_key IN "
        "(?, ?, ?, ?) ORDER BY day, metric_key",
        (person_id, *di.COUNT_COLUMNS),
    ).fetchall()


def _fuente(conn, person_id: int = 1):
    fila = conn.execute(
        "SELECT * FROM fuente_importacion WHERE person_id = ? AND fuente = ?", (person_id, di.FUENTE)
    ).fetchone()
    return dict(fila) if fila is not None else None


# ---- ruling 1: a valid file -> one proposal, nothing written yet ------------------------------

class ProponerTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "proponer.db")

    def test_valid_file_proposes_with_no_write(self):
        texto = _treinta_dias()
        out = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                     "resumen", _cid(self.conn))
        self.assertEqual(out["estado"], "propuesta")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 1)
        self.assertEqual(_commute_rows(self.conn), [])
        self.assertIsNone(_fuente(self.conn))

    def test_card_names_only_counts_and_dates(self):
        texto = _treinta_dias()
        out = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                     "resumen", _cid(self.conn))
        registro = out["_registro"]
        despues = registro["despues_json"]
        self.assertEqual(set(despues), {"contenido", "dias", "primer_dia", "ultimo_dia", "dias_cambio"})
        self.assertEqual(despues["dias"], 30)
        self.assertEqual(despues["primer_dia"], "2026-10-01")
        self.assertEqual(despues["ultimo_dia"], "2026-10-30")
        self.assertEqual(despues["dias_cambio"], 30)

    def test_malformed_file_is_the_reason_class_no_proposal(self):
        with self.assertRaises(herramientas.ToolError) as cm:
            herramientas.ejecutar(self.conn, 1, "importar_desplazamiento",
                                   {"contenido": "day,vehicle_minutes\n2026-01-01,30\n"}, "resumen", _cid(self.conn))
        self.assertEqual(str(cm.exception), "esquema")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 0)

    def test_future_day_is_refused_no_proposal(self):
        texto = _csv([("2099-01-01", 30, 2, 10, 0)])
        with self.assertRaises(herramientas.ToolError) as cm:
            herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                   "resumen", _cid(self.conn))
        self.assertEqual(str(cm.exception), "dia_futuro")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 0)

    def test_oversize_file_refused_like_the_route(self):
        texto = "x" * (di.MAX_BYTES + 1)
        with self.assertRaises(herramientas.ToolError) as cm:
            herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                   "resumen", _cid(self.conn))
        self.assertEqual(str(cm.exception), "demasiado_grande")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM propuesta_agente").fetchone()[0], 0)

    def test_tool_refused_below_resumen(self):
        texto = _treinta_dias()
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                   "ninguno", _cid(self.conn))


# ---- ruling B: the card agrees in number in both languages ------------------------------------

class TarjetaTest(unittest.TestCase):
    """BODY-DESPLAZAMIENTO-ASISTENTE-1-W ruling B: the day count and the changed-days count each
    read singular at 1, and a one-day file names its single date rather than a range 'from X to X'."""

    def setUp(self):
        self.conn = _conn(self, "tarjeta.db")

    def _proponer(self, texto):
        return herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                      "resumen", _cid(self.conn))

    def _despues(self, texto):
        return self._proponer(texto)["_registro"]["despues_json"]

    def _tarjeta(self, lang, despues):
        defs = comun._defs(self.conn)
        _, texto, _ = asistente.TARJETAS["importar_desplazamiento"](T(lang), defs, despues, None, self.conn, 1)
        return texto

    def test_thirty_days_range_plural_in_both_languages(self):
        despues = self._despues(_treinta_dias())
        self.assertEqual(
            self._tarjeta("es", despues),
            "30 días de desplazamiento, del 1 oct 2026 al 30 oct 2026 (30 días cambiarían)",
        )
        self.assertEqual(
            self._tarjeta("en", despues),
            "30 days of commute data, from 1 Oct 2026 to 30 Oct 2026 (30 days would change)",
        )

    def test_one_day_file_names_its_single_date_not_a_range(self):
        texto = _csv([("2026-10-01", 20, 1, 5, 0)])
        despues = self._despues(texto)
        self.assertEqual(despues["dias"], 1)
        self.assertEqual(despues["dias_cambio"], 1)
        self.assertEqual(
            self._tarjeta("es", despues),
            "1 día de desplazamiento, el 1 oct 2026 (1 día cambiaría)",
        )
        self.assertEqual(
            self._tarjeta("en", despues),
            "1 day of commute data, on 1 Oct 2026 (1 day would change)",
        )

    def test_reimport_with_one_changed_day_reads_singular_for_cambios_only(self):
        texto_viejo = _treinta_dias(computed_at="2026-11-01T06:00:00+00:00")
        out_viejo = self._proponer(texto_viejo)
        propuestas.aplicar(self.conn, 1, out_viejo["propuesta_id"])

        from datetime import date, timedelta
        inicio = date.fromisoformat("2026-10-01")
        filas = [((inicio + timedelta(days=i)).isoformat(), 20 + i, 1 + (i % 3), 5 + (i % 7), i % 4)
                 for i in range(30)]
        filas[0] = (filas[0][0], 999, filas[0][2], filas[0][3], filas[0][4])
        texto_nuevo = _csv(filas, computed_at="2026-12-01T06:00:00+00:00")
        despues = self._despues(texto_nuevo)
        self.assertEqual(despues["dias"], 30)
        self.assertEqual(despues["dias_cambio"], 1)
        self.assertEqual(
            self._tarjeta("es", despues),
            "30 días de desplazamiento, del 1 oct 2026 al 30 oct 2026 (1 día cambiaría)",
        )
        self.assertEqual(
            self._tarjeta("en", despues),
            "30 days of commute data, from 1 Oct 2026 to 30 Oct 2026 (1 day would change)",
        )


# ---- ruling 2: applying writes the SAME rows the device route writes --------------------------

class AplicarTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "aplicar.db")

    def test_applying_writes_the_same_rows_as_the_device_route(self):
        texto = _treinta_dias()
        out = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                     "resumen", _cid(self.conn))
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "aplicada")

        gemelo = _conn(self, "gemelo.db")
        di.importar(gemelo, 1, texto)

        self.assertEqual(
            [(r["day"], r["metric_key"], r["value"], r["source"]) for r in _commute_rows(self.conn)],
            [(r["day"], r["metric_key"], r["value"], r["source"]) for r in _commute_rows(gemelo)],
        )
        self.assertEqual(_fuente(self.conn)["computed_at"], _fuente(gemelo)["computed_at"])

    def test_reimport_of_the_same_text_says_zero_days_change(self):
        texto = _treinta_dias()
        out1 = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                      "resumen", _cid(self.conn))
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])

        out2 = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                      "resumen", _cid(self.conn))
        self.assertEqual(out2["_registro"]["despues_json"]["dias_cambio"], 0)

    def test_a_later_file_with_some_days_unchanged_counts_only_the_real_diffs(self):
        texto1 = _treinta_dias(computed_at="2026-11-01T06:00:00+00:00")
        out1 = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto1},
                                      "resumen", _cid(self.conn))
        propuestas.aplicar(self.conn, 1, out1["propuesta_id"])

        # Same 30 days, same values, a NEW computed_at -- importar() would still rewrite the whole
        # fuente_importacion row (a different computed_at), but no day's own value actually moves.
        texto2 = _treinta_dias(computed_at="2026-12-01T06:00:00+00:00")
        out2 = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto2},
                                      "resumen", _cid(self.conn))
        self.assertEqual(out2["_registro"]["despues_json"]["dias_cambio"], 0)


# ---- ruling 3: undo restores the previous state, or removes the rows when there were none -----

class DeshacerTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "deshacer.db")

    def test_undo_removes_everything_when_there_was_no_prior_import(self):
        texto = _treinta_dias()
        out = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                     "resumen", _cid(self.conn))
        pid = out["propuesta_id"]
        propuestas.aplicar(self.conn, 1, pid)
        self.assertNotEqual(_commute_rows(self.conn), [])

        resultado = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(_commute_rows(self.conn), [])
        self.assertIsNone(_fuente(self.conn))

    def test_undo_restores_the_previous_import_exactly(self):
        texto_viejo = _treinta_dias(computed_at="2026-11-01T06:00:00+00:00")
        di.importar(self.conn, 1, texto_viejo)
        self.conn.commit()
        antes_filas = _commute_rows(self.conn)
        antes_fuente = _fuente(self.conn)

        texto_nuevo = _treinta_dias(computed_at="2026-12-01T06:00:00+00:00", start="2026-10-15")
        out = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto_nuevo},
                                     "resumen", _cid(self.conn))
        pid = out["propuesta_id"]
        propuestas.aplicar(self.conn, 1, pid)
        self.assertNotEqual(_commute_rows(self.conn), antes_filas)

        resultado = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(resultado["estado"], "deshecha")
        self.assertEqual(_commute_rows(self.conn), antes_filas)
        self.assertEqual(_fuente(self.conn), antes_fuente)

    def test_drift_since_apply_refuses_the_undo(self):
        texto = _treinta_dias()
        out = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                     "resumen", _cid(self.conn))
        pid = out["propuesta_id"]
        propuestas.aplicar(self.conn, 1, pid)

        # Something else re-imports a different file afterwards -- the undo must refuse, never
        # overwrite that later, real import. A start safely before `texto`'s own range, never a
        # future day regardless of when this test runs.
        texto2 = _treinta_dias(computed_at="2027-01-01T06:00:00+00:00", start="2026-08-01")
        di.importar(self.conn, 1, texto2)
        self.conn.commit()

        resultado = propuestas.deshacer(self.conn, 1, pid)
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")


# ---- ruling 4: no door-specific code -- another person's data is never touched ----------------

class AislamientoTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn(self, "aislamiento.db")
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()

    def test_another_persons_commute_data_is_never_touched(self):
        texto_b = _treinta_dias(computed_at="2026-09-01T06:00:00+00:00")
        di.importar(self.conn, 2, texto_b)
        self.conn.commit()
        filas_b_antes = _commute_rows(self.conn, 2)

        texto_a = _treinta_dias(computed_at="2026-11-01T06:00:00+00:00")
        out = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto_a},
                                     "resumen", _cid(self.conn, 1))
        propuestas.aplicar(self.conn, 1, out["propuesta_id"])

        self.assertEqual(_commute_rows(self.conn, 2), filas_b_antes)
        self.assertEqual(len(_commute_rows(self.conn, 1)), 120)

    def test_preview_dias_cambio_ignores_another_persons_values(self):
        texto = _treinta_dias()
        di.importar(self.conn, 2, texto)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "importar_desplazamiento", {"contenido": texto},
                                     "resumen", _cid(self.conn, 1))
        self.assertEqual(out["_registro"]["despues_json"]["dias_cambio"], 30)


if __name__ == "__main__":
    unittest.main()
