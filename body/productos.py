"""BODY-COMIDA-PRODUCTO-1: the one module for products Body knows -- creating a manual one,
setting its nutrition (kept ONCE per product, never re-estimated per meal), listing and searching
what a person may see, and computing a meal line's own nutrient contribution. Pure functions over
a connection; no web imports. This slice's only `origen` is 'manual' (typed by hand); the despensa
read itself is BODY-COMIDA-PRODUCTO-2-DESPENSA -- every function here is already shaped for a
'despensa' product to flow through the same ownership scope (body/db.py's producto table, ruling 1
CORRECTED), through `household.household_of`, with no further change.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import datetime, timezone

from . import db, household

_UNIDAD_DE_BASIS = {"100g": "g", "100ml": "ml", "unidad": "unidad"}
FUENTES_NUTRICION = ("etiqueta", "consulta", "estimado")


class ProductoDesconocido(Exception):
    """A producto_id that does not exist, or that this person may not see (another person's own
    manual product, or another household's)."""

    def __init__(self, producto_id):
        self.producto_id = producto_id


class UnidadNoCoincide(Exception):
    """A meal line's own `unidad` does not match this product's own nutrition basis."""

    def __init__(self, nombre: str, esperada: str, recibida: str):
        self.nombre = nombre
        self.esperada = esperada
        self.recibida = recibida


class ProductoEnUso(Exception):
    """BODY-COMIDA-PRODUCTO-1-B: a manual product `crear_producto`'s own undo may not delete,
    because some meal still names it in `comida_producto` -- refused BY NAME."""

    def __init__(self, nombre: str):
        self.nombre = nombre


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalizar(texto: str) -> str:
    """Accent/case-insensitive compare key: strip combining marks (NFKD), casefold, trim."""
    sin_acentos = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return sin_acentos.casefold().strip()


def _nombre_mostrado(p: dict, lang: str) -> str:
    """This person's own-language name, falling back across es/en/the plain `nombre` (ruling 2's
    own ordering rule reuses this same fallback)."""
    if lang == "en":
        return p["nombre_en"] or p["nombre_es"] or p["nombre"]
    return p["nombre_es"] or p["nombre_en"] or p["nombre"]


def crear_producto_manual(conn: sqlite3.Connection, person_id: int, nombre: str, *,
                           marca: str | None = None, tamano_neto: float | None = None,
                           tamano_unidad: str | None = None) -> int:
    """Ruling 2: name required; brand, size and unit optional. `tamano_unidad`, when given, must be
    one of the three basis units -- the same set a meal line's own `unidad` is checked against."""
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("nombre: hace falta un nombre")
    if tamano_unidad is not None and tamano_unidad not in _UNIDAD_DE_BASIS.values():
        raise ValueError(f"tamano_unidad desconocida: {tamano_unidad!r}")
    if tamano_neto is not None and (isinstance(tamano_neto, bool) or not isinstance(tamano_neto, (int, float))
                                     or tamano_neto <= 0):
        raise ValueError("tamano_neto debe ser un número mayor que cero")
    now = _now()
    with conn:
        cur = conn.execute(
            "INSERT INTO producto (person_id, origen, nombre, marca, tamano_neto, tamano_unidad, "
            "created_at) VALUES (?, 'manual', ?, ?, ?, ?, ?)",
            (person_id, nombre, (marca or "").strip() or None,
             float(tamano_neto) if tamano_neto is not None else None, tamano_unidad, now),
        )
    return cur.lastrowid


def borrar_producto_manual(conn: sqlite3.Connection, person_id: int, producto_id: int) -> None:
    """BODY-COMIDA-PRODUCTO-1-B: `crear_producto`'s own undo -- deletes a manual product this
    person typed, refused (ProductoDesconocido) when it is not theirs, or (ProductoEnUso, by the
    product's own display name) once any meal already names it in `comida_producto`, the same
    "defense in depth" pair `db.borrar_experimento` uses for `experimento_registro`."""
    fila = conn.execute(
        "SELECT * FROM producto WHERE id = ? AND person_id = ? AND origen = 'manual'",
        (producto_id, person_id)).fetchone()
    if fila is None:
        raise ProductoDesconocido(producto_id)
    en_uso = conn.execute(
        "SELECT 1 FROM comida_producto WHERE producto_id = ? LIMIT 1", (producto_id,)
    ).fetchone()
    if en_uso is not None:
        lang = db.person(conn, person_id)["language"]
        raise ProductoEnUso(_nombre_mostrado(dict(fila), lang))
    with conn:
        conn.execute("DELETE FROM producto_nutricion WHERE producto_id = ?", (producto_id,))
        conn.execute("DELETE FROM producto WHERE id = ?", (producto_id,))


def lineas_de_comida(conn: sqlite3.Connection, comida_id: int) -> list[dict]:
    """This meal's own current product lines -- [{producto_id, cantidad, unidad}], empty for a
    meal that names none. Used to snapshot a correction's own `antes` state (body/agente/
    herramientas.py's `_corregir_comida`) so `propuestas.deshacer` can restore it exactly."""
    return [{"producto_id": r["producto_id"], "cantidad": r["cantidad"], "unidad": r["unidad"]}
            for r in conn.execute(
                "SELECT producto_id, cantidad, unidad FROM comida_producto WHERE comida_id = ? ORDER BY id",
                (comida_id,))]


def establecer_nutricion(conn: sqlite3.Connection, producto_id: int, metric_key: str, amount_per: float,
                          basis: str, fuente: str) -> None:
    """One figure, kept once: a later call for the same (producto_id, metric_key) REPLACES it.
    Every nutrition row of the SAME product must share the SAME basis (never enforced by the
    schema itself, since `basis` lives per row) -- so the picker's own portion field, and a meal
    line's own unit check, can read "the product's basis" off any one of its rows."""
    if metric_key not in db.INGESTA_METRIC_KEYS:
        raise ValueError(f"ingesta desconocida o no acumulable: {metric_key!r}")
    if basis not in _UNIDAD_DE_BASIS:
        raise ValueError(f"basis desconocida: {basis!r}")
    if fuente not in FUENTES_NUTRICION:
        raise ValueError(f"fuente desconocida: {fuente!r}")
    if isinstance(amount_per, bool) or not isinstance(amount_per, (int, float)) or amount_per <= 0:
        raise ValueError("amount_per debe ser un número mayor que cero")
    with conn:
        otra = conn.execute(
            "SELECT basis FROM producto_nutricion WHERE producto_id = ? AND metric_key != ? LIMIT 1",
            (producto_id, metric_key),
        ).fetchone()
        if otra is not None and otra["basis"] != basis:
            raise ValueError(f"basis no coincide con la del resto de este producto: {otra['basis']!r}")
        conn.execute(
            "INSERT INTO producto_nutricion (producto_id, metric_key, amount_per, basis, fuente) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(producto_id, metric_key) DO UPDATE SET "
            "amount_per = excluded.amount_per, basis = excluded.basis, fuente = excluded.fuente",
            (producto_id, metric_key, float(amount_per), basis, fuente),
        )


def productos_visibles(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Ruling 2: every product this person may see -- their own manual ones plus their active
    household's -- ordered recently-eaten-by-this-person first, then alphabetically by this
    person's own-language name. Every read of `producto` anywhere in this app goes through here."""
    lang = db.person(conn, person_id)["language"]
    hh = household.household_of(conn, person_id)
    if hh is not None:
        filas = conn.execute(
            "SELECT * FROM producto WHERE person_id = ? OR hogar_id = ? ORDER BY id",
            (person_id, hh.id),
        ).fetchall()
    else:
        filas = conn.execute("SELECT * FROM producto WHERE person_id = ? ORDER BY id", (person_id,)).fetchall()
    recientes = {r["producto_id"] for r in conn.execute(
        "SELECT DISTINCT cp.producto_id AS producto_id FROM comida_producto cp "
        "JOIN comida c ON c.id = cp.comida_id WHERE c.person_id = ?", (person_id,))}
    productos = []
    for p in filas:
        d = dict(p)
        d["nombre_mostrado"] = _nombre_mostrado(d, lang)
        productos.append(d)
    productos.sort(key=lambda p: (0 if p["id"] in recientes else 1, _normalizar(p["nombre_mostrado"])))
    return productos


def buscar(conn: sqlite3.Connection, person_id: int, texto: str) -> list[dict]:
    """Searches across nombre, nombre_es, nombre_en and alias, accent/case-insensitive, within this
    person's own visible products (never a raw SELECT -- always through `productos_visibles`)."""
    q = _normalizar(texto or "")
    if not q:
        return []
    out = []
    for p in productos_visibles(conn, person_id):
        alias = json.loads(p["alias"]) if p["alias"] else []
        campos = [p["nombre"], p["nombre_es"], p["nombre_en"], *alias]
        if any(q in _normalizar(campo) for campo in campos if campo):
            out.append(p)
    return out


def basis_de_producto(conn: sqlite3.Connection, producto_id: int) -> str | None:
    """The one basis every nutrition row of `producto_id` shares (None when it has none yet) --
    the picker's own portion-field unit pre-fill."""
    fila = conn.execute(
        "SELECT basis FROM producto_nutricion WHERE producto_id = ? LIMIT 1", (producto_id,)
    ).fetchone()
    return _UNIDAD_DE_BASIS[fila["basis"]] if fila is not None else None


def calcular_lineas(conn: sqlite3.Connection, person_id: int, lineas: list[dict]) -> tuple[list[dict], list[dict]]:
    """Ruling 3: `lineas` is [{producto_id, cantidad, unidad}] -- a product the person may not see,
    or a unit that does not match that product's own basis, is refused BY NAME (ProductoDesconocido/
    UnidadNoCoincide, named by producto_id/the product's own display name respectively -- callers
    translate to the person's language). Returns (cantidades, detalle):

    * cantidades: [{metric_key, amount, estimado}], one per nutrient ANY listed product
      contributes to, summed across every one of them (ruling 3's own "Body sums ... across the
      listed products"). `estimado` is False only when every contributing figure's own `fuente` is
      'etiqueta' -- a product with no figure for a nutrient contributes nothing to it at all,
      leaving that nutrient entirely to the caller's own free-text estimate, never forcing a zero.
    * detalle: the same lineas, resolved with each product's own display name and cast to
      (int, float, str) -- exactly the shape `comida_producto` stores, and what the picker/leer_dia
      show back to the person.
    """
    visibles = {p["id"]: p for p in productos_visibles(conn, person_id)}
    # `sumas` is a dict kept in first-seen order (never a set): the metric keys it collects become
    # `cantidades`' own row order below, and a `set`'s iteration order is hash-randomized per
    # process -- the one time that mattered was BODY-COMIDA-PRODUCTO-1's own demo seed, whose
    # two-separate-process determinism test caught it (two ingesta rows swapping id/order with no
    # change in content).
    sumas: dict[str, float] = {}
    todo_etiqueta: dict[str, bool] = {}
    detalle = []
    for linea in lineas:
        producto_id = linea.get("producto_id")
        producto = visibles.get(producto_id)
        if producto is None:
            raise ProductoDesconocido(producto_id)
        cantidad = linea.get("cantidad")
        if isinstance(cantidad, bool) or not isinstance(cantidad, (int, float)) or cantidad <= 0:
            raise ValueError(f"cantidad inválida para {producto['nombre_mostrado']!r}")
        unidad = linea.get("unidad")
        if unidad not in _UNIDAD_DE_BASIS.values():
            raise ValueError(f"unidad desconocida para {producto['nombre_mostrado']!r}: {unidad!r}")
        filas_nutricion = conn.execute(
            "SELECT metric_key, amount_per, basis, fuente FROM producto_nutricion WHERE producto_id = ?",
            (producto_id,)).fetchall()
        if filas_nutricion:
            unidad_esperada = _UNIDAD_DE_BASIS[filas_nutricion[0]["basis"]]
            if unidad != unidad_esperada:
                raise UnidadNoCoincide(producto["nombre_mostrado"], unidad_esperada, unidad)
        for fila in filas_nutricion:
            factor = cantidad / 100 if fila["basis"] in ("100g", "100ml") else cantidad
            sumas[fila["metric_key"]] = sumas.get(fila["metric_key"], 0.0) + fila["amount_per"] * factor
            todo_etiqueta[fila["metric_key"]] = todo_etiqueta.get(fila["metric_key"], True) and \
                (fila["fuente"] == "etiqueta")
        detalle.append({"producto_id": producto_id, "cantidad": float(cantidad), "unidad": unidad,
                         "nombre": producto["nombre_mostrado"]})
    cantidades = [{"metric_key": k, "amount": v, "estimado": not todo_etiqueta.get(k, False)}
                  for k, v in sumas.items()]
    return cantidades, detalle
