"""How long it takes Body to see that a night has ended (BODY-SUENO-LLEGADA-1,
ORCH-CARLOS-DELEGATES-TIER-1-1): Carlos, relayed by the desk, before deciding whether a
bed-presence sensor is worth buying -- "let's keep track on how much time it takes body app to
detect that I'm awake, for multiple days, and we'll see how to adjust and if we could work only
with the body app."

    python -m body.llegada_sueno --dias N   # default DIAS_DEFECTO

Reads `llegada_sueno` (body/db.py, written once per night by body/pull.py's shared per-person
pull), never Garmin's own health figures -- every number this module prints or returns is a
count or a number of MINUTES, never a time of day. Three gaps, each split by jornada
(trabajo/libre/desconocido, body/turnos_costo.py's own shift reader, shared, never copied):

    total        noche_fin  -> primera_llegada_en  (the whole pipeline Carlos actually feels)
    reloj_a_nube noche_fin  -> reloj_en_llegada     (the watch reaching Garmin's own cloud)
    nube_a_body  reloj_en_llegada -> primera_llegada_en  (Body's own pull noticing it)

A night whose gap comes out negative (a corrected night: a refetch moved noche_fin or an older
primera_llegada_en now sits before it) is counted apart, under 'descartadas', never averaged into
any of the three blocks above.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, fechas, garmin_cuenta, turnos_costo

DIAS_DEFECTO = 14

_JORNADAS = ("trabajo", "libre", "desconocido")
_BLOQUES = ("total", "reloj_a_nube", "nube_a_body")


def _jornada(conn, person_id: int, day: str) -> str:
    """BODY-SUENO-LLEGADA-1 ruling 3: never guessed -- turnos_costo's own per-day shift reader
    (`_shift_on`), shared, never copied. A shift row whose kind is 'off' is a day off ('libre');
    any other kind is a work day ('trabajo'); no shift row at all is 'desconocido' -- a day never
    confirmed either way, not assumed free."""
    shift = turnos_costo._shift_on(conn, person_id, day)
    if shift is None:
        return "desconocido"
    return "libre" if shift["kind"] == "off" else "trabajo"


def _a_utc(local_iso: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(local_iso).replace(tzinfo=tz).astimezone(timezone.utc)


def _minutos(desde: datetime, hasta: datetime) -> float:
    return (hasta - desde).total_seconds() / 60


def _resumen_bloque(valores: list[float]) -> dict:
    """Median/fastest/slowest MINUTES for one gap, over whichever nights actually landed a value
    in it -- {"noches": 0} (no figure at all) when none did, so the report never divides by zero
    or prints a bogus median of an empty list."""
    if not valores:
        return {"noches": 0}
    return {
        "noches": len(valores),
        "mediana_min": round(statistics.median(valores)),
        "mas_rapido_min": round(min(valores)),
        "mas_lento_min": round(max(valores)),
    }


def resumen_de_filas(conn, person_id: int, filas: list[dict]) -> dict:
    """The actual minutes-and-counts math, over whatever `llegada_sueno` rows the caller already
    fetched -- `resumen` below (the CLI report) and `herramientas._leer_llegada_sueno`'s own
    'resumen' field both build on this, never two copies of the same arithmetic. See the module
    docstring for the three blocks and the jornada split; a night with no `noche_fin` stored (not
    expected from body/pull.py's own writer, which only ever writes a non-empty one, but never
    assumed here) contributes to 'noches' and 'por_via' only."""
    person = db.person(conn, person_id)
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")

    por_via: dict[str, int] = {}
    descartadas = 0
    valores = {bloque: {j: [] for j in _JORNADAS} for bloque in _BLOQUES}

    for fila in filas:
        por_via[fila["via"]] = por_via.get(fila["via"], 0) + 1
        if not fila["noche_fin"]:
            continue
        jornada = _jornada(conn, person_id, fila["day"])
        fin = _a_utc(fila["noche_fin"], tz)
        llegada = datetime.fromisoformat(fila["primera_llegada_en"])
        gap_total = _minutos(fin, llegada)
        gap_reloj_a_nube = gap_nube_a_body = None
        if fila["reloj_en_llegada"]:
            reloj_dt = datetime.fromisoformat(fila["reloj_en_llegada"])
            gap_reloj_a_nube = _minutos(fin, reloj_dt)
            gap_nube_a_body = _minutos(reloj_dt, llegada)
        gaps = [gap_total] + (
            [gap_reloj_a_nube, gap_nube_a_body] if gap_reloj_a_nube is not None else [])
        if any(g < 0 for g in gaps):
            descartadas += 1
            continue
        valores["total"][jornada].append(gap_total)
        if gap_reloj_a_nube is not None:
            valores["reloj_a_nube"][jornada].append(gap_reloj_a_nube)
            valores["nube_a_body"][jornada].append(gap_nube_a_body)

    return {
        "noches": len(filas),
        "descartadas": descartadas,
        "por_via": por_via,
        **{bloque: {j: _resumen_bloque(valores[bloque][j]) for j in _JORNADAS} for bloque in _BLOQUES},
    }


def resumen(conn, person_id: int, desde: str, hasta: str) -> dict:
    """The same, fetching `llegada_sueno` rows in [desde, hasta] itself (`db.llegadas_sueno`,
    shared with `herramientas._leer_llegada_sueno`'s own 'noches' listing)."""
    return resumen_de_filas(conn, person_id, db.llegadas_sueno(conn, person_id, desde, hasta))


def _texto_bloque(etiqueta: str, bloque: dict) -> list[str]:
    out = [f"  {etiqueta}:"]
    for j in _JORNADAS:
        b = bloque[j]
        if b["noches"] == 0:
            out.append(f"    {j}: sin noches")
        else:
            out.append(f"    {j}: {b['noches']} noches -- mediana {b['mediana_min']} min, "
                        f"mas rapido {b['mas_rapido_min']} min, mas lento {b['mas_lento_min']} min")
    return out


def _texto_resumen(r: dict) -> list[str]:
    lineas = [f"{r['noches']} noches, {r['descartadas']} descartadas"]
    via_txt = ", ".join(f"{k}: {v}" for k, v in sorted(r["por_via"].items()))
    lineas.append(f"  por via: {via_txt}" if via_txt else "  por via: (ninguna)")
    lineas += _texto_bloque("reloj -> body (total)", r["total"])
    lineas += _texto_bloque("reloj -> nube", r["reloj_a_nube"])
    lineas += _texto_bloque("nube -> body", r["nube_a_body"])
    return lineas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m body.llegada_sueno")
    ap.add_argument("--dias", type=int, default=DIAS_DEFECTO)
    a = ap.parse_args(argv)

    conn = db.connect()
    try:
        db.ensure_ready(conn)
        personas = garmin_cuenta.every_person_id(conn)
        if not personas:
            print("llegada_sueno: sin personas con cuenta de Garmin")
            return 0
        for person_id in personas:
            person = db.person(conn, person_id)
            hoy = fechas.hoy_local(person)
            desde = (hoy - timedelta(days=a.dias - 1)).isoformat()
            r = resumen(conn, person_id, desde, hoy.isoformat())
            print(f"persona {person_id}:")
            for linea in _texto_resumen(r):
                print(linea)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
