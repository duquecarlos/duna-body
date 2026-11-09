"""BODY-PROPUESTAS-TABLA-1: the guard that a ninth proposal action cannot be half-added. Three
places must name the exact same set of actions -- body/agente/propuestas.py's TABLA, body/rutas/
asistente.py's TARJETAS, and the `propuesta`-kind tools in body/agente/herramientas.py's
HERRAMIENTAS -- and every TABLA entry must carry all four of its callables, with "no undo writer"
limited to exactly the old SIN_DESHACER set.
"""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body.agente import herramientas, propuestas
from body.rutas import asistente


def _herramientas_propuesta() -> set:
    return {h["nombre"] for h in herramientas.HERRAMIENTAS if h["tipo"] == "propuesta"}


class PropuestasTablaTests(unittest.TestCase):
    def test_tabla_tarjetas_y_herramientas_nombran_el_mismo_conjunto(self):
        nombres_herramientas = _herramientas_propuesta()
        self.assertTrue(nombres_herramientas)
        self.assertEqual(set(propuestas.TABLA.keys()), nombres_herramientas)
        self.assertEqual(set(asistente.TARJETAS.keys()), nombres_herramientas)

    def test_cada_entrada_de_tabla_trae_sus_cuatro_funciones(self):
        for nombre, entrada in propuestas.TABLA.items():
            with self.subTest(accion=nombre):
                self.assertTrue(callable(entrada.drift_aplicar))
                self.assertTrue(callable(entrada.drift_deshacer))
                self.assertTrue(callable(entrada.escribir_aplicacion))
                self.assertTrue(callable(entrada.escribir_deshacer))

    def test_sin_deshacer_es_exactamente_las_entradas_sin_escritor_de_deshacer(self):
        sin_deshacer = {nombre for nombre, entrada in propuestas.TABLA.items() if not entrada.tiene_deshacer}
        self.assertEqual(sin_deshacer, set(propuestas.SIN_DESHACER))
        self.assertEqual(sin_deshacer, {"proponer_confirmar_turnos"})

    def test_cada_tarjeta_es_invocable(self):
        for nombre, constructor in asistente.TARJETAS.items():
            with self.subTest(accion=nombre):
                self.assertTrue(callable(constructor))


if __name__ == "__main__":
    unittest.main()
