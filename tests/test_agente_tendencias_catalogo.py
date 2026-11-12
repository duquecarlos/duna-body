"""BODY-AGENTE-TENDENCIAS-CATALOGO-1: the assistant's leer_tendencias reads the same catalogue the
Trends screen shows (body/catalogo_tendencias.py), instead of a hand-typed tuple that could drift
from it. Invented demo data only, no real health figures.
"""
from __future__ import annotations

import ast
import statistics
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import catalogo_tendencias, db, turnos_costo
from body.agente import herramientas

REPO_ROOT = Path(__file__).resolve().parent.parent


def _conn(case: unittest.TestCase) -> "db.sqlite3.Connection":
    conn = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "tendencias_catalogo.db")
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn


def _seed_days(conn, person_id: int, key: str, start: date, n: int, base: float, source: str,
               step: float = 0.5) -> None:
    for i in range(n):
        db.set_metric(conn, (start + timedelta(i)).isoformat(), key, base + i * step, source, person_id)


class CatalogueMatchesTrendsTest(unittest.TestCase):
    """The guard that the two can never drift again: the assistant's enum is exactly the set the
    Trends screen can show, TREND_KEYS plus every TREND_GROUPS key."""

    def test_tendencia_metricas_equals_trend_keys_plus_every_group_key(self):
        esperado = set(catalogo_tendencias.TREND_KEYS) | {
            k for _, keys in catalogo_tendencias.TREND_GROUPS for k in keys
        }
        self.assertEqual(set(herramientas.TENDENCIA_METRICAS), esperado)

    def test_todas_has_no_duplicates(self):
        self.assertEqual(len(catalogo_tendencias.TODAS), len(set(catalogo_tendencias.TODAS)))


class SchemaAcceptsTheFourGainedKeysTest(unittest.TestCase):
    def test_leer_tendencias_enum_accepts_the_four_keys(self):
        enum = herramientas._PARAM_TENDENCIAS["properties"]["metric"]["enum"]
        for clave in ("flex_alcance_cm", "snoring_min", "sleep_spo2_avg", "sleep_respiration_avg"):
            self.assertIn(clave, enum)


class LeerTendenciasReadsSelfAndNightKeysTest(unittest.TestCase):
    """Invented values for one person, one self-measured key and one night key: leer_tendencias
    returns them, and a second person's own values never appear (isolation)."""

    def setUp(self):
        self.conn = _conn(self)
        start = date(2026, 10, 1)
        self.flex_1 = [30.0 + i * 0.5 for i in range(3)]
        self.spo2_1 = [96.0 + i * 0.5 for i in range(14)]
        _seed_days(self.conn, 1, "flex_alcance_cm", start, 3, 30.0, "self")
        _seed_days(self.conn, 1, "sleep_spo2_avg", start, 14, 96.0, "garmin")
        # Person 2's own figures: a different base, so a broken person_id filter changes the mean.
        _seed_days(self.conn, 2, "flex_alcance_cm", start, 3, 10.0, "self")
        _seed_days(self.conn, 2, "sleep_spo2_avg", start, 14, 60.0, "garmin")
        self.conn.commit()

    def test_self_measured_key_appears_from_fewer_than_14_days(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_tendencias",
                                     {"metric": "flex_alcance_cm"}, "resumen")
        self.assertIn("flex_alcance_cm", out["metricas"])
        self.assertEqual(out["metricas"]["flex_alcance_cm"]["dias_con_datos"], 3)

    def test_night_key_appears_at_the_watch_cadence(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_tendencias",
                                     {"metric": "sleep_spo2_avg"}, "resumen")
        self.assertIn("sleep_spo2_avg", out["metricas"])
        self.assertEqual(out["metricas"]["sleep_spo2_avg"]["dias_con_datos"], 14)

    def test_another_persons_values_never_appear(self):
        out = herramientas.ejecutar(self.conn, 1, "leer_tendencias", {}, "resumen")
        self.assertAlmostEqual(out["metricas"]["flex_alcance_cm"]["media_historica"],
                                statistics.fmean(self.flex_1))
        self.assertAlmostEqual(out["metricas"]["sleep_spo2_avg"]["media_historica"],
                                statistics.fmean(self.spo2_1))


class LeerTendenciasCarriesPatronTurnosTest(unittest.TestCase):
    """BODY-TENDENCIAS-LEYENDA-PATRON-1 ruling 3: leer_tendencias's own patron_turnos equals the
    same spec turnos_costo.frase_patron would build from turnos_costo.perfiles_patron -- the
    Trends screen's own source, read directly here rather than through the web route."""

    def _place_blocks(self, hoy: date, lengths: list[int], gap: int = 10) -> list[date]:
        end_limit = hoy - timedelta(turnos_costo.REFERENCIA_DIAS + 6)
        total = sum(lengths) + gap * (len(lengths) - 1)
        start = end_limit - timedelta(total - 1)
        inicios, cur = [], start
        for n in lengths:
            inicios.append(cur)
            cur += timedelta(n + gap)
        return inicios

    def test_patron_turnos_matches_frase_patron(self):
        conn = _conn(self)
        from body import fechas
        hoy = fechas.hoy_local(db.person(conn, 1))
        start_ref, end_ref = hoy - timedelta(turnos_costo.REFERENCIA_DIAS), hoy - timedelta(1)
        cycle = [50.0, 55.0, 60.0, 65.0, 70.0]
        d, i = start_ref, 0
        while d <= end_ref:
            db.set_metric(conn, d.isoformat(), "hrv_last_night", cycle[i % len(cycle)], "garmin", 1)
            d, i = d + timedelta(1), i + 1
        for inicio in self._place_blocks(hoy, [2, 2, 2]):
            fin = inicio + timedelta(1)
            dia = inicio
            while dia <= fin:
                conn.execute("INSERT INTO shift (person_id, day, kind, source) VALUES "
                             "(?, ?, 'night', 'manual')", (1, dia.isoformat()))
                dia += timedelta(1)
            bloque = turnos_costo.Bloque(inicio=inicio.isoformat(), fin=fin.isoformat(), dias=2)
            for offset, v in {"noche_1": 40.0, "post_1": 60.0}.items():
                day = turnos_costo._offset_day(bloque, offset)
                db.set_metric(conn, day.isoformat(), "hrv_last_night", v, "garmin", 1)
        conn.commit()

        out = herramientas.ejecutar(conn, 1, "leer_tendencias", {"metric": "hrv_last_night"}, "resumen")
        perfiles = turnos_costo.perfiles_patron(conn, 1, hoy)
        esperado = turnos_costo.frase_patron(perfiles["hrv_last_night"], "hrv_last_night")
        self.assertIsNotNone(esperado)
        self.assertEqual(out["metricas"]["hrv_last_night"]["patron_turnos"], esperado)

    def test_metric_outside_the_shift_cost_model_never_carries_patron_turnos(self):
        conn = _conn(self)
        _seed_days(conn, 1, "sleep_rem_min", date(2026, 10, 1), 14, 85, "garmin")
        conn.commit()
        out = herramientas.ejecutar(conn, 1, "leer_tendencias", {"metric": "sleep_rem_min"}, "resumen")
        self.assertNotIn("patron_turnos", out["metricas"]["sleep_rem_min"])


class CatalogueModuleImportsNoRouteOrAgentModuleTest(unittest.TestCase):
    def test_catalogo_tendencias_imports_nothing_from_rutas_or_agente(self):
        tree = ast.parse((REPO_ROOT / "body" / "catalogo_tendencias.py").read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "rutas" in alias.name or "agente" in alias.name:
                        offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if "rutas" in node.module or "agente" in node.module:
                    offenders.append(node.module)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
