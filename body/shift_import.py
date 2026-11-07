"""Imports shift files that another app (Personal) extracts from Carlos's schedule email, in the format
FORMATO.md describes (one `turnos-<received month>_<...>.json` per schedule mail; every non-matching file,
such as a `codigos.json` sidecar, is ignored). Nothing is written silently: a file is validated whole and
proposed as a batch, and only a human confirm writes it into `shift` (docs/DESIGN.md rule 1).

A batch's precedence is the pair (received, extracted_at), compared in that order: Personal re-extracts the
same mail (same `received`, later `extracted_at`) when a shift's hours or code are corrected, and that
re-extraction must win over the earlier one for the days it covers.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from . import db

VALID_KINDS = {"morning", "afternoon", "night", "day", "off", "other"}
SLOT_INDEX_NAME = "idx_shift_slot"


class ImportRefused(Exception):
    """The whole file failed validation, or the batch could not be found; nothing was written."""


class SlotIndexMissing(Exception):
    """The migration could not create the slot-uniqueness index; confirm refuses until it exists."""


def slot_index_ok(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (SLOT_INDEX_NAME,)
    ).fetchone() is not None


def _validate(shifts: list[dict]) -> None:
    seen_slots: set[tuple[str, str]] = set()
    day_kind: dict[str, str] = {}
    for s in shifts:
        day = s.get("day")
        kind = s.get("kind")
        start = s.get("start_local")
        if kind not in VALID_KINDS:
            raise ImportRefused(f"unknown kind {kind!r} on {day}")
        if not isinstance(day, str):
            raise ImportRefused(f"malformed day {day!r}")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ImportRefused(f"malformed day {day!r}")
        if kind == "off":
            if start is not None:
                raise ImportRefused(f"off day has a start_local on {day}")
        else:
            if start is None:
                raise ImportRefused(f"working shift missing start_local on {day}")
            if not isinstance(start, str) or start[:10] != day:
                raise ImportRefused(f"start_local date does not match day on {day}")
        slot = (day, start or "")
        if slot in seen_slots:
            raise ImportRefused(f"duplicate slot on {day}")
        seen_slots.add(slot)
        prior = day_kind.get(day)
        if prior is not None and (prior == "off" or kind == "off"):
            raise ImportRefused(f"an off day and a working shift both present on {day}")
        day_kind.setdefault(day, kind)


def propose_data(conn: sqlite3.Connection, data: dict, file_name: str, person_id: int) -> int:
    """Validates the whole file first and writes nothing on any error. Re-proposing the same
    (person, file_name, received, extracted_at) is a no-op that returns the existing batch id.
    Shared by the path-based `propose` below and body/web.py's POST /api/turnos."""
    source = data.get("source") or {}
    received = source.get("received")
    extracted_at = source.get("extracted_at")
    if not received:
        raise ImportRefused("missing source.received")
    if not extracted_at:
        raise ImportRefused("missing source.extracted_at")
    shifts = data.get("shifts") or []
    _validate(shifts)

    existing = conn.execute(
        "SELECT id FROM shift_batch WHERE person_id = ? AND file_name = ? AND received = ? AND extracted_at = ?",
        (person_id, file_name, received, extracted_at),
    ).fetchone()
    if existing:
        return existing["id"]

    day_count = len({s["day"] for s in shifts})
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO shift_batch (person_id, file_name, received, extracted_at, source_person, "
            "source_timezone, status, day_count, created_at) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
            (person_id, file_name, received, extracted_at, data.get("person"), data.get("timezone"),
             day_count, now),
        )
        batch_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO shift_batch_item (batch_id, day, kind, code, start_local, end_local, holiday, "
            "sunday, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(batch_id, s["day"], s["kind"], s.get("code"), s.get("start_local"), s.get("end_local"),
              int(bool(s.get("holiday"))), int(bool(s.get("sunday"))), s.get("note")) for s in shifts],
        )
    return batch_id


def propose(conn: sqlite3.Connection, path: str | Path, person_id: int) -> int:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return propose_data(conn, data, path.name, person_id)


_MOTIVO_ESQUEMA_PREFIXES = ("missing source.",)
_MOTIVO_DUPLICADO_MARKERS = ("duplicate slot", "both present")


def refusal_motivo(exc: ImportRefused) -> str:
    """Classifies an ImportRefused into a reason CLASS that never carries a date -- used by
    body/web.py's POST /api/turnos, which must never echo the exception's own message (it
    carries a day)."""
    msg = str(exc)
    if msg.startswith(_MOTIVO_ESQUEMA_PREFIXES):
        return "esquema"
    if any(marker in msg for marker in _MOTIVO_DUPLICADO_MARKERS):
        return "turno_duplicado"
    return "dia_malformado"


def preview(conn: sqlite3.Connection, batch_id: int) -> dict:
    """Read-only: how many days, how many nights, and which days a manual edit will keep as is."""
    batch = conn.execute("SELECT * FROM shift_batch WHERE id = ?", (batch_id,)).fetchone()
    items = conn.execute("SELECT * FROM shift_batch_item WHERE batch_id = ? ORDER BY day", (batch_id,)).fetchall()
    person_id = batch["person_id"]
    by_day: dict[str, list] = defaultdict(list)
    for it in items:
        by_day[it["day"]].append(it)
    night_days = {d for d, its in by_day.items() if any(i["kind"] == "night" for i in its)}
    kept_days = sorted(
        d for d in by_day
        if conn.execute("SELECT 1 FROM shift WHERE person_id = ? AND day = ? AND source = 'manual'",
                        (person_id, d)).fetchone()
    )
    return {"batch": dict(batch), "day_count": len(by_day), "night_count": len(night_days), "kept_days": kept_days}


def confirm(conn: sqlite3.Connection, batch_id: int) -> None:
    """Day by day, one transaction: a manual row is kept, a newer confirmed batch's days are kept, and
    everything else is replaced -- never a manual row, and never anything but this person's import rows."""
    if not slot_index_ok(conn):
        raise SlotIndexMissing("the slot index is missing; confirm refuses until the migration creates it")
    batch = conn.execute("SELECT * FROM shift_batch WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ImportRefused(f"no such batch {batch_id}")
    person_id = batch["person_id"]
    order = (batch["received"], batch["extracted_at"])
    items = conn.execute("SELECT * FROM shift_batch_item WHERE batch_id = ? ORDER BY day", (batch_id,)).fetchall()
    by_day: dict[str, list] = defaultdict(list)
    for it in items:
        by_day[it["day"]].append(it)

    with conn:
        for day, day_items in by_day.items():
            manual = conn.execute(
                "SELECT 1 FROM shift WHERE person_id = ? AND day = ? AND source = 'manual'", (person_id, day)
            ).fetchone()
            if manual:
                for it in day_items:
                    conn.execute("UPDATE shift_batch_item SET outcome = 'manual_kept' WHERE id = ?", (it["id"],))
                continue
            existing = conn.execute(
                "SELECT b.received, b.extracted_at FROM shift s JOIN shift_batch b ON b.id = s.batch_id "
                "WHERE s.person_id = ? AND s.day = ? AND s.source LIKE 'personal:%' LIMIT 1",
                (person_id, day),
            ).fetchone()
            if existing and (existing["received"], existing["extracted_at"]) > order:
                for it in day_items:
                    conn.execute("UPDATE shift_batch_item SET outcome = 'newer_kept' WHERE id = ?", (it["id"],))
                continue
            conn.execute(
                "DELETE FROM shift WHERE person_id = ? AND day = ? AND source LIKE 'personal:%'", (person_id, day)
            )
            for it in day_items:
                conn.execute(
                    "INSERT INTO shift (person_id, day, kind, code, start_local, end_local, source, batch_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (person_id, day, it["kind"], it["code"], it["start_local"], it["end_local"],
                     f"personal:{batch['file_name']}", batch_id),
                )
                conn.execute("UPDATE shift_batch_item SET outcome = 'applied' WHERE id = ?", (it["id"],))
        conn.execute(
            "UPDATE shift_batch SET status = 'confirmed', decided_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), batch_id),
        )
        _mark_superseded(conn, person_id, batch_id)


def _mark_superseded(conn: sqlite3.Connection, person_id: int, just_confirmed: int) -> None:
    rows = conn.execute(
        "SELECT DISTINCT batch_id FROM shift_batch_item WHERE outcome = 'applied' AND batch_id != ? "
        "AND batch_id IN (SELECT id FROM shift_batch WHERE person_id = ? AND status = 'confirmed')",
        (just_confirmed, person_id),
    ).fetchall()
    for r in rows:
        bid = r["batch_id"]
        if conn.execute("SELECT 1 FROM shift WHERE batch_id = ?", (bid,)).fetchone() is None:
            conn.execute("UPDATE shift_batch SET status = 'superseded' WHERE id = ?", (bid,))


def dismiss(conn: sqlite3.Connection, batch_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE shift_batch SET status = 'dismissed', decided_at = ? WHERE id = ? AND status = 'proposed'",
            (datetime.now().isoformat(timespec="seconds"), batch_id),
        )


def confirm_all(conn: sqlite3.Connection, person_id: int) -> dict:
    """Confirms every proposed batch of this person, in (received, extracted_at) order, each through the
    unchanged confirm() -- so precedence, manual-kept and the no-DELETE-of-manual rule apply exactly as they
    do one batch at a time. One transaction PER BATCH, not one for the whole set: confirm()'s own `with conn:`
    commits as soon as it returns, so nesting it inside a second `with conn:` here would not make the set
    atomic without changing confirm()'s own transaction handling, which stays out of scope for this slice.
    Refuses up front, writing nothing, when the slot index is missing -- the same guard confirm() applies."""
    if not slot_index_ok(conn):
        raise SlotIndexMissing("the slot index is missing; confirm_all refuses until the migration creates it")
    batches = conn.execute(
        "SELECT id FROM shift_batch WHERE person_id = ? AND status = 'proposed' "
        "ORDER BY received, extracted_at, id",
        (person_id,),
    ).fetchall()
    days_applied = days_kept_manual = days_kept_newer = 0
    for b in batches:
        confirm(conn, b["id"])
        counts = {
            r["outcome"]: r["n"]
            for r in conn.execute(
                "SELECT outcome, COUNT(DISTINCT day) AS n FROM shift_batch_item WHERE batch_id = ? "
                "GROUP BY outcome",
                (b["id"],),
            )
        }
        days_applied += counts.get("applied", 0)
        days_kept_manual += counts.get("manual_kept", 0)
        days_kept_newer += counts.get("newer_kept", 0)
    return {
        "batches": len(batches),
        "days_applied": days_applied,
        "days_kept_manual": days_kept_manual,
        "days_kept_newer": days_kept_newer,
    }


def summary_proposed(conn: sqlite3.Connection, person_id: int) -> dict:
    """Read-only: how many batches are waiting, the day range they span, and total days/nights across all of
    them -- for the one combined card shown when more than one file is proposed at once."""
    batch_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM shift_batch WHERE person_id = ? AND status = 'proposed'", (person_id,)
        ).fetchall()
    ]
    if not batch_ids:
        return {"batch_count": 0, "first_day": None, "last_day": None, "day_count": 0, "night_count": 0}
    placeholders = ",".join("?" * len(batch_ids))
    items = conn.execute(
        f"SELECT day, kind FROM shift_batch_item WHERE batch_id IN ({placeholders})", batch_ids
    ).fetchall()
    days = {it["day"] for it in items}
    nights = {it["day"] for it in items if it["kind"] == "night"}
    return {
        "batch_count": len(batch_ids),
        "first_day": min(days) if days else None,
        "last_day": max(days) if days else None,
        "day_count": len(days),
        "night_count": len(nights),
    }


def latest_proposed(conn: sqlite3.Connection, person_id: int):
    return conn.execute(
        "SELECT * FROM shift_batch WHERE person_id = ? AND status = 'proposed' "
        "ORDER BY received DESC, extracted_at DESC, id DESC LIMIT 1",
        (person_id,),
    ).fetchone()


def latest_confirmed(conn: sqlite3.Connection, person_id: int):
    return conn.execute(
        "SELECT * FROM shift_batch WHERE person_id = ? AND status = 'confirmed' "
        "ORDER BY received DESC, extracted_at DESC, id DESC LIMIT 1",
        (person_id,),
    ).fetchone()


def _candidate_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("turnos-*.json"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m body.shift_import")
    ap.add_argument("--dir")
    ap.add_argument("--person-id", type=int, default=None, help="default: the local person")
    a = ap.parse_args(argv)

    folder_str = a.dir or os.environ.get("BODY_SHIFTS_DIR")
    if not folder_str:
        print("no shifts folder configured (--dir or BODY_SHIFTS_DIR)")
        return
    folder = Path(folder_str)
    if not folder.is_dir():
        print(f"shifts folder not found: {folder}")
        return

    entries = []
    for f in _candidate_files(folder):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            src = data.get("source") or {}
            entries.append((f, src.get("received") or "", src.get("extracted_at") or ""))
        except (OSError, json.JSONDecodeError):
            entries.append((f, "", ""))
    entries.sort(key=lambda e: (e[1], e[2]))

    conn = db.connect()
    try:
        db.init(conn)
        person_id = a.person_id if a.person_id is not None else db.local_person_id(conn)
        read = proposed = known = refused = 0
        notes: list[str] = []
        for f, received, extracted_at in entries:
            read += 1
            existed = bool(received and extracted_at and conn.execute(
                "SELECT 1 FROM shift_batch WHERE person_id = ? AND file_name = ? AND received = ? AND extracted_at = ?",
                (person_id, f.name, received, extracted_at),
            ).fetchone())
            try:
                propose(conn, f, person_id)
            except ImportRefused as e:
                refused += 1
                notes.append(str(e))
                continue
            if existed:
                known += 1
            else:
                proposed += 1

        line = f"files read: {read}, batches proposed: {proposed}, already known: {known}, files refused: {refused}"
        if notes:
            line += " (" + "; ".join(notes) + ")"
        print(line)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
