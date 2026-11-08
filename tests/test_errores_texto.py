"""BODY-LOGIN-MENSAJES-1: body/errores.py is Body's one rule for exception text that leaves the
process -- the TYPE name only, unless the exception is one of Body's own classes whose message
Body itself composed. Three things are tested here: the two helper functions, that
garminconnect's own logging never reaches a handler (so its library text cannot land in the
host's journal through Python's logging.lastResort), and an AST guard that fails any other spot
in the files this slice owns that renders an `except ... as <name>` name's text directly.

The guard's scan is scoped to the files BODY-LOGIN-MENSAJES-1 touches (`_SITIOS_DEL_CENSO`), not
all of body/**/*.py: a whole-package run of the same scanner (done once, by hand, while writing
this slice) also finds body/backup.py:55 and three sites in body/vivo.py (`str(exc)` on a
RuntimeError/OSError/TimeoutExpired bound name, truncated to 40 chars by `_motivo()` but not
routed through the helper) -- both outside this slice's `touches:`, pre-existing, and unrelated
to the Garmin sign-in census this slice was dispatched against. They are left for a follow-up
slice rather than widened into here.
"""
import ast
import contextlib
import io
import logging
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import errores, garmin_adapter, shift_import  # noqa: F401 (garmin_adapter: import-time silencing)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SITIOS_DEL_CENSO = [
    "body/login.py",
    "body/garmin_adapter.py",
    "body/shift_import.py",
    "body/conectar_pc.py",
    "body/subir_turnos.py",
    "body/traer_semana.py",
    "body/agente/bucle.py",
    "body/agente/precios.py",
]


def _es_nombre_desnudo(nodo, nombre: str) -> bool:
    return isinstance(nodo, ast.Name) and nodo.id == nombre


def _ofensas_en(nodo: ast.AST, nombre: str) -> list[tuple[int, str]]:
    """Walks nodo's subtree for a direct render of the bare except-bound name `nombre`: a call
    to str()/repr()/format() (builtin or `x.format(...)`) with it as an argument, a `%` operand,
    or an f-string FormattedValue whose value IS the bare name. `type(nombre).__name__` and
    `nombre.__class__.__name__` are untouched by this check -- they are never the bare name at
    the position examined, only a wrapping attribute/call -- which is exactly how body/errores.py
    and the sites it guards stay unflagged."""
    ofensas = []
    for sub in ast.walk(nodo):
        if isinstance(sub, ast.Call):
            func = sub.func
            fname = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None
            )
            if fname in ("str", "repr", "format"):
                for a in sub.args:
                    if _es_nombre_desnudo(a, nombre):
                        ofensas.append((sub.lineno, f"{fname}({nombre})"))
                for kw in sub.keywords:
                    if kw.value is not None and _es_nombre_desnudo(kw.value, nombre):
                        ofensas.append((sub.lineno, f"{fname}(...{nombre}=...)"))
        elif isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mod):
            right = sub.right
            objetivos = (
                right.elts if isinstance(right, (ast.Tuple, ast.List))
                else right.values if isinstance(right, ast.Dict)
                else [right]
            )
            for t in objetivos:
                if _es_nombre_desnudo(t, nombre):
                    ofensas.append((sub.lineno, f"%-format({nombre})"))
        elif isinstance(sub, ast.JoinedStr):
            for v in sub.values:
                if isinstance(v, ast.FormattedValue) and _es_nombre_desnudo(v.value, nombre):
                    ofensas.append((sub.lineno, f"f-string{{{nombre}}}"))
    return ofensas


def ofensas_en_fuente(fuente: str, archivo: str = "<fuente>") -> list[str]:
    """Every `except ... as <name>:` in `fuente` whose body renders <name>'s text directly.
    Returns formatted 'archivo:linea: ofensa' strings, one per offending use."""
    arbol = ast.parse(fuente, filename=archivo)
    listado = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ExceptHandler) and nodo.name:
            for stmt in nodo.body:
                for linea, desc in _ofensas_en(stmt, nodo.name):
                    listado.append(f"{archivo}:{linea}: except-as {nodo.name!r} -> {desc}")
    return listado


def ofensas_en_archivo(ruta_relativa: str) -> list[str]:
    ruta = _REPO_ROOT / ruta_relativa
    return ofensas_en_fuente(ruta.read_text(encoding="utf-8"), ruta_relativa)


class TextoSeguroTest(unittest.TestCase):
    def test_devuelve_solo_el_tipo(self):
        try:
            raise ValueError("demo-secreto-no-mostrar")
        except ValueError as e:
            resultado = errores.texto_seguro(e)
        self.assertEqual(resultado, "ValueError")
        self.assertNotIn("demo-secreto-no-mostrar", resultado)

    def test_funciona_para_una_excepcion_propia_tambien(self):
        try:
            raise shift_import.ImportRefused("no debería verse este texto en texto_seguro")
        except shift_import.ImportRefused as e:
            self.assertEqual(errores.texto_seguro(e), "ImportRefused")


class TextoPropioTest(unittest.TestCase):
    def test_devuelve_el_mensaje_de_una_excepcion_propia_de_body(self):
        try:
            raise shift_import.ImportRefused("unknown kind demo on 2026-01-01")
        except shift_import.ImportRefused as e:
            self.assertEqual(errores.texto_propio(e), "unknown kind demo on 2026-01-01")

    def test_rechaza_una_excepcion_que_no_es_de_body(self):
        try:
            raise ValueError("no es de body")
        except ValueError as e:
            with self.assertRaises(TypeError):
                errores.texto_propio(e)


class LoggerSilenciadoTest(unittest.TestCase):
    def test_un_registro_de_error_de_garminconnect_no_llega_a_ningun_manejador(self):
        logger_lib = logging.getLogger("garminconnect.client.sso")
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            logger_lib.error("texto-de-biblioteca-que-no-debe-aparecer")
        self.assertNotIn("texto-de-biblioteca-que-no-debe-aparecer", buffer.getvalue())

    def test_la_raiz_garminconnect_no_propaga(self):
        self.assertFalse(logging.getLogger("garminconnect").propagate)


class GuardaAstTest(unittest.TestCase):
    def test_detecta_el_patron_de_main_antes_de_este_slice(self):
        fuente_vulnerable = (
            "try:\n"
            "    pass\n"
            "except Exception as e:\n"
            '    print(f"Sign-in failed: {type(e).__name__}: {e}")\n'
        )
        ofensas = ofensas_en_fuente(fuente_vulnerable, "demo_login_viejo.py")
        self.assertEqual(len(ofensas), 1)
        self.assertIn("f-string{e}", ofensas[0])

    def test_no_marca_type_name_ni_la_llamada_al_helper(self):
        fuente_segura = (
            "try:\n"
            "    pass\n"
            "except Exception as e:\n"
            '    print(f"Sign-in failed: {errores.texto_seguro(e)}")\n'
            "except OtroError as exc:\n"
            '    print(f"{type(exc).__name__}: {exc.response.status_code}")\n'
        )
        self.assertEqual(ofensas_en_fuente(fuente_segura), [])

    def test_los_sitios_del_censo_quedan_limpios(self):
        offenders = []
        for ruta in _SITIOS_DEL_CENSO:
            offenders.extend(ofensas_en_archivo(ruta))
        self.assertEqual(offenders, [], "sitios sin pasar por body/errores.py:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
