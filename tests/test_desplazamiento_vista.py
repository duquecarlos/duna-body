"""BODY-DESPLAZAMIENTO-3: the Tendencias commute group and its freshness line, Día's one-line
provenance note, and the per-source delete on Privacidad. Invented demo data only, no real
location or health figures.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from tests._db import open_conn

from body import config, db, desplazamiento_import as di, fuentes, identity, privacidad, web
from body.agente import herramientas

HOY = date.today()


def _csv(rows, computed_at="2026-01-01T06:00:00+00:00", header=True):
    lines = [",".join(di.HEADER)] if header else []
    for day, vm, vt, wm, cm in rows:
        lines.append(f"{day},{vm},{vt},{wm},{cm},{computed_at}")
    return "\n".join(lines) + "\n"


def _fila_fuente(conn, person_id, fuente=di.FUENTE, primer_dia=None, ultimo_dia=None, dias=1,
                  importado_at=None, ultimo_error=None, ultimo_error_at=None):
    conn.execute(
        "INSERT INTO fuente_importacion (person_id, fuente, primer_dia, ultimo_dia, computed_at, dias, "
        "importado_at, ultimo_error, ultimo_error_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person_id, fuente, primer_dia, ultimo_dia, "2026-01-01T00:00:00+00:00", dias,
         importado_at, ultimo_error, ultimo_error_at),
    )
    conn.commit()


# ---- ruling 1: estado_fuente / the registry -------------------------------------------------------

class RegistryTest(unittest.TestCase):
    def test_keys_are_read_from_the_importer_s_own_mapping(self):
        self.assertEqual(fuentes.FUENTES[di.FUENTE]["metric_keys"], tuple(di.VIDA_KEYS.keys()))

    def test_fuente_de_metrica(self):
        self.assertEqual(fuentes.fuente_de_metrica("vehicle_minutes"), di.FUENTE)
        self.assertIsNone(fuentes.fuente_de_metrica("resting_hr"))


class EstadoFuenteTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "estado.db")
        db.init(self.conn)

    def test_no_row_is_never_imported(self):
        self.assertIsNone(fuentes.estado_fuente(self.conn, 1, di.FUENTE, HOY))

    def test_a_row_with_only_an_error_is_still_never_imported(self):
        _fila_fuente(self.conn, 1, importado_at=None, ultimo_error="esquema",
                     ultimo_error_at="2026-01-01T00:00:00+00:00")
        self.assertIsNone(fuentes.estado_fuente(self.conn, 1, di.FUENTE, HOY))

    def test_unknown_fuente_is_none(self):
        self.assertIsNone(fuentes.estado_fuente(self.conn, 1, "no-such-fuente", HOY))

    def test_fresh_import_is_not_desactualizada_and_has_no_error(self):
        _fila_fuente(self.conn, 1, primer_dia=(HOY - timedelta(10)).isoformat(),
                     ultimo_dia=(HOY - timedelta(2)).isoformat(), dias=9,
                     importado_at="2026-01-01T00:00:00+00:00")
        estado = fuentes.estado_fuente(self.conn, 1, di.FUENTE, HOY)
        self.assertFalse(estado["desactualizada"])
        self.assertIsNone(estado["error"])
        self.assertEqual(estado["dias"], 9)

    def test_old_ultimo_dia_is_desactualizada(self):
        _fila_fuente(self.conn, 1, primer_dia=(HOY - timedelta(60)).isoformat(),
                     ultimo_dia=(HOY - timedelta(50)).isoformat(),
                     importado_at="2026-01-01T00:00:00+00:00")
        estado = fuentes.estado_fuente(self.conn, 1, di.FUENTE, HOY)
        self.assertTrue(estado["desactualizada"])

    def test_error_newer_than_last_success_is_reported(self):
        _fila_fuente(self.conn, 1, primer_dia=(HOY - timedelta(10)).isoformat(),
                     ultimo_dia=(HOY - timedelta(2)).isoformat(),
                     importado_at="2026-01-01T00:00:00+00:00", ultimo_error="dia_futuro",
                     ultimo_error_at="2026-02-01T00:00:00+00:00")
        estado = fuentes.estado_fuente(self.conn, 1, di.FUENTE, HOY)
        self.assertEqual(estado["error"], "dia_futuro")

    def test_error_older_than_last_success_is_not_reported(self):
        _fila_fuente(self.conn, 1, primer_dia=(HOY - timedelta(10)).isoformat(),
                     ultimo_dia=(HOY - timedelta(2)).isoformat(),
                     importado_at="2026-02-01T00:00:00+00:00", ultimo_error="dia_futuro",
                     ultimo_error_at="2026-01-01T00:00:00+00:00")
        estado = fuentes.estado_fuente(self.conn, 1, di.FUENTE, HOY)
        self.assertIsNone(estado["error"])


# ---- ruling 2: the Tendencias freshness line, es/en, every variant --------------------------------

class _PageTestCase(unittest.TestCase):
    def setUp(self):
        orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", orig_db_path))
        config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / (self.id().rsplit(".", 1)[-1] + ".db")
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.init(self.conn)
        # The default 9 need at least one card for /trends to render at all, rather than its
        # own "soon" empty state, before the commute group (behind "Ver todas las medidas")
        # ever gets built.
        d0 = HOY - timedelta(20)
        for i in range(15):
            db.set_metric(self.conn, (d0 + timedelta(i)).isoformat(), "resting_hr", 58.0 - i * 0.1, "garmin", 1)
        self.conn.commit()
        self.addCleanup(web.app.dependency_overrides.clear)
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    def _page(self, lang: str) -> str:
        db.set_language(self.conn, lang, 1)
        self.conn.commit()
        return self.client.get("/trends").text


class FreshnessLineTest(_PageTestCase):
    def test_never_imported(self):
        self.assertIn("Aún no hay datos de desplazamiento", self._page("es"))
        self.assertIn("No commute data yet", self._page("en"))

    def test_fresh(self):
        _fila_fuente(self.conn, 1, primer_dia=(HOY - timedelta(10)).isoformat(),
                     ultimo_dia=(HOY - timedelta(2)).isoformat(),
                     importado_at="2026-01-01T00:00:00+00:00")
        es, en = self._page("es"), self._page("en")
        self.assertIn("Datos hasta", es)
        self.assertNotIn("Hace más de", es)
        self.assertIn("Data up to", en)
        self.assertNotIn("No new data for over", en)

    def test_stale(self):
        _fila_fuente(self.conn, 1, primer_dia=(HOY - timedelta(90)).isoformat(),
                     ultimo_dia=(HOY - timedelta(50)).isoformat(),
                     importado_at="2026-01-01T00:00:00+00:00")
        es, en = self._page("es"), self._page("en")
        self.assertIn("Datos hasta", es)
        self.assertIn("Hace más de 45 días", es)
        self.assertIn("Data up to", en)
        self.assertIn("No new data for over 45 days", en)

    def test_error_replaces_the_freshness_line(self):
        _fila_fuente(self.conn, 1, primer_dia=(HOY - timedelta(10)).isoformat(),
                     ultimo_dia=(HOY - timedelta(2)).isoformat(),
                     importado_at="2026-01-01T00:00:00+00:00", ultimo_error="esquema",
                     ultimo_error_at="2026-02-01T00:00:00+00:00")
        es, en = self._page("es"), self._page("en")
        self.assertIn("se rechazó", es)
        self.assertNotIn("Datos hasta", es)
        self.assertIn("was refused", en)
        self.assertNotIn("Data up to", en)


# ---- ruling 2: the assistant's leer_tendencias gains the group through TODAS -----------------------

class LeerTendenciasIncludesCommuteTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "leer_tendencias.db")
        db.init(self.conn)

    def test_leer_tendencias_accepts_vehicle_minutes(self):
        d0 = HOY - timedelta(20)
        for i in range(14):
            db.set_metric(self.conn, (d0 + timedelta(i)).isoformat(), "vehicle_minutes", 20.0 + i, di.FUENTE, 1)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_tendencias", {"metric": "vehicle_minutes"}, "completo")
        self.assertIn("vehicle_minutes", out["metricas"])

    def test_leer_tendencias_with_no_filter_still_finds_it(self):
        d0 = HOY - timedelta(20)
        for i in range(14):
            db.set_metric(self.conn, (d0 + timedelta(i)).isoformat(), "vehicle_minutes", 20.0 + i, di.FUENTE, 1)
        self.conn.commit()
        out = herramientas.ejecutar(self.conn, 1, "leer_tendencias", {}, "completo")
        self.assertEqual(set(out["metricas"]), {"vehicle_minutes"})


# ---- ruling 3: Día names the source once, and shows no row past ultimo_dia -------------------------

class DiaFuenteLineTest(_PageTestCase):
    def test_names_the_source_on_a_day_with_data_and_not_after_ultimo_dia(self):
        dia1, dia2 = (HOY - timedelta(5)).isoformat(), (HOY - timedelta(4)).isoformat()
        texto = _csv([(dia1, 30, 2, 10, 0), (dia2, 25, 1, 5, 0)])
        di.importar(self.conn, 1, texto)
        self.conn.commit()

        db.set_language(self.conn, "es", 1)
        self.conn.commit()
        page_con_dato = self.client.get(f"/day/{dia2}").text
        self.assertIn("De tu historial de ubicaciones", page_con_dato)

        dia_sin_dato = (HOY - timedelta(3)).isoformat()
        page_sin_dato = self.client.get(f"/day/{dia_sin_dato}").text
        self.assertNotIn("De tu historial de ubicaciones", page_sin_dato)


# ---- BODY-DESPLAZAMIENTO-3-M fix 1 (ruling 3): no row of bare "--"s on a day a vida source --------
# ------------------------------------------- never covered -----------------------------------------

class DiaCommuteCardsTest(_PageTestCase):
    """body/rutas/hoy.py's day_view(): the four commute cards (vehicle_minutes, vehicle_trips,
    walking_minutes, cycling_minutes) show only on a day the commute source actually covers --
    never a row of bare "--"s past its own ultimo_dia, and never at all for a person who has
    never imported it."""

    COMMUTE_LABELS_ES = ("Tiempo en vehículo", "Viajes en vehículo",
                         "Minutos caminando", "Minutos en bicicleta")

    def test_no_commute_cards_the_day_after_ultimo_dia(self):
        dia1, dia2 = (HOY - timedelta(5)).isoformat(), (HOY - timedelta(4)).isoformat()
        di.importar(self.conn, 1, _csv([(dia1, 30, 2, 10, 0), (dia2, 25, 1, 5, 0)]))
        self.conn.commit()
        db.set_language(self.conn, "es", 1)
        self.conn.commit()

        page_con_dato = self.client.get(f"/day/{dia2}").text
        for label in self.COMMUTE_LABELS_ES:
            self.assertIn(label, page_con_dato)
        self.assertIn('id="vehicle_minutes"', page_con_dato)

        dia_sin_dato = (HOY - timedelta(3)).isoformat()
        page_sin_dato = self.client.get(f"/day/{dia_sin_dato}").text
        for label in self.COMMUTE_LABELS_ES:
            self.assertNotIn(label, page_sin_dato)
        self.assertNotIn('id="vehicle_minutes"', page_sin_dato)
        self.assertNotIn('id="vehicle_trips"', page_sin_dato)
        self.assertNotIn('id="walking_minutes"', page_sin_dato)
        self.assertNotIn('id="cycling_minutes"', page_sin_dato)

    def test_never_imported_person_sees_no_commute_cards_on_any_day(self):
        db.set_language(self.conn, "es", 1)
        self.conn.commit()
        page = self.client.get(f"/day/{HOY.isoformat()}").text
        for label in self.COMMUTE_LABELS_ES:
            self.assertNotIn(label, page)
        self.assertNotIn('id="vehicle_minutes"', page)

    def test_a_day_with_data_still_shows_all_four_and_the_source_line(self):
        dia = (HOY - timedelta(5)).isoformat()
        di.importar(self.conn, 1, _csv([(dia, 30, 2, 10, 0)]))
        self.conn.commit()
        db.set_language(self.conn, "es", 1)
        self.conn.commit()
        page = self.client.get(f"/day/{dia}").text
        for label in self.COMMUTE_LABELS_ES:
            self.assertIn(label, page)
        self.assertIn("De tu historial de ubicaciones", page)


# ---- ruling 4: the per-source delete ---------------------------------------------------------------

class DeleteFuenteTest(unittest.TestCase):
    def setUp(self):
        self.conn = open_conn(self, Path(tempfile.mkdtemp(dir=TMP)) / "borrar_fuente.db")
        db.init(self.conn)
        self.conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        self.conn.commit()

    def test_removes_only_this_person_s_commute_rows_keeps_garmin_and_self_same_day(self):
        dia = (HOY - timedelta(5)).isoformat()
        di.importar(self.conn, 1, _csv([(dia, 30, 2, 10, 0)]))
        di.importar(self.conn, 2, _csv([(dia, 40, 3, 15, 5)]))
        db.set_metric(self.conn, dia, "resting_hr", 55.0, "garmin", 1)
        db.set_metric(self.conn, dia, "snoring_min", 12.0, "self", 1)
        self.conn.commit()

        ok = privacidad.delete_fuente(self.conn, 1, di.FUENTE)
        self.assertTrue(ok)

        rows_1 = self.conn.execute(
            "SELECT metric_key FROM daily_metric WHERE person_id = 1 AND metric_key IN "
            "('vehicle_minutes', 'vehicle_trips', 'walking_minutes', 'cycling_minutes')"
        ).fetchall()
        self.assertEqual(rows_1, [])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM daily_metric WHERE person_id = 1 AND metric_key = 'resting_hr'").fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM daily_metric WHERE person_id = 1 AND metric_key = 'snoring_min'").fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM fuente_importacion WHERE person_id = 1 AND fuente = ?", (di.FUENTE,)).fetchone())

        rows_2 = self.conn.execute(
            "SELECT COUNT(*) AS n FROM daily_metric WHERE person_id = 2 AND metric_key = 'vehicle_minutes'"
        ).fetchone()["n"]
        self.assertEqual(rows_2, 1)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM fuente_importacion WHERE person_id = 2 AND fuente = ?", (di.FUENTE,)).fetchone())

    def test_unknown_fuente_is_refused(self):
        di.importar(self.conn, 1, _csv([((HOY - timedelta(5)).isoformat(), 30, 2, 10, 0)]))
        ok = privacidad.delete_fuente(self.conn, 1, "no-such-fuente")
        self.assertFalse(ok)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM fuente_importacion WHERE person_id = 1").fetchone())

    def test_fuente_never_imported_by_this_person_is_refused(self):
        self.assertFalse(privacidad.delete_fuente(self.conn, 1, di.FUENTE))

    def test_reupload_after_delete_imports_again(self):
        texto = _csv([((HOY - timedelta(5)).isoformat(), 30, 2, 10, 0)])
        di.importar(self.conn, 1, texto)
        privacidad.delete_fuente(self.conn, 1, di.FUENTE)
        resultado = di.importar(self.conn, 1, texto)
        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["sin_cambios"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM daily_metric WHERE person_id = 1 AND metric_key = 'vehicle_minutes'").fetchone())


class BorrarFuenteRouteTest(_PageTestCase):
    def test_confirmation_step_renders_without_js_then_deletes(self):
        dia1, dia2 = (HOY - timedelta(6)).isoformat(), (HOY - timedelta(5)).isoformat()
        di.importar(self.conn, 1, _csv([(dia1, 30, 2, 10, 0), (dia2, 25, 1, 5, 0)]))
        self.conn.commit()

        listado = self.client.get("/privacidad").text
        self.assertIn("Borrar estos datos", listado)

        confirmar = self.client.get("/privacidad", params={"confirmar": di.FUENTE}).text
        self.assertIn('action="/privacidad/fuente/borrar"', confirmar)
        self.assertIn(f'value="{di.FUENTE}"', confirmar)
        self.assertIn("Sí, borrar", confirmar)
        # BODY-DESPLAZAMIENTO-3-M fix 2: the label is quoted, never possessive ("el/los datos de X").
        self.assertIn("¿Borrar «Resumen diario de desplazamiento»? Una nueva subida lo vuelve a traer.",
                      confirmar)

        r = self.client.post("/privacidad/fuente/borrar", data={"fuente": di.FUENTE})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM fuente_importacion WHERE person_id = 1 AND fuente = ?", (di.FUENTE,)).fetchone())

        despues = self.client.get(r.headers["location"]).text
        self.assertIn("Se borró «Resumen diario de desplazamiento». Una nueva subida lo vuelve a traer.", despues)

    def test_confirmation_and_done_wording_in_english(self):
        di.importar(self.conn, 1, _csv([((HOY - timedelta(5)).isoformat(), 30, 2, 10, 0)]))
        db.set_language(self.conn, "en", 1)
        self.conn.commit()

        confirmar = self.client.get("/privacidad", params={"confirmar": di.FUENTE}).text
        self.assertIn("Delete “Daily commute summary”? A new upload brings it back.", confirmar)

        r = self.client.post("/privacidad/fuente/borrar", data={"fuente": di.FUENTE})
        despues = self.client.get(r.headers["location"]).text
        self.assertIn("“Daily commute summary” deleted. A new upload brings it back.", despues)

    def test_unknown_fuente_posted_is_404(self):
        r = self.client.post("/privacidad/fuente/borrar", data={"fuente": "no-such-fuente"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
