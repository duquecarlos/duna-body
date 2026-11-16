"""Plan vs real for a horizon (BODY-PLAN-MODULO-1, taking BODY-PLAN-ESQUEMA-1's schema and
BODY-PLAN-PERSONAL-1's idea: Carlos's own plan in Body as plan vs real, "just as we are doing with
the money plan in Finanzas").

Ruling 1: pure reads over a connection -- never stores, never renders. Every later screen (goal
detail, Hoy, the assistant's tools) reads this module and recomputes nothing. `semana` and
`plan_vs_real` fetch every data source they need ONCE per call (daily_metric, sleep_session, the
strength and cardio classifiers, the horizon's own rule/target/plan rows, shifts, periods and
night blocks) and group it in Python per week, so their own statement count stays BOUNDED --
independent of how many weeks, target lines, rules or checkpoints a horizon carries.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import date, timedelta

from . import db, entreno_racha, fechas, goals
from .turnos_costo import bloques_noche

# Garmin typeKeys this app already classifies (grepped from body/history.py's own
# KNOWN_ACTIVITY_TYPES, Garmin's vocabulary, never a person's data) that are cardio for this
# measure -- strength_training is its own measure (ruling 5, entreno_racha.TIPOS_FUERZA, never
# widened here), and 'yoga'/'other' are neither. The orchestrator's spec named a few typeKeys this
# app has never actually seen (stair_climbing, rowing, soccer); they are left out until a pull
# actually produces one, rather than invented here (see open_followups).
TIPOS_CARDIO = frozenset({
    "running", "trail_running", "treadmill_running", "track_running",
    "cycling", "indoor_cycling", "mountain_biking", "road_biking",
    "swimming", "lap_swimming", "open_water_swimming",
    "walking", "hiking", "elliptical", "cardio",
})
MIN_CARDIO_S = 20 * 60  # a session shorter than this does not count toward sesiones_cardio

SENTIDOS_CUENTA = ("al_menos", "como_maximo")
SENTIDOS_HORA = ("antes_de", "despues_de")

# Ruling 2: the weekly-target vocabulary. 'metodo' documents how each one's real figure is
# computed (used by this module's own docstrings and the ledger entry; _real_medida below branches
# on the medida key itself, not on this field). Any metric_def key not listed here is also valid
# (validar_medida's else branch) -- its real figure is the week's mean, sentidos SENTIDOS_CUENTA.
MEDIDAS = {
    "sueno_minutos": {"unit": "min", "metodo": "media", "sentidos": SENTIDOS_CUENTA},
    "hora_acostarse": {"unit": "hora", "metodo": "mediana", "sentidos": SENTIDOS_HORA},
    "hora_levantarse": {"unit": "hora", "metodo": "mediana", "sentidos": SENTIDOS_HORA},
    "sesiones_fuerza": {"unit": "sesiones", "metodo": "cuenta", "sentidos": SENTIDOS_CUENTA},
    "sesiones_cardio": {"unit": "sesiones", "metodo": "cuenta", "sentidos": SENTIDOS_CUENTA},
    "minutos_intensidad": {"unit": "min", "metodo": "suma", "sentidos": SENTIDOS_CUENTA},
}

_CHEQUEO_TIPOS = ("acostarse_antes", "levantarse_antes", "sueno_minimo", "sin_sesion", "sesion_antes_de")
_CHEQUEO_CLASES = ("fuerza", "cardio", "cualquiera")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_VENTANAS = ("semana", "desde_inicio")
_CONTEXTOS_REGLA = ("siempre", "turno", "tras_noche", "periodo", "descanso")


def _parse(d: str) -> date:
    return date.fromisoformat(d)


def _parse_hhmm(hora) -> tuple[int, int]:
    m = _HHMM_RE.match(hora or "")
    if not m:
        raise ValueError(f"hora: not HH:MM: {hora!r}")
    return int(m.group(1)), int(m.group(2))


def _parse_hora_local(iso_dt: str) -> tuple[int, int]:
    """The (hour, minute) of an ISO local datetime string, ignoring its date part."""
    parte = iso_dt.split("T")[-1] if "T" in iso_dt else iso_dt.split(" ")[-1]
    hh, mm = parte.split(":")[:2]
    return int(hh), int(mm)


def _minutos_despues_mediodia(hh: int, mm: int) -> int:
    """The schema's own clock-time convention (body/db.py's horizonte_meta_semanal.valor comment):
    minutes after 12:00 noon, wrapping across midnight -- 23:30 = 690, 00:45 = 765."""
    return ((hh - 12) % 24) * 60 + mm


def _cumple_valor(real: float, sentido: str, valor: float) -> bool:
    if sentido == "al_menos":
        return real >= valor
    if sentido == "como_maximo":
        return real <= valor
    if sentido == "antes_de":
        return real <= valor
    if sentido == "despues_de":
        return real >= valor
    raise ValueError(f"sentido: unknown {sentido!r}")


def validar_medida(conn, medida: str, sentido: str) -> None:
    """The one validator for a weekly target's (medida, sentido) pair (ruling 2). Raises
    ValueError naming the invalid field first; the proposal slice calls this before writing a
    horizonte_meta_semanal row -- db.crear_meta_semanal/corregir_meta_semanal check only
    non-empty."""
    if medida in MEDIDAS:
        permitidos = MEDIDAS[medida]["sentidos"]
    else:
        if conn.execute("SELECT 1 FROM metric_def WHERE key = ?", (medida,)).fetchone() is None:
            raise ValueError(f"medida: unknown {medida!r}")
        permitidos = SENTIDOS_CUENTA
    if sentido not in permitidos:
        raise ValueError(f"sentido: {sentido!r} not allowed for medida {medida!r} (allowed: {permitidos!r})")


def validar_chequeo(chequeo) -> None:
    """None is valid -- solo_texto (ruling 6). Raises ValueError naming the invalid field first;
    the proposal slice calls this before writing a horizonte_regla row -- db.crear_regla_horizonte
    checks only well-formed JSON."""
    if chequeo is None:
        return
    if not isinstance(chequeo, dict) or "tipo" not in chequeo:
        raise ValueError("chequeo: must be null or a dict with a 'tipo'")
    tipo = chequeo["tipo"]
    if tipo not in _CHEQUEO_TIPOS:
        raise ValueError(f"chequeo.tipo: unknown {tipo!r}")
    if tipo in ("acostarse_antes", "levantarse_antes", "sesion_antes_de"):
        _parse_hhmm(chequeo.get("hora"))
    if tipo == "sueno_minimo":
        minutos = chequeo.get("minutos")
        if not isinstance(minutos, (int, float)) or isinstance(minutos, bool) or minutos <= 0:
            raise ValueError("chequeo.minutos: must be a positive number")
    if tipo in ("sin_sesion", "sesion_antes_de"):
        if chequeo.get("clase") not in _CHEQUEO_CLASES:
            raise ValueError(f"chequeo.clase: unknown {chequeo.get('clase')!r}")


def validar_criterios(conn, criterios) -> None:
    """The validator for a checkpoint's `criterios` list (ruling 7). Raises ValueError naming the
    invalid field first; the proposal slice calls this before writing a horizonte_checkpoint row
    -- db.crear_checkpoint_horizonte checks only "a non-empty list"."""
    if not isinstance(criterios, list) or not criterios:
        raise ValueError("criterios: must be a non-empty list")
    for c in criterios:
        if not isinstance(c, dict):
            raise ValueError("criterios: each item must be a dict")
        validar_medida(conn, c.get("medida"), c.get("sentido"))
        valor = c.get("valor")
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            raise ValueError("criterios: valor must be a number")
        if c.get("ventana") not in _VENTANAS:
            raise ValueError(f"criterios: ventana must be one of {_VENTANAS!r}")


def historial_meta(conn, meta_id: int) -> list[dict]:
    """Walks `reemplaza_id` back from `meta_id` to the first version -- the audit trail for one
    weekly target line (ruling 3), oldest first. One statement per version in the chain (bounded
    by how many times THIS line was corrected, not by the horizon's week or line count)."""
    cadena = []
    current_id = meta_id
    while current_id is not None:
        row = conn.execute(
            "SELECT id, horizonte_goal_id, person_id, week_start, medida, sentido, valor, nota, "
            "vigente, reemplaza_id, origen, creado_en FROM horizonte_meta_semanal WHERE id = ?",
            (current_id,),
        ).fetchone()
        if row is None:
            break
        cadena.append(dict(row))
        current_id = row["reemplaza_id"]
    cadena.reverse()
    return cadena


def _dias_semana(monday: date) -> list[str]:
    return [(monday + timedelta(i)).isoformat() for i in range(7)]


def _sesiones_sueno(conn, person_id: int, desde: date, hasta: date) -> dict[str, dict]:
    """sleep_session rows touching [desde, hasta], re-keyed from Garmin's own storage convention
    (the wake day) to the calendar day each night STARTED on -- ruling 6's own choice ("the night
    that ends on the next morning belongs to the day it started"), applied once here so ruling 2's
    weekly figure and ruling 6's day-by-day chequeo never disagree about which day a night
    belongs to. Reads one day wider than needed (hasta + 1) since a night starting on `hasta`
    files under the next wake day."""
    filas = conn.execute(
        "SELECT day, start_local, end_local FROM sleep_session WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, desde.isoformat(), (hasta + timedelta(1)).isoformat()),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in filas:
        dia = r["start_local"].split("T")[0] if r["start_local"] else r["day"]
        if desde.isoformat() <= dia <= hasta.isoformat():
            out[dia] = {"start_local": r["start_local"], "end_local": r["end_local"]}
    return out


def _es_cardio(row, pareja) -> bool:
    def cumple(r):
        return r is not None and r["type"] in TIPOS_CARDIO and (r["duration_s"] or 0) >= MIN_CARDIO_S
    return cumple(row) or cumple(pareja)


def _sesiones_cardio(conn, person_id: int, desde: date | None = None, hasta: date | None = None) -> list[dict]:
    """Ruling 2's own cardio measure: an `activity` row (or its linked manual pair -- counted
    once, the same dedup entreno_racha.sesiones_fuerza applies to strength) whose type is in
    TIPOS_CARDIO and whose duration_s >= MIN_CARDIO_S. One SQL statement over this person's whole
    activity history, regardless of the range -- same reasoning as entreno_racha.sesiones_fuerza
    (ruling 1): callers slice by date in Python."""
    filas = conn.execute(
        "SELECT source, source_id, day, type, duration_s, start_local, vinculo_manual FROM activity "
        "WHERE person_id = ?", (person_id,),
    ).fetchall()
    por_clave = {(r["source"], r["source_id"]): r for r in filas}
    vinculadas = {r["vinculo_manual"] for r in filas if r["vinculo_manual"]}
    sesiones = []
    for r in filas:
        if r["source"] == "manual" and r["source_id"] in vinculadas:
            continue
        pareja = por_clave.get(("manual", r["vinculo_manual"])) if r["vinculo_manual"] else None
        if not _es_cardio(r, pareja):
            continue
        start = r["start_local"] or (pareja["start_local"] if pareja else None)
        sesiones.append({"day": _parse(r["day"]), "start_local": start})
    if desde is not None:
        sesiones = [s for s in sesiones if s["day"] >= desde]
    if hasta is not None:
        sesiones = [s for s in sesiones if s["day"] <= hasta]
    return sesiones


def _planes_hechos_fuerza(conn, person_id: int, desde: date, hasta: date) -> list[date]:
    """BODY-PLAN-SESION-HECHA-1 rulings 1/2: every day in [desde, hasta] carrying a `plan` row of
    this person with status 'done' AND exercises (`ejercicios_json`) -- the plan's own word about a
    strength session, counted only when there is something to say what was done. One statement,
    bounded like every other `_recopilar_datos` read; never scoped to one horizon's `goal_id`, the
    same way a measured session isn't -- a done session is a fact about the person's day,
    regardless of which plan (or none) it was written under."""
    return [_parse(r["day"]) for r in conn.execute(
        "SELECT day FROM plan WHERE person_id = ? AND status = 'done' AND ejercicios_json IS NOT NULL "
        "AND day BETWEEN ? AND ?", (person_id, desde.isoformat(), hasta.isoformat())
    )]


def _recopilar_datos(conn, person_id: int, desde: date, hasta: date) -> dict:
    """Every raw data source a medida's real figure can come from, fetched ONCE for [desde,
    hasta] -- ruling 1's bounded statement count: one combined daily_metric read (every key, not
    one query per medida) plus one sleep_session read, one strength-classifier call, one
    cardio-classifier call and one done-plan-day read (BODY-PLAN-SESION-HECHA-1), independent of
    how many weekly lines or checkpoints use them. `fuerza_con_plan` is `fuerza` unioned with those
    done-plan days (entreno_racha.con_plan_hecho) -- the only one of these `_real_medida` reads for
    `sesiones_fuerza`; `fuerza` itself stays the watch-only list every other reader (the week's
    rules) already uses."""
    metricas: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT day, metric_key, value FROM daily_metric WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, desde.isoformat(), hasta.isoformat())
    ):
        metricas.setdefault(r["day"], {})[r["metric_key"]] = r["value"]
    fuerza = entreno_racha.sesiones_fuerza(conn, person_id, desde, hasta)
    return {
        "metricas": metricas,
        "sueno": _sesiones_sueno(conn, person_id, desde, hasta),
        "fuerza": fuerza,
        "fuerza_con_plan": entreno_racha.con_plan_hecho(
            fuerza, _planes_hechos_fuerza(conn, person_id, desde, hasta)),
        "cardio": _sesiones_cardio(conn, person_id, desde, hasta),
    }


def _real_medida(datos: dict, medida: str, dias: list[str]) -> dict:
    """The real figure for `medida` over exactly `dias` -- ruling 2's own method per medida, pure
    Python over `datos` (ruling 9: a day with no reading is left out, never zeroed; 'real' is None
    -- an honest gap -- only when NOT ONE day in `dias` has a reading)."""
    if medida == "sueno_minutos":
        vals = [v for v in (datos["metricas"].get(d, {}).get("sleep_minutes") for d in dias) if v is not None]
        return {"real": statistics.fmean(vals) if vals else None, "dias_con_dato": len(vals), "tipo": "media"}
    if medida in ("hora_acostarse", "hora_levantarse"):
        campo = "start_local" if medida == "hora_acostarse" else "end_local"
        valores = []
        for d in dias:
            s = datos["sueno"].get(d)
            if s and s[campo]:
                valores.append(_minutos_despues_mediodia(*_parse_hora_local(s[campo])))
        if not valores:
            return {"real": None, "dias_con_dato": 0, "valores": [], "tipo": "mediana"}
        return {"real": statistics.median(valores), "dias_con_dato": len(valores), "valores": valores,
                "tipo": "mediana"}
    if medida == "sesiones_fuerza":
        # BODY-PLAN-SESION-HECHA-1 ruling 1/2: `fuerza_con_plan`, never the measured-only `fuerza`
        # -- a done plan row with exercises counts once on a day with no measured session; a
        # cardio measure has no such fallback, it stays measured only (no plan row ever says
        # cardio was done, so none reaches `fuerza_con_plan` or this branch).
        n = sum(1 for s in datos["fuerza_con_plan"] if s["day"].isoformat() in dias)
        return {"real": n, "dias_con_dato": len(dias), "tipo": "cuenta"}
    if medida == "sesiones_cardio":
        n = sum(1 for s in datos["cardio"] if s["day"].isoformat() in dias)
        return {"real": n, "dias_con_dato": len(dias), "tipo": "cuenta", "zona_medida": False}
    if medida == "minutos_intensidad":
        vals = [v for v in (datos["metricas"].get(d, {}).get("intensity_minutes") for d in dias) if v is not None]
        return {"real": sum(vals) if vals else None, "dias_con_dato": len(vals), "tipo": "suma"}
    # Any other metric_def key (validar_medida's else branch): the window's mean.
    vals = [v for v in (datos["metricas"].get(d, {}).get(medida) for d in dias) if v is not None]
    return {"real": statistics.fmean(vals) if vals else None, "dias_con_dato": len(vals), "tipo": "media"}


def _estado_cuenta(real: float, sentido: str, valor: float, dias_restantes: int, semana_terminada: bool) -> str:
    """Ruling 4: a count/sum line's estado. al_menos reuses goals.estado_cuota, THE reachability
    rule; como_maximo has no reachability question -- it is already broken once `real` passes
    `valor`, regardless of days left."""
    if sentido == "al_menos":
        if semana_terminada:
            return "cumple" if real >= valor else "no_cumple"
        mapa = {"cumplida": "cumple", "en_curso": "en_curso", "no_alcanzable": "no_cumple"}
        return mapa[goals.estado_cuota(real, valor, dias_restantes)]
    if real > valor:
        return "no_cumple"
    return "cumple" if semana_terminada else "en_curso"


def _linea(datos: dict, medida: str, sentido: str, valor: float, dias: list[str], *,
           dias_restantes: int, semana_terminada: bool) -> dict:
    """One weekly-target line's plan-vs-real (ruling 3/4/9): sin_dato when nothing was measured;
    for a mean/median medida, en_curso while the week is open (no reachability guess on an
    average, ruling 4); for a count/sum medida, en_curso/no_cumple/cumple via `_estado_cuenta`."""
    r = _real_medida(datos, medida, dias)
    out = {"medida": medida, "sentido": sentido, "valor": valor, "real": r["real"],
           "dias_con_dato": r["dias_con_dato"]}
    if "zona_medida" in r:
        out["zona_medida"] = r["zona_medida"]
    if r["tipo"] in ("media", "mediana"):
        if r["real"] is None:
            out["estado"] = "sin_dato"
        elif not semana_terminada:
            out["estado"] = "en_curso"
        else:
            out["estado"] = "cumple" if _cumple_valor(r["real"], sentido, valor) else "no_cumple"
        if r["tipo"] == "mediana":
            valores = r.get("valores", [])
            out["noches_con_dato"] = len(valores)
            out["noches_cumplen"] = sum(1 for v in valores if _cumple_valor(v, sentido, valor))
    else:
        out["estado"] = "sin_dato" if r["real"] is None else _estado_cuenta(
            r["real"], sentido, valor, dias_restantes, semana_terminada)
    return out


def _recopilar_reglas(conn, horizonte_goal_id: int, person_id: int, desde: date, hasta: date) -> dict:
    """Every rule-context input for [desde, hasta], fetched ONCE (ruling 1/6): the horizon's own
    active rules, the range's confirmed shifts, its marked periods, and -- for every distinct
    'tras_noche' day count a rule names -- ONE `bloques_noche` call over the range padded by the
    largest N, never a `contexto_dia` call per day."""
    reglas = conn.execute(
        "SELECT id, contexto, contexto_valor, texto, chequeo, orden FROM horizonte_regla "
        "WHERE horizonte_goal_id = ? AND activa = 1 ORDER BY orden, id",
        (horizonte_goal_id,),
    ).fetchall()
    shifts = {r["day"]: r["kind"] for r in conn.execute(
        "SELECT day, kind FROM shift WHERE person_id = ? AND day BETWEEN ? AND ?",
        (person_id, desde.isoformat(), hasta.isoformat()))}
    periodos = conn.execute(
        "SELECT kind, start_day, end_day FROM period WHERE person_id = ? AND start_day <= ? "
        "AND (end_day IS NULL OR end_day >= ?)",
        (person_id, hasta.isoformat(), desde.isoformat())
    ).fetchall()

    max_n = max((int(r["contexto_valor"]) for r in reglas if r["contexto"] == "tras_noche"), default=0)
    bloques = bloques_noche(conn, person_id, (desde - timedelta(max_n)).isoformat(), hasta.isoformat()) \
        if max_n else []
    tras_noche_dias: dict[int, set[str]] = {}
    for r in reglas:
        if r["contexto"] == "tras_noche":
            n = int(r["contexto_valor"])
            dias: set[str] = set()
            for b in bloques:
                fin = _parse(b.fin)
                for i in range(1, n + 1):
                    dias.add((fin + timedelta(i)).isoformat())
            tras_noche_dias[r["id"]] = dias

    return {"reglas": reglas, "shifts": shifts, "periodos": periodos, "tras_noche_dias": tras_noche_dias}


def _regla_aplica(regla, dia_iso: str, info: dict) -> bool | None:
    """Ruling 6: whether `dia_iso` is one of this rule's days. None only for 'descanso' with no
    confirmed shift row at all that day -- UNKNOWN, never assumed rest."""
    contexto = regla["contexto"]
    if contexto == "siempre":
        return True
    if contexto == "turno":
        return info["shifts"].get(dia_iso) == regla["contexto_valor"]
    if contexto == "descanso":
        kind = info["shifts"].get(dia_iso)
        return None if kind is None else kind == "off"
    if contexto == "tras_noche":
        return dia_iso in info["tras_noche_dias"].get(regla["id"], set())
    if contexto == "periodo":
        return any(p["kind"] == regla["contexto_valor"] and p["start_day"] <= dia_iso
                   and (p["end_day"] is None or p["end_day"] >= dia_iso) for p in info["periodos"])
    raise ValueError(f"contexto: unknown {contexto!r}")


def _chequeo_cumple(chequeo: dict, dia_iso: str, datos: dict) -> bool | None:
    """Ruling 6: whether this rule's chequeo is met on `dia_iso`. None = sin_dato -- a day this
    rule applies to, but with no reading to judge it by."""
    tipo = chequeo["tipo"]
    if tipo in ("acostarse_antes", "levantarse_antes"):
        campo = "start_local" if tipo == "acostarse_antes" else "end_local"
        s = datos["sueno"].get(dia_iso)
        if not s or not s[campo]:
            return None
        real = _minutos_despues_mediodia(*_parse_hora_local(s[campo]))
        objetivo = _minutos_despues_mediodia(*_parse_hhmm(chequeo["hora"]))
        return real <= objetivo
    if tipo == "sueno_minimo":
        v = datos["metricas"].get(dia_iso, {}).get("sleep_minutes")
        return None if v is None else v >= chequeo["minutos"]
    if tipo == "sin_sesion":
        clase = chequeo["clase"]
        tiene_fuerza = any(s["day"].isoformat() == dia_iso for s in datos["fuerza"])
        tiene_cardio = any(s["day"].isoformat() == dia_iso for s in datos["cardio"])
        if clase == "fuerza":
            return not tiene_fuerza
        if clase == "cardio":
            return not tiene_cardio
        return not (tiene_fuerza or tiene_cardio)
    if tipo == "sesion_antes_de":
        clase = chequeo["clase"]
        candidatos = []
        if clase in ("fuerza", "cualquiera"):
            candidatos += [s for s in datos["fuerza"] if s["day"].isoformat() == dia_iso]
        if clase in ("cardio", "cualquiera"):
            candidatos += [s for s in datos["cardio"] if s["day"].isoformat() == dia_iso]
        if not candidatos:
            return False  # no such session at all that day: the rule is not met, not unknown
        inicios = [s["start_local"] for s in candidatos if s["start_local"]]
        if not inicios:
            return None  # a session happened but its start time was never recorded
        objetivo = _minutos_despues_mediodia(*_parse_hhmm(chequeo["hora"]))
        return min(_minutos_despues_mediodia(*_parse_hora_local(s)) for s in inicios) <= objetivo
    raise ValueError(f"chequeo.tipo: unknown {tipo!r}")


def _evaluar_regla(regla, dias: list[str], info: dict, datos: dict) -> dict:
    chequeo = json.loads(regla["chequeo"]) if regla["chequeo"] else None
    aplica = cumple = no_cumple = sin_dato = 0
    detalle: list[dict] = []
    for d in dias:
        ap = _regla_aplica(regla, d, info)
        if ap is False:
            continue
        aplica += 1
        if ap is None:
            sin_dato += 1
            detalle.append({"day": d, "cumple": None})
            continue
        if chequeo is None:
            detalle.append({"day": d, "cumple": None})
            continue
        c = _chequeo_cumple(chequeo, d, datos)
        if c is None:
            sin_dato += 1
        elif c:
            cumple += 1
        else:
            no_cumple += 1
        detalle.append({"day": d, "cumple": c})
    return {"regla_id": regla["id"], "contexto": regla["contexto"], "contexto_valor": regla["contexto_valor"],
            "texto": regla["texto"], "solo_texto": chequeo is None, "dias_aplica": aplica,
            "dias_cumple": cumple, "dias_no_cumple": no_cumple, "dias_sin_dato": sin_dato, "dias": detalle}


def _semana_desde_datos(horizonte_goal_id: int, monday: date, datos: dict, reglas_info: dict,
                         metas_por_semana: dict, semanas_titulo: dict, planes_por_dia: dict,
                         hoy: date) -> dict:
    sunday = monday + timedelta(6)
    dias = _dias_semana(monday)
    semana_terminada = hoy > sunday
    dias_restantes = 0 if semana_terminada else max(0, (sunday - hoy).days + 1)

    titulo_row = semanas_titulo.get(monday.isoformat())
    lineas = []
    for m in metas_por_semana.get(monday.isoformat(), []):
        linea = _linea(datos, m["medida"], m["sentido"], m["valor"], dias,
                        dias_restantes=dias_restantes, semana_terminada=semana_terminada)
        linea["meta_id"] = m["id"]
        linea["nota"] = m["nota"]
        lineas.append(linea)

    sesiones_planeadas = []
    hechas = 0
    for d in dias:
        for p in planes_por_dia.get(d, []):
            sesiones_planeadas.append(p)
            if p["status"] == "done":
                hechas += 1

    reglas = [_evaluar_regla(r, dias, reglas_info, datos) for r in reglas_info["reglas"]]

    return {
        "horizonte_goal_id": horizonte_goal_id, "week_start": monday.isoformat(),
        "titulo": titulo_row["titulo"] if titulo_row else None,
        "nota": titulo_row["nota"] if titulo_row else None,
        "lineas": lineas,
        "sesiones_planeadas": sesiones_planeadas, "sesiones_hechas": hechas,
        "sesiones_total": len(sesiones_planeadas),
        "reglas": reglas,
    }


def plan_vs_real(conn, horizonte_goal_id: int, person_id: int, desde: str, hasta: str,
                  hoy: date | None = None) -> list[dict]:
    """Ruling 3: `semana`'s own body, for every Monday-starting week whose range touches [desde,
    hasta], in ONE bounded set of statements regardless of how many weeks that is (ruling 1) --
    every data source is read once over the whole padded range and grouped per week in Python."""
    hoy = hoy or date.today()
    desde_m = fechas.lunes_de(_parse(desde))
    hasta_m = fechas.lunes_de(_parse(hasta))
    rango_desde, rango_hasta = desde_m, hasta_m + timedelta(6)

    datos = _recopilar_datos(conn, person_id, rango_desde, rango_hasta)
    reglas_info = _recopilar_reglas(conn, horizonte_goal_id, person_id, rango_desde, rango_hasta)

    semanas_titulo = {r["week_start"]: r for r in conn.execute(
        "SELECT week_start, titulo, nota FROM horizonte_semana WHERE horizonte_goal_id = ? "
        "AND week_start BETWEEN ? AND ?",
        (horizonte_goal_id, rango_desde.isoformat(), rango_hasta.isoformat()))}

    metas_por_semana: dict[str, list] = {}
    for r in conn.execute(
        "SELECT id, week_start, medida, sentido, valor, nota FROM horizonte_meta_semanal "
        "WHERE horizonte_goal_id = ? AND week_start BETWEEN ? AND ? AND vigente = 1",
        (horizonte_goal_id, rango_desde.isoformat(), rango_hasta.isoformat())
    ):
        metas_por_semana.setdefault(r["week_start"], []).append(dict(r))

    planes_por_dia: dict[str, list] = {}
    for r in conn.execute(
        "SELECT id, day, time, title, status FROM plan WHERE goal_id = ? AND person_id = ? "
        "AND day BETWEEN ? AND ?",
        (horizonte_goal_id, person_id, rango_desde.isoformat(), rango_hasta.isoformat())
    ):
        planes_por_dia.setdefault(r["day"], []).append(dict(r))

    out = []
    m = desde_m
    while m <= hasta_m:
        out.append(_semana_desde_datos(horizonte_goal_id, m, datos, reglas_info, metas_por_semana,
                                        semanas_titulo, planes_por_dia, hoy))
        m += timedelta(7)
    return out


def semana(conn, horizonte_goal_id: int, person_id: int, week_start: str, hoy: date | None = None) -> dict:
    """Ruling 3: one week's plan vs real. `week_start` must be a Monday (db.crear_semana_horizonte's
    own rule)."""
    monday = _parse(week_start)
    if monday != fechas.lunes_de(monday):
        raise ValueError(f"week_start: {week_start!r} is not a Monday")
    return plan_vs_real(conn, horizonte_goal_id, person_id, week_start, week_start, hoy=hoy)[0]


def checkpoints(conn, horizonte_goal_id: int, person_id: int, hoy: date | None = None) -> list[dict]:
    """Ruling 7: every checkpoint for this horizon, each criterion evaluated against its own
    ventana ('semana' = the week containing the checkpoint day; 'desde_inicio' = the horizon's own
    start_day up to the checkpoint day). Nothing is stored -- a snapshot, if ever, is a later
    slice. One combined data fetch over the whole span every checkpoint/criterion needs, so the
    statement count stays bounded regardless of how many checkpoints or criteria there are."""
    hoy = hoy or date.today()
    goal = conn.execute("SELECT start_day FROM goal WHERE id = ?", (horizonte_goal_id,)).fetchone()
    inicio = _parse(goal["start_day"]) if goal else hoy

    filas = conn.execute(
        "SELECT id, day, titulo, criterios, nota FROM horizonte_checkpoint "
        "WHERE horizonte_goal_id = ? ORDER BY day, id", (horizonte_goal_id,)
    ).fetchall()
    if not filas:
        return []

    ultimo_dia = max(_parse(f["day"]) for f in filas)
    rango_desde = min(inicio, ultimo_dia, hoy)
    rango_hasta = max(ultimo_dia, hoy)
    datos = _recopilar_datos(conn, person_id, rango_desde, rango_hasta)

    out = []
    for f in filas:
        dia = _parse(f["day"])
        criterios = json.loads(f["criterios"])
        resultado = {"id": f["id"], "day": f["day"], "titulo": f["titulo"], "nota": f["nota"]}
        if dia > hoy:
            resultado["estado"] = "pendiente"
            resultado["criterios"] = [
                {"medida": c["medida"], "sentido": c["sentido"], "valor": c["valor"],
                 "ventana": c["ventana"], "real": None, "estado": None} for c in criterios]
            out.append(resultado)
            continue

        detalle = []
        hubo_falla = False
        hubo_gap = False
        for c in criterios:
            if c["ventana"] == "semana":
                dias_ventana = _dias_semana(fechas.lunes_de(dia))
            else:
                dias_ventana = [(inicio + timedelta(i)).isoformat() for i in range((dia - inicio).days + 1)]
            r = _real_medida(datos, c["medida"], dias_ventana)
            real = r["real"]
            if real is None:
                estado_c = "sin_dato"
                hubo_gap = True
            elif _cumple_valor(real, c["sentido"], c["valor"]):
                estado_c = "cumple"
            else:
                estado_c = "no_cumple"
                hubo_falla = True
            detalle.append({"medida": c["medida"], "sentido": c["sentido"], "valor": c["valor"],
                             "ventana": c["ventana"], "real": real, "estado": estado_c})
        resultado["criterios"] = detalle
        resultado["estado"] = "no_cumple" if hubo_falla else ("sin_dato" if hubo_gap else "cumple")
        out.append(resultado)
    return out


# BODY-PLAN-HOY-1 ruling 1: the ONE definition of a plan, read by every caller that used to decide
# this on its own (herramientas.py's `_leer_planes`, metas.py's own "is this a horizon" test). A
# UNION of every table a plan row can live in, never a per-candidate query -- `es_horizonte` and
# `horizontes` below each fold it into their own single statement.
_PLAN_ROW_HORIZONTES = (
    "SELECT horizonte_goal_id FROM goal WHERE horizonte_goal_id IS NOT NULL "
    "UNION SELECT horizonte_goal_id FROM horizonte_regla "
    "UNION SELECT horizonte_goal_id FROM horizonte_semana "
    "UNION SELECT horizonte_goal_id FROM horizonte_meta_semanal "
    "UNION SELECT horizonte_goal_id FROM horizonte_checkpoint"
)


def es_horizonte(conn, goal_id: int) -> bool:
    """A goal is a plan (a horizon) when it is a milestone with no horizonte_goal_id of its own AND
    it carries at least one plan row -- a child goal, a rule, a week, a weekly target line or a
    checkpoint. One bounded statement, independent of how many plan rows this goal or any other
    one has."""
    return bool(conn.execute(
        f"SELECT EXISTS(SELECT 1 FROM goal WHERE id = ? AND kind = 'milestone' "
        f"AND horizonte_goal_id IS NULL AND id IN ({_PLAN_ROW_HORIZONTES}))",
        (goal_id,),
    ).fetchone()[0])


def horizontes(conn, person_id: int, hoy: date, *, solo_activos: bool = False) -> list[int]:
    """Every goal id `person_id` can see -- their own, plus any shared household milestone, the
    same visibility rule `goals.goal_scope_sql` names -- that is a horizon by `es_horizonte`'s own
    rule, active plans first, then newest. `solo_activos` additionally requires status 'active' and
    the horizon's own span to cover `hoy`.

    BODY-PLAN-HOY-1 ruling 3: Today calls this on every view, so a person with no plan must pay at
    most one extra statement for it -- too little room for `goals.goal_scope_sql`'s own two-call
    shape (`household.household_of` first, then the listing). The household lookup is folded into
    this SAME statement instead, as a scalar subquery: a person with no household row makes it
    NULL, and 'hogar_id = NULL' is never true, so the OR's household branch drops out on its own --
    the identical net effect goal_scope_sql gets by skipping that branch's SQL text entirely when
    `household_of` returns None."""
    extra = ""
    params: tuple = (person_id, person_id)
    if solo_activos:
        extra = " AND status = 'active' AND start_day <= ? AND deadline >= ?"
        params = params + (hoy.isoformat(), hoy.isoformat())
    rows = conn.execute(
        "SELECT id FROM goal WHERE (person_id = ? OR (visibility = 'household' AND kind = 'milestone' "
        "AND hogar_id = (SELECT hogar_id FROM hogar_miembro WHERE person_id = ?))) "
        f"AND kind = 'milestone' AND horizonte_goal_id IS NULL{extra} "
        f"AND id IN ({_PLAN_ROW_HORIZONTES}) ORDER BY (status = 'active') DESC, id DESC",
        params,
    ).fetchall()
    return [r["id"] for r in rows]


def resumen(conn, horizonte_goal_id: int, person_id: int, hoy: date | None = None) -> dict | None:
    """Ruling 8: the horizon goal, its child goals (each with their OWN existing
    goals.evaluar_semana verdict -- never recomputed here), its experiments, this week (ruling 3),
    the next checkpoint and the days left to the deadline. A child goal of another person
    (household horizon) is read through goals.goal_access, never a raw query that skips it."""
    hoy = hoy or date.today()
    goal = conn.execute("SELECT * FROM goal WHERE id = ?", (horizonte_goal_id,)).fetchone()
    if goal is None:
        return None

    this_monday = fechas.lunes_de(hoy).isoformat()
    hijos = []
    for h in conn.execute("SELECT id FROM goal WHERE horizonte_goal_id = ?", (horizonte_goal_id,)):
        acceso = goals.goal_access(conn, h["id"], person_id)
        if acceso is None:
            continue
        verdict = goals.evaluar_semana(conn, h["id"], person_id, this_monday)
        hijos.append({"goal_id": h["id"], "acceso": acceso, "verdict": verdict})

    experimentos = [dict(r) for r in conn.execute(
        "SELECT id, titulo, estado FROM experimento WHERE goal_id = ? AND person_id = ?",
        (horizonte_goal_id, person_id))]

    semana_actual = semana(conn, horizonte_goal_id, person_id, this_monday, hoy=hoy)
    cps = checkpoints(conn, horizonte_goal_id, person_id, hoy=hoy)
    siguiente_checkpoint = next((c for c in cps if c["estado"] == "pendiente"), None)

    dias_a_deadline = (_parse(goal["deadline"]) - hoy).days if goal["deadline"] else None

    return {
        "goal_id": horizonte_goal_id, "titulo": goal["title"], "start_day": goal["start_day"],
        "deadline": goal["deadline"], "dias_a_deadline": dias_a_deadline, "hijos": hijos,
        "experimentos": experimentos, "semana_actual": semana_actual,
        "siguiente_checkpoint": siguiente_checkpoint,
    }
