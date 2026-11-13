"""Weekly strength-session count across the whole watch history, against the plan's floor, tagged
by night-shift week (BODY-ENTRENO-RACHA-SEMANAL-1). Feeds BODY-TENDENCIAS-RACHA-GRAFICO-1's chart.

Pure reads: a bounded number of statements independent of the number of weeks -- one `activity`
read (BODY-TENDENCIAS-RACHA-GRAFICO-1-M: each row carries its own `con_set` flag via a correlated
EXISTS against `workout_set`, folding the old separate MIN(day) and DISTINCT workout_set reads into
this one) and one `bloques_noche` call.

Ruling 1: a strength session is an `activity` row of the person whose `type` is in `TIPOS_FUERZA`,
OR one that has at least one active, non-excluded `workout_set` row -- checked on EITHER half of a
linked watch/manual pair (`vinculo_manual`), since the pair is one session and either half may carry
the type or the sets. The pair counts once, keyed to the watch row's own day. Week = the ISO week
starting Monday (`fechas.lunes_de` of the session's day).

Ruling 2: the floor is data, passed in as `piso`. This module never types a default; resolving which
plan/habit supplies it is BODY-TENDENCIAS-RACHA-GRAFICO-1's own decision (the habit table has no
activity-type link -- see that slice's open follow-up).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .fechas import lunes_de
from .turnos_costo import bloques_noche

TIPOS_FUERZA = {"strength_training"}


def _parse(d: str) -> date:
    return date.fromisoformat(d)


def _es_fuerza(row, pareja) -> bool:
    if row["type"] in TIPOS_FUERZA or row["con_set"]:
        return True
    if pareja is not None:
        if pareja["type"] in TIPOS_FUERZA or pareja["con_set"]:
            return True
    return False


def _filas(conn: sqlite3.Connection, person_id: int):
    return conn.execute(
        "SELECT a.source, a.source_id, a.day, a.type, a.start_local, a.vinculo_manual, EXISTS ("
        "  SELECT 1 FROM workout_set ws WHERE ws.person_id = a.person_id AND "
        "  ws.activity_source = a.source AND ws.activity_source_id = a.source_id AND "
        "  ws.set_type = 'active' AND ws.excluded_by_person = 0"
        ") AS con_set FROM activity a WHERE a.person_id = ?",
        (person_id,),
    ).fetchall()


def _clasificar(filas) -> list[dict]:
    """Every classified strength session in `filas` (ruling 1: `_es_fuerza` plus the watch/manual
    pairing -- a linked pair counted once, keyed to the watch row's own day) -- one entry per
    session, {'day': date, 'start_local': str | None}."""
    por_clave = {(r["source"], r["source_id"]): r for r in filas}
    vinculadas = {r["vinculo_manual"] for r in filas if r["vinculo_manual"]}
    sesiones = []
    for r in filas:
        if r["source"] == "manual" and r["source_id"] in vinculadas:
            continue  # counted once via the watch row it is linked to, below
        pareja = por_clave.get(("manual", r["vinculo_manual"])) if r["vinculo_manual"] else None
        if not _es_fuerza(r, pareja):
            continue
        start = r["start_local"] or (pareja["start_local"] if pareja else None)
        sesiones.append({"day": _parse(r["day"]), "start_local": start})
    return sesiones


def sesiones_fuerza(conn: sqlite3.Connection, person_id: int, desde: date | None = None,
                     hasta: date | None = None) -> list[dict]:
    """Public classifier (BODY-PLAN-MODULO-1 ruling 5): the exact per-session strength rule
    `racha_semanal` applies (`_es_fuerza` plus the watch/manual pairing), as every session in
    [desde, hasta] inclusive (either end optional) -- one entry per session,
    {'day': date, 'start_local': str | None}. One SQL statement over this person's whole activity
    history regardless of the range: body/horizonte.py calls this once per range it needs, never
    once per week, so its own statement count stays bounded (ruling 1) the same way this
    module's own `racha_semanal` already is."""
    sesiones = _clasificar(_filas(conn, person_id))
    if desde is not None:
        sesiones = [s for s in sesiones if s["day"] >= desde]
    if hasta is not None:
        sesiones = [s for s in sesiones if s["day"] <= hasta]
    return sesiones


def racha_semanal(conn: sqlite3.Connection, person_id: int, *, piso: int | None = None,
                   hoy: date | None = None) -> dict | None:
    hoy = hoy or date.today()

    filas = _filas(conn, person_id)
    if not filas:
        return None

    sesiones = _clasificar(filas)
    if not sesiones:
        return None

    sesiones_por_semana: dict[date, int] = {}
    for s in sesiones:
        semana = lunes_de(s["day"])
        sesiones_por_semana[semana] = sesiones_por_semana.get(semana, 0) + 1

    primera = min(r["day"] for r in filas)
    desde = lunes_de(_parse(primera))
    hasta = lunes_de(hoy)

    bloques = bloques_noche(conn, person_id, desde.isoformat(), (hasta + timedelta(days=6)).isoformat())
    noches: set[date] = set()
    for b in bloques:
        d = _parse(b.inicio)
        fin = _parse(b.fin)
        while d <= fin:
            noches.add(lunes_de(d))
            d += timedelta(days=1)

    semanas = []
    semana = desde
    while semana <= hasta:
        sesiones = sesiones_por_semana.get(semana, 0)
        alcanza = None if piso is None else sesiones >= piso
        semanas.append({"lunes": semana.isoformat(), "sesiones": sesiones,
                         "noche": semana in noches, "alcanza": alcanza})
        semana += timedelta(days=7)

    out: dict = {
        "semanas": semanas,
        "desde": desde.isoformat(),
        "dias_historia": (hoy - _parse(primera)).days,
    }
    if piso is None:
        return out

    runs: list[tuple[int, int]] = []
    inicio_idx = None
    for i, s in enumerate(semanas):
        if s["alcanza"]:
            if inicio_idx is None:
                inicio_idx = i
        elif inicio_idx is not None:
            runs.append((inicio_idx, i - 1))
            inicio_idx = None
    if inicio_idx is not None:
        runs.append((inicio_idx, len(semanas) - 1))

    rachas = []
    for inicio_idx, fin_idx in runs:
        termina_en_noche = fin_idx + 1 < len(semanas) and semanas[fin_idx + 1]["noche"]
        rachas.append({
            "inicio": semanas[inicio_idx]["lunes"],
            "fin": semanas[fin_idx]["lunes"],
            "semanas": fin_idx - inicio_idx + 1,
            "termina_en_noche": termina_en_noche,
        })
    out["rachas"] = rachas

    mas_larga = None
    if rachas:
        max_len = max(r["semanas"] for r in rachas)
        mas_larga = next(r for r in reversed(rachas) if r["semanas"] == max_len)
    out["mas_larga"] = mas_larga

    idx_actual = len(semanas) - 1
    idx_anterior = idx_actual - 1
    actual = None
    for (inicio_idx, fin_idx), racha in zip(runs, rachas):
        if inicio_idx <= idx_actual <= fin_idx or inicio_idx <= idx_anterior <= fin_idx:
            actual = racha
            break
    out["actual"] = actual

    terminadas = [r for r in rachas if r is not actual]
    out["terminadas_en_noche"] = {
        "n": sum(1 for r in terminadas if r["termina_en_noche"]),
        "de": len(terminadas),
    }

    return out
