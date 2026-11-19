"""BODY-HISTORIA-VIDA-1 (ORCH-CARLOS-DELEGATES-TIER-1-1, on BODY-HISTORIA-VIDA-1-INV's findings):
a practice is WHAT THE WATCH DATA SHOWS the person did -- kept apart from a habit, what they
COMMIT to (goals.add_habit). Pure reads over a connection and a person id for the math; the
writers (`crear`/`archivar`) are the only place `practica` rows are ever inserted or changed, since
the table carries no CHECK on `fuente` or `origen` (ruling B) -- both are validated here, in
Python, never raising on bad input, always returning a reason code instead.

Every day of a practice is one of exactly THREE states (ruling C): `hecho`, `no_hecho` or
`sin_dato` -- a day with no watch/self data at all (a commute day not yet imported, a sensor off)
is `sin_dato`, never `no_hecho`; `sin_dato` never counts against the person. Streaks and seasons
are computed ON READ, versioned by MODELO (ruling D) -- nothing derived here is ever stored. The
run-detection itself (`fechas.rachas_de_estados`) is shared with `entreno_racha.racha_semanal`,
generalised from its own week-run loop, not duplicated (ruling 4).

No person-facing text in this slice (BODY-HISTORIA-VIDA-1's own ruling): the screen and the
assistant tools are BODY-HISTORIA-VIDA-2.
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import db, viajes
from .fechas import lunes_de, rachas_de_estados

MODELO = {
    "version": "practicas-v1",
    # Ruling 4: up to this many consecutive `sin_dato` days are bridged inside a day-level racha --
    # they neither break nor extend it, and are counted separately as `dias_puente`.
    "PUENTE_SIN_DATO": 2,
    # Ruling 4: a week with fewer data days (hecho + no_hecho) than this is `semana_sin_dato` --
    # too little watch coverage that week to say whether the practice's own weekly goal was met.
    "MIN_DIAS_CON_DATO": 4,
    # Ruling 4: a `temporada` is a run of at least this many consecutive `cumple` weeks, bridging
    # at most one `semana_sin_dato` week.
    "MIN_SEMANAS_TEMPORADA": 3,
    # Ruling 6: an activity type, or a walking/cycling-minutes day, must show up in at least this
    # many distinct weeks of the person's own history before `sugerencias` offers it.
    "MIN_SEMANAS_SUGERENCIA": 8,
}

ORIGENES = ("persona", "asistente", "sugerida")

REASON_CODES = (
    "fuente_desconocida", "origen_invalido", "titulo_invalido", "meta_dias_semana_invalida",
    "regla_invalida", "tipos_invalidos", "min_minutos_invalido", "metric_key_desconocida",
    "op_invalido", "valor_invalido", "campo_invalido", "hora_invalida",
)

_HORA_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Ruling 5's own two context keys, beside shift kind.
CONTEXTO_KEYS = ("sleep_minutes", "readiness")


# ---- ruling 2: FUENTES, each with its regla shape and a validator -----------------------------

def _validar_actividad(regla) -> str | None:
    if not isinstance(regla, dict):
        return "regla_invalida"
    tipos = regla.get("tipos")
    if not isinstance(tipos, list) or not tipos or not all(isinstance(t, str) and t for t in tipos):
        return "tipos_invalidos"
    if "min_minutos" in regla and regla["min_minutos"] is not None:
        mm = regla["min_minutos"]
        if not isinstance(mm, int) or isinstance(mm, bool) or mm < 1:
            return "min_minutos_invalido"
    if set(regla) - {"tipos", "min_minutos"}:
        return "regla_invalida"
    return None


def _validar_metrica_umbral(regla) -> str | None:
    if not isinstance(regla, dict):
        return "regla_invalida"
    claves = {d[0] for d in db.METRIC_DEFS}
    if regla.get("metric_key") not in claves:
        return "metric_key_desconocida"
    if regla.get("op") not in (">=", "<="):
        return "op_invalido"
    valor = regla.get("valor")
    if not isinstance(valor, (int, float)) or isinstance(valor, bool):
        return "valor_invalido"
    if set(regla) - {"metric_key", "op", "valor"}:
        return "regla_invalida"
    return None


def _validar_sueno_horario(regla) -> str | None:
    if not isinstance(regla, dict):
        return "regla_invalida"
    if regla.get("campo") not in ("inicio", "fin"):
        return "campo_invalido"
    if regla.get("op") not in ("antes", "despues"):
        return "op_invalido"
    hora = regla.get("hora")
    if not isinstance(hora, str) or not _HORA_RE.match(hora):
        return "hora_invalida"
    if set(regla) - {"campo", "op", "hora"}:
        return "regla_invalida"
    return None


FUENTES = {
    "actividad": _validar_actividad,
    "metrica_umbral": _validar_metrica_umbral,
    "sueno_horario": _validar_sueno_horario,
}


# ---- writers ------------------------------------------------------------------------------------

def crear(conn: sqlite3.Connection, person_id: int, titulo: str, fuente: str, regla: dict,
          meta_dias_semana: int = 5, origen: str = "persona") -> int | str:
    """Ruling 2. Returns the new row's id, or a REASON_CODES string -- never raises on bad input."""
    if fuente not in FUENTES:
        return "fuente_desconocida"
    if origen not in ORIGENES:
        return "origen_invalido"
    if not isinstance(titulo, str) or not titulo.strip():
        return "titulo_invalido"
    if (not isinstance(meta_dias_semana, int) or isinstance(meta_dias_semana, bool)
            or not (1 <= meta_dias_semana <= 7)):
        return "meta_dias_semana_invalida"
    razon = FUENTES[fuente](regla)
    if razon:
        return razon
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO practica (person_id, titulo, fuente, regla_json, meta_dias_semana, origen, "
            "creada_en) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (person_id, titulo.strip(), fuente, json.dumps(regla, sort_keys=True), meta_dias_semana,
             origen, ahora),
        )
    return cur.lastrowid


def archivar(conn: sqlite3.Connection, person_id: int, practica_id: int) -> bool:
    """Sets `archivada_en` -- never deletes. False when there is no such live row for this person."""
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "UPDATE practica SET archivada_en = ? WHERE id = ? AND person_id = ? AND archivada_en IS NULL",
            (ahora, practica_id, person_id),
        )
    return cur.rowcount > 0


def editar(conn: sqlite3.Connection, person_id: int, practica_id: int, titulo: str,
           meta_dias_semana: int) -> bool | str:
    """BODY-HISTORIA-VIDA-3 ruling 2: the same titulo/meta_dias_semana validation `crear` applies,
    against an existing row instead of an inserted one -- fuente and regla never change here, a
    person retitles or retargets a practice, never reclassifies its source. Returns True on
    success, a REASON_CODES string on bad input (nothing written), or False when there is no such
    live row for this person (the route's own 404, same as `archivar`)."""
    if not isinstance(titulo, str) or not titulo.strip():
        return "titulo_invalido"
    if (not isinstance(meta_dias_semana, int) or isinstance(meta_dias_semana, bool)
            or not (1 <= meta_dias_semana <= 7)):
        return "meta_dias_semana_invalida"
    with conn:
        cur = conn.execute(
            "UPDATE practica SET titulo = ?, meta_dias_semana = ? "
            "WHERE id = ? AND person_id = ? AND archivada_en IS NULL",
            (titulo.strip(), meta_dias_semana, practica_id, person_id),
        )
    return cur.rowcount > 0


def propia_activa(conn: sqlite3.Connection, person_id: int, practica_id: int) -> bool:
    """BODY-HISTORIA-VIDA-2 ruling 4: true only for this person's own, un-archived practice --
    the one check every habit-practica link (proponer_meta/proponer_plan habitos, goal page
    'Retomar') goes through, so a habit can never cite another person's practice or an archived
    one as its evidence source."""
    row = conn.execute(
        "SELECT 1 FROM practica WHERE id = ? AND person_id = ? AND archivada_en IS NULL",
        (practica_id, person_id),
    ).fetchone()
    return row is not None


def listar(conn: sqlite3.Connection, person_id: int, incluir_archivadas: bool = False) -> list[dict]:
    where = "person_id = ?" + ("" if incluir_archivadas else " AND archivada_en IS NULL")
    rows = conn.execute(
        f"SELECT id, titulo, fuente, regla_json, meta_dias_semana, origen, creada_en, archivada_en "
        f"FROM practica WHERE {where} ORDER BY creada_en, id",
        (person_id,),
    ).fetchall()
    return [
        {"id": r["id"], "titulo": r["titulo"], "fuente": r["fuente"], "regla": json.loads(r["regla_json"]),
         "meta_dias_semana": r["meta_dias_semana"], "origen": r["origen"], "creada_en": r["creada_en"],
         "archivada_en": r["archivada_en"]}
        for r in rows
    ]


# ---- ruling 3: three states a day, one SELECT per source family for the whole range -----------

def _dias_rango(desde: date, hasta: date) -> list[str]:
    out = []
    d = desde
    while d <= hasta:
        out.append(d.isoformat())
        d += timedelta(1)
    return out


def _estados_de(desde: date, hasta: date, hecho: set[str], tiene_dato: set[str]) -> dict[str, str]:
    out = {}
    for dia in _dias_rango(desde, hasta):
        if dia in hecho:
            out[dia] = "hecho"
        elif dia in tiene_dato:
            out[dia] = "no_hecho"
        else:
            out[dia] = "sin_dato"
    return out


def _estados_actividad(regla: dict, desde: date, hasta: date, metric_days: set[str],
                        act_rows: list) -> dict[str, str]:
    """Ruling 1 (BODY-HISTORIA-VIDA-2-R2): the pure compute half of `_calcular_actividad`, shared
    with `resumen()` below, which fetches `metric_days`/`act_rows` ONCE for every active 'actividad'
    practica together instead of once per practica."""
    vinculadas = {r["vinculo_manual"] for r in act_rows if r["vinculo_manual"]}
    tipos = set(regla["tipos"])
    min_minutos = regla.get("min_minutos")
    act_days: set[str] = set()
    hecho: set[str] = set()
    for r in act_rows:
        act_days.add(r["day"])
        if r["source"] == "manual" and r["source_id"] in vinculadas:
            continue
        if r["type"] not in tipos:
            continue
        if min_minutos is not None and (r["duration_s"] is None or r["duration_s"] < min_minutos * 60):
            continue
        hecho.add(r["day"])
    return _estados_de(desde, hasta, hecho, metric_days | act_days)


def _calcular_actividad(conn, person_id: int, desde: date, hasta: date, regla: dict) -> dict[str, str]:
    """"Has data": any Garmin daily_metric row OR any activity row (the watch was worn). "Hecho":
    the linked-manual-half drop `history.training` applies (history.py:135-140) -- a linked pair
    counts through its watch half -- then a matching `type` of at least `min_minutos`, if given."""
    desde_s, hasta_s = desde.isoformat(), hasta.isoformat()
    metric_days = {r["day"] for r in conn.execute(
        "SELECT DISTINCT day FROM daily_metric WHERE person_id = ? AND source = 'garmin' "
        "AND day BETWEEN ? AND ?", (person_id, desde_s, hasta_s))}
    act_rows = conn.execute(
        "SELECT day, type, duration_s, source, source_id, vinculo_manual FROM activity "
        "WHERE person_id = ? AND day BETWEEN ? AND ?", (person_id, desde_s, hasta_s),
    ).fetchall()
    return _estados_actividad(regla, desde, hasta, metric_days, act_rows)


def _estados_metrica_umbral(regla: dict, desde: date, hasta: date, rows: list) -> dict[str, str]:
    """Ruling 1's pure compute half of `_calcular_metrica_umbral`, shared with `resumen()`."""
    op, valor = regla["op"], regla["valor"]
    tiene_dato = {r["day"] for r in rows}
    hecho = {r["day"] for r in rows if (r["value"] >= valor if op == ">=" else r["value"] <= valor)}
    return _estados_de(desde, hasta, hecho, tiene_dato)


def _calcular_metrica_umbral(conn, person_id: int, desde: date, hasta: date, regla: dict) -> dict[str, str]:
    """"Has data": a daily_metric row for `metric_key`, any source."""
    rows = conn.execute(
        "SELECT day, value FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = ? AND day BETWEEN ? AND ?",
        (person_id, regla["metric_key"], desde.isoformat(), hasta.isoformat()),
    ).fetchall()
    return _estados_metrica_umbral(regla, desde, hasta, rows)


def _hora_a_minutos(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _campo_a_minutos(ts: str | None) -> int | None:
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts)
    return dt.hour * 60 + dt.minute


def _estados_sueno_horario(regla: dict, desde: date, hasta: date, rows: list) -> dict[str, str]:
    """Ruling 1's pure compute half of `_calcular_sueno_horario`, shared with `resumen()`. `rows`
    carries BOTH `start_local` and `end_local` (resumen() fetches once for every active
    'sueno_horario' practica together, whichever `campo` each one's own regla picks)."""
    campo = "start_local" if regla["campo"] == "inicio" else "end_local"
    limite = _hora_a_minutos(regla["hora"])
    op = regla["op"]
    tiene_dato = {r["day"] for r in rows}
    hecho: set[str] = set()
    for r in rows:
        minutos = _campo_a_minutos(r[campo])
        if minutos is None:
            continue
        if (op == "antes" and minutos <= limite) or (op == "despues" and minutos >= limite):
            hecho.add(r["day"])
    return _estados_de(desde, hasta, hecho, tiene_dato)


def _calcular_sueno_horario(conn, person_id: int, desde: date, hasta: date, regla: dict) -> dict[str, str]:
    """"Has data": a sleep_session row -- the wake day is the day, as stored (ruling 2)."""
    rows = conn.execute(
        "SELECT day, start_local, end_local FROM sleep_session WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, desde.isoformat(), hasta.isoformat()),
    ).fetchall()
    return _estados_sueno_horario(regla, desde, hasta, rows)


_CALCULADORAS = {
    "actividad": _calcular_actividad,
    "metrica_umbral": _calcular_metrica_umbral,
    "sueno_horario": _calcular_sueno_horario,
}


def dias_estado(conn: sqlite3.Connection, person_id: int, fuente: str, regla: dict, desde: date,
                 hasta: date) -> dict[str, str]:
    """Ruling 3: day (ISO string) -> 'hecho' | 'no_hecho' | 'sin_dato', for every day in
    [desde, hasta] inclusive. One SELECT per source family for the whole range -- never one per
    day or per week."""
    return _CALCULADORAS[fuente](conn, person_id, desde, hasta, regla)


def _primero_ultimo_dato(conn: sqlite3.Connection, person_id: int, fuente: str,
                          regla: dict) -> tuple[date | None, date | None]:
    """Ruling 7's default range: first to last day with data for this practice's own source
    family -- one SELECT, never scanning day by day."""
    if fuente == "actividad":
        row = conn.execute(
            "SELECT MIN(day) AS a, MAX(day) AS b FROM "
            "(SELECT day FROM daily_metric WHERE person_id = ? AND source = 'garmin' "
            "UNION SELECT day FROM activity WHERE person_id = ?)", (person_id, person_id),
        ).fetchone()
    elif fuente == "metrica_umbral":
        row = conn.execute(
            "SELECT MIN(day) AS a, MAX(day) AS b FROM daily_metric WHERE person_id = ? AND metric_key = ?",
            (person_id, regla["metric_key"]),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MIN(day) AS a, MAX(day) AS b FROM sleep_session WHERE person_id = ?", (person_id,),
        ).fetchone()
    if row["a"] is None:
        return None, None
    return date.fromisoformat(row["a"]), date.fromisoformat(row["b"])


# ---- ruling 4: day-level racha ------------------------------------------------------------------

def _racha_dict(r: dict, dias_ordenados: list[str]) -> dict:
    return {"desde": dias_ordenados[r["inicio"]], "hasta": dias_ordenados[r["fin"]],
            "dias": r["fin"] - r["inicio"] + 1, "dias_puente": len(r["puentes"])}


def _rachas_dia(dias_ordenados: list[str], estados: dict[str, str]) -> tuple[dict | None, dict | None, int]:
    lista = [estados[d] for d in dias_ordenados]
    runs = rachas_de_estados(lista, on="hecho", bridge="sin_dato", bridge_max=MODELO["PUENTE_SIN_DATO"])
    actual = runs[-1] if runs and runs[-1]["abierta"] else None
    mas_larga = None
    if runs:
        max_len = max(r["fin"] - r["inicio"] + 1 for r in runs)
        mas_larga = next(r for r in reversed(runs) if r["fin"] - r["inicio"] + 1 == max_len)
    return (
        _racha_dict(actual, dias_ordenados) if actual else None,
        _racha_dict(mas_larga, dias_ordenados) if mas_larga else None,
        len(runs),
    )


# ---- ruling 4: weekly rollup and temporadas -----------------------------------------------------

def _semanas(dias_ordenados: list[str], estados: dict[str, str], meta_dias_semana: int) -> list[dict]:
    por_semana: dict[date, list[str]] = defaultdict(list)
    for d in dias_ordenados:
        por_semana[lunes_de(date.fromisoformat(d))].append(d)
    out = []
    for lunes in sorted(por_semana):
        dias_semana = por_semana[lunes]
        ests = [estados[d] for d in dias_semana]
        hecho = ests.count("hecho")
        no_hecho = ests.count("no_hecho")
        sin_dato = ests.count("sin_dato")
        semana_sin_dato = (hecho + no_hecho) < MODELO["MIN_DIAS_CON_DATO"]
        cumple = None if semana_sin_dato else hecho >= meta_dias_semana
        out.append({
            "lunes": lunes.isoformat(), "hecho": hecho, "no_hecho": no_hecho, "sin_dato": sin_dato,
            "semana_sin_dato": semana_sin_dato, "cumple": cumple,
            "primer_dia": dias_semana[0], "ultimo_dia": dias_semana[-1],
        })
    return out


def _temporadas_crudas(semanas: list[dict]) -> list[dict]:
    """Ruling 4's `temporada`: a run of >= MODELO['MIN_SEMANAS_TEMPORADA'] consecutive `cumple`
    weeks, bridging at most one `semana_sin_dato` week -- the SAME shared helper as the day-level
    racha above, run a second time over weeks instead of days."""
    estados_semana = [
        "semana_sin_dato" if s["semana_sin_dato"] else ("cumple" if s["cumple"] else "no_cumple")
        for s in semanas
    ]
    runs = rachas_de_estados(estados_semana, on="cumple", bridge="semana_sin_dato", bridge_max=1)
    out = []
    for r in runs:
        span = r["fin"] - r["inicio"] + 1
        if span < MODELO["MIN_SEMANAS_TEMPORADA"]:
            continue
        hecho_total = sum(semanas[i]["hecho"] for i in range(r["inicio"], r["fin"] + 1))
        out.append({
            "desde": semanas[r["inicio"]]["lunes"], "hasta": semanas[r["fin"]]["lunes"],
            "semanas": span, "dias_hecho": hecho_total, "tasa": hecho_total / (span * 7),
            "_primer_dia": semanas[r["inicio"]]["primer_dia"], "_ultimo_dia": semanas[r["fin"]]["ultimo_dia"],
        })
    return out


# ---- ruling 5: context, on read, for a temporada and for the whole history as the baseline -----

def _turnos_en_rango(conn, person_id: int, desde: str, hasta: str) -> dict[str, int]:
    """One kind per day, first wins -- the same resolution `history.by_shift` already applies."""
    rows = conn.execute(
        "SELECT day, kind FROM shift WHERE person_id = ? AND day BETWEEN ? AND ? ORDER BY day",
        (person_id, desde, hasta),
    ).fetchall()
    day_kind: dict[str, str] = {}
    for r in rows:
        day_kind.setdefault(r["day"], r["kind"])
    counts: dict[str, int] = defaultdict(int)
    for kind in day_kind.values():
        counts[kind] += 1
    return dict(counts)


def _contexto(conn, person_id: int, desde: str, hasta: str, viaje_dias_totales: set[str]) -> dict:
    """viaje_dias_totales is BODY-HISTORIA-VIDA-LUGARES-2-R ruling 7's own one-read-per-route trip
    set (the caller's whole span, read once by `estado()`) -- this just counts the slice of it
    inside [desde, hasta], never a second statement."""
    dias = (date.fromisoformat(hasta) - date.fromisoformat(desde)).days + 1
    turnos = _turnos_en_rango(conn, person_id, desde, hasta)
    viaje_dias = sum(1 for d in viaje_dias_totales if desde <= d <= hasta)
    medias: dict[str, float | None] = {}
    for key in CONTEXTO_KEYS:
        rows = conn.execute(
            "SELECT value FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = ? AND day BETWEEN ? AND ?",
            (person_id, key, desde, hasta),
        ).fetchall()
        medias[key] = statistics.fmean(r["value"] for r in rows) if rows else None
    return {"dias": dias, "turnos": turnos, "viaje_dias": viaje_dias, **medias}


# ---- ruling 7: one entry point for slice 2 ------------------------------------------------------

def estado(conn: sqlite3.Connection, person_id: int, practica_id: int, desde: date | None = None,
           hasta: date | None = None) -> dict | None:
    """The definition, per-day states, weeks, streaks, temporadas with context, the whole-history
    baseline, and MODELO['version'] -- default range = the person's whole history for that source
    family (first to last day with data). None when there is no such practice for this person."""
    row = conn.execute(
        "SELECT id, titulo, fuente, regla_json, meta_dias_semana, origen, creada_en, archivada_en "
        "FROM practica WHERE id = ? AND person_id = ?", (practica_id, person_id),
    ).fetchone()
    if row is None:
        return None
    fuente = row["fuente"]
    regla = json.loads(row["regla_json"])
    definicion = {
        "id": row["id"], "titulo": row["titulo"], "fuente": fuente, "regla": regla,
        "meta_dias_semana": row["meta_dias_semana"], "origen": row["origen"],
        "creada_en": row["creada_en"], "archivada_en": row["archivada_en"],
    }

    primero, ultimo = _primero_ultimo_dato(conn, person_id, fuente, regla)
    if primero is None:
        return {
            "definicion": definicion, "desde": None, "hasta": None, "dias": {}, "semanas": [],
            "racha_actual": None, "racha_mas_larga": None, "n_rachas": 0, "temporadas": [],
            "baseline": None, "modelo_version": MODELO["version"],
        }

    rango_desde = desde or primero
    rango_hasta = hasta or ultimo
    estados = dias_estado(conn, person_id, fuente, regla, rango_desde, rango_hasta)
    dias_ordenados = _dias_rango(rango_desde, rango_hasta)

    racha_actual, racha_mas_larga, n_rachas = _rachas_dia(dias_ordenados, estados)
    semanas = _semanas(dias_ordenados, estados, row["meta_dias_semana"])
    # Ruling 7: one trip read for this whole call, covering every temporada AND the baseline (the
    # baseline's own [primero, ultimo] is always the widest span either could ask for) -- never a
    # read per temporada.
    viaje_dias_totales = viajes.dias_de_viaje(conn, person_id, primero.isoformat(), ultimo.isoformat())
    temporadas = []
    for t in _temporadas_crudas(semanas):
        contexto = _contexto(conn, person_id, t["_primer_dia"], t["_ultimo_dia"], viaje_dias_totales)
        temporadas.append({k: v for k, v in t.items() if not k.startswith("_")} | {"contexto": contexto})
    baseline = _contexto(conn, person_id, primero.isoformat(), ultimo.isoformat(), viaje_dias_totales)

    return {
        "definicion": definicion,
        "desde": rango_desde.isoformat(), "hasta": rango_hasta.isoformat(),
        "dias": estados, "semanas": semanas,
        "racha_actual": racha_actual, "racha_mas_larga": racha_mas_larga, "n_rachas": n_rachas,
        "temporadas": temporadas, "baseline": baseline, "modelo_version": MODELO["version"],
    }


# ---- ruling 1 (BODY-HISTORIA-VIDA-2-R2, ORCH-CARLOS-DELEGATES-TIER-1-1): a batched summary -------

def _primero_ultimo_de(dias: set[str]) -> tuple[date | None, date | None]:
    if not dias:
        return None, None
    ordenados = sorted(dias)
    return date.fromisoformat(ordenados[0]), date.fromisoformat(ordenados[-1])


def resumen(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """/history's own per-practice summary (`body/rutas/historial.py`'s `_practicas_resumen_ctx`):
    for every active practice, its current streak and most recent week -- the SAME figures
    `estado()["racha_actual"]` and `estado()["semanas"][-1]` give, but reading each SOURCE FAMILY
    at MOST ONCE for all active practices together (activity rows once, daily_metric rows once per
    distinct metric_key the active practices actually use, sleep rows once), then running the SAME
    day-state calculators and the SAME run-detection helper (`_rachas_dia`/`_semanas`) as `estado()`
    in Python. `estado()` itself -- the drill-down -- is untouched and still reads per practica,
    scoped to that practica's own [desde, hasta]. This function's own statement count stays
    constant whether the person tracks 1 practice or many of the same family (tests/
    test_rendimiento.py's own guard); its racha_actual/ultima_semana are byte-identical to
    estado()'s own racha_actual/semanas[-1] for the same practica (tests/test_practicas.py's own
    guard)."""
    activas = listar(conn, person_id)
    if not activas:
        return []

    actividad_practicas = [p for p in activas if p["fuente"] == "actividad"]
    datos_actividad = None
    if actividad_practicas:
        metric_days = {r["day"] for r in conn.execute(
            "SELECT DISTINCT day FROM daily_metric WHERE person_id = ? AND source = 'garmin'", (person_id,))}
        act_rows = conn.execute(
            "SELECT day, type, duration_s, source, source_id, vinculo_manual FROM activity "
            "WHERE person_id = ?", (person_id,),
        ).fetchall()
        datos_actividad = (metric_days, act_rows)

    filas_por_metric_key: dict[str, list] = {}
    for p in activas:
        if p["fuente"] != "metrica_umbral":
            continue
        mk = p["regla"]["metric_key"]
        if mk not in filas_por_metric_key:
            filas_por_metric_key[mk] = conn.execute(
                "SELECT day, value FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = ?",
                (person_id, mk),
            ).fetchall()

    sueno_practicas = [p for p in activas if p["fuente"] == "sueno_horario"]
    filas_sueno = None
    if sueno_practicas:
        filas_sueno = conn.execute(
            "SELECT day, start_local, end_local FROM sleep_session WHERE person_id = ?", (person_id,),
        ).fetchall()

    out = []
    for p in activas:
        fuente, regla = p["fuente"], p["regla"]
        if fuente == "actividad":
            metric_days, act_rows = datos_actividad
            primero, ultimo = _primero_ultimo_de(metric_days | {r["day"] for r in act_rows})
        elif fuente == "metrica_umbral":
            filas = filas_por_metric_key[regla["metric_key"]]
            primero, ultimo = _primero_ultimo_de({r["day"] for r in filas})
        else:
            primero, ultimo = _primero_ultimo_de({r["day"] for r in filas_sueno})

        if primero is None:
            out.append({"id": p["id"], "titulo": p["titulo"], "meta_dias_semana": p["meta_dias_semana"],
                        "racha_actual": None, "ultima_semana": None})
            continue

        dias_ordenados = _dias_rango(primero, ultimo)
        if fuente == "actividad":
            estados = _estados_actividad(regla, primero, ultimo, *datos_actividad)
        elif fuente == "metrica_umbral":
            estados = _estados_metrica_umbral(regla, primero, ultimo, filas_por_metric_key[regla["metric_key"]])
        else:
            estados = _estados_sueno_horario(regla, primero, ultimo, filas_sueno)

        racha_actual, _, _ = _rachas_dia(dias_ordenados, estados)
        semanas = _semanas(dias_ordenados, estados, p["meta_dias_semana"])
        out.append({
            "id": p["id"], "titulo": p["titulo"], "meta_dias_semana": p["meta_dias_semana"],
            "racha_actual": racha_actual, "ultima_semana": semanas[-1] if semanas else None,
        })
    return out


# ---- ruling 6: suggestions, as data, never auto-created -----------------------------------------

def sugerencias(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Every `activity.type` (after the linked-manual-half drop) in >= MODELO['MIN_SEMANAS_SUGERENCIA']
    distinct weeks, plus walking_minutes/cycling_minutes with a value > 0 in that many distinct
    weeks -- as ready-to-create definitions (fuente, regla, a neutral `clave`, never a title) with
    their week counts, omitting any already covered by an active practica of the same rule. Nothing
    is written."""
    act_rows = conn.execute(
        "SELECT day, type, source, source_id, vinculo_manual FROM activity WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    vinculadas = {r["vinculo_manual"] for r in act_rows if r["vinculo_manual"]}
    semanas_por_tipo: dict[str, set] = defaultdict(set)
    for r in act_rows:
        if r["source"] == "manual" and r["source_id"] in vinculadas:
            continue
        if not r["type"]:
            continue
        semanas_por_tipo[r["type"]].add(lunes_de(date.fromisoformat(r["day"])))

    activas = listar(conn, person_id, incluir_archivadas=False)
    reglas_activas = {(p["fuente"], json.dumps(p["regla"], sort_keys=True)) for p in activas}

    out: list[dict] = []
    for tipo, semanas in sorted(semanas_por_tipo.items()):
        if len(semanas) < MODELO["MIN_SEMANAS_SUGERENCIA"]:
            continue
        regla = {"tipos": [tipo]}
        if ("actividad", json.dumps(regla, sort_keys=True)) in reglas_activas:
            continue
        out.append({"clave": tipo, "fuente": "actividad", "regla": regla, "semanas": len(semanas)})

    for metric_key in ("walking_minutes", "cycling_minutes"):
        rows = conn.execute(
            "SELECT day FROM daily_metric_efectiva WHERE person_id = ? AND metric_key = ? AND value > 0",
            (person_id, metric_key),
        ).fetchall()
        semanas = {lunes_de(date.fromisoformat(r["day"])) for r in rows}
        if len(semanas) < MODELO["MIN_SEMANAS_SUGERENCIA"]:
            continue
        regla = {"metric_key": metric_key, "op": ">=", "valor": 1}
        if ("metrica_umbral", json.dumps(regla, sort_keys=True)) in reglas_activas:
            continue
        out.append({"clave": metric_key, "fuente": "metrica_umbral", "regla": regla, "semanas": len(semanas)})

    return out
