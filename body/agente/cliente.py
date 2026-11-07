"""The ONLY module that sends anything to a model provider (Tier 1, body-app CLAUDE.md;
BODY-ASSISTANT-DATA-LEVEL-1, Carlos's own approval to send his daily figures to the configured
provider, only those a question needs, over TLS, only when he asks).

`Proveedor` is the one interface `body/agente/bucle.py` calls: `responder(sistema, mensajes,
herramientas) -> Respuesta`. Three adapters sit behind it -- `Anthropic`, `OpenAI`, `Gemini`
(agent-capabilities-v1 section 7) -- each with a single injectable transport so tests never touch
the network. Usage is normalised here to DISJOINT counts (amendment 2): `entrada` never includes
`cache_lectura` or `cache_escritura`; a vendor that reports prompt tokens INCLUDING cached ones has
the cached part subtracted once, at this boundary.

Never logs a request or response body, a prompt, a tool result, or a key -- only an error's class
name or HTTP status.

Messages and tools travel in one generic, provider-agnostic shape that `body/agente/bucle.py`
builds and stores:
    mensaje  = {"rol": "usuario" | "asistente", "bloques": [bloque, ...]}
    bloque   = {"tipo": "texto", "texto": str}
             | {"tipo": "llamada_herramienta", "id": str, "nombre": str, "parametros": dict}
             | {"tipo": "resultado_herramienta", "id": str, "nombre": str, "resultado": Any}
             | {"tipo": "resultado_herramienta", "id": str, "nombre": str, "error": str}
    herramienta = {"nombre": str, "descripcion": str, "parametros": <JSON schema dict>}
Each adapter converts this shape to and from its vendor's own wire format.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import httpx

from . import ajustes

ANTHROPIC_VERSION = "2023-06-01"
PROVEEDOR_DEFAULT = "anthropic"
MODELO_DEFAULT = "claude-sonnet-5-5"
ORDEN_PROVEEDORES = ("anthropic", "openai", "gemini")

_logger = logging.getLogger("body.agente.cliente")


@dataclass(frozen=True)
class BloqueTexto:
    texto: str


@dataclass(frozen=True)
class LlamadaHerramienta:
    id: str
    nombre: str
    parametros: dict
    firma: str | None = None  # Gemini's thoughtSignature, carried raw; other providers never set it.


@dataclass(frozen=True)
class Uso:
    entrada: int = 0
    salida: int = 0
    cache_lectura: int = 0
    cache_escritura: int = 0


@dataclass(frozen=True)
class Respuesta:
    bloques: list = field(default_factory=list)  # BloqueTexto | LlamadaHerramienta
    motivo_fin: str = ""
    uso: Uso = field(default_factory=Uso)


class ProveedorError(Exception):
    """A provider call failed. Carries only the error CLASS or HTTP status, never a body."""


class Proveedor:
    def responder(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> Respuesta:
        raise NotImplementedError


def _texto_resultado(bloque: dict) -> str:
    if "error" in bloque:
        return bloque["error"]
    return json.dumps(bloque["resultado"], ensure_ascii=False)


# ---- Anthropic (Messages API) ----------------------------------------------------------------

def _contenido_anthropic(bloques: list[dict]) -> list[dict]:
    content = []
    for b in bloques:
        if b["tipo"] == "texto":
            if b["texto"]:
                content.append({"type": "text", "text": b["texto"]})
        elif b["tipo"] == "llamada_herramienta":
            content.append({"type": "tool_use", "id": b["id"], "name": b["nombre"], "input": b["parametros"]})
        elif b["tipo"] == "resultado_herramienta":
            bloque_result = {"type": "tool_result", "tool_use_id": b["id"], "content": _texto_resultado(b)}
            if "error" in b:
                bloque_result["is_error"] = True
            content.append(bloque_result)
    return content


def _mensajes_anthropic(mensajes: list[dict]) -> list[dict]:
    rol = {"usuario": "user", "asistente": "assistant"}
    return [{"role": rol[m["rol"]], "content": _contenido_anthropic(m["bloques"])} for m in mensajes]


def _herramientas_anthropic(herramientas: list[dict]) -> list[dict]:
    out = []
    for i, h in enumerate(herramientas):
        spec = {"name": h["nombre"], "description": h["descripcion"], "input_schema": h["parametros"]}
        if i == len(herramientas) - 1:
            spec["cache_control"] = {"type": "ephemeral"}
        out.append(spec)
    return out


def _desde_anthropic(data: dict) -> Respuesta:
    bloques = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            bloques.append(BloqueTexto(block.get("text", "")))
        elif block.get("type") == "tool_use":
            bloques.append(LlamadaHerramienta(block["id"], block["name"], block.get("input") or {}))
    u = data.get("usage") or {}
    uso = Uso(entrada=u.get("input_tokens", 0) or 0, salida=u.get("output_tokens", 0) or 0,
              cache_lectura=u.get("cache_read_input_tokens", 0) or 0,
              cache_escritura=u.get("cache_creation_input_tokens", 0) or 0)
    return Respuesta(bloques=bloques, motivo_fin=data.get("stop_reason") or "", uso=uso)


class Anthropic(Proveedor):
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, modelo: str, api_key: str, transporte=None):
        self.modelo = modelo
        self.api_key = api_key
        self._transporte = transporte or self._post_real

    def _post_real(self, payload: dict) -> dict:
        try:
            r = httpx.post(self.URL, json=payload, timeout=60.0, headers={
                "x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            })
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            _logger.warning("Anthropic http_%s", e.response.status_code)
            raise ProveedorError(f"http_{e.response.status_code}") from None
        except httpx.HTTPError as e:
            _logger.warning("Anthropic %s", type(e).__name__)
            raise ProveedorError(type(e).__name__) from None

    def responder(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> Respuesta:
        payload = {"model": self.modelo, "max_tokens": 1024, "system": sistema,
                   "messages": _mensajes_anthropic(mensajes)}
        if herramientas:
            payload["tools"] = _herramientas_anthropic(herramientas)
        return _desde_anthropic(self._transporte(payload))


# ---- OpenAI (Responses API, house shape -- duna-lotes-api agente/proveedores/openai.py) -------

def _items_openai(mensajes: list[dict]) -> list[dict]:
    items = []
    for m in mensajes:
        for b in m["bloques"]:
            if b["tipo"] == "texto":
                if not b["texto"]:
                    continue
                if m["rol"] == "usuario":
                    items.append({"role": "user", "content": [{"type": "input_text", "text": b["texto"]}]})
                else:
                    items.append({"role": "assistant", "content": [{"type": "output_text", "text": b["texto"]}]})
            elif b["tipo"] == "llamada_herramienta":
                items.append({"type": "function_call", "call_id": b["id"], "name": b["nombre"],
                              "arguments": json.dumps(b["parametros"], ensure_ascii=False)})
            elif b["tipo"] == "resultado_herramienta":
                items.append({"type": "function_call_output", "call_id": b["id"], "output": _texto_resultado(b)})
    return items


def _herramientas_openai(herramientas: list[dict]) -> list[dict]:
    return [{"type": "function", "name": h["nombre"], "description": h["descripcion"],
             "parameters": h["parametros"]} for h in herramientas]


def _desde_openai(data: dict) -> Respuesta:
    bloques = []
    for item in data.get("output", []):
        tipo = item.get("type")
        if tipo == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    bloques.append(BloqueTexto(c.get("text", "")))
        elif tipo == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                args = {}
            bloques.append(LlamadaHerramienta(item["call_id"], item["name"], args))
    u = data.get("usage") or {}
    cacheados = ((u.get("input_tokens_details") or {}).get("cached_tokens", 0)) or 0
    entrada = u.get("input_tokens", 0) or 0
    uso = Uso(entrada=max(entrada - cacheados, 0), salida=u.get("output_tokens", 0) or 0,
              cache_lectura=cacheados, cache_escritura=0)
    incompleta = data.get("incomplete_details")
    motivo_fin = incompleta.get("reason") or "incompleta" if incompleta else "completed"
    return Respuesta(bloques=bloques, motivo_fin=motivo_fin, uso=uso)


class OpenAI(Proveedor):
    URL = "https://api.openai.com/v1/responses"

    def __init__(self, modelo: str, api_key: str, transporte=None):
        self.modelo = modelo
        self.api_key = api_key
        self._transporte = transporte or self._post_real

    def _post_real(self, payload: dict) -> dict:
        try:
            r = httpx.post(self.URL, json=payload, timeout=60.0,
                           headers={"authorization": f"Bearer {self.api_key}", "content-type": "application/json"})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            _logger.warning("OpenAI http_%s", e.response.status_code)
            raise ProveedorError(f"http_{e.response.status_code}") from None
        except httpx.HTTPError as e:
            _logger.warning("OpenAI %s", type(e).__name__)
            raise ProveedorError(type(e).__name__) from None

    def responder(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> Respuesta:
        payload = {"model": self.modelo, "instructions": sistema, "input": _items_openai(mensajes),
                   "store": False}
        if herramientas:
            payload["tools"] = _herramientas_openai(herramientas)
        return _desde_openai(self._transporte(payload))


# ---- Gemini (generateContent) -----------------------------------------------------------------

def _partes_gemini(bloques: list[dict]) -> list[dict]:
    partes = []
    for b in bloques:
        if b["tipo"] == "texto":
            if b["texto"]:
                partes.append({"text": b["texto"]})
        elif b["tipo"] == "llamada_herramienta":
            parte = {"functionCall": {"name": b["nombre"], "args": b["parametros"]}}
            firma = b.get("firma")
            if firma is not None:
                parte["thoughtSignature"] = firma
            partes.append(parte)
        elif b["tipo"] == "resultado_herramienta":
            respuesta = {"error": b["error"]} if "error" in b else {"resultado": b["resultado"]}
            partes.append({"functionResponse": {"name": b["nombre"], "response": respuesta}})
    return partes


def _mensajes_gemini(mensajes: list[dict]) -> list[dict]:
    out = []
    for m in mensajes:
        tiene_resultado = any(b["tipo"] == "resultado_herramienta" for b in m["bloques"])
        rol = "function" if tiene_resultado else ("model" if m["rol"] == "asistente" else "user")
        out.append({"role": rol, "parts": _partes_gemini(m["bloques"])})
    return out


def _herramientas_gemini(herramientas: list[dict]) -> list[dict]:
    return [{"functionDeclarations": [
        {"name": h["nombre"], "description": h["descripcion"], "parameters": h["parametros"]} for h in herramientas
    ]}]


def _desde_gemini(data: dict) -> Respuesta:
    candidato = data["candidates"][0]
    bloques = []
    for parte in (candidato.get("content") or {}).get("parts", []):
        if "text" in parte:
            bloques.append(BloqueTexto(parte["text"]))
        elif "functionCall" in parte:
            fc = parte["functionCall"]
            bloques.append(LlamadaHerramienta(fc["name"], fc["name"], fc.get("args") or {},
                                              firma=parte.get("thoughtSignature")))
    meta = data.get("usageMetadata") or {}
    cacheados = meta.get("cachedContentTokenCount", 0) or 0
    prompt = meta.get("promptTokenCount", 0) or 0
    uso = Uso(entrada=max(prompt - cacheados, 0), salida=meta.get("candidatesTokenCount", 0) or 0,
              cache_lectura=cacheados, cache_escritura=0)
    return Respuesta(bloques=bloques, motivo_fin=candidato.get("finishReason") or "", uso=uso)


class Gemini(Proveedor):
    def __init__(self, modelo: str, api_key: str, transporte=None):
        self.modelo = modelo
        self.api_key = api_key
        self._transporte = transporte or self._post_real

    def _url(self) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{self.modelo}:generateContent"

    def _post_real(self, payload: dict) -> dict:
        try:
            r = httpx.post(self._url(), json=payload, timeout=60.0,
                           headers={"x-goog-api-key": self.api_key, "content-type": "application/json"})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            _logger.warning("Gemini http_%s", e.response.status_code)
            raise ProveedorError(f"http_{e.response.status_code}") from None
        except httpx.HTTPError as e:
            _logger.warning("Gemini %s", type(e).__name__)
            raise ProveedorError(type(e).__name__) from None

    def responder(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> Respuesta:
        payload = {"contents": _mensajes_gemini(mensajes), "systemInstruction": {"parts": [{"text": sistema}]}}
        if herramientas:
            payload["tools"] = _herramientas_gemini(herramientas)
        return _desde_gemini(self._transporte(payload))


# ---- Demo (BODY-ASISTENTE-PANEL-1 decision 8): no network, no key, so the panel can be measured
# in a browser without a real provider. Selected only by BODY_AGENTE_PROVEEDOR=demo, never default.

_DEMO_RESPUESTA = (
    "Esto es una respuesta de muestra del asistente, para que puedas ver el panel sin un "
    "proveedor de IA real conectado.\n\n"
    "Así se ve una lista dentro de una respuesta:\n"
    "- Primer punto de ejemplo\n"
    "- Segundo punto de ejemplo\n"
    "- Tercer punto de ejemplo\n\n"
    "Y así se ve una tabla pequeña:\n\n"
    "| Parte | Valor |\n"
    "| --- | --- |\n"
    "| Ejemplo A | 1 |\n"
    "| Ejemplo B | 2 |\n"
)


class Demo(Proveedor):
    """A scripted two-round exchange: round one asks for `leer_ajustes` (the one read tool that
    exists at every nivel_datos), round two answers with fixed, invented Spanish text -- never a
    figure from the tool's real result.

    BODY-ASISTENTE-PROPUESTAS-1 decision 8: when the question contains "idioma", round one calls
    `proponer_idioma` (toward "en", the simplest "other language" a stateless demo can target) so
    the orchestrator can measure the proposal card in a browser."""

    def __init__(self):
        self._ronda = 0

    def responder(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> Respuesta:
        self._ronda += 1
        if self._ronda == 1:
            ultima = mensajes[-1]["bloques"] if mensajes else []
            pregunta = "".join(b.get("texto", "") for b in ultima if b.get("tipo") == "texto")
            if "idioma" in pregunta.lower():
                return Respuesta(bloques=[LlamadaHerramienta("demo_1", "proponer_idioma", {"lang": "en"})],
                                  motivo_fin="tool_use", uso=Uso())
            return Respuesta(bloques=[LlamadaHerramienta("demo_1", "leer_ajustes", {})],
                              motivo_fin="tool_use", uso=Uso())
        return Respuesta(bloques=[BloqueTexto(_DEMO_RESPUESTA)], motivo_fin="end_turn", uso=Uso())


# ---- selection across every configured provider (BODY-ASISTENTE-PROVEEDORES-1 decision 1/2) --

def _modelo_anthropic() -> str:
    return os.environ.get("BODY_AGENTE_MODELO_ANTHROPIC") or os.environ.get("BODY_AGENTE_MODELO") or MODELO_DEFAULT


_FABRICAS = {
    "anthropic": (Anthropic, "BODY_AGENTE_ANTHROPIC_API_KEY", _modelo_anthropic),
    "openai": (OpenAI, "BODY_AGENTE_OPENAI_API_KEY", lambda: os.environ.get("BODY_AGENTE_MODELO_OPENAI")),
    "gemini": (Gemini, "BODY_AGENTE_GEMINI_API_KEY", lambda: os.environ.get("BODY_AGENTE_MODELO_GEMINI")),
}


def proveedores_configurados() -> dict[str, Proveedor]:
    """Every provider whose API key AND model are both set, keyed by name, anthropic/openai/gemini
    order. `demo` never appears here -- it is selected only by BODY_AGENTE_PROVEEDOR=demo,
    exclusively (decision 1)."""
    out: dict[str, Proveedor] = {}
    for nombre in ORDEN_PROVEEDORES:
        clase, var_key, modelo_fn = _FABRICAS[nombre]
        api_key = os.environ.get(var_key)
        modelo = modelo_fn()
        if api_key and modelo:
            out[nombre] = clase(modelo, api_key)
    return out


def nombre_elegido(disponibles: dict, conn=None, person_id: int | None = None) -> str | None:
    """The provider name that would answer right now (decision 2): the person's stored choice if
    it is configured; else BODY_AGENTE_PROVEEDOR if configured; else the first configured, in
    anthropic/openai/gemini order; else None."""
    if conn is not None and person_id is not None:
        elegido = ajustes.proveedor(conn, person_id)
        if elegido in disponibles:
            return elegido
    nombre_env = os.environ.get("BODY_AGENTE_PROVEEDOR", PROVEEDOR_DEFAULT)
    if nombre_env in disponibles:
        return nombre_env
    for nombre in ORDEN_PROVEEDORES:
        if nombre in disponibles:
            return nombre
    return None


def proveedor_configurado(conn=None, person_id: int | None = None) -> Proveedor | None:
    """None when nothing is configured -- the assistant is then "not configured" (web.py answers
    with the `no_configurado` SSE error). The app still starts either way: nothing here raises for
    a missing key."""
    nombre_env = os.environ.get("BODY_AGENTE_PROVEEDOR", PROVEEDOR_DEFAULT)
    if nombre_env == "demo":
        return Demo()
    disponibles = proveedores_configurados()
    nombre = nombre_elegido(disponibles, conn, person_id)
    return disponibles.get(nombre) if nombre else None
