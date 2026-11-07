"""Loads Carlos's local `.env` -- the file beside this repo where he pastes the assistant's API
key on his own PC (BODY-ENV-LOCAL-1). Read only by `body/web.py`'s `main()`, before uvicorn starts,
never at import, so tests and other modules never touch a real file.

ALLOWLIST: only names starting with `PREFIJO` are taken from the file; everything else is ignored
and counted, never loaded -- a stray Cuenta or host-secret line here must never reach os.environ.
The real environment always wins: a name already set there is left alone. Never logs or returns a
value -- only the counts `cargar()` returns.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

RUTA_DEFECTO = Path(__file__).resolve().parents[1] / ".env"
PREFIJO = "BODY_AGENTE_"
_NOMBRE_VALIDO = re.compile(r"[A-Z0-9_]+")
_EXPORT = re.compile(r"^export\s+")


def cargar(ruta: Path | None = None, entorno=os.environ) -> tuple[int, int]:
    ruta = RUTA_DEFECTO if ruta is None else ruta
    if not ruta.exists():
        return 0, 0
    texto = ruta.read_text(encoding="utf-8-sig")
    cargados = ignorados = 0
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        linea = _EXPORT.sub("", linea)
        nombre, separador, valor = linea.partition("=")
        if not separador:
            continue
        nombre = nombre.strip()
        valor = valor.strip()
        if not nombre.startswith(PREFIJO) or not _NOMBRE_VALIDO.fullmatch(nombre):
            ignorados += 1
            continue
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ("'", '"'):
            valor = valor[1:-1]
        if not valor or nombre in entorno:
            continue
        entorno[nombre] = valor
        cargados += 1
    return cargados, ignorados
