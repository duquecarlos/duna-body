"""Historial (BODY-WEB-ESTRUCTURA-7): the `/history` screen -- coverage, training, patterns and
the by-shift breakdown. Pure move: path, method, templates and body identical.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends

from .. import db, history, identity, recuperacion
from ..i18n import T
from . import comun

router = APIRouter()

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


def _recovery_cond_label(t: T, h: dict) -> str:
    factor, grupo = h["factor"], h["peor"]
    if factor == "turno":
        return t("history.recovery.cond.turno", kind=t("shift.kind." + grupo).lower())
    if factor == "tipo_sesion":
        info = history._kind_info(grupo) if grupo != "otro" else None
        kind_label = (_kind_label(t, info) if info else t("activity.generic")).lower()
        return t("history.recovery.cond.tipo_sesion", kind=kind_label)
    return t(_RECOVERY_COND_KEYS[factor][grupo])


def _recovery_ctx(t: T, est: dict) -> dict:
    """«Tu historia»'s "Recuperación después de entrenar" section (BODY-RECUPERACION-1): one plain
    line with the typical minutes and how many sessions it rests on, the two or three largest
    group differences as plain sentences, "pocos datos" when none qualify, and one line that
    caffeine, food and what he did after will count once he tells Body -- always, regardless of
    whether a finding was found. `est["n"] == 0` -- no derived figure yet at all -- is its own,
    simpler state."""
    if est["n"] == 0:
        return {"tiene_datos": False}
    hallazgos = [
        t("history.recovery.hallazgo", peor=_recovery_cond_label(t, h),
          min_peor=t.num(h["min_peor"]), min_mejor=t.num(h["min_mejor"]),
          n_peor=t.num(h["n_peor"]), n_mejor=t.num(h["n_mejor"]))
        for h in est["hallazgos"]
    ]
    return {
        "tiene_datos": True,
        "resumen": t("history.recovery.resumen", mediana=t.minutes(est["mediana"]), n=t.num(est["n"])),
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
                   shift=_shift_ctx(t, defs, shift_data), turnos=turnos_on, recovery=recovery)
