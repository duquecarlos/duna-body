"""Turnos' hero: what a week of nights costs, and how long recovery takes (BODY-TURNOS-COSTO-1).

Pure reads over a connection and a person id, no web imports -- the same shape as body/history.py and
body/readiness.py. Every number here is an ESTIMATE, versioned (`MODELO["version"]`), against the
person's OWN day-shift weeks, never a population table (docs/DESIGN.md rules 4, 5, 12). The normal band
(25th-75th percentile) is the same rule body/baselines.py's `baseline()` uses -- reused here, not
redefined, because the reference population is different (the person's own day-shift days over 120
days, excluding nights and the days right after one, not a 30-day trailing window).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db
from .lector import Lector

VERSION = "turnos-v1"
MODELO = {
    "version": VERSION,
    "metricas": ["sleep_minutes", "resting_hr", "hrv_last_night", "stress_avg", "body_battery_wake"],
    "min_bloques": 2,
    "ventana_post": 10,
    "dias_estables": 2,
    "min_dias_referencia": 10,
}

RESUMEN_DIAS = 180       # blocks considered for the hero come from this many trailing days
REFERENCIA_DIAS = 120    # the "tus semanas de día" reference window
MIN_BANDA_VALORES = 3    # a day qualifies toward recovery when at least this many metrics are in band
MIN_BLOQUES_PREVIOS = 3  # a metric needs at least this many earlier night blocks to get a verdict
                         # against them (BODY-TURNOS-REFERENCIA-NOCHE-1) -- like for like, never the
                         # day-week reference, which is why this is a separate floor from min_bloques.

REASON_CODES = ("sin_turnos", "sin_noches", "pocos_bloques", "sin_datos_reloj", "pocos_bloques_previos")

# BODY-TURNOS-PERFIL-DIA-1: the fixed day-offsets `perfil_por_dia` reports, relative to a block's own
# first/last night -- in this order, since it is also the x-axis order the hero's chart draws.
OFFSETS = ("antes", "noche_1", "noche_ultima", "post_1", "post_2", "post_3", "post_4")

# Below this many units of real difference, a row reads "igual" instead of "mas"/"menos"
# (BODY-TURNOS-COSTO-1-M, fix b): roughly a quarter-hour of sleep, a beat of resting HR, a
# millisecond of HRV, three points of a 0-100 scale -- small enough that a reader's own day-to-day
# noise would otherwise read as a verdict.
UMBRAL_IGUAL = {"min": 15.0, "bpm": 1.0, "ms": 1.0, "pts": 3.0}

_BETTER = {d[0]: d[3] for d in db.METRIC_DEFS}
_UNIT = {d[0]: d[2] for d in db.METRIC_DEFS}


@dataclass
class Bloque:
    inicio: str  # ISO day, first night of the block
    fin: str     # ISO day, last night of the block
    dias: int


@dataclass
class Referencia:
    media: float
    bajo: float
    alto: float
    n: int


def _parse(d: str) -> date:
    return date.fromisoformat(d)


def bloques_noche(conn, person_id: int, desde: str, hasta: str) -> list[Bloque]:
    """Maximal runs of consecutive days whose confirmed shift.kind is 'night', within [desde, hasta].
    A single day that is not night (an 'off' day, or any other kind) splits a run: it never joins two
    night runs into one."""
    rows = conn.execute(
        "SELECT DISTINCT day FROM shift WHERE person_id = ? AND kind = 'night' AND day BETWEEN ? AND ? "
        "ORDER BY day",
        (person_id, desde, hasta),
    ).fetchall()
    days = [_parse(r["day"]) for r in rows]
    bloques: list[list[date]] = []
    cur: list[date] = []
    for d in days:
        if cur and d == cur[-1] + timedelta(1):
            cur.append(d)
        else:
            if cur:
                bloques.append(cur)
            cur = [d]
    if cur:
        bloques.append(cur)
    return [Bloque(inicio=b[0].isoformat(), fin=b[-1].isoformat(), dias=len(b)) for b in bloques]


def _post_noche_de_bloques(bloques: list[Bloque]) -> set[str]:
    dias: set[str] = set()
    for b in bloques:
        fin = _parse(b.fin)
        for i in range(1, MODELO["ventana_post"] + 1):
            dias.add((fin + timedelta(i)).isoformat())
    return dias


def dias_post_noche(conn, person_id: int, desde: str, hasta: str) -> set[str]:
    """The `ventana_post` days right after the last night of each night block whose own nights
    fall in [desde, hasta] -- the post-night recovery window alone, with no night day in it.
    Public so body/experimentos.py's `excluir_tras_noche` filter reuses this one definition
    instead of redefining it (BODY-EXPERIMENTOS-MODELO-1)."""
    return _post_noche_de_bloques(bloques_noche(conn, person_id, desde, hasta))


def _dias_excluidos(conn, person_id: int, hoy: date) -> set[str]:
    """Night days and the `ventana_post` days right after each block's last night -- never counted
    toward "tus semanas de día" (a night-shift week always excludes its own recovery tail)."""
    desde = (hoy - timedelta(REFERENCIA_DIAS + MODELO["ventana_post"])).isoformat()
    hasta = (hoy - timedelta(1)).isoformat()
    bloques = bloques_noche(conn, person_id, desde, hasta)
    excluidos: set[str] = set()
    for b in bloques:
        fin = _parse(b.fin)
        d = _parse(b.inicio)
        while d <= fin:
            excluidos.add(d.isoformat())
            d += timedelta(1)
    excluidos |= _post_noche_de_bloques(bloques)
    return excluidos


def referencia(conn, person_id: int, hoy: date | None = None, *, lector: Lector | None = None) -> dict[str, Referencia | None]:
    """Per metric: mean and normal band (25th-75th percentile, body/baselines.py's rule) over the
    person's own day-shift days in the REFERENCIA_DIAS days before `hoy`. Fewer than
    MODELO['min_dias_referencia'] values -> no reference for that metric (honest gap). `lector`,
    when given, must cover at least [hoy - REFERENCIA_DIAS, hoy - 1] (BODY-RENDIMIENTO-2)."""
    hoy = hoy or date.today()
    start = (hoy - timedelta(REFERENCIA_DIAS)).isoformat()
    end = (hoy - timedelta(1)).isoformat()
    excluidos = _dias_excluidos(conn, person_id, hoy)
    out: dict[str, Referencia | None] = {}
    for metric in MODELO["metricas"]:
        serie = lector.series(metric, start, end) if lector is not None else db.series(conn, metric, start, end, person_id)
        vals = [v for day, v in serie.items() if day not in excluidos]
        if len(vals) < MODELO["min_dias_referencia"]:
            out[metric] = None
            continue
        q = statistics.quantiles(vals, n=4)
        out[metric] = Referencia(media=statistics.fmean(vals), bajo=q[0], alto=q[2], n=len(vals))
    return out


def _signo(metric: str) -> int:
    """So the signed cost is always positive when a night week is WORSE, whichever direction the
    metric's own normal is better in (db.METRIC_DEFS)."""
    return 1 if _BETTER.get(metric) == "lower" else -1


def diferencia_y_palabra(metric: str, costo: float) -> tuple[float, str]:
    """From a metric's signed cost (positive = worse, `_costo_bloque`'s convention, kept for the
    recovery rule and the assistant tool), the REAL difference a reader should see -- night-block
    mean minus reference mean, its own sign -- and a plain DIRECTION word, not a verdict: 'mas' |
    'menos' | 'igual', from the difference's own sign (BODY-TURNOS-PALABRA-DIRECCION-1). Three of
    the five metrics are keyed to one sleep session by Garmin's own fields, and on a night-shift day
    that session is the person's daytime rest, not a night comparable to the reference -- so
    'better'/'worse' can be an artefact of WHEN the watch measures, not a real verdict. Applying
    `_signo` a second time undoes the first signing, since it is always +-1."""
    diff = costo * _signo(metric)
    if abs(diff) < UMBRAL_IGUAL.get(_UNIT.get(metric, ""), 0.0):
        return diff, "igual"
    return diff, "mas" if diff > 0 else "menos"


def _costo_bloque(conn, person_id: int, bloque: Bloque, ref: dict[str, Referencia | None],
                   *, lector: Lector | None = None) -> dict[str, float | None]:
    fin = _parse(bloque.fin)
    end = (fin + timedelta(MODELO["dias_estables"])).isoformat()
    costo: dict[str, float | None] = {}
    for metric in MODELO["metricas"]:
        r = ref.get(metric)
        if r is None:
            costo[metric] = None
            continue
        serie = lector.series(metric, bloque.inicio, end) if lector is not None \
            else db.series(conn, metric, bloque.inicio, end, person_id)
        vals = list(serie.values())
        if not vals:
            costo[metric] = None
            continue
        costo[metric] = (statistics.fmean(vals) - r.media) * _signo(metric)
    return costo


def _medias_bloque(conn, person_id: int, bloque: Bloque, *, lector: Lector | None = None) -> dict[str, float | None]:
    """Raw window mean per metric over the same window `_costo_bloque` uses (inicio..fin+dias_estables),
    with no reference subtracted -- what BODY-TURNOS-REFERENCIA-NOCHE-1's last-week section compares
    one night block against another, never against the day-week reference."""
    fin = _parse(bloque.fin)
    end = (fin + timedelta(MODELO["dias_estables"])).isoformat()
    medias: dict[str, float | None] = {}
    for metric in MODELO["metricas"]:
        serie = lector.series(metric, bloque.inicio, end) if lector is not None \
            else db.series(conn, metric, bloque.inicio, end, person_id)
        vals = list(serie.values())
        medias[metric] = statistics.fmean(vals) if vals else None
    return medias


def _ultima_semana(bloques: list[Bloque], bloques_out: list[dict], medias: list[dict[str, float | None]],
                    hoy: date) -> dict | None:
    """BODY-TURNOS-REFERENCIA-NOCHE-1: the last COMPLETE night block (its cost window fully on or
    before `hoy`) against the person's own EARLIER night blocks in the same window -- like for like,
    never the day-week reference three of the five metrics cannot honestly use. Per metric: the last
    block's window mean vs the mean of the earlier blocks' own window means (one value per block, so a
    long block never outweighs short ones), only when at least MIN_BLOQUES_PREVIOS earlier blocks have
    that metric. No block with a complete window at all -> None."""
    last_idx = None
    for i, b in enumerate(bloques):
        if _parse(b.fin) + timedelta(MODELO["dias_estables"]) <= hoy:
            last_idx = i
    if last_idx is None:
        return None

    metricas_out = []
    max_n = 0
    for metric in MODELO["metricas"]:
        last_mean = medias[last_idx].get(metric)
        valores = [medias[i][metric] for i in range(last_idx) if medias[i].get(metric) is not None]
        n = len(valores)
        if last_mean is None or n < MIN_BLOQUES_PREVIOS:
            continue
        costo_equiv = (last_mean - statistics.fmean(valores)) * _signo(metric)
        diff, palabra = diferencia_y_palabra(metric, costo_equiv)
        veredicto = "igual" if palabra == "igual" else ("peor" if costo_equiv > 0 else "mejor")
        metricas_out.append({"metric": metric, "diff": round(abs(diff), 1), "direccion": palabra,
                              "veredicto": veredicto})
        max_n = max(max_n, n)

    recup_anteriores = [bloques_out[i]["recuperacion_dias"] for i in range(last_idx)
                         if bloques_out[i]["recuperacion_dias"] is not None]

    return {
        "inicio": bloques[last_idx].inicio,
        "fin": bloques[last_idx].fin,
        "n": max_n,
        "metricas": metricas_out,
        "motivo": None if metricas_out else "pocos_bloques_previos",
        "recuperacion_dias": bloques_out[last_idx]["recuperacion_dias"],
        "recuperacion_mediana": round(statistics.median(recup_anteriores)) if recup_anteriores else None,
    }


def _offset_day(bloque: Bloque, offset: str) -> date | None:
    """The calendar day `offset` names for this block, or None when the offset does not apply to
    it -- `noche_ultima` on a one-night block would be the same day as `noche_1`, so it is skipped
    rather than double-counting that night."""
    inicio, fin = _parse(bloque.inicio), _parse(bloque.fin)
    if offset == "antes":
        return inicio - timedelta(1)
    if offset == "noche_1":
        return inicio
    if offset == "noche_ultima":
        return fin if fin != inicio else None
    return fin + timedelta(int(offset.split("_")[1]))


def perfil_por_dia(conn, person_id: int, metric: str, hoy: date | None = None, *,
                    lector: Lector | None = None, bloques: list[Bloque] | None = None,
                    ref: dict[str, Referencia | None] | None = None) -> dict:
    """BODY-TURNOS-PERFIL-DIA-1: for `metric`, how far each day OFFSET around a night block sits from
    the person's own day-week reference -- the same `referencia()` the hero's cost rows use, so the
    centre is that reference's mean and the band is its 25th-75th percentile, just re-expressed as an
    offset from that centre (centre reads as 0) instead of an absolute figure. Averaged over every
    night block in the same RESUMEN_DIAS window `resumen()` uses -- a day with no value is skipped,
    never zeroed, and an offset no block in the window actually reaches (`noche_ultima` on an
    all-one-night history) is left out of `offsets` rather than reported as zero.

    `lector`, `bloques` and `ref`, when given, must be `resumen()`'s own -- its single Lector already
    covers [hoy - RESUMEN_DIAS - 1, hoy + ventana_post] (wide enough for every offset here, including
    `antes` and `post_4`), and its own `bloques_noche()`/`referencia()` calls are this function's
    exactly, so `resumen()` passes them through instead of this function repeating those statements
    (BODY-RENDIMIENTO-2's own rule). Standalone callers (tests, or any future second consumer) omit
    them and this function reads them itself, exactly as before.

    Always returns a dict, never a bare None (resumen()'s and `_ultima_semana`'s own convention):
    `motivo` is one of REASON_CODES, and every other key is absent, when there are fewer than
    MIN_BLOQUES_PREVIOS night blocks in the window or `metric` has no reference at all; otherwise
    `motivo` is None and the dict carries `offsets` (one {"offset", "media", "n"} per OFFSET that has
    at least one contributing day), `centro`, `banda` ({"bajo", "alto"}, centred the same way),
    `mas_bajo` or `mas_alto` (whichever direction db.METRIC_DEFS calls worse for this metric) naming
    the offset furthest from usual, `vuelta` (the first post_* offset back inside the band, or None),
    `noches_tipicas` (the median block length) and `n_bloques`."""
    hoy = hoy or date.today()
    desde = (hoy - timedelta(RESUMEN_DIAS)).isoformat()
    if lector is None:
        desde_lector = (hoy - timedelta(RESUMEN_DIAS + 1)).isoformat()
        hasta_lector = (hoy + timedelta(MODELO["ventana_post"])).isoformat()
        lector = Lector(conn, person_id, desde_lector, hasta_lector, MODELO["metricas"])
    if ref is None:
        ref = referencia(conn, person_id, hoy, lector=lector)
    r = ref.get(metric)
    if bloques is None:
        bloques = bloques_noche(conn, person_id, desde, hoy.isoformat())
    if r is None or len(bloques) < MIN_BLOQUES_PREVIOS:
        return {"metric": metric, "motivo": "pocos_bloques_previos"}

    por_offset: dict[str, list[float]] = {o: [] for o in OFFSETS}
    for b in bloques:
        for offset in OFFSETS:
            dia = _offset_day(b, offset)
            if dia is None or dia > hoy:
                continue
            v = lector.day_values(dia.isoformat()).get(metric)
            if v is not None:
                por_offset[offset].append(v - r.media)

    offsets_out = [{"offset": o, "media": round(statistics.fmean(vs), 1), "n": len(vs)}
                   for o in OFFSETS if (vs := por_offset[o])]
    if not offsets_out:
        return {"metric": metric, "motivo": "pocos_bloques_previos"}

    peor_es_bajo = _BETTER.get(metric) == "higher"
    extremo = (min if peor_es_bajo else max)(offsets_out, key=lambda o: o["media"])
    banda_bajo, banda_alto = round(r.bajo - r.media, 1), round(r.alto - r.media, 1)
    vuelta = next((o["offset"] for o in offsets_out
                   if o["offset"].startswith("post_") and banda_bajo <= o["media"] <= banda_alto), None)

    return {
        "metric": metric,
        "motivo": None,
        "centro": round(r.media, 1),
        "banda": {"bajo": banda_bajo, "alto": banda_alto},
        "offsets": offsets_out,
        ("mas_bajo" if peor_es_bajo else "mas_alto"): extremo["offset"],
        "vuelta": vuelta,
        "noches_tipicas": round(statistics.median(b.dias for b in bloques)),
        "n_bloques": len(bloques),
    }


def frase_patron(perfil: dict, metric: str) -> dict | None:
    """BODY-TENDENCIAS-LEYENDA-PATRON-1 ruling 1: turns a successful `perfil_por_dia` result into a
    sentence SPEC -- a kind plus its own parameters, never prose -- so Trends (body/rutas/
    tendencias.py) and the assistant's leer_tendencias (body/agente/herramientas.py) can share it
    and render or report it their own way. `perfil["motivo"]` set (no profile) -> None. Otherwise,
    in precedence order: the worst offset's mean inside the band -> {"kind": "estable"} (nights do
    not move this metric beyond usual); outside the band and `vuelta` set -> {"kind": "vuelve",
    "peor", "vuelta", "direccion"}; outside the band and no `vuelta` -> {"kind": "no_vuelve",
    "peor", "direccion"}. `direccion` ("baja"/"sube") is the side the worst offset's mean sits on
    relative to centre (0 once `perfil_por_dia` re-expresses it that way), never the metric's own
    better/worse word, so a lower-is-better metric whose nights run high still reads "sube". No
    figure from the person's data is in the spec -- `peor`/`vuelta` are OFFSET NAMES, `direccion`
    is a word -- so a caption built from this can never leak a number into a log."""
    if perfil.get("motivo") is not None:
        return None
    peor_es_bajo = _BETTER.get(metric) == "higher"
    peor_offset = perfil["mas_bajo" if peor_es_bajo else "mas_alto"]
    extremo = next(o for o in perfil["offsets"] if o["offset"] == peor_offset)
    banda = perfil["banda"]
    if banda["bajo"] <= extremo["media"] <= banda["alto"]:
        return {"kind": "estable"}
    direccion = "baja" if extremo["media"] < 0 else "sube"
    vuelta = perfil.get("vuelta")
    if vuelta is not None:
        return {"kind": "vuelve", "peor": peor_offset, "vuelta": vuelta, "direccion": direccion}
    return {"kind": "no_vuelve", "peor": peor_offset, "direccion": direccion}


class _LectorVarias:
    """Same `.series()`/`.day_values()` contract as body/lector.py's `Lector`, but loads every
    metric named with ONE SQL statement (`metric_key IN (...)`) instead of Lector's one query per
    metric. `perfiles_patron` below needs all of MODELO['metricas'] (five metrics) at once, every
    time Trends renders -- five separate Lector queries would cost /trends' own statement ceiling
    (tests/test_rendimiento.py) five statements just for this, where one combined read costs it
    one (BODY-TENDENCIAS-LEYENDA-PATRON-1). Used only here, so it stays private rather than moving
    into body/lector.py."""

    def __init__(self, conn, person_id: int, desde: str, hasta: str, metrics) -> None:
        self._series: dict[str, dict[str, float]] = {m: {} for m in metrics}
        placeholders = ",".join("?" * len(metrics))
        rows = conn.execute(
            f"SELECT metric_key, day, value FROM daily_metric WHERE person_id = ? "
            f"AND metric_key IN ({placeholders}) AND day BETWEEN ? AND ?",
            (person_id, *metrics, desde, hasta),
        ).fetchall()
        for r in rows:
            self._series[r["metric_key"]][r["day"]] = r["value"]

    def series(self, metric: str, start: str, end: str) -> dict[str, float]:
        return {day: value for day, value in self._series.get(metric, {}).items() if start <= day <= end}

    def day_values(self, day: str) -> dict[str, float]:
        return {metric: serie[day] for metric, serie in self._series.items() if day in serie}


def perfiles_patron(conn, person_id: int, hoy: date | None = None) -> dict[str, dict]:
    """BODY-TENDENCIAS-LEYENDA-PATRON-1: `perfil_por_dia` for every metric in MODELO['metricas'],
    sharing one `_LectorVarias`, one `bloques_noche` call and one `referencia()` call across all
    five -- the one caller (Trends, and the assistant's leer_tendencias) that needs a pattern per
    metric, never a statement per metric."""
    hoy = hoy or date.today()
    desde = (hoy - timedelta(RESUMEN_DIAS)).isoformat()
    desde_lector = (hoy - timedelta(RESUMEN_DIAS + 1)).isoformat()
    hasta_lector = (hoy + timedelta(MODELO["ventana_post"])).isoformat()
    lector = _LectorVarias(conn, person_id, desde_lector, hasta_lector, MODELO["metricas"])
    ref = referencia(conn, person_id, hoy, lector=lector)
    bloques = bloques_noche(conn, person_id, desde, hoy.isoformat())
    return {m: perfil_por_dia(conn, person_id, m, hoy, lector=lector, bloques=bloques, ref=ref)
            for m in MODELO["metricas"]}


def _dia_en_banda(conn, person_id: int, day: str, ref: dict[str, Referencia | None],
                   *, lector: Lector | None = None) -> bool:
    valores = lector.day_values(day) if lector is not None else db.day_values(conn, day, person_id)
    en_banda = 0
    for metric in MODELO["metricas"]:
        r = ref.get(metric)
        v = valores.get(metric)
        if r is None or v is None:
            continue
        if r.bajo <= v <= r.alto:
            en_banda += 1
    return en_banda >= MIN_BANDA_VALORES


def _recuperacion_bloque(conn, person_id: int, bloque: Bloque, ref: dict[str, Referencia | None],
                          hoy: date, *, lector: Lector | None = None) -> int | None:
    """The first day d >= 1 after the block's last night such that d and the next
    `dias_estables - 1` days all have at least MIN_BANDA_VALORES metrics back in their reference band.
    Not reached within `ventana_post` days (or a day's data does not exist yet) -> None."""
    fin = _parse(bloque.fin)
    for d in range(1, MODELO["ventana_post"] + 1):
        ventana = [fin + timedelta(d + i) for i in range(MODELO["dias_estables"])]
        if any(day > hoy for day in ventana):
            return None
        if all(_dia_en_banda(conn, person_id, day.isoformat(), ref, lector=lector) for day in ventana):
            return d
    return None


def _motivo(conn, person_id: int, bloques: list[Bloque]) -> str:
    if not db.days_with_data(conn, person_id):
        return "sin_datos_reloj"
    any_shift = conn.execute("SELECT 1 FROM shift WHERE person_id = ? LIMIT 1", (person_id,)).fetchone()
    if any_shift is None:
        return "sin_turnos"
    if not bloques:
        return "sin_noches"
    return "pocos_bloques"


def _shift_on(conn, person_id: int, day_iso: str) -> dict | None:
    row = conn.execute(
        "SELECT kind, code, start_local, end_local FROM shift WHERE person_id = ? AND day = ? "
        "ORDER BY start_local LIMIT 1",
        (person_id, day_iso),
    ).fetchone()
    return None if row is None else {"kind": row["kind"], "code": row["code"],
                                      "start_local": row["start_local"], "end_local": row["end_local"]}


def turno_de_hoy(conn, person_id: int, hoy: date) -> dict:
    """Hoy's second promise (BODY-HOY-TURNO-1): today's confirmed shift, the next confirmed shift
    ahead (skipping 'off' days, within 14 days), and -- when the person is still inside a recovery
    window after their last night block -- how far into it they are. Pure read, all from confirmed
    `shift` rows and this module's own `resumen()`; nothing stored."""
    hoy_shift = _shift_on(conn, person_id, hoy.isoformat())

    siguiente = None
    for i in range(1, 15):
        candidato = _shift_on(conn, person_id, (hoy + timedelta(i)).isoformat())
        if candidato is not None and candidato["kind"] != "off":
            siguiente = candidato
            break

    recuperacion = None
    if hoy_shift is None or hoy_shift["kind"] != "night":
        desde = (hoy - timedelta(RESUMEN_DIAS)).isoformat()
        hasta = (hoy - timedelta(1)).isoformat()
        bloques = bloques_noche(conn, person_id, desde, hasta)
        if bloques:
            fin = _parse(bloques[-1].fin)
            n = (hoy - fin).days
            if n <= MODELO["ventana_post"]:
                m = resumen(conn, person_id, hoy)["dias_recuperacion"]
                if m is not None and n <= m:
                    recuperacion = {"dia": n, "estimado": m}

    return {"hoy": hoy_shift, "siguiente": siguiente, "recuperacion": recuperacion}


def contexto_dia(conn, person_id: int, dia: date) -> dict | None:
    """Where `dia` sits relative to a night block (BODY-TURNOS-ENLACES-1): inside one ->
    {"tipo": "noche", "n": k, "de": length} (k, length from `bloques_noche`'s own maximal run, so a
    block already-confirmed ahead of `dia` still counts toward `length`); within `ventana_post` days
    after a block's last night, and `dia` itself not a night -> {"tipo": "despues", "n": d}; else
    None. Pure read of confirmed `shift` rows, nothing stored."""
    ventana = MODELO["ventana_post"]
    desde = (dia - timedelta(60)).isoformat()
    hasta = (dia + timedelta(60)).isoformat()
    bloques = bloques_noche(conn, person_id, desde, hasta)
    anterior = None
    for b in bloques:
        inicio, fin = _parse(b.inicio), _parse(b.fin)
        if inicio <= dia <= fin:
            return {"tipo": "noche", "n": (dia - inicio).days + 1, "de": b.dias}
        if fin < dia:
            anterior = b
    if anterior is not None:
        d = (dia - _parse(anterior.fin)).days
        if d <= ventana:
            return {"tipo": "despues", "n": d}
    return None


def siguiente_bloque_noche(conn, person_id: int, hoy: date, dias: int = 7) -> dict | None:
    """BODY-HOY-SEMANA-NOCTURNA-1 (ruling 6): the next night block whose first night has not yet
    started and falls within `dias` days of `hoy` -- its own dates only, no autonomous-plan wording
    (DESIGN rule 14; BODY-MOCK-R2-CENSO-1-INV conflict #3). A block already under way (its own
    `inicio` on or before `hoy`, including one `hoy` itself sits inside) never qualifies -- this is
    for what is still AHEAD. `desde = hoy - 1` is enough to tell a block that started yesterday or
    earlier (so it is excluded) from one that genuinely starts tomorrow, without reading further
    back than that one extra day."""
    desde = (hoy - timedelta(1)).isoformat()
    hasta = (hoy + timedelta(dias)).isoformat()
    for b in bloques_noche(conn, person_id, desde, hasta):
        inicio = _parse(b.inicio)
        if hoy < inicio <= hoy + timedelta(dias):
            return {"inicio": b.inicio, "fin": b.fin}
    return None


def resumen(conn, person_id: int, hoy: date | None = None) -> dict:
    """Blocks of the last RESUMEN_DIAS days with their cost and recovery; the hero `dias_recuperacion`
    is the median recovery over blocks that have a figure, only when at least MODELO['min_bloques'] do;
    otherwise None with a reason code (REASON_CODES). `ultima_semana` (BODY-TURNOS-REFERENCIA-NOCHE-1)
    is the last complete block against the person's own earlier night blocks -- see `_ultima_semana`.
    Reads its whole metric window ONCE, through a
    single `Lector` covering [hoy - RESUMEN_DIAS - 1, hoy + ventana_post] -- wide enough for
    `referencia`'s own [hoy - REFERENCIA_DIAS, hoy - 1], for every block's cost and recovery window
    (REFERENCIA_DIAS <= RESUMEN_DIAS, no block's window reaches past hoy + dias_estables), and for
    `perfil_por_dia`'s own `antes`..`post_4` offsets (ventana_post >= dias_estables), so `perfil_hrv`
    below costs this call no extra statement (BODY-TURNOS-PERFIL-DIA-1, BODY-RENDIMIENTO-2)."""
    hoy = hoy or date.today()
    desde = (hoy - timedelta(RESUMEN_DIAS)).isoformat()
    desde_lector = (hoy - timedelta(RESUMEN_DIAS + 1)).isoformat()
    hasta_lector = (hoy + timedelta(max(MODELO["dias_estables"], MODELO["ventana_post"]))).isoformat()
    lector = Lector(conn, person_id, desde_lector, hasta_lector, MODELO["metricas"])
    ref = referencia(conn, person_id, hoy, lector=lector)
    bloques = bloques_noche(conn, person_id, desde, hoy.isoformat())

    bloques_out = []
    recuperaciones = []
    medias_out: list[dict[str, float | None]] = []
    costos_por_metrica: dict[str, list[float]] = {m: [] for m in MODELO["metricas"]}
    for b in bloques:
        costo = _costo_bloque(conn, person_id, b, ref, lector=lector)
        recup = _recuperacion_bloque(conn, person_id, b, ref, hoy, lector=lector)
        medias_out.append(_medias_bloque(conn, person_id, b, lector=lector))
        if recup is not None:
            recuperaciones.append(recup)
        for metric, v in costo.items():
            if v is not None:
                costos_por_metrica[metric].append(v)
        bloques_out.append({"inicio": b.inicio, "fin": b.fin, "dias": b.dias,
                            "recuperacion_dias": recup, "costo": costo})

    costo_promedio = {m: (round(statistics.fmean(vs), 1) if vs else None) for m, vs in costos_por_metrica.items()}

    dias_recuperacion = None
    motivo = None
    if len(recuperaciones) >= MODELO["min_bloques"]:
        dias_recuperacion = round(statistics.median(recuperaciones))
    else:
        motivo = _motivo(conn, person_id, bloques)

    return {
        "version": VERSION,
        "dias_recuperacion": dias_recuperacion,
        "motivo": motivo,
        "bloques": bloques_out,
        "bloques_con_recuperacion": len(recuperaciones),
        "referencia": {m: (None if r is None else {"media": round(r.media, 1), "bajo": round(r.bajo, 1),
                                                     "alto": round(r.alto, 1), "n": r.n})
                       for m, r in ref.items()},
        "costo_promedio": costo_promedio,
        "ultima_semana": _ultima_semana(bloques, bloques_out, medias_out, hoy),
        # BODY-TURNOS-PERFIL-DIA-1: additive -- VERSION stays "turnos-v1" since this changes no
        # figure resumen() already reported, only adds a new one. lector/bloques/ref are this
        # call's own, reused rather than recomputed (no extra statement, BODY-RENDIMIENTO-2).
        "perfil_hrv": perfil_por_dia(conn, person_id, "hrv_last_night", hoy,
                                      lector=lector, bloques=bloques, ref=ref),
    }
