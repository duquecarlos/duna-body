"""The person's own hand-typed shift cycle (`shift_pattern`, BODY-TURNOS-ESQUEMA-1), for someone who
works shifts but has no file this app can read (BODY-TURNOS-OPCIONAL-5). A cycle is whatever the
person types -- nothing here is Carlos's own schedule, or anyone's real one.

A cycle entry is `{"kind", "start", "end"}`: `kind` is one of `shift_import.VALID_KINDS`; `start` /
`end` are `"HH:MM"` for every kind except `off`, where both are null. `end <= start` means the shift
crosses midnight (valid); `start == end` does not (refused).

`expandir()` turns the saved cycle into the same shape `shift_import._validate` expects (`day`,
`kind`, `start_local`, `end_local`, `code`), and `proponer()` hands that to `shift_import.propose_data`
with `kind="pattern"` -- the same propose-then-confirm flow a file import uses, never a silent write.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

from . import shift_import

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class PatronInvalido(Exception):
    """The cycle failed validation; nothing was written. `errors` is a list of
    `{"index": int | None, "code": str}` -- `index` is the cycle position (None for a whole-cycle
    refusal such as its length), `code` is a reason CLASS that never carries a date or a time."""

    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} invalid cycle row(s)")
        self.errors = errors


def validar(cycle: list[dict]) -> None:
    """Raises PatronInvalido (never writes) on: an invalid cycle length; an unknown kind; a working
    day missing a time or with a malformed one; `start == end`; an `off` day carrying a time."""
    if not isinstance(cycle, list) or not (1 <= len(cycle) <= 56):
        raise PatronInvalido([{"index": None, "code": "largo_invalido"}])
    errores: list[dict] = []
    for i, entry in enumerate(cycle):
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if kind not in shift_import.VALID_KINDS:
            errores.append({"index": i, "code": "tipo_desconocido"})
            continue
        start, end = entry.get("start"), entry.get("end")
        if kind == "off":
            if start is not None or end is not None:
                errores.append({"index": i, "code": "descanso_con_hora"})
            continue
        if not (isinstance(start, str) and _HHMM.match(start)) or not (isinstance(end, str) and _HHMM.match(end)):
            errores.append({"index": i, "code": "hora_invalida"})
            continue
        if start == end:
            errores.append({"index": i, "code": "misma_hora"})
    if errores:
        raise PatronInvalido(errores)


def guardar(conn: sqlite3.Connection, person_id: int, start_day: str, cycle: list[dict], horizon_weeks: int) -> None:
    """Validates the whole cycle first and writes nothing on any error; upserts the one row."""
    validar(cycle)
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO shift_pattern (person_id, start_day, cycle, horizon_weeks, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET "
            "start_day = excluded.start_day, cycle = excluded.cycle, horizon_weeks = excluded.horizon_weeks, "
            "updated_at = excluded.updated_at",
            (person_id, start_day, json.dumps(cycle), horizon_weeks, now),
        )


def leer(conn: sqlite3.Connection, person_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM shift_pattern WHERE person_id = ?", (person_id,)).fetchone()
    if row is None:
        return None
    return {"person_id": row["person_id"], "start_day": row["start_day"], "cycle": json.loads(row["cycle"]),
            "horizon_weeks": row["horizon_weeks"], "updated_at": row["updated_at"]}


def quitar(conn: sqlite3.Connection, person_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM shift_pattern WHERE person_id = ?", (person_id,))


def expandir(pattern: dict, desde: date) -> list[dict]:
    """The `shifts` list in shift_import's format, for every day from `max(desde, start_day)` across
    `horizon_weeks * 7` days -- never a day before `desde`, even when `start_day` is in the past, so
    an edited pattern never rewrites history. The cycle phase is anchored to `start_day`, not to
    wherever expansion actually begins."""
    start_day = date.fromisoformat(pattern["start_day"])
    cycle = pattern["cycle"]
    n = len(cycle)
    efectivo = max(desde, start_day)
    shifts: list[dict] = []
    for i in range(pattern["horizon_weeks"] * 7):
        day = efectivo + timedelta(i)
        entry = cycle[(day - start_day).days % n]
        if entry["kind"] == "off":
            shifts.append({"day": day.isoformat(), "kind": "off", "code": None,
                           "start_local": None, "end_local": None})
            continue
        start_h, start_m = (int(x) for x in entry["start"].split(":"))
        end_h, end_m = (int(x) for x in entry["end"].split(":"))
        end_day = day + timedelta(1) if (end_h, end_m) <= (start_h, start_m) else day
        shifts.append({
            "day": day.isoformat(), "kind": entry["kind"], "code": None,
            "start_local": f"{day.isoformat()}T{entry['start']}",
            "end_local": f"{end_day.isoformat()}T{entry['end']}",
        })
    return shifts


def proponer(conn: sqlite3.Connection, person_id: int, hoy: date) -> int:
    """Expands the saved pattern from `hoy` and proposes it as a batch, dismissing any still-proposed
    pattern batch of this person first -- only the newest pattern proposal stands; an import batch
    proposed meanwhile is never touched. Returns the new batch id."""
    pattern = leer(conn, person_id)
    shifts = expandir(pattern, hoy)
    for r in conn.execute(
        "SELECT id FROM shift_batch WHERE person_id = ? AND status = 'proposed' AND kind = 'pattern'",
        (person_id,),
    ).fetchall():
        shift_import.dismiss(conn, r["id"])
    now = datetime.now().isoformat(timespec="seconds")
    data = {"source": {"received": hoy.isoformat(), "extracted_at": now}, "shifts": shifts}
    return shift_import.propose_data(conn, data, "patron-manual", person_id, kind="pattern")
