"""BODY-PLAN-FASES-6 (ORCH-CARLOS-DELEGATES-TIER-1-1, BODY-PLAN-FASES-1-INV follow-up): two
situations a session proposal cannot see on its own -- coming back after a GAP (a week or more
with no strength session) and the day after a HEAVY LOAD (a ball game, a run, any activity whose
own recovery runs long against the person's own usual). Carlos, relayed by the desk 2026-11-19:
"the Greece plan can wait until I got back home ... I didn't train for about a week ... Maybe
Sunday morning I'll go to play basketball."

Pure reads over a connection and a person id, no web imports, computed on read -- no derived
table, the same shape body/noche_inusual.py already keeps for its own flag. Every threshold is a
VERSIONED MODEL (`MODELO`), the reasoning beside each value, never a constant named for one
person (docs/DESIGN.md rules 4, 5, 12). `body/entreno_racha.py`, `body/recuperacion.py` and
`body/anatomia.py` are called here, never edited (ORCH-CARLOS-DELEGATES-TIER-1-1's approval
reason) -- this module's own job is only the gap/heavy-load judgement and the small type/name ->
categoria lookup neither of those three modules carries.

Ruling 1 (the gap, `_reentrada`): walks this person's own strength-session days
(`entreno_racha.sesiones_fuerza`, already person-scoped) backward from `dia`, counting how many
sessions back the chain reaches before hitting a stretch of `MODELO['gap_dias']` days or more with
none -- that count IS the session number since the gap (1st, 2nd, 3rd back). BODY-REENTRADA-
ESCALA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1, the Personal desk 2026-11-24: "ten days does not cost
strength; the usual return after one to two weeks is the same weights, about one set fewer,
stopping 1-2 reps short of failure"): the ramp's own SHAPE depends on how long that gap actually
ran, never one ramp for any gap of seven days or more. `MODELO['escalones']` holds one ramp per
gap band, as DATA (`_escalon` below picks the band the gap's own length falls into, once, the
first time the gap is found); from there the walk reads that band's own `factores` by session
number exactly as reentrada-v1 read its single list. Once the chain reaches a factor of 1.0
(normal, nothing left to scale), the walk simply does not flag -- never an explicit "back to
normal" key, the same "no line, no claim" shape body/noche_inusual.py's own `evaluar` keeps for a
normal night. Bounded: one SQL statement (`sesiones_fuerza`'s own one-statement read of this
person's whole history) regardless of how far back the walk looks, since every band's own
`factores` is short, plus one more only once a qualifying gap is actually found (ruling 2 below).

Ruling 2 (BODY-REENTRADA-ESCALA-1, context next to the gap): a qualifying gap also carries
`dias_activos_en_brecha` -- how many of the gap's OWN days (strictly between the last strength
session and the day training resumed) had ANY activity logged, from this person's own activity
rows. Context only, and it never changes the factor: strength detraining follows the strength
stimulus, and a ball game or a walk does not load the same lifts a strength ramp is about. Hoy's
own readiness already carries its own signal for that, next to this one.

Ruling 3 (the heavy day, `_tras_carga`): the day before `dia`'s own activities, the first one
whose own recovery (`recuperacion.recuperacion_actividad`) runs at least `MODELO['carga_alta_factor']`
times this person's OWN typical recovery (`recuperacion.estudio`'s overall median) -- or, when
that activity's own recovery could not be read honestly, whose duration sits in this person's own
top tercile for activities of that same type (never a population figure either way). Its own
loaded muscle groups come from `anatomia.grupos_de_categorias`, fed by this activity's own
training categoria: a logged strength session's own `workout_set.category` values, or -- a
plain cardio/ball activity carries no categoria of its own -- a small, versioned type/name lookup
below (`_TIPO_A_CATEGORIA`, `_OTRO_NOMBRE_A_CATEGORIA`). An activity whose categoria cannot be
named this way contributes no signal: nothing for the card to point at, nothing for a proposal to
exclude.
"""
from __future__ import annotations

import sqlite3
import statistics
import unicodedata
from datetime import date, timedelta

from . import anatomia, db, entreno_racha, recuperacion

VERSION = "reentrada-v2"

MODELO = {
    "version": VERSION,
    # A week or more with no strength session reads as a gap worth flagging (Carlos's own words,
    # relayed: "about a week") -- the same natural-week granularity recuperacion.MODELO's own
    # 'dias_historia' and body/baselines.py's WINDOW_DAYS already use for a person's own history.
    # Also the shortest band's own 'desde_dias' below, so the two never drift apart.
    "gap_dias": 7,
    # BODY-REENTRADA-ESCALA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1): the ramp's own shape depends on
    # how LONG the gap ran, not just that one existed -- the Personal desk, 2026-11-24, on Carlos's
    # own re-entry after ten days: "ten days does not cost strength; the usual return after one to
    # two weeks is the same weights, about one set fewer, stopping 1-2 reps short of failure." A
    # ten-day gap and a six-week gap no longer share one ramp. Each band is {desde_dias, hasta_dias
    # (None = open-ended), nombre, factores} -- `_escalon` below picks the band the gap's own
    # length falls into, once, the first time a qualifying gap is found; `_reentrada` then walks
    # that band's own 'factores' by session number exactly as reentrada-v1 walked its single list.
    # A band's own last factor is always 1.0 (back to normal) on purpose, for the same reason
    # reentrada-v1's own list ended in 1.0: `senales` omits the 'reentrada' key entirely once the
    # factor reaches it, since there is nothing left to scale and nothing to say.
    "escalones": [
        # One to two weeks: close to normal training, not a real detraining window -- one gentle
        # step (close to "about one set fewer" at the usual weight) then straight back to normal.
        {"desde_dias": 7, "hasta_dias": 14, "nombre": "corto", "factores": [0.9, 1.0]},
        # Two to four weeks: enough to lose some of the trained edge -- reentrada-v1's own middle
        # step, kept here as its own band rather than applied to every qualifying gap.
        {"desde_dias": 15, "hasta_dias": 28, "nombre": "medio", "factores": [0.75, 0.9, 1.0]},
        # A month or more: reentrada-v1's own original three-step ramp, now reserved for a gap
        # this long rather than the only ramp there ever was.
        {"desde_dias": 29, "hasta_dias": None, "nombre": "largo", "factores": [0.6, 0.8, 1.0]},
    ],
    # An activity's own recovery minutes (recuperacion.recuperacion_actividad) counts as a heavy
    # load once they run at least this many times the person's OWN typical recovery
    # (recuperacion.estudio's own overall median) -- "half again" reads as a clear outlier for a
    # single day without being so high it only ever fires on an extreme one.
    "carga_alta_factor": 1.5,
}

# Garmin `activity.type` values this module reads as its own leg-loading cardio -- the running/
# cycling/walking families body/history.py's own KNOWN_ACTIVITY_TYPES already groups, read here
# for a different reason (which muscle groups a day's load touches, not which chart bucket it
# falls in). A type not listed here (swimming, yoga, elliptical, cardio, strength_training --
# handled on its own below, through its logged sets) lights no group -- never guessed.
_TIPO_A_CATEGORIA: dict[str, str] = {
    "running": "pierna", "trail_running": "pierna", "treadmill_running": "pierna", "track_running": "pierna",
    "cycling": "pierna", "indoor_cycling": "pierna", "mountain_biking": "pierna", "road_biking": "pierna",
    "walking": "pierna", "hiking": "pierna",
}

# Garmin's generic "other" bucket carries no type code of its own -- a ball game lands here under
# its own free-text name (body/demo.py's own basketball session: type 'other', name 'Baloncesto'/
# 'Basketball'). A short, versioned list of common leg-loading ball games/sports, case/accent-
# folded, both languages this app ships; a name not listed here lights no group, never guessed.
_OTRO_NOMBRE_A_CATEGORIA: dict[str, str] = {
    "baloncesto": "pierna", "basketball": "pierna",
    "futbol": "pierna", "football": "pierna", "soccer": "pierna",
    "voleibol": "pierna", "volleyball": "pierna",
    "tenis": "pierna", "tennis": "pierna",
}


def _normaliza(nombre: str | None) -> str:
    if not nombre:
        return ""
    return unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().casefold()


def _categorias_actividad(conn: sqlite3.Connection, person_id: int, row) -> list[str]:
    """This one activity's own training categoria(s), for `anatomia.grupos_de_categorias` -- its
    own logged sets' own categoria for a strength session (db.exercise_sets, the SAME read Hoy's
    own body view already uses for today), else the fixed type/name lookup above. [] when neither
    source names one."""
    if row["type"] == "strength_training":
        sets = db.exercise_sets(conn, person_id, row["source"], row["source_id"])
        return [s["category"] for s in sets if s["category"]]
    if row["type"] in _TIPO_A_CATEGORIA:
        return [_TIPO_A_CATEGORIA[row["type"]]]
    if row["type"] == "other":
        categoria = _OTRO_NOMBRE_A_CATEGORIA.get(_normaliza(row["name"]))
        return [categoria] if categoria else []
    return []


def _duracion_top_tercil(conn: sqlite3.Connection, person_id: int, row) -> bool:
    """Ruling 3's fallback: this activity's own duration against this person's OWN tercile split
    for activities of the SAME type -- fewer than 3 such activities and there is no honest tercile
    yet (the recuperacion.estudio's own 'min_n'-style guard), so this reads as not-heavy rather
    than guessing."""
    if row["duration_s"] is None:
        return False
    duraciones = [r["duration_s"] for r in conn.execute(
        "SELECT duration_s FROM activity WHERE person_id = ? AND type = ? AND duration_s IS NOT NULL",
        (person_id, row["type"])).fetchall()]
    if len(duraciones) < 3:
        return False
    terciles = statistics.quantiles(duraciones, n=3)
    return row["duration_s"] >= terciles[1]


def _escalon(gap_dias_reales: int) -> dict:
    """Ruling 1 (BODY-REENTRADA-ESCALA-1): the band the gap's own length falls into -- the last
    one whose own 'hasta_dias' is None always matches, so this never falls through."""
    for escalon in MODELO["escalones"]:
        if gap_dias_reales >= escalon["desde_dias"] and (
                escalon["hasta_dias"] is None or gap_dias_reales <= escalon["hasta_dias"]):
            return escalon
    return MODELO["escalones"][-1]


def _dias_activos_en_brecha(conn: sqlite3.Connection, person_id: int, dia_sesion: date, hasta: date) -> int:
    """Ruling 2 (BODY-REENTRADA-ESCALA-1): how many of the gap's OWN days -- strictly between the
    last strength session and the day training resumed -- had ANY activity logged, this person's
    own rows only. Context for the assistant's words, never a change to the factor."""
    desde = (dia_sesion + timedelta(1)).isoformat()
    fin = (hasta - timedelta(1)).isoformat()
    if desde > fin:
        return 0
    fila = conn.execute(
        "SELECT COUNT(DISTINCT day) AS n FROM activity WHERE person_id = ? AND day >= ? AND day <= ?",
        (person_id, desde, fin)).fetchone()
    return fila["n"]


def _reentrada(conn: sqlite3.Connection, person_id: int, dia: date) -> dict | None:
    sesiones = entreno_racha.sesiones_fuerza(conn, person_id, hasta=dia - timedelta(1))
    dias = sorted({s["day"] for s in sesiones})
    if not dias:
        return None
    gap_dias = MODELO["gap_dias"]
    anterior = dia
    n = 0
    for dia_sesion in reversed(dias):
        n += 1
        gap = (anterior - dia_sesion).days
        if gap >= gap_dias:
            escalon = _escalon(gap)
            factores = escalon["factores"]
            factor = factores[n - 1] if n <= len(factores) else factores[-1]
            if factor >= 1.0:
                return None
            return {"dias_sin_fuerza": gap, "sesion_n": n, "factor": factor,
                    "escalon": escalon["nombre"],
                    "dias_activos_en_brecha": _dias_activos_en_brecha(conn, person_id, dia_sesion, anterior)}
        anterior = dia_sesion
    return None


def _tras_carga(conn: sqlite3.Connection, person_id: int, dia: date) -> dict | None:
    ayer = (dia - timedelta(1)).isoformat()
    filas = conn.execute(
        "SELECT * FROM activity WHERE person_id = ? AND day = ? ORDER BY start_local",
        (person_id, ayer),
    ).fetchall()
    if not filas:
        return None
    tipico = recuperacion.estudio(conn, person_id)["mediana"]
    for row in filas:
        resultado = recuperacion.recuperacion_actividad(conn, person_id, row)
        alto = (tipico is not None and resultado.minutos is not None
                and resultado.minutos > tipico * MODELO["carga_alta_factor"])
        if not alto:
            alto = _duracion_top_tercil(conn, person_id, row)
        if not alto:
            continue
        grupos = anatomia.grupos_de_categorias(_categorias_actividad(conn, person_id, row))
        if not grupos:
            continue
        return {"actividad": {"tipo": row["type"], "nombre": row["name"]},
                "recuperacion_min": resultado.minutos, "grupos": sorted(grupos)}
    return None


def senales(conn: sqlite3.Connection, person_id: int, dia: date) -> dict:
    """This one day's own `reentrada`/`tras_carga` signals, each present only when it applies
    (ruling 1's own "no key" omission; ruling 3's own "nothing to name" omission) -- {} when
    neither does. Another person's own sessions and activities never counted: every read this
    calls is already scoped by `person_id`."""
    out: dict = {}
    r = _reentrada(conn, person_id, dia)
    if r is not None:
        out["reentrada"] = r
    tc = _tras_carga(conn, person_id, dia)
    if tc is not None:
        out["tras_carga"] = tc
    return out
