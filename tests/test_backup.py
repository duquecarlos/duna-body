"""body/backup.py: consistent snapshot + integrity check (BODY-HOST-BACKUP-1). Demo data only."""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import backup, config, db


def _table_counts(path) -> dict:
    conn = sqlite3.connect(path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="body-backup-test-"))
        config.BODY_HOME = self.tmp
        config.DB_PATH = self.tmp / "body.db"
        conn = db.connect()
        db.init(conn)
        db.set_metric(conn, "2026-11-01", "resting_hr", 55, "garmin", 1)
        db.set_metric(conn, "2026-11-02", "resting_hr", 56, "garmin", 1)
        conn.commit()
        conn.close()

    def test_snapshot_matches_source_tables_and_counts(self):
        out_path = self.tmp / "snap.sqlite"
        table_count = backup.snapshot(out_path)
        self.assertTrue(out_path.exists())
        source_counts = _table_counts(config.DB_PATH)
        snap_counts = _table_counts(out_path)
        self.assertEqual(source_counts, snap_counts)
        self.assertEqual(table_count, len(source_counts))

    @unittest.skipUnless(os.name == "posix", "chmod 0600 is POSIX-only")
    def test_snapshot_mode_0600(self):
        out_path = self.tmp / "snap-mode.sqlite"
        backup.snapshot(out_path)
        self.assertEqual(out_path.stat().st_mode & 0o777, 0o600)

    def test_snapshot_main_prints_only_counts(self):
        out_path = self.tmp / "snap-cli.sqlite"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = backup.main(["snapshot", "--out", str(out_path)])
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("bytes=", output)
        self.assertIn("tables=", output)
        self.assertNotIn("55", output)
        self.assertNotIn("56", output)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="body-backup-check-"))
        config.BODY_HOME = self.tmp
        config.DB_PATH = self.tmp / "body.db"
        conn = db.connect()
        db.init(conn)
        db.set_metric(conn, "2026-11-01", "resting_hr", 55, "garmin", 1)
        conn.commit()
        conn.close()

    def test_check_ok_prints_counts_not_values(self):
        snap_path = self.tmp / "snap.sqlite"
        backup.snapshot(snap_path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = backup.check(snap_path)
        self.assertTrue(ok)
        output = buf.getvalue()
        self.assertIn("ok", output.splitlines())
        self.assertIn("tables=", output)
        self.assertNotIn("55", output)

    def test_check_fails_on_corrupted_file(self):
        bad_path = self.tmp / "bad.sqlite"
        bad_path.write_bytes(b"not a sqlite file at all, just garbage bytes")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = backup.check(bad_path)
        self.assertFalse(ok)
        self.assertNotIn("55", buf.getvalue())

    def test_main_check_exit_codes(self):
        snap_path = self.tmp / "snap-main.sqlite"
        backup.snapshot(snap_path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = backup.main(["check", str(snap_path)])
        self.assertEqual(rc, 0)

        bad_path = self.tmp / "bad-main.sqlite"
        bad_path.write_bytes(b"garbage")
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc2 = backup.main(["check", str(bad_path)])
        self.assertEqual(rc2, 1)


if __name__ == "__main__":
    unittest.main()
