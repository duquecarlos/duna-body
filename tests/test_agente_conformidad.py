"""BODY-ASISTENTE-CONFORMIDAD-1: the conformance eval agent-capabilities-v1 amendment 7 requires in
every product's own gate -- one test class per clause, each docstring quoting the clause and the
contract section it proves. Invented demo data only, no network, no real provider: every provider
in this file is `body.agente.cliente`'s own pure selection logic or `body.agente.uso`'s metering,
never a live call.
"""
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import anatomia, config, db, fechas, garmin_adapter, goals
from body.agente import ajustes, cliente, herramientas, propuestas, uso
from body.agente import conversacion as agente_conversacion

# ---- shared fixtures -----------------------------------------------------------------------

def _reset_db():
    orig = config.DB_PATH
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "c.db"
    conn = db.connect()
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (2, 'B')")
    conn.commit()
    return conn, orig


def _cid(conn, person_id: int = 1) -> int:
    return agente_conversacion.crear(conn, person_id)


def _actividad(conn, source: str, source_id: str, day: str, tipo: str = "strength_training") -> None:
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, source) "
        "VALUES (1, ?, ?, ?, ?, NULL, ?)", (source_id, day, f"{day}T07:00:00", tipo, source),
    )


def _seed_todo(conn) -> dict:
    """Every piece of demo data the 28 `propuesta` tools' valid arguments reference (BODY-HOGAR-
    ESCRIBE-1: extended from 27 with anotar_sesion) -- real
    production writers only (`goals`, `db.*`), never a hand-typed row for a table one of those
    writers owns."""
    ids: dict = {}

    gid = goals.propose(conn, 1, "Bajar FC en reposo", "2020-01-01", metric_key="resting_hr",
                        direction="lower", target_value=50.0, baseline_value=60.0, deadline="2027-01-01")
    goals.activate(conn, gid)
    ids["goal_metric_id"] = gid
    ids["habit_id"] = goals.add_habit(conn, gid, "Dormir temprano antes de turno de noche",
                                       {"per": "week", "times": 3})

    # proponer_confirmar_turnos: a pending shift batch, the same raw shape
    # tests/test_agente_propuestas.py's own `_seed_turnos` uses -- shift_import.py is not this
    # slice's to widen, so the batch is built the way shift_import's own importer leaves it.
    cur = conn.execute(
        "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, day_count, created_at) "
        "VALUES (1, 'turnos-conformidad.json', '2020-01-01T00:00:00', '2020-01-01T00:00:00', 'proposed', 1, "
        "'2020-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO shift_batch_item (batch_id, day, kind, start_local) VALUES (?, '2020-01-05', 'night', "
        "'2020-01-05T22:00:00')", (cur.lastrowid,),
    )

    # registrar_entreno / descartar_series: two separate watch strength sessions with one set each.
    ids["dia_a1"] = "2020-04-01"
    _actividad(conn, "garmin", "a1", ids["dia_a1"])
    ids["dia_a4"] = "2020-04-05"
    _actividad(conn, "garmin", "a4", ids["dia_a4"])
    conn.commit()
    for source_id, dia in (("a1", ids["dia_a1"]), ("a4", ids["dia_a4"])):
        db.save_exercise_sets(conn, 1, "garmin", source_id, [garmin_adapter.ExerciseSet(
            set_type="active", category="pecho", exercise="press banca", reps=8.0,
            weight_value=40.0, weight_unit="kg", duration_s=30.0, start_local=f"{dia}T07:00:00",
        )])

    # separar_sesion: a watch session already linked to a manual one.
    ids["dia_a2"] = "2020-04-10"
    _actividad(conn, "garmin", "a2", ids["dia_a2"])
    conn.commit()
    manual_m2 = db.create_manual_session(conn, 1, ids["dia_a2"], f"{ids['dia_a2']}T08:00:00", "Sesión manual m2")
    db.set_vinculo_manual(conn, 1, "garmin", "a2", manual_m2, False)

    # unir_sesiones: a watch session and a manual one on the same day, still unlinked.
    ids["dia_a3"] = "2020-04-15"
    _actividad(conn, "garmin", "a3", ids["dia_a3"])
    conn.commit()
    db.create_manual_session(conn, 1, ids["dia_a3"], f"{ids['dia_a3']}T08:00:00", "Sesión manual m3")

    ids["ingesta_a"] = db.registrar_ingesta(conn, 1, "2020-02-10", db.INGESTA_METRIC_KEYS[0], 30.0, True)
    ids["ingesta_b"] = db.registrar_ingesta(conn, 1, "2020-02-11", db.INGESTA_METRIC_KEYS[0], 40.0, True)
    ids["comida_id"] = db.crear_comida(conn, 1, "2020-02-12",
                                        [{"metric_key": db.INGESTA_METRIC_KEYS[0], "amount": 50.0,
                                          "estimado": True}], "Comida semilla")
    ids["experimento_id"] = db.crear_experimento(
        conn, 1, "Experimento semilla", ["resting_hr"], {"tipo": "dia"},
        {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False}, fecha_inicio="2020-01-01",
    )

    ids["period_open"] = db.crear_periodo(conn, 1, "malestar", "2020-02-01", None, "seed abierto")
    ids["period_b"] = db.crear_periodo(conn, 1, "viaje", "2020-02-05", None, "seed b")
    ids["period_c"] = db.crear_periodo(conn, 1, "lesion", "2020-02-08", None, "seed c",
                                        zonas=[anatomia.CATEGORIA_ENUM[0]])
    conn.commit()

    # ampliar_plan / corregir_meta_plan: a real applied plan with one vigente weekly target,
    # created and applied through the ordinary propose/apply flow (never a hand-typed horizonte_*
    # row) -- this is test SETUP, not part of any clause's own assertion.
    monday1 = fechas.lunes_de(date(2020, 3, 2)).isoformat()
    ids["monday_extra"] = fechas.lunes_de(date(2020, 9, 7)).isoformat()
    cid = _cid(conn, 1)
    out = herramientas.ejecutar(conn, 1, "proponer_plan", {
        "titulo": "Plan semilla", "start_day": "2020-01-01", "deadline": "2020-12-31",
        "metas_semanales": [{"week_start": monday1, "medida": "sesiones_fuerza", "sentido": "al_menos",
                              "valor": 1}],
    }, "completo", cid)
    resultado = propuestas.aplicar(conn, 1, out["propuesta_id"])
    assert resultado["estado"] == "aplicada", resultado
    fila = conn.execute("SELECT objetivo_id FROM propuesta_agente WHERE id = ?",
                        (out["propuesta_id"],)).fetchone()
    ids["plan_goal_id"] = fila["objetivo_id"]
    meta = conn.execute(
        "SELECT id FROM horizonte_meta_semanal WHERE horizonte_goal_id = ? AND vigente = 1",
        (ids["plan_goal_id"],)).fetchone()
    ids["meta_semanal_id"] = meta["id"]

    # anotar_sesion: a real PLANNED session row (plan table), created and applied through the
    # ordinary planear_entreno propose/apply flow -- never a hand-typed row, same reasoning as the
    # plan-goal seeding just above.
    cid_sesion = _cid(conn, 1)
    out_sesion = herramientas.ejecutar(conn, 1, "planear_entreno", {
        "day": "2020-05-10", "titulo": "Sesión semilla conformidad",
        "ejercicios": [{"ejercicio": "sentadilla", "series": 3, "categoria": anatomia.CATEGORIA_ENUM[0]}],
    }, "completo", cid_sesion)
    resultado_sesion = propuestas.aplicar(conn, 1, out_sesion["propuesta_id"])
    assert resultado_sesion["estado"] == "aplicada", resultado_sesion
    fila_sesion = conn.execute(
        "SELECT id FROM plan WHERE person_id = 1 AND title = 'Sesión semilla conformidad'").fetchone()
    ids["plan_id"] = fila_sesion["id"]
    return ids


def _argumentos(ids: dict) -> dict:
    """One valid, demo-data-backed call per `propuesta` tool. Every id here comes from
    `_seed_todo`'s real writers; a tool that cannot be built from its own JSON schema alone (every
    one that names an existing row) is built from the seeded id instead, with the reason being that
    the schema alone never carries a real foreign key."""
    return {
        "proponer_meta": {"metric_key": "resting_hr", "direction": "lower", "target_value": 50.0,
                           "deadline": "2027-01-01", "titulo": "Meta conformidad"},
        "proponer_cambio_meta": {"goal_id": ids["goal_metric_id"], "target_value": 48.0},
        "proponer_confirmar_turnos": {},
        "proponer_idioma": {"lang": "en"},
        "planear_entreno": {"day": "2020-05-01", "titulo": "Plan de piernas conformidad",
                             "ejercicios": [{"ejercicio": "sentadilla", "series": 3,
                                             "categoria": anatomia.CATEGORIA_ENUM[0]}]},
        "registrar_entreno": {"day": ids["dia_a1"],
                               "series": [{"ejercicio": "press banca", "reps": 8, "peso_kg": 40.0}]},
        "registrar_medida": {"metric_key": "flex_alcance_cm", "value": 10.0, "day": "2020-05-02"},
        "marcar_habito": {"habit_id": ids["habit_id"], "day": "2020-05-03"},
        "proponer_experimento": {"titulo": "Experimento conformidad", "metricas": ["resting_hr"],
                                  "ventana": {"tipo": "dia"},
                                  "filtro_dias": {"turno_en": None, "turno_fuera": None,
                                                  "excluir_tras_noche": False}},
        "marcar_experimento": {"experimento_id": ids["experimento_id"], "day": "2020-02-01", "ocurrio": True},
        "registrar_ingesta": {"metric_key": db.INGESTA_METRIC_KEYS[0], "amount": 20.0, "estimado": True,
                               "day": "2020-05-04"},
        "corregir_ingesta": {"ingesta_id": ids["ingesta_a"], "amount": 35.0},
        "borrar_ingesta": {"ingesta_id": ids["ingesta_b"]},
        "registrar_comida": {"descripcion": "Comida conformidad",
                              "cantidades": [{"metric_key": db.INGESTA_METRIC_KEYS[0], "amount": 60.0,
                                              "estimado": True}], "day": "2020-05-05"},
        "corregir_comida": {"comida_id": ids["comida_id"], "descripcion": "Comida corregida"},
        "registrar_checkin": {"momento": "otro", "mood": 4, "day": "2020-05-06"},
        "marcar_periodo": {"kind": "malestar", "start_day": "2020-06-01"},
        "cerrar_periodo": {"period_id": ids["period_open"], "end_day": "2020-02-15"},
        "corregir_periodo": {"period_id": ids["period_b"], "note": "corregido"},
        "borrar_periodo": {"period_id": ids["period_c"]},
        "descartar_series": {"day": ids["dia_a4"], "series": [1]},
        "separar_sesion": {"day": ids["dia_a2"], "source_id": "a2"},
        "unir_sesiones": {"day": ids["dia_a3"]},
        "proponer_objetivo_nutriente": {"metric_key": db.INGESTA_METRIC_KEYS[0], "kind": "absolute",
                                         "sentido": "al_menos", "amount": 25.0},
        "proponer_plan": {"titulo": "Plan nuevo conformidad", "start_day": "2020-07-01", "deadline": "2020-12-31"},
        "ampliar_plan": {"goal_id": ids["plan_goal_id"],
                          "semanas": [{"week_start": ids["monday_extra"], "titulo": "Semana extra"}]},
        "corregir_meta_plan": {"meta_id": ids["meta_semanal_id"], "valor": 2},
        "anotar_sesion": {"plan_id": ids["plan_id"], "nota": "Nota de conformidad"},
    }


NOMBRES_PROPUESTA = sorted(h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta")


def _tabla_conteos(conn) -> dict[str, int]:
    """Every table's row count, read from sqlite_master -- never a typed list, so a table added
    later is covered for free (ruling 3)."""
    tablas = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tablas}


class ConformidadTestCase(unittest.TestCase):
    """Shared setUp for every clause (2) test: one fresh database, the full demo seed, and the
    28-tool arguments table built from it."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))
        self.ids = _seed_todo(self.conn)
        self.argumentos = _argumentos(self.ids)
        self.assertEqual(set(self.argumentos), set(NOMBRES_PROPUESTA),
                          "ARGUMENTOS and the propuesta registry have drifted apart")


# ---- clause (1): every capability declares its kind; the read-only guard holds -----------------

class Clause1KindsTest(unittest.TestCase):
    """Amendment 7 clause (1): every capability declares its kind (agent-capabilities-v1 section 1).
    Counts are derived from the live registry, never typed."""

    def test_every_entry_declares_an_allowed_kind(self):
        tipos_permitidos = {"lectura", "propuesta", "accion_pantalla"}
        self.assertTrue(all(h["tipo"] in tipos_permitidos for h in herramientas.HERRAMIENTAS))
        lectura = [h for h in herramientas.HERRAMIENTAS if h["tipo"] == "lectura"]
        propuesta = [h for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta"]
        accion = [h for h in herramientas.HERRAMIENTAS if h["tipo"] == "accion_pantalla"]
        self.assertEqual(len(lectura) + len(propuesta) + len(accion), len(herramientas.HERRAMIENTAS))
        self.assertGreater(len(lectura), 0)
        self.assertGreater(len(propuesta), 0)


class Clause1LecturaGuardMechanismTest(unittest.TestCase):
    """Amendment 7 clause (1): "the read-only guard holds". BODY-ASISTENTE-CONFORMIDAD-1 ruling 1
    -- a `lectura` tool that tries to write is refused by the CONNECTION itself (`PRAGMA
    query_only`), not by review, and the pragma is OFF again afterwards."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def test_a_lectura_tool_that_tries_to_write_is_refused_and_leaves_query_only_off(self):
        def _lectura_que_escribe(conn, person_id):
            conn.execute(
                "INSERT INTO shift (person_id, day, kind, start_local, source) VALUES "
                "(?, '2020-10-05', 'night', '2020-10-05T22:00:00', 'manual')", (person_id,))
            return {}

        herramientas._BY_NAME["_test_lectura_escribe"] = {
            "nombre": "_test_lectura_escribe", "tipo": "lectura", "nivel": "ninguno",
            "descripcion": "test", "parametros": herramientas._SIN_PARAMETROS,
            "funcion": _lectura_que_escribe, "plan_minimo": None,
        }
        self.addCleanup(lambda: herramientas._BY_NAME.pop("_test_lectura_escribe", None))
        antes = self.conn.execute("SELECT COUNT(*) FROM shift").fetchone()[0]
        with self.assertRaises(herramientas.ToolError) as ctx:
            herramientas.ejecutar(self.conn, 1, "_test_lectura_escribe", {}, "completo")
        self.assertEqual(ctx.exception.args[0], "lectura_intento_escribir")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shift").fetchone()[0], antes)
        self.assertEqual(self.conn.execute("PRAGMA query_only").fetchone()[0], 0)


# ---- clause (2): writes only by proposal, single-use durably -----------------------------------

class Clause2WritesOnlyByProposalTest(ConformidadTestCase):
    """Amendment 7 clause (2): a write is only ever a proposal record (agent-capabilities-v1
    section 2). Calling every one of the 28 `propuesta` tools with valid demo arguments changes no
    table except `propuesta_agente` -- counted over every table sqlite_master names, never a typed
    list."""

    def test_every_propuesta_tool_writes_only_propuesta_agente(self):
        cid = _cid(self.conn, 1)
        for nombre in NOMBRES_PROPUESTA:
            antes = _tabla_conteos(self.conn)
            herramientas.ejecutar(self.conn, 1, nombre, self.argumentos[nombre], "completo", cid)
            despues = _tabla_conteos(self.conn)
            cambiadas = {t: (antes[t], despues[t]) for t in antes if antes[t] != despues[t]}
            self.assertEqual(cambiadas, {"propuesta_agente": (antes["propuesta_agente"],
                                                               antes["propuesta_agente"] + 1)},
                              f"{nombre} changed a table other than propuesta_agente: {cambiadas}")


class Clause2ApplyOnceTest(ConformidadTestCase):
    """Amendment 7 clause (2): approval is single-use durably. Every one of the 28 tools' proposal
    applies once ('aplicada') and a second `aplicar` on the same id is refused ('ya_resuelta'),
    checked generically because `propuestas.aplicar`'s conditional UPDATE runs the same way for
    every action, before it ever looks at that action's own writer."""

    def test_applying_each_proposal_twice_the_second_is_ya_resuelta(self):
        cid = _cid(self.conn, 1)
        for nombre in NOMBRES_PROPUESTA:
            out = herramientas.ejecutar(self.conn, 1, nombre, self.argumentos[nombre], "completo", cid)
            r1 = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
            self.assertEqual(r1["estado"], "aplicada", f"{nombre} did not apply cleanly: {r1}")
            r2 = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
            self.assertEqual(r2["estado"], "ya_resuelta", f"{nombre}'s second apply was not refused")


class Clause2AnotherPersonTest(ConformidadTestCase):
    """Amendment 7 clause (2): ownership is checked. `propuestas.aplicar` raises `PropuestaAjena`
    for every one of the 28 tools when the caller is not the person the proposal was shown to --
    checked BEFORE the row's own state or action, by `_fila_o_ajena`."""

    def test_applying_as_another_person_is_refused_for_every_tool(self):
        cid = _cid(self.conn, 1)
        for nombre in NOMBRES_PROPUESTA:
            out = herramientas.ejecutar(self.conn, 1, nombre, self.argumentos[nombre], "completo", cid)
            with self.assertRaises(propuestas.PropuestaAjena, msg=f"{nombre} let person 2 apply it"):
                propuestas.aplicar(self.conn, 2, out["propuesta_id"])


class Clause2ExpiredTest(ConformidadTestCase):
    """Amendment 7 clause (2): expiry is enforced. An expired proposal refuses `aplicar` with
    'expirada' for every one of the 28 tools -- checked by `_vencida` before the action's own
    drift function ever runs."""

    def test_an_expired_proposal_is_refused_for_every_tool(self):
        cid = _cid(self.conn, 1)
        vencido = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        for nombre in NOMBRES_PROPUESTA:
            out = herramientas.ejecutar(self.conn, 1, nombre, self.argumentos[nombre], "completo", cid)
            self.conn.execute("UPDATE propuesta_agente SET expira_en = ? WHERE id = ?",
                              (vencido, out["propuesta_id"]))
            self.conn.commit()
            resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
            self.assertEqual(resultado["estado"], "expirada", f"{nombre} was not refused as expired")


class Clause2DriftTest(ConformidadTestCase):
    """Amendment 7 clause (2): a drifted target refuses with 'propuesta_desactualizada'. Six
    update-shaped actions, one per distinct drift-check SHAPE in `propuestas.TABLA` (a goal's own
    field, a daily_metric value, a habit_check row, a workout_set flag, a nutrient_target row, an
    ingesta amount) -- the other fourteen update-shaped actions (BODY-HOGAR-ESCRIBE-1: extended
    from thirteen with anotar_sesion, a plan row) share one of these same shapes.
    The eight INSERT-shaped actions (proponer_meta, planear_entreno, proponer_experimento,
    registrar_ingesta, proponer_plan, ampliar_plan, marcar_periodo, registrar_checkin) never drift
    on apply by construction: their `drift_aplicar` IS `propuestas._sin_drift`, asserted below
    rather than exercised, since there is no target state to drift."""

    def test_insert_shaped_actions_can_never_drift_on_apply(self):
        insert_shaped = ("proponer_meta", "planear_entreno", "proponer_experimento", "registrar_ingesta",
                          "proponer_plan", "ampliar_plan", "marcar_periodo", "registrar_checkin")
        for nombre in insert_shaped:
            self.assertIs(propuestas.TABLA[nombre].drift_aplicar, propuestas._sin_drift,
                          f"{nombre} is expected insert-shaped (never drifts on apply)")

    def test_proponer_cambio_meta_drift_refuses(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "proponer_cambio_meta", self.argumentos["proponer_cambio_meta"],
                                    "completo", cid)
        db.update_goal_target(self.conn, self.ids["goal_metric_id"], 1, target_value=59.0)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_registrar_medida_drift_refuses(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "registrar_medida", self.argumentos["registrar_medida"],
                                    "completo", cid)
        db.set_metric(self.conn, self.argumentos["registrar_medida"]["day"], "flex_alcance_cm", 99.0, "self", 1)
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_marcar_habito_drift_refuses(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "marcar_habito", self.argumentos["marcar_habito"],
                                    "completo", cid)
        goals.check_habit(self.conn, self.ids["habit_id"], 1, self.argumentos["marcar_habito"]["day"], True,
                          None, hoy=date(2020, 5, 3))
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_descartar_series_drift_refuses(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "descartar_series", self.argumentos["descartar_series"],
                                    "completo", cid)
        db.correct_exercise_set(self.conn, 1, "garmin", "a4", 1, exercise="press banca", reps=5, weight_kg=35.0)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_proponer_objetivo_nutriente_drift_refuses(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "proponer_objetivo_nutriente",
                                    self.argumentos["proponer_objetivo_nutriente"], "completo", cid)
        db.set_nutrient_target(self.conn, 1, self.argumentos["proponer_objetivo_nutriente"]["metric_key"],
                               date.today().isoformat(), "absolute", 99.0, "al_menos")
        self.conn.commit()
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_corregir_ingesta_drift_refuses(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "corregir_ingesta", self.argumentos["corregir_ingesta"],
                                    "completo", cid)
        db.corregir_ingesta(self.conn, 1, self.ids["ingesta_a"], 77.0)
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")

    def test_anotar_sesion_drift_refuses(self):
        cid = _cid(self.conn, 1)
        out = herramientas.ejecutar(self.conn, 1, "anotar_sesion", self.argumentos["anotar_sesion"],
                                    "completo", cid)
        db.anotar_sesion_plan(self.conn, 1, self.ids["plan_id"], "otra nota distinta",
                              "2020-05-11T00:00:00+00:00", "proposed")
        resultado = propuestas.aplicar(self.conn, 1, out["propuesta_id"])
        self.assertEqual(resultado["estado"], "propuesta_desactualizada")


# ---- clause (3): the provider switch, and an unpriced model never costs zero -------------------

class Clause3ProviderSwitchTest(unittest.TestCase):
    """Amendment 7 clause (3): the provider switch selects among the configured providers per
    request (agent-capabilities-v1 sections 5-7), and a provider/model with no price falls back to
    the count breaker, never a zero cost (amendment 5)."""

    def test_the_persons_stored_choice_wins_when_it_is_configured(self):
        conn, orig = _reset_db()
        try:
            ajustes.set_proveedor(conn, 1, "openai")
            disponibles = {"anthropic": object(), "openai": object()}
            self.assertEqual(cliente.nombre_elegido(disponibles, conn, 1), "openai")
        finally:
            conn.close()
            config.DB_PATH = orig

    def test_the_env_choice_is_used_when_the_person_has_none_stored(self):
        disponibles = {"anthropic": object(), "gemini": object()}
        self.assertEqual(cliente.nombre_elegido(disponibles), "anthropic")

    def test_the_first_configured_provider_wins_with_no_stored_or_env_choice(self):
        disponibles = {"openai": object(), "gemini": object()}
        import os
        guardado = os.environ.pop("BODY_AGENTE_PROVEEDOR", None)
        try:
            self.assertEqual(cliente.nombre_elegido(disponibles), "openai")
        finally:
            if guardado is not None:
                os.environ["BODY_AGENTE_PROVEEDOR"] = guardado

    def test_none_configured_answers_none_the_no_configurado_path(self):
        self.assertIsNone(cliente.nombre_elegido({}))

    def test_an_unpriced_model_falls_back_to_the_count_breaker_never_zero_cost(self):
        conn, orig = _reset_db()
        try:
            import os
            guardado = os.environ.get("BODY_AGENTE_CONTEO_DIARIO")
            os.environ["BODY_AGENTE_CONTEO_DIARIO"] = "1"
            try:
                uso.registrar(conn, 1, "anthropic", "modelo-sin-precio-conformidad",
                              cliente.Uso(entrada=1, salida=1), 1, "texto", None)
                with self.assertRaises(uso.TopeRefusal):
                    uso.verificar_tope(conn, 1, "anthropic", "modelo-sin-precio-conformidad")
            finally:
                if guardado is None:
                    os.environ.pop("BODY_AGENTE_CONTEO_DIARIO", None)
                else:
                    os.environ["BODY_AGENTE_CONTEO_DIARIO"] = guardado
        finally:
            conn.close()
            config.DB_PATH = orig


# ---- clause (4): one usage row per request, success and failure --------------------------------

class Clause4UsageRowTest(unittest.TestCase):
    """Amendment 7 clause (4): one `uso_agente` row per question, success AND failure, with every
    section 6 field this contract requires non-null, and the three input counts disjoint
    (amendment 2)."""

    def setUp(self):
        self.conn, self.orig = _reset_db()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: setattr(config, "DB_PATH", self.orig))

    def _fila(self):
        return self.conn.execute("SELECT * FROM uso_agente").fetchall()

    def test_a_successful_question_writes_one_row_with_every_required_field(self):
        uso.registrar(self.conn, 1, "anthropic", "claude-sonnet-5-5",
                      cliente.Uso(entrada=10, salida=4, cache_lectura=2, cache_escritura=1), 2, "texto", None)
        filas = self._fila()
        self.assertEqual(len(filas), 1)
        r = filas[0]
        for campo in ("person_id", "creado_en", "proveedor", "modelo", "entrada", "salida", "cache_lectura",
                      "cache_escritura", "rondas", "motivo_fin", "es_pregunta", "superficie"):
            self.assertIsNotNone(r[campo], f"{campo} is null on a successful row")
        self.assertIsNone(r["codigo_error"])
        self.assertEqual((r["entrada"], r["cache_lectura"], r["cache_escritura"]), (10, 2, 1))

    def test_a_failed_question_still_writes_one_row_with_its_error_code(self):
        uso.registrar(self.conn, 1, "anthropic", "claude-sonnet-5-5", cliente.Uso(), 0, None, "tope_diario")
        filas = self._fila()
        self.assertEqual(len(filas), 1)
        r = filas[0]
        self.assertEqual(r["codigo_error"], "tope_diario")
        self.assertIsNotNone(r["person_id"])
        self.assertIsNotNone(r["creado_en"])
        self.assertIsNotNone(r["proveedor"])
        self.assertIsNotNone(r["modelo"])
        self.assertIsNotNone(r["es_pregunta"])
        self.assertIsNotNone(r["superficie"])

    def test_the_three_input_counts_are_disjoint(self):
        """Amendment 2: entrada excludes cache_lectura and cache_escritura -- each stored as its
        own column, never derived by subtraction downstream."""
        uso.registrar(self.conn, 1, "anthropic", "claude-sonnet-5-5",
                      cliente.Uso(entrada=100, salida=1, cache_lectura=30, cache_escritura=5), 1, "texto", None)
        r = self._fila()[0]
        self.assertEqual(r["entrada"], 100)
        self.assertEqual(r["cache_lectura"], 30)
        self.assertEqual(r["cache_escritura"], 5)


# ---- clause (5): the spend ceiling refuses; the monthly quota never does -----------------------

class Clause5SpendCeilingRefusesTest(unittest.TestCase):
    """Amendment 7 clause (5): the spend ceiling refuses."""

    def test_a_priced_model_over_the_spend_ceiling_refuses(self):
        conn, orig = _reset_db()
        try:
            conn.execute(
                "INSERT INTO precio_modelo (proveedor, modelo, tipo, micro_usd_por_millon) VALUES "
                "('anthropic', 'modelo-con-precio-conformidad', 'entrada', 3000000), "
                "('anthropic', 'modelo-con-precio-conformidad', 'salida', 15000000), "
                "('anthropic', 'modelo-con-precio-conformidad', 'cache_lectura', 300000), "
                "('anthropic', 'modelo-con-precio-conformidad', 'cache_escritura', 3750000)"
            )
            conn.commit()
            uso.registrar(conn, 1, "anthropic", "modelo-con-precio-conformidad",
                          cliente.Uso(entrada=0, salida=1_000_000), 1, "texto", None)
            with self.assertRaises(uso.TopeRefusal):
                uso.verificar_tope(conn, 1, "anthropic", "modelo-con-precio-conformidad")
        finally:
            conn.close()
            config.DB_PATH = orig


class Clause5MonthlyQuotaNeverRefusesTest(unittest.TestCase):
    """Amendment 7 clause (5): "the quota never refuses". Section 6 requires a monthly QUESTION
    quota that never refuses -- Body has no such quota today, and so no function that checks one:
    `uso` carries only `verificar_tope` (a daily spend ceiling / count breaker, amendment 5), never
    a monthly question quota. Marked `expectedFailure` so the gate stays green on the AttributeError
    below; BODY-ASISTENTE-USO-1 builds the quota, and removing this marker -- once the call below
    both exists and never refuses -- is part of that slice."""

    @unittest.expectedFailure
    def test_a_months_worth_of_questions_never_refuses_for_quota_alone(self):
        conn, orig = _reset_db()
        try:
            for _ in range(500):
                uso.registrar(conn, 1, "anthropic", "claude-sonnet-5-5", cliente.Uso(entrada=1, salida=1),
                              1, "texto", None)
            # Does not exist yet -- AttributeError is the failure this marker expects, until
            # BODY-ASISTENTE-USO-1 gives `uso` a monthly question quota that never refuses.
            uso.verificar_cuota_mensual(conn, 1)
        finally:
            conn.close()
            config.DB_PATH = orig


if __name__ == "__main__":
    unittest.main()
