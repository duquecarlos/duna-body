"""Who is in my household: the ONE place Body asks this (contract: dev-protocol/contracts/hogar-v1.md).

Cuenta Duna owns the answer: the claim `duna_hogar` = {"id": "<uuid>", "miembros": ["<sub>", ...]} or
null. `hogar_miembro` mirrors it locally. Rows with fuente='cuenta' are written ONLY by
`sincronizar_desde_claim` below, called from body/cuenta.py at sign-in and on the hourly refresh.
Rows with fuente='local' are the demo's and any test's own data, written directly -- there is no
Cuenta claim behind them.

Goals, plans and privacy never import anything but `household_of`, so a later change to the source
touches only this file.

What Body does with a household stays Body's: shared goals and plans, a weekly check per member,
and health figures private unless their owner shares a specific one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Shared with body/db.py's migration (hogar_local -> hogar_miembro), so the table's shape is
# defined exactly once.
_COLUMNS = """
    person_id      INTEGER PRIMARY KEY REFERENCES person(id),
    hogar_id       TEXT NOT NULL,
    member_sub     TEXT,
    fuente         TEXT NOT NULL DEFAULT 'local' CHECK (fuente IN ('local', 'cuenta')),
    actualizado_at TEXT NOT NULL
"""

SCHEMA = f"""
-- Local mirror of Cuenta's duna_hogar claim (one active household per person, like Cuenta).
CREATE TABLE IF NOT EXISTS hogar_miembro (
{_COLUMNS}
);
"""


@dataclass(frozen=True)
class Household:
    id: str                   # Cuenta hogar uuid: the id every Duna app uses
    member_ids: tuple[int, ...]  # Body person ids of the ACTIVE members (including the asker)


def household_of(conn, person_id: int) -> Household | None:
    row = conn.execute("SELECT hogar_id FROM hogar_miembro WHERE person_id = ?", (person_id,)).fetchone()
    if not row:
        return None
    members = conn.execute("SELECT person_id FROM hogar_miembro WHERE hogar_id = ? ORDER BY person_id",
                           (row["hogar_id"],)).fetchall()
    return Household(row["hogar_id"], tuple(m["person_id"] for m in members))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_claim(valor) -> tuple[str, list[str]] | None:
    """None when `valor` is not the shape hogar-v1 promises; otherwise (hogar_id, miembros)."""
    if not isinstance(valor, dict):
        return None
    hogar_id = valor.get("id")
    miembros = valor.get("miembros")
    if not isinstance(hogar_id, str) or not isinstance(miembros, list):
        return None
    if not all(isinstance(m, str) for m in miembros):
        return None
    return hogar_id, miembros


def sincronizar_desde_claim(conn, person_id: int, claims) -> str:
    """Mirrors `claims["duna_hogar"]` into `hogar_miembro` -- the ONLY writer of fuente='cuenta'
    rows. Returns:

    * 'sin_dato': `claims` is not a dict, `duna_hogar` is not one of its keys, or its value is
      malformed -- nothing changes.
    * 'sin_hogar': the claim is `None`, or this person's own `cuenta_sub` is absent from
      `miembros` -- this person's row (if any) is removed.
    * 'actualizado': every Body person whose `person.cuenta_sub` is in `miembros` now has a row
      (hogar_id, their sub, fuente='cuenta', actualizado_at=now), replacing any row they had; every
      row for this hogar_id whose person's sub is NOT in `miembros` is removed. A member with no
      Body person yet is skipped -- they join when they first sign in.

    One transaction; a member with no linked Body user is simply absent from it, never an error.
    """
    if not isinstance(claims, dict) or "duna_hogar" not in claims:
        return "sin_dato"
    valor = claims["duna_hogar"]
    if valor is None:
        with conn:
            conn.execute("DELETE FROM hogar_miembro WHERE person_id = ?", (person_id,))
        return "sin_hogar"
    parsed = _parse_claim(valor)
    if parsed is None:
        return "sin_dato"
    hogar_id, miembros = parsed
    this_person = conn.execute("SELECT cuenta_sub FROM person WHERE id = ?", (person_id,)).fetchone()
    this_sub = this_person["cuenta_sub"] if this_person else None
    if this_sub not in miembros:
        # The claim is about this person's own household, so a `miembros` that leaves them out is
        # malformed -- treated exactly like `valor is None`: only this person's row is touched.
        with conn:
            conn.execute("DELETE FROM hogar_miembro WHERE person_id = ?", (person_id,))
        return "sin_hogar"
    now = _now_iso()
    # miembros is never empty here: this_sub (not None) is in it, checked above.
    placeholders = ",".join("?" * len(miembros))
    with conn:
        personas = conn.execute(
            f"SELECT id, cuenta_sub FROM person WHERE cuenta_sub IN ({placeholders})", miembros
        ).fetchall()
        for p in personas:
            conn.execute(
                "INSERT INTO hogar_miembro (person_id, hogar_id, member_sub, fuente, actualizado_at) "
                "VALUES (?, ?, ?, 'cuenta', ?) "
                "ON CONFLICT(person_id) DO UPDATE SET hogar_id = excluded.hogar_id, "
                "member_sub = excluded.member_sub, fuente = 'cuenta', actualizado_at = excluded.actualizado_at",
                (p["id"], hogar_id, p["cuenta_sub"], now),
            )
        conn.execute(
            f"DELETE FROM hogar_miembro WHERE hogar_id = ? AND person_id NOT IN "
            f"(SELECT id FROM person WHERE cuenta_sub IN ({placeholders}))",
            (hogar_id, *miembros),
        )
    return "actualizado"
