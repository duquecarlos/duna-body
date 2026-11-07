"""Links an existing LOCAL person to a Cuenta Duna sub, once.

    python -m body.vincular_cuenta --csv vinculos-body.csv            # dry run: counts only
    python -m body.vincular_cuenta --csv vinculos-body.csv --apply    # writes person.cuenta_sub

The CSV has two columns: id_local, sub. Idempotent: a row whose person is already linked to that
same sub changes nothing; a row that would change an EXISTING link to a different sub is refused,
never overwritten. Never matches by email -- only by the `id_local` the CSV names.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys

from . import db


def read_rows(path: str) -> list[tuple[str, str]]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            id_local = (row.get("id_local") or "").strip()
            sub = (row.get("sub") or "").strip()
            if id_local and sub:
                rows.append((id_local, sub))
    return rows


def plan(conn: sqlite3.Connection, rows: list[tuple[str, str]]) -> dict:
    to_link, already_linked, refused = [], [], []
    for id_local, sub in rows:
        try:
            person_id = int(id_local)
        except ValueError:
            refused.append((id_local, sub, "id_local is not an integer"))
            continue
        person = conn.execute("SELECT id, cuenta_sub FROM person WHERE id = ?", (person_id,)).fetchone()
        if person is None:
            refused.append((id_local, sub, "no such person"))
        elif person["cuenta_sub"] is None:
            to_link.append((person["id"], sub))
        elif person["cuenta_sub"] == sub:
            already_linked.append((person["id"], sub))
        else:
            refused.append((id_local, sub, "already linked to a different sub"))
    return {"to_link": to_link, "already_linked": already_linked, "refused": refused}


def apply(conn: sqlite3.Connection, to_link: list[tuple[int, str]]) -> tuple[int, list[tuple[int, str]]]:
    applied, failed = 0, []
    for person_id, sub in to_link:
        try:
            with conn:
                conn.execute("UPDATE person SET cuenta_sub = ? WHERE id = ?", (sub, person_id))
            applied += 1
        except sqlite3.IntegrityError:
            failed.append((person_id, sub))
    return applied, failed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.vincular_cuenta")
    p.add_argument("--csv", required=True, help="vinculos-<producto>.csv (id_local, sub)")
    p.add_argument("--apply", action="store_true", help="write the links (default: dry run)")
    a = p.parse_args(argv)

    rows = read_rows(a.csv)
    conn = db.connect()
    db.init(conn)
    result = plan(conn, rows)
    applied = failed = None
    if a.apply:
        applied, failed = apply(conn, result["to_link"])

    print(f"to link: {len(result['to_link'])}")
    print(f"already linked: {len(result['already_linked'])}")
    print(f"refused: {len(result['refused'])}")
    if a.apply:
        print(f"applied: {applied}")
        if failed:
            print(f"failed (sub already taken by another person): {len(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
