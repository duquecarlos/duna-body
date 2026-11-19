"""Historial (BODY-WEB-ESTRUCTURA-7): the `/history` screen -- coverage, training, patterns and
the by-shift breakdown. BODY-HISTORIA-VIDA-2 ruling 2 added "Tus prácticas" to `/history` itself
and its own drill-down, `/history/practica/{practica_id}` (+ its archive action). BODY-HISTORIA-
VIDA-3 made the drill-down's own main visual the whole history in WEEKS (ruling 1) and added an
edit form (ruling 2).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import db, history, identity, practicas, recuperacion
from ..i18n import T
from . import comun

router = APIRouter()

# BODY-HISTORIA-VIDA-3 ruling 1: "Últimas 4 semanas" bounds the drill-down's own RECENT day grid to
# the last 4 weeks -- the whole history's own per-week rollup (practicas.estado()'s own "semanas",
# never bounded) is now the main visual above it.
_PRACTICAS_DIAS_RECIENTES = 28
# Ruling 1: rows of 13 weeks (a quarter) each, labelled at their own start.
_SEMANAS_POR_FILA = 13

# BODY-RECUPERACION-1: each factor's "peor" group label, read through its own i18n key -- 'turno'
# and 'tipo_sesion' are handled separately below (their group key is data: a shift kind or a
# Garmin activity type, each with its own existing label lookup, not a fixed phrase per key).
_RECOVERY_COND_KEYS = {
    "sueno_anoche": {"corta": "history.recovery.cond.sueno_corta", "normal": "history.recovery.cond.sueno_normal"},
    "hrv_anoche": {"baja": "history.recovery.cond.hrv_baja", "normal": "history.recovery.cond.hrv_normal"},
    "hora_dia": {"manana": "history.recovery.cond.hora_manana", "tarde": "history.recovery.cond.hora_tarde",
                 "noche": "history.recovery.cond.hora_noche", "desconocida": "history.recovery.cond.hora_desconocida"},
    "duracion_tercil": {"corta": "history.recovery.cond.duracion_corta", "media": "history.recovery.cond.duracion_media",
                         "larga": "history.recovery.cond.duracion_larga"},
}

# Fixed palette classes (body/static/body.css), never a var(...) string in markup (BODY-ESTILOS-1).
TRAINING_COLOR_CLASSES = ["c-body", "c-night", "c-amber", "c-info", "c-est"]
OTHER_COLOR_CLASS = "c-other"
TRAINING_RECENT_MONTHS = 12


def _kind_label(t: T, info: dict | None) -> str:
    if not info:
        return "—"
    return t("activity.type." + info["key"]) if info["known"] else info["fallback_label"]


def _color_map(types: list[dict]) -> dict[str, str]:
    colors, i = {}, 0
    for ty in types:
        if ty["key"] == "other":
            colors["other"] = OTHER_COLOR_CLASS
        else:
            colors[ty["key"]] = TRAINING_COLOR_CLASSES[i % len(TRAINING_COLOR_CLASSES)]
            i += 1
    return colors


def _training_ctx(t: T, train: dict) -> dict:
    colors = _color_map(train["types"])
    types = [{"label": _kind_label(t, ty), "sessions": t.num(ty["sessions"]), "minutes": t.minutes(ty["minutes"]),
              "km": f"{t.num(ty['km'], 1)} km" if ty["km"] is not None else None, "cls": colors[ty["key"]]}
             for ty in train["types"]]
    max_minutes = train["max_month_minutes"] or 0.0
    months = []
    for m in train["months"]:
        segs = [{"label": _kind_label(t, seg), "cls": colors.get(seg["key"], OTHER_COLOR_CLASS),
                 "pct": (seg["minutes"] / max_minutes * 100) if max_minutes else 0.0} for seg in m["by_type"]]
        months.append({"month": t.month_year(date.fromisoformat(m["month"] + "-01")),
                       "href": f"/trends?m={m['month']}", "sessions": t.num(m["sessions"]),
                       "minutes": t.minutes(m["minutes"]), "segs": segs})
    months.reverse()  # newest first (BODY-HISTORIA-POLISH-1): training() builds them oldest first
    yoy, yoy_ctx = train["yoy"], None
    if yoy:
        yoy_ctx = {"now": t("history.training_yoy_now", sessions=t.num(yoy["sessions_now"]),
                             minutes=t.minutes(yoy["minutes_now"])),
                   "prev": t("history.training_yoy_prev", sessions=t.num(yoy["sessions_year_ago"]),
                             minutes=t.minutes(yoy["minutes_year_ago"]))}
    return {"types": types, "months": months[:TRAINING_RECENT_MONTHS], "months_older": months[TRAINING_RECENT_MONTHS:],
            "usual_week": (t("history.training_usual_week", n=t.num(train["usual_week_sessions"]))
                           if train["usual_week_sessions"] is not None else None),
            "longest": _kind_label(t, train["longest_kind"]) if train["longest_kind"] else None,
            "frequent": _kind_label(t, train["most_frequent_kind"]) if train["most_frequent_kind"] else None,
            "yoy": yoy_ctx}


def _finding_text(t: T, defs: dict, f: dict) -> str:
    """A plain sentence, not "Your toughest {label}": the metric in its sentence form (never title case
    mid-sentence) and the direction said from `better` -- a "lower is better" metric reads worst where it
    runs highest, a "higher is better" metric reads worst where it runs lowest."""
    metric = t("metric." + f["metric"] + ".sentence")
    value = comun._value(t, f["metric"], f["value"])
    unit = comun._unit(t, defs[f["metric"]])
    better = defs[f["metric"]]["better"]
    key = f"history.find.{f['axis']}_better_{better}"
    if f["axis"] == "weekday":
        return t(key, metric=metric, day=t("date.days")[f["index"]], value=value, unit=unit)
    return t(key, metric=metric, month=t("date.months")[f["index"]], value=value, unit=unit)


def _pattern_rows(t: T, defs: dict, pat: dict) -> dict | None:
    present = [k for k in history.PATTERN_KEYS if pat["metrics"].get(k)]
    if not present:
        return None
    marks = {(f["metric"], f["axis"], f["index"]) for f in pat["findings"]}

    def row(axis: str, idx: int, label: str) -> dict:
        vals = []
        for k in present:
            b = pat["metrics"][k][axis][idx]
            vals.append({"text": comun._value(t, k, b["center"]) if b else "—", "mark": (k, axis, idx) in marks})
        return {"label": label, "vals": vals}

    return {"heads": [t("trends.short." + k) for k in present],
            "weekday": [row("weekday", i, t("date.days")[i][:3].capitalize()) for i in range(7)],
            "month": [row("month", i, t("date.months")[i][:3].capitalize()) for i in range(12)]}


def _periodo_excluido_ctx(t: T, conn, person_id: int, cov: dict, marcados: set[str]) -> str | None:
    """BODY-PERIODO-PANTALLAS-1: one honest line for the whole page -- how many days across the
    person's own watch history were marked (illness, travel, injury) and so left out of patterns()
    and by_shift() above (history.py's own exclusion, not re-derived here), and which kinds. None
    of this is the person's own note text (DESIGN: never rendered outside the assistant panel).
    `marcados` is read once by the route (patterns()'s own docstring says why), not re-read here."""
    if not marcados:
        return None
    periodos_rows = db.periodos(conn, person_id, cov["first_day"], cov["last_day"])
    kinds = sorted({t("periodo.kind." + p["kind"]) for p in periodos_rows})
    n = len(marcados)
    key = "history.periodo_excluido_one" if n == 1 else "history.periodo_excluido"
    return t(key, n=t.num(n), kinds=", ".join(kinds))


def _recovery_cond_label(t: T, factor: str, grupo: str) -> str:
    """One group's own condition phrase, worded so it reads naturally both as the sentence's
    opening ("Después de {label}, ..." / "After {label}, ...") and after its second "después de" /
    "after" (BODY-RECUPERACION-1-M point 2: the finding now names both the worse AND the better
    group). Shared between `h["peor"]` and `h["mejor"]` -- neither is special to this function."""
    if factor == "turno":
        if grupo == "off":
            return t("history.recovery.cond.turno_descanso")
        return t("history.recovery.cond.turno", kind=t("shift.kind." + grupo).lower())
    if factor == "tipo_sesion":
        info = history._kind_info(grupo) if grupo != "otro" else None
        kind_label = (_kind_label(t, info) if info else t("activity.generic")).lower()
        return t("history.recovery.cond.tipo_sesion", kind=kind_label)
    return t(_RECOVERY_COND_KEYS[factor][grupo])


def _recovery_valor(t: T, mediana: float | None, horas: int) -> str:
    """One finding's own group value, worded (BODY-RECUPERACION-1-C point 2 applies inside every
    group too): an exact minutes figure when that group's own median is defined, else the same
    honest "more than the horizon" MODELO already gives the overall summary."""
    if mediana is not None:
        return t.minutes(mediana)
    return t("history.recovery.mas_de_valor", horas=t.num(horas))


def _recovery_ctx(t: T, est: dict) -> dict:
    """«Tu historia»'s "Recuperación después de entrenar" section (BODY-RECUPERACION-1,
    BODY-RECUPERACION-1-C): one plain line with the typical minutes and how many sessions it
    rests on -- naming how many never came back within the horizon when any did (point 2), the
    two or three largest group differences as plain sentences, "pocos datos" when none qualify,
    and one line that caffeine, food and what he did after will count once he tells Body --
    always, regardless of whether a finding was found. `est["n"] == 0` -- no derived figure yet at
    all -- is its own, simpler state."""
    if est["n"] == 0:
        return {"tiene_datos": False}
    horas = recuperacion.MODELO["horizonte_h"]
    hallazgos = [
        t("history.recovery.hallazgo", peor=_recovery_cond_label(t, h["factor"], h["peor"]),
          mejor=_recovery_cond_label(t, h["factor"], h["mejor"]),
          val_peor=_recovery_valor(t, h["min_peor"], horas), val_mejor=_recovery_valor(t, h["min_mejor"], horas),
          n_peor=t.num(h["n_peor"]), n_mejor=t.num(h["n_mejor"]))
        for h in est["hallazgos"]
    ]
    if est["mediana"] is None:
        resumen = t("history.recovery.resumen_mas_de", horas=t.num(horas), n=t.num(est["n"]))
    elif est["n_censurados"] == 0:
        resumen = t("history.recovery.resumen", mediana=t.minutes(est["mediana"]), n=t.num(est["n"]))
    else:
        key = ("history.recovery.resumen_censurado_one" if est["n_censurados"] == 1
               else "history.recovery.resumen_censurado")
        resumen = t(key, mediana=t.minutes(est["mediana"]), n=t.num(est["n"]),
                     n_censurados=t.num(est["n_censurados"]), horas=t.num(horas))
    return {
        "tiene_datos": True,
        "resumen": resumen,
        "hallazgos": hallazgos,
        "sin_hallazgos": t("history.recovery.sin_hallazgos") if not hallazgos else None,
        "futuro": t("history.recovery.futuro"),
    }


def _shift_ctx(t: T, defs: dict, shift_data: dict) -> dict:
    if not shift_data["has_shifts"]:
        return {"has_shifts": False}
    kinds = []
    for row in shift_data["kinds"]:
        metrics = [{"label": t("metric." + k), "value": comun._value(t, k, row["metrics"].get(k)),
                    "unit": comun._unit(t, defs[k]) if row["metrics"].get(k) is not None else ""}
                   for k in history.PATTERN_KEYS]
        kinds.append({"kind": row["kind"], "kind_label": t("shift.kind." + row["kind"]),
                      "days": t("history.days_count", n=t.num(row["days"])), "metrics": metrics})
    return {"has_shifts": True, "kinds": kinds}


# ---- "Tus prácticas" (BODY-HISTORIA-VIDA-2 ruling 2): what the watch shows the person does,
# never what they committed to -- that is a habit, body/rutas/metas.py's own screen. -------------

def _sugerencia_label(t: T, s: dict) -> str:
    if s["fuente"] == "actividad":
        return _kind_label(t, history._kind_info(s["clave"]))
    return t("metric." + s["clave"])


def _practicas_resumen_ctx(t: T, conn, person_id: int) -> list[dict]:
    """BODY-HISTORIA-VIDA-2-R2 ruling 1: `practicas.resumen()` reads each source family once for
    every active practice together, so this section's own cost is a constant, never one that grows
    with the number of practices (tests/test_rendimiento.py's own guard)."""
    out = []
    for p in practicas.resumen(conn, person_id):
        racha = p["racha_actual"]
        semana = p["ultima_semana"]
        partes = []
        if racha:
            key = "history.practicas_streak_one" if racha["dias"] == 1 else "history.practicas_streak"
            partes.append(t(key, n=t.num(racha["dias"])))
        if semana:
            partes.append(t("history.practicas_week", hecho=t.num(semana["hecho"]), meta=t.num(p["meta_dias_semana"])))
        out.append({"id": p["id"], "titulo": p["titulo"], "detalle": " · ".join(partes) or None})
    return out


def _sugerencias_ctx(t: T, conn, person_id: int) -> list[dict]:
    out = []
    for s in practicas.sugerencias(conn, person_id):
        label = _sugerencia_label(t, s)
        key = "history.practicas_suggest_weeks_one" if s["semanas"] == 1 else "history.practicas_suggest_weeks"
        out.append({"label": label, "fuente": s["fuente"], "regla_json": json.dumps(s["regla"], sort_keys=True),
                    "weeks": t(key, n=t.num(s["semanas"]))})
    return out


@router.post("/history/practicas")
@comun._cierra_conexion_async
async def history_practica_create(request: Request, person_id: int = Depends(identity.current_person_id)):
    """One tap from a suggestion `_sugerencias_ctx` already computed, as the person's own, direct
    creation (origen 'persona', practicas.crear's own default) -- never through the assistant."""
    conn, person, t = comun._ctx(person_id)
    f = await comun._form(request)
    fuente = f.get("fuente")
    titulo = (f.get("titulo") or "").strip()
    try:
        regla = json.loads(f.get("regla") or "")
    except ValueError:
        return RedirectResponse("/history", status_code=303)
    if fuente in practicas.FUENTES and titulo:
        practicas.crear(conn, person_id, titulo, fuente, regla)
    return RedirectResponse("/history", status_code=303)


def _regla_texto(t: T, fuente: str, regla: dict) -> str:
    if fuente == "actividad":
        tipos = ", ".join(_kind_label(t, history._kind_info(tp)) for tp in regla["tipos"])
        if regla.get("min_minutos"):
            return t("practica.regla_actividad_min", tipos=tipos, min=t.num(regla["min_minutos"]))
        return t("practica.regla_actividad", tipos=tipos)
    if fuente == "metrica_umbral":
        op = t("practica.op_mayor") if regla["op"] == ">=" else t("practica.op_menor")
        return t("practica.regla_metrica", metric=t("metric." + regla["metric_key"]), op=op, valor=t.num(regla["valor"]))
    campo = t("practica.campo_" + regla["campo"])
    op = t("practica.op_antes") if regla["op"] == "antes" else t("practica.op_despues")
    return t("practica.regla_sueno", campo=campo, op=op, hora=regla["hora"])


def _turnos_listado(t: T, turnos: dict[str, int]) -> str:
    """BODY-HISTORIA-VIDA-3 ruling 4: each shift kind with its own count and the matching singular
    ("1 día libre") or plural ("32 días libres") noun -- never the bare kind label the ruling found
    reading as a unit-less count ("32 descanso"). Highest count first, same order as before."""
    def nombre(k: str, v: int) -> str:
        if v == 1:
            return f"{t.num(v)} {t('shift.kind.noun.' + k)}"
        return f"{t.num(v)} {t('shift.kind.noun_plural.' + k)}"
    return ", ".join(nombre(k, v) for k, v in sorted(turnos.items(), key=lambda kv: -kv[1]))


def _viaje_dias_txt(t: T, n: int) -> str:
    return t("practica.contexto_viaje_one") if n == 1 else t("practica.contexto_viaje_other", n=n)


def _contexto_texto(t: T, ctx: dict) -> str | None:
    partes = []
    if ctx["turnos"]:
        partes.append(_turnos_listado(t, ctx["turnos"]))
    if ctx.get("viaje_dias"):
        partes.append(_viaje_dias_txt(t, ctx["viaje_dias"]))
    if ctx.get("sleep_minutes") is not None:
        partes.append(t("practica.contexto_sueno", minutos=t.minutes(ctx["sleep_minutes"])))
    if ctx.get("readiness") is not None:
        partes.append(t("practica.contexto_readiness", valor=t.num(ctx["readiness"])))
    return " · ".join(partes) if partes else None


def _dias_strip_ctx(t: T, dias: dict[str, str]) -> list[dict]:
    if not dias:
        return []
    recientes = sorted(dias)[-_PRACTICAS_DIAS_RECIENTES:]
    estado_word = {"hecho": t("practica.dia_hecho"), "no_hecho": t("practica.dia_no_hecho"),
                   "sin_dato": t("practica.dia_sin_dato")}
    primera = date.fromisoformat(recientes[0])
    cells = [{"day": None, "estado": None, "aria": None} for _ in range(primera.weekday())]
    for d in recientes:
        estado = dias[d]
        cells.append({"day": d, "estado": estado,
                      "aria": f"{comun._short_date(t, date.fromisoformat(d))}: {estado_word[estado]}"})
    return cells


def _temporada_ctx(t: T, tp: dict) -> dict:
    total = tp["semanas"] * 7
    resumen = t("practica.temporada_resumen", hecho=t.num(tp["dias_hecho"]), total=t.num(total),
                desde=comun._short_date(t, date.fromisoformat(tp["desde"])),
                hasta=comun._short_date(t, date.fromisoformat(tp["hasta"])))
    return {"resumen": resumen, "contexto": _contexto_texto(t, tp["contexto"]),
            "primer_dia": tp["desde"], "ultimo_dia": tp["hasta"]}


def _mes_anio_corto(t: T, d: date) -> str:
    """BODY-HISTORIA-VIDA-3 ruling 1: a weekly-strip row's own start label -- the same 3-letter
    month abbreviation comun._short_date already uses, without the day (a row is a stretch of
    weeks, not one date)."""
    return f"{t('date.months')[d.month - 1][:3]} {d.year}"


def _semanas_en_temporada(temporadas: list[dict]) -> set[str]:
    """Every week (by its own Monday, ISO) that falls inside any of `temporadas` -- ruling 1's "a
    season's weeks are outlined on the strip so a season is visible where it sits"."""
    out: set[str] = set()
    for tp in temporadas:
        lunes = date.fromisoformat(tp["desde"])
        hasta = date.fromisoformat(tp["hasta"])
        while lunes <= hasta:
            out.add(lunes.isoformat())
            lunes += timedelta(weeks=1)
    return out


def _semana_clase(semana: dict, meta: int) -> str:
    """Ruling 1's four week states, never a colour of failure: w-gap (too little watch coverage
    that week to say whether the goal was met -- the SAME `semana_sin_dato` flag practicas.py's own
    weekly rollup already carries), w-empty (0 done), w-partial (some, short of the goal), w-met
    (the goal or more)."""
    if semana["semana_sin_dato"]:
        return "w-gap"
    if semana["hecho"] >= meta:
        return "w-met"
    if semana["hecho"] > 0:
        return "w-partial"
    return "w-empty"


def _semanas_strip_ctx(t: T, e: dict) -> list[dict]:
    semanas = e["semanas"]
    if not semanas:
        return []
    meta = e["definicion"]["meta_dias_semana"]
    en_temporada = _semanas_en_temporada(e["temporadas"])
    celdas = []
    for s in semanas:
        lunes = s["lunes"]
        clase = _semana_clase(s, meta)
        if clase == "w-gap":
            aria = t("practica.semana_aria_gap", desde=comun._short_date(t, date.fromisoformat(lunes)))
        else:
            aria = t("practica.semana_aria", desde=comun._short_date(t, date.fromisoformat(lunes)),
                     hecho=t.num(s["hecho"]), meta=t.num(meta))
        celdas.append({"clase": clase, "en_temporada": lunes in en_temporada, "aria": aria})
    filas = []
    for i in range(0, len(celdas), _SEMANAS_POR_FILA):
        fila = celdas[i:i + _SEMANAS_POR_FILA]
        filas.append({"label": _mes_anio_corto(t, date.fromisoformat(semanas[i]["lunes"])), "celdas": fila})
    return filas


def _practica_detail_ctx(t: T, e: dict) -> dict:
    d = e["definicion"]
    racha_actual = e["racha_actual"]
    racha_mas_larga = e["racha_mas_larga"]
    racha_actual_txt = t("practica.sin_racha")
    if racha_actual:
        key = "practica.racha_actual_one" if racha_actual["dias"] == 1 else "practica.racha_actual"
        racha_actual_txt = t(key, n=t.num(racha_actual["dias"]))
    racha_larga_txt = None
    if racha_mas_larga:
        key = "practica.racha_mas_larga_one" if racha_mas_larga["dias"] == 1 else "practica.racha_mas_larga"
        racha_larga_txt = t(key, n=t.num(racha_mas_larga["dias"]),
                             desde=comun._short_date(t, date.fromisoformat(racha_mas_larga["desde"])),
                             hasta=comun._short_date(t, date.fromisoformat(racha_mas_larga["hasta"])))
    objetivo_key = "practica.objetivo_semanal_one" if d["meta_dias_semana"] == 1 else "practica.objetivo_semanal"
    baseline_contexto = _contexto_texto(t, e["baseline"]) if e["baseline"] else None
    return {
        "id": d["id"], "titulo": d["titulo"], "regla_texto": _regla_texto(t, d["fuente"], d["regla"]),
        "meta_dias_semana": d["meta_dias_semana"],
        "objetivo_semanal": t(objetivo_key, n=t.num(d["meta_dias_semana"])),
        "archivada": d["archivada_en"] is not None,
        "sin_datos": e["desde"] is None,
        "racha_actual": racha_actual_txt,
        "racha_mas_larga": racha_larga_txt,
        "semanas": _semanas_strip_ctx(t, e),
        "dias": _dias_strip_ctx(t, e["dias"]),
        "dias_letras": comun._week_letters(t),
        "temporadas": [_temporada_ctx(t, tp) for tp in e["temporadas"]],
        "sin_temporadas": t("practica.sin_temporadas") if e["desde"] is not None and not e["temporadas"] else None,
        "baseline": t("practica.baseline_sentence", contexto=baseline_contexto) if baseline_contexto else None,
    }


@router.get("/history/practica/{practica_id}")
@comun._cierra_conexion
def history_practica_detail(practica_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    e = practicas.estado(conn, person_id, practica_id)
    if e is None:
        raise HTTPException(404)
    return comun._render("practica.html", t=t, tab="trends", practica=_practica_detail_ctx(t, e),
                   turnos=db.trabaja_turnos_si(conn, person_id))


@router.post("/history/practica/{practica_id}")
@comun._cierra_conexion_async
async def history_practica_editar(practica_id: int, request: Request,
                                   person_id: int = Depends(identity.current_person_id)):
    """BODY-HISTORIA-VIDA-3 ruling 2: titulo and the weekly target, validated like `crear`
    (practicas.editar) -- fuente/regla never change here, that would be a different practice."""
    conn, person, t = comun._ctx(person_id)
    if not practicas.propia_activa(conn, person_id, practica_id):
        raise HTTPException(404)
    f = await comun._form(request)
    titulo = (f.get("titulo") or "").strip()
    try:
        meta_dias_semana = int(f.get("meta_dias_semana", ""))
    except ValueError:
        meta_dias_semana = -1
    practicas.editar(conn, person_id, practica_id, titulo, meta_dias_semana)
    return RedirectResponse(f"/history/practica/{practica_id}", status_code=303)


@router.post("/history/practica/{practica_id}/archive")
@comun._cierra_conexion
def history_practica_archive(practica_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    if not practicas.propia_activa(conn, person_id, practica_id):
        raise HTTPException(404)
    practicas.archivar(conn, person_id, practica_id)
    return RedirectResponse("/history", status_code=303)


@router.get("/history")
@comun._cierra_conexion
def history_view(person_id: int = Depends(identity.current_person_id)):
    conn, person, t = comun._ctx(person_id)
    turnos_on = db.trabaja_turnos_si(conn, person_id)
    cov = history.coverage(conn, person_id)
    if not cov:
        return comun._render("soon.html", t=t, tab="trends", title=t("history.title"),
                       text=comun._sync_status_text(conn, person, t), turnos=turnos_on)
    defs = comun._defs(conn)
    train = history.training(conn, person_id)
    # BODY-PERIODO-PANTALLAS-1: marked days, read once for the whole page (patterns()'s own
    # docstring says why) and reused by patterns(), by_shift() and the count line below.
    marcados = db.dias_marcados(conn, person_id, cov["first_day"], cov["last_day"])
    pat = history.patterns(conn, person_id, marcados)
    shift_data = history.by_shift(conn, person_id, marcados)
    recovery = _recovery_ctx(t, recuperacion.estudio(conn, person_id))

    since = comun._short_date(t, date.fromisoformat(cov["first_day"]))
    coverage_line = t("history.coverage", since=since, days=t.num(cov["days_present"]))
    gap_line = (t("history.coverage_gap", days=t.num(cov["longest_gap_days"]),
                  start=comun._short_date(t, date.fromisoformat(cov["longest_gap_start"])),
                  end=comun._short_date(t, date.fromisoformat(cov["longest_gap_end"])))
                if cov["longest_gap_days"] > 0 else t("history.coverage_no_gap"))
    hero_finding = _finding_text(t, defs, pat["findings"][0]) if pat["findings"] else None
    periodo_line = _periodo_excluido_ctx(t, conn, person_id, cov, marcados)

    return comun._render("history.html", t=t, tab="trends", coverage_line=coverage_line, gap_line=gap_line,
                   activity_count=t.num(cov["activity_count"]), hero_finding=hero_finding, periodo_line=periodo_line,
                   training=_training_ctx(t, train) if train else None,
                   patterns=_pattern_rows(t, defs, pat),
                   findings=[_finding_text(t, defs, f) for f in pat["findings"][1:]],
                   shift=_shift_ctx(t, defs, shift_data), turnos=turnos_on, recovery=recovery,
                   practicas=_practicas_resumen_ctx(t, conn, person_id),
                   sugerencias=_sugerencias_ctx(t, conn, person_id))
