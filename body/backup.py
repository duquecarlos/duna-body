"""Body's off-host backup (BODY-HOST-BACKUP-1): a consistent copy of the SQLite database for
`deploy/host/body-backup.sh` to encrypt and upload, and the check Carlos runs on his PC after
decrypting a downloaded backup.

    python -m body.backup snapshot --out <path>   # a consistent copy of config.DB_PATH
    python -m body.backup check <path>             # integrity_check + row counts on a decrypted copy

Never prints a value from any row -- counts and dates only.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import config


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()]


def snapshot(out_path: Path) -> int:
    """Write a consistent copy of config.DB_PATH to out_path using SQLite's own backup API --
    never a plain file copy, which could catch the live database mid-write. Returns the table
    count. Mode 0600 (best effort: a no-op on filesystems without POSIX permission bits)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    src = sqlite3.connect(f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(out_path)
        try:
            src.backup(dest)
            table_count = len(_table_names(dest))
        finally:
            dest.close()
    finally:
        src.close()
    out_path.chmod(0o600)
    return table_count


def check(path: Path) -> bool:
    """PRAGMA integrity_check plus per-table row counts on a decrypted snapshot. Prints 'ok' and
    the counts on success; never prints a row value."""
    conn = sqlite3.connect(path)
    try:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            print(f"integrity_check failed: {exc}")
            return False
        if result != "ok":
            print(f"integrity_check failed: {result}")
            return False
        tables = _table_names(conn)
        print("ok")
        print(f"tables={len(tables)}")
        for name in tables:
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            print(f"{name}={count}")
        return True
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.backup")
    sub = p.add_subparsers(dest="action", required=True)

    snap_p = sub.add_parser("snapshot", help="write a consistent copy of the database")
    snap_p.add_argument("--out", required=True, help="path to write the copy to")

    check_p = sub.add_parser("check", help="integrity_check plus row counts on a decrypted copy")
    check_p.add_argument("path", help="path to a decrypted SQLite file")

    a = p.parse_args(argv)
    if a.action == "snapshot":
        out_path = Path(a.out)
        table_count = snapshot(out_path)
        print(f"bytes={out_path.stat().st_size} tables={table_count}")
        return 0
    ok = check(Path(a.path))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
