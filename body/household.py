"""Who is in my household: the ONE place Body asks this (contract: dev-protocol/contracts/hogar-v1.md).

Cuenta Duna owns the answer: the claim `duna_hogar` = {"id": "<uuid>", "miembros": ["<sub>", ...]} or null.
Body has no Cuenta sign-in yet, so today the source is local: a mirror table with the same shape.
When Body gets its Cuenta client (CUENTA-API-CLIENTE-BODY-1), add `ClaimSource` here and switch `current_source`.
Goals, plans and privacy never import anything but `household_of`, so the switch touches only this file.

What Body does with a household stays Body's: shared goals and plans, a weekly check per member,
and health figures private unless their owner shares a specific one.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db

SCHEMA = """
-- Local mirror of Cuenta's duna_hogar claim (one active household per person, like Cuenta).
CREATE TABLE IF NOT EXISTS hogar_local (
    person_id INTEGER PRIMARY KEY REFERENCES person(id),
    hogar_id  TEXT NOT NULL,
    member_sub TEXT                       -- the person's Cuenta sub, once known
);
"""


@dataclass(frozen=True)
class Household:
    id: str                   # Cuenta hogar uuid: the id every Duna app uses
    member_ids: tuple[int, ...]  # Body person ids of the ACTIVE members (including the asker)


class LocalSource:
    def household_of(self, conn, person_id: int) -> Household | None:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT hogar_id FROM hogar_local WHERE person_id = ?", (person_id,)).fetchone()
        if not row:
            return None
        members = conn.execute("SELECT person_id FROM hogar_local WHERE hogar_id = ? ORDER BY person_id",
                               (row["hogar_id"],)).fetchall()
        return Household(row["hogar_id"], tuple(m["person_id"] for m in members))


current_source = LocalSource()


def household_of(conn, person_id: int = db.DEFAULT_PERSON_ID) -> Household | None:
    return current_source.household_of(conn, person_id)
