"""Links this PC to a hosted Body.

    python -m body.conectar_pc [--url https://body.duna.solutions]
    python -m body.conectar_pc --olvidar

Mint the device key in the hosted app's Ajustes -> "Conexion del PC", then run this. The key is
read hidden (never echoed, never on argv) and saved to BODY_HOME/dispositivo.json; this then checks
it with one GET /api/vida/semana.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import errores, pc_cliente

DEFAULT_URL = "https://body.duna.solutions"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m body.conectar_pc")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--olvidar", action="store_true")
    a = ap.parse_args(argv)

    if a.olvidar:
        pc_cliente.borrar_enlace()
        print("Enlace olvidado.")
        return 0

    clave = getpass.getpass("Clave del dispositivo (bdy_...): ").strip()
    if not clave.startswith("bdy_"):
        print("Esa clave no tiene el formato esperado (debe empezar con bdy_).")
        return 1

    try:
        pc_cliente.escribir_enlace(a.url, clave)
    except pc_cliente.UrlRefusada as e:
        print(errores.texto_propio(e))
        return 1

    try:
        status, _ = pc_cliente.llamar("GET", "/api/vida/semana")
    except Exception as e:
        print(f"No se pudo contactar el servidor ({type(e).__name__}).")
        return 1

    if status == 200:
        print("PC conectado.")
        return 0
    if status == 401:
        pc_cliente.borrar_enlace()
        print("Esa clave no sirve o fue desconectada.")
        return 1
    if status == 403:
        print("A esta clave le falta el permiso vida:leer.")
        return 1
    print(f"El servidor respondió con un error inesperado ({status}).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
