"""BODY-ESTADOS-VACIOS-3: the hosted-only sweep. BODY-ESTADOS-VACIOS-1 (ledger 0776687) swept
every GET route as six sparse people, but only in LOCAL mode -- every hosted-only route (`/garmin`,
`/ajustes/dispositivos`, `/login`, `/login/cuenta/start`, `/login/cuenta/callback`,
`/privacidad/consentimiento`, `/api/vida/semana`) was a documented 404 there. Now that a hosted
Body (body.duna.solutions) is the next deploy, this module sweeps those same sparse states --
reused directly from tests/test_estados_vacios.py's seed builders, now parameterised by an
existing `pid` -- inside a real Cuenta Duna session, built the same way tests/test_cuenta_rp.py's
CuentaRpTestCase does.

That is the FAITHFUL mechanism, not a dependency override: identity.current_person_id is where
the consent gate (body/identity.py's ConsentRequired) lives, and H1 below exists precisely to
exercise that gate. Overriding `identity.current_person_id` with
`app.dependency_overrides` -- the mechanism tests/test_estados_vacios.py uses for its LOCAL
sweep, and perfectly faithful there since local mode never gates on consent at all -- would
replace that function outright and so could never observe its own gate refusing an unconsented
person; it would also never exercise `cuenta.resolve_session`'s session-cookie read, so an
unsigned request could never be told apart from a signed one either. A real cookie-carrying
TestClient per person, signed in through the fake issuer, is the only way to see what
identity.current_person_id itself decides.

Person 1 is populated in the same database with distinctive invented marker strings (never a
real figure) -- a leak of their data into another person's page is then a plain substring check.
Two states only hosted mode has: H1 (signed in, consent NOT accepted) and H2 (signed in,
consented, Garmin never connected). Invented demo data only, no real health figures, no network.
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.testclient import TestClient

from body import db, goals, privacidad, web
from tests._rutas import rutas_planas
from tests.test_cuenta_rp import CuentaRpTestCase
from tests.test_estados_vacios import _ALWAYS_404_LOCAL, _TURNOS_GATED, STATES, TODAY, _leaks_in

PERSON1_NAME = "ZZ1-PERSONA-UNO-MARCADOR"
PERSON1_GOAL_TITLE = "ZZ1-META-UNO-MARCADOR"
PERSON1_HABIT_NAME = "ZZ1-HABITO-UNO-MARCADOR"
PERSON1_MARKERS = (PERSON1_NAME, PERSON1_GOAL_TITLE, PERSON1_HABIT_NAME)

LANGS = ("es", "en")

# Needs a seeded conversation (body/agente, outside this slice's touches) -- same exclusion as
# tests/test_estados_vacios.py's own sweep.
_SKIPPED_PARAM_ROUTES = {"/asistente/conversacion/{conversacion_id}"}

# Routes whose documented status never depends on who -- or whether anyone -- is signed in.
_NO_IDENTITY_200 = {"/healthz", "/sw.js"}
_NO_IDENTITY_303 = {"/login", "/login/cuenta/start"}
_CALLBACK_400 = {"/login/cuenta/callback"}
_DEVICE_GATED_401 = {"/api/vida/semana"}
_CONSENT_EXEMPT = {"/privacidad/consentimiento"}

# Of the seven hosted-only routes, only these three can ever answer 200 -- the other four's own
# documented, correct behaviour is a redirect (/login, /login/cuenta/start) or a refusal
# (/login/cuenta/callback with no real flow, /api/vida/semana with no device key). See this
# module's own ledger entry for the deviation this records against the spec's literal wording.
_HOSTED_ONLY_CAN_200 = {"/garmin", "/ajustes/dispositivos", "/privacidad/consentimiento"}


def _expected_hosted(path: str, *, signed_in: bool, consented: bool, turnos_on: bool) -> set:
    if path in _NO_IDENTITY_200:
        return {200}
    if path in _NO_IDENTITY_303:
        return {303}
    if path in _CALLBACK_400:
        return {400}
    if path in _DEVICE_GATED_401:
        return {401}
    if path in _CONSENT_EXEMPT:
        return {200} if signed_in else {303}
    if not signed_in:
        return {303}
    if not consented:
        return {303}
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


def _seed_persona_uno(conn) -> None:
    """Person 1 already exists (body/db.py's init() -- 'Me'). Gives it a distinctive display
    name, goal title and habit name: never shared with any other person this module signs in, so
    none of the three should ever appear on another person's page."""
    conn.execute("UPDATE person SET display_name = ? WHERE id = 1", (PERSON1_NAME,))
    today_iso = TODAY.isoformat()
    deadline = (TODAY + timedelta(60)).isoformat()
    gid = goals.propose(conn, 1, PERSON1_GOAL_TITLE, today_iso, kind="milestone", deadline=deadline)
    goals.activate(conn, gid)
    goals.add_habit(conn, gid, PERSON1_HABIT_NAME, {"per": "week", "times": 1})
    conn.commit()


class HostedEstadosVaciosSweepTest(CuentaRpTestCase):
    """Reuses CuentaRpTestCase's fresh temp db, fake issuer and hosted env (the exact mechanism
    tests/test_garmin_host.py's GarminHostedTestCase and tests/test_hogar_cuenta_1.py also build
    on) -- never a second mechanism of its own."""

    def setUp(self):
        super().setUp()
        self._sub_n = 0
        conn = db.connect()
        try:
            _seed_persona_uno(conn)
        finally:
            conn.close()

    def _next_sub(self) -> str:
        self._sub_n += 1
        return f"99999999-0000-0000-0000-{self._sub_n:012d}"

    def _sign_in_fresh(self) -> tuple[TestClient, int]:
        """A brand-new TestClient (its own cookie jar) signed in as a brand-new sub, so each
        person in this sweep is genuinely a different session, exactly as a hosted deploy would
        see two different people -- never the same client re-signing in."""
        client = TestClient(web.app, follow_redirects=False)
        sub = self._next_sub()
        auth = self._authorize_params(client.get("/login"))
        self.issuer.next_id_token = self.issuer.id_token(nonce=auth["nonce"], sub=sub)
        client.get("/login/cuenta/callback", params={"code": f"c-{sub}", "state": auth["state"]})
        conn = db.connect()
        try:
            pid = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (sub,)).fetchone()["id"]
        finally:
            conn.close()
        return client, pid

    def test_hosted_sweep_every_state_every_language(self):
        get_routes = [r for r in rutas_planas(web.app.routes) if "GET" in getattr(r, "methods", ())]
        self.assertGreater(len(get_routes), 20)

        defects = []
        swept = 0
        home_results = []
        hosted_only_statuses: dict[str, set] = {r: set() for r in _ALWAYS_404_LOCAL}

        def _sweep_as(client: TestClient, label_prefix: str, ids: dict, *, signed_in: bool, consented: bool):
            nonlocal swept
            for route in get_routes:
                for path in _paths_for(route.path, ids):
                    swept += 1
                    label = f"{label_prefix} GET {path}"
                    try:
                        r = client.get(path)
                    except Exception as e:
                        defects.append((label, "exception", f"{type(e).__name__}: {e}"))
                        continue
                    if path == "/":
                        home_results.append((label_prefix, r.status_code))
                    if path in hosted_only_statuses:
                        hosted_only_statuses[path].add(r.status_code)
                    expected = _expected_hosted(
                        route.path, signed_in=signed_in, consented=consented, turnos_on=ids["turnos_on"]
                    )
                    if r.status_code == 500:
                        defects.append((label, "500", r.text[:300]))
                        continue
                    if r.status_code not in expected:
                        defects.append((label, "unexpected-status",
                                        f"got {r.status_code}, expected one of {expected}"))
                        continue
                    for marker in PERSON1_MARKERS:
                        if marker in r.text:
                            defects.append((label, "cross-person-leak", f"person 1 marker {marker!r} found"))
                    if r.status_code == 200:
                        is_html = "text/html" in r.headers.get("content-type", "")
                        leaks = _leaks_in(r.text, is_html)
                        if leaks:
                            defects.append((label, "leak", repr(leaks)))

        # ---- A, B, C, D, D2, E, F: reused, signed in, consented, id != 1 -------------------------
        for state_key, seed_fn in STATES.items():
            for lang in LANGS:
                client, pid = self._sign_in_fresh()
                conn = db.connect()
                try:
                    ids = seed_fn(conn, lang, pid=pid)
                    privacidad.record_consent(conn, pid)
                finally:
                    conn.close()
                _sweep_as(client, f"{state_key}/{lang}", ids, signed_in=True, consented=True)

        # ---- H1: signed in, consent NOT accepted -------------------------------------------------
        for lang in LANGS:
            client, pid = self._sign_in_fresh()
            conn = db.connect()
            try:
                conn.execute("UPDATE person SET language = ? WHERE id = ?", (lang, pid))
                conn.commit()
            finally:
                conn.close()
            ids = {"person_id": pid, "days": [TODAY.isoformat()], "goal_ids": [], "turnos_on": False}
            _sweep_as(client, f"H1/{lang}", ids, signed_in=True, consented=False)

        # ---- H2: signed in, consented, Garmin never connected -------------------------------------
        for lang in LANGS:
            client, pid = self._sign_in_fresh()
            conn = db.connect()
            try:
                conn.execute("UPDATE person SET language = ? WHERE id = ?", (lang, pid))
                privacidad.record_consent(conn, pid)
                conn.commit()
            finally:
                conn.close()
            ids = {"person_id": pid, "days": [TODAY.isoformat()], "goal_ids": [], "turnos_on": False}
            _sweep_as(client, f"H2/{lang}", ids, signed_in=True, consented=True)

        # ---- unsigned: every person route redirects to sign-in or answers its documented status --
        anon = TestClient(web.app, follow_redirects=False)
        anon_ids = {"person_id": None, "days": [TODAY.isoformat()], "goal_ids": ["1"], "turnos_on": False}
        _sweep_as(anon, "anon", anon_ids, signed_in=False, consented=False)

        self.assertEqual(defects, [], "defect(s) found by the hosted sweep:\n" +
                         "\n".join(f"  {label}: {kind} -- {detail}" for label, kind, detail in defects))

        # Non-vacuous: every signed-in state/language reached "/" with 200.
        self.assertEqual(len(home_results), (len(STATES) + 2) * len(LANGS) + 1)  # +2 = H1, H2; +1 = anon
        for label_prefix, code in home_results:
            if label_prefix == "H1/es" or label_prefix == "H1/en" or label_prefix == "anon":
                self.assertEqual(code, 303, f"{label_prefix} GET / -> {code}")
            else:
                self.assertEqual(code, 200, f"{label_prefix} GET / -> {code}")
        self.assertGreater(swept, 300, "hosted sweep covered too few (route, state, language) calls")

        # Every hosted-only route was actually reached (not silently skipped), and the three that
        # CAN answer 200 did so for at least one person.
        for route in _ALWAYS_404_LOCAL:
            self.assertTrue(hosted_only_statuses[route], f"{route} never exercised by the hosted sweep")
            self.assertNotIn(404, hosted_only_statuses[route], f"{route} still answered 404 in hosted mode")
            if route in _HOSTED_ONLY_CAN_200:
                self.assertIn(200, hosted_only_statuses[route], f"{route} never answered 200 for any person")


if __name__ == "__main__":
    unittest.main()
