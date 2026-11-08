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
    def test_tarea_fuera_del_allowlist_sale_con_2(self):
        self.assertEqual(vivo.main(["no-existe"]), 2)

    def test_despacha_con_body_env_file_apuntando_al_repo_principal(self):
        capturado = {}

        def _stub(tarea, cmd, cwd, env):
            capturado["tarea"] = tarea
            capturado["cmd"] = cmd
            capturado["cwd"] = cwd
            capturado["env"] = env
            return 0

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "_despachar", side_effect=_stub) as mock_despachar:
            codigo = vivo.main(["pull", "--days", "2"])

        self.assertEqual(codigo, 0)
        mock_despachar.assert_called_once()
        self.assertEqual(capturado["tarea"], "pull")
        self.assertEqual(capturado["cmd"], [sys.executable, "-m", "body.pull", "--days", "2"])
        self.assertEqual(capturado["cwd"], self.live)
        self.assertEqual(
            Path(capturado["env"]["BODY_ENV_FILE"]).resolve(), (self.repo / ".env").resolve()
        )

    def test_body_env_file_ya_puesto_no_se_sobreescribe(self):
        capturado = {}

        def _stub(tarea, cmd, cwd, env):
            capturado["env"] = env
            return 0

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "_despachar", side_effect=_stub), \
             patch.dict(vivo.os.environ, {"BODY_ENV_FILE": "C:/otra/ruta/.env"}):
            vivo.main(["pull"])

        self.assertEqual(capturado["env"]["BODY_ENV_FILE"], "C:/otra/ruta/.env")


if __name__ == "__main__":
    unittest.main()
