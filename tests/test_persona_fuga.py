"""BODY-PERSONA-FUGA-1: a black-box guard over BODY-SEGURIDAD-CENSO-1's one recorded unknown --
internal row-level filtering inside body/history.py, body/readiness.py, body/turnos_costo.py,
body/compartir.py and body/household.py was read as a pattern, not line by line. Two invented
people (A, B), every person-owned table seeded with a DISTINCTIVE marker, every GET route and
device-key API rendered as one of them, and every assistant read tool called as one of them: no
marker that belongs to the OTHER person may ever appear -- except what BODY-HOGAR-MIEMBRO-1 /
BODY-HOGAR-COMPARTIR-1 deliberately share, and only once A and B are linked and B has shared it.

Tests only (approval-reason, ORCH-CARLOS-DELEGATES-TIER-1-1): a leak found here is reported, never
patched in this module.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import (
    compartir, config, cuenta, db, dispositivo, enlaces, fechas, garmin_cuenta, goals, household,
    identity, patron_turnos, privacidad, readiness, shift_import, vida_export, web,
)
from body.agente import ajustes as agente_ajustes
from body.agente import conversacion as agente_conversacion
from body.agente import herramientas as agente_herramientas
from body.agente import propuestas as agente_propuestas
from body.i18n import T
from tests._rutas import rutas_planas

PERSON_A = 1   # db.local_person_id's answer in local mode
PERSON_B = 2

TODAY = date(2026, 11, 12)
N_DAYS = 20
DAYS = [(TODAY - timedelta(N_DAYS - 1 - i)).isoformat() for i in range(N_DAYS)]  # oldest .. TODAY

# Figures far outside each other's -- the spec's own examples (a resting HR of 187, a step count of
# 98765, a sleep of 17h43) plus enough of the other TODAY_CARDS/PATTERN_KEYS metrics to exercise
# Hoy, Dia, Tendencias and Historial without inventing a crash (readiness.compute tolerates missing
# parts; these constant series simply never clear MIN_PARTS, so the readiness score stays honestly
# None -- never a reason by itself to skip a figure).
METRICS_A = {"resting_hr": 55, "hrv_last_night": 48, "sleep_minutes": 430, "body_battery_wake": 62,
             "stress_avg": 27, "steps": 9000}
METRICS_B = {"resting_hr": 187, "hrv_last_night": 5, "sleep_minutes": 1063, "body_battery_wake": 2,
             "stress_avg": 95, "steps": 98765}

_T_ES = T("es")


def _fig_text(key: str, value: float) -> str:
    """The exact rendered form comun._value/_fig_unit produce for this value, in Spanish (the
    default language every seeded person keeps) -- minutes for the one _is_minutes key this test
    seeds, a plain number otherwise."""
    if key == "sleep_minutes":
        return _T_ES.minutes(value)
    return _T_ES.num(value)


# Only these two are searched for as leak markers: steps/sleep_minutes render with a thousands
# separator or an "h"/"min" split ("98.765", "17 h 43") that never occurs by coincidence elsewhere
# on a page or in an export (the raw JSON export carries the SAME figure as a plain unformatted
# number, e.g. 98765.0 -- no literal "98.765" substring either way). resting_hr was tried here too
# and dropped: MEASURED to self-collide -- with A's resting_hr=55, a sweep of B's own
# /privacidad/exportar failed on '55' matching "...T18:31:55+00:00", the SECONDS field of the
# export's own timestamp, nothing to do with A. Any one- or two-digit figure is a hostage to every
# hour/minute/second/day field a page prints, so this guard never searches for one (hrv_last_night/
# body_battery_wake/stress_avg are seeded for baseline/Tendencias/Historial realism only, same
# reason).
_FIGURE_KEYS = ("steps", "sleep_minutes")
FIGS_A = {k: _fig_text(k, METRICS_A[k]) for k in _FIGURE_KEYS}
FIGS_B = {k: _fig_text(k, METRICS_B[k]) for k in _FIGURE_KEYS}

ENLACE_REF = {"A": "123456789012", "B": "987654321098"}

client = TestClient(web.app, follow_redirects=False)

# ---- table census: every table with a literal person_id column, per body/privacidad.py, plus the
# three tables owned through another id (habit via goal_id, goal_enlace via goal_id,
# shift_batch_item via batch_id) -- the full inventory BODY-SEGURIDAD-CENSO-1 named.
_REACHED_THROUGH_TABLES = {"habit", "goal_enlace", "shift_batch_item"}
_HOUSEHOLD_ONLY_TABLES = {"hogar_miembro", "figura_compartida"}  # need an active household to exist at all


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "fuga.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.init(conn)
    conn.execute("INSERT INTO person (id, display_name) VALUES (?, 'B')", (PERSON_B,))
    conn.commit()
    return conn


def _as(person_id: int) -> None:
    web.app.dependency_overrides[identity.current_person_id] = lambda: person_id


def _seed_person(conn, person_id: int, tag: str) -> dict:
    """Every person-owned table this app has, except hogar_miembro / figura_compartida (need an
    ACTIVE household to exist at all -- compartir.compartir raises ValueError otherwise; seeded
    separately once A and B are linked). Returns every id/key a sweep needs, plus the markers used."""
    m = METRICS_A if tag == "A" else METRICS_B
    for day in DAYS:
        for key, value in m.items():
            db.set_metric(conn, day, key, value, "garmin", person_id)
    readiness.recompute_all(conn, person_id)  # the normal recompute, not a hand-built row

    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) VALUES (?, 'act-1', ?, ?, NULL, ?, 1800, "
        "5000, 130, 150, 300, 'garmin')",
        (person_id, DAYS[-1], f"{DAYS[-1]}T07:00:00", f"ZZ{tag}-ACTIVIDAD"),
    )
    conn.execute(
        "INSERT INTO workout_set (person_id, activity_source, activity_source_id, set_order, "
        "set_type, person_exercise, created_at, updated_at) VALUES (?, 'garmin', 'act-1', 1, "
        "'active', ?, ?, ?)",
        (person_id, f"ZZ{tag}-SERIE", f"{DAYS[-1]}T07:05:00+00:00", f"{DAYS[-1]}T07:05:00+00:00"),
    )
    sleep_start, sleep_end = (("23:01", "06:13") if tag == "A" else ("23:07", "07:19"))
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, DAYS[-1], f"{DAYS[-2]}T{sleep_start}:00", f"{DAYS[-1]}T{sleep_end}:00"),
    )
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source) "
        "VALUES (?, ?, 'night', ?, ?, ?, 'manual')",
        (person_id, DAYS[-2], f"ZZ{tag}-TURNO", f"{DAYS[-2]}T22:00:00", f"{DAYS[-1]}T06:00:00"),
    )

    received = f"2026-10-{20 if tag == 'A' else 25}T12:00:00-05:00"
    conn.execute(
        "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, status, day_count, "
        "created_at, kind) VALUES (?, ?, ?, ?, 'proposed', 1, ?, 'import')",
        (person_id, f"turnos-{tag.lower()}.json", received, received, received),
    )
    batch_id = conn.execute(
        "SELECT id FROM shift_batch WHERE person_id = ? AND file_name = ?",
        (person_id, f"turnos-{tag.lower()}.json"),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO shift_batch_item (batch_id, day, kind, code, start_local, end_local) "
        "VALUES (?, ?, 'night', ?, ?, ?)",
        (batch_id, DAYS[-1], f"ZZ{tag}-LOTE", f"{DAYS[-1]}T22:00:00", f"{DAYS[-1]}T23:00:00"),
    )

    patron_turnos.guardar(conn, person_id, DAYS[0], [{"kind": "night", "start": "22:00", "end": "06:00"}],
                          horizon_weeks=8 if tag == "A" else 26)

    gid = goals.propose(conn, person_id, f"ZZ{tag}-META-PRIVADA", DAYS[0], kind="metric", metric_key="resting_hr",
                        direction="lower", target_value=(50.0 if tag == "A" else 150.0),
                        baseline_value=float(m["resting_hr"]), deadline="2027-06-01")
    goals.activate(conn, gid)
    hid = goals.add_habit(conn, gid, f"ZZ{tag}-HABITO", {"per": "day", "times": 1})
    goals.check_habit(conn, hid, person_id, DAYS[-1], True, f"ZZ{tag}-NOTA", hoy=TODAY)
    monday = fechas.lunes_de(TODAY - timedelta(7))
    goals.weekly_check(conn, gid, person_id, monday.isoformat())
    enlaces.enlazar(conn, gid, "lugares", ENLACE_REF[tag])

    gid_proposed = goals.propose(conn, person_id, f"ZZ{tag}-META-PROPUESTA", DAYS[0], kind="milestone",
                                 deadline="2027-01-01")
    gid_archived = goals.propose(conn, person_id, f"ZZ{tag}-META-ARCHIVADA", DAYS[0], kind="milestone",
                                 deadline="2027-01-01")
    goals.activate(conn, gid_archived)
    goals.pause(conn, gid_archived)

    conn.execute(
        "INSERT INTO plan (person_id, goal_id, day, title, status) VALUES (?, ?, ?, ?, 'proposed')",
        (person_id, gid, DAYS[-1], f"ZZ{tag}-PLAN"),
    )

    # Wave 9 (BODY-EXPERIMENTOS-ESQUEMA-1): experimento, experimento_registro and moment are all
    # directly person-keyed (each carries its own person_id column), so they are seeded here like
    # every other such table -- no household needed, nothing "reached through" another id.
    # BODY-EXPERIMENTOS-PANTALLA-1 gave this one a screen (Metas' section and /experiments/{id}):
    # its titulo and its one registro's nota now render there, so both move into _markers_of's
    # active marker list below. moment stays excluded -- no screen, API or assistant tool reads it.
    eid = db.crear_experimento(
        conn, person_id, f"ZZ{tag}-EXPERIMENTO", ["resting_hr"], {"tipo": "dia"},
        {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False},
    )
    mid = db.crear_momento(conn, person_id, f"{DAYS[-1]}T07:00:00-05:00", "manual", f"ZZ{tag}-MOMENTO")
    db.registrar_dia_experimento(conn, eid, person_id, DAYS[-1], True, f"ZZ{tag}-REGISTRO", mid)

    # BODY-PROTEINA-1: ingesta is person-keyed and, through leer_dia, has its FIRST reader this
    # slice -- so, unlike moment (no reader yet), its marker belongs in the tool sweep below, not in
    # the route-only active list (no screen renders it yet).
    db.registrar_ingesta(conn, person_id, DAYS[-1], "protein_g", 30.0, False, f"ZZ{tag}-INGESTA")

    # BODY-PERIODO-1: period is person-keyed and, through leer_dia's new `periodos` list, has its
    # FIRST reader this slice -- same treatment as ingesta above, not the route-only active list
    # (the screens that will show it are a separate, later slice).
    db.crear_periodo(conn, person_id, "malestar", DAYS[-1], DAYS[-1], f"ZZ{tag}-PERIODO")

    agente_ajustes.set_nivel_datos(conn, person_id, "completo")
    privacidad.record_consent(conn, person_id)

    conv_id = agente_conversacion.crear(conn, person_id, titulo=f"ZZ{tag}-CONVERSACION")
    agente_conversacion.agregar_mensaje(conn, conv_id, person_id, "usuario",
                                        [{"tipo": "texto", "texto": f"ZZ{tag}-MENSAJE"}])
    propuesta = agente_propuestas.crear(conn, person_id, conv_id, "proponer_idioma", {"lang": "en"},
                                        antes={"lang": "es"})

    device_key = dispositivo.crear(conn, person_id, f"ZZ{tag}-DISPOSITIVO", {"turnos:subir", "vida:leer"})
    garmin_cuenta.guardar(conn, person_id, f"FAKE-TEST-TOKEN-ZZ{tag}-NOT-REAL")
    cuenta.create_session(conn, person_id, f"sid-zz{tag.lower()}")

    now = f"{DAYS[-1]}T12:00:00+00:00"
    conn.execute(
        "INSERT INTO raw_payload (person_id, day, kind, source, fetched_at, payload) "
        "VALUES (?, ?, 'user_summary', 'garmin', ?, '{}')",
        (person_id, DAYS[-1], now),
    )
    conn.execute(
        "INSERT INTO uso_agente (person_id, creado_en, proveedor, modelo, entrada, salida, rondas, "
        "es_pregunta, superficie) VALUES (?, ?, 'demo', 'demo', 10, 10, 1, 1, 'asistente')",
        (person_id, now),
    )
    conn.execute(
        "INSERT INTO tope_agente (person_id, diario_micro_usd, conteo_diario) VALUES (?, 100000, 50)",
        (person_id,),
    )
    conn.commit()

    return {
        "gid": gid, "hid": hid, "gid_proposed": gid_proposed, "gid_archived": gid_archived,
        "conv_id": conv_id, "batch_id": batch_id, "device_id": conn.execute(
            "SELECT id FROM dispositivo WHERE person_id = ?", (person_id,)).fetchone()["id"],
        "device_key": device_key, "propuesta_id": propuesta["id"], "eid": eid,
    }


def _markers_of(tag: str) -> list[str]:
    """Every TEXT marker this person's seed planted that some screen, API or export can actually
    show -- searched for literally, in every sweep response. shift_batch_item's own `code` column
    (seeded as ZZ<tag>-LOTE, for the table census below) is deliberately NOT in this list: no
    screen, API or export reads that column today (turnos.py's preview/shifts.html show only
    counts and kept-days; privacidad.export_person's shift_batches is a plain `SELECT * FROM
    shift_batch`, never joined to its items) -- a marker that renders nowhere would make the
    "appears for its own owner" half of this guard vacuous, not a real check. Wave 9's own
    ZZ<tag>-MOMENTO is excluded for the same reason (no route, API or assistant tool reads
    `moment` yet); ZZ<tag>-EXPERIMENTO (the experiment's own titulo) and ZZ<tag>-REGISTRO (its one
    registro's nota) moved INTO this active list with BODY-EXPERIMENTOS-PANTALLA-1, which gave
    `experimento`/`experimento_registro` their first reader -- Metas' section and
    /experiments/{id} render both."""
    return [f"ZZ{tag}-{suffix}" for suffix in
            ("ACTIVIDAD", "SERIE", "TURNO", "META-PRIVADA", "HABITO", "NOTA", "META-PROPUESTA",
             "META-ARCHIVADA", "PLAN", "CONVERSACION", "MENSAJE", "DISPOSITIVO", "EXPERIMENTO",
             "REGISTRO")] + [ENLACE_REF[tag]]


def _figures_of(tag: str) -> dict:
    return FIGS_A if tag == "A" else FIGS_B


# ---- every GET route this app has, from tests/_rutas.rutas_planas -- a new one that appears here
# and is not in EXPECTED_GET_PATHS fails _assert_route_set_unchanged below instead of silently
# going unswept.
EXPECTED_GET_PATHS = sorted([
    "/", "/how/{day}", "/day/{day}", "/goals", "/goals/new", "/goals/{goal_id}",
    "/experiments/{experiment_id}",
    "/settings", "/ajustes/asistente", "/ajustes/dispositivos", "/api/vida/semana",
    "/healthz", "/ayuda", "/garmin", "/history",
    "/privacidad/consentimiento", "/privacidad", "/privacidad/exportar",
    "/sw.js", "/login", "/login/cuenta/start", "/login/cuenta/callback",
    "/trends", "/shifts", "/shifts/como", "/shifts/patron",
    "/asistente/conversacion/{conversacion_id}",
])

# Routes that need no identity-scoped id substitution and are safe to call plainly.
PLAIN_GET_ROUTES = [
    "/", "/goals", "/goals/new", "/settings", "/ajustes/asistente", "/ajustes/dispositivos",
    "/healthz", "/ayuda", "/garmin", "/history",
    "/privacidad/consentimiento", "/privacidad", "/privacidad/exportar",
    "/sw.js", "/login", "/login/cuenta/start", "/login/cuenta/callback",
    "/trends", "/shifts", "/shifts/como", "/shifts/patron",
]

READ_TOOLS = ["leer_hoy", "leer_dia", "leer_como_calculo", "leer_tendencias", "leer_historia",
              "leer_metas", "leer_meta", "leer_turnos", "leer_costo_turnos", "leer_ajustes",
              "leer_tutoriales"]


class _BaseFugaTest(unittest.TestCase):
    """Shared setup: A and B, UNLINKED, each fully seeded (hosted mode on, so the device-key API
    and the dispositivos/garmin screens are reachable -- identity.current_person_id is overridden
    for every other route, so no real Cuenta session is needed for them)."""

    def setUp(self):
        self.conn = _reset_db(self)
        env = {cuenta.ISSUER_ENV: "https://fake-cuenta.test", cuenta.PUBLIC_URL_ENV: "http://127.0.0.1:8770",
               cuenta.CLIENT_SECRET_ENV: "s3cr3t-not-real", garmin_cuenta.KEY_ENV: os.urandom(32).hex()}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ids = {"A": _seed_person(self.conn, PERSON_A, "A"), "B": _seed_person(self.conn, PERSON_B, "B")}

    def _sweep_routes_as(self, viewer_tag: str, other_tag: str,
                         allowed_figs: frozenset = frozenset()) -> tuple[list[str], int]:
        """GET-sweeps every route in EXPECTED_GET_PATHS as `viewer_tag`; returns (texts, n_swept).
        Raises via assertions the moment a foreign marker or figure appears (unless that exact
        rendered figure string is in `allowed_figs` -- the one case this spec means to allow is a
        figure B deliberately shared, BODY-HOGAR-COMPARTIR-1), or an id-taking route fails to 404
        on the other person's id."""
        viewer_id = PERSON_A if viewer_tag == "A" else PERSON_B
        ids = self.ids[viewer_tag]
        other_ids = self.ids[other_tag]
        foreign_markers = _markers_of(other_tag)
        foreign_figs = [f for f in _figures_of(other_tag).values() if f not in allowed_figs]
        _as(viewer_id)
        texts = []
        n = 0
        for path in PLAIN_GET_ROUTES:
            r = client.get(path)
            n += 1
            # /login/cuenta/callback with no flujo cookie answers 400 (FlowRefused) -- it runs
            # ahead of any identity resolution (no Depends(current_person_id) at all), so it
            # carries no figure or marker of anyone and is included only for route coverage.
            self.assertIn(r.status_code, (200, 303, 400, 404), f"{viewer_tag} GET {path} -> {r.status_code}")
            if r.status_code == 200:
                texts.append(r.text)
        for day in (DAYS[-1], DAYS[-2]):
            r = client.get(f"/how/{day}")
            n += 1
            self.assertEqual(r.status_code, 200, f"{viewer_tag} GET /how/{day}")
            texts.append(r.text)
            r = client.get(f"/day/{day}")
            n += 1
            self.assertEqual(r.status_code, 200, f"{viewer_tag} GET /day/{day}")
            texts.append(r.text)

        for key in ("gid", "gid_proposed", "gid_archived"):
            r = client.get(f"/goals/{ids[key]}")
            n += 1
            self.assertEqual(r.status_code, 200, f"{viewer_tag} GET /goals/{{{key}}}")
            texts.append(r.text)
        n += 1
        r = client.get(f"/goals/{other_ids['gid']}")
        self.assertEqual(r.status_code, 404, f"{viewer_tag} GET /goals/<{other_tag}'s id> did not 404")

        r = client.get(f"/experiments/{ids['eid']}")
        n += 1
        self.assertEqual(r.status_code, 200, f"{viewer_tag} GET /experiments/{{eid}}")
        texts.append(r.text)
        n += 1
        r = client.get(f"/experiments/{other_ids['eid']}")
        self.assertEqual(r.status_code, 404, f"{viewer_tag} GET /experiments/<{other_tag}'s id> did not 404")

        r = client.get(f"/asistente/conversacion/{ids['conv_id']}")
        n += 1
        self.assertEqual(r.status_code, 200)
        texts.append(r.text)
        r = client.get(f"/asistente/conversacion/{other_ids['conv_id']}")
        self.assertEqual(r.status_code, 404, f"{viewer_tag} GET /asistente/conversacion/<{other_tag}'s id> did not 404")

        r = client.get("/api/vida/semana", headers={"Authorization": f"Bearer {ids['device_key']}"})
        n += 1
        self.assertEqual(r.status_code, 200)
        texts.append(r.text)

        for text in texts:
            for marker in foreign_markers:
                self.assertNotIn(marker, text, f"{viewer_tag} sweep leaked {other_tag}'s marker {marker!r}")
            for fig in foreign_figs:
                self.assertNotIn(fig, text, f"{viewer_tag} sweep leaked {other_tag}'s figure {fig!r}")
        return texts, n

    def _sweep_tools_as(self, viewer_tag: str, other_tag: str) -> int:
        viewer_id = PERSON_A if viewer_tag == "A" else PERSON_B
        ids = self.ids[viewer_tag]
        other_ids = self.ids[other_tag]
        foreign_markers = _markers_of(other_tag)
        n = 0
        for nombre in READ_TOOLS:
            if nombre == "leer_dia" or nombre == "leer_como_calculo":
                params = {"day": DAYS[-1]}
            elif nombre == "leer_meta":
                params = {"goal_id": ids["gid"]}
            else:
                params = {}
            out = agente_herramientas.ejecutar(self.conn, viewer_id, nombre, params, "completo")
            n += 1
            text = repr(out)
            for marker in foreign_markers:
                self.assertNotIn(marker, text, f"{viewer_tag} tool {nombre} leaked {other_tag}'s marker {marker!r}")
            if nombre == "leer_dia":
                # BODY-PROTEINA-1: leer_dia's new ingesta list -- checked directly (own marker
                # renders, the other person's never does) rather than through the active marker
                # list, since no screen reads it yet (see _seed_person's own comment).
                self.assertIn(f"ZZ{viewer_tag}-INGESTA", text,
                              f"{viewer_tag}'s own ingesta marker never rendered by leer_dia")
                self.assertNotIn(f"ZZ{other_tag}-INGESTA", text,
                                 f"{viewer_tag} tool leer_dia leaked {other_tag}'s ingesta marker")
                # BODY-PERIODO-1: same direct check for leer_dia's new `periodos` list.
                self.assertIn(f"ZZ{viewer_tag}-PERIODO", text,
                              f"{viewer_tag}'s own period marker never rendered by leer_dia")
                self.assertNotIn(f"ZZ{other_tag}-PERIODO", text,
                                 f"{viewer_tag} tool leer_dia leaked {other_tag}'s period marker")
        out_own_meta = agente_herramientas.ejecutar(self.conn, viewer_id, "leer_meta", {"goal_id": ids["gid"]},
                                                    "completo")
        self.assertTrue(out_own_meta["encontrada"])
        out_foreign_meta = agente_herramientas.ejecutar(self.conn, viewer_id, "leer_meta",
                                                         {"goal_id": other_ids["gid"]}, "completo")
        self.assertFalse(out_foreign_meta["encontrada"])
        return n


class UnlinkedSweepTest(_BaseFugaTest):
    """BODY and B are NOT in a household: every GET route, device-key API and read tool, both ways
    -- not one marker of the other may ever appear."""

    def test_sweep_as_a_then_as_b_no_leak_either_way(self):
        texts_a, n_routes_a = self._sweep_routes_as("A", "B")
        n_tools_a = self._sweep_tools_as("A", "B")
        texts_b, n_routes_b = self._sweep_routes_as("B", "A")
        n_tools_b = self._sweep_tools_as("B", "A")

        # Non-vacuous: the sweep actually visited something, and each marker IS rendered somewhere
        # when its own owner looks -- a marker never shown anywhere would prove nothing either way.
        total = n_routes_a + n_tools_a + n_routes_b + n_tools_b
        self.assertGreater(total, 25, "sweep covered too few routes/tools to be a real guard")
        all_text_a = "\n".join(texts_a)
        all_text_b = "\n".join(texts_b)
        for marker in _markers_of("A"):
            self.assertIn(marker, all_text_a, f"A's own marker {marker!r} never rendered anywhere -- fix or drop it")
        for marker in _markers_of("B"):
            self.assertIn(marker, all_text_b, f"B's own marker {marker!r} never rendered anywhere -- fix or drop it")
        for fig in _figures_of("A").values():
            self.assertIn(fig, all_text_a, f"A's own figure {fig!r} never rendered anywhere -- fix or drop it")
        for fig in _figures_of("B").values():
            self.assertIn(fig, all_text_b, f"B's own figure {fig!r} never rendered anywhere -- fix or drop it")


class DeviceAndPropuestaCrossIdTest(_BaseFugaTest):
    """Two NEW resource types this spec's seed introduces (device keys, assistant proposals): the
    id-taking POST actions on them 404 on the other person's id too, same as goals/habits already
    do (BODY-PERSON-SCOPE-1)."""

    def test_device_revoke_404s_on_the_other_persons_device(self):
        _as(PERSON_A)
        r = client.post(f"/ajustes/dispositivos/{self.ids['B']['device_id']}/revocar")
        # The route itself answers 303 either way (revocar() is a silent no-op on a foreign id --
        # dispositivo.revocar's own WHERE person_id = ? already refuses the row); the row itself is
        # the proof, not the status code.
        self.assertEqual(r.status_code, 303)
        row = self.conn.execute("SELECT revocado_en FROM dispositivo WHERE id = ?",
                                (self.ids["B"]["device_id"],)).fetchone()
        self.assertIsNone(row["revocado_en"], "A's revoke call touched B's device key")

    def test_propuesta_actions_404_on_the_other_persons_propuesta(self):
        _as(PERSON_A)
        for accion in ("aplicar", "descartar", "deshacer"):
            r = client.post(f"/asistente/propuestas/{self.ids['B']['propuesta_id']}/{accion}")
            self.assertEqual(r.status_code, 404, f"A POST .../{accion} on B's propuesta did not 404")
        row = self.conn.execute("SELECT estado FROM propuesta_agente WHERE id = ?",
                                (self.ids["B"]["propuesta_id"],)).fetchone()
        self.assertEqual(row["estado"], "propuesta", "A's action calls touched B's propuesta")


class LinkedSharingSweepTest(_BaseFugaTest):
    """A and B linked into one household; B shares exactly resting_hr, and a milestone goal is
    created shared (BODY-HOGAR-MIEMBRO-1 / BODY-HOGAR-COMPARTIR-1). The shared items may now appear
    on A's pages in their allowed places; every OTHER marker of B's -- private goal, habit,
    activity, device, conversation, the OTHER five figures -- must still never appear anywhere."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, 'h-fuga', "
            "'2026-01-01T00:00:00+00:00')", (PERSON_A,))
        self.conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, 'h-fuga', "
            "'2026-01-01T00:00:00+00:00')", (PERSON_B,))
        self.conn.commit()
        compartir.compartir(self.conn, PERSON_B, "resting_hr")
        hh = household.household_of(self.conn, PERSON_A)
        gid_shared = goals.propose(self.conn, PERSON_B, "ZZB-META-COMPARTIDA", DAYS[0], kind="milestone",
                                   deadline="2027-06-01", hogar_id=hh.id, visibility="household")
        goals.activate(self.conn, gid_shared)
        self.conn.commit()
        self.gid_shared = gid_shared

    def test_shared_figure_and_goal_appear_unshared_markers_still_hidden(self):
        with mock.patch("body.rutas.comun._today_local", return_value=TODAY):
            _as(PERSON_A)
            r_home = client.get("/")
            self.assertEqual(r_home.status_code, 200)
            r_goals = client.get("/goals")
            self.assertEqual(r_goals.status_code, 200)
            r_goal_shared = client.get(f"/goals/{self.gid_shared}")
            self.assertEqual(r_goal_shared.status_code, 200)

        # ALLOWED: B's shared figure and B's shared milestone goal title show on A's pages. The
        # rendered resting_hr figure is computed locally here, not through _FIGURE_KEYS/FIGS_B --
        # this is a presence (assertIn) check on one page's own content, not an absence check
        # across a whole sweep, so the short-number collision risk that keeps resting_hr out of
        # the broad guard (see _FIGURE_KEYS's comment) does not apply to it.
        self.assertIn(_fig_text("resting_hr", METRICS_B["resting_hr"]), r_home.text)
        self.assertIn("ZZB-META-COMPARTIDA", r_goals.text)
        self.assertIn("ZZB-META-COMPARTIDA", r_goal_shared.text)

        # STILL FORBIDDEN: every other marker/figure B owns, private or not shared. FIGS_B/
        # _markers_of carry no overlap with the shared goal or the shared figure, so this needs no
        # allow-list.
        texts, n_routes = self._sweep_routes_as("A", "B")
        self.assertGreater(n_routes, 25)
        combined = r_home.text + r_goals.text + r_goal_shared.text + "\n".join(texts)
        for marker in _markers_of("B"):
            self.assertNotIn(marker, combined, f"linked sweep leaked B's unshared marker {marker!r}")
        for key, fig in FIGS_B.items():
            if key == "resting_hr":
                continue
            self.assertNotIn(fig, combined, f"linked sweep leaked B's unshared figure {fig!r} ({key})")

    def test_b_sees_nothing_of_a_since_a_shared_nothing(self):
        texts, n_routes = self._sweep_routes_as("B", "A")
        self.assertGreater(n_routes, 10)
        combined = "\n".join(texts)
        for marker in _markers_of("A"):
            self.assertNotIn(marker, combined, f"B's sweep leaked A's marker {marker!r} though A shared nothing")
        for fig in FIGS_A.values():
            self.assertNotIn(fig, combined, f"B's sweep leaked A's figure {fig!r} though A shared nothing")


class VidaExportNoLeakTest(_BaseFugaTest):
    """vida_export's week section -- the hosted route (A's own device key) and the module function
    directly -- carries no marker or figure of the other person, unlinked or linked."""

    def _check_both_states(self, viewer_tag: str, other_tag: str):
        viewer_id = PERSON_A if viewer_tag == "A" else PERSON_B
        ids = self.ids[viewer_tag]
        monday = vida_export.current_week_monday(hoy=TODAY)
        data = vida_export.week_section(self.conn, monday, viewer_id)
        text = repr(data)
        _as(viewer_id)
        r = client.get("/api/vida/semana", headers={"Authorization": f"Bearer {ids['device_key']}"})
        self.assertEqual(r.status_code, 200)
        text += r.text
        for marker in _markers_of(other_tag):
            self.assertNotIn(marker, text, f"vida_export leaked {other_tag}'s marker {marker!r} to {viewer_tag}")
        for fig in _figures_of(other_tag).values():
            self.assertNotIn(fig, text, f"vida_export leaked {other_tag}'s figure {fig!r} to {viewer_tag}")

    def test_unlinked(self):
        self._check_both_states("A", "B")
        self._check_both_states("B", "A")

    def test_linked_and_shared(self):
        self.conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, 'h-fuga', "
            "'2026-01-01T00:00:00+00:00')", (PERSON_A,))
        self.conn.execute(
            "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, 'h-fuga', "
            "'2026-01-01T00:00:00+00:00')", (PERSON_B,))
        self.conn.commit()
        compartir.compartir(self.conn, PERSON_B, "resting_hr")
        self._check_both_states("A", "B")
        self._check_both_states("B", "A")


class TableCensusTest(_BaseFugaTest):
    """Every person-owned table either got a seeded row in _seed_person, or is accounted for here
    with why (compartir.compartir refuses with no active household, and household_of/hogar_miembro
    is the same story, so both are seeded only in the linked-state tests above)."""

    def test_every_person_table_is_seeded_or_explained(self):
        all_person_tables = privacidad.person_tables(self.conn)
        self.assertEqual(
            all_person_tables,
            privacidad.FULLY_HANDLED_TABLES | privacidad.PARTIAL_EXPORT_TABLES | privacidad.NEVER_EXPORTED_TABLES,
            "a table with a person_id column exists that body/privacidad.py does not account for -- "
            "this guard's own table census is stale",
        )
        seeded_in_unlinked_setup = (
            privacidad.FULLY_HANDLED_TABLES | privacidad.PARTIAL_EXPORT_TABLES | privacidad.NEVER_EXPORTED_TABLES
            | _REACHED_THROUGH_TABLES
        ) - _HOUSEHOLD_ONLY_TABLES
        for table in sorted(seeded_in_unlinked_setup):
            count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertGreater(count, 0, f"{table} has a person_id column but setUp seeded no row in it")
        # hogar_miembro / figura_compartida: empty here (no household yet), seeded by
        # LinkedSharingSweepTest / VidaExportNoLeakTest.test_linked_and_shared instead.
        for table in sorted(_HOUSEHOLD_ONLY_TABLES):
            count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, f"{table} unexpectedly has rows before any household link")


class RouteSetUnchangedTest(unittest.TestCase):
    """If a new GET route is added to body/rutas/*.py and not to EXPECTED_GET_PATHS above, this
    fails loudly instead of the sweep silently covering 25 of 26 routes forever."""

    def test_discovered_get_routes_match_the_hand_list(self):
        discovered = sorted({
            r.path for r in rutas_planas(web.app.routes)
            if hasattr(r, "methods") and r.methods and "GET" in r.methods
        })
        self.assertEqual(discovered, EXPECTED_GET_PATHS)


if __name__ == "__main__":
    unittest.main()
