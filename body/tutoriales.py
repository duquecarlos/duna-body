"""tutoriales-v1: Body's own catalog (`tutoriales/catalogo.json`, section 7) and Growth's published
manifest, read at runtime, never written here. Section 7b: onboarding completion is read from data
Body already has, never a stored flag -- `primeros_pasos` below is that read.

`catalogo()` loads the JSON once per process. `manifest()` fetches
`{BODY_TUTORIALES_BASE_URL}/body/manifest.json` over HTTPS with a 3 s timeout, cached in process for
10 minutes; with no base URL configured, a fetch error, or a reply that is not the expected shape, it
reads as an empty manifest (section 7: "a manifest that cannot be fetched is treated as empty, never
as an error screen") -- Ayuda still renders every task, just with "Video en preparación" in its place.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx

from . import dispositivo, privacidad

ROOT = Path(__file__).resolve().parent.parent
CATALOGO_PATH = ROOT / "tutoriales" / "catalogo.json"

_CACHE_SEGUNDOS = 600
_TIMEOUT_SEGUNDOS = 3.0

_catalogo_cache: dict | None = None
_manifest_cache: dict[str, tuple[float, dict]] = {}

_PARAM_RE = re.compile(r"\{[^}]+\}")

ONBOARDING_ORDEN = (
    "iniciar-sesion-y-aceptar", "conectar-garmin", "vincular-este-pc", "primera-mirada-a-hoy",
)

SOLO_EN_LINEA = frozenset({"/login", "/garmin", "/ajustes/dispositivos"})
"""Routes that answer 404 when `cuenta.hosted()` is false (section 7 fix b): Ayuda must not link to
them, and the count here is checked against the live routes by a test, so it cannot drift."""


def catalogo() -> dict:
    global _catalogo_cache
    if _catalogo_cache is None:
        _catalogo_cache = json.loads(CATALOGO_PATH.read_text(encoding="utf-8"))
    return _catalogo_cache


def _forma_valida(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("tareas"), dict)


def _fetch_remota(url: str) -> dict:
    r = httpx.get(url, timeout=_TIMEOUT_SEGUNDOS)
    r.raise_for_status()
    return r.json()


def manifest() -> dict:
    base = os.environ.get("BODY_TUTORIALES_BASE_URL")
    if not base:
        return {}
    url = f"{base}/body/manifest.json"
    now = time.monotonic()
    cached = _manifest_cache.get(url)
    if cached is not None and now - cached[0] < _CACHE_SEGUNDOS:
        return cached[1]
    try:
        data = _fetch_remota(url)
    except Exception:
        return {}
    if not _forma_valida(data):
        return {}
    _manifest_cache[url] = (now, data)
    return data


def video(task_id: str) -> dict | None:
    return manifest().get("tareas", {}).get(task_id)


def capitulos_con_tareas() -> list[dict]:
    """Chapters in order, each with its tasks in order (tutoriales-v1 section 5.1, Ayuda)."""
    data = catalogo()
    tareas_por_capitulo: dict[str, list[dict]] = {}
    for t in data["tareas"]:
        tareas_por_capitulo.setdefault(t["capitulo"], []).append(t)
    out = []
    for c in sorted(data["capitulos"], key=lambda c: c["orden"]):
        tareas = sorted(tareas_por_capitulo.get(c["id"], []), key=lambda t: t["orden"])
        out.append({"id": c["id"], "titulo": c["titulo"], "tareas": tareas})
    return out


def ir_a_pantalla(tarea: dict) -> str | None:
    """The "Ir a la pantalla" target (section 7): the task's own `pantalla` when it is static, else
    `pantalla_lista` when given, else no link at all -- a dynamic `pantalla` with no list to pick
    from gets no link."""
    pantalla = tarea.get("pantalla")
    if pantalla and "{" not in pantalla:
        return pantalla
    return tarea.get("pantalla_lista")


def _coincide(patron: str, path: str) -> bool:
    marcador = "\x00"
    plano = _PARAM_RE.sub(marcador, patron)
    expr = "^" + re.escape(plano).replace(marcador, "[^/]+") + "$"
    return re.fullmatch(expr, path) is not None


def tarea_en_ruta(path: str) -> dict | None:
    """The first catalog task (in catalog order) whose `pantalla` matches this concrete request
    path, static or dynamic (tutoriales-v1 section 5.2, decision 3) -- or None when this screen
    carries no task, so the in-context "¿Cómo se hace?" door has nothing to show."""
    for t in catalogo()["tareas"]:
        patron = t.get("pantalla")
        if patron and _coincide(patron, path):
            return t
    return None


def primeros_pasos(conn: sqlite3.Connection, person_id: int, hosted: bool) -> list[dict]:
    """The Primeros pasos checklist (tutoriales-v1 section 5.3, 7b, decision 4): each item's tick is
    read, at call time, from data Body already has -- never a stored flag. `vincular-este-pc` is
    local-mode-only skipped (the PC already *is* the app there), and `primera-mirada-a-hoy` is done
    exactly when every item before it is."""
    tareas = {t["id"]: t for t in catalogo()["tareas"] if t.get("onboarding")}
    inicio = privacidad.has_consented(conn, person_id) if hosted else True
    tiene_datos = conn.execute(
        "SELECT 1 FROM daily_metric WHERE person_id = ? LIMIT 1", (person_id,)
    ).fetchone() is not None
    vinculado = bool(dispositivo.listar(conn, person_id)) if hosted else None
    otros = [inicio, tiene_datos] + ([bool(vinculado)] if hosted else [])
    hecho = {
        "iniciar-sesion-y-aceptar": inicio,
        "conectar-garmin": tiene_datos,
        "vincular-este-pc": bool(vinculado),
        "primera-mirada-a-hoy": all(otros),
    }
    out = []
    for tid in ONBOARDING_ORDEN:
        if tid == "vincular-este-pc" and not hosted:
            continue
        out.append({"id": tid, "titulo": tareas[tid]["titulo"], "hecho": hecho[tid]})
    return out
