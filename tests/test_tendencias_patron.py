"""BODY-TENDENCIAS-LEYENDA-PATRON-1 ruling 1: turnos_costo.frase_patron turns a successful
perfil_por_dia result into a sentence SPEC (a kind plus its own parameters), never prose, on
invented perfil-shaped dicts -- the function only reads the shape perfil_por_dia already
documents, never a real database.
"""
from tests import _isolate  # noqa: F401,E402

import unittest

from body import turnos_costo

_BANDA = {"bajo": -5.0, "alto": 5.0}


def _perfil(metric: str, offsets: dict[str, float], peor: str, vuelta: str | None,
            banda: dict = _BANDA) -> dict:
    """A perfil_por_dia-shaped dict: `offsets` maps an offset name to its own 'media' (the only
    figure frase_patron reads), `peor` names the key ('mas_bajo'/'mas_alto' decided by the
    metric's own direction, same as perfil_por_dia itself)."""
    peor_es_bajo = turnos_costo._BETTER.get(metric) == "higher"
    return {
        "metric": metric, "motivo": None, "centro": 40.0, "banda": banda,
        "offsets": [{"offset": o, "media": m, "n": 5} for o, m in offsets.items()],
        ("mas_bajo" if peor_es_bajo else "mas_alto"): peor,
        "vuelta": vuelta, "noches_tipicas": 2, "n_bloques": 5,
    }


class FrasePatronTest(unittest.TestCase):
    def test_estable_when_the_worst_offset_sits_inside_the_band(self):
        perfil = _perfil("hrv_last_night", {"noche_1": -3.0}, "noche_1", None)
        self.assertEqual(turnos_costo.frase_patron(perfil, "hrv_last_night"), {"kind": "estable"})

    def test_vuelve_baja_for_a_higher_is_better_metric(self):
        # hrv_last_night: higher is better -> its worse direction is low (below centre, "baja").
        perfil = _perfil("hrv_last_night", {"noche_1": -12.0, "post_1": -1.0}, "noche_1", "post_1")
        self.assertEqual(turnos_costo.frase_patron(perfil, "hrv_last_night"),
                          {"kind": "vuelve", "peor": "noche_1", "vuelta": "post_1", "direccion": "baja"})

    def test_vuelve_sube_for_a_lower_is_better_metric(self):
        # resting_hr: lower is better -> its worse direction is high (above centre, "sube").
        perfil = _perfil("resting_hr", {"noche_1": 8.0, "post_1": 1.0}, "noche_1", "post_1")
        self.assertEqual(turnos_costo.frase_patron(perfil, "resting_hr"),
                          {"kind": "vuelve", "peor": "noche_1", "vuelta": "post_1", "direccion": "sube"})

    def test_no_vuelve_when_outside_the_band_and_vuelta_is_none(self):
        perfil = _perfil("hrv_last_night", {"noche_1": -12.0}, "noche_1", None)
        self.assertEqual(turnos_costo.frase_patron(perfil, "hrv_last_night"),
                          {"kind": "no_vuelve", "peor": "noche_1", "direccion": "baja"})

    def test_motivo_set_is_none(self):
        self.assertIsNone(turnos_costo.frase_patron({"metric": "hrv_last_night",
                                                      "motivo": "pocos_bloques_previos"},
                                                     "hrv_last_night"))

    def test_spec_carries_no_number_from_the_persons_data(self):
        """peor/vuelta are offset NAMES and direccion is a word -- no mean, no ms, nothing a log
        could read as a health figure (the spec's own privacy rule)."""
        perfil = _perfil("hrv_last_night", {"noche_1": -12.0, "post_1": -1.0}, "noche_1", "post_1")
        frase = turnos_costo.frase_patron(perfil, "hrv_last_night")
        for valor in frase.values():
            self.assertIsInstance(valor, str)
        self.assertEqual(set(frase), {"kind", "peor", "vuelta", "direccion"})


if __name__ == "__main__":
    unittest.main()
