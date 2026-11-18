import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-daily-sync.ps1"


def _git(*args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _resolved(raw, base):
    if not os.path.isabs(raw):
        raw = os.path.join(str(base), raw)
    return Path(raw).resolve()


class InstallerWebTaskTest(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_web_task_has_at_logon_trigger(self):
        m = re.search(r'\$webTrigger\s*=\s*(.+)', self.text)
        self.assertIsNotNone(m, "no $webTrigger assignment found")
        self.assertIn("New-ScheduledTaskTrigger", m.group(1))
        self.assertIn("-AtLogOn", m.group(1))
        self.assertIn('Register-ScheduledTask -TaskName "Body app web"', self.text)
        self.assertIn("-Trigger $webTrigger", self.text)

    def test_lan_flag_only_inside_lan_branch(self):
        m = re.search(r'if\s*\(\$Lan\)\s*\{([^}]*)\}', self.text)
        self.assertIsNotNone(m, "no if ($Lan) branch found")
        self.assertIn("--lan", m.group(1))
        outside = self.text[: m.start()] + self.text[m.end():]
        self.assertNotIn("--lan", outside)

    def test_no_literal_lan_bind_address(self):
        self.assertNotIn("0.0.0.0", self.text)

    def test_no_absolute_personal_path(self):
        self.assertNotIn("C:\\Duna", self.text)
        self.assertNotIn("C:\\Users", self.text)

    def test_web_task_has_its_own_settings_object(self):
        self.assertIn("$webSettings", self.text)
        m = re.search(r'Register-ScheduledTask -TaskName "Body app web".*', self.text)
        self.assertIsNotNone(m)
        self.assertIn("-Settings $webSettings", m.group(0))
        self.assertNotIn("-Settings $settings", m.group(0))

    def test_remove_line_names_all_three_tasks(self):
        m = re.search(r'^# Remove with:.*$', self.text, re.MULTILINE)
        self.assertIsNotNone(m, "no '# Remove with:' comment line found")
        line = m.group(0)
        self.assertIn("Body app daily sync", line)
        self.assertIn("Body app weekly file", line)
        self.assertIn("Body app web", line)


class InstallerWakeCheckTaskTest(unittest.TestCase):
    """BODY-DESPERTAR-1: the wake check task -- 04:00, repeating every 20 minutes for 9 hours."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_task_registered(self):
        self.assertIn('Register-ScheduledTask -TaskName "Body app wake check"', self.text)

    def test_trigger_is_daily_at_0400(self):
        m = re.search(r'\$wakeTrigger\s*=\s*(.+)', self.text)
        self.assertIsNotNone(m, "no $wakeTrigger assignment found")
        trigger = m.group(1)
        self.assertIn("-Daily", trigger)
        self.assertIn('-At "04:00"', trigger)
        self.assertNotIn("-RepetitionInterval", trigger)
        self.assertNotIn("-RepetitionDuration", trigger)

    def test_repetition_copied_from_a_once_trigger_every_20_minutes_for_9_hours(self):
        m = re.search(r'\$wakeTrigger\.Repetition\s*=\s*(.+)', self.text)
        self.assertIsNotNone(m, "no $wakeTrigger.Repetition assignment found")
        repetition = m.group(1)
        self.assertIn("-Once", repetition)
        self.assertIn('-At "04:00"', repetition)
        self.assertIn("-RepetitionInterval", repetition)
        self.assertIn("New-TimeSpan -Minutes 20", repetition)
        self.assertIn("-RepetitionDuration", repetition)
        self.assertIn("New-TimeSpan -Hours 9", repetition)

    def test_no_recurring_trigger_combines_repetition_params_in_the_same_call(self):
        """The parameter-set rule that broke BODY-DESPERTAR-1: -Daily/-Weekly belong to a
        different parameter set than -RepetitionInterval/-RepetitionDuration, so no single
        New-ScheduledTaskTrigger call may carry both."""
        calls = re.findall(r"New-ScheduledTaskTrigger[^\n]*", self.text)
        self.assertTrue(calls, "no New-ScheduledTaskTrigger calls found")
        for call in calls:
            is_recurring = "-Daily" in call or "-Weekly" in call
            has_repetition = "-RepetitionInterval" in call or "-RepetitionDuration" in call
            self.assertFalse(
                is_recurring and has_repetition,
                f"-Daily/-Weekly combined with repetition params in the same call: {call}",
            )

    def test_action_runs_the_despertar_task(self):
        m = re.search(r'\$wakeAction\s*=\s*(.+)', self.text)
        self.assertIsNotNone(m, "no $wakeAction assignment found")
        self.assertIn("-m body.vivo despertar", m.group(1))

    def test_remove_line_names_the_wake_check(self):
        linea = next(l for l in self.text.splitlines() if l.startswith("# Remove with:"))
        self.assertIn("Body app wake check", linea)


class InstallerLiveCheckoutTest(unittest.TestCase):
    """BODY-LIVE-CHECKOUT-1: every task action runs through body.vivo from -LiveDir."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def _actions(self):
        return re.findall(r"New-ScheduledTaskAction[^\n]*", self.text)

    def test_every_action_runs_through_body_vivo(self):
        actions = self._actions()
        self.assertTrue(actions, "no New-ScheduledTaskAction calls found")
        self.assertIn('$webArgs = "-m body.vivo web', self.text)
        for action in actions:
            if "-Argument $webArgs" in action:
                continue
            self.assertIn("-m body.vivo ", action, f"not routed through body.vivo: {action}")
            self.assertNotRegex(action, r"-m body\.(?!vivo)\w+",
                                 f"action bypasses body.vivo: {action}")

    def test_every_action_working_directory_is_livedir(self):
        actions = self._actions()
        self.assertTrue(actions)
        for action in actions:
            self.assertIn("-WorkingDirectory $LiveDir", action,
                           f"action does not use $LiveDir: {action}")

    def test_livedir_default_has_no_absolute_personal_path(self):
        m = re.search(r'if\s*\(-not\s*\$LiveDir\)\s*\{\s*\$LiveDir\s*=\s*"([^"]+)"', self.text)
        self.assertIsNotNone(m, "no default $LiveDir assignment found")
        self.assertNotIn("C:\\", m.group(1))

    def test_worktree_added_detached(self):
        self.assertIn("git -C $repo worktree add --detach $LiveDir main", self.text)

    def test_no_absolute_personal_path(self):
        self.assertNotIn("C:\\Duna", self.text)
        self.assertNotIn("C:\\Users", self.text)


class InstallerWorktreeReuseTest(unittest.TestCase):
    """BODY-LIVE-CHECKOUT-1-M: the reuse decision asks git, not a text match on the path."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_no_longer_matches_worktree_list_with_match(self):
        self.assertNotIn("worktree list --porcelain", self.text)
        self.assertNotIn("-match [regex]::Escape($LiveDir)", self.text)

    def test_reuse_decision_uses_rev_parse_git_common_dir(self):
        self.assertIn("git -C $LiveDir rev-parse --git-common-dir", self.text)
        self.assertIn('Resolve-Path -LiteralPath "$repo\\.git"', self.text)

    def test_refuses_a_livedir_that_is_not_this_repos_worktree(self):
        start = self.text.index("if (Test-Path -LiteralPath $LiveDir)")
        close = self.text.index("\n} else {", start)
        block = self.text[start:close]
        self.assertIn("exit 1", block)
        self.assertIn("worktree add", self.text[close:])


class InstallerCommuteUploadTaskTest(unittest.TestCase):
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 6: -CommuteFile registers one more hosted daily task,
    right after the shift upload; absent, no such task is registered and the installer says so."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_commute_file_param_exists(self):
        self.assertIn("[string]$CommuteFile", self.text)

    def test_commute_action_runs_subir_desplazamiento_with_the_file(self):
        m = re.search(r'\$commuteAction\s*=\s*(.+)', self.text)
        self.assertIsNotNone(m, "no $commuteAction assignment found")
        self.assertIn("-m body.vivo subir_desplazamiento", m.group(1))
        self.assertIn('--file `"$CommuteFile`"', m.group(1))

    def test_commute_task_registered_inside_an_if_commutefile_guard(self):
        self.assertIn("if ($CommuteFile) {", self.text)
        start = self.text.index("if ($CommuteFile) {")
        close = self.text.index("\n    }", start)
        block = self.text[start:close]
        self.assertIn('Register-ScheduledTask -TaskName "Body app commute upload"', block)

    def test_commute_task_registered_right_after_the_shift_upload(self):
        upload_at = self.text.index('Register-ScheduledTask -TaskName "Body app shift upload"')
        commute_at = self.text.index('Register-ScheduledTask -TaskName "Body app commute upload"')
        fetch_at = self.text.index('Register-ScheduledTask -TaskName "Body app weekly fetch"')
        self.assertLess(upload_at, commute_at)
        self.assertLess(commute_at, fetch_at)

    def test_installer_says_in_one_line_when_commutefile_is_absent(self):
        self.assertIn("Sin subida de desplazamiento", self.text)


class InstallerLocalCommuteImportTest(unittest.TestCase):
    """BODY-DESPLAZAMIENTO-LOCAL-1: -CommuteFile on the local (non -Hosted) branch adds a
    commute import action to "Body app daily sync", right after the shift_import action."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def _daily_actions_block(self):
        start = self.text.index('$dailyActions = @(New-ScheduledTaskAction')
        close = self.text.index(
            'Register-ScheduledTask -TaskName "Body app daily sync"', start
        )
        return self.text[start:close]

    def test_commute_action_present_when_commutefile_given(self):
        block = self._daily_actions_block()
        self.assertIn("if ($CommuteFile) {", block)
        self.assertIn("-m body.vivo importar_desplazamiento", block)
        self.assertIn('--file `"$CommuteFile`"', block)
        self.assertIn("-WorkingDirectory $LiveDir", block)

    def test_commute_action_comes_after_shift_import_action(self):
        block = self._daily_actions_block()
        shift_at = block.index("if ($ShiftsDir) {")
        commute_at = block.index("if ($CommuteFile) {")
        self.assertLess(shift_at, commute_at)

    def test_commute_action_absent_without_commutefile_guard(self):
        """The action line itself only ever runs inside the if ($CommuteFile) guard."""
        block = self._daily_actions_block()
        guard_start = block.index("if ($CommuteFile) {")
        guard_close = block.index("\n}", guard_start)
        guard_body = block[guard_start:guard_close]
        outside = block[:guard_start] + block[guard_close:]
        self.assertIn("importar_desplazamiento", guard_body)
        self.assertNotIn("importar_desplazamiento", outside)

    def _local_commute_msg_assignment(self):
        matches = re.findall(r'\$commuteMsg\s*=\s*(.+)', self.text)
        self.assertEqual(len(matches), 2, "expected one hosted and one local $commuteMsg assignment")
        return matches[1]

    def test_local_summary_reports_commute_on_with_path(self):
        self.assertIn("on, from $CommuteFile", self._local_commute_msg_assignment())

    def test_local_summary_reports_commute_off_with_how_to_turn_it_on(self):
        self.assertIn("pass -CommuteFile to turn it on", self._local_commute_msg_assignment())

    def test_write_host_includes_commute_message(self):
        write_host_line = next(
            l for l in self.text.splitlines() if l.strip().startswith('Write-Host "Registered:')
        )
        self.assertIn("$commuteMsg", write_host_line)

    def test_header_has_a_local_commute_example(self):
        self.assertIn("Example with commute import on:", self.text)
        self.assertIn("-CommuteFile", self.text.split("-Hosted:")[0])


class InstallerMainTreeGuardTest(unittest.TestCase):
    """BODY-INSTALL-DESDE-VIVO-1 ruling 1: $repo must be the main tree -- checked before
    $LiveDir defaults, before any worktree add, and before the first scheduled task."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_uses_git_dir_and_git_common_dir_on_repo(self):
        self.assertIn("git -C $repo rev-parse --git-dir", self.text)
        self.assertIn("git -C $repo rev-parse --git-common-dir", self.text)

    def test_check_appears_before_livedir_default(self):
        check_at = self.text.index("git -C $repo rev-parse --git-dir")
        default_at = self.text.index('if (-not $LiveDir) { $LiveDir = "$repo-live" }')
        self.assertLess(check_at, default_at)

    def test_check_appears_before_worktree_add(self):
        check_at = self.text.index("git -C $repo rev-parse --git-dir")
        worktree_at = self.text.index("git -C $repo worktree add --detach")
        self.assertLess(check_at, worktree_at)

    def test_check_appears_before_first_register_scheduled_task(self):
        check_at = self.text.index("git -C $repo rev-parse --git-dir")
        register_at = self.text.index("Register-ScheduledTask -TaskName")
        self.assertLess(check_at, register_at)

    def _mismatch_block(self):
        start = self.text.index("git -C $repo rev-parse --git-dir")
        close = self.text.index('if (-not $LiveDir) { $LiveDir = "$repo-live" }')
        return self.text[start:close]

    def test_mismatch_branch_prints_one_line_and_exits_1(self):
        block = self._mismatch_block()
        self.assertIn("exit 1", block)
        self.assertIn("arbol vivo", block)
        self.assertIn("install-daily-sync.ps1", block)

    def test_no_absolute_personal_path(self):
        block = self._mismatch_block()
        self.assertNotIn("C:\\Duna", block)
        self.assertNotIn("C:\\Users", block)


class InstallerPyExistsGuardTest(unittest.TestCase):
    """BODY-INSTALL-DESDE-VIVO-1 ruling 2: $py must exist before any scheduled task is
    registered on it."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_checks_py_exists(self):
        self.assertIn("if (-not (Test-Path -LiteralPath $py))", self.text)

    def test_check_appears_before_first_register_scheduled_task(self):
        check_at = self.text.index("if (-not (Test-Path -LiteralPath $py))")
        register_at = self.text.index("Register-ScheduledTask -TaskName")
        self.assertLess(check_at, register_at)

    def _missing_block(self):
        start = self.text.index("if (-not (Test-Path -LiteralPath $py))")
        close = self.text.index("$pyw = ", start)
        return self.text[start:close]

    def test_missing_branch_names_py_path_and_exits_1(self):
        block = self._missing_block()
        self.assertIn("exit 1", block)
        self.assertIn("$py", block)

    def test_no_absolute_personal_path(self):
        block = self._missing_block()
        self.assertNotIn("C:\\Duna", block)
        self.assertNotIn("C:\\Users", block)


class InstallerMainTreeVsWorktreeGitBehaviourTest(unittest.TestCase):
    """BODY-INSTALL-DESDE-VIVO-1: proves the two git commands the installer compares
    actually differ on a worktree and match on the main tree -- never the script itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git("init", "-q", cwd=self.repo)
        _git("config", "user.email", "test@example.invalid", cwd=self.repo)
        _git("config", "user.name", "Test", cwd=self.repo)
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        _git("add", "a.txt", cwd=self.repo)
        _git("commit", "-q", "-m", "initial", cwd=self.repo)
        self.worktree = Path(self.tmp.name) / "repo-live"
        _git("worktree", "add", "--detach", str(self.worktree), "HEAD", cwd=self.repo)

    def test_main_tree_git_dir_and_common_dir_match(self):
        git_dir = _git("rev-parse", "--git-dir", cwd=self.repo)
        common_dir = _git("rev-parse", "--git-common-dir", cwd=self.repo)
        self.assertEqual(
            _resolved(git_dir, self.repo),
            _resolved(common_dir, self.repo),
        )

    def test_worktree_git_dir_and_common_dir_differ(self):
        git_dir = _git("rev-parse", "--git-dir", cwd=self.worktree)
        common_dir = _git("rev-parse", "--git-common-dir", cwd=self.worktree)
        self.assertNotEqual(
            _resolved(git_dir, self.worktree),
            _resolved(common_dir, self.worktree),
        )


if __name__ == "__main__":
    unittest.main()
