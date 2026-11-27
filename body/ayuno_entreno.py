"""How a session's own start compares to the person's last meal (BODY-AYUNO-ENTRENO-1): Carlos,
relayed by the Personal desk 2026-11-24, asked "how I perform fasted, how long my fast windows
are". A NEW module, never an edit to body/ventana_comida.py (whose own docstring forbids
judgement) -- this one DOES judge, against the person's own history, versioned like every other
model in this app (docs/DESIGN.md rules 4, 5, 12).

Two entry points. `horas_desde_comida` is the single-session read `body/rutas/entreno_sesion.py`'s
note line and the assistant's own `ayuno` key both use. `comparar` is the session page's own
fasted-vs-fed comparison: ORCH-CARLOS-DELEGATES-TIER-1-1's own trap is read here once, never once
per historical session -- the whole pool's meal timestamps are fetched in ONE query, bounded to the
pool's own earliest day (minus `_MARGEN_DIAS`, so a meal logged the evening before that day is
still read), and every session's own hours are then resolved in Python against that one list
(`_horas_desde_lista`).

Neither `body/entreno_sesion.py` nor `body/esfuerzo_sesion.py` is in this slice's `touches`, so the
few lines of theirs this module's own `comparar` needs (the batched activity_detail read
`esfuerzo_sesion.historial` already does, `entreno_sesion.sesiones_anteriores`'s own pool) are read
through their PUBLIC functions and -- for the detail batching, which neither module exposes as a
function of its own -- written again here, not imported from a module this slice cannot touch.
"""
from __future__ import annotations

import bisect
import json
import statistics
from datetime import date, datetime, timedelta

from . import db, entreno_sesion, esfuerzo_sesion, garmin_adapter

MODELO = {"version": "ayuno-entreno-v1", "umbral_horas": 8, "ventana_busqueda_horas": 36}

# 36h back from a session that started at or just after local midnight reaches 1.5 calendar days
# earlier -- 2 full days of margin on the meal read's own lower bound covers that regardless of
# what hour any session in the pool actually started at.
_MARGEN_DIAS = 2

# Ruling 1: the SAME comparison-pool floor/ceiling entreno_sesion.py and esfuerzo_sesion.py already
# name -- read here rather than redefined, so one number still governs every module that splits this
# person's own history into a comparable pool.
HISTORIAL_N = entreno_sesion.HISTORIAL_N
HISTORIAL_MIN = entreno_sesion.HISTORIAL_MIN

_BUCKETS = ("en_ayunas", "alimentado")


def _sin_offset(ts: str) -> datetime:
    """The same naive reading ventana_comida.py's own `_sin_offset` gives `ingesta.ts_local` --
    duplicated rather than imported (ventana_comida.py is not in this slice's `touches`)."""
    dt = datetime.fromisoformat(ts)
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _horas_o_none(inicio_local: str, ultima_comida: str | None) -> float | None:
    if ultima_comida is None:
        return None
    horas = (_sin_offset(inicio_local) - _sin_offset(ultima_comida)).total_seconds() / 3600
    return None if horas > MODELO["ventana_busqueda_horas"] else horas


def horas_desde_comida(conn, person_id: int, inicio_local: str) -> float | None:
    """Hours from the person's own last `ingesta.ts_local` at or before `inicio_local` (a session's
    own start, naive local time) to that start. None when no timed meal is recorded before it at
    all, or when the nearest one sits beyond `MODELO['ventana_busqueda_horas']` -- beyond that
    window Body does not know, which is never read as "fasted that long"."""
    row = conn.execute(
        "SELECT MAX(ts_local) AS ultima FROM ingesta WHERE person_id = ? AND ts_local IS NOT NULL "
        "AND ts_local <= ?",
        (person_id, inicio_local),
    ).fetchone()
    return _horas_o_none(inicio_local, row["ultima"] if row else None)


def _horas_desde_lista(timestamps_asc: list[str], inicio_local: str) -> float | None:
    """The same rule `horas_desde_comida` applies through SQL, against an already-loaded, ascending
    list of this person's own meal timestamps -- `comparar`'s own one-read-for-the-whole-pool
    guarantee. ISO "YYYY-MM-DDTHH:MM:SS" strings sort the same as the instants they name, so a plain
    bisect finds the latest one at or before `inicio_local` with no parsing until the one hit is
    resolved to hours."""
    if not inicio_local:
        return None
    idx = bisect.bisect_right(timestamps_asc, inicio_local) - 1
    return _horas_o_none(inicio_local, timestamps_asc[idx] if idx >= 0 else None)


def _nombre_bucket(horas: float | None) -> str | None:
    if horas is None:
        return None
    return "en_ayunas" if horas >= MODELO["umbral_horas"] else "alimentado"


def _detalle_por_fuente(conn, person_id: int, filas) -> dict[tuple[str, str], list]:
    """Groups `filas` (activity rows carrying their own source/source_id) by source and reads their
    stored 'detalle'/'zonas' rows in ONE query per distinct source -- the same batching
    esfuerzo_sesion.historial already does for its own single pool, applied here per bucket.
    Duplicated rather than imported: esfuerzo_sesion.py is not in this slice's `touches`."""
    por_fuente: dict[str, list[str]] = {}
    for f in filas:
        por_fuente.setdefault(f["source"], []).append(f["source_id"])
    detalle: dict[tuple[str, str], list] = {}
    for source, ids in por_fuente.items():
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT activity_source_id AS source_id, kind, payload FROM activity_detail WHERE "
            f"person_id = ? AND activity_source = ? AND kind IN ('detalle', 'zonas') AND "
            f"activity_source_id IN ({placeholders})",
            (person_id, source, *ids)).fetchall()
        for r in rows:
            detalle.setdefault((source, r["source_id"]), []).append(r)
    return detalle


def _figuras_comparables(filas) -> dict:
    """entreno_sesion.COMPARABLE_KEYS' own median over the newest HISTORIAL_N of `filas` (already
    newest-first) that actually carry each figure -- the same honest-gap rule entreno_sesion.
    comparaciones applies to its own single pool."""
    out = {}
    for k in entreno_sesion.COMPARABLE_KEYS:
        vals = [f[k] for f in filas if f[k] is not None][:HISTORIAL_N]
        out[k] = statistics.median(vals) if len(vals) >= HISTORIAL_MIN else None
    return out


def _figuras_esfuerzo(filas, detalle: dict, zona_persona: str) -> dict:
    """The same zona-alta-share/recovery-drop medians esfuerzo_sesion.historial computes for its own
    single pool, applied here to one bucket's own `filas` (already newest-first)."""
    alta_vals, caida_vals = [], []
    for f in filas:
        rows = detalle.get((f["source"], f["source_id"]))
        if not rows:
            continue
        muestras = zonas_filas = ()
        for r in rows:
            payload = json.loads(r["payload"])
            if r["kind"] == "detalle":
                muestras = garmin_adapter.parse_activity_hr_series(payload, zona_persona)
            else:
                zonas_filas = garmin_adapter.parse_hr_zones(payload)
        if muestras:
            esf = esfuerzo_sesion.esfuerzos(muestras, f["start_local"], f["duration_s"])
            if esf["suficiente"] and esf["recuperacion"] is not None:
                caida_vals.append(esf["recuperacion"])
        if zonas_filas:
            z = esfuerzo_sesion.zonas(zonas_filas)
            if z is not None and z["alta_share"] is not None:
                alta_vals.append(z["alta_share"])
    alta_vals, caida_vals = alta_vals[:HISTORIAL_N], caida_vals[:HISTORIAL_N]
    return {
        "zona_alta": statistics.median(alta_vals) if len(alta_vals) >= HISTORIAL_MIN else None,
        "caida": statistics.median(caida_vals) if len(caida_vals) >= HISTORIAL_MIN else None,
    }


def _bucket_medianas(conn, person_id: int, filas, detalle: dict, zona_persona: str) -> dict:
    out = _figuras_comparables(filas)
    out.update(_figuras_esfuerzo(filas, detalle, zona_persona))
    return out


def comparar(conn, person_id: int, sesion: dict) -> dict | None:
    """Ruling 1: this session's own same-type (same-name, for Garmin's generic "other" bucket)
    history pool (entreno_sesion.sesiones_anteriores), split into `en_ayunas` (hours since the
    last meal >= MODELO['umbral_horas']) and `alimentado` (less); a session with unknown hours
    (no meal within the look-back window) sits in neither. None only when the pool itself is
    empty -- no earlier same-kind session at all. `n` always carries both buckets' raw counts;
    a bucket's own medians (COMPARABLE_KEYS plus esfuerzo_sesion's zona-alta share and recovery
    drop) sit under its name only once it has its OWN HISTORIAL_MIN sessions -- never computed,
    let alone shown, below that floor.

    The meal data behind every bucket is read ONCE, bounded to [the pool's earliest day .. this
    session's own day] -- never once per historical session, the trap this slice's own INV named."""
    columnas = "avg_hr, max_hr, calories, source, source_id, duration_s"
    pool = entreno_sesion.sesiones_anteriores(conn, person_id, sesion, columnas)
    if not pool:
        return None

    desde = (date.fromisoformat(min(f["day"] for f in pool)) - timedelta(_MARGEN_DIAS)).isoformat()
    hasta = sesion["day"]
    filas_comida = conn.execute(
        "SELECT ts_local FROM ingesta WHERE person_id = ? AND ts_local IS NOT NULL AND day BETWEEN ? "
        "AND ? ORDER BY ts_local",
        (person_id, desde, hasta),
    ).fetchall()
    timestamps = sorted(r["ts_local"] for r in filas_comida)

    buckets: dict[str, list] = {n: [] for n in _BUCKETS}
    for f in pool:
        nombre = _nombre_bucket(_horas_desde_lista(timestamps, f["start_local"]))
        if nombre:
            buckets[nombre].append(f)

    out: dict = {"version": MODELO["version"], "n": {n: len(fs) for n, fs in buckets.items()}}
    calificados = {n: fs for n, fs in buckets.items() if len(fs) >= HISTORIAL_MIN}
    if calificados:
        detalle = _detalle_por_fuente(conn, person_id, [f for fs in calificados.values() for f in fs])
        zona_persona = db.person(conn, person_id)["timezone"]
        for nombre, fs in calificados.items():
            out[nombre] = _bucket_medianas(conn, person_id, fs, detalle, zona_persona)
    for nombre in _BUCKETS:
        out.setdefault(nombre, None)
    return out


def comparar_tendencias(conn, person_id: int, desde: str | None, hasta: str | None) -> dict | None:
    """Ruling 3: Trends' own aggregate -- every typed session in [desde, hasta] (None, None reads
    the whole history), grouped the same way `entreno_sesion.sesiones_anteriores` groups a single
    session's pool (by type, or by folded name for Garmin's generic "other" bucket), split into
    `en_ayunas`/`alimentado` through the SAME batched meal read `comparar` uses -- one query for
    the activities, one for the meals, regardless of how many types or sessions the window holds
    (ruling 6: a fixed cost, never one read per source the window happens to carry). Each type's
    own COMPARABLE_KEYS medians, shown only when BOTH its buckets meet HISTORIAL_MIN -- esfuerzo_
    sesion's per-source figures are left out of this aggregate on purpose: reading them would cost
    one activity_detail query per distinct source, which is exactly the per-session cost ruling 6
    forbids here.

    None only when the window carries no typed, timed session at all -- the card is then omitted
    outright, the same "no card" choice entreno_racha.racha_semanal already makes for a person who
    has never logged a strength session. A window WITH sessions but nothing bucketed to compare
    still returns a dict (an honest empty state, never a blank card)."""
    where = "person_id = ? AND type IS NOT NULL AND start_local IS NOT NULL"
    params: list = [person_id]
    if desde and hasta:
        where += " AND day BETWEEN ? AND ?"
        params += [desde, hasta]
    filas = conn.execute(
        f"SELECT type, name, source, source_id, day, start_local, avg_hr, max_hr, calories "
        f"FROM activity WHERE {where} ORDER BY day DESC, start_local DESC",
        params,
    ).fetchall()
    if not filas:
        return None

    dias = [f["day"] for f in filas]
    desde_comida = (date.fromisoformat(min(dias)) - timedelta(_MARGEN_DIAS)).isoformat()
    filas_comida = conn.execute(
        "SELECT ts_local FROM ingesta WHERE person_id = ? AND ts_local IS NOT NULL AND day BETWEEN ? "
        "AND ? ORDER BY ts_local",
        (person_id, desde_comida, max(dias)),
    ).fetchall()
    timestamps = sorted(r["ts_local"] for r in filas_comida)

    horas_todas: list[float] = []
    grupos: dict = {}
    for f in filas:
        horas = _horas_desde_lista(timestamps, f["start_local"])
        if horas is not None:
            horas_todas.append(horas)
        clave = (f["type"], entreno_sesion._normaliza_nombre(f["name"])) \
            if f["type"] == entreno_sesion.TIPO_GENERICO else (f["type"], None)
        grupos.setdefault(clave, {"tipo": f["type"], "nombre": f["name"] if clave[1] else None,
                                   "en_ayunas": [], "alimentado": []})
        nombre = _nombre_bucket(horas)
        if nombre:
            grupos[clave][nombre].append(f)

    tipos = []
    for g in grupos.values():
        if len(g["en_ayunas"]) < HISTORIAL_MIN or len(g["alimentado"]) < HISTORIAL_MIN:
            continue
        tipos.append({
            "tipo": g["tipo"], "nombre": g["nombre"],
            "en_ayunas": _figuras_comparables(g["en_ayunas"]),
            "alimentado": _figuras_comparables(g["alimentado"]),
            "n": {"en_ayunas": len(g["en_ayunas"]), "alimentado": len(g["alimentado"])},
        })

    ventana_horas = statistics.median(horas_todas) if len(horas_todas) >= HISTORIAL_MIN else None
    return {"version": MODELO["version"], "tipos": tipos, "ventana_horas": ventana_horas}
