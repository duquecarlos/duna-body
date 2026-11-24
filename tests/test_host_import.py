"""deploy/host/body-import-pc.sh (BODY-MUDANZA-HOST-VERBO-1): text-level checks, mirroring
test_host_backup.py's style. Nothing here executes the script; it is read as text."""
import unittest
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "deploy" / "host"
IMPORT = HOST_DIR / "body-import-pc.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class BasicsTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(IMPORT)

    def test_euid_check_before_anything_else(self):
        set_idx = self.text.index("set -Eeuo pipefail")
        euid_idx = self.text.index("EUID")
        self.assertGreater(euid_idx, set_idx)

    def test_path_reset_present(self):
        self.assertIn('PATH="/usr/sbin:/usr/bin:/sbin:/bin"', self.text)

    def test_no_carlos_hardcoded(self):
        self.assertNotIn("carlos", self.text.lower())

    def test_both_actions_present_in_case(self):
        self.assertRegex(self.text, r"\n\s*personas\)\n")
        self.assertRegex(self.text, r"\n\s*importar-pc\)\n")

    def test_garmin_persona_actions_present_in_case(self):
        """BODY-GARMIN-PERSONA-1, ruling 5."""
        self.assertRegex(self.text, r"\n\s*diagnostico-personas\)\n")
        self.assertRegex(self.text, r"\n\s*relinkar-garmin\)\n")

    def test_cobertura_action_present_in_case(self):
        """BODY-HISTORIA-MINUTO-2, ruling 2."""
        self.assertRegex(self.text, r"\n\s*cobertura\)\n")

    def test_detalle_historia_action_present_in_case(self):
        """BODY-DETALLE-HISTORIA-1, ruling 5."""
        self.assertRegex(self.text, r"\n\s*detalle-historia\)\n")


class PersonasActionTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(IMPORT)

    def test_personas_takes_no_arguments(self):
        start = self.text.index("personas)")
        end = self.text.index("importar-pc)", start)
        body = self.text[start:end]
        self.assertIn("personas takes no arguments", body)

    def test_personas_runs_as_data_user_via_runuser(self):
        start = self.text.index("personas)")
        end = self.text.index("importar-pc)", start)
        body = self.text[start:end]
        self.assertIn('runuser -u "$DATA_USER"', body)
        self.assertIn("body.mudanza personas", body)

    def test_personas_conteo_dispatches_to_cobertura_conteo(self):
        """BODY-HISTORIA-MINUTO-2, ruling 3: the plain (no-argument) personas mode is unchanged
        -- --conteo is the one extra argument this arm accepts."""
        start = self.text.index("personas)")
        end = self.text.index("importar-pc)", start)
        body = self.text[start:end]
        self.assertIn('"$1" == "--conteo"', body)
        self.assertIn("body.cobertura --conteo", body)
        self.assertIn("personas takes no arguments", body)


class DiagnosticoPersonasActionTest(unittest.TestCase):
    """BODY-GARMIN-PERSONA-1, ruling 5."""

    def setUp(self):
        self.text = _read(IMPORT)

    def test_diagnostico_personas_takes_no_arguments(self):
        start = self.text.index("diagnostico-personas)")
        end = self.text.index(";;", start)
        body = self.text[start:end]
        self.assertIn("diagnostico-personas takes no arguments", body)

    def test_diagnostico_personas_runs_as_data_user_via_runuser(self):
        start = self.text.index("diagnostico-personas)")
        end = self.text.index(";;", start)
        body = self.text[start:end]
        self.assertIn('runuser -u "$DATA_USER"', body)
        self.assertIn("body.persona_garmin diagnostico", body)


class CoberturaActionTest(unittest.TestCase):
    """BODY-HISTORIA-MINUTO-2, ruling 2: read-only -- no timer touched, no backup taken."""

    def setUp(self):
        self.text = _read(IMPORT)

    def _body(self) -> str:
        start = self.text.index("cobertura)")
        end = self.text.index("\n  importar-pc)", start)
        return self.text[start:end]

    def test_cuenta_sub_is_required(self):
        self.assertIn("cobertura requires --cuenta-sub", self._body())

    def test_runs_as_data_user_via_runuser(self):
        body = self._body()
        self.assertIn('runuser -u "$DATA_USER"', body)
        self.assertIn("body.cobertura --cuenta-sub", body)

    def test_json_flag_is_forwarded_when_given(self):
        body = self._body()
        self.assertIn("--json", body)

    def test_touches_no_timer_and_no_backup(self):
        body = self._body()
        self.assertNotIn("systemctl", body)
        self.assertNotIn("body-backup.sh", body)
        self.assertNotIn("body.backup snapshot", body)


class DetalleHistoriaActionTest(unittest.TestCase):
    """BODY-DETALLE-HISTORIA-1, ruling 5: mirrors cobertura's own arm shape."""

    def setUp(self):
        self.text = _read(IMPORT)

    def _body(self) -> str:
        start = self.text.index("detalle-historia)")
        end = self.text.index("\n  importar-pc)", start)
        return self.text[start:end]

    def test_cuenta_sub_is_required(self):
        self.assertIn("detalle-historia requires --cuenta-sub", self._body())

    def test_runs_as_data_user_via_runuser(self):
        body = self._body()
        self.assertIn('runuser -u "$DATA_USER"', body)
        self.assertIn("body.detalle_historia", body)

    def test_dry_run_and_max_flags_are_forwarded_when_given(self):
        body = self._body()
        self.assertIn("--dry-run", body)
        self.assertIn("--max", body)

    def test_touches_no_timer_and_no_backup(self):
        body = self._body()
        self.assertNotIn("systemctl", body)
        self.assertNotIn("body-backup.sh", body)
        self.assertNotIn("body.backup snapshot", body)


class RelinkarGarminActionTest(unittest.TestCase):
    """BODY-GARMIN-PERSONA-1, ruling 5."""

    def setUp(self):
        self.text = _read(IMPORT)

    def test_desde_and_hacia_required(self):
        self.assertIn("--desde and --hacia are required", self.text)

    def test_dry_run_never_passes_apply(self):
        text = self.text
        apply_check_idx = text.index('if [[ -z "$RELINKAR_APPLY" ]]; then')
        fi_idx = text.index("\n  fi\n", apply_check_idx)
        dry_run_body = text[apply_check_idx:fi_idx]
        self.assertNotIn('--apply"', dry_run_body)
        self.assertIn("body.persona_garmin relinkar", dry_run_body)
        self.assertNotIn("systemctl", dry_run_body)
        self.assertNotIn("body-backup.sh", dry_run_body)

    def test_apply_runs_the_existing_host_backup_before_the_move(self):
        text = self.text
        apply_check_idx = text.index('if [[ -z "$RELINKAR_APPLY" ]]; then')
        backup_idx = text.index('body-backup.sh"', apply_check_idx)
        apply_move_idx = text.rindex("body.persona_garmin relinkar")
        self.assertLess(apply_check_idx, backup_idx)
        self.assertLess(backup_idx, apply_move_idx)

    def test_refuses_and_never_moves_when_the_backup_fails(self):
        text = self.text
        backup_call_idx = text.index('if ! /bin/bash "$REPO_DIR/deploy/host/body-backup.sh"; then')
        exit_idx = text.index("exit 1", backup_call_idx)
        between = text[backup_call_idx:exit_idx]
        self.assertIn("refused", between)
        self.assertIn("did not run", between)
        apply_move_idx = text.rindex("body.persona_garmin relinkar")
        self.assertLess(exit_idx, apply_move_idx)

    def test_apply_move_runs_as_data_user_with_apply_flag(self):
        apply_move_idx = self.text.rindex("body.persona_garmin relinkar")
        block_end = self.text.index("\n\n", apply_move_idx)
        preceding = self.text[max(0, apply_move_idx - 300):apply_move_idx]
        block = self.text[apply_move_idx:block_end]
        self.assertIn('runuser -u "$DATA_USER"', preceding)
        self.assertIn("--apply", block)


class RequiredArgsTest(unittest.TestCase):
    def setUp(self):
        self.text = _read(IMPORT)

    def test_cuenta_sub_and_archivo_required(self):
        self.assertIn("--cuenta-sub and --archivo are required", self.text)

    def test_file_existence_checked(self):
        self.assertIn('[[ ! -f "$ARCHIVO" ]]', self.text)


class DryRunNeverAppliesTest(unittest.TestCase):
    def test_dry_run_branch_never_passes_apply(self):
        text = _read(IMPORT)
        apply_check_idx = text.index('if [[ -z "$APPLY" ]]; then')
        next_apply_section_idx = text.index("--apply from here")
        dry_run_body = text[apply_check_idx:next_apply_section_idx]
        self.assertNotIn("--apply", dry_run_body)
        self.assertIn("body.mudanza importar", dry_run_body)

    def test_dry_run_never_touches_timers_or_deletes_the_file(self):
        text = _read(IMPORT)
        apply_check_idx = text.index('if [[ -z "$APPLY" ]]; then')
        next_apply_section_idx = text.index("--apply from here")
        dry_run_body = text[apply_check_idx:next_apply_section_idx]
        self.assertNotIn("systemctl", dry_run_body)
        self.assertNotIn("rm -f", dry_run_body)


class ApplyOrderTest(unittest.TestCase):
    """Ruling 2's exact order: integrity_check, then the host backup, then stop the timers, then
    the real import -- each strictly before the next."""

    def setUp(self):
        self.text = _read(IMPORT)

    def test_integrity_check_precedes_everything_else(self):
        integrity_idx = self.text.index("PRAGMA integrity_check")
        backup_idx = self.text.index("body.backup snapshot")
        stop_idx = self.text.index("systemctl stop")
        apply_import_idx = self.text.rindex("--apply")
        self.assertLess(integrity_idx, backup_idx)
        self.assertLess(backup_idx, stop_idx)
        self.assertLess(stop_idx, apply_import_idx)

    def test_integrity_check_refuses_when_not_ok(self):
        text = self.text
        integrity_idx = text.index('if [[ "$integrity" != "ok" ]]; then')
        exit_idx = text.index("exit 1", integrity_idx)
        between = text[integrity_idx:exit_idx]
        self.assertIn("refused", between)

    def test_backup_step_never_uploaded(self):
        backup_idx = self.text.index("body.backup snapshot")
        preceding_and_following = self.text[max(0, backup_idx - 200):backup_idx + 400]
        self.assertNotIn("curl", preceding_and_following)
        self.assertNotIn("age -r", preceding_and_following)

    def test_rollback_path_is_printed(self):
        self.assertIn("rollback snapshot:", self.text)
        self.assertIn("rollback_file", self.text)

    def test_real_import_runs_as_data_user_with_apply(self):
        apply_import_idx = self.text.rindex("body.mudanza importar")
        line_end = self.text.index("\n", apply_import_idx)
        preceding = self.text[max(0, apply_import_idx - 300):apply_import_idx]
        line = self.text[apply_import_idx:line_end]
        self.assertIn('runuser -u "$DATA_USER"', preceding)
        self.assertIn("--apply", line)


class TrapRestoresAndDeletesTest(unittest.TestCase):
    def test_trap_set_before_stopping_timers(self):
        text = _read(IMPORT)
        trap_idx = text.index("trap cleanup_uploaded EXIT")
        stop_idx = text.index("systemctl stop")
        self.assertLess(trap_idx, stop_idx)

    def test_cleanup_restarts_both_timers_and_removes_the_file(self):
        text = _read(IMPORT)
        func_idx = text.index("cleanup_uploaded() {")
        func_end = text.index("\n}\n", func_idx)
        body = text[func_idx:func_end]
        self.assertIn("systemctl restart", body)
        self.assertIn("body-pull.timer", body)
        self.assertIn("body-backfill.timer", body)
        self.assertIn("rm -f", body)
        self.assertIn("$ARCHIVO", body)


class FileHandoffTest(unittest.TestCase):
    def test_file_chowned_to_data_user_before_the_real_import(self):
        text = _read(IMPORT)
        chown_idx = text.index('chown "$DATA_USER:$DATA_USER" "$ARCHIVO"')
        apply_import_idx = text.rindex("body.mudanza importar")
        self.assertLess(chown_idx, apply_import_idx)


if __name__ == "__main__":
    unittest.main()
