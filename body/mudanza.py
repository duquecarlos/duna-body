"""Move one person's Body data from a PC database into the database this process's own config
points at (BODY-MUDANZA-HERRAMIENTA-1, following BODY-MUDANZA-SERVIDOR-1-INV's recommendation).

    python -m body.mudanza importar --origen <pc.sqlite> --cuenta-sub <sub>             # dry run
    python -m body.mudanza importar --origen <pc.sqlite> --cuenta-sub <sub> --apply      # writes

The source file is opened READ-ONLY, the same way `body.backup snapshot` opens its source
(`backup.open_readonly`), and is never written to -- it is COPIED to a temporary file and that
copy, never the upload, is brought to today's schema before any row is read (BODY-MUDANZA-
ORIGEN-ANTIGUO-1, ruling 1: any person's PC can be behind the host's code). The destination is
whatever `db.connect()` resolves to (the host's own database, when this runs on the host). The
destination person is found by Cuenta `sub`; if none exists yet it is created exactly as a first
Cuenta sign-in would create it, through `cuenta.resolve_person` -- never by a hand-written INSERT
that could drift from that path (ruling 1 of BODY-MUDANZA-HERRAMIENTA-1).

Never prints a value from any row -- table names and counts only (ORCH-CARLOS-DELEGATES-TIER-1-1,
this slice's own privacy rule).
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import backup, config, cuenta, db, errores

_MISSING = object()


@dataclass(frozen=True)
class TableSpec:
    name: str
    scope: str = "person"          # "person" (own person_id column) or "parent" (via another table's row)
    parent_fk: str | None = None   # column naming the parent, when scope == "parent"
    parent_table: str | None = None
    match_cols: tuple[str, ...] | None = None   # None => no natural key, matched by full row content
    fk_remap: tuple[str, ...] = ()              # columns that hold another migrated table's old id
    fk_table: dict[str, str] = field(default_factory=dict)   # column -> the table it references
    person_ref_cols: tuple[str, ...] = ()       # columns holding a RAW person id (not goal/habit/etc.)
    other_person_filter: Callable[[dict], bool] | None = None
    id_exported: bool = False      # True when another table's fk_remap points at this one
    own_id_from: str | None = None  # checkin_nota: its own id IS checkin's id, not autoincrement
    self_ref_cols: tuple[str, ...] = ()  # columns holding an id of THIS SAME table (goal.horizonte_goal_id,
    # horizonte_meta_semanal.reemplaza_id): excluded from matching, inserted NULL, then resolved in a
    # second pass from the table's own id map once every row is migrated.


def _hogar_filter(row: dict) -> bool:
    return row.get("hogar_id") is not None


# Catalogs, sessions, tokens and devices -- never copied (ruling 2). hogar_miembro is this
# person's OWN row, but it is a mirror Cuenta re-derives at every sign-in (body/household.py's
# sincronizar_desde_claim); copying today's snapshot would just go stale, so it is left for the
# next sign-in to rebuild, like a session is left for the next sign-in to create.
SKIPPED_TABLES = {
    "metric_def": "catalog",
    "period_kind": "catalog",
    "precio_modelo": "catalog",
    "sesion": "session",
    "inicio_sesion": "session",
    "logout_jti": "session",
    "dispositivo": "device",
    "garmin_cuenta": "token (the host pulls Garmin itself, BODY-GARMIN-HOST-1 -- no token moves)",
    "hogar_miembro": "re-derived from Cuenta at the next sign-in, never hand-copied",
    # BODY-COMIDA-COMPARTIDA-1: a shared-meal link names another household member, who this
    # single-person source install never has a row for -- the same choice as figura_compartida and
    # hogar_miembro, never hand-copied. No nutrition is lost: an already-applied portion's own
    # comida/ingesta rows are this person's own data and migrate normally through the comida table.
    "comida_compartida": "names another household member as cook or eater, never hand-copied",
    "comida_compartida_porcion": "the shared-meal link's own per-eater row, never hand-copied",
    "consentimiento_comida_compartida": "re-settable in Settings after the move, never hand-copied",
    # BODY-TENDENCIAS-RACHA-GRAFICO-1: the person's own pick of which habit the strength-session
    # study compares against -- re-settable from Tendencias after the move, never hand-copied
    # (the same choice as consentimiento_comida_compartida above; its habit_id would need the same
    # id remap habit itself gets, for one setting that costs nothing to re-pick).
    "racha_piso": "re-settable from Tendencias after the move, never hand-copied",
}

# The person-scoped tables, in FK-safe order: every table a row above depends on appears earlier.
REGISTRY: tuple[TableSpec, ...] = (
    TableSpec("goal", match_cols=None, id_exported=True, other_person_filter=_hogar_filter,
              self_ref_cols=("horizonte_goal_id",)),
    TableSpec("habit", scope="parent", parent_fk="goal_id", parent_table="goal",
              match_cols=None, fk_remap=("goal_id",), fk_table={"goal_id": "goal"},
              person_ref_cols=("assignee_person_id",), id_exported=True),
    TableSpec("habit_check", match_cols=("habit_id", "person_id", "day"),
              fk_remap=("habit_id",), fk_table={"habit_id": "habit"}),
    TableSpec("goal_check", match_cols=("goal_id", "person_id", "week_start"),
              fk_remap=("goal_id",), fk_table={"goal_id": "goal"}),
    TableSpec("goal_enlace", scope="parent", parent_fk="goal_id", parent_table="goal",
              match_cols=("goal_id", "app"), fk_remap=("goal_id",), fk_table={"goal_id": "goal"}),
    TableSpec("plan", match_cols=None, fk_remap=("goal_id",), fk_table={"goal_id": "goal"},
              other_person_filter=_hogar_filter),
    TableSpec("conversacion_agente", match_cols=None, id_exported=True),
    TableSpec("mensaje_agente", match_cols=None, fk_remap=("conversacion_id",),
              fk_table={"conversacion_id": "conversacion_agente"}),
    TableSpec("propuesta_agente", match_cols=None, fk_remap=("conversacion_id", "objetivo_id"),
              fk_table={"conversacion_id": "conversacion_agente", "objetivo_id": "goal"}),
    TableSpec("uso_agente", match_cols=None),
    TableSpec("uso_herramienta_externa", match_cols=None),
    TableSpec("daily_metric", match_cols=("person_id", "day", "metric_key")),
    TableSpec("sleep_session", match_cols=("person_id", "day")),
    TableSpec("raw_payload", match_cols=("person_id", "day", "kind", "source")),
    TableSpec("activity", match_cols=("person_id", "source", "source_id")),
    TableSpec("workout_set",
              match_cols=("person_id", "activity_source", "activity_source_id", "set_order")),
    TableSpec("shift_batch", match_cols=("person_id", "file_name", "received", "extracted_at"),
              id_exported=True),
    TableSpec("shift_batch_item", scope="parent", parent_fk="batch_id", parent_table="shift_batch",
              match_cols=("batch_id", "day", "start_local"), fk_remap=("batch_id",),
              fk_table={"batch_id": "shift_batch"}),
    TableSpec("shift", match_cols=("person_id", "day", "start_local"),
              fk_remap=("batch_id",), fk_table={"batch_id": "shift_batch"}),
    TableSpec("shift_pattern", match_cols=("person_id",)),
    TableSpec("ajuste_agente", match_cols=("person_id",)),
    TableSpec("tope_agente", match_cols=("person_id",)),
    TableSpec("consentimiento", match_cols=("person_id", "version")),
    TableSpec("moment", match_cols=None, id_exported=True),
    TableSpec("experimento", match_cols=None, fk_remap=("habito_id", "goal_id"),
              fk_table={"habito_id": "habit", "goal_id": "goal"}, id_exported=True),
    # BODY-PLAN-ESQUEMA-1-R (ORCH-CARLOS-DELEGATES-TIER-1-1): the plan's own parts move with the
    # person, like goal itself -- a half-built shape (SKIPPED_TABLES) would silently lose a plan
    # the day the plan screens ship. Each child's horizonte_goal_id follows goal's own
    # other_person_filter through fk_remap -- a horizon goal filtered out as another person's
    # leaves its children "missing", counted as other_person, same as any other fk_remap.
    TableSpec("horizonte_regla", match_cols=None, fk_remap=("horizonte_goal_id",),
              fk_table={"horizonte_goal_id": "goal"}),
    TableSpec("horizonte_semana", match_cols=("horizonte_goal_id", "week_start"),
              fk_remap=("horizonte_goal_id",), fk_table={"horizonte_goal_id": "goal"}),
    TableSpec("horizonte_meta_semanal", match_cols=None, fk_remap=("horizonte_goal_id",),
              fk_table={"horizonte_goal_id": "goal"}, self_ref_cols=("reemplaza_id",)),
    TableSpec("horizonte_checkpoint", match_cols=None, fk_remap=("horizonte_goal_id",),
              fk_table={"horizonte_goal_id": "goal"}),
    TableSpec("experimento_registro", match_cols=("experimento_id", "day"),
              fk_remap=("experimento_id", "momento_id"),
              fk_table={"experimento_id": "experimento", "momento_id": "moment"}),
    TableSpec("comida", match_cols=None, id_exported=True),
    TableSpec("ingesta", match_cols=None, fk_remap=("comida_id",), fk_table={"comida_id": "comida"}),
    TableSpec("period", match_cols=None),
    TableSpec("nutrient_target", match_cols=("person_id", "metric_key", "start_day")),
    TableSpec("fuente_importacion", match_cols=("person_id", "fuente")),
    TableSpec("recuperacion_actividad",
              match_cols=("person_id", "activity_source", "activity_source_id", "modelo_version")),
    TableSpec("turno_fc", match_cols=("person_id", "day", "modelo_version")),
    TableSpec("checkin", match_cols=None, id_exported=True),
    TableSpec("checkin_nota", match_cols=("id",), own_id_from="checkin"),
    # Explicitly shared with a household (BODY-HOGAR-COMPARTIR-1) -- every row names another
    # person's visibility, so every row is counted as "other person" (ruling 5), never copied.
    TableSpec("figura_compartida", match_cols=None, other_person_filter=lambda row: True),
)

REGISTRY_BY_NAME = {spec.name: spec for spec in REGISTRY}

# habit, goal_enlace and shift_batch_item carry no literal `person_id` column (the same exception
# body/privacidad.py's own person_tables() documents) -- they are still in REGISTRY, scoped through
# their parent instead.
_NO_PERSON_ID_COLUMN = {"habit", "goal_enlace", "shift_batch_item"}


@dataclass
class TableCounts:
    rows_source: int = 0
    already_present: int = 0
    other_person: int = 0
    insert: int = 0
    update: int = 0
    skip: int = 0
    self_ref_unresolved: int = 0


@dataclass
class TableReport:
    name: str
    skipped_table: bool = False
    reason: str | None = None
    counts: TableCounts | None = None
    # Set only on the synthetic "origen" report `importar` inserts first (BODY-MUDANZA-ORIGEN-
    # ANTIGUO-1, ruling 2): the number of tables the source copy's own schema migration added.
    # skipped_table is also True on this entry, so code that only checks skipped_table to decide
    # "not a real per-table count" (the existing no-op-second-run census) still skips it correctly.
    origen_tablas_nuevas: int | None = None


def _where(cols: tuple[str, ...], row: dict) -> tuple[str, list]:
    clauses, vals = [], []
    for c in cols:
        v = row.get(c)
        if v is None:
            clauses.append(f'"{c}" IS NULL')
        else:
            clauses.append(f'"{c}" = ?')
            vals.append(v)
    return " AND ".join(clauses), vals


def _match_cols_for(spec: TableSpec, row: dict) -> tuple[str, ...]:
    if spec.own_id_from:
        return ("id",)
    if spec.match_cols is not None:
        return spec.match_cols
    return tuple(c for c in row if c != "id" and c not in spec.self_ref_cols)


def _find_existing(dest_conn: sqlite3.Connection, spec: TableSpec, row: dict) -> sqlite3.Row | None:
    cols = _match_cols_for(spec, row)
    where, vals = _where(cols, row)
    return dest_conn.execute(f'SELECT * FROM "{spec.name}" WHERE {where}', vals).fetchone()


def _needs_update(spec: TableSpec, existing: sqlite3.Row, row: dict) -> bool:
    if spec.own_id_from or spec.match_cols is None:
        return False
    other_cols = [c for c in row if c not in spec.match_cols and c != "id" and c not in spec.self_ref_cols]
    return any(existing[c] != row.get(c) for c in other_cols)


def _do_insert(cur: sqlite3.Cursor, spec: TableSpec, row: dict) -> int | None:
    cols = [c for c in row if c != "id" or spec.own_id_from]
    placeholders = ", ".join("?" for _ in cols)
    names = ", ".join(f'"{c}"' for c in cols)
    cur.execute(f'INSERT INTO "{spec.name}" ({names}) VALUES ({placeholders})', [row[c] for c in cols])
    if spec.own_id_from:
        return row["id"]
    return cur.lastrowid if "id" in row else None


def _do_update(cur: sqlite3.Cursor, spec: TableSpec, existing: sqlite3.Row, row: dict) -> None:
    key_cols = spec.match_cols
    set_cols = [c for c in row if c not in key_cols and c != "id"]
    if not set_cols:
        return
    assignments = ", ".join(f'"{c}" = ?' for c in set_cols)
    where, key_vals = _where(key_cols, {c: existing[c] for c in key_cols})
    cur.execute(f'UPDATE "{spec.name}" SET {assignments} WHERE {where}',
                [row[c] for c in set_cols] + key_vals)


def _remap_row(row: sqlite3.Row, spec: TableSpec, id_maps: dict, source_person_id: int,
                dest_person_id: int) -> tuple[str, dict]:
    """Returns ("ok"|"pending"|"missing", row). "missing" means a parent this row depends on was
    never migrated (filtered as another person, or genuinely absent) -- caller counts it as
    other-person. "pending" means the parent WILL be migrated in this same run but, in a dry run,
    was not actually inserted, so this row cannot be matched against the destination yet and is
    simply counted as an insert."""
    out = dict(row)
    if spec.scope == "person" and "person_id" in out:
        out["person_id"] = dest_person_id
    for col in spec.self_ref_cols:
        out[col] = None
    pending = False
    for col in spec.fk_remap:
        orig = out.get(col)
        if orig is None:
            continue
        table = spec.fk_table[col]
        table_map = id_maps.get(table, {})
        if orig not in table_map:
            return "missing", out
        mapped = table_map[orig]
        if mapped is None:
            pending = True
            out[col] = None
        else:
            out[col] = mapped
    for col in spec.person_ref_cols:
        orig = out.get(col)
        if orig is None:
            continue
        if orig == source_person_id:
            out[col] = dest_person_id
        else:
            return "missing", out
    return ("pending" if pending else "ok"), out


def _source_rows(source_conn: sqlite3.Connection, spec: TableSpec, source_person_id: int,
                  parent_source_ids: dict) -> list[sqlite3.Row]:
    order = " ORDER BY id" if _has_id_column(source_conn, spec.name) else ""
    if spec.scope == "person":
        return source_conn.execute(
            f'SELECT * FROM "{spec.name}" WHERE person_id = ?{order}', (source_person_id,)
        ).fetchall()
    ids = parent_source_ids.get(spec.parent_table, set())
    if not ids:
        return []
    placeholders = ", ".join("?" * len(ids))
    return source_conn.execute(
        f'SELECT * FROM "{spec.name}" WHERE {spec.parent_fk} IN ({placeholders}){order}', tuple(ids)
    ).fetchall()


def _has_id_column(conn: sqlite3.Connection, table_name: str) -> bool:
    return any(r[1] == "id" for r in conn.execute(f'PRAGMA table_info("{table_name}")'))


def _dest_already_present(dest_conn: sqlite3.Connection, spec: TableSpec, dest_person_id: int,
                           dest_parent_ids: dict) -> int:
    if spec.scope == "person":
        return dest_conn.execute(
            f'SELECT COUNT(*) FROM "{spec.name}" WHERE person_id = ?', (dest_person_id,)
        ).fetchone()[0]
    ids = dest_parent_ids.get(spec.parent_table, set())
    if not ids:
        return 0
    placeholders = ", ".join("?" * len(ids))
    return dest_conn.execute(
        f'SELECT COUNT(*) FROM "{spec.name}" WHERE {spec.parent_fk} IN ({placeholders})', tuple(ids)
    ).fetchone()[0]


def _build_parent_ids(conn: sqlite3.Connection, person_id: int) -> dict:
    return {
        "goal": {r[0] for r in conn.execute("SELECT id FROM goal WHERE person_id = ?", (person_id,))},
        "shift_batch": {r[0] for r in conn.execute(
            "SELECT id FROM shift_batch WHERE person_id = ?", (person_id,))},
    }


def _resolve_self_refs(cur: sqlite3.Cursor, spec: TableSpec, rows: list[sqlite3.Row], own_map: dict,
                        apply: bool) -> int:
    """Second pass, same transaction as the rows just inserted/matched above: for each self-ref
    column, the OLD id it named is looked up in this table's OWN id map. Present (even if its value
    is still None, a dry run's to-be-inserted row) -> resolvable, written in apply mode. Absent --
    the referenced row was never migrated (another person's, or genuinely absent) -- counted, stays
    NULL, never silently."""
    unresolved = 0
    for row in rows:
        if row["id"] not in own_map:
            continue
        this_new_id = own_map[row["id"]]
        for col in spec.self_ref_cols:
            orig = row[col]
            if orig is None:
                continue
            if orig not in own_map:
                unresolved += 1
                continue
            if apply and this_new_id is not None:
                target_new_id = own_map[orig]
                cur.execute(f'UPDATE "{spec.name}" SET "{col}" = ? WHERE id = ?',
                            (target_new_id, this_new_id))
    return unresolved


def _process_table(source_conn: sqlite3.Connection, dest_conn: sqlite3.Connection, spec: TableSpec,
                    source_person_id: int, dest_person_id: int, id_maps: dict,
                    source_parent_ids: dict, dest_parent_ids: dict, apply: bool) -> TableReport:
    rows = _source_rows(source_conn, spec, source_person_id, source_parent_ids)
    counts = TableCounts(
        rows_source=len(rows),
        already_present=_dest_already_present(dest_conn, spec, dest_person_id, dest_parent_ids),
    )
    id_maps.setdefault(spec.name, {})
    cur = dest_conn.cursor()
    for row in rows:
        status, mapped = _remap_row(row, spec, id_maps, source_person_id, dest_person_id)
        if status == "missing":
            counts.other_person += 1
            continue
        if spec.other_person_filter and spec.other_person_filter(mapped):
            counts.other_person += 1
            continue
        existing = None if status == "pending" else _find_existing(dest_conn, spec, mapped)
        if existing is None:
            counts.insert += 1
            new_id = _do_insert(cur, spec, mapped) if apply else None
        elif _needs_update(spec, existing, mapped):
            counts.update += 1
            new_id = existing["id"] if "id" in existing.keys() else None
            if apply:
                _do_update(cur, spec, existing, mapped)
        else:
            counts.skip += 1
            new_id = existing["id"] if "id" in existing.keys() else None
        if spec.id_exported or spec.self_ref_cols:
            id_maps[spec.name][row["id"]] = new_id
    if spec.self_ref_cols:
        counts.self_ref_unresolved = _resolve_self_refs(cur, spec, rows, id_maps[spec.name], apply)
    return TableReport(spec.name, counts=counts)


def _run(source_conn: sqlite3.Connection, dest_conn: sqlite3.Connection, source_person_id: int,
          dest_person_id: int, apply: bool) -> list[TableReport]:
    reports: list[TableReport] = []
    id_maps: dict = {}
    source_parent_ids = _build_parent_ids(source_conn, source_person_id)
    dest_parent_ids = _build_parent_ids(dest_conn, dest_person_id)
    for spec in REGISTRY:
        if apply:
            dest_conn.execute("SAVEPOINT mudanza_tabla")
            try:
                report = _process_table(source_conn, dest_conn, spec, source_person_id,
                                         dest_person_id, id_maps, source_parent_ids,
                                         dest_parent_ids, apply=True)
                dest_conn.execute("RELEASE mudanza_tabla")
            except Exception:
                dest_conn.execute("ROLLBACK TO mudanza_tabla")
                dest_conn.execute("RELEASE mudanza_tabla")
                raise
        else:
            report = _process_table(source_conn, dest_conn, spec, source_person_id,
                                     dest_person_id, id_maps, source_parent_ids,
                                     dest_parent_ids, apply=False)
        reports.append(report)
    for name, reason in SKIPPED_TABLES.items():
        reports.append(TableReport(name, skipped_table=True, reason=reason))
    return reports


def resolve_source_person(source_conn: sqlite3.Connection) -> int:
    """The source PC install is single-person (BODY-MUDANZA-SERVIDOR-1-INV): its one person row,
    `db.default_person_id()`. Never `db.local_person_id` -- that function INSERTs when the row is
    missing, and the source connection is read-only."""
    expected_id = db.default_person_id()
    row = source_conn.execute("SELECT id FROM person WHERE id = ?", (expected_id,)).fetchone()
    if row is None:
        raise ValueError("source database has no person row at its single-install default id")
    return row["id"]


def resolve_dest_person(dest_conn: sqlite3.Connection, cuenta_sub: str) -> int:
    """Finds the destination person by `sub`, or creates it exactly as a first Cuenta sign-in
    would (ruling 1): `cuenta.resolve_person`, never a hand-written INSERT."""
    person_id = cuenta.resolve_person(dest_conn, {"sub": cuenta_sub, "duna_productos": ["body"]})
    if person_id is None:
        raise ValueError("cuenta.resolve_person refused to create a person for this sub")
    return person_id


def personas(conn: sqlite3.Connection) -> list[dict]:
    """Every hosted person, for Carlos to find the right `--cuenta-sub` before running `importar`
    (BODY-MUDANZA-HOST-VERBO-1, ruling 3). Only `cuenta_sub`, the display name Cuenta sign-in
    stored, and a creation date -- nothing else, and never a health figure. `person` carries no
    creation timestamp of its own (adding one is a schema change, outside this slice's `touches`),
    so `creado` is the person's earliest accepted consent instead -- consent is required before any
    other screen reaches them (body/privacidad.py), so it is the closest existing date to "this
    account started here". None when they have signed in but never consented yet."""
    rows = conn.execute(
        "SELECT p.cuenta_sub AS cuenta_sub, p.display_name AS display_name, "
        "(SELECT MIN(c.accepted_at) FROM consentimiento c WHERE c.person_id = p.id) AS creado "
        "FROM person p WHERE p.cuenta_sub IS NOT NULL ORDER BY p.id"
    ).fetchall()
    return [dict(r) for r in rows]


def _print_personas(rows: list[dict]) -> None:
    for row in rows:
        print(f"cuenta_sub={row['cuenta_sub']} display_name={row['display_name']} "
              f"creado={row['creado'] or '(sin consentir)'}")


def _print_report(reports: list[TableReport], apply: bool) -> None:
    label = "done" if apply else "would"
    for r in reports:
        if r.origen_tablas_nuevas is not None:
            print(f"origen: esquema actualizado en la copia ({r.origen_tablas_nuevas} tablas nuevas)")
            continue
        if r.skipped_table:
            print(f"{r.name}: skipped ({r.reason})")
            continue
        c = r.counts
        print(
            f"{r.name}: source={c.rows_source} already_present={c.already_present} "
            f"{label}_insert={c.insert} {label}_update={c.update} {label}_skip={c.skip} "
            f"other_person={c.other_person} self_ref_unresolved={c.self_ref_unresolved}"
        )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@contextlib.contextmanager
def _migrated_source(origen: Path):
    """Any person's PC can be behind the host's code, so `origen` is never read as-is (BODY-
    MUDANZA-ORIGEN-ANTIGUO-1, ruling 1). It is copied -- opened read-only, the same way
    `body.backup snapshot` copies its own source, via sqlite's backup API -- into a temporary
    file beside the destination database (same filesystem, same owner as the data user, mode
    0600, best effort), then `db.init` runs on that COPY to bring it to today's schema: never a
    hand-written CREATE that could drift from the one real migration path. Yields (a read-only
    connection on the migrated copy, the count of tables the migration added). `origen` itself is
    never opened for writing, and the copy is deleted in this generator's own `finally` on every
    exit -- a dry run, an apply, or an exception raised while the caller reads the migrated
    rows."""
    config.ensure_home()
    fd, tmp_name = tempfile.mkstemp(dir=config.DB_PATH.parent, prefix=".mudanza-origen-",
                                     suffix=".sqlite")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with contextlib.suppress(OSError):
            tmp_path.chmod(0o600)
        src = backup.open_readonly(origen)
        try:
            copy_conn = sqlite3.connect(tmp_path)
            try:
                src.backup(copy_conn)
                before = _table_names(copy_conn)
                db.init(copy_conn)
                tablas_nuevas = len(_table_names(copy_conn) - before)
            finally:
                copy_conn.close()
        finally:
            src.close()
        migrated = backup.open_readonly(tmp_path)
        try:
            yield migrated, tablas_nuevas
        finally:
            migrated.close()
    finally:
        tmp_path.unlink(missing_ok=True)


def importar(origen: Path, cuenta_sub: str, apply: bool) -> tuple[list[TableReport], bool]:
    """Runs the import, dry run by default. Returns (reports, integrity_ok). `integrity_ok` is
    always True for a dry run (nothing was written). `reports[0]` is always the synthetic
    "origen" entry `_migrated_source` reports (ruling 2)."""
    integrity_ok = True
    with _migrated_source(origen) as (source_conn, tablas_nuevas):
        dest_conn = db.connect()
        try:
            db.ensure_ready(dest_conn)
            source_person_id = resolve_source_person(source_conn)
            dest_person_id = resolve_dest_person(dest_conn, cuenta_sub)
            reports = _run(source_conn, dest_conn, source_person_id, dest_person_id, apply=apply)
            if apply:
                fk_problems = dest_conn.execute("PRAGMA foreign_key_check").fetchall()
                integrity = dest_conn.execute("PRAGMA integrity_check").fetchone()[0]
                integrity_ok = integrity == "ok" and not fk_problems
        finally:
            dest_conn.close()
    reports.insert(0, TableReport("origen", skipped_table=True, origen_tablas_nuevas=tablas_nuevas))
    return reports, integrity_ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.mudanza")
    sub = p.add_subparsers(dest="action", required=True)
    imp = sub.add_parser("importar", help="move one person's data from a PC database here")
    imp.add_argument("--origen", required=True, help="path to the PC's body.db (opened read-only)")
    imp.add_argument("--cuenta-sub", required=True, help="the destination person's Cuenta sub")
    imp.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    sub.add_parser("personas", help="list hosted persons by cuenta_sub, display name and first consent")
    a = p.parse_args(argv)

    if a.action == "personas":
        conn = db.connect()
        try:
            db.ensure_ready(conn)
            _print_personas(personas(conn))
        finally:
            conn.close()
        return 0

    try:
        reports, integrity_ok = importar(Path(a.origen), a.cuenta_sub, apply=a.apply)
    except ValueError as exc:
        print(f"refused: {errores.texto_seguro(exc)}")
        return 1

    _print_report(reports, apply=a.apply)
    if a.apply:
        print(f"integrity_check: {'ok' if integrity_ok else 'FAILED'}")
        if not integrity_ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
