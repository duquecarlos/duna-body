"""Two host diagnostics for the hosted-Garmin-connection-on-the-wrong-person gap
BODY-GARMIN-LECTURA-VACIA-1-INV found (ORCH-CARLOS-DELEGATES-TIER-1-1):

    python -m body.persona_garmin diagnostico
    python -m body.persona_garmin relinkar --desde <id> --hacia <id>             # dry run
    python -m body.persona_garmin relinkar --desde <id> --hacia <id> --apply     # writes

`diagnostico` prints one line per `person` row -- never a `cuenta_sub`, a display name or a health
figure, only yes/no, counts and dates (ruling 1).

`relinkar` moves an existing person's hosted Garmin connection and every Garmin-written row to
another person, day by day: a day the destination already has Garmin data for is skipped whole,
never merged or overwritten (ruling 2). It refuses outright, before writing anything, when the
source looks like a second real person rather than a stray row the Cuenta key can no longer reach
(ruling 3) -- merging two real people's data is the one irreversible mistake this tool exists to
prevent. `--apply` runs the whole move in one transaction (ruling 4): either every table and the
`garmin_cuenta` row move together, or nothing does.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db, fechas, garmin_cuenta, pull

# ---- ruling 3's census: tables a real, separate person's own hands would have written rows into.
# `daily_metric` holds THREE kinds of row: 'garmin' (device, moved below), 'self' (a manual entry's
# own aggregate -- registrar_ingesta/crear_checkin's db.set_metric writes, the spec's own "manual
# measures") and 'body-%' (Body's own derived estimate -- readiness, sueno_fc; ruling 2 already
# classes these as DERIVED, same as recuperacion_actividad/turno_fc/goal_check, so they are counted
# there -- censo_derivados -- not here). Only the 'self' rows are counted as person-authored.
# `activity` holds Garmin-written ('garmin') and person-authored ('manual') rows in the SAME table
# -- the filter counts only the manual ones, as the spec's own wording asks ("a table holding both
# Garmin and manual rows counts only its non-Garmin rows"). `workout_set` is the same shape at the
# COLUMN level: `corrected_by_person` marks the slots the person actually typed into, the only ones
# that matter here (a pure Garmin-written slot is counted as Garmin data below, never here). Every
# other table here carries no Garmin or derived rows at all -- a plain person_id count.
PERSONA_TABLAS: tuple[tuple[str, str | None], ...] = (
    ("daily_metric", "source = 'self'"),
    ("activity", "source != 'garmin'"),
    ("workout_set", "corrected_by_person = 1"),
    ("ingesta", None),
    ("comida", None),
    ("goal", None),
    ("plan", None),
    ("shift", None),
    ("shift_batch", None),
    ("shift_pattern", None),
    ("period", None),
    ("checkin", None),
    ("checkin_nota", None),
    ("moment", None),
    ("conversacion_agente", None),
    ("propuesta_agente", None),
    ("escritura_externa", None),
    ("experimento", None),
    ("nutrient_target", None),
)

# ---- ruling 2's DERIVED tables -- never moved, left at the source and only counted. `turno_fc`
# and `recuperacion_actividad` are their own tables; `goal_check` is the weekly-close table
# (goals.cerrar_semanas, the one writer BODY-SEGURIDAD-3 names); `sueno_fc`/readiness write into
# `daily_metric` itself, under a `source` naming their own derivation (body/sueno_fc.py's `_fuente`,
# e.g. 'body-sueno-fc-v1') -- never 'garmin', so the day-by-day move below already leaves them
# alone; counted here as a fourth derived line for the same visibility the other three get.
DERIVADOS_TABLAS = ("recuperacion_actividad", "turno_fc", "goal_check")

# ---- ruling 2's Garmin-written, day-keyed tables: db.save_day's own four (raw_payload --
# including every intraday kind, daily_metric, sleep_session, activity) plus guarda_detalle's own
# extras (workout_set, activity_detail), which carry no `day` column of their own and move as
# FOLLOWERS of the `activity` row they belong to (same (person_id, activity_source,
# activity_source_id) naming activity.py's own FK already uses). `daily_metric`/`activity` are
# filtered to source = 'garmin' -- the hosted live pull's own tag (garmin_adapter.SOURCE) -- never
# 'garmin_export' (the PC-import path, body/mudanza.py's own one-time move, not this tool's
# concern) and never a manual/self row (ruling 3's census above already refuses on those).
# `sleep_session` carries no `source` column at all -- every row in it is Garmin-written, so no
# filter is needed or possible.
GARMIN_DIA_TABLAS: tuple[tuple[str, str | None], ...] = (
    ("raw_payload", "source = 'garmin'"),
    ("daily_metric", "source = 'garmin'"),
    ("sleep_session", None),
    ("activity", "source = 'garmin'"),
)
TABLAS_DEPENDIENTES_ACTIVIDAD = ("workout_set", "activity_detail")
TODAS_LAS_TABLAS_MOVIBLES = tuple(t for t, _ in GARMIN_DIA_TABLAS) + TABLAS_DEPENDIENTES_ACTIVIDAD


class Refusada(Exception):
    """relinkar refused before writing anything. `motivos` is one line per reason, each already
    counts-only -- never a cuenta_sub, a name or a figure."""

    def __init__(self, motivos: list[str]):
        self.motivos = list(motivos)
        super().__init__("; ".join(motivos))


# ---- diagnostico ----------------------------------------------------------------------------------

def censo_personas_autoras(conn: sqlite3.Connection, person_id: int) -> dict[str, int]:
    """Ruling 3's census for one person: table -> row count, zero-count tables left out."""
    out: dict[str, int] = {}
    for tabla, filtro in PERSONA_TABLAS:
        where = "person_id = ?" + (f" AND {filtro}" if filtro else "")
        n = conn.execute(f'SELECT COUNT(*) FROM "{tabla}" WHERE {where}', (person_id,)).fetchone()[0]
        if n:
            out[tabla] = n
    return out


def censo_derivados(conn: sqlite3.Connection, person_id: int) -> dict[str, int]:
    """Ruling 2's derived-table census for one person: table -> row count (0 included, unlike
    censo_personas_autoras -- this is a report of what is left behind, not a refusal trigger)."""
    out = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE person_id = ?', (person_id,)).fetchone()[0]
           for t in DERIVADOS_TABLAS}
    out["daily_metric(estimado)"] = conn.execute(
        "SELECT COUNT(*) FROM daily_metric WHERE person_id = ? AND source LIKE 'body-%'", (person_id,)
    ).fetchone()[0]
    return out


def diagnostico(conn: sqlite3.Connection) -> list[dict]:
    """Ruling 1: one row per `person`, counts and dates only -- never a cuenta_sub, a name or a
    figure. `nombre` is whether display_name is anything other than db.init's own seed default
    ('Me') -- the signal that distinguishes an actually-used row from the placeholder every fresh
    database starts with, printed as yes/no like everything else here, never the string itself.
    `dias_garmin_14`/`ultimo_dia_garmin`/`dias_garmin_total` all reuse
    garmin_cuenta.dias_garmin_con_valor (ruling 1: no second query shape), the same day definition
    the daily pull's own gap-fill and sync-status reads already use. The 14-day window is anchored
    on this process's own local date, the same single clock for every person in the listing -- a
    coarse, cross-person diagnostic, not a per-person precise count."""
    rows = conn.execute("SELECT id, cuenta_sub, display_name FROM person ORDER BY id").fetchall()
    hoy = date.today()
    desde_14 = (hoy - timedelta(13)).isoformat()
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for r in rows:
        pid = r["id"]
        dias_total = garmin_cuenta.dias_garmin_con_valor(conn, pid)
        dias_14 = garmin_cuenta.dias_garmin_con_valor(conn, pid, desde=desde_14)
        garmin_desde = garmin_cuenta.conectado_desde(conn, pid)
        dispositivos = conn.execute(
            "SELECT COUNT(*) FROM dispositivo WHERE person_id = ?", (pid,)
        ).fetchone()[0]
        sesiones_vivas = conn.execute(
            "SELECT COUNT(*) FROM sesion WHERE person_id = ? AND expires_at > ?", (pid, ahora)
        ).fetchone()[0]
        out.append({
            "id": pid,
            "cuenta_sub": r["cuenta_sub"] is not None,
            "nombre": r["display_name"] != "Me",
            "garmin": garmin_cuenta.conectado(conn, pid),
            "garmin_desde": garmin_desde[:10] if garmin_desde else None,
            "dias_garmin_14": len(dias_14),
            "ultimo_dia_garmin": max(dias_total) if dias_total else None,
            "dias_garmin_total": len(dias_total),
            "filas_propias": sum(censo_personas_autoras(conn, pid).values()),
            "dispositivos": dispositivos,
            "sesiones_vivas": sesiones_vivas,
        })
    return out


def _print_diagnostico(rows: list[dict]) -> None:
    for r in rows:
        print(
            f"persona {r['id']}: cuenta_sub={'si' if r['cuenta_sub'] else 'no'}, "
            f"nombre={'si' if r['nombre'] else 'no'}, garmin={'si' if r['garmin'] else 'no'}, "
            f"garmin_desde={r['garmin_desde'] or '-'}, dias_garmin_14={r['dias_garmin_14']}, "
            f"ultimo_dia_garmin={r['ultimo_dia_garmin'] or '-'}, "
            f"dias_garmin_total={r['dias_garmin_total']}, filas_propias={r['filas_propias']}, "
            f"dispositivos={r['dispositivos']}, sesiones_vivas={r['sesiones_vivas']}"
        )


# ---- relinkar ----------------------------------------------------------------------------------

def _existe_persona(conn: sqlite3.Connection, person_id: int) -> bool:
    return conn.execute("SELECT 1 FROM person WHERE id = ?", (person_id,)).fetchone() is not None


def _cuenta_sub(conn: sqlite3.Connection, person_id: int) -> str | None:
    return conn.execute("SELECT cuenta_sub FROM person WHERE id = ?", (person_id,)).fetchone()["cuenta_sub"]


def _validar(conn: sqlite3.Connection, desde_id: int, hacia_id: int) -> None:
    """Ruling 3's refusal, checked before anything is written. Every applicable reason is
    collected and raised together, each one counts-only."""
    motivos: list[str] = []
    if desde_id == hacia_id:
        motivos.append(f"--desde y --hacia son la misma persona ({desde_id})")
    desde_existe = _existe_persona(conn, desde_id)
    hacia_existe = _existe_persona(conn, hacia_id)
    if not desde_existe:
        motivos.append(f"no existe persona {desde_id} (--desde)")
    if not hacia_existe:
        motivos.append(f"no existe persona {hacia_id} (--hacia)")
    if motivos:
        raise Refusada(motivos)

    if _cuenta_sub(conn, hacia_id) is None:
        motivos.append(f"persona {hacia_id} (--hacia) no tiene cuenta_sub")
    if _cuenta_sub(conn, desde_id) is not None:
        motivos.append(f"persona {desde_id} (--desde) tiene cuenta_sub")
    for tabla, n in sorted(censo_personas_autoras(conn, desde_id).items()):
        motivos.append(f"persona {desde_id} (--desde) tiene {n} fila(s) propias en {tabla}")
    if garmin_cuenta.conectado(conn, hacia_id):
        motivos.append(f"persona {hacia_id} (--hacia) ya tiene garmin_cuenta")
    if motivos:
        raise Refusada(motivos)


def _mover_filas(conn: sqlite3.Connection, tabla: str, rows: list[sqlite3.Row], hacia_id: int,
                  apply: bool) -> int:
    """Copies `rows` into `tabla` under `hacia_id`. `id`, where the table has one (workout_set's
    own surrogate key -- every other movable table's primary key is composite, with no `id`
    column at all), is left out so the destination assigns a fresh one: the column is a plain
    autoincrement handle, never part of any UNIQUE identity this move needs to preserve, and
    copying its exact value would collide with the source's own still-present row of the same id
    (the column is not scoped per person)."""
    if rows and apply:
        cols = [c for c in rows[0].keys() if c not in ("person_id", "id")]
        col_sql = ", ".join(f'"{c}"' for c in (["person_id"] + cols))
        placeholders = ", ".join("?" for _ in range(len(cols) + 1))
        for row in rows:
            valores = [hacia_id] + [row[c] for c in cols]
            conn.execute(f'INSERT INTO "{tabla}" ({col_sql}) VALUES ({placeholders})', valores)
    return len(rows)


def _mover_garmin_cuenta(conn: sqlite3.Connection, desde_id: int, hacia_id: int, apply: bool) -> str:
    """Ruling 2: the token decrypted under `desde_id` and re-encrypted under `hacia_id` -- an
    INSERT at the new id plus a DELETE at the old one, never an UPDATE of person_id (the AEAD
    associated data binds the ciphertext to the id it was encrypted for, so an UPDATE alone would
    leave it undecryptable, exactly as the spec's own surface names). Uses garmin_cuenta.leer to
    decrypt and garmin_cuenta's own key/nonce/associated-data primitives to re-encrypt, rather than
    calling garmin_cuenta.guardar/borrar directly -- both of those commit internally, which would
    break ruling 4's single-transaction guarantee for the move as a whole; see this slice's ledger
    entry for the full reasoning."""
    row = conn.execute(
        "SELECT conectado_en, ultimo_error, backfill_hasta, ultimo_intento_en, reintentar_despues_de, "
        "reloj_sincronizo_en FROM garmin_cuenta WHERE person_id = ?", (desde_id,)
    ).fetchone()
    if row is None:
        return "ninguno"
    if not apply:
        return "moveria"
    token = garmin_cuenta.leer(conn, desde_id)
    aesgcm = AESGCM(garmin_cuenta._key())
    nonce = os.urandom(garmin_cuenta._NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, token.encode(), garmin_cuenta._aad(hacia_id))
    conn.execute(
        "INSERT INTO garmin_cuenta (person_id, token_cifrado, nonce, conectado_en, ultimo_error, "
        "backfill_hasta, ultimo_intento_en, reintentar_despues_de, reloj_sincronizo_en) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (hacia_id, ciphertext, nonce, row["conectado_en"], row["ultimo_error"], row["backfill_hasta"],
         row["ultimo_intento_en"], row["reintentar_despues_de"], row["reloj_sincronizo_en"]),
    )
    conn.execute("DELETE FROM garmin_cuenta WHERE person_id = ?", (desde_id,))
    return "movida"


def _mover_todo(conn: sqlite3.Connection, desde_id: int, hacia_id: int, apply: bool) -> dict:
    dias_origen = garmin_cuenta.dias_garmin_con_valor(conn, desde_id)
    dias_destino = garmin_cuenta.dias_garmin_con_valor(conn, hacia_id)

    counts = {t: {"mov": 0, "ya": 0} for t in TODAS_LAS_TABLAS_MOVIBLES}
    dias_movidos = dias_omitidos = 0
    primer_dia_movido: str | None = None
    recuperacion_huerfana = 0

    for day in sorted(dias_origen):
        omitido = day in dias_destino
        if omitido:
            dias_omitidos += 1
        else:
            dias_movidos += 1
            if primer_dia_movido is None:
                primer_dia_movido = day

        actividad_rows: list[sqlite3.Row] = []
        for tabla, filtro in GARMIN_DIA_TABLAS:
            where = "person_id = ? AND day = ?" + (f" AND {filtro}" if filtro else "")
            rows = conn.execute(f'SELECT * FROM "{tabla}" WHERE {where}', (desde_id, day)).fetchall()
            if tabla == "activity":
                actividad_rows = rows
            if omitido:
                counts[tabla]["ya"] += len(rows)
                continue
            counts[tabla]["mov"] += _mover_filas(conn, tabla, rows, hacia_id, apply)
            if tabla != "activity" and apply and rows:
                conn.execute(f'DELETE FROM "{tabla}" WHERE {where}', (desde_id, day))

        for row in actividad_rows:
            source_id = row["source_id"]
            for dep_tabla in TABLAS_DEPENDIENTES_ACTIVIDAD:
                wdep = "person_id = ? AND activity_source = 'garmin' AND activity_source_id = ?"
                drows = conn.execute(
                    f'SELECT * FROM "{dep_tabla}" WHERE {wdep}', (desde_id, source_id)
                ).fetchall()
                if omitido:
                    counts[dep_tabla]["ya"] += len(drows)
                    continue
                counts[dep_tabla]["mov"] += _mover_filas(conn, dep_tabla, drows, hacia_id, apply)
                if apply and drows:
                    conn.execute(f'DELETE FROM "{dep_tabla}" WHERE {wdep}', (desde_id, source_id))

        # Only now, once every dependent row is gone, is the activity row itself safe to delete --
        # workout_set/activity_detail's own FK to it would otherwise refuse the delete. One more
        # FK points at `activity` the derived `recuperacion_actividad` table's own
        # (person_id, activity_source, activity_source_id) -- ruling 2 leaves derived tables
        # untouched at the source, but a recuperacion_actividad row whose OWN activity just moved
        # away has lost the row it was computed from: it cannot be left behind (the FK refuses the
        # delete below) and it is not Garmin data to move either, so it is removed here as a
        # measured exception to "derived rows stay put", counted separately
        # (`recuperacion_huerfana`) rather than silently folded into either count. The destination
        # recomputes its own after the move (ruling 4); this never touches turno_fc/goal_check,
        # neither of which carries any FK to `activity`.
        if not omitido and apply and actividad_rows:
            for row in actividad_rows:
                recuperacion_huerfana += conn.execute(
                    "DELETE FROM recuperacion_actividad WHERE person_id = ? AND activity_source = 'garmin' "
                    "AND activity_source_id = ?", (desde_id, row["source_id"]),
                ).rowcount
            conn.execute(
                "DELETE FROM activity WHERE person_id = ? AND day = ? AND source = 'garmin'",
                (desde_id, day),
            )

    garmin_estado = _mover_garmin_cuenta(conn, desde_id, hacia_id, apply)

    return {
        "tablas": counts,
        "dias_movidos": dias_movidos,
        "dias_omitidos": dias_omitidos,
        "garmin_cuenta": garmin_estado,
        "primer_dia_movido": primer_dia_movido,
        "derivados_desde": censo_derivados(conn, desde_id),
        "recuperacion_actividad_huerfana_eliminada": recuperacion_huerfana,
    }


def relinkar(conn: sqlite3.Connection, desde_id: int, hacia_id: int, apply: bool) -> dict:
    """Ruling 2-4. Raises Refusada (ruling 3) before writing anything. `--apply` runs the whole
    move in one transaction: any error rolls back everything (ruling 4)."""
    _validar(conn, desde_id, hacia_id)
    try:
        resultado = _mover_todo(conn, desde_id, hacia_id, apply)
    except Exception:
        if apply:
            conn.rollback()
        raise
    if apply:
        conn.commit()
        recompute = _recompute_destino(conn, hacia_id, resultado["primer_dia_movido"])
        resultado["recompute"] = recompute
    return resultado


def _recompute_destino(conn: sqlite3.Connection, hacia_id: int, primer_dia_movido: str | None) -> dict:
    """Ruling 4, after the commit: the destination's existing post-pull recomputations, the same
    safe wrappers the daily pull uses (each already catches its own failure and prints a
    counts-only line) -- never re-run for the source, which lost its Garmin data to move."""
    desde = date.fromisoformat(primer_dia_movido) if primer_dia_movido else None
    pull._recalcular_recuperacion_seguro(conn, hacia_id)
    pull._recalcular_turno_fc_seguro(conn, hacia_id)
    pull._recalcular_sueno_fc_seguro(conn, hacia_id)
    hoy = fechas.hoy_local(db.person(conn, hacia_id))
    pull._cerrar_semanas_seguro(conn, hacia_id, desde=desde)
    return {"hoy": hoy.isoformat(), "desde": primer_dia_movido}


def _print_relinkar(resultado: dict, apply: bool) -> None:
    etiqueta = "movidas" if apply else "moveria"
    for tabla, c in resultado["tablas"].items():
        print(f"{tabla}: {etiqueta}={c['mov']} ya_tenia={c['ya']}")
    etiqueta_dias = "movidos" if apply else "moveria"
    print(f"dias: {etiqueta_dias}={resultado['dias_movidos']} omitidos={resultado['dias_omitidos']}")
    print(f"garmin_cuenta: {resultado['garmin_cuenta']}")
    for tabla, n in resultado["derivados_desde"].items():
        print(f"{tabla} (origen, derivado, no se mueve): {n}")
    if resultado["recuperacion_actividad_huerfana_eliminada"]:
        print(
            "recuperacion_actividad (origen, huerfana de una actividad movida, eliminada): "
            f"{resultado['recuperacion_actividad_huerfana_eliminada']}"
        )


# ---- CLI ----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.persona_garmin")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("diagnostico", help="per-person counts only -- never a sub, a name or a figure")
    r = sub.add_parser(
        "relinkar", help="move one person's hosted Garmin connection and data to another, dry run by default"
    )
    r.add_argument("--desde", required=True, type=int)
    r.add_argument("--hacia", required=True, type=int)
    r.add_argument("--apply", action="store_true", help="write the move (default: dry run)")
    a = p.parse_args(argv)

    conn = db.connect()
    try:
        db.ensure_ready(conn)
        if a.action == "diagnostico":
            _print_diagnostico(diagnostico(conn))
            return 0
        try:
            resultado = relinkar(conn, a.desde, a.hacia, apply=a.apply)
        except Refusada as exc:
            for motivo in exc.motivos:
                print(f"refusado: {motivo}")
            return 4
        _print_relinkar(resultado, apply=a.apply)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
