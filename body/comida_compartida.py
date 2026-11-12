"""BODY-COMIDA-COMPARTIDA-1: one cooks, several eat, each eater's own log gets their own portion.
The ONE caller-agnostic entry point -- Body's own assistant tool (body/agente/herramientas.py's
`_registrar_comida`, through body/agente/propuestas.py's apply/undo) calls `compartir_comida`
below exactly like a future Hogar product will, through a wire Main specifies later (ORCH-HOGAR-
PRODUCTO-1 ruling B) -- that cross-product wire itself is NOT built here.

`comida_compartida` (body/db.py's schema) is a link only: who cooked, which household, which day.
Every eater's own words, portion and nutrients live on their own `comida_compartida_porcion` row
(ruling 2) -- including the author's, applied at once; every other eater's starts 'pendiente' and
is THEIRS to apply or discard (ruling (a), the person's own consent ruling: "nothing enters a log
without its owner's tap"). Applying or discarding writes through db.crear_comida/db._borrar_comida_tx,
the SAME one writer of `comida` every other meal already uses -- a shared meal's own portion is an
ordinary meal once it lands (ruling (b): "each eater's applied row is their own").
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import compartir, db, household


class ComensalFueraDeHogar(Exception):
    """A comensal who is not a member of the author's own active household."""

    def __init__(self, person_id: int):
        self.person_id = person_id


class ComensalSinConsentimiento(Exception):
    """A comensal (other than the author) who has not turned on receiving shared meals."""

    def __init__(self, person_id: int, display_name: str):
        self.person_id = person_id
        self.display_name = display_name


class PorcionAjena(Exception):
    """A porcion_id that exists but belongs to another person, or does not exist at all."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validar_nutrientes(nutrientes) -> list[dict]:
    """`nutrientes`: metric_key -> amount for ONE eater's own portion (ruling 2) -- every key must
    be one of the accumulable intake keys, every amount a positive number, a bool refused like
    every other door in this app refuses one. Returns the `cantidades` shape db.crear_comida wants."""
    if not isinstance(nutrientes, dict) or not nutrientes:
        raise ValueError("nutrientes: se necesita al menos una cantidad")
    catalogo = set(db.INGESTA_METRIC_KEYS)
    cantidades = []
    for metric_key, amount in nutrientes.items():
        if metric_key not in catalogo:
            raise ValueError(f"ingesta desconocida o no acumulable: {metric_key!r}")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            raise ValueError(f"nutrientes[{metric_key!r}]: amount debe ser un número mayor que cero")
        cantidades.append({"metric_key": metric_key, "amount": float(amount), "estimado": True})
    return cantidades


def _resultado(conn: sqlite3.Connection, comida_compartida_id: int) -> dict:
    rows = conn.execute(
        "SELECT person_id, estado FROM comida_compartida_porcion WHERE comida_compartida_id = ?",
        (comida_compartida_id,),
    ).fetchall()
    return {"comida_compartida_id": comida_compartida_id,
            "estados": {r["person_id"]: r["estado"] for r in rows}}


def compartir_comida(conn: sqlite3.Connection, autor_person_id: int, *, day: str,
                      ts_local: str | None = None, descripcion: str | None = None,
                      comensales: list[dict], origen: str, origen_ref: str | None = None) -> dict:
    """`comensales`: [{person_id, nutrientes, porcion_texto?}], the author's own portion included
    exactly once (ruling 3) -- there is no other source for the author's own amounts. Validates,
    before writing anything: the author is in an active household and every comensal is a member
    of it (`ComensalFueraDeHogar`); no person_id repeats; every non-author comensal has consent ON
    (`ComensalSinConsentimiento`, naming them); every nutrientes dict is valid. An existing
    (origen, origen_ref) returns the existing parent UNCHANGED (idempotent) -- checked first, so a
    retry never re-validates against since-changed consent or membership."""
    if origen_ref is not None:
        existing = conn.execute(
            "SELECT id FROM comida_compartida WHERE origen = ? AND origen_ref = ?",
            (origen, origen_ref),
        ).fetchone()
        if existing is not None:
            return _resultado(conn, existing["id"])

    hh = household.household_of(conn, autor_person_id)
    if hh is None:
        raise ValueError("no active household")
    miembros = set(hh.member_ids)

    vistos: set[int] = set()
    autor_cantidades: list[dict] | None = None
    otros: list[tuple[int, list[dict], str | None]] = []
    for c in comensales:
        pid = c["person_id"]
        if pid in vistos:
            raise ValueError(f"cada comensal debe aparecer una vez: {pid!r} repetido")
        vistos.add(pid)
        if pid not in miembros:
            raise ComensalFueraDeHogar(pid)
        cantidades = _validar_nutrientes(c.get("nutrientes"))
        if pid == autor_person_id:
            autor_cantidades = cantidades
            continue
        if not compartir.recibe_comidas(conn, pid):
            raise ComensalSinConsentimiento(pid, db.person(conn, pid)["display_name"])
        otros.append((pid, cantidades, c.get("porcion_texto")))

    if autor_cantidades is None:
        raise ValueError("comensales: falta la porción del propio autor")

    now = _now()
    with conn:
        cur = conn.execute(
            "INSERT INTO comida_compartida (hogar_id, autor_person_id, day, ts_local, origen, origen_ref, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hh.id, autor_person_id, day, ts_local, origen, origen_ref, now),
        )
        comida_compartida_id = cur.lastrowid
        autor_comida_id = db._crear_comida_tx(conn, autor_person_id, day, autor_cantidades, descripcion,
                                               ts_local)
        conn.execute(
            "INSERT INTO comida_compartida_porcion (comida_compartida_id, person_id, descripcion, "
            "porcion_texto, nutrientes, estado, comida_id, decided_at) "
            "VALUES (?, ?, ?, NULL, ?, 'aplicada', ?, ?)",
            (comida_compartida_id, autor_person_id, descripcion,
             json.dumps({c["metric_key"]: c["amount"] for c in autor_cantidades}, ensure_ascii=False),
             autor_comida_id, now),
        )
        for pid, cantidades, porcion_texto in otros:
            conn.execute(
                "INSERT INTO comida_compartida_porcion (comida_compartida_id, person_id, descripcion, "
                "porcion_texto, nutrientes, estado, comida_id, decided_at) "
                "VALUES (?, ?, ?, ?, ?, 'pendiente', NULL, NULL)",
                (comida_compartida_id, pid, descripcion, porcion_texto,
                 json.dumps({c["metric_key"]: c["amount"] for c in cantidades}, ensure_ascii=False)),
            )
    return _resultado(conn, comida_compartida_id)


def aplicar_porcion(conn: sqlite3.Connection, person_id: int, porcion_id: int) -> dict:
    """The owner only -- another person's portion is `PorcionAjena`, same as metas.py's own
    assignee guard and propuestas.py's own `_fila_o_ajena`. Idempotent: an already-decided portion
    just reports its current estado again, writing nothing a second time."""
    row = conn.execute("SELECT * FROM comida_compartida_porcion WHERE id = ?", (porcion_id,)).fetchone()
    if row is None or row["person_id"] != person_id:
        raise PorcionAjena()
    if row["estado"] != "pendiente":
        return {"estado": row["estado"], "comida_id": row["comida_id"]}
    parent = conn.execute(
        "SELECT day, ts_local FROM comida_compartida WHERE id = ?", (row["comida_compartida_id"],)
    ).fetchone()
    nutrientes = json.loads(row["nutrientes"])
    cantidades = [{"metric_key": k, "amount": v, "estimado": True} for k, v in nutrientes.items()]
    now = _now()
    with conn:
        comida_id = db._crear_comida_tx(conn, person_id, parent["day"], cantidades, row["descripcion"],
                                         parent["ts_local"])
        conn.execute(
            "UPDATE comida_compartida_porcion SET estado = 'aplicada', comida_id = ?, decided_at = ? "
            "WHERE id = ? AND estado = 'pendiente'",
            (comida_id, now, porcion_id),
        )
    return {"estado": "aplicada", "comida_id": comida_id}


def descartar_porcion(conn: sqlite3.Connection, person_id: int, porcion_id: int) -> dict:
    """The owner only, same guard as `aplicar_porcion`. Never deletes the row -- kept with its
    estado, the same history-not-erasure choice figura_compartida's own withdrawn_at makes."""
    row = conn.execute(
        "SELECT person_id, estado, comida_id FROM comida_compartida_porcion WHERE id = ?", (porcion_id,)
    ).fetchone()
    if row is None or row["person_id"] != person_id:
        raise PorcionAjena()
    if row["estado"] != "pendiente":
        return {"estado": row["estado"], "comida_id": row["comida_id"]}
    now = _now()
    with conn:
        conn.execute(
            "UPDATE comida_compartida_porcion SET estado = 'descartada', decided_at = ? "
            "WHERE id = ? AND estado = 'pendiente'",
            (now, porcion_id),
        )
    return {"estado": "descartada", "comida_id": None}


def descartar_pendientes_de(conn: sqlite3.Connection, person_id: int) -> None:
    """BODY-COMIDA-COMPARTIDA-1 ruling 1: "turning it off marks every pending portion addressed to
    that person as discarded" -- called by body/rutas/ajustes.py alongside
    compartir.desactivar_recibir_comidas, never on its own."""
    now = _now()
    with conn:
        conn.execute(
            "UPDATE comida_compartida_porcion SET estado = 'descartada', decided_at = ? "
            "WHERE person_id = ? AND estado = 'pendiente'",
            (now, person_id),
        )


def pendientes(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Every portion still waiting for THIS person's own tap (ruling 5, the Hoy card) -- oldest
    meal first, so a returning person sees them in the order they arrived."""
    rows = conn.execute(
        "SELECT p.id AS porcion_id, p.descripcion, p.porcion_texto, p.nutrientes, "
        "c.autor_person_id, c.day, c.ts_local "
        "FROM comida_compartida_porcion p JOIN comida_compartida c ON c.id = p.comida_compartida_id "
        "WHERE p.person_id = ? AND p.estado = 'pendiente' ORDER BY c.created_at, p.id",
        (person_id,),
    ).fetchall()
    out = []
    for r in rows:
        autor = db.person(conn, r["autor_person_id"])
        out.append({
            "porcion_id": r["porcion_id"], "autor_nombre": autor["display_name"],
            "descripcion": r["descripcion"], "porcion_texto": r["porcion_texto"],
            "nutrientes": json.loads(r["nutrientes"]), "day": r["day"], "ts_local": r["ts_local"],
        })
    return out


def deshacer_autor(conn: sqlite3.Connection, autor_person_id: int, comida_compartida_id: int) -> None:
    """BODY-COMIDA-1's own undo, extended for a shared meal (ruling 4): removes the author's own
    `comida`, discards every still-pending portion of this same parent -- an already-applied
    portion of another eater is theirs and is left exactly as it is. Called from
    body/agente/propuestas.py's `_deshacer_registrar_comida`; a no-op once already undone (the
    author's portion is no longer 'aplicada'), so a drifted or repeated undo writes nothing."""
    porcion = conn.execute(
        "SELECT id, estado, comida_id FROM comida_compartida_porcion WHERE comida_compartida_id = ? "
        "AND person_id = ?",
        (comida_compartida_id, autor_person_id),
    ).fetchone()
    if porcion is None or porcion["estado"] != "aplicada" or porcion["comida_id"] is None:
        return
    now = _now()
    comida_id = porcion["comida_id"]
    with conn:
        # The FK out of this row to `comida` is cleared BEFORE the comida itself is deleted, never
        # after -- the other order trips comida_compartida_porcion's own FK constraint.
        conn.execute(
            "UPDATE comida_compartida_porcion SET estado = 'descartada', comida_id = NULL, decided_at = ? "
            "WHERE id = ?",
            (now, porcion["id"]),
        )
        db._borrar_comida_tx(conn, autor_person_id, comida_id)
        conn.execute(
            "UPDATE comida_compartida_porcion SET estado = 'descartada', decided_at = ? "
            "WHERE comida_compartida_id = ? AND estado = 'pendiente'",
            (now, comida_compartida_id),
        )
