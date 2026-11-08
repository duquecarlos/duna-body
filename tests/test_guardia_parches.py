"""BODY-WEB-ESTRUCTURA-1: a test must patch the module that OWNS and READS a name. Patching a
module that only re-imported it (`from .other import X`) silently does nothing at request time,
because the re-importing module's own binding still points at the original object -- exactly the
bug this slice's "no re-export shim, reach shared names as comun.X" rule exists to prevent.

This guard scans every tests/test_*.py module's `patch`/`patch.object` calls that target a body
module's attribute (resolved the same way unittest.mock itself resolves a dotted patch target: the
longest importable module prefix, then getattr for whatever is left) and fails the moment one names
an owner that (a) does not define the attribute at its own top level, or (b) is re-imported by name
into another body/ module, which would make a patch on the real owner miss that module's own copy
of the binding. It also asserts body.web is never imported by a module under body/rutas/ -- the
module boundary this slice's whole move exists to keep one-directional.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import re
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._rutas import modulos_con_rutas

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
BODY_DIR = REPO_ROOT / "body"

# Pre-existing re-imports under body/ that would make an outside patch on the name's real owner
# miss (rule (b)), found by the live scan below and unrelated to this slice's move -- allow-listed
# here, one line each, rather than refactored in this slice. Each key is (owner module, name);
# the live scan found none as of BODY-WEB-ESTRUCTURA-1, so this starts empty.
_ALLOWED_REIMPORTS: dict[tuple[str, str], set[str]] = {}


def _iter_test_files():
    return sorted(TESTS_DIR.glob("test_*.py"))


def _iter_body_files():
    return sorted(BODY_DIR.rglob("*.py"))


def _module_name_for(path: Path) -> tuple[str, bool]:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    is_init = parts[-1] == "__init__"
    if is_init:
        parts = parts[:-1]
    return ".".join(parts), is_init


def _is_patch_call(node: ast.Call) -> tuple[bool, bool]:
    """(is_patch_object, is_patch_string) for a Call node -- matches `patch(...)`,
    `mock.patch(...)`, `patch.object(...)` and `mock.patch.object(...)` by the called name alone,
    so it is immune to however the module got imported."""
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "object":
        inner = f.value
        if (isinstance(inner, ast.Name) and inner.id == "patch") or (
            isinstance(inner, ast.Attribute) and inner.attr == "patch"
        ):
            return True, False
        return False, False
    if isinstance(f, ast.Name) and f.id == "patch":
        return False, True
    if isinstance(f, ast.Attribute) and f.attr == "patch":
        return False, True
    return False, False


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted path, for every name this test file imports from the `body` package
    (direct or aliased) -- the only base names this guard's `patch.object` resolution is in scope
    for (rule's own wording: "<name> resolves to an imported body module")."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "body" or node.module.startswith("body.")
        ):
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "body" or alias.name.startswith("body."):
                    bindings[alias.asname or alias.name.split(".")[0]] = alias.asname or "body"
    return bindings


def _resolve_dotted(path: str):
    """The same resolution unittest.mock itself uses for a string patch target: the longest
    importable module prefix, then plain getattr for whatever is left."""
    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for p in parts[i:]:
            obj = getattr(obj, p)
        return obj
    raise ImportError(path)


def _resolve_expr(expr: ast.expr, bindings: dict[str, str]):
    """Resolves a `patch.object(<expr>, ...)` first argument -- an attribute chain such as
    `web.agente_cliente` -- to the real runtime object it names, using `bindings` only for the
    leftmost name. None if that base name is not one of this file's own body-package imports."""
    chain: list[str] = []
    node = expr
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    base = bindings.get(node.id)
    if base is None:
        return None
    try:
        obj = _resolve_dotted(base)
    except (ImportError, AttributeError):
        return None
    for attr in reversed(chain):
        obj = getattr(obj, attr, None)
        if obj is None:
            return None
    return obj


def _resolve_patch_string(value: str):
    """Owner object and attribute name for a `patch("a.b.c")` string target, body.* only."""
    parts = value.split(".")
    if not parts or parts[0] != "body":
        return None, None
    for i in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        owner = obj
        for p in parts[i:-1]:
            owner = getattr(owner, p, None)
            if owner is None:
                break
        else:
            return owner, parts[-1]
    return None, None


def _is_shim_alias_assign(node: ast.Assign) -> bool:
    """True for a top-level `X = comun.Y` / `X = fechas.Z` -- a bare re-binding of another
    module's attribute under a second name. Ruling BODY-WEB-ESTRUCTURA-PLAN-1 rule 3 forbids this
    shape (BODY-WEB-ESTRUCTURA-3): the next mover re-binds again, and a patch on one binding
    misses the other."""
    return (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name))


def _defined_at_top_level(module, name: str, strict: bool = False) -> bool:
    """def, class, assignment or import binding at the module's own top level -- read from its
    source (not runtime getattr, which would also find a name only inherited or re-exported).

    strict=True (BODY-WEB-ESTRUCTURA-3) additionally rejects a shim-alias assignment
    (`_is_shim_alias_assign`): a name only ever bound that way is not really defined at this
    module's own top level, it is borrowed from whatever module the alias's value names."""
    try:
        src = Path(module.__file__).read_text(encoding="utf-8")
    except (TypeError, OSError):
        return False
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, ast.Assign):
            if strict and _is_shim_alias_assign(node):
                continue
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((alias.asname or alias.name.split(".")[-1]) == name for alias in node.names):
                return True
    return False


def _package_of(mod_name: str, is_init: bool) -> str:
    if is_init:
        return mod_name
    return mod_name.rsplit(".", 1)[0] if "." in mod_name else mod_name


def _resolve_relative_module(importing_module: str, is_init: bool, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    dotted = "." * node.level + (node.module or "")
    try:
        return importlib.util.resolve_name(dotted, _package_of(importing_module, is_init))
    except ImportError:
        return None


def _reimporters_of(owner_module_name: str, name: str) -> list[str]:
    """Every module under body/ (other than the owner itself) that does
    `from <owner> import name`, directly or through a relative import -- a patch on the real
    owner would miss a route call written through one of these modules' own copied binding."""
    offenders = []
    for path in _iter_body_files():
        mod_name, is_init = _module_name_for(path)
        if mod_name == owner_module_name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if _resolve_relative_module(mod_name, is_init, node) != owner_module_name:
                continue
            if any(alias.name == name for alias in node.names):
                offenders.append(mod_name)
    return offenders


def _scan_patch_targets():
    """(test_file, lineno, owner_module_name, attr) for every patch target in tests/ that
    resolves to a body module's attribute."""
    targets = []
    for path in _iter_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bindings = _import_bindings(tree)
        rel = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_obj, is_str = _is_patch_call(node)
            if is_obj:
                if len(node.args) < 2:
                    continue
                attr_node = node.args[1]
                if not (isinstance(attr_node, ast.Constant) and isinstance(attr_node.value, str)):
                    continue
                owner = _resolve_expr(node.args[0], bindings)
                attr = attr_node.value
            elif is_str:
                if not node.args:
                    continue
                arg0 = node.args[0]
                if not (isinstance(arg0, ast.Constant) and isinstance(arg0.value, str)):
                    continue
                owner, attr = _resolve_patch_string(arg0.value)
            else:
                continue
            if owner is None or not inspect.ismodule(owner):
                continue
            if not (owner.__name__ == "body" or owner.__name__.startswith("body.")):
                continue
            targets.append((rel, node.lineno, owner.__name__, attr))
    return targets


class PatchTargetGuardTest(unittest.TestCase):
    def test_every_body_patch_targets_the_owning_module(self):
        targets = _scan_patch_targets()
        self.assertGreater(len(targets), 0, "no body-module patch target found -- the scan is broken")

        not_defined = []
        reimported = []
        for test_file, lineno, owner_name, attr in targets:
            owner_mod = importlib.import_module(owner_name)
            if not _defined_at_top_level(owner_mod, attr):
                not_defined.append(f"{test_file}:{lineno} patches {owner_name}.{attr}, but {attr} is not "
                                   f"defined at {owner_name}'s own top level")
            offenders = [m for m in _reimporters_of(owner_name, attr)
                        if m not in _ALLOWED_REIMPORTS.get((owner_name, attr), set())]
            if offenders:
                reimported.append(f"{test_file}:{lineno} patches {owner_name}.{attr}, but "
                                  f"{', '.join(sorted(offenders))} re-import {attr} by name -- a call "
                                  f"through that module's own binding would miss this patch")

        offenders_text = "\n".join(not_defined + reimported)
        self.assertEqual(not_defined, [], f"stale patch target(s):\n{offenders_text}")
        self.assertEqual(reimported, [], f"patch target(s) with a re-importing module:\n{offenders_text}")

    def test_no_test_module_reads_dot_routes_directly(self):
        """BODY-WEB-ESTRUCTURA-1-M: every test that needs the app's real route set reads it
        through tests/_rutas.py's rutas_planas() -- the one place that knows FastAPI 0.162 wraps
        an included router in a non-flattened `_IncludedRouter`. A test file that instead reads
        some object's `.routes` itself would carry its own, independently stale copy of that
        knowledge (as tests/test_web_entry.py, tests/test_agente_registro.py,
        tests/test_asistente_panel.py and tests/test_tutoriales_catalogo.py each did before this
        slice). The one allowed `.routes` read anywhere in a test file is the argument passed
        straight into a `rutas_planas(...)` call, e.g. `rutas_planas(web.app.routes)`."""
        offenders = []
        for path in _iter_test_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            allowed: set[tuple[int, int]] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name != "rutas_planas":
                    continue
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Attribute) and sub.attr == "routes":
                            allowed.add((sub.lineno, sub.col_offset))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr == "routes"
                        and (node.lineno, node.col_offset) not in allowed):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], f"test module(s) reading `.routes` directly: {offenders}")

    def test_rutas_modules_never_import_web(self):
        offenders = []
        for path in sorted((BODY_DIR / "rutas").glob("*.py")):
            mod_name, is_init = _module_name_for(path)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "body.web":
                            offenders.append(mod_name)
                elif isinstance(node, ast.ImportFrom):
                    resolved = _resolve_relative_module(mod_name, is_init, node)
                    if resolved == "body.web":
                        offenders.append(mod_name)
                    elif resolved == "body" and any(a.name == "web" for a in node.names):
                        offenders.append(mod_name)
        self.assertEqual(offenders, [], f"body/rutas/ module(s) importing body.web: {offenders}")


class NoShimAliasGuardTest(unittest.TestCase):
    """BODY-WEB-ESTRUCTURA-3: no top-level assignment in body/web.py or body/rutas/*.py re-binds
    another body module's attribute under a new name. Four such shims survived in body/web.py
    until this slice -- `_static_hash`, `STATIC_HASH`, `static_url` (ESTRUCTURA-1-M) and
    `_short_date` (ESTRUCTURA-2), each `X = comun.Y` -- because the guard that should have caught
    them did not exist."""

    def test_no_top_level_name_aliases_another_body_modules_attribute(self):
        offenders = []
        for path, tree in modulos_con_rutas():
            for node in tree.body:
                if isinstance(node, ast.Assign) and _is_shim_alias_assign(node):
                    target = node.targets[0].id
                    owner = node.value.value.id
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                                      f"{target} = {owner}.{node.value.attr}")
        self.assertEqual(offenders, [], f"shim alias assignment(s): {offenders}")


_DUNDER_RE = re.compile(r"^__.+__$")


def _is_route_module_owner(owner_name: str) -> bool:
    return owner_name == "body.web" or owner_name.startswith("body.rutas")


class RouteModuleAttrReadGuardTest(unittest.TestCase):
    """Extends guard (a) beyond patch targets (BODY-WEB-ESTRUCTURA-3): every `name.ATTR` read
    anywhere in tests/, where `name` resolves to body.web or a body.rutas module, must name an
    attribute truly defined at that module's own top level -- not a module dunder such as
    `__file__`, and not a shim alias (_defined_at_top_level with strict=True). A test reading
    `web.STATIC_HASH` after the shim moved to comun.py would otherwise read a stale module-level
    name that import machinery no longer sets, with no guard to say why."""

    def test_every_route_module_attribute_read_is_truly_defined(self):
        targets = []
        for path in _iter_test_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            bindings = _import_bindings(tree)
            rel = str(path.relative_to(REPO_ROOT))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                base = node.value
                if not isinstance(base, ast.Name):
                    continue
                dotted = bindings.get(base.id)
                if dotted is None:
                    continue
                try:
                    owner = _resolve_dotted(dotted)
                except (ImportError, AttributeError):
                    continue
                if not inspect.ismodule(owner):
                    continue
                if not _is_route_module_owner(owner.__name__):
                    continue
                if _DUNDER_RE.match(node.attr):
                    continue
                targets.append((rel, node.lineno, owner.__name__, node.attr))

        self.assertGreater(len(targets), 0, "no body.web/body.rutas attribute read found -- the scan is broken")
        offenders = []
        for rel, lineno, owner_name, attr in targets:
            owner_mod = importlib.import_module(owner_name)
            if not _defined_at_top_level(owner_mod, attr, strict=True):
                offenders.append(f"{rel}:{lineno} reads {owner_name}.{attr}, not defined at its own top level")
        self.assertEqual(offenders, [], f"stale attribute read(s):\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
