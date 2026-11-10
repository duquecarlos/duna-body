"""Carlos's study 2 (vida/estudios.md, relayed by the Personal desk, 2026-11-09): "on morning
shifts his HR stays low most of the shift" -- compared by shift kind and by the previous night's
sleep.

Pure reads over a connection and a person id, no web imports -- the same shape as
body/recuperacion.py, body/turnos_costo.py and body/baselines.py. Every number here is an
ESTIMATE, versioned (`MODELO["version"]`), against the person's OWN history, never a population
table (docs/DESIGN.md rules 4, 5, 12). The day cache, activity-hours exclusion and hourly-usual
helpers come from body/intradia_horario.py (BODY-TURNO-FC-1 moved them out of recuperacion.py so
both modules share one definition). The intraday spacing/offset helpers (`_espaciado_mediana_min`,
`_sin_offset`) are IMPORTED from body/experimentos.py, not duplicated, the same rule recuperacion.py
already follows.

ORCH-CARLOS-DELEGATES-TIER-1-1 rulings this module is built to:
  (1) the unit is ONE SHIFT: its own window start_local..end_local (a night shift crosses
      midnight), never a calendar day.
  (2) the comparison is the SAME CLOCK HOURS on the person's own days off (shift kind 'off') over
      MODELO's look-back -- "your heart rate during a morning shift vs the same hours on a day
      off" -- never a population number. This is what `intradia_horario.usual_hora`'s own
      `solo_descanso=True` computes.
  (3) per shift: median heart rate and median stress inside the window, coverage, and the share of
      the window's samples at or below his usual for those hours (the "stays low" observation) --
      a window under MODELO['cobertura_min'] gets no figure, with a reason (REASON_CODES).
  (4) groups with counts: by shift kind, and inside each kind by the previous night's sleep (short
      vs usual, the same split body/recuperacion.py's own estudio() uses) -- a group under
      MODELO['min_n_grupo'] says "pocos_datos".
  (5) marked-period days (the shift's own day) and hours holding an activity are left out.
  (6) the shift schedule is the published one -- the screen says so once (body/rutas/turnos.py).
  (7) nothing enters vida_export.
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db, intradia_horario
from .experimentos import _espaciado_mediana_min, _sin_offset

VERSION = "turno-fc-v1"
MODELO = {
    "version": VERSION,
    # Over the person's own days off (shift kind 'off') only -- ruling 2's own comparison
    # baseline. Off days are roughly 2 of every 7 in a typical rotation (the demo's own 4-week
    # cycle, and the real published schedule alike), so a 30-day window (recuperacion.MODELO's own
    # figure, drawn from EVERY day) would leave an hour's own "usual" with too few off days to
    # qualify; 90 days gives about three times the raw days to draw an off-day sample from.
    "dias_historia": 90,
    # Below this fraction of the samples a window's own observed spacing implies, the window is a
    # coverage gap, not a thin-but-honest reading -- same convention as experimentos.COBERTURA_MIN
    # and recuperacion.MODELO's own floor, reused here for BOTH the shift window's own coverage
    # check and each (day, hour) pair's own coverage check inside intradia_horario.usual_hora.
    "cobertura_min": 0.5,
    # Fewer qualifying OFF days than this for a given clock hour and there is no honest "usual" yet
    # for that hour -- lower than recuperacion.MODELO's own 10, since the 'off'-only filter here
    # already leaves far fewer candidate days per hour than recuperacion's unrestricted one.
    "min_dias_horario": 8,
    # Fewer shifts than this in a kind, or a kind+sleep group: "pocos_datos", never a median that
    # is really 1-2 shifts (ruling 4) -- the same floor recuperacion.MODELO['min_n_grupo'] uses.
    "min_n_grupo": 5,
    # Below this many bpm, a kind's own difference from its days-off reference reads as noise, not
    # a finding -- turnos_costo.UMBRAL_IGUAL's own bpm floor is 1.0 (one metric's own day-to-day
    # noise); doubled here since this compares a MEDIAN of per-shift medians against a MEDIAN of
    # per-hour usuals, one extra layer of averaging on either side.
    "ruido_min_diferencia_bpm": 2.0,
    "max_hallazgos": 3,
}

REASON_CODES = db.TURNO_FC_MOTIVOS


@dataclass
class Resultado:
    fc_mediana: float | None
    estres_mediana: float | None
    bajo_usual_pct: float | None
    referencia_fc: float | None
    cobertura: float | None
    motivo: str | None  # one of REASON_CODES, or None when the figures above are set


def _muestras_ventana(conn, person_id: int, zona: str, kind: str, dias_ventana: list[str],
                       inicio, fin, horas_actividad: dict[str, set[int]], cache: dict
                       ) -> list[tuple[str, float, object]]:
    """Every (`kind`) sample inside [`inicio`, `fin`] across `dias_ventana`'s calendar days, with
    any sample whose own (day, hour) holds one of the person's activities left out (ruling 5) --
    sorted by time. Returns (ts_local, value, parsed_datetime) triples, since callers need both the
    raw string (for `_espaciado_mediana_min`) and the parsed hour."""
    out = []
    for dia in dias_ventana:
        entry = intradia_horario.leer_dia(conn, person_id, dia, zona, cache, kind=kind)
        for ts, v in entry["muestras"]:
            t = _sin_offset(ts)
            if not (inicio <= t <= fin):
                continue
            if t.hour in horas_actividad.get(t.date().isoformat(), ()):
                continue
            out.append((ts, v, t))
    out.sort(key=lambda x: x[2])
    return out


def turno_fc(conn, person_id: int, shift_row, cache: dict | None = None) -> Resultado:
    """One shift's own figures (ruling 3): the median heart rate and stress inside its own window,
    the share of heart-rate samples at or below the person's usual for the hours touched (the
    "stays low" observation), the days-off reference itself (the mean of those same hours' own
    usual, persisted here so estudio()'s kind grouping never recomputes it live on a page render --
    BODY-RENDIMIENTO-1's own statement ceiling on /shifts), and the window's own coverage -- or an
    honest reason when any of that cannot be said (REASON_CODES). `cache` is a per-run dict (see
    intradia_horario.leer_dia) shared across many calls by `recalcular`; a bare call (tests, a
    single lookup) gets its own."""
    if cache is None:
        cache = {}
    day = shift_row["day"]
    if db.dias_marcados(conn, person_id, day, day):
        return Resultado(None, None, None, None, None, "marcado")

    start_local, end_local = shift_row["start_local"], shift_row["end_local"]
    if start_local is None or end_local is None:
        return Resultado(None, None, None, None, None, "sin_cobertura")

    zona = db.person(conn, person_id)["timezone"]
    inicio, fin = _sin_offset(start_local), _sin_offset(end_local)
    dias_ventana = sorted({inicio.date().isoformat(), fin.date().isoformat()})
    horas_actividad = intradia_horario.horas_actividad_por_dia(conn, person_id, cache)

    fc = _muestras_ventana(conn, person_id, zona, "intradia_fc", dias_ventana, inicio, fin,
                            horas_actividad, cache)
    if not fc:
        return Resultado(None, None, None, None, None, "sin_cobertura")

    espaciado = _espaciado_mediana_min([(ts, v) for ts, v, _ in fc])
    if espaciado is None:
        return Resultado(None, None, None, None, None, "sin_cobertura")
    esperadas = (fin - inicio).total_seconds() / 60 / espaciado
    if len(fc) < MODELO["cobertura_min"] * esperadas:
        return Resultado(None, None, None, None, None, "sin_cobertura")

    horas_tocadas = sorted({t.hour for _, _, t in fc})
    cutoffs: dict[int, float] = {}
    for hora in horas_tocadas:
        usual, _n_dias = intradia_horario.usual_hora(
            conn, person_id, zona, day, hora, cache,
            dias_historia=MODELO["dias_historia"], min_dias_horario=MODELO["min_dias_horario"],
            cobertura_min=MODELO["cobertura_min"], solo_descanso=True)
        if usual is None:
            return Resultado(None, None, None, None, None, "sin_rango")
        cutoffs[hora] = usual

    fc_values = [v for _, v, _ in fc]
    bajo = sum(1 for _, v, t in fc if v <= cutoffs[t.hour])
    bajo_usual_pct = round(100 * bajo / len(fc_values))
    referencia_fc = round(statistics.fmean(cutoffs.values()), 1)
    cobertura = round(len(fc) / esperadas, 2) if esperadas > 0 else None

    estres = _muestras_ventana(conn, person_id, zona, "intradia_estres", dias_ventana, inicio, fin,
                                horas_actividad, cache)
    estres_values = [v for _, v, _ in estres]

    return Resultado(
        fc_mediana=round(statistics.median(fc_values), 1),
        estres_mediana=round(statistics.median(estres_values), 1) if estres_values else None,
        bajo_usual_pct=bajo_usual_pct, referencia_fc=referencia_fc, cobertura=cobertura, motivo=None,
    )


def _shifts_sin_calcular(conn, person_id: int, version: str):
    return conn.execute(
        "SELECT s.day AS day, s.kind AS kind, s.code AS code, s.start_local AS start_local, "
        "s.end_local AS end_local FROM shift s WHERE s.person_id = ? AND s.kind != 'off' "
        "AND s.start_local = (SELECT MIN(start_local) FROM shift s2 "
        "WHERE s2.person_id = s.person_id AND s2.day = s.day) "
        "AND NOT EXISTS (SELECT 1 FROM turno_fc t WHERE t.person_id = s.person_id AND t.day = s.day "
        "AND t.modelo_version = ?)",
        (person_id, version),
    ).fetchall()


def recalcular(conn, person_id: int) -> dict:
    """Fills `turno_fc` for every working shift (kind != 'off', one per day -- the earliest
    start_local on the rare day with more than one row) of this person with no row yet under
    MODELO's current version -- idempotent, so a re-run after the first is cheap. A version bump
    (MODELO['version'] changed) simply recomputes: the NOT EXISTS clause above finds no row under
    the new version and fills every one, leaving the old version's rows in place (ruling 1, same
    convention as recuperacion.recalcular). Shares ONE `cache` dict across every shift in this
    call, so a day touched by several shifts' own windows or look-back is read via db.intradia at
    most once per (day, kind) for this whole run. Returns counts only (never a health figure)."""
    version = MODELO["version"]
    rows = _shifts_sin_calcular(conn, person_id, version)
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    motivos: dict[str, int] = {}
    nuevas = 0
    cache: dict = {}
    with conn:
        for r in rows:
            resultado = turno_fc(conn, person_id, r, cache)
            conn.execute(
                "INSERT OR REPLACE INTO turno_fc (person_id, day, kind, modelo_version, fc_mediana, "
                "estres_mediana, bajo_usual_pct, referencia_fc, cobertura, motivo, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (person_id, r["day"], r["kind"], version, resultado.fc_mediana, resultado.estres_mediana,
                 resultado.bajo_usual_pct, resultado.referencia_fc, resultado.cobertura, resultado.motivo,
                 ahora),
            )
            nuevas += 1
            if resultado.motivo is not None:
                motivos[resultado.motivo] = motivos.get(resultado.motivo, 0) + 1
    return {"nuevas": nuevas, "motivos": motivos}


def estudio(conn, person_id: int) -> dict:
    """Over this person's own derived rows (current MODELO version): per shift kind, the median
    heart rate against the days-off reference for the same hours (ruling 2, read straight from
    each row's own persisted `referencia_fc` -- never recomputed here, so this stays a handful of
    cheap SELECTs, well under BODY-RENDIMIENTO-1's own statement ceiling on /shifts), with the
    previous night's sleep split inside each kind (ruling 4, the same corta/normal split
    body/recuperacion.py's own estudio() uses) -- a group under MODELO['min_n_grupo'] reads
    "pocos_datos" instead of a figure. The top MODELO['max_hallazgos'] kind-level differences (vs
    the days-off reference) are returned as ready-to-word findings; a difference under MODELO's
    noise floor is never a finding."""
    version = MODELO["version"]
    rows = conn.execute(
        "SELECT day, kind, fc_mediana, referencia_fc, motivo FROM turno_fc "
        "WHERE person_id = ? AND modelo_version = ?",
        (person_id, version),
    ).fetchall()
    if not rows:
        return {"kinds": {}, "hallazgos": []}

    dias = sorted({r["day"] for r in rows})
    placeholders = ",".join("?" * len(dias))
    sleep_rows = conn.execute(
        f"SELECT day, value FROM daily_metric WHERE person_id = ? AND metric_key = 'sleep_minutes' "
        f"AND day IN ({placeholders})",
        (person_id, *dias),
    ).fetchall()
    sleep_por_dia = {r["day"]: r["value"] for r in sleep_rows}
    sleep_mediana = statistics.median(sleep_por_dia.values()) if sleep_por_dia else None

    por_kind: dict[str, dict] = defaultdict(lambda: {"fc": [], "referencia": [], "sleep": defaultdict(list)})
    for r in rows:
        if r["motivo"] is not None:
            continue
        day, kind = r["day"], r["kind"]
        entry = por_kind[kind]
        entry["fc"].append(r["fc_mediana"])
        if r["referencia_fc"] is not None:
            entry["referencia"].append(r["referencia_fc"])
        if day in sleep_por_dia and sleep_mediana is not None:
            bucket = "corta" if sleep_por_dia[day] < sleep_mediana else "normal"
            entry["sleep"][bucket].append(r["fc_mediana"])

    kinds_out: dict[str, dict] = {}
    hallazgos = []
    for kind, data in por_kind.items():
        n = len(data["fc"])
        if n < MODELO["min_n_grupo"]:
            kinds_out[kind] = {"motivo": "pocos_datos", "n": n}
            continue
        fc_mediana = round(statistics.median(data["fc"]))
        referencia_mediana = round(statistics.median(data["referencia"])) if data["referencia"] else None
        diff = (referencia_mediana - fc_mediana) if referencia_mediana is not None else None
        sleep_out = {}
        for bucket, valores in data["sleep"].items():
            if len(valores) >= MODELO["min_n_grupo"]:
                sleep_out[bucket] = {"fc_mediana": round(statistics.median(valores)), "n": len(valores)}
            else:
                sleep_out[bucket] = {"motivo": "pocos_datos", "n": len(valores)}
        kinds_out[kind] = {"n": n, "fc_mediana": fc_mediana, "referencia": referencia_mediana,
                            "diff": diff, "sleep": sleep_out}
        if diff is not None and abs(diff) >= MODELO["ruido_min_diferencia_bpm"]:
            hallazgos.append({"kind": kind, "diff": diff, "n": n, "fc_mediana": fc_mediana,
                               "referencia": referencia_mediana})

    hallazgos.sort(key=lambda h: -abs(h["diff"]))
    return {"kinds": kinds_out, "hallazgos": hallazgos[:MODELO["max_hallazgos"]]}


def main(argv: list[str] | None = None) -> int:
    """`python -m body.turno_fc` -- the vivo task `turno_fc` (body/vivo.py's TAREAS), run once for
    the first backfill and safe to re-run (`recalcular` is idempotent). Hosted: every connected
    person; local: the one local person. Prints totals only, never a person id or a health figure."""
    from . import cuenta

    conn = db.connect()
    try:
        db.ensure_ready(conn)
        from . import garmin_cuenta
        person_ids = garmin_cuenta.every_person_id(conn) if cuenta.hosted() else [db.local_person_id(conn)]
        nuevas = 0
        motivos: dict[str, int] = {}
        for person_id in person_ids:
            resultado = recalcular(conn, person_id)
            nuevas += resultado["nuevas"]
            for k, v in resultado["motivos"].items():
                motivos[k] = motivos.get(k, 0) + v
        print(f"turno_fc: {nuevas} filas nuevas en {len(person_ids)} persona(s), motivos {motivos}",
              flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
