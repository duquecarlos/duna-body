"""The assistant (BODY-ASISTENTE-PROVEEDOR-1): one tool loop, streamed as SSE, plus the
conversation GET and the Aplicar/Descartar/Deshacer propuesta actions (BODY-WEB-ESTRUCTURA-4,
pure move out of body/web.py).

`asistente_preguntar` is the connection guard's one documented exception
(tests/test_conexiones_cerradas.py): its StreamingResponse generator keeps using the connection
after the route function itself returns, and closes it in the generator's own `finally` -- it
carries neither `_cierra_conexion` decorator, by name alone (no other route in this app is named
`asistente_preguntar`).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import anatomia, comidas, db, history, horizonte, identity, nutriente_objetivo, turnos_costo
from ..agente import bucle as agente_bucle
from ..agente import cliente as agente_cliente
from ..agente import conversacion as agente_conversacion
from ..agente import herramientas as agente_herramientas
from ..agente import propuestas as agente_propuestas
from ..agente import uso as agente_uso
from ..i18n import T
from . import comun

router = APIRouter()

MAX_PREGUNTA_CHARS = 2000
IDIOMA_LABEL = {"es": "Español", "en": "English"}
_ETIQUETA_KEYS = {
    "aplicar": "asistente.propuesta.aplicar", "descartar": "asistente.propuesta.descartar",
    "deshacer": "asistente.propuesta.deshacer", "aplicada": "asistente.propuesta.aplicada",
    "descartada": "asistente.propuesta.descartada", "deshecha": "asistente.propuesta.deshecha",
    "no_vigente": "asistente.propuesta.no_vigente", "desactualizada": "asistente.propuesta.desactualizada",
    "sin_deshacer": "asistente.propuesta.sin_deshacer",
    "entreno_reloj": "asistente.propuesta.entreno_reloj", "entreno_tu": "asistente.propuesta.entreno_tu",
    # BODY-DESHACER-DERIVADOS-1 rulings 3, 4: a card whose fetch itself failed (never a label the
    # backend returned), and /deshacer's own distinct drift sentence.
    "fallo": "asistente.propuesta.fallo",
    "deshacer_desactualizado": "asistente.propuesta.deshacer_desactualizado",
}


def _evento_sse(nombre: str, data: dict) -> str:
    return f"event: {nombre}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _etiquetas(t: T) -> dict:
    return {k: t(v) for k, v in _ETIQUETA_KEYS.items()}


def _objetivo_txt(t: T, defs: dict, metric_key: str | None, target_value, deadline: str) -> str:
    fecha = comun._short_date(t, date.fromisoformat(deadline))
    if metric_key is None or target_value is None:
        return t("goals.card.milestone_date", date=fecha)
    return t("asistente.propuesta.meta_objetivo", valor=comun._fig_unit(t, metric_key, target_value, defs), fecha=fecha)


def _numero_o_texto(t: T, valor) -> str:
    """BODY-ENTRENO-NUMEROS-1 ruling 3: a legacy proposal row stored before this slice can hold a
    `reps`/`peso_kg`/`series` that is not a number (e.g. "12-15") -- `t.num` would crash on it
    (ValueError: "Unknown format code"). Shown as its own raw text instead, unformatted."""
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return t.num(valor)
    return str(valor)


def _ejercicio_plan_txt(t: T, e: dict) -> str:
    if e.get("reps") is not None:
        return t("asistente.propuesta.entreno_ejercicio_reps", ejercicio=e["ejercicio"],
                 series=_numero_o_texto(t, e["series"]), reps=e["reps"])
    return t("asistente.propuesta.entreno_ejercicio", ejercicio=e["ejercicio"],
             series=_numero_o_texto(t, e["series"]))


def _nombre_legible(codigo: str | None) -> str | None:
    """Garmin's own code ("BARBELL_ROW") made readable for display, underscores as spaces and only
    the first letter capital (BODY-ENTRENO-TOOLS-1-M fix 1) -- never the stored garmin_exercise/
    garmin_category themselves, which stay the raw code."""
    if not codigo:
        return None
    texto = codigo.replace("_", " ").strip()
    return f"{texto[:1].upper()}{texto[1:].lower()}" if texto else None


def _serie_txt(t: T, s: dict | None, nombre: str | None = None) -> str | None:
    """One set's short display text for the watch-vs-you table: "<ejercicio> · <reps/weight>",
    or None for an empty cell (no watch set at that order). nombre, when given, overrides the
    exercise text (the watch side's readable name, built by _nombre_legible from the raw
    garmin_exercise/garmin_category -- fix 1)."""
    if s is None:
        return None
    ejercicio = (nombre if nombre is not None else (s.get("ejercicio") or "")).strip()
    reps, peso = s.get("reps"), s.get("peso_kg")
    if reps is not None and peso is not None:
        detalle = t("asistente.propuesta.entreno_reps_peso", reps=_numero_o_texto(t, reps),
                   peso=_numero_o_texto(t, peso))
    elif reps is not None:
        detalle = t("asistente.propuesta.entreno_reps", reps=_numero_o_texto(t, reps))
    elif peso is not None:
        detalle = t("asistente.propuesta.entreno_peso", peso=_numero_o_texto(t, peso))
    else:
        detalle = None
    if ejercicio and detalle:
        return f"{ejercicio} · {detalle}"
    return ejercicio or detalle or t("asistente.propuesta.entreno_sin_dato")


def _actividad_txt(t: T, actividad: dict) -> str:
    """The watch activity that is a habit's evidence, by name plus duration -- mirrors body/rutas/
    hoy.py's `_activity_name` fallback (an unnamed activity shows its translated known type, or a
    generic label) and `t.minutes`, since that module is outside this slice's touches."""
    nombre = actividad.get("nombre")
    if not nombre:
        tipo = actividad.get("tipo")
        nombre = t("activity.type." + tipo) if tipo in history.KNOWN_ACTIVITY_TYPES else t("activity.generic")
    duracion = actividad.get("duracion_s")
    return f"{nombre} · {t.minutes(duracion / 60)}" if duracion else nombre


def _cadencia_txt(t: T, cadencia: dict) -> str:
    """Duplicated from body/rutas/metas.py's `_cadence_text` (BODY-PLAN-PROPUESTAS-1): that module
    is outside this slice's touches, same reasoning `_momento_checkin_txt` above already used."""
    per = t("goals.cadence.per." + cadencia.get("per", "week"))
    return t("goals.cadence", n=cadencia.get("times", 1), per=per)


def _medida_label(t: T, medida: str) -> str:
    if medida in horizonte.MEDIDAS:
        return t("asistente.propuesta.plan_medida." + medida)
    return t("metric." + medida)


def _singular_key(base: str, n) -> str:
    return f"{base}_one" if n == 1 else base


def _frag(t: T, base: str, count, **kw) -> str:
    """One count's own singular-aware fragment -- `base`/`base_one` through `_singular_key`,
    mirroring body/rutas/turnos.py's own `_frag` (that module is outside this slice's touches, same
    reasoning as every other duplicated helper in this file). `count` is a separate positional
    argument, never a `**kw` name, so a fragment whose own placeholder is called `n` (as every one
    of them here is) can still be passed."""
    return t(_singular_key(base, count), **kw)


def _hora_desde_minutos(total: float) -> str:
    """The inverse of horizonte._minutos_despues_mediodia's own convention (minutes after 12:00
    noon, wrapping across midnight): 690 -> 23:30, 765 -> 00:45."""
    total = int(round(total))
    hh = ((total // 60) + 12) % 24
    mm = total % 60
    return f"{hh:02d}:{mm:02d}"


def _sesion_nombre_txt(t: T, medida: str, n) -> str:
    """BODY-PLAN-PROPUESTAS-1-R ruling 1a: the session noun, singular at n == 1, never the bare
    "sesiones" unit -- "sesión de fuerza"/"sesiones de fuerza", "sesión de cardio"/"sesiones de
    cardio" (and their English equivalents)."""
    uno = n == 1
    if medida == "sesiones_fuerza":
        return t("asistente.propuesta.plan_sesion_fuerza_one") if uno else t("asistente.propuesta.plan_sesion_fuerza")
    return t("asistente.propuesta.plan_sesion_cardio_one") if uno else t("asistente.propuesta.plan_sesion_cardio")


def _sesion_txt(t: T, medida: str, sentido: str, valor) -> str:
    """Ruling 1a: "al menos 3 sesiones de cardio" / "at least 3 cardio sessions" -- one phrase, the
    noun said once."""
    sentido_txt = t("asistente.propuesta.plan_sentido." + sentido)
    nombre = _sesion_nombre_txt(t, medida, valor)
    return t("asistente.propuesta.plan_sesion_valor", sentido=sentido_txt, n=t.num(valor), medida=nombre)


def _reloj_txt(t: T, medida: str, sentido: str, hora: str) -> str:
    """Ruling 1a: "acostarse antes de las 23:00" / "in bed before 23:00" -- four literal keys (two
    medidas × two sentidos), never a concatenated key (no new test_i18n_paridad.py dynamic-prefix
    entry needed, the same choice `_momento_checkin_txt` above already made)."""
    if medida == "hora_acostarse":
        clave = ("asistente.propuesta.plan_reloj_acostarse_antes" if sentido == "antes_de"
                 else "asistente.propuesta.plan_reloj_acostarse_despues")
    else:
        clave = ("asistente.propuesta.plan_reloj_levantarse_antes" if sentido == "antes_de"
                 else "asistente.propuesta.plan_reloj_levantarse_despues")
    return t(clave, hora=hora)


def _minutos_target_txt(t: T, valor) -> str:
    """Ruling 2 (BODY-PLAN-PANTALLA-1-R): one measure, one unit on a row -- the `min`-unit target
    said with the SAME formatter its real figure already uses on this row (t.minutes), dropping a
    whole hour's trailing ":00" (420 -> "7 h", not "7 h 00"; mirrors body/rutas/metas.py's own
    _plan_minutos_txt, the goal-detail screen's equivalent row)."""
    txt = t.minutes(valor)
    return txt[:-3] if txt.endswith(" 00") else txt


def _medida_target_txt(t: T, defs: dict, medida: str, sentido: str, valor) -> str:
    """BODY-PLAN-PROPUESTAS-1-R ruling 1a: one natural phrase, the measure's noun said once and the
    unit never repeating it -- session/clock/minute shapes for horizonte.MEDIDAS, the card's own
    metric-value helper for anything else (validar_medida's else branch). No week reference; see
    `_medida_target_con_semana_txt` for the variant that carries one."""
    info = horizonte.MEDIDAS.get(medida)
    if info is not None and info["unit"] == "hora":
        return _reloj_txt(t, medida, sentido, _hora_desde_minutos(valor))
    if info is not None and info["unit"] == "sesiones":
        return _sesion_txt(t, medida, sentido, valor)
    sentido_txt = t("asistente.propuesta.plan_sentido." + sentido)
    valor_txt = (_minutos_target_txt(t, valor) if info is not None
                 else comun._fig_unit(t, medida, valor, defs))
    return t("asistente.propuesta.plan_medida_valor", medida=_medida_label(t, medida), sentido=sentido_txt,
              valor=valor_txt)


def _medida_target_con_semana_txt(t: T, defs: dict, medida: str, sentido: str, valor, week_start: str) -> str:
    """Ruling 1a: the week reference stays "(semana del 9 nov 2026)" -- used by corregir_meta_plan's
    before/after and ampliar_plan's new weekly lines."""
    target = _medida_target_txt(t, defs, medida, sentido, valor)
    semana = comun._short_date(t, date.fromisoformat(week_start))
    return t("asistente.propuesta.plan_meta_semanal", target=target, semana=semana)


def _semana_targets_txt(t: T, defs: dict, metas_semana: list) -> str:
    return " · ".join(
        _medida_target_con_semana_txt(t, defs, m["medida"], m["sentido"], m["valor"], m["week_start"])
        for m in metas_semana
    )


def _tarjeta_meta_nueva(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    if despues.get("tipo") == "milestone":
        despues_txt = t("asistente.propuesta.meta_milestone", titulo=despues["titulo"],
                        fecha=comun._short_date(t, date.fromisoformat(despues["deadline"])))
    else:
        despues_txt = t("asistente.propuesta.meta_nueva", metric=t("metric." + despues["metric_key"]),
                        direccion=t("asistente.propuesta.direccion." + despues["direction"]),
                        valor=comun._fig_unit(t, despues["metric_key"], despues["target_value"], defs),
                        fecha=comun._short_date(t, date.fromisoformat(despues["deadline"])))
    habitos = despues.get("habitos") or []
    if habitos:
        habitos_txt = ", ".join(t("asistente.propuesta.meta_habito_linea", titulo=h["titulo"],
                                   cadencia=_cadencia_txt(t, h["cadencia"])) for h in habitos)
        despues_txt += "\n" + t("asistente.propuesta.plan_habitos", habitos=habitos_txt)
    return None, despues_txt, None


def _tarjeta_cambio_meta(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    metric_key = despues.get("metric_key")
    antes_txt = _objetivo_txt(t, defs, metric_key, antes_json["target_value"], antes_json["deadline"])
    despues_txt = _objetivo_txt(t, defs, metric_key, despues["target_value"], despues["deadline"])
    return antes_txt, despues_txt, None


def _tarjeta_confirmar_turnos(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    resumen = antes_json["resumen"]
    antes_txt = t("asistente.propuesta.turnos_actual", n=t.num(resumen["batch_count"]))
    despues_txt = t("asistente.propuesta.turnos_nuevo")
    return antes_txt, despues_txt, None


def _tarjeta_idioma(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    antes_txt = IDIOMA_LABEL.get(antes_json["lang"], antes_json["lang"])
    despues_txt = IDIOMA_LABEL.get(despues["lang"], despues["lang"])
    return antes_txt, despues_txt, None


def _choque_turno_txt(t: T, motivo: dict) -> str:
    """BODY-TURNO-DIA-PLAN-1 ruling 4: the one plain line under a planear_entreno card's date,
    from `turnos_costo.motivo_no_libre`'s own dict -- `{turno}` is the existing `shift.kind.<kind>`
    label, lower-cased the same way `_filtro_dias_txt` above already does."""
    if motivo["tipo"] == "sale_de_noche":
        return t("asistente.propuesta.sale_de_noche")
    turno = t("shift.kind." + motivo["kind"]).lower()
    if motivo["inicio"] and motivo["fin"]:
        return t("asistente.propuesta.turno_choque", turno=turno, inicio=motivo["inicio"], fin=motivo["fin"])
    return t("asistente.propuesta.turno_choque_sin_hora", turno=turno)


def _tarjeta_planear_entreno(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    ejercicios_txt = ", ".join(_ejercicio_plan_txt(t, e) for e in despues["ejercicios"])
    despues_txt = t("asistente.propuesta.entreno_plan", titulo=despues["titulo"],
                    fecha=comun._short_date(t, date.fromisoformat(despues["day"])), ejercicios=ejercicios_txt)
    # BODY-TURNO-DIA-PLAN-1 ruling 4: computed at RENDER time from the current confirmed shifts
    # (the same "never a stored snapshot" choice the zonas line below already makes) -- never
    # refuses and never hides Aplicar, just a plain line under the date; a free day shows nothing.
    if conn is not None:
        motivo = turnos_costo.motivo_no_libre(conn, person_id, date.fromisoformat(despues["day"]))
        if motivo is not None:
            despues_txt += "\n" + _choque_turno_txt(t, motivo)
    return None, despues_txt, None


def _tarjeta_registrar_entreno(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    titulo_manual = despues.get("titulo") if despues["manual"] else None
    if titulo_manual:
        despues_txt = t("asistente.propuesta.entreno_registrado_titulo", titulo=titulo_manual, fecha=fecha)
    else:
        despues_txt = t("asistente.propuesta.entreno_registrado", fecha=fecha)
    if despues["manual"]:
        series_tarjeta = [{"orden": i, "reloj": None, "tu": _serie_txt(t, s)}
                           for i, s in enumerate(despues["series"], start=1)]
        categorias = {s.get("categoria") for s in despues["series"]}
    else:
        # The watch side's readable name comes from the raw garmin_exercise/garmin_category in
        # antes_json["sets_antes"] -- never from m["reloj"]["ejercicio"], which can already be a
        # prior person correction (fix 1).
        crudos = {s["set_order"]: s for s in antes_json["sets_antes"]}
        series_tarjeta = []
        for m in despues["mapeo"]:
            crudo = crudos.get(m["set_order"], {})
            nombre = _nombre_legible(crudo.get("garmin_exercise")) or _nombre_legible(crudo.get("garmin_category"))
            series_tarjeta.append({"orden": m["set_order"], "reloj": _serie_txt(t, m["reloj"], nombre=nombre),
                                    "tu": _serie_txt(t, m["tu"])})
        # BODY-SERIES-ORDEN-1 rulings 3, 4: the watch's own active slots this call leaves unmatched
        # get excluded on apply -- said in one plain line, never silently dropped from the card.
        no_emparejadas = despues.get("no_emparejadas") or []
        if no_emparejadas:
            despues_txt += "\n" + t("asistente.propuesta.entreno_series_descartadas", n=len(no_emparejadas))
        categorias = {m["tu"].get("categoria") for m in despues["mapeo"]}
    # BODY-LESION-ZONA-1 ruling 2: the one shared plain line, appended once for the whole card, when
    # any of this session's own categories falls inside today's open-lesion zonas.
    if conn is not None:
        zonas_dia = db.zonas_en_recuperacion(conn, person_id, despues["day"])
        liviano = anatomia.texto_liviano_a_proposito(t, categorias & zonas_dia)
        if liviano:
            despues_txt += "\n" + liviano
    return None, despues_txt, series_tarjeta


def _tarjeta_corregir_entreno(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-CORREGIR-MEDIDA-ENTRENO-1-W ruling B: one summary line naming the set's own position
    and what changes -- `{cambios}` built with `_serie_txt`, the same ejercicio/reps/peso_kg
    vocabulary `_tarjeta_registrar_entreno`'s own card already uses for one set, rather than new
    i18n keys (the same "formatted like the existing card" choice `_tarjeta_corregir_comida` made
    with `_comida_cambios_lista`, here with no new per-field keys needed)."""
    fecha = comun._short_date(t, date.fromisoformat(despues["dia"]))
    cambios = _serie_txt(t, despues)
    despues_txt = t("asistente.propuesta.corregir_entreno_resumen", orden=despues["set_order"], dia=fecha,
                     cambios=cambios)
    return None, despues_txt, None


def _tarjeta_registrar_medida(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    metric_key = despues["metric_key"]
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    antes_txt = None
    if antes_json and antes_json.get("value") is not None:
        antes_txt = t("asistente.propuesta.medida_valor", metric=t("metric." + metric_key),
                      valor=comun._fig_unit(t, metric_key, antes_json["value"], defs), fecha=fecha)
    despues_txt = t("asistente.propuesta.medida_valor", metric=t("metric." + metric_key),
                    valor=comun._fig_unit(t, metric_key, despues["value"], defs), fecha=fecha)
    return antes_txt, despues_txt, None


_MOMENTO_I18N = {
    "desayuno": "asistente.propuesta.ingesta_momento_desayuno",
    "almuerzo": "asistente.propuesta.ingesta_momento_almuerzo",
    "merienda": "asistente.propuesta.ingesta_momento_merienda",
    "cena": "asistente.propuesta.ingesta_momento_cena",
}


def _momento_clave_txt(t: T, clave: str) -> str:
    """One of the four default labels, or the clave itself capitalised when it is a custom one with
    no i18n key (BODY-HOGAR-ESCRIBE-1-W ruling 2)."""
    key = _MOMENTO_I18N.get(clave)
    return t(key) if key else clave.capitalize()


def _momento_ingesta_txt(t: T, conn, person_id: int, ts_local: str | None) -> str | None:
    """BODY-PROTEINA-1: a plain-word time-of-day label from `ts_local`'s own hour -- "lunch" style,
    never shown when there is no `ts_local` (the entry is "sometime that day" only). BODY-HOGAR-
    ESCRIBE-1-W ruling 2: rewired to the person's own `comidas.momento_de` windows instead of a
    hardcoded hour rule -- same labels for the four defaults."""
    if not ts_local:
        return None
    clave = comidas.momento_de(conn, person_id, ts_local)
    if clave is None:
        return None
    return _momento_clave_txt(t, clave)


def _ingesta_valor_txt(t: T, defs: dict, metric_key: str, amount: float, estimado: bool, fecha: str) -> str:
    valor = comun._fig_unit(t, metric_key, amount, defs)
    if estimado:
        valor = t("asistente.propuesta.ingesta_valor_estimado", valor=valor)
    return t("asistente.propuesta.ingesta_valor", metric=t("metric." + metric_key), valor=valor, fecha=fecha)


def _tarjeta_registrar_ingesta(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    partes = [_ingesta_valor_txt(t, defs, despues["metric_key"], despues["amount"], despues["estimado"], fecha)]
    momento = _momento_ingesta_txt(t, conn, person_id, despues.get("ts_local"))
    if momento:
        partes.append(momento)
    if despues.get("descripcion"):
        partes.append(t("asistente.propuesta.ingesta_descripcion", descripcion=despues["descripcion"]))
    return None, " · ".join(partes), None


def _tarjeta_corregir_ingesta(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    antes_txt = _ingesta_valor_txt(t, defs, despues["metric_key"], antes_json["amount"], despues["estimado"], fecha)
    despues_txt = _ingesta_valor_txt(t, defs, despues["metric_key"], despues["amount"], despues["estimado"], fecha)
    return antes_txt, despues_txt, None


def _tarjeta_borrar_ingesta(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    fecha = comun._short_date(t, date.fromisoformat(antes_json["day"]))
    partes = [_ingesta_valor_txt(t, defs, antes_json["metric_key"], antes_json["amount"], antes_json["estimado"],
                                 fecha)]
    momento = _momento_ingesta_txt(t, conn, person_id, antes_json.get("ts_local"))
    if momento:
        partes.append(momento)
    if antes_json.get("descripcion"):
        partes.append(t("asistente.propuesta.ingesta_descripcion", descripcion=antes_json["descripcion"]))
    antes_txt = " · ".join(partes)
    despues_txt = t("asistente.propuesta.ingesta_eliminado")
    return antes_txt, despues_txt, None


def _comida_cantidad_txt(t: T, defs: dict, c: dict) -> str:
    """BODY-COMIDA-1 ruling 4: one nutrient's own line, marked «≈» when estimated -- the catalogue
    order for the whole list comes from the caller (herramientas.INGESTA_METRIC_KEYS), never from
    `cantidades`' own stored order."""
    valor = comun._fig_unit(t, c["metric_key"], c["amount"], defs)
    clave = "asistente.propuesta.comida_cantidad_estimado" if c.get("estimado", True) \
        else "asistente.propuesta.comida_cantidad"
    return t(clave, metric=t("metric." + c["metric_key"]), valor=valor)


def _tarjeta_registrar_comida(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-COMIDA-1-M fix 2: three lines, never one long bold paragraph -- the description on its
    own line, date and meal-word/time beneath it, then every amount (fixed catalogue order, each
    marked «≈» when estimated) on its own line, mirroring Día's own two-line meal
    (body/rutas/hoy.py's `_comidas_dia_ctx`, body/templates/day.html). `\\n` joins the lines; the
    renderer (body/static/assistant.js) splits `despues` on it and keeps only the first line bold.
    Never raises on a stored amount this card cannot show (_tarjeta_propuesta_segura's own
    fallback covers any surprise, same as every other card here)."""
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    # BODY-HOGAR-ESCRIBE-1-W ruling 3: the person's own stated moment wins over the hour-derived
    # one, when they gave one.
    momento_clave = despues.get("momento")
    momento = _momento_clave_txt(t, momento_clave) if momento_clave \
        else _momento_ingesta_txt(t, conn, person_id, despues.get("ts_local"))
    linea_fecha = fecha if not momento else fecha + " · " + momento
    por_clave = {c["metric_key"]: c for c in despues["cantidades"]}
    orden = [k for k in agente_herramientas.INGESTA_METRIC_KEYS if k in por_clave]
    cantidades_txt = " · ".join(_comida_cantidad_txt(t, defs, por_clave[k]) for k in orden)
    lineas = [t("asistente.propuesta.ingesta_descripcion", descripcion=despues["descripcion"]), linea_fecha]
    if cantidades_txt:
        lineas.append(cantidades_txt)
    return None, "\n".join(lineas), None


def _hora_o_fecha_corta(t: T, day: str, ts_local: str | None) -> str:
    """BODY-COMIDA-CORREGIR-1: the meal's own clock time (HH:MM) when it has one, or its short
    date otherwise -- the same fallback `comidas_del_dia`'s own ordering already treats an untimed
    meal with (sorts last, never crashes)."""
    if ts_local:
        return ts_local[11:16]
    return comun._short_date(t, date.fromisoformat(day))


def _comida_cambios_lista(t: T, defs: dict, antes: dict, despues: dict) -> list[str]:
    """Ruling 2: {cambios} built from what differs between the meal's prior and new state -- new
    time, new description, amounts changed/added/removed -- one i18n key per kind, in the
    catalogue order (herramientas.INGESTA_METRIC_KEYS) for every amount line."""
    cambios = []
    if despues.get("ts_local") != antes.get("ts_local") and despues.get("ts_local"):
        cambios.append(t("asistente.propuesta.comida_cambio_hora", hora=despues["ts_local"][11:16]))
    if despues.get("descripcion") != antes.get("descripcion"):
        cambios.append(t("asistente.propuesta.comida_cambio_descripcion", descripcion=despues["descripcion"]))
    antes_por_clave = {c["metric_key"]: c for c in antes.get("cantidades", [])}
    despues_por_clave = {c["metric_key"]: c for c in despues.get("cantidades", [])}
    orden = [k for k in agente_herramientas.INGESTA_METRIC_KEYS
             if k in antes_por_clave or k in despues_por_clave]
    for k in orden:
        a, d = antes_por_clave.get(k), despues_por_clave.get(k)
        metric = t("metric." + k)
        if a is None and d is not None:
            cambios.append(t("asistente.propuesta.comida_cambio_cantidad_nueva", metric=metric,
                             valor=comun._fig_unit(t, k, d["amount"], defs)))
        elif a is not None and d is None:
            cambios.append(t("asistente.propuesta.comida_cambio_cantidad_quitada", metric=metric))
        elif a is not None and d is not None and a["amount"] != d["amount"]:
            cambios.append(t("asistente.propuesta.comida_cambio_cantidad_valor", metric=metric,
                             valor=comun._fig_unit(t, k, d["amount"], defs)))
    return cambios


def _tarjeta_corregir_comida(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-COMIDA-CORREGIR-1 ruling 2: one summary line naming the meal by its OLD time (or date,
    when it had none) and listing what changes -- never a full antes/despues pair of blocks, since
    a correction can touch time, wording and several amounts together."""
    hora_antes = _hora_o_fecha_corta(t, antes_json["day"], antes_json.get("ts_local"))
    cambios = _comida_cambios_lista(t, defs, antes_json, despues)
    despues_txt = t("asistente.propuesta.corregir_comida_resumen", hora_antes=hora_antes,
                     cambios=" · ".join(cambios))
    return None, despues_txt, None


def _momento_checkin_txt(t: T, momento: str) -> str:
    """BODY-CHECKIN-1: three literal t() calls, not a concatenated key, the same choice
    `_periodo_kind_txt` above made -- so test_i18n_paridad.py's dynamic-prefix guard needs no new
    entry. Duplicated in body/rutas/hoy.py (its own Día section), since neither module may import
    the other for one helper."""
    if momento == "despertar":
        return t("checkin.momento.despertar")
    if momento == "post_turno":
        return t("checkin.momento.post_turno")
    return t("checkin.momento.otro")


def _checkin_valor_txt(t: T, defs: dict, metric_key: str, valor: int) -> str:
    return t("asistente.propuesta.checkin_valor", metric=t("metric." + metric_key),
              valor=comun._fig_unit(t, metric_key, valor, defs))


def _tarjeta_registrar_checkin(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-CHECKIN-1 ruling 3: one card -- the date, the clock time when the check-in has one
    (BODY-CHECKIN-1-M fix: Día shows it, so the card must too) and the moment word, then every
    given reading as «4 de 5» (comun._fig_unit, the same "of5" rendering soreness already uses),
    then the note in quotes. Insert-shaped, like registrar_comida: there is no 'before'."""
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    partes = [fecha]
    ts_local = despues.get("ts_local")
    if ts_local:
        partes.append(ts_local[11:16])
    partes.append(_momento_checkin_txt(t, despues["momento"]))
    lineas = [" · ".join(partes)]
    valores_txt = " · ".join(
        _checkin_valor_txt(t, defs, k, despues[k]) for k in db.CHECKIN_METRIC_KEYS if despues.get(k) is not None
    )
    if valores_txt:
        lineas.append(valores_txt)
    if despues.get("nota"):
        lineas.append(t("asistente.propuesta.checkin_nota", nota=despues["nota"]))
    return None, "\n".join(lineas), None


def _periodo_kind_txt(t: T, kind: str) -> str:
    """BODY-PERIODO-1: three literal t() calls, not a concatenated key, so test_i18n_paridad.py's
    dynamic-prefix guard needs no new entry (the same choice BODY-PROTEINA-1 made for
    _momento_ingesta_txt)."""
    if kind == "viaje":
        return t("periodo.kind.viaje")
    if kind == "lesion":
        return t("periodo.kind.lesion")
    return t("periodo.kind.malestar")


def _periodo_dias_txt(t: T, start_day: str, end_day: str | None) -> str:
    inicio = comun._short_date(t, date.fromisoformat(start_day))
    if end_day is None:
        return t("asistente.propuesta.periodo_abierto", fecha=inicio)
    fin = comun._short_date(t, date.fromisoformat(end_day))
    if end_day == start_day:
        return t("asistente.propuesta.periodo_un_dia", fecha=inicio)
    return t("asistente.propuesta.periodo_rango", desde=inicio, hasta=fin)


def _periodo_txt(t: T, kind: str, start_day: str, end_day: str | None, note: str | None) -> str:
    """BODY-PERIODO-1 ruling 2: the card says the kind, the days, and that Body will leave those
    days out when it compares with the usual -- every period card builder below reuses this."""
    partes = [t("asistente.propuesta.periodo_resumen", kind=_periodo_kind_txt(t, kind),
                dias=_periodo_dias_txt(t, start_day, end_day))]
    if note:
        partes.append(t("asistente.propuesta.periodo_nota", nota=note))
    partes.append(t("asistente.propuesta.periodo_excluido"))
    return " · ".join(partes)


def _tarjeta_proponer_objetivo_nutriente(t: T, defs: dict, despues: dict, antes_json: dict | None, conn,
                                          person_id: int) -> tuple:
    """BODY-NUTRIENTE-OBJETIVO-1 ruling 3: the metric's name, the target in words (reused from
    body/nutriente_objetivo.py -- the same words Ajustes' own listing shows, ruling 6) and the
    start day. ORCH-PROPUESTAS-TABLA-NUTRIENTE-1: this function and its TARJETAS entry below are
    this slice's one necessary file outside its declared `touches` -- BODY-PROPUESTAS-TABLA-1's
    own static census (tests/test_propuestas_tabla.py, outside touches, pins the tool list across
    this dict, propuestas.TABLA and herramientas.HERRAMIENTAS) fails without it, and the standing
    dispatch rule for a census test that pins a tool list is to extend it to the new truth."""
    metric = t("metric." + despues["metric_key"])
    palabras = nutriente_objetivo.texto_objetivo(t, despues["kind"], despues["amount"], despues["sentido"],
                                                  comun._unit(t, defs[despues["metric_key"]]))
    fecha = comun._short_date(t, date.fromisoformat(despues["start_day"]))
    despues_txt = t("nutriente.objetivo.resumen", metric=metric, palabras=palabras, fecha=fecha)
    return None, despues_txt, None


def _tarjeta_marcar_periodo(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    despues_txt = _periodo_txt(t, despues["kind"], despues["start_day"], despues.get("end_day"),
                                despues.get("note"))
    return None, despues_txt, None


def _tarjeta_cerrar_periodo(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    antes_txt = _periodo_txt(t, despues["kind"], despues["start_day"], None, despues.get("note"))
    despues_txt = _periodo_txt(t, despues["kind"], despues["start_day"], despues["end_day"], despues.get("note"))
    return antes_txt, despues_txt, None


def _tarjeta_corregir_periodo(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    antes_txt = _periodo_txt(t, antes_json["kind"], antes_json["start_day"], antes_json.get("end_day"),
                              antes_json.get("note"))
    despues_txt = _periodo_txt(t, despues["kind"], despues["start_day"], despues.get("end_day"),
                                despues.get("note"))
    return antes_txt, despues_txt, None


def _tarjeta_borrar_periodo(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    antes_txt = _periodo_txt(t, antes_json["kind"], antes_json["start_day"], antes_json.get("end_day"),
                              antes_json.get("note"))
    despues_txt = t("asistente.propuesta.periodo_eliminado")
    return antes_txt, despues_txt, None


def _tarjeta_descartar_series(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-SERIES-ORDEN-1 ruling 4: the plain count line first, then each slot's own watch
    exercise/reps -- the `tu` column stays empty, since nothing the person said is being shown
    here, only which watch sets are proposed as not done."""
    despues_txt = t("asistente.propuesta.entreno_series_descartadas", n=len(despues["series"]))
    series_tarjeta = []
    for s in despues["series"]:
        nombre = _nombre_legible(s.get("ejercicio")) or _nombre_legible(s.get("categoria"))
        series_tarjeta.append({"orden": s["set_order"], "reloj": _serie_txt(t, s, nombre=nombre), "tu": None})
    return None, despues_txt, series_tarjeta


def _sesion_con_hora_txt(titulo: str, hora_local: str | None) -> str:
    hora = hora_local[11:16] if hora_local else None
    return f"{titulo} ({hora})" if hora else titulo


def _tarjeta_separar_sesion(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-ENTRENO-VINCULO-1 ruling 4: both sessions' own title and time, plain words -- the undo
    of an automatic or a chat-raised link."""
    reloj_txt = _sesion_con_hora_txt(despues["reloj_titulo"], despues.get("reloj_hora"))
    manual_txt = _sesion_con_hora_txt(despues["manual_titulo"], despues.get("manual_hora"))
    despues_txt = t("asistente.propuesta.entreno_separar", reloj=reloj_txt, manual=manual_txt)
    return None, despues_txt, None


def _tarjeta_unir_sesiones(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-ENTRENO-VINCULO-1 ruling 5: the batch fix for a day stored before this slice, or one
    `separar_sesion` already split."""
    despues_txt = t("asistente.propuesta.entreno_unir", reloj=despues["reloj_titulo"], manual=despues["manual_titulo"])
    return None, despues_txt, None


def _filtro_dias_txt(t: T, filtro_dias: dict) -> str:
    """"which days count" in plain words (BODY-EXPERIMENTOS-REGISTRO-1): the shift kinds a day must
    or must not have, plus whether a post-night recovery day is excluded -- built from
    `shift.kind.*`'s own labels, never a second hand-typed name per kind."""
    partes = []
    if filtro_dias.get("turno_en"):
        nombres = ", ".join(t("shift.kind." + k) for k in filtro_dias["turno_en"]).lower()
        partes.append(t("asistente.propuesta.experimento_turno_en", turnos=nombres))
    if filtro_dias.get("turno_fuera"):
        nombres = ", ".join(t("shift.kind." + k) for k in filtro_dias["turno_fuera"]).lower()
        partes.append(t("asistente.propuesta.experimento_turno_fuera", turnos=nombres))
    if filtro_dias.get("excluir_tras_noche"):
        partes.append(t("asistente.propuesta.experimento_sin_tras_noche"))
    if not partes:
        return t("asistente.propuesta.experimento_todos_los_dias")
    return ", ".join(partes)


def _tarjeta_proponer_experimento(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    metricas_txt = ", ".join(t("metric." + m) for m in despues["metricas"])
    dias_txt = _filtro_dias_txt(t, despues["filtro_dias"])
    fecha = comun._short_date(t, date.fromisoformat(despues["fecha_inicio"]))
    despues_txt = t("asistente.propuesta.experimento_nuevo", titulo=despues["titulo"], metricas=metricas_txt,
                    dias=dias_txt, fecha=fecha)
    return None, despues_txt, None


def _tarjeta_proponer_practica(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-HISTORIA-VIDA-2 ruling 3: insert-shaped, like `_tarjeta_proponer_experimento` -- one line,
    no 'antes'."""
    n = despues["meta_dias_semana"]
    despues_txt = _frag(t, "asistente.propuesta.practica_nueva", n, titulo=despues["titulo"], n=t.num(n))
    return None, despues_txt, None


def _tarjeta_marcar_experimento(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    titulo = despues["titulo"]
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    clave = "asistente.propuesta.experimento_hecho" if despues["ocurrio"] else "asistente.propuesta.experimento_no_hecho"
    despues_txt = t(clave, experimento=titulo, fecha=fecha)
    antes_txt = None
    if antes_json is not None:
        clave_antes = ("asistente.propuesta.experimento_hecho" if antes_json["ocurrio"]
                       else "asistente.propuesta.experimento_no_hecho")
        antes_txt = t(clave_antes, experimento=titulo, fecha=fecha)
    return antes_txt, despues_txt, None


def _tarjeta_marcar_habito(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    habito = despues["habito_titulo"]
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    if despues.get("actividad"):
        despues_txt = t("asistente.propuesta.habito_hecho_actividad", habito=habito, fecha=fecha,
                        actividad=_actividad_txt(t, despues["actividad"]))
    else:
        despues_txt = t("asistente.propuesta.habito_hecho", habito=habito, fecha=fecha)
    antes_txt = None
    if antes_json is not None and antes_json.get("done") is False:
        antes_txt = t("asistente.propuesta.habito_no_marcado", habito=habito, fecha=fecha)
    return antes_txt, despues_txt, None


def _dia_mes_corto(t: T, d: date) -> str:
    """Day + abbreviated month, no year, in the order each language reads it naturally -- "9 nov" /
    "Nov 9" (BODY-PLAN-PROPUESTAS-1-R ruling 1c). Duplicated from body/rutas/experimentos.py's own
    `_dia_mes` (that module is outside this slice's touches, same reasoning as every other
    duplicated helper in this file) and widened to flip word order per language, since this
    phrase's own worked example does (unlike comun._short_date, which keeps day-first in both)."""
    mes = t("date.months")[d.month - 1][:3]
    return t("asistente.propuesta.plan_fecha_corta", dia=d.day, mes=mes)


def _plan_resumen_linea(t: T, despues: dict) -> str | None:
    """Ruling 1c: reglas, semanas con metas (distinct week_start values among metas_semanales, not
    the "semanas" notes list), chequeos -- only the parts that exist, in that order. None when the
    plan has none of the three (the old unconditional "0 semanas · 0 metas · 0 checkpoints" line is
    gone)."""
    n_reglas = len(despues.get("reglas") or [])
    n_semanas_con_metas = len({m["week_start"] for m in (despues.get("metas_semanales") or [])})
    n_checkpoints = len(despues.get("checkpoints") or [])
    partes = []
    if n_reglas:
        partes.append(_frag(t, "asistente.propuesta.plan_reglas_n", n_reglas, n=t.num(n_reglas)))
    if n_semanas_con_metas:
        partes.append(_frag(t, "asistente.propuesta.plan_semanas_con_metas_n", n_semanas_con_metas,
                            n=t.num(n_semanas_con_metas)))
    if n_checkpoints:
        partes.append(_frag(t, "asistente.propuesta.plan_chequeos_n", n_checkpoints, n=t.num(n_checkpoints)))
    return " · ".join(partes) if partes else None


def _primera_semana_txt(t: T, defs: dict, metas_semanales: list) -> str | None:
    """Ruling 1c: the first week that has targets, its own targets in the order the plan lists
    them -- no line at all for a plan with no weekly targets."""
    if not metas_semanales:
        return None
    primera = min(m["week_start"] for m in metas_semanales)
    targets = " · ".join(_medida_target_txt(t, defs, m["medida"], m["sentido"], m["valor"])
                         for m in metas_semanales if m["week_start"] == primera)
    fecha = _dia_mes_corto(t, date.fromisoformat(primera))
    return t("asistente.propuesta.plan_primera_semana", fecha=fecha, targets=targets)


def _tarjeta_proponer_plan(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """Ruling 8: a whole plan is one card -- plain-word lines, each only shown when that part of
    the plan is non-empty, never a JSON dump. Text rulings 1a-1e (BODY-PLAN-PROPUESTAS-1-R): the
    parts count line and the first-week line, below."""
    lineas = [t("asistente.propuesta.plan_nuevo", titulo=despues["titulo"],
                desde=comun._short_date(t, date.fromisoformat(despues["start_day"])),
                hasta=comun._short_date(t, date.fromisoformat(despues["deadline"])))]
    if despues.get("why"):
        lineas.append(t("asistente.propuesta.plan_porque", why=despues["why"]))
    metas = despues.get("metas_finales") or []
    if metas:
        metas_txt = ", ".join(
            t("asistente.propuesta.plan_meta_final_linea", titulo=m["titulo"],
              valor=comun._fig_unit(t, m["metric_key"], m["target_value"], defs))
            for m in metas)
        lineas.append(t("asistente.propuesta.plan_metas_finales", metas=metas_txt))
    habitos = despues.get("habitos") or []
    if habitos:
        habitos_txt = ", ".join(t("asistente.propuesta.plan_habito_linea", titulo=h["titulo"],
                                   cadencia=_cadencia_txt(t, h["cadencia"])) for h in habitos)
        lineas.append(t("asistente.propuesta.plan_habitos", habitos=habitos_txt))
    resumen = _plan_resumen_linea(t, despues)
    if resumen:
        lineas.append(resumen)
    primera_semana = _primera_semana_txt(t, defs, despues.get("metas_semanales") or [])
    if primera_semana:
        lineas.append(primera_semana)
    return None, "\n".join(lineas), None


def _tarjeta_ampliar_plan(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """Ruling 4: insert-only additions to an existing plan -- one plain line per kind of addition,
    only when that list is non-empty. Text ruling 1d (BODY-PLAN-PROPUESTAS-1-R): agreed-number
    counts and the new weekly lines through ruling 1a; the plain "semanas" notes list is never
    shown, same reasoning as 1c."""
    partes = []
    n_reglas = len(despues.get("reglas") or [])
    n_checkpoints = len(despues.get("checkpoints") or [])
    metas_semanales = despues.get("metas_semanales") or []
    if n_reglas:
        partes.append(_frag(t, "asistente.propuesta.plan_regla_nueva", n_reglas, n=t.num(n_reglas)))
    if metas_semanales:
        partes.append(t("asistente.propuesta.plan_ampliar_metas_semanales",
                        metas=_semana_targets_txt(t, defs, metas_semanales)))
    if n_checkpoints:
        partes.append(_frag(t, "asistente.propuesta.plan_chequeo_nuevo", n_checkpoints, n=t.num(n_checkpoints)))
    despues_txt = "\n".join(partes) if partes else t("asistente.propuesta.plan_ampliar_vacio")
    return None, despues_txt, None


def _tarjeta_corregir_meta_plan(t: T, defs: dict, despues: dict, antes_json: dict | None, conn,
                                 person_id: int) -> tuple:
    """Ruling 3: before -> after, with the measure's own unit -- never a bare number. Text ruling
    1a (BODY-PLAN-PROPUESTAS-1-R): the before/after phrases are the same natural-sentence shapes
    `_semana_targets_txt` uses."""
    antes_txt = _medida_target_con_semana_txt(t, defs, despues["medida"], despues["sentido"],
                                              antes_json["valor"], despues["week_start"])
    despues_txt = _medida_target_con_semana_txt(t, defs, despues["medida"], despues["sentido"],
                                                despues["valor"], despues["week_start"])
    return antes_txt, despues_txt, None


def _tarjeta_anotar_sesion(t: T, defs: dict, despues: dict, antes_json: dict | None, conn, person_id: int) -> tuple:
    """BODY-HOGAR-ESCRIBE-1 ruling 4: the planned session's own title and date, then the note in
    quotes, then a plain "marked as done" line only when `hecha` was true -- insert-shaped like
    `registrar_checkin`'s own card, never an antes/despues pair (a note has no prior wording the
    person is correcting)."""
    fecha = comun._short_date(t, date.fromisoformat(despues["day"]))
    lineas = [t("asistente.propuesta.sesion_titulo_fecha", titulo=despues["titulo"], fecha=fecha),
              t("asistente.propuesta.sesion_nota", nota=despues["nota"])]
    if despues.get("hecha"):
        lineas.append(t("asistente.propuesta.sesion_hecha"))
    return None, "\n".join(lineas), None


def _tarjeta_importar_desplazamiento(t: T, defs: dict, despues: dict, antes_json: dict | None, conn,
                                      person_id: int) -> tuple:
    """BODY-DESPLAZAMIENTO-ASISTENTE-1 ruling 2: counts and dates only -- never a figure from the
    file itself. Insert-shaped like `_tarjeta_confirmar_turnos`'s own whole-file summary, never an
    antes/despues pair (the file itself is what changed, not one existing value).
    BODY-DESPLAZAMIENTO-ASISTENTE-1-W ruling B: the day count and the changed-days count each agree
    in number through `_frag`'s own `_one` sibling-key convention, and a one-day file names its
    single date (`desplazamiento_dia_unico`) rather than a range of the same day to itself."""
    desde = comun._short_date(t, date.fromisoformat(despues["primer_dia"]))
    hasta = comun._short_date(t, date.fromisoformat(despues["ultimo_dia"]))
    dias_txt = _frag(t, "asistente.propuesta.desplazamiento_dias_n", despues["dias"], n=t.num(despues["dias"]))
    if despues["primer_dia"] == despues["ultimo_dia"]:
        rango_txt = t("asistente.propuesta.desplazamiento_dia_unico", desde=desde)
    else:
        rango_txt = t("asistente.propuesta.desplazamiento_rango", desde=desde, hasta=hasta)
    cambios_txt = _frag(t, "asistente.propuesta.desplazamiento_cambios_n", despues["dias_cambio"],
                         n=t.num(despues["dias_cambio"]))
    despues_txt = f"{dias_txt}, {rango_txt} ({cambios_txt})"
    return None, despues_txt, None


# One card builder per action (BODY-PROPUESTAS-TABLA-1), keyed exactly like body/agente/
# propuestas.py's TABLA and body/agente/herramientas.py's `propuesta`-kind tools
# (tests/test_propuestas_tabla.py enforces the three sets match). Each builder returns
# (antes_txt, despues_txt, series_tarjeta); _tarjeta_propuesta below applies the shared frame.
TARJETAS = {
    "proponer_meta": _tarjeta_meta_nueva,
    "proponer_cambio_meta": _tarjeta_cambio_meta,
    "proponer_confirmar_turnos": _tarjeta_confirmar_turnos,
    "proponer_idioma": _tarjeta_idioma,
    "planear_entreno": _tarjeta_planear_entreno,
    "registrar_entreno": _tarjeta_registrar_entreno,
    "corregir_entreno": _tarjeta_corregir_entreno,
    "registrar_medida": _tarjeta_registrar_medida,
    "marcar_habito": _tarjeta_marcar_habito,
    "proponer_experimento": _tarjeta_proponer_experimento,
    "marcar_experimento": _tarjeta_marcar_experimento,
    "registrar_ingesta": _tarjeta_registrar_ingesta,
    "corregir_ingesta": _tarjeta_corregir_ingesta,
    "borrar_ingesta": _tarjeta_borrar_ingesta,
    "registrar_comida": _tarjeta_registrar_comida,
    "corregir_comida": _tarjeta_corregir_comida,
    "registrar_checkin": _tarjeta_registrar_checkin,
    "marcar_periodo": _tarjeta_marcar_periodo,
    "cerrar_periodo": _tarjeta_cerrar_periodo,
    "corregir_periodo": _tarjeta_corregir_periodo,
    "borrar_periodo": _tarjeta_borrar_periodo,
    "descartar_series": _tarjeta_descartar_series,
    "separar_sesion": _tarjeta_separar_sesion,
    "unir_sesiones": _tarjeta_unir_sesiones,
    "proponer_objetivo_nutriente": _tarjeta_proponer_objetivo_nutriente,
    "proponer_plan": _tarjeta_proponer_plan,
    "ampliar_plan": _tarjeta_ampliar_plan,
    "corregir_meta_plan": _tarjeta_corregir_meta_plan,
    "anotar_sesion": _tarjeta_anotar_sesion,
    "importar_desplazamiento": _tarjeta_importar_desplazamiento,
    "proponer_practica": _tarjeta_proponer_practica,
}


def _tarjeta_propuesta(t: T, defs: dict, registro: dict, origen_conversacion: str | None = None, *,
                        conn=None, person_id: int | None = None) -> dict:
    """The `propuesta` SSE event payload (agent-capabilities-v1 section 2, decision 5): `antes`/
    `despues` are short plain-word strings in the person's language, built here from the record's
    structured JSON -- never stored as text. `etiquetas` carries this card's button and result
    labels pre-rendered, since body/templates/base.html (outside this slice's touches) is the only
    other place assistant.js could read i18n text from. `origen_conversacion` (BODY-MCP-LOCAL-1) is
    this proposal's own conversation's `origen` column (agente_conversacion.origen_de): 'mcp' shows
    a small plain label on the card, so the person knows it came from their personal assistant
    (the desk), never from Body's own chat panel -- rendered here, not in JS, like every other
    string on this card. `registro["aplicada_via"]` (BODY-MCP-APLICAR-1) overrides that label with
    a more specific one when this same outside caller also APPLIED it itself, through its own
    `aplicar_propuesta` tool, rather than the person tapping Aplicar in Body."""
    accion = registro["accion"]
    despues = registro["despues_json"]
    antes_json = registro["antes_json"]
    # ORCH-CARLOS-DELEGATES-TIER-1-1 ruling 2: a same-day sibling the apply replaced still carries
    # the real 'descartada' estado (no schema change), but its own despues_json gained a
    # 'reemplazada' marker -- shown here as the whole card's text, in place of the stale proposal
    # content its own TARJETAS builder would otherwise still render.
    if despues.get("reemplazada"):
        antes_txt, despues_txt, series_tarjeta = None, t("asistente.propuesta.reemplazada"), None
    else:
        antes_txt, despues_txt, series_tarjeta = TARJETAS[accion](t, defs, despues, antes_json, conn, person_id)
        if antes_txt is not None:
            antes_txt = t("asistente.propuesta.antes", valor=antes_txt)
            despues_txt = t("asistente.propuesta.despues", valor=despues_txt)
        elif despues_txt:
            despues_txt = t("asistente.propuesta.solo_propuesta", valor=despues_txt)
    # BODY-MCP-APLICAR-1 ruling 4: an apply THIS caller made itself (aplicada_via == 'mcp') gets
    # its own, more specific label -- never the generic "from your personal assistant" one, since
    # that applies regardless of who tapped Aplicar.
    if registro["aplicada_via"] == "mcp":
        origen_etiqueta = t("asistente.propuesta.aplicado_mcp")
    elif registro["aplicada_via"] == "hogar":
        origen_etiqueta = t("asistente.propuesta.aplicado_hogar")
    elif origen_conversacion == "mcp":
        origen_etiqueta = t("asistente.propuesta.origen_mcp")
    elif origen_conversacion == "hogar":
        origen_etiqueta = t("asistente.propuesta.origen_hogar")
    else:
        origen_etiqueta = None
    return {
        "id": registro["id"], "accion": accion, "titulo": t("asistente.propuesta.titulo." + accion),
        "antes": antes_txt, "despues": despues_txt, "series": series_tarjeta, "expira_en": registro["expira_en"],
        "deshacer": accion not in agente_propuestas.SIN_DESHACER, "estado": registro["estado"],
        "etiquetas": _etiquetas(t), "origen_etiqueta": origen_etiqueta,
    }


def _tarjeta_propuesta_segura(t: T, defs: dict, registro: dict, origen_conversacion: str | None = None, *,
                               conn=None, person_id: int | None = None) -> dict:
    """BODY-ENTRENO-NUMEROS-1 ruling 3: one bad stored card (e.g. a legacy `reps` that is not a
    number in a shape `_numero_o_texto` does not cover) must never take the whole conversation's
    card list down. Catches anything `_tarjeta_propuesta` still raises and shows that one card as a
    plain fallback line instead, so the conversation's other cards and the panel still render. Logs
    only the exception class and the propuesta id to stderr -- never the payload (health data never
    leaves the PC through a log)."""
    try:
        return _tarjeta_propuesta(t, defs, registro, origen_conversacion, conn=conn, person_id=person_id)
    except Exception as exc:
        print(f"asistente: propuesta {registro['id']} no se pudo mostrar ({type(exc).__name__})",
              file=sys.stderr)
        return {
            "id": registro["id"], "accion": registro["accion"],
            "titulo": t("asistente.propuesta.titulo." + registro["accion"]),
            "antes": None, "despues": t("asistente.propuesta.error_render"), "series": None,
            "expira_en": registro["expira_en"], "deshacer": False, "estado": registro["estado"],
            "etiquetas": _etiquetas(t), "origen_etiqueta": None,
        }


@router.post("/asistente/preguntar")
async def asistente_preguntar(request: Request, person_id: int = Depends(identity.current_person_id)):
    try:
        cuerpo = await request.json()
    except Exception:
        raise HTTPException(400)
    if not isinstance(cuerpo, dict):
        raise HTTPException(400)
    pregunta = cuerpo.get("pregunta")
    if not isinstance(pregunta, str) or not (1 <= len(pregunta) <= MAX_PREGUNTA_CHARS):
        raise HTTPException(400)
    conversacion_id = cuerpo.get("conversacion_id")
    if conversacion_id is not None and not isinstance(conversacion_id, int):
        raise HTTPException(400)

    conn, _, t = comun._ctx(person_id)
    agente_herramientas.fijar_turnos_actual(db.trabaja_turnos_si(conn, person_id))
    if conversacion_id is not None:
        try:
            agente_conversacion.verificar_propiedad(conn, conversacion_id, person_id)
        except agente_conversacion.ConversacionAjena:
            conn.close()
            raise HTTPException(404)
    else:
        conversacion_id = agente_conversacion.crear(conn, person_id)

    proveedor_env = os.environ.get("BODY_AGENTE_PROVEEDOR", agente_cliente.PROVEEDOR_DEFAULT)
    if proveedor_env == "demo":
        proveedor_nombre, modelo = "demo", "demo"
    else:
        configurados = agente_cliente.proveedores_configurados()
        resuelto = agente_cliente.nombre_elegido(configurados, conn, person_id)
        proveedor_nombre = resuelto or proveedor_env
        # `resuelto` is None when nothing is configured -- `generar()` then returns on the
        # no_configurado error before `modelo` is read in production. This reads the client's OWN
        # default constant rather than a second BODY_AGENTE_MODELO lookup, so no second default
        # exists (BODY-MODELOS-ESTANDAR-1 decision 4).
        modelo = configurados[resuelto].modelo if resuelto else agente_cliente.MODELO_DEFAULT
    proveedor = agente_cliente.proveedor_configurado(conn, person_id)

    async def generar():
        try:
            if proveedor is None:
                yield _evento_sse("error", {"codigo": "no_configurado", "mensaje": t("agente.error.no_configurado")})
                return
            try:
                agente_uso.verificar_tope(conn, person_id, proveedor_nombre, modelo)
            except agente_uso.TopeRefusal:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, agente_cliente.Uso(), 0,
                                      None, "tope_diario")
                yield _evento_sse("error", {"codigo": "tope_diario", "mensaje": t("agente.error.tope_diario")})
                return

            historial = agente_conversacion.historial(conn, conversacion_id, person_id)
            historial.append({"rol": "usuario", "bloques": [{"tipo": "texto", "texto": pregunta}]})
            sistema = t("agente.sistema")
            estados = agente_propuestas.estado_conversacion(conn, person_id, conversacion_id)
            if estados:
                lineas = "\n".join(f"- #{p['id']} {p['accion']}: {p['estado']}" for p in estados)
                sistema += "\n\n" + t("agente.estado_propuestas_header") + "\n" + lineas
            try:
                resultado = agente_bucle.ejecutar(conn, person_id, proveedor, sistema, historial, conversacion_id)
            except agente_bucle.LimiteRondas as e:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, e.uso_total, e.rondas,
                                      "limite_rondas", "limite_rondas")
                yield _evento_sse("error", {"codigo": "limite_rondas", "mensaje": t("agente.error.limite_rondas")})
                return
            except agente_bucle.FalloProveedor as e:
                agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, e.uso_total, e.rondas,
                                      None, "proveedor")
                yield _evento_sse("error", {"codigo": "proveedor", "mensaje": t("agente.error.proveedor")})
                return

            defs = comun._defs(conn)
            origen_conversacion = agente_conversacion.origen_de(conn, conversacion_id)
            for ev in resultado.eventos:
                if isinstance(ev, agente_bucle.EventoHerramienta):
                    yield _evento_sse("herramienta", {"nombre": ev.nombre})
                elif isinstance(ev, agente_bucle.EventoPropuesta):
                    yield _evento_sse("propuesta", _tarjeta_propuesta(t, defs, ev.registro, origen_conversacion,
                                                                       conn=conn, person_id=person_id))
                else:
                    yield _evento_sse("texto", {"delta": ev.texto})

            agente_conversacion.agregar_mensaje(conn, conversacion_id, person_id, "usuario",
                                                [{"tipo": "texto", "texto": pregunta}])
            for m in resultado.nuevos_mensajes:
                agente_conversacion.agregar_mensaje(conn, conversacion_id, person_id, m["rol"], m["bloques"])
            agente_uso.registrar(conn, person_id, proveedor_nombre, modelo, resultado.uso_total, resultado.rondas,
                                  "texto", None)
            yield _evento_sse("fin", {"conversacion_id": conversacion_id, "rondas": resultado.rondas,
                                      "motivo_fin": "texto"})
        finally:
            conn.close()

    return StreamingResponse(generar(), media_type="text/event-stream")


_PENDIENTE_MCP = "pendiente"


@router.get("/asistente/conversacion/{conversacion_id}")
@comun._cierra_conexion
def asistente_conversacion(conversacion_id: str, person_id: int = Depends(identity.current_person_id)):
    """`conversacion_id` carries one sentinel besides a real id (BODY-MCP-LOCAL-1-B): `_PENDIENTE_MCP`
    ("pendiente") resolves to the newest conversation that still holds an outside-caller proposal
    in state `propuesta`, if any -- the assistant panel's own default when it opens with no
    conversation named. 404, same as an unknown real id, when there is none: the panel's existing
    404 handling (assistant.js's `cargarConversacion`) already does nothing further, which is
    exactly "fall back to today's behaviour." The response's own `conversacion_id` field (added by
    this slice) lets the panel learn which real id it just loaded, to remember for later turns."""
    conn, _, t = comun._ctx(person_id)
    if conversacion_id == _PENDIENTE_MCP:
        resuelto = agente_conversacion.pendientes_mcp(conn, person_id)[1]
        if resuelto is None:
            # Ruling 3 (BODY-TUTORIALES-EN-1): "no pending conversation" is the normal case for this
            # sentinel, not an error -- 200 with empty turns/propuestas, never the 404 an unknown or
            # someone else's REAL id still gets below. A browser console logging an error on every
            # ordinary Today load (the surface report) hides real errors there.
            conn.close()
            return JSONResponse({"conversacion_id": None, "turnos": [], "propuestas": []})
    else:
        try:
            resuelto = int(conversacion_id)
        except ValueError:
            conn.close()
            raise HTTPException(404)
    try:
        turnos = agente_conversacion.turnos_visibles(conn, resuelto, person_id)
    except agente_conversacion.ConversacionAjena:
        conn.close()
        raise HTTPException(404)
    defs = comun._defs(conn)
    origen_conversacion = agente_conversacion.origen_de(conn, resuelto)
    tarjetas = [_tarjeta_propuesta_segura(t, defs, r, origen_conversacion, conn=conn, person_id=person_id)
                for r in agente_propuestas.listar(conn, person_id, resuelto)]
    conn.close()
    return JSONResponse({"conversacion_id": resuelto, "turnos": turnos, "propuestas": tarjetas})


def _propuesta_respuesta(conn, person_id: int, propuesta_id: int, accion, *, ruta: str = "") -> JSONResponse:
    try:
        resultado = accion(conn, person_id, propuesta_id)
    except agente_propuestas.PropuestaAjena:
        conn.close()
        raise HTTPException(404)
    except Exception:
        # BODY-DESHACER-DERIVADOS-1 ruling 2: a raise from the action's own writer (escribir_
        # deshacer, most of all) never reaches FastAPI's default HTML error page -- the card
        # always gets JSON it can parse, even on a 500.
        conn.rollback()
        conn.close()
        return JSONResponse({"estado": "error"}, status_code=500)
    # Ruling 4: the route is the one place that knows which path was called -- /deshacer's own
    # drift refusal reads differently from /aplicar's, so it gets its own estado here rather than
    # the JS card guessing which sentence a shared "propuesta_desactualizada" means.
    if ruta == "deshacer" and resultado.get("estado") == "propuesta_desactualizada":
        resultado = {**resultado, "estado": "deshacer_desactualizado"}
    conn.close()
    return JSONResponse(resultado)


@router.post("/asistente/propuestas/{propuesta_id}/aplicar")
@comun._cierra_conexion
def propuesta_aplicar(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.aplicar, ruta="aplicar")


@router.post("/asistente/propuestas/{propuesta_id}/descartar")
@comun._cierra_conexion
def propuesta_descartar(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.descartar, ruta="descartar")


@router.post("/asistente/propuestas/{propuesta_id}/deshacer")
@comun._cierra_conexion
def propuesta_deshacer(propuesta_id: int, person_id: int = Depends(identity.current_person_id)):
    conn, _, _ = comun._ctx(person_id)
    return _propuesta_respuesta(conn, person_id, propuesta_id, agente_propuestas.deshacer, ruta="deshacer")
