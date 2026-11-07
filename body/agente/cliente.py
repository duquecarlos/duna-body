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

ANTHROPIC_VERSION = "2023-06-01"
PROVEEDOR_DEFAULT = "anthropic"
MODELO_DEFAULT = "claude-sonnet-5-5"

_logger = logging.getLogger("body.agente.cliente")


@dataclass(frozen=True)
class BloqueTexto:
    texto: str


@dataclass(frozen=True)
class LlamadaHerramienta:
    id: str
    nombre: str
    parametros: dict


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


# ---- OpenAI (Chat Completions API) -----------------------------------------------------------

def _mensajes_openai(mensajes: list[dict]) -> list[dict]:
    out = []
    for m in mensajes:
        resultados = [b for b in m["bloques"] if b["tipo"] == "resultado_herramienta"]
        if resultados:
            for b in resultados:
                out.append({"role": "tool", "tool_call_id": b["id"], "content": _texto_resultado(b)})
            continue
        texto = "".join(b["texto"] for b in m["bloques"] if b["tipo"] == "texto")
        if m["rol"] == "usuario":
            out.append({"role": "user", "content": texto})
            continue
        llamadas = [b for b in m["bloques"] if b["tipo"] == "llamada_herramienta"]
        msg = {"role": "assistant", "content": texto or None}
        if llamadas:
            msg["tool_calls"] = [
                {"id": l["id"], "type": "function",
                 "function": {"name": l["nombre"], "arguments": json.dumps(l["parametros"], ensure_ascii=False)}}
                for l in llamadas
            ]
        out.append(msg)
    return out


def _herramientas_openai(herramientas: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": h["nombre"], "description": h["descripcion"],
                                               "parameters": h["parametros"]}} for h in herramientas]


def _desde_openai(data: dict) -> Respuesta:
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    bloques = []
    if msg.get("content"):
        bloques.append(BloqueTexto(msg["content"]))
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"]["arguments"])
        except (TypeError, json.JSONDecodeError):
            args = {}
        bloques.append(LlamadaHerramienta(tc["id"], tc["function"]["name"], args))
    u = data.get("usage") or {}
    cacheados = ((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)) or 0
    prompt = u.get("prompt_tokens", 0) or 0
    uso = Uso(entrada=max(prompt - cacheados, 0), salida=u.get("completion_tokens", 0) or 0,
              cache_lectura=cacheados, cache_escritura=0)
    return Respuesta(bloques=bloques, motivo_fin=choice.get("finish_reason") or "", uso=uso)


class OpenAI(Proveedor):
    URL = "https://api.openai.com/v1/chat/completions"

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
        payload = {"model": self.modelo,
                   "messages": [{"role": "system", "content": sistema}] + _mensajes_openai(mensajes)}
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
            partes.append({"functionCall": {"name": b["nombre"], "args": b["parametros"]}})
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
            bloques.append(LlamadaHerramienta(fc["name"], fc["name"], fc.get("args") or {}))
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
            r = httpx.post(self._url(), json=payload, timeout=60.0, params={"key": self.api_key},
                           headers={"content-type": "application/json"})
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


# ---- selection by env (BODY-ASISTENTE-PROVEEDOR-1 decision 1) -------------------------------

_FABRICAS = {
    "anthropic": (Anthropic, "BODY_AGENTE_ANTHROPIC_API_KEY"),
    "openai": (OpenAI, "BODY_AGENTE_OPENAI_API_KEY"),
    "gemini": (Gemini, "BODY_AGENTE_GEMINI_API_KEY"),
}


def proveedor_configurado() -> Proveedor | None:
    """None when the selected provider has no API key configured -- the assistant is then "not
    configured" (web.py answers with the `no_configurado` SSE error). The app still starts either
    way: nothing here raises for a missing key."""
    nombre = os.environ.get("BODY_AGENTE_PROVEEDOR", PROVEEDOR_DEFAULT)
    entrada = _FABRICAS.get(nombre)
    if entrada is None:
        return None
    clase, var_key = entrada
    api_key = os.environ.get(var_key)
    if not api_key:
        return None
    modelo = os.environ.get("BODY_AGENTE_MODELO", MODELO_DEFAULT)
    return clase(modelo, api_key)
