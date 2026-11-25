"""BODY-TENDENCIAS-LEYENDA-PATRON-1 ruling 1: turnos_costo.frase_patron turns a successful
perfil_por_dia result into a sentence SPEC (a kind plus its own parameters), never prose, on
invented perfil-shaped dicts -- the function only reads the shape perfil_por_dia already
documents, never a real database. Rulings 2-4: the rendered /trends caption, built from that
spec by body/rutas/tendencias.py's own _frase_patron_texto, on invented demo data.
"""
from tests import _isolate  # noqa: F401,E402

import statistics
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from body import config, db, identity, turnos_costo, web
from body.i18n import T
from body.rutas import comun, tendencias

_BANDA = {"bajo": -5.0, "alto": 5.0}


def _perfil(metric: str, offsets: dict[str, float], peor: str, vuelta: str | None,
            banda: dict = _BANDA) -> dict:
    """A perfil_por_dia-shaped dict: `offsets` maps an offset name to its own 'media' (the only
    figure frase_patron reads), `peor` names the key ('mas_bajo'/'mas_alto' decided by the
    metric's own direction, same as perfil_por_dia itself)."""
    peor_es_bajo = turnos_costo._BETTER.get(metric) == "higher"
    return {
        "metric": metric, "motivo": None, "centro": 40.0, "banda": banda,
        "offsets": [{"offset": o, "media": m, "n": 5} for o, m in offsets.items()],
        ("mas_bajo" if peor_es_bajo else "mas_alto"): peor,
        "vuelta": vuelta, "noches_tipicas": 2, "n_bloques": 5,
    }


class FrasePatronTest(unittest.TestCase):
    def test_estable_when_the_worst_offset_sits_inside_the_band(self):
        perfil = _perfil("hrv_last_night", {"noche_1": -3.0}, "noche_1", None)
        self.assertEqual(turnos_costo.frase_patron(perfil, "hrv_last_night"), {"kind": "estable"})

    def test_vuelve_baja_for_a_higher_is_better_metric(self):
        # hrv_last_night: higher is better -> its worse direction is low (below centre, "baja").
        perfil = _perfil("hrv_last_night", {"noche_1": -12.0, "post_1": -1.0}, "noche_1", "post_1")
        self.assertEqual(turnos_costo.frase_patron(perfil, "hrv_last_night"),
                          {"kind": "vuelve", "peor": "noche_1", "vuelta": "post_1", "direccion": "baja"})

    def test_vuelve_sube_for_a_lower_is_better_metric(self):
        # resting_hr: lower is better -> its worse direction is high (above centre, "sube").
        perfil = _perfil("resting_hr", {"noche_1": 8.0, "post_1": 1.0}, "noche_1", "post_1")
        self.assertEqual(turnos_costo.frase_patron(perfil, "resting_hr"),
                          {"kind": "vuelve", "peor": "noche_1", "vuelta": "post_1", "direccion": "sube"})

    def test_no_vuelve_when_outside_the_band_and_vuelta_is_none(self):
        perfil = _perfil("hrv_last_night", {"noche_1": -12.0}, "noche_1", None)
        self.assertEqual(turnos_costo.frase_patron(perfil, "hrv_last_night"),
                          {"kind": "no_vuelve", "peor": "noche_1", "direccion": "baja"})

    def test_motivo_set_is_none(self):
        self.assertIsNone(turnos_costo.frase_patron({"metric": "hrv_last_night",
                                                      "motivo": "pocos_bloques_previos"},
                                                     "hrv_last_night"))

    def test_spec_carries_no_number_from_the_persons_data(self):
        """peor/vuelta are offset NAMES and direccion is a word -- no mean, no ms, nothing a log
        could read as a health figure (the spec's own privacy rule)."""
        perfil = _perfil("hrv_last_night", {"noche_1": -12.0, "post_1": -1.0}, "noche_1", "post_1")
        frase = turnos_costo.frase_patron(perfil, "hrv_last_night")
        for valor in frase.values():
            self.assertIsInstance(valor, str)
        self.assertEqual(set(frase), {"kind", "peor", "vuelta", "direccion"})


class FrasePatronTextoTest(unittest.TestCase):
    """Ruling 2's own sentence per kind, es and en, straight from body/rutas/tendencias.py's
    _frase_patron_texto -- the function that turns frase_patron's spec into the one sentence a
    card shows, with no database at all."""

    def test_estable(self):
        frase = {"kind": "estable"}
        self.assertEqual(tendencias._frase_patron_texto(T("es"), frase), "Estable alrededor de tus noches.")
        self.assertEqual(tendencias._frase_patron_texto(T("en"), frase), "Steady around your night shifts.")

    def test_vuelve_baja(self):
        frase = {"kind": "vuelve", "peor": "noche_1", "vuelta": "post_1", "direccion": "baja"}
        self.assertEqual(tendencias._frase_patron_texto(T("es"), frase),
                          "Baja más la primera noche; vuelve a tu rango el día +1 después de un bloque de noches.")
        self.assertEqual(tendencias._frase_patron_texto(T("en"), frase),
                          "Lowest on the first night; back in your range by day +1 after a night block.")

    def test_vuelve_sube(self):
        frase = {"kind": "vuelve", "peor": "noche_ultima", "vuelta": "post_2", "direccion": "sube"}
        self.assertEqual(tendencias._frase_patron_texto(T("es"), frase),
                          "Sube más la última noche; vuelve a tu rango el día +2 después de un bloque de noches.")
        self.assertEqual(tendencias._frase_patron_texto(T("en"), frase),
                          "Highest on the last night; back in your range by day +2 after a night block.")

    def test_no_vuelve_baja(self):
        frase = {"kind": "no_vuelve", "peor": "antes", "direccion": "baja"}
        self.assertEqual(tendencias._frase_patron_texto(T("es"), frase),
                          "Baja más el día antes; 4 días después aún no vuelve a tu rango.")
        self.assertEqual(tendencias._frase_patron_texto(T("en"), frase),
                          "Lowest the day before; 4 days after, not back in your range yet.")

    def test_no_vuelve_sube(self):
        frase = {"kind": "no_vuelve", "peor": "post_4", "direccion": "sube"}
        self.assertEqual(tendencias._frase_patron_texto(T("es"), frase),
                          "Sube más el día +4 después de un bloque de noches; 4 días después aún no vuelve a tu "
                          "rango.")
        self.assertEqual(tendencias._frase_patron_texto(T("en"), frase),
                          "Highest on day +4 after a night block; 4 days after, not back in your range yet.")

    def test_none_frase_is_no_caption(self):
        self.assertIsNone(tendencias._frase_patron_texto(T("es"), None))


# ---- rendered /trends card: real plumbing, invented demo data, deterministic blocks -------------
HOY = date(2026, 6, 1)
_REF_CYCLE = [50.0, 55.0, 60.0, 65.0, 70.0]


def _fill_reference(conn, person_id: int, metric: str, hoy: date) -> list[float]:
    start, end = hoy - timedelta(turnos_costo.REFERENCIA_DIAS), hoy - timedelta(1)
    vals, d, i = [], start, 0
    while d <= end:
        v = _REF_CYCLE[i % len(_REF_CYCLE)]
        db.set_metric(conn, d.isoformat(), metric, v, "garmin", person_id)
        vals.append(v)
        d, i = d + timedelta(1), i + 1
    return vals


def _place_blocks(hoy: date, lengths: list[int], gap: int = 10) -> list[date]:
    end_limit = hoy - timedelta(turnos_costo.REFERENCIA_DIAS + 6)
    total = sum(lengths) + gap * (len(lengths) - 1)
    start = end_limit - timedelta(total - 1)
    inicios, cur = [], start
    for n in lengths:
        inicios.append(cur)
        cur += timedelta(n + gap)
    return inicios


def _seed_block(conn, person_id: int, metric: str, inicio: date, dias: int,
                valores: dict[str, float | None]) -> None:
    fin = inicio + timedelta(dias - 1)
    d = inicio
    while d <= fin:
        conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES (?, ?, 'night', 'manual')",
                     (person_id, d.isoformat()))
        d += timedelta(1)
    bloque = turnos_costo.Bloque(inicio=inicio.isoformat(), fin=fin.isoformat(), dias=dias)
    for offset, v in valores.items():
        if v is None:
            continue
        dia = turnos_costo._offset_day(bloque, offset)
        if dia is not None:
            db.set_metric(conn, dia.isoformat(), metric, v, "garmin", person_id)


class TrendsCaptionRenderedTest(unittest.TestCase):
    """hrv_last_night's own reference and three deterministic night blocks produce a known
    "vuelve"/"baja" profile -- the one case exercised end to end, through the real /trends route,
    with real i18n. steps gets a card too (TREND_MIN_DAYS worth of data) but no reference in the
    shift-cost model, so it must never carry a caption."""

    def setUp(self):
        self.orig_db_path = config.DB_PATH
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig_db_path))
        self.addCleanup(web.app.dependency_overrides.clear)
        config.DB_PATH = Path(tempfile.mkdtemp(dir=_isolate.TMP)) / "patron.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        self.hrv_vals = _fill_reference(conn, 1, "hrv_last_night", HOY)
        hrv_media = statistics.fmean(self.hrv_vals)
        q = statistics.quantiles(self.hrv_vals, n=4)
        banda_bajo = q[0] - hrv_media
        self.assertLess(-20.0, banda_bajo)  # the seeded noche_1 drop must sit outside the band
        for inicio, dias in zip(_place_blocks(HOY, [2, 2, 2]), [2, 2, 2]):
            _seed_block(conn, 1, "hrv_last_night", inicio, dias,
                        {"noche_1": hrv_media - 20.0, "post_1": hrv_media})
        for i in range(20):
            db.set_metric(conn, (HOY - timedelta(i)).isoformat(), "steps", 8000 + i * 10, "garmin", 1)
        conn.commit()
        web.app.dependency_overrides[identity.current_person_id] = lambda: 1
        self.client = TestClient(web.app, follow_redirects=False)

    @staticmethod
    def _chunk(page: str, key: str) -> str:
        start = page.index(f'id="{key}"')
        return page[start:page.index("</div>", page.index('class="legend mt-4"', start))]

    def test_es_caption_on_the_hrv_card(self):
        with mock.patch.object(comun, "_today_local", return_value=HOY):
            page = self.client.get("/trends").text
        chunk = self._chunk(page, "hrv_last_night")
        self.assertIn('class="read mt-4"', chunk)
        self.assertIn("Baja más la primera noche; vuelve a tu rango el día +1 después de un bloque de noches.",
                      chunk)

    def test_en_caption_on_the_hrv_card(self):
        conn = db.connect()
        self.addCleanup(conn.close)
        db.set_language(conn, "en", 1)
        conn.commit()
        with mock.patch.object(comun, "_today_local", return_value=HOY):
            page = self.client.get("/trends").text
        chunk = self._chunk(page, "hrv_last_night")
        self.assertIn("Lowest on the first night; back in your range by day +1 after a night block.", chunk)

    def test_no_caption_on_a_metric_outside_the_model(self):
        with mock.patch.object(comun, "_today_local", return_value=HOY):
            page = self.client.get("/trends").text
        chunk = self._chunk(page, "steps")
        self.assertNotIn('class="read mt-4"', chunk)

    def test_no_caption_without_a_successful_profile(self):
        """Fewer than MIN_BLOQUES_PREVIOS night blocks: the profile itself fails (motivo set), so
        frase_patron returns None and the card carries no caption, even though it still renders
        (the reference data alone is enough for a normal trend card)."""
        config.DB_PATH = Path(tempfile.mkdtemp(dir=_isolate.TMP)) / "sin_perfil.db"
        conn = db.connect()
        self.addCleanup(conn.close)
        db.init(conn)
        _fill_reference(conn, 1, "hrv_last_night", HOY)
        for inicio, dias in zip(_place_blocks(HOY, [2, 2]), [2, 2]):  # fewer than MIN_BLOQUES_PREVIOS
            _seed_block(conn, 1, "hrv_last_night", inicio, dias, {"noche_1": 40.0})
        conn.commit()
        client = TestClient(web.app, follow_redirects=False)
        with mock.patch.object(comun, "_today_local", return_value=HOY):
            page = client.get("/trends").text
        chunk = self._chunk(page, "hrv_last_night")
        self.assertNotIn('class="read mt-4"', chunk)


if __name__ == "__main__":
    unittest.main()
