"""Imports Personal's daily commute summary (ORCH-CARLOS-DELEGATES-TIER-1-1, BODY-DESPLAZAMIENTO-2):
one row per LOCAL day with four non-negative integer counts, computed by the Personal desk from the
person's own Timeline export -- no coordinates, no place names here or there. The whole file is
rewritten on each Personal run (about monthly); `validar` refuses the WHOLE file on any defect, the
same all-or-nothing rule body/shift_import.py's own `_validate` applies, and `importar` writes
nothing on refusal, recording only the reason CLASS -- never a figure or a date -- in
fuente_importacion.ultimo_error.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from . import config, db

FUENTE = "personal:commute-diario.csv"
HEADER = ["day", "vehicle_minutes", "vehicle_trips", "walking_minutes", "cycling_minutes", "computed_at"]
COUNT_COLUMNS = ("vehicle_minutes", "vehicle_trips", "walking_minutes", "cycling_minutes")
MAX_BYTES = 1024 * 1024

# Ties each Body metric_def key to its vida-v1 shared key (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2),
# so the contract names live in one place.
VIDA_KEYS = {
    "vehicle_minutes": "vida.vehicle_minutes",
    "vehicle_trips": "vida.vehicle_trips",
    "walking_minutes": "vida.walking_minutes",
    "cycling_minutes": "vida.cycling_minutes",
}


class ImportRefused(Exception):
    """The whole file failed validation, or was too big; nothing was written. `motivo` is a
    reason CLASS only, never a figure or a date."""

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def validar(texto: str, *, today: date | None = None) -> list[dict]:
    """Validates the whole file and returns its rows (one dict per day); writes nothing -- that is
    `importar`'s job. Refuses the WHOLE file, same all-or-nothing rule as shift_import._validate,
    on: a header other than the exact one, a bad date, a duplicate day, a non-integer or negative
    count, mixed computed_at, a day in the future, or over 1 MB."""
    if len(texto.encode("utf-8")) > MAX_BYTES:
        raise ImportRefused("demasiado_grande")
    try:
        rows = list(csv.reader(io.StringIO(texto)))
    except csv.Error:
        raise ImportRefused("esquema")
    if not rows or rows[0] != HEADER:
        raise ImportRefused("esquema")

    hoy = today if today is not None else datetime.now(timezone.utc).date()
    seen_days: set[str] = set()
    computed_ats: set[str] = set()
    out: list[dict] = []
    for row in rows[1:]:
        if len(row) != len(HEADER):
            raise ImportRefused("esquema")
        day_s, *counts_s, computed_at = row
        try:
            day = date.fromisoformat(day_s)
        except ValueError:
            raise ImportRefused("dia_malformado")
        if day_s in seen_days:
            raise ImportRefused("dia_duplicado")
        seen_days.add(day_s)
        if day > hoy:
            raise ImportRefused("dia_futuro")
        counts = {}
        for name, value_s in zip(COUNT_COLUMNS, counts_s):
            value_s = value_s.strip()
            if not value_s.lstrip("-").isdigit():
                raise ImportRefused("conteo_invalido")
            value = int(value_s)
            if value < 0:
                raise ImportRefused("conteo_invalido")
            counts[name] = value
        if not computed_at:
            raise ImportRefused("computed_at_malformado")
        try:
            parsed = datetime.fromisoformat(computed_at)
        except ValueError:
            raise ImportRefused("computed_at_malformado")
        if parsed.utcoffset() is None:
            raise ImportRefused("computed_at_malformado")
        computed_ats.add(computed_at)
        if len(computed_ats) > 1:
            raise ImportRefused("computed_at_mixto")
        out.append({"day": day_s, **counts, "computed_at": computed_at})
    if not out:
        raise ImportRefused("esquema")
    return out


def refusal_motivo(exc: ImportRefused) -> str:
    return exc.motivo


def importar(conn: Connection, person_id: int, texto: str) -> dict:
    """Validates the whole file first. On refusal, records ultimo_error/ultimo_error_at in
    fuente_importacion and changes nothing else. On success, writes each row's four values through
    the existing daily_metric writer (db.set_metric) with source FUENTE, then upserts
    fuente_importacion -- a no-op when this computed_at was already imported. Days absent from the
    file are never deleted or zeroed: a gap stays a gap."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        rows = validar(texto)
    except ImportRefused as e:
        with conn:
            conn.execute(
                "INSERT INTO fuente_importacion (person_id, fuente, ultimo_error, ultimo_error_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(person_id, fuente) DO UPDATE SET "
                "ultimo_error = excluded.ultimo_error, ultimo_error_at = excluded.ultimo_error_at",
                (person_id, FUENTE, e.motivo, now),
            )
        return {"ok": False, "motivo": e.motivo}

    computed_at = rows[0]["computed_at"]
    dias = sorted(r["day"] for r in rows)
    existing = conn.execute(
        "SELECT computed_at FROM fuente_importacion WHERE person_id = ? AND fuente = ?",
        (person_id, FUENTE),
    ).fetchone()
    if existing is not None and existing["computed_at"] == computed_at:
        return {"ok": True, "sin_cambios": True, "dias": len(rows),
                "primer_dia": dias[0], "ultimo_dia": dias[-1]}

    with conn:
        for r in rows:
            for key in COUNT_COLUMNS:
                db.set_metric(conn, r["day"], key, r[key], FUENTE, person_id)
        conn.execute(
            "INSERT INTO fuente_importacion (person_id, fuente, primer_dia, ultimo_dia, computed_at, "
            "dias, importado_at, ultimo_error, ultimo_error_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(person_id, fuente) DO UPDATE SET primer_dia = excluded.primer_dia, "
            "ultimo_dia = excluded.ultimo_dia, computed_at = excluded.computed_at, dias = excluded.dias, "
            "importado_at = excluded.importado_at, ultimo_error = NULL, ultimo_error_at = NULL",
            (person_id, FUENTE, dias[0], dias[-1], computed_at, len(rows), now),
        )
    return {"ok": True, "sin_cambios": False, "dias": len(rows), "primer_dia": dias[0], "ultimo_dia": dias[-1]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m body.desplazamiento_import")
    ap.add_argument("--file")
    a = ap.parse_args(argv)

    path = Path(a.file) if a.file else config.commute_file()
    if not path:
        print("no commute file configured (--file or BODY_COMMUTE_FILE)")
        return 1
    if not path.is_file():
        print(f"commute file not found: {path}")
        return 1

    texto = path.read_text(encoding="utf-8")
    conn = db.connect()
    try:
        db.init(conn)
        person_id = db.local_person_id(conn)
        resultado = importar(conn, person_id, texto)
    finally:
        conn.close()

    if not resultado["ok"]:
        print(f"rechazado ({resultado['motivo']})")
        return 1
    estado = "sin cambios" if resultado["sin_cambios"] else "importado"
    print(f"{estado}: {resultado['dias']} dias ({resultado['primer_dia']} a {resultado['ultimo_dia']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
