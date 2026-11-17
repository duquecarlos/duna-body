"""The importar-pc/personas verbs on deploy/host/duna-deploy-body, and the README section they
need (BODY-MUDANZA-HOST-VERBO-1). Named test_deploy_host.py, distinct from the existing
test_host_deploy.py, so this slice's diff stays inside its own declared `touches`. Nothing here
executes a shell script; everything is a text-level assertion."""
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "deploy" / "host"

ENTRY = HOST_DIR / "duna-deploy-body"
SUDOERS = HOST_DIR / "duna-deploy-body.sudoers"
README = HOST_DIR / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(ENTRY)

    def test_usage_lists_both_new_verbs(self):
        self.assertIn("importar-pc --cuenta-sub <sub> --archivo <path> [--apply]", self.text)
        self.assertIn("personas", self.text)

    def test_no_carlos_hardcoded(self):
        self.assertNotIn("carlos", self.text.lower())


class CaseArmsTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(ENTRY)

    def test_importar_pc_case_present(self):
        self.assertRegex(self.text, r"\n\s*importar-pc\)\n")

    def test_personas_case_present(self):
        self.assertRegex(self.text, r"\n\s*personas\)\n")

    def test_personas_rejects_arguments(self):
        start = self.text.index("\n  personas)")
        end = self.text.index(";;", start)
        body = self.text[start:end]
        self.assertIn("$# -ne 0", body)
        self.assertIn("exit 2", body)


class ExecDispatchTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(ENTRY)

    def test_product_import_script_constant_present(self):
        self.assertIn(
            'PRODUCT_IMPORT_SCRIPT="$REPO_DIR/deploy/host/body-import-pc.sh"', self.text
        )

    def test_importar_pc_execs_with_forwarded_args(self):
        self.assertIn('exec /bin/bash "$PRODUCT_IMPORT_SCRIPT" importar-pc "$@"', self.text)

    def test_personas_execs_with_no_forwarded_args(self):
        self.assertIn('exec /bin/bash "$PRODUCT_IMPORT_SCRIPT" personas', self.text)

    def test_import_dispatch_never_pulls(self):
        import_idx = self.text.index('exec /bin/bash "$PRODUCT_IMPORT_SCRIPT" importar-pc')
        pull_idx = self.text.index("pull --ff-only")
        self.assertLess(import_idx, pull_idx)


class SudoersUnchangedTest(unittest.TestCase):
    def test_sudoers_still_allows_the_whole_script_not_per_verb(self):
        text = _read(SUDOERS)
        lines = [l for l in text.splitlines() if l.strip() != ""]
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            lines[0], "carlos ALL=(root) NOPASSWD: /usr/local/sbin/duna-deploy-body"
        )
        self.assertNotIn("importar-pc", text)
        self.assertNotIn("personas", text)


class ReadmeImportSectionTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(README)

    def test_section_present_with_both_verbs(self):
        self.assertIn("Importar desde el PC", self.text)
        self.assertIn("importar-pc --cuenta-sub", self.text)
        self.assertIn("duna-deploy-body personas", self.text)

    def test_steps_are_in_order(self):
        section_idx = self.text.index("## Importar desde el PC")
        next_section_idx = self.text.index("## Host facts")
        section = self.text[section_idx:next_section_idx]
        self.assertNotIn(
            "sudo install -o root -g root -m 755 deploy/host/duna-deploy-body", section,
            "the hand-reinstall step should be gone now the wrapper self-updates",
        )
        scp_idx = section.index("scp pc-snapshot.sqlite")
        personas_idx = section.index("duna-deploy-body personas")
        dry_run_idx = section.index(
            "importar-pc --cuenta-sub <sub> --archivo <staging-path>\n"
        )
        apply_idx = section.index(
            "importar-pc --cuenta-sub <sub> --archivo <staging-path> --apply"
        )
        healthz_idx = section.index("/healthz")
        garmin_idx = section.index("garmin/conectar")
        self.assertLess(scp_idx, personas_idx)
        self.assertLess(personas_idx, dry_run_idx)
        self.assertLess(dry_run_idx, apply_idx)
        self.assertLess(apply_idx, healthz_idx)
        self.assertLess(healthz_idx, garmin_idx)

    def test_no_real_values_only_placeholders(self):
        section_idx = self.text.index("## Importar desde el PC")
        next_section_idx = self.text.index("## Host facts")
        section = self.text[section_idx:next_section_idx]
        self.assertIn("<sub>", section)
        self.assertIn("<staging-path>", section)
        self.assertIn("<host-user>", section)
        self.assertNotIn("sub-", section)

    def test_rollback_paragraph_present_and_honest_about_no_restore_command(self):
        section_idx = self.text.index("## Importar desde el PC")
        next_section_idx = self.text.index("## Host facts")
        section = self.text[section_idx:next_section_idx]
        self.assertIn("Rollback", section)
        self.assertIn("rollback snapshot", section)
        self.assertIn("no restore command yet", section)
        self.assertIn("/srv/duna/data/body/body.db", section)


if __name__ == "__main__":
    unittest.main()
