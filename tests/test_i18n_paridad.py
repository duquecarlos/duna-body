"""BODY-I18N-GUARD-1: a test that notices when es.json and en.json drift apart, instead of the
silent Spanish fallback / raw-key fallback in body/i18n/__init__.py's T.__call__ doing it for us."""
from tests import _isolate  # noqa: F401,E402

import json
import pathlib
import re
import string
import unittest
from unittest import mock

from body import anatomia, db, history, horizonte, noche_inusual, tutoriales
from body import turnos_costo
from body.agente import propuestas
from body.rutas import comun, metas, tendencias, turnos

ROOT = pathlib.Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / "body" / "i18n"
_FMT = string.Formatter()


def _load(lang: str) -> dict:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def _placeholders(value: str) -> set:
    return {name for _, name, _, _ in _FMT.parse(value) if name}


# A call whose first argument is a string literal: t('key') / t("key"), with or without kwargs
# after it. Group 2 catches a '+' or '~' right after the literal -- Jinja uses '~', Python uses
# '+' -- which marks the literal as a PREFIX (the rest of the key is built at runtime), not a
# full key.
_CALL_RE = re.compile(r"(?<![A-Za-z_])[Tt]\(\s*['\"]([A-Za-z0-9_.]*)['\"]\s*([+~]?)")
# t(f"...") or t(f'...') -- a key assembled entirely inside an f-string. Out of scope by the spec;
# counted so the skip count is visible rather than silently zero.
_FSTRING_CALL_RE = re.compile(r"(?<![A-Za-z_])[Tt]\(\s*f['\"]")

_SCAN_PATHS = list((ROOT / "body" / "templates").glob("*.html")) + [
    p for p in (ROOT / "body").rglob("*.py") if "i18n" not in p.parts
]


def _scan_calls():
    """Returns (literal_keys, dynamic_prefixes, fstring_call_count) found across templates and code.

    dynamic_prefixes maps a literal prefix (e.g. "metric.") to the set of (path, lineno) sites that
    used it, for a readable failure when a NEW, unrecognised dynamic prefix shows up.
    """
    literal_keys = set()
    dynamic_prefixes: dict[str, set] = {}
    fstring_count = 0
    for path in _SCAN_PATHS:
        text = path.read_text(encoding="utf-8")
        fstring_count += len(_FSTRING_CALL_RE.findall(text))
        for m in _CALL_RE.finditer(text):
            prefix, concat = m.group(1), m.group(2)
            lineno = text.count("\n", 0, m.start()) + 1
            if concat:
                dynamic_prefixes.setdefault(prefix + concat, set()).add((str(path.relative_to(ROOT)), lineno))
            else:
                literal_keys.add(prefix)
    return literal_keys, dynamic_prefixes, fstring_count


# Fixed lists a dynamic "prefix" + concatenation can resolve to, read from the source each prefix's
# value actually comes from (not re-typed by hand). The dict key matches what `_scan_calls` records:
# the literal prefix exactly as it appears before the '+' or '~' in the source.
_METRIC_KEYS = [d[0] for d in db.METRIC_DEFS]
_TREND_GROUP_KEYS = [k for _, keys in comun.TREND_GROUPS for k in keys]
_TREND_Q_KEYS = comun.TREND_KEYS + _TREND_GROUP_KEYS
_TREND_SHORT_KEYS = sorted(set(tendencias.WEEK_KEYS) | set(history.PATTERN_KEYS))

_FIXED_DYNAMIC = {
    # body/web.py: t("metric." + k) -- every metric Body knows about (body/db.py METRIC_DEFS).
    "metric.+": _METRIC_KEYS,
    # body/web.py: t("part." + key) -- the readiness score's own parts (body/readiness.py WEIGHTS).
    "part.+": ["hrv", "sleep", "battery", "resting_hr", "stress"],
    # body/web.py, body/templates/shift_pattern.html: t("shift.kind." + k) / t('shift.kind.' + k) --
    # _STRIP_KIND_ORDER is the superset (it also covers the strip's "no shift" legend entry).
    "shift.kind.+": turnos._STRIP_KIND_ORDER,
    # body/rutas/hoy.py _dia_por_delante_ctx(): t("shift.kind.title." + k) -- the day-ahead row's
    # own shift title, reading as a shift ("Day shift"/"Turno de día") rather than the bare kind
    # (BODY-MOCK-R2-HOY-1-M fix 3). Same kind set as shift.kind.+ above.
    "shift.kind.title.+": turnos._STRIP_KIND_ORDER,
    # body/web.py: t("shifts.cost_sub." + key) -- the cost model's own metric list (turnos_costo.MODELO).
    "shifts.cost_sub.+": turnos_costo.MODELO["metricas"],
    # body/turnos_costo.py diferencia_y_palabra(): the only three words it returns.
    "shifts.word.+": ["mas", "menos", "igual"],
    # body/turnos_costo.py _motivo(): the only four reasons it returns.
    "shifts.reason.~": ["sin_turnos", "sin_noches", "pocos_bloques", "sin_datos_reloj"],
    # body/patron_turnos.py validar(): per-row error codes. Its whole-cycle "largo_invalido" (index
    # None) reaches t() through this same prefix too, since BODY-PATRON-ERROR-CICLO-1 -- checked
    # directly by tests/test_patron_error_ciclo_1.py, not enumerated here.
    "patron.error.+": ["tipo_desconocido", "hora_invalida", "misma_hora", "descanso_con_hora"],
    # body/web.py TREND_KEYS + TREND_GROUPS -- every metric that gets a Tendencias card.
    "trends.q.+": _TREND_Q_KEYS,
    # body/web.py TREND_GROUPS -- only the "Ver todas las medidas" extras get a plain-word subtitle.
    "trends.plain.+": _TREND_GROUP_KEYS,
    # body/web.py TREND_GROUPS' own group names.
    "trends.group.+": [g for g, _ in comun.TREND_GROUPS],
    # body/web.py WEEK_KEYS ∪ history.PATTERN_KEYS -- every metric the week/weekday tables can show.
    "trends.short.+": _TREND_SHORT_KEYS,
    # body/history.py KNOWN_ACTIVITY_TYPES -- the Garmin activity types Body has a label for.
    "activity.type.+": sorted(history.KNOWN_ACTIVITY_TYPES),
    # body/rutas/metas.py AREAS / DIRECTIONS (also reused for asistente.propuesta.direccion.).
    "goals.area.+": metas.AREAS,
    "goals.direction.+": metas.DIRECTIONS,
    "asistente.propuesta.direccion.+": metas.DIRECTIONS,
    # body/templates/goal.html, goal_new.html option values.
    "habit.context.+": ["before_night", "after_night", "off_days"],
    "goals.cadence.per.+": ["day", "week", "month"],
    # body/db.py CHECK (goal.status): only paused/done/dropped ever reach the archive card.
    "goals.status.+": ["paused", "done", "dropped"],
    # body/db.py CHECK (ajuste_agente.nivel_datos).
    "ajustes.asistente.opcion.+": ["completo", "resumen", "ninguno"],
    # body/rutas/asistente.py: t("asistente.propuesta.titulo." + accion) -- every proposal kind
    # the agent can raise, read from body/agente/propuestas.py's own TABLA (BODY-PROPUESTAS-TABLA-1,
    # the same source tests/test_propuestas_tabla.py compares with HERRAMIENTAS and asistente.
    # TARJETAS), never a hand-typed list (BODY-I18N-TITULO-PROPUESTA-CENSO-1).
    "asistente.propuesta.titulo.+": sorted(propuestas.TABLA),
    # body/templates/today.html, how.html: t('band.' ~ r.band) -- readiness.py Readiness.band().
    "band.~": ["high", "medium", "low"],
    # body/templates/trends.html: t('trends.r.' + x) -- the range picker's own values.
    "trends.r.+": tendencias.RANGES,
    # body/rutas/experimentos.py _metric_line(): t("experimentos.resultado." + resultado).
    "experimentos.resultado.+": ["mas_bajo", "mas_alto", "sin_cambio", "pocos_dias", "aun_no"],
    # body/rutas/historial.py, body/rutas/hoy.py: t("periodo.kind." + p["kind"]) (body/db.py PERIOD_KINDS).
    "periodo.kind.+": [k for k, _ in db.PERIOD_KINDS],
    # body/rutas/turnos.py _turno_fc_direccion(): the only two words it returns.
    "shifts.pulso.+": list(turnos._PULSO_DIRECCION_WORDS),
    # body/rutas/turnos.py _turno_fc_sueno_lineas(): the fixed corta/normal sleep split (the same
    # one body/turno_fc.py's own estudio() uses).
    "shifts.pulso.sueno_+": ["corta", "normal"],
    # body/rutas/turnos.py _perfil_offset_palabra(): every OFFSET turnos_costo.perfil_por_dia can
    # return (BODY-TURNOS-PERFIL-DIA-1).
    "shifts.perfil.offset.+": list(turnos_costo.OFFSETS),
    # body/rutas/tendencias.py _frase_patron_texto(): every OFFSET perfil_por_dia can name as the
    # worst point, used as the "cuando" time phrase (BODY-TENDENCIAS-LEYENDA-PATRON-1).
    "trends.patron.cuando.+": list(turnos_costo.OFFSETS),
    # body/rutas/tendencias.py _frase_patron_texto(): `vuelta` is always a post_* offset
    # (turnos_costo.perfil_por_dia only ever looks at post_* offsets for it).
    "trends.patron.vuelta.+": [o for o in turnos_costo.OFFSETS if o.startswith("post_")],
    # body/rutas/tendencias.py _frase_patron_texto(): turnos_costo.frase_patron's own two directions.
    "trends.patron.vuelve.+": ["baja", "sube"],
    "trends.patron.no_vuelve.+": ["baja", "sube"],
    # body/rutas/turnos.py _perfil_eje_label(): the three OFFSETS that get a translated x-axis label
    # -- post_1..post_4 are rendered as a literal "+1".."+4", the same in both languages, never t().
    "shifts.perfil.eje.+": ["antes", "noche_1", "noche_ultima"],
    # body/rutas/hoy.py _noche_inusual_senales(): body/noche_inusual.py MODELO's own three signals.
    "noche.senal.+": list(noche_inusual.MODELO["senales"]),
    # body/anatomia.py contexto(): every view id, across the three screens (anclajes.json's own
    # vistas_por_pantalla).
    "cuerpo.vista.+": sorted({v["id"] for vs in anatomia.VISTAS_POR_PANTALLA.values() for v in vs}),
    # body/anatomia.py _alt(): each screen's own view ids, one literal prefix per screen.
    "cuerpo.alt.hoy.+": [v["id"] for v in anatomia.VISTAS_POR_PANTALLA["hoy"]],
    "cuerpo.alt.tendencias.+": [v["id"] for v in anatomia.VISTAS_POR_PANTALLA["tendencias"]],
    "cuerpo.alt.turnos.+": [v["id"] for v in anatomia.VISTAS_POR_PANTALLA["turnos"]],
    # body/rutas/hoy.py _cuerpo_hoy_ctx(): body/anatomia.py GRUPOS_ORDEN -- every glow group a
    # training category can light.
    "cuerpo.grupo.+": anatomia.GRUPOS_ORDEN,
    # body/anatomia.py texto_liviano_a_proposito() (BODY-LESION-ZONA-1): every categoria word, the
    # same CATEGORIA_ENUM a training exercise's own categoria already speaks.
    "entreno.categoria.+": list(anatomia.CATEGORIA_ENUM),
    # body/tutoriales.py _titulo_capitulo()/_titulo_tarea() (BODY-TUTORIALES-EN-1, ruling 1): every
    # chapter and task id in the catalog itself, so a new catalog entry with no English title fails
    # here rather than showing half-translated on screen.
    "tutoriales.capitulo.+": [c["id"] for c in tutoriales.catalogo()["capitulos"]],
    "tutoriales.tarea.+": [t["id"] for t in tutoriales.catalogo()["tareas"]],
    # body/rutas/asistente.py _medida_label()/_medida_target_txt() (BODY-PLAN-PROPUESTAS-1 ruling
    # 8, widened BODY-PLAN-PROPUESTAS-1-R ruling 1a): horizonte.py's own weekly-target vocabulary
    # and its three sentido/one-unit words (the "sesiones" unit's own bare count is gone --
    # replaced by the noun-bearing plan_sesion_fuerza/plan_sesion_cardio pair, each a literal key).
    "asistente.propuesta.plan_medida.+": sorted(horizonte.MEDIDAS),
    "asistente.propuesta.plan_sentido.+": ["al_menos", "como_maximo", "antes_de", "despues_de"],
    "asistente.propuesta.plan_unidad.+": ["min"],
    # body/rutas/metas.py _plan_estado_chip(): every estado a weekly target line or a checkpoint
    # criterion can carry (BODY-PLAN-PANTALLA-1), read from metas.PLAN_ESTADOS rather than retyped.
    "goals.plan.estado.+": list(metas.PLAN_ESTADOS),
    # body/rutas/historial.py _regla_texto(): a sueno_horario practica's own two fields
    # (practicas.py's own _validar_sueno_horario CHECK).
    "practica.campo_+": ["inicio", "fin"],
}

# body/web.py _finding_text(): key = f"history.find.{axis}_better_{better}", then t(key, ...) -- a
# key built from a bare variable, invisible to both regexes above (neither a string literal nor an
# f-string argument reaches t() itself). Checked on its own instead; enumerable because history.py
# filters PATTERN_KEYS findings to better in ("higher", "lower") before this runs -- "neutral" never
# reaches it.
_HISTORY_FIND_KEYS = [f"history.find.{axis}_better_{better}"
                      for axis in ("weekday", "month") for better in ("higher", "lower")]


class TestI18nParidad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.es = _load("es")
        cls.en = _load("en")

    def test_same_key_set(self):
        es_only = sorted(set(self.es) - set(self.en))
        en_only = sorted(set(self.en) - set(self.es))
        self.assertEqual(es_only, [], f"keys present in es.json but missing from en.json: {es_only}")
        self.assertEqual(en_only, [], f"keys present in en.json but missing from es.json: {en_only}")

    def test_placeholders_and_shapes_match(self):
        placeholder_mismatches = []
        shape_mismatches = []
        for key in sorted(set(self.es) & set(self.en)):
            ev, nv = self.es[key], self.en[key]
            if isinstance(ev, str) and isinstance(nv, str):
                pe, pn = _placeholders(ev), _placeholders(nv)
                if pe != pn:
                    placeholder_mismatches.append((key, sorted(pe), sorted(pn)))
            elif isinstance(ev, list) and isinstance(nv, list):
                if len(ev) != len(nv):
                    shape_mismatches.append((key, f"es has {len(ev)} items", f"en has {len(nv)} items"))
            elif isinstance(ev, dict) and isinstance(nv, dict):
                if set(ev) != set(nv):
                    shape_mismatches.append((key, sorted(ev), sorted(nv)))
            elif type(ev) is not type(nv):
                shape_mismatches.append((key, type(ev).__name__, type(nv).__name__))
        self.assertEqual(placeholder_mismatches, [],
                          f"{{placeholder}} names differ between es and en: {placeholder_mismatches}")
        self.assertEqual(shape_mismatches, [], f"value shapes differ between es and en: {shape_mismatches}")

    def test_no_empty_values(self):
        empties = []
        for lang, table in (("es", self.es), ("en", self.en)):
            for key, value in table.items():
                if isinstance(value, str) and value.strip() == "":
                    empties.append(f"{lang}.json: {key!r}")
        self.assertEqual(empties, [], f"empty or whitespace-only values: {empties}")

    def test_literal_code_keys_exist_in_es(self):
        literal_keys, _dynamic, _fstring_count = _scan_calls()
        literal_keys.discard("")  # a key built as a bare variable (t(key, ...)) scans as an empty literal
        missing = sorted(k for k in literal_keys if k not in self.es)
        self.assertEqual(missing, [],
                          f"keys referenced literally in templates/code but missing from es.json: {missing}")

    def test_dynamic_prefixes_resolve_in_es(self):
        _literal, dynamic_prefixes, fstring_count = _scan_calls()
        # 0 today -- body/web.py's one bare-variable key (history.find.*) is a separate case, below,
        # not a t(f"...") call. A non-zero count here means a NEW key built inside an f-string
        # argument to t(), which this guard cannot read the fixed list for without being taught it.
        self.assertEqual(fstring_count, 0,
                          "a new t(f'...') call appeared -- add it to the dynamic-key coverage above "
                          "(this count is not a generic tripwire, it is this slice's own census)")
        unknown_prefixes = sorted(set(dynamic_prefixes) - set(_FIXED_DYNAMIC))
        self.assertEqual(unknown_prefixes, [],
                          f"new dynamic t(...) prefix(es) with no fixed-list coverage: {unknown_prefixes} "
                          f"(sites: { {p: sorted(dynamic_prefixes[p]) for p in unknown_prefixes} })")
        missing = []
        for prefix, values in _FIXED_DYNAMIC.items():
            if prefix not in dynamic_prefixes:
                continue  # listed for completeness; the source no longer builds this prefix dynamically
            bare_prefix = prefix.rstrip("+~").strip("'")
            for value in values:
                key = f"{bare_prefix}{value}"
                if key not in self.es:
                    missing.append(key)
        self.assertEqual(missing, [],
                          f"a dynamic prefix can resolve to a key missing from es.json: {sorted(missing)}")

    def test_titulo_de_propuesta_detecta_un_kind_fabricado_sin_clave(self):
        """BODY-I18N-TITULO-PROPUESTA-CENSO-1: the derived list (sorted(propuestas.TABLA)) must
        actually catch a proposal kind with no asistente.propuesta.titulo.<kind> key -- proven by
        monkeypatching TABLA with a fabricated kind, never a real one, and re-deriving the list
        the same way _FIXED_DYNAMIC does, inside the patch."""
        kind = "kind_fabricado_sin_titulo_1"
        with mock.patch.dict(propuestas.TABLA, {kind: propuestas.TABLA["proponer_meta"]}):
            kinds = sorted(propuestas.TABLA)
            missing = [f"asistente.propuesta.titulo.{k}" for k in kinds
                       if f"asistente.propuesta.titulo.{k}" not in self.es]
        self.assertEqual(missing, [f"asistente.propuesta.titulo.{kind}"])

    def test_deshacer_derivados_failure_labels_exist_in_both_languages(self):
        """BODY-DESHACER-DERIVADOS-1 rulings 3, 4: these two keys are read off a dict (never a
        literal t("...") call), so the generic scan above cannot see them -- named here instead."""
        for key in ("asistente.propuesta.fallo", "asistente.propuesta.deshacer_desactualizado"):
            self.assertIn(key, self.es, f"{key} missing from es.json")
            self.assertIn(key, self.en, f"{key} missing from en.json")

    def test_history_find_keys_exist(self):
        missing = sorted(k for k in _HISTORY_FIND_KEYS if k not in self.es)
        self.assertEqual(missing, [], f"body/web.py _finding_text() can build a key missing from es.json: "
                                       f"{missing}")


if __name__ == "__main__":
    unittest.main()
