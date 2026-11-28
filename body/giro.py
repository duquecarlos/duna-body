"""Hoy's own image spin (BODY-CUERPO-SPIN-HOY-1), ported from Carlos's approved mock
(research/avatar-3d-mock/index.html v10). The figure is 16 painted views, 22.5 degrees apart, in
six layers (muscles, skeleton, heart, lungs, nerves, skin) -- art files under body/static/giro/,
copied in by this slice from design/giro/ (BODY-ARTE-FUERA-DEL-REPO-1). Pure data and pure
functions only: no database read, no request, no template -- body/rutas/hoy.py is the one caller,
and it passes in body.musculo.compute's own result (already computed once there), never a second
read of it.

Only the muscle layer is ever tinted (ruling 5); the other five layers render plain.
"""
from __future__ import annotations

from . import anatomia
from .i18n import T

N_VISTAS = 16

# The mock's own `REGION` array, index 0 kept as None exactly as the mock keeps it (a mask pixel's
# grey value, divided by 10, is the index into this list -- research/avatar-giro/regiones_render.py
# writes the masks this way, research/avatar-giro/regiones_ajustar.py fits them to each frame).
# Index 0 names no muscle at all.
GIRO_REGIONES: tuple[str | None, ...] = (
    None, "pecho", "hombros", "deltoides_post", "biceps", "triceps", "antebrazos", "abdomen",
    "oblicuos", "trapecio", "cuello", "espalda_alta", "dorsales", "lumbares", "gluteos",
    "cuadriceps", "aductores", "isquios", "gemelos", "tibiales",
)

# The 10 of GIRO_REGIONES' 19 muscles body.musculo.compute actually tracks (musculo-v1) -- the
# same `anatomia.REGIONES` muscle claves, read from there rather than retyped (ruling 5 / ruling 9:
# "a region musculo-v1 does not track"). The other 9 get "no_medido" below.
_MODELADAS: frozenset[str] = frozenset(r["clave"] for r in anatomia.REGIONES if r["tipo"] == "musculo")

# The six layers, file-prefix order -- the muscle layer's own file/data-capa prefix is "giro" (the
# mock's own choice, kept), not "musculos"; the i18n key suffix for its label is "musculos" (ruling 7).
CAPAS: tuple[str, ...] = ("giro", "esqueleto", "corazon", "pulmones", "nervios", "piel")
_CAPA_CLAVE_I18N: dict[str, str] = {
    "giro": "musculos", "esqueleto": "esqueleto", "corazon": "corazon", "pulmones": "pulmones",
    "nervios": "nervios", "piel": "piel",
}

# The mock's own `ESTADO` rgb values (ruling 5), plus `no_medido` -- both untinted, same as
# `sin_datos` (a region with no colour draws plain, same as a layer that is never tinted at all).
COLORES: dict[str, tuple[int, int, int] | None] = {
    "en_rango": (120, 190, 120),
    "recuperando": (120, 165, 230),
    "subentrenado": (240, 200, 110),
    "sobrecargado": (235, 120, 100),
    "sin_datos": None,
    "no_medido": None,
}

# Ruling 4's three jump views -- the mock's own data-ir frame indices.
VISTAS_SALTO: tuple[tuple[str, int], ...] = (("frente", 0), ("lado", 4), ("espalda", 8))

# aria-valuetext per frame (0-15): each frame names the nearest of the three jump views above, by
# circular distance -- "lado" covers both the mock's left and right thirds, since ruling 7 gives
# this slice only one word for "side" (never the mock's own per-frame English sentences, which
# ruling 7 does not approve). Computed once, not hand-typed, so N_VISTAS staying 16 is the only
# thing that could ever change it.
_CARDINAL_INDICES: tuple[int, ...] = tuple(i for _, i in VISTAS_SALTO) + (12,)
_CARDINAL_NOMBRE: dict[int, str] = {0: "frente", 4: "lado", 8: "espalda", 12: "lado"}


def _vista_mas_cercana(indice: int) -> str:
    mejor = min(_CARDINAL_INDICES, key=lambda c: min(abs(indice - c), N_VISTAS - abs(indice - c)))
    return _CARDINAL_NOMBRE[mejor]


def construir_estados(estados_modelo: dict[str, str]) -> dict[str, str]:
    """Ruling 5: one state per GIRO_REGIONES muscle (18 of them -- index 0 is skipped, it names
    none) -- musculo.compute's own word for the 10 it tracks (`estados_modelo`, the SAME dict
    body/rutas/hoy.py already built once for the 2D view, never a second `musculo.compute` call),
    "no_medido" for the other 9. Never "sin_datos" for an untracked region -- that would read as
    "Body measured this and found nothing", which is false; "no_medido" says Body does not measure
    it yet."""
    return {clave: (estados_modelo.get(clave, "sin_datos") if clave in _MODELADAS else "no_medido")
            for clave in GIRO_REGIONES if clave is not None}


def nombre_cuadro(capa: str, indice: int) -> str:
    return f"{capa}-{indice:02d}.webp"


def nombre_mascara(indice: int) -> str:
    return f"giro-regiones-{indice:02d}.png"


def etiquetas_regiones(t: T) -> dict[str, str]:
    """Ruling 7: reuse the existing `cuerpo.grupo.<clave>` keys for the 10 muscles the 2D caption
    already names; the other 9 get this slice's own new `cuerpo.giro.region.<clave>` keys. Every
    call here passes a literal string straight to `t(...)` (tests/test_i18n_paridad.py's dynamic-
    prefix guard needs each one typed out, not built from `clave` at runtime)."""
    return {
        "pecho": t("cuerpo.grupo.pecho"),
        "hombros": t("cuerpo.grupo.hombros"),
        "deltoides_post": t("cuerpo.grupo.deltoides_post"),
        "biceps": t("cuerpo.grupo.biceps"),
        "triceps": t("cuerpo.giro.region.triceps"),
        "antebrazos": t("cuerpo.giro.region.antebrazos"),
        "abdomen": t("cuerpo.grupo.abdomen"),
        "oblicuos": t("cuerpo.giro.region.oblicuos"),
        "trapecio": t("cuerpo.giro.region.trapecio"),
        "cuello": t("cuerpo.giro.region.cuello"),
        "espalda_alta": t("cuerpo.grupo.espalda_alta"),
        "dorsales": t("cuerpo.grupo.dorsales"),
        "lumbares": t("cuerpo.giro.region.lumbares"),
        "gluteos": t("cuerpo.grupo.gluteos"),
        "cuadriceps": t("cuerpo.grupo.cuadriceps"),
        "aductores": t("cuerpo.giro.region.aductores"),
        "isquios": t("cuerpo.grupo.isquios"),
        "gemelos": t("cuerpo.giro.region.gemelos"),
        "tibiales": t("cuerpo.giro.region.tibiales"),
    }


def etiquetas_estado(t: T) -> dict[str, str]:
    """Ruling 7's new state words -- the legend's and the tap line's own text, one literal `t()`
    call each, same reason as `etiquetas_regiones` above."""
    return {
        "en_rango": t("cuerpo.giro.estado.en_rango"),
        "recuperando": t("cuerpo.giro.estado.recuperando"),
        "subentrenado": t("cuerpo.giro.estado.subentrenado"),
        "sobrecargado": t("cuerpo.giro.estado.sobrecargado"),
        "sin_datos": t("cuerpo.giro.estado.sin_datos"),
        "no_medido": t("cuerpo.giro.estado.no_medido"),
    }


def etiquetas_capas(t: T) -> dict[str, str]:
    return {
        "giro": t("cuerpo.giro.capa.musculos"),
        "esqueleto": t("cuerpo.giro.capa.esqueleto"),
        "corazon": t("cuerpo.giro.capa.corazon"),
        "pulmones": t("cuerpo.giro.capa.pulmones"),
        "nervios": t("cuerpo.giro.capa.nervios"),
        "piel": t("cuerpo.giro.capa.piel"),
    }


def etiqueta_vista(t: T, nombre: str) -> str:
    if nombre == "frente":
        return t("cuerpo.giro.vista.frente")
    if nombre == "lado":
        return t("cuerpo.giro.vista.lado")
    return t("cuerpo.giro.vista.espalda")


def vistas_texto(t: T) -> list[str]:
    """One aria-valuetext per of the 16 frames -- the JS slider reads this array by frame index,
    the same shape the mock's own `NOMBRES` array has, built from this slice's own 3 view words
    instead of the mock's 16 invented English sentences (ruling 7 approves no such text)."""
    return [etiqueta_vista(t, _vista_mas_cercana(i)) for i in range(N_VISTAS)]
