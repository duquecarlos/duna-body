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


if __name__ == "__main__":
    unittest.main()
