"""python -m body.cobertura [--cuenta-sub <sub>] [--json]
   python -m body.cobertura --conteo

Read-only, counts-and-dates-only coverage report of what is actually stored for one person's
minute-by-minute history (BODY-HISTORIA-MINUTO-2, following BODY-HISTORIA-MINUTO-1-INV's own
census). Opens the database read-only (body.backup.open_readonly) -- no write, no migration, no
network, no Garmin call -- and prints, per stream: intraday heart rate and stress, per source
(days with a row, first/last day, days whose stored payload parses to at least one valid minute
point, via db._INTRADIA_PARSER_NAMES); the sleep payload's five per-minute keys
(garmin_adapter._SLEEP_SERIES_SPECS), each as nights present/non-empty and their median sample
count; the body-battery feed's own intraday array, the same way, by day; and the keeper check --
of the days in the keeper's own INTRADIA_ALCANCE_DIAS window (pull.py) that have a daily pull, how
many have no live-API intraday row, and the oldest such missing day; and a `detalle_actividad` block
(BODY-ESFUERZO-COBERTURA-1) over the last DETALLE_VENTANA_DIAS days of activities (capped at
DETALLE_VENTANA_MAX, newest first) naming which link the session-effort card's own parsing chain
(garmin_adapter.parse_activity_hr_series, esfuerzo_sesion.serie_por_minuto/esfuerzos) breaks on for
each one -- descriptor key names, raw timestamp cell types, and each activity's own coverage against
its own window. Never a health figure: every number here is a count of rows or samples, or an
activity's own type code and day, never a heart-rate value, a name, a title or a source_id.

`--cuenta-sub` resolves the destination person by a plain `SELECT id FROM person WHERE
cuenta_sub = ?` -- the same lookup body.cuenta.resolve_person runs before it would ever consider
creating a row, never the create-on-first-sign-in half of it: this command is read-only end to
end, so it can only ever find an existing person, never make one. Required when hosted
(body.cuenta.hosted()); optional on a single-person install, where the local person
(db.default_person_id()) is used when it is left out.

`--conteo` prints a different, cross-person report instead: ORCH-CARLOS-DELEGATES-TIER-1-1
ruling 3's standing-deploy-go census (conteo_personas) -- two counts only, never a cuenta_sub,
a name or an email.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from . import backup, config, cuenta, db, errores, esfuerzo_sesion, fechas, garmin_adapter, pull

# BODY-ESFUERZO-COBERTURA-1: the window and cap the new detalle_actividad block reads over --
# named here so a later slice can widen either without hunting through the function.
DETALLE_VENTANA_DIAS = 120
DETALLE_VENTANA_MAX = 60

# list_key (the raw payload key parse_sleep_series reads) -> its own short name in that
# function's output dict -- derived from the single source of truth so this never drifts from it.
_SLEEP_SHORT_POR_CLAVE = {spec[1]: spec[0] for spec in garmin_adapter._SLEEP_SERIES_SPECS}


def _resolver_persona(conn: sqlite3.Connection, cuenta_sub: str | None) -> int:
    if cuenta.hosted() and not cuenta_sub:
        raise ValueError("--cuenta-sub is required when hosted")
    if cuenta_sub:
        row = conn.execute("SELECT id FROM person WHERE cuenta_sub = ?", (cuenta_sub,)).fetchone()
        if row is None:
            raise ValueError("no person found for that --cuenta-sub")
        return row["id"]
    return db.default_person_id()


def _bloque_intradia(conn: sqlite3.Connection, person_id: int, kind: str, zona: str) -> dict:
    parser = getattr(garmin_adapter, db._INTRADIA_PARSER_NAMES[kind])
    out = {}
    for source in (garmin_adapter.SOURCE, garmin_adapter.SOURCE_EXPORT):
        rows = conn.execute(
            "SELECT day, payload FROM raw_payload WHERE person_id = ? AND kind = ? AND source = ? "
            "ORDER BY day",
            (person_id, kind, source),
        ).fetchall()
        validos = 0
        for r in rows:
            try:
                serie = parser(json.loads(r["payload"]), zona)
            except (TypeError, ValueError):
                serie = ()
            if serie:
                validos += 1
        out[source] = {
            "dias": len(rows),
            "primero": rows[0]["day"] if rows else None,
            "ultimo": rows[-1]["day"] if rows else None,
            "dias_validos": validos,
        }
    return out


def _sleep_payloads(conn: sqlite3.Connection, person_id: int) -> dict[str, Any]:
    """One payload per stored night, API source preferred over an import's when both exist for the
    same day -- the same precedence db.sueno_minuto_a_minuto uses. A payload string that will not
    parse as JSON is kept as None (the night is still stored; it simply carries no data)."""
    rows = conn.execute(
        "SELECT day, payload FROM raw_payload WHERE person_id = ? AND kind = 'sleep' "
        "ORDER BY (source != ?)",
        (person_id, garmin_adapter.SOURCE),
    ).fetchall()
    out: dict[str, Any] = {}
    for r in rows:
        if r["day"] in out:
            continue
        try:
            out[r["day"]] = json.loads(r["payload"])
        except (TypeError, ValueError):
            out[r["day"]] = None
    return out


def _claves_crudas_sleep(payloads: dict[str, Any]) -> dict[str, int]:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: every TOP-LEVEL key name any stored sleep payload
    carries (never walked into a key's own nested content), with the number of nights it is
    present AND non-empty -- names and counts only, never a value, so the next host run can name
    the real SpO2 key if `wellnessEpochSPO2DataDTOList` (BODY-COBERTURA-HOST-1: present on 0 of
    884 stored payloads) is simply the wrong assumed name, not an absent reading."""
    conteos: dict[str, int] = {}
    for payload in payloads.values():
        if not isinstance(payload, dict):
            continue
        for clave, valor in payload.items():
            if valor:
                conteos[clave] = conteos.get(clave, 0) + 1
    return conteos


def _bloque_sleep(conn: sqlite3.Connection, person_id: int, zona: str) -> dict:
    payloads = _sleep_payloads(conn, person_id)
    dias = sorted(payloads)
    analizadas = {day: garmin_adapter.parse_sleep_series(payloads[day], zona) for day in dias}
    claves = {}
    for list_key, corto in _SLEEP_SHORT_POR_CLAVE.items():
        muestras = [len(analizadas[day][corto]) for day in dias if analizadas[day].get(corto)]
        claves[list_key] = {
            "noches": len(muestras),
            "mediana_muestras": statistics.median(muestras) if muestras else None,
        }
    return {
        "noches": len(dias),
        "primero": dias[0] if dias else None,
        "ultimo": dias[-1] if dias else None,
        "claves": claves,
        "claves_crudas": _claves_crudas_sleep(payloads),
    }


def _body_battery_muestras(payload: Any) -> int:
    """The same extraction garmin_adapter's own body-battery fallback uses
    (garmin_adapter.py:572-578), counted rather than reduced to high/low."""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return 0
    arr = payload[0].get("bodyBatteryValuesArray")
    if not isinstance(arr, list):
        return 0
    return sum(
        1 for v in arr
        if isinstance(v, list) and len(v) > 1 and garmin_adapter._num(v[1]) is not None
    )


def _bloque_body_battery(conn: sqlite3.Connection, person_id: int) -> dict:
    rows = conn.execute(
        "SELECT payload FROM raw_payload WHERE person_id = ? AND kind = 'body_battery'",
        (person_id,),
    ).fetchall()
    muestras = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        n = _body_battery_muestras(payload)
        if n:
            muestras.append(n)
    return {
        "dias": len(rows),
        "dias_con_datos": len(muestras),
        "mediana_muestras": statistics.median(muestras) if muestras else None,
    }


def _bloque_keeper(conn: sqlite3.Connection, person_id: int, zona: str) -> dict:
    hoy = fechas.hoy_local({"timezone": zona})
    limite = hoy - timedelta(days=pull.INTRADIA_ALCANCE_DIAS)
    ayer = hoy - timedelta(days=1)
    limite_iso, ayer_iso = limite.isoformat(), ayer.isoformat()
    # The daily pull always attempts 'sleep' (pull._FEEDS_ORDEN), including as an explicit empty
    # marker when Garmin returned nothing -- the same signal db.sueno_minuto_a_minuto's own
    # docstring already relies on for "this day's ordinary sync ran".
    pull_rows = conn.execute(
        "SELECT DISTINCT day FROM raw_payload WHERE person_id = ? AND kind = 'sleep' AND "
        "source = ? AND day BETWEEN ? AND ?",
        (person_id, garmin_adapter.SOURCE, limite_iso, ayer_iso),
    ).fetchall()
    dias_con_pull = {r["day"] for r in pull_rows}
    out = {}
    for kind in db.INTRADIA_KINDS:
        intradia_rows = conn.execute(
            "SELECT DISTINCT day FROM raw_payload WHERE person_id = ? AND kind = ? AND "
            "source = ? AND day BETWEEN ? AND ?",
            (person_id, kind, garmin_adapter.SOURCE, limite_iso, ayer_iso),
        ).fetchall()
        dias_con_intradia = {r["day"] for r in intradia_rows}
        faltantes = sorted(dias_con_pull - dias_con_intradia)
        out[kind] = {
            "dias_con_pull_diario": len(dias_con_pull),
            "dias_sin_intradia": len(faltantes),
            "dia_mas_antiguo_faltante": faltantes[0] if faltantes else None,
        }
    return out


def _tipo_celda_cruda(valor: Any) -> str:
    """int / float / str / missing -- never "bool", since Python's bool is a subclass of int and
    a Garmin timestamp cell is never a bool."""
    if valor is None:
        return "missing"
    if isinstance(valor, bool):
        return "other"
    if isinstance(valor, int):
        return "int"
    if isinstance(valor, float):
        return "float"
    if isinstance(valor, str):
        return "str"
    return "other"


def _actividades_con_detalle(conn: sqlite3.Connection, person_id: int, zona: str) -> list[tuple]:
    """This person's own activities whose day is within DETALLE_VENTANA_DIAS of today, newest
    first, capped at DETALLE_VENTANA_MAX -- each paired with whatever activity_detail rows it has
    (kind -> parsed JSON, or None for stored-but-unparseable text), read the same way _sleep_
    payloads above reads raw_payload directly."""
    hoy = fechas.hoy_local({"timezone": zona})
    desde = (hoy - timedelta(days=DETALLE_VENTANA_DIAS)).isoformat()
    filas = conn.execute(
        "SELECT source, source_id, type, day, start_local, duration_s FROM activity WHERE "
        "person_id = ? AND day >= ? ORDER BY day DESC, start_local DESC LIMIT ?",
        (person_id, desde, DETALLE_VENTANA_MAX),
    ).fetchall()
    out = []
    for fila in filas:
        detalle_rows = conn.execute(
            "SELECT kind, payload FROM activity_detail WHERE person_id = ? AND activity_source = ? "
            "AND activity_source_id = ? AND kind IN ('detalle', 'vueltas', 'zonas')",
            (person_id, fila["source"], fila["source_id"]),
        ).fetchall()
        payloads: dict[str, Any] = {}
        for r in detalle_rows:
            try:
                payloads[r["kind"]] = json.loads(r["payload"])
            except (TypeError, ValueError):
                payloads[r["kind"]] = None
        out.append((fila, payloads))
    return out


def _fila_detalle_actividad(fila, detalle_payload: Any, zona: str, orden: int) -> dict:
    """Ruling 2/3: one activity's own row, read through the SAME parsers and the SAME window the
    card uses (garmin_adapter.parse_activity_hr_series, esfuerzo_sesion.serie_por_minuto/
    esfuerzos) -- never a figure, never a name, never a source_id."""
    start_local, duration_s = fila["start_local"], fila["duration_s"]
    descriptores = metrics = None
    if isinstance(detalle_payload, dict):
        descriptores = detalle_payload.get("metricDescriptors")
        metrics = detalle_payload.get("activityDetailMetrics")

    muestras = (garmin_adapter.parse_activity_hr_series(detalle_payload, zona)
                if detalle_payload is not None else ())
    serie = esfuerzo_sesion.serie_por_minuto(muestras, start_local, duration_s)
    minutos_en_serie = {dt for dt, _ in serie}
    muestras_en_ventana = sum(1 for t, _ in muestras if datetime.fromisoformat(t) in minutos_en_serie)
    total_min = (duration_s or 0) / 60
    cobertura_pct = round(len(serie) / total_min * 100, 1) if total_min else None
    esf = esfuerzo_sesion.esfuerzos(muestras, start_local, duration_s)

    desfase_inicio = desfase_fin = None
    if muestras and start_local:
        tiempos = [datetime.fromisoformat(t) for t, _ in muestras]
        inicio_dt = datetime.fromisoformat(start_local)
        desfase_inicio = round((min(tiempos) - inicio_dt).total_seconds() / 60)
        if duration_s is not None:
            fin_dt = inicio_dt + timedelta(seconds=duration_s)
            desfase_fin = round((max(tiempos) - fin_dt).total_seconds() / 60)

    ts_idx = (garmin_adapter._descriptor_index(descriptores, ("directtimestamp", "timestamp"))
              if descriptores is not None else None)
    crudos = []
    if ts_idx is not None and isinstance(metrics, list):
        for entrada in metrics:
            if not isinstance(entrada, dict):
                continue
            fila_cruda = entrada.get("metrics")
            if isinstance(fila_cruda, list) and len(fila_cruda) > ts_idx:
                celda = fila_cruda[ts_idx]
                if isinstance(celda, (int, float)) and not isinstance(celda, bool):
                    crudos.append(celda)
    intervalo_mediano_s = (statistics.median(abs(crudos[i + 1] - crudos[i]) / 1000
                                              for i in range(len(crudos) - 1))
                            if len(crudos) >= 2 else None)

    return {
        "orden": orden,
        "type": fila["type"],
        "day": fila["day"],
        "minutos_sesion": round(duration_s / 60) if duration_s is not None else None,
        "filas_crudas": len(metrics) if isinstance(metrics, list) else 0,
        "muestras_parseadas": len(muestras),
        "muestras_en_ventana": muestras_en_ventana,
        "minutos_distintos_en_ventana": len(serie),
        "cobertura_pct": cobertura_pct,
        "desfase_min": {"inicio": desfase_inicio, "fin": desfase_fin},
        "intervalo_mediano_s": intervalo_mediano_s,
        "suficiente": esf["suficiente"],
    }


def _bloque_detalle_actividad(conn: sqlite3.Connection, person_id: int, zona: str) -> dict:
    """BODY-ESFUERZO-COBERTURA-1: the diagnostic the orchestrator's candidates (a), (b), (c), (d)
    all need the same raw material for -- which descriptor key names and raw timestamp cell types
    the real payloads actually carry, and where each activity's own parsed samples land against
    its own window, so one host run can name which link breaks without guessing."""
    pares = _actividades_con_detalle(conn, person_id, zona)
    claves_descriptor: dict[str, int] = {}
    tipos_index = {"int": 0, "other": 0}
    claves_nivel_superior: dict[str, int] = {}
    tipo_timestamp_crudo = {"int": 0, "float": 0, "str": 0, "missing": 0}
    con_fila_detalle = con_fila_vueltas = con_fila_zonas = 0
    con_muestra_detalle = 0
    actividades = []

    for orden, (fila, payloads) in enumerate(pares, start=1):
        if "detalle" in payloads:
            con_fila_detalle += 1
        if "vueltas" in payloads:
            con_fila_vueltas += 1
        if "zonas" in payloads:
            con_fila_zonas += 1

        detalle_payload = payloads.get("detalle")
        if "detalle" in payloads:
            descriptores = metrics = None
            if isinstance(detalle_payload, dict):
                for clave in detalle_payload:
                    claves_nivel_superior[clave] = claves_nivel_superior.get(clave, 0) + 1
                descriptores = detalle_payload.get("metricDescriptors")
                metrics = detalle_payload.get("activityDetailMetrics")
                if isinstance(descriptores, list):
                    vistas = set()
                    for entrada in descriptores:
                        if not isinstance(entrada, dict):
                            continue
                        clave = entrada.get("key")
                        if isinstance(clave, str) and clave not in vistas:
                            vistas.add(clave)
                            claves_descriptor[clave] = claves_descriptor.get(clave, 0) + 1
                        idx = entrada.get("index")
                        etiqueta = "int" if isinstance(idx, int) and not isinstance(idx, bool) else "other"
                        tipos_index[etiqueta] += 1
            ts_idx = (garmin_adapter._descriptor_index(descriptores, ("directtimestamp", "timestamp"))
                      if isinstance(descriptores, list) else None)
            celda = None
            if ts_idx is not None and isinstance(metrics, list) and metrics:
                primera = metrics[0]
                fila_cruda = primera.get("metrics") if isinstance(primera, dict) else None
                if isinstance(fila_cruda, list) and len(fila_cruda) > ts_idx:
                    celda = fila_cruda[ts_idx]
            tipo_timestamp_crudo[_tipo_celda_cruda(celda) if ts_idx is not None else "missing"] += 1

        fila_out = _fila_detalle_actividad(fila, detalle_payload, zona, orden)
        if fila_out["muestras_parseadas"]:
            con_muestra_detalle += 1
        actividades.append(fila_out)

    return {
        "actividades_en_ventana": len(pares),
        "con_fila_detalle": con_fila_detalle,
        "con_fila_vueltas": con_fila_vueltas,
        "con_fila_zonas": con_fila_zonas,
        "con_muestra_detalle": con_muestra_detalle,
        "claves_descriptor": claves_descriptor,
        "tipos_index_descriptor": tipos_index,
        "claves_nivel_superior_detalle": claves_nivel_superior,
        "tipo_timestamp_crudo": tipo_timestamp_crudo,
        "actividades": actividades,
    }


def conteo_personas(conn: sqlite3.Connection) -> dict:
    """ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 3: the standing deploy go's own expiry condition
    ("until someone other than me uses Body", dev-protocol BODY-STANDING-DEPLOY-GO-RELAYED-1)
    measured as two counts, never a name, a sub or an email -- hosted persons (person.cuenta_sub
    IS NOT NULL) with an accepted consent, and hosted persons with any activity in the last 30
    days. Activity is `sesion.last_seen_at` -- body.cuenta.resolve_session updates it on every
    page view (throttled), the cheapest honest activity signal already stored."""
    consentidos = conn.execute(
        "SELECT COUNT(DISTINCT p.id) FROM person p JOIN consentimiento c ON c.person_id = p.id "
        "WHERE p.cuenta_sub IS NOT NULL"
    ).fetchone()[0]
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    activos_30d = conn.execute(
        "SELECT COUNT(DISTINCT p.id) FROM person p JOIN sesion s ON s.person_id = p.id "
        "WHERE p.cuenta_sub IS NOT NULL AND s.last_seen_at >= ?",
        (limite,),
    ).fetchone()[0]
    return {"consentidos": consentidos, "activos_30d": activos_30d}


def recopilar(conn: sqlite3.Connection, cuenta_sub: str | None = None) -> dict:
    person_id = _resolver_persona(conn, cuenta_sub)
    zona = db.person(conn, person_id)["timezone"]
    out: dict[str, Any] = {}
    for kind in db.INTRADIA_KINDS:
        out[kind] = _bloque_intradia(conn, person_id, kind, zona)
    out["sleep"] = _bloque_sleep(conn, person_id, zona)
    out["body_battery"] = _bloque_body_battery(conn, person_id)
    out["keeper"] = _bloque_keeper(conn, person_id, zona)
    out["detalle_actividad"] = _bloque_detalle_actividad(conn, person_id, zona)
    return out


def _imprimir(out: dict) -> None:
    for kind in db.INTRADIA_KINDS:
        for source, datos in out[kind].items():
            print(
                f"{kind}.{source}: dias={datos['dias']} primero={datos['primero']} "
                f"ultimo={datos['ultimo']} dias_validos={datos['dias_validos']}"
            )
    sleep = out["sleep"]
    print(f"sleep: noches={sleep['noches']} primero={sleep['primero']} ultimo={sleep['ultimo']}")
    for clave, datos in sleep["claves"].items():
        print(f"sleep.{clave}: noches={datos['noches']} mediana_muestras={datos['mediana_muestras']}")
    for clave, noches in sleep["claves_crudas"].items():
        print(f"sleep.clave_cruda.{clave}: noches={noches}")
    bb = out["body_battery"]
    print(
        f"body_battery: dias={bb['dias']} dias_con_datos={bb['dias_con_datos']} "
        f"mediana_muestras={bb['mediana_muestras']}"
    )
    for kind, datos in out["keeper"].items():
        print(
            f"keeper.{kind}: dias_con_pull_diario={datos['dias_con_pull_diario']} "
            f"dias_sin_intradia={datos['dias_sin_intradia']} "
            f"dia_mas_antiguo_faltante={datos['dia_mas_antiguo_faltante']}"
        )
    da = out["detalle_actividad"]
    print(
        f"detalle_actividad: actividades_en_ventana={da['actividades_en_ventana']} "
        f"con_fila_detalle={da['con_fila_detalle']} con_fila_vueltas={da['con_fila_vueltas']} "
        f"con_fila_zonas={da['con_fila_zonas']} con_muestra_detalle={da['con_muestra_detalle']}"
    )
    for clave, n in da["claves_descriptor"].items():
        print(f"detalle_actividad.claves_descriptor.{clave}: payloads={n}")
    print(
        f"detalle_actividad.tipos_index_descriptor: int={da['tipos_index_descriptor']['int']} "
        f"other={da['tipos_index_descriptor']['other']}"
    )
    for clave, n in da["claves_nivel_superior_detalle"].items():
        print(f"detalle_actividad.claves_nivel_superior_detalle.{clave}: payloads={n}")
    tc = da["tipo_timestamp_crudo"]
    print(
        f"detalle_actividad.tipo_timestamp_crudo: int={tc['int']} float={tc['float']} "
        f"str={tc['str']} missing={tc['missing']}"
    )
    for a in da["actividades"]:
        print(
            f"detalle_actividad.actividad[{a['orden']}]: type={a['type']} day={a['day']} "
            f"minutos_sesion={a['minutos_sesion']} filas_crudas={a['filas_crudas']} "
            f"muestras_parseadas={a['muestras_parseadas']} muestras_en_ventana={a['muestras_en_ventana']} "
            f"minutos_distintos_en_ventana={a['minutos_distintos_en_ventana']} "
            f"cobertura_pct={a['cobertura_pct']} desfase_min.inicio={a['desfase_min']['inicio']} "
            f"desfase_min.fin={a['desfase_min']['fin']} intervalo_mediano_s={a['intervalo_mediano_s']} "
            f"suficiente={a['suficiente']}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.cobertura")
    p.add_argument("--cuenta-sub", help="the hosted person's Cuenta sub (required when hosted)")
    p.add_argument("--json", action="store_true", help="print the same report as JSON")
    p.add_argument("--conteo", action="store_true",
                    help="print only two counts: hosted persons consented, and active in the "
                         "last 30 days -- never a cuenta_sub, a name or a health figure")
    a = p.parse_args(argv)

    if a.conteo:
        conn = backup.open_readonly(config.DB_PATH)
        try:
            out = conteo_personas(conn)
        finally:
            conn.close()
        print(f"consentidos={out['consentidos']} activos_30d={out['activos_30d']}")
        return 0

    conn = backup.open_readonly(config.DB_PATH)
    try:
        out = recopilar(conn, a.cuenta_sub)
    except ValueError as exc:
        print(f"refused: {errores.texto_seguro(exc)}")
        return 1
    finally:
        conn.close()

    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        _imprimir(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
