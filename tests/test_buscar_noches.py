"""BODY-MINUTO-ANALISIS-3 (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2): the assistant tool
`buscar_noches` -- the REVERSE of comparar_noche: not "how does this night rank" but "WHICH of
this person's own nights match a feature or a context". Invented demo data only, no real health
figures, no network.

A known ten-night history, person 1, 2026-03-01 (D1) .. 2026-03-10 (D10):
  - `hrv_last_night` ascending 30, 32, .., 48 (D1=30 .. D10=48), a stored figure on every night.
  - D1..D5 each carry a sleep_session AND a minute 'sleep' payload with exactly 1, 2, 3, 4, 5 awake
    segments in that order (`segmentos_despierto`), a shape feature.
  - D6..D10 each carry a sleep_session but NO minute payload at all -- the honest "no minute
    detail" gap `noches_sin_minutos_en_rango` counts.
  - A single night-shift the evening before D3 (2026-03-02) -- D3 alone carries both 'tras_turno'
    (tipo='night') and 'tras_turno_noche' (its own block day) in its context.
  - `alcohol_g` on the evening before D2 (2026-03-01) -- D2 alone carries 'con_alcohol'.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._db import open_conn

from body import db, garmin_adapter
from body.agente import herramientas

ZONA_OFFSET_H = 5  # America/Bogota, the same fixed offset test_noche_rasgos.py already uses

DIAS = [f"2026-03-{i:02d}" for i in range(1, 11)]  # D1..D10
HRV = [30.0 + i * 2 for i in range(10)]  # D1=30 .. D10=48


def _conn(case: unittest.TestCase):
    c = open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")
    db.init(c)
    return c


def _ms(local_iso: str) -> int:
    local = datetime.fromisoformat(local_iso)
    utc = local + timedelta(hours=ZONA_OFFSET_H)
    return int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _etapas_con_despiertos(inicio_iso: str, n: int) -> list[tuple[str, int]]:
    """n separate 'despierto' (3) runs of 5 minutes each, 10 minutes apart, each preceded and
    followed by a 'ligero' (1) point so `_segmentos` reads them as n distinct segments, never one
    merged run."""
    inicio = datetime.fromisoformat(inicio_iso)
    pts = [(inicio.isoformat(), 1)]
    t = inicio
    for _ in range(n):
        t = t + timedelta(minutes=10)
        pts.append((t.isoformat(), 3))
        t = t + timedelta(minutes=5)
        pts.append((t.isoformat(), 1))
    return pts


def _guardar_etapas(conn, person_id: int, day: str, etapas: list[tuple[str, int]]) -> None:
    payload = {"sleepLevels": [{"startGMT": _ms(ts), "activityLevel": c} for ts, c in etapas]}
    conn.execute(
        "INSERT OR REPLACE INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, ?, 'sleep', ?, ?, ?)",
        (person_id, day, garmin_adapter.SOURCE, "2026-01-01T00:00:00+00:00", json.dumps(payload)),
    )


def _sesion(conn, person_id: int, wake_day: str) -> None:
    evening = (datetime.fromisoformat(wake_day) - timedelta(1)).date().isoformat()
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, wake_day, f"{evening}T23:00:00", f"{wake_day}T06:00:00"),
    )


def _seed(conn, person_id: int = 1) -> None:
    for i, dia in enumerate(DIAS):
        db.set_metric(conn, dia, "hrv_last_night", HRV[i], "garmin", person_id)
        _sesion(conn, person_id, dia)
        if i < 5:  # D1..D5: a minute payload with i+1 awake segments
            evening = (datetime.fromisoformat(dia) - timedelta(1)).date().isoformat()
            etapas = _etapas_con_despiertos(f"{evening}T23:00:00", i + 1)
            _guardar_etapas(conn, person_id, dia, etapas)
    # D3's own evening (2026-03-02) carries a single night shift.
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, source) VALUES "
        "(?, '2026-03-02', 'night', '2026-03-02T22:00:00', 'manual')", (person_id,),
    )
    # D2's own evening (2026-03-01) carries alcohol.
    db.set_metric(conn, "2026-03-01", "alcohol_g", 20.0, "garmin", person_id)
    conn.commit()


class RegistroTest(unittest.TestCase):
    def test_registered_as_lectura_completo(self):
        self.assertEqual(herramientas.tipo_de("buscar_noches"), "lectura")
        entrada = herramientas._BY_NAME["buscar_noches"]
        self.assertEqual(entrada["nivel"], "completo")
        self.assertIn("buscar_noches", {h["nombre"] for h in herramientas.disponibles("completo")})
        self.assertNotIn("buscar_noches", {h["nombre"] for h in herramientas.disponibles("resumen")})


class StaticGuardTest(unittest.TestCase):
    def test_source_never_mentions_propuestas(self):
        import inspect
        import textwrap
        entrada = herramientas._BY_NAME["buscar_noches"]
        src = textwrap.dedent(inspect.getsource(entrada["funcion"]))
        self.assertNotIn("propuestas", src)


class FiltroSobreCifraGuardadaTest(unittest.TestCase):
    def test_a_stored_figure_filter_selects_exactly_the_hand_picked_nights(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 40}]},
            "completo",
        )
        self.assertEqual(out["n_coinciden"], 5)
        self.assertEqual({n["fecha"] for n in out["noches"]}, set(DIAS[0:5]))


class FiltroSobreRasgoDeFormaTest(unittest.TestCase):
    def test_a_shape_feature_filter_selects_exactly_the_hand_picked_nights(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "segmentos_despierto", "op": ">", "valor": 3}]},
            "completo",
        )
        self.assertEqual(out["n_coinciden"], 2)
        self.assertEqual({n["fecha"] for n in out["noches"]}, {DIAS[3], DIAS[4]})

    def test_nights_without_minute_data_never_match_and_are_counted(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "segmentos_despierto", "op": ">", "valor": 0}]},
            "completo",
        )
        fechas = {n["fecha"] for n in out["noches"]}
        self.assertFalse(fechas & set(DIAS[5:10]))
        self.assertEqual(out["n_en_rango"], 10)
        self.assertEqual(out["noches_sin_minutos_en_rango"], 5)


class GrupoConFiltrosTest(unittest.TestCase):
    def test_grupo_combines_with_filtros_by_and(self):
        # D3 itself is inside the shift's own block (ruling 2's 'bloque_noche'); D4/D5 fall inside
        # turnos_costo.MODELO['ventana_post'] (10) days of RECOVERY after it ('dias_desde_ultimo_
        # turno_noche'), which `_coincide_grupo`'s own tras_turno_noche check already treats the
        # same way comparar_noche's own CompararGrupoTest does -- D1/D2 carry neither.
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 40}],
             "grupo": {"tras_turno_noche": True}},
            "completo",
        )
        self.assertEqual(out["n_coinciden"], 3)
        self.assertEqual({n["fecha"] for n in out["noches"]}, {DIAS[2], DIAS[3], DIAS[4]})

    def test_an_unknown_grupo_key_is_refused(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "buscar_noches", {"grupo": {"no_existe": 1}}, "completo")


class OrdenYLimiteTest(unittest.TestCase):
    def test_orden_desc_and_limite_return_the_expected_top_n(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "orden": {"rasgo": "hrv_last_night", "dir": "desc"}, "limite": 3},
            "completo",
        )
        self.assertEqual([n["fecha"] for n in out["noches"]], [DIAS[9], DIAS[8], DIAS[7]])

    def test_orden_asc_and_limite_return_the_expected_top_n(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "orden": {"rasgo": "hrv_last_night", "dir": "asc"}, "limite": 2},
            "completo",
        )
        self.assertEqual([n["fecha"] for n in out["noches"]], [DIAS[0], DIAS[1]])

    def test_n_coinciden_counts_every_match_before_the_limit(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 40}], "limite": 2},
            "completo",
        )
        self.assertEqual(out["n_coinciden"], 5)
        self.assertEqual(len(out["noches"]), 2)

    def test_default_order_with_no_orden_is_most_recent_first(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 40}], "limite": 2},
            "completo",
        )
        self.assertEqual([n["fecha"] for n in out["noches"]], [DIAS[4], DIAS[3]])

    def test_an_unknown_orden_rasgo_is_refused(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "buscar_noches", {"orden": {"rasgo": "no_existe", "dir": "asc"}},
                                   "completo")


class RasgosDevueltosTest(unittest.TestCase):
    def test_default_rasgos_are_the_ones_named_in_filtros(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 40}], "limite": 1},
            "completo",
        )
        noche = out["noches"][0]
        self.assertEqual(set(noche), {"fecha", "hrv_last_night", "contexto"})

    def test_explicit_rasgos_overrides_the_default(self):
        conn = _conn(self)
        _seed(conn)
        db.set_metric(conn, DIAS[0], "sleep_score", 70.0, "garmin", 1)
        conn.commit()
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 40}],
             "rasgos": ["sleep_score"], "limite": 10},
            "completo",
        )
        noche = next(n for n in out["noches"] if n["fecha"] == DIAS[0])
        self.assertEqual(noche["sleep_score"], 70.0)
        self.assertNotIn("hrv_last_night", noche)


class EnComunTest(unittest.TestCase):
    def test_en_comun_shares_equal_hand_computed_shares(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 40}]},
            "completo",
        )
        self.assertEqual(out["n_coinciden"], 5)
        en_comun = out["en_comun"]
        # 'tras_turno' (a specific shift kind) is carried only by D3's own evening (the shift's own
        # day); 'tras_turno_noche' is also carried by D4/D5 (ruling 2's recovery window, the same
        # one GrupoConFiltrosTest's own comment above explains).
        self.assertEqual(en_comun["tras_turno"], {"coincidentes": 0.2, "en_rango": 0.1})
        self.assertEqual(en_comun["tras_turno_noche"], {"coincidentes": 0.6, "en_rango": 0.8})
        self.assertEqual(en_comun["con_alcohol"], {"coincidentes": 0.2, "en_rango": 0.1})
        self.assertEqual(en_comun["viaje"], {"coincidentes": 0.0, "en_rango": 0.0})
        self.assertEqual(en_comun["periodo"], {"coincidentes": 0.0, "en_rango": 0.0})
        self.assertEqual(en_comun["comida_tarde_min_max"], {"coincidentes": 0.0, "en_rango": 0.0})

    def test_en_comun_is_absent_under_five_matches(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 37}]},
            "completo",
        )
        self.assertEqual(out["n_coinciden"], 4)
        self.assertNotIn("en_comun", out)


class ValidacionTest(unittest.TestCase):
    def test_unknown_rasgo_in_filtros_is_refused_by_name(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(conn, 1, "buscar_noches",
                                   {"filtros": [{"rasgo": "no_existe", "op": ">", "valor": 1}]}, "completo")
        self.assertIn("no_existe", str(ctx.exception))

    def test_unknown_op_in_filtros_is_refused_by_name(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(conn, 1, "buscar_noches",
                                   {"filtros": [{"rasgo": "hrv_last_night", "op": "!=", "valor": 1}]}, "completo")
        self.assertIn("!=", str(ctx.exception))

    def test_malformed_desde_is_refused(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "buscar_noches", {"desde": "no-es-fecha"}, "completo")

    def test_future_hasta_is_refused(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "buscar_noches", {"hasta": "2099-01-01"}, "completo")

    def test_desde_after_hasta_is_refused(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "buscar_noches", {"desde": DIAS[-1], "hasta": DIAS[0]}, "completo")

    def test_limite_out_of_range_is_refused(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "buscar_noches", {"limite": 0}, "completo")
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "buscar_noches", {"limite": 61}, "completo")

    def test_refused_below_nivel_completo(self):
        conn = _conn(self)
        _seed(conn)
        for nivel in ("resumen", "ninguno"):
            with self.assertRaises(herramientas.ToolError):
                herramientas.ejecutar(conn, 1, "buscar_noches", {}, nivel)


class OtraPersonaTest(unittest.TestCase):
    def test_another_persons_nights_are_never_counted(self):
        conn = _conn(self)
        _seed(conn)
        conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
        for dia in DIAS:
            db.set_metric(conn, dia, "hrv_last_night", 1000.0, "garmin", 2)
        conn.commit()
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": ">", "valor": 100}]},
            "completo",
        )
        self.assertEqual(out["n_coinciden"], 0)
        self.assertEqual(out["n_en_rango"], 10)


class JsonSerializableTest(unittest.TestCase):
    def test_output_survives_json_dumps(self):
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(
            conn, 1, "buscar_noches",
            {"desde": DIAS[0], "hasta": DIAS[-1],
             "filtros": [{"rasgo": "hrv_last_night", "op": "<", "valor": 40}]},
            "completo",
        )
        json.dumps(out)


if __name__ == "__main__":
    unittest.main()
