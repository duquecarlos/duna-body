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

REASON_CODES = ("sin_turnos", "sin_noches", "pocos_bloques", "sin_datos_reloj")

_BETTER = {d[0]: d[3] for d in db.METRIC_DEFS}


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


def _dias_excluidos(conn, person_id: int, hoy: date) -> set[str]:
    """Night days and the `ventana_post` days right after each block's last night -- never counted
    toward "tus semanas de día" (a night-shift week always excludes its own recovery tail)."""
    desde = (hoy - timedelta(REFERENCIA_DIAS + MODELO["ventana_post"])).isoformat()
    hasta = (hoy - timedelta(1)).isoformat()
    excluidos: set[str] = set()
    for b in bloques_noche(conn, person_id, desde, hasta):
        fin = _parse(b.fin)
        d = _parse(b.inicio)
        while d <= fin:
            excluidos.add(d.isoformat())
            d += timedelta(1)
        for i in range(1, MODELO["ventana_post"] + 1):
            excluidos.add((fin + timedelta(i)).isoformat())
    return excluidos


def referencia(conn, person_id: int, hoy: date | None = None) -> dict[str, Referencia | None]:
    """Per metric: mean and normal band (25th-75th percentile, body/baselines.py's rule) over the
    person's own day-shift days in the REFERENCIA_DIAS days before `hoy`. Fewer than
    MODELO['min_dias_referencia'] values -> no reference for that metric (honest gap)."""
    hoy = hoy or date.today()
    start = (hoy - timedelta(REFERENCIA_DIAS)).isoformat()
    end = (hoy - timedelta(1)).isoformat()
    excluidos = _dias_excluidos(conn, person_id, hoy)
    out: dict[str, Referencia | None] = {}
    for metric in MODELO["metricas"]:
        serie = db.series(conn, metric, start, end, person_id)
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


def _costo_bloque(conn, person_id: int, bloque: Bloque, ref: dict[str, Referencia | None]) -> dict[str, float | None]:
    fin = _parse(bloque.fin)
    end = (fin + timedelta(MODELO["dias_estables"])).isoformat()
    costo: dict[str, float | None] = {}
    for metric in MODELO["metricas"]:
        r = ref.get(metric)
        if r is None:
            costo[metric] = None
            continue
        serie = db.series(conn, metric, bloque.inicio, end, person_id)
        vals = list(serie.values())
        if not vals:
            costo[metric] = None
            continue
        costo[metric] = (statistics.fmean(vals) - r.media) * _signo(metric)
    return costo


def _dia_en_banda(conn, person_id: int, day: str, ref: dict[str, Referencia | None]) -> bool:
    valores = db.day_values(conn, day, person_id)
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
                          hoy: date) -> int | None:
    """The first day d >= 1 after the block's last night such that d and the next
    `dias_estables - 1` days all have at least MIN_BANDA_VALORES metrics back in their reference band.
    Not reached within `ventana_post` days (or a day's data does not exist yet) -> None."""
    fin = _parse(bloque.fin)
    for d in range(1, MODELO["ventana_post"] + 1):
        ventana = [fin + timedelta(d + i) for i in range(MODELO["dias_estables"])]
        if any(day > hoy for day in ventana):
            return None
        if all(_dia_en_banda(conn, person_id, day.isoformat(), ref) for day in ventana):
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


def resumen(conn, person_id: int, hoy: date | None = None) -> dict:
    """Blocks of the last RESUMEN_DIAS days with their cost and recovery; the hero `dias_recuperacion`
    is the median recovery over blocks that have a figure, only when at least MODELO['min_bloques'] do;
    otherwise None with a reason code (REASON_CODES)."""
    hoy = hoy or date.today()
    ref = referencia(conn, person_id, hoy)
    desde = (hoy - timedelta(RESUMEN_DIAS)).isoformat()
    bloques = bloques_noche(conn, person_id, desde, hoy.isoformat())

    bloques_out = []
    recuperaciones = []
    costos_por_metrica: dict[str, list[float]] = {m: [] for m in MODELO["metricas"]}
    for b in bloques:
        costo = _costo_bloque(conn, person_id, b, ref)
        recup = _recuperacion_bloque(conn, person_id, b, ref, hoy)
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
    }
