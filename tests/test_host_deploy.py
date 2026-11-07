import re
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "deploy" / "host"

ENTRY = HOST_DIR / "duna-deploy-body"
PRODUCT = HOST_DIR / "body-deploy.sh"
SUDOERS = HOST_DIR / "duna-deploy-body.sudoers"
WEB_UNIT = HOST_DIR / "body-web.service"
PULL_UNIT = HOST_DIR / "body-pull.service"
PULL_TIMER = HOST_DIR / "body-pull.timer"
VIDA_UNIT = HOST_DIR / "body-vida.service"
VIDA_TIMER = HOST_DIR / "body-vida.timer"
CADDYFILE = HOST_DIR / "Caddyfile.body"
ENV_EXAMPLE = HOST_DIR / "body.env.example"
README = HOST_DIR / "README.md"

SCRIPTS = (ENTRY, PRODUCT)


def _read(path):
    return path.read_text(encoding="utf-8")


class EntryScriptBasicsTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(ENTRY)

    def test_usage_allowlist_present(self):
        self.assertIn("usage: duna-deploy-body", self.text)
        self.assertIn("status", self.text)
        self.assertIn("deploy [--dry-run]", self.text)

    def test_unknown_subcommand_exits_2(self):
        m = re.search(r"\*\)\s*\n\s*usage\s*\n\s*exit 2", self.text)
        self.assertIsNotNone(m, "no catch-all *) usage / exit 2 branch found")

    def test_no_args_exits_2(self):
        m = re.search(r"\$#\s*-lt\s*1.*\n\s*usage\s*\n\s*exit 2", self.text)
        self.assertIsNotNone(m, "no $# -lt 1 -> usage/exit 2 guard found")

    def test_euid_check_before_anything_else(self):
        set_idx = self.text.index("set -Eeuo pipefail")
        euid_idx = self.text.index("EUID")
        self.assertGreater(euid_idx, set_idx)
        between = self.text[set_idx + len("set -Eeuo pipefail"):euid_idx]
        self.assertNotIn("REPO_DIR", between)
        self.assertNotIn('PATH=', between)

    def test_path_reset_present(self):
        self.assertIn('PATH="/usr/sbin:/usr/bin:/sbin:/bin"', self.text)

    def test_repo_constant_present(self):
        self.assertIn('REPO_DIR="/srv/duna/repos/duna-body"', self.text)


class ProductScriptBasicsTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(PRODUCT)

    def test_repo_constant_present(self):
        self.assertIn('REPO_DIR="/srv/duna/repos/duna-body"', self.text)

    def test_path_reset_present(self):
        self.assertIn('PATH="/usr/sbin:/usr/bin:/sbin:/bin"', self.text)


class ExecBitTest(unittest.TestCase):
    def test_both_scripts_executable_in_git_index(self):
        import subprocess

        out = subprocess.run(
            ["git", "ls-files", "-s", "deploy/host/duna-deploy-body", "deploy/host/body-deploy.sh"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        modes = dict(
            (line.split()[3], line.split()[0]) for line in out.splitlines() if line.strip()
        )
        for path in ("deploy/host/duna-deploy-body", "deploy/host/body-deploy.sh"):
            self.assertEqual(modes.get(path), "100755", f"{path} is not executable in the index")


class NoHardcodedOwnerTest(unittest.TestCase):
    def test_owner_is_never_hardcoded_to_carlos(self):
        for path in SCRIPTS:
            text = _read(path)
            self.assertNotIn("carlos", text.lower())
            self.assertIn("owner=", text.replace(" ", ""), f"{path.name} does not read owner at runtime")


class GitOwnershipTest(unittest.TestCase):
    def test_every_git_dash_c_call_is_prefixed_by_runuser(self):
        for path in SCRIPTS:
            for lineno, line in enumerate(_read(path).splitlines(), start=1):
                unescaped = line.replace('\\"', '"')
                if "git -C" in unescaped:
                    self.assertIn(
                        'runuser -u "$owner" -- git -C', unescaped,
                        f"{path.name}:{lineno}: git call missing runuser -u \"$owner\" -- prefix: {line}",
                    )


class BeforeShaTest(unittest.TestCase):
    def test_entry_captures_before_sha_before_pull(self):
        text = _read(ENTRY)
        cap_idx = text.index("before_sha=")
        pull_idx = text.rindex("pull --ff-only")
        self.assertLess(cap_idx, pull_idx)

    def test_entry_passes_before_sha_to_exec(self):
        text = _read(ENTRY)
        self.assertIn('exec /bin/bash "$PRODUCT_SCRIPT" --before-sha "$before_sha"', text)

    def test_entry_execs_product_script(self):
        text = _read(ENTRY)
        self.assertTrue(
            any(
                line.strip().startswith('exec /bin/bash "$PRODUCT_SCRIPT"')
                for line in text.splitlines()
            ),
            "no bare 'exec /bin/bash \"$PRODUCT_SCRIPT\"' line found",
        )

    def test_exec_line_uses_bin_bash_not_the_exec_bit(self):
        text = _read(ENTRY)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("exec ") or '"exec ' in stripped:
                self.assertIn(
                    "/bin/bash", stripped,
                    f"exec line does not run the script through /bin/bash: {line}",
                )

    def test_product_validates_before_sha_is_40_hex(self):
        text = _read(PRODUCT)
        self.assertIn("[0-9a-f]{40}", text)
        self.assertIn("--before-sha", text)


class HealthCheckRetryTest(unittest.TestCase):
    def test_bare_sleep_1_then_single_curl_is_gone(self):
        text = _read(PRODUCT)
        self.assertNotIn(
            'sleep 1\nif ! curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then', text,
            "old single-shot 'sleep 1' + one curl check still present",
        )

    def test_retry_loop_bound_present(self):
        text = _read(PRODUCT)
        self.assertRegex(text, r"\bfor\b.*\b(1\s+20|20)\b")
        self.assertIn("health_ok", text)

    def test_health_check_succeeds_on_200_before_rollback(self):
        text = _read(PRODUCT)
        healthz_idx = text.index("HEALTH_URL")
        rollback_idx = text.rindex('rollback_and_fail "health check"')
        self.assertLess(healthz_idx, rollback_idx)
        self.assertIn("200", text)


class RollbackReinstallsDependenciesTest(unittest.TestCase):
    def test_pip_appears_inside_rollback_and_fail(self):
        text = _read(PRODUCT)
        start = text.index("rollback_and_fail() {")
        end = text.index("\n}\n", start)
        body = text[start:end]
        self.assertIn("pip", body, "rollback_and_fail does not reinstall dependencies")


class VenvCheckTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(PRODUCT)

    def test_check_precedes_venv_creation_and_names_python3_venv(self):
        check_idx = self.text.index("ensurepip")
        venv_idx = self.text.index("python3 -m venv")
        self.assertLess(check_idx, venv_idx)
        self.assertIn("python3-venv is missing", self.text)
        self.assertIn("sudo apt install python3-venv", self.text)

    def test_check_runs_as_owner(self):
        check_idx = self.text.index("ensurepip")
        preceding = self.text[max(0, check_idx - 80):check_idx]
        self.assertIn('runuser -u "$owner" --', preceding)

    def test_check_failure_does_not_restart_the_service(self):
        missing_idx = self.text.index("python3-venv is missing")
        exit_idx = self.text.index("exit 1", missing_idx)
        between = self.text[missing_idx:exit_idx]
        self.assertNotIn("systemctl", between)
        self.assertNotIn("rollback_and_fail", between)
        self.assertNotIn("fail_step", between)


class CaddyValidateBeforeReloadTest(unittest.TestCase):
    def test_validate_precedes_reload(self):
        text = _read(PRODUCT)
        validate_idx = text.index("caddy validate")
        reload_idx = text.rindex("systemctl reload caddy")
        self.assertLess(validate_idx, reload_idx)

    def test_validate_failure_restores_or_removes_and_does_not_reload(self):
        text = _read(PRODUCT)
        validate_idx = text.index("caddy validate")
        reload_idx = text.rindex("systemctl reload caddy")
        between = text[validate_idx:reload_idx]
        self.assertIn("rm", between)
        self.assertIn("$CADDY_DST", between)
        self.assertIn("mv", between)
        self.assertIn("$CADDY_BAK", between)
        self.assertIn("fail_step", between)


class InstallCaddySiteIfChangedTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(PRODUCT)

    def test_cmp_guard_precedes_install(self):
        cmp_idx = self.text.index("cmp -s")
        self.assertIn("$CADDY_SRC", self.text[cmp_idx:cmp_idx + 60])
        self.assertIn("$CADDY_DST", self.text[cmp_idx:cmp_idx + 60])
        install_idx = self.text.index('install -o root -g root -m 644 "$CADDY_SRC" "$CADDY_DST"')
        self.assertLess(cmp_idx, install_idx)

    def test_backup_made_before_install_and_before_validate(self):
        start = self.text.index("install_caddy_site_if_changed() {")
        end = self.text.index("\n}\n", start)
        body = self.text[start:end]
        backup_idx = body.index("$CADDY_BAK")
        install_idx = body.index('install -o root -g root -m 644 "$CADDY_SRC" "$CADDY_DST"')
        validate_idx = body.index("caddy validate")
        self.assertLess(backup_idx, install_idx)
        self.assertLess(install_idx, validate_idx)

    def test_reload_only_reachable_after_a_passing_validate(self):
        start = self.text.index("install_caddy_site_if_changed() {")
        end = self.text.index("\n}\n", start)
        body = self.text[start:end]
        validate_idx = body.index("caddy validate")
        fail_step_idx = body.index("fail_step", validate_idx)
        reload_idx = body.index("systemctl reload caddy")
        self.assertLess(fail_step_idx, reload_idx)

    def test_rollback_reuses_the_same_function(self):
        start = self.text.index("rollback_and_fail() {")
        end = self.text.index("\n}\n", start)
        body = self.text[start:end]
        self.assertIn("install_caddy_site_if_changed", body)


class ServiceHardeningTest(unittest.TestCase):
    def test_umask_and_protecthome_in_every_service(self):
        for path in (WEB_UNIT, PULL_UNIT, VIDA_UNIT):
            text = _read(path)
            self.assertIn("UMask=0077", text, f"{path.name} missing UMask=0077")
            self.assertIn("ProtectHome=yes", text, f"{path.name} missing ProtectHome=yes")


class NoShiftImportYetTest(unittest.TestCase):
    def test_pull_unit_runs_only_the_pull(self):
        text = _read(PULL_UNIT)
        self.assertNotIn("shift_import", text)
        exec_starts = [l for l in text.splitlines() if l.startswith("ExecStart=")]
        self.assertEqual(len(exec_starts), 1)
        self.assertIn("body.pull", exec_starts[0])

    def test_readme_says_so(self):
        text = _read(README)
        self.assertIn("shift import", text)
        self.assertIn("uploader", text)


class HonestCaddyCommentTest(unittest.TestCase):
    def test_caddyfile_states_the_confirmed_import_path(self):
        text = _read(CADDYFILE)
        self.assertIn("confirmed by Main", text)
        self.assertIn("/etc/caddy/sites/*.caddy", text)

    def test_readme_lists_hosts_facts_from_main(self):
        text = _read(README)
        for token in ("Ubuntu 24.04.4", "python3", "caddy", "duna-body", "python3-venv"):
            self.assertIn(token, text)
        self.assertNotIn("confirmed with Main", text)


class DryRunWrapperTest(unittest.TestCase):
    SIDE_EFFECT_CMDS = re.compile(r"\b(systemctl|install|useradd)\b")

    def test_no_bare_side_effecting_commands_outside_run(self):
        for path in SCRIPTS:
            for lineno, line in enumerate(_read(path).splitlines(), start=1):
                if "pip" in line and "install" in line:
                    continue  # pip's own subcommand, not the coreutils `install`
                if "is missing" in line:
                    continue  # the python3-venv refusal message, not a command
                for m in self.SIDE_EFFECT_CMDS.finditer(line):
                    prefix = line[: m.start()]
                    self.assertTrue(
                        prefix.rstrip().endswith("run"),
                        f"{path.name}:{lineno}: bare '{m.group(1)}' not wrapped by run(): {line}",
                    )

    def test_run_helper_defined_in_both_scripts(self):
        for path in SCRIPTS:
            self.assertIn("run()", _read(path).replace(" ", ""))


class EnvFileSecrecyTest(unittest.TestCase):
    def test_no_dump_of_env_file(self):
        forbidden = ("cat ", "echo ", "printf ", "source ")
        for path in SCRIPTS:
            for lineno, line in enumerate(_read(path).splitlines(), start=1):
                if "body.env" in line or "ENV_FILE" in line:
                    for f in forbidden:
                        self.assertNotIn(
                            f, line, f"{path.name}:{lineno}: dumps the env file: {line}"
                        )

    def test_no_pty_flag(self):
        for path in SCRIPTS:
            self.assertNotIn("--pty", _read(path))


class DataDirTest(unittest.TestCase):
    def test_data_dir_mode_0700_and_owner(self):
        text = _read(ENTRY)
        self.assertIn('DATA_DIR="/srv/duna/data/body"', text)
        self.assertIn('DATA_USER="duna-body"', text)
        self.assertIn('chmod 0700 "$DATA_DIR"', text)
        self.assertIn('chown "$DATA_USER:$DATA_USER" "$DATA_DIR"', text)

    def test_system_user_created_with_its_own_group(self):
        text = _read(ENTRY)
        useradd_idx = text.index("useradd --system")
        line_end = text.index("\n", useradd_idx)
        line = text[useradd_idx:line_end]
        self.assertIn("--user-group", line)

    def test_only_rm_touches_caddy_dst(self):
        for path in SCRIPTS:
            for lineno, line in enumerate(_read(path).splitlines(), start=1):
                if re.search(r"\brm\b", line):
                    self.assertIn(
                        "$CADDY_DST", line,
                        f"{path.name}:{lineno}: 'rm' found touching something other than "
                        f"$CADDY_DST: {line}",
                    )


class SudoersTest(unittest.TestCase):
    def test_exact_single_line(self):
        text = _read(SUDOERS)
        lines = [l for l in text.splitlines() if l.strip() != ""]
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            lines[0], "carlos ALL=(root) NOPASSWD: /usr/local/sbin/duna-deploy-body"
        )


class ReadmeTest(unittest.TestCase):
    def test_install_lines_reference_existing_files(self):
        text = _read(README)
        for fname in (
            "duna-deploy-body",
            "duna-deploy-body.sudoers",
            "Caddyfile.body",
            "body.env.example",
            "body-deploy.sh",
        ):
            self.assertIn(fname, text)
            self.assertTrue(
                (HOST_DIR / fname).exists(), f"{fname} referenced in README but missing"
            )


class SystemdUnitTest(unittest.TestCase):
    def test_web_unit(self):
        text = _read(WEB_UNIT)
        self.assertIn("User=duna-body", text)
        self.assertIn("EnvironmentFile=", text)
        self.assertIn("8770", text)
        self.assertNotIn("--lan", text)

    def test_pull_unit_and_timer(self):
        unit = _read(PULL_UNIT)
        self.assertIn("User=duna-body", unit)
        self.assertIn("EnvironmentFile=", unit)
        timer = _read(PULL_TIMER)
        self.assertIn("OnCalendar=", timer)
        self.assertIn("Persistent=true", timer)

    def test_vida_unit_and_timer(self):
        unit = _read(VIDA_UNIT)
        self.assertIn("User=duna-body", unit)
        self.assertIn("EnvironmentFile=", unit)
        timer = _read(VIDA_TIMER)
        self.assertIn("OnCalendar=", timer)
        self.assertIn("Persistent=true", timer)


class CaddyfileTest(unittest.TestCase):
    def test_proxies_and_blocks_backchannel(self):
        text = _read(CADDYFILE)
        self.assertIn("reverse_proxy 127.0.0.1:8770", text)
        self.assertIn("respond /login/cuenta/logout-token 404", text)

    def test_no_log_directive(self):
        text = _read(CADDYFILE)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertFalse(
                re.match(r"log\b", stripped), f"unexpected log directive: {line}"
            )


class EnvExampleTest(unittest.TestCase):
    NAMES = [
        "BODY_CUENTA_CLIENT_SECRET",
        "BODY_CUENTA_ISSUER",
        "BODY_PUBLIC_URL",
        "BODY_TOKEN_KEY",
        "BODY_BACKUP_AGE_RECIPIENT",
        "BODY_R2_ACCESS_KEY_ID",
        "BODY_R2_SECRET_ACCESS_KEY",
        "BODY_R2_ENDPOINT",
        "BODY_R2_BUCKET",
        "BODY_AGENTE_ANTHROPIC_API_KEY",
    ]
    FIXED = {
        "BODY_CUENTA_ISSUER": "https://cuenta.duna.solutions",
        "BODY_PUBLIC_URL": "https://body.duna.solutions",
        "BODY_R2_BUCKET": "duna-respaldo",
    }

    def test_every_name_present_and_values_blank_except_fixed(self):
        text = _read(ENV_EXAMPLE)
        assignments = dict(re.findall(r"^(BODY_[A-Z0-9_]+)=(.*)$", text, re.MULTILINE))
        for name in self.NAMES:
            self.assertIn(name, assignments, f"{name} missing from body.env.example")
        self.assertEqual(set(assignments) - set(self.NAMES), set())
        for name, value in assignments.items():
            if name in self.FIXED:
                self.assertEqual(value, self.FIXED[name])
            else:
                self.assertEqual(value, "", f"{name} has a non-blank value: {value!r}")


class LineEndingTest(unittest.TestCase):
    def test_every_file_under_deploy_host_is_lf(self):
        for path in HOST_DIR.iterdir():
            if path.is_file():
                raw = path.read_bytes()
                self.assertNotIn(b"\r\n", raw, f"{path.name} has CRLF line endings")


if __name__ == "__main__":
    unittest.main()
