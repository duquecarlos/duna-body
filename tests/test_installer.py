import re
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-daily-sync.ps1"


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


if __name__ == "__main__":
    unittest.main()
