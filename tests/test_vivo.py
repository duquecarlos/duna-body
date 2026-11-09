"""body.vivo: the launcher every scheduled task runs through (BODY-LIVE-CHECKOUT-1).

Builds throwaway git repos in a temp directory -- never touches this repository's own worktree.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import vivo


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10
    )


def _git_ok(cwd, *args):
    resultado = _git(cwd, *args)
    assert resultado.returncode == 0, resultado.stderr
    return resultado.stdout.strip()


def _head_is_detached(cwd) -> bool:
    return _git(cwd, "symbolic-ref", "-q", "HEAD").returncode != 0


_base: Path | None = None
_repo: Path | None = None
_live: Path | None = None
_initial_sha: str | None = None


def setUpModule():
    """One throwaway repo + worktree, built once for the whole module (git on Windows is slow to
    spawn -- BODY-TESTS-VELOCIDAD-1). Every test gets a clean start by resetting both trees back
    to this initial commit rather than re-running `init`/`worktree add`."""
    global _base, _repo, _live, _initial_sha
    _base = Path(tempfile.mkdtemp(prefix="body-vivo-test-"))
    _repo = _base / "repo"
    _repo.mkdir()
    _git_ok(_repo, "init", "-b", "main")
    _git_ok(_repo, "config", "user.email", "test@example.com")
    _git_ok(_repo, "config", "user.name", "Test")
    (_repo / "a.txt").write_text("1", encoding="utf-8")
    _git_ok(_repo, "add", "a.txt")
    _git_ok(_repo, "commit", "-m", "first")
    _initial_sha = _git_ok(_repo, "rev-parse", "main")
    _live = _base / "live"
    _git_ok(_repo, "worktree", "add", "--detach", str(_live), "main")


def tearDownModule():
    shutil.rmtree(_base, ignore_errors=True)


class _RepoFixture(unittest.TestCase):
    def setUp(self):
        self.repo = _repo
        self.live = _live

    def _commit_on_main(self, texto: str) -> str:
        (self.repo / "a.txt").write_text(texto, encoding="utf-8")
        _git_ok(self.repo, "add", "a.txt")
        _git_ok(self.repo, "commit", "-m", texto)
        return _git_ok(self.repo, "rev-parse", "main")


class RefrescarTest(_RepoFixture):
    """The only class whose tests commit on main or dirty the live tree, so it is the only one
    that needs each test reset back to the shared fixture's initial commit."""

    def setUp(self):
        super().setUp()
        # No test here ever creates an untracked file -- only `a.txt`'s tracked content changes --
        # so a plain reset/force-checkout is enough; no separate `clean` is needed.
        _git_ok(self.repo, "reset", "--hard", _initial_sha)
        _git_ok(self.live, "checkout", "--force", "--detach", _initial_sha)

    def test_mueve_el_arbol_vivo_a_la_nueva_main(self):
        antes = _git_ok(self.live, "rev-parse", "HEAD")
        nueva = self._commit_on_main("segundo")
        resultado = vivo.refrescar(self.live)
        self.assertEqual(resultado, "actualizado")
        self.assertEqual(_git_ok(self.live, "rev-parse", "HEAD"), nueva)
        self.assertNotEqual(antes, nueva)
        self.assertTrue(_head_is_detached(self.live))

    def test_es_un_no_op_cuando_ya_esta_al_dia(self):
        resultado = vivo.refrescar(self.live)
        self.assertEqual(resultado, "ya_al_dia")
        self.assertTrue(_head_is_detached(self.live))

    def test_refusa_un_arbol_vivo_sucio_y_no_lo_mueve(self):
        self._commit_on_main("segundo")
        antes = _git_ok(self.live, "rev-parse", "HEAD")
        (self.live / "a.txt").write_text("sucio", encoding="utf-8")
        resultado = vivo.refrescar(self.live)
        self.assertEqual(resultado, "sin_cambio:sucio")
        self.assertEqual(_git_ok(self.live, "rev-parse", "HEAD"), antes)
        self.assertTrue(_head_is_detached(self.live))
        self.assertEqual((self.live / "a.txt").read_text(encoding="utf-8"), "sucio")

    def test_refusa_actuar_sobre_el_arbol_principal(self):
        antes = _git_ok(self.repo, "rev-parse", "HEAD")
        self._commit_on_main("segundo")
        resultado = vivo.refrescar(self.repo)
        self.assertEqual(resultado, "sin_cambio:no_es_arbol_vivo")
        self.assertEqual(_git_ok(self.repo, "rev-parse", "main"), _git_ok(self.repo, "rev-parse", "HEAD"))
        self.assertFalse(_head_is_detached(self.repo))
        self.assertNotEqual(antes, _git_ok(self.repo, "rev-parse", "HEAD"))


class ProcesoFrescoTest(unittest.TestCase):
    """Runs `python -m body.vivo` in an actual fresh interpreter, on its own throwaway worktree
    that carries a real copy of the `body` package. This is the case the fake-module regression
    in MainTest cannot reach: inside the shared unittest process, `body.vivo` had already
    imported `body.errores` long before any test ran, so a guard keyed on `sys.modules` minus a
    hardcoded exception list never saw itself trip -- only a fresh process does.

    `subir_turnos --help` is the task: argparse intercepts `--help` and exits before `main()`
    reaches the shifts folder, the device link or the network, so it touches no data."""

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="body-vivo-proc-test-"))
        cls.repo = cls.base / "repo"
        paquete_origen = Path(vivo.__file__).resolve().parent
        shutil.copytree(
            paquete_origen, cls.repo / "body", ignore=shutil.ignore_patterns("__pycache__")
        )
        _git_ok(cls.repo, "init", "-b", "main")
        _git_ok(cls.repo, "config", "user.email", "test@example.com")
        _git_ok(cls.repo, "config", "user.name", "Test")
        _git_ok(cls.repo, "add", "-A")
        _git_ok(cls.repo, "commit", "-m", "primero")
        cls.sha_primero = _git_ok(cls.repo, "rev-parse", "main")
        (cls.repo / "body" / "marca.txt").write_text("segundo", encoding="utf-8")
        _git_ok(cls.repo, "add", "-A")
        _git_ok(cls.repo, "commit", "-m", "segundo")
        cls.sha_segundo = _git_ok(cls.repo, "rev-parse", "main")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.live = self.base / f"live-{self._testMethodName}"
        self.home = self.base / f"home-{self._testMethodName}"
        _git_ok(self.repo, "worktree", "add", "--detach", str(self.live), self.sha_primero)
        self.addCleanup(shutil.rmtree, self.live, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_un_proceso_fresco_se_actualiza_a_la_nueva_main(self):
        env = {**os.environ, "BODY_HOME": str(self.home)}
        resultado = subprocess.run(
            [sys.executable, "-m", "body.vivo", "subir_turnos", "--help"],
            cwd=str(self.live), env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("actualizado", resultado.stdout)
        self.assertEqual(_git_ok(self.live, "rev-parse", "HEAD"), self.sha_segundo)

        estado = json.loads((self.home / vivo.ESTADO_NOMBRE).read_text(encoding="utf-8"))
        self.assertEqual(estado["resultado"], "actualizado")
        self.assertEqual(estado["tarea"], "subir_turnos")
        self.assertEqual(estado["sha"], _git_ok(self.live, "rev-parse", "--short", "HEAD"))
        self.assertEqual(estado["sha_main"], _git_ok(self.repo, "rev-parse", "--short", "main"))
        self.assertIn("marca_tiempo_utc", estado)


class RepoPrincipalTest(_RepoFixture):
    def test_repo_principal_es_el_padre_del_common_dir(self):
        self.assertEqual(vivo.repo_principal(self.live).resolve(), self.repo.resolve())
        self.assertEqual(vivo.repo_principal(self.repo).resolve(), self.repo.resolve())


class MainTest(_RepoFixture):
    """`main` runs the task in-process via `runpy.run_module`, never `os.execve` or a subprocess
    spawn of a python interpreter (BODY-LIVE-CHECKOUT-1-M)."""

    def setUp(self):
        super().setUp()
        self._argv_original = list(sys.argv)
        self._path_original = list(sys.path)
        self.addCleanup(lambda: setattr(sys, "argv", self._argv_original))
        self.addCleanup(lambda: setattr(sys, "path", self._path_original))

    def test_tarea_fuera_del_allowlist_sale_con_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(vivo.main(["no-existe"]), 2)

    def test_corre_la_tarea_con_runpy_en_el_mismo_proceso(self):
        capturado = {}

        def _run_module(nombre, run_name=None, alter_sys=None):
            capturado["nombre"] = nombre
            capturado["run_name"] = run_name
            capturado["alter_sys"] = alter_sys
            capturado["argv"] = list(sys.argv)

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir") as mock_chdir, \
             patch.object(vivo.runpy, "run_module", side_effect=_run_module) as mock_run_module, \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["pull", "--days", "2"])

            self.assertEqual(
                Path(vivo.os.environ["BODY_ENV_FILE"]).resolve(), (self.repo / ".env").resolve()
            )

        self.assertEqual(codigo, 0)
        mock_chdir.assert_called_once_with(self.live)
        mock_run_module.assert_called_once()
        self.assertEqual(capturado["nombre"], "body.pull")
        self.assertEqual(capturado["run_name"], "__main__")
        self.assertTrue(capturado["alter_sys"])
        self.assertEqual(capturado["argv"], ["body.pull", "--days", "2"])

    def test_systemexit_de_la_tarea_propaga_su_codigo(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", side_effect=SystemExit(7)), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 7)

    def test_retorno_normal_de_la_tarea_es_cero(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", return_value=None), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 0)

    def test_body_env_file_ya_puesto_no_se_sobreescribe(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module"), \
             patch.dict(vivo.os.environ, {"BODY_ENV_FILE": "C:/otra/ruta/.env"}), \
             contextlib.redirect_stdout(io.StringIO()):
            vivo.main(["pull"])
            self.assertEqual(vivo.os.environ["BODY_ENV_FILE"], "C:/otra/ruta/.env")

    def test_no_usa_execve_ni_subprocess_para_lanzar_la_tarea(self):
        subprocess_run_original = vivo.subprocess.run

        def _subprocess_run_vigilado(cmd, *args, **kwargs):
            primero = cmd[0] if isinstance(cmd, (list, tuple)) else cmd
            self.assertNotEqual(
                primero, sys.executable, "la tarea no debe lanzarse con subprocess.run"
            )
            return subprocess_run_original(cmd, *args, **kwargs)

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module") as mock_run_module, \
             patch.object(
                 vivo.os, "execve", side_effect=AssertionError("os.execve must not be called")
             ), \
             patch.object(vivo.subprocess, "run", side_effect=_subprocess_run_vigilado), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["web", "--port", "1"])

        self.assertEqual(codigo, 0)
        mock_run_module.assert_called_once_with("body.web", run_name="__main__", alter_sys=True)

    def test_modulos_body_previos_se_registran_pero_la_tarea_igual_corre(self):
        nombre_falso = "body._fixture_modulos_previos"
        sys.modules[nombre_falso] = object()
        self.addCleanup(sys.modules.pop, nombre_falso, None)
        buf = []

        class _Stdout:
            def write(self, texto):
                buf.append(texto)

            def flush(self):
                pass

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar") as mock_refrescar, \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module") as mock_run_module, \
             patch.object(vivo.sys, "stdout", _Stdout()), \
             patch.dict(vivo.os.environ, {}, clear=False):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 0)
        mock_refrescar.assert_not_called()
        mock_run_module.assert_called_once()
        self.assertIn("sin_cambio:modulos_previos", "".join(buf))

    def test_el_archivo_de_estado_tiene_exactamente_los_campos_esperados(self):
        for resultado_esperado in ("actualizado", "sin_cambio:algo"):
            with patch.object(vivo, "arbol_vivo", return_value=self.live), \
                 patch.object(vivo, "refrescar", return_value=resultado_esperado), \
                 patch.object(vivo.os, "chdir"), \
                 patch.object(vivo.runpy, "run_module"), \
                 patch.dict(vivo.os.environ, {}, clear=False), \
                 contextlib.redirect_stdout(io.StringIO()):
                vivo.main(["pull"])

            estado = json.loads(
                (vivo.config.BODY_HOME / vivo.ESTADO_NOMBRE).read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(estado.keys()), {"resultado", "tarea", "sha", "sha_main", "marca_tiempo_utc"}
            )
            self.assertEqual(estado["resultado"], resultado_esperado)
            self.assertEqual(estado["tarea"], "pull")

    def test_estado_no_escribible_no_impide_la_tarea(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.config, "ensure_home", side_effect=OSError("sin disco")), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module") as mock_run_module, \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 0)
        mock_run_module.assert_called_once()

    def test_no_falla_sin_stdout_como_bajo_pythonw(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module"), \
             patch.object(vivo.sys, "stdout", None), \
             patch.dict(vivo.os.environ, {}, clear=False):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 0)


if __name__ == "__main__":
    unittest.main()
