"""Consent, export and delete-all (host-deploy-v1-amendment-sensitive-data rule 6, Ley 1581 dato
sensible; BODY-HOSTING-PLAN-1 step 6). The gate itself lives in body/identity.py, which calls
`has_consented` below; body/web.py wires the three routes to the functions here, the same way it
already wires body/cuenta.py's functions.

Local mode (one person on their own PC) never gates on consent -- body/identity.py only checks it
in hosted mode -- but export and delete-all work the same in both modes.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from . import compartir, db, enlaces
from .agente import ajustes as agente_ajustes

CONSENT_VERSION = "consentimiento-v3"

# Every table with a literal `person_id` column, and how this module accounts for its rows.
# Checked by tests/test_privacidad.py's coverage test: a new person-keyed table that is added to
# neither set fails that test, so it can never slip past export or delete-all unnoticed.
FULLY_HANDLED_TABLES = {
    "daily_metric", "sleep_session", "activity", "shift", "shift_batch", "shift_pattern",
    "goal", "habit_check", "goal_check", "plan", "ajuste_agente", "consentimiento", "hogar_miembro",
    "conversacion_agente", "mensaje_agente", "uso_agente", "tope_agente", "propuesta_agente",
    "figura_compartida", "workout_set",
    # Wave 9 (BODY-EXPERIMENTOS-ESQUEMA-1): experimento, its "it happened" log and moment.
    "experimento", "experimento_registro", "moment",
    # BODY-PROTEINA-1: one row per thing eaten or drunk -- the person's own words, like moment.note.
    "ingesta",
    # BODY-PERIODO-1: one row per marked stretch of days -- the note is the person's own words,
    # like moment.note and ingesta.descripcion.
    "period",
    # BODY-RECUPERACION-1: one row per (activity, model version) -- a figure derived from the
    # watch's own minute-by-minute reading, never typed by the person.
    "recuperacion_actividad",
    # BODY-TURNO-FC-1: one row per (shift day, model version) -- a figure derived from the watch's
    # own minute-by-minute reading, never typed by the person. Not exported to body/vida_export.py
    # (ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 7) -- only to this module's own full personal export.
    "turno_fc",
}
# raw_payload: kind/date only, never the stored JSON payload. garmin_cuenta: the connected-since
# date only, never the encrypted token (BODY-GARMIN-HOST-1). dispositivo: name/scopes/dates only,
# never token_hash (BODY-DISPOSITIVO-1).
#
# habit and goal_enlace carry no literal `person_id` column (person_tables() below would never find
# them) -- both are owned through goal_id instead, and both are still fully exported and deleted,
# via a join on goal.person_id (habit above, goal_enlace through body/enlaces.py).
PARTIAL_EXPORT_TABLES = {"raw_payload", "garmin_cuenta", "dispositivo"}
NEVER_EXPORTED_TABLES = {"sesion"}  # session plumbing, not data about the person's body or life


def person_tables(conn: sqlite3.Connection) -> set[str]:
    """Every table in this database with a column literally named `person_id`."""
    names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    out = set()
    for name in names:
        cols = {c[1] for c in conn.execute(f"PRAGMA table_info({name})")}
        if "person_id" in cols:
            out.add(name)
    return out


# ---- consent -------------------------------------------------------------------------------------

def has_consented(conn: sqlite3.Connection, person_id: int, version: str = CONSENT_VERSION) -> bool:
    return conn.execute(
        "SELECT 1 FROM consentimiento WHERE person_id = ? AND version = ?", (person_id, version)
    ).fetchone() is not None


def record_consent(conn: sqlite3.Connection, person_id: int, version: str = CONSENT_VERSION) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO consentimiento (person_id, version, accepted_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id, version) DO NOTHING",
            (person_id, version, now),
        )


# ---- export ---------------------------------------------------------------------------------------

def export_person(conn: sqlite3.Connection, person_id: int) -> dict:
    """Everything this person's own export holds: `body-<date>.json` in body/web.py's download.
    Reads only rows this person owns -- a household member's own checks on a shared goal are theirs
    to export from their own account, not this one."""
    person = dict(conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone())

    vida = {
        "goals": [dict(r) for r in conn.execute(
            "SELECT * FROM goal WHERE person_id = ? ORDER BY id", (person_id,))],
        "habits": [dict(r) for r in conn.execute(
            "SELECT h.* FROM habit h JOIN goal g ON g.id = h.goal_id WHERE g.person_id = ? ORDER BY h.id",
            (person_id,))],
        "habit_checks": [dict(r) for r in conn.execute(
            "SELECT * FROM habit_check WHERE person_id = ? ORDER BY day", (person_id,))],
        "goal_checks": [dict(r) for r in conn.execute(
            "SELECT * FROM goal_check WHERE person_id = ? ORDER BY week_start", (person_id,))],
        "plans": [dict(r) for r in conn.execute(
            "SELECT * FROM plan WHERE person_id = ? ORDER BY id", (person_id,))],
        "shifts": [dict(r) for r in conn.execute(
            "SELECT * FROM shift WHERE person_id = ? ORDER BY day", (person_id,))],
        "shift_batches": [dict(r) for r in conn.execute(
            "SELECT * FROM shift_batch WHERE person_id = ? ORDER BY received", (person_id,))],
        "shift_pattern": (lambda r: dict(r) if r else None)(conn.execute(
            "SELECT * FROM shift_pattern WHERE person_id = ?", (person_id,)).fetchone()),
        # goal_enlace carries no person_id of its own (like habit above) -- enlaces.export_rows
        # joins through goal_id, the only module that reads this table.
        "enlaces": enlaces.export_rows(conn, person_id),
    }
    experimentos = {
        "experimentos": [dict(r) for r in conn.execute(
            "SELECT * FROM experimento WHERE person_id = ? ORDER BY id", (person_id,))],
        "registros": [dict(r) for r in conn.execute(
            "SELECT * FROM experimento_registro WHERE person_id = ? ORDER BY day", (person_id,))],
        "momentos": [dict(r) for r in conn.execute(
            "SELECT * FROM moment WHERE person_id = ? ORDER BY ts_local", (person_id,))],
    }
    ingesta = [dict(r) for r in conn.execute(
        "SELECT id, day, ts_local, metric_key, amount, estimado, descripcion, created_at, updated_at "
        "FROM ingesta WHERE person_id = ? ORDER BY day, id", (person_id,))]
    periodos = [dict(r) for r in conn.execute(
        "SELECT id, kind, start_day, end_day, note, created_at, updated_at "
        "FROM period WHERE person_id = ? ORDER BY start_day, id", (person_id,))]
    dias = [dict(r) for r in conn.execute(
        "SELECT day, metric_key, value, source FROM daily_metric WHERE person_id = ? ORDER BY day",
        (person_id,))]
    actividades = [dict(r) for r in conn.execute(
        "SELECT * FROM activity WHERE person_id = ? ORDER BY day", (person_id,))]
    sets_entreno = [dict(r) for r in conn.execute(
        "SELECT * FROM workout_set WHERE person_id = ? ORDER BY activity_source_id, set_order",
        (person_id,))]
    sueno = [dict(r) for r in conn.execute(
        "SELECT * FROM sleep_session WHERE person_id = ? ORDER BY day", (person_id,))]
    recuperacion_actividades = [dict(r) for r in conn.execute(
        "SELECT activity_source, activity_source_id, modelo_version, minutos, motivo, computed_at "
        "FROM recuperacion_actividad WHERE person_id = ? ORDER BY computed_at", (person_id,))]
    turno_fc_filas = [dict(r) for r in conn.execute(
        "SELECT day, kind, modelo_version, fc_mediana, estres_mediana, bajo_usual_pct, referencia_fc, "
        "cobertura, motivo, computed_at FROM turno_fc WHERE person_id = ? ORDER BY day", (person_id,))]
    # raw_payload is a partial-export table: its kind and date only, never the stored JSON blob.
    sincronizaciones = [dict(r) for r in conn.execute(
        "SELECT day, kind, source, fetched_at FROM raw_payload WHERE person_id = ? ORDER BY day",
        (person_id,))]
    consentimientos = [dict(r) for r in conn.execute(
        "SELECT version, accepted_at FROM consentimiento WHERE person_id = ? ORDER BY accepted_at",
        (person_id,))]
    hogar_row = conn.execute(
        "SELECT hogar_id, member_sub, fuente FROM hogar_miembro WHERE person_id = ?", (person_id,)
    ).fetchone()
    figuras_compartidas = compartir.export_rows(conn, person_id)
    ajustes = {"nivel_datos": agente_ajustes.nivel_datos(conn, person_id)}
    # garmin_cuenta is a partial-export table: the connected-since date only, never the encrypted
    # token or anything that could rebuild it.
    garmin_row = conn.execute(
        "SELECT conectado_en FROM garmin_cuenta WHERE person_id = ?", (person_id,)
    ).fetchone()
    garmin = {"conectado_desde": garmin_row["conectado_en"]} if garmin_row else None
    # dispositivo is a partial-export table: name, scopes and dates only, never token_hash.
    dispositivos = [dict(r) for r in conn.execute(
        "SELECT nombre, alcances, creado_en, usado_en FROM dispositivo WHERE person_id = ? ORDER BY creado_en",
        (person_id,))]
    conversaciones = [dict(r) for r in conn.execute(
        "SELECT id, creada_en, titulo FROM conversacion_agente WHERE person_id = ? ORDER BY id",
        (person_id,))]
    mensajes_agente = [dict(r) for r in conn.execute(
        "SELECT id, conversacion_id, rol, contenido_json, creado_en FROM mensaje_agente "
        "WHERE person_id = ? ORDER BY id", (person_id,))]
    uso_agente_rows = [dict(r) for r in conn.execute(
        "SELECT creado_en, proveedor, modelo, entrada, salida, cache_lectura, cache_escritura, rondas, "
        "motivo_fin, codigo_error, es_pregunta, superficie FROM uso_agente WHERE person_id = ? ORDER BY id",
        (person_id,))]
    tope_agente_row = conn.execute(
        "SELECT diario_micro_usd, conteo_diario FROM tope_agente WHERE person_id = ?", (person_id,)
    ).fetchone()
    propuestas_agente = [dict(r) for r in conn.execute(
        "SELECT id, conversacion_id, accion, objetivo_id, antes_json, despues_json, estado, creada_en, "
        "expira_en, resuelta_en FROM propuesta_agente WHERE person_id = ? ORDER BY id", (person_id,))]
    asistente = {
        "conversaciones": conversaciones, "mensajes": mensajes_agente, "uso": uso_agente_rows,
        "tope": dict(tope_agente_row) if tope_agente_row else None, "propuestas": propuestas_agente,
    }

    return {
        "formato": "body-export-v1",
        "exportado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": {"display_name": person["display_name"], "timezone": person["timezone"],
                    "language": person["language"], "trabaja_turnos": person["trabaja_turnos"]},
        "hogar": dict(hogar_row) if hogar_row else None,
        "figuras_compartidas": figuras_compartidas,
        "vida": vida,
        "experimentos": experimentos,
        "ingesta": ingesta,
        "periodos": periodos,
        "dias": dias,
        "actividades": actividades,
        "sets_entreno": sets_entreno,
        "sueno": sueno,
        "recuperacion_actividades": recuperacion_actividades,
        "turno_fc": turno_fc_filas,
        "sincronizaciones": sincronizaciones,
        "consentimientos": consentimientos,
        "ajustes": ajustes,
        "garmin": garmin,
        "dispositivos": dispositivos,
        "asistente": asistente,
    }


# ---- delete-all -----------------------------------------------------------------------------------

def delete_person(conn: sqlite3.Connection, person_id: int) -> None:
    """Deletes every row this person owns, and every row that exists only because of a goal they
    created -- a shared household goal they made disappears for every member too, as the privacy
    page says. One transaction, foreign keys ON (set by db.connect already): on any error nothing
    is left half-deleted."""
    with conn:
        # experimento.habito_id references habit(id) -- removed before habit itself goes, same as
        # every other table below that references a row this sweep is about to remove.
        conn.execute("DELETE FROM experimento_registro WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM moment WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM experimento WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM ingesta WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM period WHERE person_id = ?", (person_id,))
        db.invalidar_periodos_cache(conn, person_id)
        conn.execute("UPDATE habit SET assignee_person_id = NULL WHERE assignee_person_id = ?", (person_id,))
        enlaces.delete_rows_for_person(conn, person_id)
        conn.execute(
            "DELETE FROM habit_check WHERE person_id = ? OR habit_id IN ("
            "SELECT id FROM habit WHERE goal_id IN (SELECT id FROM goal WHERE person_id = ?))",
            (person_id, person_id),
        )
        conn.execute(
            "DELETE FROM goal_check WHERE person_id = ? OR goal_id IN ("
            "SELECT id FROM goal WHERE person_id = ?)",
            (person_id, person_id),
        )
        conn.execute(
            "DELETE FROM habit WHERE goal_id IN (SELECT id FROM goal WHERE person_id = ?)", (person_id,)
        )
        conn.execute(
            "DELETE FROM plan WHERE person_id = ? OR goal_id IN (SELECT id FROM goal WHERE person_id = ?)",
            (person_id, person_id),
        )
        # propuesta_agente.objetivo_id references goal(id) -- cleared before goal itself goes, same
        # as habit/habit_check/goal_check/plan above.
        conn.execute(
            "DELETE FROM propuesta_agente WHERE person_id = ? OR objetivo_id IN "
            "(SELECT id FROM goal WHERE person_id = ?)", (person_id, person_id),
        )
        conn.execute("DELETE FROM goal WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM daily_metric WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM sleep_session WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM raw_payload WHERE person_id = ?", (person_id,))
        # workout_set's FK targets activity's composite key -- deleted first, same as every other
        # table that references a row this sweep is about to remove.
        conn.execute("DELETE FROM workout_set WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM recuperacion_actividad WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM turno_fc WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM activity WHERE person_id = ?", (person_id,))
        conn.execute(
            "DELETE FROM shift_batch_item WHERE batch_id IN (SELECT id FROM shift_batch WHERE person_id = ?)",
            (person_id,),
        )
        conn.execute("DELETE FROM shift_batch WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM shift WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM shift_pattern WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM ajuste_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM consentimiento WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM garmin_cuenta WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM dispositivo WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM mensaje_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM conversacion_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM uso_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM tope_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM hogar_miembro WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM sesion WHERE person_id = ?", (person_id,))
        compartir.delete_rows(conn, person_id)
        conn.execute("DELETE FROM person WHERE id = ?", (person_id,))
    _borrar_fuera_de_bd(person_id)


def _borrar_fuera_de_bd(person_id: int) -> None:
    """Placeholder call point for anything this person's delete-all must also remove OUTSIDE the
    database. Empty, and stays empty: BODY-GARMIN-HOST-1's Garmin token lives in `garmin_cuenta`,
    a person-keyed table the generic deletes above already remove -- there is nothing left outside
    the database. Takes only an id, never a figure, like every function in this module."""
    return None
