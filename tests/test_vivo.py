"""body.vivo: the launcher every scheduled task runs through (BODY-LIVE-CHECKOUT-1).

Builds throwaway git repos in a temp directory -- never touches this repository's own worktree.
"""
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


class _RepoFixture(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="body-vivo-test-"))
        self.repo = base / "repo"
        self.repo.mkdir()
        _git_ok(self.repo, "init", "-b", "main")
        _git_ok(self.repo, "config", "user.email", "test@example.com")
        _git_ok(self.repo, "config", "user.name", "Test")
        (self.repo / "a.txt").write_text("1", encoding="utf-8")
        _git_ok(self.repo, "add", "a.txt")
        _git_ok(self.repo, "commit", "-m", "first")
        self.live = base / "live"
        _git_ok(self.repo, "worktree", "add", "--detach", str(self.live), "main")

    def _commit_on_main(self, texto: str) -> str:
        (self.repo / "a.txt").write_text(texto, encoding="utf-8")
        _git_ok(self.repo, "add", "a.txt")
        _git_ok(self.repo, "commit", "-m", texto)
        return _git_ok(self.repo, "rev-parse", "main")


class RefrescarTest(_RepoFixture):
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
        self.assertEqual(vivo.main(["no-existe"]), 2)

    def test_corre_la_tarea_con_runpy_en_el_mismo_proceso(self):
        capturado = {}

        def _run_module(nombre, run_name=None, alter_sys=None):
            capturado["nombre"] = nombre
            capturado["run_name"] = run_name
            capturado["alter_sys"] = alter_sys
            capturado["argv"] = list(sys.argv)

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo.os, "chdir") as mock_chdir, \
             patch.object(vivo.runpy, "run_module", side_effect=_run_module) as mock_run_module, \
             patch.dict(vivo.os.environ, {}, clear=False):
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
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", side_effect=SystemExit(7)), \
             patch.dict(vivo.os.environ, {}, clear=False):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 7)

    def test_retorno_normal_de_la_tarea_es_cero(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", return_value=None), \
             patch.dict(vivo.os.environ, {}, clear=False):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 0)

    def test_body_env_file_ya_puesto_no_se_sobreescribe(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module"), \
             patch.dict(vivo.os.environ, {"BODY_ENV_FILE": "C:/otra/ruta/.env"}):
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
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module") as mock_run_module, \
             patch.object(
                 vivo.os, "execve", side_effect=AssertionError("os.execve must not be called")
             ), \
             patch.object(vivo.subprocess, "run", side_effect=_subprocess_run_vigilado), \
             patch.dict(vivo.os.environ, {}, clear=False):
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

    def test_no_falla_sin_stdout_como_bajo_pythonw(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module"), \
             patch.object(vivo.sys, "stdout", None), \
             patch.dict(vivo.os.environ, {}, clear=False):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 0)


if __name__ == "__main__":
    unittest.main()
