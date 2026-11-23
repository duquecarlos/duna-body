"""The post-session check-in's own default document (BODY-PARTIDO-2, ORCH-CARLOS-DELEGATES-
TIER-1-1): a short set of questions, chosen by the session's own kind, kept as DATA -- never a
branch on one sport in code. Pure, no web imports, no t(); body/rutas/entreno_sesion.py formats
with t() and renders.

Carlos, relayed by the desk 2026-11-22: "Is basketball improving my cardio, and what kind of game
and preparation makes me play best?" -- the watch measures the effort (BODY-PARTIDO-1); it cannot
know WHAT KIND of game it was or HOW he prepared. His direction the same day: the structure applies
to ANY sport, later to other people (BODY-PARTIDO-GENERAL).

Question KEYS form one shared bank across every sport ("sensacion", "calentamiento", "comida" ride
along in every specific set), so a later slice can compare preparation across sports by clave alone.
MATCHING is data too: each specific set lists the activity `type` codes it covers and, for Garmin's
own generic 'other' bucket, the folded names (entreno_sesion._normaliza_nombre) that name it --
basketball games are not a type code Garmin hands out, they land as type 'other' with a name like
"Baloncesto" (body/demo.py's own basketball session; body/reentrada.py's own folded-name table)."""
from __future__ import annotations

from . import entreno_sesion

KIND_UNICA = "opcion_unica"
KIND_ESCALA = "escala"
KIND_SI_NO = "si_no"

_SENSACION = {"clave": "sensacion", "tipo_pregunta": KIND_ESCALA,
              "texto": "checkin_sesion.pregunta.sensacion", "opciones": None}
_CALENTAMIENTO = {"clave": "calentamiento", "tipo_pregunta": KIND_SI_NO,
                   "texto": "checkin_sesion.pregunta.calentamiento", "opciones": None}
_COMIDA = {"clave": "comida", "tipo_pregunta": KIND_UNICA, "texto": "checkin_sesion.pregunta.comida",
           "opciones": [
               {"clave": "menos_1h", "texto": "checkin_sesion.opcion.comida.menos_1h"},
               {"clave": "1_3h", "texto": "checkin_sesion.opcion.comida.1_3h"},
               {"clave": "mas_3h", "texto": "checkin_sesion.opcion.comida.mas_3h"},
           ]}

# Ruling 2: the GENERIC set -- for any session that matches no specific set below.
GENERICO = (_SENSACION, _CALENTAMIENTO, _COMIDA)

_FORMATO = {"clave": "formato", "tipo_pregunta": KIND_UNICA, "texto": "checkin_sesion.pregunta.formato",
            "opciones": [
                {"clave": "uno_uno", "texto": "checkin_sesion.opcion.formato.uno_uno"},
                {"clave": "media_cancha", "texto": "checkin_sesion.opcion.formato.media_cancha"},
                {"clave": "cancha_completa", "texto": "checkin_sesion.opcion.formato.cancha_completa"},
                {"clave": "otro", "texto": "checkin_sesion.opcion.formato.otro"},
            ]}
_TERRENO = {"clave": "terreno", "tipo_pregunta": KIND_UNICA, "texto": "checkin_sesion.pregunta.terreno",
            "opciones": [
                {"clave": "calle", "texto": "checkin_sesion.opcion.terreno.calle"},
                {"clave": "montana", "texto": "checkin_sesion.opcion.terreno.montana"},
                {"clave": "pista", "texto": "checkin_sesion.opcion.terreno.pista"},
                {"clave": "caminadora", "texto": "checkin_sesion.opcion.terreno.caminadora"},
            ]}
_ENFOQUE = {"clave": "enfoque", "tipo_pregunta": KIND_UNICA, "texto": "checkin_sesion.pregunta.enfoque",
            "opciones": [
                {"clave": "superior", "texto": "checkin_sesion.opcion.enfoque.superior"},
                {"clave": "inferior", "texto": "checkin_sesion.opcion.enfoque.inferior"},
                {"clave": "completo", "texto": "checkin_sesion.opcion.enfoque.completo"},
                {"clave": "cardio", "texto": "checkin_sesion.opcion.enfoque.cardio"},
            ]}

# Running type codes body/history.py's own KNOWN_ACTIVITY_TYPES carries (census, not a guess).
_TIPOS_RUNNING = frozenset({"running", "trail_running", "treadmill_running", "track_running"})

# Each entry: the Garmin `type` codes this set covers, the folded 'other' names that also cover it
# (basketball is never a type code Garmin hands out -- it lands as 'other'), and the preguntas in
# display order. Checked in order; the first match wins.
_CONJUNTOS: tuple[dict, ...] = (
    {"tipos": frozenset({"basketball"}),
     "nombres_other": frozenset({"basketball", "baloncesto", "basquetbol"}),
     "preguntas": (_FORMATO, _SENSACION, _CALENTAMIENTO, _COMIDA)},
    {"tipos": _TIPOS_RUNNING, "nombres_other": frozenset(),
     "preguntas": (_TERRENO, _SENSACION, _CALENTAMIENTO, _COMIDA)},
    {"tipos": frozenset({"strength_training"}), "nombres_other": frozenset(),
     "preguntas": (_ENFOQUE, _SENSACION, _CALENTAMIENTO)},
)


def set_para(tipo: str | None, nombre: str | None) -> list[dict]:
    """Ruling 2: the matched default set for a session's own (tipo, nombre) -- a non-generic type
    matches by its CODE alone; Garmin's generic 'other' bucket matches by the folded name instead.
    GENERICO for anything that matches no specific set (ruling 2's own "for ANY session that
    matches no specific set")."""
    nombre_norm = entreno_sesion._normaliza_nombre(nombre)
    for c in _CONJUNTOS:
        if tipo in c["tipos"]:
            return list(c["preguntas"])
        if tipo == entreno_sesion.TIPO_GENERICO and nombre_norm in c["nombres_other"]:
            return list(c["preguntas"])
    return list(GENERICO)
