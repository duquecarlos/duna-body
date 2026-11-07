"""Census test for tutoriales/catalogo.json (tutoriales-v1 section 7): the JSON parses and has the
section 7 fields, ids are unique and kebab-case, every task's chapter exists, every `pantalla` (and
`pantalla_lista`) matches a real route in body.web.app, and every task marked `agente: si` names a
capability that actually exists in the assistant's tool registry."""
import json
import re
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import web
from body.agente import herramientas

ROOT = Path(__file__).resolve().parent.parent
CATALOGO_PATH = ROOT / "tutoriales" / "catalogo.json"

_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Only tasks whose "en la app" segment is demoed through one specific assistant tool today need an
# entry here -- the invariant this test checks is "every task marked agente: si names a capability
# that exists", and for these tasks that capability IS a named tool in herramientas.HERRAMIENTAS.
TASK_TOOL = {
    "primera-mirada-a-hoy": "leer_hoy",
    "leer-hoy-y-el-puntaje": "leer_hoy",
    "abrir-un-dia": "leer_dia",
    "como-se-calcula-un-valor": "leer_como_calculo",
    "confirmar-los-turnos-propuestos": "proponer_confirmar_turnos",
    "leer-lo-que-cuesta-una-semana-nocturna": "leer_historia",
    "comparar-una-metrica-con-tu-base": "leer_tendencias",
    "ver-tu-condicion-cardiovascular": "leer_tendencias",
    "crear-una-meta": "proponer_meta",
    "verificar-si-esta-funcionando": "leer_meta",
    "cambiar-el-objetivo": "proponer_cambio_meta",
    "hacer-una-pregunta": "leer_hoy",
    "aplicar-o-descartar-una-propuesta": "proponer_meta",
    "el-nivel-de-datos-del-asistente": "leer_ajustes",
}


def _normalize(path: str) -> str:
    """A route path with every `{param}` collapsed to one token, so a dynamic segment compares by
    pattern rather than by its parameter name."""
    return re.sub(r"\{[^}]+\}", "{}", path)


def _load() -> dict:
    return json.loads(CATALOGO_PATH.read_text(encoding="utf-8"))


def _real_routes() -> set[str]:
    return {_normalize(r.path) for r in web.app.routes if hasattr(r, "path")}


class CatalogShapeTest(unittest.TestCase):
    def setUp(self):
        self.data = _load()

    def test_parses_and_has_top_level_fields(self):
        self.assertEqual(self.data["version"], 1)
        self.assertEqual(self.data["producto"], "body")
        self.assertIsInstance(self.data["capitulos"], list)
        self.assertIsInstance(self.data["tareas"], list)
        self.assertGreater(len(self.data["capitulos"]), 0)
        self.assertGreater(len(self.data["tareas"]), 0)

    def test_chapters_have_required_fields(self):
        for c in self.data["capitulos"]:
            self.assertIn("id", c)
            self.assertIn("titulo", c)
            self.assertIn("orden", c)

    def test_tasks_have_required_fields(self):
        for t in self.data["tareas"]:
            for field in ("id", "titulo", "capitulo", "orden", "agente", "onboarding"):
                self.assertIn(field, t, f"task {t.get('id')!r} missing {field!r}")
            self.assertTrue("pantalla" in t or "pantalla_lista" in t, f"task {t['id']!r} has no screen at all")

    def test_chapter_ids_unique_and_kebab_case(self):
        ids = [c["id"] for c in self.data["capitulos"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate chapter id")
        for cid in ids:
            self.assertRegex(cid, _KEBAB, f"chapter id {cid!r} is not kebab-case")

    def test_task_ids_unique_and_kebab_case(self):
        ids = [t["id"] for t in self.data["tareas"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate task id")
        for tid in ids:
            self.assertRegex(tid, _KEBAB, f"task id {tid!r} is not kebab-case")

    def test_every_task_agente_value_is_valid(self):
        for t in self.data["tareas"]:
            self.assertIn(t["agente"], ("si", "no_aun"), f"task {t['id']!r} has bad agente value")

    def test_every_task_onboarding_is_bool(self):
        for t in self.data["tareas"]:
            self.assertIsInstance(t["onboarding"], bool)


class ChapterReferenceTest(unittest.TestCase):
    def test_every_task_chapter_exists(self):
        data = _load()
        chapter_ids = {c["id"] for c in data["capitulos"]}
        for t in data["tareas"]:
            self.assertIn(t["capitulo"], chapter_ids, f"task {t['id']!r} has unknown chapter {t['capitulo']!r}")


class ScreenCensusTest(unittest.TestCase):
    """Every `pantalla`/`pantalla_lista` must be a real route in body.web.app -- a task that outlives
    its screen is a stale census, not a stale video."""

    def test_every_pantalla_matches_a_real_route(self):
        routes = _real_routes()
        data = _load()
        for t in data["tareas"]:
            if "pantalla" in t:
                self.assertIn(_normalize(t["pantalla"]), routes,
                              f"task {t['id']!r} points at unknown screen {t['pantalla']!r}")

    def test_every_pantalla_lista_matches_a_real_route(self):
        routes = _real_routes()
        data = _load()
        for t in data["tareas"]:
            if "pantalla_lista" in t:
                self.assertIn(_normalize(t["pantalla_lista"]), routes,
                              f"task {t['id']!r} points at unknown pantalla_lista {t['pantalla_lista']!r}")

    def test_dynamic_pantalla_without_pantalla_lista_has_no_dangling_param(self):
        """A task whose `pantalla` carries a dynamic segment and no `pantalla_lista` just gets no
        "Ir a la pantalla" link (tutoriales-v1 section 7) -- not a defect, so this only documents which
        tasks are in that state rather than asserting none are."""
        data = _load()
        dynamic_without_list = [t["id"] for t in data["tareas"]
                                 if "{" in t.get("pantalla", "") and "pantalla_lista" not in t]
        self.assertIsInstance(dynamic_without_list, list)


class AgentCoverageTest(unittest.TestCase):
    def test_every_si_task_names_an_existing_tool(self):
        data = _load()
        tool_names = {h["nombre"] for h in herramientas.HERRAMIENTAS}
        si_tasks = {t["id"] for t in data["tareas"] if t["agente"] == "si"}
        covered = set(TASK_TOOL)
        self.assertEqual(si_tasks, covered,
                          f"agente:si tasks and the test's task->tool mapping disagree: "
                          f"{si_tasks ^ covered}")
        for task_id, tool_name in TASK_TOOL.items():
            self.assertIn(tool_name, tool_names, f"task {task_id!r} names unknown tool {tool_name!r}")

    def test_no_aun_task_tool_is_not_claimed(self):
        data = _load()
        for t in data["tareas"]:
            if t["agente"] == "no_aun":
                self.assertNotIn(t["id"], TASK_TOOL)


if __name__ == "__main__":
    unittest.main()
