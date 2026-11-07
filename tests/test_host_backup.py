"""Off-host backup (BODY-HOST-BACKUP-1): text-level checks on the shell side, mirroring
test_host_deploy.py's style. Nothing here executes a .sh file or the entry script."""
import re
import subprocess
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "deploy" / "host"

ENTRY = HOST_DIR / "duna-deploy-body"
DEPLOY = HOST_DIR / "body-deploy.sh"
BACKUP = HOST_DIR / "body-backup.sh"
BACKUP_UNIT = HOST_DIR / "body-backup.service"
BACKUP_TIMER = HOST_DIR / "body-backup.timer"
README = HOST_DIR / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class EntryAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(ENTRY)

    def test_usage_lists_backup_with_dry_run(self):
        self.assertIn("backup [--dry-run]", self.text)

    def test_backup_case_present(self):
        self.assertRegex(self.text, r"\n\s*backup\)\n")

    def test_backup_execs_body_backup_script(self):
        self.assertIn('PRODUCT_BACKUP_SCRIPT="$REPO_DIR/deploy/host/body-backup.sh"', self.text)
        self.assertIn('exec /bin/bash "$PRODUCT_BACKUP_SCRIPT"', self.text)
        self.assertIn('exec /bin/bash "$PRODUCT_BACKUP_SCRIPT" --dry-run', self.text)

    def test_backup_never_pulls(self):
        backup_exec_idx = self.text.index('exec /bin/bash "$PRODUCT_BACKUP_SCRIPT"')
        pull_idx = self.text.index("pull --ff-only")
        self.assertLess(backup_exec_idx, pull_idx)

    def test_status_includes_backup_timer_and_last_run(self):
        self.assertIn("body-backup.timer", self.text)
        self.assertIn("ExecMainExitTimestamp", self.text)

    def test_no_carlos_hardcoded(self):
        self.assertNotIn("carlos", self.text.lower())


class BackupScriptBasicsTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(BACKUP)

    def test_euid_check_before_anything_else(self):
        set_idx = self.text.index("set -Eeuo pipefail")
        euid_idx = self.text.index("EUID")
        self.assertGreater(euid_idx, set_idx)

    def test_path_reset_present(self):
        self.assertIn('PATH="/usr/sbin:/usr/bin:/sbin:/bin"', self.text)

    def test_age_refusal_names_apt_install(self):
        self.assertIn("age is missing: run once on the host: sudo apt install age", self.text)

    def test_run_helper_defined(self):
        self.assertIn("run()", self.text.replace(" ", ""))

    def test_no_carlos_hardcoded(self):
        self.assertNotIn("carlos", self.text.lower())


class EnvParsingNeverSourcesTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(BACKUP)

    def test_never_sources_or_cats_the_env_file(self):
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            if "ENV_FILE" in line or "body.env" in line:
                self.assertNotRegex(
                    line, r"(^|[;&|]|\s)source\s", f"line {lineno} sources the env file: {line}"
                )
                self.assertNotRegex(
                    line, r"(^|[;&|]|\s)\.\s+\$?ENV_FILE", f"line {lineno} dot-sources the env file: {line}"
                )
                self.assertNotIn("cat ", line, f"line {lineno} cats the env file: {line}")

    def test_never_echoes_a_secret_named_variable(self):
        forbidden = re.compile(
            r"\b(echo|printf)\b[^\n]*\$\{?(access_key|secret_key|recipient|BODY_R2_ACCESS_KEY_ID|BODY_R2_SECRET_ACCESS_KEY)\b"
        )
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            if "printf 'user = " in line:
                continue  # the one place a value is used, piped straight into curl's stdin, never echoed
            self.assertNotRegex(line, forbidden, f"line {lineno} echoes a secret-named variable: {line}")


class CredentialsOnlyViaCurlConfigTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(BACKUP)

    def test_curl_uses_dash_k_dash(self):
        self.assertIn("-K -", self.text)

    def test_no_secret_variable_as_curl_argument(self):
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            if "curl" in line:
                self.assertNotIn("$access_key", line, f"line {lineno}: secret in curl argv: {line}")
                self.assertNotIn("$secret_key", line, f"line {lineno}: secret in curl argv: {line}")

    def test_aws_sigv4_present(self):
        self.assertIn("--aws-sigv4", self.text)
        self.assertIn("aws:amz:auto:s3", self.text)

    def test_curl_runs_quiet_with_errors_shown(self):
        curl_invocations = [
            line for line in self.text.splitlines() if "--aws-sigv4" in line
        ]
        self.assertEqual(len(curl_invocations), 2, "expected the real and dry-run curl lines")
        for line in curl_invocations:
            self.assertIn("-sS", line, f"curl missing -sS: {line}")

    def test_dry_run_line_also_shows_dash_s_dash_s(self):
        dry_run_line = next(
            line for line in self.text.splitlines() if "echo \"printf" in line
        )
        self.assertIn("-sS", dry_run_line)


class PlaintextDeletionTrapTest(unittest.TestCase):
    def test_trap_removes_plain_file(self):
        text = _read(BACKUP)
        func_idx = text.index("cleanup_plain() {")
        func_end = text.index("\n}\n", func_idx)
        body = text[func_idx:func_end]
        self.assertIn("rm -f", body)
        self.assertIn("$plain_file", body)
        trap_idx = text.index("trap cleanup_plain EXIT")
        self.assertGreater(trap_idx, func_idx)

    def test_trap_also_removes_the_encrypted_copy(self):
        text = _read(BACKUP)
        func_idx = text.index("cleanup_plain() {")
        func_end = text.index("\n}\n", func_idx)
        body = text[func_idx:func_end]
        self.assertIn("$enc_file", body)


class EnvValueQuoteStrippingTest(unittest.TestCase):
    def test_read_env_var_strips_one_pair_of_surrounding_quotes(self):
        text = _read(BACKUP)
        func_idx = text.index("read_env_var() {")
        func_end = text.index("\n}\n", func_idx)
        body = text[func_idx:func_end]
        self.assertIn('"$value" == \\"*\\"', body)
        self.assertIn("\"$value\" == \\'*\\'", body)
        self.assertNotIn("echo", body)
        self.assertNotIn("printf '%s\\n' \"$value\"", body)


class DeployInstallsBackupUnitsTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(DEPLOY)

    def test_units_array_includes_backup(self):
        self.assertIn("body-backup.service", self.text)
        self.assertIn("body-backup.timer", self.text)

    def test_enable_line_includes_backup_timer(self):
        m = re.search(r"systemctl enable[^\n]*", self.text)
        self.assertIsNotNone(m, "no systemctl enable line found")
        self.assertIn("body-backup.timer", m.group(0))


class BackupUnitsTest(unittest.TestCase):
    def test_service_is_oneshot_root(self):
        text = _read(BACKUP_UNIT)
        self.assertIn("Type=oneshot", text)
        self.assertIn("User=root", text)
        self.assertIn("ExecStart=/usr/local/sbin/duna-deploy-body backup", text)

    def test_timer_daily_0330_bogota(self):
        text = _read(BACKUP_TIMER)
        self.assertIn("OnCalendar=*-*-* 03:30:00 America/Bogota", text)
        self.assertIn("Persistent=true", text)


class ExecBitAndLineEndingTest(unittest.TestCase):
    def test_backup_script_executable_in_index(self):
        out = subprocess.run(
            ["git", "ls-files", "-s", "deploy/host/body-backup.sh"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        self.assertTrue(out.strip(), "body-backup.sh not tracked")
        mode = out.split()[0]
        self.assertEqual(mode, "100755", "body-backup.sh is not executable in the index")

    def test_new_host_files_are_lf(self):
        for path in (BACKUP, BACKUP_UNIT, BACKUP_TIMER):
            raw = path.read_bytes()
            self.assertNotIn(b"\r\n", raw, f"{path.name} has CRLF line endings")


class ReadmeMentionsBackupTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(README)

    def test_readme_no_longer_says_not_implemented(self):
        self.assertNotIn("is not implemented yet", self.text)

    def test_readme_names_lifecycle_rule_and_restore_check(self):
        self.assertIn("duna-respaldo", self.text)
        self.assertIn("lifecycle", self.text.lower())
        self.assertIn("restore", self.text.lower())
        self.assertIn("30 days", self.text)

    def test_readme_names_age_install_command(self):
        self.assertIn("sudo apt install age", self.text)


if __name__ == "__main__":
    unittest.main()
