"""A goal can point at where the rest of its plan lives in another Duna app, instead of copying it
(DESIGN rule 17; BODY-HOGAR-ENLACE-1, carrying out orchestrator ledger BODY-HOGAR-PLAN-1 ruling 5
and (cont. 4)). The only module that reads or writes `goal_enlace` -- body/web.py and
body/agente/herramientas.py go through the functions here, never the table directly.

`APPS` is the registry: app key -> how to build its URL and validate what a person pastes. Body
stores only the app key and that app's own opaque `ref` -- never a title copied from the other
app, never a URL typed by the person (the URL is always BUILT from the registry here, so no
arbitrary link can be planted on a shared goal). An app key stored that is no longer in this
registry is simply not rendered (`enlaces_de` skips it).

Finanzas has no goal/budget entity with a stable id today (Finanzas orchestrator, 2026-11-07), so
it is left out of `APPS` until it has one -- adding it later is one registry entry, no schema
change.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

APPS = {
    "lugares": {
        "label_key": "goals.enlace.app.lugares",
        "origin_env": "DUNA_LUGARES_ORIGEN",
        "origin_default": "https://lugares.duna.solutions",
        "path": "/viajes/{ref}",
        "ref_pattern": re.compile(r"^[1-9][0-9]{0,11}$"),
    },
}


def _origin(app: str) -> str:
    spec = APPS[app]
    return os.environ.get(spec["origin_env"]) or spec["origin_default"]


def url_de(app: str, ref: str) -> str | None:
    """The URL for this app/ref, built from the registry -- None for an unknown app or a ref that
    fails its pattern. Never built from anything the person typed directly."""
    spec = APPS.get(app)
    if spec is None or not spec["ref_pattern"].match(ref):
        return None
    return _origin(app) + spec["path"].format(ref=quote(ref, safe=""))


def ref_desde_entrada(app: str, texto: str | None) -> str | None:
    """What a person will actually paste: the bare id, or a full URL on this app's own origin and
    path (with or without a trailing slash, query or fragment). None for an unknown app, a URL on
    any other host or path, or junk that matches neither."""
    spec = APPS.get(app)
    if spec is None or not texto:
        return None
    texto = texto.strip()
    if spec["ref_pattern"].match(texto):
        return texto
    parsed = urlparse(texto)
    origin_parsed = urlparse(_origin(app))
    if parsed.scheme != origin_parsed.scheme or parsed.netloc != origin_parsed.netloc:
        return None
    prefix = spec["path"].split("{ref}")[0]
    if not parsed.path.startswith(prefix):
        return None
    candidate = parsed.path[len(prefix):].rstrip("/")
    return candidate if spec["ref_pattern"].match(candidate) else None


def enlazar(conn: sqlite3.Connection, goal_id: int, app: str, ref: str) -> None:
    """Upserts the one link this goal has for this app. Refuses (ValueError, nothing written) an
    unknown app or a ref that fails its pattern -- the caller is expected to have already checked
    with `ref_desde_entrada`, but this stays defensive the way body/compartir.py's `compartir` is."""
    if url_de(app, ref) is None:
        raise ValueError(f"unknown app or invalid ref: {app!r} {ref!r}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO goal_enlace (goal_id, app, ref, creado_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(goal_id, app) DO UPDATE SET ref = excluded.ref, creado_at = excluded.creado_at",
            (goal_id, app, ref, now),
        )


def quitar(conn: sqlite3.Connection, goal_id: int, app: str) -> None:
    with conn:
        conn.execute("DELETE FROM goal_enlace WHERE goal_id = ? AND app = ?", (goal_id, app))


def enlaces_de(conn: sqlite3.Connection, goal_id: int) -> list[dict]:
    """[{app, label_key, url}] for this goal -- only registry apps whose stored ref still builds a
    valid URL; an unknown or now-invalid row is silently skipped, never rendered."""
    rows = conn.execute("SELECT app, ref FROM goal_enlace WHERE goal_id = ? ORDER BY id", (goal_id,)).fetchall()
    out = []
    for r in rows:
        url = url_de(r["app"], r["ref"])
        if url is not None:
            out.append({"app": r["app"], "label_key": APPS[r["app"]]["label_key"], "url": url})
    return out


# ---- for body/privacidad.py's export and delete-all, so it never queries the table itself -------

def export_rows(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Every link of a goal this person created -- their own export, joined through goal_id since
    this table carries no person_id of its own (the same join habit/habit_check use for `habit`)."""
    rows = conn.execute(
        "SELECT ge.goal_id, ge.app, ge.ref, ge.creado_at FROM goal_enlace ge "
        "JOIN goal g ON g.id = ge.goal_id WHERE g.person_id = ? ORDER BY ge.id",
        (person_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_rows_for_person(conn: sqlite3.Connection, person_id: int) -> None:
    """Deletes the links of every goal this person created -- called before the goal itself is
    deleted (FK order), same as habit/habit_check/goal_check/plan in body/privacidad.py."""
    conn.execute(
        "DELETE FROM goal_enlace WHERE goal_id IN (SELECT id FROM goal WHERE person_id = ?)", (person_id,)
    )
