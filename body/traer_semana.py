"""Fetches this week's Body section from the hosted server for Personal's Sunday review.

    python -m body.traer_semana [--semana YYYY-MM-DD]

Writes to EXACTLY the path body/vida_export.py writes locally (BODY_HOME/vida/body-week-<monday>.json),
atomically (temp file + replace), mode 0600. Prints only the path -- never a value from the file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from . import config, errores, pc_cliente

VIDA_DIR_NAME = "vida"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m body.traer_semana")
    ap.add_argument("--semana", help="el lunes de la semana, YYYY-MM-DD (por defecto: la semana actual)")
    a = ap.parse_args(argv)

    try:
        pc_cliente.leer_enlace()
    except pc_cliente.EnlaceFaltante as e:
        print(errores.texto_propio(e))
        return 1

    ruta = "/api/vida/semana"
    if a.semana:
        ruta += f"?semana={a.semana}"

    try:
        status, data = pc_cliente.llamar("GET", ruta)
    except Exception as e:
        print(f"No se pudo contactar el servidor ({type(e).__name__}).")
        return 1

    if status == 401:
        print("La conexión con el servidor ya no es válida. Corre: python -m body.conectar_pc")
        return 1
    if status == 403:
        print("A esta clave le falta el permiso vida:leer.")
        return 1
    if status != 200 or data is None:
        print(f"El servidor respondió con un error inesperado ({status}).")
        return 1

    week_start = data.get("week_start") if isinstance(data, dict) else None
    monday = None
    if isinstance(week_start, str):
        try:
            candidato = date.fromisoformat(week_start)
        except ValueError:
            candidato = None
        if candidato is not None and candidato.weekday() == 0:
            monday = candidato
    if monday is None:
        print("El servidor no envió una semana válida (week_start).")
        return 1

    out_dir = config.BODY_HOME / VIDA_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"body-week-{monday.isoformat()}.json"
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp_path.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
