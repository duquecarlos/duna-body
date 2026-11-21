"""Day and night CONTEXT, derived on read (BODY-MINUTO-ANALISIS-5-CONTEXTO): Carlos, relayed by the
desk 2026-11-20, wants comparisons that can say "nights after a shift", "nights while travelling",
"nights after a late meal". BODY-MINUTO-ANALISIS-4-INV measured that every one of those (altitude
excepted, out of this slice) is already derivable from tables that exist, with three precedents of
deriving this kind of thing on read and storing nothing new: body/turnos_costo.py's `bloques_noche`,
body/ventana_comida.py's `comida_a_sueno`, body/practicas.py's `_contexto`, and body/noche_inusual.py's
own docstring ("Computed on read ... no derived table"). So this module is ONLY reads over the
readers that already exist -- never a new table, never a stored tag, never a re-implementation of
one of their rules.

`contexto_dias(conn, person_id, desde, hasta)` answers for a RANGE of calendar days: one bulk
statement per source table (shift, viaje_lugares, period, activity, recuperacion_actividad,
daily_metric_efectiva), never one per day, so its own statement count stays the same whether the
range is a week or ninety days (tests/test_contexto_dia.py's own guard). Each possible key below is
present on a given day's dict ONLY when that day actually has something to say -- never a zero, a
False or an invented value standing in for "no data":

  - `turno`: {"tipo": the shift's own stored kind for that day (body/db.py's SHIFT_KINDS), present
    only when a `shift` row exists; "bloque_noche": {"posicion" (1-based night number inside its own
    run), "largo" (the run's own total nights)}, present only when the day itself falls inside one of
    turnos_costo.bloques_noche's own night runs; "dias_desde_ultimo_turno_noche", present only when
    the day falls inside turnos_costo.MODELO["ventana_post"] days after the LAST night of the nearest
    earlier run -- the exact recovery look-back turnos_costo.referencia()/dias_post_noche() already
    use, cited here rather than re-defined.}
  - `viaje`: {"ref", "dia" (1-based day-in-trip)} from one of this person's own cached Lugares trips
    of 2+ days (body/viajes.py's own definition of what counts as "a trip" -- a single day Lugares
    reports is never one, mirrored here rather than re-derived from scratch), OR {"dia"} alone from a
    marked `period` of kind 'viaje' covering the day when no Lugares trip does. A Lugares trip wins
    when both exist for the same day.
  - `periodos`: every one of this person's own `period` rows (any kind, read through db.periodos --
    never a hardcoded kind list) covering the day, each as {"kind", and "zonas" when the period itself
    carries one}.
  - `carga`: {"actividades" (count), "minutos" (summed activity duration_s / 60)}, present only when
    at least one `activity` row exists that day; "recuperacion_min" (summed recuperacion_actividad.
    minutos for that day's activities, current model version only) added only when at least one of
    them has a computed figure.
  - `alcohol_g`: the day's own stored `alcohol_g` figure (daily_metric_efectiva), present only when
    one exists.
  - `desplazamiento` (ORCH-CARLOS-DELEGATES-TIER-1-1, BODY-MINUTO-ANALISIS-7): {each of the Personal
    commute source's own metric keys (fuentes.FUENTES[desplazamiento_import.FUENTE]["metric_keys"],
    never retyped) that the day actually has, with its stored value} -- read for the whole range in
    ONE query over `daily_metric_efectiva`, present only when at least one of those keys has a
    value, never a 0 filled in for a key the day lacks.
  - `altitud` (BODY-MINUTO-ANALISIS-6-ALTITUD, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2):
    {"altitud_m", "casa_m", "cambio_m" (signed: altitud_m minus casa_m), "fuente"}, present only
    when BOTH a home row (db.altitud_estancia_rango's own es_casa=1 rows) covers the day AND
    EITHER a stay row covers it too (altitud_m/fuente are the STAY's own) OR no trip/'viaje'
    period covers it (the day's own `viaje` key above is None -- then the person is taken to be at
    home: altitud_m = casa_m, cambio_m = 0, fuente is the HOME row's own). A day inside a trip or a
    'viaje' period with no stay row carries NO `altitud` key at all -- unknown, never guessed as
    home. Read for the whole range in ONE statement (db.altitud_estancia_rango), split into home
    and stay rows in pure Python; a night takes its evening day's own `altitud`, like every other
    key in this list except `viaje`/`periodos` (contexto_noche's own evening/wake-day merge never
    touches this key).

`contexto_noche(conn, person_id, dia_despertar)` answers for the ONE night Garmin files under
`dia_despertar` (body/agente/herramientas.py's `_leer_noche` keys a night by its own wake day, the
same convention body/ventana_comida.py's `comida_a_sueno` documents: "`sleep_session` files a night
under its WAKE day"). A night's own context is the EVENING's -- the day before `dia_despertar`: its
shift, its load, its alcohol -- since that is the day whose work and choices the night in question
actually followed. `comida_antes_de_dormir` is the one key with no per-day equivalent in
`contexto_dias`: it is literally `comida_a_sueno(evening)` (that function's own gap from the evening's
last timed meal to THIS night's own sleep start), converted to minutes, added only when that gap
exists. `viaje`/`periodos` are read over BOTH the evening day and the wake day and merged (a trip or
a marked period can start exactly on the wake day without yet covering the evening before it), since
the night itself spans both calendar dates. `turno`/`carga`/`alcohol_g`/`desplazamiento` stay the
evening's alone, per Carlos's own framing above ("nights after a shift", "nights after a late meal"
-- the day that produced the night, not the day the night happens to be filed under): a night takes
its evening day's commute, the same mapping those other evening-only keys already use.

Nothing here ever reads another person's rows: every statement below is scoped by `person_id`, the
same as every reader it calls.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import db, desplazamiento_import, fuentes, recuperacion, turnos_costo, ventana_comida

CONTEXT_KEYS = ("turno", "viaje", "periodos", "carga", "alcohol_g", "desplazamiento",
                "comida_antes_de_dormir", "altitud")


def _dia_kind_por_dia(conn, person_id: int, desde: str, hasta: str) -> dict[str, str]:
    """This person's own shift kind, one per day, first wins -- the same resolution
    body/practicas.py's `_turnos_en_rango` and body/history.py's `by_shift` already apply."""
    rows = conn.execute(
        "SELECT day, kind FROM shift WHERE person_id = ? AND day BETWEEN ? AND ? ORDER BY day",
        (person_id, desde, hasta),
    ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        out.setdefault(r["day"], r["kind"])
    return out


def _turno_por_dia(conn, person_id: int, desde: str, hasta: str) -> dict[str, dict]:
    """turnos_costo.bloques_noche called over an EXTENDED range (ventana_post days before `desde`)
    so a night run that ENDED before `desde` but whose own recovery tail still reaches into
    [desde, hasta] is not missed -- one extra statement, never one per day."""
    ventana_post = turnos_costo.MODELO["ventana_post"]
    desde_ext = (date.fromisoformat(desde) - timedelta(ventana_post)).isoformat()
    bloques = turnos_costo.bloques_noche(conn, person_id, desde_ext, hasta)
    dia_kind = _dia_kind_por_dia(conn, person_id, desde, hasta)

    out: dict[str, dict] = {}
    for b in bloques:
        inicio_d, fin_d = date.fromisoformat(b.inicio), date.fromisoformat(b.fin)
        d, pos = inicio_d, 1
        while d <= fin_d:
            day = d.isoformat()
            if desde <= day <= hasta:
                out.setdefault(day, {})["bloque_noche"] = {"posicion": pos, "largo": b.dias}
            d += timedelta(1)
            pos += 1
        for i in range(1, ventana_post + 1):
            day = (fin_d + timedelta(i)).isoformat()
            if desde <= day <= hasta:
                entry = out.setdefault(day, {})
                actual = entry.get("dias_desde_ultimo_turno_noche")
                if actual is None or i < actual:
                    entry["dias_desde_ultimo_turno_noche"] = i

    for day, kind in dia_kind.items():
        out.setdefault(day, {})["tipo"] = kind
    return out


def _actividad_por_dia(conn, person_id: int, desde: str, hasta: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT day, COUNT(*) AS n, SUM(duration_s) AS dur FROM activity "
        "WHERE person_id = ? AND day BETWEEN ? AND ? GROUP BY day",
        (person_id, desde, hasta),
    ).fetchall()
    return {r["day"]: {"actividades": r["n"], "minutos": (r["dur"] or 0.0) / 60} for r in rows}


def _recuperacion_por_dia(conn, person_id: int, desde: str, hasta: str) -> dict[str, float]:
    """Summed `recuperacion_actividad.minutos` per day, current model version only (the same
    version `recuperacion.estudio` filters on) -- never a stale earlier-version row mixed in."""
    rows = conn.execute(
        "SELECT a.day AS day, SUM(ra.minutos) AS rec FROM recuperacion_actividad ra "
        "JOIN activity a ON a.person_id = ra.person_id AND a.source = ra.activity_source "
        "AND a.source_id = ra.activity_source_id "
        "WHERE ra.person_id = ? AND ra.modelo_version = ? AND a.day BETWEEN ? AND ? "
        "AND ra.minutos IS NOT NULL GROUP BY a.day",
        (person_id, recuperacion.VERSION, desde, hasta),
    ).fetchall()
    return {r["day"]: r["rec"] for r in rows}


def _alcohol_por_dia(conn, person_id: int, desde: str, hasta: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT day, value FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = 'alcohol_g' "
        "AND day BETWEEN ? AND ?",
        (person_id, desde, hasta),
    ).fetchall()
    return {r["day"]: r["value"] for r in rows}


def _desplazamiento_por_dia(conn, person_id: int, desde: str, hasta: str) -> dict[str, dict]:
    """day -> {metric_key: value}, ONE query over daily_metric_efectiva for the Personal commute
    source's own metric keys -- read from fuentes.FUENTES[desplazamiento_import.FUENTE]["metric_keys"],
    never retyped here, so a later change to that source's own key list is picked up automatically."""
    claves = fuentes.FUENTES[desplazamiento_import.FUENTE]["metric_keys"]
    filas = conn.execute(
        "SELECT day, metric_key, value FROM daily_metric_efectiva WHERE person_id = ? AND day BETWEEN ? AND ? "
        f"AND metric_key IN ({','.join('?' for _ in claves)})",
        (person_id, desde, hasta, *claves),
    ).fetchall()
    out: dict[str, dict] = {}
    for f in filas:
        out.setdefault(f["day"], {})[f["metric_key"]] = f["value"]
    return out


def _altitud_rango(conn, person_id: int, desde: str, hasta: str) -> tuple[list[dict], list[dict]]:
    """db.altitud_estancia_rango's own ONE statement for the whole range, split into this
    person's own home rows (es_casa=1) and stay rows (es_casa=0) -- BODY-MINUTO-ANALISIS-6-ALTITUD
    ruling 2."""
    filas = db.altitud_estancia_rango(conn, person_id, desde, hasta)
    casas = [f for f in filas if f["es_casa"]]
    estancias = [f for f in filas if not f["es_casa"]]
    return casas, estancias


def _fila_altitud_para_dia(filas: list[dict], day: str) -> dict | None:
    for f in filas:
        if f["start_day"] <= day and (f["end_day"] is None or day <= f["end_day"]):
            return f
    return None


def _viajes_2_mas_dias(conn, person_id: int, desde: str, hasta: str) -> list[dict]:
    """db.viajes_lugares's own rows, filtered to 2+-day spans -- body/viajes.py's own definition of
    what counts as "a trip" anywhere in this app, mirrored here (not re-read through viajes.py
    itself, which would cost a second statement over the same table)."""
    out = []
    for t in db.viajes_lugares(conn, person_id, desde, hasta):
        s, e = date.fromisoformat(t["starts_at"]), date.fromisoformat(t["ends_at"])
        if (e - s).days + 1 >= 2:
            out.append(t)
    return out


def _viaje_para_dia(trips: list[dict], periodos_viaje: list, day: str) -> dict | None:
    d = date.fromisoformat(day)
    for t in trips:
        s, e = date.fromisoformat(t["starts_at"]), date.fromisoformat(t["ends_at"])
        if s <= d <= e:
            return {"ref": t["ref"], "dia": (d - s).days + 1}
    for p in periodos_viaje:
        s = date.fromisoformat(p["start_day"])
        e = date.fromisoformat(p["end_day"]) if p["end_day"] else None
        if s <= d and (e is None or d <= e):
            return {"dia": (d - s).days + 1}
    return None


def _periodos_para_dia(periodos_rows: list, day: str) -> list[dict]:
    out = []
    for r in periodos_rows:
        if r["start_day"] > day or (r["end_day"] is not None and r["end_day"] < day):
            continue
        entry = {"kind": r["kind"]}
        if r["zonas"]:
            entry["zonas"] = json.loads(r["zonas"])
        out.append(entry)
    return out


def contexto_dias(conn, person_id: int, desde: str, hasta: str) -> dict[str, dict]:
    """Every day in [desde, hasta] (ISO, inclusive) -> its own derived context dict, built from one
    bulk statement per source table (see the module docstring for the full list) -- the per-day loop
    below is pure Python over already-fetched rows, so it adds no further statements whatever the
    range's length."""
    turno_por_dia = _turno_por_dia(conn, person_id, desde, hasta)
    trips = _viajes_2_mas_dias(conn, person_id, desde, hasta)
    periodos_rows = db.periodos(conn, person_id, desde, hasta)
    periodos_viaje = [r for r in periodos_rows if r["kind"] == "viaje"]
    actividad_por_dia = _actividad_por_dia(conn, person_id, desde, hasta)
    recuperacion_por_dia = _recuperacion_por_dia(conn, person_id, desde, hasta)
    alcohol_por_dia = _alcohol_por_dia(conn, person_id, desde, hasta)
    desplazamiento_por_dia = _desplazamiento_por_dia(conn, person_id, desde, hasta)
    casas, estancias = _altitud_rango(conn, person_id, desde, hasta)

    out: dict[str, dict] = {}
    d, hasta_d = date.fromisoformat(desde), date.fromisoformat(hasta)
    while d <= hasta_d:
        day = d.isoformat()
        entry: dict = {}
        turno = turno_por_dia.get(day)
        if turno:
            entry["turno"] = turno
        viaje = _viaje_para_dia(trips, periodos_viaje, day)
        if viaje is not None:
            entry["viaje"] = viaje
        periodos_dia = _periodos_para_dia(periodos_rows, day)
        if periodos_dia:
            entry["periodos"] = periodos_dia
        carga = actividad_por_dia.get(day)
        if carga is not None and carga["actividades"] > 0:
            c = dict(carga)
            rec = recuperacion_por_dia.get(day)
            if rec is not None:
                c["recuperacion_min"] = rec
            entry["carga"] = c
        alcohol = alcohol_por_dia.get(day)
        if alcohol is not None:
            entry["alcohol_g"] = alcohol
        desplazamiento = desplazamiento_por_dia.get(day)
        if desplazamiento:
            entry["desplazamiento"] = desplazamiento
        casa = _fila_altitud_para_dia(casas, day)
        if casa is not None:
            estancia = _fila_altitud_para_dia(estancias, day)
            if estancia is not None:
                entry["altitud"] = {"altitud_m": estancia["altitud_m"], "casa_m": casa["altitud_m"],
                                     "cambio_m": estancia["altitud_m"] - casa["altitud_m"],
                                     "fuente": estancia["fuente"]}
            elif viaje is None:
                entry["altitud"] = {"altitud_m": casa["altitud_m"], "casa_m": casa["altitud_m"],
                                     "cambio_m": 0, "fuente": casa["fuente"]}
        out[day] = entry
        d += timedelta(1)
    return out


def contexto_noche(conn, person_id: int, dia_despertar: str) -> dict:
    """The one night Garmin files under `dia_despertar` -- see the module docstring for the
    evening/wake-day mapping. `contexto_dias` is called over the two calendar dates the night
    itself spans (the evening and the wake day), never a wider range, so this stays a fixed,
    range-independent statement count regardless of which night is asked about."""
    evening = (date.fromisoformat(dia_despertar) - timedelta(1)).isoformat()
    base = contexto_dias(conn, person_id, evening, dia_despertar)
    entry = dict(base.get(evening, {}))

    wake_entry = base.get(dia_despertar, {})
    if "viaje" not in entry and "viaje" in wake_entry:
        entry["viaje"] = wake_entry["viaje"]
    if "periodos" in wake_entry:
        ya = {p["kind"] for p in entry.get("periodos", [])}
        extra = [p for p in wake_entry["periodos"] if p["kind"] not in ya]
        if extra:
            entry["periodos"] = entry.get("periodos", []) + extra

    comida = ventana_comida.comida_a_sueno(conn, person_id, evening)
    if comida is not None:
        entry["comida_antes_de_dormir"] = {"minutos": comida.horas * 60}
    return entry
