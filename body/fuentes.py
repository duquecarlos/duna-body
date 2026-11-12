"""One registry of imported sources (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3, BODY-DESPLAZAMIENTO-3
ruling 1): the sources Body imports from another app, as data, read by the Tendencias freshness
line and the per-source delete -- a later source joins by adding one entry here, never by a second
list elsewhere. `metric_keys` is read from the importer's own mapping constant (never re-listed),
so the two stay in step by construction.
"""
from __future__ import annotations

from datetime import date
from sqlite3 import Connection

from . import desplazamiento_import

FUENTES: dict[str, dict] = {
    desplazamiento_import.FUENTE: {
        "label_key": "fuente.personal_commute.label",
        # The file changes when the person re-exports Timeline, about monthly -- 45 days gives
        # margin before calling it stale.
        "metric_keys": tuple(desplazamiento_import.VIDA_KEYS.keys()),
        "umbral_dias": 45,
    },
}


def fuente_de_metrica(key: str) -> str | None:
    """Which registered source writes `key`, or None when no source does."""
    for fuente, datos in FUENTES.items():
        if key in datos["metric_keys"]:
            return fuente
    return None


def estado_fuente(conn: Connection, person_id: int, fuente: str, hoy: date) -> dict | None:
    """None when this source has never successfully imported for this person (no row at all, or a
    row that only ever recorded a refusal). Otherwise {primer_dia, ultimo_dia, dias, importado_at,
    desactualizada, error}: `desactualizada` is true once `ultimo_dia` is older than the source's
    own threshold; `error` is the last refusal's reason CLASS when that refusal happened AFTER the
    last success, else None (a refusal older than the last success is stale news, not today's
    state)."""
    if fuente not in FUENTES:
        return None
    row = conn.execute(
        "SELECT primer_dia, ultimo_dia, dias, importado_at, ultimo_error, ultimo_error_at "
        "FROM fuente_importacion WHERE person_id = ? AND fuente = ?",
        (person_id, fuente),
    ).fetchone()
    if row is None or row["importado_at"] is None:
        return None
    ultimo_dia = date.fromisoformat(row["ultimo_dia"])
    error = None
    if row["ultimo_error_at"] is not None and row["ultimo_error_at"] > row["importado_at"]:
        error = row["ultimo_error"]
    return {
        "primer_dia": row["primer_dia"],
        "ultimo_dia": row["ultimo_dia"],
        "dias": row["dias"],
        "importado_at": row["importado_at"],
        "desactualizada": (hoy - ultimo_dia).days > FUENTES[fuente]["umbral_dias"],
        "error": error,
    }
