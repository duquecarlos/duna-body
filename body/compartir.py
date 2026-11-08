"""Sharing ONE figure at a time with a household, and withdrawing it (hogar-v1: "health numbers
stay private to their person; sharing is per figure, chosen by its owner, and can be withdrawn").
The only module that reads or writes `figura_compartida` -- body/privacidad.py's export and
delete-all go through `export_rows`/`delete_rows` below rather than querying the table directly.

A shared figure is visible only while the share is active AND viewer and owner are in that same
ACTIVE household (membership read through body/household.py's `household_of`, the one place that
is read): leaving the household or withdrawing hides it at once. What `visibles_para` exposes is
the owner's LATEST `daily_metric` row for that key -- never a series, a baseline or a history.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import household


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compartir(conn: sqlite3.Connection, person_id: int, metric_key: str) -> None:
    """Shares one figure with the person's active household. Refuses (ValueError, nothing
    written) with no active household or an unknown metric key. Sharing a figure that is already
    actively shared in that household is a no-op (the existing shared_at is kept)."""
    hh = household.household_of(conn, person_id)
    if hh is None:
        raise ValueError("no active household")
    known = conn.execute("SELECT 1 FROM metric_def WHERE key = ?", (metric_key,)).fetchone()
    if known is None:
        raise ValueError(f"unknown metric key: {metric_key}")
    existing = conn.execute(
        "SELECT 1 FROM figura_compartida WHERE person_id = ? AND metric_key = ? AND hogar_id = ? "
        "AND withdrawn_at IS NULL",
        (person_id, metric_key, hh.id),
    ).fetchone()
    if existing is not None:
        return
    with conn:
        conn.execute(
            "INSERT INTO figura_compartida (person_id, metric_key, hogar_id, shared_at) VALUES (?, ?, ?, ?)",
            (person_id, metric_key, hh.id, _now()),
        )


def retirar(conn: sqlite3.Connection, person_id: int, metric_key: str) -> None:
    """Withdraws this person's currently active share(s) of this figure. The row stays, with
    withdrawn_at set, so the person can see what they shared and when. A no-op when nothing is
    actively shared."""
    with conn:
        conn.execute(
            "UPDATE figura_compartida SET withdrawn_at = ? WHERE person_id = ? AND metric_key = ? "
            "AND withdrawn_at IS NULL",
            (_now(), person_id, metric_key),
        )


def mis_compartidas(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """This person's own active shares IN THEIR CURRENT ACTIVE HOUSEHOLD only: [{metric_key, since}],
    for the Ajustes card. A share left active in a household the person has since moved out of is
    not listed here -- nobody in that old household can see it any more either (`visibles_para`
    reads the same `hogar_id`), so Ajustes marking the figure "shared" would be a lie. `retirar`
    still withdraws everywhere regardless of household, which stays the safe direction. Empty with
    no active household."""
    hh = household.household_of(conn, person_id)
    if hh is None:
        return []
    rows = conn.execute(
        "SELECT metric_key, shared_at FROM figura_compartida WHERE person_id = ? AND hogar_id = ? "
        "AND withdrawn_at IS NULL ORDER BY metric_key",
        (person_id, hh.id),
    ).fetchall()
    return [{"metric_key": r["metric_key"], "since": r["shared_at"]} for r in rows]


def visibles_para(conn: sqlite3.Connection, viewer_id: int) -> list[dict]:
    """Every OTHER member of the viewer's active household who has an active share in THAT same
    household: [{name, metric_key, value, day}]. value/day come from that owner's LATEST
    daily_metric row for the key, or (None, None) when they have no data yet for it -- never a
    guess. Empty when the viewer has no active household."""
    hh = household.household_of(conn, viewer_id)
    if hh is None:
        return []
    out = []
    for member_id in sorted(hh.member_ids):
        if member_id == viewer_id:
            continue
        shares = conn.execute(
            "SELECT metric_key FROM figura_compartida WHERE person_id = ? AND hogar_id = ? "
            "AND withdrawn_at IS NULL ORDER BY metric_key",
            (member_id, hh.id),
        ).fetchall()
        if not shares:
            continue
        name_row = conn.execute("SELECT display_name FROM person WHERE id = ?", (member_id,)).fetchone()
        name = name_row["display_name"] if name_row else "?"
        for s in shares:
            latest = conn.execute(
                "SELECT day, value FROM daily_metric WHERE person_id = ? AND metric_key = ? "
                "ORDER BY day DESC LIMIT 1",
                (member_id, s["metric_key"]),
            ).fetchone()
            out.append({
                "name": name, "metric_key": s["metric_key"],
                "value": latest["value"] if latest else None,
                "day": latest["day"] if latest else None,
            })
    return out


# ---- for body/privacidad.py's export and delete-all, so it never queries the table itself -------

def export_rows(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Every row this person owns, including withdrawn ones (their own history of what they
    shared and when)."""
    rows = conn.execute(
        "SELECT metric_key, hogar_id, shared_at, withdrawn_at FROM figura_compartida WHERE person_id = ? "
        "ORDER BY id",
        (person_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_rows(conn: sqlite3.Connection, person_id: int) -> None:
    """Plain DELETE, no transaction of its own: body/privacidad.py's delete-all calls this from
    inside its own single `with conn:` block, and nesting a second one here would commit that
    transaction early."""
    conn.execute("DELETE FROM figura_compartida WHERE person_id = ?", (person_id,))
