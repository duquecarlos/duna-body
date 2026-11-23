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

from . import compartir, cuenta, db, enlaces, fuentes
from .agente import ajustes as agente_ajustes

CONSENT_VERSION = "consentimiento-v4"

# Every table with a literal `person_id` column, and how this module accounts for its rows.
# Checked by tests/test_privacidad.py's coverage test: a new person-keyed table that is added to
# neither set fails that test, so it can never slip past export or delete-all unnoticed.
FULLY_HANDLED_TABLES = {
    "daily_metric", "sleep_session", "activity", "shift", "shift_batch", "shift_pattern",
    "goal", "habit_check", "goal_check", "plan", "ajuste_agente",
    # BODY-HOGAR-ESCRIBE-1-W ruling 2/6: a person's own meal-moment windows -- settings, no free
    # text, registered the same way as ajuste_agente right above it.
    "momento_comida",
    "consentimiento", "hogar_miembro",
    "conversacion_agente", "mensaje_agente", "uso_agente", "tope_agente", "propuesta_agente",
    # BODY-MCP-LOCAL-1: one row per call the local MCP server made against the tool registry --
    # tool name, UTC time, caller id only.
    "uso_herramienta_externa",
    # BODY-HOGAR-REGISTRAR-IDEMPOTENTE-1: one row per outside writer's own idempotency key -- a
    # tool name and an opaque ref the writer chose, never a health figure.
    "escritura_externa",
    "figura_compartida", "workout_set",
    # Wave 9 (BODY-EXPERIMENTOS-ESQUEMA-1): experimento, its "it happened" log and moment.
    "experimento", "experimento_registro", "moment",
    # BODY-PROTEINA-1: one row per thing eaten or drunk -- the person's own words, like moment.note.
    # BODY-COMIDA-1: comida groups a meal's own ingesta rows under one description/time -- the
    # same person's own words, exported and deleted alongside ingesta.
    "ingesta", "comida",
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
    # BODY-CHECKIN-1: the three 1-5 readings only, never the note -- its own sibling table
    # checkin_nota is in NEVER_EXPORTED_TABLES below (ruling 4, the strictest privacy level).
    "checkin",
    # BODY-NUTRIENTE-OBJETIVO-1: one row per (metric, start_day) -- the person's own chosen target,
    # no free text.
    "nutrient_target",
    # ORCH-CARLOS-DELEGATES-TIER-1-1: one row per import source -- dates, counts and a reason CLASS
    # only (never a figure), so the whole row is exported and deleted like any other.
    "fuente_importacion",
    # BODY-COMIDA-COMPARTIDA-1 ruling 1: whether this person receives a household member's shared
    # meal -- a feature flag, the same per-person treatment figura_compartida already gets.
    "consentimiento_comida_compartida",
    # BODY-COMIDA-COMPARTIDA-1 ruling 2: one row per EATER of a shared meal -- this eater's own
    # words/portion/nutrients, exported and deleted like any other of this person's own rows.
    # comida_compartida itself (the parent link) carries autor_person_id, not a literal person_id,
    # so person_tables() never finds it -- handled by hand in export_person/delete_person below,
    # the same way habit/goal_enlace are (comment further down).
    "comida_compartida_porcion",
    # BODY-TENDENCIAS-RACHA-GRAFICO-1 ruling 1: the person's own pick of which habit the strength-
    # session study compares against -- one row, a habit id and nothing else.
    "racha_piso",
    # BODY-PLAN-ESQUEMA-1: the plan's own parts, each a child row under a horizon goal -- the
    # person's own wording (horizonte_regla.texto), weekly targets and checkpoints.
    "horizonte_regla", "horizonte_semana", "horizonte_meta_semanal", "horizonte_checkpoint",
    # BODY-PLAN-FASES-2 ruling 1: a plan's own phases -- the person's own titulo/nota, like
    # horizonte_regla.texto above.
    "horizonte_fase",
    # BODY-ENTRENO-DETALLE-1 ruling 1: one row per (activity, kind) -- the watch's own
    # heart-rate-curve/laps/zones detail for one of this person's own activities, never typed by
    # them. Unlike raw_payload below (a PARTIAL_EXPORT_TABLE, kind/date only), the full payload
    # is exported here -- the same per-activity detail Carlos asked this slice to keep
    # ("yes, everything, man.", 2026-11-15), about his own body, from his own account.
    "activity_detail",
    # BODY-HISTORIA-VIDA-1 ruling 8: a practice is what the watch data shows the person did
    # (body/practicas.py) -- the person's own title and rule, exported and deleted like any other
    # of their own rows.
    "practica",
    # BODY-DESPERTAR-REAL-4 ruling 1: one row per (day, correction) -- the person's own tap,
    # exported and deleted like any other of their own rows, Garmin's own sleep_session row
    # untouched either way.
    "despertar_correccion",
    # BODY-HISTORIA-VIDA-LUGARES-1 ruling 4: this person's own trips, re-fetched from Lugares --
    # nothing Lugares did not send, no names, no coordinates.
    "viaje_lugares",
    # BODY-MINUTO-ANALISIS-6-ALTITUD (ORCH-CARLOS-DELEGATES-TIER-1-1): one row per home/stay
    # stretch -- an altitude and dates only, no place name, no coordinates.
    "altitud_estancia",
    # BODY-PARTIDO-2 (ORCH-CARLOS-DELEGATES-TIER-1-1): the person's own post-session check-in --
    # set_pregunta (which set they have, by tipo/nombre_norm) and set_pregunta_respuesta (their own
    # answers, by stable option key, never display text). set_pregunta_item carries no literal
    # person_id (reached through set_pregunta_id, like habit/goal_enlace above) and is exported/
    # deleted by hand, below.
    "set_pregunta", "set_pregunta_respuesta",
}
# raw_payload: kind/date only, never the stored JSON payload. garmin_cuenta: the connected-since
# date only, never the encrypted token (BODY-GARMIN-HOST-1). dispositivo: name/scopes/dates only,
# never token_hash (BODY-DISPOSITIVO-1).
#
# habit and goal_enlace carry no literal `person_id` column (person_tables() below would never find
# them) -- both are owned through goal_id instead, and both are still fully exported and deleted,
# via a join on goal.person_id (habit above, goal_enlace through body/enlaces.py).
# BODY-PLAN-FASES-5: entreno_dolor is a partial-export table -- every column except `movimiento`
# (the person's own words about what hurt, never logged or exported off the host, same promise
# checkin_nota's own note keeps at the strictest level, though this one still fully exports its
# NUMBERS).
PARTIAL_EXPORT_TABLES = {"raw_payload", "garmin_cuenta", "dispositivo", "entreno_dolor"}
# BODY-CHECKIN-1 ruling 4: checkin_nota is the strictest privacy level -- never in this module's own
# export, never in body/vida_export.py, never shared with a household. sesion is session plumbing,
# not data about the person's body or life. lugares_sync is this module's own sync bookkeeping
# (attempt/success timestamps and an error CLASS, BODY-HISTORIA-VIDA-LUGARES-1 ruling 4) -- same
# treatment as sesion, deleted below but never exported.
NEVER_EXPORTED_TABLES = {"sesion", "checkin_nota", "lugares_sync"}


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


def acepto_version_anterior(conn: sqlite3.Connection, person_id: int) -> bool:
    """True when this person has an accepted `consentimiento` row of some version OTHER than the
    current one -- the consent page's own "what changed" notice (BODY-CONSENTIMIENTO-V4 ruling 3)
    shows it only then, never to a first-time person. It never gates by itself; has_consented,
    above, still decides whether the person may pass."""
    return conn.execute(
        "SELECT 1 FROM consentimiento WHERE person_id = ? AND version != ?", (person_id, CONSENT_VERSION)
    ).fetchone() is not None


def guarda_detalle(conn: sqlite3.Connection, person_id: int) -> bool:
    """The one question every per-activity-detail fetcher (full heart-rate curves, laps, zones,
    strength sets, and minute-by-minute nights and days) must ask before it stores anything: True
    in local mode (one person on their own PC -- the same exemption the hosted consent gate
    already carries), else whether this person has accepted EXACTLY "consentimiento-v4". Pinned to
    that literal, never to CONSENT_VERSION, so a later v5 widening never silently changes what v4
    already granted. Nothing calls this yet -- BODY-ENTRENO-DETALLE-1 will."""
    if not cuenta.hosted():
        return True
    return has_consented(conn, person_id, "consentimiento-v4")


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
        # BODY-TENDENCIAS-RACHA-GRAFICO-1 ruling 1: the person's own chosen floor habit, if any.
        "racha_piso": (lambda r: dict(r) if r else None)(conn.execute(
            "SELECT habit_id, updated_at FROM racha_piso WHERE person_id = ?", (person_id,)).fetchone()),
    }
    experimentos = {
        "experimentos": [dict(r) for r in conn.execute(
            "SELECT * FROM experimento WHERE person_id = ? ORDER BY id", (person_id,))],
        "registros": [dict(r) for r in conn.execute(
            "SELECT * FROM experimento_registro WHERE person_id = ? ORDER BY day", (person_id,))],
        "momentos": [dict(r) for r in conn.execute(
            "SELECT * FROM moment WHERE person_id = ? ORDER BY ts_local", (person_id,))],
    }
    # BODY-PLAN-ESQUEMA-1: the plan's own parts this person wrote -- scoped by this module's own
    # person_id column on each of the four tables, not by the horizon goal's ownership (a household
    # member's own rows on a shared horizon goal are theirs to export from their own account).
    horizonte = {
        "reglas": [dict(r) for r in conn.execute(
            "SELECT * FROM horizonte_regla WHERE person_id = ? ORDER BY horizonte_goal_id, orden, id",
            (person_id,))],
        "semanas": [dict(r) for r in conn.execute(
            "SELECT * FROM horizonte_semana WHERE person_id = ? ORDER BY horizonte_goal_id, week_start",
            (person_id,))],
        "metas_semanales": [dict(r) for r in conn.execute(
            "SELECT * FROM horizonte_meta_semanal WHERE person_id = ? ORDER BY horizonte_goal_id, week_start, id",
            (person_id,))],
        "checkpoints": [dict(r) for r in conn.execute(
            "SELECT * FROM horizonte_checkpoint WHERE person_id = ? ORDER BY horizonte_goal_id, day",
            (person_id,))],
        # BODY-PLAN-FASES-2 ruling 1: scoped the same way as the three siblings above -- this
        # module's own person_id column on the phase row, not the horizon goal's ownership.
        "fases": [dict(r) for r in conn.execute(
            "SELECT * FROM horizonte_fase WHERE person_id = ? ORDER BY horizonte_goal_id, orden, id",
            (person_id,))],
    }
    ingesta = [dict(r) for r in conn.execute(
        "SELECT id, day, ts_local, metric_key, amount, estimado, descripcion, comida_id, created_at, "
        "updated_at FROM ingesta WHERE person_id = ? ORDER BY day, id", (person_id,))]
    comidas = [dict(r) for r in conn.execute(
        "SELECT id, day, ts_local, descripcion, momento, created_at, updated_at "
        "FROM comida WHERE person_id = ? ORDER BY day, id", (person_id,))]
    # BODY-HOGAR-ESCRIBE-1-W ruling 2/6: this person's own meal-moment windows, registered like
    # ajuste_agente (the `ajustes` dict below) -- settings, no free text.
    momentos_comida = [dict(r) for r in conn.execute(
        "SELECT clave, desde_hora, hasta_hora, esperado, orden FROM momento_comida "
        "WHERE person_id = ? ORDER BY orden", (person_id,))]
    periodos = [dict(r) for r in conn.execute(
        "SELECT id, kind, start_day, end_day, note, zonas, created_at, updated_at "
        "FROM period WHERE person_id = ? ORDER BY start_day, id", (person_id,))]
    # BODY-PLAN-FASES-5: every number and the exercise/zona naming it, never `movimiento` (the
    # person's own words about what hurt) -- the one restricted field this table's partial export
    # leaves out.
    dolor_entreno = [dict(r) for r in conn.execute(
        "SELECT id, day, sets_source, sets_source_id, ejercicio, zona, dolor_durante, dolor_despues, "
        "dolor_manana, manana_registrado_en, creado_en FROM entreno_dolor WHERE person_id = ? "
        "ORDER BY day, id", (person_id,))]
    objetivos_nutriente = [dict(r) for r in conn.execute(
        "SELECT metric_key, start_day, kind, amount, sentido, created_at FROM nutrient_target "
        "WHERE person_id = ? ORDER BY metric_key, start_day", (person_id,))]
    # BODY-PARTIDO-2: this person's own check-in sets and answers -- set_pregunta_item carries no
    # literal person_id (like habit above), joined through set_pregunta_id instead.
    checkin_sesion_sets = [dict(r) for r in conn.execute(
        "SELECT id, tipo, nombre_norm, creado_en FROM set_pregunta WHERE person_id = ? ORDER BY id",
        (person_id,))]
    checkin_sesion_items = [dict(r) for r in conn.execute(
        "SELECT si.set_pregunta_id, si.orden, si.clave, si.tipo_pregunta, si.texto, si.opciones_json, "
        "si.origen FROM set_pregunta_item si JOIN set_pregunta sp ON sp.id = si.set_pregunta_id "
        "WHERE sp.person_id = ? ORDER BY si.set_pregunta_id, si.orden", (person_id,))]
    checkin_sesion_respuestas = [dict(r) for r in conn.execute(
        "SELECT sets_source, sets_source_id, pregunta_clave, valor, creado_en, actualizado_en "
        "FROM set_pregunta_respuesta WHERE person_id = ? ORDER BY sets_source_id, pregunta_clave",
        (person_id,))]
    fuentes_importacion = [dict(r) for r in conn.execute(
        "SELECT fuente, primer_dia, ultimo_dia, computed_at, dias, importado_at, ultimo_error, "
        "ultimo_error_at FROM fuente_importacion WHERE person_id = ? ORDER BY fuente", (person_id,))]
    # BODY-CHECKIN-1 ruling 4: never the note (checkin_nota) -- not even here, this person's own
    # full export.
    checkins = [dict(r) for r in conn.execute(
        "SELECT id, day, ts_local, momento, mood, energy, stress_felt, created_at "
        "FROM checkin WHERE person_id = ? ORDER BY day, id", (person_id,))]
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
    # BODY-DESPERTAR-REAL-4 ruling 1: this person's own correction for each day, beside Garmin's
    # own sleep above -- never merged into it.
    despertar_correcciones = [dict(r) for r in conn.execute(
        "SELECT day, tipo, hora_local, modelo_version, origen, creada_en FROM despertar_correccion "
        "WHERE person_id = ? ORDER BY day", (person_id,))]
    detalle_actividades = [dict(r) for r in conn.execute(
        "SELECT activity_source, activity_source_id, kind, fetched_at, payload FROM activity_detail "
        "WHERE person_id = ? ORDER BY activity_source, activity_source_id, kind", (person_id,))]
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
    consentimiento_comidas_compartidas = compartir.export_rows_consentimiento_comidas(conn, person_id)
    # BODY-COMIDA-COMPARTIDA-1 ruling 6: this person's own meals shared with others -- authored by
    # them (comida_compartida has no literal person_id, only autor_person_id) -- and every portion
    # addressed to THEM, in any meal, including one someone else authored.
    comidas_compartidas_mias = [dict(r) for r in conn.execute(
        "SELECT id, hogar_id, day, ts_local, origen, origen_ref, created_at FROM comida_compartida "
        "WHERE autor_person_id = ? ORDER BY id", (person_id,))]
    comidas_compartidas_porciones = [dict(r) for r in conn.execute(
        "SELECT id, comida_compartida_id, descripcion, porcion_texto, nutrientes, estado, comida_id, "
        "decided_at FROM comida_compartida_porcion WHERE person_id = ? ORDER BY id", (person_id,))]
    # aplicar_mcp (BODY-MCP-APLICAR-1, ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 7): the person's own
    # setting that lets their personal assistant apply a change it proposed, in their own export.
    ajustes = {"nivel_datos": agente_ajustes.nivel_datos(conn, person_id),
               "aplicar_mcp": db.get_aplicar_mcp(conn, person_id)}
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
        "SELECT id, creada_en, titulo, origen FROM conversacion_agente WHERE person_id = ? ORDER BY id",
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
        "expira_en, resuelta_en, aplicada_via FROM propuesta_agente WHERE person_id = ? ORDER BY id",
        (person_id,))]
    # BODY-MCP-LOCAL-1: every call the local MCP server made against the tool registry -- the tool
    # name, the UTC time, the caller id, never a param or a result.
    uso_externo_rows = [dict(r) for r in conn.execute(
        "SELECT herramienta, llamada_en, caller FROM uso_herramienta_externa WHERE person_id = ? ORDER BY id",
        (person_id,))]
    # BODY-HOGAR-REGISTRAR-IDEMPOTENTE-1: one row per outside writer's own idempotency key -- the
    # tool name and the writer's own opaque ref, never a health figure.
    escrituras_externas_rows = [dict(r) for r in conn.execute(
        "SELECT origen, ref, herramienta, propuesta_id, creada_en FROM escritura_externa "
        "WHERE person_id = ? ORDER BY creada_en", (person_id,))]
    # BODY-HISTORIA-VIDA-1 ruling 8: this person's own practices (body/practicas.py) -- their own
    # title and rule, never the derived math (computed on read, never stored).
    practicas_rows = [dict(r) for r in conn.execute(
        "SELECT id, titulo, fuente, regla_json, meta_dias_semana, origen, creada_en, archivada_en "
        "FROM practica WHERE person_id = ? ORDER BY id", (person_id,))]
    # BODY-HISTORIA-VIDA-LUGARES-1 ruling 4: this person's own trips, re-fetched from Lugares --
    # nothing Lugares did not send, no names, no coordinates.
    viajes_lugares_rows = [dict(r) for r in conn.execute(
        "SELECT ref, starts_at, ends_at, fetched_at FROM viaje_lugares WHERE person_id = ? ORDER BY starts_at",
        (person_id,))]
    # BODY-MINUTO-ANALISIS-6-ALTITUD (ORCH-CARLOS-DELEGATES-TIER-1-1): this person's own home and
    # stay rows -- an altitude and dates only, no place name, no coordinates.
    altitudes_rows = [dict(r) for r in conn.execute(
        "SELECT id, es_casa, start_day, end_day, altitud_m, fuente, viaje_ref, creado_en "
        "FROM altitud_estancia WHERE person_id = ? ORDER BY start_day", (person_id,))]
    asistente = {
        "conversaciones": conversaciones, "mensajes": mensajes_agente, "uso": uso_agente_rows,
        "tope": dict(tope_agente_row) if tope_agente_row else None, "propuestas": propuestas_agente,
        "uso_externo": uso_externo_rows, "escrituras_externas": escrituras_externas_rows,
    }

    return {
        "formato": "body-export-v1",
        "exportado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": {"display_name": person["display_name"], "timezone": person["timezone"],
                    "language": person["language"], "trabaja_turnos": person["trabaja_turnos"]},
        "hogar": dict(hogar_row) if hogar_row else None,
        "figuras_compartidas": figuras_compartidas,
        "consentimiento_comidas_compartidas": consentimiento_comidas_compartidas,
        "comidas_compartidas_mias": comidas_compartidas_mias,
        "comidas_compartidas_porciones": comidas_compartidas_porciones,
        "vida": vida,
        "experimentos": experimentos,
        "horizonte": horizonte,
        "ingesta": ingesta,
        "comidas": comidas,
        "momentos_comida": momentos_comida,
        "periodos": periodos,
        "dolor_entreno": dolor_entreno,
        "objetivos_nutriente": objetivos_nutriente,
        "checkin_sesion": {"sets": checkin_sesion_sets, "items": checkin_sesion_items,
                           "respuestas": checkin_sesion_respuestas},
        "fuentes_importacion": fuentes_importacion,
        "checkins": checkins,
        "dias": dias,
        "actividades": actividades,
        "sets_entreno": sets_entreno,
        "detalle_actividades": detalle_actividades,
        "sueno": sueno,
        "despertar_correcciones": despertar_correcciones,
        "recuperacion_actividades": recuperacion_actividades,
        "turno_fc": turno_fc_filas,
        "sincronizaciones": sincronizaciones,
        "consentimientos": consentimientos,
        "ajustes": ajustes,
        "garmin": garmin,
        "dispositivos": dispositivos,
        "asistente": asistente,
        "practicas": practicas_rows,
        "viajes_lugares": viajes_lugares_rows,
        "altitudes": altitudes_rows,
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
        # BODY-COMIDA-COMPARTIDA-1 ruling 6: every OTHER eater's portion of a meal THIS person
        # authored keeps their own comida (comida_id stays) but loses the link to a parent that is
        # about to disappear -- an applied one is orphan-safed (comida_compartida_id -> NULL); a
        # portion with no comida of its own (pendiente/descartada) has nothing left to preserve and
        # goes with the parent. Must run BEFORE this person's own comida/ingesta rows are deleted
        # below (their own portion rows, deleted next, reference those same comida rows) and before
        # the parent rows themselves are deleted (their children must be clear first, same FK-order
        # reasoning habit/goal_check/plan already follow against goal below).
        conn.execute(
            "UPDATE comida_compartida_porcion SET comida_compartida_id = NULL WHERE estado = 'aplicada' "
            "AND person_id != ? AND comida_compartida_id IN "
            "(SELECT id FROM comida_compartida WHERE autor_person_id = ?)", (person_id, person_id),
        )
        conn.execute(
            "DELETE FROM comida_compartida_porcion WHERE estado != 'aplicada' AND person_id != ? "
            "AND comida_compartida_id IN (SELECT id FROM comida_compartida WHERE autor_person_id = ?)",
            (person_id, person_id),
        )
        conn.execute("DELETE FROM comida_compartida_porcion WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM comida_compartida WHERE autor_person_id = ?", (person_id,))
        conn.execute("DELETE FROM ingesta WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM comida WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM period WHERE person_id = ?", (person_id,))
        db.invalidar_periodos_cache(conn, person_id)
        conn.execute("DELETE FROM nutrient_target WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM fuente_importacion WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM checkin_nota WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM checkin WHERE person_id = ?", (person_id,))
        conn.execute("UPDATE habit SET assignee_person_id = NULL WHERE assignee_person_id = ?", (person_id,))
        # BODY-TENDENCIAS-RACHA-GRAFICO-1 ruling 1: another household member's own racha_piso pick
        # may name one of THIS person's habits (a shared milestone's habit is readable, so pickable,
        # by every member) -- cleared to NULL before this person's own habits go, same FK-order
        # reasoning as assignee_person_id just above. This person's OWN racha_piso row is simply
        # deleted, below, with every other person-owned table.
        conn.execute(
            "UPDATE racha_piso SET habit_id = NULL WHERE habit_id IN "
            "(SELECT id FROM habit WHERE goal_id IN (SELECT id FROM goal WHERE person_id = ?))",
            (person_id,),
        )
        conn.execute("DELETE FROM racha_piso WHERE person_id = ?", (person_id,))
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
        # BODY-HOGAR-REGISTRAR-IDEMPOTENTE-1 ruling 3: escritura_externa.propuesta_id references
        # propuesta_agente(id) -- cleared before propuesta_agente itself goes, same FK-order
        # reasoning as every other table below that references a row this sweep is about to
        # remove. Scoped by this person's own person_id alone (never objetivo_id): every row here
        # was written for this exact person's own applied write, never another person's.
        conn.execute("DELETE FROM escritura_externa WHERE person_id = ?", (person_id,))
        # BODY-HISTORIA-VIDA-1 ruling 8, widened by BODY-HISTORIA-VIDA-2 ruling 4: habit.practica_id
        # now references `practica`, but it is never a dangling reference by this point -- every
        # habit this person owns (through a goal they created, shared or not) was already deleted
        # above, and a habit can only ever carry a practica_id pointing at ITS OWN creator's own
        # practica (validated at creation: "the person's own, active practice"), never another
        # member's. A plain delete suffices.
        conn.execute("DELETE FROM practica WHERE person_id = ?", (person_id,))
        # propuesta_agente.objetivo_id references goal(id) -- cleared before goal itself goes, same
        # as habit/habit_check/goal_check/plan above.
        conn.execute(
            "DELETE FROM propuesta_agente WHERE person_id = ? OR objetivo_id IN "
            "(SELECT id FROM goal WHERE person_id = ?)", (person_id, person_id),
        )
        # BODY-PLAN-ESQUEMA-1: the plan's own parts under a horizon goal this person created --
        # deleted before the goal itself goes, same FK-order reasoning as habit/habit_check/
        # goal_check/plan above. Scoped by person_id OR by horizonte_goal_id, so a household
        # member's own rows on a shared horizon goal that is about to disappear go too, same
        # cascade habit_check/goal_check already follow.
        conn.execute(
            "DELETE FROM horizonte_checkpoint WHERE person_id = ? OR horizonte_goal_id IN "
            "(SELECT id FROM goal WHERE person_id = ?)", (person_id, person_id),
        )
        conn.execute(
            "DELETE FROM horizonte_meta_semanal WHERE person_id = ? OR horizonte_goal_id IN "
            "(SELECT id FROM goal WHERE person_id = ?)", (person_id, person_id),
        )
        conn.execute(
            "DELETE FROM horizonte_semana WHERE person_id = ? OR horizonte_goal_id IN "
            "(SELECT id FROM goal WHERE person_id = ?)", (person_id, person_id),
        )
        conn.execute(
            "DELETE FROM horizonte_regla WHERE person_id = ? OR horizonte_goal_id IN "
            "(SELECT id FROM goal WHERE person_id = ?)", (person_id, person_id),
        )
        # BODY-PLAN-FASES-2 ruling 1: same cascade as its three siblings above -- a rule pointing
        # at one of this person's own phases was already cleared (fase_id is just another column
        # on the row just deleted above), so this delete needs no separate fase_id cleanup.
        conn.execute(
            "DELETE FROM horizonte_fase WHERE person_id = ? OR horizonte_goal_id IN "
            "(SELECT id FROM goal WHERE person_id = ?)", (person_id, person_id),
        )
        # experimento.goal_id and goal.horizonte_goal_id can point at a goal this person created
        # (their own horizon, shared with the household) from ANOTHER person's row -- cleared to
        # NULL before the goal itself goes, same reasoning as racha_piso.habit_id/
        # habit.assignee_person_id above.
        conn.execute(
            "UPDATE experimento SET goal_id = NULL WHERE goal_id IN (SELECT id FROM goal WHERE person_id = ?)",
            (person_id,),
        )
        conn.execute(
            "UPDATE goal SET horizonte_goal_id = NULL WHERE horizonte_goal_id IN "
            "(SELECT id FROM goal WHERE person_id = ?)", (person_id,),
        )
        conn.execute("DELETE FROM goal WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM daily_metric WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM sleep_session WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM despertar_correccion WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM raw_payload WHERE person_id = ?", (person_id,))
        # BODY-HISTORIA-VIDA-LUGARES-1 ruling 4: this person's own cached trips and sync state,
        # both re-fetched from Lugares, never hand-copied -- same census treatment as every other
        # plain person-keyed table above.
        conn.execute("DELETE FROM viaje_lugares WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM lugares_sync WHERE person_id = ?", (person_id,))
        # BODY-MINUTO-ANALISIS-6-ALTITUD (ORCH-CARLOS-DELEGATES-TIER-1-1): same census treatment
        # as viaje_lugares above -- a plain person-keyed table, no row this sweep must clear first.
        conn.execute("DELETE FROM altitud_estancia WHERE person_id = ?", (person_id,))
        # workout_set's FK targets activity's composite key -- deleted first, same as every other
        # table that references a row this sweep is about to remove. activity_detail has no
        # formal FK (same as workout_set, above) but names the same (source, source_id) pair --
        # deleted first for the same reason.
        conn.execute("DELETE FROM workout_set WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM activity_detail WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM entreno_dolor WHERE person_id = ?", (person_id,))
        # BODY-PARTIDO-2: set_pregunta_item references set_pregunta(id) -- cleared before
        # set_pregunta itself goes, same FK-order reasoning as every other table above that
        # references a row this sweep is about to remove.
        conn.execute(
            "DELETE FROM set_pregunta_item WHERE set_pregunta_id IN "
            "(SELECT id FROM set_pregunta WHERE person_id = ?)", (person_id,),
        )
        conn.execute("DELETE FROM set_pregunta WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM set_pregunta_respuesta WHERE person_id = ?", (person_id,))
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
        conn.execute("DELETE FROM momento_comida WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM consentimiento WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM garmin_cuenta WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM dispositivo WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM mensaje_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM conversacion_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM uso_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM uso_herramienta_externa WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM tope_agente WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM hogar_miembro WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM sesion WHERE person_id = ?", (person_id,))
        compartir.delete_rows(conn, person_id)
        conn.execute("DELETE FROM person WHERE id = ?", (person_id,))
    _borrar_fuera_de_bd(person_id)


# ---- per-source delete (BODY-DESPLAZAMIENTO-3 ruling 4) -------------------------------------------

def fuentes_importadas(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Every source this person has ever SUCCESSFULLY imported from (importado_at not null) --
    an error-only row (never a real import) has nothing to list or delete."""
    return [dict(r) for r in conn.execute(
        "SELECT fuente, primer_dia, ultimo_dia, dias FROM fuente_importacion "
        "WHERE person_id = ? AND importado_at IS NOT NULL ORDER BY fuente", (person_id,)
    )]


def delete_fuente(conn: sqlite3.Connection, person_id: int, fuente: str) -> bool:
    """Removes exactly this person's daily_metric rows written by `fuente` (its own registered
    metric keys only) and its fuente_importacion row, in one transaction. False (nothing touched)
    for an unknown fuente or one this person has never imported -- Garmin, self-typed and every
    other source's rows are untouched, since the delete is scoped to this one source's own keys."""
    datos = fuentes.FUENTES.get(fuente)
    if datos is None:
        return False
    existe = conn.execute(
        "SELECT 1 FROM fuente_importacion WHERE person_id = ? AND fuente = ? AND importado_at IS NOT NULL",
        (person_id, fuente),
    ).fetchone()
    if existe is None:
        return False
    keys = datos["metric_keys"]
    placeholders = ",".join("?" * len(keys))
    with conn:
        conn.execute(
            f"DELETE FROM daily_metric WHERE person_id = ? AND source = ? AND metric_key IN ({placeholders})",
            (person_id, fuente, *keys),
        )
        conn.execute("DELETE FROM fuente_importacion WHERE person_id = ? AND fuente = ?", (person_id, fuente))
    return True


def _borrar_fuera_de_bd(person_id: int) -> None:
    """Placeholder call point for anything this person's delete-all must also remove OUTSIDE the
    database. Empty, and stays empty: BODY-GARMIN-HOST-1's Garmin token lives in `garmin_cuenta`,
    a person-keyed table the generic deletes above already remove -- there is nothing left outside
    the database. Takes only an id, never a figure, like every function in this module."""
    return None
