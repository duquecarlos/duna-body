"""The self-update helper (BODY-HOST-AUTOACTUALIZAR-1): deploy/host/autoactualizar.sh, and how
duna-deploy-body and body-deploy.sh wire it in. Nothing here runs against /usr/local/sbin or
/etc: the helper's functions are sourced into a temp directory with its INSTALLED_*/PREV_* path
constants reassigned there, and `install`/`visudo` are stubbed on PATH -- never the real root-only
coreutils invocation. Everything else is a text-level assertion on the scripts as written."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "deploy" / "host"

HELPER = HOST_DIR / "autoactualizar.sh"
ENTRY = HOST_DIR / "duna-deploy-body"
PRODUCT = HOST_DIR / "body-deploy.sh"
SUDOERS = HOST_DIR / "duna-deploy-body.sudoers"

_BASH_TIMEOUT_S = 300


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _to_msys_path(path: Path) -> str:
    text = str(path).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _run_bash(script_text: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "run.sh"
        script_path.write_text(script_text, encoding="utf-8", newline="\n")
        try:
            return subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True,
                timeout=_BASH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise AssertionError(
                f"bash did not finish inside _BASH_TIMEOUT_S={_BASH_TIMEOUT_S}s; "
                f"stderr so far: {e.stderr!r}"
            ) from e


class BashHarness(unittest.TestCase):
    """Shared scaffolding: a temp dir with INSTALLED_*/PREV_* reassigned into it, and stub
    install/visudo on PATH so no real root action is ever attempted."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)

        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self._write_stub_install()
        self._write_stub_visudo(accept=True)

        self.installed_script = self.tmp / "installed_duna_deploy_body"
        self.installed_sudoers = self.tmp / "installed_sudoers"
        self.prev_script = self.tmp / "installed_duna_deploy_body.prev"
        self.prev_sudoers = self.tmp / ".installed_sudoers.prev"

    def _write_stub_install(self):
        stub = self.fakebin / "install"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "mode=\"\"\n"
            "args=()\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  case \"$1\" in\n"
            "    -o|-g) shift 2 ;;\n"
            "    -m) mode=\"$2\"; shift 2 ;;\n"
            "    *) args+=(\"$1\"); shift ;;\n"
            "  esac\n"
            "done\n"
            "src=\"${args[0]}\"\n"
            "dst=\"${args[1]}\"\n"
            "cp \"$src\" \"$dst\"\n"
            "[[ -n \"$mode\" ]] && chmod \"$mode\" \"$dst\"\n"
            "echo \"STUB_INSTALL mode=$mode src=$src dst=$dst\" >> \""
            + _to_msys_path(self.tmp / "install.log")
            + "\"\n",
            encoding="utf-8",
            newline="\n",
        )
        stub.chmod(0o755)

    def _write_stub_visudo(self, accept: bool):
        stub = self.fakebin / "visudo"
        body = "exit 0\n" if accept else "echo stub-visudo-refused >&2\nexit 1\n"
        stub.write_text(
            "#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n"
        )
        stub.chmod(0o755)

    def _preamble(self) -> str:
        return (
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            'export PATH="' + _to_msys_path(self.fakebin) + ':$PATH"\n'
            'source "' + _to_msys_path(HELPER) + '"\n'
            'INSTALLED_SCRIPT_PATH="' + _to_msys_path(self.installed_script) + '"\n'
            'INSTALLED_SUDOERS_PATH="' + _to_msys_path(self.installed_sudoers) + '"\n'
            'PREV_SCRIPT_PATH="' + _to_msys_path(self.prev_script) + '"\n'
            'PREV_SUDOERS_PATH="' + _to_msys_path(self.prev_sudoers) + '"\n'
        )

    def _valid_sudoers_text(self) -> str:
        return "carlos ALL=(root) NOPASSWD: " + _to_msys_path(self.installed_script) + "\n"

    def _write_repo_candidates(self, script_text="script-v1\n", sudoers_text=None):
        if sudoers_text is None:
            sudoers_text = self._valid_sudoers_text()
        repo_script = self.tmp / "repo_duna_deploy_body"
        repo_sudoers = self.tmp / "repo_sudoers"
        repo_script.write_text(script_text, encoding="utf-8", newline="\n")
        repo_sudoers.write_text(sudoers_text, encoding="utf-8", newline="\n")
        return repo_script, repo_sudoers


class ValidarSudoersCandidatoTest(BashHarness):
    """Pure string logic: no install/visudo stub ever needs to fire for these. Deliberately does
    NOT override INSTALLED_SCRIPT_PATH, so the real repo sudoers file (which names the real
    install path) can be checked exactly as it would be on the host."""

    def _real_path_preamble(self) -> str:
        return (
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            'source "' + _to_msys_path(HELPER) + '"\n'
        )

    def _validar(self, content: str):
        candidate = self.tmp / "candidate"
        candidate.write_text(content, encoding="utf-8", newline="\n")
        script = (
            self._real_path_preamble()
            + 'if validar_sudoers_candidato "' + _to_msys_path(candidate) + '"; then\n'
            + '  echo ACCEPTED\n'
            + 'else\n'
            + '  echo "REFUSED: $(validar_sudoers_candidato \"' + _to_msys_path(candidate) + '\")"\n'
            + 'fi\n'
        )
        return _run_bash(script)

    def test_accepts_the_repos_own_sudoers_file(self):
        result = self._validar(_read(SUDOERS))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACCEPTED", result.stdout)

    def test_refuses_two_rules(self):
        content = (
            "carlos ALL=(root) NOPASSWD: /usr/local/sbin/duna-deploy-body\n"
            "carlos ALL=(root) NOPASSWD: /usr/local/sbin/duna-deploy-body\n"
        )
        result = self._validar(content)
        self.assertIn("REFUSED", result.stdout)

    def test_refuses_an_argument_after_the_path(self):
        content = "carlos ALL=(root) NOPASSWD: /usr/local/sbin/duna-deploy-body deploy\n"
        result = self._validar(content)
        self.assertIn("REFUSED", result.stdout)

    def test_refuses_all_in_the_command_position(self):
        content = "carlos ALL=(root) NOPASSWD: ALL\n"
        result = self._validar(content)
        self.assertIn("REFUSED", result.stdout)

    def test_refuses_a_defaults_line(self):
        content = "Defaults requiretty\n"
        result = self._validar(content)
        self.assertIn("REFUSED", result.stdout)

    def test_refuses_another_path(self):
        content = "carlos ALL=(root) NOPASSWD: /usr/local/sbin/some-other-script\n"
        result = self._validar(content)
        self.assertIn("REFUSED", result.stdout)

    def test_ignores_blank_and_comment_lines(self):
        content = (
            "# a comment\n"
            "\n"
            "carlos ALL=(root) NOPASSWD: /usr/local/sbin/duna-deploy-body\n"
            "\n"
        )
        result = self._validar(content)
        self.assertIn("ACCEPTED", result.stdout)


class AutoactualizarComparaTest(BashHarness):
    def test_both_unchanged_sets_no_flags(self):
        self.installed_script.write_text("same\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text(_read(SUDOERS), encoding="utf-8", newline="\n")
        repo_script, repo_sudoers = self._write_repo_candidates(
            script_text="same\n", sudoers_text=_read(SUDOERS)
        )
        script = (
            self._preamble()
            + 'autoactualizar_comparar "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
            + 'echo "SCRIPT=[$SCRIPT_CHANGED] SUDOERS=[$SUDOERS_CHANGED]"\n'
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SCRIPT=[] SUDOERS=[]", result.stdout)

    def test_changed_script_sets_only_script_flag(self):
        self.installed_script.write_text("old\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text(_read(SUDOERS), encoding="utf-8", newline="\n")
        repo_script, repo_sudoers = self._write_repo_candidates(
            script_text="new\n", sudoers_text=_read(SUDOERS)
        )
        script = (
            self._preamble()
            + 'autoactualizar_comparar "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
            + 'echo "SCRIPT=[$SCRIPT_CHANGED] SUDOERS=[$SUDOERS_CHANGED]"\n'
        )
        result = _run_bash(script)
        self.assertIn("SCRIPT=[1] SUDOERS=[]", result.stdout)

    def test_missing_installed_copy_counts_as_changed(self):
        repo_script, repo_sudoers = self._write_repo_candidates()
        script = (
            self._preamble()
            + 'autoactualizar_comparar "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
            + 'echo "SCRIPT=[$SCRIPT_CHANGED] SUDOERS=[$SUDOERS_CHANGED]"\n'
        )
        result = _run_bash(script)
        self.assertIn("SCRIPT=[1] SUDOERS=[1]", result.stdout)


class AutoactualizarInstalarTest(BashHarness):
    def test_up_to_date_installs_nothing(self):
        self.installed_script.write_text("same\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text(_read(SUDOERS), encoding="utf-8", newline="\n")
        repo_script, repo_sudoers = self._write_repo_candidates(
            script_text="same\n", sudoers_text=_read(SUDOERS)
        )
        script = (
            self._preamble()
            + 'autoactualizar_instalar "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("entry script: up to date", result.stdout)
        self.assertFalse((self.tmp / "install.log").exists())

    def test_first_install_backs_up_nothing_but_installs_both(self):
        repo_script, repo_sudoers = self._write_repo_candidates()
        script = (
            self._preamble()
            + 'autoactualizar_instalar "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.installed_script.exists())
        self.assertTrue(self.installed_sudoers.exists())
        self.assertFalse(self.prev_script.exists())
        self.assertFalse(self.prev_sudoers.exists())
        log = (self.tmp / "install.log").read_text(encoding="utf-8")
        self.assertIn("mode=0755", log)
        self.assertIn("mode=0440", log)

    def test_changed_script_backs_up_the_old_one_at_mode_0440(self):
        self.installed_script.write_text("old\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text(_read(SUDOERS), encoding="utf-8", newline="\n")
        repo_script, repo_sudoers = self._write_repo_candidates(
            script_text="new\n", sudoers_text=_read(SUDOERS)
        )
        script = (
            self._preamble()
            + 'autoactualizar_instalar "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.prev_script.read_text(encoding="utf-8"), "old\n")
        self.assertEqual(self.installed_script.read_text(encoding="utf-8"), "new\n")
        log = (self.tmp / "install.log").read_text(encoding="utf-8")
        self.assertIn("mode=0440 src=" + _to_msys_path(self.installed_script), log)
        self.assertIn("mode=0755 src=" + _to_msys_path(repo_script), log)

    def test_invalid_sudoers_candidate_refuses_and_installs_nothing(self):
        self.installed_script.write_text("old\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text(_read(SUDOERS), encoding="utf-8", newline="\n")
        repo_script, repo_sudoers = self._write_repo_candidates(
            script_text="new\n", sudoers_text="carlos ALL=(root) NOPASSWD: ALL\n"
        )
        script = (
            self._preamble()
            + 'autoactualizar_instalar "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
        )
        result = _run_bash(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refused", result.stderr)
        self.assertEqual(self.installed_script.read_text(encoding="utf-8"), "old\n")
        self.assertFalse(self.prev_script.exists())
        self.assertFalse((self.tmp / "install.log").exists())

    def test_visudo_refusal_refuses_and_installs_nothing(self):
        self._write_stub_visudo(accept=False)
        self.installed_script.write_text("old\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text("old-sudoers\n", encoding="utf-8", newline="\n")
        repo_script, repo_sudoers = self._write_repo_candidates(script_text="new\n")
        script = (
            self._preamble()
            + 'autoactualizar_instalar "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
        )
        result = _run_bash(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("visudo", result.stderr)
        self.assertEqual(self.installed_script.read_text(encoding="utf-8"), "old\n")
        self.assertFalse((self.tmp / "install.log").exists())


class AutoactualizarPlanTest(BashHarness):
    def test_up_to_date(self):
        self.installed_script.write_text("same\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text(_read(SUDOERS), encoding="utf-8", newline="\n")
        repo_script, repo_sudoers = self._write_repo_candidates(
            script_text="same\n", sudoers_text=_read(SUDOERS)
        )
        script = (
            self._preamble()
            + 'autoactualizar_plan "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run: entry script: up to date", result.stdout)
        self.assertFalse((self.tmp / "install.log").exists())

    def test_changed_sudoers_would_pass(self):
        self.installed_script.write_text("same\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text(
            "carlos ALL=(root) NOPASSWD: /usr/local/sbin/some-other-script\n",
            encoding="utf-8", newline="\n",
        )
        repo_script, repo_sudoers = self._write_repo_candidates(script_text="same\n")
        script = (
            self._preamble()
            + 'autoactualizar_plan "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run: sudoers check: would pass", result.stdout)
        self.assertFalse((self.tmp / "install.log").exists())

    def test_changed_sudoers_would_fail_names_the_reason(self):
        self.installed_script.write_text("same\n", encoding="utf-8", newline="\n")
        self.installed_sudoers.write_text(
            "carlos ALL=(root) NOPASSWD: /usr/local/sbin/some-other-script\n",
            encoding="utf-8", newline="\n",
        )
        repo_script, repo_sudoers = self._write_repo_candidates(
            script_text="same\n", sudoers_text="carlos ALL=(root) NOPASSWD: ALL\n"
        )
        script = (
            self._preamble()
            + 'autoactualizar_plan "' + _to_msys_path(repo_script) + '" "'
            + _to_msys_path(repo_sudoers) + '"\n'
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run: sudoers check: would FAIL:", result.stdout)
        self.assertFalse((self.tmp / "install.log").exists())


class HelperConstantsTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(HELPER)

    def test_constants_verbatim(self):
        self.assertIn(
            'INSTALLED_SCRIPT_PATH="/usr/local/sbin/duna-deploy-body"', self.text
        )
        self.assertIn(
            'INSTALLED_SUDOERS_PATH="/etc/sudoers.d/duna-deploy-body"', self.text
        )
        self.assertIn(
            'PREV_SCRIPT_PATH="/usr/local/sbin/duna-deploy-body.prev"', self.text
        )
        self.assertIn(
            'PREV_SUDOERS_PATH="/etc/sudoers.d/.duna-deploy-body.prev"', self.text
        )

    def test_functions_defined(self):
        for fn in (
            "validar_sudoers_candidato",
            "autoactualizar_comparar",
            "autoactualizar_plan",
            "autoactualizar_instalar",
        ):
            self.assertIn(f"{fn}()", self.text.replace(" ", ""))


class SudoersInstallPathMatchesReadmeTest(unittest.TestCase):
    """The helper's INSTALLED_SUDOERS_PATH must be the same path the one-time install section of
    the README uses -- confirmed by reading it rather than assumed."""

    def test_helper_path_matches_readme_one_time_install(self):
        readme = _read(HOST_DIR / "README.md")
        self.assertIn(
            "sudo install -o root -g root -m 440 deploy/host/duna-deploy-body.sudoers "
            "/etc/sudoers.d/duna-deploy-body",
            readme,
        )
        self.assertIn(
            'INSTALLED_SUDOERS_PATH="/etc/sudoers.d/duna-deploy-body"', _read(HELPER)
        )


class SyntaxCheckTest(unittest.TestCase):
    def test_bash_n_on_all_three_scripts(self):
        for path in (HELPER, ENTRY, PRODUCT):
            result = subprocess.run(
                ["bash", "-n", _to_msys_path(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, f"{path.name}: {result.stderr}")


class WrapperPlacementTest(unittest.TestCase):
    """Text-level structural checks on the wrapper -- it refuses to run here (EUID != 0 on a dev
    box), so its self-update flow is verified by order, not by executing it."""

    def setUp(self):
        self.text = _read(ENTRY)

    def test_self_update_strictly_between_pull_and_final_exec(self):
        pull_idx = self.text.rindex('git -C "$REPO_DIR" pull --ff-only')
        source_idx = self.text.index(
            'source "$REPO_DIR/deploy/host/autoactualizar.sh"', pull_idx
        )
        final_exec_idx = self.text.rindex('exec /bin/bash "$PRODUCT_SCRIPT" --before-sha')
        self.assertLess(pull_idx, source_idx)
        self.assertLess(source_idx, final_exec_idx)

    def test_second_pass_skips_rev_parse_and_pull(self):
        internal_idx = self.text.index('if [[ -z "$AUTOACTUALIZADO_INTERNO" ]]; then')
        else_idx = self.text.index("else", internal_idx)
        fi_idx = self.text.index("\nfi\n", else_idx)
        guarded = self.text[internal_idx:else_idx]
        unguarded_after = self.text[else_idx:fi_idx]
        self.assertIn("rev-parse HEAD", guarded)
        self.assertIn("pull --ff-only", guarded)
        self.assertNotIn("rev-parse HEAD", unguarded_after)
        self.assertNotIn("pull --ff-only", unguarded_after)

    def test_autoactualizado_interno_absent_from_usage(self):
        start = self.text.index("usage: duna-deploy-body")
        end = self.text.index("USAGE", start)
        self.assertNotIn("--autoactualizado-interno", self.text[start:end])

    def test_internal_flags_require_each_other(self):
        self.assertIn(
            'if [[ -n "$AUTOACTUALIZADO_INTERNO" && -z "$CARRIED_BEFORE_SHA" ]]; then', self.text
        )
        self.assertIn(
            'if [[ -z "$AUTOACTUALIZADO_INTERNO" && -n "$CARRIED_BEFORE_SHA" ]]; then', self.text
        )

    def test_rollback_script_in_usage_and_case_arms(self):
        start = self.text.index("usage: duna-deploy-body")
        end = self.text.index("USAGE", start)
        self.assertIn("rollback-script", self.text[start:end])
        self.assertRegex(self.text, r"\n\s*rollback-script\)\n")

    def test_rollback_script_refuses_on_missing_or_bad_prev(self):
        start = self.text.index('if [[ "$cmd" == "rollback-script" ]]; then')
        end = self.text.index("\nfi\n", start)
        body = self.text[start:end]
        self.assertIn('[[ ! -f "$PREV_SCRIPT_PATH" || ! -f "$PREV_SUDOERS_PATH" ]]', body)
        self.assertIn("exit 1", body)
        self.assertIn("visudo -cf", body)
        self.assertIn('install -o root -g root -m 0755 "$PREV_SCRIPT_PATH"', body)
        self.assertIn('install -o root -g root -m 0440 "$PREV_SUDOERS_PATH"', body)

    def test_dry_run_sources_plan_only_never_instalar(self):
        dry_idx = self.text.index(
            'if [[ -n "$DRY_RUN" ]]; then', self.text.rindex("ensure_data_dir")
        )
        end_idx = self.text.index("\nfi\n", dry_idx)
        body = self.text[dry_idx:end_idx]
        self.assertIn("autoactualizar_plan", body)
        self.assertNotIn("autoactualizar_instalar", body)
        self.assertNotIn("install ", body)

    def test_self_reexec_carries_before_sha_and_internal_flag(self):
        self.assertIn(
            'exec "$INSTALLED_SCRIPT_PATH" deploy --autoactualizado-interno --before-sha '
            '"$before_sha"',
            self.text,
        )


class BootstrapPlacementTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(PRODUCT)

    def test_bootstrap_strictly_before_the_venv_block(self):
        owner_idx = self.text.index('owner="$(stat -c %U "$REPO_DIR")"')
        source_idx = self.text.index(
            'source "$REPO_DIR/deploy/host/autoactualizar.sh"', owner_idx
        )
        venv_idx = self.text.index("ensurepip")
        self.assertLess(owner_idx, source_idx)
        self.assertLess(source_idx, venv_idx)

    def test_no_exec_in_the_bootstrap(self):
        source_idx = self.text.index('source "$REPO_DIR/deploy/host/autoactualizar.sh"')
        venv_idx = self.text.index("ensurepip")
        bootstrap = self.text[source_idx:venv_idx]
        self.assertNotIn("exec", bootstrap)

    def test_bootstrap_calls_instalar_with_no_reexec_when_changed(self):
        source_idx = self.text.index('source "$REPO_DIR/deploy/host/autoactualizar.sh"')
        venv_idx = self.text.index("ensurepip")
        bootstrap = self.text[source_idx:venv_idx]
        self.assertIn("autoactualizar_comparar", bootstrap)
        self.assertIn("autoactualizar_instalar", bootstrap)
        self.assertIn("entry script: up to date", bootstrap)


if __name__ == "__main__":
    unittest.main()
