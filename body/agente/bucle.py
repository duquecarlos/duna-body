"""The tool loop over Body's lectura registry (BODY-ASISTENTE-PROVEEDOR-1, decision 2).

Tools offered = `herramientas.disponibles(nivel)` for the person's CURRENT `nivel_datos`; every
call still goes through `herramientas.ejecutar(..., nivel)` -- enforcement at dispatch, never only
at offer. A tool error becomes a tool-result error text fed back to the model, never an exception
shown to the person. At most `MAX_RONDAS` model rounds and `MAX_LECTURAS` tool reads per question;
past either, the loop stops and raises `LimiteRondas` rather than guessing an answer.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import ajustes, herramientas
from .cliente import BloqueTexto, LlamadaHerramienta, Proveedor, ProveedorError, Uso

MAX_RONDAS = 8
MAX_LECTURAS = 5


def _sumar_uso(a: Uso, b: Uso) -> Uso:
    return Uso(entrada=a.entrada + b.entrada, salida=a.salida + b.salida,
               cache_lectura=a.cache_lectura + b.cache_lectura,
               cache_escritura=a.cache_escritura + b.cache_escritura)


class LimiteRondas(Exception):
    """MAX_RONDAS model rounds or MAX_LECTURAS tool reads were reached with no final answer."""

    def __init__(self, uso_total: Uso, rondas: int, lecturas: int):
        super().__init__(f"límite alcanzado: {rondas} rondas, {lecturas} lecturas")
        self.uso_total = uso_total
        self.rondas = rondas
        self.lecturas = lecturas


class FalloProveedor(Exception):
    """The provider call itself failed (network, HTTP status). Carries usage already spent."""

    def __init__(self, uso_total: Uso, rondas: int):
        super().__init__("fallo del proveedor")
        self.uso_total = uso_total
        self.rondas = rondas


@dataclass(frozen=True)
class EventoTexto:
    texto: str


@dataclass(frozen=True)
class EventoHerramienta:
    nombre: str


@dataclass(frozen=True)
class ResultadoBucle:
    eventos: list                 # EventoTexto | EventoHerramienta, in order
    texto_final: str
    rondas: int
    lecturas: int
    uso_total: Uso
    nuevos_mensajes: list = field(default_factory=list)  # to persist via conversacion.agregar_mensaje


def _tool_specs(nivel: str) -> list[dict]:
    return [{"nombre": h["nombre"], "descripcion": h["descripcion"], "parametros": h["parametros"]}
            for h in herramientas.disponibles(nivel)]


def ejecutar(conn: sqlite3.Connection, person_id: int, proveedor: Proveedor, sistema: str,
             mensajes: list[dict]) -> ResultadoBucle:
    nivel = ajustes.nivel_datos(conn, person_id)
    tools = _tool_specs(nivel)
    inicio = len(mensajes)
    mensajes = list(mensajes)
    eventos: list = []
    uso_total = Uso()
    rondas = 0
    lecturas = 0

    while True:
        rondas += 1
        if rondas > MAX_RONDAS:
            raise LimiteRondas(uso_total, rondas - 1, lecturas)
        try:
            resp = proveedor.responder(sistema, mensajes, tools)
        except ProveedorError:
            raise FalloProveedor(uso_total, rondas - 1) from None
        uso_total = _sumar_uso(uso_total, resp.uso)

        textos = [b.texto for b in resp.bloques if isinstance(b, BloqueTexto)]
        llamadas = [b for b in resp.bloques if isinstance(b, LlamadaHerramienta)]
        for texto in textos:
            eventos.append(EventoTexto(texto))

        bloques_asistente = [{"tipo": "texto", "texto": tx} for tx in textos] + [
            {"tipo": "llamada_herramienta", "id": l.id, "nombre": l.nombre, "parametros": l.parametros}
            for l in llamadas
        ]
        mensajes.append({"rol": "asistente", "bloques": bloques_asistente})

        if not llamadas:
            return ResultadoBucle(eventos, "".join(textos), rondas, lecturas, uso_total, mensajes[inicio:])

        resultados = []
        for l in llamadas:
            if lecturas >= MAX_LECTURAS:
                # A refusal consumes no read budget, so this cap is only ever hit by an actual,
                # successful read -- never by a tool call that was going to be refused anyway.
                raise LimiteRondas(uso_total, rondas, lecturas)
            eventos.append(EventoHerramienta(l.nombre))
            try:
                salida = herramientas.ejecutar(conn, person_id, l.nombre, l.parametros, nivel)
            except herramientas.ToolError as e:
                resultados.append({"tipo": "resultado_herramienta", "id": l.id, "nombre": l.nombre,
                                    "error": str(e)})
                continue
            lecturas += 1
            resultados.append({"tipo": "resultado_herramienta", "id": l.id, "nombre": l.nombre,
                                "resultado": salida})
        mensajes.append({"rol": "usuario", "bloques": resultados})
