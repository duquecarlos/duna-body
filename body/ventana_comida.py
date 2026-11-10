"""The eating window, pure reads (BODY-AYUNO-1): Carlos asked (via Personal, 2026-11-09) "we can
keep track of fasting windows also", and this week he aims to sleep "at least 3-4 hours after
eating". Every figure here is a PURE READ of `ingesta.ts_local` (BODY-PROTEINA-1) -- no inference,
no new table, no judgement -- the same shape as body/turnos_costo.py and body/despertar.py: plain
functions over a connection and a person id.

A gap is never invented: no `ingesta` row that day, no window; one timed entry, a window of one
meal stated as such (duracion_min = 0.0, not a missing value); an entry with no `ts_local`
("sometime that day") counts as eaten but never sets the window -- `Ventana.sin_hora` says how
many there were.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass
class Ventana:
    day: str
    primera: str | None          # ts_local of the day's first TIMED entry, or None if none had a time
    ultima: str | None           # ts_local of the day's last TIMED entry, or None if none had a time
    duracion_min: float | None   # ultima - primera in minutes; 0.0 for a single timed entry; None with no timed entry
    entradas: int                # every `ingesta` row that day, timed or not
    sin_hora: int                # how many of `entradas` carry no ts_local ("sometime that day")


@dataclass
class Ayuno:
    horas: float
    desde: str   # the gap's own start -- a meal's ts_local, or a sleep session's start_local
    hasta: str   # the gap's own end -- a meal's ts_local, or a sleep session's start_local


def _sin_offset(ts: str) -> datetime:
    """A local timestamp read back as naive -- the same reading body/experimentos.py's own
    `_sin_offset` gives `sleep_session`/`moment` timestamps. `ingesta.ts_local` is written the
    same way (body/agente/herramientas.py's `_hora_a_ts_local`: "{day}T{hora}:00", no UTC offset),
    so comparing two of them as naive datetimes is the honest reading, never a timezone
    conversion."""
    dt = datetime.fromisoformat(ts)
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def ventana(conn, person_id: int, day: str) -> Ventana | None:
    """The day's own eating window: the first and last TIMED `ingesta.ts_local`, its length, the
    total entry count and how many of those carried no time at all. None when the day has no
    `ingesta` row whatsoever -- never a window invented from nothing."""
    rows = conn.execute(
        "SELECT ts_local FROM ingesta WHERE person_id = ? AND day = ? ORDER BY ts_local",
        (person_id, day),
    ).fetchall()
    if not rows:
        return None
    timed = sorted(r["ts_local"] for r in rows if r["ts_local"] is not None)
    sin_hora = sum(1 for r in rows if r["ts_local"] is None)
    if not timed:
        return Ventana(day=day, primera=None, ultima=None, duracion_min=None,
                        entradas=len(rows), sin_hora=sin_hora)
    primera, ultima = timed[0], timed[-1]
    duracion_min = (_sin_offset(ultima) - _sin_offset(primera)).total_seconds() / 60
    return Ventana(day=day, primera=primera, ultima=ultima, duracion_min=duracion_min,
                   entradas=len(rows), sin_hora=sin_hora)


def ayuno_previo(conn, person_id: int, day: str) -> Ayuno | None:
    """The overnight gap from the PREVIOUS day's last timed meal to THIS day's first -- only when
    both exist (never invented from one side alone)."""
    anterior = (date.fromisoformat(day) - timedelta(1)).isoformat()
    v_ant, v_hoy = ventana(conn, person_id, anterior), ventana(conn, person_id, day)
    if v_ant is None or v_ant.ultima is None or v_hoy is None or v_hoy.primera is None:
        return None
    horas = (_sin_offset(v_hoy.primera) - _sin_offset(v_ant.ultima)).total_seconds() / 3600
    return Ayuno(horas=horas, desde=v_ant.ultima, hasta=v_hoy.primera)


def comida_a_sueno(conn, person_id: int, day: str) -> Ayuno | None:
    """The gap from `day`'s last timed meal to the START of that night's sleep -- the "3-4 hours
    after eating" check Carlos is watching this week, a read, never a verdict. `sleep_session`
    files a night under its WAKE day (body/db.py's save_day, see body/despertar.py's own comment),
    so that night's row sits at day+1 -- only when both exist."""
    v = ventana(conn, person_id, day)
    if v is None or v.ultima is None:
        return None
    dia_despertar = (date.fromisoformat(day) + timedelta(1)).isoformat()
    row = conn.execute(
        "SELECT start_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, dia_despertar),
    ).fetchone()
    if row is None or row["start_local"] is None:
        return None
    horas = (_sin_offset(row["start_local"]) - _sin_offset(v.ultima)).total_seconds() / 3600
    return Ayuno(horas=horas, desde=v.ultima, hasta=row["start_local"])


def ayuno_ahora(conn, person_id: int, now_local: datetime) -> float | None:
    """Hours since the person's own last timed meal, as of `now_local` (naive, their own zone) --
    whichever day it fell on, for Hoy's "fasting now". None when no timed meal at or before
    `now_local` exists."""
    ahora_iso = now_local.isoformat()
    row = conn.execute(
        "SELECT MAX(ts_local) AS ultima FROM ingesta "
        "WHERE person_id = ? AND ts_local IS NOT NULL AND ts_local <= ?",
        (person_id, ahora_iso),
    ).fetchone()
    if row is None or row["ultima"] is None:
        return None
    return (now_local - _sin_offset(row["ultima"])).total_seconds() / 3600
