"""Consent, export and delete-all (host-deploy-v1-amendment-sensitive-data rule 6, Ley 1581 dato
sensible; BODY-HOSTING-PLAN-1 step 6). The gate itself lives in body/identity.py, which calls
`has_consented` below; body/web.py wires the three routes to the functions here, the same way it
already wires body/cuenta.py's functions.

Local mode (one person on their own PC) never gates on consent -- body/identity.py only checks it
in hosted mode -- but export and delete-all work the same in both modes.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from . import household
from .agente import ajustes as agente_ajustes

CONSENT_VERSION = "consentimiento-v1"

# Every table with a literal `person_id` column, and how this module accounts for its rows.
# Checked by tests/test_privacidad.py's coverage test: a new person-keyed table that is added to
# neither set fails that test, so it can never slip past export or delete-all unnoticed.
FULLY_HANDLED_TABLES = {
    "daily_metric", "sleep_session", "activity", "shift", "shift_batch",
    "goal", "habit_check", "goal_check", "plan", "ajuste_agente", "consentimiento", "hogar_local",
}
# raw_payload: kind/date only, never the stored JSON payload. garmin_cuenta: the connected-since
# date only, never the encrypted token (BODY-GARMIN-HOST-1).
PARTIAL_EXPORT_TABLES = {"raw_payload", "garmin_cuenta"}
NEVER_EXPORTED_TABLES = {"sesion"}  # session plumbing, not data about the person's body or life


def person_tables(conn: sqlite3.Connection) -> set[str]:
    """Every table in this database with a column literally named `person_id`."""
    names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    out = set()
    for name in names:
        cols = {c[1] for c in conn.execute(f"PRAGMA table_info({name})")}
        if "person_id" in cols:
            out.add(name)
    return out


# ---- consent -------------------------------------------------------------------------------------

def has_consented(conn: sqlite3.Connection, person_id: int, version: str = CONSENT_VERSION) -> bool:
    return conn.execute(
        "SELECT 1 FROM consentimiento WHERE person_id = ? AND version = ?", (person_id, version)
    ).fetchone() is not None


def record_consent(conn: sqlite3.Connection, person_id: int, version: str = CONSENT_VERSION) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO consentimiento (person_id, version, accepted_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id, version) DO NOTHING",
            (person_id, version, now),
        )


# ---- export ---------------------------------------------------------------------------------------

def export_person(conn: sqlite3.Connection, person_id: int) -> dict:
    """Everything this person's own export holds: `body-<date>.json` in body/web.py's download.
    Reads only rows this person owns -- a household member's own checks on a shared goal are theirs
    to export from their own account, not this one."""
    conn.executescript(household.SCHEMA)
    person = dict(conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone())

    vida = {
        "goals": [dict(r) for r in conn.execute(
            "SELECT * FROM goal WHERE person_id = ? ORDER BY id", (person_id,))],
        "habits": [dict(r) for r in conn.execute(
            "SELECT h.* FROM habit h JOIN goal g ON g.id = h.goal_id WHERE g.person_id = ? ORDER BY h.id",
            (person_id,))],
        "habit_checks": [dict(r) for r in conn.execute(
            "SELECT * FROM habit_check WHERE person_id = ? ORDER BY day", (person_id,))],
        "goal_checks": [dict(r) for r in conn.execute(
            "SELECT * FROM goal_check WHERE person_id = ? ORDER BY week_start", (person_id,))],
        "plans": [dict(r) for r in conn.execute(
            "SELECT * FROM plan WHERE person_id = ? ORDER BY id", (person_id,))],
        "shifts": [dict(r) for r in conn.execute(
            "SELECT * FROM shift WHERE person_id = ? ORDER BY day", (person_id,))],
        "shift_batches": [dict(r) for r in conn.execute(
            "SELECT * FROM shift_batch WHERE person_id = ? ORDER BY received", (person_id,))],
    }
    dias = [dict(r) for r in conn.execute(
        "SELECT day, metric_key, value, source FROM daily_metric WHERE person_id = ? ORDER BY day",
        (person_id,))]
    actividades = [dict(r) for r in conn.execute(
        "SELECT * FROM activity WHERE person_id = ? ORDER BY day", (person_id,))]
    sueno = [dict(r) for r in conn.execute(
        "SELECT * FROM sleep_session WHERE person_id = ? ORDER BY day", (person_id,))]
    # raw_payload is a partial-export table: its kind and date only, never the stored JSON blob.
    sincronizaciones = [dict(r) for r in conn.execute(
        "SELECT day, kind, source, fetched_at FROM raw_payload WHERE person_id = ? ORDER BY day",
        (person_id,))]
    consentimientos = [dict(r) for r in conn.execute(
        "SELECT version, accepted_at FROM consentimiento WHERE person_id = ? ORDER BY accepted_at",
        (person_id,))]
    hogar_row = conn.execute(
        "SELECT hogar_id, member_sub FROM hogar_local WHERE person_id = ?", (person_id,)
    ).fetchone()
    ajustes = {"nivel_datos": agente_ajustes.nivel_datos(conn, person_id)}
    # garmin_cuenta is a partial-export table: the connected-since date only, never the encrypted
    # token or anything that could rebuild it.
    garmin_row = conn.execute(
        "SELECT conectado_en FROM garmin_cuenta WHERE person_id = ?", (person_id,)
    ).fetchone()
    garmin = {"conectado_desde": garmin_row["conectado_en"]} if garmin_row else None

    return {
        "formato": "body-export-v1",
        "exportado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": {"display_name": person["display_name"], "timezone": person["timezone"],
                    "language": person["language"]},
        "hogar": dict(hogar_row) if hogar_row else None,
        "vida": vida,
        "dias": dias,
        "actividades": actividades,
        "sueno": sueno,
        "sincronizaciones": sincronizaciones,
        "consentimientos": consentimientos,
        "ajustes": ajustes,
        "garmin": garmin,
    }


# ---- delete-all -----------------------------------------------------------------------------------

def delete_person(conn: sqlite3.Connection, person_id: int) -> None:
    """Deletes every row this person owns, and every row that exists only because of a goal they
    created -- a shared household goal they made disappears for every member too, as the privacy
    page says. One transaction, foreign keys ON (set by db.connect already): on any error nothing
    is left half-deleted."""
    conn.executescript(household.SCHEMA)
    with conn:
        conn.execute("UPDATE habit SET assignee_person_id = NULL WHERE assignee_person_id = ?", (person_id,))
        conn.execute(
            "DELETE FROM habit_check WHERE person_id = ? OR habit_id IN ("
            "SELECT id FROM habit WHERE goal_id IN (SELECT id FROM goal WHERE person_id = ?))",
            (person_id, person_id),
        )
        conn.execute(
            "DELETE FROM goal_check WHERE person_id = ? OR goal_id IN ("
            "SELECT id FROM goal WHERE person_id = ?)",
            (person_id, person_id),
        )
        conn.execute(
            "DELETE FROM habit WHERE goal_id IN (SELECT id FROM goal WHERE person_id = ?)", (person_id,)
        )
        conn.execute(
            "DELETE FROM plan WHERE person_id = ? OR goal_id IN (SELECT id FROM goal WHERE person_id = ?)",
            (person_id, person_id),
        )
        conn.execute("DELETE FROM goal WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM daily_metric WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM sleep_session WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM raw_payload WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM activity WHERE person_id = ?", (person_id,))
        conn.execute(
            "DELETE FROM shift_batch_item WHERE batch_id IN (SELECT id FROM shift_batch WHERE person_id = ?)",
            (person_id,),
        )
        conn.execute("DELETE FROM shift_batch WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM shift WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM ajuste_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM consentimiento WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM garmin_cuenta WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM hogar_local WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM sesion WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM person WHERE id = ?", (person_id,))
    _borrar_fuera_de_bd(person_id)


def _borrar_fuera_de_bd(person_id: int) -> None:
    """Placeholder call point for anything this person's delete-all must also remove OUTSIDE the
    database. Empty, and stays empty: BODY-GARMIN-HOST-1's Garmin token lives in `garmin_cuenta`,
    a person-keyed table the generic deletes above already remove -- there is nothing left outside
    the database. Takes only an id, never a figure, like every function in this module."""
    return None
