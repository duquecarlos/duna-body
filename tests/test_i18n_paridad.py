"""BODY-I18N-GUARD-1: a test that notices when es.json and en.json drift apart, instead of the
silent Spanish fallback / raw-key fallback in body/i18n/__init__.py's T.__call__ doing it for us."""
from tests import _isolate  # noqa: F401,E402

import json
import pathlib
import re
import string
import unittest

from body import db, history
from body import turnos_costo
from body import web

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
_TREND_GROUP_KEYS = [k for _, keys in web.TREND_GROUPS for k in keys]
_TREND_Q_KEYS = web.TREND_KEYS + _TREND_GROUP_KEYS
_TREND_SHORT_KEYS = sorted(set(web.WEEK_KEYS) | set(history.PATTERN_KEYS))

_FIXED_DYNAMIC = {
    # body/web.py: t("metric." + k) -- every metric Body knows about (body/db.py METRIC_DEFS).
    "metric.+": _METRIC_KEYS,
    # body/web.py: t("part." + key) -- the readiness score's own parts (body/readiness.py WEIGHTS).
    "part.+": ["hrv", "sleep", "battery", "resting_hr", "stress"],
    # body/web.py, body/templates/shift_pattern.html: t("shift.kind." + k) / t('shift.kind.' + k) --
    # _STRIP_KIND_ORDER is the superset (it also covers the strip's "no shift" legend entry).
    "shift.kind.+": web._STRIP_KIND_ORDER,
    # body/web.py: t("shifts.cost_sub." + key) -- the cost model's own metric list (turnos_costo.MODELO).
    "shifts.cost_sub.+": turnos_costo.MODELO["metricas"],
    # body/turnos_costo.py diferencia_y_palabra(): the only three words it returns.
    "shifts.word.+": ["peor", "mejor", "igual"],
    # body/turnos_costo.py _motivo(): the only four reasons it returns.
    "shifts.reason.~": ["sin_turnos", "sin_noches", "pocos_bloques", "sin_datos_reloj"],
    # body/patron_turnos.py validar(): per-row error codes (its whole-cycle "largo_invalido" is
    # filtered out by index before reaching t(), so it never needs an i18n string).
    "patron.error.+": ["tipo_desconocido", "hora_invalida", "misma_hora", "descanso_con_hora"],
    # body/web.py TREND_KEYS + TREND_GROUPS -- every metric that gets a Tendencias card.
    "trends.q.+": _TREND_Q_KEYS,
    # body/web.py TREND_GROUPS -- only the "Ver todas las medidas" extras get a plain-word subtitle.
    "trends.plain.+": _TREND_GROUP_KEYS,
    # body/web.py TREND_GROUPS' own group names.
    "trends.group.+": [g for g, _ in web.TREND_GROUPS],
    # body/web.py WEEK_KEYS ∪ history.PATTERN_KEYS -- every metric the week/weekday tables can show.
    "trends.short.+": _TREND_SHORT_KEYS,
    # body/history.py KNOWN_ACTIVITY_TYPES -- the Garmin activity types Body has a label for.
    "activity.type.+": sorted(history.KNOWN_ACTIVITY_TYPES),
    # body/web.py AREAS / DIRECTIONS (also reused for asistente.propuesta.direccion.).
    "goals.area.+": web.AREAS,
    "goals.direction.+": web.DIRECTIONS,
    "asistente.propuesta.direccion.+": web.DIRECTIONS,
    # body/templates/goal.html, goal_new.html option values.
    "habit.context.+": ["before_night", "after_night", "off_days"],
    "goals.cadence.per.+": ["day", "week", "month"],
    # body/db.py CHECK (goal.status): only paused/done/dropped ever reach the archive card.
    "goals.status.+": ["paused", "done", "dropped"],
    # body/db.py CHECK (ajuste_agente.nivel_datos).
    "ajustes.asistente.opcion.+": ["completo", "resumen", "ninguno"],
    # body/web.py _propuesta_ctx(): the only four proposal kinds the assistant can raise.
    "asistente.propuesta.titulo.+": ["proponer_meta", "proponer_cambio_meta",
                                      "proponer_confirmar_turnos", "proponer_idioma"],
    # body/templates/today.html, how.html: t('band.' ~ r.band) -- readiness.py Readiness.band().
    "band.~": ["high", "medium", "low"],
    # body/templates/trends.html: t('trends.r.' + x) -- the range picker's own values.
    "trends.r.+": web.RANGES,
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

    def test_history_find_keys_exist(self):
        missing = sorted(k for k in _HISTORY_FIND_KEYS if k not in self.es)
        self.assertEqual(missing, [], f"body/web.py _finding_text() can build a key missing from es.json: "
                                       f"{missing}")


if __name__ == "__main__":
    unittest.main()
