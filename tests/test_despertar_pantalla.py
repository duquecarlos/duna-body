"""BODY-DESPERTAR-REAL-7: the screen for body/despertar_real.py's own model and the
`corregir_despertar` propuesta (BODY-DESPERTAR-REAL-4) -- Día's own line/button/card/curve
(rulings 1-3), Hoy's own line (ruling 4), and their cost (ruling 5). Invented demo data only, no
real health figures.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, fechas, identity, web
from tests.test_despertar_real import (BASE_FC, BASE_MUESTRAS, DAY, END_LOCAL, START_LOCAL, _comida, _guardar_fc,
                                        _serie, _sesion)


def _web_conn(case: unittest.TestCase, name: str) -> "db.sqlite3.Connection":
    """The same web-reading-connection shape tests/test_noche_inusual.py's own `_web_conn` uses."""
    orig = config.DB_PATH
    case.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    case.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / name
    conn = db.connect()
    case.addCleanup(conn.close)
    db.init(conn)
    web.app.dependency_overrides[identity.current_person_id] = lambda: 1
    return conn


def _seed_levantado_media(conn) -> None:
    """QuietAwakeThenRiseTest's own quiet-then-flat shape plus a logged meal before any rise --
    the SAME fixture tests/test_despertar_real.py's `EventoAcotaLevantadoTest` uses: propuesta
    'levantado' at 07:15, confianza 'media' (the event-bound downgrade), full FC coverage so the
    curve has points throughout."""
    _sesion(conn, 1, DAY, START_LOCAL, END_LOCAL)
    _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 9 * 60, 2, BASE_FC + 8.0))
    _comida(conn, 1, DAY, f"{DAY}T07:15:00")
    db.set_metric(conn, DAY, "sleep_minutes", 420.0, "garmin", 1)
    conn.commit()


def _seed_sueno_alta(conn) -> None:
    """DozingTailTest's own shape: propuesta 'sueno' at 06:30, confianza 'alta' (full coverage, no
    event)."""
    _sesion(conn, 1, DAY, START_LOCAL, END_LOCAL)
    _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 6 * 60 + 28, 2, BASE_FC + 2.0)
                + _serie(DAY, 6 * 60 + 30, 9 * 60, 2, BASE_FC + 8.0))
    db.set_metric(conn, DAY, "sleep_minutes", 420.0, "garmin", 1)
    conn.commit()


def _seed_baja(conn) -> None:
    """WatchOffGapTest's own shape: nothing after Garmin's own end -- confianza 'baja'."""
    _sesion(conn, 1, DAY, START_LOCAL, END_LOCAL)
    _guardar_fc(conn, 1, DAY, BASE_MUESTRAS)
    db.set_metric(conn, DAY, "sleep_minutes", 420.0, "garmin", 1)
    conn.commit()


def _seed_ninguna_alta(conn) -> None:
    """ImmediateRiseTest's own shape: Garmin was already right -- propuesta 'ninguna', confianza
    'alta'."""
    _sesion(conn, 1, DAY, START_LOCAL, END_LOCAL)
    _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 9 * 60, 2, BASE_FC + 20.0))
    db.set_metric(conn, DAY, "sleep_minutes", 420.0, "garmin", 1)
    conn.commit()


def _seed_ambos_alta(conn) -> None:
    """BODY-DESPERTAR-REAL-6A-R2's own DozingThenQuietThenRiseTest shape (tests/test_despertar_
    real.py): dozed past Garmin's own end (sueno at 06:30), quiet awake, then a later sustained
    rise (levantado at 07:00) -- a morning that offers BOTH."""
    _sesion(conn, 1, DAY, START_LOCAL, END_LOCAL)
    _guardar_fc(conn, 1, DAY, BASE_MUESTRAS + _serie(DAY, 5 * 60, 6 * 60 + 28, 2, BASE_FC + 2.0)
                + _serie(DAY, 6 * 60 + 30, 6 * 60 + 58, 2, BASE_FC + 8.0)
                + _serie(DAY, 7 * 60, 9 * 60, 2, BASE_FC + 20.0))
    db.set_metric(conn, DAY, "sleep_minutes", 420.0, "garmin", 1)
    conn.commit()


def _propuesta_id(conn, tipo: str | None = None) -> int:
    sql = ("SELECT id, despues_json FROM propuesta_agente WHERE person_id = 1 AND "
           "accion = 'corregir_despertar' AND estado = 'propuesta' ORDER BY creada_en DESC")
    for f in conn.execute(sql).fetchall():
        if tipo is None or json.loads(f["despues_json"]).get("tipo") == tipo:
            return f["id"]
    raise AssertionError(f"no pending corregir_despertar propuesta found for tipo={tipo!r}")


class LineaYBotonTest(unittest.TestCase):
    """Ruling 1: the line and the "¿Corregir?" button, es and en."""

    def test_levantado_media_muestra_linea_y_boton_es(self):
        conn = _web_conn(self, "linea_boton_es.db")
        _seed_levantado_media(conn)
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertIn("Parece que te levantaste alrededor de las 07:15 (Garmin marcó 05:00).", page)
        self.assertIn("¿Corregir?", page)
        self.assertIn(f'action="/day/{DAY}/despertar/proponer"', page)
        self.assertNotIn("None", page)

    def test_levantado_media_muestra_linea_y_boton_en(self):
        conn = _web_conn(self, "linea_boton_en.db")
        db.set_language(conn, "en", 1)
        _seed_levantado_media(conn)
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertIn("Looks like you got up around 07:15 (Garmin says 05:00).", page)
        self.assertIn("Correct it?", page)

    def test_sueno_alta_muestra_linea_es(self):
        conn = _web_conn(self, "sueno_es.db")
        _seed_sueno_alta(conn)
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertIn("Parece que seguiste durmiendo hasta las 06:30 (Garmin marcó 05:00).", page)
        self.assertIn("¿Corregir?", page)

    def test_baja_no_muestra_linea(self):
        conn = _web_conn(self, "baja.db")
        _seed_baja(conn)
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertNotIn("¿Corregir?", page)
        self.assertNotIn("Parece que", page)

    def test_ninguna_no_muestra_linea(self):
        conn = _web_conn(self, "ninguna.db")
        _seed_ninguna_alta(conn)
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertNotIn("¿Corregir?", page)
        self.assertNotIn("Parece que", page)


class ProponerAplicarDeshacerTest(unittest.TestCase):
    """Ruling 1-2: the whole cycle through the screen alone -- propose, the card, apply, the
    correction, undo back to the start."""

    def test_ciclo_completo_levantado(self):
        conn = _web_conn(self, "ciclo.db")
        _seed_levantado_media(conn)
        client = TestClient(web.app, follow_redirects=False)

        page = client.get(f"/day/{DAY}").text
        self.assertIn("¿Corregir?", page)

        r = client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "levantado"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/day/{DAY}")
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM propuesta_agente WHERE person_id = 1 AND accion = 'corregir_despertar' "
            "AND estado = 'propuesta'",
        ).fetchone()["n"]
        self.assertEqual(n, 1, "exactly one corregir_despertar propuesta")

        page = client.get(f"/day/{DAY}").text
        self.assertNotIn("¿Corregir?", page)
        self.assertIn("Te levantaste a las 07:15 (Garmin marcó tu despertar a las 05:00)", page)
        self.assertIn("Aplicar", page)
        self.assertIn("Descartar", page)
        # the explanatory line stays, for context, beside the proposal's own card.
        self.assertIn("Parece que te levantaste alrededor de las 07:15", page)

        propuesta_id = _propuesta_id(conn)
        r = client.post(f"/day/{DAY}/despertar/propuestas/{propuesta_id}/aplicar")
        self.assertEqual(r.status_code, 303)
        correccion = db.despertar_correcciones(conn, 1, DAY).get("levantado")
        self.assertIsNotNone(correccion)
        self.assertEqual(correccion["hora_local"], "07:15")

        page = client.get(f"/day/{DAY}").text
        self.assertIn("Garmin marcó el fin del sueño a las 05:00.", page)
        self.assertIn("Tu corrección: te levantaste a las 07:15", page)
        self.assertNotIn("¿Corregir?", page)
        self.assertNotIn("Aplicar", page)
        self.assertNotIn("Descartar", page)
        self.assertIn("Deshacer", page)
        self.assertNotIn("None", page)

        r = client.post(f"/day/{DAY}/despertar/deshacer", data={"tipo": "levantado"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.despertar_correcciones(conn, 1, DAY), {})

        # back to the model's own line and button -- the one leftover 'aplicada' propuesta row
        # from before is never read again (ruling 2 reads db.despertar_correcciones directly).
        page = client.get(f"/day/{DAY}").text
        self.assertIn("¿Corregir?", page)
        self.assertIn("Parece que te levantaste alrededor de las 07:15", page)

    def test_descartar_borra_la_propuesta_pendiente_sin_corregir_nada(self):
        conn = _web_conn(self, "descartar.db")
        _seed_levantado_media(conn)
        client = TestClient(web.app, follow_redirects=False)
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "levantado"})
        propuesta_id = _propuesta_id(conn)
        r = client.post(f"/day/{DAY}/despertar/propuestas/{propuesta_id}/descartar")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.despertar_correcciones(conn, 1, DAY), {})
        page = client.get(f"/day/{DAY}").text
        self.assertIn("¿Corregir?", page)
        self.assertNotIn("Aplicar", page)


class AmbosTiposTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-6A-R2 ruling 4: a two-kind morning shows two lines and two buttons,
    then two "Tu corrección" lines and two Deshacer buttons once both are applied; undoing one
    leaves the other."""

    def test_dos_lineas_y_dos_botones(self):
        conn = _web_conn(self, "ambos_lineas.db")
        _seed_ambos_alta(conn)
        page = TestClient(web.app, follow_redirects=False).get(f"/day/{DAY}").text
        self.assertIn("Parece que seguiste durmiendo hasta las 06:30 (Garmin marcó 05:00).", page)
        self.assertIn("Parece que te levantaste alrededor de las 07:00 (Garmin marcó 05:00).", page)
        self.assertEqual(page.count("¿Corregir?"), 2)
        self.assertNotIn("None", page)

    def test_aplicar_ambos_muestra_dos_correcciones_y_dos_deshacer(self):
        conn = _web_conn(self, "ambos_aplicar.db")
        _seed_ambos_alta(conn)
        client = TestClient(web.app, follow_redirects=False)
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "sueno"})
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "levantado"})
        client.post(f"/day/{DAY}/despertar/propuestas/{_propuesta_id(conn, 'sueno')}/aplicar")
        client.post(f"/day/{DAY}/despertar/propuestas/{_propuesta_id(conn, 'levantado')}/aplicar")

        correcciones = db.despertar_correcciones(conn, 1, DAY)
        self.assertEqual(set(correcciones), {"sueno", "levantado"})

        page = client.get(f"/day/{DAY}").text
        self.assertIn("Tu corrección: dormiste hasta las 06:30", page)
        self.assertIn("Tu corrección: te levantaste a las 07:00", page)
        self.assertEqual(page.count("Deshacer"), 2)
        self.assertNotIn("¿Corregir?", page)
        self.assertNotIn("None", page)

    def test_deshacer_uno_deja_el_otro(self):
        conn = _web_conn(self, "ambos_deshacer.db")
        _seed_ambos_alta(conn)
        client = TestClient(web.app, follow_redirects=False)
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "sueno"})
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "levantado"})
        client.post(f"/day/{DAY}/despertar/propuestas/{_propuesta_id(conn, 'sueno')}/aplicar")
        client.post(f"/day/{DAY}/despertar/propuestas/{_propuesta_id(conn, 'levantado')}/aplicar")

        client.post(f"/day/{DAY}/despertar/deshacer", data={"tipo": "sueno"})
        correcciones = db.despertar_correcciones(conn, 1, DAY)
        self.assertEqual(set(correcciones), {"levantado"})

        page = client.get(f"/day/{DAY}").text
        self.assertIn("Tu corrección: te levantaste a las 07:00", page)
        self.assertNotIn("Tu corrección: dormiste", page)
        self.assertEqual(page.count("Deshacer"), 1)


class SuenoCorregidoTotalTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-6B ruling 4: once a 'sueno' correction is applied, Día's sleep card
    shows the CORRECTED total and window end, with a plain line under them naming Garmin's own
    figures -- both always visible. The stage bar and sueno_fc line are untouched by this slice.
    A 'levantado'-only correction changes neither total nor window (ruling 3: it changes no
    figure)."""

    def _aplicar_sueno(self, conn, client):
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "sueno"})
        client.post(f"/day/{DAY}/despertar/propuestas/{_propuesta_id(conn, 'sueno')}/aplicar")

    def test_dia_shows_the_corrected_total_and_window_end_with_garmins_own_line(self):
        conn = _web_conn(self, "sueno_corregido_dia.db")
        _seed_sueno_alta(conn)  # proposes 'sueno' at 06:30, Garmin's own end 05:00, sleep=420
        client = TestClient(web.app, follow_redirects=False)
        self._aplicar_sueno(conn, client)

        page = client.get(f"/day/{DAY}").text
        self.assertIn("8 h 30", page)   # 420 + 90 minutes, the corrected total
        self.assertIn("06:30", page)    # the corrected window end (== the correction's own hora)
        self.assertIn("Incluye tu corrección: Garmin marcó 7 h 00 hasta las 05:00.", page)
        self.assertNotIn("None", page)

    def test_leer_dia_states_the_corrected_total_and_garmins_own_labelled(self):
        from body.agente import herramientas
        conn = _web_conn(self, "sueno_corregido_leer_dia.db")
        _seed_sueno_alta(conn)
        client = TestClient(web.app, follow_redirects=False)
        self._aplicar_sueno(conn, client)

        out = herramientas.ejecutar(conn, 1, "leer_dia", {"day": DAY}, "completo")
        sleep_minutes = next(m for m in out["metrics"] if m["key"] == "sleep_minutes")
        self.assertEqual(sleep_minutes["value"], 510.0)
        self.assertEqual(out["sleep"]["sleep_minutes_garmin"], 420.0)

    def test_hoy_tile_carries_the_short_mark_when_corrected(self):
        conn = _web_conn(self, "sueno_corregido_hoy.db")
        _seed_sueno_alta(conn)
        client = TestClient(web.app, follow_redirects=False)
        page = client.get("/").text
        self.assertNotIn("incluye tu corrección", page)
        self._aplicar_sueno(conn, client)
        page = client.get("/").text
        self.assertIn("incluye tu corrección", page)
        self.assertNotIn("None", page)

    def test_levantado_only_leaves_the_total_and_window_untouched(self):
        conn = _web_conn(self, "levantado_total_sin_cambio.db")
        _seed_levantado_media(conn)  # proposes 'levantado' at 07:15, sleep=420, Garmin's end 05:00
        client = TestClient(web.app, follow_redirects=False)
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "levantado"})
        client.post(f"/day/{DAY}/despertar/propuestas/{_propuesta_id(conn, 'levantado')}/aplicar")

        page = client.get(f"/day/{DAY}").text
        self.assertIn("7 h 00", page)   # 420 minutes, unchanged
        self.assertNotIn("Incluye tu corrección", page)


class OtraPersonaTest(unittest.TestCase):
    """Another person's propuesta is refused, never applied or discarded on their behalf."""

    def test_aplicar_de_otra_persona_es_no_op(self):
        conn = _web_conn(self, "ajena.db")
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        conn.commit()
        _seed_levantado_media(conn)
        client = TestClient(web.app, follow_redirects=False)
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "levantado"})
        propuesta_id = _propuesta_id(conn)

        web.app.dependency_overrides[identity.current_person_id] = lambda: 2
        r = TestClient(web.app, follow_redirects=False).post(
            f"/day/{DAY}/despertar/propuestas/{propuesta_id}/aplicar")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.despertar_correcciones(conn, 1, DAY), {})
        fila = conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?", (propuesta_id,)).fetchone()
        self.assertEqual(fila["estado"], "propuesta", "another person's apply must not touch it")


class HoyLineaTest(unittest.TestCase):
    """Ruling 4: Hoy shows the same line for today's own sleep only, no button, no curve, nothing
    once a correction exists."""

    def _hoy(self, conn) -> str:
        return fechas.hoy_local(db.person(conn, 1)).isoformat()

    def test_hoy_muestra_la_linea_con_enlace(self):
        conn = _web_conn(self, "hoy_despertar.db")
        hoy = self._hoy(conn)
        start = f"{hoy[:-2]}{int(hoy[-2:]) - 1:02d}T22:00:00"
        end = f"{hoy}T05:00:00"
        _sesion(conn, 1, hoy, start, end)
        _guardar_fc(conn, 1, hoy, [(ts.replace(DAY, hoy), v) for ts, v in BASE_MUESTRAS]
                    + _serie(hoy, 5 * 60, 9 * 60, 2, BASE_FC + 8.0))
        _comida(conn, 1, hoy, f"{hoy}T07:15:00")
        db.set_metric(conn, hoy, "sleep_minutes", 420.0, "garmin", 1)
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertIn("Parece que te levantaste alrededor de las 07:15", page)
        self.assertIn("Ver el día", page)
        self.assertIn(f'href="/day/{hoy}"', page)
        self.assertNotIn("¿Corregir?", page)
        self.assertNotIn("chart-svg", page)

    def test_hoy_nada_cuando_ya_hay_correccion(self):
        conn = _web_conn(self, "hoy_corregido.db")
        hoy = self._hoy(conn)
        start = f"{hoy[:-2]}{int(hoy[-2:]) - 1:02d}T22:00:00"
        end = f"{hoy}T05:00:00"
        _sesion(conn, 1, hoy, start, end)
        _guardar_fc(conn, 1, hoy, [(ts.replace(DAY, hoy), v) for ts, v in BASE_MUESTRAS]
                    + _serie(hoy, 5 * 60, 9 * 60, 2, BASE_FC + 8.0))
        _comida(conn, 1, hoy, f"{hoy}T07:15:00")
        db.guardar_despertar_correccion(conn, 1, hoy, "levantado", "07:15", "despertar-real-v1", "persona")
        conn.commit()
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertNotIn("Parece que te levantaste", page)

    def test_hoy_nada_para_un_dia_que_no_es_hoy(self):
        """DAY (2026-11-11) is never today -- `_despertar_hoy_ctx`'s own day == today_local gate."""
        conn = _web_conn(self, "hoy_otro_dia.db")
        _seed_levantado_media(conn)
        page = TestClient(web.app, follow_redirects=False).get("/").text
        self.assertNotIn("Parece que te levantaste alrededor de las 07:15", page)


class CurvaSvgTest(unittest.TestCase):
    """Ruling 3: the inline SVG function itself -- two marks, a gap drawn as a gap, hidden when
    there is no point at all."""

    def test_ninguna_curva_sin_puntos(self):
        from datetime import datetime

        from body.rutas.hoy import _despertar_curva_svg
        desde, hasta = datetime(2026, 11, 11, 4, 30), datetime(2026, 11, 11, 7, 45)
        self.assertIsNone(_despertar_curva_svg(None, [], desde, hasta, desde, hasta))

    def test_dos_marcas_y_hueco_nunca_una_linea_sobre_el_hueco(self):
        from datetime import datetime, timedelta

        from body.i18n import T
        from body.rutas.hoy import _despertar_curva_svg
        t = T("es")
        desde = datetime(2026, 11, 11, 4, 30)
        garmin_dt = datetime(2026, 11, 11, 5, 0)
        marca_dt = datetime(2026, 11, 11, 7, 15)
        hasta = datetime(2026, 11, 11, 7, 45)
        # a continuous run 04:30-05:30 (every 2 min), nothing 05:30-07:00 (a sin_dato-sized gap),
        # then a second run 07:00-07:45.
        puntos = []
        dt = desde
        while dt <= datetime(2026, 11, 11, 5, 30):
            puntos.append((dt, 50.0))
            dt += timedelta(minutes=2)
        dt = datetime(2026, 11, 11, 7, 0)
        while dt <= hasta:
            puntos.append((dt, 58.0))
            dt += timedelta(minutes=2)
        svg = _despertar_curva_svg(t, puntos, desde, hasta, garmin_dt, [marca_dt])
        self.assertIsNotNone(svg)
        self.assertEqual(svg.count("<polyline"), 2, "the gap must split the line into two runs")
        self.assertEqual(svg.count('stroke-dasharray="2 3"'), 2, "two dashed vertical marks")
        self.assertIn("05:00", svg)
        self.assertIn("07:15", svg)
        self.assertIn('role="img"', svg)
        self.assertIn("aria-label=", svg)


class SinNoneTest(unittest.TestCase):
    """Ruling 5: no 'None' ever rendered, in any of the three screen states."""

    def test_sin_none_en_los_tres_estados(self):
        conn = _web_conn(self, "sin_none.db")
        _seed_levantado_media(conn)
        client = TestClient(web.app, follow_redirects=False)
        self.assertNotIn("None", client.get(f"/day/{DAY}").text)
        client.post(f"/day/{DAY}/despertar/proponer", data={"tipo": "levantado"})
        self.assertNotIn("None", client.get(f"/day/{DAY}").text)
        propuesta_id = _propuesta_id(conn)
        client.post(f"/day/{DAY}/despertar/propuestas/{propuesta_id}/aplicar")
        self.assertNotIn("None", client.get(f"/day/{DAY}").text)


class TaplinkBotonTest(unittest.TestCase):
    """BODY-DESPERTAR-REAL-7-M: a `<button class="taplink">` ('¿Corregir?' here, and the existing
    'Descartar'/'separar' buttons elsewhere) must look exactly like an `a.taplink` -- no grey
    button chrome -- so body.css carries its own reset rule, not just the shared `.taplink` one."""

    def test_body_css_resetea_el_boton_taplink(self):
        css = Path("body/static/body.css").read_text(encoding="utf-8")
        self.assertIn("button.taplink{background:none;border:0;padding:0;", css)


if __name__ == "__main__":
    unittest.main()
