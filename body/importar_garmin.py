"""BODY-GARMIN-IMPORT-1: imports Carlos's full Garmin account export into the same intraday
storage pull.traer_intradia already fills from the live API, so db.intradia() returns identical
pairs whichever path stored a day -- see db.guardar_intradia/intradia/intradia_almacenado for the
storage and precedence design. Backfills the heart-rate and stress history (hundreds of days) that
Garmin's own API no longer serves, since it keeps only its own retention window.

    python -m body.importar_garmin <path-to-export.zip> [--desde YYYY-MM-DD] [--hasta YYYY-MM-DD] [--seco]

Local person only -- run by hand on Carlos's own PC against the zip the Garmin "export your data"
request produces, never as a web route or a scheduled job. `--seco` counts what the run WOULD do
without writing anything.

Reads ONLY entries under DI_CONNECT/DI-Connect-Uploaded-Files/ that are themselves zips of .fit
files, and inside those, only .fit entries -- the export's profile, consent and customer entries
are never opened, by construction (the loop below never calls .read() or .open() on anything else).
No network. Prints counts and dates only -- never a health figure.
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, fit, garmin_adapter

UPLOADED_PREFIX = "DI_CONNECT/DI-Connect-Uploaded-Files/"

# file_id (global msg 0) field 0 `type`: 32 = monitoring_b (minute heart-rate/stress), the only
# type this slice imports. 4 = activity, left alone here -- see module docstring in the spec and
# the report for what an activity import would add.
TYPE_MONITORING_B = 32

# global msg -> field numbers body.fit.read_messages should decode for that message.
_WANTED = {0: {0}, 55: {26, 27, 253}, 227: {0, 1}}

_KIND_FC = "intradia_fc"
_KIND_ESTRES = "intradia_estres"


@dataclass
class _ParsedFit:
    file_type: int | None
    hr: list[tuple[int, int]] = field(default_factory=list)       # (timestamp_ms_utc, bpm)
    stress: list[tuple[int, int]] = field(default_factory=list)   # (timestamp_ms_utc, value)


def _parse_fit_bytes(data: bytes) -> _ParsedFit:
    file_type: int | None = None
    hr: list[tuple[int, int]] = []
    stress: list[tuple[int, int]] = []
    for gmn, fields_ in fit.read_messages(data, _WANTED):
        if gmn == 0:
            if file_type is None:
                file_type = fields_.get(0)
        elif gmn == 55:
            ts = fields_.get(253)
            bpm = fields_.get(27)
            if ts is not None and bpm is not None and bpm > 0:  # 0 bpm is not a reading either
                hr.append(((fit.FIT_EPOCH_UNIX_S + ts) * 1000, bpm))
        elif gmn == 227:
            value = fields_.get(0)
            ts = fields_.get(1, fields_.get(253))  # stress_level_time, else the record's own timestamp
            if value is not None and ts is not None and value >= 0:  # negative = no reading, see
                stress.append(((fit.FIT_EPOCH_UNIX_S + ts) * 1000, value))  # parse_stress_series
    return _ParsedFit(file_type, hr, stress)


def _iter_fit_files(zip_path: Path):
    """Yields raw bytes of every .fit entry inside every DI-Connect-Uploaded-Files/*.zip entry of
    the outer export zip. Never opens or reads any other entry of either zip."""
    with zipfile.ZipFile(zip_path) as outer:
        for name in outer.namelist():
            if not name.startswith(UPLOADED_PREFIX) or not name.lower().endswith(".zip"):
                continue
            with outer.open(name) as fh:
                inner_bytes = fh.read()
            with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
                for inner_name in inner.namelist():
                    if not inner_name.lower().endswith(".fit"):
                        continue
                    yield inner.read(inner_name)


@dataclass
class Resultado:
    conteo_tipos: Counter
    archivos_con_error: int
    dias_importados: dict[str, int]       # kind -> count of days written (or, --seco, would-write)
    dias_omitidos_api: int                # days skipped because the API already covers them
    primer_dia: str | None
    ultimo_dia: str | None


def importar(conn, person_id: int, zip_path: Path, *, desde: date | None = None,
              hasta: date | None = None, seco: bool = False) -> Resultado:
    zona = db.person(conn, person_id)["timezone"]
    tz = ZoneInfo(zona)

    conteo_tipos: Counter = Counter()
    archivos_con_error = 0
    hr_por_ts: dict[int, int] = {}
    stress_por_ts: dict[int, int] = {}

    for data in _iter_fit_files(zip_path):
        try:
            parsed = _parse_fit_bytes(data)
        except fit.FitError:
            archivos_con_error += 1
            continue
        conteo_tipos[parsed.file_type if parsed.file_type is not None else "desconocido"] += 1
        if parsed.file_type != TYPE_MONITORING_B:
            continue
        for ts_ms, bpm in parsed.hr:
            hr_por_ts[ts_ms] = bpm
        for ts_ms, value in parsed.stress:
            stress_por_ts[ts_ms] = value

    hr_por_dia = _agrupar_por_dia_local(hr_por_ts, tz)
    stress_por_dia = _agrupar_por_dia_local(stress_por_ts, tz)

    dias = sorted(set(hr_por_dia) | set(stress_por_dia))
    dias = [d for d in dias if (desde is None or date.fromisoformat(d) >= desde)
            and (hasta is None or date.fromisoformat(d) <= hasta)]

    dias_importados = {_KIND_FC: 0, _KIND_ESTRES: 0}
    dias_omitidos_api = 0
    for day in dias:
        if db.intradia_almacenado(conn, person_id, day):
            dias_omitidos_api += 1
            continue
        if day in hr_por_dia:
            dias_importados[_KIND_FC] += 1
            if not seco:
                db.guardar_intradia(conn, person_id, day, _KIND_FC,
                                     {"heartRateValues": hr_por_dia[day]},
                                     source=garmin_adapter.SOURCE_EXPORT)
        if day in stress_por_dia:
            dias_importados[_KIND_ESTRES] += 1
            if not seco:
                db.guardar_intradia(conn, person_id, day, _KIND_ESTRES,
                                     {"stressValuesArray": stress_por_dia[day]},
                                     source=garmin_adapter.SOURCE_EXPORT)

    primer_dia = dias[0] if dias else None
    ultimo_dia = dias[-1] if dias else None
    return Resultado(conteo_tipos, archivos_con_error, dias_importados, dias_omitidos_api,
                      primer_dia, ultimo_dia)


def _agrupar_por_dia_local(por_ts: dict[int, int], tz: ZoneInfo) -> dict[str, list[list[int]]]:
    """Groups {timestamp_ms_utc: value} samples (already de-duplicated by timestamp -- a day
    spans several monitoring files, and this dict is built across all of them) into local
    calendar days, sorted. Values stay as raw UTC-ms [timestamp, value] pairs, in Garmin's own
    get_heart_rates/get_stress_data shape, so garmin_adapter.parse_heart_rate_series/
    parse_stress_series converts them to local time at READ time -- exactly as it does for a day
    pulled through the live API (BODY-MOMENTOS-PULL-1's design, reused unchanged)."""
    por_dia: dict[str, dict[int, int]] = defaultdict(dict)
    for ts_ms, value in por_ts.items():
        local_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(tz)
        por_dia[local_dt.date().isoformat()][ts_ms] = value
    return {day: [[ts, v] for ts, v in sorted(mapping.items())] for day, mapping in por_dia.items()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.importar_garmin")
    p.add_argument("zip_path", type=Path)
    p.add_argument("--desde", type=date.fromisoformat, default=None)
    p.add_argument("--hasta", type=date.fromisoformat, default=None)
    p.add_argument("--seco", action="store_true")
    a = p.parse_args(argv)

    conn = db.connect()
    try:
        db.init(conn)
        person_id = db.local_person_id(conn)
        r = importar(conn, person_id, a.zip_path, desde=a.desde, hasta=a.hasta, seco=a.seco)
    finally:
        conn.close()

    etiqueta = "seco: " if a.seco else ""
    print(f"{etiqueta}archivos por tipo: "
          + ", ".join(f"{k}={v}" for k, v in sorted(r.conteo_tipos.items(), key=str)))
    print(f"{etiqueta}dias importados -- {_KIND_FC}: {r.dias_importados[_KIND_FC]}, "
          f"{_KIND_ESTRES}: {r.dias_importados[_KIND_ESTRES]}")
    print(f"{etiqueta}primer dia: {r.primer_dia or '-'}, ultimo dia: {r.ultimo_dia or '-'}")
    print(f"{etiqueta}dias omitidos (ya cubiertos por la API): {r.dias_omitidos_api}")
    print(f"{etiqueta}archivos con error: {r.archivos_con_error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
