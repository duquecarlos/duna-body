"""Stage one of Body's assistant (wave 7): nivel_datos, the static lectura registry and its guards."""
import ast
import inspect
import json
import sqlite3
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from fastapi.routing import APIRoute

from body import db, goals, web
from body.agente import ajustes, herramientas

# Every GET route in body/web.py, mapped to the tool that answers the same question, or to None with a
# reason it needs no tool. A new GET route with no entry here fails the test below on purpose.
ROUTE_TOOL = {
    "/": "leer_hoy",
    "/how/{day}": "leer_como_calculo",
    "/day/{day}": "leer_dia",
    "/shifts": ("leer_turnos", "leer_costo_turnos"),
    "/shifts/como": "leer_costo_turnos",
    "/trends": "leer_tendencias",
    "/history": "leer_historia",
    "/goals": "leer_metas",
    "/goals/new": None,          # a creation form, not a data screen
    "/goals/{goal_id}": "leer_meta",
    "/settings": "leer_ajustes",
    "/sw.js": None,               # the service worker script, not a data screen
    "/healthz": None,             # liveness check, no data, no tool
    "/login": None,               # sign-in plumbing, no data, no tool
    "/login/cuenta/start": None,  # sign-in plumbing, no data, no tool
    "/login/cuenta/callback": None,  # sign-in plumbing, no data, no tool
    "/privacidad/consentimiento": None,  # privacy plumbing; the assistant never reads or triggers it
    "/privacidad": None,          # privacy plumbing; the assistant never reads or triggers it
    "/privacidad/exportar": None,  # privacy plumbing; the assistant never reads or triggers it
    "/garmin": None,              # account-connection plumbing; the assistant never reads or triggers it
    "/ajustes/dispositivos": None,  # device-key management; the assistant never reads or triggers it
    "/ajustes/asistente": None,   # the setting itself; the assistant reads it through leer_ajustes, never sets it
    "/api/vida/semana": None,     # device-key API for Personal; the assistant never reads or triggers it
    "/asistente/conversacion/{conversacion_id}": None,  # the panel's own history reload, not a tool
    "/ayuda": "leer_tutoriales",
}

WRITER_NAMES = {
    "weekly_check", "propose", "activate", "pause", "finish", "drop", "add_habit", "check_habit",
    "confirm", "confirm_all", "dismiss", "set_language", "set_nivel_datos",
}
WRITE_SQL_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")
MAX_WALK_DEPTH = 8


def _conn():
    return db.connect(Path(tempfile.mkdtemp(dir=TMP)) / "a.db")


def _seed(conn) -> int:
    db.init(conn)
    for i in range(20):
        day = f"2026-10-{i + 1:02d}"
        db.set_metric(conn, day, "hrv_last_night", 55 + i % 5, "garmin", 1)
        db.set_metric(conn, day, "sleep_minutes", 400 + i, "garmin", 1)
        db.set_metric(conn, day, "resting_hr", 58 - i % 3, "garmin", 1)
        db.set_metric(conn, day, "body_battery_wake", 70 + i % 10, "garmin", 1)
        db.set_metric(conn, day, "stress_avg", 20 + i % 5, "garmin", 1)
        db.set_metric(conn, day, "steps", 7000 + i * 50, "garmin", 1)
    conn.execute(
        "INSERT INTO shift (person_id, day, kind, start_local, source) VALUES "
        "(1, '2026-10-05', 'night', '2026-10-05T22:00:00', 'manual')"
    )
    gid = goals.propose(conn, 1, "Bajar FC en reposo", "2026-10-01", metric_key="resting_hr",
                        direction="lower", target_value=50.0, baseline_value=60.0, deadline="2026-12-01")
    goals.activate(conn, gid)
    goals.add_habit(conn, gid, "Dormir temprano antes de turno de noche", {"per": "week", "times": 3})
    conn.commit()
    return gid


def _all_tool_calls():
    gid_placeholder = {"goal_id": 1}  # overwritten per-test with the real seeded id where it matters
    return {
        "leer_hoy": {}, "leer_dia": {"day": "2026-10-05"}, "leer_como_calculo": {"day": "2026-10-05"},
        "leer_tendencias": {}, "leer_historia": {}, "leer_metas": {}, "leer_meta": gid_placeholder,
        "leer_turnos": {}, "leer_costo_turnos": {}, "leer_ajustes": {}, "leer_tutoriales": {},
    }


# --- STATIC guard: walk each lectura tool's call graph inside body/ and refuse a known writer. ---

def _is_write_sql_call(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute) and call.func.attr in ("execute", "executemany") and call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            verb = first.value.strip().split(None, 1)[0].upper() if first.value.strip() else ""
            if verb in WRITE_SQL_PREFIXES:
                return verb
    return None


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _resolve_call_target(call: ast.Call, glb: dict):
    f = call.func
    if isinstance(f, ast.Name):
        return glb.get(f.id)
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        mod = glb.get(f.value.id)
        return getattr(mod, f.attr, None) if mod is not None else None
    return None


def _walk_for_writers(func, seen: set, depth: int, found: list) -> None:
    if depth > MAX_WALK_DEPTH or func in seen:
        return
    seen.add(func)
    try:
        src = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return
    tree = ast.parse(src)
    glb = getattr(func, "__globals__", {})
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        verb = _is_write_sql_call(node)
        if verb:
            found.append(f"{func.__qualname__}: {verb} SQL literal")
            continue
        name = _call_name(node)
        if name in WRITER_NAMES:
            found.append(f"{func.__qualname__}: calls {name}()")
            continue
        target = _resolve_call_target(node, glb)
        if inspect.isfunction(target) and (getattr(target, "__module__", "") or "").startswith("body"):
            _walk_for_writers(target, seen, depth + 1, found)


PROPUESTA_NOMBRES = {"proponer_meta", "proponer_cambio_meta", "proponer_confirmar_turnos", "proponer_idioma"}
# Privacy and account controls deliberately NEVER become proposal tools (BODY-ASISTENTE-PROPUESTAS-1
# approval reason): nivel_datos, consent, export, delete-all, device keys, Garmin connection,
# household membership all stay on their own screens.
NUNCA_PROPUESTA = {
    "set_nivel_datos", "record_consent", "export_person", "delete_person", "crear_dispositivo",
    "revocar_dispositivo", "conectar_garmin", "desconectar_garmin", "unir_hogar", "salir_hogar",
}


class RegistryShapeTest(unittest.TestCase):
    def test_ten_lectura_tools_with_expected_levels(self):
        expected_levels = {
            "leer_hoy": "completo", "leer_dia": "completo", "leer_como_calculo": "completo",
            "leer_tendencias": "resumen", "leer_historia": "resumen", "leer_metas": "resumen",
            "leer_meta": "resumen", "leer_turnos": "ninguno", "leer_costo_turnos": "resumen",
            "leer_ajustes": "ninguno", "leer_tutoriales": "ninguno",
        }
        lectura = [h for h in herramientas.HERRAMIENTAS if h["tipo"] == "lectura"]
        self.assertEqual(len(lectura), 11)
        self.assertEqual({h["nombre"]: h["nivel"] for h in lectura}, expected_levels)

    def test_four_propuesta_tools_at_nivel_ninguno(self):
        propuesta = [h for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta"]
        self.assertEqual({h["nombre"] for h in propuesta}, PROPUESTA_NOMBRES)
        self.assertTrue(all(h["nivel"] == "ninguno" for h in propuesta))
        self.assertFalse(PROPUESTA_NOMBRES & NUNCA_PROPUESTA)

    def test_every_entry_has_a_known_tipo(self):
        self.assertTrue(all(h["tipo"] in ("lectura", "propuesta") for h in herramientas.HERRAMIENTAS))
        self.assertEqual(len(herramientas.HERRAMIENTAS), 15)


class RouteMappingTest(unittest.TestCase):
    def test_every_get_route_has_a_mapping_decision(self):
        get_paths = {r.path for r in web.app.routes if isinstance(r, APIRoute) and "GET" in r.methods}
        self.assertTrue(get_paths)
        self.assertEqual(get_paths, set(ROUTE_TOOL),
                         "a GET route appeared or disappeared with no tool-mapping decision")
        for path, entry in ROUTE_TOOL.items():
            tool_names = entry if isinstance(entry, tuple) else (entry,) if entry is not None else ()
            for tool_name in tool_names:
                self.assertIn(tool_name, herramientas._BY_NAME, f"{path} -> unknown tool {tool_name!r}")


class StaticGuardTest(unittest.TestCase):
    def test_no_lectura_tool_reaches_a_writer(self):
        for h in herramientas.HERRAMIENTAS:
            if h["tipo"] != "lectura":
                continue
            found: list[str] = []
            _walk_for_writers(h["funcion"], set(), 0, found)
            self.assertEqual(found, [], f"{h['nombre']} reaches a writer: {found}")

    def test_no_lectura_tool_imports_propuestas(self):
        # Decision 1: only a `propuesta` tool may import body/agente/propuestas.py. A `lectura`
        # tool's source never even names the module.
        for h in herramientas.HERRAMIENTAS:
            if h["tipo"] != "lectura":
                continue
            src = textwrap.dedent(inspect.getsource(h["funcion"]))
            self.assertNotIn("propuestas", src, f"{h['nombre']} (lectura) references propuestas")

    def test_every_propuesta_tool_creates_a_record_through_propuestas(self):
        for h in herramientas.HERRAMIENTAS:
            if h["tipo"] != "propuesta":
                continue
            src = textwrap.dedent(inspect.getsource(h["funcion"]))
            self.assertIn("propuestas.crear", src, f"{h['nombre']} (propuesta) never calls propuestas.crear")


class BehaviouralGuardTest(unittest.TestCase):
    def test_running_every_lectura_tool_does_not_change_the_database(self):
        conn = _conn()
        gid = _seed(conn)
        casos = _all_tool_calls()
        casos["leer_meta"] = {"goal_id": gid}
        for nombre, params in casos.items():
            before = conn.total_changes
            herramientas.ejecutar(conn, 1, nombre, params, "completo")
            self.assertEqual(conn.total_changes, before, f"{nombre} changed the database")


class DisponiblesTest(unittest.TestCase):
    def test_completo_allows_every_tool(self):
        names = {h["nombre"] for h in herramientas.disponibles("completo")}
        self.assertEqual(names, {h["nombre"] for h in herramientas.HERRAMIENTAS})

    def test_resumen_allows_resumen_and_ninguno(self):
        names = {h["nombre"] for h in herramientas.disponibles("resumen")}
        expected = {h["nombre"] for h in herramientas.HERRAMIENTAS if h["nivel"] in ("resumen", "ninguno")}
        self.assertEqual(names, expected)

    def test_ninguno_allows_only_ninguno(self):
        names = {h["nombre"] for h in herramientas.disponibles("ninguno")}
        expected = {h["nombre"] for h in herramientas.HERRAMIENTAS if h["nivel"] == "ninguno"}
        self.assertEqual(names, expected)


class EjecutarGuardTest(unittest.TestCase):
    def test_leer_dia_refused_at_resumen_and_ninguno(self):
        conn = _conn()
        _seed(conn)
        for nivel in ("resumen", "ninguno"):
            with self.assertRaises(herramientas.ToolError):
                herramientas.ejecutar(conn, 1, "leer_dia", {"day": "2026-10-05"}, nivel)

    def test_unknown_tool_refused(self):
        conn = _conn()
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "leer_no_existe", {}, "completo")

    def test_missing_required_param_refused(self):
        conn = _conn()
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "leer_dia", {}, "completo")


class ResumenHasNoDailyValuesTest(unittest.TestCase):
    def test_leer_tendencias_reports_aggregates_only(self):
        conn = _conn()
        _seed(conn)
        out = herramientas.ejecutar(conn, 1, "leer_tendencias", {"rango": "30"}, "resumen")
        self.assertTrue(out["metricas"])
        for metrica in out["metricas"].values():
            self.assertNotIn("values", metrica)
            self.assertNotIn("valores", metrica)
            for campo in ("media_historica", "p25", "p75", "media_30d", "media_30d_previa",
                         "media_rango", "media_rango_previo"):
                if campo in metrica:
                    self.assertIsInstance(metrica[campo], (int, float, type(None)),
                                          f"{campo} is not an aggregate: {metrica[campo]!r}")

    def test_leer_historia_reports_aggregates_only(self):
        conn = _conn()
        _seed(conn)
        out = herramientas.ejecutar(conn, 1, "leer_historia", {}, "resumen")
        self.assertIsInstance(out["cobertura"], dict)
        self.assertNotIn("daily_metric", json.dumps(out))


class SinVerificacionTest(unittest.TestCase):
    def test_leer_metas_says_sin_verificacion_when_none_exists(self):
        conn = _conn()
        db.init(conn)
        gid = goals.propose(conn, 1, "Meta sin verificar", "2026-10-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-01")
        goals.activate(conn, gid)
        out = herramientas.ejecutar(conn, 1, "leer_metas", {}, "resumen")
        self.assertEqual(out["metas"][0]["ultima_verificacion"], "sin verificación aún")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0], 0)

    def test_leer_meta_says_sin_verificacion_when_none_exists(self):
        conn = _conn()
        db.init(conn)
        gid = goals.propose(conn, 1, "Meta sin verificar", "2026-10-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-01")
        goals.activate(conn, gid)
        out = herramientas.ejecutar(conn, 1, "leer_meta", {"goal_id": gid}, "resumen")
        self.assertEqual(out["verificaciones"], "sin verificación aún")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0], 0)

    def test_leer_meta_unknown_goal(self):
        conn = _conn()
        db.init(conn)
        out = herramientas.ejecutar(conn, 1, "leer_meta", {"goal_id": 999}, "resumen")
        self.assertFalse(out["encontrada"])


class NivelDatosTest(unittest.TestCase):
    def test_default_completo_with_no_row(self):
        conn = _conn()
        db.init(conn)
        self.assertEqual(ajustes.nivel_datos(conn, 1), "completo")

    def test_set_get_round_trip(self):
        conn = _conn()
        db.init(conn)
        ajustes.set_nivel_datos(conn, 1, "resumen")
        self.assertEqual(ajustes.nivel_datos(conn, 1), "resumen")
        ajustes.set_nivel_datos(conn, 1, "ninguno")
        self.assertEqual(ajustes.nivel_datos(conn, 1), "ninguno")

    def test_invalid_level_refused_by_the_function(self):
        conn = _conn()
        db.init(conn)
        with self.assertRaises(ValueError):
            ajustes.set_nivel_datos(conn, 1, "parcial")

    def test_invalid_level_refused_by_the_check_constraint(self):
        conn = _conn()
        db.init(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ajuste_agente (person_id, nivel_datos, updated_at) VALUES (1, 'parcial', '2026-01-01')"
            )


class JsonSerializableTest(unittest.TestCase):
    def test_every_lectura_tool_output_survives_json_dumps(self):
        conn = _conn()
        gid = _seed(conn)
        casos = _all_tool_calls()
        casos["leer_meta"] = {"goal_id": gid}
        lectura_nombres = {h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "lectura"}
        self.assertEqual(set(casos), lectura_nombres)
        for nombre, params in casos.items():
            out = herramientas.ejecutar(conn, 1, nombre, params, "completo")
            json.dumps(out)


if __name__ == "__main__":
    unittest.main()
