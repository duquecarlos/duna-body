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

from tests._db import open_conn
from tests._rutas import rutas_planas

from body import db, goals, web
from body.agente import ajustes, bucle, herramientas

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
    "/history/practica/{practica_id}": "leer_practicas",
    "/goals": "leer_metas",
    "/goals/new": None,          # a creation form, not a data screen
    "/goals/{goal_id}": "leer_meta",
    "/experiments/{experiment_id}": "leer_experimentos",
    "/shifts/patron": None,       # a creation form, not a data screen
    "/settings": "leer_ajustes",
    "/sw.js": None,               # the service worker script, not a data screen
    "/healthz": None,             # liveness check, no data, no tool
    "/login": None,               # sign-in plumbing, no data, no tool
    "/login/cuenta/start": None,  # sign-in plumbing, no data, no tool
    "/login/cuenta/callback": None,  # sign-in plumbing, no data, no tool
    "/privacidad/consentimiento": None,  # privacy plumbing; the assistant never reads or triggers it
    "/privacidad": None,          # privacy plumbing; the assistant never reads or triggers it
    "/privacidad/exportar": None,  # privacy plumbing; the assistant never reads or triggers it
    "/garmin": "leer_estado_reloj",  # BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 3: the sync state itself
    "/garmin/estado-vivo": None,  # BODY-SYNC-VIVO-2 ruling 3: the "Tu reloj" card's own poll fragment, not a tool
    "/ajustes/dispositivos": None,  # device-key management; the assistant never reads or triggers it
    "/ajustes/asistente": None,   # the setting itself; the assistant reads it through leer_ajustes, never sets it
    "/api/vida/semana": None,     # device-key API for Personal; the assistant never reads or triggers it
    "/api/hogar/manana": None,    # device-key API for Hogar; the assistant never reads or triggers it
    "/api/hogar/serie": None,     # device-key API for Hogar; the assistant never reads or triggers it
    "/api/hogar/turnos": None,    # device-key API for Hogar; the assistant never reads or triggers it
    "/api/hogar/herramientas": None,  # device-key API for Hogar's own writes; mirrors the registry's own list
    "/api/asistente/mcp/herramientas": None,  # hosted MCP door; mirrors leer_ajustes'/the registry's own list
    "/asistente/conversacion/{conversacion_id}": None,  # the panel's own history reload, not a tool
    "/ayuda": "leer_tutoriales",
    # No lectura tool returns this screen's own data: leer_entreno's day/range read is restricted to
    # strength_training/manual sessions (never a Garmin-automatic one like basketball), and leer_dia's
    # activities carry no sets, no habitual-comparison and no recovery line -- neither is this page's
    # equivalent for every session it serves (BODY-ENTRENO-VISTA-SESION-1-B).
    "/day/{day}/entreno/{source_id}": None,  # no assistant tool reads one session yet (BODY-ENTRENO-VISTA-SESION-2)
}

WRITER_NAMES = {
    "weekly_check", "propose", "activate", "pause", "finish", "drop", "add_habit", "check_habit",
    "confirm", "confirm_all", "dismiss", "set_language", "set_nivel_datos",
}
WRITE_SQL_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")
MAX_WALK_DEPTH = 8


def _conn(case: unittest.TestCase):
    return open_conn(case, Path(tempfile.mkdtemp(dir=TMP)) / "a.db")


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
    db.crear_experimento(
        conn, 1, "Ducha fría corta en turno de tarde", ["resting_hr"], {"tipo": "dia"},
        {"turno_en": None, "turno_fuera": None, "excluir_tras_noche": False}, fecha_inicio="2026-10-01",
    )
    conn.commit()
    return gid


def _all_tool_calls():
    gid_placeholder = {"goal_id": 1}  # overwritten per-test with the real seeded id where it matters
    return {
        "leer_hoy": {}, "leer_dia": {"day": "2026-10-05"}, "leer_como_calculo": {"day": "2026-10-05"},
        "leer_tendencias": {}, "leer_historia": {}, "leer_metas": {}, "leer_meta": gid_placeholder,
        "leer_turnos": {}, "leer_costo_turnos": {}, "leer_ajustes": {}, "leer_tutoriales": {},
        "leer_estado_reloj": {}, "leer_dias_libres": {"desde": "2026-10-01", "dias": 3},
        "leer_entreno": {"day": "2026-10-05"}, "leer_experimentos": {},
        "leer_serie": {"metricas": ["resting_hr"], "desde": "2026-10-01", "hasta": "2026-10-05", "paso": "dia"},
        "leer_intradia": {"fecha": "2026-10-05", "desde": "06:00", "hasta": "08:00"},
        "leer_noche": {"fecha": "2026-10-05"},
        "comparar_noche": {"fecha": "2026-10-05"},
        "buscar_noches": {},
        "leer_plan": gid_placeholder, "leer_planes": {}, "leer_practicas": {},
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


PROPUESTA_NOMBRES = {"proponer_meta", "proponer_cambio_meta", "proponer_confirmar_turnos", "proponer_idioma",
                     "planear_entreno", "registrar_entreno", "corregir_entreno", "registrar_medida",
                     "marcar_habito",
                     "proponer_experimento", "marcar_experimento",
                     "registrar_ingesta", "corregir_ingesta", "borrar_ingesta", "registrar_comida",
                     "corregir_comida",
                     "registrar_checkin", "marcar_periodo", "cerrar_periodo", "corregir_periodo",
                     "borrar_periodo", "descartar_series", "separar_sesion", "unir_sesiones",
                     "proponer_objetivo_nutriente",
                     "proponer_plan", "corregir_meta_plan", "ampliar_plan", "anotar_sesion",
                     "importar_desplazamiento", "proponer_practica", "corregir_despertar", "registrar_dolor",
                     "corregir_fase"}
# Privacy and account controls deliberately NEVER become proposal tools (BODY-ASISTENTE-PROPUESTAS-1
# approval reason): nivel_datos, consent, export, delete-all, device keys, Garmin connection,
# household membership all stay on their own screens.
NUNCA_PROPUESTA = {
    "set_nivel_datos", "record_consent", "export_person", "delete_person", "crear_dispositivo",
    "revocar_dispositivo", "conectar_garmin", "desconectar_garmin", "unir_hogar", "salir_hogar",
}


class RegistryShapeTest(unittest.TestCase):
    def test_seventeen_lectura_tools_with_expected_levels(self):
        # BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 3: extended from sixteen -- leer_estado_reloj joined
        # the registry at nivel 'ninguno' (it carries no health figure), never a loosened count.
        # BODY-TURNO-DIA-PLAN-1 ruling 2: extended from seventeen -- leer_dias_libres is the one
        # new entry, also at nivel 'ninguno' (shift times carry no health figure either).
        # BODY-HISTORIA-VIDA-2: extended from eighteen -- leer_practicas is the one new entry, at
        # nivel 'resumen' (counts and streaks, no daily figure).
        # BODY-DESPERTAR-REAL-2: extended from nineteen -- leer_intradia is the one new entry, at
        # nivel 'completo' (the same gate leer_dia itself enforces).
        # BODY-NOCHE-LECTURA-1: extended from twenty -- leer_noche is the one new entry, also at
        # nivel 'completo' (the same gate leer_intradia itself enforces).
        # BODY-MINUTO-ANALISIS-2: extended from twenty-one -- comparar_noche is the one new entry,
        # also at nivel 'completo' (it reads the same per-minute payloads leer_noche does).
        # BODY-MINUTO-ANALISIS-3: extended from twenty-two -- buscar_noches is the one new entry,
        # also at nivel 'completo' (it reads the same per-minute payloads comparar_noche does).
        expected_levels = {
            "leer_hoy": "completo", "leer_dia": "completo", "leer_como_calculo": "completo",
            "leer_tendencias": "resumen", "leer_historia": "resumen", "leer_metas": "resumen",
            "leer_meta": "resumen", "leer_turnos": "ninguno", "leer_costo_turnos": "resumen",
            "leer_ajustes": "ninguno", "leer_tutoriales": "ninguno", "leer_entreno": "completo",
            "leer_experimentos": "resumen", "leer_serie": "resumen", "leer_intradia": "completo",
            "leer_noche": "completo", "comparar_noche": "completo", "buscar_noches": "completo",
            "leer_plan": "resumen", "leer_planes": "resumen", "leer_estado_reloj": "ninguno",
            "leer_dias_libres": "ninguno", "leer_practicas": "resumen",
        }
        lectura = [h for h in herramientas.HERRAMIENTAS if h["tipo"] == "lectura"]
        self.assertEqual(len(lectura), 23)
        self.assertEqual({h["nombre"]: h["nivel"] for h in lectura}, expected_levels)

    def test_thirty_propuesta_tools_with_one_at_nivel_resumen(self):
        # BODY-HOGAR-ESCRIBE-1: extended from 27 -- anotar_sesion is the one new entry.
        # BODY-CORREGIR-MEDIDA-ENTRENO-1-W: extended from 28 -- corregir_entreno is the one new entry.
        # BODY-DESPLAZAMIENTO-ASISTENTE-1: extended from 29 -- importar_desplazamiento is the one new
        # entry, and the first `propuesta` tool not gated at 'ninguno': ruling 4 sets it at 'resumen',
        # the same nivel_datos the commute data it imports is itself shown at -- counts and dates
        # only, never a day's own figure.
        # BODY-HISTORIA-VIDA-2: extended from 30 -- proponer_practica is the one new entry, at nivel
        # 'ninguno' like every other propuesta tool except importar_desplazamiento.
        # BODY-DESPERTAR-REAL-4: extended from 31 -- corregir_despertar is the one new entry, also
        # at nivel 'ninguno'.
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: extended from 32 -- registrar_dolor is the one
        # new entry, also at nivel 'ninguno'.
        # BODY-PLAN-FASES-2 ruling 3: extended from 33 -- corregir_fase is the one new entry, also
        # at nivel 'ninguno'.
        propuesta = [h for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta"]
        self.assertEqual({h["nombre"] for h in propuesta}, PROPUESTA_NOMBRES)
        niveles = {h["nombre"]: h["nivel"] for h in propuesta}
        self.assertEqual(niveles.pop("importar_desplazamiento"), "resumen")
        self.assertTrue(all(nivel == "ninguno" for nivel in niveles.values()))
        self.assertFalse(PROPUESTA_NOMBRES & NUNCA_PROPUESTA)

    def test_every_entry_has_a_known_tipo(self):
        self.assertTrue(all(h["tipo"] in ("lectura", "propuesta") for h in herramientas.HERRAMIENTAS))
        # BODY-HOGAR-ESCRIBE-1: extended from 44 -- anotar_sesion is the one new entry.
        # BODY-CORREGIR-MEDIDA-ENTRENO-1-W: extended from 45 -- corregir_entreno is the one new entry.
        # BODY-TURNO-DIA-PLAN-1: extended from 46 -- leer_dias_libres is the one new entry.
        # BODY-DESPLAZAMIENTO-ASISTENTE-1: extended from 47 -- importar_desplazamiento is the one new entry.
        # BODY-HISTORIA-VIDA-2: extended from 48 -- leer_practicas and proponer_practica are the two new entries.
        # BODY-DESPERTAR-REAL-2: extended from 50 -- leer_intradia is the one new entry.
        # BODY-DESPERTAR-REAL-4: extended from 51 -- corregir_despertar is the one new entry.
        # BODY-NOCHE-LECTURA-1: extended from 52 -- leer_noche is the one new entry.
        # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 5: extended from 53 -- registrar_dolor is the one
        # new entry.
        # BODY-MINUTO-ANALISIS-2: extended from 54 -- comparar_noche is the one new entry.
        # BODY-MINUTO-ANALISIS-3: extended from 55 -- buscar_noches is the one new entry.
        # BODY-PLAN-FASES-2 ruling 3: extended from 56 -- corregir_fase is the one new entry.
        self.assertEqual(len(herramientas.HERRAMIENTAS), 57)

    def test_every_entry_carries_the_reserved_plan_field_unset(self):
        # BODY-ASISTENTE-CONFORMIDAD-1 ruling 2 (agent-capabilities-v1 section 5): the field
        # exists on every entry; nobody fills it, since Body has no plan tiers yet.
        self.assertTrue(all("plan_minimo" in h for h in herramientas.HERRAMIENTAS))
        self.assertTrue(all(h["plan_minimo"] is None for h in herramientas.HERRAMIENTAS))

    def test_reserved_plan_field_never_reaches_the_provider_tool_spec(self):
        for spec in bucle._tool_specs("completo"):
            self.assertEqual(set(spec), {"nombre", "descripcion", "parametros"})

    def test_a_plan_gated_entry_is_hidden_by_disponibles(self):
        nombre = herramientas.HERRAMIENTAS[0]["nombre"]
        original = herramientas._BY_NAME[nombre]["plan_minimo"]
        herramientas._BY_NAME[nombre]["plan_minimo"] = "pro"
        try:
            self.assertNotIn(nombre, {h["nombre"] for h in herramientas.disponibles("completo")})
        finally:
            herramientas._BY_NAME[nombre]["plan_minimo"] = original


class RouteMappingTest(unittest.TestCase):
    def test_every_get_route_has_a_mapping_decision(self):
        get_paths = {r.path for r in rutas_planas(web.app.routes) if isinstance(r, APIRoute) and "GET" in r.methods}
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
        conn = _conn(self)
        gid = _seed(conn)
        casos = _all_tool_calls()
        casos["leer_meta"] = {"goal_id": gid}
        casos["leer_plan"] = {"goal_id": gid}
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
        conn = _conn(self)
        _seed(conn)
        for nivel in ("resumen", "ninguno"):
            with self.assertRaises(herramientas.ToolError):
                herramientas.ejecutar(conn, 1, "leer_dia", {"day": "2026-10-05"}, nivel)

    def test_unknown_tool_refused(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "leer_no_existe", {}, "completo")

    def test_missing_required_param_refused(self):
        conn = _conn(self)
        _seed(conn)
        with self.assertRaises(herramientas.ToolError):
            herramientas.ejecutar(conn, 1, "leer_dia", {}, "completo")


class ResumenHasNoDailyValuesTest(unittest.TestCase):
    def test_leer_tendencias_reports_aggregates_only(self):
        conn = _conn(self)
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
        conn = _conn(self)
        _seed(conn)
        out = herramientas.ejecutar(conn, 1, "leer_historia", {}, "resumen")
        self.assertIsInstance(out["cobertura"], dict)
        self.assertNotIn("daily_metric", json.dumps(out))


class SinVerificacionTest(unittest.TestCase):
    def test_leer_metas_says_sin_verificacion_when_none_exists(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Meta sin verificar", "2026-10-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-01")
        goals.activate(conn, gid)
        out = herramientas.ejecutar(conn, 1, "leer_metas", {}, "resumen")
        self.assertEqual(out["metas"][0]["ultima_verificacion"], "sin verificación aún")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0], 0)

    def test_leer_meta_says_sin_verificacion_when_none_exists(self):
        conn = _conn(self)
        db.init(conn)
        gid = goals.propose(conn, 1, "Meta sin verificar", "2026-10-01", metric_key="steps", direction="raise",
                            target_value=9000.0, baseline_value=6000.0, deadline="2026-12-01")
        goals.activate(conn, gid)
        out = herramientas.ejecutar(conn, 1, "leer_meta", {"goal_id": gid}, "resumen")
        self.assertEqual(out["verificaciones"], "sin verificación aún")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM goal_check").fetchone()[0], 0)

    def test_leer_meta_unknown_goal(self):
        conn = _conn(self)
        db.init(conn)
        out = herramientas.ejecutar(conn, 1, "leer_meta", {"goal_id": 999}, "resumen")
        self.assertFalse(out["encontrada"])


class NivelDatosTest(unittest.TestCase):
    def test_default_completo_with_no_row(self):
        conn = _conn(self)
        db.init(conn)
        self.assertEqual(ajustes.nivel_datos(conn, 1), "completo")

    def test_set_get_round_trip(self):
        conn = _conn(self)
        db.init(conn)
        ajustes.set_nivel_datos(conn, 1, "resumen")
        self.assertEqual(ajustes.nivel_datos(conn, 1), "resumen")
        ajustes.set_nivel_datos(conn, 1, "ninguno")
        self.assertEqual(ajustes.nivel_datos(conn, 1), "ninguno")

    def test_invalid_level_refused_by_the_function(self):
        conn = _conn(self)
        db.init(conn)
        with self.assertRaises(ValueError):
            ajustes.set_nivel_datos(conn, 1, "parcial")

    def test_invalid_level_refused_by_the_check_constraint(self):
        conn = _conn(self)
        db.init(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ajuste_agente (person_id, nivel_datos, updated_at) VALUES (1, 'parcial', '2026-01-01')"
            )


class JsonSerializableTest(unittest.TestCase):
    def test_every_lectura_tool_output_survives_json_dumps(self):
        conn = _conn(self)
        gid = _seed(conn)
        casos = _all_tool_calls()
        casos["leer_meta"] = {"goal_id": gid}
        casos["leer_plan"] = {"goal_id": gid}
        lectura_nombres = {h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "lectura"}
        self.assertEqual(set(casos), lectura_nombres)
        for nombre, params in casos.items():
            out = herramientas.ejecutar(conn, 1, nombre, params, "completo")
            json.dumps(out)


if __name__ == "__main__":
    unittest.main()
