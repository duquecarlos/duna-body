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
many have no live-API intraday row, and the oldest such missing day. Never a health figure: every
number here is a count of rows or samples, never a value from one.

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

from . import backup, config, cuenta, db, errores, fechas, garmin_adapter, pull

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
