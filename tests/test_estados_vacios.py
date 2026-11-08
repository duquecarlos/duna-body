"""BODY-ESTADOS-VACIOS-1: every screen swept for a person who is NOT Carlos-with-871-days.

The existing GET sweeps (tests/test_get_sin_escritura.py, tests/test_persona_fuga.py,
tests/test_rendimiento.py) all run on ONE populated fixture (body/demo.py's seed). This module
seeds six sparse/empty states instead -- a brand-new account, one day of data, a watch that sends
only some metrics, shifts turned on with no shift yet (plus a night block with no data after it),
a goal with no data, and a watch that stopped syncing -- each in Spanish AND English, and sweeps
every GET route tests/_rutas.rutas_planas finds for each one.

Excluded from the sweep, deliberately: "/asistente/conversacion/{conversacion_id}" and
"/api/vida/semana". The first needs a seeded conversation (body/agente, not in this slice's
touches); the second (device-key auth) always 404s in local mode regardless of person state
(identity.person_from_device raises HTTPException(404) before any person is resolved) -- both are
still reached structurally by every other local-mode test that sweeps the full GET set, so neither
is a coverage gap this slice leaves silently open.

Non-vacuous: every state/language combination must reach "/" with 200, and the total number of
(route, state, language) calls is asserted well above a token sample.
"""
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import config, db, demo, fechas, goals, identity, web
from tests._rutas import rutas_planas

client = TestClient(web.app, follow_redirects=False)

TODAY = fechas.hoy_local({})

# Every metric a watch can send in one day (body/demo.py's own catalogue) -- used for states B and
# F, which both need a "full" day. Values are the midpoint of demo.py's own plausible range: never
# a real figure, never a zero used as if it were a measurement.
_FULL_METRIC_KEYS = demo.CORE_DENSE + demo.SECONDARY + demo.CARDIO_SPARSE
_INVENTED = {k: round((lo + hi) / 2, 1) for k, (lo, hi) in demo.RANGES.items()}

# Routes that always 404 in LOCAL mode (no Cuenta sign-in, no device keys configured here),
# whatever the person's own state is -- body/rutas/{garmin,dispositivos,sesion,privacidad,
# dispositivos}.py all gate on cuenta.hosted() or identity.person_from_device before touching the
# person at all.
_ALWAYS_404_LOCAL = {
    "/garmin", "/ajustes/dispositivos", "/login", "/login/cuenta/start", "/login/cuenta/callback",
    "/privacidad/consentimiento", "/api/vida/semana",
}
_TURNOS_GATED = {"/shifts", "/shifts/como", "/shifts/patron"}
# Needs a seeded conversation (body/agente, outside this slice's touches) -- see module docstring.
_SKIPPED_PARAM_ROUTES = {"/asistente/conversacion/{conversacion_id}"}

# ---- raw-value-leak detection -------------------------------------------------------------------
# Word-boundary match for the five leak words, so a real word that merely CONTAINS one of them
# (e.g. "información" contains "inf") never matches -- \b requires a non-word character on both
# sides, and none of these five ever sits at a word boundary inside a real Spanish or English
# sentence this app renders. No allow-list was needed in practice (see the slice's ledger entry).
_LEAK_WORDS_RE = re.compile(r"\b(None|nan|NaN|inf|Traceback|undefined)\b")
_LEAK_LITERALS = ("{{", "}}", "{%", "[]", "{}")
_POLYLINE_RE = re.compile(r'<polyline\b[^>]*\bpoints="([^"]*)"')


def _leaks_in(text: str, is_html: bool) -> list[str]:
    """Word-boundary leak words apply to every response (a "NaN" could leak into the JSON export
    too, if a computed float ever went bad). The four punctuation literals and the polyline check
    are Jinja/SVG-leak signals and apply only to rendered HTML -- an empty list or dict is a
    perfectly ordinary JSON value on /privacidad/exportar, and "}}" is plain JavaScript syntax in
    /sw.js's own body, neither a leak."""
    found = [m.group(1) for m in _LEAK_WORDS_RE.finditer(text)]
    if not is_html:
        return found
    found += [lit for lit in _LEAK_LITERALS if lit in text]
    bad_polylines = [m.group(1) for m in _POLYLINE_RE.finditer(text)
                      if len([p for p in m.group(1).strip().split() if p]) < 2]
    if bad_polylines:
        found.append(f"polyline<2pts:{bad_polylines}")
    return found


def _reset_db(tc: unittest.TestCase):
    orig = config.DB_PATH
    tc.addCleanup(lambda: setattr(config, "DB_PATH", orig))
    tc.addCleanup(web.app.dependency_overrides.clear)
    config.DB_PATH = Path(tempfile.mkdtemp(dir=TMP)) / "vacios.db"
    conn = db.connect()
    tc.addCleanup(conn.close)
    db.ensure_ready(conn)
    return conn


def _mk_person(conn, lang: str, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO person (display_name, timezone, language) VALUES (?, 'America/Bogota', ?)", (name, lang))
    return cur.lastrowid


def _full_day(conn, person_id: int, day: str) -> None:
    for key in _FULL_METRIC_KEYS:
        db.set_metric(conn, day, key, _INVENTED[key], "garmin", person_id)
    conn.execute(
        "INSERT INTO sleep_session (person_id, day, start_local, end_local) VALUES (?, ?, ?, ?)",
        (person_id, day, f"{day}T23:05:00", f"{day}T06:50:00"))
    conn.execute(
        "INSERT INTO activity (person_id, source_id, day, start_local, type, name, duration_s, "
        "distance_m, avg_hr, max_hr, calories, source) VALUES (?, ?, ?, ?, 'walking', NULL, 1200, "
        "2000, 100, 115, 150, 'garmin')",
        (person_id, f"{day}-1", day, f"{day}T18:00:00"))


# ---- A: brand new -- a person row only ----------------------------------------------------------
def _seed_A(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "A")
    conn.commit()
    return {"person_id": pid, "days": [TODAY.isoformat()], "goal_ids": [], "turnos_on": False}


# ---- B: one day of data, today, every metric a watch can send -------------------------------
def _seed_B(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "B")
    day = TODAY.isoformat()
    _full_day(conn, pid, day)
    conn.commit()
    return {"person_id": pid, "days": [day], "goal_ids": [], "turnos_on": False}


# ---- C: 20 days with ONLY sleep -- a watch/setting that sends some metrics but not others -------
def _seed_C(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "C")
    days = [(TODAY - timedelta(19 - i)).isoformat() for i in range(20)]
    for i, day in enumerate(days):
        db.set_metric(conn, day, "sleep_minutes", 400 + i, "garmin", pid)
    conn.commit()
    return {"person_id": pid, "days": [days[0], days[-1]], "goal_ids": [], "turnos_on": False}


# ---- D: trabaja_turnos = yes, zero shift rows, zero metrics (the honest "sin_datos_reloj" path) -
def _seed_D(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "D")
    db.set_trabaja_turnos(conn, True, pid)
    conn.commit()
    return {"person_id": pid, "days": [TODAY.isoformat()], "goal_ids": [], "turnos_on": True}


# ---- D2: one night block, trabaja_turnos = yes, no watch data at all (before or after) -----------
def _seed_D2(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "D2")
    db.set_trabaja_turnos(conn, True, pid)
    start = TODAY - timedelta(6)
    for i in range(3):
        d = (start + timedelta(i)).isoformat()
        nd = (start + timedelta(i + 1)).isoformat()
        conn.execute(
            "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source) "
            "VALUES (?, ?, 'night', 'N', ?, ?, 'manual')",
            (pid, d, f"{d}T22:00:00", f"{nd}T06:00:00"))
    after_day = (start + timedelta(3)).isoformat()
    conn.commit()
    return {"person_id": pid, "days": [start.isoformat(), after_day], "goal_ids": [], "turnos_on": True}


# ---- E: an active metric goal on a metric with no data at all, a habit goal with zero checks ----
def _seed_E(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "E")
    today_iso = TODAY.isoformat()
    deadline = (TODAY + timedelta(60)).isoformat()
    gid_metric = goals.propose(conn, pid, "ZZE-METRIC", today_iso, kind="metric", metric_key="vo2max",
                               direction="raise", target_value=50.0, baseline_value=40.0, deadline=deadline)
    goals.activate(conn, gid_metric)
    gid_habit = goals.propose(conn, pid, "ZZE-HABIT-GOAL", today_iso, kind="milestone", deadline=deadline)
    goals.activate(conn, gid_habit)
    goals.add_habit(conn, gid_habit, "ZZE-HABIT", {"per": "week", "times": 1})
    conn.commit()
    return {"person_id": pid, "days": [today_iso], "goal_ids": [gid_metric, gid_habit], "turnos_on": False}


# ---- F: data only in the past -- the last day is 40 days ago, the watch stopped syncing ---------
def _seed_F(conn, lang: str) -> dict:
    pid = _mk_person(conn, lang, "F")
    last_day = TODAY - timedelta(40)
    for i in range(10):
        _full_day(conn, pid, (last_day - timedelta(9 - i)).isoformat())
    conn.commit()
    return {"person_id": pid, "days": [last_day.isoformat(), TODAY.isoformat()], "goal_ids": [], "turnos_on": False}


STATES = {"A": _seed_A, "B": _seed_B, "C": _seed_C, "D": _seed_D, "D2": _seed_D2, "E": _seed_E, "F": _seed_F}
LANGS = ("es", "en")


def _expected_statuses(path: str, turnos_on: bool) -> set:
    if path in _ALWAYS_404_LOCAL:
        return {404}
    if path in _TURNOS_GATED:
        return {200} if turnos_on else {404}
    return {200}


def _paths_for(route_path: str, ids: dict):
    if route_path in _SKIPPED_PARAM_ROUTES:
        return []
    if "{day}" in route_path:
        return [route_path.replace("{day}", d) for d in ids["days"]]
    if "{goal_id}" in route_path:
        return [route_path.replace("{goal_id}", str(g)) for g in ids["goal_ids"]]
    return [route_path]


class EstadosVaciosTest(unittest.TestCase):
    def setUp(self):
        self.conn = _reset_db(self)

    def test_every_sparse_state_sweeps_clean(self):
        get_routes = [r for r in rutas_planas(web.app.routes) if "GET" in getattr(r, "methods", ())]
        self.assertGreater(len(get_routes), 20)

        defects = []
        swept = 0
        home_results = []

        for state_key, seed_fn in STATES.items():
            for lang in LANGS:
                ids = seed_fn(self.conn, lang)
                person_id = ids["person_id"]
                web.app.dependency_overrides[identity.current_person_id] = (lambda pid=person_id: pid)
                for route in get_routes:
                    for path in _paths_for(route.path, ids):
                        swept += 1
                        label = f"{state_key}/{lang} GET {path}"
                        try:
                            r = client.get(path)
                        except Exception as e:
                            defects.append((label, "exception", f"{type(e).__name__}: {e}"))
                            continue
                        if path == "/":
                            home_results.append((state_key, lang, r.status_code))
                        expected = _expected_statuses(route.path, ids["turnos_on"])
                        if r.status_code == 500:
                            defects.append((label, "500", r.text[:300]))
                            continue
                        if r.status_code not in expected:
                            defects.append((label, "unexpected-status",
                                           f"got {r.status_code}, expected one of {expected}"))
                            continue
                        if r.status_code == 200:
                            is_html = "text/html" in r.headers.get("content-type", "")
                            leaks = _leaks_in(r.text, is_html)
                            if leaks:
                                defects.append((label, "leak", repr(leaks)))
                web.app.dependency_overrides.pop(identity.current_person_id, None)

        self.assertEqual(defects, [], "defect(s) found by the sparse-state sweep:\n" +
                         "\n".join(f"  {label}: {kind} -- {detail}" for label, kind, detail in defects))

        # Non-vacuous: every one of the 7 states x 2 languages reached "/" with 200.
        self.assertEqual(len(home_results), len(STATES) * len(LANGS))
        for state_key, lang, code in home_results:
            self.assertEqual(code, 200, f"{state_key}/{lang} GET / -> {code}")
        self.assertGreater(swept, 150, "sweep covered too few (route, state, language) calls to be a real guard")


if __name__ == "__main__":
    unittest.main()
