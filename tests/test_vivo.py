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


class TareasFlagTest(unittest.TestCase):
    """`--tareas` is how a parent process asks a child's `body.vivo` which task names it knows,
    without importing it (BODY-VIVO-TAREA-NUEVA-1)."""

    def test_tareas_imprime_los_nombres_separados_por_coma(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            codigo = vivo.main(["--tareas"])

        self.assertEqual(codigo, 0)
        nombres = set(buf.getvalue().strip().split(","))
        self.assertEqual(nombres, set(vivo.TAREAS))


class TareaDesconocidaTest(_RepoFixture):
    """BODY-VIVO-TAREA-NUEVA-1: an unknown task name no longer returns before the refresh. The
    refresh still runs (same guard, same status file); only what happens AFTER depends on whether
    the refreshed tree turns out to know the name."""

    def setUp(self):
        super().setUp()
        _git_ok(self.repo, "reset", "--hard", _initial_sha)
        _git_ok(self.live, "checkout", "--force", "--detach", _initial_sha)
        self._argv_original = list(sys.argv)
        self._path_original = list(sys.path)
        self.addCleanup(lambda: setattr(sys, "argv", self._argv_original))
        self.addCleanup(lambda: setattr(sys, "path", self._path_original))

    def test_tarea_desconocida_pero_el_arbol_refrescado_la_conoce_lanza_un_hijo(self):
        self._commit_on_main("segundo")
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(
                 vivo, "_tareas_conocidas_por_el_arbol", return_value={"nueva"}
             ) as mock_conocidas, \
             patch.object(vivo, "_relanzar_en_arbol_nuevo", return_value=5) as mock_relanzar, \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["nueva", "--dias", "3"])

        self.assertEqual(codigo, 5)
        mock_conocidas.assert_called_once_with(self.live)
        mock_relanzar.assert_called_once_with(self.live, ["nueva", "--dias", "3"])

    def test_tarea_desconocida_y_el_arbol_tampoco_la_conoce_sale_con_2(self):
        self._commit_on_main("segundo")
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "_tareas_conocidas_por_el_arbol", return_value=set()), \
             patch.object(vivo, "_relanzar_en_arbol_nuevo") as mock_relanzar, \
             contextlib.redirect_stderr(io.StringIO()), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["nueva"])

        self.assertEqual(codigo, 2)
        mock_relanzar.assert_not_called()

    def test_tarea_desconocida_sin_refresco_sale_con_2_sin_hijo(self):
        (self.live / "a.txt").write_text("sucio", encoding="utf-8")
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(
                 vivo, "_tareas_conocidas_por_el_arbol", return_value={"nueva"}
             ) as mock_conocidas, \
             patch.object(vivo, "_relanzar_en_arbol_nuevo") as mock_relanzar, \
             contextlib.redirect_stderr(io.StringIO()), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["nueva"])

        self.assertEqual(codigo, 2)
        mock_relanzar.assert_not_called()
        mock_conocidas.assert_not_called()

    def test_argv_vacio_sale_con_2_sin_refresco(self):
        with patch.object(vivo, "refrescar") as mock_refrescar, \
             contextlib.redirect_stderr(io.StringIO()):
            codigo = vivo.main([])

        self.assertEqual(codigo, 2)
        mock_refrescar.assert_not_called()


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

    def test_tarea_conocida_no_lanza_un_hijo(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module"), \
             patch.object(vivo, "_relanzar_en_arbol_nuevo") as mock_relanzar, \
             patch.object(vivo, "_tareas_conocidas_por_el_arbol") as mock_conocidas, \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 0)
        mock_relanzar.assert_not_called()
        mock_conocidas.assert_not_called()

    def test_no_falla_sin_stdout_como_bajo_pythonw(self):
        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module"), \
             patch.object(vivo.sys, "stdout", None), \
             patch.dict(vivo.os.environ, {}, clear=False):
            codigo = vivo.main(["pull"])

        self.assertEqual(codigo, 0)


class TareasVidaDiaTest(unittest.TestCase):
    """BODY-VIDA-DIA-1: the two new scheduled-task entries vivo dispatches through."""

    def test_vida_dia_y_manana_estan_en_tareas(self):
        self.assertIn("vida_dia", vivo.TAREAS)
        self.assertIn("manana", vivo.TAREAS)


class MananaTest(_RepoFixture):
    """`manana` runs the same pull `pull` uses, then vida_dia -- even when the pull step fails
    (no network, Garmin down), so a single scheduled run still writes the day's file."""

    def setUp(self):
        super().setUp()
        self._argv_original = list(sys.argv)
        self._path_original = list(sys.path)
        self.addCleanup(lambda: setattr(sys, "argv", self._argv_original))
        self.addCleanup(lambda: setattr(sys, "path", self._path_original))

    def test_manana_corre_vida_dia_aunque_el_pull_previo_lance(self):
        llamadas = []

        def _run_module(nombre, run_name=None, alter_sys=None):
            llamadas.append(nombre)
            if nombre == "body.pull":
                raise RuntimeError("sin red, demo")

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", side_effect=_run_module), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["manana"])

        self.assertEqual(codigo, 0)
        self.assertEqual(llamadas, ["body.pull", "body.vida_dia"])

    def test_manana_corre_vida_dia_aunque_el_pull_previo_salga_con_systemexit(self):
        llamadas = []

        def _run_module(nombre, run_name=None, alter_sys=None):
            llamadas.append(nombre)
            if nombre == "body.pull":
                raise SystemExit(2)

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", side_effect=_run_module), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            codigo = vivo.main(["manana"])

        self.assertEqual(codigo, 0)
        self.assertEqual(llamadas, ["body.pull", "body.vida_dia"])

    def test_pull_previo_usa_days_1(self):
        argvs = []

        def _run_module(nombre, run_name=None, alter_sys=None):
            argvs.append(list(sys.argv))

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", side_effect=_run_module), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            vivo.main(["manana"])

        self.assertEqual(argvs[0], ["body.pull", "--days", "1"])
        self.assertEqual(argvs[1], ["body.vida_dia"])

    def test_tarea_pull_sola_no_dispara_el_paso_previo(self):
        llamadas = []

        def _run_module(nombre, run_name=None, alter_sys=None):
            llamadas.append(nombre)

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", side_effect=_run_module), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            vivo.main(["pull"])

        self.assertEqual(llamadas, ["body.pull"])


class IntradiaTareaTest(_RepoFixture):
    """BODY-MOMENTOS-PULL-1: `intradia` shares body.pull's own module, selected by a --intradia
    flag vivo prepends on the task's behalf (PREFIJO_ARGV) -- the person never types that flag."""

    def setUp(self):
        super().setUp()
        self._argv_original = list(sys.argv)
        self._path_original = list(sys.path)
        self.addCleanup(lambda: setattr(sys, "argv", self._argv_original))
        self.addCleanup(lambda: setattr(sys, "path", self._path_original))

    def test_intradia_esta_en_tareas_y_apunta_a_pull(self):
        self.assertIn("intradia", vivo.TAREAS)
        self.assertEqual(vivo.TAREAS["intradia"], "body.pull")

    def test_intradia_antepone_la_bandera_y_pasa_dias(self):
        argvs = []

        def _run_module(nombre, run_name=None, alter_sys=None):
            argvs.append(list(sys.argv))

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", side_effect=_run_module), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            vivo.main(["intradia", "--dias", "30"])

        self.assertEqual(argvs[0], ["body.pull", "--intradia", "--dias", "30"])

    def test_otras_tareas_no_llevan_la_bandera_de_intradia(self):
        argvs = []

        def _run_module(nombre, run_name=None, alter_sys=None):
            argvs.append(list(sys.argv))

        with patch.object(vivo, "arbol_vivo", return_value=self.live), \
             patch.object(vivo, "refrescar", return_value="ya_al_dia"), \
             patch.object(vivo.os, "chdir"), \
             patch.object(vivo.runpy, "run_module", side_effect=_run_module), \
             patch.dict(vivo.os.environ, {}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()):
            vivo.main(["pull", "--days", "2"])

        self.assertEqual(argvs[0], ["body.pull", "--days", "2"])


class DespertarTareaTest(unittest.TestCase):
    """BODY-DESPERTAR-1: the wake check task is wired to body.despertar."""

    def test_despertar_esta_en_tareas(self):
        self.assertIn("despertar", vivo.TAREAS)
        self.assertEqual(vivo.TAREAS["despertar"], "body.despertar")


class ImportarGarminTareaTest(unittest.TestCase):
    """BODY-GARMIN-IMPORT-1: the export importer runs through the live tree like every other
    task, arguments passed straight through."""

    def test_importar_garmin_esta_en_tareas(self):
        self.assertIn("importar_garmin", vivo.TAREAS)
        self.assertEqual(vivo.TAREAS["importar_garmin"], "body.importar_garmin")

    def test_no_bandera_antepuesta(self):
        self.assertNotIn("importar_garmin", vivo.PREFIJO_ARGV)


class RecuperacionTareaTest(unittest.TestCase):
    """BODY-RECUPERACION-1: the first backfill runs through the live tree like every other task,
    no arguments needed; the daily pull calls body.recuperacion.recalcular per person after that,
    so this task is for the one-off catch-up only."""

    def test_recuperacion_esta_en_tareas(self):
        self.assertIn("recuperacion", vivo.TAREAS)
        self.assertEqual(vivo.TAREAS["recuperacion"], "body.recuperacion")

    def test_no_bandera_antepuesta(self):
        self.assertNotIn("recuperacion", vivo.PREFIJO_ARGV)

    def test_no_paso_previo(self):
        self.assertNotIn("recuperacion", vivo.PASOS_PREVIOS)


class InstallScriptWakeCheckTest(unittest.TestCase):
    """A PowerShell installer is never executed in tests -- asserted by reading its text
    (BODY-DESPERTAR-1)."""

    @classmethod
    def setUpClass(cls):
        ruta = Path(vivo.__file__).resolve().parents[1] / "scripts" / "install-daily-sync.ps1"
        cls.texto = ruta.read_text(encoding="utf-8")

    def test_nueva_tarea_existe(self):
        self.assertIn('"Body app wake check"', self.texto)
        self.assertIn("-m body.vivo despertar", self.texto)

    def test_otras_tareas_siguen_presentes_sin_cambios(self):
        for nombre in ("Body app daily sync", "Body app weekly file", "Body app morning file",
                       "Body app web", "Body app shift upload", "Body app weekly fetch"):
            self.assertIn(f'"{nombre}"', self.texto)

    def test_linea_de_remocion_nombra_la_tarea_nueva(self):
        linea = next(l for l in self.texto.splitlines() if l.startswith("# Remove with:"))
        self.assertIn('"Body app wake check"', linea)


class InstallScriptMorningFileTest(unittest.TestCase):
    """A PowerShell installer is never executed in tests -- asserted by reading its text
    (BODY-VIDA-DIA-1)."""

    @classmethod
    def setUpClass(cls):
        ruta = Path(vivo.__file__).resolve().parents[1] / "scripts" / "install-daily-sync.ps1"
        cls.texto = ruta.read_text(encoding="utf-8")

    def test_nueva_tarea_y_parametro_existen(self):
        self.assertIn('"Body app morning file"', self.texto)
        self.assertIn("-m body.vivo manana", self.texto)
        self.assertIn("$MorningAt", self.texto)
        self.assertIn('@("04:40", "07:40", "12:40")', self.texto)

    def test_otras_tareas_siguen_presentes_sin_cambios(self):
        for nombre in ("Body app daily sync", "Body app weekly file", "Body app web",
                       "Body app shift upload", "Body app weekly fetch"):
            self.assertIn(f'"{nombre}"', self.texto)
        self.assertIn("-m body.vivo pull --days 2", self.texto)
        self.assertIn("-m body.vivo vida_export --current", self.texto)

    def test_linea_de_remocion_nombra_la_tarea_nueva(self):
        linea = next(l for l in self.texto.splitlines() if l.startswith("# Remove with:"))
        self.assertIn('"Body app morning file"', linea)


if __name__ == "__main__":
    unittest.main()
