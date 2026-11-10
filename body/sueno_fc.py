"""BODY-SUENO-FC-1: the sleep-window heart rate itself (not the daily resting_hr aggregate Garmin
already stores) -- BODY-INFERENCIA-FC-1-INV's slice 1, read as db.intradia('intradia_fc', ...)
filtered to [sleep_session.start_local, sleep_session.end_local], with the same honest-gap/
coverage-floor treatment body/recuperacion.py and body/turno_fc.py already use. Its own baseline
(body/baselines.py, unchanged by this slice) is what slice 2 (NOCHE-INUSUAL-1, "this night looked
unusual") needs next.

Pure reads over a connection and a person id, no web imports -- the same shape as
body/recuperacion.py and body/turno_fc.py. Every number here is an ESTIMATE, versioned
(`MODELO["version"]`), against the person's OWN history, never a population table (docs/DESIGN.md
rules 4, 5, 12). The day cache and the intraday spacing/offset helpers come from
body/intradia_horario.py and body/experimentos.py, the same shared modules recuperacion.py and
turno_fc.py already use, not duplicated here.

ORCH-CARLOS-DELEGATES-TIER-1-1 rulings this module is built to:
  (1) the two series are DAILY METRICS -- `sleep_hr_avg` and `sleep_hr_min` -- filed under the
      WAKE day exactly as sleep_session is, in `daily_metric` (never a side table), so baselines,
      Tendencias, Dia, the assistant's day read and slice 2's unusual-night flag all reach them
      through the paths that already exist.
  (2) `daily_metric.source` names this derivation and its own version (`_fuente`, e.g.
      'body-sueno-fc-v1') -- a version bump recomputes (and REPLACES that night's figure, since
      `daily_metric` carries no model-version column of its own, unlike recuperacion_actividad/
      turno_fc's own tables); the Garmin API writes neither key, so there is nothing of its to
      overwrite.
  (3) the minimum is a robust low (MODELO['percentil_bajo']), never a single sample.
  (4) honest gaps: no sleep session, or minute coverage under MODELO['cobertura_min'] inside the
      window -> no value that night (REASON_CODES) -- the existing "-" on screens.
  (5) readiness does NOT read these keys in this slice.
  (6) direction for both is 'lower' -- a lower sleeping heart rate reads as more rested, the same
      word the app already uses for resting_hr -- labelled with the same care: never medical.
"""
from __future__ import annotations

import statistics
import sys

from . import db, intradia_horario
from .experimentos import _espaciado_mediana_min, _sin_offset

VERSION = "sueno-fc-v1"
MODELO = {
    "version": VERSION,
    # Below this fraction of the samples the window's own observed spacing implies, the window is
    # a coverage gap, not a thin-but-honest reading -- same convention as experimentos.COBERTURA_MIN
    # and recuperacion.MODELO's/turno_fc.MODELO's own floor.
    "cobertura_min": 0.5,
    # The low is the Nth percentile of the window's own samples, not the single lowest one -- a
    # sleep window typically holds a couple hundred per-minute readings, so one stray low sample
    # (a strap slip, a missed beat) cannot reach this rank; it still reads as a real sustained low,
    # not a single-point artefact (ruling 3).
    "percentil_bajo": 10,
}

CLAVE_AVG = "sleep_hr_avg"
CLAVE_MIN = "sleep_hr_min"
REASON_CODES = db.SUENO_FC_MOTIVOS


def _fuente(version: str) -> str:
    """daily_metric.source for this derivation's own rows -- 'body-' + the model version (ruling
    2's own example, 'body-sueno-fc-v1') -- never the version alone, so a row reads as Body's own
    derivation, distinct from a raw source tag like 'garmin' or 'self'."""
    return f"body-{version}"


def _percentil_bajo(ordenados: list[float], percentil: float) -> float:
    """Nearest-rank percentile over an already-sorted list -- simple and deterministic for the
    small n a single night's window holds."""
    n = len(ordenados)
    indice = min(n - 1, max(0, round(percentil / 100 * (n - 1))))
    return ordenados[indice]


def noche(conn, person_id: int, wake_day: str, cache: dict | None = None) -> tuple[float, float] | str:
    """This one wake day's own sleep-window heart rate: the mean and a robust low
    (MODELO['percentil_bajo']) over every intradia_fc sample inside [sleep_session.start_local,
    sleep_session.end_local] -- or an honest reason (REASON_CODES) when that cannot be said.
    `cache` is a per-run dict (intradia_horario.leer_dia's own cache) shared across many calls by
    `recalcular`; a bare call (tests, a single lookup) gets its own."""
    if cache is None:
        cache = {}
    row = conn.execute(
        "SELECT start_local, end_local FROM sleep_session WHERE person_id = ? AND day = ?",
        (person_id, wake_day),
    ).fetchone()
    if row is None or row["start_local"] is None or row["end_local"] is None:
        return "sin_sesion"

    zona = db.person(conn, person_id)["timezone"]
    inicio, fin = _sin_offset(row["start_local"]), _sin_offset(row["end_local"])
    dias_ventana = sorted({inicio.date().isoformat(), fin.date().isoformat()})
    muestras: list[tuple[str, float]] = []
    for dia in dias_ventana:
        muestras.extend(intradia_horario.leer_dia(conn, person_id, dia, zona, cache)["muestras"])
    en_ventana = sorted((ts, v) for ts, v in muestras if inicio <= _sin_offset(ts) <= fin)
    if not en_ventana:
        return "sin_cobertura"

    espaciado = _espaciado_mediana_min(en_ventana)
    if espaciado is None:
        return "sin_cobertura"
    esperadas = (fin - inicio).total_seconds() / 60 / espaciado
    if len(en_ventana) < MODELO["cobertura_min"] * esperadas:
        return "sin_cobertura"

    valores = sorted(v for _, v in en_ventana)
    avg = round(statistics.fmean(valores), 1)
    bajo = round(_percentil_bajo(valores, MODELO["percentil_bajo"]), 1)
    return avg, bajo


def recalcular(conn, person_id: int) -> dict:
    """Fills `sleep_hr_avg`/`sleep_hr_min` for every sleep_session of this person with no row yet
    under MODELO's current version -- checked on `sleep_hr_avg` alone, since both keys are always
    written together, never one without the other. Idempotent, cheap on a re-run for the common
    and growing case: a night already holding a current-version row is skipped. A version bump
    simply recomputes and REPLACES that night's figure (ruling 2) -- `daily_metric` carries no
    model-version column of its own, unlike recuperacion_actividad/turno_fc's own tables, so the
    older version's row is overwritten rather than kept alongside it. A night with a reason instead
    of a value (REASON_CODES) writes nothing, so it is read again on every future run -- bounded by
    this person's own sleep_session count, never by anything that grows faster than their actual
    nights. Shares ONE `cache` dict (see intradia_horario.leer_dia) across every night in this call,
    so a day touched by more than one night's own window is read via db.intradia at most once.
    Returns counts only (never a health figure): rows written and a tally of reason codes."""
    version = MODELO["version"]
    fuente = _fuente(version)
    rows = conn.execute(
        "SELECT day FROM sleep_session s WHERE s.person_id = ? AND NOT EXISTS ("
        "SELECT 1 FROM daily_metric d WHERE d.person_id = s.person_id AND d.day = s.day "
        "AND d.metric_key = ? AND d.source = ?)",
        (person_id, CLAVE_AVG, fuente),
    ).fetchall()
    motivos: dict[str, int] = {}
    nuevas = 0
    cache: dict = {}
    with conn:
        for r in rows:
            resultado = noche(conn, person_id, r["day"], cache)
            if isinstance(resultado, str):
                motivos[resultado] = motivos.get(resultado, 0) + 1
                continue
            avg, bajo = resultado
            db.set_metric(conn, r["day"], CLAVE_AVG, avg, fuente, person_id)
            db.set_metric(conn, r["day"], CLAVE_MIN, bajo, fuente, person_id)
            nuevas += 1
    return {"nuevas": nuevas, "motivos": motivos}


def main(argv: list[str] | None = None) -> int:
    """`python -m body.sueno_fc` -- the vivo task `sueno_fc` (body/vivo.py's TAREAS), run once for
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
        print(f"sueno_fc: {nuevas} filas nuevas en {len(person_ids)} persona(s), motivos {motivos}",
              flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
