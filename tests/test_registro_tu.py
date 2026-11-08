"""BODY-REGISTRO-TU-1: Body speaks tu, in one register, to one person -- never usted -- and a link
is never followed directly by punctuation a chat client could read as part of it (hub item
hit_01m6ymj1w53wcm7ghds29vpk1s, adopted from Pedidos' ORDERS-QUESTION-ROUTING-REGISTER-PACK-1).

Guard 1 scans every es.json string for usted markers: the word itself, its plural, the formal
imperatives, and specific risky pronoun phrases (bare le/lo/la are not matched -- they are mostly
relative pronouns and definite articles in this app's Spanish, so a bare-word match would be noise).
It also scans su/sus/tiene/puede, which read as "you" when addressed to the reader but are often
third person about a thing; every hit must be in the allow-list below, with the reason it is not
an usted address.

Guard 2 scans es.json, en.json and the assistant's own fixed text (body/agente/*.py, except
cliente.py, which is Tier 1) for punctuation glued directly onto a URL placeholder or a literal
http(s) link.
"""
from tests import _isolate  # noqa: F401,E402

import ast
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / "body" / "i18n"
AGENTE_DIR = ROOT / "body" / "agente"


def _load(lang: str) -> dict:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def _string_constants(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


# ---- Guard 1: register (tu, never usted) ---------------------------------------------------

_USTED_WORDS = (
    "usted", "ustedes",
    "ingrese", "seleccione", "haga", "escriba", "elija", "intente", "verifique", "revise",
    "toque", "pulse", "presione", "confirme", "agregue", "conecte", "descargue",
)
_USTED_WORD_PATTERN = re.compile(r"\b(?:" + "|".join(_USTED_WORDS) + r")\b", re.I)

# le/lo/la are mostly relative pronouns ("lo que...") and definite articles ("la hora...") in this
# app's Spanish, so a bare-word match would be noise (spec: cover them with specific phrases
# instead). None of these actually occur in es.json today; they are here so the guard would catch
# one if it appeared.
_USTED_PHRASES = ("le recomendamos", "le enviamos", "lo invitamos", "le pedimos", "le sugerimos")
_USTED_PHRASE_PATTERN = re.compile("|".join(re.escape(p) for p in _USTED_PHRASES), re.I)

# su / sus / tiene / puede addressed to the reader read as usted. Every real occurrence in es.json
# is listed here, each with the reason it is third person about a thing and not the reader.
_THIRD_PERSON_PATTERN = re.compile(r"\b(?:su|sus|tiene|puede)\b", re.I)

_THIRD_PERSON_ALLOW = {
    "how.explain": "su peso' refers to 'las demas partes' (the other score parts), not the reader",
    "soon.goals": "'su plan' refers to the goals themselves (their plan), not the reader",
    "goals.enlace.field_entrada": "'su numero' refers to 'el viaje' (the trip), not the reader",
    "settings.compartir.nota": "'su fecha' refers to 'el ultimo valor' (the shared figure), not the reader",
    "today.hogar.procedencia": "'su reloj' refers to {name}, a household member, not the reader",
    "goals.visibility_milestone_note": "'sus propios numeros' refers to 'cada quien' (each member), not the reader",
    "trends.tap_month": "'sus dias' refers to 'un mes' (the month), not the reader",
    "sin_acceso.text": "'tiene' subject is 'tu cuenta Duna' (the account), not a direct usted address",
    "goals.no_habits": "'tiene' subject is 'esta meta' (the goal), not the reader",
    "ajustes.asistente.opcion.completo.desc": "'puede' subject is 'el asistente', not the reader",
    "dispositivos.new_key_intro": "'puede' subject is 'Body', not the reader",
    "asistente.propuesta.sin_deshacer": "'se puede' is an impersonal construction, not usted",
}


class TestRegistroTuEsJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.es = _load("es")

    def _strings(self):
        return [(k, v) for k, v in self.es.items() if isinstance(v, str)]

    def test_no_usted_words_or_phrases(self):
        strings = self._strings()
        self.assertGreater(len(strings), 0, "no es.json strings were scanned")
        hits = [(k, v) for k, v in strings
                if _USTED_WORD_PATTERN.search(v) or _USTED_PHRASE_PATTERN.search(v)]
        self.assertEqual(hits, [], f"usted-register marker(s) found in es.json: {hits}")

    def test_su_sus_tiene_puede_are_all_allow_listed_third_person(self):
        strings = self._strings()
        self.assertGreater(len(strings), 0, "no es.json strings were scanned")
        hits = [(k, v) for k, v in strings
                if _THIRD_PERSON_PATTERN.search(v) and k not in _THIRD_PERSON_ALLOW]
        self.assertEqual(hits, [],
                          f"su/sus/tiene/puede addressed to the reader (not in the allow-list): {hits}")

    def test_allow_list_has_no_stale_entries(self):
        strings = dict(self._strings())
        matched = {k for k, v in strings.items() if _THIRD_PERSON_PATTERN.search(v)}
        stale = sorted(set(_THIRD_PERSON_ALLOW) - matched)
        self.assertEqual(stale, [], f"allow-listed key(s) no longer match su/sus/tiene/puede: {stale}")

    def test_usted_word_guard_bites_on_a_planted_formal_string(self):
        self.assertIsNotNone(_USTED_WORD_PATTERN.search("Ingrese su correo"))

    def test_usted_phrase_guard_bites_on_a_planted_formal_string(self):
        self.assertIsNotNone(_USTED_PHRASE_PATTERN.search("Le recomendamos revisar su conexion"))


# ---- Guard 2: no punctuation glued to a link -----------------------------------------------

# {url}, {link}, {enlace}, {href} or any {...url...}-shaped placeholder, immediately followed by
# one of .,;:)! -- a chat client can read that punctuation as part of the link.
_PLACEHOLDER_PUNCT_PATTERN = re.compile(r"\{[^{}]*(?:url|link|enlace|href)[^{}]*\}[.,;:)!]", re.I)
# A literal http(s):// token, immediately followed by one of .,;:)! that ends the token (the next
# character is whitespace, a quote, a closing bracket, or the end of the string) -- so a dot that
# is part of the link's own path (followed by more link) is not a false hit.
_URL_PUNCT_PATTERN = re.compile(r"https?://\S*?[.,;:)!](?=\s|[\"'\)\]]|\Z)", re.I)


def _link_punct_hit(text: str) -> bool:
    return bool(_PLACEHOLDER_PUNCT_PATTERN.search(text) or _URL_PUNCT_PATTERN.search(text))


def _agente_strings() -> dict:
    out = {}
    for path in sorted(AGENTE_DIR.glob("*.py")):
        if path.name == "cliente.py":  # Tier 1, never touched or scanned for a fix
            continue
        rel = str(path.relative_to(ROOT))
        for i, literal in enumerate(_string_constants(path)):
            out[f"{rel}:{i}"] = literal
    return out


class TestEnlaceSinPuntuacionPegada(unittest.TestCase):
    def test_no_punctuation_glued_to_a_link(self):
        strings = {}
        for lang in ("es", "en"):
            for key, value in _load(lang).items():
                if isinstance(value, str):
                    strings[f"{lang}.json:{key}"] = value
        strings.update(_agente_strings())
        self.assertGreater(len(strings), 0, "no strings were scanned")
        hits = {k: v for k, v in strings.items() if _link_punct_hit(v)}
        self.assertEqual(hits, {}, f"punctuation glued directly to a link: {hits}")

    def test_placeholder_guard_bites_on_a_planted_string(self):
        self.assertTrue(_link_punct_hit("Mira esto: {url}."))

    def test_raw_url_guard_bites_on_a_planted_string(self):
        self.assertTrue(_link_punct_hit("Ve a https://body.app."))

    def test_raw_url_guard_does_not_false_positive_on_an_internal_dot(self):
        self.assertFalse(_link_punct_hit("Ve a https://body.app/ruta para mas info"))


if __name__ == "__main__":
    unittest.main()
