"""Uploads Personal's daily commute summary to the hosted Body (ORCH-CARLOS-DELEGATES-TIER-1-1):

    python -m body.subir_desplazamiento [--file <path>]

File default: BODY_COMMUTE_FILE (body/config.py), the same env var body/desplazamiento_import.py
reads locally. Posts the whole file to /api/desplazamiento every run -- the route's own
same-computed_at no-op (body/desplazamiento_import.importar) makes re-uploading an unchanged file
harmless, so no local dedup bookkeeping is needed, unlike subir_turnos's per-file sha tracking (one
file here, rewritten about monthly, not many files arriving daily). Prints counts and the date
range only -- never a figure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, errores, pc_cliente


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m body.subir_desplazamiento")
    ap.add_argument("--file")
    a = ap.parse_args(argv)

    path = Path(a.file) if a.file else config.commute_file()
    if not path:
        print("no commute file configured (--file o BODY_COMMUTE_FILE)")
        return 1
    if not path.is_file():
        print(f"commute file not found: {path}")
        return 1

    try:
        pc_cliente.leer_enlace()
    except pc_cliente.EnlaceFaltante as e:
        print(errores.texto_propio(e))
        return 1

    data = path.read_bytes()
    try:
        status, out = pc_cliente.llamar("POST", "/api/desplazamiento", body=data)
    except Exception as e:
        print(f"No se pudo contactar el servidor ({type(e).__name__}).")
        return 1

    if status == 200:
        dias = (out or {}).get("dias", 0)
        if out and out.get("sin_cambios"):
            print(f"sin cambios: {dias} dias")
        else:
            print(f"importado: {dias} dias")
        return 0
    if status == 422:
        motivo = (out or {}).get("motivo", "desconocido")
        print(f"rechazado ({motivo})")
        return 1
    if status in (401, 403):
        print("La conexión con el servidor ya no es válida. Corre: python -m body.conectar_pc")
        return 1
    print(f"error inesperado ({status})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
