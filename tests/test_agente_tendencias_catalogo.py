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

from body import catalogo_tendencias, db
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
