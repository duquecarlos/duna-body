"""Uploads new shift files to the hosted Body.

    python -m body.subir_turnos [--dir <folder>]

Folder default: BODY_SHIFTS_DIR, same env var body/shift_import.py reads locally. Every
turnos-*.json whose sha256 was not already recorded as uploaded is POSTed to /api/turnos. Prints
counts only -- never a file's date or shift content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import config, pc_cliente
from .shift_import import _candidate_files

SUBIDOS_NAME = "turnos-subidos.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _leer_subidos() -> set[str]:
    path = config.BODY_HOME / SUBIDOS_NAME
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def _escribir_subidos(shas: set[str]) -> None:
    config.ensure_home()
    (config.BODY_HOME / SUBIDOS_NAME).write_text(json.dumps(sorted(shas)), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m body.subir_turnos")
    ap.add_argument("--dir")
    a = ap.parse_args(argv)

    folder_str = a.dir or os.environ.get("BODY_SHIFTS_DIR")
    if not folder_str:
        print("no shifts folder configured (--dir o BODY_SHIFTS_DIR)")
        return 1
    folder = Path(folder_str)
    if not folder.is_dir():
        print(f"shifts folder not found: {folder}")
        return 1

    try:
        pc_cliente.leer_enlace()
    except pc_cliente.EnlaceFaltante as e:
        print(str(e))
        return 1

    subidos = _leer_subidos()
    n_subidos = n_ya_estaban = n_rechazados = 0

    for f in _candidate_files(folder):
        data = f.read_bytes()
        sha = _sha256(data)
        if sha in subidos:
            continue
        try:
            status, out = pc_cliente.llamar(
                "POST", "/api/turnos", body=data, headers={"X-Body-Archivo": f.name}
            )
        except Exception as e:
            _escribir_subidos(subidos)
            print(f"No se pudo contactar el servidor ({type(e).__name__}).")
            return 1
        if status == 200:
            subidos.add(sha)
            if out and out.get("nuevo"):
                n_subidos += 1
            else:
                n_ya_estaban += 1
        elif status == 422:
            motivo = (out or {}).get("motivo", "desconocido")
            print(f"{f.name}: rechazado ({motivo})")
            n_rechazados += 1
        elif status in (401, 403):
            _escribir_subidos(subidos)
            print("La conexión con el servidor ya no es válida. Corre: python -m body.conectar_pc")
            return 1
        else:
            print(f"{f.name}: error inesperado ({status})")
            n_rechazados += 1

    _escribir_subidos(subidos)
    print(f"{n_subidos} subidos, {n_ya_estaban} ya estaban, {n_rechazados} rechazados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
